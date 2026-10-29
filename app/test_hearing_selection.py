import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import hearing
import selection_menus as menus
from mechanism_assistant import validate, describe
from model_decision import instructions
import test_selection_menus


class HearingSelectionTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_selection_menus.SelectionTests.setUp
    event = test_selection_menus.SelectionTests.event
    write_config = test_selection_menus.SelectionTests.write_config
    read_config = test_selection_menus.SelectionTests.read_config
    snapshot = test_selection_menus.SelectionTests.snapshot
    handle = test_selection_menus.SelectionTests.handle

    async def select(self):
        await self.handle('选择听觉模型')
        await self.handle('2')
        await self.handle('1')

    async def test_choice_changes_only_hearing_and_restores_after_restart(self):
        before = self.snapshot()
        await self.select()
        self.assertEqual(self.read_config(), self.config)
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertEqual(self.fake.current_provider, 'old')
        self.assertEqual(self.fake.current_model, 'old-model')
        self.fake.reload_llm_client.assert_not_awaited()
        self.fake.hearing_sync = None
        route = hearing.route(self.fake, self.read_config())
        self.assertEqual(route['model'], 'new-model')
        self.assertEqual(route['api_key'], 'TEST_KEY')
        self.assertTrue(route['enabled'])
        state = (self.root / 'hearing_sync_state.json').read_text(encoding='utf-8')
        self.assertNotIn('TEST_KEY', state)
        self.assertNotIn('https://', state)
        self.assertFalse(json.loads(state)['enabled'])

    async def test_sync_is_left_atomically_and_original_independent_config_is_kept(self):
        self.config['audio_input'] = {'enabled': False, 'model': 'independent', 'api_key': 'OLD_KEY',
            'endpoint': 'https://original.example/v1', 'trigger_groups': [123], 'include_frames': False}
        self.write_config()
        self.fake._init_deepseek = Mock()
        self.fake.deepseek_client = SimpleNamespace(api_key='MAIN_KEY', base_url='https://main.example/v1')
        switch = hearing.sync(self.fake)
        switch.set_enabled(True)
        await self.select()
        self.assertFalse(switch.enabled)
        switch.set_enabled(True)
        self.assertEqual(hearing.route(self.fake, self.read_config())['model'], 'old-model')
        switch.set_enabled(False)
        route = hearing.route(self.fake, self.read_config())
        self.assertEqual(route['model'], 'new-model')
        self.assertEqual(route['trigger_groups'], [123])
        self.assertEqual(self.read_config()['audio_input'], self.config['audio_input'])

    async def test_cancel_does_not_write_hearing_state_or_config(self):
        before = self.snapshot()
        await self.handle('選擇聽覺模型')
        self.assertIn('当前听觉模型', self.send.call_args.args[2])
        self.assertIn('不会修改主模型', self.send.call_args.args[2])
        await self.handle('2')
        await self.handle('取消')
        self.assertEqual(before, self.snapshot())
        self.assertIn('已取消选择听觉模型', self.send.call_args.args[2])

    async def test_permissions_rechecked_on_open_and_commit(self):
        self.assertTrue(await self.handle('选择听觉模型', self.event(user=8)))
        self.assertFalse(self.states)
        await self.handle('选择听觉模型')
        await self.handle('2')
        self.config['admin_qq'] = []
        self.write_config()
        await self.handle('1')
        self.assertIn('没有选择听觉模型的权限', self.send.call_args.args[2])
        self.assertFalse((self.root / 'hearing_sync_state.json').exists())

    async def test_cross_chat_and_other_user_cannot_use_selection(self):
        await self.handle('选择听觉模型')
        for event in (self.event(group=None), self.event(group=456), self.event(user=8)):
            self.assertFalse(await self.handle('1', event))
            self.assertFalse(await self.handle('取消', event))
        self.assertEqual(self.states['group_123_7']['stage'], 'provider')

    async def test_expiry_and_concurrent_change_reject_old_choice(self):
        await self.handle('选择听觉模型')
        expires = self.states['group_123_7']['expires']
        with patch.object(menus.time, 'monotonic', return_value=expires + 1):
            await self.handle('1')
        self.assertIn('已过期', self.send.call_args.args[2])
        await self.handle('选择听觉模型')
        hearing.sync(self.fake).select('new', 'new-model')
        await self.handle('1')
        self.assertIn('设置已变化', self.send.call_args.args[2])

    async def test_pool_credentials_follow_edits_but_disabled_or_removed_route_fails_closed(self):
        await self.select()
        original_signature = hearing.signature(self.fake, self.read_config())
        self.config['llm_providers']['new']['api_key'] = 'ROTATED_KEY'
        self.write_config()
        self.assertEqual(hearing.route(self.fake, self.read_config())['api_key'], 'ROTATED_KEY')
        self.assertNotEqual(original_signature, hearing.signature(self.fake, self.read_config()))
        for field, value in (('enabled', False), ('models', []), ('api_key', '')):
            cfg = self.read_config()
            cfg['llm_providers']['new'][field] = value
            self.assertFalse(hearing.route(self.fake, cfg)['enabled'])
            self.assertEqual(hearing.route(self.fake, cfg)['api_key'], '')

    async def test_pool_model_removed_before_commit_cannot_be_saved(self):
        await self.handle('选择听觉模型')
        await self.handle('2')
        self.config['llm_providers']['new']['models'] = []
        self.write_config()
        await self.handle('1')
        self.assertIn('当前不可用', self.send.call_args.args[2])
        self.assertFalse((self.root / 'hearing_sync_state.json').exists())

    async def test_missing_credentials_do_not_change_settings(self):
        self.config['llm_providers']['new']['api_key'] = ''
        self.write_config()
        await self.select()
        self.assertIn('缺少接口或密钥', self.send.call_args.args[2])
        self.assertFalse((self.root / 'hearing_sync_state.json').exists())

    async def test_atomic_save_failure_keeps_previous_route_and_sync(self):
        switch = hearing.sync(self.fake)
        switch.set_enabled(True)
        before = switch.path.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('disk')):
            await menus.set_hearing_model(self.fake, self.data, 'new', 'new-model')
        self.assertTrue(switch.enabled)
        self.assertIsNone(switch.selection)
        self.assertEqual(switch.path.read_bytes(), before)
        self.assertIn('未保存', self.send.call_args.args[2])

    async def test_old_boolean_file_migrates_and_corrupt_selection_blocks(self):
        path = self.root / 'hearing_sync_state.json'
        path.write_text('{"version":1,"enabled":false}', encoding='utf-8')
        self.assertTrue(hearing.HearingSync(path).storage_ok)
        path.write_text('{"version":1,"enabled":false,"selection":[]}', encoding='utf-8')
        self.fake.hearing_sync = None
        self.assertFalse(hearing.route(self.fake, self.read_config())['enabled'])
        await menus.set_hearing_model(self.fake, self.data, 'new', 'new-model')
        self.assertIn('未保存', self.send.call_args.args[2])

    async def test_real_private_hook_opens_and_cancels_without_main_model_call(self):
        data = self.event(group=None)
        with patch.multiple(bot, BASE_DIR=self.root, user_states=self.states, bot_qq='900',
                load_config=self.read_config, send_msg_to_user=self.send,
                decision_transport=Mock(), _get_mechanism_assistant=lambda: self.router), \
             patch.object(bot, 'hearing_sync', None, create=True), \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask:
            for command in ('选择听觉模型', '取消'):
                data['raw_message'] = command
                await bot.on_private_message(data)
            ask.assert_not_awaited()
        self.assertFalse(self.states)

    async def test_real_group_hook_and_traditional_command(self):
        with patch.multiple(bot, BASE_DIR=self.root, user_states=self.states, bot_qq='900',
                load_config=self.read_config, send_msg_to_user=self.send,
                decision_transport=Mock(), _get_mechanism_assistant=lambda: self.router,
                _get_proactive_mode=Mock(return_value=Mock()), group_file_state=Mock()), \
             patch.object(bot, 'hearing_sync', None, create=True), \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask:
            for command in ('[CQ:at,qq=900] 選擇聽覺模型', '取消'):
                self.data['raw_message'] = command
                await bot.on_group_message(self.data)
            ask.assert_not_awaited()
        self.assertFalse(self.states)

    async def test_natural_action_is_admin_only_and_distinct_from_main_model_action(self):
        self.assertEqual(validate('hearing_model_menu', {}, 'private_7', True)['action'], 'hearing_model_menu')
        with self.assertRaises(ValueError):
            validate('hearing_model_menu', {}, 'private_8', False)
        self.assertIn('不改变主模型', describe({'action': 'hearing_model_menu', 'args': {}}))
        self.assertIn('hearing_model_menu', instructions('private_7', self.config, True))


if __name__ == '__main__':
    unittest.main()
