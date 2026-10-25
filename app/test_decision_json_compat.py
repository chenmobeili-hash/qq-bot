import json
import unittest
from unittest.mock import AsyncMock, patch

import bot
from mechanism_assistant import SuggestedReply
from model_decision import DecisionError, parse
import test_model_decision as fixtures


def wrapped(reply='自然回应。', **kwargs):
    return 'PRIVATE_PREFACE：需要先理解用户的意思，然后按格式作答。\n\n' + fixtures.envelope(reply=reply, **kwargs)


class EnvelopeTests(unittest.TestCase):
    def parsed(self, raw, admin=True):
        return parse(raw, 'group_123_456', {}, admin)

    def test_plain_json_keeps_original_behavior(self):
        result = self.parsed(fixtures.envelope(reply='正常回答'))
        self.assertEqual(result.reply, '正常回答')
        self.assertFalse(result.normalized)

    def test_chinese_and_english_prose_are_removed_before_validating(self):
        for prefix in ('需要先理解用户含糊的回复，然后按JSON回应。\n\n',
                       'I need to understand the reply before returning JSON.\n\n'):
            with self.subTest(prefix=prefix):
                result = self.parsed(prefix + fixtures.envelope(reply='嗯，接着聊吧。'))
                self.assertEqual(result.reply, '嗯，接着聊吧。')
                self.assertTrue(result.normalized)

    def test_one_whole_fence_plain_json_fence_and_bom_are_supported(self):
        body = fixtures.envelope()
        for value in ('```json\n' + body + '\n```', '```JSON\r\n' + body + '\r\n```',
                      '```\n' + body + '\n```', '\ufeff' + body):
            with self.subTest(value=value):
                self.assertTrue(self.parsed(value).normalized)

    def test_prose_plus_one_fenced_final_object_is_supported(self):
        result = self.parsed('先想一下该怎样回答。\n```json\n' + fixtures.envelope() + '\n```')
        self.assertEqual(result.kind, 'chat')
        self.assertTrue(result.normalized)

    def test_nested_memory_and_escaped_braces_in_strings_are_not_split(self):
        data = {'kind': 'chat', 'reply': '字符串里可以有 {括号}、[数组]、引号"和反斜线\\。', 'action': None,
                'memory': [{'op': 'remember', 'scope': 'personal', 'key': '印象', 'text': '一起谈过集合{A}。'}]}
        result = self.parsed('简短前言。\n' + json.dumps(data, ensure_ascii=False))
        self.assertEqual(result.reply, data['reply'])
        self.assertEqual(result.memory, data['memory'])

    def test_two_objects_or_array_and_object_are_ambiguous(self):
        body = fixtures.envelope()
        for value in ('示例：' + body + '\n正式回答：' + body,
                      '资料：{}\n' + body, '前文：[]\n' + body):
            with self.subTest(value=value), self.assertRaises(DecisionError):
                self.parsed(value)

    def test_valid_inner_decision_is_not_salvaged_from_truncated_outer_object(self):
        body = fixtures.envelope()
        for value in ('前言\n{"wrapper": ' + body,
                      '{"wrapper": ' + body,
                      '前言\n[ ' + body):
            with self.subTest(value=value), self.assertRaises(DecisionError):
                self.parsed(value)

    def test_complete_outer_wrong_schema_does_not_expose_inner_decision(self):
        with self.assertRaisesRegex(DecisionError, 'invalid_envelope'):
            self.parsed('前言\n' + json.dumps({'wrapper': json.loads(fixtures.envelope())}))

    def test_duplicate_keys_remain_rejected_in_wrapped_json(self):
        for body in ('{"kind":"chat","reply":"a","reply":"b","action":null}',
                     '{"kind":"operation","reply":"确认吗","action":{"action":"search","args":{"query":"a","query":"b"}}}'):
            with self.assertRaisesRegex(DecisionError, 'duplicate_keys'):
                self.parsed('前言\n' + body)

    def test_trailing_prose_and_broken_or_foreign_fences_are_rejected(self):
        body = fixtures.envelope()
        for value in ('前言\n' + body + '\n还有一句话', '```json\n' + body,
                      '前言\n```python\n' + body + '\n```',
                      '前言\n```json\n' + body + '\n```\n尾声'):
            with self.subTest(value=value), self.assertRaises(DecisionError):
                self.parsed(value)

    def test_missing_json_plain_text_and_legacy_markers_are_still_invalid(self):
        for value in ('我需要想想怎么回答。', '好的\n[[mechanism:{"action":"reading_on","args":{}}]]',
                      '前言\n{"kind":"chat",', '前言\n{broken}\n' + fixtures.envelope()):
            with self.subTest(value=value), self.assertRaises(DecisionError):
                self.parsed(value)

    def test_envelope_kind_permissions_and_memory_rules_stay_strict(self):
        bad = [wrapped(kind='unknown'), wrapped(action='exec', args={'cmd': 'x'}, kind='operation'),
               wrapped(action='proactive_on', kind='operation'),
               '前言\n' + json.dumps({'kind': 'chat', 'reply': 'x', 'action': None,
                    'memory': [{'op': 'remember', 'scope': 'identity', 'key': 'name', 'text': 'unauthorized'}]})]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(DecisionError):
                self.parsed(value, admin=False)

    def test_size_limit_and_non_object_json_remain_rejected(self):
        for value in ('前言' * 50001 + fixtures.envelope(), '[]', '42', '"string"'):
            with self.subTest(value=value[:20]), self.assertRaises(DecisionError):
                self.parsed(value)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SemanticIntegrationTests.setUp

    async def test_prefaced_reply_is_one_call_and_only_reply_reaches_history_or_logs(self):
        self.create.return_value = fixtures.response(wrapped(reply='小汐听着呢。'))
        result = await bot.ask_deepseek(self.key, '嗯嗯，是这样呢')
        self.assertEqual(result, '小汐听着呢。')
        self.create.assert_awaited_once()
        self.assertNotIn('PRIVATE_PREFACE', str(self.history))
        self.assertNotIn('PRIVATE_PREFACE', str(bot.log.call_args_list))
        self.assertIn('JSON包装已规范化', str(bot.log.call_args_list))
        self.assertEqual(self.create.call_args.kwargs['response_format'], {'type': 'json_object'})

    async def test_wrapped_operation_still_waits_for_delivered_confirmation(self):
        self.create.return_value = fixtures.response(wrapped(kind='operation', reply='要让我多说些吗？',
            action='proactive_level', args={'level': '活跃'}))
        with patch.object(bot, '_execute_mechanism', AsyncMock()) as execute:
            result = await bot.ask_deepseek(self.key, '你也多参与些吧')
            self.assertIsInstance(result, SuggestedReply)
            execute.assert_not_awaited()
            self.router.activate(self.key, result.mechanism_ticket)
            data = {**self.data, 'raw_message': '[CQ:at,qq=999] 可以'}
            with patch.object(bot, 'send_msg_to_user', AsyncMock()):
                await bot._handle_mechanism_control(data, '可以', self.config)
            execute.assert_awaited_once()

    async def test_wrapped_memory_is_written_without_raw_preface(self):
        data = {'kind': 'chat', 'reply': '想把这点留下。', 'action': None,
                'memory': [{'op': 'remember', 'scope': 'personal', 'key': '共同兴趣', 'text': '一起聊时间哲学。'}]}
        self.create.return_value = fixtures.response('PRIVATE_PREFACE\n' + json.dumps(data, ensure_ascii=False))
        await bot.ask_deepseek(self.key, '今天聊得挺开心')
        state = (self.base / 'autonomous_memory.json').read_text(encoding='utf-8')
        self.assertIn('时间哲学', state)
        self.assertNotIn('PRIVATE_PREFACE', state)
        self.create.assert_awaited_once()

    async def test_ambiguous_or_invalid_output_does_not_retry_write_or_prepare_actions(self):
        for value in (wrapped() + '\n' + fixtures.envelope(), '只有前言，没有JSON。'):
            self.create.reset_mock()
            self.create.return_value = fixtures.response(value)
            result = await bot.ask_deepseek(self.key, '普通聊天')
            self.assertIn('没有返回有效', result)
            self.create.assert_awaited_once()
            self.assertFalse(self.router.pending)
            self.assertFalse(self.history[self.key])
            self.assertFalse((self.base / 'autonomous_memory.json').exists())


if __name__ == '__main__':
    unittest.main()
