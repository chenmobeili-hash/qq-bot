from collections import defaultdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
from group_file_reader import GroupFileState
from mechanism_assistant import MechanismAssistant, SuggestedReply
from model_decision import DecisionError, DecisionTransport, instructions, parse
from paper_reader import PaperLibrary


def envelope(kind='chat', reply='你好呀。', action=None, args=None):
    return json.dumps({'kind': kind, 'reply': reply,
                       'action': {'action': action, 'args': args or {}} if action else None}, ensure_ascii=False)


def response(raw, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason=finish)])


class ProtocolTests(unittest.TestCase):
    def test_chat_clarification_operation_and_readonly_are_explicit(self):
        cfg = {'deepseek': {'trigger_groups': [123]}}
        key = 'group_123_456'
        for kind, action, args in [('chat', None, {}), ('clarify', None, {}),
            ('operation', 'proactive_level', {'level': '活跃'}),
            ('inspect', 'inspect_group_files', {}),
            ('clarify', 'read_group_file', {'name': '待上传文件'})]:
            self.assertEqual(parse(envelope(kind, action=action, args=args), key, cfg, True).kind, kind)

    def test_plain_reply_or_optional_old_marker_cannot_replace_a_decision(self):
        for raw in ['普通回复', '可以。\n[[mechanism:{"action":"reading_on","args":{}}]]',
                    '{"kind":"chat","reply":"ok","action":null,"extra":1}',
                    '{"kind":"chat","reply":"ok","action":null,"action":null}',
                    envelope('chat', action='reading_on'), envelope('operation', action='exec', args={'cmd': 'oops'}),
                    envelope('operation', action='reading_on', args={'group_id': '999'}),
                    envelope('inspect', action='inspect_group_files', args={'group_id': '999'}),
                    envelope('clarify', action='reading_on')]:
            with self.subTest(raw=raw), self.assertRaises(DecisionError):
                parse(raw, 'group_123_456', {}, True)

    def test_permissions_are_program_checked(self):
        with self.assertRaises(DecisionError):
            parse(envelope('operation', action='model_set', args={'provider': 'p', 'model': 'm'}), 'private_7', {}, False)
        with self.assertRaises(DecisionError):
            parse(envelope('inspect', action='inspect_group_files'), 'private_7', {}, True)
        with self.assertRaises(DecisionError):
            parse(envelope('inspect', action='inspect_group_files'), 'group_999_7', {'paper_reader': {'groups': [123]}}, True)

    def test_catalogue_contains_all_allowed_operations_and_no_admin_escalation(self):
        guide = instructions('group_123_456', {}, False)
        self.assertIn('read_group_file', guide)
        self.assertIn('inspect_group_files', guide)
        self.assertNotIn('"action": "proactive_level"', guide)
        self.assertIn('每轮必须', guide)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_json_mode_is_one_call(self):
        create = AsyncMock(return_value=response(envelope()))
        client = SimpleNamespace(base_url='https://synthetic.invalid/v1', chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        await DecisionTransport().create(client, 'm', [], structured=True, log=lambda *a: None)
        create.assert_awaited_once()
        self.assertEqual(create.await_args.kwargs['response_format'], {'type': 'json_object'})

    async def test_only_definite_format_rejection_uses_bounded_cached_compatibility(self):
        class Unsupported(Exception):
            status_code = 400
            body = {'error': {'message': 'response_format is not supported'}}
        create = AsyncMock(side_effect=[Unsupported(), response(envelope()), response(envelope())])
        client = SimpleNamespace(base_url='https://synthetic.invalid/v1', chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        transport = DecisionTransport()
        await transport.create(client, 'm', [], structured=True, log=lambda *a: None)
        await transport.create(client, 'm', [], structured=True, log=lambda *a: None)
        self.assertEqual(create.await_count, 3)
        self.assertIn('response_format', create.await_args_list[0].kwargs)
        self.assertNotIn('response_format', create.await_args_list[1].kwargs)
        self.assertNotIn('response_format', create.await_args_list[2].kwargs)

    async def test_network_or_rate_limit_failure_is_not_a_format_retry(self):
        create = AsyncMock(side_effect=TimeoutError)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(TimeoutError):
            await DecisionTransport().create(client, 'm', [], structured=True, log=lambda *a: None)
        create.assert_awaited_once()


class SemanticIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.key = 'group_123_456'
        self.config = {'admin_qq': [456], 'deepseek': {'enabled': True, 'trigger_groups': [123], 'max_history': 4}}
        self.config['llm_providers'] = {'test': {'enabled': True, 'models': ['text', 'vision']}}
        self.states = {'可选人格（全局）': ['工程版', '小鲸鱼'], '可选模型（全局）': {'test': ['text', 'vision']}}
        self.router = MechanismAssistant()
        self.transport = DecisionTransport()
        self.files = GroupFileState()
        self.library = PaperLibrary()
        self.history = defaultdict(list)
        self.create = AsyncMock(return_value=response(envelope()))
        self.client = SimpleNamespace(base_url='https://synthetic.invalid/v1', chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.book = {'file_id': 'book-id', 'name': '耶什克_黑格爾手冊_終版.pdf', 'size': 100}
        self.data = {'message_type': 'group', 'group_id': 123, 'sender': {'user_id': 456},
                     'message': [{'type': 'at', 'data': {'qq': '999'}}], 'raw_message': '[CQ:at,qq=999] 里面到底在说什么'}
        for p in (patch.object(bot, 'BASE_DIR', self.base), patch.object(bot, 'load_config', return_value=self.config),
                  patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', self.client),
                  patch.object(bot, '_persona_prompt', return_value='虚构测试人格'), patch.object(bot, '_persona_signature', 'style'),
                  patch.object(bot, '_persona_name', '测试'), patch.object(bot, '_mechanism_signature', return_value='sig'),
                  patch.object(bot, '_semantic_state', new_callable=AsyncMock, return_value=self.states),
                  patch.object(bot, '_get_whale_stickers', return_value=None), patch.object(bot, '_save_conversations'),
                  patch.object(bot, 'conversations', self.history), patch.object(bot, 'mechanism_assistant', self.router),
                  patch.object(bot, 'decision_transport', self.transport), patch.object(bot, 'group_file_state', self.files),
                  patch.object(bot, 'paper_library', self.library), patch.object(bot, 'bot_qq', 999), patch.object(bot, 'log')):
            p.start()
            self.addCleanup(p.stop)

    async def test_ordinary_chat_is_one_model_call_and_saves_only_visible_prose(self):
        result = await bot.ask_deepseek(self.key, '今天的云像棉花糖')
        self.assertEqual(result, '你好呀。')
        self.create.assert_awaited_once()
        self.assertFalse(self.router.pending)
        self.assertEqual(self.history[self.key][-1]['content'], '你好呀。')
        self.assertNotIn('"kind"', str(self.history))

    async def test_model_can_choose_a_feature_without_any_trigger_words(self):
        words = '别总坐在旁边看热闹嘛，这里聊起来时你也凑过来多说几句'
        self.create.return_value = response(envelope('operation', '要让我多参与些吗？', 'proactive_level', {'level': '活跃'}))
        with patch.object(bot, '_execute_mechanism', new_callable=AsyncMock) as execute:
            result = await bot.ask_deepseek(self.key, words)
        self.assertIsInstance(result, SuggestedReply)
        self.assertIn('本群', result)
        self.assertIn('活跃', result)
        execute.assert_not_awaited()
        self.create.assert_awaited_once()

    async def test_normal_group_entry_uses_model_not_previous_keyword_router(self):
        self.create.return_value = response(envelope('operation', '要读这本吗？', 'read_group_file', {'name': '黑格尔手册'}))
        with patch.object(bot, '_paper_candidates', new_callable=AsyncMock, return_value=[self.book]), \
             patch.object(bot, '_handle_named_paper_request', side_effect=AssertionError('old keyword path')), \
             patch.object(bot, '_handle_paper_followup', side_effect=AssertionError('old keyword path')), \
             patch.object(bot, 'send_group_message', new_callable=AsyncMock, return_value=True) as send:
            await bot.on_group_message(self.data)
        self.assertIsInstance(send.await_args.args[1], SuggestedReply)
        self.assertIn(self.book['name'], send.await_args.args[1])
        pending = self.router.pending[self.key]
        self.assertEqual(pending.context['question'], '里面到底在说什么')

    async def test_query_then_operation_is_bounded_and_does_not_download_before_confirm(self):
        self.create.side_effect = [response(envelope('inspect', '', 'inspect_group_files')),
            response(envelope('operation', '读这份如何？', 'read_group_file', {'name': '黑格尔手册'}))]
        with patch.object(bot, '_semantic_inspection', new_callable=AsyncMock, return_value={'候选': [{'name': self.book['name']}]}), \
             patch.object(bot, '_paper_candidates', new_callable=AsyncMock, return_value=[self.book]), \
             patch.object(bot, '_download_group_paper', new_callable=AsyncMock) as download:
            result = await bot.ask_deepseek(self.key, '把他刚发的那本捋一捋呗')
        self.assertIsInstance(result, SuggestedReply)
        self.assertEqual(self.create.await_count, 2)
        download.assert_not_awaited()
        self.assertIn(self.book['name'], str(self.create.await_args.kwargs['messages']))

    async def test_clarify_does_not_create_or_execute_a_pending_operation(self):
        self.create.return_value = response(envelope('clarify', '你想看什么主题的视频？'))
        result = await bot.ask_deepseek(self.key, '有空看点啥呗')
        self.assertEqual(result, '你想看什么主题的视频？')
        self.assertFalse(self.router.pending)

    async def test_waiting_for_upload_is_semantic_and_still_requires_confirmation(self):
        self.create.return_value = response(envelope('clarify', '把要看的那本发来吧。', 'read_group_file', {'name': '待上传文件'}))
        await bot.ask_deepseek(self.key, '接下来发给你的那个，帮我看看里面在说啥')
        with patch.object(bot, 'send_group_message', new_callable=AsyncMock, return_value=True) as send, \
             patch.object(bot, '_run_paper_read', new_callable=AsyncMock) as run:
            await bot._on_paper_upload({'group_id': 123, 'user_id': 456, 'file': self.book})
        self.assertIsInstance(send.await_args.args[1], SuggestedReply)
        run.assert_not_awaited()

    async def test_malformed_or_legacy_reply_fails_visibly_without_fake_operation(self):
        for raw in ('可以，已经开启了。', '好的\n[[mechanism:{"action":"reading_on","args":{}}]]'):
            self.create.return_value = response(raw)
            result = await bot.ask_deepseek(self.key, '随便一种表达')
            self.assertIn('没有返回有效', result)
            self.assertFalse(self.router.pending)
        self.assertFalse(self.history[self.key])

    async def test_inspection_repetition_stops_without_action_or_hidden_loop(self):
        self.create.return_value = response(envelope('inspect', '', 'inspect_group_files'))
        with patch.object(bot, '_semantic_inspection', new_callable=AsyncMock, return_value={'候选': []}) as inspect:
            result = await bot.ask_deepseek(self.key, '看看那个')
        self.assertIn('没有返回有效', result)
        self.assertEqual(self.create.await_count, 2)
        inspect.assert_awaited_once()
        self.assertFalse(self.router.pending)

    async def test_superseded_model_turn_is_not_delivered_or_saved(self):
        async def replaced(**kwargs):
            self.transport.invalidate(self.key)
            return response(envelope('operation', '', 'reading_on'))
        self.create.side_effect = replaced
        self.assertEqual(await bot.ask_deepseek(self.key, '你有空自己看书吧'), '')
        self.assertFalse(self.router.pending)
        self.assertFalse(self.history[self.key])

    async def test_disallowed_admin_action_cannot_be_proposed_by_an_unprivileged_user(self):
        self.config['admin_qq'] = []
        self.create.return_value = response(envelope('operation', '', 'vision_sync_on'))
        result = await bot.ask_deepseek(self.key, '看图也用这个模型')
        self.assertIn('没有返回有效', result)
        self.assertFalse(self.router.pending)

    async def test_named_persona_and_model_are_checked_against_real_choices(self):
        self.create.return_value = response(envelope('operation', '换一下吗？', 'style_set', {'name': '../fake'}))
        result = await bot.ask_deepseek(self.key, '换成那个')
        self.assertNotIsInstance(result, SuggestedReply)
        self.create.return_value = response(envelope('operation', '换一下吗？', 'model_set', {'provider': 'test', 'model': 'vision'}))
        result = await bot.ask_deepseek(self.key, '用那个能看图的模型')
        self.assertIsInstance(result, SuggestedReply)
        self.assertIn('全局', result)


if __name__ == '__main__':
    unittest.main()
