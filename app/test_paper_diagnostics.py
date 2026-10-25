import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import bot
from paper_reader import PaperError


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_names_actual_stage(self):
        async def timeout():
            raise asyncio.TimeoutError()
        with patch.object(bot, "log") as log:
            with self.assertRaisesRegex(PaperError, "模型阅读回答阶段超时"):
                await bot._paper_stage("模型阅读回答", timeout(), timeout=2)
        self.assertIn("超时", log.call_args.args[1])

    async def test_network_error_does_not_echo_signed_url_or_key(self):
        async def error():
            raise aiohttp.ClientError("https://download.example/file?token=PRIVATE_TOKEN")
        with patch.object(bot, "log") as log:
            with self.assertRaises(PaperError) as result:
                await bot._paper_stage("下载群文件", error(), timeout=2)
        self.assertIn("下载群文件阶段连接失败", str(result.exception))
        self.assertNotIn("PRIVATE_TOKEN", str(result.exception) + str(log.call_args_list))

    async def test_http_status_identifies_model_service_error(self):
        class FakeModelError(Exception):
            status_code = 429
        async def error():
            raise FakeModelError("PRIVATE_SERVICE_BODY")
        with patch.object(bot, "log"):
            with self.assertRaisesRegex(PaperError, "模型阅读回答阶段请求返回HTTP 429"):
                await bot._paper_stage("模型阅读回答", error(), timeout=2)

    async def test_parse_scan_error_preserves_specific_explanation(self):
        async def error():
            raise PaperError("扫描件没有文字，请先OCR")
        with patch.object(bot, "log"):
            with self.assertRaisesRegex(PaperError, "提取文档文字：扫描件"):
                await bot._paper_stage("提取文档文字", error(), timeout=2)

    async def test_download_done_records_only_size(self):
        with patch.object(bot, "log") as log:
            value = await bot._paper_stage("下载群文件", AsyncMock(return_value=(b"test", "pdf", "PRIVATE_URL"))(), timeout=2)
        self.assertEqual(value[0], b"test")
        self.assertIn("下载4字节", log.call_args.args[1])
        self.assertNotIn("PRIVATE_URL", str(log.call_args_list))

    async def test_cancellation_remains_cancellation(self):
        async def cancelled():
            raise asyncio.CancelledError()
        with patch.object(bot, "log"):
            with self.assertRaises(asyncio.CancelledError):
                await bot._paper_stage("下载群文件", cancelled(), timeout=2)


if __name__ == "__main__":
    unittest.main()
