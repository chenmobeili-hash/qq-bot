import asyncio
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import audio_reader
import bot
import bilibili_frames
import hearing
from media_reader import MediaError
import media_reader
import qq_media
import video_review
from mechanism_assistant import validate


def chunks(count=2):
    return {'kind': '音频', 'duration': count * 30, 'frames': [], 'audio_chunks': [
        {'start': i * 30, 'end': (i + 1) * 30, 'format': 'mp3', 'base64': base64.b64encode(b'ID3fixture').decode()}
        for i in range(count)]}


def response(summary='有一段人声，背景较安静。', more=False, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(
        content=json.dumps({'summary': summary, 'need_more': more}, ensure_ascii=False)))])


class HearingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'admin_qq': [7], 'audio_input': {'enabled': True, 'api_key': 'FAKE_AUDIO_KEY',
                    'endpoint': 'https://audio.example/v1', 'model': 'audio-fixture', 'trigger_groups': [123]}}
        self.fake = SimpleNamespace(BASE_DIR=Path(self.temp.name), hearing_sync=None,
            _init_deepseek=Mock(), deepseek_client=SimpleNamespace(api_key='FAKE_MAIN_KEY', base_url='https://main.example/v1'),
            current_model='main-fixture', load_config=lambda: self.cfg, log=Mock(),
            is_admin=lambda user, cfg: str(user) == '7', send_msg_to_user=AsyncMock())
        self.create = AsyncMock(return_value=response())
        self.api = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        class Client:
            async def __aenter__(inner):
                return self.api
            async def __aexit__(inner, *args):
                pass
        self.factory = self.enterContext(patch.object(hearing, 'AsyncOpenAI', return_value=Client()))

    async def test_sync_tracks_main_route_and_preserves_independent_configuration(self):
        before = deepcopy(self.cfg)
        self.assertEqual(hearing.route(self.fake, self.cfg), self.cfg['audio_input'])
        switch = hearing.sync(self.fake)
        switch.set_enabled(True)
        current = hearing.route(self.fake, self.cfg)
        self.assertEqual(current['model'], 'main-fixture')
        self.assertEqual(current['trigger_groups'], [123])
        self.fake.current_model = 'next-main'
        self.assertEqual(hearing.route(self.fake, self.cfg)['model'], 'next-main')
        self.assertNotIn('KEY', switch.path.read_text())
        switch.set_enabled(False)
        self.assertEqual(hearing.route(self.fake, self.cfg), before['audio_input'])
        self.assertEqual(self.cfg, before)

    async def test_admin_only_and_global_command_works_in_group_or_private(self):
        for group in (None, 123):
            data = {'sender': {'user_id': 8}, **({'group_id': group} if group else {})}
            await hearing.command(self.fake, data, '听觉同步 开启', self.cfg)
            self.assertFalse(hearing.sync(self.fake).enabled)
            data['sender']['user_id'] = 7
            await hearing.command(self.fake, data, '聽覺同步 開啟', self.cfg)
            self.assertTrue(hearing.sync(self.fake).enabled)
            self.assertIn('所有群和私聊', self.fake.send_msg_to_user.await_args.args[2])
            await hearing.command(self.fake, data, '听觉同步 关闭', self.cfg)
            self.assertFalse(hearing.sync(self.fake).enabled)

    async def test_corrupt_state_and_failed_write_do_not_fallback_or_change_config(self):
        switch = hearing.sync(self.fake)
        with patch.object(Path, 'replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                switch.set_enabled(True)
        self.assertFalse(switch.enabled)
        switch.path.write_text('broken', encoding='utf-8')
        self.fake.hearing_sync = None
        self.assertFalse(hearing.route(self.fake, self.cfg)['enabled'])

    async def test_real_input_audio_payload_is_used_and_only_text_notes_return(self):
        state = {}
        text = await hearing.review(self.fake, chunks(), self.cfg, '说了什么', state, 123)
        request = self.create.await_args.kwargs
        self.assertEqual(request['model'], 'audio-fixture')
        self.assertEqual(request['modalities'], ['text'])
        audio = request['messages'][1]['content'][1]
        self.assertEqual(audio['type'], 'input_audio')
        self.assertEqual(base64.b64decode(audio['input_audio']['data']), b'ID3fixture')
        self.assertEqual(state['seen'], [0])
        self.assertIn('实际听取1/2段', text)
        self.assertNotIn('base64', text)
        self.assertNotIn('FAKE_AUDIO_KEY', repr(self.fake.log.call_args_list))

    async def test_continuation_uses_only_unheard_chunks_and_complete_cache_needs_no_api(self):
        self.create.return_value = response(more=True)
        state = {}
        with patch.object(hearing, 'ROUND_CHUNKS', 1):
            await hearing.review(self.fake, chunks(), self.cfg, state=state)
            await hearing.review(self.fake, chunks(), self.cfg, state=state)
            await hearing.review(self.fake, chunks(), self.cfg, state=state)
        self.assertEqual(state['seen'], [0, 1])
        self.assertEqual(self.create.await_count, 2)

    async def test_partial_failure_keeps_success_and_retry_starts_at_failed_chunk(self):
        self.create.side_effect = [response(more=True), TimeoutError()]
        state = {}
        text = await hearing.review(self.fake, chunks(), self.cfg, state=state)
        self.assertEqual(state['seen'], [0])
        self.assertIn('保留成功进度', text)
        self.create.side_effect = None
        self.create.return_value = response()
        await hearing.review(self.fake, chunks(), self.cfg, state=state)
        self.assertEqual(state['seen'], [0, 1])

    async def test_round_cap_and_followup_only_submit_unheard_segments(self):
        self.create.return_value = response(more=True)
        state = {}
        await hearing.review(self.fake, chunks(7), self.cfg, state=state)
        self.assertEqual(state['seen'], [0, 1, 2, 3, 4])
        await hearing.review(self.fake, chunks(7), self.cfg, state=state)
        self.assertEqual(state['seen'], list(range(7)))
        self.assertEqual(self.create.await_count, 7)

    async def test_no_fake_audio_success_on_invalid_or_truncated_outputs(self):
        for value in (response(finish='length'), response('我需要遵循当前角色设定。'),
                      SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='bare analysis'))])):
            self.create.return_value = value
            state = {}
            with self.assertRaises(MediaError):
                await hearing.review(self.fake, chunks(), self.cfg, state=state)
            self.assertEqual(state['seen'], [])

    async def test_unsupported_endpoint_reports_failure_without_raw_error_or_retry(self):
        class Unsupported(Exception):
            status_code = 400
        self.create.side_effect = Unsupported('PRIVATE ENDPOINT RESPONSE')
        with self.assertRaisesRegex(MediaError, '接口拒绝'):
            await hearing.review(self.fake, chunks(), self.cfg)
        self.assertEqual(self.create.await_count, 1)
        self.assertNotIn('PRIVATE ENDPOINT RESPONSE', repr(self.fake.log.call_args_list))

    async def test_route_changes_discard_pending_result_and_reset_progress(self):
        async def changed(**kwargs):
            self.cfg['audio_input']['model'] = 'next'
            return response()
        self.create.side_effect = changed
        state = {}
        with self.assertRaisesRegex(MediaError, '已变化'):
            await hearing.review(self.fake, chunks(), self.cfg, state=state)
        self.assertEqual(state, {})

    async def test_group_allowlist_blocks_before_request_and_optional_frames_are_opt_in(self):
        with self.assertRaises(MediaError):
            await hearing.review(self.fake, chunks(), self.cfg, group=456)
        self.factory.assert_not_called()
        result = {**chunks(), 'frames': [{'second': 0, 'base64': 'frame'}]}
        await hearing.review(self.fake, result, self.cfg)
        self.assertFalse(any(p['type'] == 'image_url' for p in self.create.await_args.kwargs['messages'][1]['content']))
        self.cfg['audio_input']['include_frames'] = True
        await hearing.review(self.fake, result, self.cfg)
        self.assertTrue(any(p['type'] == 'image_url' for p in self.create.await_args.kwargs['messages'][1]['content']))

    async def test_round_has_hard_deadline_and_cancellation_propagates(self):
        async def stalled(**kwargs):
            await asyncio.sleep(10)
        self.create.side_effect = stalled
        with patch.object(hearing, 'ROUND_SECONDS', .02), self.assertRaises(MediaError):
            await hearing.review(self.fake, chunks(), self.cfg)
        self.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await hearing.review(self.fake, chunks(), self.cfg)


class MediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'audio_input': {'enabled': True, 'api_key': 'FAKE', 'endpoint': 'https://example.com/v1', 'model': 'audio'}}
        for value in (patch.object(bot, 'BASE_DIR', Path(self.temp.name)),
                      patch.object(bot, 'hearing_sync', None, create=True),
                      patch.object(bot, 'load_config', return_value=self.cfg),
                      patch.object(bot, '_effective_vision_config', return_value={'enabled': False}),
                      patch.object(bot, 'log')):
            self.enterContext(value)
        self.media = qq_media.MediaController(bot)

    async def test_private_audio_and_record_need_no_command_group_requires_mention(self):
        for segment in ({'type': 'file', 'data': {'name': 'record.mp3', 'file_id': 'fixture'}},
                        {'type': 'record', 'data': {'file': 'voice.silk', 'url': 'https://example.com/voice'}}):
            with patch.object(self.media, 'read', AsyncMock()) as read:
                await self.media.message({'sender': {'user_id': 7}, 'message_type': 'private', 'message': [segment]})
                read.assert_awaited_once()
            with patch.object(self.media, 'read', AsyncMock()) as read:
                await self.media.message({'sender': {'user_id': 7}, 'group_id': 123, 'message': [segment]})
                read.assert_not_awaited()

    async def test_audio_only_flow_forwards_notes_to_persona_and_continues_without_download(self):
        data = {'sender': {'user_id': 7}, 'message_type': 'private'}
        self.media.remember('private_7', {'name': 'voice.mp3', 'file_id': 'fixture'})
        async def heard(b, result, cfg, question, state, group=None):
            state['notes'] = ['实际听到了人声']
            return '音轨：实际听取1/2段。实际听到了人声'
        with patch.object(self.media, 'download', AsyncMock(return_value=b'ID3fixture')) as download, \
             patch.object(audio_reader, 'prepare', AsyncMock(return_value=chunks())), \
             patch.object(hearing, 'review', side_effect=heard), \
             patch.object(bot, 'ask_deepseek', AsyncMock(return_value='人格回答')) as ask, \
             patch.object(bot, 'send_msg_to_user', AsyncMock()):
            await self.media.read(data)
            self.assertIn('实际听到了人声', ask.await_args.args[1])
            self.assertNotIn('未提供声音', ask.await_args.args[1])
            await self.media.read(data, more=True, audio_only=True)
            self.assertEqual(download.await_count, 1)

    async def test_continue_hearing_is_scoped_and_group_command_requires_mention(self):
        with patch.object(self.media, 'read', AsyncMock()) as read:
            await self.media.message({'sender': {'user_id': 7}, 'group_id': 123, 'raw_message': '继续听'})
            read.assert_not_awaited()
            await self.media.message({'sender': {'user_id': 7}, 'message_type': 'private', 'raw_message': '继续听'})
            self.assertTrue(read.await_args.kwargs['audio_only'])

    async def test_voice_bridge_conversion_uses_its_new_size_or_base64(self):
        data = {'sender': {'user_id': 7}}
        info = {'_kind': 'record', 'file': 'voice.silk', 'size': 5}
        with patch.object(bot, 'call_api', AsyncMock(return_value={'url': 'https://example.com/voice.mp3'})), \
             patch.object(qq_media, 'download_media', AsyncMock(return_value=(b'ID3converted-longer', 'audio/mp3'))) as download:
            self.assertEqual(await self.media.download(data, info), b'ID3converted-longer')
            self.assertEqual(download.await_args.args[3], 0)
        with patch.object(bot, 'call_api', AsyncMock(return_value={'base64': base64.b64encode(b'ID3converted').decode()})):
            self.assertEqual(await self.media.download(data, info), b'ID3converted')

    async def test_audio_cache_counts_bytes_and_expires_without_deleting_notes(self):
        clock = [1000]
        cache = video_review.ReviewCache(lambda: clock[0], limit=100)
        data = chunks()
        data['audio_chunks'][0]['base64'] = 'a' * 150
        with self.assertRaises(MediaError):
            cache.put('private_7', data)
        cache = video_review.ReviewCache(lambda: clock[0])
        notes = {'hearing': {'notes': ['文字记忆']}}
        cache.put('private_7', chunks(), notes)
        clock[0] += 3601
        self.assertIsNone(cache.get('private_7'))
        self.assertEqual(notes['hearing']['notes'], ['文字记忆'])

    async def test_local_bridge_voice_requires_explicit_cache_root_and_valid_audio(self):
        root = Path(self.temp.name) / 'records'
        root.mkdir()
        sound = root / 'converted.mp3'
        sound.write_bytes(b'ID3fixture')
        with self.assertRaisesRegex(MediaError, 'local_record_roots'):
            await qq_media.bridge_record_bytes(bot, str(sound), 1024)
        self.cfg['audio_input']['local_record_roots'] = [str(root)]
        self.assertEqual(await qq_media.bridge_record_bytes(bot, str(sound), 1024), b'ID3fixture')
        outside = root.parent / 'outside.mp3'
        outside.write_bytes(b'ID3outside')
        with self.assertRaises(MediaError):
            await qq_media.bridge_record_bytes(bot, str(outside), 1024)
        sound.write_bytes(b'PRIVATE CONFIGURATION TEXT')
        with self.assertRaises(MediaError):
            await qq_media.bridge_record_bytes(bot, str(sound), 1024)

    async def test_successful_hearing_survives_visual_failure(self):
        with patch.object(hearing, 'allowed', return_value=True), \
             patch.object(hearing, 'review', AsyncMock(return_value='音轨实际听到人声')), \
             patch.object(video_review, '_visual_review', AsyncMock(side_effect=MediaError('图片格式不支持'))):
            result = await video_review.review(bot, {**chunks(), 'frames': [{'second': 0, 'base64': 'f'}]}, self.cfg)
        self.assertIn('仅为实际听觉结果', result)
        self.assertIn('音轨实际听到人声', result)

    async def test_bilibili_capture_extracts_audio_from_same_download(self):
        account = SimpleNamespace(request=AsyncMock(return_value=({'durl': [{'url': 'https://a.bilivideo.com/video'}]}, {})), cookies=lambda: {})
        with patch.object(bilibili_frames, 'download', AsyncMock(return_value=b'video')) as download, \
             patch.object(media_reader, 'capture_video', AsyncMock(return_value=chunks())) as capture:
            result = await bilibili_frames.capture_media(account, 'BV1234567890', 1, 60, self.temp.name, include_audio=True)
        self.assertTrue(result['audio_chunks'])
        download.assert_awaited_once()
        self.assertTrue(capture.await_args.kwargs['include_audio'])

    async def test_bilibili_final_model_gets_notes_not_audio_bytes(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=json.dumps({'kind': 'send', 'reply': '实际画面和人声。'})))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        item = {'frames_loaded': True, 'frames': [{'second': 0, 'base64': 'PRIVATE_FRAME_DATA'}],
                '_audio_chunks': [{'base64': 'PRIVATE_AUDIO_DATA'}], 'audio_status': '提取成功'}
        with patch.object(bot, '_persona_prompt', return_value='角色'), \
             patch.object(bot, '_vision_route_signature', return_value='v'), patch.object(bot, '_init_deepseek'), \
             patch.object(bot, 'deepseek_client', client), \
             patch.object(bot, '_describe_bilibili_frames', AsyncMock(return_value='程序已看见画面并实际听到人声')):
            self.assertEqual(await bot._generate_bilibili(item, '概括', self.cfg), '实际画面和人声。')
        payload = str(create.await_args.kwargs['messages'])
        self.assertNotIn('PRIVATE_AUDIO_DATA', payload)
        self.assertNotIn('PRIVATE_FRAME_DATA', payload)

    def test_natural_language_hearing_actions_keep_admin_boundary(self):
        for key in ('group_123_7', 'private_7'):
            for action in ('audio_read', 'audio_more'):
                validate(action, {}, key, False)
            with self.assertRaises(ValueError):
                validate('hearing_sync_on', {}, key, False)
            validate('hearing_sync_on', {}, key, True)


class DecoderTests(unittest.IsolatedAsyncioTestCase):
    def test_formats_and_silk_are_not_confused(self):
        self.assertEqual(audio_reader.audio_format(b'ID3fixture')[0], 'mp3')
        self.assertEqual(audio_reader.audio_format(b'fLaCfixture')[0], 'flac')
        with self.assertRaisesRegex(MediaError, 'SILK'):
            audio_reader.audio_format(b'#!SILK_V3')
        with self.assertRaises(MediaError):
            audio_reader.audio_format(b'not an audio file')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_real_wav_decode_and_video_audio_cleanup(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b'\0\0' * 16000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = await audio_reader.prepare(buffer.getvalue(), root)
            self.assertEqual(len(result['audio_chunks']), 1)
            self.assertGreater(len(base64.b64decode(result['audio_chunks'][0]['base64'])), 100)
            self.assertEqual(list(root.iterdir()), [])
            path = root / 'generated.mp4'
            await media_reader._process([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:d=1', '-f', 'lavfi', '-i',
                'sine=frequency=440:duration=1', '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', path], 20, 10000)
            body = path.read_bytes()
            path.unlink()
            result = await media_reader.capture_video(body, root, include_audio=True)
            self.assertTrue(result['frames'])
            self.assertTrue(result['audio_chunks'])
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
