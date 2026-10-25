"""Download one bounded real Bilibili stream, then decode only local bytes."""
import asyncio
from urllib.parse import urljoin, urlsplit

import aiohttp

import media_reader


def media_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    cdn = any(host.endswith('.' + domain) for domain in ('bilivideo.com', 'bilivideo.cn'))
    akamai = host.startswith('upos-') and host.endswith('-mirrorakam.akamaized.net')
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not (cdn or akamai)):
        raise media_reader.MediaError('B站没有提供受支持的真实视频流地址')
    return url


async def download(url):
    from bilibili_feed import USER_AGENT
    async with asyncio.timeout(60), aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=25, sock_read=20), trust_env=True,
            headers={'User-Agent': USER_AGENT, 'Referer': 'https://www.bilibili.com/'}) as session:
        for _ in range(4):
            async with session.get(media_url(url), allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = media_url(urljoin(url, response.headers.get('Location', '')))
                    continue
                response.raise_for_status()
                if int(response.headers.get('Content-Length') or 0) > media_reader.MAX_VIDEO_BYTES:
                    raise media_reader.MediaError('低清视频仍超过64MB，请截取片段后发送')
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > media_reader.MAX_VIDEO_BYTES:
                        raise media_reader.MediaError('视频流超过64MB，已停止下载')
                return bytes(body)
    raise media_reader.MediaError('视频流重定向过多')


async def capture_frames(account, bvid, cid, duration, directory):
    media, _ = await account.request('api.bilibili.com', '/x/player/playurl',
        params={'bvid': bvid, 'cid': cid, 'qn': 16, 'fnval': 0, 'platform': 'html5'}, cookies=account.cookies())
    segments = media.get('durl') or []
    if len(segments) != 1:
        return [], '未取得单段可读取的媒体流，未抽帧'
    body = await download(media_url(segments[0].get('url', '')))
    result = await media_reader.capture_video(body, directory, frame_limit=40)
    frames = result['frames']
    return frames, ('实际抽取' + str(len(frames)) + '张视频采样；需视觉模型分批查看后才计为已看，未读取音轨')
