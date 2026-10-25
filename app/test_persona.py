import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from persona import load_persona


class PersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "prompts").mkdir()
        (self.root / "personas").mkdir()
        (self.root / "prompts" / "小鲸鱼.txt").write_text("默认鲸鱼语气", encoding="utf-8")
        (self.root / "prompts" / "另一人格.txt").write_text("新角色语气", encoding="utf-8")
        (self.root / "personas" / "内在人格.txt").write_text("持续保留共同经历", encoding="utf-8")
        self.cfg = {"deepseek": {"enabled": True, "trigger_private": True, "trigger_groups": [123],
                                 "system_prompt": "旧配置角色"}}

    def test_default_and_live_txt_edits(self):
        prompt, name, signature = load_persona(self.root, self.cfg)
        self.assertEqual(name, "小鲸鱼")
        self.assertIn("持续保留共同经历", prompt)
        self.assertIn("默认鲸鱼语气", prompt)
        (self.root / "prompts" / "小鲸鱼.txt").write_text("修改后的语气", encoding="utf-8")
        updated = load_persona(self.root, self.cfg)
        self.assertIn("修改后的语气", updated[0])
        self.assertNotEqual(signature, updated[2])

    def test_style_switch_retains_core_and_blocks_marker_path_traversal(self):
        marker = self.root / "prompts" / "_current_style.txt"
        marker.write_text("另一人格", encoding="utf-8")
        prompt, name, _ = load_persona(self.root, self.cfg)
        self.assertEqual(name, "另一人格")
        self.assertIn("持续保留共同经历", prompt)
        self.assertIn("新角色语气", prompt)
        marker.write_text("../outside", encoding="utf-8")
        self.assertEqual(load_persona(self.root, self.cfg)[1], "小鲸鱼")

    def test_changed_persona_discards_active_context_but_keeps_chat_memory(self):
        mode = SimpleNamespace(groups={"123": object()})
        with patch.object(bot, "BASE_DIR", self.root), patch.object(bot, "_persona_signature", None), \
             patch.object(bot, "proactive_mode", mode), patch.dict(bot.conversations, {"private_7": []}, clear=True):
            bot._persona_prompt(self.cfg)
            (self.root / "prompts" / "_current_style.txt").write_text("另一人格", encoding="utf-8")
            bot._persona_prompt(self.cfg)
            self.assertFalse(mode.groups)
            self.assertIn("private_7", bot.conversations)

    async def test_group_and_private_switch_preserve_all_histories_on_disk(self):
        for kind in ("group", "private"):
            histories = {"private_7": [{"role": "user", "content": "我明天有面试"}],
                         "group_123_8": [{"role": "user", "content": "另一个人的记录"}],
                         "style_wait_7": ["另一人格"]}
            data = {"message_type": kind, "raw_message": "[CQ:at,qq=999] 切换风格" if kind == "group" else "切换风格",
                    "message": [], "sender": {"user_id": 7, "nickname": "测试"}}
            if kind == "group":
                data['group_id'] = 123
            with patch.object(bot, "BASE_DIR", self.root), patch.object(bot, "load_config", return_value=self.cfg), \
                 patch.object(bot, "bot_qq", "999"), patch.object(bot, "log"), \
                 patch.object(bot, "_get_proactive_mode", return_value=SimpleNamespace(observe=lambda *a, **kw: None)), \
                 patch.object(bot, "proactive_mode", None), patch.object(bot, "deepseek_client", object()), \
                 patch.object(bot, "send_group_message", AsyncMock()), patch.object(bot, "call_api", AsyncMock()), \
                 patch.dict(bot.conversations, histories, clear=True), patch.dict(bot.user_states, {}, clear=True):
                if kind == "group":
                    await bot.on_group_message(data)
                else:
                    await bot.on_private_message(data)
                state = bot.user_states[bot._mechanism_key(data)]
                data['raw_message'] = str(state['choices'].index('另一人格') + 1)
                if kind == "group":
                    await bot.on_group_message(data)
                else:
                    await bot.on_private_message(data)
                saved = json.loads((self.root / "conversations.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["private_7"][0]["content"], "我明天有面试")
                self.assertIn("group_123_8", saved)
                self.assertNotIn("style_wait_7", saved)
                bot.conversations.clear()
                bot._load_conversations()
                self.assertEqual(bot.conversations["private_7"][0]["content"], "我明天有面试")

    def test_history_contains_voice_marker_and_legacy_still_loads(self):
        with patch.dict(bot.conversations, {"private_7": [
                {"role": "assistant", "content": "旧口吻", "persona": "旧角色"},
                {"role": "user", "content": "旧格式记录"}]}, clear=True):
            history = bot._build_history("private_7", 10)
            self.assertIn("历史表达人格：旧角色", history[0]["content"])
            self.assertIn("旧格式记录", history[1]["content"])

    async def test_direct_reply_injects_both_layers_and_saves_style(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({'kind': 'chat', 'reply': '我记得你提过面试。', 'action': None}, ensure_ascii=False)), finish_reason="stop")])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, "BASE_DIR", self.root), patch.object(bot, "load_config", return_value=self.cfg), \
             patch.object(bot, "_init_deepseek"), patch.object(bot, "deepseek_client", client), \
             patch.object(bot, "proactive_mode", None), patch.dict(bot.conversations, {}, clear=True):
            await bot.ask_deepseek("private_7", "你好")
            system = create.call_args.kwargs["messages"][0]["content"]
            self.assertIn("持续保留共同经历", system)
            self.assertIn("默认鲸鱼语气", system)
            self.assertEqual(bot.conversations["private_7"][-1]["persona"], "小鲸鱼")

    async def test_inflight_old_style_reply_is_not_saved(self):
        async def generate(**kwargs):
            (self.root / "prompts" / "_current_style.txt").write_text("另一人格", encoding="utf-8")
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({'kind': 'chat', 'reply': '过期旧人格答案', 'action': None}, ensure_ascii=False)), finish_reason="stop")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=generate)))
        with patch.object(bot, "BASE_DIR", self.root), patch.object(bot, "load_config", return_value=self.cfg), \
             patch.object(bot, "_init_deepseek"), patch.object(bot, "deepseek_client", client), \
             patch.object(bot, "proactive_mode", None), patch.dict(bot.conversations, {}, clear=True):
            result = await bot.ask_deepseek("private_7", "你好")
            self.assertNotIn("过期旧人格答案", result)
            self.assertFalse(bot.conversations["private_7"])


if __name__ == "__main__":
    unittest.main()
