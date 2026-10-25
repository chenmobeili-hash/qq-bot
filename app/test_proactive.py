import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from proactive import ProactiveMode, settings


class ModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = 100000.0
        self.config = {"deepseek": {"enabled": True, "trigger_groups": [123]},
                       "proactive": {"groups": [123], "reply_probability": 1}}
        self.path = Path(self.directory.name) / "proactive_state.json"
        self.mode = ProactiveMode(self.path, lambda *a: None,
                                  clock=lambda: self.clock, roll=lambda: 0)
        self.now = datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=8)))
        self.generate = AsyncMock(return_value="这个话题挺有意思。")
        self.send = AsyncMock(return_value=True)

    def conversation(self):
        for text in ("最近玩了个游戏", "音乐挺好听", "剧情也不错"):
            self.mode.observe(123, "群友", text, self.config)
        self.clock += 9

    async def tick(self):
        await self.mode.tick(lambda: self.config, lambda cfg: self.now,
                             lambda: True, self.generate, self.send)

    async def test_disabled_default_and_whitelist(self):
        config = {"deepseek": {"enabled": True}}
        self.assertFalse(self.mode.enabled(123, config))
        self.mode.set_enabled(123, True)
        self.assertTrue(self.mode.enabled(123, config))
        self.assertFalse(self.mode.enabled(456, self.config))

    async def test_enable_disable_persists_without_replaying_old_messages(self):
        self.conversation()
        self.mode.set_enabled(123, False)
        self.assertNotIn("123", self.mode.groups)
        reloaded = ProactiveMode(self.path, lambda *a: None)
        self.assertFalse(reloaded.enabled(123, self.config))
        reloaded.set_enabled(123, True)
        self.assertTrue(reloaded.enabled(123, self.config))
        self.assertFalse(reloaded.groups)

    async def test_join_cooldown_daily_quota_persist(self):
        self.config["proactive"]["daily_limit"] = 1
        self.conversation()
        await self.tick()
        self.send.assert_awaited_once_with(123, "这个话题挺有意思。")
        self.assertEqual(self.mode.daily["123"]["count"], 1)
        self.clock += 700
        self.conversation()
        await self.tick()
        self.assertEqual(self.send.await_count, 1)
        reloaded = ProactiveMode(self.path, lambda *a: None)
        self.assertEqual(reloaded.daily["123"]["count"], 1)

    async def test_quiet_hours_offline_and_no_repeat_for_same_batch(self):
        self.conversation()
        self.now = self.now.replace(hour=23)
        await self.tick()
        self.generate.assert_not_awaited()
        self.now = self.now.replace(hour=12)
        await self.mode.tick(lambda: self.config, lambda cfg: self.now,
                             lambda: False, self.generate, self.send)
        self.generate.assert_not_awaited()
        self.generate.return_value = None
        await self.tick()
        self.clock += 700
        await self.tick()
        self.assertEqual(self.generate.await_count, 1)
        self.send.assert_not_awaited()

    async def test_idle_only_once_until_human_returns(self):
        self.mode.observe(123, "群友", "先去吃饭了", self.config)
        self.clock += 3601
        await self.tick()
        self.assertEqual(self.generate.call_args.args[1], "idle")
        self.clock += 3601
        await self.tick()
        self.assertEqual(self.send.await_count, 1)

    async def test_direct_request_priority_and_bot_reply_suppresses_join(self):
        self.conversation()
        self.mode.observe(123, "群友", "帮我解题", self.config, direct=True)
        self.clock += 9
        await self.tick()
        self.generate.assert_not_awaited()
        self.mode.note_bot(123, "答案是……")
        self.clock += 601
        await self.tick()
        self.generate.assert_not_awaited()

    async def test_new_message_or_disable_during_generation_discards_result(self):
        self.conversation()
        async def arriving(*args):
            self.mode.observe(123, "另一人", "等等，话题变了", self.config)
            return "旧话题回复"
        self.generate.side_effect = arriving
        await self.tick()
        self.send.assert_not_awaited()
        self.clock += 700
        self.conversation()
        async def disabled(*args):
            self.mode.set_enabled(123, False)
            return "过期回复"
        self.generate.side_effect = disabled
        await self.tick()
        self.send.assert_not_awaited()

    async def test_no_parallel_generations(self):
        self.conversation()
        started, release = asyncio.Event(), asyncio.Event()
        async def wait(*args):
            started.set()
            await release.wait()
            return "接话"
        self.generate.side_effect = wait
        first = asyncio.create_task(self.tick())
        await started.wait()
        await self.tick()
        release.set()
        await first
        self.assertEqual(self.generate.await_count, 1)

    async def test_failure_consumes_quota_and_does_not_retry_immediately(self):
        self.conversation()
        self.send.return_value = False
        await self.tick()
        await self.tick()
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.mode.daily["123"]["count"], 1)

    async def test_quiet_range_and_clamping(self):
        cfg = settings({})
        self.assertTrue(self.mode.quiet(cfg, 0))
        self.assertFalse(self.mode.quiet(cfg, 12))
        self.assertFalse(self.mode.quiet({**cfg, "quiet_start": 8, "quiet_end": 8}, 8))
        self.assertEqual(settings({"proactive": {"cooldown_seconds": -1}})["cooldown_seconds"], 60)

    async def test_level_is_per_group_persistent_and_keeps_enablement_and_quota(self):
        self.mode.set_enabled(123, True)
        self.mode.daily["123"] = {"date": "2026-10-22", "count": 7}
        self.mode.set_level(123, "活跃")
        reloaded = ProactiveMode(self.path, lambda *a: None)
        self.assertTrue(reloaded.enabled(123, self.config))
        self.assertEqual(reloaded.levels, {"123": "活跃"})
        self.assertEqual(reloaded.daily["123"]["count"], 7)
        self.assertEqual(reloaded.effective_settings(123, self.config)["daily_limit"], 30)
        self.assertEqual(reloaded.effective_settings(456, self.config)["daily_limit"], 24)
        self.mode.set_level(456, "活跃")
        self.assertFalse(self.mode.enabled(456, self.config))

    async def test_old_state_loads_and_configuration_can_be_restored(self):
        self.path.write_text(json.dumps({"groups": {"123": True}, "daily": {}}), encoding="utf-8")
        mode = ProactiveMode(self.path, lambda *a: None)
        self.assertTrue(mode.enabled(123, self.config))
        self.config["proactive"].update(cooldown_seconds=740, quiet_start=21)
        mode.set_level(123, "活跃")
        self.assertEqual(mode.effective_settings(123, self.config)["cooldown_seconds"], 120)
        self.assertEqual(mode.effective_settings(123, self.config)["quiet_start"], 21)
        mode.set_level(123, "配置")
        self.assertEqual(mode.effective_settings(123, self.config)["cooldown_seconds"], 740)
        self.assertNotIn("123", mode.levels)

    async def test_active_level_can_join_single_message_but_keeps_quiet_hours(self):
        self.mode.set_level(123, "活跃")
        self.mode.observe(123, "群友", "终于写完了", self.config)
        self.clock += 9
        self.now = self.now.replace(hour=23)
        await self.tick()
        self.generate.assert_not_awaited()
        self.now = self.now.replace(hour=12)
        await self.tick()
        self.send.assert_awaited_once()
        self.mode.observe(123, "群友", "准备休息一会", self.config)
        self.clock += 119
        await self.tick()
        self.assertEqual(self.send.await_count, 1)
        self.clock += 2
        await self.tick()
        self.assertEqual(self.send.await_count, 2)

    async def test_level_switch_during_generation_discards_old_reply(self):
        self.conversation()
        async def switched(*args):
            self.mode.set_level(123, "活跃")
            return "旧档回复"
        self.generate.side_effect = switched
        await self.tick()
        self.send.assert_not_awaited()
        self.assertNotIn("123", self.mode.groups)

    async def test_failed_level_save_rolls_back_selection(self):
        self.mode.set_level(123, "低频")
        with patch.object(self.mode, "save", side_effect=OSError):
            with self.assertRaises(OSError):
                self.mode.set_level(123, "活跃")
        self.assertEqual(self.mode.levels["123"], "低频")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_admin_can_change_level(self):
        mode = SimpleNamespace(set_level=unittest.mock.Mock())
        with patch.object(bot, "_get_proactive_mode", return_value=mode), \
             patch.object(bot, "send_group_message", AsyncMock()) as send:
            self.assertTrue(await bot._handle_proactive_command(123, "7", "主动模式 活跃", {"admin_qq": [8]}))
            mode.set_level.assert_not_called()
            self.assertIn("只有管理员", send.call_args.args[1])

    async def test_admin_level_command_status_and_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            mode = ProactiveMode(Path(directory) / "state.json", lambda *a: None)
            cfg = {"admin_qq": [8], "deepseek": {"enabled": True, "trigger_groups": [123]}}
            with patch.object(bot, "_get_proactive_mode", return_value=mode), \
                 patch.object(bot, "send_group_message", AsyncMock()) as send:
                for name in ("低频", "标准", "活跃"):
                    self.assertTrue(await bot._handle_proactive_command(123, "8", "主动模式 档位 " + name, cfg))
                    self.assertEqual(mode.levels["123"], name)
                    self.assertFalse(mode.enabled(123, cfg))
                await bot._handle_proactive_command(123, "8", "主动模式 开启", cfg)
                self.assertTrue(mode.enabled(123, cfg))
                self.assertEqual(mode.levels["123"], "活跃")
                await bot._handle_proactive_command(123, "7", "主动模式 状态", cfg)
                self.assertIn("档位：活跃", send.call_args.args[1])
                self.assertIn("65%", send.call_args.args[1])
                await bot._handle_proactive_command(123, "8", "主动模式 配置", cfg)
                self.assertNotIn("123", mode.levels)

    async def test_only_admin_can_toggle(self):
        mode = SimpleNamespace(set_enabled=lambda *args: self.fail("unauthorized change"))
        with patch.object(bot, "_get_proactive_mode", return_value=mode), \
             patch.object(bot, "send_group_message", AsyncMock()) as send:
            self.assertTrue(await bot._handle_proactive_command(123, "7", "主动模式 开启", {"admin_qq": [8]}))
            self.assertIn("只有管理员", send.call_args.args[1])

    async def test_admin_toggle_and_whitelist(self):
        mode = SimpleNamespace(set_enabled=unittest.mock.Mock())
        cfg = {"admin_qq": [8], "deepseek": {"enabled": True, "trigger_groups": [123]}}
        with patch.object(bot, "_get_proactive_mode", return_value=mode), \
             patch.object(bot, "send_group_message", AsyncMock()):
            await bot._handle_proactive_command(123, "8", "主动模式 开启", cfg)
            mode.set_enabled.assert_called_once_with(123, True)
            await bot._handle_proactive_command(456, "8", "主动模式 开启", cfg)
            self.assertEqual(mode.set_enabled.call_count, 1)

    async def test_full_direct_input_is_unchanged(self):
        text = "完整题目" * 1000
        event = {"group_id": 123, "raw_message": "[CQ:at,qq=999] " + text,
                 "message": [], "sender": {"user_id": 7, "nickname": "测试"}}
        cfg = {"deepseek": {"enabled": True, "trigger_groups": [123]}}
        mode = SimpleNamespace(observe=unittest.mock.Mock())
        with patch.object(bot, "bot_qq", "999"), patch.object(bot, "load_config", return_value=cfg), \
             patch.object(bot, "log"), \
             patch.object(bot, "_get_proactive_mode", return_value=mode), \
             patch.object(bot, "deepseek_client", object()), \
             patch.object(bot, "ask_deepseek", AsyncMock(return_value="完整答案")) as ask, \
             patch.object(bot, "send_group_message", AsyncMock()):
            await bot.on_group_message(event)
            self.assertEqual(ask.call_args.args[1], text)

    async def test_generation_silent_length_and_character_cap(self):
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, "_init_deepseek"), patch.object(bot, "deepseek_client", client), \
             patch.object(bot, "load_config", return_value={}):
            for text, finish, expected in (("[SILENT]", "stop", None), ("未完成", "length", None),
                                           (json.dumps({"kind": "send", "reply": "好" * 150}), "stop", 160),
                                           (json.dumps({"kind": "send", "reply": "好" * 400}), "stop", None)):
                create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=text), finish_reason=finish)])
                result = await bot._generate_proactive(123, "join", [], {})
                self.assertIsNone(result) if expected is None else self.assertLessEqual(len(result), expected)


if __name__ == "__main__":
    unittest.main()
