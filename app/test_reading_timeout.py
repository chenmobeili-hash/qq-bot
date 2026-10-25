"""Scaled-time regression: slow reading must outlive the old SDK/outer limits."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import paper_reader
import reading_bridge


class ReadingTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(bot, 'BASE_DIR', Path(temporary.name)))
        self.config = {'deepseek': {'enabled': True}}
        self.doc = {'title': 'Slow test.pdf', 'kind': 'PDF文字', 'total': 1, 'truncated': False,
                    'units': [{'label': '第1页', 'text': 'Synthetic test document. ' * 8}]}
        self.sdk_options, self.budgets = [], []
        self.delay = 130  # Exceeds old 90/120s limits, fits the new 145s attempt budget.
        self.response = '基于真实测试摘录的回答[第1页]。'
        self.real_wait = asyncio.wait_for
        self.scale = 0.01

        async def scaled_wait(operation, timeout=None):
            self.budgets.append(timeout)
            return await self.real_wait(operation, timeout * self.scale if timeout is not None else None)

        def with_options(**options):
            self.sdk_options.append(options)
            # The shared SDK client still defaults to 90s. Only a per-call override avoids it.
            async def create(**request):
                async def complete():
                    await asyncio.sleep(self.delay * self.scale)
                    return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                        message=SimpleNamespace(content=self.response))])
                return await self.real_wait(complete(), options.get('timeout', 90) * self.scale)
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        client = SimpleNamespace(with_options=Mock(side_effect=with_options))
        for item in (patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client),
                     patch.object(bot, 'current_model', 'fixture-model'),
                     patch.object(bot, 'load_config', return_value=self.config),
                     patch.object(bot, '_persona_prompt', return_value='fixture persona'),
                     patch.object(bot, '_speaker_context', return_value=''),
                     patch.object(bot, '_paper_model_route', return_value='same route'),
                     patch.object(bot, '_reading_signature', return_value='same signature'),
                     patch.object(bot, '_paper_allowed', return_value=True),
                     patch.object(bot, 'log'), patch.object(asyncio, 'wait_for', side_effect=scaled_wait)):
            self.enterContext(item)

    def assert_model_budget(self):
        self.assertEqual(self.sdk_options[-1], {'max_retries': 0, 'timeout': 145.0})
        self.assertIn(300.0, self.budgets)

    async def test_slow_manual_read_survives_both_sdk_and_stage_wrappers(self):
        library = paper_reader.PaperLibrary()
        with patch.object(bot, '_get_paper_library', return_value=library), \
             patch.object(bot, '_get_paper_autonomy', return_value=SimpleNamespace(storage_ok=False)), \
             patch.object(bot, '_download_group_paper', AsyncMock(return_value=self.doc)), \
             patch.object(bot, 'send_group_message', AsyncMock(return_value=True)) as send:
            await bot._run_paper_read(123, '456', '', self.config, selected_info={'name': 'Slow test.pdf'})
        self.assertIn('基于真实测试摘录', send.await_args.args[1])
        self.assertIn(305.0, self.budgets)
        self.assert_model_budget()

    async def test_slow_document_followup_uses_same_extended_budget(self):
        library = paper_reader.PaperLibrary()
        library.store_doc(('123', '456'), self.doc)
        with patch.object(bot, '_get_paper_library', return_value=library), \
             patch.object(bot, '_get_paper_autonomy', return_value=SimpleNamespace(storage_ok=False)), \
             patch.object(bot, 'send_group_message', AsyncMock(return_value=True)) as send:
            await bot._run_paper_read(123, '456', '解释这一页', self.config, followup=True)
        self.assertIn('基于真实测试摘录', send.await_args.args[1])
        self.assertIn(305.0, self.budgets)
        self.assert_model_budget()

    async def test_slow_autonomous_read_survives_outer_stage(self):
        result = await bot._review_autonomous_paper(self.doc, self.config)
        self.assertIn('基于真实测试摘录', result)
        self.assertIn(305.0, self.budgets)
        self.assert_model_budget()

    async def test_background_sample_and_deep_steps_survive_old_120s_limit(self):
        self.response = json.dumps({'note': '精确笔记', 'reflection': '读后感', 'memory': '保留记忆'})
        job = {'scope': 'group_123_456', 'title': 'Slow test.pdf', 'reflection': '', 'memory': ''}
        for mode in ('sample', 'deep'):
            with self.subTest(mode=mode):
                result = await reading_bridge.generate(bot, job, {'mode': mode, 'text': 'fixture text'}, {})
                self.assertEqual(result['reflection'], '读后感')
                self.assert_model_budget()

    async def test_over_300_seconds_still_times_out(self):
        self.delay = 350
        with self.assertRaises(paper_reader.PaperError):
            await bot._review_autonomous_paper(self.doc, self.config)
        self.assert_model_budget()


if __name__ == '__main__':
    unittest.main()
