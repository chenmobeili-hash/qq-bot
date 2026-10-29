import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import full_hearing
import hearing
import media_reader
import qq_media
import test_full_listening as fixtures
import video_review
from mechanism_assistant import validate
from model_decision import instructions


class FullHearingTests(unittest.IsolatedAsyncioTestCase):
    event = fixtures.FullListeningTests.event
    stop_jobs = fixtures.FullListeningTests.stop_jobs

    def setUp(self):
        fixtures.FullListeningTests.setUp(self)
        self.fake.is_admin = lambda actor, cfg: str(actor) == '7'
        self.enterContext(patch.object(qq_media, 'controller', return_value=self.media))
        self.enterContext(patch.object(bot, 'log', Mock()))

    def enable(self):
        full_hearing.settings(self.fake).set_enabled(True)

    async def run_default(self, data=None):
        data = data or self.data
        await self.media.read(data)
        key = self.fake._mechanism_key(data)
        await self.full.jobs[key]['task']
        return self.full.jobs[key]

    async def test_default_off_and_admin_switch_persists_without_changing_config(self):
        before = deepcopy(self.cfg)
        self.assertFalse(full_hearing.enabled(self.fake))
        await hearing.command(self.fake, self.event(user=8), '完整听力 开启', self.cfg)
        self.assertFalse(full_hearing.enabled(self.fake))
        await hearing.command(self.fake, self.data, '完整聽力 開啟', self.cfg)
        self.assertTrue(full_hearing.enabled(self.fake))
        self.fake.full_hearing_mode = None
        self.assertTrue(full_hearing.enabled(self.fake))
        self.assertEqual(before, self.cfg)
        text = (self.fake.BASE_DIR / 'full_hearing_mode.json').read_text(encoding='utf-8')
        self.assertNotIn('FAKE_KEY', text)
        self.create.assert_not_awaited()

    async def test_status_is_readable_by_ordinary_user_without_writing(self):
        await hearing.command(self.fake, self.event(user=8), '全面聽力 狀態', self.cfg)
        self.assertIn('关闭', self.send.await_args.args[2])
        self.assertFalse((self.fake.BASE_DIR / 'full_hearing_mode.json').exists())

    async def test_atomic_switch_write_failure_keeps_off(self):
        with patch.object(Path, 'replace', side_effect=OSError('disk')):
            await hearing.command(self.fake, self.data, '完整听力 开启', self.cfg)
        self.assertFalse(full_hearing.enabled(self.fake))
        self.assertIn('未保存', self.send.await_args.args[2])

    async def test_corrupt_mode_file_does_not_silently_fall_back_to_partial_audio(self):
        (self.fake.BASE_DIR / 'full_hearing_mode.json').write_text('broken', encoding='utf-8')
        await self.media.read(self.data)
        self.assertIn('状态文件异常', self.send.await_args.args[2])
        self.create.assert_not_awaited()

    async def test_plain_audio_read_automatically_hears_all(self):
        self.enable()
        job = await self.run_default()
        self.assertTrue(job['automatic'])
        self.assertEqual(job['phase'], '完成')
        self.assertEqual(self.create.await_count, 12)

    async def test_plain_private_upload_gets_full_audio_without_extra_command(self):
        self.enable()
        data = {**self.data, 'message': [{'type': 'file', 'data': {'name': 'clip.mp3', 'file_id': 'fixture'}}]}
        self.assertTrue(await self.media.message(data))
        await self.full.jobs[self.key]['task']
        self.assertEqual(self.create.await_count, 12)

    async def test_unmentioned_group_media_is_still_not_read(self):
        self.enable()
        data = self.event(group=123)
        data['message'] = [{'type': 'record', 'data': {'file': 'voice.silk'}}]
        self.assertTrue(await self.media.message(data))
        self.assertFalse(self.full.jobs)
        self.create.assert_not_awaited()

    async def test_video_default_combines_sampled_visuals_and_complete_audio(self):
        self.enable()
        self.media.remember(self.key, {'name': 'clip.mp4', 'file_id': 'fixture'})
        self.fake._effective_vision_config = lambda cfg: {'enabled': True}
        result = {**fixtures.chunks(), 'kind': 'QQ视频', 'frames': [{'second': 0, 'base64': 'frame'}]}
        with patch.object(media_reader, 'capture_video', AsyncMock(return_value=result)) as capture, \
             patch.object(video_review, '_visual_review', AsyncMock(return_value='画面采样：出现一只猫。')) as visual:
            job = await self.run_default()
        self.assertTrue(capture.await_args.kwargs['include_audio'])
        visual.assert_awaited_once()
        self.assertEqual(self.create.await_count, 12)
        prompt = self.ask.await_args.args[1]
        self.assertIn('出现一只猫', prompt)
        self.assertIn('12/12段', prompt)
        self.assertIn('画面仍只是采样', prompt)
        self.assertEqual(job['phase'], '完成')

    async def test_visual_failure_does_not_discard_complete_hearing(self):
        self.enable()
        self.media.remember(self.key, {'name': 'clip.mp4', 'file_id': 'fixture'})
        self.fake._effective_vision_config = lambda cfg: {'enabled': True}
        result = {**fixtures.chunks(), 'frames': [{'second': 0, 'base64': 'frame'}]}
        with patch.object(media_reader, 'capture_video', AsyncMock(return_value=result)), \
             patch.object(video_review, '_visual_review', AsyncMock(side_effect=media_reader.MediaError('visual fail'))):
            job = await self.run_default()
        self.assertEqual(job['phase'], '完成')
        self.assertIn('仅为实际听觉结果', self.ask.await_args.args[1])

    async def test_real_record_event_calls_bridge_get_record_then_hears_all(self):
        self.enable()
        self.fake.call_api = AsyncMock(return_value={'base64': base64.b64encode(b'ID3converted').decode()})
        actual_download = qq_media.MediaController.download.__get__(self.media)
        with patch.object(self.media, 'download', actual_download):
            await self.media.message({**self.data, 'message': [{'type': 'record', 'data': {'file': 'voice.silk'}}]})
            await self.full.jobs[self.key]['task']
        self.fake.call_api.assert_awaited_once_with('get_record', {'file': 'voice.silk', 'out_format': 'mp3'}, timeout=30)
        self.assertEqual(self.create.await_count, 12)

    async def test_record_conversion_failure_never_claims_heard(self):
        self.enable()
        self.fake.call_api = AsyncMock(return_value={})
        actual_download = qq_media.MediaController.download.__get__(self.media)
        with patch.object(self.media, 'download', actual_download):
            await self.media.message({**self.data, 'message': [{'type': 'record', 'data': {'file': 'voice.silk'}}]})
            await self.full.jobs[self.key]['task']
        self.assertIn('暂停', self.full.jobs[self.key]['phase'])
        self.assertIn('接入层未提供', self.send.await_args.args[2])
        self.create.assert_not_awaited()

    async def test_switching_mode_off_cancels_automatic_job_preserving_completed_chunk(self):
        self.enable()
        reached = asyncio.Event()
        count = [0]
        async def wait(**kwargs):
            count[0] += 1
            if count[0] == 1:
                return fixtures.response()
            reached.set()
            await asyncio.Event().wait()
        self.create.side_effect = wait
        await self.media.read(self.data)
        await reached.wait()
        await hearing.command(self.fake, self.data, '完整听力 关闭', self.cfg)
        task = self.full.jobs[self.key]['task']
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.media.reviews.get(self.key)['state']['hearing']['seen'], [0])
        self.assertFalse(full_hearing.enabled(self.fake))

    async def test_switching_mode_off_does_not_cancel_explicit_hear_all_request(self):
        self.enable()
        reached, release = asyncio.Event(), asyncio.Event()
        async def wait(**kwargs):
            reached.set()
            await release.wait()
            return fixtures.response()
        self.create.side_effect = wait
        await self.full.start(self.data)
        await reached.wait()
        await hearing.command(self.fake, self.data, '完整听力 关闭', self.cfg)
        self.assertFalse(self.full.jobs[self.key]['task'].done())
        release.set()
        await self.full.jobs[self.key]['task']
        self.assertEqual(self.create.await_count, 12)

    async def test_inline_bili_review_uses_all_segments_and_preserves_visuals(self):
        self.enable()
        result = {**fixtures.chunks(), 'kind': 'B站视频', 'frames': [{'second': 0, 'base64': 'frame'}]}
        self.fake._effective_vision_config = lambda cfg: {'enabled': True}
        with patch.object(video_review, '_visual_review', AsyncMock(return_value='画面观察')):
            text = await video_review.review(self.fake, result, self.cfg, '概括', {}, group=123)
        self.assertEqual(self.create.await_count, 12)
        self.assertIn('画面观察', text)
        self.assertIn('12/12段', text)

    async def test_inline_failure_pauses_instead_of_returning_partial_success(self):
        self.enable()
        self.create.side_effect = [fixtures.response(), TimeoutError()]
        result = {**fixtures.chunks(), 'frames': [{'second': 0, 'base64': 'frame'}]}
        state = {}
        with self.assertRaises(media_reader.MediaError):
            await video_review.review(self.fake, result, self.cfg, state=state)
        self.assertEqual(state['hearing']['seen'], [0])

    async def test_inline_stop_is_scoped_and_waits_for_inflight_return(self):
        self.enable()
        reached, release = asyncio.Event(), asyncio.Event()
        count = [0]
        async def wait(**kwargs):
            count[0] += 1
            if count[0] == 1:
                return fixtures.response()
            reached.set()
            await release.wait()
            return fixtures.response()
        self.create.side_effect = wait
        cfg = {**self.cfg, '_media_key': self.key}
        state = {}
        task = asyncio.create_task(full_hearing.review(self.fake, fixtures.chunks(), cfg, state=state))
        await reached.wait()
        await self.full.control(self.event(user=8), 'stop')
        self.assertFalse(task.done())
        self.full.jobs[self.key] = {'phase': '旧完成', 'task': SimpleNamespace(done=lambda: True)}
        await self.full.control(self.data, 'status')
        self.assertIn('1/12段', self.send.await_args.args[2])
        self.assertNotIn('旧完成', self.send.await_args.args[2])
        await self.full.control(self.data, 'stop')
        release.set()
        with self.assertRaisesRegex(media_reader.MediaError, '停止'):
            await task
        self.assertEqual(state['seen'], [0])
        self.assertFalse(full_hearing.settings(self.fake).active)

    async def test_inline_mode_off_signal_does_not_cancel_parent_runner_task(self):
        self.enable()
        async def off(**kwargs):
            full_hearing.settings(self.fake).set_enabled(False)
            return fixtures.response()
        self.create.side_effect = off
        with self.assertRaisesRegex(media_reader.MediaError, '停止'):
            await full_hearing.review(self.fake, fixtures.chunks(), self.cfg)
        self.assertEqual(asyncio.current_task().cancelling(), 0)

    async def test_inline_cache_expiry_stops_before_committing_stale_segment(self):
        self.enable()
        result = fixtures.chunks()
        self.media.reviews.put(self.key, result, {})
        async def expire(**kwargs):
            self.clock[0] += 3601
            return fixtures.response()
        self.create.side_effect = expire
        state = {}
        with self.assertRaisesRegex(media_reader.MediaError, '缓存'):
            await full_hearing.review(self.fake, result, {**self.cfg, '_media_key': self.key}, state=state)
        self.assertEqual(state['seen'], [])

    async def test_global_mode_does_not_override_group_allowlist(self):
        self.enable()
        self.cfg['audio_input']['trigger_groups'] = [456]
        event = self.event(group=123)
        key = self.fake._mechanism_key(event)
        self.media.remember(key, {'name': 'clip.mp3', 'file_id': 'fixture'})
        await self.media.read(event)
        self.assertFalse(self.full.jobs)
        self.create.assert_not_awaited()

    async def test_mode_off_keeps_existing_one_round_behavior(self):
        await self.media.read(self.data)
        self.assertFalse(self.full.jobs)
        self.assertEqual(self.create.await_count, 1)

    async def test_natural_mode_operations_are_distinct_and_admin_only(self):
        for operation in ('full_hearing_on', 'full_hearing_off'):
            with self.assertRaises(ValueError):
                validate(operation, {}, 'private_8', False)
            self.assertEqual(validate(operation, {}, 'private_7', True)['action'], operation)
        self.assertEqual(validate('full_hearing_status', {}, 'private_8', False)['action'], 'full_hearing_status')
        self.assertIn('full_hearing_on', instructions('private_7', self.cfg, True))

    async def test_mentioned_group_voice_message_is_fully_heard(self):
        self.enable()
        data = self.event(group=123, text='[CQ:at,qq=900]')
        data['message'] = [{'type': 'record', 'data': {'file': 'voice.silk'}}]
        await self.media.message(data)
        key = self.fake._mechanism_key(data)
        await self.full.jobs[key]['task']
        self.assertEqual(self.create.await_count, 12)
        self.assertEqual(self.full.jobs[key]['phase'], '完成')

    async def test_actual_bili_generation_finishes_audio_before_main_summary(self):
        self.enable()
        result = fixtures.chunks()
        item = {'frames_loaded': True, 'frames': [{'second': 0, 'base64': 'PRIVATE_FRAME'}],
                '_audio_chunks': result['audio_chunks'], 'audio_status': '已提取', 'duration_seconds': 360}
        create_main = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=json.dumps({'kind': 'send', 'reply': '看到了猫，也听到了人声。'})))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_main)))
        with patch.multiple(bot, BASE_DIR=self.fake.BASE_DIR, load_config=lambda: self.cfg,
                _effective_vision_config=lambda cfg: {'enabled': True}, _vision_route_signature=lambda cfg: 'visual',
                full_hearing_mode=full_hearing.settings(self.fake), create=True), \
             patch.object(bot, '_persona_prompt', return_value='人格'), \
             patch.object(bot, '_runtime_prompt', return_value=''), \
             patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), \
             patch.object(video_review, '_visual_review', AsyncMock(return_value='采样画面：猫')):
            text = await bot._generate_bilibili(item, '看看', self.cfg)
        self.assertIn('人声', text)
        self.assertEqual(self.create.await_count, 12)
        create_main.assert_awaited_once()
        prompt = str(create_main.await_args.kwargs['messages'])
        self.assertIn('12/12段', prompt)
        self.assertIn('采样画面', prompt)
        self.assertNotIn('PRIVATE_FRAME', prompt)

    async def test_bili_partial_failure_message_does_not_claim_nothing_was_read(self):
        reader = SimpleNamespace(resolve=AsyncMock(return_value='BV1234567890'), read=AsyncMock(return_value={
            'frames_loaded': True, 'frames': [{'second': 0, 'base64': 'frame'}], 'url': 'https://example.com'}))
        with patch.object(bot, '_get_bilibili_feed', return_value=SimpleNamespace(reader=reader)), \
             patch.object(bot, '_generate_bilibili', AsyncMock(side_effect=media_reader.MediaError('听觉请求超时'))), \
             patch.object(bot, 'send_msg_to_user', self.send):
            await bot._handle_bilibili_read(self.data, 'BV1234567890', self.cfg)
        self.assertIn('不能算完整听取', self.send.await_args.args[2])
        self.assertNotIn('我没有读到内容', self.send.await_args.args[2])

    async def test_cached_bili_continuation_keeps_visuals_even_with_an_older_audio_attachment(self):
        self.enable()
        self.fake._effective_vision_config = lambda cfg: {'enabled': True}
        result = {**fixtures.chunks(3), 'kind': 'B站视频', 'frames': [{'second': 0, 'base64': 'frame'}]}
        self.media.reviews.put(self.key, result, {})
        with patch.object(video_review, '_visual_review', AsyncMock(return_value='B站画面')) as visual:
            await self.media.read(self.data, more=True)
            await self.full.jobs[self.key]['task']
        visual.assert_awaited_once()
        self.download.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
