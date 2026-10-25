import asyncio
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from yarl import URL
import bot
import image_proxy
from image_network import ImageAddressError, ImageDoHResolver
import qq_media
import test_image_proxy as fixtures

REAL_PREFLIGHT = image_proxy.preflight


class StrictProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ProxyTests.setUp(self)
        self.config = {'image_download': {'proxy_only': True}}
        self.bot.load_config = lambda: self.config

    network = fixtures.ProxyTests.network

    async def test_proxy_success_keeps_signed_url_tls_size_limit_and_cache(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        with self.network([fixtures.response(headers={'Content-Type': 'image/png', 'Content-Length': '7'})]):
            result = await image_proxy.download_image(self.bot, fixtures.TARGET, 1, max_bytes=10)
        self.assertEqual(result, (b'picture', 'image/png'))
        self.assertEqual(str(self.requests[0][0]), fixtures.TARGET)
        self.assertNotIn('ssl', self.requests[0][1])
        self.direct.assert_not_awaited()
        self.cache.put.assert_called_once_with(fixtures.TARGET, *result)

    async def test_proxy_failure_categories_never_trigger_direct_or_log_secrets(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        for category in ('connect', 'stall', 'tls', 'payload', 'length', 'http'):
            with self.subTest(category=category), patch.object(image_proxy, 'proxy_download',
                    AsyncMock(side_effect=qq_media._ProxyFailure(category, 503 if category == 'http' else None))):
                self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.direct.assert_not_awaited()
        self.cache.put.assert_not_called()
        logs = '\n'.join(self.logs)
        self.assertIn('event=proxy_only_failure', logs)
        self.assertNotIn('event=fallback', logs)
        self.assertNotIn('route=direct', logs)
        self.assertNotIn('PRIVATE_', logs)

    async def test_transport_exception_also_cannot_fall_back(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=OSError('PRIVATE_ERROR'))):
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.direct.assert_not_awaited()
        self.assertNotIn('PRIVATE_ERROR', '\n'.join(self.logs))

    async def test_missing_proxy_does_not_resolve_or_dial_cdn(self):
        self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.preflight.assert_not_awaited()
        self.direct.assert_not_awaited()
        self.assertIn('category=proxy_missing', '\n'.join(self.logs))

    async def test_unsupported_socks_config_fails_closed(self):
        os.environ['HTTPS_PROXY'] = 'socks5://PRIVATE_USER:PRIVATE_PASS@proxy.example:1080'
        self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.preflight.assert_not_awaited()
        self.direct.assert_not_awaited()
        self.assertIn('category=proxy_config', '\n'.join(self.logs))
        self.assertNotIn('PRIVATE_', '\n'.join(self.logs))

    async def test_cache_hit_works_without_proxy_and_does_not_refresh_ttl(self):
        self.cache.get.return_value = (b'cached', 'image/jpeg')
        with patch.object(qq_media, '_environment_proxy') as select:
            self.assertEqual(await image_proxy.download_image(self.bot, fixtures.TARGET, 1), self.cache.get.return_value)
        select.assert_not_called()
        self.preflight.assert_not_awaited()
        self.direct.assert_not_awaited()
        self.cache.put.assert_not_called()
        self.assertIn('event=cache_hit route=cache', '\n'.join(self.logs))

    async def test_bad_boolean_never_silently_enables_direct(self):
        for value in ('false', 'true', 0, 1, None, []):
            self.config['image_download']['proxy_only'] = value
            with self.subTest(value=value):
                self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.preflight.assert_not_awaited()
        self.direct.assert_not_awaited()

    async def test_default_false_and_live_toggle_preserve_legacy_fallback(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('connect'))):
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
            self.config.clear()
            self.assertEqual((await image_proxy.download_image(self.bot, fixtures.TARGET, 1))[0], b'direct')
            self.config['image_download'] = {'proxy_only': True}
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.direct.assert_awaited_once()

    async def test_preflight_rejection_still_stops_all_image_routes(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        self.preflight.side_effect = ImageAddressError('blocked public-DNS validation')
        with patch.object(image_proxy, 'proxy_download', AsyncMock()) as proxy:
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        proxy.assert_not_awaited()
        self.direct.assert_not_awaited()

    async def test_cancellation_is_not_converted_into_direct_fallback(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await image_proxy.download_image(self.bot, fixtures.TARGET, 1)
        self.direct.assert_not_awaited()

    async def test_shared_total_deadline_still_ends_strict_request(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        self.bot.VISION_DOWNLOAD_TIMEOUT = .05
        async def slow(*args):
            await asyncio.sleep(1)
        with patch.object(image_proxy, 'proxy_download', slow):
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.direct.assert_not_awaited()
        self.assertIn('event=overall_timeout', '\n'.join(self.logs))

    async def test_media_cache_false_does_not_inherit_image_only_policy(self):
        with patch.object(bot, 'load_config', return_value=self.config), \
             patch.object(bot, '_download_vision_image_direct', AsyncMock(return_value=(b'video', 'video/mp4'))) as direct:
            result = await bot._download_vision_image('http://43.129.139.192/file', 1, cache=False, max_bytes=100)
        self.assertEqual(result, (b'video', 'video/mp4'))
        direct.assert_awaited_once_with('http://43.129.139.192/file', 1, cache=False, max_bytes=100)

    async def test_full_entry_uses_real_local_proxy_and_real_preflight_control_flow(self):
        from aiohttp import web
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(buffer, format='PNG')
        payload, received, mode = buffer.getvalue(), [], [200]
        async def handler(request):
            received.append(request.raw_path)
            return web.Response(status=mode[0], body=payload, content_type='image/png')
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        os.environ['HTTP_PROXY'] = f'http://127.0.0.1:{port}'
        dns = {'Status': 0, 'Answer': [{'type': 1, 'data': '8.8.8.8'}]}
        target = 'http://images.example/picture?sig=PRIVATE_TEST'
        with patch.object(image_proxy, 'preflight', REAL_PREFLIGHT), \
             patch.object(ImageDoHResolver, '_query', AsyncMock(return_value=dns)):
            result = await image_proxy.download_image(self.bot, target, 1, max_bytes=10000)
            self.assertEqual(result, (payload, 'image/png'))
            Image.open(io.BytesIO(result[0])).verify()
            mode[0] = 503
            self.assertIsNone(await image_proxy.download_image(self.bot, target, 1, max_bytes=10000))
        self.assertEqual(len(received), 2)
        self.direct.assert_not_awaited()
        self.assertNotIn('PRIVATE_TEST', '\n'.join(self.logs))


if __name__ == '__main__':
    unittest.main()
