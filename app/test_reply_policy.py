import unittest
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


def response(text, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=text), finish_reason=finish)])


class ReplyPolicyTests(unittest.TestCase):
    def test_sentence_flood_is_merged(self):
        text = '你好！这是一条测试消息。' * 20
        chunks = bot._split_reply_sentences(text)
        self.assertLessEqual(len(chunks), 3)
        self.assertEqual(''.join(chunks), text)

    def test_hard_limits_for_pathological_output(self):
        for text in ['长' * 10000, '一句话。\n' * 2000,
                     'a' * 399 + '\n' + 'b' * 399 + '\n' + 'c' * 10000]:
            chunks = bot._split_reply_sentences(text)
            self.assertLessEqual(len(chunks), 3)
            self.assertTrue(all(len(x) <= 800 for x in chunks))
            self.assertLessEqual(sum(map(len, chunks)), 1200)

    def test_short_sentences_urls_and_decimal_survive(self):
        self.assertEqual(bot._split_reply_sentences('你好！晚安。'), ['你好！', '晚安。'])
        text = '地址 https://example.com/a。数值3.14。'
        self.assertEqual(''.join(bot._split_reply_sentences(text)), text)
        self.assertEqual(bot._split_reply_sentences(''), [])

    def test_beijing_clock_and_midnight_are_independent_of_host(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 12, 31, 16, 5, tzinfo=timezone.utc).astimezone(tz)
        with patch.object(bot, 'datetime', FrozenDatetime):
            prompt = bot._runtime_prompt({})
            self.assertIn('2027-01-01 00:05:00 +0800', prompt)
            self.assertIn('星期五，深夜', prompt)
            self.assertIn('2026-12-31 16:05:00 +0000', bot._runtime_prompt({'timezone_offset_hours': 0}))

    def test_legacy_and_new_history_use_only_api_message_fields(self):
        history = [{'role': 'user', 'content': '晚安'},
                   {'role': 'assistant', 'content': '晚安！', 'created_at': '2026-10-08T23:00:00+08:00'}]
        with patch.dict(bot.conversations, {'test': history}, clear=True):
            messages = bot._build_history('test', 10)
        self.assertIn('未记录，不能推断为今天', messages[0]['content'])
        self.assertIn('2026-10-08T23:00:00+08:00', messages[1]['content'])
        self.assertTrue(all(set(m) == {'role', 'content'} for m in messages))
        self.assertNotIn('created_at', history[0])


class ChatFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlong_output_rewrites_once_and_saves_sent_answer(self):
        create = AsyncMock(side_effect=[response('长' * 3000, 'length'), response('简短完整的回答。')])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        cfg = {'deepseek': {'max_history': 10, 'system_prompt': '角色设定'}}
        with patch.object(bot, 'load_config', return_value=cfg), \
             patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), \
             patch.object(bot, '_save_conversations'), \
             patch.dict(bot.conversations, {'test': [{'role': 'user', 'content': '晚安'}]}, clear=True):
            reply = await bot.ask_deepseek('test', '你好')
            self.assertEqual(reply, '简短完整的回答。')
            self.assertEqual(create.await_count, 2)
            messages = create.call_args_list[0].kwargs['messages']
            self.assertEqual(messages[-2]['role'], 'system')
            self.assertIn('当前真实时间', messages[-2]['content'])
            self.assertIn('日常闲聊用2至4句简短的话', messages[-2]['content'])
            self.assertEqual(messages[-1]['content'], '你好')
            self.assertEqual(bot.conversations['test'][-1]['content'], reply)
            self.assertIn('created_at', bot.conversations['test'][-1])
            self.assertIn('不要接着续写', create.call_args_list[1].kwargs['messages'][-1]['content'])

    async def test_ignoring_rewrite_still_cannot_flood(self):
        create = AsyncMock(side_effect=[response('长' * 3000), response('长' * 5000)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, 'load_config', return_value={}), \
             patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), \
             patch.object(bot, '_save_conversations'), patch.dict(bot.conversations, {}, clear=True):
            reply = await bot.ask_deepseek('test', '详细解释')
        self.assertLessEqual(len(reply), 1200)
        self.assertLessEqual(len(bot._split_reply_sentences(reply)), 3)

    async def test_empty_reasoning_output_retries_without_saving_empty_history(self):
        create = AsyncMock(side_effect=[response(None, 'length'), response(None, 'length')])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, 'load_config', return_value={}), \
             patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), \
             patch.object(bot, '_save_conversations') as save, patch.dict(bot.conversations, {}, clear=True):
            self.assertIsNone(await bot.ask_deepseek('test', '你好'))
            save.assert_not_called()
        self.assertEqual(create.await_count, 2)
        self.assertEqual(create.call_args_list[1].kwargs['max_tokens'], 4000)

    async def test_group_send_count_is_bounded(self):
        api = AsyncMock(return_value={'status': 'ok'})
        with patch.object(bot, 'call_api', api), patch.object(bot.asyncio, 'sleep', AsyncMock()), \
             patch.object(bot, 'is_duplicate_send', return_value=False), \
             patch.object(bot, 'record_sent_history'):
            self.assertTrue(await bot.send_group_message(123, '一句话。' * 400, split_sentences=True))
        self.assertLessEqual(api.await_count, 3)
        self.assertTrue(all(len(c.args[1]['message']) <= 800 for c in api.call_args_list))

    async def test_private_send_count_is_bounded(self):
        cfg = {'deepseek': {'enabled': True, 'trigger_private': True}}
        data = {'raw_message': '你好', 'sender': {'user_id': 123, 'nickname': '测试'}}
        api = AsyncMock(return_value={'status': 'ok'})
        with patch.object(bot, 'load_config', return_value=cfg), \
             patch.object(bot, 'deepseek_client', object()), \
             patch.object(bot, 'ask_deepseek', AsyncMock(return_value='一句话。' * 400)), \
             patch.object(bot, 'call_api', api), patch.object(bot.asyncio, 'sleep', AsyncMock()), \
             patch.object(bot, 'is_duplicate_send', return_value=False), \
             patch.object(bot, 'record_sent_history'), patch.dict(bot.conversations, {}, clear=True):
            await bot.on_private_message(data)
        self.assertGreater(api.await_count, 0)
        self.assertLessEqual(api.await_count, 3)
        self.assertTrue(all(c.args[0] == 'send_private_msg' for c in api.call_args_list))

    async def test_scheduled_output_handles_empty_then_validated_final(self):
        create = AsyncMock(side_effect=[response(None, 'length'), response(json.dumps({'kind': 'send', 'reply': '晚安。'}))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(bot, '_init_deepseek'), patch.object(bot, 'deepseek_client', client), patch.object(bot, 'load_config', return_value={}):
            reply = await bot.generate_timed_message('早安', {})
        self.assertLessEqual(len(reply), 80)
        self.assertIn('80字以内', create.call_args_list[0].kwargs['messages'][0]['content'])
        self.assertIn('当前真实时间', create.call_args_list[0].kwargs['messages'][0]['content'])


if __name__ == '__main__':
    unittest.main()
