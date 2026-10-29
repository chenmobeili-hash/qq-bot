"""Optional notes cannot invalidate valid replies or relax core permissions."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import unittest

import bot
import long_term_memory as memory
from mechanism_assistant import SuggestedReply
from model_decision import Decision, DecisionError, parse
import test_model_decision as fixtures
from test_model_stream import Chunks, chunk


def note(**kwargs):
    return dict({'op': 'remember', 'scope': 'personal', 'key': '兴趣', 'text': 'PRIVATE_NOTE'}, **kwargs)


def raw(ops, **kwargs):
    return json.dumps(dict({'kind': 'chat', 'reply': '正常回答。', 'action': None, 'memory': ops}, **kwargs), ensure_ascii=False)


class ValidationTests(unittest.TestCase):
    def test_every_validation_rule_has_safe_specific_code_and_index(self):
        cases = [
            ('not-list', 'memory_not_list', -1),
            ([note()] * 4, 'memory_too_many', 3),
            ([None], 'memory_op_invalid', 0),
            ([{'op': 'unknown'}], 'memory_op_invalid', 0),
            ([dict(note(), updated_at='PRIVATE_NOTE')], 'memory_fields_invalid', 0),
            ([note(scope='other')], 'memory_scope_invalid', 0),
            ([note(scope=[])], 'memory_scope_invalid', 0),
            ([note(scope='identity')], 'memory_identity_forbidden', 0),
            ([note(key='../path')], 'memory_key_invalid', 0),
            ([note(), note()], 'memory_key_duplicate', 1),
            ([note(importance=True)], 'memory_retention_invalid', 0),
            ([note(importance=1.5)], 'memory_retention_invalid', 0),
            ([note(durable='false')], 'memory_retention_invalid', 0),
            ([note(text=None)], 'memory_text_invalid', 0),
            ([note(text='  ')], 'memory_text_empty', 0),
            ([note(text='PRIVATE_NOTE' * 50)], 'memory_text_too_long', 0),
            ([note(text='[CQ:at,qq=1]')], 'memory_text_forbidden', 0),
            ([note(text='[[mechanism:PRIVATE_NOTE')], 'memory_text_forbidden', 0),
            ([note(text='PRIVATE_NOTE\x00')], 'memory_text_forbidden', 0),
        ]
        for ops, code, index in cases:
            with self.subTest(code=code):
                with self.assertRaises(memory.MemoryValidationError) as error:
                    memory.validate_ops(ops, 'private_7', {}, False)
                self.assertIsInstance(error.exception, ValueError)
                self.assertEqual((error.exception.code, error.exception.index), (code, index))
                self.assertNotIn('PRIVATE_NOTE', str(error.exception))
                decision = parse(raw(ops), 'private_7', {}, False)
                self.assertEqual((decision.reply, decision.memory, decision.memory_error), ('正常回答。', [], (code, index)))

    def test_channel_disabled_and_bad_scope_are_batch_errors(self):
        for key, config in (('private_7', {'long_term_memory': {'enabled': False}}), ('../outside', {})):
            with self.assertRaises(memory.MemoryValidationError) as error:
                memory.validate_ops([note()], key, config, True)
            self.assertEqual((error.exception.code, error.exception.index), ('memory_channel_unavailable', -1))
        self.assertEqual(memory.validate_ops([], '../outside', {}, False), [])

    def test_mixed_batch_is_dropped_not_partially_salvaged(self):
        result = parse(raw([note(), note(key='bad', text='x' * 501)]), 'private_7', {}, False)
        self.assertEqual(result.memory, [])
        self.assertEqual(result.memory_error, ('memory_text_too_long', 1))

    def test_core_errors_win_over_invalid_memory(self):
        cases = [
            ({'kind': 'unknown'}, 'invalid_kind_or_reply'),
            ({'reply': None}, 'invalid_kind_or_reply'),
            ({'action': {'action': 'proactive_on', 'args': {}}}, 'invalid_chat'),
            ({'reply': '  '}, 'invalid_chat'),
            ({'kind': 'clarify', 'reply': ' '}, 'empty_clarification'),
            ({'kind': 'operation', 'action': 'invalid'}, 'invalid_action_shape'),
            ({'kind': 'operation', 'action': {'action': 'proactive_on', 'args': {}}}, 'operation_denied_or_invalid'),
            ({'kind': 'inspect', 'action': {'action': 'unknown', 'args': {}}}, 'inspection_denied'),
        ]
        for kwargs, reason in cases:
            with self.subTest(reason=reason), patch.object(memory, 'validate_ops') as validator:
                with self.assertRaisesRegex(DecisionError, '^' + reason + '$'):
                    parse(raw('invalid', **kwargs), 'group_1_7', {}, False)
                validator.assert_not_called()

    def test_invalid_memory_cannot_salvage_malformed_core_envelope(self):
        for value in ('broken', raw('invalid')[:-1], raw('invalid') + '\n{}'):
            with self.assertRaises(DecisionError):
                parse(value, 'private_7', {}, False)

    def test_inspect_discards_invalid_notes_but_rejects_valid_nonempty_notes(self):
        action = {'action': 'inspect_group_files', 'args': {}}
        value = parse(raw('invalid', kind='inspect', reply='', action=action), 'group_1_7', {}, False)
        self.assertEqual((value.kind, value.action, value.memory_error), ('inspect', action, ('memory_not_list', -1)))
        with self.assertRaisesRegex(DecisionError, 'memory_requires_final_decision'):
            parse(raw([note()], kind='inspect', action=action), 'group_1_7', {}, False)

    def test_valid_memory_and_empty_legacy_decision_remain_compatible(self):
        value = parse(raw([note()]), 'private_7', {}, False)
        self.assertEqual(value.memory, [note()])
        self.assertIsNone(value.memory_error)
        self.assertIsNone(Decision('chat', 'text', None).memory_error)

    def test_unexpected_validation_exception_has_no_private_diagnostics(self):
        with patch.object(memory, 'validate_ops', side_effect=TypeError('PRIVATE_NOTE')):
            value = parse(raw([]), 'private_7', {}, False)
        self.assertEqual(value.memory_error, ('memory_validation_failed', -1))

    def test_exception_constructor_never_exposes_arbitrary_message(self):
        self.assertEqual(str(memory.MemoryValidationError('PRIVATE_NOTE', 'PRIVATE_NOTE')),
                         'memory_validation_failed index=-1')

    def test_guidance_includes_field_shape_uniqueness_and_optional_fallback(self):
        text = memory.guidance('private_7', {}, False)
        for phrase in ('可选附带项', 'op/scope/key/text', 'updated_at/uses/source', '1至40',
                       '同scope同key最多出现一次', '[CQ:', '[[mechanism:', '不合规时整批不保存'):
            self.assertIn(phrase, text)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SemanticIntegrationTests.setUp

    async def test_bad_batch_keeps_reply_without_write_retry_or_private_logs(self):
        self.create.return_value = fixtures.response(raw([note(), note(key='second', text='PRIVATE_NOTE' * 60)]))
        with patch.object(memory, 'commit') as commit:
            reply = await bot.ask_deepseek(self.key, '正常聊天')
        self.assertIn('正常回答', reply)
        self.assertIn('未保存', reply)
        self.assertIn('不能算作已经记住', reply)
        commit.assert_not_called()
        self.create.assert_awaited_once()
        self.assertFalse((self.base / memory.FILE).exists())
        self.assertIn('code=memory_text_too_long index=1', str(bot.log.call_args_list))
        self.assertNotIn('PRIVATE_NOTE', str(bot.log.call_args_list))
        self.assertNotIn('PRIVATE_NOTE', str(self.history))
        self.assertEqual(self.history[self.key][-1]['content'], reply)

    async def test_existing_memory_file_is_unchanged_after_invalid_mixed_batch(self):
        memory.commit(self.base, self.key, [note(text='原有笔记')], self.config, True, memory.snapshot(self.base, self.key))
        before = (self.base / memory.FILE).read_bytes()
        self.create.return_value = fixtures.response(raw([{'op': 'forget', 'scope': 'personal', 'key': '兴趣'}, note(text=0)]))
        reply = await bot.ask_deepseek(self.key, '再聊聊')
        self.assertIn('未保存', reply)
        self.assertEqual((self.base / memory.FILE).read_bytes(), before)

    async def test_streamed_valid_reply_with_invalid_memory_still_needs_one_call(self):
        value = raw([dict(note(), source='PRIVATE_NOTE')])
        self.create.return_value = Chunks([chunk(value[:20]), chunk(value[20:]), chunk(finish='stop')])
        reply = await bot.ask_deepseek(self.key, '流式聊天')
        self.assertIn('正常回答', reply)
        self.assertIn('未保存', reply)
        self.create.assert_awaited_once()
        self.assertTrue(self.create.await_args.kwargs['stream'])

    async def test_valid_memory_still_commits_once(self):
        self.create.return_value = fixtures.response(raw([note(text='共同兴趣')]))
        with patch.object(memory, 'commit', wraps=memory.commit) as commit:
            reply = await bot.ask_deepseek(self.key, '正常聊天')
        commit.assert_called_once()
        self.assertNotIn('未保存', reply)
        self.assertIn('共同兴趣', (self.base / memory.FILE).read_text(encoding='utf-8'))

    async def test_invalid_core_and_memory_do_not_create_pending_operation(self):
        self.create.return_value = fixtures.response(raw('bad', kind='operation', action={'action': 'exec', 'args': {}}))
        with patch.object(bot, '_execute_mechanism', AsyncMock()) as execute, patch.object(memory, 'commit') as commit:
            reply = await bot.ask_deepseek(self.key, '正常聊天')
        self.assertIn('没有返回有效', reply)
        self.assertFalse(self.router.pending)
        execute.assert_not_awaited()
        commit.assert_not_called()

    async def test_authorized_operation_keeps_confirmation_and_memory_notice(self):
        self.create.return_value = fixtures.response(raw('bad', kind='operation', reply='准备确认。' * 70,
            action={'action': 'proactive_level', 'args': {'level': '活跃'}}))
        with patch.object(bot, '_execute_mechanism', AsyncMock()) as execute:
            reply = await bot.ask_deepseek(self.key, '多参加聊天吧')
        self.assertIsInstance(reply, SuggestedReply)
        self.assertIn('回复“可以”执行', reply)
        self.assertIn('未保存', reply)
        self.assertTrue(self.router.pending)
        execute.assert_not_awaited()

    async def test_invalid_inspect_memory_does_not_block_query_or_final_reply(self):
        self.create.side_effect = [fixtures.response(raw('bad', kind='inspect', reply='',
            action={'action': 'inspect_group_files', 'args': {}})), fixtures.response(raw([], reply='查到了清单。'))]
        with patch.object(bot, '_semantic_inspection', AsyncMock(return_value={'files': []})) as inspect:
            reply = await bot.ask_deepseek(self.key, '群里有哪些文档')
        inspect.assert_awaited_once()
        self.assertEqual(reply, '查到了清单。')
        self.assertEqual(self.create.await_count, 2)
        self.assertFalse((self.base / memory.FILE).exists())
