"""Reading generation retries must not duplicate delivery or advance failed cursors."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import APITimeoutError, APIConnectionError, APIStatusError

import bot
import paper_reader
import reading_bridge
from reading_sessions import ReadingSessions, PriorityGate


def timeout_error():
    return APITimeoutError(request=httpx.Request('POST', 'https://example.com/PRIVATE_TOKEN'))


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = self.enterContext(patch.object(bot, 'log'))
        self.sleep = self.enterContext(patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock))

    async def test_timeout_and_connection_failures_retry_once_without_exposing_secrets(self):
        errors = (timeout_error(), APIConnectionError(request=httpx.Request('POST', 'https://example.com/PRIVATE_TOKEN'),
                   message='PRIVATE_RESPONSE_BODY'), asyncio.TimeoutError('PRIVATE_ERROR'))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                request = AsyncMock(side_effect=[error, 'success'])
                self.assertEqual(await bot._paper_model_call(request, stage='测试阅读'), 'success')
                self.assertEqual(request.await_count, 2)
                self.assertEqual(request.await_args_list[0].args, (145.0,))
                self.assertIn(type(error).__name__, str(self.log.call_args_list))
        self.assertNotIn('PRIVATE_', str(self.log.call_args_list))
        self.assertTrue(all(call.args == (5.0,) for call in self.sleep.await_args_list))

    async def test_second_failure_propagates_without_a_third_attempt(self):
        request = AsyncMock(side_effect=[timeout_error(), timeout_error()])
        with self.assertRaises(APITimeoutError):
            await bot._paper_model_call(request, stage='测试阅读')
        self.assertEqual(request.await_count, 2)
        self.sleep.assert_awaited_once_with(5.0)
        self.assertIn('retry=False', str(self.log.call_args_list))

    async def test_status_errors_and_invalid_results_are_not_network_retries(self):
        for status in (400, 401, 429, 500):
            response = httpx.Response(status, request=httpx.Request('POST', 'https://example.com'))
            error = APIStatusError('PRIVATE_RESPONSE_BODY', response=response, body=None)
            request = AsyncMock(side_effect=error)
            with self.assertRaises(APIStatusError):
                await bot._paper_model_call(request, stage='测试阅读')
            request.assert_awaited_once()
        request = AsyncMock(side_effect=ValueError('PRIVATE_PARSE_ERROR'))
        with self.assertRaises(ValueError):
            await bot._paper_model_call(request, stage='测试阅读')
        request.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.assertNotIn('PRIVATE_', str(self.log.call_args_list))

    async def test_cancellation_is_never_retried(self):
        request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await bot._paper_model_call(request, stage='测试阅读')
        request.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_cancel_during_retry_wait_never_starts_second_request(self):
        self.sleep.side_effect = asyncio.CancelledError()
        request = AsyncMock(side_effect=timeout_error())
        with self.assertRaises(asyncio.CancelledError):
            await bot._paper_model_call(request, stage='测试阅读')
        request.assert_awaited_once()

    async def test_context_change_before_retry_discards_stale_work(self):
        request = AsyncMock(side_effect=timeout_error())
        current = iter([True, False])
        with self.assertRaisesRegex(paper_reader.PaperError, '已停止旧请求'):
            await bot._paper_model_call(request, stage='测试阅读', is_current=lambda: next(current))
        request.assert_awaited_once()
        self.assertIn('context_changed', str(self.log.call_args_list))

    async def test_overall_budget_cancels_even_a_slow_retry_wait(self):
        blocked = asyncio.Event()
        async def wait(_):
            await blocked.wait()
        self.sleep.side_effect = wait
        request = AsyncMock(side_effect=timeout_error())
        with patch.object(reading_bridge, 'READING_RETRY_DELAY', 0.001):
            with self.assertRaises(asyncio.TimeoutError):
                await bot._paper_model_call(request, stage='测试阅读', attempt_timeout=0.02, total_timeout=0.04)
        request.assert_awaited_once()

    async def test_total_deadline_stops_two_hanging_attempts(self):
        async def hang(timeout):
            await asyncio.Event().wait()
        request = AsyncMock(side_effect=hang)
        with patch.object(reading_bridge, 'READING_RETRY_DELAY', 0.001):
            with self.assertRaises(asyncio.TimeoutError):
                await bot._paper_model_call(request, stage='测试阅读', attempt_timeout=0.08, total_timeout=0.14)
        self.assertEqual(request.await_count, 2)
        self.assertLessEqual(request.await_args_list[1].args[0], 0.08)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(bot, 'BASE_DIR', self.root))
        self.config = {'deepseek': {'enabled': True}}
        self.doc = {'title': 'Retry.pdf', 'kind': 'PDF文字', 'total': 158,
                    'units': [{'label': '第100页', 'text': 'Synthetic text ' * 10}], 'truncated': True}
        self.api = AsyncMock()
        self.options = self.enterContext(patch.object(bot, 'deepseek_client', SimpleNamespace(
            with_options=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.api))))))
        self.log = self.enterContext(patch.object(bot, 'log'))
        self.enterContext(patch.object(reading_bridge, 'READING_RETRY_DELAY', 0.001))
        for item in (patch.object(bot, '_init_deepseek'), patch.object(bot, 'load_config', return_value=self.config),
                     patch.object(bot, '_persona_prompt', return_value='fixture persona'),
                     patch.object(bot, '_speaker_context', return_value=''),
                     patch.object(bot, '_paper_model_route', return_value='same route'),
                     patch.object(bot, '_reading_signature', return_value='same signature'),
                     patch.object(bot, '_paper_allowed', return_value=True)):
            self.enterContext(item)

    @staticmethod
    def response(text):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=text))])

    async def test_manual_retry_publishes_answer_once_and_marks_only_success(self):
        self.api.side_effect = [timeout_error(), self.response('本次真实阅读结论。')]
        library = paper_reader.PaperLibrary()
        reviewed = SimpleNamespace(storage_ok=True, mark_reviewed=lambda *args: None)
        with patch.object(bot, '_get_paper_library', return_value=library), \
             patch.object(bot, '_get_paper_autonomy', return_value=reviewed), \
             patch.object(bot, '_download_group_paper', AsyncMock(return_value=self.doc)), \
             patch.object(bot, 'send_group_message', AsyncMock(return_value=True)) as send:
            await bot._run_paper_read(123, '456', '', self.config, selected_info={'name': 'Retry.pdf'})
        self.assertEqual(self.api.await_count, 2)
        answers = [call for call in send.await_args_list if '本次真实阅读结论' in call.args[1]]
        self.assertEqual(len(answers), 1)

    async def test_selection_retry_is_inside_extended_choice_stage(self):
        self.api.side_effect = [timeout_error(), self.response('{"index":0,"reply":"我想先读Retry.pdf"}')]
        result = await bot._choose_paper([{'name': 'Retry.pdf', 'size': 100}], '', self.config)
        self.assertEqual(result['name'], 'Retry.pdf')
        self.assertEqual(self.api.await_count, 2)
        self.assertIn('stage=文献挑选模型', str(self.log.call_args_list))

    async def test_background_retry_commits_once_resets_failure_count_and_survives_restart(self):
        now = [100000.0]
        store = ReadingSessions(self.root, clock=lambda: now[0])
        gate = PriorityGate(clock=lambda: now[0])
        sha = store.cache_pdf(b'%PDF-test-retry')
        self.doc['_reading_sha'] = sha
        job = store.start('group_123_456', self.doc, 'previous reflection')
        job['errors'] = 2  # One successful step must clear the previous failures.
        store.save()
        now[0] += 31
        self.api.side_effect = [timeout_error(), self.response(json.dumps(
            {'note': '第101至158页的真实测试笔记', 'reflection': '累计感想', 'memory': '精简记忆'}))]
        extract = AsyncMock(return_value={'start': 101, 'end': 158, 'total': 158, 'units': [
            {'label': '第101页', 'text': 'Synthetic later range text ' * 10}], 'empty': [],
            'kind': 'PDF文字', 'truncated': True})
        publish = AsyncMock(return_value=True)
        await store.tick(gate, lambda: True, extract, paper_reader.build_context,
            lambda job, payload, retained: reading_bridge.generate(bot, job, payload, retained), publish)
        self.assertEqual((job['next_sample'], job['errors'], job['active']), (159, 0, False))
        self.assertEqual(self.api.await_count, 2)
        publish.assert_awaited_once()
        reboot = ReadingSessions(self.root, clock=lambda: now[0])
        self.assertEqual(reboot.current('group_123_456')['next_sample'], 159)

    async def test_failed_retry_counts_one_step_and_preserves_cursor_and_notes(self):
        now = [100000.0]
        store = ReadingSessions(self.root, clock=lambda: now[0])
        gate = PriorityGate(clock=lambda: now[0])
        self.doc['_reading_sha'] = store.cache_pdf(b'%PDF-test-failure')
        job = store.start('group_123_456', self.doc, 'previous reflection')
        now[0] += 31
        self.api.side_effect = [timeout_error(), timeout_error()]
        extract = AsyncMock(return_value={'start': 101, 'end': 158, 'total': 158, 'units': [
            {'label': '第101页', 'text': 'Synthetic text ' * 10}], 'empty': [], 'kind': 'PDF文字', 'truncated': True})
        publish = AsyncMock()
        await store.tick(gate, lambda: True, extract, paper_reader.build_context,
            lambda job, payload, retained: reading_bridge.generate(bot, job, payload, retained), publish)
        self.assertEqual((job['next_sample'], job['errors'], job['reflection']), (101, 1, 'previous reflection'))
        self.assertEqual(self.api.await_count, 2)
        publish.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
