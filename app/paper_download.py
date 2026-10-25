"""Bounded public document GETs, preserving the caller's validated resolver."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
import inspect
import re
import ssl
import time
from urllib.parse import urljoin
import uuid

import aiohttp

CONNECT_SECONDS = 30
ATTEMPT_SECONDS = 60
READ_SECONDS = 30
DOWNLOAD_SECONDS = 125
DOWNLOAD_STAGE_SECONDS = 130
GROUP_STAGE_SECONDS = 300
LINK_STAGE_SECONDS = 300
_active_attempt = ContextVar('paper_download_attempt', default=None)
_logger = None


def set_logger(logger):
    global _logger
    _logger = logger


def _log(message):
    try:
        if _logger:
            _logger('INFO', message)
        else:
            print(message, flush=True)
    except Exception:
        pass  # Diagnostics must not turn a successful transfer into a failed one.


class _Attempt:
    def __init__(self, request_id, number):
        self.request_id, self.number = request_id, number
        self.started = time.monotonic()
        self.phase, self.received = 'setup', 0
        self.connection_started = None

    def event(self, event, **values):
        # All fields are internal enums/numbers. Never stringify exceptions, URLs, hosts, or headers.
        fields = dict(id=self.request_id, attempt=self.number, event=event,
                      elapsed_ms=round((time.monotonic() - self.started) * 1000),
                      phase=self.phase, bytes=self.received, **values)
        _log('[PAPER_DOWNLOAD] ' + ' '.join(f'{k}={v}' for k, v in fields.items()))


@contextmanager
def resolver_phase(name):
    """Optional VM hook: wrap the actual DNS/DoH/check await; never pass URLs or error text."""
    if name not in ('system_dns', 'doh', 'address_validation'):
        raise ValueError('Unknown resolver phase')
    attempt = _active_attempt.get()
    started = time.monotonic()
    if attempt:
        attempt.phase = 'resolver_' + name
        attempt.event('resolver_' + name + '_start')
    try:
        yield
    except BaseException:
        if attempt:
            attempt.event('resolver_' + name + '_failed', duration_ms=round((time.monotonic() - started) * 1000))
        raise
    else:
        if attempt:
            attempt.event('resolver_' + name + '_end', duration_ms=round((time.monotonic() - started) * 1000))
            attempt.phase = 'resolver'


class TimedResolver(aiohttp.abc.AbstractResolver):
    """Time a validating resolver without reinterpreting its connection addresses.

    The delegate enforces the public-name policy. A trusted platform may route
    a validated name through a non-public DNS mapping; those rows are transport
    data, not the public DNS answers which the delegate validated.
    """
    def __init__(self, delegate, error, attempt):
        self.delegate, self.error, self.attempt = delegate, error, attempt

    async def resolve(self, host, port=0, family=0):
        attempt = self.attempt
        attempt.phase = 'resolver'
        attempt.event('resolver_start')
        started = time.monotonic()
        rows = await self.delegate.resolve(host, port, family)
        # Preserve the delegate's validation decision and exact connection rows.
        # Do not classify mapping addresses here or swallow the delegate's errors.
        if not rows:
            raise self.error('文献解析器未返回连接地址，已停止读取。')
        attempt.event('resolver_end', duration_ms=round((time.monotonic() - started) * 1000))
        attempt.phase = 'connection'
        return rows

    async def close(self):
        result = self.delegate.close()
        if inspect.isawaitable(result):
            await result


def _trace(attempt):
    trace = aiohttp.TraceConfig()
    async def connecting(*_args):
        attempt.phase, attempt.connection_started = 'connection', time.monotonic()
        attempt.event('connection_start')
    async def connected(*_args):
        attempt.phase = 'response_headers'
        elapsed = time.monotonic() - (attempt.connection_started or time.monotonic())
        attempt.event('connection_ready', duration_ms=round(elapsed * 1000))
    async def headers_sent(*_args):
        attempt.phase = 'response_headers'
        attempt.event('request_headers_sent')
    trace.on_connection_create_start.append(connecting)
    trace.on_connection_create_end.append(connected)
    if hasattr(trace, 'on_request_headers_sent'):
        trace.on_request_headers_sent.append(headers_sent)
    return trace


class _RetryHTTP(Exception):
    def __init__(self, status, delay):
        self.status, self.delay = status, delay


def _retry_after(value):
    if not value or len(value) > 128:
        return 1.0
    try:
        if re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):
            return float(value)
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError, OSError):
        return 1.0


async def _one(url, resolver_factory, validate_url, error, max_bytes, attempt):
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    resolver = TimedResolver(resolver_factory(), error, attempt)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, ssl=context)
    timeout = aiohttp.ClientTimeout(total=ATTEMPT_SECONDS, connect=CONNECT_SECONDS,
        sock_connect=CONNECT_SECONDS, sock_read=READ_SECONDS)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False, trace_configs=[_trace(attempt)]) as session:
            # One attempt deadline also covers redirects, whose individual requests have their own aiohttp timeout.
            async with asyncio.timeout(ATTEMPT_SECONDS):
                for hop in range(6):
                    url = validate_url(url)
                    attempt.phase = 'connection'
                    async with session.get(url, allow_redirects=False,
                            headers={'User-Agent': 'QQPaperReader/1.0 (user-requested paper reading)'}) as response:
                        attempt.phase = 'response_headers'
                        attempt.event('response_headers', status=response.status, hop=hop)
                        if response.status in (301, 302, 303, 307, 308):
                            if not response.headers.get('Location'):
                                raise error('文献链接重定向缺少目标地址。')
                            url = urljoin(url, response.headers['Location'])
                            continue
                        if response.status in (408, 429, 500, 502, 503, 504):
                            raise _RetryHTTP(response.status, _retry_after(response.headers.get('Retry-After')))
                        if response.status != 200:
                            raise error(f'文献下载返回 HTTP {response.status}；请上传可读取的论文文件。')
                        if response.content_length is not None and response.content_length > max_bytes:
                            raise error(f'文献超过{max_bytes // (1024 * 1024)} MB，请拆分后上传。')
                        attempt.phase = 'body'
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            if not chunk:
                                continue
                            first = attempt.received == 0
                            attempt.received += len(chunk)
                            if first:
                                attempt.event('first_body_chunk')
                            if attempt.received > max_bytes:
                                raise error(f'文献超过{max_bytes // (1024 * 1024)} MB，请拆分后上传。')
                            body.extend(chunk)
                        attempt.event('download_complete')
                        return bytes(body), response.headers.get('Content-Type', ''), url
                raise error('文献链接重定向次数过多。')
    finally:
        await resolver.close()


async def fetch_document(url, *, resolver_factory, validate_url, error, max_bytes):
    """Fetch using a resolver_factory which validates public names and fails closed.

    This is an internal dependency supplied by paper_reader.PublicResolver, not
    an untrusted user's choice. URL validation is still applied to every hop.
    """
    url = validate_url(url)
    request_id = uuid.uuid4().hex[:10]
    started = time.monotonic()
    attempt = None
    try:
        async with asyncio.timeout(DOWNLOAD_SECONDS):
            for number in (1, 2):
                attempt = _Attempt(request_id, number)
                token = _active_attempt.set(attempt)
                attempt.event('attempt_start')
                try:
                    return await _one(url, resolver_factory, validate_url, error, max_bytes, attempt)
                except asyncio.CancelledError:
                    attempt.event('cancelled')
                    raise
                except (aiohttp.ClientSSLError, aiohttp.ServerFingerprintMismatch, ssl.SSLError) as exc:
                    attempt.event('failed', error_type=type(exc).__name__)
                    raise error('文献下载证书或TLS校验失败，未重试；请检查CA证书与运行环境。') from None
                except (_RetryHTTP, asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
                    status = exc.status if isinstance(exc, _RetryHTTP) else None
                    attempt.event('temporary_failure', error_type=type(exc).__name__, status=status or 0)
                    delay = exc.delay if isinstance(exc, _RetryHTTP) else 1.0
                    remaining = DOWNLOAD_SECONDS - (time.monotonic() - started)
                    if number == 2 or delay + CONNECT_SECONDS >= remaining:
                        reason = f'HTTP {status}' if status else '超时或临时连接故障'
                        raise error(f'文献下载{reason}；已尝试{number}次，尚未完成阅读。') from None
                except error as exc:
                    attempt.event('rejected', error_type=type(exc).__name__)
                    raise
                finally:
                    _active_attempt.reset(token)
                attempt.phase = 'retry_wait'
                attempt.event('retry_scheduled', delay_ms=round(delay * 1000))
                await asyncio.sleep(delay)
    except asyncio.TimeoutError:
        if attempt:
            attempt.event('overall_timeout')
        raise error('文献下载达到125秒总期限，尚未完成阅读。') from None
