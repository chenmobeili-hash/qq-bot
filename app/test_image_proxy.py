import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from yarl import URL
import bot
from image_network import ImageAddressError, ImageDoHResolver
import image_proxy
import qq_media

ENV = ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy')
TARGET = 'https://multimedia.nt.qq.com.cn/download?sig=PRIVATE_SIGNATURE&x=a%2Fb'
PROXY = 'http://user:PRIVATE_PASSWORD@proxy.example:8080'


class Context:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        pass


def response(status=200, chunks=(b'picture',), headers=None):
    async def iterate(_):
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk
    return SimpleNamespace(status=status, headers=headers or {}, content=SimpleNamespace(iter_chunked=iterate))


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {key: '' for key in ENV}))
        self.logs, self.requests, self.sessions = [], [], []
        self.cache = SimpleNamespace(get=Mock(return_value=None), put=Mock())
        self.direct = AsyncMock(return_value=(b'direct', 'image/png'))
        self.bot = SimpleNamespace(log=lambda level, text: self.logs.append(text), VISION_DOWNLOAD_TIMEOUT=55,
            _get_vision_image_cache=lambda: self.cache, _download_vision_image_direct=self.direct)
        self.preflight = self.enterContext(patch.object(image_proxy, 'preflight', AsyncMock(side_effect=lambda url, emit: URL(url, encoded=True))))

    def network(self, responses):
        def session(**kwargs):
            self.sessions.append(kwargs)
            class Session(Context):
                def __init__(inner):
                    super().__init__(inner)
                def get(inner, target, **options):
                    self.assertGreater(self.preflight.await_count, 0)
                    self.requests.append((target, options))
                    item = responses[len(self.requests) - 1]
                    if isinstance(item, BaseException):
                        raise item
                    class Request(Context):
                        async def __aenter__(request):
                            for callback in kwargs['trace_configs'][0].on_connection_create_end:
                                await callback()
                            return item
                    return Request(item)
            return Session()
        return patch.object(image_proxy.aiohttp, 'ClientSession', side_effect=session)

    async def test_proxy_first_original_signed_host_default_tls_and_private_auth(self):
        os.environ['HTTPS_PROXY'] = PROXY
        with self.network([response(headers={'Content-Type': 'image/png', 'Content-Length': '7'})]):
            result = await image_proxy.download_image(self.bot, TARGET, 1)
        self.assertEqual(result, (b'picture', 'image/png'))
        self.direct.assert_not_awaited()
        self.assertEqual(str(self.requests[0][0]), TARGET)
        self.assertEqual(str(self.requests[0][1]['proxy']), 'http://proxy.example:8080')
        self.assertEqual(self.requests[0][1]['proxy_auth'].password, 'PRIVATE_PASSWORD')
        self.assertNotIn('ssl', self.requests[0][1])
        self.assertFalse(self.requests[0][1]['allow_redirects'])
        self.cache.put.assert_called_once_with(TARGET, b'picture', 'image/png')
        self.assertIn('route=proxy', '\n'.join(self.logs))
        self.assertNotIn('PRIVATE_', '\n'.join(self.logs))
        self.assertNotIn('proxy.example', '\n'.join(self.logs))

    async def test_cache_hit_precedes_all_dns_proxy_selection_and_transport_without_refreshing_ttl(self):
        os.environ['HTTPS_PROXY'] = PROXY
        self.cache.get.return_value = (b'cached-webp', 'image/webp')
        with patch.object(qq_media, '_environment_proxy') as env, patch.object(image_proxy.aiohttp, 'ClientSession') as session:
            self.assertEqual(await image_proxy.download_image(self.bot, TARGET, 1), self.cache.get.return_value)
        self.preflight.assert_not_awaited()
        env.assert_not_called()
        session.assert_not_called()
        self.direct.assert_not_awaited()
        self.cache.put.assert_not_called()

    async def test_no_proxy_keeps_pinned_direct_transport_after_public_validation(self):
        with patch.object(image_proxy.aiohttp, 'ClientSession') as session:
            result = await image_proxy.download_image(self.bot, TARGET, 2)
        self.assertEqual(result[0], b'direct')
        self.preflight.assert_awaited_once()
        session.assert_not_called()
        self.direct.assert_awaited_once_with(TARGET, 2, cache=False, max_bytes=None,
                                            _url_validator=image_proxy.validate_target)
        self.assertNotIn('fallback', '\n'.join(self.logs))

    async def test_proxy_http_failure_falls_back_once_and_records_sanitized_category(self):
        os.environ['HTTPS_PROXY'] = PROXY
        with self.network([response(status=503)]):
            result = await image_proxy.download_image(self.bot, TARGET, 1)
        self.assertEqual(result[0], b'direct')
        self.direct.assert_awaited_once()
        self.assertIn('event=fallback route=proxy', '\n'.join(self.logs))
        self.assertIn('category=http status=503', '\n'.join(self.logs))

    async def test_connection_tls_payload_and_stall_failures_fall_back_without_raw_exception(self):
        os.environ['HTTPS_PROXY'] = PROXY
        for item, category in ((aiohttp.ClientConnectionError('PRIVATE_ERROR'), 'connect'),
            (aiohttp.ClientSSLError(None, OSError('PRIVATE_ERROR')), 'tls'),
            (response(chunks=(b'old', aiohttp.ClientPayloadError('PRIVATE_ERROR'))), 'payload'),
            (response(chunks=(b'old', asyncio.TimeoutError('PRIVATE_ERROR'))), 'stall')):
            with self.subTest(category=category):
                self.requests.clear()
                self.direct.reset_mock()
                with self.network([item]):
                    self.assertEqual((await image_proxy.download_image(self.bot, TARGET, 1))[0], b'direct')
                self.direct.assert_awaited_once()
                self.assertIn('category=' + category, '\n'.join(self.logs))
        self.assertNotIn('PRIVATE_ERROR', '\n'.join(self.logs))

    async def test_length_mismatch_and_unrequested_partial_response_fall_back(self):
        os.environ['HTTPS_PROXY'] = PROXY
        for item in (response(headers={'Content-Length': '100'}), response(status=206)):
            self.requests.clear()
            self.direct.reset_mock()
            with self.network([item]):
                self.assertEqual((await image_proxy.download_image(self.bot, TARGET, 1))[0], b'direct')
            self.direct.assert_awaited_once()

    async def test_size_violation_is_terminal_and_not_bypassed_by_another_route(self):
        os.environ['HTTPS_PROXY'] = PROXY
        with self.network([response(headers={'Content-Length': '100'})]):
            self.assertIsNone(await image_proxy.download_image(self.bot, TARGET, 1, max_bytes=5))
        self.direct.assert_not_awaited()
        self.cache.put.assert_not_called()

    async def test_redirect_private_dns_rejection_is_terminal_without_fallback(self):
        os.environ['HTTPS_PROXY'] = PROXY
        self.preflight.side_effect = [URL(TARGET, encoded=True), ImageAddressError('PRIVATE_ERROR')]
        with self.network([response(302, headers={'Location': 'https://internal.example/private'})]):
            self.assertIsNone(await image_proxy.download_image(self.bot, TARGET, 1))
        self.assertEqual(len(self.requests), 1)
        self.direct.assert_not_awaited()

    async def test_redirect_revalidates_and_preserves_new_signature(self):
        os.environ['HTTPS_PROXY'] = PROXY
        with self.network([response(302, headers={'Location': '/other?sig=EXACT%2B&x=a%2Fb'}), response()]):
            await image_proxy.download_image(self.bot, TARGET, 1)
        self.assertEqual(self.preflight.await_count, 2)
        self.assertTrue(str(self.requests[-1][0]).endswith('/other?sig=EXACT%2B&x=a%2Fb'))

    async def test_invalid_proxy_uses_same_vm13_selection_rules_and_falls_back_safely(self):
        os.environ['HTTPS_PROXY'] = 'socks5://user:PRIVATE_PASSWORD@proxy.example'
        result = await image_proxy.download_image(self.bot, TARGET, 1)
        self.assertEqual(result[0], b'direct')
        self.assertIn('category=proxy_config', '\n'.join(self.logs))
        self.assertNotIn('PRIVATE_PASSWORD', '\n'.join(self.logs))

    async def test_cancellation_never_falls_back(self):
        os.environ['HTTPS_PROXY'] = PROXY
        with self.network([asyncio.CancelledError()]), self.assertRaises(asyncio.CancelledError):
            await image_proxy.download_image(self.bot, TARGET, 1)
        self.direct.assert_not_awaited()

    async def test_shared_total_deadline_includes_fallback(self):
        os.environ['HTTPS_PROXY'] = PROXY
        self.bot.VISION_DOWNLOAD_TIMEOUT = 0.3
        async def proxy(*args):
            await asyncio.sleep(0.05)
            raise qq_media._ProxyFailure('stall')
        async def direct(*args, **kwargs):
            await asyncio.sleep(0.5)
        self.direct.side_effect = direct
        with patch.object(image_proxy, 'proxy_download', side_effect=proxy):
            self.assertIsNone(await image_proxy.download_image(self.bot, TARGET, 1))
        self.direct.assert_awaited_once()
        self.assertIn('event=overall_timeout route=direct', '\n'.join(self.logs))

    async def test_start_deadline_is_removed_after_first_byte_for_slow_progress(self):
        os.environ['HTTPS_PROXY'] = PROXY
        async def chunks(_):
            yield b'pic'
            await asyncio.sleep(0.08)
            yield b'ture'
        item = response()
        item.content.iter_chunked = chunks
        with self.network([item]), patch.object(image_proxy, 'START_SECONDS', 0.03):
            self.assertEqual((await image_proxy.download_image(self.bot, TARGET, 1))[0], b'picture')
        self.direct.assert_not_awaited()

    async def test_first_byte_never_arrives_triggers_early_fallback(self):
        os.environ['HTTPS_PROXY'] = PROXY
        async def chunks(_):
            await asyncio.sleep(0.5)
            yield b'late'
        item = response()
        item.content.iter_chunked = chunks
        with self.network([item]), patch.object(image_proxy, 'START_SECONDS', 0.03):
            self.assertEqual((await image_proxy.download_image(self.bot, TARGET, 1))[0], b'direct')
        self.direct.assert_awaited_once()

    async def test_ten_megabyte_webp_bytes_pass_through_unchanged_to_cache(self):
        os.environ['HTTPS_PROXY'] = PROXY
        payload = b'RIFF1234WEBP' + b'animation-data' * 800000
        with self.network([response(chunks=(payload,), headers={'Content-Type': 'image/webp', 'Content-Length': str(len(payload))})]):
            result = await image_proxy.download_image(self.bot, TARGET, 1)
        self.assertEqual(result, (payload, 'image/webp'))
        self.assertEqual(self.cache.put.call_args.args[1], payload)

    async def test_literal_and_nonpublic_image_targets_are_rejected_without_any_network(self):
        for target in ('https://8.8.8.8/a', 'http://127.0.0.1/a', 'http://[::1]/a', 'https://localhost/a',
                       'https://images.example:8080/a'):
            with self.subTest(target=target):
                self.assertIsNone(await image_proxy.download_image(self.bot, target, 1))
        self.preflight.assert_not_awaited()
        self.direct.assert_not_awaited()


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_doh_mixed_public_private_answers_fail_before_proxy_or_direct(self):
        payload = {'Status': 0, 'Answer': [{'type': 1, 'data': '1.1.1.1'}, {'type': 1, 'data': '10.0.0.1'}]}
        fake = SimpleNamespace(VISION_DOWNLOAD_TIMEOUT=55, log=Mock(),
            _get_vision_image_cache=lambda: SimpleNamespace(get=lambda url: None), _download_vision_image_direct=AsyncMock())
        with patch.dict(os.environ, {'HTTPS_PROXY': PROXY}), \
             patch.object(ImageDoHResolver, '_query', AsyncMock(return_value=payload)), \
             patch.object(image_proxy, 'proxy_download', AsyncMock()) as proxy:
            self.assertIsNone(await image_proxy.download_image(fake, TARGET, 1))
        proxy.assert_not_awaited()
        fake._download_vision_image_direct.assert_not_awaited()

    async def test_unavailable_doh_does_not_switch_to_unvalidated_proxy_or_direct(self):
        fake = SimpleNamespace(VISION_DOWNLOAD_TIMEOUT=55, log=Mock(),
            _get_vision_image_cache=lambda: SimpleNamespace(get=lambda url: None), _download_vision_image_direct=AsyncMock())
        with patch.dict(os.environ, {'HTTPS_PROXY': PROXY}), \
             patch.object(ImageDoHResolver, '_query', AsyncMock(side_effect=aiohttp.ClientConnectionError('PRIVATE_ERROR'))), \
             patch.object(image_proxy, 'proxy_download', AsyncMock()) as proxy:
            self.assertIsNone(await image_proxy.download_image(fake, TARGET, 1))
        proxy.assert_not_awaited()
        fake._download_vision_image_direct.assert_not_awaited()

    async def test_media_cache_false_entry_never_reenters_image_proxy(self):
        with patch.object(bot, '_download_vision_image_direct', AsyncMock(return_value=(b'media', 'video/mp4'))) as direct, \
             patch.object(image_proxy, 'download_image', AsyncMock()) as image:
            result = await bot._download_vision_image('http://43.129.139.192/file', 1, cache=False, max_bytes=100)
        self.assertEqual(result[0], b'media')
        direct.assert_awaited_once_with('http://43.129.139.192/file', 1, cache=False, max_bytes=100)
        image.assert_not_awaited()

    async def test_successful_media_proxy_failure_uses_direct_only_once_with_original_media_ip(self):
        with patch.dict(os.environ, {'HTTP_PROXY': PROXY}), patch.object(bot, 'log'), \
             patch.object(qq_media, '_proxy_download', AsyncMock(side_effect=qq_media._ProxyFailure('stall'))), \
             patch.object(bot, '_download_vision_image_direct', AsyncMock(return_value=(b'media', 'video/mp4'))) as direct, \
             patch.object(image_proxy, 'download_image', AsyncMock()) as image:
            result = await qq_media.download_media(bot, 'http://43.129.139.192/file', 100)
        self.assertEqual(result[0], b'media')
        direct.assert_awaited_once()
        image.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
