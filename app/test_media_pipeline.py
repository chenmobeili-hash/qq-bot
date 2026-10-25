import asyncio
import base64
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
import bot
import qq_media


def webp(animated):
    stream = io.BytesIO()
    first = Image.new('RGB', (16, 16), 'red')
    first.save(stream, 'WEBP', save_all=animated, append_images=[Image.new('RGB', (16, 16), 'blue')] if animated else [],
               duration=100, lossless=True)
    return stream.getvalue()


class ImagePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(bot, 'BASE_DIR', Path(temporary.name)))
        self.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='静态图片描述'), finish_reason='stop')]))
        self.cfg = {'qwen_vl': {'enabled': True, 'model': 'fixture'}}
        for item in (patch.object(bot, 'load_config', return_value=self.cfg),
                     patch.object(bot, '_effective_vision_config', return_value=self.cfg['qwen_vl']),
                     patch.object(bot, '_ensure_qwen_vl_client', AsyncMock(return_value=True)),
                     patch.object(bot, 'qwen_vl_client', SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))),
                     patch.object(bot, '_vision_route_signature', return_value='same'),
                     patch.object(bot, '_saucenao_search_only', AsyncMock(return_value=None)),
                     patch.object(bot, '_saucenao_compose', AsyncMock(return_value=None)), patch.object(bot, 'log')):
            self.enterContext(item)

    async def test_animated_image_enters_multiframe_route_not_single_frame_or_search(self):
        with patch.object(bot, '_download_vision_image', AsyncMock(return_value=(webp(True), 'application/octet-stream'))), \
             patch.object(qq_media, 'describe_frames', AsyncMock(return_value='动态WebP：读取2帧，红色变为蓝色')) as describe:
            result = await bot.describe_images(['https://example.com/fixture'])
        self.assertIn('红色变为蓝色', result)
        self.assertEqual(len(describe.await_args.args[1]['frames']), 2)
        self.create.assert_not_awaited()
        bot._saucenao_search_only.assert_not_awaited()

    async def test_static_webp_sends_jpeg_in_existing_image_route(self):
        with patch.object(bot, '_download_vision_image', AsyncMock(return_value=(webp(False), 'image/webp'))):
            self.assertIn('静态图片描述', await bot.describe_images(['https://example.com/fixture']))
        uri = self.create.await_args.kwargs['messages'][0]['content'][1]['image_url']['url']
        self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
        self.assertTrue(base64.b64decode(uri.split(',', 1)[1]).startswith(b'\xff\xd8'))

    async def test_frame_request_uses_effective_synchronized_model_and_timestamps(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='画面发生变化'), finish_reason='stop')]))
        client = SimpleNamespace(with_options=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        with patch.object(bot, 'qwen_vl_client', client), \
             patch.object(bot, '_effective_vision_config', return_value={'enabled': True, 'model': 'synchronized-main'}):
            result = await qq_media.describe_frames(bot, {'kind': '动态WebP', 'frames_total': 2, 'duration': 1,
                'frames': [{'second': 0, 'base64': 'one'}, {'second': 0.5, 'base64': 'two'}]}, self.cfg)
        self.assertIn('读取2/2帧', result)
        self.assertEqual(create.await_args.kwargs['model'], 'synchronized-main')
        parts = create.await_args.kwargs['messages'][0]['content']
        self.assertEqual(len([part for part in parts if part['type'] == 'image_url']), 2)
        self.assertIn('0.5秒', str(parts))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_stream_size_limit_rejects_before_cache_write(self):
        for headers in ({'Content-Length': '8'}, {}):
            with self.subTest(headers=headers):
                cache = SimpleNamespace(get=Mock(), put=Mock())
                async def chunks(_):
                    yield b'12345678'
                response = SimpleNamespace(status=200, headers=headers, content=SimpleNamespace(iter_chunked=chunks))
                class Context:
                    async def __aenter__(self):
                        return response
                    async def __aexit__(self, *args):
                        pass
                def session(**kwargs):
                    class Session(Context):
                        async def __aenter__(self):
                            return self
                        async def __aexit__(self, *args):
                            await kwargs['connector'].close()
                        def get(self, *args, **options):
                            return Context()
                    return Session()
                with patch.object(bot.aiohttp, 'ClientSession', side_effect=session), \
                     patch.object(bot, '_get_vision_image_cache', return_value=cache), patch.object(bot, 'log'):
                    self.assertIsNone(await bot._download_vision_image('https://example.com/fixture', 1, cache=False, max_bytes=4))
                cache.get.assert_not_called()
                cache.put.assert_not_called()


if __name__ == '__main__':
    unittest.main()
