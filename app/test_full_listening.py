import asyncio
import base64
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import audio_reader
import full_listening
import hearing
import qq_media
from mechanism_assistant import validate, describe


def chunks(count=12):
    return {'kind': '音频', 'duration': count * 30, 'frames': [], 'audio_chunks': [
        {'start': i * 30, 'end': (i + 1) * 30, 'format': 'mp3',
         'base64': base64.b64encode(b'ID3fixture-' + str(i).encode()).decode()} for i in range(count)]}


def response(more=False):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
        content=json.dumps({'summary': '实际听到一段人声，背景安静。', 'need_more': more}, ensure_ascii=False)))])


class FullListeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'audio_input': {'enabled': True, 'api_key': 'FAKE_KEY', 'model': 'audio-fixture',
                    'endpoint': 'https://fixture.example/v1', 'trigger_groups': []}}
        self.send, self.ask = AsyncMock(return_value=True), AsyncMock(return_value='这些声音的整体感受。')
        self.mode = SimpleNamespace(background_busy=set(), observe=Mock())
        self.fake = SimpleNamespace(BASE_DIR=Path(self.temp.name), bot_qq='900', load_config=lambda: self.cfg,
            hearing_sync=None, _mechanism_key=bot._mechanism_key, log=Mock(),
            send_msg_to_user=self.send, ask_deepseek=self.ask,
            reading_bridge=SimpleNamespace(gate=SimpleNamespace(foreground=lambda: nullcontext())),
            _get_proactive_mode=lambda: self.mode, _private_file_segments=bot._private_file_segments,
            _strip_cq_media=bot._strip_cq_media, _strip_at=bot._strip_at,
            _is_mentioned=lambda raw, parts: '[CQ:at,qq=900]' in raw,
            _effective_vision_config=lambda cfg: {'enabled': False})
        self.clock = [1000]
        self.media = qq_media.MediaController(self.fake, lambda: self.clock[0])
        self.full = self.media.full_listener
        self.data = self.event()
        self.key = 'private_7'
        self.media.remember(self.key, {'name': 'clip.mp3', 'file_id': 'fixture'})
        self.download = self.enterContext(patch.object(self.media, 'download', AsyncMock(return_value=b'ID3fixture')))
        self.prepare = self.enterContext(patch.object(audio_reader, 'prepare', AsyncMock(return_value=chunks())))
        self.create = AsyncMock(return_value=response())
        api = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        class Client:
            async def __aenter__(inner):
                return api
            async def __aexit__(inner, *args):
                pass
        self.factory = self.enterContext(patch.object(hearing, 'AsyncOpenAI', return_value=Client()))
        self.addAsyncCleanup(self.stop_jobs)

    async def stop_jobs(self):
        tasks = [job['task'] for job in self.full.jobs.values() if not job['task'].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def event(self, group=None, user=7, text=''):
        event = {'message_type': 'private' if group is None else 'group',
                 'sender': {'user_id': user, 'nickname': 'test'}, 'raw_message': text, 'message': []}
        if group is not None:
            event['group_id'] = group
        return event

    async def finish(self, data=None):
        data = data or self.data
        await self.full.start(data)
        key = self.fake._mechanism_key(data)
        await self.full.jobs[key]['task']
        return self.full.jobs[key]

    async def test_all_segments_even_when_model_says_need_more_false(self):
        job = await self.finish()
        self.assertEqual(self.create.await_count, 12)
        self.assertEqual(self.factory.call_count, 3)
        saved = self.media.reviews.get(self.key)
        self.assertEqual(saved['state']['hearing']['seen'], list(range(12)))
        self.assertEqual(job['phase'], '完成')
        self.assertEqual(self.ask.await_count, 1)
        self.assertIn('音轨已听完：12/12段', self.send.await_args.args[2])
        self.assertNotIn('base64', self.ask.await_args.args[1])
        self.assertEqual(self.download.await_count, 1)

    async def test_video_extracts_only_audio_and_does_not_claim_full_visual(self):
        self.media.remember(self.key, {'name': 'video.mp4', 'file_id': 'fixture'})
        await self.finish()
        self.prepare.assert_awaited_once()
        self.assertIn('不能声称完整看过全部画面', self.ask.await_args.args[1])

    async def test_cached_partial_progress_and_bili_audio_resume_without_download(self):
        result = chunks(8)
        state = {'hearing': {'route': hearing.signature(self.fake, self.cfg), 'seen': [0, 1], 'notes': ['已听片段0', '已听片段1']}}
        result['kind'] = 'B站视频'
        self.media.recent.clear()
        self.media.reviews.put(self.key, result, state)
        await self.finish()
        self.download.assert_not_awaited()
        self.assertEqual(self.create.await_count, 6)
        first_audio = self.create.await_args_list[0].kwargs['messages'][1]['content'][1]['input_audio']['data']
        self.assertEqual(base64.b64decode(first_audio), b'ID3fixture-2')

    async def test_duplicate_start_and_inflight_manual_review_do_not_duplicate_calls(self):
        reached, release = asyncio.Event(), asyncio.Event()
        async def wait(**kwargs):
            reached.set()
            await release.wait()
            return response()
        self.create.side_effect = wait
        await self.full.start(self.data)
        await reached.wait()
        task = self.full.jobs[self.key]['task']
        await self.full.start(self.data)
        await self.media.read(self.data, more=True, audio_only=True)
        self.assertIs(task, self.full.jobs[self.key]['task'])
        self.assertEqual(self.create.await_count, 1)
        release.set()
        await task

    async def test_failure_after_success_pauses_and_retry_starts_at_failed_segment(self):
        self.create.side_effect = [response(), TimeoutError()]
        job = await self.finish()
        state = self.media.reviews.get(self.key)['state']['hearing']
        self.assertEqual(state['seen'], [0])
        self.assertIn('暂停', job['phase'])
        self.ask.assert_not_awaited()
        self.create.side_effect = None
        self.create.return_value = response()
        await self.finish()
        self.assertEqual(self.create.await_count, 13)
        self.assertEqual(state['seen'], list(range(12)))

    async def test_unsupported_audio_stops_after_one_request(self):
        class Unsupported(Exception):
            status_code = 400
        self.create.side_effect = Unsupported('PRIVATE_RESPONSE')
        job = await self.finish()
        self.assertIn('暂停', job['phase'])
        self.assertEqual(self.create.await_count, 1)
        self.assertNotIn('PRIVATE_RESPONSE', repr(self.send.await_args_list))

    async def test_stop_cancels_inflight_segment_and_resumes_only_remaining(self):
        reached = asyncio.Event()
        async def wait(**kwargs):
            reached.set()
            await asyncio.Event().wait()
        self.create.side_effect = [response(), wait]
        # AsyncMock list side effects do not await coroutine functions; use a dispatcher.
        count = [0]
        async def dispatch(**kwargs):
            count[0] += 1
            if count[0] == 1:
                return response()
            return await wait(**kwargs)
        self.create.side_effect = dispatch
        await self.full.start(self.data)
        await reached.wait()
        await self.full.control(self.data, 'stop')
        state = self.media.reviews.get(self.key)['state']['hearing']
        self.assertEqual(state['seen'], [0])
        self.assertTrue(self.full.jobs[self.key]['task'].cancelled())
        self.create.side_effect = None
        self.create.return_value = response()
        await self.finish()
        self.assertEqual(state['seen'], list(range(12)))

    async def test_other_sender_and_other_chat_cannot_stop_owner_job(self):
        reached, release = asyncio.Event(), asyncio.Event()
        async def wait(**kwargs):
            reached.set()
            await release.wait()
            return response()
        self.create.side_effect = wait
        await self.full.start(self.data)
        await reached.wait()
        task = self.full.jobs[self.key]['task']
        for event in (self.event(user=8), self.event(group=123), self.event(group=456)):
            await self.full.control(event, 'stop')
            self.assertFalse(task.cancelled())
            self.assertFalse(task.done())
        release.set()
        await task

    async def test_new_media_cancels_old_job_before_reading_new_media(self):
        reached = asyncio.Event()
        async def wait(**kwargs):
            reached.set()
            await asyncio.Event().wait()
        self.create.side_effect = wait
        await self.full.start(self.data)
        await reached.wait()
        old = self.full.jobs[self.key]['task']
        with patch.object(self.media, 'read', AsyncMock()) as read:
            await self.media.message({**self.data, 'message': [
                {'type': 'file', 'data': {'name': 'new.mp3', 'file_id': 'new'}}]})
            read.assert_awaited_once()
        self.assertTrue(old.cancelled())
        self.ask.assert_not_awaited()

    async def test_cache_expiry_discards_inflight_result_before_progress_commit(self):
        async def expired(**kwargs):
            self.clock[0] += 3601
            return response()
        self.create.side_effect = expired
        job = await self.finish()
        self.assertIn('暂停', job['phase'])
        self.assertEqual(job['saved']['state']['hearing']['seen'], [])
        self.assertEqual(self.create.await_count, 1)
        self.ask.assert_not_awaited()

    async def test_route_change_does_not_publish_old_result_or_continue(self):
        async def changed(**kwargs):
            self.cfg['audio_input']['model'] = 'new-model'
            return response()
        self.create.side_effect = changed
        job = await self.finish()
        self.assertIn('暂停', job['phase'])
        self.assertEqual(self.create.await_count, 1)
        self.ask.assert_not_awaited()

    async def test_query_progress_while_model_waits(self):
        reached = asyncio.Event()
        async def wait(**kwargs):
            reached.set()
            await asyncio.Event().wait()
        self.create.side_effect = wait
        await self.full.start(self.data)
        await reached.wait()
        await self.full.control(self.data, 'status')
        self.assertIn('收听中', self.send.await_args.args[2])
        self.assertIn('0/12段', self.send.await_args.args[2])

    async def test_private_command_reservation_is_consumed_by_next_upload(self):
        self.media.recent.clear()
        self.assertTrue(await self.media.message(self.event(text='自動聽完音頻')))
        self.assertTrue(self.media.waiting[self.key]['full'])
        await self.media.message({**self.data, 'message': [
            {'type': 'file', 'data': {'name': 'voice.mp3', 'file_id': 'fixture'}}]})
        await self.full.jobs[self.key]['task']
        self.assertEqual(self.create.await_count, 12)

    async def test_stop_cancels_waiting_upload_reservation(self):
        self.media.recent.clear()
        await self.media.message(self.event(text='听完音频'))
        await self.media.message(self.event(text='暫停聽完'))
        self.assertNotIn(self.key, self.media.waiting)
        self.assertFalse(self.full.jobs)

    async def test_group_requires_mention_and_traditional_video_command_works(self):
        data = self.event(group=123, text='聽完視頻')
        self.assertFalse(await self.media.message(data))
        self.assertFalse(self.full.jobs)
        key = self.fake._mechanism_key(data)
        self.media.remember(key, {'name': 'clip.mp4', 'file_id': 'fixture'})
        data['raw_message'] = '[CQ:at,qq=900] 聽完視頻'
        self.assertTrue(await self.media.message(data))
        await self.full.jobs[key]['task']
        self.assertEqual(self.full.jobs[key]['phase'], '完成')
        self.assertFalse(self.mode.background_busy)

    async def test_disabled_hearing_or_group_not_allowed_does_not_start(self):
        self.cfg['audio_input']['trigger_groups'] = [456]
        await self.full.start(self.event(group=123))
        self.assertFalse(self.full.jobs)
        self.cfg['audio_input']['enabled'] = False
        await self.full.start(self.data)
        self.assertFalse(self.full.jobs)
        self.create.assert_not_awaited()

    async def test_round_deadline_is_preserved_in_full_mode(self):
        async def wait(**kwargs):
            await asyncio.Event().wait()
        self.create.side_effect = wait
        with patch.object(hearing, 'ROUND_SECONDS', 0.01):
            job = await self.finish()
        self.assertIn('暂停', job['phase'])
        self.assertEqual(self.create.await_count, 1)

    async def test_failed_start_delivery_makes_no_download_or_model_calls(self):
        self.send.return_value = False
        job = await self.finish()
        self.assertIn('未执行', job['phase'])
        self.download.assert_not_awaited()
        self.create.assert_not_awaited()

    async def test_no_audio_track_is_reported_without_calling_audio_model(self):
        self.prepare.return_value = {**chunks(0), 'audio_status': '源文件没有音轨'}
        job = await self.finish()
        self.assertIn('暂停', job['phase'])
        self.assertIn('没有音轨', self.send.await_args.args[2])
        self.create.assert_not_awaited()

    async def test_natural_operations_validate_and_have_explicit_scope(self):
        for operation in ('audio_full', 'audio_full_stop', 'audio_full_status'):
            self.assertEqual(validate(operation, {}, 'private_7', False)['action'], operation)
            self.assertTrue(describe({'action': operation, 'args': {}}))
        self.assertIn('不代表视频画面完整看过', describe({'action': 'audio_full', 'args': {}}))

    async def test_uses_cache_populated_while_waiting_for_other_media_work(self):
        await self.media.lock.acquire()
        await self.full.start(self.data)
        await asyncio.sleep(0)
        result = chunks(3)
        self.media.reviews.put(self.key, result, {'hearing': {
            'route': hearing.signature(self.fake, self.cfg), 'seen': [0], 'notes': ['已听0']}})
        self.media.lock.release()
        await self.full.jobs[self.key]['task']
        self.download.assert_not_awaited()
        self.assertEqual(self.create.await_count, 2)

    async def test_full_task_limit_keeps_extra_request_from_starting(self):
        await self.media.lock.acquire()
        for user in range(7, 12):
            data = self.event(user=user)
            self.media.remember(self.fake._mechanism_key(data), {'name': 'clip.mp3', 'file_id': 'fixture'})
            await self.full.start(data)
        self.assertEqual(len(self.full.jobs), 4)
        self.assertIn('任务较多', self.send.await_args.args[2])
        self.media.lock.release()

    async def test_hearing_change_preserves_previous_success_under_old_route(self):
        count = [0]
        async def changed(**kwargs):
            count[0] += 1
            if count[0] == 2:
                self.cfg['audio_input']['model'] = 'next-model'
            return response()
        self.create.side_effect = changed
        job = await self.finish()
        state = job['saved']['state']['hearing']
        self.assertEqual(state['seen'], [0])
        self.assertEqual(len(state['notes']), 1)
        self.assertIn('暂停', job['phase'])
        self.ask.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
