import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import direct_vision as direct
import visual_search as search
from model_decision import DecisionError, parse, instructions
import test_long_term_memory as existing


def raw(image=1, **extra):
    return json.dumps({'kind': 'inspect', 'reply': '', 'action': {'action': 'search_image', 'args': {'image': image, **extra}}})


class ProtocolTests(unittest.TestCase):
    def test_enabled_search_is_typed_and_cannot_accept_url_path_or_other_target(self):
        cfg = {'saucenao': {'enabled': True, 'api_key': 'FAKE_KEY'}}
        self.assertEqual(parse(raw(), 'private_7', cfg, False).action['args'], {'image': 1})
        self.assertEqual(parse(raw(2), 'group_1_7', cfg, False).kind, 'inspect')
        for value in (raw(True), raw(0), raw(7), raw('1'), raw(url='https://evil'), raw(scope='private_8')):
            with self.assertRaises(DecisionError):
                parse(value, 'private_7', cfg, False)
        with self.assertRaises(DecisionError):
            parse(raw(), 'private_7', {}, False)
        self.assertIn('上传所选图片', instructions('private_7', cfg, False))


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1800000000.0
        self.cfg = {'saucenao': {'enabled': True, 'api_key': 'FAKE_KEY', 'debug_save_image': True}}
        self.fake = SimpleNamespace(BASE_DIR=self.root, load_config=lambda: self.cfg,
            _vision_route_signature=lambda cfg: 'route', log=Mock(),
            _saucenao_search_only=AsyncMock(return_value={'ok': True}),
            _saucenao_compose=AsyncMock(return_value='相似度93%，候选画师：测试画师；来源：https://public.example/work'))
        self.turn = {'scope': 'private_7', 'search_images': [{'image': 1,
            'uri': 'data:image/png;base64,' + base64.b64encode(b'cached-image').decode(), 'created': self.now - 30}]}
        token = direct._turn.set(self.turn)
        self.addCleanup(direct._turn.reset, token)
        search._results.clear()
        search._locks.clear()
        search._inflight.clear()
        self.enterContext(patch.object(search.time, 'time', side_effect=lambda: self.now))
        self.enterContext(patch.object(direct, 'valid', return_value=True))
        self.enterContext(patch.object(search.image_search, 'check_quota', return_value=(True, '')))

    async def test_followup_uploads_cached_bytes_not_new_download_and_disables_debug_files(self):
        result = await search.search(self.fake, 'private_7', {'image': 1}, self.cfg)
        self.assertEqual(result['status'], 'found')
        args = self.fake._saucenao_search_only.call_args.args
        self.assertEqual(args[0], b'cached-image')
        self.assertIs(args[1]['saucenao']['debug_save_image'], False)
        self.assertTrue(self.cfg['saucenao']['debug_save_image'])

    async def test_expired_after_ten_minutes_does_not_upload_or_extend_image_age(self):
        self.now += 571
        value = await search.search(self.fake, 'private_7', {'image': 1}, self.cfg)
        self.assertEqual(value['status'], 'unavailable')
        self.fake._saucenao_search_only.assert_not_awaited()
        self.assertEqual(self.turn['search_images'][0]['created'], self.now - 601)

    async def test_no_cross_user_group_or_image_index(self):
        for scope, index in (('private_8', 1), ('group_1_7', 1), ('private_7', 2)):
            result = await search.search(self.fake, scope, {'image': index}, self.cfg)
            self.assertEqual(result['status'], 'unavailable')
        self.fake._saucenao_search_only.assert_not_awaited()

    async def test_disabled_feature_never_uploads(self):
        self.cfg['saucenao']['enabled'] = False
        self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'disabled')
        self.fake._saucenao_search_only.assert_not_awaited()

    async def test_quota_limit_is_distinct_from_no_result(self):
        with patch.object(search.image_search, 'check_quota', return_value=(False, 'limit')):
            self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'limited')
        self.fake._saucenao_search_only.assert_not_awaited()

    async def test_repeated_followup_reuses_result_without_repeat_upload(self):
        await search.search(self.fake, 'private_7', {'image': 1}, self.cfg)
        self.now += 60
        again = await search.search(self.fake, 'private_7', {'image': 1}, self.cfg)
        self.assertTrue(again['cached'])
        self.fake._saucenao_search_only.assert_awaited_once()

    async def test_empty_or_failed_search_does_not_claim_source_found(self):
        self.fake._saucenao_compose.return_value = None
        self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'no_verified_result')
        search._results.clear()
        self.fake._saucenao_search_only.side_effect = TimeoutError('private details')
        self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'failed')
        self.assertNotIn('private details', str(self.fake.log.call_args_list))

    async def test_route_change_discards_result(self):
        async def changed(*args):
            self.fake._vision_route_signature = lambda cfg: 'new'
            return {'ok': True}
        self.fake._saucenao_search_only.side_effect = changed
        self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'expired_or_changed')

    async def test_selected_image_expiring_during_search_is_not_hidden_by_a_newer_image(self):
        self.turn['search_images'].append({'image': 2, 'uri': 'data:image/png;base64,bmV3', 'created': self.now})
        async def delayed(*args):
            self.now += 571
            return {'ok': True}
        self.fake._saucenao_search_only.side_effect = delayed
        value = await search.search(self.fake, 'private_7', {'image': 1}, self.cfg)
        self.assertEqual(value['status'], 'expired_or_changed')

    async def test_concurrent_search_does_not_duplicate_upload(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def pending(*args):
            started.set()
            await release.wait()
            return {'ok': True}
        self.fake._saucenao_search_only.side_effect = pending
        first = asyncio.create_task(search.search(self.fake, 'private_7', {'image': 1}, self.cfg))
        await started.wait()
        self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'busy')
        release.set()
        await first
        self.fake._saucenao_search_only.assert_awaited_once()

    async def test_cancellation_keeps_busy_until_real_worker_finishes(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def pending(*args):
            started.set()
            await release.wait()
            return {'ok': True}
        self.fake._saucenao_search_only.side_effect = pending
        first = asyncio.create_task(search.search(self.fake, 'private_7', {'image': 1}, self.cfg))
        await started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await search.search(self.fake, 'private_7', {'image': 1}, self.cfg))['status'], 'busy')
        release.set()
        await search._inflight[str(self.root)]


class ModelTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.IntegrationTests.setUp

    async def test_model_inspection_searches_then_answers_using_real_result(self):
        self.config['saucenao'] = {'enabled': True, 'api_key': 'FAKE_KEY'}
        self.create.side_effect = [existing.response(raw()), existing.response(existing.raw([], reply='搜到了一个相似作品，可能出自测试画师。'))]
        with patch.object(bot.visual_search, 'search', new_callable=AsyncMock, return_value={'status': 'found', '资料': '候选：测试画师'}) as query:
            reply = await bot.ask_deepseek('private_7', '搜搜刚才那张图的画师')
        self.assertIn('测试画师', reply)
        query.assert_awaited_once()
        self.assertEqual(query.call_args.args[2], {'image': 1})
        self.assertEqual(self.create.await_count, 2)
        self.assertIn('候选：测试画师', str(self.create.call_args.kwargs['messages']))

    async def test_model_can_choose_to_answer_without_search(self):
        self.config['saucenao'] = {'enabled': True, 'api_key': 'FAKE_KEY'}
        self.create.return_value = existing.response(existing.raw([], reply='截图右侧写着你好。'))
        with patch.object(bot.visual_search, 'search', new_callable=AsyncMock) as query:
            await bot.ask_deepseek('private_7', '读一下截图的字，不要搜图')
        query.assert_not_awaited()
        self.create.assert_awaited_once()

    async def test_repeated_search_request_same_turn_is_rejected_before_second_upload(self):
        self.config['saucenao'] = {'enabled': True, 'api_key': 'FAKE_KEY'}
        self.create.return_value = existing.response(raw())
        with patch.object(bot.visual_search, 'search', new_callable=AsyncMock, return_value={'status': 'no_verified_result'}) as query:
            reply = await bot.ask_deepseek('private_7', '搜搜看')
        query.assert_awaited_once()
        self.assertIn('没有返回有效', reply)


if __name__ == '__main__':
    unittest.main()
