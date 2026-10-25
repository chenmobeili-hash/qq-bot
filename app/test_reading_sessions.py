import asyncio
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta

import bot
import paper_reader
import reading_bridge
import reading_pages
from reading_sessions import ReadingSessions, PriorityGate, PRECISE_SECONDS
from model_decision import parse


class SessionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = [100000.0]
        self.store = ReadingSessions(self.root, clock=lambda: self.now[0])
        self.gate = PriorityGate(clock=lambda: self.now[0])
        sha = self.store.cache_pdf(b'%PDF-test-fixture')
        self.doc = {'_reading_sha': sha, 'title': '测试书.pdf', 'total': 565,
                    'units': [{'label': '第100页', 'text': 'initial text'}]}
        self.job = self.store.start('group_123_456', self.doc, '初读感想')
        self.now[0] += 31
        self.extract = AsyncMock(side_effect=self.extracted)
        self.generate = AsyncMock(return_value={'note': '具体笔记', 'reflection': '完整累计读后感', 'memory': '自选记忆'})
        self.publish = AsyncMock(return_value=True)

    def tearDown(self):
        self.tmp.cleanup()

    def extracted(self, path, start, end, deep):
        return {'start': start, 'end': end, 'total': 565, 'units': [
            {'label': f'第{p}页', 'text': 'body ' * 50} for p in range(start, end + 1)],
            'empty': [], 'kind': 'PDF文字', 'truncated': not deep}

    async def tick(self, **overrides):
        return await self.store.tick(self.gate, overrides.get('eligible', lambda: True),
            self.extract, paper_reader.build_context, self.generate, self.publish,
            job_allowed=overrides.get('job_allowed', lambda job: True))

    async def test_sample_moves_101_to_200_then_201_and_persists_real_locators(self):
        await self.tick()
        self.assertEqual(self.job['next_sample'], 201)
        self.assertEqual(self.extract.call_args.args[1:3], (101, 200))
        context = self.generate.call_args.args[1]['document']
        self.assertLessEqual(len(context['coverage']['locators']), 12)
        self.assertIn('第101页', context['coverage']['locators'])
        reboot = ReadingSessions(self.root, clock=lambda: self.now[0])
        self.assertEqual(reboot.current('group_123_456')['next_sample'], 201)
        await self.tick()
        self.assertEqual(self.job['next_sample'], 301)
        self.assertIn('抽样', self.publish.call_args.args[1])

    async def test_interrupt_during_model_discards_step_then_resumes_saved_cursor(self):
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        self.generate.side_effect = blocked
        task = asyncio.create_task(self.tick())
        await entered.wait()
        with self.gate.foreground():
            await task
            self.assertEqual(self.job['next_sample'], 101)
            self.assertTrue(self.job['active'])
        self.publish.assert_not_awaited()
        self.generate.side_effect = None
        await self.tick()
        self.assertEqual(self.job['next_sample'], 101)
        self.now[0] += 31
        await self.tick()
        self.assertEqual(self.job['next_sample'], 201)

    async def test_state_changes_just_before_commit_discards_result(self):
        def change(*args):
            self.gate.revision += 1
            return {'note': 'x', 'reflection': 'y', 'memory': 'z'}
        self.generate.side_effect = change
        await self.tick()
        self.assertEqual(self.job['next_sample'], 101)
        self.publish.assert_not_awaited()

    async def test_deep_dense_page_is_not_skipped_and_restarts_at_saved_offset(self):
        self.store.control('group_123_456', 'deep')
        def dense(path, start, end, deep):
            return {'start': start, 'end': end, 'total': 565, 'units': [{'label': '第1页', 'text': '字' * 25000}], 'empty': []}
        self.extract.side_effect = dense
        await self.tick()
        self.assertEqual((self.job['next_deep'], self.job['part']), (1, 12000))
        await self.tick()
        self.assertEqual((self.job['next_deep'], self.job['part']), (1, 24000))
        await self.tick()
        self.assertEqual((self.job['next_deep'], self.job['part']), (2, 0))
        self.assertEqual(len(self.job['precise']), 3)
        self.assertEqual(self.job['next_sample'], 101)
        self.publish.assert_not_awaited()

    async def test_five_hour_expiry_keeps_reflection_selected_memory_and_cursor(self):
        self.store.control('group_123_456', 'deep')
        await self.tick()
        self.assertIn('body ', self.job['precise'][0]['text'])
        self.now[0] += PRECISE_SECONDS - 1
        self.assertTrue(self.store.memory_for('group_123_456')['precise_notes'])
        self.now[0] += 1
        reboot = ReadingSessions(self.root, clock=lambda: self.now[0])
        retained = reboot.memory_for('group_123_456')
        self.assertEqual(retained['precise_notes'], [])
        self.assertEqual(retained['reflection'], '完整累计读后感')
        self.assertEqual(retained['selected_memory'], '自选记忆')
        self.assertEqual(reboot.current('group_123_456')['next_deep'], 2)
        self.assertNotIn('body ', (self.root / 'reading_sessions.json').read_text())

    async def test_expiration_is_per_page_not_extended_by_later_reading(self):
        self.store.control('group_123_456', 'deep')
        await self.tick()
        self.now[0] += 100
        await self.tick()
        self.now[0] += PRECISE_SECONDS - 100
        self.assertEqual([n['page'] for n in self.store.memory_for('group_123_456')['precise_notes']], [2])

    async def test_pause_accept_resume_and_scope_isolation(self):
        self.store.control('group_123_456', 'accept')
        await self.tick()
        self.extract.assert_not_awaited()
        self.store.control('group_123_456', 'resume')
        await self.tick()
        self.assertEqual(self.job['next_sample'], 201)
        self.assertIsNone(self.store.memory_for('group_999_456'))
        self.assertIsNone(self.store.memory_for('group_123_999'))
        self.store.control('group_123_456', 'pause')
        self.assertFalse(self.job['active'])

    async def test_disallowed_job_does_not_block_other_group(self):
        second = self.store.start('group_999_456', self.doc, 'other group')
        await self.tick(job_allowed=lambda j: j['scope'].startswith('group_999_'))
        self.assertEqual(self.job['next_sample'], 101)
        self.assertEqual(second['next_sample'], 201)

    async def test_failures_and_oversized_memory_never_advance_then_pause(self):
        self.generate.return_value['memory'] = '长' * 3001
        for _ in range(3):
            await self.tick()
            self.now[0] += 2000
        self.assertEqual(self.job['next_sample'], 101)
        self.assertFalse(self.job['active'])
        self.assertEqual(self.job['errors'], 3)
        self.publish.assert_not_awaited()

    async def test_missing_or_tampered_source_never_calls_parser(self):
        path = self.store.source(self.job)
        path.write_bytes(b'changed')
        await self.tick()
        self.extract.assert_not_awaited()
        self.assertEqual(self.job['next_sample'], 101)

    async def test_save_failure_rolls_back_cursor(self):
        before = copy.deepcopy(self.job)
        with patch.object(self.store, 'save', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                await self.tick()
        self.assertEqual(self.job['next_sample'], before['next_sample'])
        self.publish.assert_not_awaited()

    async def test_new_document_suspends_old_but_keeps_progress(self):
        await self.tick()
        doc = {**self.doc, '_reading_sha': self.store.cache_pdf(b'%PDF-another')}
        new = self.store.start('group_123_456', doc, '新书')
        self.assertFalse(self.job['active'])
        self.assertFalse(self.job['current'])
        self.assertEqual(self.job['next_sample'], 201)
        self.assertIs(self.store.current('group_123_456'), new)

    async def test_blank_page_is_gap_not_fabricated_reading(self):
        self.store.control('group_123_456', 'deep')
        self.extract.return_value = {'start': 1, 'end': 1, 'total': 565, 'units': [], 'empty': [1]}
        self.extract.side_effect = None
        await self.tick()
        self.assertTrue(self.generate.call_args.args[1]['no_text'])
        self.assertEqual(self.job['gaps'], [1])
        self.assertEqual(self.job['next_deep'], 2)

    async def test_cancelled_parser_is_killed_and_waited(self):
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError),
                                  kill=lambda: None, wait=AsyncMock())
        with patch('reading_pages.asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch.object(process, 'kill') as kill:
            with self.assertRaises(asyncio.CancelledError):
                await reading_pages.read('fixture.pdf', 1, 1, True)
        kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_real_worker_can_read_later_range(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(205):
            writer.add_blank_page(width=72, height=72)
        path = self.root / 'pages.pdf'
        with path.open('wb') as stream:
            writer.write(stream)
        data = await reading_pages.read(path, 101, 200)
        self.assertEqual((data['start'], data['end'], data['total']), (101, 200, 205))
        self.assertEqual(data['empty'], list(range(101, 201)))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_manual_read_registers_background_cursor(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(bot, 'BASE_DIR', Path(temp)):
            store = reading_bridge.sessions(bot)
            sha = store.cache_pdf(b'%PDF-manual')
            doc = {'_reading_sha': sha, 'title': 'Manual.pdf', 'kind': 'PDF文字', 'total': 250,
                   'units': [{'label': '第100页', 'text': 'example text'}], 'truncated': True}
            with patch.object(bot, '_get_paper_library', return_value=paper_reader.PaperLibrary()), \
                 patch.object(bot, '_get_paper_autonomy', return_value=SimpleNamespace(storage_ok=False)), \
                 patch.object(bot, '_download_group_paper', AsyncMock(return_value=doc)), \
                 patch.object(bot, '_generate_paper', AsyncMock(return_value='有效初读')), \
                 patch.object(bot, 'send_group_message', AsyncMock(return_value=True)), \
                 patch.object(bot, 'load_config', return_value={}), patch.object(bot, 'log'):
                await bot._run_paper_read(123, '456', '', {}, selected_info={'name': 'Manual.pdf', 'id': '1'})
            self.assertEqual(store.current('group_123_456')['next_sample'], 101)

    async def test_real_initial_pdf_extraction_caches_bytes_without_second_download(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 10 Td (' + b'Example public invented document. ' * 4 + b') Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        body = io.BytesIO()
        writer.write(body)
        with tempfile.TemporaryDirectory() as temp, patch.object(bot, 'BASE_DIR', Path(temp)), patch.object(bot.paper_reader, 'fetch_bytes') as fetch:
            doc = await bot.paper_reader.extract_in_worker(body.getvalue(), 'fixture.pdf', 'fixture')
            self.assertTrue(doc['_reading_sha'])
            job = reading_bridge.sessions(bot).start('group_123_456', doc, '初读')
            self.assertEqual(reading_bridge.sessions(bot).source(job).read_bytes(), body.getvalue())
            fetch.assert_not_called()

    async def test_outgoing_and_proactive_generation_have_foreground_priority(self):
        now = [100.0]
        local_gate = PriorityGate(clock=lambda: now[0])
        called = []
        @reading_bridge.foreground
        async def reply():
            called.append(local_gate.busy)
        with patch.object(reading_bridge, 'gate', local_gate):
            await reply()
        self.assertEqual(called, [1])
        self.assertFalse(local_gate.idle())
        now[0] += 31
        self.assertTrue(local_gate.idle())

    async def test_forgetting_shared_memory_is_personal_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(bot, 'BASE_DIR', Path(temp)):
            store = reading_bridge.sessions(bot)
            sha = store.cache_pdf(b'%PDF-shared')
            doc = {'_reading_sha': sha, 'title': 'Shared', 'total': 100,
                   'units': [{'label': '第1页', 'text': 'shared text'}]}
            store.start('group_123_0', doc, '共享感想')
            self.assertIsNotNone(reading_bridge.memory(bot, 'group_123_456'))
            store.control('group_123_456', 'forget')
            self.assertIsNone(reading_bridge.memory(bot, 'group_123_456'))
            self.assertIsNotNone(reading_bridge.memory(bot, 'group_123_789'))
            reboot = ReadingSessions(Path(temp))
            self.assertEqual(reboot.ignored['group_123_456'], sha)

    async def test_reading_actions_and_readonly_memory_use_normal_model_protocol(self):
        for action in ('document_deep', 'document_resume', 'document_pause', 'document_accept', 'document_progress'):
            data = json.dumps({'kind': 'operation', 'reply': '按这个方式读吗？', 'action': {'action': action, 'args': {}}})
            self.assertEqual(parse(data, 'group_123_456', {}, False).action['action'], action)
        data = json.dumps({'kind': 'inspect', 'reply': '', 'action': {'action': 'inspect_reading_memory', 'args': {}}})
        self.assertEqual(parse(data, 'group_123_456', {}, False).kind, 'inspect')

    async def test_schedule_due_pending_confirmation_busy_and_offline_block_reading(self):
        fake = SimpleNamespace(ws_connection=object(), pending_messages=[],
            _get_paper_library=lambda: SimpleNamespace(busy=set()),
            _get_mechanism_assistant=lambda: SimpleNamespace(pending={}, clock=lambda: 0),
            _reading_signature=lambda cfg: 'same', load_config=lambda: {}, schedule=SimpleNamespace(jobs=[]))
        self.assertTrue(reading_bridge.can_read(fake, 'same'))
        fake.schedule.jobs = [SimpleNamespace(next_run=datetime.now() + timedelta(seconds=20))]
        self.assertFalse(reading_bridge.can_read(fake, 'same'))
        fake.schedule.jobs = []
        fake.pending_messages.append({})
        self.assertFalse(reading_bridge.can_read(fake, 'same'))
        fake.pending_messages.clear()
        fake._get_mechanism_assistant = lambda: SimpleNamespace(pending={'x': SimpleNamespace(expires=120)}, clock=lambda: 0)
        self.assertFalse(reading_bridge.can_read(fake, 'same'))

    async def test_expired_assistant_details_removed_from_memory_and_disk(self):
        from collections import defaultdict
        with tempfile.TemporaryDirectory() as temp, patch.object(bot, 'BASE_DIR', Path(temp)), patch.object(bot, 'conversations', defaultdict(list)):
            bot.conversations['group_123_456'] = [
                {'role': 'assistant', 'content': 'EXPIRED_DETAIL', 'reading_expires': 1},
                {'role': 'user', 'content': 'user words'},
                {'role': 'assistant', 'content': 'still valid', 'reading_expires': 9999999999}]
            bot._save_conversations()
            self.assertNotIn('EXPIRED_DETAIL', (Path(temp) / 'conversations.json').read_text())
            self.assertEqual(len(bot._build_history('group_123_456', 10)), 2)

    async def test_deep_inspection_uses_retained_memory_not_initial_raw_pdf(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(bot, 'BASE_DIR', Path(temp)):
            store = reading_bridge.sessions(bot)
            sha = store.cache_pdf(b'%PDF-fake')
            doc = {'_reading_sha': sha, 'title': 'Book', 'total': 2, 'units': [{'label': '第1页', 'text': 'raw private book text'}]}
            store.start('group_123_456', doc, 'reflection')
            store.control('group_123_456', 'deep')
            with patch.object(bot, '_get_paper_library') as library:
                result = await bot._semantic_inspection('inspect_current_document', 'group_123_456', '这本怎么样', {})
            library.assert_not_called()
            self.assertEqual(result['reflection'], 'reflection')
            self.assertNotIn('raw private book text', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
