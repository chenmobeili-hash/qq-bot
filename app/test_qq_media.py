import asyncio
import base64
import io
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
import bot
import media_reader
import qq_media
from mechanism_assistant import validate


def webp(frames=3):
    images = [Image.new('RGB', (32, 24), color) for color in ('red', 'green', 'blue', 'yellow', 'purple', 'white', 'black', 'orange')[:frames]]
    stream = io.BytesIO()
    images[0].save(stream, 'WEBP', save_all=frames > 1, append_images=images[1:], duration=[80, 120, 160, 80, 80, 80, 80, 80][:frames], lossless=True)
    return stream.getvalue()


class DecoderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    async def test_real_animated_webp_keeps_later_frames_timing_and_cleans_source(self):
        result = await media_reader.prepare_webp(webp(), self.root / 'media')
        self.assertTrue(result['animated'])
        self.assertEqual(result['frames_total'], 3)
        self.assertEqual([frame['second'] for frame in result['frames']], [0, 0.08, 0.2])
        colors = []
        for frame in result['frames']:
            with Image.open(io.BytesIO(base64.b64decode(frame['base64']))) as image:
                colors.append(image.getpixel((10, 10)))
        self.assertGreater(colors[0][0], colors[0][2])
        self.assertGreater(colors[2][2], colors[2][0])
        self.assertEqual(list((self.root / 'media').iterdir()), [])

    async def test_static_webp_becomes_real_jpeg(self):
        result = await media_reader.prepare_webp(webp(1), self.root / 'media')
        self.assertFalse(result['animated'])
        self.assertTrue(base64.b64decode(result['frames'][0]['base64']).startswith(b'\xff\xd8'))

    async def test_longer_webp_samples_first_and_last_and_caps_six(self):
        result = await media_reader.prepare_webp(webp(8), self.root / 'media')
        self.assertEqual(len(result['frames']), 6)
        self.assertEqual((result['frames'][0]['frame'], result['frames'][-1]['frame']), (1, 8))

    async def test_ordinary_image_does_not_start_decoder(self):
        with patch.object(media_reader, '_process', new_callable=AsyncMock) as process:
            self.assertIsNone(await media_reader.prepare_webp(b'ordinary-image', self.root / 'media'))
        process.assert_not_awaited()

    async def test_corrupt_webp_fails_without_leaving_source(self):
        with self.assertRaises(media_reader.MediaError):
            await media_reader.prepare_webp(b'RIFF0000WEBPbroken', self.root / 'media')
        self.assertEqual(list((self.root / 'media').iterdir()), [])

    async def test_cancellation_kills_and_reaps_decoder_process(self):
        process = SimpleNamespace(returncode=None, stdout=SimpleNamespace(read=AsyncMock(side_effect=asyncio.CancelledError())),
                                  kill=Mock(), wait=AsyncMock())
        with patch.object(media_reader.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await media_reader._process(['fixture'], 1, 100)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    def test_stale_cleanup_only_removes_owned_inputs(self):
        root = self.root / 'media'
        owned = root / 'video-abcdefgh'
        owned.mkdir(parents=True)
        (owned / 'input.mp4').write_bytes(b'expired input')
        (root / 'keep-user-document.txt').write_text('keep')
        os.utime(owned / 'input.mp4', (1, 1))
        os.utime(owned, (1, 1))
        media_reader.cleanup(root, now=5000)
        self.assertFalse(owned.exists())
        self.assertEqual((root / 'keep-user-document.txt').read_text(), 'keep')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe must be installed for video decoding')
    async def test_real_mp4_produces_actual_frames_and_deletes_temp_copy(self):
        path = self.root / 'fixture.mp4'
        await media_reader._process([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'lavfi', '-i', 'testsrc=duration=2:size=64x48:rate=10', '-c:v', 'mpeg4', '-pix_fmt', 'yuv420p',
            '-y', path], 15, 65536)
        result = await media_reader.capture_video(path.read_bytes(), self.root / 'media')
        self.assertEqual(len(result['frames']), 6)
        self.assertGreater(result['frames'][-1]['second'], result['frames'][0]['second'])
        self.assertGreater(len({frame['base64'] for frame in result['frames']}), 1)
        self.assertEqual(list((self.root / 'media').iterdir()), [])
        self.assertTrue(path.exists())  # Only the bot's temporary copy is removed.

    async def test_disguised_playlist_is_rejected_before_ffmpeg(self):
        with patch.object(media_reader, '_process', new_callable=AsyncMock) as process:
            with self.assertRaises(media_reader.MediaError):
                await media_reader.capture_video(b'#EXTM3U\nhttp://127.0.0.1/secret', self.root / 'media')
        process.assert_not_awaited()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {key: '' for key in (
            'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')}))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = {'deepseek': {'enabled': True}, 'qwen_vl': {'enabled': True, 'api_key': 'fixture', 'model': 'fixture'}}
        self.media = qq_media.MediaController(bot)
        self.enterContext(patch.object(bot, 'BASE_DIR', self.root))
        self.enterContext(patch.object(bot, 'bot_qq', 999))
        self.enterContext(patch.object(bot, 'load_config', return_value=self.cfg))
        self.enterContext(patch.object(bot, '_effective_vision_config', return_value=self.cfg['qwen_vl']))
        self.send = self.enterContext(patch.object(bot, 'send_msg_to_user', new_callable=AsyncMock))
        self.enterContext(patch.object(bot, 'log'))

    def group(self, user=456, raw='', message=None):
        return {'group_id': 123, 'message_type': 'group', 'sender': {'user_id': user}, 'raw_message': raw,
                'message': message or []}

    async def test_group_video_is_remembered_without_unsolicited_read(self):
        data = self.group(message=[{'type': 'video', 'data': {'url': 'https://example.com/SECRET', 'file': 'clip.mp4'}}])
        with patch.object(self.media, 'read', new_callable=AsyncMock) as read:
            self.assertTrue(await self.media.message(data))
        read.assert_not_awaited()
        self.assertIn('group_123_456', self.media.recent)
        self.assertNotIn('SECRET', str(self.media.status('group_123_456')))

    async def test_wait_is_bound_to_same_group_and_sender(self):
        await self.media.message(self.group(raw='[CQ:at,qq=999] 看视频', message=[{'type': 'at', 'data': {'qq': '999'}}]))
        self.assertIn('group_123_456', self.media.waiting)
        with patch.object(self.media, 'read', new_callable=AsyncMock) as read:
            await self.media.message(self.group(user=457, message=[{'type': 'video', 'data': {'url': 'https://example.com/x'}}]))
            read.assert_not_awaited()
            await self.media.message(self.group(message=[{'type': 'video', 'data': {'url': 'https://example.com/x'}}]))
            read.assert_awaited_once()

    async def test_upload_then_command_reads_own_recent_attachment(self):
        self.media.remember('group_123_456', {'name': 'clip.mp4', 'id': 'one'})
        with patch.object(self.media, 'read', new_callable=AsyncMock) as read:
            await self.media.message(self.group(raw='[CQ:at,qq=999] 看看这个视频', message=[{'type': 'at', 'data': {'qq': '999'}}]))
        read.assert_awaited_once()

    async def test_private_direct_video_and_webp_file_need_no_mention(self):
        for segment in ({'type': 'video', 'data': {'file': 'clip.mp4', 'url': 'https://example.com/clip'}},
                        {'type': 'file', 'data': {'file_name': 'motion.webp', 'file_id': 'w1'}}):
            with self.subTest(segment=segment):
                data = {'message_type': 'private', 'sender': {'user_id': 456}, 'raw_message': '', 'message': [segment]}
                with patch.object(self.media, 'read', new_callable=AsyncMock) as read:
                    self.assertTrue(await self.media.message(data))
                read.assert_awaited_once()
                self.assertIn('private_456', self.media.recent)

    async def test_private_waited_video_is_not_taken_from_group(self):
        data = {'message_type': 'private', 'sender': {'user_id': 456}, 'raw_message': '看视频', 'message': []}
        await self.media.message(data)
        self.assertIn('private_456', self.media.waiting)
        with patch.object(self.media, 'read', new_callable=AsyncMock) as read:
            await self.media.message(self.group(message=[{'type': 'video', 'data': {'url': 'https://example.com/clip'}}]))
            read.assert_not_awaited()
            await self.media.message({**data, 'raw_message': '', 'message': [
                {'type': 'video', 'data': {'url': 'https://example.com/clip'}}]})
            read.assert_awaited_once()

    async def test_private_wrapper_intercepts_before_normal_file_translation(self):
        data = {'message_type': 'private', 'sender': {'user_id': 456}, 'raw_message': '', 'message': [
            {'type': 'file', 'data': {'file_name': 'clip.mp4', 'file_id': 'v1'}}]}
        with patch.object(qq_media, 'controller', return_value=self.media), \
             patch.object(self.media, 'read', new_callable=AsyncMock) as read, \
             patch.object(bot, '_translate_private_file', new_callable=AsyncMock) as translate:
            await bot.on_private_message(data)
        read.assert_awaited_once()
        translate.assert_not_awaited()

    async def test_expired_attachment_is_not_available(self):
        self.media.clock = lambda: 1
        self.media.remember('group_123_456', {'name': 'clip.mp4'})
        self.media.clock = lambda: 602
        self.assertNotIn('已登记你10分钟内的附件', self.media.status('group_123_456'))

    async def test_group_file_download_preserves_group_api_and_uses_no_image_cache(self):
        with patch.object(bot, 'call_api', AsyncMock(return_value={'url': 'https://example.com/SECRET'})) as call, \
             patch.object(bot, '_download_vision_image', AsyncMock(return_value=(b'video', 'video/mp4'))) as download:
            body = await self.media.download(self.group(), {'name': 'clip.mp4', 'id': 'f1', 'size': 5, 'busid': 102})
        self.assertEqual(body, b'video')
        call.assert_awaited_once_with('get_group_file_url', {'group_id': 123, 'file_id': 'f1', 'busid': 102}, timeout=30)
        self.assertEqual(download.await_args.kwargs, {'cache': False, 'max_bytes': media_reader.MAX_VIDEO_BYTES})

    async def test_local_path_is_not_read(self):
        with patch.object(bot, 'call_api', AsyncMock(return_value={'file': str(self.root / 'secrets.txt')})), \
             patch.object(bot, '_download_vision_image', new_callable=AsyncMock) as download:
            with self.assertRaises(media_reader.MediaError):
                await self.media.download({'sender': {'user_id': 456}}, {'name': 'clip.mp4', 'id': 'f1'})
        download.assert_not_awaited()

    async def test_actual_media_result_is_forwarded_to_persona_once(self):
        data = self.group()
        self.media.remember('group_123_456', {'name': 'clip.mp4'})
        result = {'kind': 'QQ视频', 'duration': 1, 'frames': [{'second': 0, 'base64': 'fixture'}]}
        with patch.object(self.media, 'download', AsyncMock(return_value=b'video')), \
             patch.object(media_reader, 'prepare_webp', AsyncMock(return_value=None)), \
             patch.object(media_reader, 'capture_video', AsyncMock(return_value=result)), \
             patch.object(qq_media.video_review, 'review', AsyncMock(return_value='仅采样画面；一只猫走过')), \
             patch.object(bot, 'ask_deepseek', AsyncMock(return_value='人格回复：小猫走过啦')) as ask:
            await self.media.read(data)
        ask.assert_awaited_once()
        self.assertIn('一只猫走过', ask.await_args.args[1])
        self.assertIn('未提供声音', ask.await_args.args[1])
        self.assertEqual(self.send.await_args.args[2], '人格回复：小猫走过啦')

    async def test_visual_route_switch_discards_old_description(self):
        api = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='stale'), finish_reason='stop')]))
        client = SimpleNamespace(with_options=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=api))))
        with patch.object(bot, '_ensure_qwen_vl_client', AsyncMock(return_value=True)), \
             patch.object(bot, '_vision_route_signature', side_effect=['old', 'new']), patch.object(bot, 'qwen_vl_client', client):
            with self.assertRaises(media_reader.MediaError):
                await qq_media.describe_frames(bot, {'kind': 'QQ视频', 'frames': [{'second': 0, 'base64': 'fixture'}]}, self.cfg)

    def test_natural_language_action_is_valid_in_group_and_private(self):
        for scope in ('group_123_456', 'private_456'):
            validate('qq_media_read', {}, scope, False)


if __name__ == '__main__':
    unittest.main()
