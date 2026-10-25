import asyncio
import base64
from collections import defaultdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
from whale_stickers import WhaleStickers, StickerReply, scope_from_user, strip_markers

ROOT = Path(__file__).parent / "resources" / "whale_stickers"


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.0
        self.pack = WhaleStickers(ROOT, Path(self.tmp.name) / "state.json", clock=lambda: self.now)

    def test_all_48_images_are_real_png_and_both_variants(self):
        self.assertEqual(len(self.pack.catalog), 24)
        for id_ in self.pack.catalog:
            for variant in ("blue", "black"):
                segment = self.pack.image_segment(id_, variant)
                content = base64.b64decode(segment["data"]["file"].removeprefix("base64://"))
                self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_scope_isolation(self):
        self.assertEqual(scope_from_user("group_123_456"), "g:123")
        self.assertEqual(scope_from_user("private_123"), "p:123")
        self.assertIsNone(scope_from_user("../../secret"))
        self.pack.set_option("g:123", auto=False, variant="black")
        self.assertFalse(self.pack.offer("g:123", {}))
        self.assertTrue(self.pack.offer("p:123", {}))

    def test_select_and_strip_without_leaking_tags(self):
        text, selected = self.pack.extract("这个想法很有趣。\n[[whale:great-question:blue]]", "g:123", {})
        self.assertEqual(text, "这个想法很有趣。")
        self.assertEqual(selected, ("great-question", "blue"))
        for invalid in ("../../config.yaml", "not-a-sticker", "great-question:red"):
            text, selected = self.pack.extract("答复[[whale:" + invalid + "]]", "g:123", {})
            self.assertEqual(text, "答复")
            self.assertIsNone(selected)

    def test_quotes_and_code_do_not_send_images(self):
        for text in ('示例："[[whale:great-question:blue]]"', '```\n[[whale:great-question:blue]]\n```'):
            self.assertIsNone(self.pack.extract(text, "g:123", {})[1])
        code = '```\n[[whale:great-question:blue]]\n```'
        self.assertEqual(strip_markers(code), code)

    def test_corrupted_target_state_fails_closed(self):
        self.pack.state_path.write_text('{"g:1":{"last_at":"yesterday"}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            WhaleStickers(ROOT, self.pack.state_path)

    def test_black_preference_and_persistence(self):
        self.pack.set_option("p:1", variant="black")
        self.assertEqual(self.pack.extract("[[whale:great-question:blue]]", "p:1", {})[1], ("great-question", "black"))
        restarted = WhaleStickers(ROOT, self.pack.state_path)
        self.assertEqual(restarted.settings("p:1", {})["variant"], "black")

    def test_cooldown_persists_and_recent_images_excluded(self):
        self.pack.reserve("g:1", "great-question", "blue", {})
        self.assertEqual(self.pack.guidance("g:1", {}), "")
        restarted = WhaleStickers(ROOT, self.pack.state_path, clock=lambda: self.now)
        self.assertIsNone(restarted.reserve("g:1", "enough", "blue", {}))
        self.now += 91
        self.assertTrue(restarted.offer("g:1", {}))
        self.assertIsNone(restarted.extract("[[whale:great-question:blue]]", "g:1", {})[1])
        self.assertNotIn("great-question=", restarted.guidance("g:1", {}))

    def test_manual_can_send_when_automatic_is_off(self):
        self.pack.set_option("p:1", auto=False)
        self.assertIsNone(self.pack.extract("[[whale:enough:blue]]", "p:1", {})[1])
        self.assertIsNotNone(self.pack.reserve("p:1", "enough", "blue", {}, manual=True))
        self.assertIsNone(self.pack.reserve("p:1", "enough", "blue", {}, manual=True))

    def test_global_switch_blocks_manual_too(self):
        self.assertIsNone(self.pack.reserve("p:1", "enough", "blue", {"whale_stickers": {"enabled": False}}, manual=True))

    def test_rollback_keeps_concurrent_setting_change(self):
        reservation = self.pack.reserve("g:1", "enough", "blue", {})
        self.pack.set_option("g:1", auto=False)
        self.pack.rejected("g:1", reservation)
        self.assertFalse(self.pack.settings("g:1", {})["auto"])
        self.assertNotIn("last_at", self.pack.state["g:1"])

    def test_unknown_images_and_truncated_markers(self):
        with self.assertRaises(ValueError):
            self.pack.image_segment("../../config", "blue")
        self.assertEqual(strip_markers("正常答复[[whale:trunca"), "正常答复")
        self.assertEqual(self.pack.resolve("好问题"), "great-question")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pack = WhaleStickers(ROOT, Path(self.tmp.name) / "state.json")
        self.patches = [patch.object(bot, "_get_whale_stickers", return_value=self.pack),
                        patch.object(bot, "load_config", return_value={}), patch.object(bot, "log"),
                        patch.object(bot, "proactive_mode", None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_real_png_bytes_in_onebot_message(self):
        with patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}) as api:
            self.assertTrue(await bot._send_whale_sticker("g:1", ("great-question", "black"), manual=True))
            action, params = api.await_args.args
            self.assertEqual(action, "send_group_msg")
            self.assertEqual(params["group_id"], 1)
            body = base64.b64decode(params["message"][0]["data"]["file"][9:])
            self.assertEqual(body, (ROOT / "black" / "08-great-question.png").read_bytes())

    async def test_parallel_replies_reserve_only_one_image(self):
        with patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}) as api:
            await asyncio.gather(*(bot._send_whale_sticker("g:1", ("great-question", "blue")) for _ in range(4)))
            api.assert_awaited_once()

    async def test_explicit_success_ack_without_data_is_accepted(self):
        async def accepted(*args, **kwargs):
            kwargs["outcome"]["status"] = "ok"
            return None
        with patch.object(bot, "call_api", new_callable=AsyncMock, side_effect=accepted):
            self.assertTrue(await bot._send_whale_sticker("p:1", ("enough", "blue")))

    async def test_unknown_receipt_does_not_resend(self):
        async def uncertain(*args, **kwargs):
            kwargs["outcome"]["status"] = "unknown"
            return None
        with patch.object(bot, "call_api", new_callable=AsyncMock, side_effect=uncertain) as api:
            self.assertFalse(await bot._send_whale_sticker("g:1", ("enough", "blue")))
            await bot._send_whale_sticker("g:1", ("enough", "blue"))
            api.assert_awaited_once()

    async def test_explicit_failure_allows_later_retry(self):
        async def failed(*args, **kwargs):
            kwargs["outcome"]["status"] = "failed"
            return None
        with patch.object(bot, "call_api", new_callable=AsyncMock, side_effect=failed):
            await bot._send_whale_sticker("g:1", ("enough", "blue"))
        with patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}):
            self.assertTrue(await bot._send_whale_sticker("g:1", ("enough", "blue")))

    async def test_persona_switch_discards_old_sticker(self):
        with patch.object(bot, "_persona_prompt"), patch.object(bot, "_persona_signature", "new"), \
                patch.object(bot, "call_api", new_callable=AsyncMock) as api:
            self.assertFalse(await bot._send_reply_sticker("g:1", StickerReply("旧答复", ("enough", "blue"), "old")))
            api.assert_not_awaited()

    async def test_non_admin_cannot_change_group_settings(self):
        data = {"message_type": "group", "group_id": 1, "sender": {"user_id": 2}}
        with patch.object(bot, "is_admin", return_value=False), \
                patch.object(bot, "send_msg_to_user", new_callable=AsyncMock):
            self.assertTrue(await bot._handle_whale_sticker_command(data, "鲸鱼表情 关闭", {}))
            self.assertTrue(self.pack.settings("g:1", {})["auto"])

    async def test_private_user_can_disable_their_own_auto_stickers(self):
        data = {"message_type": "private", "sender": {"user_id": 2}}
        with patch.object(bot, "send_msg_to_user", new_callable=AsyncMock):
            await bot._handle_whale_sticker_command(data, "鲸鱼表情 关闭", {})
        self.assertFalse(self.pack.settings("p:2", {})["auto"])
        self.assertTrue(self.pack.settings("g:2", {})["auto"])

    async def test_group_text_then_one_image_and_image_only(self):
        with patch.object(bot, "recent_sent_history", []), patch.object(bot, "_persona_prompt"), \
                patch.object(bot, "_persona_signature", "style"), \
                patch.object(bot, "call_api", new_callable=AsyncMock, return_value={"message_id": 1}) as api:
            self.assertTrue(await bot.send_group_message(1, StickerReply("好问题", ("great-question", "blue"), "style")))
            self.assertEqual(api.await_count, 2)
            self.assertEqual(api.await_args_list[0].args[1]["message"], "好问题")
            self.assertIsInstance(api.await_args_list[1].args[1]["message"], list)
            self.assertTrue(await bot.send_group_message(2, StickerReply("", ("enough", "blue"), "style")))
            self.assertEqual(api.await_count, 3)

    async def test_model_reply_selected_once_and_history_contains_no_protocol(self):
        config = {"deepseek": {"enabled": True, "max_history": 2}}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({'kind': 'chat', 'reply': '可以下班摸鱼啦。\n[[whale:fish-philosophy:blue]]', 'action': None}, ensure_ascii=False)), finish_reason="stop")])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        history = defaultdict(list)
        with patch.object(bot, "BASE_DIR", Path(self.tmp.name)), patch.object(bot, "load_config", return_value=config), \
                patch.object(bot, "_init_deepseek"), patch.object(bot, "deepseek_client", client), \
                patch.object(bot, "_persona_prompt", return_value="当前人格"), patch.object(bot, "_persona_signature", "style"), \
                patch.object(bot, "_persona_name", "鲸鱼娘"), patch.object(bot, "conversations", history), \
                patch.object(bot, "_save_conversations"):
            reply = await bot.ask_deepseek("private_2", "下班了吗")
        create.assert_awaited_once()
        self.assertIsInstance(reply, StickerReply)
        self.assertEqual(reply.whale_sticker, ("fish-philosophy", "blue"))
        self.assertEqual(str(reply), "可以下班摸鱼啦。")
        self.assertNotIn("[[whale:", str(history))

    async def test_private_handler_text_and_image_including_sticker_only(self):
        config = {"deepseek": {"enabled": True, "trigger_private": True}}
        data = {"message_type": "private", "raw_message": "你好呀", "sender": {"user_id": 2, "nickname": "测试"}, "message": []}
        with patch.object(bot, "load_config", return_value=config), patch.object(bot, "deepseek_client", object()), \
                patch.object(bot, "_persona_prompt"), patch.object(bot, "_persona_signature", "style"), \
                patch.object(bot, "recent_sent_history", []), patch.object(bot, "ask_deepseek", new_callable=AsyncMock,
                    return_value=StickerReply("好呀。", ("daily-chat", "blue"), "style")), \
                patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}) as api:
            await bot.on_private_message(data)
            self.assertEqual(api.await_count, 2)
            self.assertEqual(api.await_args_list[0].args[1]["message"], "好呀。")
            self.assertIsInstance(api.await_args_list[1].args[1]["message"], list)
        # A fresh scope avoids the deliberately persistent per-chat cooldown.
        data["sender"]["user_id"] = 3
        with patch.object(bot, "load_config", return_value=config), patch.object(bot, "deepseek_client", object()), \
                patch.object(bot, "_persona_prompt"), patch.object(bot, "_persona_signature", "style"), \
                patch.object(bot, "ask_deepseek", new_callable=AsyncMock,
                    return_value=StickerReply("", ("daily-chat", "blue"), "style")), \
                patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}) as api:
            await bot.on_private_message(data)
            api.assert_awaited_once()
            self.assertIsInstance(api.await_args.args[1]["message"], list)


if __name__ == "__main__":
    unittest.main()
