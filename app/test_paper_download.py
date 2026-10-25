import ast
import asyncio
import hashlib
import json
from pathlib import Path
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import bot
import paper_download as download
import paper_reader as paper
from apply_download_update import DELEGATE, fingerprint, patch_source


class FakeResponse:
    def __init__(self, status=200, chunks=(b'complete',), headers=None, length=None):
        self.status, self.headers, self.content_length = status, headers or {}, length
        async def chunks_iter(_size):
            for chunk in chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk
        self.content = SimpleNamespace(iter_chunked=chunks_iter)


class Context:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        return False


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        self.log_patch = patch.object(download, '_logger', lambda level, message: self.logs.append(message))
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.resolver = SimpleNamespace(resolve=AsyncMock(return_value=[{'host': '8.8.8.8'}]), close=AsyncMock())
        self.args = dict(resolver_factory=lambda: self.resolver, validate_url=paper.public_url,
                         error=paper.PaperError, max_bytes=paper.MAX_BYTES)

    async def fetch(self, url='https://example.com/book?token=SECRET'):
        return await download.fetch_document(url, **self.args)

    def sessions(self, responses, *, resolve_host=None, connected_rows=None):
        seen = []
        def factory(**kwargs):
            response = responses[len(seen)]
            seen.append(kwargs)
            class Session(Context):
                def __init__(self):
                    super().__init__(self)
                def get(self, *args, **request_kwargs):
                    if isinstance(response, Exception):
                        raise response
                    if resolve_host:
                        class ResolvingContext(Context):
                            async def __aenter__(self):
                                trace = kwargs['trace_configs'][0]
                                for callback in trace.on_connection_create_start:
                                    await callback(None, None, None)
                                rows = await kwargs['connector']._resolver.resolve(resolve_host, 443, 2)
                                if connected_rows is not None:
                                    connected_rows.extend(rows)
                                for callback in trace.on_connection_create_end:
                                    await callback(None, None, None)
                                return response
                        return ResolvingContext(response)
                    return Context(response)
                async def __aexit__(self, *args):
                    await kwargs['connector'].close()
            return Session()
        return factory, seen

    async def test_partial_download_retry_restarts_from_zero_and_honors_larger_connect(self):
        factory, calls = self.sessions([FakeResponse(chunks=(b'partial', aiohttp.ClientPayloadError('SECRET'))),
                                        FakeResponse(chunks=(b'fresh', b' data'))])
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory), patch.object(download.asyncio, 'sleep', AsyncMock()) as sleep:
            body, _, _ = await self.fetch()
        self.assertEqual(body, b'fresh data')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['timeout'].connect, 30)
        self.assertEqual(calls[0]['timeout'].total, 60)
        self.assertTrue(calls[0]['connector']._ssl.check_hostname)
        self.assertEqual(calls[0]['connector']._ssl.verify_mode, ssl.CERT_REQUIRED)
        sleep.assert_awaited_once_with(1.0)
        logs = '\n'.join(self.logs)
        self.assertIn('attempt=1 event=temporary_failure', logs)
        self.assertIn('bytes=7', logs)
        self.assertIn('attempt=2', logs)
        self.assertNotIn('SECRET', logs)
        self.assertNotIn('example.com', logs)

    async def test_two_connection_timeouts_stop_after_one_retry(self):
        with patch.object(download, '_one', AsyncMock(side_effect=asyncio.TimeoutError('SECRET'))) as one, patch.object(download.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(paper.PaperError, '已尝试2次') as error:
                await self.fetch()
        self.assertEqual(one.await_count, 2)
        self.assertNotIn('SECRET', str(error.exception) + '\n'.join(self.logs))

    async def test_certificate_failure_never_retries(self):
        with patch.object(download, '_one', AsyncMock(side_effect=ssl.SSLCertVerificationError('SECRET'))) as one:
            with self.assertRaisesRegex(paper.PaperError, '校验失败'):
                await self.fetch()
        self.assertEqual(one.await_count, 1)

    async def test_404_and_oversized_file_never_retry(self):
        for response in (FakeResponse(status=404), FakeResponse(length=paper.MAX_BYTES + 1),
                         FakeResponse(chunks=(b'x' * 9,))):
            factory, calls = self.sessions([response])
            with patch.object(download.aiohttp, 'ClientSession', side_effect=factory):
                if response.status == 200 and response.content_length is None:
                    self.args['max_bytes'] = 8
                with self.assertRaises(paper.PaperError):
                    await self.fetch()
            self.assertEqual(len(calls), 1)

    async def test_temporary_http_status_retries_but_permanent_412_does_not(self):
        for status, expected in ((503, 2), (429, 2), (408, 2), (412, 1)):
            factory, calls = self.sessions([FakeResponse(status=status), FakeResponse()])
            with patch.object(download.aiohttp, 'ClientSession', side_effect=factory), patch.object(download.asyncio, 'sleep', AsyncMock()):
                if expected == 2:
                    await self.fetch()
                else:
                    with self.assertRaisesRegex(paper.PaperError, '412'):
                        await self.fetch()
            self.assertEqual(len(calls), expected)

    async def test_retry_after_is_respected_or_not_retried_if_beyond_budget(self):
        factory, calls = self.sessions([FakeResponse(status=429, headers={'Retry-After': '5'}), FakeResponse()])
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory), patch.object(download.asyncio, 'sleep', AsyncMock()) as sleep:
            await self.fetch()
            sleep.assert_awaited_once_with(5.0)
        factory, calls = self.sessions([FakeResponse(status=429, headers={'Retry-After': '999'})])
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory), patch.object(download.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(paper.PaperError):
                await self.fetch()
            sleep.assert_not_awaited()
            self.assertEqual(len(calls), 1)

    async def test_cancelled_download_never_retries(self):
        with patch.object(download, '_one', AsyncMock(side_effect=asyncio.CancelledError)) as one:
            with self.assertRaises(asyncio.CancelledError):
                await self.fetch()
        self.assertEqual(one.await_count, 1)

    async def test_existing_fetch_delegates_to_target_public_resolver(self):
        with patch.object(download, 'fetch_document', AsyncMock(return_value=(b'test', '', ''))) as fetch:
            await paper.fetch_bytes('https://example.com/book')
        self.assertIs(fetch.call_args.kwargs['resolver_factory'], paper.PublicResolver)
        self.assertIs(fetch.call_args.kwargs['validate_url'], paper.public_url)

    async def test_failed_group_download_does_not_call_model_or_mark_read(self):
        reader = SimpleNamespace(storage_ok=True, mark_reviewed=Mock())
        with patch.object(bot, '_get_paper_library', return_value=paper.PaperLibrary()), \
             patch.object(bot, '_get_paper_autonomy', return_value=reader), \
             patch.object(bot, 'call_api', AsyncMock(return_value={'url': 'https://example.com/book?token=SECRET'})), \
             patch.object(download, '_one', AsyncMock(side_effect=asyncio.TimeoutError)), \
             patch.object(download.asyncio, 'sleep', AsyncMock()), \
             patch.object(bot, '_generate_paper', AsyncMock()) as model, \
             patch.object(bot, 'send_group_message', AsyncMock(return_value=True)), \
             patch.object(bot, 'log'):
            await bot._run_paper_read(123, '456', '', {}, selected_info={'name': 'book.md', 'id': 'fixture'})
        model.assert_not_awaited()
        reader.mark_reviewed.assert_not_called()

    async def test_total_deadline_is_real_and_cancel_does_not_become_retry(self):
        async def block(*args):
            await asyncio.sleep(10)
        with patch.object(download, '_one', side_effect=block) as one, patch.object(download, 'DOWNLOAD_SECONDS', 0.02):
            with self.assertRaisesRegex(paper.PaperError, '总期限'):
                await self.fetch()
        self.assertEqual(one.call_count, 1)

    async def test_private_redirect_is_rejected_before_next_request(self):
        factory, calls = self.sessions([FakeResponse(status=302, headers={'Location': 'http://127.0.0.1/SECRET'})])
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory):
            with self.assertRaisesRegex(paper.PaperError, '公网'):
                await self.fetch()
        self.assertEqual(len(calls), 1)

    async def test_vm_resolver_is_called_and_its_doh_timing_hook_is_available(self):
        async def resolve(*args):
            with download.resolver_phase('system_dns'):
                pass
            with download.resolver_phase('doh'):
                pass
            with download.resolver_phase('address_validation'):
                pass
            return [{'host': '8.8.8.8'}]
        self.resolver.resolve.side_effect = resolve
        attempt = download._Attempt('fixture', 1)
        token = download._active_attempt.set(attempt)
        try:
            timed = download.TimedResolver(self.resolver, paper.PaperError, attempt)
            rows = await timed.resolve('example.com', 443, 2)
            await timed.close()
        finally:
            download._active_attempt.reset(token)
        self.assertEqual(rows, [{'host': '8.8.8.8'}])
        self.resolver.resolve.assert_awaited_once_with('example.com', 443, 2)
        self.assertIn('resolver_doh_end', '\n'.join(self.logs))

    async def test_delegate_rejection_is_propagated_unchanged(self):
        original = paper.PaperError('真实域名地址非公网，DoH校验拒绝')
        self.resolver.resolve.side_effect = original
        timed = download.TimedResolver(self.resolver, paper.PaperError, download._Attempt('fixture', 1))
        with self.assertRaises(paper.PaperError) as raised:
            await timed.resolve('example.com', 443, 2)
        self.assertIs(raised.exception, original)

    async def test_validated_platform_mapping_rows_are_returned_unchanged(self):
        rows = [{'hostname': 'mapped.example', 'host': '198.18.59.148', 'port': 443,
                 'family': 2, 'proto': 6, 'flags': 0}]
        # Stand-in for the VM delegate: validation is independent of the mapped rows.
        validated = []
        async def resolve(host, port, family):
            validated.append(host)
            with download.resolver_phase('doh'):
                public_answers = ['8.8.8.8']
                self.assertTrue(public_answers)
            return rows
        self.resolver.resolve.side_effect = resolve
        timed = download.TimedResolver(self.resolver, paper.PaperError, download._Attempt('fixture', 1))
        result = await timed.resolve('mapped.example', 443, 2)
        self.assertIs(result, rows)
        self.assertEqual(validated, ['mapped.example'])
        self.assertIn('event=resolver_end', '\n'.join(self.logs))

    async def test_validated_mapping_reaches_connection_response_and_body(self):
        rows = [{'hostname': 'mapped.example', 'host': '198.18.59.147', 'port': 443,
                 'family': 2, 'proto': 6, 'flags': 0}]
        self.resolver.resolve.return_value = rows
        connected_rows = []
        factory, calls = self.sessions([FakeResponse(chunks=(b'valid mapped body',))],
            resolve_host='mapped.example', connected_rows=connected_rows)
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory), patch.object(download.asyncio, 'sleep', AsyncMock()) as sleep:
            body, _, _ = await self.fetch('https://mapped.example/book?token=SECRET')
        self.assertEqual(body, b'valid mapped body')
        self.assertEqual(connected_rows, rows)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()
        logs = '\n'.join(self.logs)
        events = ('resolver_start', 'resolver_end', 'connection_ready', 'response_headers',
                  'first_body_chunk', 'download_complete')
        positions = [logs.index('event=' + event) for event in events]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('SECRET', logs)

    async def test_delegate_private_name_rejection_is_logged_once_without_retry(self):
        original = paper.PaperError('真实域名地址非公网，DoH校验拒绝')
        self.resolver.resolve.side_effect = original
        factory, calls = self.sessions([FakeResponse()], resolve_host='rejected.example')
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory), patch.object(download.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(paper.PaperError) as raised:
                await self.fetch('https://rejected.example/book')
        self.assertIs(raised.exception, original)
        self.assertEqual(len(calls), 1)
        self.resolver.resolve.assert_awaited_once()
        sleep.assert_not_awaited()
        logs = '\n'.join(self.logs)
        self.assertIn('event=rejected', logs)
        self.assertNotIn('event=resolver_end', logs)
        self.assertNotIn('event=response_headers', logs)
        self.assertNotIn('event=retry_scheduled', logs)

    async def test_actual_public_resolver_still_rejects_private_dns(self):
        loop = asyncio.get_running_loop()
        self.args['resolver_factory'] = paper.PublicResolver
        factory, calls = self.sessions([FakeResponse()], resolve_host='rejected.example')
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[(2, 1, 6, '', ('10.1.2.3', 443))])), \
             patch.object(download.aiohttp, 'ClientSession', side_effect=factory), \
             patch.object(download.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaisesRegex(paper.PaperError, '非公网'):
                await self.fetch('https://rejected.example/book')
        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()

    async def test_empty_delegate_result_still_stops_before_connection(self):
        self.resolver.resolve.return_value = []
        factory, calls = self.sessions([FakeResponse()], resolve_host='empty.example')
        with patch.object(download.aiohttp, 'ClientSession', side_effect=factory):
            with self.assertRaisesRegex(paper.PaperError, '未返回连接地址'):
                await self.fetch('https://empty.example/book')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('event=connection_ready', '\n'.join(self.logs))

    async def test_real_local_http_stream_has_header_first_chunk_byte_logs(self):
        # Local-only fixture: the production wrapper always uses public_url. No public network access.
        async def server(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nfixture!')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        service = await asyncio.start_server(server, '127.0.0.1', 0)
        try:
            port = service.sockets[0].getsockname()[1]
            self.args['validate_url'] = lambda value: value
            body, _, _ = await self.fetch(f'http://127.0.0.1:{port}/private-fixture?token=SECRET')
        finally:
            service.close()
            await service.wait_closed()
        self.assertEqual(body, b'fixture!')
        logs = '\n'.join(self.logs)
        for event in ('connection_ready', 'request_headers_sent', 'response_headers', 'first_body_chunk', 'download_complete'):
            self.assertIn('event=' + event, logs)
        self.assertIn('bytes=8', logs)
        self.assertNotIn('SECRET', logs)


class PatchTests(unittest.TestCase):
    def test_target_doh_code_preserved_byte_for_byte_and_repeated_patch_is_noop(self):
        source = '''# keep this comment\nclass PublicResolver:\n    async def resolve(self):\n        return await DOH_PROXY_PRIVATE_HELPER()\nasync def fetch_bytes(url):\n    return await old_download(url)\n# retain every following byte\nVALUE = 42\n'''
        original = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef))
        result = patch_source(source, [fingerprint(original)])
        self.assertTrue(result.startswith(source.split('async def fetch_bytes')[0]))
        self.assertIn('# retain every following byte\nVALUE = 42\n', result)
        self.assertEqual(patch_source(result, []), result)
        self.assertIn('resolver_factory=PublicResolver', result)

    def test_unknown_custom_fetch_is_rejected_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'custom logic'):
            patch_source('async def fetch_bytes(url):\n    return await vm_proxy(url)\n', [])

    def test_every_outer_budget_has_room_for_inner_budget(self):
        self.assertGreater(download.DOWNLOAD_STAGE_SECONDS, download.DOWNLOAD_SECONDS)
        self.assertGreater(download.DOWNLOAD_SECONDS, download.ATTEMPT_SECONDS * 2 + 1)
        self.assertGreater(download.GROUP_STAGE_SECONDS, 35 + download.DOWNLOAD_STAGE_SECONDS + 125)
        tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8-sig'))
        expected = {'下载群文件': 130, '下载并解析群文档': 300, '读取论文链接': 300}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_paper_stage'
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in expected):
                timeout = next(k.value.value for k in node.keywords if k.arg == 'timeout')
                self.assertEqual(timeout, expected[node.args[0].value])


if __name__ == '__main__':
    unittest.main()
