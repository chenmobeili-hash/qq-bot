import asyncio
from contextlib import asynccontextmanager
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import bot


class Context:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *_args):
        return False


def response(status=200, chunks=(b'fixture',), headers=None):
    async def iterate(_size):
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk
    return SimpleNamespace(status=status, headers=headers or {}, content=SimpleNamespace(iter_chunked=iterate))


class ImageDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        self.log = patch.object(bot, 'log', side_effect=lambda level, message: self.logs.append(message))
        self.log.start()
        self.addCleanup(self.log.stop)
        cache_patch = patch.object(bot, '_get_vision_image_cache', return_value=SimpleNamespace(get=lambda url: None, put=lambda *args: None))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def sessions(self, responses):
        seen = []
        def factory(**kwargs):
            item = responses[len(seen)]
            seen.append(kwargs)
            class Session(Context):
                def __init__(self):
                    super().__init__(self)
                def get(self, *_args, **options):
                    kwargs['request_timeout'] = options['timeout']
                    kwargs.setdefault('requests', []).append(str(_args[0]))
                    if isinstance(item, BaseException):
                        raise item
                    return Context(item)
                async def __aexit__(self, *_args):
                    await kwargs['connector'].close()
            return Session()
        return factory, seen

    async def test_fast_download_keeps_type_and_55_second_budget(self):
        factory, sessions = self.sessions([response(headers={'Content-Type': 'image/png'})])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory):
            result = await bot._download_vision_image_direct('https://example.com/image?token=SECRET', 1)
        self.assertEqual(result, (b'fixture', 'image/png'))
        self.assertEqual(len(sessions), 1)
        timeout = sessions[0]['request_timeout']
        self.assertEqual((timeout.total, timeout.connect, timeout.sock_connect, timeout.sock_read), (55, 20, 20, 20))
        self.assertTrue(sessions[0]['connector'].force_close)
        self.assertIn('event=download_complete', '\n'.join(self.logs))
        self.assertNotIn('SECRET', '\n'.join(self.logs))

    async def test_payload_failure_retries_with_fresh_connector_and_body(self):
        factory, sessions = self.sessions([response(chunks=(b'old', aiohttp.ClientPayloadError('SECRET'))),
                                           response(chunks=(b'new', b' body'))])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory), patch.object(bot.asyncio, 'sleep', AsyncMock()):
            result = await bot._download_vision_image_direct('https://example.com/SECRET', 2)
        self.assertEqual(result, (b'new body', 'image/jpeg'))
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0]['connector'], sessions[1]['connector'])
        logs = '\n'.join(self.logs)
        self.assertIn('event=temporary_failure', logs)
        self.assertIn('bytes=3', logs)
        self.assertIn('attempt=2', logs)
        self.assertNotIn('SECRET', logs)

    async def test_retry_uses_second_rank_and_connection_logs_keep_selected_ip(self):
        real_session = aiohttp.ClientSession
        observed = []
        fixture = response(headers={'Content-Type': 'image/png'})
        def factory(**kwargs):
            if 'connector' not in kwargs:
                return real_session(**kwargs)  # Real, idle DoH session; its query is mocked below.
            connector = kwargs['connector']
            class Session(Context):
                def __init__(self):
                    super().__init__(self)
                def get(self, url, **options):
                    class RequestContext(Context):
                        async def __aenter__(self):
                            for callback in kwargs['trace_configs'][0].on_connection_create_start:
                                await callback()
                            rows = await connector._resolver.resolve(url.host, url.port)
                            observed.append([row['host'] for row in rows])
                            if len(observed) == 1:
                                raise aiohttp.ClientConnectionError('SECRET')
                            for callback in kwargs['trace_configs'][0].on_connection_create_end:
                                await callback()
                            return fixture
                    return RequestContext(fixture)
                async def __aexit__(self, *_args):
                    await connector.close()
            return Session()
        payload = {'Status': 0, 'Answer': [{'type': 1, 'data': ip} for ip in ('8.8.8.8', '1.1.1.20', '1.1.1.3')]}
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory), \
             patch.object(bot.ImageDoHResolver, '_query', AsyncMock(return_value=payload)), \
             patch.object(bot.asyncio, 'sleep', AsyncMock()):
            result = await bot._download_vision_image_direct('https://images.example/x?sig=SECRET', 1)
        self.assertEqual(result, (b'fixture', 'image/png'))
        self.assertEqual(observed, [['1.1.1.3'], ['1.1.1.20']])
        first_failure = next(line for line in self.logs if 'event=temporary_failure' in line)
        connected = next(line for line in self.logs if 'event=connection_ready' in line)
        self.assertIn('attempt=1', first_failure)
        self.assertIn('dialed=1.1.1.3', first_failure)
        self.assertIn('attempt=2', connected)
        self.assertIn('dialed=1.1.1.20', connected)
        self.assertNotIn('SECRET', '\n'.join(self.logs))

    async def test_redirect_to_private_target_is_rejected_before_second_request(self):
        factory, sessions = self.sessions([response(status=302, headers={'Location': 'http://127.0.0.1/SECRET'})])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory):
            self.assertIsNone(await bot._download_vision_image_direct('https://images.example/x', 1))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]['requests']), 1)
        self.assertIn('event=permanent_failure', '\n'.join(self.logs))
        self.assertNotIn('SECRET', '\n'.join(self.logs))

    async def test_public_ip_literal_is_logged_without_dns(self):
        factory, sessions = self.sessions([response()])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory):
            self.assertIsNotNone(await bot._download_vision_image_direct('https://8.8.8.8/x', 1))
        self.assertIn('source=literal', '\n'.join(self.logs))
        self.assertIn('dialed=8.8.8.8', '\n'.join(self.logs))

    async def test_non_200_still_skips_without_retry_even_for_503(self):
        for status in (403, 404, 429, 503):
            factory, sessions = self.sessions([response(status=status)])
            with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory), patch.object(bot.asyncio, 'sleep', AsyncMock()) as sleep:
                self.assertIsNone(await bot._download_vision_image_direct('https://example.com/SECRET', 1))
            self.assertEqual(len(sessions), 1)
            sleep.assert_not_awaited()
        self.assertNotIn('SECRET', '\n'.join(self.logs))

    async def test_certificate_error_never_retries(self):
        factory, sessions = self.sessions([aiohttp.ClientSSLError(None, OSError('SECRET'))])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory), patch.object(bot.asyncio, 'sleep', AsyncMock()) as sleep:
            self.assertIsNone(await bot._download_vision_image_direct('https://example.com/SECRET', 1))
        self.assertEqual(len(sessions), 1)
        sleep.assert_not_awaited()
        self.assertIn('event=permanent_failure', '\n'.join(self.logs))
        self.assertNotIn('SECRET', '\n'.join(self.logs))

    async def test_external_cancellation_is_not_retried(self):
        factory, sessions = self.sessions([asyncio.CancelledError()])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory):
            with self.assertRaises(asyncio.CancelledError):
                await bot._download_vision_image_direct('https://example.com/fixture', 1)
        self.assertEqual(len(sessions), 1)
        self.assertIn('event=attempt_cancelled', '\n'.join(self.logs))

    async def test_download_budget_caps_slow_progress_across_the_whole_operation(self):
        async def slow_chunks(_size):
            while True:
                yield b'x'
                await asyncio.sleep(0.02)
        item = response()
        item.content.iter_chunked = slow_chunks
        factory, sessions = self.sessions([item])
        with patch.object(bot.aiohttp, 'ClientSession', side_effect=factory), patch.object(bot, 'VISION_DOWNLOAD_TIMEOUT', 0.08):
            self.assertIsNone(await bot._download_vision_image_direct('https://example.com/fixture', 1))
        self.assertEqual(len(sessions), 1)
        self.assertIn('event=overall_timeout', '\n'.join(self.logs))

    @asynccontextmanager
    async def stalled_server(self, *, always=False, send_headers=True, partial=b'old'):
        release = asyncio.Event()
        count = []
        handlers = set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            index = len(count)
            count.append(index)
            try:
                await reader.readuntil(b'\r\n\r\n')
                if index == 0 or always:
                    if send_headers:
                        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n' + partial)
                        await writer.drain()
                    await release.wait()
                else:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nfresh')
                    await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                handlers.discard(task)
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            yield f'http://127.0.0.1:{port}/fixture', count
        finally:
            server.close()
            release.set()
            if handlers:
                await asyncio.gather(*list(handlers))
            await server.wait_closed()

    async def test_real_body_stall_times_out_then_second_connection_succeeds(self):
        async with self.stalled_server() as (url, count), \
                _timeouts():
            result = await bot._download_vision_image_direct(url, 1)
        self.assertEqual(result, (b'fresh', 'image/jpeg'))
        self.assertEqual(len(count), 2)
        failure = next(line for line in self.logs if 'event=temporary_failure' in line)
        self.assertIn('phase=body', failure)
        self.assertIn('bytes=3', failure)
        self.assertIn('attempt=2', '\n'.join(self.logs))

    async def test_two_real_zero_byte_stalls_finish_well_before_two_total_timeouts(self):
        started = time.monotonic()
        async with self.stalled_server(always=True, partial=b'') as (url, count), _timeouts():
            result = await bot._download_vision_image_direct(url, 1)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertEqual(len(count), 2)
        failures = [line for line in self.logs if 'event=temporary_failure' in line]
        self.assertEqual(len(failures), 2)
        self.assertTrue(all('bytes=0' in line and 'phase=body' in line for line in failures))
        self.assertLess(elapsed, 1.2)  # Two total budgets would be four seconds with this fixture.

    async def test_real_header_stall_is_identified_and_retried(self):
        async with self.stalled_server(send_headers=False) as (url, count), _timeouts():
            self.assertIsNotNone(await bot._download_vision_image_direct(url, 1))
        self.assertEqual(len(count), 2)
        failure = next(line for line in self.logs if 'event=temporary_failure' in line)
        self.assertIn('phase=response_headers', failure)
        self.assertIn('bytes=0', failure)


@asynccontextmanager
async def _timeouts():
    with patch.object(bot, 'validate_image_url', side_effect=lambda value: value), \
         patch.object(bot, 'VISION_DOWNLOAD_READ_TIMEOUT', 0.08), \
         patch.object(bot, 'VISION_DOWNLOAD_CONNECT_TIMEOUT', 0.3), \
         patch.object(bot, 'VISION_DOWNLOAD_TIMEOUT', 2.0), \
         patch.object(bot, 'VISION_DOWNLOAD_RETRY_DELAY', 0.01):
        yield


class VisionPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='固定测试描述'), finish_reason='stop')]))
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.config = {'qwen_vl': {'enabled': True, 'model': 'fixture'}}
        self.patches = [patch.object(bot, 'load_config', return_value=self.config),
            patch.object(bot, '_effective_vision_config', return_value=self.config['qwen_vl']),
            patch.object(bot, '_ensure_qwen_vl_client', AsyncMock(return_value=True)),
            patch.object(bot, 'qwen_vl_client', self.client), patch.object(bot, '_vision_route_signature', return_value='fixture'),
            patch.object(bot, '_saucenao_search_only', AsyncMock(return_value=None)),
            patch.object(bot, '_saucenao_compose', AsyncMock(return_value=None)), patch.object(bot, 'log')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_three_image_cap_and_skipped_download_preserve_numbering(self):
        with patch.object(bot, '_download_vision_image', AsyncMock(side_effect=[None, (b'two', 'image/png'), (b'three', 'image/png')])) as fetch:
            result = await bot.describe_images(['first', 'second', 'third', 'fourth'])
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(self.create.await_count, 2)
        self.assertNotIn('【图1】', result)
        self.assertIn('【图2】', result)
        self.assertIn('【图3】', result)

    async def test_download_failure_never_invokes_model(self):
        with patch.object(bot, '_download_vision_image', AsyncMock(return_value=None)):
            self.assertIsNone(await bot.describe_images(['fixture']))
        self.create.assert_not_awaited()
        bot._saucenao_search_only.assert_not_awaited()

    async def test_model_keeps_three_attempts_independent_of_download(self):
        self.create.side_effect = [asyncio.TimeoutError(), asyncio.TimeoutError(), SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='第三次模型成功'), finish_reason='stop')])]
        with patch.object(bot, '_download_vision_image', AsyncMock(return_value=(b'fixture', 'image/jpeg'))) as fetch, \
             patch.object(bot.asyncio, 'sleep', AsyncMock()):
            result = await bot.describe_images(['fixture'])
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual(fetch.await_count, 1)
        self.assertIn('第三次模型成功', result)
        self.assertEqual(bot.VISION_TIMEOUT, 180)
        self.assertEqual(bot.VISION_RETRIES, 3)

    async def test_sauce_and_model_still_run_concurrently(self):
        sauce_started = asyncio.Event()
        model_started = asyncio.Event()
        async def sauce(*_args):
            sauce_started.set()
            await model_started.wait()
            return None
        async def model(**_kwargs):
            model_started.set()
            await asyncio.wait_for(sauce_started.wait(), 0.5)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='并发成功'), finish_reason='stop')])
        self.create.side_effect = model
        with patch.object(bot, '_download_vision_image', AsyncMock(return_value=(b'fixture', 'image/jpeg'))), \
             patch.object(bot, '_saucenao_search_only', side_effect=sauce):
            result = await bot.describe_images(['fixture'])
        self.assertIn('并发成功', result)


if __name__ == '__main__':
    unittest.main()
