"""Offline fake SerpApi server; never uses live keys or uploads to public services."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from PIL import Image

import bot
import direct_vision
import serpapi_lens as lens
import visual_search
import configure_serpapi
from model_decision import DecisionError, inspection_allowed, parse
import test_long_term_memory as existing


def image_bytes(size=(160, 120), **metadata):
    output = io.BytesIO()
    Image.new('RGB', size, '#2c71b7').save(output, format='PNG', **metadata)
    return output.getvalue()


class ConfigTests(unittest.TestCase):
    def test_blank_key_disabled_until_real_config_or_environment_is_supplied(self):
        self.assertFalse(lens.configured({}))
        with patch.dict(os.environ, {'SERPAPI_API_KEY': 'FAKE_ENV_KEY'}):
            self.assertTrue(lens.configured({'serpapi': {'enabled': True}}))
            self.assertEqual(lens.settings({'serpapi': {'enabled': True, 'api_key': 'LOCAL_FAKE'}})['api_key'], 'LOCAL_FAKE')
            self.assertFalse(lens.configured({'serpapi': {'enabled': False}}))

    def test_invalid_proxy_config_or_language_rejected_without_echoing_secrets(self):
        for addition in ({'proxy': 'file:///secret'}, {'proxy': 'http://host?api_key=SECRET'},
                         {'hl': 'x&api_key=SECRET'}, {'timeout_seconds': 'SECRET'}, {'api_key': 'SEC\nRET'}):
            with self.assertRaises(lens.LensError) as raised:
                lens.settings({'serpapi': {'enabled': True, 'api_key': 'FAKE_KEY', **addition}})
            self.assertNotIn('SECRET', str(raised.exception))

    def test_outbound_copy_is_valid_jpeg_and_below_upload_limit(self):
        original = image_bytes((2200, 1200))
        result = lens.prepare(original)
        self.assertLessEqual(len(result), 480000)
        with Image.open(io.BytesIO(result)) as im:
            self.assertEqual(im.format, 'JPEG')
            self.assertLessEqual(max(im.size), 1600)
            self.assertFalse(im.getexif())
        self.assertTrue(original.startswith(b'\x89PNG'))

    def test_invalid_or_oversized_image_does_not_become_an_upload(self):
        for body in (b'not an image', b'', b'x' * (12 * 1024 * 1024 + 1)):
            with self.assertRaises(lens.LensError):
                lens.prepare(body)

    def test_result_normalization_bounded_no_raw_metadata_or_thumbnail_fetch(self):
        result = lens.normalize({'search_metadata': {'api_key': 'PRIVATE_KEY'},
            'exact_matches': [{'title': 'Candidate', 'link': 'https://public.example/item', 'source': 'Source'}],
            'visual_matches': [{'title': str(i), 'link': f'https://public.example/{i}', 'thumbnail': 'https://private.invalid'} for i in range(20)]})
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0]['match'], 'exact')
        self.assertNotIn('api_key', json.dumps(result))
        self.assertNotIn('thumbnail', json.dumps(result))
        self.assertIsNone(lens.public_link('javascript:alert(1)'))
        self.assertIsNone(lens.public_link('https://public.example/?api_key=SECRET'))

    def test_protocol_supports_optional_provider_and_lens_only_config(self):
        config = {'serpapi': {'enabled': True, 'api_key': 'FAKE_KEY'}}
        self.assertTrue(inspection_allowed('search_image', 'private_7', config))
        for provider in ('auto', 'lens', 'saucenao'):
            raw = json.dumps({'kind': 'inspect', 'reply': '', 'action': {'action': 'search_image', 'args': {'image': 1, 'provider': provider}}})
            self.assertEqual(parse(raw, 'private_7', config, False).action['args']['provider'], provider)
        for args in ({'image': 1, 'provider': 'https://evil'}, {'image': 1, 'provider': 'lens', 'url': 'evil'}):
            with self.assertRaises(DecisionError):
                parse(json.dumps({'kind': 'inspect', 'reply': '', 'action': {'action': 'search_image', 'args': args}}), 'private_7', config, False)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.now = Path(self.tmp.name), 1800000000.0
        self.cfg = lens.settings({'serpapi': {'enabled': True, 'api_key': 'FAKE', 'daily_limit': 2}})

    def test_quota_and_cooldown_survive_restart_and_expire_on_wall_clock(self):
        store = lens.Quota(self.root, lambda: self.now)
        self.assertIsNone(store.reserve(self.cfg))
        self.assertEqual(lens.Quota(self.root, lambda: self.now).reserve(self.cfg), 'cooldown')
        self.now += 9
        self.assertIsNone(store.reserve(self.cfg))
        self.now += 9
        self.assertEqual(store.reserve(self.cfg), 'daily_limit')
        self.now += 86401
        self.assertIsNone(lens.Quota(self.root, lambda: self.now).reserve(self.cfg))

    def test_corrupt_quota_preserved_instead_of_reset(self):
        file = self.root / lens.STATE_FILE
        file.write_bytes(b'corrupt private state')
        with self.assertRaises(lens.LensError):
            lens.Quota(self.root).reserve(self.cfg)
        self.assertEqual(file.read_bytes(), b'corrupt private state')


class ConfigWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_preview_apply_backup_and_idempotence_preserve_original_fields(self):
        path = self.root / 'config.yaml'
        before = b'# private comment\r\nadmin_qq: [7]\r\nsecret: FAKE_OLD_KEY\r\n'
        path.write_bytes(before)
        self.assertEqual(configure_serpapi.configure(self.root)['status'], 'preview')
        self.assertEqual(path.read_bytes(), before)
        result = configure_serpapi.configure(self.root, True)
        self.assertEqual(result['status'], 'added')
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual((self.root / result['backup'] / 'config.yaml').read_bytes(), before)
        self.assertEqual(configure_serpapi.configure(self.root, True)['status'], 'preserved')
        self.assertNotIn('FAKE_OLD_KEY', str(result))

    def test_existing_key_proxy_and_enabled_value_never_overwritten(self):
        path = self.root / 'config.yaml'
        before = b'serpapi:\n  enabled: false\n  api_key: MY_FAKE_KEY\n  proxy: http://127.0.0.1:1234\n'
        path.write_bytes(before)
        self.assertEqual(configure_serpapi.configure(self.root, True)['status'], 'preserved')
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_configuration_not_replaced(self):
        path = self.root / 'config.yaml'
        for before in (b'[invalid: yaml', b'[]'):
            path.write_bytes(before)
            with self.assertRaises(Exception):
                configure_serpapi.configure(self.root, True)
            self.assertEqual(path.read_bytes(), before)

    def test_atomic_replace_failure_keeps_original_file_and_backup(self):
        path = self.root / 'config.yaml'
        before = b'admin_qq: [7]\n'
        path.write_bytes(before)
        with patch.object(configure_serpapi.os, 'replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                configure_serpapi.configure(self.root, True)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.serpapi-config-*.tmp')))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'serpapi': {'enabled': True, 'api_key': 'FAKE_SECRET_KEY', 'daily_limit': 20, 'min_interval_seconds': 1}}
        self.calls = []
        self.status, self.upload_result = 200, {'image_id': 'synthetic-image-id'}
        self.search_result = {'search_metadata': {'status': 'Success'}, 'visual_matches': [
            {'title': '测试作品候选', 'link': 'https://public.example/work', 'source': 'Example'}]}
        self.current = True
        self.change_after_upload = False
        self.log = Mock()
        app = web.Application()
        app.router.add_post('/image', self.upload)
        app.router.add_get('/search.json', self.query)
        app.router.add_get('/account.json', self.account)
        app.router.add_get('/redirect-target', self.redirect_target)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        endpoint = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        for name, suffix in (('UPLOAD_URL', '/image'), ('SEARCH_URL', '/search.json'), ('ACCOUNT_URL', '/account.json')):
            self.enterContext(patch.object(lens, name, endpoint + suffix))
        self.enterContext(patch.dict(os.environ, {'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}))

    async def upload(self, request):
        form = await request.post()
        self.calls.append(('upload', form))
        if self.change_after_upload:
            self.current = False
        if self.status == 302:
            raise web.HTTPFound('/redirect-target')
        return web.json_response(self.upload_result, status=self.status)

    async def query(self, request):
        self.calls.append(('search', dict(request.query)))
        if isinstance(self.search_result, str):
            return web.Response(text=self.search_result)
        return web.json_response(self.search_result)

    async def account(self, request):
        self.calls.append(('account', dict(request.query)))
        return web.json_response({'account_email': 'private@example.invalid', 'api_key': 'FAKE_SECRET_KEY', 'total_searches_left': 42})

    async def redirect_target(self, request):
        self.calls.append(('redirect-followed', dict(request.query)))
        return web.json_response({'image_id': 'evil'})

    async def search(self, **kwargs):
        return await lens.search(image_bytes(), self.cfg, self.root, self.log, current=lambda: self.current, **kwargs)

    async def test_real_multipart_upload_then_search_by_image_id(self):
        result = await self.search()
        self.assertEqual(result['status'], 'found')
        self.assertEqual([c[0] for c in self.calls], ['upload', 'search'])
        upload = self.calls[0][1]
        self.assertEqual(upload['api_key'], 'FAKE_SECRET_KEY')
        self.assertEqual(upload['image'].content_type, 'image/jpeg')
        query = self.calls[1][1]
        self.assertEqual(query['engine'], 'google_lens')
        self.assertEqual(query['image_id'], 'synthetic-image-id')
        self.assertNotIn('url', query)
        self.assertNotIn('FAKE_SECRET_KEY', json.dumps(result))
        self.assertNotIn('FAKE_SECRET_KEY', str(self.log.call_args_list))

    async def test_upload_403_is_access_denied_not_invalid_key_guess(self):
        self.status = 403
        self.upload_result = {'error': 'FAKE_SECRET_KEY raw sensitive response'}
        result = await self.search()
        self.assertEqual(result['reason'], 'access_denied')
        self.assertEqual(result['phase'], 'upload')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('FAKE_SECRET_KEY', str(result) + str(self.log.call_args_list))

    async def test_upload_401_and_429_are_distinct(self):
        self.status = 401
        self.assertEqual((await self.search())['reason'], 'authentication')
        with patch.object(lens.Quota, 'reserve', return_value=None):
            self.status = 429
            self.assertEqual((await self.search())['reason'], 'provider_limit')

    async def test_redirect_not_followed_with_key(self):
        self.status = 302
        self.assertEqual((await self.search())['reason'], 'provider_http_302')
        self.assertEqual([c[0] for c in self.calls], ['upload'])

    async def test_missing_image_id_stops_before_paid_search(self):
        self.upload_result = {'message': 'ok'}
        self.assertEqual((await self.search())['reason'], 'invalid_image_id')
        self.assertEqual([c[0] for c in self.calls], ['upload'])

    async def test_generation_expiry_after_upload_stops_before_search(self):
        self.change_after_upload = True
        self.assertEqual((await self.search())['status'], 'expired_or_changed')
        self.assertEqual([c[0] for c in self.calls], ['upload'])

    async def test_empty_results_not_fabricated_and_async_response_not_success(self):
        self.search_result = {'search_metadata': {'status': 'Success'}, 'visual_matches': []}
        self.assertEqual((await self.search())['status'], 'no_verified_result')
        with patch.object(lens.Quota, 'reserve', return_value=None):
            self.search_result = {'search_metadata': {'status': 'Processing'}}
            self.assertEqual((await self.search())['reason'], 'not_completed')

    async def test_invalid_or_oversized_response_is_bounded(self):
        self.search_result = 'not JSON FAKE_SECRET_KEY'
        self.assertEqual((await self.search())['reason'], 'invalid_response')
        with patch.object(lens.Quota, 'reserve', return_value=None):
            self.search_result = 'x' * (lens.MAX_RESPONSE + 1)
            self.assertEqual((await self.search())['reason'], 'response_too_large')

    async def test_local_quota_blocks_before_upload_and_survives_new_instance(self):
        self.cfg['serpapi']['daily_limit'] = 0
        self.assertEqual((await self.search())['status'], 'limited')
        self.assertFalse(self.calls)

    async def test_account_check_uses_no_upload_or_search_and_omits_identity(self):
        result = await lens.check(self.cfg)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['searches_left'], 42)
        self.assertEqual([c[0] for c in self.calls], ['account'])
        self.assertNotIn('private@example', str(result))
        self.assertNotIn('FAKE_SECRET_KEY', str(result))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'serpapi': {'enabled': True, 'api_key': 'FAKE_LENS_KEY'},
                    'saucenao': {'enabled': True, 'api_key': 'FAKE_SAUCE_KEY'}}
        self.bot = type('Fake', (), {})()
        self.bot.BASE_DIR, self.bot.log = self.root, Mock()
        self.bot.load_config = lambda: self.cfg
        self.bot._vision_route_signature = lambda cfg: 'fixed'
        self.bot._saucenao_search_only = AsyncMock(return_value={'ok': True})
        self.bot._saucenao_compose = AsyncMock(return_value='SauceNAO candidate')
        token = direct_vision._turn.set({'scope': 'private_7', 'search_images': [
            {'image': 1, 'uri': 'data:image/png;base64,' + base64.b64encode(image_bytes()).decode(), 'created': __import__('time').time()}]})
        self.addCleanup(direct_vision._turn.reset, token)
        self.enterContext(patch.object(direct_vision, 'valid', return_value=True))
        self.lens = self.enterContext(patch.object(lens, 'search', new_callable=AsyncMock,
            return_value={'status': 'found', 'provider': 'lens', 'results': [{'title': 'Lens candidate'}]}))
        self.enterContext(patch.object(visual_search.image_search, 'check_quota', return_value=(True, '')))
        visual_search._results.clear()
        visual_search._locks.clear()
        visual_search._inflight.clear()

    async def test_auto_prefers_lens_and_explicit_saucenao_works(self):
        result = await visual_search.search(self.bot, 'private_7', {'image': 1}, self.cfg)
        self.assertEqual(result['provider'], 'lens')
        self.bot._saucenao_search_only.assert_not_awaited()
        result = await visual_search.search(self.bot, 'private_7', {'image': 1, 'provider': 'saucenao'}, self.cfg)
        self.assertEqual(result['provider'], 'saucenao')
        self.bot._saucenao_search_only.assert_awaited_once()

    async def test_lens_error_does_not_silently_upload_to_saucenao(self):
        self.lens.return_value = {'status': 'failed', 'reason': 'access_denied', 'provider': 'lens'}
        self.assertEqual((await visual_search.search(self.bot, 'private_7', {'image': 1}, self.cfg))['status'], 'failed')
        self.bot._saucenao_search_only.assert_not_awaited()

    async def test_cache_is_separate_for_provider_and_key(self):
        await visual_search.search(self.bot, 'private_7', {'image': 1}, self.cfg)
        self.assertTrue((await visual_search.search(self.bot, 'private_7', {'image': 1}, self.cfg))['cached'])
        self.lens.assert_awaited_once()
        self.cfg['serpapi']['api_key'] = 'NEW_FAKE_KEY'
        await visual_search.search(self.bot, 'private_7', {'image': 1}, self.cfg)
        self.assertEqual(self.lens.await_count, 2)

    async def test_lens_disabled_is_not_replaced_by_saucenao_when_explicit(self):
        self.cfg['serpapi']['enabled'] = False
        result = await visual_search.search(self.bot, 'private_7', {'image': 1, 'provider': 'lens'}, self.cfg)
        self.assertEqual(result['status'], 'disabled')
        self.lens.assert_not_awaited()
        self.bot._saucenao_search_only.assert_not_awaited()


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.IntegrationTests.setUp

    async def test_recent_image_followup_not_stolen_by_legacy_text_search_prefix(self):
        self.config['deepseek']['trigger_private'] = True
        data = {'message_type': 'private', 'sender': {'user_id': 7}, 'raw_message': '查一下这张图的出处', 'message': []}
        with patch.object(bot, 'bot_qq', 999), patch.object(bot, 'ask_deepseek', new_callable=AsyncMock, return_value='候选来源'), \
             patch.object(visual_search, 'has_recent_input', return_value=True), \
             patch.object(bot, 'web_search', new_callable=AsyncMock) as text_search, \
             patch.object(bot, 'call_api', new_callable=AsyncMock), patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            await bot.on_private_message(data)
            text_search.assert_not_awaited()
            self.assertEqual(bot.ask_deepseek.call_args.args[1], '查一下这张图的出处')
            self.assertIs(bot.ask_deepseek.call_args.args[2], False)

    async def test_recent_input_only_uses_same_scope_unexpired_cache(self):
        now = __import__('time').time()
        direct_vision._images.clear()
        direct_vision._images[(str(self.root), 'private_7')] = [{'created': now}]
        try:
            with patch.object(direct_vision, 'enabled', return_value=True):
                self.assertTrue(visual_search.has_recent_input(bot, 'private_7'))
                self.assertFalse(visual_search.has_recent_input(bot, 'private_8'))
                with patch.object(visual_search.time, 'time', return_value=now + 601):
                    self.assertFalse(visual_search.has_recent_input(bot, 'private_7'))
        finally:
            direct_vision._images.clear()

    async def test_lens_choice_roundtrip_then_main_answers_from_results(self):
        self.config['serpapi'] = {'enabled': True, 'api_key': 'FAKE_LENS_KEY'}
        choice = json.dumps({'kind': 'inspect', 'reply': '', 'action': {'action': 'search_image', 'args': {'image': 1, 'provider': 'lens'}}})
        self.create.side_effect = [existing.response(choice), existing.response(existing.raw([], reply='Lens找到的相关网页提到了测试作品，仍需核对。'))]
        with patch.object(bot.visual_search, 'search', new_callable=AsyncMock, return_value={'status': 'found', 'provider': 'lens', 'results': [{'title': '测试作品', 'url': 'https://public.example'}]}) as query:
            result = await bot.ask_deepseek('private_7', '用谷歌搜搜刚才的图')
        self.assertEqual(query.call_args.args[2], {'image': 1, 'provider': 'lens'})
        self.assertIn('测试作品', result)
        self.assertEqual(self.create.await_count, 2)


if __name__ == '__main__':
    unittest.main()
