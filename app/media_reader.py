"""Bounded local WebP decoding and real video sampling; never opens remote URLs."""
import asyncio
import base64
from fractions import Fraction
import io
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_WEBP_BYTES = 20 * 1024 * 1024
MAX_FRAMES = 6


class MediaError(ValueError):
    pass


def is_webp(body):
    return len(body) >= 12 and body[:4] == b'RIFF' and body[8:12] == b'WEBP'


def _directory(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise MediaError('媒体临时目录不能是符号链接')
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def cleanup(directory, now=None):
    """Remove only our stale inputs after abnormal termination; never recurse."""
    directory = Path(directory)
    if not directory.exists():
        return
    if directory.is_symlink():
        raise MediaError('媒体临时目录不能是符号链接')
    cutoff = (time.time() if now is None else now) - 3600
    for child in list(directory.iterdir())[:128]:
        if child.is_symlink() or not child.is_dir() or not re.fullmatch(r'(?:webp|video|audio)-[\w-]{6,}', child.name):
            continue
        if child.stat().st_mtime > cutoff:
            continue
        for path in child.iterdir():
            if (path.name in ('input.webp', 'input.mp4', 'input.mkv', 'input.avi', 'input.flv', 'input.mp3', 'input.wav', 'input.aac', 'input.flac', 'input.ogg', 'input.amr')
                    or re.fullmatch(r'chunk-\d{3}\.mp3', path.name)) and path.is_file() and not path.is_symlink() and path.stat().st_mtime <= cutoff:
                path.unlink()
        try:
            child.rmdir()
        except OSError:
            pass


async def _process(args, timeout, limit):
    process = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        async def collect():
            body = await process.stdout.read(limit + 1)
            if len(body) > limit:
                raise MediaError('媒体解析输出超过上限')
            # read(n) need not return n bytes. Continue to EOF, still bounded.
            while chunk := await process.stdout.read(min(65536, limit + 1 - len(body))):
                body += chunk
                if len(body) > limit:
                    raise MediaError('媒体解析输出超过上限')
            await process.wait()
            if process.returncode:
                raise MediaError('媒体文件无法解码，或缺少所需解码器')
            return body
        return await asyncio.wait_for(collect(), timeout)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def _webp_worker(path):
    try:
        from PIL import Image
    except ImportError:
        raise MediaError('缺少Pillow，请安装更新包里的依赖') from None
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with Image.open(path) as image:
        if image.format != 'WEBP' or image.width * image.height > 20_000_000:
            raise MediaError('WebP尺寸过大或格式无效')
        count = getattr(image, 'n_frames', 1)
        if not 1 <= count <= 400:
            raise MediaError('WebP超过400帧，请截取较短片段')
        selected = {round(i * (count - 1) / max(1, min(count, MAX_FRAMES) - 1)) for i in range(min(count, MAX_FRAMES))}
        frames, elapsed = [], 0
        for index in range(count):
            image.seek(index)
            image.load()  # Decode dependent/composited frames before sampling, not just the first bitmap.
            duration = image.info.get('duration', 100)
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                duration = 100
            if index in selected:
                rgba = image.convert('RGBA')
                rgba.thumbnail((768, 768))
                canvas = Image.new('RGB', rgba.size, 'white')
                canvas.paste(rgba, mask=rgba.getchannel('A'))
                stream = io.BytesIO()
                canvas.save(stream, 'JPEG', quality=85)
                frames.append({'frame': index + 1, 'second': round(elapsed / 1000, 3),
                               'base64': base64.b64encode(stream.getvalue()).decode('ascii')})
            elapsed += duration
        return {'kind': '动态WebP' if count > 1 else '静态WebP', 'animated': count > 1,
                'frames_total': count, 'duration': elapsed / 1000, 'frames': frames}


async def prepare_webp(body, directory):
    if not is_webp(body):
        return None
    if len(body) > MAX_WEBP_BYTES:
        raise MediaError('WebP超过20MB，请先缩小文件')
    directory = _directory(directory)
    with tempfile.TemporaryDirectory(prefix='webp-', dir=directory) as temporary:
        path = Path(temporary) / 'input.webp'
        assert path.resolve().is_relative_to(directory.resolve())
        await asyncio.to_thread(path.write_bytes, body)
        os.chmod(path, 0o600)
        raw = await _process([sys.executable, Path(__file__).resolve(), path], 30, 8 * 1024 * 1024)
        try:
            result = json.loads(raw)
            if result.get('error'):
                raise MediaError(result['error'])
            return result
        except (KeyError, TypeError, json.JSONDecodeError):
            raise MediaError('WebP解析未得到有效画面') from None


def video_format(body):
    if body.startswith(b'FLV\x01'):
        return 'flv', '.flv'
    if len(body) >= 12 and body[4:8] == b'ftyp':
        return 'mov', '.mp4'
    if body.startswith(b'\x1aE\xdf\xa3'):
        return 'matroska', '.mkv'
    if body.startswith(b'RIFF') and body[8:12] == b'AVI ':
        return 'avi', '.avi'
    raise MediaError('仅支持真实MP4/MOV、WebM/MKV或AVI视频；不会把封面当作视频')


async def capture_video(body, directory, *, frame_limit=MAX_FRAMES, include_audio=False):
    if not body or len(body) > MAX_VIDEO_BYTES:
        raise MediaError('视频为空或超过64MB，请截取较短片段')
    format_name, suffix = video_format(body)
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise MediaError('缺少FFmpeg/ffprobe，请在bot运行环境安装后再试')
    directory = _directory(directory)
    if type(frame_limit) is not int or not 1 <= frame_limit <= 40:
        raise MediaError('无效的抽帧数量上限')
    async with asyncio.timeout(120):
        with tempfile.TemporaryDirectory(prefix='video-', dir=directory) as temporary:
            path = Path(temporary) / ('input' + suffix)
            assert path.resolve().is_relative_to(directory.resolve())
            await asyncio.to_thread(path.write_bytes, body)
            os.chmod(path, 0o600)
            # Force a recognized container and prohibit network protocols inside media files.
            raw = await _process([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', format_name,
                '-select_streams', 'v:0', '-show_entries', 'format=duration:stream=width,height,duration,avg_frame_rate',
                '-of', 'json', path], 15, 65536)
            try:
                probe = json.loads(raw)
                stream = probe['streams'][0]
                duration = float(probe.get('format', {}).get('duration') or stream.get('duration') or 0)
                pixels = int(stream['width']) * int(stream['height'])
            except (ValueError, TypeError, KeyError, IndexError):
                raise MediaError('无法确定视频时长或画面尺寸') from None
            if not math.isfinite(duration) or not 0 < duration <= 1800 or not 0 < pixels <= 40_000_000:
                raise MediaError('视频需有有效画面且不超过30分钟；请先截取或缩小视频')
            count = min(frame_limit, max(6, math.ceil(duration / 15) + 1))
            try:
                fps = float(Fraction(stream.get('avg_frame_rate', '0/1')))
                tail = max(0.2, 1 / fps) if math.isfinite(fps) and fps > 0 else 1
            except (ValueError, ZeroDivisionError):
                tail = 1
            moments = sorted({round(max(0, duration - min(tail, duration / 2)) * i / max(1, count - 1), 3)
                              for i in range(count)})
            frames = []
            for moment in moments:
                jpeg = await _process([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-protocol_whitelist', 'file,pipe', '-ss', str(moment), '-f', format_name, '-i', path,
                    '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=768:768:force_original_aspect_ratio=decrease',
                    '-an', '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'], 12, 1_000_000)
                if not jpeg.startswith(b'\xff\xd8'):
                    continue
                frames.append({'second': moment, 'base64': base64.b64encode(jpeg).decode('ascii')})
            if not frames:
                raise MediaError('视频没有解码出有效画面')
            result = {'kind': 'QQ视频', 'duration': duration, 'frames': frames}
            if include_audio:
                import audio_reader
                try:
                    result.update(await audio_reader.chunks_from_path(path, format_name, temporary))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result.update(audio_chunks=[], audio_status='音轨提取未完成，未读取声音')
            return result


if __name__ == '__main__':
    try:
        if sys.platform != 'win32':
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        result = _webp_worker(sys.argv[1])
    except MediaError as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': 'WebP文件损坏或无法解码'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
