"""Image-only DoH resolution to verified public IPv4 transport addresses."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp

DOH_SECONDS = 8.0
DOH_ENDPOINTS = ('https://dns.google/resolve', 'https://cloudflare-dns.com/dns-query')


class ImageAddressError(ValueError):
    pass


class ImageDoHUnavailable(aiohttp.ClientConnectionError):
    pass


def validate_image_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme not in ('https', 'http') or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, 80, 443)
                or any(ord(c) < 33 for c in url) or '\\' in url
                or host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local'))):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise ValueError()
        return url  # Do not rewrite the signed path/query, hostname, or scheme.
    except (ValueError, TypeError):
        raise ImageAddressError('图片地址不符合公网HTTP/HTTPS要求') from None


def public_answers(data):
    if not isinstance(data, dict) or data.get('Status') != 0 or not isinstance(data.get('Answer'), list):
        raise ImageAddressError('图片DoH未返回有效地址')
    addresses = []
    for answer in data['Answer']:
        if not isinstance(answer, dict):
            raise ImageAddressError('图片DoH响应无效')
        if answer.get('type') not in (1, 28):
            continue  # A normal CNAME chain may accompany the requested A records.
        try:
            ip = ipaddress.ip_address(answer.get('data', ''))
        except ValueError:
            raise ImageAddressError('图片DoH地址无效') from None
        if not ip.is_global or (answer['type'] == 1 and ip.version != 4) or (answer['type'] == 28 and ip.version != 6):
            raise ImageAddressError('图片DoH返回非公网或无效地址')
        if ip.version == 4 and str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise ImageAddressError('图片DoH未返回公网IPv4地址')
    return addresses


class ImageDoHResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, emit=lambda *args, **kwargs: None, *, attempt=1):
        if type(attempt) is not int or attempt < 1:
            raise ValueError('Image attempt must be a positive integer')
        self.emit = emit
        self.attempt = attempt

    async def _query(self, session, endpoint, host):
        async with session.get(endpoint, params={'name': host, 'type': 'A'},
                headers={'Accept': 'application/dns-json'}, allow_redirects=False) as response:
            if response.status != 200:
                raise ImageDoHUnavailable('图片DoH服务暂时不可用')
            return await response.json(content_type=None)

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        self.emit('resolution_start', source='doh')
        try:
            async with asyncio.timeout(DOH_SECONDS):
                timeout = aiohttp.ClientTimeout(total=DOH_SECONDS / len(DOH_ENDPOINTS))
                # Only DNS-over-HTTPS uses the environment proxy. Image bytes connect directly.
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True,
                        cookie_jar=aiohttp.DummyCookieJar()) as session:
                    for index, endpoint in enumerate(DOH_ENDPOINTS):
                        try:
                            data = await self._query(session, endpoint, host)
                        except asyncio.CancelledError:
                            raise
                        except (asyncio.TimeoutError, aiohttp.ClientError):
                            self.emit('doh_endpoint_failed', source='doh', endpoint=index + 1)
                            continue
                        except (ValueError, TypeError):
                            raise ImageAddressError('图片DoH响应无效') from None
                        addresses = public_answers(data)  # A validation rejection is final, never bypassed.
                        self.emit('resolved', source='doh', addresses=len(addresses))
                        # Select only after validating the entire answer. DNS order cannot change the rank.
                        ordered = sorted(addresses, key=ipaddress.IPv4Address)
                        rank = min(self.attempt - 1, len(ordered) - 1)
                        selected = ordered[rank]
                        self.emit('dial_selected', source='doh', dialed=selected, rank=rank + 1)
                        return [{'hostname': host, 'host': selected, 'port': port, 'family': socket.AF_INET,
                                 'proto': socket.IPPROTO_TCP, 'flags': socket.AI_NUMERICHOST}]
        except asyncio.TimeoutError:
            self.emit('resolution_timeout', source='doh')
            raise ImageDoHUnavailable('图片DoH解析超时') from None
        raise ImageDoHUnavailable('图片DoH解析未完成')

    async def close(self):
        pass  # Each resolution closes its own session; no cache or background resources.
