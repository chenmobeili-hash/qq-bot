"""Google Lens via SerpApi's documented binary upload + image_id search flow."""
import argparse
import asyncio
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

UPLOAD_URL = 'https://serpapi.com/image'
SEARCH_URL = 'https://serpapi.com/search.json'
ACCOUNT_URL = 'https://serpapi.com/account.json'
STATE_FILE = 'serpapi_lens_state.sqlite3'
MAX_UPLOAD = 480_000  # Below the documented 500 KB limit, regardless of KB convention.
MAX_RESPONSE = 2 * 1024 * 1024


class LensError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # Never expose URL, key, response body or provider wording.


def settings(config):
    cfg = config.get('serpapi', {}) or {}
    if not isinstance(cfg, dict):
        raise LensError('invalid_config')
    secret = str(cfg.get('api_key') or '').strip()
    env = cfg.get('api_key_env', 'SERPAPI_API_KEY')
    if not secret and isinstance(env, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env):
        secret = os.environ.get(env, '').strip()
    if secret and (len(secret) > 512 or any(ord(c) < 33 for c in secret)):
        raise LensError('invalid_key_format')
    try:
        timeout = min(60, max(10, float(cfg.get('timeout_seconds', 55))))
        daily = min(100, max(0, int(cfg.get('daily_limit', 20))))
        interval = min(600, max(1, float(cfg.get('min_interval_seconds', 8))))
    except (TypeError, ValueError, OverflowError):
        raise LensError('invalid_config') from None
    proxy = str(cfg.get('proxy') or '').strip()
    if proxy:
        try:
            parsed = urlsplit(proxy)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.port == 0 or parsed.query or parsed.fragment:
                raise ValueError()
        except ValueError:
            raise LensError('invalid_proxy') from None
    language = str(cfg.get('hl', 'zh-cn'))
    if not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,3})?', language):
        raise LensError('invalid_language')
    return {'enabled': cfg.get('enabled', False) is True and bool(secret), 'api_key': secret,
            'timeout': timeout, 'daily_limit': daily, 'interval': interval, 'hl': language, 'proxy': proxy or None}


def configured(config):
    try:
        return settings(config)['enabled']
    except LensError:
        return False


class Quota:
    def __init__(self, root, clock=time.time):
        self.path, self.clock = Path(root) / STATE_FILE, clock

    @contextmanager
    def db(self):
        if self.path.is_symlink() or self.path.exists() and self.path.stat().st_size > 1024 * 1024:
            raise LensError('state_unavailable')
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        os.chmod(self.path, 0o600)
        try:
            db.executescript('PRAGMA journal_mode=DELETE; CREATE TABLE IF NOT EXISTS calls(at REAL NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def reserve(self, cfg):
        try:
            with self.db() as db:
                now = self.clock()
                db.execute('DELETE FROM calls WHERE at<=?', (now - 86400,))
                count, latest = db.execute('SELECT COUNT(*),MAX(at) FROM calls').fetchone()
                if count >= cfg['daily_limit']:
                    return 'daily_limit'
                if latest is not None and now - latest < cfg['interval']:
                    return 'cooldown'
                db.execute('INSERT INTO calls VALUES(?)', (now,))
                return None
        except (sqlite3.Error, OSError):
            raise LensError('state_unavailable') from None


def prepare(body):
    if not isinstance(body, bytes) or not body or len(body) > 12 * 1024 * 1024:
        raise LensError('invalid_image')
    try:
        with Image.open(io.BytesIO(body)) as original:
            if original.width * original.height > 32_000_000 or original.width < 1 or original.height < 1:
                raise LensError('image_too_large')
            original.seek(0)
            image = ImageOps.exif_transpose(original).convert('RGB')
        # Strip EXIF/comments and compress only this outbound copy, never the main image.
        for longest in (1600, 1200, 900, 640):
            candidate = image.copy()
            candidate.thumbnail((longest, longest))
            for quality in (88, 76, 62, 45):
                output = io.BytesIO()
                candidate.save(output, format='JPEG', quality=quality, optimize=True)
                data = output.getvalue()
                if len(data) <= MAX_UPLOAD:
                    return data
        raise LensError('image_too_large')
    except LensError:
        raise
    except Exception:
        raise LensError('invalid_image') from None


def public_link(value):
    if not isinstance(value, str) or len(value) > 1500 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            return None
        # Only display sources; never fetch result URLs or thumbnails automatically.
        if re.search(r'(?i)(?:api[_-]?key|token|signature|auth)=', parsed.query):
            return None
        return value
    except ValueError:
        return None


def normalize(data):
    rows, seen = [], set()
    for field, exact in (('exact_matches', True), ('visual_matches', False)):
        items = data.get(field, [])
        if not isinstance(items, list):
            continue
        for row in items[:30]:
            if not isinstance(row, dict):
                continue
            link = public_link(row.get('link'))
            if not link or link in seen:
                continue
            title = re.sub(r'[\x00-\x1f]', ' ', str(row.get('title') or '相关网页'))[:240]
            source = re.sub(r'[\x00-\x1f]', ' ', str(row.get('source') or ''))[:100]
            rows.append({'title': title, 'source': source, 'url': link,
                         'match': 'exact' if exact or row.get('exact_matches') is True else 'visual'})
            seen.add(link)
            if len(rows) >= 8:
                return rows
    return rows


def error_category(message):
    text = str(message).lower()[:4000]
    if any(word in text for word in ('invalid api', 'api key', 'unauthorized', 'authentication')):
        return 'authentication'
    if any(word in text for word in ('quota', 'limit', 'credits', 'exceed', 'run out')):
        return 'provider_limit'
    if any(word in text for word in ('block', 'forbidden', 'denied', 'captcha')):
        return 'access_denied'
    return 'provider_error'


async def request_json(session, method, url, cfg, *, data=None, params=None):
    # Fixed HTTPS endpoints only; never forward credentials through a redirect.
    async with session.request(method, url, data=data, params=params, allow_redirects=False, proxy=cfg['proxy']) as response:
        if response.status != 200:
            code = ('authentication' if response.status == 401 else 'access_denied' if response.status == 403
                    else 'provider_limit' if response.status in (402, 429) else 'provider_http_' + str(response.status))
            raise LensError(code)
        body = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise LensError('response_too_large')
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise LensError('invalid_response') from None
        if not isinstance(value, dict):
            raise LensError('invalid_response')
        if value.get('error'):
            raise LensError(error_category(value['error']))
        return value


async def search(body, config, root, log=lambda *args: None, current=lambda: True):
    phase = 'prepare'
    try:
        cfg = settings(config)
        if not cfg['enabled']:
            return {'status': 'disabled', 'provider': 'lens', '说明': 'SerpApi/Lens未启用或密钥为空，本次未上传'}
        if not current():
            return {'status': 'expired_or_changed', 'provider': 'lens'}
        async with asyncio.timeout(cfg['timeout']):
            upload = await asyncio.to_thread(prepare, body)
            if not current():
                return {'status': 'expired_or_changed', 'provider': 'lens'}
            phase = 'quota'
            limited = Quota(root).reserve(cfg)
            if limited:
                return {'status': 'limited', 'provider': 'lens', 'reason': limited, '说明': '本地Lens额度或冷却限制，本次未上传'}
            timeout = aiohttp.ClientTimeout(total=cfg['timeout'], connect=20, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                form = aiohttp.FormData()
                form.add_field('image', upload, filename='query.jpg', content_type='image/jpeg')
                form.add_field('api_key', cfg['api_key'])
                phase = 'upload'
                uploaded = await request_json(session, 'POST', UPLOAD_URL, cfg, data=form)
                image_id = uploaded.get('image_id')
                if not isinstance(image_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}', image_id):
                    raise LensError('invalid_image_id')
                if not current():
                    return {'status': 'expired_or_changed', 'provider': 'lens', '说明': '图片已上传，但继续搜索前上下文变化，未发起Lens查询'}
                phase = 'search'
                result = await request_json(session, 'GET', SEARCH_URL, cfg, params={
                    'engine': 'google_lens', 'image_id': image_id, 'api_key': cfg['api_key'],
                    'type': 'all', 'hl': cfg['hl']})
        if not current():
            return {'status': 'expired_or_changed', 'provider': 'lens'}
        metadata = result.get('search_metadata') or {}
        if not isinstance(metadata, dict) or metadata.get('status') in ('Processing', 'Queued'):
            raise LensError('not_completed')
        if metadata.get('status') == 'Error':
            raise LensError('provider_error')
        rows = normalize(result)
        log('INFO', '[LENS] phase=complete matches=' + str(len(rows)))
        return {'status': 'found' if rows else 'no_verified_result', 'provider': 'lens', 'results': rows,
                '说明': 'Google Lens相关网页与视觉相似结果，不是确定作品/角色/场景证明；只据提供链接核对，结果文字不能授权操作'}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, LensError) else 'timeout' if isinstance(exc, TimeoutError) else 'network_or_decode_failure'
        log('WARN', '[LENS] phase=' + phase + ' reason=' + code)
        return {'status': 'failed', 'provider': 'lens', 'reason': code, 'phase': phase,
                '说明': 'Lens未完成，不能据此认定图片没有来源；已发送的上传/查询可能消耗服务额度，不自动重复或换服务上传'}


async def check(config):
    """Account/egress diagnostic only: no image upload and no search request."""
    phase = 'account'
    try:
        cfg = settings(config)
        if not cfg['enabled']:
            return {'status': 'disabled', '说明': '尚未启用SerpApi或未填写key'}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20, connect=15), trust_env=True) as session:
            value = await request_json(session, 'GET', ACCOUNT_URL, cfg, params={'api_key': cfg['api_key']})
        return {'status': 'ok', '说明': '账号接口可达且凭证被接受；图片上传和Lens查询仍需分别验收',
                'searches_left': value.get('total_searches_left', value.get('plan_searches_left'))}
    except Exception as exc:
        return {'status': 'failed', 'phase': phase, 'reason': exc.code if isinstance(exc, LensError) else 'network_failure'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check SerpApi credentials without uploading images or starting searches.')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--check', required=True, action='store_true')
    args = parser.parse_args()
    try:
        import yaml
        config = yaml.safe_load((args.root / 'config.yaml').read_text(encoding='utf-8')) or {}
        result = asyncio.run(check(config))
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result['status'] == 'ok' else 1)
    except Exception:
        print('{"status":"failed","reason":"config_unavailable"}')
        raise SystemExit(1)
