import asyncio
import base64
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import bilibili_frames
from bilibili_feed import subtitle_timeline
import media_reader
import qq_media
import video_review
from mechanism_assistant import validate


def response(summary='观察', more=False, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({'summary': summary, 'need_more': more})), finish_reason=finish)])


def video(count=20):
    return {'kind': 'QQ视频', 'duration': 300,
            'frames': [{'second': i * 15, 'base64': 'frame' + str(i)} for i in range(count)]}


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = AsyncMock(return_value=response())
        self.cfg = {'enabled': True, 'model': 'fixture'}
        self.bot = SimpleNamespace(_effective_vision_config=lambda cfg: cfg,
            _ensure_qwen_vl_client=AsyncMock(return_value=True), _vision_route_signature=lambda cfg: 'same',
            load_config=lambda: self.cfg, VISION_TIMEOUT=180, log=Mock(),
            qwen_vl_client=SimpleNamespace(with_options=lambda **kw: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=self.api)))))

    async def test_overview_covers_start_middle_end_and_stops_when_model_satisfied(self):
        state = {}
        text = await video_review.review(self.bot, video(), self.cfg, '猫在干什么', state)
        self.assertEqual(state['seen'], [0, 6, 13, 19])
        self.api.assert_awaited_once()
        parts = self.api.await_args.kwargs['messages'][1]['content']
        uris = [p['image_url']['url'] for p in parts if p['type'] == 'image_url']
        self.assertEqual(uris, ['data:image/jpeg;base64,frame' + str(i) for i in state['seen']])
        self.assertIn('模型实际查看4张', text)
        self.assertIn('已抽取20张', text)
        self.assertIn('285', text)

    async def test_model_can_request_more_but_one_round_never_exceeds_three_pages(self):
        self.api.return_value = response(more=True)
        state = {}
        await video_review.review(self.bot, video(40), self.cfg, state=state)
        self.assertEqual(self.api.await_count, 3)
        self.assertEqual(len(state['seen']), 12)
        sent = []
        for call in self.api.await_args_list:
            sent.extend(p['image_url']['url'] for p in call.kwargs['messages'][1]['content'] if p['type'] == 'image_url')
        self.assertEqual(len(set(sent)), 12)

    async def test_continuation_resumes_unseen_images_and_full_coverage_never_calls_model_again(self):
        state = {}
        await video_review.review(self.bot, video(6), self.cfg, state=state)
        self.assertEqual(len(state['seen']), 4)
        await video_review.review(self.bot, video(6), self.cfg, state=state)
        self.assertEqual(len(state['seen']), 6)
        self.assertEqual(self.api.await_count, 2)
        await video_review.review(self.bot, video(6), self.cfg, state=state)
        self.assertEqual(self.api.await_count, 2)

    async def test_failed_page_keeps_completed_page_and_retry_starts_at_failed_page(self):
        self.api.side_effect = [response('首批', True), TimeoutError()]
        state = {}
        text = await video_review.review(self.bot, video(), self.cfg, state=state)
        self.assertEqual(len(state['seen']), 4)
        self.assertIn('补看未完成', text)
        self.api.side_effect = None
        await video_review.review(self.bot, video(), self.cfg, state=state)
        self.assertEqual(state['seen'], [0, 1, 2, 3, 4, 6, 13, 19])

    async def test_cancellation_propagates_without_committing_failed_page(self):
        self.api.side_effect = asyncio.CancelledError()
        state = {}
        with self.assertRaises(asyncio.CancelledError):
            await video_review.review(self.bot, video(), self.cfg, state=state)
        self.assertEqual(state['seen'], [])

    async def test_route_switch_discards_current_result_even_after_prior_success(self):
        self.api.side_effect = [response(more=True), response()]
        routes = iter(['same', 'same', 'different', 'different'])
        self.bot._vision_route_signature = lambda cfg: next(routes)
        with self.assertRaises(media_reader.MediaError):
            await video_review.review(self.bot, video(), self.cfg, state={})

    async def test_truncation_never_advances_page_and_plain_text_does_not_trigger_autonomy(self):
        self.api.return_value = response(finish='length')
        state = {}
        with self.assertRaises(media_reader.MediaError):
            await video_review.review(self.bot, video(), self.cfg, state=state)
        self.assertEqual(state['seen'], [])
        self.api.return_value.choices[0].finish_reason = 'stop'
        self.api.return_value.choices[0].message.content = '看到红色汽车'
        await video_review.review(self.bot, video(), self.cfg, state=state)
        self.assertEqual(len(state['seen']), 4)

    async def test_new_route_rechecks_actual_images_instead_of_reusing_old_model_notes(self):
        state = {'route': 'old', 'seen': list(range(20)), 'notes': ['旧路由描述']}
        text = await video_review.review(self.bot, video(), self.cfg, state=state)
        self.assertNotIn('旧路由描述', text)
        self.assertEqual(len(state['seen']), 4)

    def test_memory_cache_fixed_expiry_and_global_eviction(self):
        now = [1]
        cache = video_review.ReviewCache(lambda: now[0], limit=150)
        cache.put('group_1_2', video(6))
        now[0] = 3599
        self.assertIsNotNone(cache.get('group_1_2'))
        now[0] = 3602
        self.assertIsNone(cache.get('group_1_2'))
        for i in range(20):
            cache.put('private_' + str(i), video(6))
        self.assertLessEqual(sum(row['bytes'] for row in cache.rows.values()), 150)
        self.assertLessEqual(len(cache.rows), 16)

    def test_subtitles_preserve_times_mark_unknown_and_obey_limit(self):
        text, cut = subtitle_timeline([{'from': 1.5, 'to': 3, 'content': '你好'}, {'content': '未知'}])
        self.assertIn('[1.5–3.0秒]', text)
        self.assertIn('[时间未知]', text)
        self.assertFalse(cut)
        text, cut = subtitle_timeline([{'content': 'a' * 200}], 50)
        self.assertLessEqual(len(text), 50)
        self.assertTrue(cut)

    def test_cdn_hosts_checked_before_every_fetch(self):
        for url in ('http://a.bilivideo.com/v', 'https://evil.example/v',
                    'https://a.bilivideo.com.evil.example/v', 'https://u:p@a.bilivideo.com/v',
                    'https://a.bilivideo.com:444/v'):
            with self.subTest(url=url), self.assertRaises(media_reader.MediaError):
                bilibili_frames.media_url(url)
        self.assertEqual(bilibili_frames.media_url('https://a.bilivideo.com/v?sign=keep'),
                         'https://a.bilivideo.com/v?sign=keep')


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_bilibili_registers_same_actor_cache_and_keeps_text_timeline(self):
        cfg = {'bilibili': {'enabled': True}}
        item = {'bvid': 'BV1xx411c7mD', 'url': 'https://www.bilibili.com/video/BV1xx411c7mD',
                'frames': video()['frames'], 'frames_loaded': True, 'duration_seconds': 300,
                'subtitle': '[1–3秒] 真实字幕', 'transcript_loaded': True}
        reader = SimpleNamespace(resolve=AsyncMock(return_value=item['bvid']), read=AsyncMock(return_value=item))
        controller = qq_media.MediaController(bot)
        with patch.object(bot, '_get_bilibili_feed', return_value=SimpleNamespace(reader=reader)), \
             patch.object(qq_media, 'controller', return_value=controller), \
             patch.object(bot, '_generate_bilibili', AsyncMock(return_value='人格分享')), \
             patch.object(bot, 'send_msg_to_user', AsyncMock()):
            await bot._handle_bilibili_read({'group_id': 1, 'sender': {'user_id': 2}}, item['bvid'], cfg)
        saved = controller.reviews.get('group_1_2')
        self.assertEqual(saved['result']['subtitle'], '[1–3秒] 真实字幕')
        self.assertIs(saved['state'], item['_review_state'])
        self.assertIsNone(controller.reviews.get('group_1_3'))
        self.assertIsNone(controller.reviews.get('private_2'))

    async def test_bilibili_generation_forwards_evidence_without_raw_frame_payload_or_internal_state(self):
        cfg = {'qwen_vl': {'enabled': True}}
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='人格总结'), finish_reason='stop')]))
        client = SimpleNamespace(with_options=lambda **kw: SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create))))
        item = {'frames': video()['frames'], 'frames_loaded': True, 'duration_seconds': 300}
        with patch.object(bot, '_persona_prompt', return_value='人格'), \
             patch.object(bot, '_runtime_prompt', return_value=''), patch.object(bot, '_init_deepseek'), \
             patch.object(bot, 'deepseek_client', client), patch.object(bot, 'current_model', 'main'), \
             patch.object(bot, 'load_config', return_value=cfg), \
             patch.object(bot, '_vision_route_signature', return_value='same'), \
             patch.object(video_review, 'review', AsyncMock(return_value='实际看4张，0秒至285秒，一只猫')) as review:
            text = await bot._generate_bilibili(item, '小猫', cfg)
        self.assertEqual(text, '人格总结')
        review.assert_awaited_once()
        public = json.loads(create.await_args.kwargs['messages'][1]['content'])['video_data']
        self.assertIn('一只猫', public['frame_description'])
        self.assertNotIn('frames', public)
        self.assertNotIn('_review_result', public)
        self.assertNotIn('_review_state', public)

    async def test_new_attachment_during_vision_discards_old_reply(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, 'BASE_DIR', Path(directory)), \
             patch.object(bot, 'load_config', return_value={}), \
             patch.object(bot, '_effective_vision_config', return_value={'enabled': True}), \
             patch.object(bot, 'send_msg_to_user', AsyncMock()) as send, \
             patch.object(bot, 'ask_deepseek', AsyncMock()) as ask, patch.object(bot, 'log'):
            controller = qq_media.MediaController(bot)
            controller.reviews.put('private_2', video(), {})
            async def changed(*args):
                controller.remember('private_2', {'name': 'new.mp4'})
                return '旧视频描述'
            with patch.object(video_review, 'review', side_effect=changed):
                await controller.read({'sender': {'user_id': 2}, 'message_type': 'private'}, more=True)
            ask.assert_not_awaited()
            self.assertIn('旧结果未发布', send.await_args.args[2])

    async def test_private_continue_scoped_to_actor_and_no_redownload(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, 'BASE_DIR', Path(directory)), \
             patch.object(bot, 'load_config', return_value={'qwen_vl': {'enabled': True}}), \
             patch.object(bot, '_effective_vision_config', return_value={'enabled': True}), \
             patch.object(bot, 'send_msg_to_user', AsyncMock()) as send, \
             patch.object(bot, 'ask_deepseek', AsyncMock(return_value='后续人格回复')), patch.object(bot, 'log'):
            controller = qq_media.MediaController(bot)
            controller.reviews.put('private_123', video(), {})
            data = {'sender': {'user_id': 123}, 'message_type': 'private'}
            with patch.object(controller, 'download', AsyncMock()) as download, \
                 patch.object(video_review, 'review', AsyncMock(return_value='后续画面')) as review:
                await controller.read(data, more=True)
                review.assert_awaited_once()
                download.assert_not_awaited()
                await controller.read({**data, 'sender': {'user_id': 124}}, more=True)
                self.assertIn('没有可补看', send.await_args.args[2])
                self.assertEqual(review.await_count, 1)

    async def test_continue_direct_phrase_and_natural_language_action_available_in_both_scopes(self):
        for key in ('group_1_2', 'private_2'):
            validate('video_more', {}, key, False)
        with patch.object(bot, 'bot_qq', 999):
            controller = qq_media.MediaController(bot)
            with patch.object(controller, 'read', AsyncMock()) as read:
                await controller.message({'sender': {'user_id': 2}, 'message_type': 'private',
                                          'raw_message': '继续看视频', 'message': []})
                self.assertTrue(read.await_args.kwargs['more'])

    async def test_bilibili_download_then_local_decode_actual_frames(self):
        account = SimpleNamespace(request=AsyncMock(return_value=({'durl': [{'url': 'https://a.bilivideo.com/v'}]}, {})),
                                  cookies=lambda: {})
        with patch.object(bilibili_frames, 'download', AsyncMock(return_value=b'actual-video')) as download, \
             patch.object(media_reader, 'capture_video', AsyncMock(return_value=video())) as capture:
            frames, status = await bilibili_frames.capture_frames(account, 'BVfixture', 1, 300, 'temp')
        self.assertEqual(len(frames), 20)
        self.assertEqual(capture.await_args.args[0], b'actual-video')
        self.assertEqual(capture.await_args.kwargs['frame_limit'], 40)
        self.assertIn('需视觉模型', status)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_actual_long_video_has_more_samples_and_last_frame_and_cleans_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'long.mp4'
            await media_reader._process([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-f', 'lavfi', '-i', 'testsrc=duration=120:size=32x24:rate=1', '-c:v', 'mpeg4',
                '-pix_fmt', 'yuv420p', '-y', path], 20, 65536)
            result = await media_reader.capture_video(path.read_bytes(), root / 'media', frame_limit=40)
            self.assertGreater(len(result['frames']), 6)
            self.assertGreater(result['frames'][-1]['second'], 115)
            self.assertEqual(list((root / 'media').iterdir()), [])
            self.assertTrue(base64.b64decode(result['frames'][0]['base64']).startswith(b'\xff\xd8'))


if __name__ == '__main__':
    unittest.main()
