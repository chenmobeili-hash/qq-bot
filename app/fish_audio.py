"""Bounded Fish Audio TTS. No automatic retries, paid-model fallback or raw logs."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

import aiohttp

API_URL = 'https://api.fish.audio/v1/tts'
MODELS = ('s2.1-pro-free', 's2.1-pro', 's2-pro')
STATE_FILE = 'fish_tts.sqlite3'
MAX_AUDIO = 2 * 1024 * 1024


class TTSError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise TTSError('invalid_config')
    return value


def settings(config):
    cfg = config.get('fish_audio', {}) or {}
    if not isinstance(cfg, dict):
        raise TTSError('invalid_config')
    secret = cfg.get('api_key') or ''
    env = cfg.get('api_key_env', 'FISH_AUDIO_API_KEY')
    if not secret and isinstance(env, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env):
        secret = os.environ.get(env, '')
    if not isinstance(secret, str) or len(secret) > 1024 or any(ord(c) < 33 for c in secret):
        raise TTSError('invalid_key_format')
    model = cfg.get('model', 's2.1-pro-free')
    if model not in MODELS:
        raise TTSError('unsupported_model')
    voices = dict(cfg.get('voices', {}) or {})
    if cfg.get('voice_id'):
        voices.setdefault('默认音色', cfg['voice_id'])
    if len(voices) > 32 or any(not isinstance(name, str) or not re.fullmatch(r'[\w .\-]{1,40}', name)
            or not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', identity)
            for name, identity in voices.items()):
        raise TTSError('invalid_voice_catalog')
    default = cfg.get('default_voice') or next(iter(voices), '')
    if default and default not in voices:
        raise TTSError('unknown_default_voice')
    proxy = cfg.get('proxy') or None
    if proxy:
        try:
            parsed = urlsplit(proxy)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.port == 0 or parsed.query or parsed.fragment:
                raise ValueError()
        except (ValueError, TypeError):
            raise TTSError('invalid_proxy') from None
    return dict(enabled=cfg.get('enabled', False) is True, api_key=secret, model=model, voices=voices,
        default_voice=default, proxy=proxy, env_proxy=cfg.get('use_env_proxy', True) is True,
        timeout=float(_number(cfg.get('timeout_seconds', 55), 5, 90)),
        max_chars=int(_number(cfg.get('max_chars', 180), 20, 300)),
        max_seconds=float(_number(cfg.get('max_seconds', 30), 5, 60)),
        daily_limit=int(_number(cfg.get('daily_limit', 60), 0, 500)),
        scope_limit=int(_number(cfg.get('scope_daily_limit', 20), 0, 100)),
        interval=float(_number(cfg.get('auto_cooldown_seconds', 60), 0, 3600)))


def scope_key(key):
    match = re.fullmatch(r'group_(\d+)(?:_\d+)?', str(key))
    if match:
        return 'g:' + match[1]
    match = re.fullmatch(r'private_(\d+)', str(key))
    return 'p:' + match[1] if match else None


class State:
    def __init__(self, root, clock=time.time):
        self.path, self.clock = Path(root) / STATE_FILE, clock

    @contextmanager
    def db(self):
        if self.path.is_symlink() or self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024:
            raise TTSError('state_unavailable')
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
            os.chmod(self.path, 0o600)
            db.executescript('''PRAGMA journal_mode=DELETE;
                CREATE TABLE IF NOT EXISTS scopes(scope TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,voice TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS calls(ticket TEXT PRIMARY KEY,scope TEXT NOT NULL,at REAL NOT NULL,status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS calls_time ON calls(at);''')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except (sqlite3.Error, OSError):
            if db is not None and db.in_transaction:
                db.execute('ROLLBACK')
            raise TTSError('state_unavailable') from None
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            if db is not None:
                db.close()

    def profile(self, scope):
        if not re.fullmatch(r'[gp]:\d+', str(scope)):
            raise TTSError('invalid_scope')
        with self.db() as db:
            row = db.execute('SELECT enabled,voice,version FROM scopes WHERE scope=?', (scope,)).fetchone()
        return dict(enabled=bool(row[0]), voice=row[1], version=row[2]) if row else dict(enabled=False, voice='', version=0)

    def set(self, scope, *, enabled=None, voice=None):
        self.profile(scope)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO scopes(scope) VALUES(?)', (scope,))
            if enabled is not None:
                db.execute('UPDATE scopes SET enabled=?,version=version+1 WHERE scope=?', (int(enabled), scope))
            if voice is not None:
                db.execute('UPDATE scopes SET voice=?,version=version+1 WHERE scope=?', (voice, scope))

    def reserve(self, scope, ticket, cfg, manual=False):
        with self.db() as db:
            now = self.clock()
            db.execute('DELETE FROM calls WHERE at<=?', (now - 86400,))
            if db.execute('SELECT 1 FROM calls WHERE ticket=?', (ticket,)).fetchone():
                return 'already_attempted'
            if db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] >= cfg['daily_limit']:
                return 'daily_limit'
            count, latest = db.execute('SELECT COUNT(*),MAX(at) FROM calls WHERE scope=?', (scope,)).fetchone()
            if count >= cfg['scope_limit']:
                return 'scope_limit'
            if not manual and latest is not None and now - latest < cfg['interval']:
                return 'cooldown'
            db.execute('INSERT INTO calls VALUES(?,?,?,?)', (ticket, scope, now, 'attempted'))
        return None

    def finish(self, ticket, status):
        with self.db() as db:
            db.execute('UPDATE calls SET status=? WHERE ticket=?', (status, ticket))


def clean_text(text, max_chars):
    text = str(text).strip()
    if not text or len(text) > max_chars or re.search(r'\[CQ:|\[\[|```|https?://|base64://|data:|<\|', text, re.I):
        raise TTSError('unsuitable_text')
    if re.search(r'[\x00-\x08\x0b-\x1f]', text):
        raise TTSError('unsuitable_text')
    # Do not interpret model-generated emotion/control tags or Markdown as Fish commands.
    if re.search(r'\[[^\]\n]+\]|[*#`<>]', text):
        raise TTSError('unsuitable_text')
    return text


class Cache:
    def __init__(self, root, clock=time.time):
        self.root, self.clock, self.active = Path(root) / 'tts_cache', clock, set()

    def prepare(self):
        if self.root.is_symlink():
            raise TTSError('cache_unavailable')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        return self.root

    def cleanup(self):
        if not self.root.exists():
            return
        if self.root.is_symlink():
            raise TTSError('cache_unavailable')
        rows = [p for p in self.root.iterdir() if not p.is_symlink() and p.is_file()
                and re.fullmatch(r'tts-[0-9a-f]{32}\.(?:mp3|part)', p.name)]
        rows.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in rows)
        for path in rows:
            size = path.stat().st_size
            if path in self.active:
                continue
            if self.clock() - path.stat().st_mtime >= 3600 or total > 20 * 1024 * 1024:
                path.unlink(missing_ok=True)
                total -= size


async def _probe(path, max_seconds):
    executable = shutil.which('ffprobe')
    if not executable:
        raise TTSError('ffprobe_missing')
    process = await asyncio.create_subprocess_exec(executable, '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'format=duration,format_name:stream=codec_type', '-of', 'json', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(6):
            body, _ = await process.communicate()
        value = json.loads(body)
        duration = float(value['format']['duration'])
        if (process.returncode != 0 or value['format'].get('format_name') != 'mp3'
                or not any(row.get('codec_type') == 'audio' for row in value.get('streams', []))
                or not math.isfinite(duration) or not 0 < duration <= max_seconds):
            raise TTSError('invalid_audio')
        return duration
    except (ValueError, KeyError, TypeError):
        raise TTSError('invalid_audio') from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def synthesize(text, voice_id, cfg, cache, *, guard=lambda: True, log=None):
    started = time.monotonic()
    def record(phase, **fields):
        if log:
            try:
                log('INFO', '[FISH_TTS] phase=' + phase + ' elapsed_ms=' + str(round((time.monotonic()-started)*1000))
                    + ''.join(' '+key+'='+str(value) for key,value in fields.items()))
            except Exception:
                pass
    text = clean_text(text, cfg['max_chars'])
    if not cfg['enabled'] or not cfg['api_key'] or not voice_id:
        raise TTSError('not_configured')
    if not shutil.which('ffprobe'):
        raise TTSError('ffprobe_missing')
    if not guard():
        raise TTSError('context_changed')
    path = None
    try:
        async with asyncio.timeout(cfg['timeout']):
            cache.prepare()
            cache.cleanup()
            path = cache.root / ('tts-' + uuid.uuid4().hex + '.mp3')
            cache.active.add(path)
            timeout = aiohttp.ClientTimeout(total=cfg['timeout'], connect=min(20, cfg['timeout']), sock_read=min(25, cfg['timeout']))
            async with aiohttp.ClientSession(timeout=timeout, trust_env=cfg['env_proxy']) as session:
                record('request_start')
                async with session.post(API_URL, headers={'Authorization': 'Bearer ' + cfg['api_key'], 'model': cfg['model']},
                    json={'text': text, 'reference_id': voice_id, 'format': 'mp3', 'mp3_bitrate': 64,
                          'sample_rate': 44100, 'normalize': True, 'prosody': {'speed': 1.0}},
                    proxy=cfg['proxy'], allow_redirects=False, ssl=True) as response:
                    record('headers', status=response.status)
                    if response.status != 200:
                        raise TTSError({401:'auth_failed',402:'quota_or_balance',403:'access_denied',429:'rate_limit',503:'service_unavailable'}.get(response.status, 'http_failure'))
                    if any(kind in response.headers.get('Content-Type', '').lower() for kind in ('json','text/')):
                        raise TTSError('invalid_audio')
                    if response.content_length is not None and response.content_length > MAX_AUDIO:
                        raise TTSError('audio_too_large')
                    body = bytearray()
                    async for piece in response.content.iter_chunked(16384):
                        if not guard():
                            raise TTSError('context_changed')
                        if not body:
                            record('first_body')
                        body.extend(piece)
                        if len(body) > MAX_AUDIO:
                            raise TTSError('audio_too_large')
                    if not body:
                        raise TTSError('empty_audio')
                    record('audio_received', bytes=len(body))
            if not guard():
                raise TTSError('context_changed')
            path.write_bytes(body)
            os.chmod(path, 0o600)
            duration = await _probe(path, cfg['max_seconds'])
            record('audio_validated', duration_ms=round(duration*1000))
            if not guard():
                raise TTSError('context_changed')
            return path, duration
    except BaseException as exc:
        if path:
            cache.active.discard(path)
            path.unlink(missing_ok=True)
        if isinstance(exc, TTSError) or not isinstance(exc, Exception):
            raise
        raise TTSError('timeout' if isinstance(exc, TimeoutError) else 'connection_or_decode_failure') from None
