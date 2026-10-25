from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
import image_proxy
from vision_sync import VisionSync


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sync.json"
        self.sync = VisionSync(self.path)
        self.config = {"qwen_vl": {"enabled": True, "model": "independent-vision", "endpoint": "https://vision.example/v1",
                                   "api_key": "FAKE_INDEPENDENT_KEY", "trigger_groups": [123]}}
        self.main = {"enabled": True, "model": "main-multimodal", "endpoint": "https://main.example/v1", "api_key": "FAKE_MAIN_KEY"}

    def test_default_independent_and_disable_restores_exact_original(self):
        before = deepcopy(self.config)
        self.assertEqual(self.sync.route(self.config), self.config["qwen_vl"])
        self.sync.set_enabled(True)
        self.assertEqual(self.sync.route(self.config, self.main)["model"], "main-multimodal")
        self.sync.set_enabled(False)
        self.assertEqual(self.sync.route(self.config, self.main), before["qwen_vl"])
        self.assertEqual(self.config, before)

    def test_enabled_follows_model_provider_and_preserves_group_restrictions(self):
        self.sync.set_enabled(True)
        first = self.sync.route(self.config, self.main)
        self.assertEqual(first["trigger_groups"], [123])
        self.main.update(model="next-model", endpoint="https://next.example/v1", api_key="FAKE_NEXT_KEY")
        next_route = self.sync.route(self.config, self.main)
        self.assertEqual(next_route["model"], "next-model")
        self.assertEqual(next_route["endpoint"], "https://next.example/v1")
        self.assertNotEqual(first["api_key"], next_route["api_key"])

    def test_state_persists_only_boolean_never_credentials(self):
        self.sync.set_enabled(True)
        self.assertTrue(VisionSync(self.path).enabled)
        saved = self.path.read_text()
        self.assertNotIn("KEY", saved)
        self.assertNotIn("endpoint", saved)

    def test_disabled_original_vision_can_be_synced_then_restores_disabled(self):
        self.config["qwen_vl"]["enabled"] = False
        self.sync.set_enabled(True)
        self.assertTrue(self.sync.route(self.config, self.main)["enabled"])
        self.sync.set_enabled(False)
        self.assertFalse(self.sync.route(self.config, self.main)["enabled"])

    def test_missing_main_route_and_corruption_do_not_silently_fall_back(self):
        self.sync.set_enabled(True)
        self.assertFalse(self.sync.route(self.config, {})["enabled"])
        self.path.write_text("broken", encoding="utf-8")
        broken = VisionSync(self.path)
        self.assertFalse(broken.storage_ok)
        self.assertFalse(broken.route(self.config, self.main)["enabled"])

    def test_failed_write_preserves_previous_switch(self):
        with patch.object(Path, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.sync.set_enabled(True)
        self.assertFalse(self.sync.enabled)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sync = VisionSync(Path(self.directory.name) / "sync.json")
        self.config = {"admin_qq": [2], "deepseek": {"enabled": True},
                       "qwen_vl": {"enabled": True, "model": "old-vision", "endpoint": "https://old.example/v1", "api_key": "OLD_FAKE"}}
        self.main_client = SimpleNamespace(api_key="MAIN_FAKE", base_url="https://main.example/v1/")
        self.data = {"message_type": "group", "group_id": 123, "sender": {"user_id": 2}}
        for item in (patch.object(bot, "vision_sync", self.sync), patch.object(bot, "load_config", return_value=self.config),
                     patch.object(bot, "_init_deepseek"), patch.object(bot, "deepseek_client", self.main_client),
                     patch.object(bot, "current_model", "main-multimodal")):
            item.start()
            self.addCleanup(item.stop)

    async def test_global_command_admin_check_and_immediate_reversible_config(self):
        before = deepcopy(self.config)
        with patch.object(bot, "send_msg_to_user", new_callable=AsyncMock) as send:
            self.data["sender"]["user_id"] = 3
            await bot._handle_vision_sync_command(self.data, "视觉同步 开启", self.config)
            self.assertFalse(self.sync.enabled)
            self.data["sender"]["user_id"] = 2
            await bot._handle_vision_sync_command(self.data, "视觉同步 开启", self.config)
            self.assertTrue(self.sync.enabled)
            self.assertIn("所有群", send.await_args.args[2])
            self.assertEqual(bot._effective_vision_config(self.config)["model"], "main-multimodal")
            await bot._handle_vision_sync_command(self.data, "视觉同步 关闭", self.config)
        self.assertEqual(bot._effective_vision_config(self.config)["model"], "old-vision")
        self.assertEqual(self.config, before)

    async def test_bilibili_real_frame_request_uses_synced_model(self):
        self.sync.set_enabled(True)
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="固定测试画面"), finish_reason="stop")]))
        client = SimpleNamespace(with_options=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        with patch.object(bot, "_ensure_qwen_vl_client", new_callable=AsyncMock, return_value=True) as ensure, \
             patch.object(bot, "qwen_vl_client", client):
            result = await bot._describe_bilibili_frames([{"second": 1, "base64": "TEST_IMAGE_FIXTURE"}], self.config)
        self.assertIn("固定测试画面", result)
        self.assertEqual(create.await_args.kwargs["model"], "main-multimodal")
        self.assertEqual(ensure.await_args.args[0]["api_key"], "MAIN_FAKE")

    async def test_nonvision_model_error_does_not_fall_back_to_old_vision(self):
        self.sync.set_enabled(True)
        create = AsyncMock(side_effect=ValueError("model does not support image input"))
        client = SimpleNamespace(with_options=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        with patch.object(bot, "_ensure_qwen_vl_client", new_callable=AsyncMock, return_value=True), \
             patch.object(bot, "qwen_vl_client", client):
            with self.assertRaises(ValueError):
                await bot._describe_bilibili_frames([{"second": 1, "base64": "TEST"}], self.config)
        create.assert_awaited_once()

    async def test_switch_during_frame_request_cancels_old_description(self):
        self.sync.set_enabled(True)
        async def create(**kwargs):
            self.sync.set_enabled(False)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="old result"), finish_reason="stop")])
        client = SimpleNamespace(with_options=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        with patch.object(bot, "_ensure_qwen_vl_client", new_callable=AsyncMock, return_value=True), \
             patch.object(bot, "qwen_vl_client", client):
            self.assertIsNone(await bot._describe_bilibili_frames([{"second": 1, "base64": "TEST"}], self.config))

    async def test_direct_picture_uses_effective_main_vision_route(self):
        self.sync.set_enabled(True)
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="角色特征：固定测试\n关键词：测试\n场景：测试\n文字：无"), finish_reason="stop")]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        async def chunks(_size):
            yield b'public-fixture'
        response = SimpleNamespace(status=200, headers={"Content-Type": "image/png"},
            content=SimpleNamespace(iter_chunked=chunks))
        download_budgets = []
        class Context:
            async def __aenter__(self):
                return response
            async def __aexit__(self, *args):
                pass
        class Session(Context):
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                await connector.close()
            def get(self, *args, **kwargs):
                download_budgets.append(kwargs['timeout'].total)
                return Context()
        connector = bot.aiohttp.TCPConnector(force_close=True)
        with patch.object(bot, "_ensure_qwen_vl_client", new_callable=AsyncMock, return_value=True), \
             patch.object(image_proxy, 'preflight', AsyncMock()), \
             patch.object(image_proxy.qq_media, '_environment_proxy', return_value=None), \
             patch.object(bot, "qwen_vl_client", client), patch.object(bot.aiohttp, "TCPConnector", return_value=connector), \
             patch.object(bot.aiohttp, "ClientSession", return_value=Session()), \
             patch.object(bot, '_get_vision_image_cache', return_value=SimpleNamespace(get=lambda url: None, put=lambda *args: None)), \
             patch.object(bot, "_saucenao_search_only", new_callable=AsyncMock, return_value=None), \
             patch.object(bot, "_saucenao_compose", new_callable=AsyncMock, return_value=None), patch.object(bot, "log"):
            result = await bot.describe_images(["https://images.example/public.png"])
        self.assertIn("固定测试", result)
        self.assertEqual(create.await_args.kwargs["model"], "main-multimodal")
        self.assertEqual(download_budgets, [55.0])


if __name__ == "__main__":
    unittest.main()
