import asyncio
import base64
from collections import defaultdict
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import direct_vision as direct
import private_documents as documents
import paper_reader
import reading_bridge
from reading_sessions import ReadingSessions
from mechanism_assistant import allowed, MechanismAssistant
from model_decision import inspection_allowed, parse
import test_long_term_memory as existing


def file_event(user=7, name='手册.pdf', fid='attachment-1', **values):
    info = {'name': name, 'file_id': fid, **values}
    return {'message_type': 'private', 'sender': {'user_id': user}, 'user_id': user,
            'message': [{'type': 'file', 'data': info}], 'raw_message': ''}, info


class DirectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        direct._images.clear()
        self.enterContext(patch.object(bot, 'BASE_DIR', self.root))
        self.cfg = {'deepseek': {'enabled': True}, 'qwen_vl': {}}
        self.enterContext(patch.object(bot, 'load_config', return_value=self.cfg))
        self.sync = SimpleNamespace(enabled=True, storage_ok=True)
        self.enterContext(patch.object(bot, '_get_vision_sync', return_value=self.sync))
        self.enterContext(patch.object(bot, '_vision_route_signature', return_value='fixed-route'))
        self.enterContext(patch.object(bot, 'log'))
        self.enterContext(patch.object(direct.media_reader, 'prepare_webp', new_callable=AsyncMock, return_value=None))
        self.download = self.enterContext(patch.object(bot, '_download_vision_image', new_callable=AsyncMock,
            return_value=(b'fake-png-fixture', 'image/png')))
        self.context = {'scope': 'private_7', 'data': {'raw_message': '截图右边那句话是什么意思'}}
        self.token = direct._event.set(self.context)
        self.turn = direct._turn.set(None)
        self.addCleanup(direct._event.reset, self.token)
        self.addCleanup(direct._turn.reset, self.turn)

    def messages(self):
        return [{'role': 'system', 'content': 'persona'}, {'role': 'assistant', 'content': '刚才在讨论左右是谁'},
                {'role': 'user', 'content': '右边那句呢'}]

    async def test_main_message_contains_actual_image_and_previous_context_not_description_call(self):
        result = await direct.capture(bot, ['https://public.example/image'])
        self.assertIsInstance(result, direct.OriginalImages)
        messages = self.messages()
        direct.augment(bot, 'private_7', messages)
        self.assertIn('刚才在讨论左右', messages[1]['content'])
        self.assertIsInstance(messages[-1]['content'], list)
        data = next(p for p in messages[-1]['content'] if p['type'] == 'image_url')
        self.assertEqual(base64.b64decode(data['image_url']['url'].split(',')[1]), b'fake-png-fixture')
        self.assertEqual(direct.prompt_text(bot, 'old model-generated summary'), '截图右边那句话是什么意思')

    async def test_followup_reuses_image_without_downloading_again_or_resetting_expiry(self):
        await direct.capture(bot, ['https://public.example/image'])
        direct.augment(bot, 'private_7', self.messages())
        direct.commit(bot, 'private_7')
        created = direct._images[(str(self.root), 'private_7')][0]['created']
        direct._event.set({'scope': 'private_7', 'data': {}})
        messages = self.messages()
        direct.augment(bot, 'private_7', messages)
        self.assertIsInstance(messages[-1]['content'], list)
        direct.commit(bot, 'private_7')
        self.assertEqual(direct._images[(str(self.root), 'private_7')][0]['created'], created)
        self.download.assert_awaited_once()

    async def test_images_do_not_cross_groups_senders_private_chats_or_roots(self):
        await direct.capture(bot, ['https://public.example/image'])
        direct.augment(bot, 'private_7', self.messages())
        direct.commit(bot, 'private_7')
        for scope in ('private_8', 'group_1_7', 'group_2_7', 'group_1_8'):
            messages = self.messages()
            direct.augment(bot, scope, messages)
            self.assertIsInstance(messages[-1]['content'], str)
        with patch.object(bot, 'BASE_DIR', self.root / 'other'):
            direct._event.set(None)
            messages = self.messages()
            direct.augment(bot, 'private_7', messages)
            self.assertIsInstance(messages[-1]['content'], str)

    async def test_one_hour_expiry_and_restart_leave_only_text(self):
        await direct.capture(bot, ['https://public.example/image'])
        direct.augment(bot, 'private_7', self.messages())
        direct.commit(bot, 'private_7')
        direct._event.set(None)
        created = direct._images[(str(self.root), 'private_7')][0]['created']
        with patch.object(direct.time, 'time', return_value=created + 3601):
            messages = self.messages()
            direct.augment(bot, 'private_7', messages)
            self.assertIsInstance(messages[-1]['content'], str)
        self.assertFalse(direct._images)

    async def test_disabled_sync_and_group_restrictions_do_not_capture(self):
        self.sync.enabled = False
        self.assertIsNone(await direct.capture(bot, ['https://public.example/image']))
        self.sync.enabled = True
        self.cfg['qwen_vl']['trigger_groups'] = [2]
        self.context['scope'] = 'group_1_7'
        self.assertIsNone(await direct.capture(bot, ['https://public.example/image']))
        self.download.assert_not_awaited()

    async def test_failed_new_image_never_reuses_old_as_new(self):
        await direct.capture(bot, ['https://public.example/first'])
        direct.augment(bot, 'private_7', self.messages())
        direct.commit(bot, 'private_7')
        self.download.return_value = None
        await direct.capture(bot, ['https://public.example/second'])
        messages = self.messages()
        direct.augment(bot, 'private_7', messages)
        self.assertIsInstance(messages[-2]['content'], str)
        self.assertIn('失败', messages[-1]['content'])
        self.assertNotIn('image_url', str(messages))

    async def test_route_change_before_commit_cancels(self):
        await direct.capture(bot, ['https://public.example/image'])
        direct.augment(bot, 'private_7', self.messages())
        with patch.object(bot, '_vision_route_signature', return_value='different'):
            self.assertFalse(direct.valid(bot, 'private_7'))
            direct.commit(bot, 'private_7')
        self.assertFalse(direct._images)

    async def test_animated_webp_passes_real_sampled_frames_and_caps_total(self):
        prepared = {'animated': True, 'frames': [{'second': i, 'base64': base64.b64encode(b'frame-' + bytes([i])).decode()} for i in range(6)]}
        with patch.object(direct.media_reader, 'prepare_webp', AsyncMock(return_value=prepared)):
            await direct.capture(bot, ['https://public.example/image'] * 3)
        messages = self.messages()
        direct.augment(bot, 'private_7', messages)
        self.assertEqual(sum(p['type'] == 'image_url' for p in messages[-1]['content']), 6)


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.IntegrationTests.setUp

    async def test_actual_main_request_uses_original_image_and_saves_text_only(self):
        direct._images.clear()
        ctx = {'scope': 'private_7', 'data': {'raw_message': '右边是谁'}, 'prepared': True,
               'created': __import__('time').time(), 'parts': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,UE5H'}}]}
        token, turn = direct._event.set(ctx), direct._turn.set(None)
        try:
            with patch.object(direct, 'enabled', return_value=True), patch.object(bot, '_vision_route_signature', return_value='same'):
                self.create.return_value = existing.response(existing.raw([], reply='右侧是聊天对方。'))
                await bot.ask_deepseek('private_7', 'transcribed visual summary')
                actual = self.create.call_args.kwargs['messages']
                self.assertEqual(actual[-1]['content'][0]['text'], '右边是谁')
                self.assertTrue(any(p['type'] == 'image_url' for p in actual[-1]['content']))
                saved = json.dumps(bot.conversations['private_7'], ensure_ascii=False)
                self.assertNotIn('base64', saved)
                self.assertNotIn('image_url', saved)
                self.assertIn('右边是谁', saved)
                direct._event.set({'scope': 'private_7', 'data': {}})
                self.create.return_value = existing.response(existing.raw([], reply='我再看一下右侧。'))
                await bot.ask_deepseek('private_7', '再看看')
                self.assertTrue(any(p['type'] == 'image_url' for p in self.create.call_args.kwargs['messages'][-1]['content']))
        finally:
            direct._event.reset(token)
            direct._turn.reset(turn)
            direct._images.clear()

    async def test_main_rejects_image_route_change_without_saving_answer(self):
        token, turn = direct._event.set(None), direct._turn.set({'scope': 'private_7', 'expires': 0, 'route': 'old'})
        try:
            with patch.object(direct, 'augment'):
                reply = await bot.ask_deepseek('private_7', '看图')
            self.assertIn('取消', reply)
            self.assertFalse(bot.conversations['private_7'])
        finally:
            direct._event.reset(token)
            direct._turn.reset(turn)


class PrivateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'deepseek': {'enabled': True, 'trigger_private': True}, 'paper_reader': {}}
        self.library = paper_reader.PaperLibrary()
        self.send = AsyncMock(return_value=True)
        documents._files.clear()
        documents._waits.clear()
        for item in (patch.object(bot, 'BASE_DIR', self.root), patch.object(bot, 'load_config', return_value=self.cfg),
                     patch.object(bot, '_get_paper_library', return_value=self.library),
                     patch.object(bot, '_reading_signature', return_value='test-route'),
                     patch.object(bot, 'send_msg_to_user', self.send), patch.object(bot, 'log')):
            self.enterContext(item)

    async def test_register_then_read_minutes_later_and_scopes_are_isolated(self):
        data, row = file_event()
        await documents.receive(bot, data, [row], self.cfg)
        self.assertEqual(documents.recent(bot, 'private_7')[0]['name'], '手册.pdf')
        self.assertFalse(documents.recent(bot, 'private_8'))
        self.assertFalse(documents.recent(bot, 'group_1_7'))
        with patch.object(documents, 'read', new_callable=AsyncMock) as read:
            await documents.command(bot, {**data, 'raw_message': '读文献 手册'}, self.cfg)
            read.assert_awaited_once()
            self.assertEqual(read.call_args.kwargs['row']['file_id'], 'attachment-1')

    async def test_natural_request_resolves_recent_file_instead_of_claiming_missing(self):
        data, row = file_event()
        await documents.receive(bot, data, [row], self.cfg)
        self.assertTrue(allowed('read_private_file', 'private_7', False))
        proposal = {'action': 'read_private_file', 'args': {'name': '这份文件'}}
        value, context, clarification = await bot._resolve_semantic_proposal(proposal, 'private_7', '给个提要', self.cfg, {})
        self.assertIsNone(clarification)
        self.assertEqual(value['args']['name'], '手册.pdf')
        self.assertEqual(context['nonce'], documents.recent(bot, 'private_7')[0]['_nonce'])

    async def test_multiple_files_require_selection_and_filename_can_choose(self):
        data, first = file_event()
        _, second = file_event(name='另一本.docx', fid='attachment-2')
        documents.register(bot, 'private_7', [first, second])
        self.assertEqual(len(documents.resolve(bot, 'private_7', '这份文件')), 2)
        self.assertEqual(documents.resolve(bot, 'private_7', '另一本')[0]['file_id'], 'attachment-2')
        with patch.object(documents, 'read', new_callable=AsyncMock) as read:
            await documents.command(bot, {**data, 'raw_message': '读文献'}, self.cfg)
            read.assert_not_awaited()

    async def test_wait_then_upload_reads_correct_file(self):
        data, row = file_event()
        await documents.command(bot, {**data, 'raw_message': '读文献 手册'}, self.cfg)
        with patch.object(documents, 'read', new_callable=AsyncMock) as read:
            await documents.receive(bot, data, [row], self.cfg)
            read.assert_awaited_once()

    async def test_expired_metadata_not_available_and_caption_reads_without_translation(self):
        data, row = file_event()
        with patch.object(documents.time, 'time', return_value=1000):
            documents.register(bot, 'private_7', [row])
        with patch.object(documents.time, 'time', return_value=1601):
            self.assertFalse(documents.recent(bot, 'private_7'))
        with patch.object(documents, 'read', new_callable=AsyncMock) as read:
            await documents.receive(bot, {**data, 'raw_message': '请读一下并给提要'}, [row], self.cfg)
            read.assert_awaited_once()

    async def test_private_downloader_uses_existing_public_resolver_path_and_size_checks(self):
        _, row = file_event(url='https://public.example/signed')
        doc = {'title': '手册', 'units': []}
        with patch.object(bot.paper_reader, 'fetch_bytes', AsyncMock(return_value=(b'data', 'application/pdf', 'url'))) as fetch, \
             patch.object(bot.paper_reader, 'extract_in_worker', AsyncMock(return_value=doc)) as extract:
            self.assertEqual(await documents.download(bot, row, '7'), doc)
            fetch.assert_awaited_once_with(row['url'])
            extract.assert_awaited_once()
        with self.assertRaises(paper_reader.PaperError):
            await documents.download(bot, {**row, 'size': 65 * 1024 * 1024}, '7')

    async def test_fallback_private_url_and_base64_supported_but_local_paths_rejected(self):
        _, row = file_event(file_hash='fixture-hash')
        doc = {'title': 'document'}
        api = AsyncMock(side_effect=[{}, {'base64': base64.b64encode(b'body').decode()}])
        with patch.object(bot, 'call_api', api), patch.object(bot.paper_reader, 'extract_in_worker', AsyncMock(return_value=doc)):
            self.assertEqual(await documents.download(bot, row, '7'), doc)
            self.assertEqual(api.call_args_list[0].args[1]['user_id'], 7)
        with patch.object(bot, 'call_api', AsyncMock(return_value={'file': str(self.root / 'config.yaml')})):
            with self.assertRaises(paper_reader.PaperError):
                await documents.download(bot, row, '7')

    async def test_download_failure_does_not_mark_document_read_or_start_session(self):
        data, row = file_event()
        with patch.object(documents, 'download', AsyncMock(side_effect=TimeoutError())):
            await documents.read(bot, data, '概括', self.cfg, row=row)
        self.assertIsNone(self.library.current_doc(('private', '7')))
        self.assertFalse(self.library.busy)
        self.assertIn('未完成', self.send.call_args.args[-1])

    async def test_generation_failure_does_not_mark_document_read(self):
        data, row = file_event()
        doc = {'title': 'book', 'kind': 'PDF文字', 'units': [], 'total': 100}
        with patch.object(documents, 'download', AsyncMock(return_value=doc)), \
             patch.object(bot, '_generate_paper', AsyncMock(side_effect=TimeoutError())):
            await documents.read(bot, data, '概括', self.cfg, row=row)
        self.assertIsNone(self.library.current_doc(('private', '7')))

    async def test_pdf_private_session_survives_restart_and_not_visible_to_group(self):
        store = reading_bridge.sessions(bot)
        digest = store.cache_pdf(b'%PDF-fixture')
        doc = {'_reading_sha': digest, 'title': 'PrivatePDF', 'total': 250,
               'units': [{'label': '第100页', 'text': '真实测试文字'}]}
        job = store.start('private_7', doc, '私人初读感想')
        self.assertEqual(job['next_sample'], 101)
        restarted = ReadingSessions(self.root)
        self.assertTrue(restarted.storage_ok)
        self.assertEqual(restarted.current('private_7')['next_sample'], 101)
        self.assertIsNone(restarted.current('private_8'))
        self.assertIsNone(reading_bridge.memory(bot, 'group_1_7'))
        self.assertTrue(reading_bridge.job_allowed(bot, job))

    async def test_private_read_success_stores_scoped_doc_and_progress(self):
        data, row = file_event()
        store = reading_bridge.sessions(bot)
        digest = store.cache_pdf(b'%PDF-fixture')
        doc = {'_reading_sha': digest, 'title': 'PrivatePDF', 'kind': 'PDF文字', 'total': 250,
               'units': [{'label': '第100页', 'text': '真实测试文字'}]}
        with patch.object(documents, 'download', AsyncMock(return_value=doc)), \
             patch.object(bot, '_generate_paper', AsyncMock(return_value='只读了前100页的部分摘录')):
            await documents.read(bot, data, '概括', self.cfg, row=row)
        self.assertEqual(self.library.current_doc(('private', '7'))['title'], 'PrivatePDF')
        self.assertIsNone(self.library.current_doc(('1', '7')))
        self.assertEqual(store.current('private_7')['next_sample'], 101)

    async def test_forget_clears_only_own_private_document(self):
        self.library.store_doc(('private', '7'), {'title': 'Private'})
        self.library.store_doc(('1', '7'), {'title': 'Group'})
        data, row = file_event()
        documents.register(bot, 'private_7', [row])
        await documents.command(bot, {**data, 'raw_message': '忘记文献'}, self.cfg)
        self.assertIsNone(self.library.current_doc(('private', '7')))
        self.assertEqual(self.library.current_doc(('1', '7'))['title'], 'Group')
        self.assertFalse(documents.recent(bot, 'private_7'))

    async def test_private_inspections_and_control_actions_are_allowed_but_group_listing_is_not(self):
        for name in ('inspect_private_files', 'inspect_current_document', 'inspect_reading_memory'):
            self.assertTrue(inspection_allowed(name, 'private_7', self.cfg))
        self.assertFalse(inspection_allowed('inspect_group_files', 'private_7', self.cfg))
        for name in ('document_deep', 'document_resume', 'document_pause', 'document_accept', 'document_progress', 'document_forget'):
            self.assertTrue(allowed(name, 'private_7', False))
        raw = json.dumps({'kind': 'clarify', 'reply': '请发文件', 'action': {'action': 'read_private_file', 'args': {'name': '待上传文件'}}})
        self.assertEqual(parse(raw, 'private_7', self.cfg, False).kind, 'clarify')

    async def test_private_disabled_does_not_register_attachment(self):
        self.cfg['paper_reader']['private_enabled'] = False
        data, row = file_event()
        await documents.receive(bot, data, [row], self.cfg)
        self.assertFalse(documents.recent(bot, 'private_7'))

    async def test_offline_file_notice_dispatches_private_file_event(self):
        with patch.object(bot, 'on_private_message', new_callable=AsyncMock) as receive:
            await bot.handle_event({'post_type': 'notice', 'notice_type': 'offline_file', 'user_id': 7,
                                    'file': {'name': 'manual.pdf', 'id': '1'}})
            self.assertEqual(receive.call_args.args[0]['message_type'], 'private')
            self.assertEqual(receive.call_args.args[0]['message'][0]['type'], 'file')

    async def test_actual_private_handler_registers_file_instead_of_requesting_translation(self):
        data, row = file_event()
        with patch.object(bot, 'bot_qq', 999), patch.object(bot, 'deepseek_client', object()), \
             patch.object(bot, '_get_mechanism_assistant', return_value=MechanismAssistant()), \
             patch.object(documents, 'receive', new_callable=AsyncMock) as receive:
            await bot.on_private_message(data)
            receive.assert_awaited_once()
            self.assertEqual(receive.call_args.args[2][0]['name'], '手册.pdf')

    async def test_translation_reservation_keeps_priority_over_document_reading(self):
        data, row = file_event()
        with patch.object(bot, 'bot_qq', 999), patch.object(bot, 'deepseek_client', object()), \
             patch.object(bot, '_get_mechanism_assistant', return_value=MechanismAssistant()), \
             patch.dict(bot.translation_waits, {'7': {'expires_at': __import__('time').time() + 600, 'group_id': None}}, clear=True), \
             patch.object(bot, '_translate_private_file', new_callable=AsyncMock) as translate, \
             patch.object(documents, 'receive', new_callable=AsyncMock) as receive:
            await bot.on_private_message(data)
            translate.assert_awaited_once()
            receive.assert_not_awaited()

    async def test_filename_containing_read_word_is_data_not_an_instruction(self):
        data, row = file_event(name='阅读方法.pdf')
        with patch.object(documents, 'read', new_callable=AsyncMock) as read:
            await documents.receive(bot, {**data, 'raw_message': '[CQ:file,name=阅读方法.pdf,file_id=attachment-1]'}, [row], self.cfg)
            read.assert_not_awaited()

    async def test_private_pdf_controls_preserve_other_scopes_and_precise_expiry(self):
        data, row = file_event()
        store = reading_bridge.sessions(bot)
        digest = store.cache_pdf(b'%PDF-test')
        doc = {'_reading_sha': digest, 'title': 'Private', 'total': 250,
               'units': [{'label': '第100页', 'text': '正文'}]}
        job = store.start('private_7', doc, '感想')
        await reading_bridge.control(bot, data, 'deep', self.cfg)
        self.assertEqual(job['mode'], 'deep')
        await reading_bridge.control(bot, data, 'pause', self.cfg)
        self.assertFalse(job['active'])
        await reading_bridge.control(bot, data, 'resume', self.cfg)
        self.assertTrue(job['active'])
        self.assertIsNone(store.current('private_8'))


if __name__ == '__main__':
    unittest.main()
