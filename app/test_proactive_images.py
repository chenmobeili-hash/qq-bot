import asyncio
import base64
import io
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
import bot
import proactive_images
from proactive import ProactiveMode


class PassiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = 100000
        self.cfg = {'deepseek': {'enabled': True, 'trigger_groups': [123]},
                    'qwen_vl': {'enabled': True, 'trigger_groups': [123]},
                    'proactive': {'groups': [123], 'reply_probability': 1, 'min_messages': 1}}
        self.path = Path(temporary.name) / 'proactive.json'
        self.mode = ProactiveMode(self.path, Mock(), clock=lambda: self.now, roll=lambda: 0)
        self.date = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        for item in (patch.object(bot, '_get_proactive_mode', return_value=self.mode),
                     patch.object(bot, 'load_config', return_value=self.cfg),
                     patch.object(bot, '_effective_vision_config', side_effect=lambda cfg: cfg.get('qwen_vl', {})),
                     patch.object(bot, '_vision_route_signature', return_value='same'),
                     patch.object(bot, '_bot_now', return_value=self.date), patch.object(bot, 'log')):
            self.enterContext(item)

    def observe(self, group=123, url='https://example.com/image?secret=PRIVATE'):
        self.mode.observe(group, '群友', '', self.cfg, image_urls=[url])
        return [dict(row) for row in self.mode.groups[str(group)].messages]

    async def test_pure_image_counts_as_activity_and_vision_is_deferred_until_eligible_tick(self):
        self.observe()
        generate = AsyncMock(return_value=None)
        with patch.object(proactive_images, 'describe', AsyncMock(return_value='一只猫')) as describe:
            await self.mode.tick(lambda: self.cfg, lambda cfg: self.date, lambda: True, generate, AsyncMock())
            describe.assert_not_awaited()
            self.now += 9
            async def selected(group, reason, messages, config):
                return await proactive_images.enrich(bot, group, messages, config)
            await self.mode.tick(lambda: self.cfg, lambda cfg: self.date, lambda: True, selected, AsyncMock(return_value=True))
            describe.assert_awaited_once()
        self.assertEqual(self.mode.image_daily['123']['count'], 1)

    async def test_own_group_description_saved_without_model_url_and_no_repeat_attempt(self):
        messages = self.observe()
        with patch.object(proactive_images, 'describe', AsyncMock(return_value='一只猫')) as describe:
            enriched = await proactive_images.enrich(bot, 123, messages, self.cfg)
            self.assertIn('一只猫', enriched[0]['text'])
            self.assertNotIn('PRIVATE', json.dumps([{'sender': m['sender'], 'text': m['text']} for m in enriched]))
            self.assertNotIn('images', self.mode.groups['123'].messages[0])
            await proactive_images.enrich(bot, 123, [dict(m) for m in self.mode.groups['123'].messages], self.cfg)
            describe.assert_awaited_once()

    async def test_disabled_group_visual_allowlist_and_expired_image_never_download(self):
        messages = self.observe()
        with patch.object(proactive_images, 'describe', AsyncMock()) as describe:
            self.cfg['qwen_vl']['trigger_groups'] = [456]
            await proactive_images.enrich(bot, 123, messages, self.cfg)
            self.cfg['qwen_vl']['trigger_groups'] = [123]
            self.now += 601
            await proactive_images.enrich(bot, 123, messages, self.cfg)
            self.mode.prune_images(self.mode.groups['123'])
            self.assertNotIn('images', self.mode.groups['123'].messages[0])
            self.mode.set_enabled(123, False)
            await proactive_images.enrich(bot, 123, messages, self.cfg)
            describe.assert_not_awaited()

    async def test_failures_and_silence_consume_persisted_visual_quota_without_retries(self):
        self.cfg['proactive']['daily_limit'] = 1
        with patch.object(proactive_images, 'describe', AsyncMock(side_effect=TimeoutError('hidden'))) as describe:
            result = await proactive_images.enrich(bot, 123, self.observe(), self.cfg)
            self.assertIn('没有识别成功', result[0]['text'])
            await proactive_images.enrich(bot, 123, self.observe(url='https://example.com/other'), self.cfg)
            describe.assert_awaited_once()
        restored = ProactiveMode(self.path, Mock())
        self.assertEqual(restored.image_daily['123']['count'], 1)
        self.assertFalse(restored.reserve_image(123, self.cfg, self.date.date().isoformat()))

    async def test_new_message_or_disable_during_vision_prevents_stale_public_reply(self):
        messages = self.observe()
        async def arrived(*args):
            self.mode.observe(123, '群友', '等等', self.cfg, direct=True)
            return '一只猫'
        with patch.object(proactive_images, 'describe', side_effect=arrived):
            self.assertIsNone(await proactive_images.enrich(bot, 123, messages, self.cfg))
        self.assertIn('一只猫', self.mode.groups['123'].messages[0]['text'])
        async def disabled(*args):
            self.mode.set_enabled(123, False)
            return '旧画面'
        with patch.object(proactive_images, 'describe', side_effect=disabled):
            self.assertIsNone(await proactive_images.enrich(bot, 123, self.observe(), self.cfg))

    async def test_cancellation_stops_attempt_without_background_retry(self):
        messages = self.observe()
        with patch.object(proactive_images, 'describe', AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await proactive_images.enrich(bot, 123, messages, self.cfg)
        self.assertTrue(self.mode.groups['123'].messages[0]['image_attempted'])

    async def test_passive_event_supports_structured_only_images_without_triggering_direct_chat(self):
        event = {'group_id': 123, 'raw_message': '', 'sender': {'user_id': 7, 'nickname': '群友'},
                 'message': [{'type': 'image', 'data': {'url': 'https://example.com/image'}}]}
        with patch.object(bot, 'bot_qq', 999), patch.object(bot, 'deepseek_client', None), \
             patch.object(bot, 'describe_images', AsyncMock()) as direct, \
             patch.object(bot, 'send_group_message', AsyncMock()) as send:
            await bot.on_group_message(event)
        direct.assert_not_awaited()
        send.assert_not_awaited()
        self.assertEqual(len(self.mode.groups['123'].messages), 1)
        self.assertEqual(self.mode.groups['123'].messages[0]['images'], ['https://example.com/image'])

    def test_urls_are_validated_deduplicated_and_cq_entities_decoded(self):
        event = {'raw_message': '[CQ:image,url=https://example.com/a?x=1&amp;y=2]',
                 'message': [{'type': 'image', 'data': {'file': 'file:///secret'}},
                             {'type': 'image', 'data': {'url': 'http://127.0.0.1/secret'}}]}
        self.assertEqual(proactive_images.urls(bot, event), ['https://example.com/a?x=1&y=2'])

    def test_direct_images_not_observed_and_disabled_images_not_queued(self):
        self.mode.observe(123, '群友', '', self.cfg, direct=True, image_urls=['https://example.com/a'])
        self.assertEqual(len(self.mode.groups['123'].messages), 0)
        self.cfg['proactive']['images_enabled'] = False
        self.observe()
        self.assertEqual(len(self.mode.groups['123'].messages), 0)

    async def test_quiet_cooldown_and_offline_scheduler_never_enters_vision(self):
        self.observe()
        self.now += 9
        generate = AsyncMock()
        await self.mode.tick(lambda: self.cfg, lambda cfg: self.date.replace(hour=23), lambda: True, generate, AsyncMock())
        await self.mode.tick(lambda: self.cfg, lambda cfg: self.date, lambda: False, generate, AsyncMock())
        self.mode.groups['123'].last_attempt = self.now
        await self.mode.tick(lambda: self.cfg, lambda cfg: self.date, lambda: True, generate, AsyncMock())
        generate.assert_not_awaited()


class VisualTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_webp_sends_actual_later_frames_without_reverse_search(self):
        output = io.BytesIO()
        Image.new('RGB', (16, 16), 'red').save(output, 'WEBP', save_all=True,
            append_images=[Image.new('RGB', (16, 16), 'blue')], lossless=True, duration=100)
        api = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='红色变蓝色'),
                                                                            finish_reason='stop')]))
        cfg = {'qwen_vl': {'enabled': True, 'model': 'main-vision'}}
        client = SimpleNamespace(with_options=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=api))))
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, 'BASE_DIR', Path(directory)), \
             patch.object(bot, '_effective_vision_config', return_value=cfg['qwen_vl']), \
             patch.object(bot, '_vision_route_signature', return_value='same'), \
             patch.object(bot, 'load_config', return_value=cfg), \
             patch.object(bot, '_ensure_qwen_vl_client', AsyncMock(return_value=True)), \
             patch.object(bot, 'qwen_vl_client', client), \
             patch.object(bot, '_download_vision_image', AsyncMock(return_value=(output.getvalue(), 'image/webp'))), \
             patch.object(bot, '_saucenao_search_only', AsyncMock()) as search:
            text = await proactive_images.describe(bot, 'https://example.com/a', cfg)
        self.assertEqual(text, '红色变蓝色')
        search.assert_not_awaited()
        parts = api.await_args.kwargs['messages'][0]['content']
        self.assertEqual(len([p for p in parts if p['type'] == 'image_url']), 2)
        self.assertEqual(api.await_args.kwargs['model'], 'main-vision')

    async def test_download_failure_never_calls_vision(self):
        with patch.object(bot, '_vision_route_signature', return_value='same'), \
             patch.object(bot, '_effective_vision_config', return_value={'enabled': True}), \
             patch.object(bot, '_ensure_qwen_vl_client', AsyncMock(return_value=True)), \
             patch.object(bot, '_download_vision_image', AsyncMock(return_value=None)), \
             patch.object(bot, 'qwen_vl_client') as client:
            self.assertIsNone(await proactive_images.describe(bot, 'https://example.com/a', {}))
            client.with_options.assert_not_called()


if __name__ == '__main__':
    unittest.main()
