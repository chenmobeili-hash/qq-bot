"""Bounded local decoding to small MP3 chunks; no remote paths or permanent audio."""
import asyncio
import base64
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import media_reader
from media_reader import MediaError

MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 1800
CHUNK_SECONDS = 30
MAX_CHUNKS = 61


def audio_format(body):
    if body.startswith(b'RIFF') and body[8:12] == b'WAVE':
        return 'wav', '.wav'
    if body.startswith(b'ID3') or (len(body) > 1 and body[0] == 255 and body[1] & 0xE0 == 0xE0):
        return ('aac', '.aac') if len(body) > 1 and body[1] & 0xF6 == 0xF0 else ('mp3', '.mp3')
    if body.startswith(b'fLaC'):
        return 'flac', '.flac'
    if body.startswith(b'OggS'):
        return 'ogg', '.ogg'
    if body.startswith(b'#!AMR'):
        return 'amr', '.amr'
    if body.startswith((b'#!SILK', b'\x02#!SILK')):
        raise MediaError('QQ语音仍是SILK格式，当前解码器不能读取；请改发MP3/WAV或让接入层转为MP3')
    try:
        return media_reader.video_format(body)
    except MediaError:
        raise MediaError('不支持此音频编码，请发MP3、WAV、M4A、FLAC、Ogg或受支持的视频') from None


async def chunks_from_path(path, format_name, directory):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise MediaError('缺少FFmpeg/ffprobe，尚未提取音轨')
    raw = await media_reader._process([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-f', format_name, '-select_streams', 'a:0', '-show_entries', 'format=duration:stream=duration',
        '-of', 'json', path], 15, 65536)
    try:
        probe = json.loads(raw)
        if not probe.get('streams'):
            return {'audio_chunks': [], 'audio_status': '源文件没有音轨', 'audio_duration': 0}
        duration = float(probe.get('format', {}).get('duration') or probe['streams'][0].get('duration') or 0)
    except (ValueError, TypeError, KeyError, IndexError):
        raise MediaError('无法确认音轨时长，尚未读取声音') from None
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise MediaError('音频需有有效时长且不超过30分钟，请截取较短片段')
    output = Path(directory) / 'chunk-%03d.mp3'
    await media_reader._process([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-protocol_whitelist', 'file,pipe', '-f', format_name, '-i', path, '-map', '0:a:0',
        '-vn', '-t', str(duration), '-ac', '1', '-ar', '16000', '-threads', '1',
        '-c:a', 'libmp3lame', '-b:a', '32k', '-f', 'segment', '-segment_time', str(CHUNK_SECONDS),
        '-reset_timestamps', '1', output], 90, 65536)
    files = sorted(Path(directory).glob('chunk-*.mp3'))
    if not files or len(files) > MAX_CHUNKS or sum(p.stat().st_size for p in files) > 12 * 1024 * 1024:
        raise MediaError('音轨解码结果为空或超过分段上限')
    chunks = []
    for index, file in enumerate(files):
        os.chmod(file, 0o600)
        body = await asyncio.to_thread(file.read_bytes)
        if not body or len(body) > 512 * 1024:
            raise MediaError('音频片段超过上限')
        start = index * CHUNK_SECONDS
        chunks.append({'start': start, 'end': min(duration, start + CHUNK_SECONDS),
                       'format': 'mp3', 'base64': base64.b64encode(body).decode('ascii')})
    return {'audio_chunks': chunks, 'audio_status': '已提取音轨，尚未提交听觉模型', 'audio_duration': duration}


async def prepare(body, directory):
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
        raise MediaError('音频为空或超过64MB')
    format_name, suffix = audio_format(body)
    directory = media_reader._directory(directory)
    async with asyncio.timeout(120):
        with tempfile.TemporaryDirectory(prefix='audio-', dir=directory) as temporary:
            path = Path(temporary) / ('input' + suffix)
            await asyncio.to_thread(path.write_bytes, body)
            os.chmod(path, 0o600)
            result = await chunks_from_path(path, format_name, temporary)
            return {'kind': '音频', 'duration': result['audio_duration'], 'frames': [], **result}
