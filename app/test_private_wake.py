import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import cognitive_runtime as runtime
import private_wake
from mechanism_assistant import allowed, validate
from proactive import ProactiveMode
from paper_autonomy import PaperAutonomy
from paper_reader import PaperLibrary


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason='stop')])


class PrivateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.clock=1800000000.0
        self.cfg={'deepseek':{'enabled':True,'trigger_private':True},
                  'private_wake':{'wake_min_seconds':7200,'wake_max_seconds':7200}}
        self.now=datetime(2026,10,26,12,tzinfo=timezone(timedelta(hours=8)))
        self.fake=SimpleNamespace(BASE_DIR=self.root,log=Mock(),_mechanism_key=lambda d: 'private_'+str(d['sender']['user_id']),
            load_config=lambda:self.cfg,_bot_now=lambda c:self.now,send_msg_to_user=AsyncMock())
        self.mode=private_wake.PrivateWake(self.root/'private_wake_state.json',Mock(),clock=lambda:self.clock)
        private_wake._modes[str(self.root)]=self.mode
        self.generate=AsyncMock(return_value='唤醒')
        self.send=AsyncMock(return_value=True)

    async def tick(self):
        await self.mode.tick(lambda:private_wake.config_for(self.cfg),lambda c:self.now,lambda:True,self.generate,self.send)

    async def test_opt_in_is_off_for_everyone_by_default(self):
        await self.tick()
        self.generate.assert_not_awaited()
        self.assertFalse(self.mode.enabled(2,self.cfg))

    async def test_actual_sender_only_and_persistence(self):
        data={'message_type':'private','sender':{'user_id':2},'user_id':99,'raw_message':'自主唤醒 开启 99'}
        self.assertTrue(await private_wake.command(self.fake,data,'开启',self.cfg))
        self.assertTrue(self.mode.enabled(2,self.cfg))
        self.assertFalse(self.mode.enabled(99,self.cfg))
        reload=private_wake.PrivateWake(self.mode.path,Mock(),clock=lambda:self.clock)
        self.assertTrue(reload.enabled(2,self.cfg))

    async def test_group_admin_and_private_permission_stay_distinct(self):
        self.assertTrue(allowed('private_wake_on','private_2',False))
        self.assertFalse(allowed('private_wake_on','group_1_2',True))
        self.assertFalse(allowed('wake_on','group_1_2',False))
        with self.assertRaises(ValueError): validate('private_wake_on',{'qq':'99'},'private_2',True)

    async def test_no_group_mode_required_and_two_opportunities(self):
        self.cfg['proactive']={'enabled':False,'groups':[],'wake_enabled':False}
        self.mode.set_enabled(2,True)
        await self.tick()
        for _ in range(3):
            self.clock+=7201
            await self.tick()
        self.assertEqual(self.send.await_count,2)
        self.assertEqual({c.args[0] for c in self.send.await_args_list},{2})

    async def test_master_private_switch_and_quiet_hours(self):
        self.mode.set_enabled(2,True)
        await self.tick()
        self.clock+=7201
        self.cfg['deepseek']['trigger_private']=False
        await self.tick()
        self.cfg['deepseek']['trigger_private']=True
        self.now=self.now.replace(hour=23)
        await self.tick()
        self.generate.assert_not_awaited()

    async def test_off_mid_generation_discards_result(self):
        self.mode.set_enabled(2,True)
        await self.tick()
        self.clock+=7201
        async def changed(*args):
            self.mode.set_enabled(2,False)
            return '旧台词'
        self.generate.side_effect=changed
        await self.tick()
        self.send.assert_not_awaited()

    async def test_restarts_do_not_push_deadline_out(self):
        self.mode.set_enabled(2,True)
        await self.tick()
        due=self.mode.wake_due['2']
        self.clock+=3600
        self.mode=private_wake.PrivateWake(self.mode.path,Mock(),clock=lambda:self.clock)
        await self.tick()
        self.assertEqual(self.mode.wake_due['2'],due)
        self.clock=due+1
        await self.tick()
        self.send.assert_awaited_once()

    async def test_recent_activity_restored_and_prevents_early_wake(self):
        self.mode.set_enabled(2,True)
        await self.tick()
        self.clock+=7100
        self.mode.observe(2,'对方','在吗',private_wake.config_for(self.cfg),sender_id=2)
        self.mode=private_wake.PrivateWake(self.mode.path,Mock(),clock=lambda:self.clock)
        self.clock+=200
        await self.tick()
        self.clock+=301
        await self.tick()
        self.generate.assert_not_awaited()
        self.clock+=1801
        await self.tick()
        self.send.assert_awaited_once()

    async def test_private_generation_has_only_owner_history_and_strict_final(self):
        self.mode.set_enabled(2,True)
        create=AsyncMock(return_value=response('{"kind":"send","reply":"上次聊的书，我还想到了另一个问题。"}'))
        cfg=self.cfg
        with patch.multiple(bot,BASE_DIR=self.root,load_config=lambda:cfg,
                deepseek_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))), \
             patch.object(bot,'_init_deepseek'),patch.object(bot,'_public_generation_signature',return_value=('人格','route')), \
             patch.object(bot,'_speaker_context',return_value='已核实为普通用户'), \
             patch.object(bot,'_build_history',return_value=[{'role':'user','content':'本人私聊资料'}]) as history, \
             patch.object(runtime,'ready',return_value=True):
            result=await private_wake.generate(bot,'2','wake',[],cfg)
        self.assertIsInstance(result,private_wake.WakeReply)
        history.assert_called_once_with('private_2',4)
        self.assertIn('本人私聊资料',str(create.await_args.kwargs['messages']))

    async def test_failed_or_unknown_delivery_not_saved_as_spoken(self):
        self.mode.set_enabled(2,True)
        value=private_wake.WakeReply('本句没有确认发送')
        value.signature,value.revision,value.gate_revision=('persona','route'),runtime.revision,bot.reading_bridge.gate.revision
        with patch.multiple(bot,BASE_DIR=self.root,load_config=lambda:self.cfg), \
             patch.object(bot,'_public_generation_signature',return_value=value.signature), \
             patch.object(bot,'call_api',AsyncMock(return_value=None)),patch.object(runtime,'ready',return_value=True), \
             patch.dict(bot.conversations,{},clear=True):
            self.assertFalse(await private_wake.send(bot,'2',value))
            self.assertFalse(bot.conversations)

    async def test_nonadmin_status_command_is_silent_without_model(self):
        fake=SimpleNamespace(_mechanism_key=lambda d:'group_1_2',load_config=lambda:{},is_admin=lambda u,c:False,
            send_msg_to_user=AsyncMock())
        self.assertTrue(await runtime.command(fake,{'raw_message':'#sl','sender':{'user_id':2},'group_id':1}))
        fake.send_msg_to_user.assert_not_awaited()

    async def test_admin_status_uses_program_state_and_no_secrets(self):
        fake=SimpleNamespace(_mechanism_key=lambda d:'private_2',load_config=lambda:{'current_model':'test-model','api_key':'SECRET'},
            is_admin=lambda u,c:True,_bot_now=lambda c:self.now,ws_connection=object(),send_msg_to_user=AsyncMock())
        self.assertTrue(await runtime.command(fake,{'raw_message':'#SL','sender':{'user_id':2}}))
        text=fake.send_msg_to_user.await_args.args[2]
        self.assertIn('test-model',text)
        self.assertNotIn('SECRET',text)


class GuaranteeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.clock=1800000000.0
        self.now=datetime(2026,10,26,12,tzinfo=timezone(timedelta(hours=8)))
        self.cfg={'deepseek':{'enabled':True},'proactive':{'groups':[123]},
            'paper_reader':{'auto_read':{'probability':0.0000000001}}}
        self.mode=ProactiveMode(self.root/'chat.json',Mock(),clock=lambda:self.clock)
        self.reading=PaperAutonomy(self.root/'reading.json',Mock(),clock=lambda:self.clock,
            roll=lambda:(_ for _ in ()).throw(AssertionError('True random must never be used')))
        self.library=PaperLibrary(clock=lambda:self.clock)
        self.mode.seed(self.cfg)
        self.clock+=1801
        self.discover=AsyncMock(return_value=[])

    async def tick(self):
        await self.reading.tick(lambda:self.cfg,lambda c:self.now,lambda:True,self.mode,self.library,
            self.discover,AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock(),lambda c:1)

    async def test_third_check_guarantees_discovery_without_random(self):
        for _ in range(3):
            await self.tick()
            self.clock+=7201
        self.discover.assert_awaited_once()
        self.assertEqual(self.reading.groups['123']['misses'],0)
        self.assertIn('没有找到',self.reading.groups['123']['last_outcome'])

    async def test_third_guarantee_survives_restart_and_waits_out_quiet_hours(self):
        for _ in range(2):
            await self.tick()
            self.clock+=7201
        due=self.reading.groups['123']['next_check_at']
        self.mode=ProactiveMode(self.mode.path,Mock(),clock=lambda:self.clock)
        self.reading=PaperAutonomy(self.reading.path,Mock(),clock=lambda:self.clock)
        self.now=self.now.replace(hour=23)
        await self.tick()
        self.assertEqual(self.reading.groups['123']['misses'],2)
        self.assertEqual(self.reading.groups['123']['next_check_at'],due)
        self.now=self.now.replace(hour=8)
        await self.tick()
        self.discover.assert_awaited_once()

    async def test_six_hours_of_silence_no_longer_disqualifies_reading(self):
        self.clock+=7*3600
        self.assertTrue(self.reading.eligible(123,self.cfg,self.now,self.mode,self.library))

    async def test_status_does_not_consume_or_move_scheduled_checks(self):
        text=self.reading.diagnostic(123,self.cfg,self.now,self.mode,self.library)
        due=self.reading.groups['123']['next_check_at']
        self.clock+=30
        self.reading.diagnostic(123,self.cfg,self.now,self.mode,self.library)
        self.assertEqual(due,self.reading.groups['123']['next_check_at'])
        self.assertIn('最迟保底',text)
        self.assertNotIn('checks',self.reading.groups['123'])

    async def test_muted_recipient_or_quota_does_not_consume_guarantee(self):
        self.reading.groups['123']={'misses':2,'next_check_at':self.clock-1,
            'daily':{'date':self.now.date().isoformat(),'attempts':2,'reviewed':0}}
        await self.tick()
        self.assertEqual(self.reading.groups['123']['misses'],2)
        self.discover.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
