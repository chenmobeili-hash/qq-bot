import asyncio
from collections import defaultdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
from mechanism_assistant import MechanismAssistant, SuggestedReply, COMMAND_HELP, describe, validate


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.router = MechanismAssistant(clock=lambda: self.now)
        self.key = "group_123_2"
        self.proposal = {"action": "reading_on", "args": {}}
        self.config = {}

    def pending(self, active=True):
        value = self.router.prepare(self.key, self.proposal, "signature")
        if active:
            self.router.activate(self.key, value.ticket)
        return value

    def test_confirm_is_same_actor_same_chat_once_only(self):
        self.pending()
        self.assertEqual(self.router.respond("group_123_3", "可以", "signature", {}, True)[0], "none")
        self.assertEqual(self.router.respond("group_456_2", "可以", "signature", {}, True)[0], "none")
        self.assertEqual(self.router.respond(self.key, "可以", "signature", {}, True), ("confirmed", self.proposal))
        self.assertEqual(self.router.respond(self.key, "可以", "signature", {}, True)[0], "none")

    def test_unacknowledged_suggestion_is_not_executable(self):
        self.pending(active=False)
        self.assertEqual(self.router.respond(self.key, "可以", "signature", {}, True)[0], "undelivered")

    def test_expired_wrong_ticket_changed_and_revoked_authority(self):
        value = self.pending()
        self.assertEqual(self.router.respond(self.key, "确认 000000", "signature", {}, True)[0], "mismatch")
        self.now += 121
        self.assertEqual(self.router.respond(self.key, "确认 " + value.ticket, "signature", {}, True)[0], "expired")
        self.pending()
        self.assertEqual(self.router.respond(self.key, "确认", "changed", {}, True)[0], "changed")
        self.pending()
        self.assertEqual(self.router.respond(self.key, "确认", "signature", {}, False)[0], "denied")

    def test_cancel_change_topic_and_qualified_yes_do_not_execute(self):
        for response in ("取消", "不用", "算了"):
            self.pending()
            self.assertEqual(self.router.respond(self.key, response, "signature", {}, True)[0], "cancelled")
        for response in ("可以，但别改设置", "今天天气怎么样", "我引用他说：可以"):
            self.pending()
            self.assertEqual(self.router.respond(self.key, response, "signature", {}, True)[0], "none")
            self.assertFalse(self.router.pending)

    def test_valid_nonce_and_restart_clears_pending(self):
        value = self.pending()
        self.assertEqual(self.router.respond(self.key, "确认 " + value.ticket, "signature", {}, True)[0], "confirmed")
        self.pending()
        self.assertFalse(MechanismAssistant().pending)

    def test_only_white_list_and_scoped_admin_actions(self):
        for action, args, key, admin in (("exec", {"cmd": "rm"}, self.key, True),
                ("reading_on", {}, self.key, False), ("reading_on", {}, "private_2", True),
                ("bili_login", {}, self.key, True), ("vision_sync_on", {}, self.key, False),
                ("search", {"query": "[CQ:at,qq=all]"}, self.key, True),
                ("search", {"query": "x", "command": "x"}, self.key, True)):
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate(action, args, key, admin)
        self.assertEqual(validate("stickers_on", {}, "private_2", False)["action"], "stickers_on")
        with self.assertRaises(ValueError):
            validate("stickers_on", {}, self.key, False)

    def test_extract_only_one_terminal_noncode_marker(self):
        marker = '[[mechanism:{"action":"reading_on","args":{}}]]'
        text, proposal = self.router.extract("要不要让我自己找书看？\n" + marker, self.key, {}, True)
        self.assertEqual(proposal, self.proposal)
        self.assertNotIn("mechanism:", text)
        for value in ("```\n" + marker + "\n```", "`" + marker + "`", marker + "\n后续正文",
                      marker + "\n" + marker, marker + "\n```后续代码```"):
            with self.subTest(value=value):
                self.assertIsNone(self.router.extract(value, self.key, {}, True)[1])

    def test_scope_and_effect_are_explicit_in_descriptions(self):
        self.assertIn("全局", describe({"action": "vision_sync_on", "args": {}}))
        self.assertIn("所有群和私聊", describe({"action": "style_menu", "args": {}}))
        self.assertIn("本群", describe(self.proposal))
        self.assertIn("公开评论", describe({"action": "bili_on", "args": {"topics": "猫猫"}}))
        self.assertIn("全局", COMMAND_HELP)
        self.assertIn("本群", COMMAND_HELP)

    def test_model_does_not_receive_admin_options_for_unprivileged_user(self):
        guide = self.router.guidance(self.key, {}, False)
        self.assertNotIn('"action": "vision_sync_on"', guide)
        self.assertNotIn('"action": "proactive_on"', guide)
        self.assertIn('"action": "pick_document"', guide)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = MechanismAssistant()
        self.key = "group_123_2"
        self.config = {"admin_qq": [2], "deepseek": {"enabled": True, "trigger_groups": [123], "trigger_private": True}}
        self.data = {"message_type": "group", "group_id": 123, "sender": {"user_id": 2},
                     "raw_message": "[CQ:at,qq=999] 可以", "message": []}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for item in (patch.object(bot, "mechanism_assistant", self.router), patch.object(bot, "load_config", return_value=self.config),
                     patch.object(bot, "_mechanism_signature", return_value="sig"), patch.object(bot, "bot_qq", 999),
                     patch.object(bot, "log")):
            item.start()
            self.addCleanup(item.stop)

    def make(self, key=None, action="reading_on"):
        key = key or self.key
        pending = self.router.prepare(key, {"action": action, "args": {}}, "sig")
        return pending, SuggestedReply("要这样做吗？", key, pending.ticket)

    async def test_group_delivery_activates_then_confirmation_executes_once(self):
        pending, reply = self.make()
        with patch.object(bot, "recent_sent_history", []), patch.object(bot, "proactive_mode", None), \
             patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}), \
             patch.object(bot, "_send_reply_sticker", new_callable=AsyncMock):
            self.assertTrue(await bot.send_group_message(123, reply))
        self.assertTrue(pending.active)
        with patch.object(bot, "_execute_mechanism", new_callable=AsyncMock) as execute:
            self.assertTrue(await bot._handle_mechanism_control(self.data, "可以", self.config))
            self.assertFalse(await bot._handle_mechanism_control(self.data, "可以", self.config))
        execute.assert_awaited_once()

    async def test_uncertain_delivery_and_other_group_do_not_activate(self):
        pending, reply = self.make()
        with patch.object(bot, "recent_sent_history", []), patch.object(bot, "call_api", new_callable=AsyncMock, return_value=None):
            self.assertFalse(await bot.send_group_message(123, reply))
        self.assertFalse(pending.active)
        bot._mechanism_delivered(reply, "g:456")
        self.assertFalse(pending.active)

    async def test_concurrent_yes_only_dispatches_once(self):
        pending, _ = self.make()
        self.router.activate(self.key, pending.ticket)
        with patch.object(bot, "_execute_mechanism", new_callable=AsyncMock) as execute:
            await asyncio.gather(*(bot._handle_mechanism_control(self.data, "可以", self.config) for _ in range(3)))
        execute.assert_awaited_once()

    async def test_confirmation_addressed_to_another_member_cannot_execute(self):
        pending, _ = self.make()
        self.router.activate(self.key, pending.ticket)
        self.data["raw_message"] = "[CQ:at,qq=555] 可以"
        with patch.object(bot, "_execute_mechanism", new_callable=AsyncMock) as execute:
            self.assertFalse(await bot._handle_mechanism_control(self.data, "可以", self.config))
        execute.assert_not_awaited()

    async def test_dispatch_keeps_source_group_actor_and_existing_handler(self):
        with patch.object(bot, "on_group_message", new_callable=AsyncMock) as group:
            await bot._execute_mechanism(self.data, {"action": "reading_on", "args": {}}, self.config)
        forwarded = group.await_args.args[0]
        self.assertEqual(forwarded["group_id"], 123)
        self.assertEqual(forwarded["sender"]["user_id"], 2)
        self.assertIn("主动阅读 开启", forwarded["raw_message"])

    async def test_help_is_real_program_output_no_model(self):
        with patch.object(bot, "send_msg_to_user", new_callable=AsyncMock) as send, \
             patch.object(bot, "ask_deepseek", new_callable=AsyncMock) as ask:
            self.assertTrue(await bot._handle_mechanism_control(self.data, "帮助", self.config))
        ask.assert_not_awaited()
        self.assertIn("作用范围", send.await_args.args[2])

    async def test_same_llm_call_proposes_without_executing_and_hides_marker_in_history(self):
        raw = json.dumps({'kind': 'operation', 'reply': '要不要让我偶尔找书读？',
                          'action': {'action': 'reading_on', 'args': {}}}, ensure_ascii=False)
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=raw), finish_reason="stop")]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        history = defaultdict(list)
        with patch.object(bot, "BASE_DIR", Path(self.tmp.name)), patch.object(bot, "_init_deepseek"), \
             patch.object(bot, "deepseek_client", client), patch.object(bot, "_persona_prompt", return_value="虚构人格"), \
             patch.object(bot, "_persona_signature", "style"), patch.object(bot, "_persona_name", "测试"), \
             patch.object(bot, "_get_whale_stickers", return_value=None), patch.object(bot, "_save_conversations"), \
             patch.object(bot, "conversations", history), patch.object(bot, "_execute_mechanism", new_callable=AsyncMock) as execute:
            reply = await bot.ask_deepseek(self.key, "你有空可以找点书读读")
        create.assert_awaited_once()
        execute.assert_not_awaited()
        self.assertIsInstance(reply, SuggestedReply)
        self.assertIn("操作建议：", reply)
        self.assertIn("本群", reply)
        self.assertNotIn("[[mechanism:", str(history))
        self.assertFalse(self.router.pending[self.key].active)

    async def test_private_reply_delivery_also_activates(self):
        generated = []
        async def ask(*args, **kwargs):
            pending, reply = self.make("private_2", "vision_sync_on")
            generated.append(pending)
            return reply
        data = {"message_type": "private", "raw_message": "看图也跟主模型吧", "sender": {"user_id": 2}, "message": []}
        with patch.object(bot, "deepseek_client", object()), patch.object(bot, "ask_deepseek", side_effect=ask), \
             patch.object(bot, "recent_sent_history", []), patch.object(bot, "call_api", new_callable=AsyncMock, return_value={}), \
             patch.object(bot, "_send_reply_sticker", new_callable=AsyncMock):
            await bot.on_private_message(data)
        self.assertTrue(generated[0].active)


if __name__ == "__main__":
    unittest.main()
