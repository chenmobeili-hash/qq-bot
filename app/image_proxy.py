"""Cache-first, fail-closed image validation, proxy-first transport, pinned fallback."""
import asyncio
from ipaddress import ip_address
import socket
import time
import uuid

import aiohttp
from yarl import URL

from image_network import ImageAddressError, ImageDoHResolver, validate_image_url
import qq_media

CONNECT_SECONDS = 5.0
IDLE_SECONDS = 8.0
START_SECONDS = 12.0


def validate_target(location):
    validate_image_url(location)
    url = URL(location, encoded=True)
    try:
        ip_address(url.raw_host)
    except ValueError:
        return url
    # The image brief explicitly rejects literal targets. Media's cache=False
    # direct entry retains its original public-IP support, without this guard.
    raise ImageAddressError('图片下载需要有效的公网主机名')


async def preflight(location, emit):
    target = validate_target(location)
    def dns_event(event, **fields):
        if event != 'dial_selected':  # Validation is not a connection to the selected source IP.
            emit(event, **fields)
    resolver = ImageDoHResolver(dns_event)
    try:
        try:
            await resolver.resolve(target.raw_host, target.port, socket.AF_INET)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, ValueError, TypeError):
            raise ImageAddressError('图片公网解析校验未完成或被拒绝') from None
    finally:
        await resolver.close()
    return target


async def proxy_download(location, proxy, max_bytes, emit):
    phase = 'connect'
    trace = aiohttp.TraceConfig()
    async def ready(*_args):
        nonlocal phase
        phase = 'headers'
        emit('connection_ready')
    async def sent(*_args):
        nonlocal phase
        phase = 'headers'
    trace.on_connection_create_end.append(ready)
    trace.on_request_headers_sent.append(sent)
    timeout = aiohttp.ClientTimeout(total=None, connect=CONNECT_SECONDS, sock_connect=CONNECT_SECONDS, sock_read=IDLE_SECONDS)
    try:
        async with asyncio.timeout(START_SECONDS) as starter, aiohttp.ClientSession(timeout=timeout,
                trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), trace_configs=[trace],
                headers={'Accept-Encoding': 'identity'}) as session:
            current = URL(location, encoded=True)  # Already validated before selecting either route.
            for hop in range(11):
                phase = 'connect'
                async with session.get(current, proxy=proxy[0], proxy_auth=proxy[1], allow_redirects=False) as response:
                    emit('response_headers', status=response.status)
                    if response.status in (301, 302, 303, 307, 308) and response.headers.get('Location') and hop < 10:
                        from urllib.parse import urljoin
                        current = await preflight(urljoin(str(current), response.headers['Location']), emit)
                        continue
                    if not 200 <= response.status < 300 or response.status == 206:
                        raise qq_media._ProxyFailure('http', response.status)
                    try:
                        declared = int(response.headers.get('Content-Length') or 0)
                    except (TypeError, ValueError):
                        raise qq_media._ProxyFailure('length') from None
                    if declared < 0:
                        raise qq_media._ProxyFailure('length')
                    if max_bytes is not None and declared > max_bytes:
                        raise ImageAddressError('图片超过下载大小上限')
                    phase = 'body'
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if not chunk:
                            continue
                        if max_bytes is not None and len(body) + len(chunk) > max_bytes:
                            raise ImageAddressError('图片超过下载大小上限')
                        body.extend(chunk)
                        if len(body) == len(chunk):
                            starter.reschedule(None)
                            emit('first_body_chunk', bytes=len(body))
                    identity = response.headers.get('Content-Encoding', 'identity').lower() in ('identity', '')
                    if not body or (identity and declared and len(body) != declared):
                        raise qq_media._ProxyFailure('length')
                    emit('download_complete', bytes=len(body))
                    return bytes(body), response.headers.get('Content-Type', 'image/jpeg')
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        raise qq_media._ProxyFailure('connect' if phase == 'connect' else 'stall') from None
    except aiohttp.ClientHttpProxyError as exc:
        raise qq_media._ProxyFailure('http', exc.status) from None
    except aiohttp.ClientSSLError:
        raise qq_media._ProxyFailure('tls') from None
    except aiohttp.ClientConnectorError:
        raise qq_media._ProxyFailure('connect') from None
    except aiohttp.ClientPayloadError:
        raise qq_media._ProxyFailure('payload') from None
    except aiohttp.ClientConnectionError:
        raise qq_media._ProxyFailure('connect' if phase == 'connect' else 'transport') from None
    raise qq_media._ProxyFailure('http')


def proxy_only(bot, *, config=None):
    """Read the image-only policy on cache misses; malformed policy fails closed."""
    if config is None:
        loader = getattr(bot, 'load_config', None)
        config = loader() if callable(loader) else {}
    if not isinstance(config, dict):
        raise ImageAddressError('图片下载配置无效')
    policy = config.get('image_download', {})
    if policy is None:
        policy = {}
    if not isinstance(policy, dict) or type(policy.get('proxy_only', False)) is not bool:
        raise ImageAddressError('image_download.proxy_only必须为布尔值')
    return policy.get('proxy_only', False)


def configured_image_proxy(value):
    """An image-only HTTP proxy; malformed explicit settings never fall back."""
    try:
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError()
        proxy = URL(value.strip())
        if (proxy.scheme not in ('http', 'https') or not proxy.host or proxy.fragment
                or proxy.query_string or proxy.path not in ('', '/')):
            raise ValueError()
        proxy.port
        auth = aiohttp.BasicAuth.from_url(proxy) if proxy.user is not None else None
        return proxy.with_user(None), auth
    except (ValueError, TypeError, UnicodeError):
        raise ImageAddressError('image_download.image_proxy_url需要有效的HTTP或HTTPS代理地址') from None


async def strict_proxy_download(location, proxy, max_bytes, emit):
    """One bounded retry through the same proxy, within download_image's deadline."""
    for attempt in (1, 2):
        emit('attempt_start', attempt=attempt, attempts=2)
        try:
            return await proxy_download(location, proxy, max_bytes, emit)
        except qq_media._ProxyFailure as exc:
            emit('attempt_failure', attempt=attempt, category=exc.category,
                 **({'status': exc.status} if exc.status is not None else {}))
            # Never retry denied requests, bad certificates, invalid DNS/URLs,
            # size errors or malformed responses, and never select a new route.
            if attempt == 2 or exc.category not in ('connect', 'stall', 'transport'):
                raise
            emit('retry_scheduled', next_attempt=2, delay_seconds=1)
            await asyncio.sleep(1)


async def download_image(bot, location, image_index, *, max_bytes=None):
    started = time.monotonic()
    request_id = uuid.uuid4().hex[:10]
    route, phase = 'direct', 'url_validation'
    def emit(event, **fields):
        detail = {'id': request_id, 'image': image_index, 'event': event, 'route': route,
                  'phase': phase, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                  'dialed': '-', **fields}
        bot.log('INFO', '[IMAGE_DOWNLOAD] ' + ' '.join(f'{key}={value}' for key, value in detail.items()))
    try:
        async with asyncio.timeout(bot.VISION_DOWNLOAD_TIMEOUT):
            validate_target(location)
            phase = 'cache'
            try:
                cached = await asyncio.wait_for(asyncio.to_thread(bot._get_vision_image_cache().get, location), 1.0)
            except (OSError, ValueError, asyncio.TimeoutError):
                emit('cache_read_failed')
                cached = None
            if cached is not None:
                emit('cache_hit', bytes=len(cached[0]), route='cache')
                return cached
            loader = getattr(bot, 'load_config', None)
            config = loader() if callable(loader) else {}
            strict = proxy_only(bot, config=config)
            policy = config.get('image_download') or {}
            proxy, config_failure = None, None
            if 'image_proxy_url' in policy:
                route = 'proxy'
                proxy = configured_image_proxy(policy['image_proxy_url'])
                emit('proxy_override_selected', source='image_config')
            else:
                try:
                    proxy = qq_media._environment_proxy(location)
                except qq_media._ProxyFailure as exc:
                    config_failure = exc.category
            route = 'proxy' if strict or proxy is not None or config_failure else 'direct'
            if strict:
                emit('proxy_only_policy')
                if proxy is None:
                    emit('proxy_only_failure', category=config_failure or 'proxy_missing')
                    return None
            phase = 'validation'
            await preflight(location, emit)  # Fail closed before proxy or source connection.
            result = None
            if proxy is not None:
                phase = 'proxy_download'
                try:
                    if strict:
                        result = await strict_proxy_download(location, proxy, max_bytes, emit)
                    else:
                        emit('attempt_start')
                        result = await proxy_download(location, proxy, max_bytes, emit)
                except qq_media._ProxyFailure as exc:
                    if strict:
                        emit('proxy_only_failure', category=exc.category,
                             **({'status': exc.status} if exc.status is not None else {}))
                        return None
                    emit('fallback', next_route='direct', category=exc.category,
                         **({'status': exc.status} if exc.status is not None else {}))
                except (aiohttp.ClientError, OSError):
                    if strict:
                        emit('proxy_only_failure', category='transport')
                        return None
                    emit('fallback', next_route='direct', category='transport')
            elif config_failure:
                emit('fallback', next_route='direct', category=config_failure)
            if result is None:
                if strict:
                    emit('proxy_only_failure', category='empty_result')
                    return None
                route, phase = 'direct', 'direct_download'
                emit('route_start')
                result = await bot._download_vision_image_direct(location, image_index, cache=False,
                    max_bytes=max_bytes, _url_validator=validate_target)
            if result is not None and bot.VISION_DOWNLOAD_TIMEOUT - (time.monotonic() - started) > 1.1:
                phase = 'cache'
                try:
                    await asyncio.wait_for(asyncio.to_thread(bot._get_vision_image_cache().put, location, *result), 1.0)
                    emit('cache_stored')
                except (OSError, ValueError, asyncio.TimeoutError):
                    emit('cache_store_failed')
            return result
    except asyncio.CancelledError:
        emit('attempt_cancelled')
        raise
    except asyncio.TimeoutError:
        emit('overall_timeout')
        return None
    except (ImageAddressError, aiohttp.ClientError, OSError, ValueError, TypeError) as exc:
        emit('permanent_failure', error_type=type(exc).__name__)
        return None
