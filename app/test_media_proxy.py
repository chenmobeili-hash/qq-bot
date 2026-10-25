import asyncio
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from yarl import URL

import media_reader
import qq_media

ENV_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')
HTTP = 'http://43.129.139.192/download?sign=PRIVATE_SIGNATURE&name=%E7%8C%AB'
HTTPS = HTTP.replace('http:', 'https:', 1)
PROXY = 'http://user:PRIVATE_PASSWORD@proxy.example:8080'


class Context:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        pass


def response(status=200, chunks=(b'video',), headers=None):
    async def iterate(_):
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk
    return SimpleNamespace(status=status, headers=headers or {}, content=SimpleNamespace(iter_chunked=iterate))


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = self.enterContext(patch.dict(os.environ, {key: '' for key in ENV_KEYS}))
        self.logs = []
        self.direct = AsyncMock(return_value=(b'direct', 'video/mp4'))
        self.bot = SimpleNamespace(log=lambda level, text: self.logs.append(text), _download_vision_image=self.direct)
        self.seen, self.requests = [], []

    def network(self, responses):
        def session(**kwargs):
            self.seen.append(kwargs)
            class Session(Context):
                def __init__(inner):
                    super().__init__(inner)
                def get(inner, target, **options):
                    self.requests.append((target, options))
                    item = responses[len(self.requests) - 1]
                    if isinstance(item, BaseException):
                        raise item
                    class Request(Context):
                        async def __aenter__(request):
                            for trace in kwargs['trace_configs']:
                                for callback in trace.on_connection_create_end:
                                    await callback(None, None, None)
                            return item
                    return Request(item)
            return Session()
        return patch.object(qq_media.aiohttp, 'ClientSession', side_effect=session)

    async def test_proxy_first_preserves_signed_ip_url_auth_and_default_tls(self):
        os.environ['HTTP_PROXY'] = PROXY
        with self.network([response(headers={'Content-Length': '5'})]):
            result = await qq_media.download_media(self.bot, HTTP, 100, 5)
        self.assertEqual(result[0], b'video')
        self.direct.assert_not_awaited()
        target, options = self.requests[0]
        self.assertEqual(str(target), HTTP)
        self.assertEqual(str(options['proxy']), 'http://proxy.example:8080')
        self.assertEqual(options['proxy_auth'].password, 'PRIVATE_PASSWORD')
        self.assertNotIn('ssl', options)
        self.assertFalse(options['allow_redirects'])
        self.assertFalse(self.seen[0]['trust_env'])
        self.assertEqual(self.seen[0]['timeout'].sock_read, 8)
        self.assertIsNone(self.seen[0]['timeout'].total)
        self.assertIn('route=proxy', ' '.join(self.logs))
        self.assertNotIn('PRIVATE_', ' '.join(self.logs))
        self.assertNotIn('proxy.example', ' '.join(self.logs))

    async def test_https_ip_uses_https_proxy_and_original_target_without_disabling_verification(self):
        os.environ['HTTPS_PROXY'] = PROXY
        with self.network([response()]):
            await qq_media.download_media(self.bot, HTTPS, 100)
        self.assertEqual(self.requests[0][0].scheme, 'https')
        self.assertNotIn('ssl', self.requests[0][1])
        self.direct.assert_not_awaited()

    async def test_no_environment_proxy_calls_only_original_direct_with_same_arguments(self):
        with patch.object(qq_media.aiohttp, 'ClientSession') as session:
            self.assertEqual(await qq_media.download_media(self.bot, HTTP, 100), (b'direct', 'video/mp4'))
        session.assert_not_called()
        self.direct.assert_awaited_once_with(HTTP, 1, cache=False, max_bytes=100)
        self.assertNotIn('fallback', ' '.join(self.logs))
        self.assertIn('route=direct', ' '.join(self.logs))

    async def test_proxy_http_rejection_falls_back_once_and_logs_category(self):
        os.environ['HTTP_PROXY'] = PROXY
        with self.network([response(status=503)]):
            result = await qq_media.download_media(self.bot, HTTP, 100)
        self.assertEqual(result[0], b'direct')
        self.direct.assert_awaited_once()
        self.assertIn('event=fallback', ' '.join(self.logs))
        self.assertIn('category=http status=503', ' '.join(self.logs))

    async def test_connection_failure_and_body_stall_fall_back_without_exception_text(self):
        for item, category in ((aiohttp.ClientConnectionError('PRIVATE_ERROR'), 'connect'),
                               (response(chunks=(b'vi', asyncio.TimeoutError('PRIVATE_ERROR'))), 'stall')):
            with self.subTest(category=category):
                self.requests.clear()
                self.direct.reset_mock()
                os.environ['HTTP_PROXY'] = PROXY
                with self.network([item]):
                    result = await qq_media.download_media(self.bot, HTTP, 100)
                self.assertEqual(result[0], b'direct')
                self.direct.assert_awaited_once()
                self.assertIn('category=' + category, ' '.join(self.logs))
        self.assertNotIn('PRIVATE_ERROR', ' '.join(self.logs))

    async def test_partial_or_wrong_size_body_is_discarded_before_full_direct_retry(self):
        os.environ['HTTP_PROXY'] = PROXY
        for item in (response(status=206), response(chunks=(b'vi',))):
            self.requests.clear()
            self.direct.reset_mock()
            with self.network([item]):
                result = await qq_media.download_media(self.bot, HTTP, 100, 5)
            self.assertEqual(result[0], b'direct')
            self.direct.assert_awaited_once()

    async def test_over_limit_is_terminal_not_retried_through_direct(self):
        os.environ['HTTP_PROXY'] = PROXY
        for item in (response(headers={'Content-Length': '200'}), response(chunks=(b'a' * 101,))):
            self.requests.clear()
            with self.network([item]), self.assertRaises(media_reader.MediaError):
                await qq_media.download_media(self.bot, HTTP, 100)
        self.direct.assert_not_awaited()

    async def test_redirects_revalidate_public_targets_and_keep_signature(self):
        os.environ['HTTP_PROXY'] = PROXY
        with self.network([response(302, headers={'Location': 'http://127.0.0.1/private'})]), \
             self.assertRaises(media_reader.MediaError):
            await qq_media.download_media(self.bot, HTTP, 100)
        self.direct.assert_not_awaited()
        self.assertEqual(len(self.requests), 1)
        self.requests.clear()
        with self.network([response(302, headers={'Location': '/another?sign=EXACT%2B&x=a%2Fb'}), response()]):
            await qq_media.download_media(self.bot, HTTP, 100)
        self.assertTrue(str(self.requests[-1][0]).endswith('/another?sign=EXACT%2B&x=a%2Fb'))

    async def test_cancellation_never_starts_direct_fallback(self):
        os.environ['HTTP_PROXY'] = PROXY
        with self.network([asyncio.CancelledError()]), self.assertRaises(asyncio.CancelledError):
            await qq_media.download_media(self.bot, HTTP, 100)
        self.direct.assert_not_awaited()

    async def test_shared_deadline_includes_fallback_instead_of_restarting_fifty_five_seconds(self):
        os.environ['HTTP_PROXY'] = PROXY
        async def proxy(*args):
            await asyncio.sleep(0.05)
            raise qq_media._ProxyFailure('stall')
        async def direct(*args, **kwargs):
            await asyncio.sleep(0.5)
            return b'late', 'video/mp4'
        self.direct.side_effect = direct
        with patch.object(qq_media, '_proxy_download', side_effect=proxy), \
             patch.object(qq_media, 'MEDIA_DOWNLOAD_SECONDS', 0.3):
            self.assertIsNone(await qq_media.download_media(self.bot, HTTP, 100))
        self.direct.assert_awaited_once()
        self.assertIn('event=deadline route=direct', ' '.join(self.logs))

    async def test_progressive_body_has_no_short_overall_proxy_timeout(self):
        os.environ['HTTP_PROXY'] = PROXY
        # Progress outlasts an idle interval without ever stalling. No fixed
        # first-byte/whole-body timer should kill an actively flowing transfer.
        async def chunks(_):
            for chunk in (b'v', b'i', b'd', b'e', b'o'):
                await asyncio.sleep(0.006)
                yield chunk
        item = response()
        item.content.iter_chunked = chunks
        with self.network([item]), patch.object(qq_media, 'MEDIA_PROXY_IDLE_SECONDS', 0.01):
            result = await qq_media.download_media(self.bot, HTTP, 100)
        self.assertEqual(result[0], b'video')
        self.direct.assert_not_awaited()

    async def test_no_first_byte_deadline_falls_back_early(self):
        os.environ['HTTP_PROXY'] = PROXY
        async def chunks(_):
            await asyncio.sleep(0.5)
            yield b'late'
        item = response()
        item.content.iter_chunked = chunks
        with self.network([item]), patch.object(qq_media, 'MEDIA_PROXY_START_SECONDS', 0.03):
            result = await qq_media.download_media(self.bot, HTTP, 100)
        self.assertEqual(result[0], b'direct')
        self.direct.assert_awaited_once()
        self.assertIn('category=stall', ' '.join(self.logs))

    async def test_first_byte_deadline_is_removed_while_body_keeps_progressing(self):
        os.environ['HTTP_PROXY'] = PROXY
        async def chunks(_):
            yield b'v'
            await asyncio.sleep(0.06)
            yield b'ideo'
        item = response()
        item.content.iter_chunked = chunks
        with self.network([item]), patch.object(qq_media, 'MEDIA_PROXY_START_SECONDS', 0.03):
            result = await qq_media.download_media(self.bot, HTTP, 100)
        self.assertEqual(result[0], b'video')
        self.direct.assert_not_awaited()

    def test_scheme_preference_lowercase_fallback_and_no_socks(self):
        os.environ['http_proxy'] = 'http://http-proxy.example:80'
        os.environ['https_proxy'] = 'http://https-proxy.example:80'
        self.assertEqual(qq_media._environment_proxy(HTTP)[0].host, 'http-proxy.example')
        self.assertEqual(qq_media._environment_proxy(HTTPS)[0].host, 'https-proxy.example')
        os.environ['HTTP_PROXY'] = 'socks5://user:PRIVATE_PASSWORD@bad.example:1080'
        with self.assertRaises(qq_media._ProxyFailure):
            qq_media._environment_proxy(HTTP)

    async def test_invalid_proxy_config_falls_back_without_secrets(self):
        os.environ['HTTP_PROXY'] = 'socks5://user:PRIVATE_PASSWORD@bad.example:1080'
        self.assertEqual((await qq_media.download_media(self.bot, HTTP, 100))[0], b'direct')
        self.assertIn('category=proxy_config', ' '.join(self.logs))
        self.assertNotIn('PRIVATE_PASSWORD', ' '.join(self.logs))

    async def test_controller_group_and_private_downloads_use_proxy_route(self):
        os.environ['HTTP_PROXY'] = PROXY
        for data, info in (({'group_id': 123, 'sender': {'user_id': 456}}, {'name': 'clip.mp4', 'id': 'group-file', 'size': 5}),
                           ({'sender': {'user_id': 456}}, {'name': 'clip.mp4', 'file_id': 'private-file', 'file_hash': 'hash', 'size': 5})):
            self.requests.clear()
            self.bot.call_api = AsyncMock(return_value={'url': HTTP})
            with self.network([response()]):
                result = await qq_media.MediaController(self.bot).download(data, info)
            self.assertEqual(result, b'video')
        self.direct.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
