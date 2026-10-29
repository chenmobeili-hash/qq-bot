import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
from paper_autonomy import PaperAutonomy, source_key
from paper_reader import PaperLibrary, PaperError
from proactive import ProactiveMode


def document(text="This synthetic test document discusses a small reproducible experiment."):
    return {"title": "Public test.pdf", "kind": "PDF文字", "total": 1, "truncated": False,
            "units": [{"label": "第1页", "text": text}]}


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = 100000.0
        self.now = datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=8)))
        self.config = {"deepseek": {"enabled": True, "trigger_groups": [123]},
                       "proactive": {"groups": [123]}, "paper_reader": {"auto_read": {"probability": 1}}}
        self.mode = ProactiveMode(self.root / "chat.json", lambda *args: None, clock=lambda: self.clock)
        self.reading = PaperAutonomy(self.root / "read.json", clock=lambda: self.clock, roll=lambda: 0)
        self.library = PaperLibrary(clock=lambda: self.clock)
        self.mode.observe(123, "person", "有空看看书", self.config)
        self.clock += 1801
        self.candidate = {"file_id": "public-book", "name": "Public test.pdf", "size": 100,
                          "_announcement": "我想翻翻《Public test.pdf》，标题看着挺有意思。"}
        self.doc = document()
        self.discover = AsyncMock(return_value=[self.candidate])
        self.choose = AsyncMock(return_value=self.candidate)
        self.download = AsyncMock(return_value=self.doc)
        self.review = AsyncMock(return_value="这份文档的证据有局限[第1页]。")
        self.send = AsyncMock(return_value=True)
        self.signature = lambda config: config.get("revision", 1)

    async def tick(self, connected=True):
        await self.reading.tick(lambda: self.config, lambda config: self.now, lambda: connected,
            self.mode, self.library, self.discover, self.choose, self.download, self.review, self.send, self.signature)

    async def test_enabled_proactive_idle_reads_once_and_publishes_shared_context(self):
        await self.tick()
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.send.await_args_list[0].args[1], self.candidate["_announcement"])
        self.assertIs(self.library.current_doc(("123", "other-user")), self.doc)
        self.assertIsNone(self.library.current_doc(("999", "other-user")))
        self.assertTrue(self.reading.seen_source(123, self.candidate))
        self.assertEqual(self.mode.daily["123"]["count"], 2)
        self.assertFalse(self.mode.background_busy)
        self.assertFalse(self.library.busy)
        self.clock += 7201
        await self.tick()
        self.assertEqual(self.review.await_count, 1)

    async def test_disabled_default_without_proactive_never_discovers(self):
        self.mode.set_enabled(123, False)
        await self.tick()
        self.discover.assert_not_awaited()

    async def test_reading_override_and_master_switch_respected(self):
        self.reading.set_enabled(123, False)
        await self.tick()
        self.discover.assert_not_awaited()
        self.reading.set_enabled(123, True)
        self.config["paper_reader"]["auto_read"]["enabled"] = False
        await self.tick()
        self.discover.assert_not_awaited()

    async def test_quiet_hours_offline_and_recent_human_skip(self):
        self.now = self.now.replace(hour=23)
        await self.tick()
        self.now = self.now.replace(hour=12)
        await self.tick(connected=False)
        self.mode.observe(123, "person", "在吗", self.config, direct=True)
        await self.tick()
        self.discover.assert_not_awaited()

    async def test_do_not_compete_with_active_chat_generation_or_manual_file(self):
        async with self.mode.lock:
            await self.tick()
        self.library.busy.add(("123", "manual-user"))
        await self.tick()
        self.discover.assert_not_awaited()

    async def test_failed_probability_is_persisted_without_spinning_model(self):
        self.config["paper_reader"]["auto_read"]["probability"] = 0.00000001
        await self.tick()
        first_check = self.reading.groups["123"]["last_check"]
        self.clock += 31
        await self.tick()
        self.assertEqual(self.reading.groups["123"]["last_check"], first_check)
        self.discover.assert_not_awaited()
        reload = PaperAutonomy(self.root / "read.json")
        self.assertEqual(reload.groups["123"]["last_check"], first_check)

    async def test_successful_history_persists_and_other_group_is_independent(self):
        await self.tick()
        reload = PaperAutonomy(self.root / "read.json")
        self.assertTrue(reload.seen_source(123, self.candidate))
        self.assertTrue(reload.seen_document(123, self.doc))
        self.assertFalse(reload.seen_document(456, self.doc))
        self.assertFalse(reload.unread(123, [self.candidate]))

    async def test_renamed_same_content_stops_before_review_and_remembers_alias(self):
        old = {"file_id": "previous-id", "name": "old.pdf", "size": 80}
        self.reading.mark_reviewed(123, old, self.doc)
        await self.tick()
        self.download.assert_awaited_once()
        self.review.assert_not_awaited()
        self.send.assert_not_awaited()
        self.assertTrue(self.reading.seen_source(123, self.candidate))

    async def test_already_seen_ids_never_reach_selector(self):
        self.reading.mark_reviewed(123, self.candidate, self.doc)
        await self.tick()
        self.choose.assert_not_awaited()

    async def test_failed_download_is_not_marked_reviewed(self):
        self.download.side_effect = PaperError("unreadable scan")
        await self.tick()
        self.assertFalse(self.reading.seen_source(123, self.candidate))
        self.assertFalse(self.mode.background_busy)
        self.assertFalse(self.library.busy)
        self.assertEqual(self.reading.summary(123, self.now.date().isoformat())[0]["reviewed"], 0)

    async def test_failed_model_is_not_marked_reviewed(self):
        self.review.side_effect = RuntimeError("synthetic model failure")
        await self.tick()
        self.assertFalse(self.reading.seen_source(123, self.candidate))
        self.assertEqual(self.send.await_count, 1)

    async def test_unknown_announcement_receipt_does_not_retry_or_claim_read(self):
        self.send.return_value = False
        await self.tick()
        self.review.assert_not_awaited()
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.mode.daily["123"]["count"], 1)
        self.assertFalse(self.reading.seen_source(123, self.candidate))

    async def test_unknown_share_receipt_still_keeps_read_history_no_resend(self):
        self.send.side_effect = [True, False]
        await self.tick()
        self.assertTrue(self.reading.seen_source(123, self.candidate))
        self.assertIs(self.library.current_doc(("123", "person")), self.doc)
        self.clock += 7201
        await self.tick()
        self.assertEqual(self.send.await_count, 2)

    async def test_silent_review_remembers_read_without_publishing_fake_summary(self):
        self.review.return_value = None
        await self.tick()
        self.assertEqual(self.send.await_count, 1)
        self.assertTrue(self.reading.seen_source(123, self.candidate))
        self.assertIs(self.library.current_doc(("123", "person")), self.doc)

    async def test_human_arrives_during_discovery_cancels_no_message(self):
        async def discover(*args):
            self.mode.observe(123, "person", "等一下", self.config, direct=True)
            return [self.candidate]
        self.discover.side_effect = discover
        await self.tick()
        self.choose.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_human_arrives_during_review_discards_share_but_remembers_actual_read(self):
        async def review(*args):
            self.assertIs(self.library.current_doc(("123", "person")), self.doc)
            self.mode.observe(123, "person", "我有个问题", self.config, direct=True)
            return "must not send stale share"
        self.review.side_effect = review
        await self.tick()
        self.assertEqual(self.send.await_count, 1)
        self.assertTrue(self.reading.seen_document(123, self.doc))

    async def test_disable_or_persona_change_mid_selection_cancels(self):
        async def choose(*args):
            self.config["revision"] = 2
            return self.candidate
        self.choose.side_effect = choose
        await self.tick()
        self.download.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_daily_attempt_and_shared_message_limits(self):
        day = self.now.date().isoformat()
        self.reading.groups["123"] = {"daily": {"date": day, "attempts": 2, "reviewed": 0}}
        await self.tick()
        self.reading.groups.clear()
        limit = self.mode.effective_settings(123, self.config)["daily_limit"]
        self.mode.daily["123"] = {"date": day, "count": limit - 1}
        await self.tick()
        self.discover.assert_not_awaited()

    async def test_reading_uses_active_group_quota_but_own_daily_limit(self):
        self.mode.set_level(123, "活跃")
        self.mode.observe(123, "person", "有空看看书", self.config)
        self.clock += 1801
        self.mode.daily["123"] = {"date": self.now.date().isoformat(), "count": 28}
        await self.tick()
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.mode.daily["123"]["count"], 30)
        self.clock += 7201
        await self.tick()
        self.assertEqual(self.review.await_count, 1)

    async def test_level_change_during_reading_cancels_old_announcement(self):
        async def choose(*args):
            self.mode.set_level(123, "活跃")
            return self.candidate
        self.choose.side_effect = choose
        await self.tick()
        self.download.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_storage_corruption_and_write_failure_stop_auto_reading(self):
        bad = self.root / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        reading = PaperAutonomy(bad)
        self.assertFalse(reading.storage_ok)
        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.reading.set_enabled(123, True)
        self.assertFalse(self.reading.storage_ok)
        await self.tick()
        self.discover.assert_not_awaited()

    async def test_signature_failure_always_releases_busy_flags(self):
        def signature(config):
            raise ValueError("synthetic persona failure")
        self.signature = signature
        await self.tick()
        self.assertFalse(self.mode.background_busy)
        self.assertFalse(self.library.busy)

    async def test_proactive_chat_does_not_speak_over_background_reader(self):
        self.mode.background_busy.add("123")
        self.assertIsNone(self.mode.reason("123", self.config, self.now))


class SharedContextTests(unittest.TestCase):
    def test_latest_shared_context_is_group_only_forget_hides_only_for_requester(self):
        now = [100.0]
        library = PaperLibrary(clock=lambda: now[0])
        old, new = document("old"), document("new")
        library.store_doc(("123", "u1"), old)
        now[0] += 5
        library.store_shared("123", new)
        self.assertIs(library.current_doc(("123", "u1")), new)
        self.assertIsNone(library.current_doc(("999", "u1")))
        library.forget(("123", "u1"))
        self.assertIsNone(library.current_doc(("123", "u1")))
        self.assertIs(library.current_doc(("123", "u2")), new)
        now[0] += 3601
        self.assertIsNone(library.current_doc(("123", "u2")))

    def test_file_size_update_is_new_source_and_identical_text_is_duplicate(self):
        self.assertNotEqual(source_key({"file_id": "a", "size": 1}), source_key({"file_id": "a", "size": 2}))

    def test_natural_book_questions_are_supported(self):
        from paper_reader import is_document_followup
        self.assertTrue(is_document_followup("这本书怎么样？"))
        self.assertTrue(is_document_followup("这书有什么意思？"))


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_read_switch_is_admin_only(self):
        reading = SimpleNamespace(set_enabled=unittest.mock.Mock())
        with patch.object(bot, "_get_paper_autonomy", return_value=reading), \
             patch.object(bot, "_get_proactive_mode", return_value=SimpleNamespace()), \
             patch.object(bot, "send_group_message", new_callable=AsyncMock) as send:
            await bot._handle_auto_read_command(123, "7", "主动阅读 关闭", {"admin_qq": [8]})
            reading.set_enabled.assert_not_called()
            self.assertIn("只有管理员", send.await_args.args[1])
            await bot._handle_auto_read_command(123, "8", "主动阅读 关闭", {"admin_qq": [8]})
            reading.set_enabled.assert_called_once_with(123, False)

    async def test_explicit_document_command_updates_human_revision_before_handling(self):
        cfg = {"deepseek": {"enabled": True, "trigger_groups": [123]}}
        mode = SimpleNamespace(observe=unittest.mock.Mock())
        data = {"group_id": 123, "raw_message": "[CQ:at,qq=999] 挑一份你感兴趣的文档读",
                "message": [], "sender": {"user_id": 7, "nickname": "user"}}
        async def handle(*args):
            mode.observe.assert_called_once()
            return True
        with patch.object(bot, "load_config", return_value=cfg), patch.object(bot, "bot_qq", 999), \
             patch.object(bot, "_get_proactive_mode", return_value=mode), \
             patch.object(bot, "_handle_paper_command", side_effect=handle):
            await bot.on_group_message(data)


class ManualHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.reading = PaperAutonomy(Path(self.directory.name) / "read.json")
        self.library = PaperLibrary()
        self.cfg = {"deepseek": {"enabled": True, "trigger_groups": [123]}}
        self.book = {"file_id": "book1", "name": "Research.pdf", "size": 100}
        self.doc = document()
        for entry in (patch.object(bot, "paper_autonomy", self.reading), patch.object(bot, "paper_library", self.library),
                      patch.object(bot, "load_config", return_value=self.cfg),
                      patch.object(bot, "send_group_message", new_callable=AsyncMock, return_value=True)):
            entry.start()
            self.addCleanup(entry.stop)

    async def test_fixed_pick_skips_persisted_seen_before_model(self):
        self.reading.mark_reviewed(123, self.book, self.doc)
        with patch.object(bot, "_paper_candidates", new_callable=AsyncMock, return_value=[self.book]), \
             patch.object(bot, "_choose_paper", new_callable=AsyncMock) as choose:
            await bot._run_paper_read(123, 456, "", self.cfg, choose=True)
        choose.assert_not_awaited()

    async def test_explicit_file_can_be_reread_and_is_recorded_only_after_success(self):
        self.library.remember_file(("123", "456"), self.book)
        self.reading.mark_reviewed(123, self.book, self.doc)
        with patch.object(bot, "_download_group_paper", new_callable=AsyncMock, return_value=self.doc), \
             patch.object(bot, "_generate_paper", new_callable=AsyncMock, return_value="实际正文的解释") as review:
            await bot._run_paper_read(123, 456, "再读一次", self.cfg, use_file=True)
        review.assert_awaited_once()
        self.assertTrue(self.reading.seen_source(123, self.book))

    async def test_manual_model_failure_is_not_marked_read(self):
        self.library.remember_file(("123", "456"), self.book)
        with patch.object(bot, "_download_group_paper", new_callable=AsyncMock, return_value=self.doc), \
             patch.object(bot, "_generate_paper", new_callable=AsyncMock, side_effect=PaperError("模型失败")):
            await bot._run_paper_read(123, 456, "", self.cfg, use_file=True)
        self.assertFalse(self.reading.seen_source(123, self.book))


if __name__ == "__main__":
    unittest.main()
