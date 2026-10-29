"""Offline Fish HTTP protocol/audio checks. Never use a real account key."""
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
import fish_audio as fish


def config(**extra):
    return {'fish_audio': dict(enabled=True, api_key='OFFLINE_PRIVATE_KEY', voice_id='offline_voice_0123456789',
                               use_env_proxy=False, **extra)}


class StateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = [100000.0]
        self.state = fish.State(self.root, clock=lambda: self.now[0])

    def test_scopes_and_voice_switches_survive_restart(self):
        self.assertFalse(self.state.profile('g:1')['enabled'])
        self.state.set('g:1', enabled=True, voice='轻声')
        other = fish.State(self.root)
        self.assertTrue(other.profile('g:1')['enabled'])
        self.assertEqual(other.profile('g:1')['voice'], '轻声')
        self.assertFalse(other.profile('p:1')['enabled'])
        self.assertFalse(other.profile('g:2')['enabled'])

    def test_attempts_are_persistent_and_no_duplicate_ticket_replays(self):
        cfg = fish.settings(config())
        self.assertIsNone(self.state.reserve('p:1', 'one', cfg))
        self.state.finish('one', 'unknown')
        self.assertEqual(fish.State(self.root, clock=lambda:self.now[0]).reserve('p:1', 'one', cfg), 'already_attempted')

    def test_auto_cooldown_manual_bypass_and_scope_cap(self):
        cfg = fish.settings(config(scope_daily_limit=2))
        self.assertIsNone(self.state.reserve('p:1', 'one', cfg))
        self.assertEqual(self.state.reserve('p:1', 'two', cfg), 'cooldown')
        self.assertIsNone(self.state.reserve('p:1', 'manual', cfg, manual=True))
        self.assertEqual(self.state.reserve('p:1', 'three', cfg, manual=True), 'scope_limit')

    def test_global_cap_is_shared_and_rolling_24h_does_not_reset_on_restart(self):
        cfg = fish.settings(config(daily_limit=1))
        self.assertIsNone(self.state.reserve('g:1', 'one', cfg))
        self.assertEqual(self.state.reserve('p:2', 'two', cfg), 'daily_limit')
        self.now[0] += 86401
        self.assertIsNone(self.state.reserve('p:2', 'two', cfg))

    def test_corrupt_state_is_preserved_and_fails_closed(self):
        self.state.path.write_bytes(b'corrupt')
        with self.assertRaises(fish.TTSError):
            self.state.profile('p:1')
        self.assertEqual(self.state.path.read_bytes(), b'corrupt')

    def test_cache_only_removes_owned_expired_audio_and_never_active_files(self):
        cache = fish.Cache(self.root, clock=lambda:self.now[0])
        cache.prepare()
        old = cache.root / ('tts-' + 'a'*32 + '.mp3')
        busy = cache.root / ('tts-' + 'b'*32 + '.mp3')
        foreign = cache.root / 'user-document.txt'
        for path in (old,busy,foreign):
            path.write_bytes(b'keep')
            os.utime(path,(self.now[0]-3601, self.now[0]-3601))
        cache.active.add(busy)
        cache.cleanup()
        self.assertFalse(old.exists())
        self.assertTrue(busy.exists())
        self.assertTrue(foreign.exists())

    def test_cache_capacity_prunes_older_completed_files(self):
        cache = fish.Cache(self.root)
        cache.prepare()
        old = cache.root / ('tts-'+'a'*32+'.mp3')
        with old.open('wb') as stream:
            stream.truncate(21*1024*1024)
        cache.cleanup()
        self.assertFalse(old.exists())

    def test_configuration_rejects_unknown_model_instead_of_paid_fallback(self):
        with self.assertRaisesRegex(fish.TTSError,'unsupported_model'):
            fish.settings(config(model='s2.1-pro-fre'))

    def test_credentials_can_come_from_environment_without_appearing_in_status(self):
        cfg = config()
        cfg['fish_audio']['api_key']=''
        with patch.dict(os.environ, {'FISH_AUDIO_API_KEY':'OFFLINE_ENV_PRIVATE'}):
            self.assertEqual(fish.settings(cfg)['api_key'],'OFFLINE_ENV_PRIVATE')

    def test_short_plain_text_only_no_control_tags_cq_links_or_long_truncation(self):
        for value in ('[CQ:at,qq=all]','[laugh]你好','```code```','https://example.com','<|speaker:0|>x','x'*181):
            with self.subTest(value=value),self.assertRaises(fish.TTSError):
                fish.clean_text(value,180)
        self.assertEqual(fish.clean_text('你好，今天过得怎么样？',180),'你好，今天过得怎么样？')


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise unittest.SkipTest('FFmpeg/ffprobe required for real offline MP3 decode checks')
        result = subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=44100:cl=mono',
            '-t','0.2','-b:a','64k','-f','mp3','pipe:1'],capture_output=True)
        if result.returncode:
            raise unittest.SkipTest('Offline audio fixture unavailable')
        cls.mp3 = result.stdout

    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache=fish.Cache(Path(temp.name))
        self.requests=[]
        self.status=200
        self.payload=self.mp3
        self.content_type='audio/mpeg'
        self.delay=0
        self.headers={}
        async def handle(request):
            self.requests.append((dict(request.headers), await request.json()))
            if self.delay:
                await asyncio.sleep(self.delay)
            return web.Response(status=self.status,body=self.payload,headers=self.headers,content_type=self.content_type)
        app=web.Application()
        app.router.add_post('/tts',handle)
        self.runner=web.AppRunner(app)
        await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0)
        await site.start()
        self.addAsyncCleanup(self.runner.cleanup)
        url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/tts'
        self.enterContext(patch.object(fish,'API_URL',url))
        self.cfg=fish.settings(config())

    async def test_real_http_posts_exact_model_voice_and_text_returns_decodable_mp3(self):
        path,duration=await fish.synthesize('你好。',self.cfg['voices']['默认音色'],self.cfg,self.cache)
        headers,body=self.requests[0]
        self.assertEqual(headers['model'],'s2.1-pro-free')
        self.assertEqual(headers['Authorization'],'Bearer OFFLINE_PRIVATE_KEY')
        self.assertEqual(body['reference_id'],'offline_voice_0123456789')
        self.assertEqual(body['text'],'你好。')
        self.assertEqual(body['format'],'mp3')
        self.assertEqual(body['mp3_bitrate'],64)
        self.assertGreater(duration,0)
        self.assertEqual(path.read_bytes(),self.mp3)

    async def test_status_errors_are_distinct_and_never_retried(self):
        for status,code in ((401,'auth_failed'),(402,'quota_or_balance'),(403,'access_denied'),(429,'rate_limit'),(503,'service_unavailable')):
            self.status=status
            self.payload=b'OFFLINE_PRIVATE_KEY + private response'
            with self.subTest(status=status),self.assertRaisesRegex(fish.TTSError,code) as error:
                await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)
            self.assertNotIn('OFFLINE',str(error.exception))
        self.assertEqual(len(self.requests),5)

    async def test_redirects_are_not_followed_with_credentials(self):
        self.status=302
        self.headers={'Location':fish.API_URL}
        with self.assertRaisesRegex(fish.TTSError,'http_failure'):
            await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)
        self.assertEqual(len(self.requests),1)

    async def test_json_error_with_status_200_is_not_treated_as_audio(self):
        self.content_type='application/json'
        self.payload=b'{"error":"PRIVATE"}'
        with self.assertRaisesRegex(fish.TTSError,'invalid_audio'):
            await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)

    async def test_corrupt_audio_is_rejected_and_temporary_file_removed(self):
        self.payload=b'ID3not-valid-mp3'
        with self.assertRaisesRegex(fish.TTSError,'invalid_audio'):
            await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)
        self.assertFalse(list(self.cache.root.glob('*.mp3')))
        self.assertFalse(self.cache.active)

    async def test_oversized_audio_is_bounded(self):
        self.payload=b'x'*(fish.MAX_AUDIO+1)
        with self.assertRaisesRegex(fish.TTSError,'audio_too_large'):
            await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)

    async def test_total_timeout_does_not_trigger_a_retry(self):
        self.delay=.1
        self.cfg['timeout']=.02
        with self.assertRaisesRegex(fish.TTSError,'timeout'):
            await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)
        # Under load the deadline can fire before the local server sees headers.
        self.assertLessEqual(len(self.requests),1)

    async def test_context_change_prevents_any_http_request(self):
        with self.assertRaisesRegex(fish.TTSError,'context_changed'):
            await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache,guard=lambda:False)
        self.assertFalse(self.requests)

    async def test_missing_decoder_is_checked_before_upload_or_charge(self):
        with patch.object(fish.shutil,'which',return_value=None):
            with self.assertRaisesRegex(fish.TTSError,'ffprobe_missing'):
                await fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache)
        self.assertFalse(self.requests)

    async def test_cancellation_is_not_converted_to_a_network_retry(self):
        self.delay=.2
        task=asyncio.create_task(fish.synthesize('你好。','offline_voice_0123456789',self.cfg,self.cache))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLessEqual(len(self.requests),1)
        self.assertFalse(self.cache.active)
