import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import cognitive_runtime as runtime
import cognitive_state as state
import public_message
from proactive import ProactiveMode


def response(text, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000
        self.store = state.Store(self.tmp.name, clock=lambda: self.now)

    def test_restart_order_and_dedup(self):
        self.store.event('group_1_2', 'human', '晚到', at=self.now - 2, dedup='1')
        self.store.event('group_1_3', 'human', '先发生', at=self.now - 3, dedup='2')
        self.store.event('group_1_2', 'human', '重复', dedup='1')
        other = state.Store(self.tmp.name, clock=lambda: self.now)
        self.assertEqual([r['text'] for r in other.recent('group_1_9')], ['先发生', '晚到'])

    def test_no_cross_chat_or_private_leak(self):
        for key in ('group_1_2', 'group_2_2', 'private_2', 'private_3'):
            self.store.event(key, 'human', key)
        self.assertEqual([r['text'] for r in self.store.recent('group_1_7')], ['group_1_2'])
        self.assertEqual([r['text'] for r in self.store.recent('private_2')], ['private_2'])

    def test_event_text_expires_and_metadata_retained(self):
        self.store.event('private_2', 'send', '精确内容', status='unknown')
        self.now += 18001
        row = self.store.recent('private_2')[0]
        self.assertEqual(row['text'], '')
        self.assertEqual(row['status'], 'unknown')
        self.now += 30 * 86400
        self.assertEqual(self.store.recent('private_2'), [])

    def test_precise_expiry_cannot_be_renewed_by_timeline(self):
        self.store.event('private_2', 'send', '精读片段', expires=self.now + 10)
        self.now += 11
        self.assertEqual(self.store.recent('private_2')[0]['text'], '')

    def test_credentials_and_urls_not_retained(self):
        self.store.event('private_2', 'human', '[CQ:image,url=https://secret] https://signed/path?token=test api_key=privatekey')
        text = self.store.recent('private_2')[0]['text']
        self.assertNotIn('secret', text)
        self.assertNotIn('signed', text)
        self.assertNotIn('privatekey', text)

    def test_summary_is_personal_and_bounded(self):
        self.store.save_summary('group_1_2', '此人的偏好', 'prefix')
        self.assertIsNone(self.store.summary('group_1_3'))
        self.assertIsNone(self.store.summary('group_1'))
        with self.assertRaises(ValueError):
            self.store.save_summary('private_2', 'x' * 2601, 'bad')

    def test_impossible_timestamps_use_receive_clock(self):
        self.store.event('private_2', 'human', at=float('inf'))
        self.assertEqual(self.store.recent('private_2')[0]['at'], self.now)

    def test_bounded_event_count(self):
        with patch.object(state, 'MAX_EVENTS', 3):
            for i in range(10):
                self.store.event('private_2', 'human', str(i))
            self.assertEqual(len(self.store.recent('private_2')), 3)


class CompressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {'deepseek': {'max_history': 10}}
        self.rows = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'消息{i}',
                      'created_at': f'2026-10-25T10:{i:02}:00+08:00'} for i in range(30)]
        self.create = AsyncMock(return_value=response(json.dumps({'summary': '用户关心某书，阅读仍待确认。'})))
        self.fake = SimpleNamespace(BASE_DIR=Path(self.tmp.name), conversations={'private_2': list(self.rows)},
            _save_conversations=Mock(), _init_deepseek=Mock(), deepseek_client=object(), current_model='model',
            _mechanism_signature=lambda cfg: 'route', load_config=lambda: self.cfg, log=Mock(),
            decision_transport=SimpleNamespace(create=self.create))

    async def test_compress_then_keep_recent_verbatim(self):
        await runtime.compress(self.fake, 'private_2', self.cfg)
        self.assertEqual(self.fake.conversations['private_2'], self.rows[-10:])
        self.assertIn('待确认', runtime.store(self.fake).summary('private_2')['text'])
        self.fake._save_conversations.assert_called_once()

    async def test_model_failure_never_deletes_source(self):
        self.create.side_effect = TimeoutError
        await runtime.compress(self.fake, 'private_2', self.cfg)
        await runtime.compress(self.fake, 'private_2', self.cfg)
        self.assertEqual(self.fake.conversations['private_2'], self.rows)
        self.assertEqual(self.create.await_count, 1)

    async def test_store_failure_never_deletes_source(self):
        with patch.object(runtime.store(self.fake), 'save_summary', side_effect=OSError):
            await runtime.compress(self.fake, 'private_2', self.cfg)
        self.assertEqual(self.fake.conversations['private_2'], self.rows)

    async def test_precise_notes_excluded_from_durable_summary(self):
        self.fake.conversations['private_2'][0] = {**self.rows[0], 'content': 'EXACT_BOOK_QUOTE', 'reading_expires': 9999999999}
        await runtime.compress(self.fake, 'private_2', self.cfg)
        request = str(self.create.call_args.args)
        self.assertNotIn('EXACT_BOOK_QUOTE', request)

    async def test_malformed_or_truncated_summary_keeps_source(self):
        self.create.return_value = response('{"summary":"valid"}', 'length')
        await runtime.compress(self.fake, 'private_2', self.cfg)
        self.assertEqual(self.fake.conversations['private_2'], self.rows)

    async def test_crash_after_commit_does_not_resummarize(self):
        runtime.store(self.fake).save_summary('private_2', '已经压缩', state.digest(self.rows[:-10]))
        await runtime.compress(self.fake, 'private_2', self.cfg)
        self.create.assert_not_awaited()
        self.assertEqual(self.fake.conversations['private_2'], self.rows[-10:])

    async def test_midflight_history_replacement_discards_summary(self):
        async def changed(*args, **kwargs):
            self.fake.conversations['private_2'] = [{'role': 'user', 'content': '全新会话'}]
            return response('{"summary":"旧会话"}')
        self.create.side_effect = changed
        await runtime.compress(self.fake, 'private_2', self.cfg)
        self.assertIsNone(runtime.store(self.fake).summary('private_2'))

    async def test_disabled_and_short_history_no_extra_call(self):
        await runtime.compress(self.fake, 'private_2', {'context_compression': {'enabled': False}})
        self.fake.conversations['private_2'] = self.rows[:4]
        await runtime.compress(self.fake, 'private_2', self.cfg)
        self.create.assert_not_awaited()


class WakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = 1800000000
        self.mode = ProactiveMode(Path(self.tmp.name) / 'proactive.json', Mock(),
                                  clock=lambda: self.clock, roll=lambda: 0)
        self.cfg = {'deepseek': {'enabled': True}, 'proactive': {'groups': [1], 'wake_min_seconds': 7200, 'wake_max_seconds': 7200}}
        self.now = datetime(2026, 10, 25, 12, tzinfo=timezone(timedelta(hours=8)))
        self.generate = AsyncMock(return_value='今天想到一个有趣的问题。')
        self.send = AsyncMock(return_value=True)

    async def tick(self):
        await self.mode.tick(lambda: self.cfg, lambda c: self.now, lambda: True, self.generate, self.send)

    async def test_no_new_human_needed_but_never_unenabled_group(self):
        await self.tick()
        self.generate.assert_not_awaited()
        self.clock += 7201
        await self.tick()
        self.assertEqual(self.generate.call_args.args[:2], ('1', 'wake'))
        self.send.assert_awaited_once()
        self.assertEqual(self.mode.daily['1']['count'], 1)
        self.assertNotIn('2', self.mode.groups)

    async def test_quiet_and_daily_shared_quota(self):
        await self.tick()
        self.clock += 7201
        self.now = self.now.replace(hour=23)
        await self.tick()
        self.generate.assert_not_awaited()
        self.now = self.now.replace(hour=12)
        self.mode.daily['1'] = {'date': self.now.date().isoformat(), 'count': 24}
        await self.tick()
        self.generate.assert_not_awaited()

    async def test_silence_still_consumes_opportunity_and_max_two(self):
        self.generate.return_value = None
        await self.tick()
        for _ in range(3):
            self.clock += 7201
            await self.tick()
        self.assertEqual(self.generate.await_count, 2)
        self.send.assert_not_awaited()

    async def test_restart_does_not_replay_overdue_wake(self):
        await self.tick()
        self.clock += 99999
        self.mode = ProactiveMode(self.mode.path, Mock(), clock=lambda: self.clock, roll=lambda: 0)
        await self.tick()
        # Recovery catches one overdue opportunity; it does not start another delay.
        self.generate.assert_awaited_once()

    async def test_turning_off_mid_generation_discards(self):
        await self.tick()
        self.clock += 7201
        async def off(*args):
            self.mode.set_wake(1, False)
            return '旧唤醒'
        self.generate.side_effect = off
        await self.tick()
        self.send.assert_not_awaited()

    async def test_new_message_mid_generation_discards(self):
        await self.tick()
        self.clock += 7201
        async def arrived(*args):
            self.mode.observe(1, '群友', '等一下', self.cfg, direct=True)
            return '旧唤醒'
        self.generate.side_effect = arrived
        await self.tick()
        self.send.assert_not_awaited()

    async def test_toggle_persists_without_enabling_group(self):
        self.mode.set_wake(2, True)
        self.assertFalse(self.mode.enabled(2, self.cfg))
        other = ProactiveMode(self.mode.path, Mock())
        self.assertTrue(other.wake_enabled(2, self.cfg))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_sentences_stay_segmented_with_hard_caps(self):
        parts = bot._split_reply_sentences('一。二。三。四。五。')
        self.assertEqual(len(parts), 3)
        self.assertEqual(''.join(parts), '一。二。三。四。五。')

    async def test_traditional_command_and_admin_check(self):
        with tempfile.TemporaryDirectory() as root:
            mode = ProactiveMode(Path(root) / 'state.json', Mock())
            fake = SimpleNamespace(_mechanism_key=lambda d: 'group_1_2', bot_qq='9',
                load_config=lambda: {}, is_admin=lambda u, c: False,
                _get_proactive_mode=lambda: mode, send_msg_to_user=AsyncMock())
            await runtime.command(fake, {'group_id': 1, 'sender': {'user_id': 2}, 'raw_message': '自主喚醒 開啟'})
            self.assertFalse(mode.wake_overrides)
            self.assertIn('只有管理员', fake.send_msg_to_user.call_args.args[2])

    async def test_timeline_converts_same_clock_and_keeps_delivery_truth(self):
        with tempfile.TemporaryDirectory() as root:
            fake = SimpleNamespace(BASE_DIR=Path(root), log=Mock(),
                _bot_now=lambda c: datetime.now(timezone(timedelta(hours=8))))
            runtime.event(fake, 'private_2', 'send', '台词', status='unknown')
            row = runtime.timeline(fake, 'private_2', {})[0]
            self.assertTrue(row['at'].endswith('+08:00'))
            self.assertEqual(row['status'], 'unknown')

    async def test_bili_manual_rejects_planning_then_accepts_final(self):
        create = AsyncMock(side_effect=[response('I should keep my response brief and in character.'),
            response(json.dumps({'kind': 'send', 'reply': '基于字幕，这段介绍了小猫的习性。'}))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        cfg = {}
        with patch.object(bot, '_persona_prompt', return_value='人格'), patch.object(bot, '_init_deepseek'), \
             patch.object(bot, 'deepseek_client', client), patch.object(bot, 'load_config', return_value=cfg), \
             patch.object(bot, '_vision_route_signature', return_value='v'), patch.object(bot.hearing, 'signature', return_value='a'):
            reply = await bot._generate_bilibili({'transcript_loaded': True, 'subtitle': '猫的习性'}, '概括', cfg)
        self.assertIn('小猫', reply)
        self.assertNotIn('I should', reply)
        self.assertEqual(create.await_count, 2)

    async def test_bili_all_raw_output_never_sent(self):
        create = AsyncMock(return_value=response('I need to put together a tsundere-style summary of this video.'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, '_persona_prompt', return_value='人格'), patch.object(bot, '_init_deepseek'), \
             patch.object(bot, 'deepseek_client', client), patch.object(bot, 'load_config', return_value={}), \
             patch.object(bot, '_vision_route_signature', return_value='v'), patch.object(bot.hearing, 'signature', return_value='a'):
            reply = await bot._generate_bilibili({'transcript_loaded': True, 'subtitle': '猫'}, '概括', {})
        self.assertIsNone(reply)

    async def test_report_phrases_rejected_inside_envelope(self):
        for text in ('I need to put together a tsundere-style summary of this video.',
                     'I should keep my response brief and in character.'):
            with self.assertRaises(Exception):
                public_message.validate_text(text, 600)

    async def test_postgeneration_ready_does_not_require_new_idle_wait(self):
        fake = SimpleNamespace(ws_connection=object(), pending_messages=[], paper_library=None,
            mechanism_assistant=None, schedule=SimpleNamespace(jobs=[]),
            reading_bridge=SimpleNamespace(gate=SimpleNamespace(busy=0, idle=lambda: False)))
        with patch.object(runtime, 'active', 0):
            self.assertFalse(runtime.ready(fake))
            self.assertTrue(runtime.ready(fake, settled=False))

    async def test_actual_send_wrapper_records_uncertain_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(bot, 'BASE_DIR', Path(root)), patch.object(bot, 'ws_connection', None), patch.object(bot, 'log'), \
                 patch.dict(bot.conversations, {}, clear=True):
                result = await bot.call_api('send_private_msg', {'user_id': 2, 'message': '测试台词'}, outcome=None)
                self.assertIsNone(result)
                row = runtime.store(bot).recent('private_2')[-1]
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['text'], '测试台词')

    async def test_generation_abandoned_after_private_activity(self):
        create = AsyncMock()
        async def changed(*args, **kwargs):
            runtime.revision += 1
            return response('{"kind":"send","reply":"旧话题"}')
        create.side_effect = changed
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), \
             patch.object(bot, 'load_config', return_value={}), patch.object(bot, '_persona_prompt', return_value='角色'), \
             patch.object(bot, '_get_whale_stickers', return_value=None), patch.object(runtime, 'active', 0):
            self.assertIsNone(await bot._generate_proactive('1', 'wake', [], {}))

    async def test_successful_proactive_is_not_blocked_by_own_foreground_gate(self):
        create = AsyncMock(return_value=response('{"kind":"send","reply":"今天想起一首曲子。"}'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), \
             patch.object(bot, 'load_config', return_value={}), patch.object(bot, '_persona_prompt', return_value='角色'), \
             patch.object(bot, '_get_whale_stickers', return_value=None), patch.object(runtime, 'active', 0):
            self.assertIn('曲子', await bot._generate_proactive('1', 'wake', [], {}))


if __name__ == '__main__':
    unittest.main()
