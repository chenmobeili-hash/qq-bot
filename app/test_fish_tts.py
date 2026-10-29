"""Scoped opt-in speech, complete-model decisions, and real bot send paths offline."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot
import fish_audio as audio
import fish_tts as tts
from mechanism_assistant import SuggestedReply, allowed
import test_model_decision as fixtures
from test_model_stream import Chunks, chunk


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.SemanticIntegrationTests.setUp(self)
        self.config['fish_audio']={'enabled':True,'api_key':'OFFLINE_PRIVATE_KEY','voice_id':'offline_voice_0123456789',
            'voices':{'轻声':'offline_voice_abcdefgh'},'use_env_proxy':False}
        self.manager=tts.manager(bot)
        self.manager.state.set('g:123',enabled=True)
        self.manager.state.set('p:456',enabled=True)
        self.enterContext(patch.object(bot,'recent_sent_history',[]))
        self.enterContext(patch.object(bot,'proactive_mode',None))

    def response(self,flag=True,kind='chat',reply='你好，我在这里。',action=None,memory=None):
        value={'kind':kind,'reply':reply,'action':action,'speak':flag}
        if memory is not None:
            value['memory']=memory
        return fixtures.response(json.dumps(value,ensure_ascii=False))

    async def voice(self,key=None):
        self.create.return_value=self.response()
        reply=await bot.ask_deepseek(key or self.key,'你好')
        self.assertIsInstance(reply,tts.VoiceReply)
        return reply

    async def fake_audio(self,*args,**kwargs):
        self.manager.cache.prepare()
        path=self.manager.cache.root/('tts-'+'a'*32+'.mp3')
        path.write_bytes(b'OFFLINE_AUDIO_BYTES')
        return path,.2

    async def test_model_selects_voice_in_the_same_complete_json_request(self):
        reply=await self.voice()
        self.assertEqual(str(reply),'你好，我在这里。')
        self.create.assert_awaited_once()
        request=self.create.await_args.kwargs['messages']
        self.assertIn('可选语音扩展',str(request))
        self.assertNotIn('OFFLINE_PRIVATE_KEY',str(request))
        self.assertNotIn('speak',self.history[self.key][-1]['content'])

    async def test_model_false_or_omitted_flag_keeps_text_only(self):
        for response in (self.response(False), fixtures.response(fixtures.envelope())):
            self.create.return_value=response
            reply=await bot.ask_deepseek(self.key,'你好')
            self.assertFalse(getattr(reply,'fish_tts',None))

    async def test_wrong_type_optional_flag_is_not_permission_to_synthesize(self):
        self.create.return_value=self.response('true')
        reply=await bot.ask_deepseek(self.key,'你好')
        self.assertEqual(reply,'你好，我在这里。')
        self.assertFalse(getattr(reply,'fish_tts',None))

    async def test_disabled_chat_does_not_add_prompt_or_speech(self):
        self.manager.state.set('g:123',enabled=False)
        self.create.return_value=fixtures.response(fixtures.envelope())
        reply=await bot.ask_deepseek(self.key,'你好')
        self.assertFalse(getattr(reply,'fish_tts',None))
        self.assertNotIn('可选语音扩展',str(self.create.await_args.kwargs['messages']))

    async def test_incomplete_stream_does_not_attach_audio_or_commit_reply(self):
        self.create.return_value=Chunks([chunk('{"kind":"chat","reply":"half","speak":true')])
        self.assertIsNone(await bot.ask_deepseek(self.key,'你好'))
        self.assertFalse(self.history[self.key])

    async def test_valid_stream_selects_voice_after_existing_parser(self):
        text=self.response().choices[0].message.content
        self.create.return_value=Chunks([chunk(text[:10]),chunk(text[10:]),chunk(finish='stop')])
        reply=await bot.ask_deepseek(self.key,'你好')
        self.assertIsInstance(reply,tts.VoiceReply)
        self.create.assert_awaited_once()

    async def test_invalid_core_not_salvaged_by_true_voice_flag(self):
        self.create.return_value=self.response(True,action={'action':'exec','args':{}})
        reply=await bot.ask_deepseek(self.key,'你好')
        self.assertIn('没有返回有效',reply)
        self.assertFalse(getattr(reply,'fish_tts',None))

    async def test_memory_repair_notice_survives_with_voice_enabled(self):
        self.create.return_value=self.response(True,memory='bad')
        with patch.object(bot.long_term_memory,'commit') as commit:
            reply=await bot.ask_deepseek(self.key,'你好')
        self.assertIn('未保存',reply)
        self.assertIsInstance(reply,tts.VoiceReply)
        commit.assert_not_called()

    async def test_suggested_operation_is_never_spoken_or_auto_executed(self):
        self.create.return_value=self.response(True,kind='operation',reply='要开启吗？',
            action={'action':'tts_on','args':{}})
        with patch.object(bot,'_execute_mechanism',AsyncMock()) as execute:
            reply=await bot.ask_deepseek(self.key,'以后可以用语音')
        self.assertIsInstance(reply,SuggestedReply)
        self.assertFalse(getattr(reply,'fish_tts',None))
        execute.assert_not_awaited()

    async def test_long_code_and_links_remain_text_only(self):
        for text in ('你好。'*70,'```x```','见https://example.com'):
            self.create.return_value=self.response(True,reply=text)
            reply=await bot.ask_deepseek(self.key,'你好')
            self.assertFalse(getattr(reply,'fish_tts',None))

    async def test_actual_group_delivery_sends_text_before_audio_and_keeps_scope(self):
        reply=await self.voice()
        with patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=self.fake_audio)) as synth:
            self.assertTrue(await bot.send_group_message(123,reply))
        self.assertEqual(api.await_count,2)
        self.assertEqual(api.await_args_list[0].args[1]['message'],'你好，我在这里。')
        segment=api.await_args_list[1].args[1]['message']
        self.assertEqual(segment[0]['type'],'record')
        self.assertTrue(segment[0]['data']['file'].startswith('base64://'))
        self.assertNotIn('base64',str(segment))
        self.assertIn('base64://',json.dumps(segment))
        synth.assert_awaited_once()

    async def test_group_send_failure_prevents_tts_spend(self):
        reply=await self.voice()
        with patch.object(bot,'call_api',AsyncMock(return_value=None)), patch.object(audio,'synthesize',AsyncMock()) as synth:
            self.assertFalse(await bot.send_group_message(123,reply))
        synth.assert_not_awaited()

    async def test_private_generic_delivery_has_text_and_voice(self):
        reply=await self.voice('private_456')
        data={'message_type':'private','sender':{'user_id':456},'user_id':456}
        with patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=self.fake_audio)):
            self.assertTrue(await bot.send_msg_to_user(data,self.config,reply))
        self.assertEqual(api.await_count,2)
        self.assertTrue(all(c.args[0]=='send_private_msg' and c.args[1]['user_id']==456 for c in api.await_args_list))

    async def test_actual_private_message_handler_keeps_voice_metadata(self):
        self.config['deepseek']['trigger_private']=True
        self.create.return_value=self.response()
        data={'message_type':'private','sender':{'user_id':456,'nickname':'offline'},'user_id':456,
              'raw_message':'你好','message':[{'type':'text','data':{'text':'你好'}}], 'message_id':777}
        with patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=self.fake_audio)) as synth:
            await bot.on_private_message(data)
        synth.assert_awaited_once()
        self.assertTrue(any(isinstance(c.args[1].get('message'),tts.RecordSegments) for c in api.await_args_list))

    async def test_tts_failure_keeps_text_success_without_audio_retry(self):
        reply=await self.voice()
        with patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=audio.TTSError('service_unavailable'))) as synth:
            self.assertTrue(await bot.send_group_message(123,reply))
        synth.assert_awaited_once()
        self.assertEqual(api.await_count,1)

    async def test_explicit_manual_speech_works_when_automatic_mode_is_off(self):
        self.manager.state.set('g:123',enabled=False)
        with patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=self.fake_audio)) as synth:
            await tts.say(bot,self.data,'你好，今天也辛苦了。',self.config)
        synth.assert_awaited_once()
        self.assertEqual(api.await_count,2)
        self.assertFalse(self.manager.state.profile('g:123')['enabled'])

    async def test_manual_tts_failure_reports_once_without_retrying_audio(self):
        self.manager.state.set('p:456',enabled=False)
        data={'message_type':'private','sender':{'user_id':456},'user_id':456}
        with patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=audio.TTSError('auth_failed'))) as synth:
            await tts.say(bot,data,'你好。',self.config)
        synth.assert_awaited_once()
        self.assertEqual(api.await_count,2)
        self.assertIn('语音未完成',str(api.await_args.args[1]['message']))

    async def test_contexts_from_two_chats_do_not_exchange_voice_decisions(self):
        async def task(key, flag):
            async def original():
                ctx=tts._turn.get()
                await asyncio.sleep(.01)
                self.assertEqual(ctx.key,key)
                ctx.speak=flag
                ctx.content='各自的台词'
                return '各自的台词'
            return await tts.generation(bot,key,original)
        group,private=await asyncio.gather(task(self.key,True),task('private_456',False))
        self.assertTrue(getattr(group,'fish_tts',None))
        self.assertFalse(getattr(private,'fish_tts',None))

    async def test_unknown_audio_receipt_is_not_replayed(self):
        reply=await self.voice()
        async def send(*args,**kwargs):
            kwargs['outcome']['status']='unknown'
            return None
        with patch.object(bot,'call_api',AsyncMock(side_effect=send)) as api, patch.object(audio,'synthesize',AsyncMock(side_effect=self.fake_audio)) as synth:
            self.assertFalse(await tts.deliver(bot,'g:123',reply))
            self.assertFalse(await tts.deliver(bot,'g:123',reply))
        self.assertEqual(api.await_count,1)
        synth.assert_awaited_once()

    async def test_wrong_chat_and_disabled_mode_prevent_old_voice_send(self):
        reply=await self.voice()
        with patch.object(audio,'synthesize',AsyncMock()) as synth:
            self.assertFalse(await tts.deliver(bot,'p:456',reply))
            self.manager.state.set('g:123',enabled=False)
            self.assertFalse(await tts.deliver(bot,'g:123',reply))
        synth.assert_not_awaited()

    async def test_new_foreground_turn_or_voice_change_invalidates_old_reply(self):
        reply=await self.voice()
        self.manager.bump('g:123')
        with patch.object(audio,'synthesize',AsyncMock()) as synth:
            self.assertFalse(await tts.deliver(bot,'g:123',reply))
        synth.assert_not_awaited()

    async def test_voice_change_during_synthesis_drops_old_audio(self):
        reply=await self.voice()
        async def audio_then_switch(*args,**kwargs):
            result=await self.fake_audio()
            self.manager.state.set('g:123',voice='轻声')
            return result
        with patch.object(audio,'synthesize',AsyncMock(side_effect=audio_then_switch)),patch.object(bot,'call_api',AsyncMock()) as api:
            self.assertFalse(await tts.deliver(bot,'g:123',reply))
        api.assert_not_awaited()

    async def test_stale_generation_cancellation_message_is_not_spoken(self):
        async def original(*args,**kwargs):
            ctx=tts._turn.get()
            ctx.speak=True
            ctx.content='原本台词'
            return '刚才切换了风格，这条回复已取消。'
        reply=await tts.generation(bot,self.key,original)
        self.assertFalse(getattr(reply,'fish_tts',None))

    async def test_public_proactive_protocol_extension_preserves_final_message_checks(self):
        import public_message
        self.create.return_value=fixtures.response('{"kind":"send","reply":"很高兴见到大家。","speak":true}')
        async def public():
            return await public_message.generate(self.client,'offline',[{'role':'system','content':'offline'},
                {'role':'user','content':'offline'}],self.transport,bot.log,limit=160,timeout=2,source='proactive')
        reply=await tts.generation(bot,self.key,public,family='public')
        self.assertIsInstance(reply,tts.VoiceReply)

    async def test_public_silent_never_becomes_voice(self):
        import public_message
        self.create.return_value=fixtures.response('{"kind":"silent","reply":"","speak":true}')
        async def public():
            return await public_message.generate(self.client,'offline',[{'role':'system','content':'offline'}],
                self.transport,bot.log,limit=160,timeout=2,source='proactive')
        self.assertIsNone(await tts.generation(bot,self.key,public,family='public'))

    async def test_private_autonomous_wake_keeps_original_type_and_sends_voice(self):
        import private_wake
        import cognitive_runtime
        self.config['deepseek']['trigger_private']=True
        private_wake.mode(bot).set_enabled(456,True)
        self.create.return_value=fixtures.response('{"kind":"send","reply":"突然想和你打声招呼。","speak":true}')
        with patch.object(cognitive_runtime,'ready',return_value=True), \
             patch.object(bot,'_build_history',return_value=[]), \
             patch.object(bot,'call_api',AsyncMock(return_value={'message_id':1})) as api, \
             patch.object(audio,'synthesize',AsyncMock(side_effect=self.fake_audio)) as synth:
            reply=await private_wake.generate(bot,456,'wake',[],self.config)
            self.assertIsInstance(reply,private_wake.WakeReply)
            self.assertTrue(getattr(reply,'fish_tts',None))
            self.assertTrue(await private_wake.send(bot,456,reply))
        synth.assert_awaited_once()
        self.assertEqual(api.await_count,2)


class CommandTests(ReplyTests):
    # Reuse only fixture setup; inherited tests are intentionally removed below.
    async def test_modes_are_scoped_and_require_admin(self):
        data={**self.data,'raw_message':'[CQ:at,qq=999] 语音模式 关闭'}
        with patch.object(bot,'send_msg_to_user',AsyncMock(return_value=True)):
            await tts.command(bot,data,'语音模式 关闭',self.config)
            self.assertFalse(self.manager.state.profile('g:123')['enabled'])
            self.assertTrue(self.manager.state.profile('p:456')['enabled'])
            other={**data,'sender':{'user_id':457}}
            await tts.command(bot,other,'语音模式 开启',self.config)
            self.assertFalse(self.manager.state.profile('g:123')['enabled'])
        self.assertFalse(allowed('tts_on','group_123_457',False))
        self.assertFalse(allowed('tts_on','private_457',False))

    async def test_voice_menu_shows_current_and_cancel_keeps_voice(self):
        with patch.object(bot,'send_msg_to_user',AsyncMock(return_value=True)) as send:
            await tts.menu(bot,self.data,self.config)
            self.assertIn('当前音色',send.await_args.args[2])
            await tts.command(bot,self.data,'取消',self.config)
        self.assertNotIn(self.key,bot.user_states)
        self.assertEqual(self.manager.state.profile('g:123')['voice'],'')

    async def test_menu_selection_changes_current_chat_only_and_not_models(self):
        before=(bot.current_model,dict(self.config['deepseek']))
        with patch.object(bot,'send_msg_to_user',AsyncMock(return_value=True)):
            await tts.menu(bot,self.data,self.config)
            await tts.command(bot,self.data,'1',self.config)
        self.assertEqual(self.manager.state.profile('g:123')['voice'],'轻声')
        self.assertEqual(self.manager.state.profile('p:456')['voice'],'')
        self.assertEqual((bot.current_model,dict(self.config['deepseek'])),before)

    async def test_expired_undelivered_and_changed_menu_cannot_select(self):
        for condition in ('expired','undelivered','changed'):
            with patch.object(bot,'send_msg_to_user',AsyncMock(return_value=True)):
                await tts.menu(bot,self.data,self.config)
                row=bot.user_states[self.key]
                if condition=='expired':row['expires']=0
                elif condition=='undelivered':row['ready']=False
                else:self.manager.state.set('g:123',enabled=False)
                await tts.command(bot,self.data,'1',self.config)
            self.assertEqual(self.manager.state.profile('g:123')['voice'],'')

    async def test_plain_group_chatter_does_not_change_mode(self):
        data={**self.data,'raw_message':'语音模式 关闭','message':[]}
        self.assertFalse(await tts.command(bot,data,'语音模式 关闭',self.config))
        self.assertTrue(self.manager.state.profile('g:123')['enabled'])

    async def test_missing_voice_does_not_enable_mode_or_claim_success(self):
        self.config['fish_audio']['voice_id']=''
        self.config['fish_audio']['voices']={}
        self.manager.state.set('g:123',enabled=False)
        with patch.object(bot,'send_msg_to_user',AsyncMock(return_value=True)) as send:
            await tts.command(bot,self.data,'语音模式 开启',self.config)
        self.assertFalse(self.manager.state.profile('g:123')['enabled'])
        self.assertIn('未开启',send.await_args.args[2])

    async def test_natural_action_uses_original_confirmation_permission_flow(self):
        self.manager.state.set('g:123',enabled=False)
        self.create.return_value=fixtures.response(fixtures.envelope(kind='operation',reply='要开启语音吗？',action='tts_on'))
        reply=await bot.ask_deepseek(self.key,'以后合适的时候用声音回答')
        self.assertIsInstance(reply,SuggestedReply)
        self.assertFalse(self.manager.state.profile('g:123')['enabled'])
        self.router.activate(self.key,reply.mechanism_ticket)
        with patch.object(bot,'send_msg_to_user',AsyncMock(return_value=True)):
            await bot._handle_mechanism_control(self.data,'可以',self.config)
        self.assertTrue(self.manager.state.profile('g:123')['enabled'])


# Avoid re-running the reply tests simply because the command fixture inherits helpers.
for _name in list(vars(ReplyTests)):
    if _name.startswith('test_') and _name not in vars(CommandTests):
        setattr(CommandTests,_name,None)
