import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

import bot
from bilibili_frames import capture_frames
from bilibili_account import BilibiliAccount, BilibiliApiError
from bilibili_feed import BilibiliFeed, BilibiliReader

BV = "BV17x411w7KC"


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = BilibiliAccount(self.tmp.name)
        self.cookies = {"SESSDATA": "test-session", "bili_jct": "test-csrf", "DedeUserID": "42"}
        self.patch = patch.object(self.account, "cookies", return_value=self.cookies)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_post_body_and_cross_restart_deduplication(self):
        request = AsyncMock(return_value=({"rpid": 123}, {}))
        with patch.object(self.account, "request", request):
            result = await self.account.interact(BV, True, "这个演示挺清楚的。")
            self.assertIn("已点赞", result)
            self.assertIn("评论已提交", result)
            self.assertEqual(request.await_count, 2)
            forms = [call.kwargs["form"] for call in request.await_args_list]
            self.assertEqual(forms[0]["csrf"], "test-csrf")
            self.assertEqual(forms[1]["type"], "1")
            self.assertEqual(forms[1]["message"], "这个演示挺清楚的。")
        restarted = BilibiliAccount(self.tmp.name)
        with patch.object(restarted, "cookies", return_value=self.cookies), \
                patch.object(restarted, "request", new_callable=AsyncMock) as again:
            await restarted.interact(BV, True, "新的一条也不能重复提交。")
            again.assert_not_awaited()
        journal = self.account.journal_path.read_text(encoding="utf-8")
        self.assertNotIn("test-session", journal)
        self.assertNotIn("test-csrf", journal)

    async def test_timeout_is_not_retried(self):
        with patch.object(self.account, "request", new_callable=AsyncMock, side_effect=TimeoutError) as request:
            first = await self.account.interact(BV, comment="具体的评论。")
            await self.account.interact(BV, comment="具体的评论。")
            request.assert_awaited_once()
            self.assertIn("未自动重试", first)
        journal = json.loads(self.account.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(next(iter(journal.values()))["state"], "uncertain")

    async def test_platform_rejection_is_distinct_from_unknown_result(self):
        with patch.object(self.account, "request", new_callable=AsyncMock, side_effect=BilibiliApiError(-101)):
            result = await self.account.interact(BV, comment="具体评论")
        self.assertIn("被平台拒绝", result)
        self.assertIn("-101", result)
        journal = json.loads(self.account.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(next(iter(journal.values()))["state"], "rejected")

    async def test_missing_auth_or_cancellation_never_posts(self):
        with patch.object(self.account, "request", new_callable=AsyncMock) as request:
            self.account.cookies.return_value = {"SESSDATA": "test"}
            await self.account.interact(BV, True, "具体评论")
            self.account.cookies.return_value = self.cookies
            await self.account.interact(BV, True, "具体评论", active=lambda: False)
            request.assert_not_awaited()

    async def test_turning_off_between_like_and_comment_cancels_comment(self):
        enabled = True
        async def completed(*args, **kwargs):
            nonlocal enabled
            enabled = False
            return {}, {}
        with patch.object(self.account, "request", new_callable=AsyncMock, side_effect=completed) as request:
            result = await self.account.interact(BV, True, "具体评论", active=lambda: enabled)
            request.assert_awaited_once()
            self.assertIn("取消", result)

    async def test_concurrent_calls_only_submit_once(self):
        with patch.object(self.account, "request", new_callable=AsyncMock, return_value=({}, {})) as request:
            await asyncio.gather(*(self.account.interact(BV, comment="具体评论") for _ in range(4)))
            request.assert_awaited_once()

    async def test_global_rolling_comment_quota(self):
        self.account.atomic_json(self.account.journal_path, {
            f"42:video{n}:comment": {"state": "accepted", "at": time.time()} for n in range(3)})
        with patch.object(self.account, "request", new_callable=AsyncMock) as request:
            result = await self.account.interact(BV, comment="具体评论")
            request.assert_not_awaited()
            self.assertIn("24小时额度", result)

    async def test_comment_validation(self):
        for comment in ("x" * 121, "@所有人", "https://example.com", "[CQ:at,qq=all]"):
            with self.assertRaises(ValueError):
                await self.account.interact(BV, comment=comment)

    async def test_login_poll_owner_binding(self):
        self.account.pending["1"] = {"key": "secret-key", "expires": time.time() + 100}
        with patch.object(self.account, "request", new_callable=AsyncMock) as request:
            result = await self.account.poll_login("2")
            self.assertIn("过期", result)
            request.assert_not_awaited()

    async def test_current_qr_host_and_reject_foreign_host(self):
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(self.account, "request", new_callable=AsyncMock, return_value=(
                    {"url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?key=test", "qrcode_key": "test-key"}, {})) as request:
            image = await self.account.start_login("1")
            self.assertTrue(image.startswith(b"\x89PNG"))
            request.return_value = ({"url": "https://account.bilibili.com.evil.example/", "qrcode_key": "bad"}, {})
            with self.assertRaises(ValueError):
                await self.account.start_login("2")

    async def test_login_success_saves_only_credentials(self):
        self.account.pending["1"] = {"key": "secret-key", "expires": time.time() + 100}
        with patch.object(self.account, "request", new_callable=AsyncMock, return_value=({"code": 0}, self.cookies)):
            result = await self.account.poll_login("1")
        self.assertIn("已保存", result)
        self.assertNotIn("test-session", result)
        values = json.loads(self.account.credentials_path.read_text(encoding="utf-8"))
        self.assertEqual(values["bili_jct"], "test-csrf")


class ReaderAndBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_cdn_bytes_decode_locally_without_forwarding_account_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            account = SimpleNamespace(cookies=lambda: {"SESSDATA": "not-for-cdn"}, request=AsyncMock(return_value=(
                {"durl": [{"url": "https://upos-hz-mirrorakam.akamaized.net/media.mp4?public-test=1"}]}, {})))
            async def chunks(_):
                yield b'actual-video-bytes'
            response = SimpleNamespace(status=200, headers={}, raise_for_status=lambda: None,
                                       content=SimpleNamespace(iter_chunked=chunks))
            class Context:
                async def __aenter__(self):
                    return response
                async def __aexit__(self, *args):
                    pass
            def session(**kwargs):
                self.assertNotIn('cookies', kwargs)
                self.assertNotIn('not-for-cdn', str(kwargs))
                class Session(Context):
                    async def __aenter__(self):
                        return self
                    def get(inner, url, **options):
                        self.assertFalse(options['allow_redirects'])
                        return Context()
                return Session()
            actual = [{'second': i, 'base64': 'fixture'} for i in range(6)]
            with patch('bilibili_frames.aiohttp.ClientSession', side_effect=session), \
                    patch('bilibili_frames.media_reader.capture_video', AsyncMock(return_value={'frames': actual})) as capture:
                frames, _ = await capture_frames(account, BV, 1, 30, directory)
            self.assertEqual(len(frames), 6)
            self.assertEqual(capture.await_args.args[0], b'actual-video-bytes')
            self.assertEqual(capture.await_args.kwargs['frame_limit'], 40)
            self.assertFalse(any(Path(directory).iterdir()))

    async def test_arbitrary_media_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for url in ("https://localhost/secret", "https://bilivideo.com.evil.example/a", "http://upos-x.bilivideo.com/a"):
                account = SimpleNamespace(cookies=lambda: {}, request=AsyncMock(return_value=({"durl": [{"url": url}]}, {})))
                with patch('bilibili_frames.download', new_callable=AsyncMock) as download:
                    with self.assertRaises(ValueError):
                        await capture_frames(account, BV, 1, 30, directory)
                    download.assert_not_awaited()

    async def test_feed_only_executes_autonomous_actions_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = SimpleNamespace(search=AsyncMock(return_value=[BV]), read=AsyncMock(return_value={
                "bvid": BV, "url": "https://www.bilibili.com/video/" + BV,
                "frames_loaded": True, "frames": [{"second": 1, "base64": "AA=="}]}))
            feed = BilibiliFeed(Path(directory) / "state.json", reader)
            config = {"deepseek": {"enabled": True}, "bilibili": {"quiet_start": 0, "quiet_end": 0}}
            now = lambda cfg: datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
            generate = AsyncMock(return_value={"share": "采样画面里的演示不错。", "like": True, "comment": "演示清楚。"})
            send = AsyncMock(return_value=True)
            with patch.object(feed.account, "interact", new_callable=AsyncMock, return_value="评论已提交") as interact:
                await feed.tick(lambda: config, now, lambda: True, generate, send)
                interact.assert_not_awaited()
                feed.configure(123, "开启", ["软件"])
                await feed.tick(lambda: config, now, lambda: True, generate, send)
                interact.assert_awaited_once()
                self.assertTrue(generate.await_args.args[2]["_bilibili_interactions"])
                feed.configure(123, "互动关闭")
                feed.groups["123"].update(next_at=0, seen=[])
                await feed.tick(lambda: config, now, lambda: True, generate, send)
                self.assertEqual(interact.await_count, 1)

    async def test_stale_persona_guard_blocks_public_post(self):
        with tempfile.TemporaryDirectory() as directory:
            account = BilibiliAccount(directory)
            with patch.object(account, "cookies", return_value={"SESSDATA": "s", "bili_jct": "c"}), \
                    patch.object(account, "request", new_callable=AsyncMock) as request:
                await account.interact(BV, True, "正文评论", active=lambda: False)
                request.assert_not_awaited()

    async def test_anonymous_subtitles_use_public_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            account = BilibiliAccount(directory)
            reader = BilibiliReader(account)
            fake = SimpleNamespace(get_info=AsyncMock(return_value={"title": "演示", "pages": [{"cid": 1, "duration": 10}]}),
                                   get_aid=lambda: 170001, get_player_info=AsyncMock())
            api = SimpleNamespace(Video=lambda **kwargs: fake)
            async def public(host, path, **kwargs):
                if path == "/x/web-interface/view":
                    return {"title": "演示", "pages": [{"cid": 1, "duration": 10}]}, {}
                return {"need_login_subtitle": True}, {}
            with patch.object(reader, "api", return_value=(api, None, None)), \
                    patch.object(account, "cookies", return_value={}), \
                    patch.object(account, "request", new_callable=AsyncMock, side_effect=public) as request, \
                    patch("bilibili_frames.capture_frames", new_callable=AsyncMock, return_value=([{"second": 1, "base64": "AA=="}], "actual frame")):
                item = await reader.read(BV)
                self.assertTrue(item["frames_loaded"])
                self.assertTrue(item["need_login_subtitle"])
                self.assertFalse(item["transcript_loaded"])
                fake.get_player_info.assert_not_awaited()
                self.assertEqual(request.await_args.args[1], "/x/player/v2")

    async def test_group_login_never_generates_qr(self):
        with patch.object(bot, "is_admin", return_value=True), patch.object(bot, "send_msg_to_user", new_callable=AsyncMock) as send, \
                patch.object(bot, "_get_bilibili_feed") as get:
            await bot._handle_bilibili_account_command({"message_type": "group", "sender": {"user_id": 1}}, "B站登录", {})
            get.assert_not_called()
            self.assertIn("私聊", send.await_args.args[2])

    async def test_manual_interaction_requires_admin(self):
        with patch.object(bot, "is_admin", return_value=False), patch.object(bot, "send_msg_to_user", new_callable=AsyncMock), \
                patch.object(bot, "_get_bilibili_feed") as get:
            await bot._handle_bilibili_account_command({"sender": {"user_id": 2}}, "B站评论 " + BV + " 内容", {})
            get.assert_not_called()

    async def test_frames_are_not_sent_to_text_model_and_interaction_policy(self):
        config = {"deepseek": {}, "_bilibili_interactions": True}
        item = {"frames_loaded": True, "frames": [{"second": 1, "base64": "PRIVATE_IMAGE_BYTES"}], "subtitle": ""}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"share":"基于采样画面，这个演示清楚。","like":true,"comment":"操作演示挺清楚的。"}'), finish_reason="stop")])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options = lambda **kwargs: client
        with patch.object(bot, "_describe_bilibili_frames", new_callable=AsyncMock, return_value="采样图展示了软件操作"), \
                patch.object(bot, "_persona_prompt", return_value="当前人格"), patch.object(bot, "_runtime_prompt", return_value=""), \
                patch.object(bot, "_init_deepseek"), patch.object(bot, "deepseek_client", client), \
                patch.object(bot, "load_config", return_value=config):
            result = await bot._generate_bilibili(item, "软件", config, True)
            self.assertTrue(result["like"])
            self.assertEqual(result["comment"], "操作演示挺清楚的。")
            self.assertNotIn("PRIVATE_IMAGE_BYTES", str(create.await_args.kwargs["messages"]))
            config["_bilibili_interactions"] = False
            result = await bot._generate_bilibili(item, "软件", config, True)
            self.assertFalse(result["like"])
            self.assertEqual(result["comment"], "")

    async def test_frames_without_vision_description_cannot_trigger_interaction(self):
        with patch.object(bot, "_describe_bilibili_frames", new_callable=AsyncMock, return_value=None), \
                patch.object(bot, "_persona_prompt", return_value="persona"), patch.object(bot, "_init_deepseek") as init:
            self.assertIsNone(await bot._generate_bilibili({"frames_loaded": True, "frames": [{}]}, "猫", {}, True))
            init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
