import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import image_proxy
import qq_media
import test_image_proxy as fixtures


class ImageProxyConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ProxyTests.setUp(self)
        self.config = {'image_download': {'proxy_only': True, 'image_proxy_url': 'http://127.0.0.1:18092'}}
        self.loader = Mock(return_value=self.config)
        self.bot.load_config = self.loader

    network = fixtures.ProxyTests.network

    async def test_override_uses_local_proxy_without_changing_environment(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        before = dict(os.environ)
        with patch.object(qq_media, '_environment_proxy') as env, self.network([fixtures.response()]):
            self.assertIsNotNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        env.assert_not_called()
        self.assertEqual(str(self.requests[0][1]['proxy']), 'http://127.0.0.1:18092')
        self.assertEqual(before, dict(os.environ))
        self.loader.assert_called_once()
        self.direct.assert_not_awaited()

    async def test_proxy_credentials_are_separate_and_not_logged(self):
        self.config['image_download']['image_proxy_url'] = fixtures.PROXY
        with self.network([fixtures.response()]):
            self.assertIsNotNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.assertEqual(self.requests[0][1]['proxy_auth'].password, 'PRIVATE_PASSWORD')
        self.assertNotIn('PRIVATE_', '\n'.join(self.logs))
        self.assertNotIn('proxy.example', '\n'.join(self.logs))

    async def test_invalid_explicit_proxy_never_uses_environment_or_direct(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        for value in ('', None, 42, 'socks5://proxy.example:1080', 'http://proxy.example/path',
                      'http://proxy.example:bad', 'http://proxy.example?token=PRIVATE_TOKEN'):
            self.config['image_download']['image_proxy_url'] = value
            with self.subTest(value=value), patch.object(qq_media, '_environment_proxy') as env:
                self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
                env.assert_not_called()
        self.direct.assert_not_awaited()
        self.preflight.assert_not_awaited()

    async def test_invalid_proxy_also_fails_closed_when_legacy_fallback_enabled(self):
        self.config['image_download'].update(proxy_only=False, image_proxy_url='not-a-proxy')
        with patch.object(qq_media, '_environment_proxy') as env:
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        env.assert_not_called()
        self.direct.assert_not_awaited()

    async def test_missing_override_retains_existing_environment_route(self):
        self.config['image_download'].pop('image_proxy_url')
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        with self.network([fixtures.response()]):
            self.assertIsNotNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.assertEqual(str(self.requests[0][1]['proxy']), 'http://proxy.example:8080')

    async def test_cache_hit_still_avoids_proxy_config_and_network(self):
        self.cache.get.return_value = b'cached', 'image/png'
        self.assertEqual(await image_proxy.download_image(self.bot, fixtures.TARGET, 1), self.cache.get.return_value)
        self.loader.assert_not_called()
        self.preflight.assert_not_awaited()

    async def test_failure_retries_same_override_without_direct(self):
        with patch.object(image_proxy, 'proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('connect'))) as fetch, \
             patch.object(image_proxy.asyncio, 'sleep', AsyncMock()):
            self.assertIsNone(await image_proxy.download_image(self.bot, fixtures.TARGET, 1))
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(fetch.await_args_list[0].args[1], fetch.await_args_list[1].args[1])
        self.direct.assert_not_awaited()

    async def test_redirect_and_tls_options_remain_validated(self):
        with self.network([fixtures.response()]):
            await image_proxy.download_image(self.bot, fixtures.TARGET, 1)
        self.assertNotIn('ssl', self.requests[0][1])
        self.assertFalse(self.requests[0][1]['allow_redirects'])
        self.assertEqual(str(self.requests[0][0]), fixtures.TARGET)

    async def test_media_selector_still_uses_environment_proxy(self):
        os.environ['HTTPS_PROXY'] = fixtures.PROXY
        proxy, _ = qq_media._environment_proxy(fixtures.TARGET)
        self.assertEqual(str(proxy), 'http://proxy.example:8080')
        self.loader.assert_not_called()


class PrivateConnectionLogTests(unittest.TestCase):
    def test_credentials_query_and_fragment_are_removed(self):
        value = bot._redacted_connection_url('ws://PRIVATE_USER:PRIVATE_PASSWORD@127.0.0.1:5098/api?access_token=PRIVATE_TOKEN#PRIVATE_FRAGMENT')
        self.assertEqual(value, 'ws://127.0.0.1:5098/api')
        self.assertNotIn('PRIVATE_', value)

    def test_ipv6_endpoint_keeps_host_and_port(self):
        self.assertEqual(bot._redacted_connection_url('ws://[::1]:5098/?token=PRIVATE_TOKEN'), 'ws://[::1]:5098/')

    def test_invalid_endpoints_never_return_raw_input(self):
        for value in ('PRIVATE_SECRET', 'ws://host:PRIVATE_PASSWORD', None):
            self.assertEqual(bot._redacted_connection_url(value), '<configured endpoint>')


if __name__ == '__main__':
    unittest.main()
