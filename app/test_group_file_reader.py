import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot
from group_file_reader import GroupFileState, match_files, reference, reading_request
from mechanism_assistant import MechanismAssistant, SuggestedReply, validate
from paper_reader import PaperLibrary


BOOK = {'file_id': 'book-1', 'name': '耶什克_黑格爾手冊_終版.pdf', 'size': 100, 'busid': 102}


class MatchingTests(unittest.TestCase):
    def test_exact_normalized_unique_ambiguous_missing(self):
        other = {'file_id': 'book-2', 'name': '黑格尔手册_旧版.pdf'}
        self.assertEqual(match_files(BOOK['name'], [BOOK, other]), [BOOK])
        self.assertEqual(match_files('《黑格爾手冊》', [BOOK]), [BOOK])
        self.assertEqual(match_files('提供黑格爾手冊的提要', [BOOK, other]), [BOOK, other])
        self.assertEqual(match_files('不存在的书', [BOOK]), [])
        self.assertEqual(match_files('黑', [BOOK]), [])
        self.assertEqual(match_files('这份文件', [BOOK]), [])
        self.assertEqual(match_files('Research', [{'file_id': 'a', 'name': 'Research.PDF'}])[0]['file_id'], 'a')

    def test_read_intent_and_question_references(self):
        self.assertTrue(reading_request('提供黑格爾手冊的提要'))
        self.assertTrue(reading_request('能否提供此文件簡明提要'))
        self.assertEqual(reference('读文献 黑格尔手册 重点分析时间范畴'), '黑格尔手册')
        for text in ('不要读这份文件', '怎么使用读文献指令', '总结一下今天群聊', '摘要是什么', '今天天气不错'):
            self.assertFalse(reading_request(text), text)

    def test_read_group_file_validation_and_permissions(self):
        proposal = validate('read_group_file', {'name': BOOK['name']}, 'group_123_456', False)
        self.assertEqual(proposal['args']['name'], BOOK['name'])
        for key, args in [('private_456', {'name': BOOK['name']}),
                          ('group_123_456', {'name': '[CQ:at,qq=all]'}),
                          ('group_123_456', {'name': 'ok.pdf', 'group_id': '999'})]:
            with self.assertRaises(ValueError):
                validate('read_group_file', args, key, False)

    def test_rejected_marker_diagnostics_do_not_log_parameters(self):
        logs = []
        router = MechanismAssistant(log=lambda *args: logs.append(' '.join(args)))
        for text in ('[[mechanism:{oops}]]',
                     '[[mechanism:{"action":"nope","args":{"name":"SECRET"}}]]',
                     '[[mechanism:{"action":"read_group_file","args":{"bad":"SECRET"}}]]',
                     '[[mechanism:{"action":"read_group_file","args":{"name":"SECRET"}}]]\nmore'):
            self.assertIsNone(router.extract(text, 'group_123_456', {}, False)[1])
        self.assertEqual(len(logs), 4)
        self.assertNotIn('SECRET', str(logs))
        self.assertIn('JSON', logs[0])
        self.assertIn('动作不允许', logs[1])
        self.assertIn('参数校验', logs[2])
        self.assertIn('末行', logs[3])

    def test_upload_requests_expire_and_cannot_cross_group_or_sender(self):
        now = [0]
        state = GroupFileState(clock=lambda: now[0])
        state.wait_for_upload(123, 456, '请求')
        self.assertIsNone(state.take_request(999, 456))
        self.assertIsNone(state.take_request(123, 457))
        now[0] = 601
        self.assertIsNone(state.take_request(123, 456))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'deepseek': {'enabled': True, 'trigger_groups': [123]}}
        self.lib = PaperLibrary()
        self.router = MechanismAssistant()
        self.state = GroupFileState()
        self.data = {'message_type': 'group', 'group_id': 123, 'sender': {'user_id': 456},
                     'raw_message': '[CQ:at,qq=999] 提供黑格爾手冊的提要',
                     'message': [{'type': 'at', 'data': {'qq': '999'}}]}
        self.rows = AsyncMock(return_value=[dict(BOOK)])
        self.send = AsyncMock(return_value=True)
        for p in (patch.object(bot, 'paper_library', self.lib), patch.object(bot, 'group_file_state', self.state),
                  patch.object(bot, 'mechanism_assistant', self.router), patch.object(bot, 'bot_qq', 999),
                  patch.object(bot, 'BASE_DIR', Path(self.tmp.name)), patch.object(bot, 'load_config', return_value=self.config),
                  patch.object(bot, '_mechanism_signature', return_value='sig'), patch.object(bot, 'log'),
                  patch.object(bot, '_paper_candidates', self.rows), patch.object(bot, 'send_group_message', self.send)):
            p.start()
            self.addCleanup(p.stop)

    async def test_legacy_rule_helper_still_matches_for_extension_callers(self):
        with patch.object(bot, 'ask_deepseek', new_callable=AsyncMock) as chat, \
             patch.object(bot, '_download_group_paper', new_callable=AsyncMock) as download:
            await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        chat.assert_not_awaited()
        download.assert_not_awaited()
        reply = self.send.await_args.args[1]
        self.assertIsInstance(reply, SuggestedReply)
        self.assertIn(BOOK['name'], reply)
        self.assertFalse(self.router.pending['group_123_456'].active)

    async def test_confirmation_preserves_question_and_exact_file_identity(self):
        await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        pending = self.router.pending['group_123_456']
        self.router.activate('group_123_456', pending.ticket)
        with patch.object(bot, '_run_paper_read', new_callable=AsyncMock) as run:
            self.assertTrue(await bot._handle_mechanism_control(self.data, '确认', self.config))
            run.assert_awaited_once_with(123, '456', '提供黑格爾手冊的提要', self.config, selected_info=BOOK)
            self.assertFalse(await bot._handle_mechanism_control(self.data, '确认', self.config))

    async def test_changed_same_name_file_requires_new_confirmation(self):
        await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        pending = self.router.pending['group_123_456']
        self.router.activate('group_123_456', pending.ticket)
        self.rows.return_value = [{**BOOK, 'file_id': 'replacement'}]
        with patch.object(bot, '_run_paper_read', new_callable=AsyncMock) as run:
            await bot._handle_mechanism_control(self.data, '确认', self.config)
        run.assert_not_awaited()
        self.assertIn('文件已经变化', self.send.await_args.args[1])

    async def test_ambiguous_and_missing_never_guess_or_claim_absence(self):
        self.rows.return_value = [BOOK, {**BOOK, 'file_id': 'book-2', 'name': '黑格尔手册_旧版.pdf'}]
        await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        self.assertIn('多份', self.send.await_args.args[1])
        self.assertFalse(self.router.pending)
        self.rows.return_value = []
        self.state.invalidate(123)
        await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        self.assertIn('当前检索范围内未找到', self.send.await_args.args[1])
        self.assertNotIn('没有收到', self.send.await_args.args[1])

    async def test_reupload_after_natural_request_offers_confirmation_only(self):
        self.rows.return_value = []
        await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        self.send.reset_mock()
        with patch.object(bot, '_run_paper_read', new_callable=AsyncMock) as run:
            await bot._on_paper_upload({'group_id': 123, 'user_id': 456, 'file': BOOK})
        run.assert_not_awaited()
        self.assertIsInstance(self.send.await_args.args[1], SuggestedReply)
        self.assertIn(BOOK['name'], self.send.await_args.args[1])

    async def test_file_message_reupload_does_not_cancel_its_own_confirmation(self):
        self.state.wait_for_upload(123, 456, '提供黑格爾手冊的提要')
        self.data['message'] = [{'type': 'file', 'data': BOOK}]
        self.data['raw_message'] = BOOK['name']
        await bot.on_group_message(self.data)
        self.assertIn('group_123_456', self.router.pending)

    async def test_recent_upload_from_self_and_quote_from_same_group(self):
        self.lib.remember_file(('123', '456'), BOOK)
        await bot._handle_named_paper_request(self.data, '能否提供此文件简明提要', self.config)
        self.assertIn(BOOK['name'], self.send.await_args.args[1])
        self.lib.files.clear()
        self.data['message'].append({'type': 'reply', 'data': {'id': '12'}})
        reply = {'group_id': 123, 'message': [{'type': 'file', 'data': BOOK}]}
        with patch.object(bot, 'call_api', new_callable=AsyncMock, return_value=reply) as api:
            await bot._handle_named_paper_request(self.data, '提供这份文件的提要', self.config)
        self.assertEqual(api.await_args.kwargs['timeout'], 5)
        self.assertIn(BOOK['name'], self.send.await_args.args[1])

    async def test_reply_metadata_does_not_hide_shorthand_or_confirmation(self):
        self.data['raw_message'] = '[CQ:reply,id=12][CQ:at,qq=999] 这份文件'
        self.data['message'].append({'type': 'reply', 'data': {'id': '12'}})
        quoted = {'group_id': 123, 'message': [{'type': 'file', 'data': BOOK}]}
        with patch.object(bot, 'call_api', new_callable=AsyncMock, return_value=quoted), \
             patch.object(bot, 'ask_deepseek', new_callable=AsyncMock) as chat:
            await bot._handle_named_paper_request(self.data, '这份文件', self.config)
        self.assertIsInstance(self.send.await_args.args[1], SuggestedReply)
        chat.assert_not_awaited()
        pending = self.router.pending['group_123_456']
        self.router.activate('group_123_456', pending.ticket)
        self.data['raw_message'] = '[CQ:reply,id=44][CQ:at,qq=999] 确认'
        with patch.object(bot, '_run_paper_read', new_callable=AsyncMock) as run:
            await bot.on_group_message(self.data)
        run.assert_awaited_once()

    async def test_unknown_or_foreign_quote_does_not_choose_different_file(self):
        self.data['message'].append({'type': 'reply', 'data': {'id': '12'}})
        with patch.object(bot, 'call_api', new_callable=AsyncMock, return_value={'group_id': 999, 'message': [{'type': 'file', 'data': BOOK}]}):
            rows = await bot._quoted_group_files(self.data)
            await bot._handle_named_paper_request(self.data, '提供这份文件的提要', self.config)
        self.assertEqual(rows, [])
        self.assertFalse(self.router.pending)
        self.assertNotIsInstance(self.send.await_args.args[1], SuggestedReply)

    async def test_changing_topic_cancels_awaited_natural_upload(self):
        self.state.wait_for_upload(123, 456, '提供黑格爾手冊的提要')
        self.assertFalse(await bot._handle_mechanism_control(self.data, '今天天气不错', self.config))
        self.send.reset_mock()
        await bot._on_paper_upload({'group_id': 123, 'user_id': 456, 'file': BOOK})
        self.send.assert_not_awaited()

    async def test_topic_change_during_directory_lookup_cancels_stale_suggestion(self):
        async def listing(group_id):
            await bot._handle_mechanism_control(self.data, '换个话题，今天天气不错', self.config)
            return [BOOK]
        self.rows.side_effect = listing
        await bot._handle_named_paper_request(self.data, '提供黑格爾手冊的提要', self.config)
        self.send.assert_not_awaited()
        self.assertFalse(self.router.pending)

    async def test_other_sender_or_group_upload_does_not_resume_request(self):
        self.state.wait_for_upload(123, 456, '提供黑格爾手冊的提要')
        await bot._on_paper_upload({'group_id': 123, 'user_id': 457, 'file': BOOK})
        await bot._on_paper_upload({'group_id': 999, 'user_id': 456, 'file': BOOK})
        self.send.assert_not_awaited()
        await bot._on_paper_upload({'group_id': 123, 'user_id': 456, 'file': BOOK})
        self.assertIsInstance(self.send.await_args.args[1], SuggestedReply)

    async def test_explicit_named_command_keeps_question_and_skips_confirmation(self):
        with patch.object(bot, '_run_paper_read', new_callable=AsyncMock) as run:
            await bot._handle_paper_command(self.data, '读文献 黑格尔手册 重点分析时间范畴', self.config)
        self.assertEqual(run.await_args.args[2], '重点分析时间范畴')
        self.assertEqual(run.await_args.kwargs['selected_info'], BOOK)
        self.assertFalse(self.router.pending)

    async def test_file_state_in_prompt_contains_names_not_ids_or_urls(self):
        self.lib.remember_file(('123', '456'), {**BOOK, 'url': 'https://example.com/SECRET'})
        context = await bot._paper_file_context('123', '456', '提供提要', self.config)
        payload = json.dumps(context, ensure_ascii=False)
        self.assertIn(BOOK['name'], payload)
        self.assertIn('已登记附件', payload)
        self.assertNotIn('SECRET', payload)
        self.assertNotIn(BOOK['file_id'], payload)

    async def test_lookup_failure_keeps_upload_metadata_without_fabricating_absence(self):
        self.lib.remember_file(('123', '456'), BOOK)
        self.rows.side_effect = TimeoutError
        snapshot = await bot._group_file_snapshot(123)
        self.assertEqual(snapshot['rows'], [BOOK])
        self.assertIn('未完成', snapshot['status'])

    async def test_named_path_can_reread_even_when_already_reviewed(self):
        doc = {'title': 'book', 'kind': 'PDF文字', 'total': 1, 'truncated': False,
               'units': [{'label': '第1页', 'text': 'test ' * 30}]}
        reading = type('Reading', (), {'storage_ok': True, 'seen_document': lambda *a: True,
                                     'mark_reviewed': lambda *a: None})()
        with patch.object(bot, '_get_paper_autonomy', return_value=reading), \
             patch.object(bot, '_download_group_paper', new_callable=AsyncMock, return_value=doc), \
             patch.object(bot, '_generate_paper', new_callable=AsyncMock, return_value='根据正文回答') as generate:
            await bot._run_paper_read(123, 456, '重读', self.config, selected_info=BOOK)
        generate.assert_awaited_once()
        self.assertIs(self.lib.current_doc(('123', '456')), doc)


if __name__ == '__main__':
    unittest.main()
