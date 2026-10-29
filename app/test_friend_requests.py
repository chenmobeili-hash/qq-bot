"""Offline, synthetic friend events and API/model mocks only."""
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import friend_requests as friend
from mechanism_assistant import allowed, describe, validate


def request(flag='test-request-flag', user=7, **extra):
    return {'post_type': 'request', 'request_type': 'friend', 'self_id': 9,
            'user_id': user, 'flag': flag, 'comment': '想认识你，一起聊聊海洋吧', **extra}


def response(decision='accept', reason='friendly', raw=None, finish='stop'):
    text = raw if raw is not None else json.dumps({'decision': decision, 'reason': reason})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1800000000.0
        self.store = friend.Store(self.root, clock=lambda: self.now)

    def rows(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM requests')]

    def test_import_default_control_does_not_create_state(self):
        self.assertEqual(self.store.control({}), (True, 0))
        self.assertFalse(self.store.path.exists())

    def test_duplicate_flags_and_settings_survive_fresh_instance(self):
        self.assertTrue(self.store.enqueue(request(), 9))
        fresh = friend.Store(self.root, clock=lambda: self.now)
        self.assertFalse(fresh.enqueue(request(), 9))
        self.store.set_enabled(False)
        self.assertEqual(fresh.control({}), (False, 1))
        self.assertIsNone(fresh.claim(9, {}))
        self.assertEqual(len(self.rows()), 1)

    def test_only_real_friend_requests_for_this_account_are_queued(self):
        invalid = [request(request_type='group'), request(post_type='message'), request(flag=''),
                   request(flag=[]), request(user=True), request(user=9), request(self_id=8),
                   request(user_id='../file'), request(comment=['invalid']), request(time=self.now - friend.TTL - 1),
                   request(time=self.now + 61), request(time=float('nan'))]
        for data in invalid:
            self.assertFalse(self.store.enqueue(data, 9))
        self.assertFalse(self.store.path.exists())

    def test_deciding_recovers_after_restart_but_never_repeats_api_commit(self):
        self.store.enqueue(request(), 9)
        job = self.store.claim(9, {})
        self.assertIsNone(self.store.claim(9, {}))
        self.now += 601
        fresh = friend.Store(self.root, clock=lambda: self.now)
        recovered = fresh.claim(9, {})
        self.assertEqual(recovered['attempts'], 2)
        self.assertFalse(fresh.update(job, 'executing'))
        self.assertTrue(fresh.update(recovered, 'executing'))
        self.now += friend.LEASE + 1
        self.assertIsNone(fresh.claim(9, {}))
        self.assertEqual(self.rows()[0]['status'], 'unknown')
        self.assertEqual(self.rows()[0]['flag'], '')

    def test_terminal_state_purges_comment_and_flag_without_losing_dedup(self):
        self.store.enqueue(request(), 9)
        job = self.store.claim(9, {})
        self.store.update(job, 'rejected', decision='reject', reason='not_interested')
        row = self.rows()[0]
        self.assertEqual((row['comment'], row['flag']), ('', ''))
        self.assertFalse(self.store.enqueue(request(), 9))

    def test_expired_pending_is_never_executed(self):
        self.store.enqueue(request(), 9)
        self.now += friend.TTL + 1
        self.assertIsNone(self.store.claim(9, {}))
        self.assertEqual(self.rows()[0]['status'], 'expired')

    def test_twenty_model_reservations_per_day_and_per_user_cooldown_persist(self):
        for i in range(20):
            self.store.enqueue(request(flag=str(i), user=100 + i), 9)
            job = self.store.claim(9, {})
            self.assertIsNotNone(job)
            self.store.update(job, 'deferred')
        self.store.enqueue(request(flag='over', user=200), 9)
        self.assertIsNone(friend.Store(self.root, clock=lambda: self.now).claim(9, {}))
        self.now += 86402
        self.assertIsNotNone(self.store.claim(9, {}))

    def test_corrupt_database_stops_without_resetting_file(self):
        self.store.path.write_bytes(b'corrupt state')
        with self.assertRaises(sqlite3.Error):
            self.store.enqueue(request(), 9)
        self.assertEqual(self.store.path.read_bytes(), b'corrupt state')


class DecisionTests(unittest.TestCase):
    def test_strict_json_never_accepts_model_supplied_targets_or_extra_actions(self):
        for raw, finish in [('{"decision":true,"reason":"friendly"}', 'stop'),
            ('{"decision":"accept","reason":"friendly","flag":"evil"}', 'stop'),
            ('{"decision":"accept","decision":"reject","reason":"friendly"}', 'stop'),
            ('{"decision":"accept","reason":"friendly"}', 'length'),
            ('accept', 'stop'), ('{"decision":"accept","reason":"invented"}', 'stop')]:
            with self.assertRaises(ValueError):
                friend.parse(raw, finish)

    def test_admin_only_global_semantic_actions(self):
        for action in ('friend_requests_on', 'friend_requests_off', 'friend_requests_status'):
            self.assertTrue(allowed(action, 'private_7', True))
            self.assertTrue(allowed(action, 'group_1_7', True))
            self.assertFalse(allowed(action, 'private_7', False))
            self.assertIn('全局', describe(validate(action, {}, 'private_7', True)))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1800000000.0
        self.store = friend.Store(self.root, clock=lambda: self.now)
        self.cfg = {'deepseek': {'enabled': True}, 'signature': 'old'}
        self.transport = SimpleNamespace(create=AsyncMock(return_value=response()))
        self.client = Mock()
        self.client.with_options.return_value = self.client
        self.fake = SimpleNamespace(BASE_DIR=self.root, bot_qq=9, load_config=lambda: self.cfg,
            _init_deepseek=Mock(), deepseek_client=self.client, current_model='synthetic-model',
            _mechanism_signature=lambda cfg: cfg['signature'], _persona_prompt=lambda cfg: 'test persona',
            _speaker_context=Mock(return_value='only applicant private notes'),
            is_admin=lambda user, cfg: str(user) == '7', decision_transport=self.transport,
            reading_bridge=SimpleNamespace(gate=SimpleNamespace(foreground=nullcontext)),
            ws_connection=object(), call_api=AsyncMock(side_effect=self.api_ok), log=Mock(),
            _mechanism_key=lambda data: ('group_1_' if data.get('group_id') else 'private_') + str(data.get('sender', {}).get('user_id')),
            send_msg_to_user=AsyncMock())
        self.enterContext(patch.object(friend, 'Store', return_value=self.store))
        self.ready = self.enterContext(patch('cognitive_runtime.ready', return_value=True))
        self.event = self.enterContext(patch('cognitive_runtime.event'))
        self.store.enqueue(request(), 9)

    async def api_ok(self, action, params, *, outcome, timeout):
        outcome.update(status='ok', api_status='ok', retcode=0)
        return None  # OneBot set_friend_add_request normally has null data.

    def row(self):
        with self.store.db() as db:
            return dict(db.execute('SELECT * FROM requests').fetchone())

    async def test_accept_uses_event_flag_null_success_data_and_sends_no_chat(self):
        await friend.tick(self.fake)
        self.fake.call_api.assert_awaited_once()
        args = self.fake.call_api.call_args
        self.assertEqual(args.args, ('set_friend_add_request', {'flag': 'test-request-flag', 'approve': True}))
        self.assertEqual(self.row()['status'], 'accepted')
        self.assertEqual(self.row()['flag'], '')
        self.fake.send_msg_to_user.assert_not_awaited()
        self.fake._speaker_context.assert_called_once_with('private_7', self.cfg, request()['comment'])
        prompt = str(self.transport.create.call_args)
        self.assertNotIn('test-request-flag', prompt)
        await friend.tick(self.fake)
        self.fake.call_api.assert_awaited_once()

    async def test_reject_is_explicit_false_not_default_true(self):
        self.transport.create.return_value = response('reject', 'not_interested')
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'rejected')
        self.assertIs(self.fake.call_api.call_args.args[1]['approve'], False)

    async def test_defer_does_not_call_api_or_repeatedly_ask_model(self):
        self.transport.create.return_value = response('defer', 'unclear')
        await friend.tick(self.fake)
        await friend.tick(self.fake)
        self.fake.call_api.assert_not_awaited()
        self.assertEqual(self.row()['status'], 'deferred')
        self.transport.create.assert_awaited_once()

    async def test_model_failure_retries_once_then_stops_without_refusal(self):
        self.transport.create.side_effect = TimeoutError('synthetic')
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'pending')
        self.now += 601
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'model_failed')
        self.now += 1000
        await friend.tick(self.fake)
        self.assertEqual(self.transport.create.await_count, 2)
        self.fake.call_api.assert_not_awaited()

    async def test_invalid_model_json_never_executes_action(self):
        self.transport.create.return_value = response(raw='{"decision":"accept","reason":"friendly","user_id":99}')
        await friend.tick(self.fake)
        self.fake.call_api.assert_not_awaited()
        self.assertEqual(self.row()['status'], 'pending')

    async def test_disabled_model_and_busy_foreground_leave_pending(self):
        self.ready.return_value = False
        await friend.tick(self.fake)
        self.ready.return_value = True
        self.cfg['deepseek']['enabled'] = False
        await friend.tick(self.fake)
        self.transport.create.assert_not_awaited()
        self.assertEqual(self.row()['status'], 'pending')

    async def test_disabled_feature_still_keeps_received_request_for_later(self):
        self.store.set_enabled(False)
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'pending')
        self.transport.create.assert_not_awaited()

    async def test_control_or_persona_changes_cancel_old_decision(self):
        async def change(*args, **kwargs):
            self.store.set_enabled(False)
            self.store.set_enabled(True)
            return response()
        self.transport.create.side_effect = change
        await friend.tick(self.fake)
        self.fake.call_api.assert_not_awaited()
        self.assertEqual(self.row()['status'], 'pending')

    async def test_route_changes_cancel_old_decision(self):
        async def change(*args, **kwargs):
            self.cfg['signature'] = 'new'
            return response()
        self.transport.create.side_effect = change
        await friend.tick(self.fake)
        self.fake.call_api.assert_not_awaited()

    async def test_expiry_during_generation_cancels_api(self):
        async def change(*args, **kwargs):
            self.now += friend.TTL + 1
            return response()
        self.transport.create.side_effect = change
        await friend.tick(self.fake)
        self.fake.call_api.assert_not_awaited()
        self.assertEqual(self.row()['status'], 'expired')

    async def test_unknown_api_receipt_is_not_success_and_never_retried(self):
        async def unknown(*args, outcome, **kwargs):
            outcome.update(status='unknown')
        self.fake.call_api.side_effect = unknown
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'unknown')
        self.now += 1000
        await friend.tick(self.fake)
        self.fake.call_api.assert_awaited_once()

    async def test_async_or_nonzero_retcode_is_not_confirmed_success(self):
        async def unknown(*args, outcome, **kwargs):
            outcome.update(status='ok', api_status='async', retcode=1)
        self.fake.call_api.side_effect = unknown
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'unknown')

    async def test_explicit_api_failure_is_distinct_from_unknown(self):
        async def failed(*args, outcome, **kwargs):
            outcome.update(status='failed', api_status='failed', retcode=100)
        self.fake.call_api.side_effect = failed
        await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'api_failed')

    async def test_cancelled_api_keeps_no_replay_boundary(self):
        self.fake.call_api.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await friend.tick(self.fake)
        self.assertEqual(self.row()['status'], 'unknown')
        await friend.tick(self.fake)
        self.fake.call_api.assert_awaited_once()

    async def test_admin_command_is_global_persistent_and_stats_only(self):
        data = {'raw_message': '好友申請 關閉', 'sender': {'user_id': 7}, 'group_id': 1}
        self.assertTrue(await friend.command(self.fake, data))
        self.assertFalse(self.store.control(self.cfg)[0])
        text = self.fake.send_msg_to_user.call_args.args[-1]
        self.assertIn('全局', text)
        self.assertNotIn(request()['comment'], text)
        self.assertNotIn('test-request-flag', text)
        await friend.command(self.fake, {**data, 'raw_message': '好友申请 开启', 'sender': {'user_id': 8}})
        self.assertFalse(self.store.control(self.cfg)[0])

    async def test_real_event_dispatch_queues_friend_but_ignores_group_requests(self):
        with patch.object(bot.friend_requests, 'receive', new_callable=AsyncMock) as receive:
            await bot.handle_event(request())
            receive.assert_awaited_once()
            await bot.handle_event(request(request_type='group'))
            receive.assert_awaited_once()

    async def test_actual_api_exposes_null_success_receipt(self):
        async def send(payload):
            item = json.loads(payload)
            bot.api_waiters[item['echo']].set_result({'status': 'ok', 'retcode': 0, 'data': None})
        with patch.object(bot, 'ws_connection', SimpleNamespace(send=send)), patch.object(bot, 'api_waiters', {}), patch.object(bot, 'log'):
            outcome = {}
            self.assertIsNone(await bot.call_api('set_friend_add_request', {'flag': 'opaque-private-flag', 'approve': True}, outcome=outcome))
        self.assertEqual((outcome['status'], outcome['api_status'], outcome['retcode']), ('ok', 'ok', 0))

    async def test_actual_api_error_never_logs_the_sensitive_request_flag(self):
        async def send(payload):
            item = json.loads(payload)
            bot.api_waiters[item['echo']].set_result({'status': 'failed', 'retcode': 100,
                'wording': 'invalid request opaque-private-flag', 'data': None})
        with patch.object(bot, 'ws_connection', SimpleNamespace(send=send)), patch.object(bot, 'api_waiters', {}), patch.object(bot, 'log') as log:
            outcome = {}
            await bot.call_api('set_friend_add_request', {'flag': 'opaque-private-flag', 'approve': True}, outcome=outcome)
        self.assertEqual(outcome['status'], 'failed')
        self.assertNotIn('opaque-private-flag', str(log.call_args_list))


if __name__ == '__main__':
    unittest.main()
