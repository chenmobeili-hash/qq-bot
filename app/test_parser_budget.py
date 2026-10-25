"""Parser budget, bounded extraction, interruption and saved-cursor regression tests."""
import ast
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import paper_download
import paper_reader
import reading_pages
from reading_sessions import ReadingSessions, PriorityGate


class ParserBudgetTests(unittest.TestCase):
    def test_linux_children_keep_memory_bound_and_allow_90_cpu_seconds(self):
        for module in (paper_reader, reading_pages):
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8-sig'))
            limits = {n.args[0].attr: ast.unparse(n.args[1]) for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == 'setrlimit'}
            self.assertEqual(limits['RLIMIT_CPU'], '(90, 90)')
            self.assertIn('768 * 1024 * 1024', limits['RLIMIT_AS'])

    def test_outer_deadlines_cover_parser_and_all_group_entry_points(self):
        stages = {}
        tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8-sig'))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_paper_stage'
                    and n.args and isinstance(n.args[0], ast.Constant)):
                for kw in n.keywords:
                    if kw.arg == 'timeout' and isinstance(kw.value, ast.Constant):
                        stages.setdefault(n.args[0].value, []).append(kw.value.value)
        self.assertEqual(stages['提取文档文字'], [125])
        self.assertEqual(stages['下载并解析群文档'], [300, 300, 300])
        self.assertEqual(stages['读取论文链接'], [300])
        self.assertEqual(paper_download.GROUP_STAGE_SECONDS, 300)
        self.assertGreater(300, 35 + paper_download.DOWNLOAD_STAGE_SECONDS + 125)


class ParserWorkerTests(unittest.IsolatedAsyncioTestCase):
    def process(self, result, delay=0):
        process = SimpleNamespace(returncode=None, kill=Mock(), wait=AsyncMock())
        async def communicate():
            await asyncio.sleep(delay)
            process.returncode = 0
            return json.dumps(result).encode(), b''
        process.communicate = communicate
        return process

    async def slow_worker(self, initial):
        result = {'units': [], 'start': 101, 'end': 200, 'total': 1278}
        # 1:100 time scale: a 36-second parse exceeded the old 30/35-second walls.
        process = self.process(result, .36)
        actual_wait_for = asyncio.wait_for
        budgets = []
        async def scaled_wait(awaitable, timeout):
            budgets.append(timeout)
            return await actual_wait_for(awaitable, timeout / 100)
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
             patch('asyncio.wait_for', scaled_wait):
            value = (await paper_reader.extract_in_worker(b'fixture', 'fixture.md', 'source') if initial
                     else await reading_pages.read('fixture.pdf', 101, 200))
        self.assertEqual(budgets, [120])
        self.assertEqual(value['total'], 1278)
        process.kill.assert_not_called()

    async def test_initial_36_second_parse_fits_new_wall_budget(self):
        await self.slow_worker(True)

    async def test_continuation_36_second_parse_fits_new_wall_budget(self):
        await self.slow_worker(False)

    async def stopped_worker(self, initial, cancelled=False):
        process = self.process({})
        async def stop(awaitable, timeout):
            awaitable.close()
            raise asyncio.CancelledError() if cancelled else asyncio.TimeoutError()
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
             patch('asyncio.wait_for', stop):
            expected = asyncio.CancelledError if cancelled else (paper_reader.PaperError if initial else asyncio.TimeoutError)
            with self.assertRaises(expected):
                if initial:
                    await paper_reader.extract_in_worker(b'fixture', 'fixture.md', 'source')
                else:
                    await reading_pages.read('fixture.pdf', 101, 200)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_initial_timeout_still_kills_and_reaps_child(self):
        await self.stopped_worker(True)

    async def test_continuation_timeout_still_kills_and_reaps_child(self):
        await self.stopped_worker(False)

    async def test_initial_foreground_cancellation_still_reaps_child(self):
        await self.stopped_worker(True, cancelled=True)

    async def test_real_initial_and_later_window_still_stop_at_100_pages(self):
        from test_paper_reader import pdf_bytes
        body = pdf_bytes(pages=205)
        initial = await paper_reader.extract_in_worker(body, 'bounded.pdf', 'fixture')
        self.assertEqual(initial['total'], 205)
        self.assertEqual(len(initial['units']), 100)
        self.assertEqual(initial['units'][-1]['label'], '第100页')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bounded.pdf'
            path.write_bytes(body)
            later = await reading_pages.read(path, 101, 200)
            self.assertEqual((later['start'], later['end'], later['total']), (101, 200, 205))
            self.assertEqual(len(later['units']), 100)
            deep = await reading_pages.read(path, 201, 201, True)
            self.assertEqual((deep['start'], deep['end']), (201, 201))
            self.assertEqual(len(deep['units']), 1)

    async def test_parse_timeout_preserves_saved_cursor_and_can_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            now = [1000.0]
            store = ReadingSessions(Path(temp), clock=lambda: now[0])
            sha = store.cache_pdf(b'%PDF-test')
            job = store.start('group_1_2', {'_reading_sha': sha, 'title': 'book', 'total': 1278,
                'units': [{'label': '第100页', 'text': 'first window'}]}, 'initial reflection')
            gate = PriorityGate(clock=lambda: now[0])
            now[0] += 31
            parse = AsyncMock(side_effect=asyncio.TimeoutError)
            model = AsyncMock(return_value={'note': 'note', 'reflection': 'reflection', 'memory': 'memory'})
            publish = AsyncMock(return_value=True)
            await store.tick(gate, lambda: True, parse, paper_reader.build_context, model, publish)
            self.assertEqual(job['next_sample'], 101)
            self.assertEqual(ReadingSessions(Path(temp), clock=lambda: now[0]).current('group_1_2')['next_sample'], 101)
            model.assert_not_awaited()
            publish.assert_not_awaited()
            now[0] = job['retry_at'] + 1
            parse.side_effect = None
            parse.return_value = {'start': 101, 'end': 200, 'total': 1278, 'empty': [],
                'kind': 'PDF', 'truncated': True, 'units': [{'label': '第200页', 'text': 'later window'}]}
            await store.tick(gate, lambda: True, parse, paper_reader.build_context, model, publish)
            self.assertEqual(parse.call_args.args[1:3], (101, 200))
            self.assertEqual(job['next_sample'], 201)


if __name__ == '__main__':
    unittest.main()
