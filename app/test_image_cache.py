import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import bot
from image_cache import ImageCache, TTL_SECONDS


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = [1000000.0]
        self.cache = ImageCache(self.root / 'vision_image_cache', clock=lambda: self.now[0])

    def test_one_hour_expiry_preserves_conversations_persona_and_qq_cache(self):
        protected = [self.root / 'conversations.json', self.root / 'persona.txt', self.root / 'qq-original.png']
        for path in protected:
            path.write_text('keep all text and original files', encoding='utf-8')
        self.cache.put('https://example.com/SECRET', b'image', 'image/png')
        self.now[0] += TTL_SECONDS - 1
        self.assertEqual(self.cache.get('https://example.com/SECRET'), (b'image', 'image/png'))
        self.now[0] += 1
        self.cache.cleanup()
        self.assertEqual(list(self.cache.root.iterdir()), [])
        for path in protected:
            self.assertEqual(path.read_text(), 'keep all text and original files')

    def test_reads_do_not_extend_expiry_and_restart_keeps_absolute_age(self):
        self.cache.put('url', b'image', 'image/jpeg')
        self.now[0] += 3000
        self.cache.get('url')
        reboot = ImageCache(self.cache.root, clock=lambda: self.now[0])
        self.now[0] += 600
        self.assertIsNone(reboot.get('url'))

    def test_metadata_contains_no_url_or_message(self):
        self.cache.put('https://example.com/SECRET_SIGNATURE', b'image', 'image/jpeg')
        meta = next(self.cache.root.glob('*.json')).read_text()
        self.assertNotIn('SECRET', meta)
        self.assertEqual(set(json.loads(meta)), {'created', 'content_type'})

    def test_unknown_files_are_never_deleted(self):
        self.cache.cleanup()
        (self.cache.root / 'user-document.txt').write_text('keep')
        (self.cache.root / 'not-our-cache.json').write_text('keep')
        self.cache.put('url', b'image', 'image/jpeg')
        self.now[0] += TTL_SECONDS
        self.cache.cleanup()
        self.assertEqual((self.cache.root / 'user-document.txt').read_text(), 'keep')
        self.assertEqual((self.cache.root / 'not-our-cache.json').read_text(), 'keep')

    def test_corrupt_metadata_is_removed(self):
        self.cache.put('url', b'image', 'image/jpeg')
        next(self.cache.root.glob('*.json')).write_text('broken')
        self.assertIsNone(self.cache.get('url'))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_bypasses_network_and_keeps_image_bytes(self):
        cache = SimpleNamespace(get=lambda url: (b'cached-image', 'image/png'))
        with patch.object(bot, '_get_vision_image_cache', return_value=cache), \
             patch.object(bot.aiohttp, 'ClientSession') as session, patch.object(bot, 'log'):
            result = await bot._download_vision_image('https://images.example/fixture', 1)
        self.assertEqual(result, (b'cached-image', 'image/png'))
        session.assert_not_called()

    async def test_cleaner_runs_without_reading_or_editing_chat_history(self):
        cache = SimpleNamespace(cleanup=lambda: None)
        history = {'private_1': [{'role': 'assistant', 'content': '记得图片里有一只猫'}]}
        async def stop(_seconds):
            bot.running = False
        with patch.object(bot, 'running', True), patch.object(bot, '_get_vision_image_cache', return_value=cache), \
             patch.object(bot, 'conversations', history), patch.object(bot.asyncio, 'sleep', side_effect=stop):
            await bot.vision_image_cache_runner()
        self.assertEqual(history['private_1'][0]['content'], '记得图片里有一只猫')


if __name__ == '__main__':
    unittest.main()
