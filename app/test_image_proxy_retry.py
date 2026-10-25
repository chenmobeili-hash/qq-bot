import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import image_proxy
from image_network import ImageAddressError
import qq_media
import test_image_proxy as fixtures


class StrictRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ProxyTests.setUp(self)
        self.bot.load_config = lambda: {'image_download': {'proxy_only': True}}
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        self.wait = self.enterContext(patch.object(image_proxy.asyncio, 'sleep', AsyncMock()))

    network = fixtures.ProxyTests.network

    async def test_real_transport_reopens_same_proxy_and_caches_only_complete_result(self):
        with self.network([aiohttp.ClientConnectionError('PRIVATE_ERROR'), fixtures.response()]):
            result = await image_proxy.download_image(self.bot, fixtures.TARGET, 1)
        self.assertEqual(result, (b'picture', 'image/jpeg'))
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(str(self.requests[1][0]), fixtures.TARGET)
        self.assertNotIn('ssl', self.requests[1][1])
        self.assertFalse(self.sessions[1]['trust_env'])
        self.wait.assert_awaited_once_with(1)
        self.cache.put.assert_called_once_with(fixtures.TARGET, *result)
        self.direct.assert_not_awaited()
        logs = '\n'.join(self.logs)
        self.assertIn('event=retry_scheduled', logs)
        self.assertIn('attempt=2', logs)
        self.assertNotIn('event=fallback', logs)
        self.assertNotIn('PRIVATE_', logs)

    async def test_two_connect_failures_stop_without_cache_or_direct(self):
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('connect'))) as fetch:
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.assertEqual(fetch.await_count, 2)
        self.wait.assert_awaited_once_with(1)
        self.direct.assert_not_awaited()
        self.cache.put.assert_not_called()
        self.assertIn('event=proxy_only_failure', '\n'.join(self.logs))

    async def test_each_transient_category_can_recover_on_second_attempt(self):
        for category in ('connect', 'stall', 'transport'):
            self.wait.reset_mock()
            with self.subTest(category=category), patch.object(image_proxy, 'proxy_download',
                    AsyncMock(side_effect=[qq_media._ProxyFailure(category), (b'ok', 'image/png')])) as fetch:
                self.assertEqual(await image_proxy.download_image(self.bot, fixtures.TARGET, 1), (b'ok', 'image/png'))
                self.assertEqual(fetch.await_count, 2)
                self.wait.assert_awaited_once_with(1)
        self.direct.assert_not_awaited()

    async def test_denial_tls_and_invalid_responses_never_retry(self):
        for category, status in (('http', 403), ('http', 407), ('http', 503),
                                 ('tls', None), ('length', None), ('payload', None)):
            with self.subTest(category=category, status=status), patch.object(image_proxy, 'proxy_download',
                    AsyncMock(side_effect=qq_media._ProxyFailure(category, status))) as fetch:
                self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
                fetch.assert_awaited_once()
        self.wait.assert_not_awaited()
        self.direct.assert_not_awaited()

    async def test_preflight_and_size_rejections_are_not_retried(self):
        with patch.object(image_proxy, 'proxy_download', AsyncMock()) as fetch:
            self.preflight.side_effect = ImageAddressError('private DNS answer')
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
            fetch.assert_not_awaited()
        self.preflight.side_effect = None
        with self.network([fixtures.response(headers={'Content-Length': '100'})]):
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1, max_bytes=10))
        self.assertEqual(len(self.requests), 1)
        self.wait.assert_not_awaited()
        self.direct.assert_not_awaited()

    async def test_cancellation_in_backoff_stops_before_second_attempt(self):
        self.wait.side_effect = asyncio.CancelledError
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('connect'))) as fetch:
            with self.assertRaises(asyncio.CancelledError):
                await image_proxy.download_image(self.bot, fixtures.TARGET, 1)
        fetch.assert_awaited_once()
        self.cache.put.assert_not_called()
        self.direct.assert_not_awaited()

    async def test_total_deadline_covers_backoff_instead_of_resetting(self):
        self.bot.VISION_DOWNLOAD_TIMEOUT = .04
        never = asyncio.Event()
        async def hold(_):
            await never.wait()
        self.wait.side_effect = hold
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('connect'))) as fetch:
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        fetch.assert_awaited_once()
        self.assertIn('event=overall_timeout', '\n'.join(self.logs))
        self.direct.assert_not_awaited()

    async def test_second_attempt_uses_original_proxy_snapshot(self):
        seen = []
        async def fetch(url, proxy, limit, emit):
            seen.append(proxy)
            if len(seen) == 1:
                os.environ['HTTPS_PROXY'] = 'http://another.example:9999'
                raise qq_media._ProxyFailure('connect')
            return b'ok', 'image/png'
        with patch.object(image_proxy, 'proxy_download', fetch):
            self.assertIsNotNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.assertEqual(seen[0], seen[1])
        self.direct.assert_not_awaited()

    async def test_non_strict_keeps_existing_single_proxy_then_direct_policy(self):
        self.bot.load_config = lambda: {'image_download': {'proxy_only': False}}
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('connect'))) as fetch:
            self.assertEqual((await image_proxy.download_image(self.bot, fixtures.TARGET, 1))[0], b'direct')
        fetch.assert_awaited_once()
        self.wait.assert_not_awaited()
        self.direct.assert_awaited_once()

    async def test_successful_first_attempt_and_cache_hits_do_not_retry(self):
        with self.network([fixtures.response()]):
            self.assertIsNotNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.assertEqual(len(self.sessions), 1)
        self.cache.get.return_value = (b'cached', 'image/png')
        with patch.object(image_proxy, 'proxy_download', AsyncMock()) as fetch:
            self.assertEqual(await image_proxy.download_image(self.bot, fixtures.TARGET, 1), self.cache.get.return_value)
            fetch.assert_not_awaited()
        self.wait.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
