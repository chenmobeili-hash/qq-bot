"""Offline protocol and integration checks. All HTTP goes to MockTransport."""
import asyncio
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, BadRequestError
from openai.types.chat import ChatCompletionChunk
import model_stream as ms


def full(text='hello', finish='stop'):
    return NS(choices=[NS(message=NS(content=text), finish_reason=finish)])


def chunk(text=None, finish=None, index=0, **delta):
    return ChatCompletionChunk.model_validate(dict(id='test', created=1, model='m', object='chat.completion.chunk',
        choices=[dict(index=index, delta=dict(content=text, **delta), finish_reason=finish)]))


class Chunks:
    def __init__(self, items, delays=None):
        self.items = list(items)
        self.delays = delays or []
        self.closed = False
    def __aiter__(self):
        return self.iterate()
    async def iterate(self):
        for i, item in enumerate(self.items):
            if i < len(self.delays):
                await asyncio.sleep(self.delays[i])
            if isinstance(item, Exception):
                raise item
            yield item
    async def close(self):
        self.closed = True


def adapter(value=None, side_effect=None, endpoint='https://offline.invalid/v1'):
    call = AsyncMock(return_value=value, side_effect=side_effect)
    return NS(base_url=endpoint, chat=NS(completions=NS(create=call))), call


class BufferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ms._unsupported.clear()
        self.enterContext(patch.dict(os.environ, {'QQ_BOT_STREAMING': '1'}))

    async def test_buffers_text_keeps_finish_usage_and_ignores_reasoning(self):
        usage = ChatCompletionChunk.model_validate(dict(id='test', created=1, model='m', object='chat.completion.chunk',
                    choices=[], usage=dict(prompt_tokens=2, completion_tokens=3, total_tokens=5)))
        s = Chunks([chunk(reasoning_content='PRIVATE_REASONING'), chunk('你'), chunk('好'), chunk(finish='stop'), usage])
        c, call = adapter(s)
        logs = Mock()
        result = await ms.create(c, model='m', messages=[], _stream_log=logs)
        self.assertEqual(result.choices[0].message.content, '你好')
        self.assertEqual(result.choices[0].finish_reason, 'stop')
        self.assertEqual(result.usage.total_tokens, 5)
        self.assertTrue(call.await_args.kwargs['stream'])
        self.assertTrue(s.closed)
        self.assertNotIn('PRIVATE_REASONING', str(logs.call_args_list))
        self.assertNotIn('你好', str(logs.call_args_list))

    async def test_length_finish_is_preserved_for_original_validator(self):
        c, _ = adapter(Chunks([chunk('truncated', 'length')]))
        result = await ms.create(c, model='m', messages=[])
        self.assertEqual(result.choices[0].finish_reason, 'length')

    async def test_partial_json_is_never_returned_or_retried(self):
        s = Chunks([chunk('{"kind":"proposal","action":')])
        c, call = adapter(s)
        with self.assertRaises(ms.IncompleteStream):
            await ms.create(c, model='m', messages=[])
        call.assert_awaited_once()
        self.assertTrue(s.closed)

    async def test_empty_eof_is_failure(self):
        c, _ = adapter(Chunks([]))
        with self.assertRaises(ms.IncompleteStream):
            await ms.create(c, model='m', messages=[])

    async def test_failure_even_after_finish_discards_buffer(self):
        s = Chunks([chunk('hello', 'stop'), httpx.ReadError('PRIVATE_RESPONSE')])
        c, call = adapter(s)
        with self.assertRaises(APIConnectionError):
            await ms.create(c, model='m', messages=[])
        call.assert_awaited_once()
        self.assertTrue(s.closed)

    async def test_content_after_finish_is_rejected(self):
        c, _ = adapter(Chunks([chunk('a', 'stop'), chunk('b')]))
        with self.assertRaises(ms.StreamProtocolError):
            await ms.create(c, model='m', messages=[])

    async def test_duplicate_finish_is_rejected(self):
        c, _ = adapter(Chunks([chunk('a', 'stop'), chunk(finish='stop')]))
        with self.assertRaises(ms.StreamProtocolError):
            await ms.create(c, model='m', messages=[])

    async def test_nontext_deltas_are_never_executed(self):
        for delta in ({'refusal': 'refused'}, {'function_call': {'name': 'delete', 'arguments': '{}'}},
                      {'tool_calls': [{'index': 0, 'id': 'x', 'type': 'function', 'function': {'name': 'delete', 'arguments': '{}'}}]}):
            with self.subTest(delta=list(delta)):
                c, _ = adapter(Chunks([chunk('untrusted', 'stop', **delta)]))
                with self.assertRaises(ms.StreamProtocolError):
                    await ms.create(c, model='m', messages=[])

    async def test_choices_do_not_mix(self):
        c, _ = adapter(Chunks([chunk('two', 'stop', 1), chunk('one', 'stop', 0)]))
        result = await ms.create(c, model='m', messages=[])
        self.assertEqual([x.message.content for x in result.choices], ['one', 'two'])

    async def test_every_choice_needs_finish(self):
        c, _ = adapter(Chunks([chunk('one', 'stop'), chunk('two', index=1)]))
        with self.assertRaises(ms.IncompleteStream):
            await ms.create(c, model='m', messages=[])

    async def test_buffer_is_bounded(self):
        c, _ = adapter(Chunks([chunk('12345', 'stop')]))
        with patch.object(ms, 'MAX_BYTES', 4):
            with self.assertRaises(ms.StreamProtocolError):
                await ms.create(c, model='m', messages=[])

    async def test_chunk_count_is_bounded(self):
        c, _ = adapter(Chunks([chunk('a'), chunk('b', 'stop')]))
        with patch.object(ms, 'MAX_CHUNKS', 1):
            with self.assertRaises(ms.StreamProtocolError):
                await ms.create(c, model='m', messages=[])

    async def test_idle_timeout_closes_without_fallback(self):
        s = Chunks([chunk('a'), chunk('b', 'stop')], [0, .15])
        c, call = adapter(s)
        with self.assertRaises(APITimeoutError):
            await ms.create(c, model='m', messages=[], _stream_policy=ms.Policy(.3, .1, .02))
        self.assertTrue(s.closed)
        call.assert_awaited_once()

    async def test_first_chunk_timeout(self):
        c, _ = adapter(Chunks([chunk('a', 'stop')], [.15]))
        with self.assertRaises(APITimeoutError):
            await ms.create(c, model='m', messages=[], _stream_policy=ms.Policy(.3, .02, .1))

    async def test_total_deadline_not_extended_by_regular_chunks(self):
        c, _ = adapter(Chunks([chunk('a')] * 8 + [chunk(finish='stop')], [.01] * 9))
        with self.assertRaises(APITimeoutError):
            await ms.create(c, model='m', messages=[], _stream_policy=ms.Policy(.035, .1, .1))

    async def test_cancel_closes_without_final_result(self):
        s = Chunks([chunk('a'), chunk('b', 'stop')], [0, 10])
        c, call = adapter(s)
        task = asyncio.create_task(ms.create(c, model='m', messages=[]))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(s.closed)
        call.assert_awaited_once()

    async def test_payload_preserves_audio_images_and_json_format(self):
        c, call = adapter(Chunks([chunk('{}', 'stop')]))
        messages = [{'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {'data': 'PRIVATE', 'format': 'mp3'}},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,PRIVATE'}}]}]
        await ms.create(c, model='m', messages=messages, modalities=['text'], response_format={'type': 'json_object'}, temperature=.2)
        self.assertIs(call.await_args.kwargs['messages'], messages)
        self.assertEqual(call.await_args.kwargs['modalities'], ['text'])
        self.assertEqual(call.await_args.kwargs['response_format'], {'type': 'json_object'})

    async def test_full_adapter_response_is_accepted(self):
        c, _ = adapter(full())
        self.assertEqual((await ms.create(c, model='m', messages=[])).choices[0].message.content, 'hello')

    async def test_full_adapter_without_finish_is_rejected(self):
        c, _ = adapter(full(finish=None))
        with self.assertRaises(ms.IncompleteStream):
            await ms.create(c, model='m', messages=[])

    async def test_explicit_unsupported_stream_falls_back_and_caches_route(self):
        class Unsupported(Exception):
            status_code = 400
            body = {'error': {'message': 'stream is not supported'}}
        c, call = adapter(side_effect=[Unsupported(), full(), full()])
        await ms.create(c, model='m', messages=[])
        await ms.create(c, model='m', messages=[])
        self.assertEqual(call.await_count, 3)
        self.assertTrue(call.await_args_list[0].kwargs['stream'])
        self.assertNotIn('stream', call.await_args_list[1].kwargs)
        self.assertNotIn('stream', call.await_args_list[2].kwargs)

    async def test_other_errors_do_not_downgrade(self):
        for status, body in ((401, 'stream unsupported'), (403, 'stream unsupported'), (429, 'stream unsupported'),
                             (500, 'stream unsupported'), (400, 'response_format unsupported for streaming'),
                             (400, 'upstream model unsupported'), (400, 'invalid model')):
            error = type('Rejected', (Exception,), {'status_code': status, 'body': body})()
            c, call = adapter(side_effect=error)
            with self.subTest(status=status, body=body):
                with self.assertRaises(type(error)):
                    await ms.create(c, model='m', messages=[])
                call.assert_awaited_once()

    async def test_nonstream_switch_does_not_send_stream_parameter(self):
        c, call = adapter(full())
        with patch.dict(os.environ, {'QQ_BOT_STREAMING': '0'}):
            await ms.create(c, model='m', messages=[])
        self.assertNotIn('stream', call.await_args.kwargs)

    def test_original_timeout_becomes_attempt_cap(self):
        self.assertEqual(ms._policy(NS(timeout=145), {}).total, 145)
        self.assertEqual(ms._policy(NS(timeout=90), {'timeout': 180}).total, 180)
        self.assertEqual(ms._policy(NS(timeout=httpx.Timeout(120)), {}).total, 120)

    def test_all_production_completions_go_through_receiver(self):
        for path in Path(__file__).parent.glob('*.py'):
            if path.name.startswith('test_') or path.name == 'model_stream.py':
                continue
            for n in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    self.assertFalse(ast.unparse(n.func).endswith('.chat.completions.create'), path.name)


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False
    async def __aiter__(self):
        for b in self.data:
            yield b
    async def aclose(self):
        self.closed = True


class RealSDKTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ms._unsupported.clear()
        self.enterContext(patch.dict(os.environ, {'QQ_BOT_STREAMING': '1'}))

    async def run_client(self, data, content_type='text/event-stream', status=200):
        stream = Bytes(data)
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(status, headers={'content-type': content_type}, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with AsyncOpenAI(api_key='offline-fake', base_url='https://offline.invalid/v1', http_client=http) as client:
                try:
                    result = await ms.create(client, model='m', messages=[{'role': 'user', 'content': 'offline'}])
                finally:
                    self.assertTrue(stream.closed)
        return result, requests

    async def test_real_sdk_sse_split_at_every_byte_including_utf8(self):
        data = ('data: ' + chunk('你好').model_dump_json() + '\n\n' +
                'data: ' + chunk(finish='stop').model_dump_json() + '\n\ndata: [DONE]\n\n').encode()
        result, requests = await self.run_client([bytes([b]) for b in data])
        self.assertEqual(result.choices[0].message.content, '你好')
        self.assertTrue(requests[0]['stream'])

    async def test_real_sdk_done_without_finish_discards_output(self):
        with self.assertRaises(ms.IncompleteStream):
            await self.run_client([('data: ' + chunk('partial').model_dump_json() + '\n\ndata: [DONE]\n\n').encode()])

    async def test_real_sdk_json_response_when_gateway_ignores_stream(self):
        value = dict(id='test', model='m', object='chat.completion', created=1,
                     choices=[dict(index=0, message=dict(role='assistant', content='full json'), finish_reason='stop')])
        result, _ = await self.run_client([json.dumps(value).encode()], 'application/json')
        self.assertEqual(result.choices[0].message.content, 'full json')

    async def test_real_sdk_error_event_is_not_text(self):
        from openai import APIError
        with self.assertRaises(APIError):
            await self.run_client([b'data: {"error":{"message":"PRIVATE_FAILURE"}}\n\n'])

    async def test_real_sdk_http_error_is_not_retried(self):
        with self.assertRaises(BadRequestError):
            await self.run_client([b'{"error":{"message":"bad model"}}'], 'application/json', 400)


class ChatCommitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_model_decision import SemanticIntegrationTests
        SemanticIntegrationTests.setUp(self)
        self.enterContext(patch.dict(os.environ, {'QQ_BOT_STREAMING': '1'}))

    async def test_incomplete_decision_never_commits_memory_or_proposal(self):
        import bot
        self.create.return_value = Chunks([chunk('{"kind":"operation","reply":"changed","action":')])
        self.assertIsNone(await bot.ask_deepseek(self.key, '帮我改一下模式'))
        self.assertFalse(self.router.pending)
        self.assertFalse(any(m.get('role') == 'assistant' for m in self.history[self.key]))
        self.create.assert_awaited_once()

    async def test_complete_stream_enters_original_chat_once(self):
        import bot
        from test_model_decision import envelope
        raw = envelope(reply='完整后再回复。')
        self.create.return_value = Chunks([chunk(raw[:12]), chunk(raw[12:]), chunk(finish='stop')])
        self.assertEqual(await bot.ask_deepseek(self.key, '测试一下'), '完整后再回复。')
        self.assertEqual(len([m for m in self.history[self.key] if m.get('role') == 'assistant']), 1)
        self.assertFalse(self.router.pending)


class ReadingCommitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_reading_retry import IntegrationTests
        IntegrationTests.setUp(self)
        self.enterContext(patch.dict(os.environ, {'QQ_BOT_STREAMING': '1'}))

    async def test_partial_streams_retry_once_and_keep_saved_cursor(self):
        import bot
        import paper_reader
        import reading_bridge
        from reading_sessions import ReadingSessions, PriorityGate
        now = [100000.0]
        store = ReadingSessions(self.root, clock=lambda: now[0])
        gate = PriorityGate(clock=lambda: now[0])
        self.doc['_reading_sha'] = store.cache_pdf(b'%PDF-streaming-fixture')
        job = store.start('group_123_456', self.doc, 'previous reflection')
        now[0] += 31
        self.api.side_effect = [Chunks([chunk('{"note":"half')]), Chunks([chunk('{"note":"half')])]
        extract = AsyncMock(return_value={'start': 101, 'end': 158, 'total': 158, 'units': [
            {'label': '第101页', 'text': 'Synthetic text ' * 10}], 'empty': [], 'kind': 'PDF文字', 'truncated': True})
        publish = AsyncMock()
        await store.tick(gate, lambda: True, extract, paper_reader.build_context,
            lambda job, payload, retained: reading_bridge.generate(bot, job, payload, retained), publish)
        self.assertEqual((job['next_sample'], job['errors'], job['reflection']), (101, 1, 'previous reflection'))
        self.assertEqual(self.api.await_count, 2)
        publish.assert_not_awaited()
        self.assertEqual(ReadingSessions(self.root, clock=lambda: now[0]).current('group_123_456')['next_sample'], 101)
