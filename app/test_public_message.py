import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
from model_decision import DecisionError, DecisionTransport
from proactive import ProactiveMode
import public_message as public


EN_STYLE = "I need to stick to the character's style here—first person, no action descriptions, no addressing by name."
EN_DECIDE = 'This is just a group management bot broadcasting an @all message, not something directed at me or inviting a real response.'
ZH_STYLE = '我需要遵循当前角色设定，再判断是否接话。'


def envelope(text='晚风很温柔，我也想把这份轻松分给大家。', kind='send'):
    return json.dumps({'kind': kind, 'reply': text}, ensure_ascii=False)


def response(text, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=text, reasoning_content='must never be delivered'), finish_reason=finish)])


def client_for(create):
    return SimpleNamespace(base_url='https://example.com/v1',
                           chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class ProtocolTests(unittest.TestCase):
    def test_only_final_field_is_used_and_english_itself_is_allowed(self):
        self.assertEqual(public.parse(envelope('Good evening, everyone.'), 'stop', 80)[0], 'Good evening, everyone.')

    def test_preface_and_fence_are_removed_without_salvaging_partial_inner_json(self):
        text = EN_STYLE + '\n```json\n' + envelope('晚上好。') + '\n```'
        self.assertEqual(public.parse(text, 'stop', 80), ('晚上好。', True))
        with self.assertRaises(DecisionError):
            public.parse('{"broken":' + envelope('晚上好。'), 'stop', 80)

    def test_raw_or_structured_meta_is_rejected_in_both_languages(self):
        for text in (EN_STYLE, EN_DECIDE, ZH_STYLE, '<think>检查人设</think>晚上好。', '分析过程：我先判断风格。'):
            for raw in (text, envelope(text)):
                with self.subTest(raw=raw), self.assertRaises(DecisionError):
                    public.parse(raw, 'stop', 1000)

    def test_bad_envelopes_actions_memories_duplicates_and_suffix_are_rejected(self):
        cases = [envelope('', 'send'), envelope('不能有正文', 'silent'), envelope('hi', 'chat'),
                 '{"kind":"send","kind":"silent","reply":"hi"}',
                 envelope('hi') + envelope('another'), envelope('hi') + '\nextra',
                 json.dumps({'kind': 'send', 'reply': 'hi', 'action': None}),
                 json.dumps({'kind': 'send', 'reply': 'hi', 'memory': []}),
                 json.dumps({'kind': 'send', 'reply': 7}), '[{"kind":"send","reply":"hi"}]']
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(DecisionError):
                public.parse(raw, 'stop', 80)

    def test_silent_and_length_limit_are_strict_not_truncation(self):
        self.assertEqual(public.parse(envelope('', 'silent'), 'stop', 80)[0], None)
        self.assertEqual(len(public.parse(envelope('好' * 80), 'stop', 80)[0]), 80)
        for raw, finish in ((envelope('好' * 81), 'stop'), (envelope('完整。'), 'length'),
                            (envelope('完整。'), 'content_filter'), (None, 'stop')):
            with self.assertRaises(DecisionError):
                public.parse(raw, finish, 80)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def run_generation(self, create, timeout=1):
        self.logs = Mock()
        return await public.generate(client_for(create), 'fixture', [
            {'role': 'system', 'content': '当前角色。'}, {'role': 'user', 'content': '问候任务'}
        ], DecisionTransport(), self.logs, limit=80, timeout=timeout, source='test')

    async def test_raw_report_then_valid_json_retries_once_and_never_echoes_raw(self):
        create = AsyncMock(side_effect=[response(EN_STYLE), response(envelope('傍晚好。'))])
        self.assertEqual(await self.run_generation(create), '傍晚好。')
        self.assertEqual(create.await_count, 2)
        self.assertNotIn(EN_STYLE, repr(self.logs.call_args_list))
        self.assertNotIn(EN_STYLE, repr(create.call_args_list))
        self.assertEqual(create.call_args_list[0].kwargs['response_format'], {'type': 'json_object'})

    async def test_persistent_raw_and_embedded_meta_return_none(self):
        for text in (EN_DECIDE, envelope(ZH_STYLE), envelope('长' * 81)):
            create = AsyncMock(return_value=response(text))
            self.assertIsNone(await self.run_generation(create))
            self.assertEqual(create.await_count, 2)

    async def test_silent_does_not_retry(self):
        create = AsyncMock(return_value=response(envelope('', 'silent')))
        self.assertIsNone(await self.run_generation(create))
        self.assertEqual(create.await_count, 1)

    async def test_length_finish_retries_with_larger_budget(self):
        create = AsyncMock(side_effect=[response(None, 'length'), response(envelope('晚上好。'))])
        self.assertEqual(await self.run_generation(create), '晚上好。')
        self.assertEqual([c.kwargs['max_tokens'] for c in create.call_args_list], [2000, 4000])

    async def test_network_error_is_not_retried_or_logged_with_response_body(self):
        create = AsyncMock(side_effect=ConnectionError('PRIVATE SERVICE BODY'))
        self.assertIsNone(await self.run_generation(create))
        self.assertEqual(create.await_count, 1)
        self.assertNotIn('PRIVATE SERVICE BODY', repr(self.logs.call_args_list))

    async def test_one_deadline_covers_both_attempts(self):
        async def slow(**kwargs):
            if create.await_count == 1:
                await asyncio.sleep(.2)
                return response(EN_STYLE)
            await asyncio.sleep(10)
        create = AsyncMock(side_effect=slow)
        self.assertIsNone(await self.run_generation(create, timeout=.3))
        self.assertEqual(create.await_count, 2)

    async def test_cancellation_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_generation(AsyncMock(side_effect=asyncio.CancelledError()))

    async def test_unsupported_json_mode_falls_back_to_still_validated_text(self):
        class Unsupported(Exception):
            status_code = 400
            body = {'error': 'response_format unsupported'}
        create = AsyncMock(side_effect=[Unsupported(), response(envelope('晚上好。'))])
        self.assertEqual(await self.run_generation(create), '晚上好。')
        self.assertNotIn('response_format', create.call_args_list[1].kwargs)


class BotBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'deepseek': {'enabled': True, 'trigger_groups': [123]},
                    'proactive': {'groups': [123], 'reply_probability': 1, 'min_messages': 1}}
        self.create = AsyncMock(return_value=response(envelope('晚上好。')))
        self.config_loader = Mock(return_value=self.cfg)
        self.now = 100000
        self.mode = ProactiveMode(Path(self.temp.name) / 'state.json', Mock(),
                                  clock=lambda: self.now, roll=lambda: 0)
        for item in (patch.object(bot, 'BASE_DIR', Path(self.temp.name)),
                     patch.object(bot, '_init_deepseek'), patch.object(bot, 'log'),
                     patch.object(bot, 'deepseek_client', client_for(self.create)),
                     patch.object(bot, 'load_config', self.config_loader),
                     patch.object(bot, '_persona_prompt', return_value='角色'),
                     patch.object(bot, '_get_whale_stickers', return_value=None),
                     patch.object(bot.proactive_images, 'enrich', side_effect=lambda b, g, m, c: m),
                     patch.object(bot, 'decision_transport', DecisionTransport()),
                     patch.object(bot, 'pending_messages', [])):
            self.enterContext(item)

    async def test_scheduled_report_never_reaches_ws_http_or_pending_queue(self):
        self.create.return_value = response(EN_STYLE)
        manager = bot.Scheduler({'target_group_ids': [123]})
        with patch.object(bot, 'send_group_message', AsyncMock()) as ws, \
             patch.object(bot, 'send_group_message_http', AsyncMock()) as http:
            await manager._send_ai_wrapper('傍晚问候')
        ws.assert_not_awaited()
        http.assert_not_awaited()
        self.assertEqual(bot.pending_messages, [])

    async def test_scheduled_boundary_rejects_bypassed_generator(self):
        manager = bot.Scheduler({'target_group_ids': [123]})
        with patch.object(bot, 'generate_timed_message', AsyncMock(return_value=EN_STYLE)), \
             patch.object(bot, 'send_group_message', AsyncMock()) as ws:
            await manager._send_ai_wrapper('傍晚问候')
        ws.assert_not_awaited()
        self.assertEqual(bot.pending_messages, [])

    async def test_valid_scheduled_message_can_fallback_and_is_marked_before_queuing(self):
        manager = bot.Scheduler({'target_group_ids': [123]})
        with patch.object(bot, 'send_group_message', AsyncMock(return_value=False)), \
             patch.object(bot, 'send_group_message_http', AsyncMock(return_value=False)):
            await manager._send_ai_wrapper('傍晚问候')
        self.assertEqual(bot.pending_messages[0]['original_text'], '晚上好。')
        self.assertTrue(bot.pending_messages[0]['public_validated'])

    async def test_scheduled_result_is_discarded_if_style_or_route_changes(self):
        async def changed(**kwargs):
            self.cfg['current_model'] = 'changed'
            return response(envelope('晚上好。'))
        self.create.side_effect = changed
        self.assertIsNone(await bot.generate_timed_message('问候', self.cfg))

    async def test_invalid_proactive_output_does_not_send_or_consume_daily_reply_quota(self):
        self.mode.observe(123, '群友', '今天聊点什么', self.cfg, sender_id='7')
        self.now += 9
        self.create.return_value = response(envelope(EN_DECIDE))
        send = AsyncMock()
        await self.mode.tick(lambda: self.cfg, lambda c: datetime(2026, 10, 25, 12, tzinfo=timezone.utc),
                             lambda: True, bot._generate_proactive, send)
        send.assert_not_awaited()
        self.assertEqual(self.mode.daily, {})

    async def test_proactive_json_can_send_and_stale_result_cannot(self):
        self.assertEqual(await bot._generate_proactive(123, 'idle', [], self.cfg), '晚上好。')
        async def changed(**kwargs):
            self.cfg['current_model'] = 'changed'
            return response(envelope('晚上好。'))
        self.create.side_effect = changed
        self.assertIsNone(await bot._generate_proactive(123, 'join', [], self.cfg))

    async def test_pending_old_raw_entry_is_discarded_and_bad_rewrite_falls_back_to_valid_original(self):
        items = [{'group_id': 123, 'original_text': EN_STYLE, 'created_at': bot.time.time()},
                 {'group_id': 123, 'original_text': '晚上好。', 'public_validated': True, 'created_at': bot.time.time()}]
        bot.pending_messages.extend(items)
        self.create.return_value = response(EN_STYLE)
        with patch.object(bot, 'OBITUARY_SENT', False), \
             patch.object(bot, 'send_group_message', AsyncMock(return_value=True)) as send:
            await bot.retry_pending_messages(self.cfg)
        send.assert_awaited_once_with(123, '晚上好。')
        self.assertFalse(bot.pending_messages)


    async def test_expired_unvalidated_pending_is_not_logged_as_text(self):
        bot.pending_messages.append({'group_id': 123, 'original_text': EN_STYLE, 'created_at': 0})
        with patch.object(bot, 'OBITUARY_SENT', False), patch.object(bot, 'log') as log, \
             patch.object(bot, 'send_group_message', AsyncMock()) as send:
            await bot.retry_pending_messages(self.cfg)
        send.assert_not_awaited()
        self.assertNotIn(EN_STYLE[:30], repr(log.call_args_list))
        self.assertFalse(bot.pending_messages)


class IgnoredSenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'deepseek': {'enabled': True}, 'proactive': {'groups': [123]}}
        self.now = 100000
        self.mode = ProactiveMode(Path(self.temp.name) / 'state.json', Mock(), clock=lambda: self.now)

    async def test_official_bot_text_image_all_and_direct_never_create_activity(self):
        for direct in (False, True):
            self.mode.observe(123, '昵称可以变化', '广播 @all', self.cfg, direct=direct,
                              image_urls=['https://example.com/image'], sender_id=2854196310)
        self.assertEqual(self.mode.groups, {})
        generate, send = AsyncMock(), AsyncMock()
        await self.mode.tick(lambda: self.cfg, lambda c: datetime.now(timezone.utc), lambda: True, generate, send)
        generate.assert_not_awaited()
        send.assert_not_awaited()

    async def test_existing_activity_and_idle_timer_are_unchanged_by_official_bot(self):
        self.mode.observe(123, '群友', '人类消息', self.cfg, sender_id='7')
        before = dict(vars(self.mode.groups['123']))
        before['messages'] = list(before['messages'])
        self.now += 100
        self.mode.observe(123, 'Q群管家', '广播', self.cfg, sender_id='2854196310')
        after = dict(vars(self.mode.groups['123']))
        after['messages'] = list(after['messages'])
        self.assertEqual(before, after)

    async def test_nickname_imitation_is_not_ignored_and_optional_list_uses_ids(self):
        self.mode.observe(123, 'Q群管家', '我是群友', self.cfg, sender_id='7')
        self.assertEqual(self.mode.groups['123'].revision, 1)
        self.cfg['proactive']['ignore_user_ids'] = [8]
        self.mode.observe(123, '群友', '不接话', self.cfg, sender_id='8')
        self.assertEqual(self.mode.groups['123'].revision, 1)

    async def test_group_event_passes_authenticated_id_to_observer(self):
        data = {'message_type': 'group', 'group_id': 123, 'user_id': 2854196310,
                'sender': {'nickname': '改名的群管家'}, 'raw_message': '广播', 'message': []}
        with patch.object(bot, 'load_config', return_value=self.cfg), \
             patch.object(bot, '_get_proactive_mode', return_value=self.mode), \
             patch.object(bot.selection_menus if hasattr(bot, 'selection_menus') else __import__('selection_menus'),
                          'handle', AsyncMock(return_value=True)):
            await bot.on_group_message(data)
        self.assertEqual(self.mode.groups, {})


if __name__ == '__main__':
    unittest.main()
