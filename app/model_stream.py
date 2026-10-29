"""Buffer Chat Completions SSE privately; publish only a finished completion.

No retry of a started stream. Existing callers own operation retries/deadlines.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import re
import time

import httpx
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from openai.types.chat import ChatCompletion


MAX_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = 100000
_unsupported = OrderedDict()
_request = httpx.Request('POST', 'https://model.invalid/chat/completions')


class IncompleteStream(APIConnectionError):
    def __init__(self):
        super().__init__(message='Model stream ended before its finish signal', request=_request)


class StreamProtocolError(ValueError):
    """Invalid final-only protocol; do not network-retry malformed model output."""


@dataclass(frozen=True)
class Policy:
    total: float
    first: float
    idle: float


def _seconds(value, default=90.0):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return default


def _policy(client, kwargs):
    timeout = kwargs.get('timeout', getattr(client, 'timeout', None))
    budget = _seconds(timeout, _seconds(getattr(timeout, 'read', None)))
    return Policy(budget, budget, budget)


def _emit(log, source, event, started, **fields):
    # Labels are our own constants. Never log endpoint, exception body, prompt or content.
    source = ''.join(c for c in source if c.isalnum() or c in '_-')[:40]
    message = '[MODEL_STREAM] source={} event={} elapsed_ms={}{}'.format(
        source, event, round((time.monotonic() - started) * 1000),
        ''.join(' {}={}'.format(k, v) for k, v in fields.items()))
    try:
        if log:
            log('INFO', message)
        else:
            logging.getLogger(__name__).info(message)
    except Exception:
        pass


def _route(client, model):
    return hashlib.sha256((str(getattr(client, 'base_url', '')) + '\n' + str(model)).encode()).hexdigest()


def _rejects_stream(exc):
    if getattr(exc, 'status_code', None) not in (400, 422):
        return False
    body = getattr(exc, 'body', None)
    if not isinstance(body, (dict, list, str)):
        return False
    text = json.dumps(body, ensure_ascii=False)[:8000].lower()
    return (re.search(r'\bstream(?:ing)?\b', text) and any(t in text for t in
            ('unsupported', 'not support', 'unknown parameter', 'unrecognized', 'not allowed', 'unexpected parameter'))
            and 'response_format' not in text)


def _complete(value):
    choices = getattr(value, 'choices', None)
    if not isinstance(choices, list) or not choices:
        raise StreamProtocolError('Missing completion choices')
    for choice in choices:
        if not getattr(choice, 'finish_reason', None):
            raise IncompleteStream()
        message = getattr(choice, 'message', None)
        if message is None or not isinstance(getattr(message, 'content', None), (str, type(None))):
            raise StreamProtocolError('Invalid completion content')
        if getattr(message, 'tool_calls', None) or getattr(message, 'function_call', None) or getattr(message, 'refusal', None) or getattr(message, 'audio', None):
            raise StreamProtocolError('Unsupported completion message')
    if sum(len((c.message.content or '').encode('utf-8')) for c in choices) > MAX_BYTES:
        raise StreamProtocolError('Completion exceeds buffer limit')
    return value


async def _json_response(stream):
    data = bytearray()
    async for chunk in stream.response.aiter_bytes():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise StreamProtocolError('Completion exceeds buffer limit')
    try:
        return _complete(ChatCompletion.model_validate_json(bytes(data)))
    except (ValueError, TypeError) as exc:
        if isinstance(exc, StreamProtocolError):
            raise
        raise StreamProtocolError('Invalid non-streaming completion') from None


async def _collect(stream, policy, log, source, started):
    # Some compatible gateways ignore stream=True and return a complete JSON body.
    response = getattr(stream, 'response', None)
    if isinstance(response, httpx.Response) and 'application/json' in response.headers.get('content-type', '').lower():
        result = await _json_response(stream)
        _emit(log, source, 'json_complete', started)
        return result
    if not hasattr(stream, '__aiter__'):
        # Also accepts full ChatCompletion objects from alternate client adapters.
        result = _complete(stream)
        _emit(log, source, 'nonstream_complete', started)
        return result
    iterator = stream.__aiter__()
    choices, metadata, usage = {}, {}, None
    count = size = 0
    first_text = False
    while True:
        try:
            async with asyncio.timeout(policy.first if count == 0 else policy.idle):
                chunk = await anext(iterator)
        except StopAsyncIteration:
            break
        except TimeoutError:
            _emit(log, source, 'first_chunk_timeout' if count == 0 else 'idle_timeout', started)
            raise APITimeoutError(request=_request) from None
        count += 1
        if count > MAX_CHUNKS:
            raise StreamProtocolError('Too many completion chunks')
        if count == 1:
            _emit(log, source, 'first_chunk', started)
        for field in ('id', 'model', 'created', 'system_fingerprint', 'service_tier'):
            value = getattr(chunk, field, None)
            if value is not None:
                metadata[field] = value
        if getattr(chunk, 'usage', None) is not None:
            usage = chunk.usage.model_dump() if hasattr(chunk.usage, 'model_dump') else chunk.usage
        for choice in chunk.choices:
            index = choice.index
            if type(index) is not int or index < 0 or index >= 16:
                raise StreamProtocolError('Invalid choice index')
            row = choices.setdefault(index, {'parts': [], 'finish': None})
            delta = choice.delta
            if any(getattr(delta, k, None) for k in ('tool_calls', 'function_call', 'refusal', 'audio')):
                raise StreamProtocolError('Unsupported completion delta')
            text = getattr(delta, 'content', None)
            if text is not None and not isinstance(text, str):
                raise StreamProtocolError('Invalid text delta')
            if text:
                if row['finish']:
                    raise StreamProtocolError('Content after finish signal')
                # reasoning_content and similar provider fields never enter visible output.
                size += len(text.encode('utf-8'))
                if size > MAX_BYTES:
                    raise StreamProtocolError('Completion exceeds buffer limit')
                row['parts'].append(text)
                if not first_text:
                    _emit(log, source, 'first_text', started)
                    first_text = True
            finish = choice.finish_reason
            if finish:
                if row['finish'] is not None:
                    raise StreamProtocolError('Duplicate finish signal')
                row['finish'] = finish
    if not choices or 0 not in choices or any(not row['finish'] for row in choices.values()):
        raise IncompleteStream()
    result = ChatCompletion.model_validate(dict(
        id=metadata.get('id', 'buffered'), model=metadata.get('model', ''),
        created=metadata.get('created', 0), object='chat.completion',
        system_fingerprint=metadata.get('system_fingerprint'),
        service_tier=metadata.get('service_tier'), usage=usage,
        choices=[dict(index=i, message=dict(role='assistant', content=''.join(row['parts'])),
                      finish_reason=row['finish']) for i, row in sorted(choices.items())]))
    _emit(log, source, 'complete', started, chunks=count, bytes=size)
    return _complete(result)


async def create(client, *, _stream_log=None, _stream_source='model', _stream_policy=None, **kwargs):
    """Return a full completion. Caller cancellation and deadlines always win."""
    started = time.monotonic()
    log, source = _stream_log, _stream_source
    policy = _stream_policy or _policy(client, kwargs)
    route = _route(client, kwargs.get('model'))
    cached = _unsupported.get(route, 0) > time.monotonic()
    enabled = os.environ.get('QQ_BOT_STREAMING', '1').strip().lower() not in ('0', 'false', 'off')
    # SDK retry loops can silently spend extra attempts before the caller gets control.
    # Operation-level retries remain where they were (e.g. reading 2 attempts / 300s).
    if isinstance(client, AsyncOpenAI):
        client = client.with_options(max_retries=0)
    stream = None
    deadline = asyncio.timeout(policy.total)
    try:
        async with deadline:
            if not enabled or cached:
                result = _complete(await client.chat.completions.create(**kwargs))
                _emit(log, source, 'nonstream_complete', started)
                return result
            try:
                first_deadline = asyncio.timeout(policy.first)
                async with first_deadline:
                    stream = await client.chat.completions.create(**kwargs, stream=True)
            except Exception as exc:
                if isinstance(exc, TimeoutError) and first_deadline.expired():
                    _emit(log, source, 'first_chunk_timeout', started)
                    raise APITimeoutError(request=_request) from None
                if not _rejects_stream(exc):
                    raise
                _unsupported[route] = time.monotonic() + 3600
                _unsupported.move_to_end(route)
                while len(_unsupported) > 64:
                    _unsupported.popitem(last=False)
                _emit(log, source, 'unsupported_fallback', started)
                # Before response bytes only, sharing the very same total deadline.
                result = _complete(await client.chat.completions.create(**kwargs))
                _emit(log, source, 'nonstream_complete', started)
                return result
            _emit(log, source, 'connected', started)
            return await _collect(stream, policy, log, source, started)
    except TimeoutError:
        if not deadline.expired():
            raise
        _emit(log, source, 'total_timeout', started)
        raise APITimeoutError(request=_request) from None
    except httpx.TimeoutException:
        _emit(log, source, 'read_timeout', started)
        raise APITimeoutError(request=_request) from None
    except httpx.TransportError:
        _emit(log, source, 'connection_error', started)
        raise APIConnectionError(request=_request, message='Model stream connection failed') from None
    except asyncio.CancelledError:
        _emit(log, source, 'cancelled', started)
        raise
    except Exception as exc:
        _emit(log, source, 'failed', started, error=type(exc).__name__)
        raise
    finally:
        if stream is not None and hasattr(stream, 'close'):
            try:
                async with asyncio.timeout(1):
                    await stream.close()
            except Exception:
                pass
