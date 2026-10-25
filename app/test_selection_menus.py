import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml
import bot
import selection_menus as menus
from mechanism_assistant import MechanismAssistant


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qq-selection-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'prompts').mkdir()
        (self.root / 'personas').mkdir()
        for name in ('小鲸鱼', '另一风格'):
            (self.root / 'prompts' / (name + '.txt')).write_text('Style ' + name, encoding='utf-8')
        self.marker = self.root / 'prompts' / '_current_style.txt'
        self.marker.write_text('小鲸鱼', encoding='utf-8')
        (self.root / 'personas' / '内在人格.txt').write_text('Do not modify identity', encoding='utf-8')
        self.config = {
            'admin_qq': [7], 'current_provider': 'old', 'current_model': 'old-model',
            'deepseek': {'enabled': False, 'system_prompt': 'preserve fallback'},
            'llm_providers': {'old': {'enabled': True, 'models': ['old-model']},
                'new': {'enabled': True, 'models': ['new-model'], 'endpoint': 'https://new.example/v1', 'api_key': 'TEST_KEY'}},
            'image_download': {'proxy_only': True, 'image_proxy_url': 'http://127.0.0.1:18092'},
        }
        self.write_config()
        self.states = {}
        self.send = AsyncMock(return_value=True)
        self.router = Mock()
        self.fake = SimpleNamespace(BASE_DIR=self.root, user_states=self.states, bot_qq='900',
            current_provider='old', current_model='old-model', send_msg_to_user=self.send,
            load_config=self.read_config, _mechanism_key=bot._mechanism_key,
            is_admin=bot.is_admin, get_provider_config=bot.get_provider_config,
            get_available_models=bot.get_available_models, reload_llm_client=AsyncMock(),
            _get_mechanism_assistant=lambda: self.router, decision_transport=Mock(),
            _is_mentioned=lambda raw, _: '[CQ:at,qq=900]' in raw, _persona_prompt=Mock())
        self.data = self.event()

    def event(self, group=123, user=7, text=''):
        value = {'message_type': 'private' if group is None else 'group',
                 'sender': {'user_id': user, 'nickname': 'test'}, 'raw_message': text, 'message': []}
        if group is not None:
            value['group_id'] = group
        return value

    def write_config(self):
        (self.root / 'config.yaml').write_text(yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False), encoding='utf-8')

    def read_config(self):
        return yaml.safe_load((self.root / 'config.yaml').read_text(encoding='utf-8'))

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    async def handle(self, command, data=None):
        return await menus.handle(self.fake, data or self.data, command, self.read_config())

    async def to_models(self):
        await self.handle('修改模型')
        await self.handle('2')

    async def test_model_menu_displays_current_and_cancel_in_same_scope(self):
        await self.handle('修改模型')
        message = self.send.call_args.args[2]
        self.assertIn('当前模型：old / old-model', message)
        self.assertIn('取消', message)
        self.assertIn('group_123_7', self.states)
        self.assertNotIn('7', self.states)

    async def test_provider_selection_keeps_current_model_until_final_choice(self):
        before = self.snapshot()
        await self.to_models()
        self.assertIn('请选择 new 的模型', self.send.call_args.args[2])
        self.assertIn('当前模型：old / old-model', self.send.call_args.args[2])
        self.assertEqual(before, self.snapshot())

    async def test_cancel_provider_menu_does_not_write_any_file(self):
        before = self.snapshot()
        await self.handle('修改模型')
        self.assertTrue(await self.handle('取消'))
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.states)
        self.assertIn('已取消修改模型', self.send.call_args.args[2])
        self.assertFalse(await self.handle('1'))
        self.fake.reload_llm_client.assert_not_awaited()

    async def test_cancel_final_model_menu_keeps_original_settings(self):
        before = self.snapshot()
        await self.to_models()
        self.assertTrue(await self.handle('不改了'))
        self.assertEqual(before, self.snapshot())
        self.fake.reload_llm_client.assert_not_awaited()

    async def test_style_menu_and_cancel_show_current_without_changes(self):
        self.data['raw_message'] = '[CQ:at,qq=900] 切换风格'
        before = self.snapshot()
        await self.handle('切换风格')
        self.assertIn('当前风格：小鲸鱼', self.send.call_args.args[2])
        self.assertNotIn('私聊我', self.send.call_args.args[2])
        await self.handle('算了')
        self.assertIn('已取消切换风格', self.send.call_args.args[2])
        self.assertEqual(before, self.snapshot())

    async def test_selected_model_preserves_image_routing_and_other_config(self):
        await self.to_models()
        await self.handle('1')
        config = self.read_config()
        self.assertEqual(config['current_model'], 'new-model')
        self.assertEqual(config['current_provider'], 'new')
        self.assertEqual(config['image_download'], self.config['image_download'])
        self.assertEqual(config['deepseek']['system_prompt'], 'preserve fallback')
        self.fake.reload_llm_client.assert_awaited_once()
        self.assertFalse(self.states)

    async def test_selected_style_changes_only_marker(self):
        self.data = self.event(group=None)
        before = self.snapshot()
        await self.handle('切换风格')
        state = next(iter(self.states.values()))
        index = state['choices'].index('另一风格') + 1
        await self.handle(str(index))
        self.assertEqual(self.marker.read_text(encoding='utf-8'), '另一风格')
        after = self.snapshot()
        changed = [name for name in before if before[name] != after[name]]
        self.assertEqual(changed, ['prompts/_current_style.txt'])

    async def test_admin_revoked_after_menu_open_cannot_select(self):
        await self.to_models()
        self.config['admin_qq'] = []
        self.write_config()
        before = self.snapshot()
        await self.handle('1')
        self.assertIn('没有修改模型的权限', self.send.call_args.args[2])
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.states)

    async def test_disabled_provider_cannot_be_selected(self):
        await self.handle('修改模型')
        self.config['llm_providers']['new']['enabled'] = False
        self.write_config()
        await self.handle('2')
        self.assertFalse(self.states)
        self.assertEqual(self.read_config()['current_model'], 'old-model')

    async def test_removed_model_is_revalidated_before_commit(self):
        await self.to_models()
        self.config['llm_providers']['new']['models'] = []
        self.write_config()
        await self.handle('1')
        self.assertIn('当前不可用', self.send.call_args.args[2])
        self.assertEqual(self.read_config()['current_model'], 'old-model')

    async def test_global_model_change_invalidates_old_menu(self):
        await self.to_models()
        self.config['current_model'] = 'changed-by-other-admin'
        self.write_config()
        await self.handle('1')
        self.assertIn('设置已变化', self.send.call_args.args[2])
        self.assertEqual(self.read_config()['current_model'], 'changed-by-other-admin')

    async def test_global_style_change_invalidates_old_menu(self):
        self.data = self.event(group=None)
        await self.handle('切换风格')
        self.marker.write_text('另一风格', encoding='utf-8')
        await self.handle('1')
        self.assertIn('设置已变化', self.send.call_args.args[2])
        self.assertEqual(self.marker.read_text(encoding='utf-8'), '另一风格')

    async def test_expired_menu_does_not_apply(self):
        await self.to_models()
        expires = next(iter(self.states.values()))['expires']
        with patch.object(menus.time, 'monotonic', return_value=expires + 1):
            await self.handle('1')
        self.assertIn('已过期', self.send.call_args.args[2])
        self.assertEqual(self.read_config()['current_model'], 'old-model')

    async def test_bad_number_keeps_menu_and_reminds_cancel(self):
        await self.handle('修改模型')
        for number in ('0', '999', '1' * 10000):
            self.assertTrue(await self.handle(number))
            self.assertIn('取消', self.send.call_args.args[2])
            self.assertIn('group_123_7', self.states)

    async def test_same_user_cannot_select_or_cancel_from_another_chat(self):
        await self.handle('修改模型')
        for data in (self.event(group=None), self.event(group=456)):
            self.assertFalse(await self.handle('1', data))
            self.assertFalse(await self.handle('取消', data))
        self.assertIn('group_123_7', self.states)

    async def test_other_user_cannot_cancel_owners_menu(self):
        await self.handle('修改模型')
        self.assertFalse(await self.handle('取消', self.event(user=8)))
        self.assertIn('group_123_7', self.states)

    async def test_menu_replacement_has_only_one_current_flow(self):
        await self.handle('修改模型')
        self.data['raw_message'] = '[CQ:at,qq=900] 切换风格'
        await self.handle('切换风格')
        self.assertEqual(len(self.states), 1)
        self.assertEqual(next(iter(self.states.values()))['kind'], 'style')

    async def test_reply_addressed_to_another_person_does_not_consume_menu(self):
        await self.handle('修改模型')
        self.data['raw_message'] = '[CQ:at,qq=888] 1'
        self.assertFalse(await self.handle('1'))
        self.assertEqual(next(iter(self.states.values()))['stage'], 'provider')

    async def test_new_topic_exits_menu_and_returns_to_normal_chat(self):
        await self.handle('修改模型')
        self.assertFalse(await self.handle('今天天气怎么样'))
        self.assertFalse(self.states)

    async def test_failed_delivery_does_not_leave_active_choice(self):
        self.send.return_value = False
        await self.handle('修改模型')
        self.assertFalse(self.states)
        self.assertFalse(await self.handle('1'))

    async def test_cancel_during_menu_delivery_cannot_reactivate_it(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def deliver(data, config, text):
            if text.startswith('正在'):
                started.set()
                await finish.wait()
            return True
        self.send.side_effect = deliver
        opening = asyncio.create_task(self.handle('修改模型'))
        await started.wait()
        await self.handle('取消')
        finish.set()
        await opening
        self.assertFalse(self.states)

    async def test_non_admin_cannot_open_menu_or_destroy_style_menu(self):
        self.data = self.event(group=None, user=8)
        await self.handle('切换风格')
        await self.handle('修改模型')
        self.assertIn('没有权限', self.send.call_args.args[2])
        self.assertEqual(next(iter(self.states.values()))['kind'], 'style')

    async def test_confirmed_natural_model_operation_rechecks_admin(self):
        self.config['admin_qq'] = []
        self.write_config()
        await menus.set_model(self.fake, self.data, 'new', 'new-model')
        self.assertIn('没有修改模型的权限', self.send.call_args.args[2])
        self.assertEqual(self.read_config()['current_model'], 'old-model')

    async def test_mechanism_confirmation_accepts_the_same_cancel_aliases(self):
        for word in menus.CANCEL_WORDS:
            router = MechanismAssistant()
            pending = router.prepare('private_7', {'action': 'model_menu', 'args': {}}, 'sig')
            router.activate('private_7', pending.ticket)
            status, operation = router.respond('private_7', word, 'sig', self.read_config(), True)
            self.assertEqual(status, 'cancelled', word)
            self.assertIsNone(operation)

    async def test_real_private_hook_handles_menu_and_cancel_without_model_calls(self):
        self.data = self.event(group=None)
        with patch.multiple(bot, BASE_DIR=self.root, user_states=self.states, bot_qq='900',
                            load_config=self.read_config, send_msg_to_user=self.send,
                            decision_transport=Mock(), _get_mechanism_assistant=lambda: self.router), \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask:
            for command in ('修改模型', '取消'):
                self.data['raw_message'] = command
                await bot.on_private_message(self.data)
            ask.assert_not_awaited()
        self.assertFalse(self.states)
        self.assertIn('已取消修改模型', self.send.call_args.args[2])

    async def test_real_group_hook_handles_style_menu_in_current_group(self):
        with patch.multiple(bot, BASE_DIR=self.root, user_states=self.states, bot_qq='900',
                            load_config=self.read_config, send_msg_to_user=self.send,
                            decision_transport=Mock(), _get_mechanism_assistant=lambda: self.router,
                            _get_proactive_mode=Mock(return_value=Mock()), group_file_state=Mock()), \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask:
            for command in ('[CQ:at,qq=900] 切换风格', '取消'):
                self.data['raw_message'] = command
                await bot.on_group_message(self.data)
            ask.assert_not_awaited()
        self.assertFalse(self.states)
        self.assertIn('已取消切换风格', self.send.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
