import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import long_term_memory
from mechanism_assistant import validate
from persona import load_persona
import selection_menus
import style_catalog
import style_enhancement as enhance
import switch_style
import test_selection_menus as fixtures


class EnhancementTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SelectionTests.setUp
    event = fixtures.SelectionTests.event
    write_config = fixtures.SelectionTests.write_config
    read_config = fixtures.SelectionTests.read_config
    snapshot = fixtures.SelectionTests.snapshot

    def character(self):
        path = self.root / 'prompts' / '亚托....txt'
        path.write_text('现在你将扮演一名叫做“亚托莉”的角色。自称亚托莉，称对方夏生。', encoding='utf-8')
        self.marker.write_text('亚托...', encoding='utf-8')
        return path

    async def command(self, command, user=7, group=None, mentioned=True):
        data = self.event(user=user, group=group, text=('[CQ:at,qq=900] ' if group is not None and mentioned else '') + command)
        return await enhance.handle(self.fake, data, command, self.read_config())

    def test_full_names_from_explicit_character_declarations(self):
        for stem, text, expected in (
            ('亚托...', '现在你将扮演一名叫做“亚托莉”的角色。', '亚托莉'),
            ('天使...', '你将扮演一名叫“天使奈奈菜”的女孩子。', '天使奈奈菜'),
            ('真寻', '现在你将模仿一名名叫绪山真寻的哥哥。', '绪山真寻'),
            ('穹妹', '人物名称:春日野穹\n角色资料', '春日野穹'),
        ):
            self.assertEqual(style_catalog.character_name(stem, text), expected)

    def test_unknown_clipped_label_is_not_guessed(self):
        self.assertEqual(style_catalog.character_name('陌生...', '只有一些语气描述'), '陌生...')

    def test_encoded_duplicate_is_hidden_without_changing_any_file(self):
        normal = self.character()
        encoded = normal.with_name('#U4e9a#U6258....txt')
        encoded.write_bytes(normal.read_bytes())
        before = self.snapshot()
        labels = style_catalog.available(self.root)
        self.assertEqual(sum(label.startswith('亚托莉') for label in labels), 1)
        self.assertEqual(labels['亚托莉'], normal)
        self.assertEqual(before, self.snapshot())

    def test_different_same_name_copy_stays_selectable(self):
        normal = self.character()
        encoded = normal.with_name('#U4e9a#U6258....txt')
        encoded.write_text(normal.read_text(encoding='utf-8') + '不同的自定义内容', encoding='utf-8')
        labels = style_catalog.available(self.root)
        self.assertIn('亚托莉', labels)
        self.assertIn('亚托莉（同名副本2）', labels)
        self.marker.write_text(encoded.stem, encoding='utf-8')
        self.assertEqual(load_persona(self.root, self.config)[1], '亚托莉（同名副本2）')
        self.assertIn('不同的自定义内容', load_persona(self.root, self.config)[0])

    def test_encoded_only_file_and_old_marker_resolve_full_name(self):
        path = self.root / 'prompts' / '#U4e9a#U6258....txt'
        path.write_text('你将扮演一名叫做“亚托莉”的角色。', encoding='utf-8')
        self.marker.write_text(path.stem, encoding='utf-8')
        before = self.snapshot()
        self.assertEqual(load_persona(self.root, self.config)[1], '亚托莉')
        self.assertEqual(before, self.snapshot())

    def test_clipped_existing_marker_displays_full_name(self):
        self.character()
        self.assertEqual(load_persona(self.root, self.config)[1], '亚托莉')

    def test_disabled_by_default_and_survives_reload_when_enabled(self):
        self.assertFalse(enhance.read(self.root))
        self.assertFalse((self.root / enhance.FILE).exists())
        enhance.set_enabled(self.root, True)
        self.assertTrue(enhance.read(self.root))
        enhance.set_enabled(self.root, False)
        self.assertFalse(enhance.read(self.root))

    def test_corrupt_state_is_not_overwritten(self):
        path = self.root / enhance.FILE
        path.write_text('{broken', encoding='utf-8')
        before = path.read_bytes()
        self.assertFalse(enhance.enabled(self.root))
        with self.assertRaises(enhance.StateError):
            enhance.set_enabled(self.root, True)
        self.assertEqual(path.read_bytes(), before)

    async def test_non_admin_cannot_change_global_mode(self):
        await self.command('风格增强 开启', user=8)
        self.assertIn('只有配置管理员', self.send.call_args.args[2])
        self.assertFalse((self.root / enhance.FILE).exists())

    async def test_on_and_off_change_only_overlay_state(self):
        self.character()
        before = self.snapshot()
        await self.command('风格增强 开启')
        self.assertTrue(enhance.read(self.root))
        self.assertIn('当前风格：亚托莉', self.send.call_args.args[2])
        await self.command('风格增强 关闭')
        self.assertFalse(enhance.read(self.root))
        self.assertTrue(all((self.root / name).read_bytes() == value for name, value in before.items()))
        self.assertEqual(self.fake.current_model, 'old-model')

    async def test_group_command_requires_mention(self):
        self.assertFalse(await self.command('风格增强 开启', group=123, mentioned=False))
        self.assertFalse((self.root / enhance.FILE).exists())
        self.assertTrue(await self.command('风格增强 开启', group=123))
        self.assertTrue(enhance.read(self.root))

    async def test_status_is_read_only_and_visible_to_regular_users(self):
        before = self.snapshot()
        await self.command('风格增强 状态', user=8)
        self.assertIn('关闭（全局）', self.send.call_args.args[2])
        self.assertEqual(before, self.snapshot())

    async def test_bare_command_enables_and_traditional_alias_can_disable(self):
        await self.command('风格增强')
        self.assertTrue(enhance.read(self.root))
        await self.command('風格增強 關閉')
        self.assertFalse(enhance.read(self.root))

    def test_enhanced_prompt_retains_identity_memory_without_name_override(self):
        self.character()
        key = 'private_7'
        expected = long_term_memory.snapshot(self.root, key)
        long_term_memory.commit(self.root, key, [{'op': 'remember', 'scope': 'identity', 'key': 'name', 'text': '持续名字汐蓝，小名小汐。'}], self.config, True, expected)
        before = (self.root / long_term_memory.FILE).read_bytes()
        original = load_persona(self.root, self.config)
        self.assertIn('可覆盖风格TXT中的默认称谓', original[0])
        enhance.set_enabled(self.root, True)
        active = load_persona(self.root, self.config)
        self.assertIn('汐蓝', active[0])
        self.assertNotIn('可覆盖风格TXT中的默认称谓', active[0])
        self.assertIn('不能一律叫主人', active[0])
        self.assertIn('当前风格显示名称：亚托莉', active[0])
        self.assertNotEqual(original[2], active[2])
        self.assertEqual((self.root / long_term_memory.FILE).read_bytes(), before)
        enhance.set_enabled(self.root, False)
        self.assertEqual(load_persona(self.root, self.config)[2], original[2])

    def test_enhancement_follows_next_selected_role_and_does_not_rewrite_files(self):
        self.character()
        enhance.set_enabled(self.root, True)
        before = self.snapshot()
        self.marker.write_text('另一风格', encoding='utf-8')
        prompt, name, _ = load_persona(self.root, self.config)
        self.assertEqual(name, '另一风格')
        self.assertIn('当前风格显示名称：另一风格', prompt)
        self.assertTrue(enhance.read(self.root))
        self.assertTrue(all((self.root / key).read_bytes() == value for key, value in before.items() if key != 'prompts/_current_style.txt'))

    def test_overlay_does_not_grant_style_text_permission(self):
        enhance.set_enabled(self.root, True)
        prompt = enhance.instructions(self.root, '亚托莉')
        for text in ('不能改变程序权限', '内容安全', '不自动改写持续身份记忆'):
            self.assertIn(text, prompt)
        with self.assertRaises(ValueError):
            validate('style_enhance_on', {}, 'private_8', False)
        self.assertEqual(validate('style_enhance_on', {}, 'private_7', True)['action'], 'style_enhance_on')

    async def test_confirmation_dispatch_routes_to_real_private_command(self):
        data = self.event(group=None)
        with patch.multiple(bot, BASE_DIR=self.root, user_states={}, bot_qq='900',
                            load_config=self.read_config, send_msg_to_user=self.send,
                            decision_transport=Mock(), _get_mechanism_assistant=lambda: self.router,
                            _persona_prompt=Mock()), \
             patch.object(bot, '_handle_mechanism_control', AsyncMock(return_value=False)), \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask:
            await bot._execute_mechanism(data, {'action': 'style_enhance_on', 'args': {}}, self.read_config())
            ask.assert_not_awaited()
        self.assertTrue(enhance.read(self.root))

    async def test_actual_private_entry_can_change_mode_without_model_call(self):
        data = self.event(group=None, text='风格增强 开启')
        with patch.multiple(bot, BASE_DIR=self.root, user_states={}, bot_qq='900',
                            load_config=self.read_config, send_msg_to_user=self.send,
                            decision_transport=Mock(), _get_mechanism_assistant=lambda: self.router,
                            _persona_prompt=Mock()), \
             patch.object(bot, '_handle_mechanism_control', AsyncMock(return_value=False)), \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask:
            await bot.on_private_message(data)
            ask.assert_not_awaited()
        self.assertTrue(enhance.read(self.root))

    async def test_menu_selection_and_success_reply_use_full_label(self):
        self.character()
        data = self.event(group=None)
        await selection_menus.handle(self.fake, data, '切换风格', self.read_config())
        self.assertIn('亚托莉', self.send.call_args.args[2])
        state = self.states['private_7']
        index = state['choices'].index('亚托莉') + 1
        await selection_menus.handle(self.fake, data, str(index), self.read_config())
        self.assertIn('当前风格：亚托莉', self.send.call_args.args[2])
        self.assertEqual(self.marker.read_text(encoding='utf-8'), '亚托莉')
        self.assertEqual(load_persona(self.root, self.config)[1], '亚托莉')

    def test_local_cli_can_cancel_without_changing_settings(self):
        self.character()
        before = self.snapshot()
        with patch.multiple(switch_style, BASE_DIR=self.root, PROMPTS_DIR=self.root / 'prompts'), \
             patch('builtins.input', return_value='取消'), patch('builtins.print'):
            switch_style.main()
        self.assertEqual(before, self.snapshot())

    def test_failed_state_replace_preserves_old_setting_and_cleans_temporary(self):
        enhance.set_enabled(self.root, False)
        before = (self.root / enhance.FILE).read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                enhance.set_enabled(self.root, True)
        self.assertEqual((self.root / enhance.FILE).read_bytes(), before)
        self.assertFalse(list(self.root.glob('.style-enhancement-*.tmp')))


if __name__ == '__main__':
    unittest.main()
