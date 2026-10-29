from collections import defaultdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
import long_term_memory as memory
from mechanism_assistant import MechanismAssistant, SuggestedReply
from model_decision import DecisionError, DecisionTransport, instructions, parse
from persona import load_persona


def note(scope='personal', key='爱好', text='喜欢讨论时间哲学'):
    return {'op': 'remember', 'scope': scope, 'key': key, 'text': text}


def raw(ops=None, kind='chat', action=None, reply='我想把这段相处留下。'):
    value = {'kind': kind, 'reply': reply, 'action': action}
    if ops is not None:
        value['memory'] = ops
    return json.dumps(value, ensure_ascii=False)


def response(value):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=value), finish_reason='stop')])


class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = 'private_7'

    def write(self, ops, key=None, admin=False):
        key = key or self.key
        return memory.commit(self.root, key, ops, {}, admin, memory.snapshot(self.root, key))

    def test_persistent_notes_update_same_label_and_forget_without_expiry(self):
        self.write([note()])
        before = (self.root / memory.FILE).read_bytes()
        self.assertIn('时间哲学', memory.personal_prompt(self.root, self.key, {}))
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())
        self.write([note(text='现在更关注语言哲学')])
        state = json.loads((self.root / memory.FILE).read_text(encoding='utf-8'))
        self.assertEqual(len(state['personal'][self.key]), 1)
        self.assertEqual(state['personal'][self.key]['爱好']['source'], self.key)
        self.write([{'op': 'forget', 'scope': 'personal', 'key': '爱好'}])
        self.assertEqual(memory.personal_prompt(self.root, self.key, {}), '')

    def test_group_people_and_private_notes_are_isolated(self):
        self.write([note(text='私聊约定不在群里提起')])
        self.write([note(text='甲群的相处')], 'group_11_7')
        self.write([note(text='乙群的相处')], 'group_22_7')
        self.assertIn('私聊约定', memory.personal_prompt(self.root, 'private_7', {}))
        self.assertNotIn('私聊约定', memory.personal_prompt(self.root, 'group_11_7', {}))
        self.assertNotIn('乙群', memory.personal_prompt(self.root, 'group_11_7', {}))
        self.assertEqual(memory.personal_prompt(self.root, 'group_11_8', {}), '')
        self.assertNotIn('私聊约定', memory.identity_prompt(self.root, {}))

    def test_identity_retains_name_across_style_change_and_fresh_load(self):
        (self.root / 'prompts').mkdir()
        (self.root / 'prompts' / '小鲸鱼.txt').write_text('默认小名小鲸鱼', encoding='utf-8')
        (self.root / 'prompts' / '工程版.txt').write_text('工程版口吻', encoding='utf-8')
        self.write([note('identity', '自我称谓', '我的名字是汐蓝，小名小汐。')], admin=True)
        prompt, _, old_signature = load_persona(self.root, {})
        self.assertIn('汐蓝', prompt)
        self.assertNotIn('private_7', prompt)
        (self.root / 'prompts' / '_current_style.txt').write_text('工程版', encoding='utf-8')
        updated, style, signature = load_persona(self.root, {})
        self.assertEqual(style, '工程版')
        self.assertIn('汐蓝', updated)
        self.assertIn('小汐', updated)
        self.assertNotEqual(signature, old_signature)

    def test_identity_requires_real_admin_and_no_destination_argument(self):
        for ops, key in [([note('identity')], self.key),
                         ([dict(note(), user_id='8')], self.key), ([note()], '../other')]:
            with self.subTest(ops=ops), self.assertRaises(ValueError):
                memory.validate_ops(ops, key, {}, False)
        self.assertFalse((self.root / memory.FILE).exists())

    def test_bad_operations_cannot_become_executable_text_or_paths(self):
        for ops in [[note(key='../secret')], [note(text='[CQ:at,qq=all]')],
                    [note(text='x' * 501)], [note()] * 4,
                    [note(), note(text='contradiction')], [{'op': 'exec'}]]:
            with self.subTest(ops=ops), self.assertRaises(ValueError):
                memory.validate_ops(ops, self.key, {}, False)

    def test_capacity_failure_does_not_partially_write(self):
        for index in range(12):
            self.write([note(key=str(index), text='字' * 500)])
        before = (self.root / memory.FILE).read_bytes()
        with self.assertRaises(ValueError):
            self.write([note(key='overflow', text='字' * 500), note(key='overflow2', text='字' * 500),
                        {'op': 'forget', 'scope': 'personal', 'key': '0'}])
        # A failed mixed update/delete batch must preserve all existing rows.
        self.assertEqual(before, (self.root / memory.FILE).read_bytes())

    def test_corrupt_file_is_preserved_and_not_reinitialized(self):
        path = self.root / memory.FILE
        path.write_bytes(b'broken memory')
        with self.assertRaises(memory.MemoryUnavailable):
            memory.snapshot(self.root, self.key)
        with self.assertRaises(memory.MemoryUnavailable):
            memory.commit(self.root, self.key, [note()], {}, False, {})
        self.assertEqual(path.read_bytes(), b'broken memory')
        self.assertIn('不可读', memory.identity_prompt(self.root, {}))

    def test_atomic_replace_failure_retains_old_file_and_cleans_temporary(self):
        self.write([note()])
        before = (self.root / memory.FILE).read_bytes()
        with patch.object(memory.os, 'replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.write([note(text='new note')])
        self.assertEqual((self.root / memory.FILE).read_bytes(), before)
        self.assertFalse(list(self.root.glob('.autonomous-memory-*.tmp')))

    def test_same_scope_stale_write_rejected_other_scopes_merge(self):
        snapshot = memory.snapshot(self.root, self.key)
        other = memory.snapshot(self.root, 'private_8')
        self.write([note()])
        with self.assertRaises(memory.MemoryUnavailable):
            memory.commit(self.root, self.key, [note(text='stale')], {}, False, snapshot)
        memory.commit(self.root, 'private_8', [note(text='another person')], {}, False, other)
        self.assertIn('时间哲学', memory.personal_prompt(self.root, self.key, {}))
        self.assertIn('another person', memory.personal_prompt(self.root, 'private_8', {}))

    def test_disabled_feature_hides_notes_and_rejects_new_writes(self):
        self.write([note()])
        cfg = {'long_term_memory': {'enabled': False}}
        self.assertEqual(memory.personal_prompt(self.root, self.key, cfg), '')
        self.assertEqual(memory.guidance(self.key, cfg, False), '')
        with self.assertRaises(ValueError):
            memory.commit(self.root, self.key, [note()], cfg, False, memory.snapshot(self.root, self.key))


class ProtocolTests(unittest.TestCase):
    def test_optional_memory_keeps_existing_three_field_protocol_compatible(self):
        self.assertEqual(parse(raw(), 'private_7', {}, False).memory, [])
        self.assertEqual(parse(raw([note()]), 'private_7', {}, False).memory, [note()])

    def test_invalid_or_unprivileged_memory_is_discarded_without_losing_reply(self):
        for value in [raw([note('identity')]), raw([note(text='x' * 501)]),
                      raw([dict(note(), target='private_8')]), raw('invalid')]:
            result = parse(value, 'private_7', {}, False)
            self.assertEqual(result.memory, [])
            self.assertTrue(result.reply)
            self.assertIsNotNone(result.memory_error)

    def test_inspection_is_readonly_even_when_model_proposes_memory(self):
        with self.assertRaises(DecisionError):
            parse(raw([note()], kind='inspect', action={'action': 'inspect_group_files', 'args': {}}),
                  'group_1_7', {}, False)

    def test_guidance_describes_autonomy_and_true_durable_channel(self):
        text = instructions('private_7', {}, False)
        self.assertIn('不需要逐条询问', text)
        self.assertIn('不会增加模型调用', text)
        self.assertIn('真实的自主长期记忆', text)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'prompts').mkdir()
        (self.root / 'prompts' / '小鲸鱼.txt').write_text('可爱的鲸鱼语气', encoding='utf-8')
        self.config = {'admin_qq': [7], 'deepseek': {'enabled': True, 'max_history': 2}}
        self.create = AsyncMock(return_value=response(raw([note()])))
        self.client = SimpleNamespace(base_url='https://synthetic.invalid',
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.router = MechanismAssistant()
        for item in (patch.object(bot, 'BASE_DIR', self.root), patch.object(bot, 'load_config', return_value=self.config),
                     patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', self.client),
                     patch.object(bot, '_persona_signature', None), patch.object(bot, 'proactive_mode', None),
                     patch.object(bot, 'mechanism_assistant', self.router), patch.object(bot, 'decision_transport', DecisionTransport()),
                     patch.object(bot, '_get_whale_stickers', return_value=None),
                     patch.object(bot, '_semantic_state', AsyncMock(return_value={'可选人格（全局）': ['小鲸鱼']})),
                     patch.object(bot, 'conversations', defaultdict(list)), patch.object(bot, 'log')):
            self.enterContext(item)

    async def test_model_autonomously_writes_without_magic_word_confirmation_or_extra_call(self):
        reply = await bot.ask_deepseek('private_7', '今天聊的这些想法很有意思')
        self.assertEqual(reply, '我想把这段相处留下。')
        self.create.assert_awaited_once()
        self.assertFalse(self.router.pending)
        self.assertIn('时间哲学', memory.personal_prompt(self.root, 'private_7', {}))
        self.assertNotIn('"memory"', bot.conversations['private_7'][-1]['content'])

    async def test_next_turn_after_history_clear_reads_saved_notes(self):
        await bot.ask_deepseek('private_7', '聊聊最近的想法')
        bot.conversations.clear()
        self.create.return_value = response(raw([]))
        await bot.ask_deepseek('private_7', '还记得吗')
        messages = self.create.call_args.kwargs['messages']
        self.assertIn('时间哲学', messages[0]['content'])

    async def test_admin_identity_updates_global_next_turn_and_keeps_txt_unchanged(self):
        path = self.root / 'prompts' / '小鲸鱼.txt'
        before = path.read_bytes()
        self.create.return_value = response(raw([note('identity', '自我称谓', '名字汐蓝，小名小汐。')]))
        await bot.ask_deepseek('private_7', '我觉得汐蓝挺适合你')
        self.assertIn('汐蓝', load_persona(self.root, {})[0])
        self.assertEqual(path.read_bytes(), before)
        self.create.return_value = response(raw([]))
        await bot.ask_deepseek('group_11_8', '你叫什么')
        content = self.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('汐蓝', content)

    async def test_disk_failure_cannot_be_reported_as_success(self):
        with patch.object(memory, 'commit', side_effect=OSError('disk failure')):
            self.create.return_value = response(raw([note()], reply='我记住啦。'))
            reply = await bot.ask_deepseek('private_7', '说说想法')
        self.assertIn('不能算作已经保存', reply)
        self.assertFalse((self.root / memory.FILE).exists())

    async def test_invalid_output_or_changed_style_saves_no_memory(self):
        async def changed(**kwargs):
            (self.root / 'prompts' / '小鲸鱼.txt').write_text('新风格', encoding='utf-8')
            return response(raw([note()]))
        self.create.side_effect = changed
        reply = await bot.ask_deepseek('private_7', '你好')
        self.assertIn('取消', reply)
        self.assertFalse((self.root / memory.FILE).exists())

    async def test_memory_and_operation_can_coexist_with_valid_confirmation_signature(self):
        self.create.return_value = response(raw([note('identity', '自我称谓', '名字汐蓝。')], kind='operation',
            action={'action': 'style_set', 'args': {'name': '小鲸鱼'}}, reply='要用这个风格吗？'))
        reply = await bot.ask_deepseek('private_7', '这个风格挺合适，也想起我们的名字')
        self.assertIsInstance(reply, SuggestedReply)
        self.router.activate('private_7', reply.mechanism_ticket)
        status, _ = self.router.respond('private_7', '可以', bot._mechanism_signature(self.config), self.config, True)
        self.assertEqual(status, 'confirmed')


if __name__ == '__main__':
    unittest.main()
