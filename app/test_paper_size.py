import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import paper_download
import paper_reader
from reading_sessions import ReadingSessions


class PaperSizeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.large = b'research material with enough real plain text.\n' * 300000
        assert 12 * 1024 * 1024 < len(cls.large) < 64 * 1024 * 1024

    def test_large_text_extracts_with_same_bounded_model_context(self):
        doc = paper_reader.extract_document(self.large, 'large.md')
        context = paper_reader.build_context(doc, '研究内容是什么？')
        self.assertEqual(paper_reader.MAX_BYTES, 64 * 1024 * 1024)
        self.assertTrue(doc['units'])
        self.assertLessEqual(sum(len(u['text']) for u in doc['units']), paper_reader.MAX_CHARS)
        self.assertLessEqual(sum(len(block['text']) for block in context['excerpts']), paper_reader.CONTEXT_CHARS)
        self.assertTrue(doc['truncated'])

    async def test_actual_worker_accepts_over_twelve_mb_without_extending_parser_deadline(self):
        doc = await paper_reader.extract_in_worker(self.large, 'large.md', '测试文件')
        self.assertTrue(doc['units'])
        self.assertLessEqual(sum(len(u['text']) for u in doc['units']), paper_reader.MAX_CHARS)

    def test_parser_and_pdf_cache_reject_over_new_limit_without_allocating_large_input(self):
        class Oversized(bytes):
            def __len__(self):
                return paper_reader.MAX_BYTES + 1
        body = Oversized(b'%PDF-1.7\nplaceholder')
        with self.assertRaisesRegex(paper_reader.PaperError, '64 MB'):
            paper_reader.extract_document(body, 'oversized.pdf')
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(ReadingSessions(Path(directory), Mock()).cache_pdf(body))

    def test_large_pdf_source_cache_can_register_and_restore_saved_progress(self):
        body = b'%PDF-1.7\n' + self.large
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ReadingSessions(root, Mock())
            digest = store.cache_pdf(body)
            self.assertEqual(digest, hashlib.sha256(body).hexdigest())
            doc = {'_reading_sha': digest, 'title': 'large.pdf', 'total': 120,
                   'units': [{'label': '第100页', 'text': '已读取这一页的文字'}]}
            job = store.start('group_123_456', doc, '读后感')
            self.assertEqual(job['next_sample'], 101)
            restored = ReadingSessions(root, Mock())
            saved = restored.current('group_123_456')
            self.assertEqual(saved['next_sample'], 101)
            self.assertEqual(restored.source(saved).read_bytes(), body)

    async def test_group_candidates_include_large_book_and_still_exclude_over_limit(self):
        files = [{'file_id': 'large', 'name': '大书.pdf', 'size': len(self.large)},
                 {'file_id': 'too-large', 'name': '超限.pdf', 'size': paper_reader.MAX_BYTES + 1}]
        with patch.object(bot, '_get_paper_library', return_value=SimpleNamespace(group_files=lambda group: [])), \
             patch.object(bot, 'call_api', AsyncMock(return_value={'files': files})):
            candidates = await bot._paper_candidates(123)
        self.assertEqual([r['file_id'] for r in candidates], ['large'])

    async def test_group_size_precheck_allows_large_metadata_and_keeps_download_length_validation(self):
        doc = {'units': [{'label': '段1', 'text': '正文'}], 'total': 1}
        with patch.object(bot, 'call_api', AsyncMock(return_value={'url': 'https://example.com/file'})) as api, \
             patch.object(paper_reader, 'fetch_bytes', AsyncMock(return_value=(self.large, 'text/plain', 'file'))) as fetch, \
             patch.object(paper_reader, 'extract_in_worker', AsyncMock(return_value=doc)), patch.object(bot, 'log'):
            result = await bot._download_group_paper(123, {'file_id': 'book', 'name': 'large.md', 'size': len(self.large)})
            self.assertIn('_content_sha256', result)
            api.assert_awaited_once()
            fetch.assert_awaited_once()
            with self.assertRaisesRegex(paper_reader.PaperError, '64 MB'):
                await bot._download_group_paper(123, {'file_id': 'book', 'name': 'large.md', 'size': paper_reader.MAX_BYTES + 1})
            self.assertEqual(api.await_count, 1)

    async def test_stream_download_accepts_large_body_and_error_uses_actual_limit(self):
        async def chunks(_):
            yield self.large
        response = SimpleNamespace(status=200, headers={}, content_length=len(self.large),
                                   content=SimpleNamespace(iter_chunked=chunks))
        class Context:
            async def __aenter__(inner):
                return response
            async def __aexit__(inner, *args):
                pass
        def session(**kwargs):
            class Session(Context):
                async def __aenter__(inner):
                    return inner
                async def __aexit__(inner, *args):
                    await kwargs['connector'].close()
                def get(inner, *args, **options):
                    return Context()
            return Session()
        resolver = SimpleNamespace(close=AsyncMock())
        with patch.object(paper_download.aiohttp, 'ClientSession', side_effect=session), \
             patch.object(paper_download, '_logger', Mock()):
            result = await paper_download.fetch_document('https://example.com/file', resolver_factory=lambda: resolver,
                validate_url=paper_reader.public_url, error=paper_reader.PaperError, max_bytes=paper_reader.MAX_BYTES)
            self.assertEqual(result[0], self.large)
            response.content_length = paper_reader.MAX_BYTES + 1
            with self.assertRaisesRegex(paper_reader.PaperError, '64 MB'):
                await paper_download.fetch_document('https://example.com/file', resolver_factory=lambda: resolver,
                    validate_url=paper_reader.public_url, error=paper_reader.PaperError, max_bytes=paper_reader.MAX_BYTES)

    def test_network_and_model_time_budgets_unchanged(self):
        self.assertEqual(paper_download.DOWNLOAD_SECONDS, 125)
        self.assertEqual(bot.reading_bridge.READING_MODEL_TIMEOUT, 300)
        self.assertEqual(paper_reader.MAX_PAGES, 100)
        self.assertEqual(paper_reader.CONTEXT_CHARS, 24000)


if __name__ == '__main__':
    unittest.main()
