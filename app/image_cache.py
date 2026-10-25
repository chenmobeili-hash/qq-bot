"""Private one-hour image cache; never reads or edits conversation memory."""
import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import re
import time
import threading
import uuid

TTL_SECONDS = 3600
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_FILES = 64


def synchronized(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        with self.lock:
            return function(self, *args, **kwargs)
    return call


class ImageCache:
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.lock = threading.RLock()

    def _prepare(self):
        if self.root.is_symlink():
            raise OSError('Image cache directory must not be a symlink')
        self.root.mkdir(exist_ok=True)
        os.chmod(self.root, 0o700)

    def _paths(self, key):
        if not re.fullmatch('[a-f0-9]{64}', key):
            raise ValueError('Invalid image cache key')
        return self.root / (key + '.image'), self.root / (key + '.json')

    def _remove(self, key):
        for path in self._paths(key):
            path.unlink(missing_ok=True)

    @synchronized
    def cleanup(self):
        self._prepare()
        now, entries = self.clock(), []
        for meta in self.root.glob('*.json'):
            if not re.fullmatch('[a-f0-9]{64}', meta.stem):
                continue
            body, _ = self._paths(meta.stem)
            try:
                if meta.is_symlink() or body.is_symlink() or meta.stat().st_size > 2048:
                    raise ValueError()
                info = json.loads(meta.read_text(encoding='utf-8'))
                created = info['created']
                if not isinstance(created, (int, float)) or not now - TTL_SECONDS < created <= now + 60:
                    raise ValueError()
                if not body.is_file():
                    raise ValueError()
                entries.append((created, meta.stem, body.stat().st_size))
            except (OSError, ValueError, TypeError, KeyError):
                self._remove(meta.stem)
        # Expired partial writes/orphans; only files with our exact generated names.
        for path in self.root.iterdir():
            if (re.fullmatch(r'[a-f0-9]{64}\.image', path.name) and not path.with_suffix('.json').exists()
                    or re.fullmatch(r'[a-f0-9]{64}\.[a-f0-9]{32}\.tmp', path.name)):
                if path.is_symlink() or now - path.stat().st_mtime >= TTL_SECONDS:
                    path.unlink(missing_ok=True)
        entries.sort()
        size = sum(row[2] for row in entries)
        while len(entries) > MAX_FILES or size > MAX_CACHE_BYTES:
            _, key, amount = entries.pop(0)
            self._remove(key)
            size -= amount

    @synchronized
    def get(self, url):
        self.cleanup()
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        body, meta = self._paths(key)
        if not meta.exists() or not body.exists():
            return None
        info = json.loads(meta.read_text(encoding='utf-8'))
        content_type = info.get('content_type', 'image/jpeg')
        if not isinstance(content_type, str) or len(content_type) > 120:
            self._remove(key)
            return None
        if self.clock() - info['created'] >= TTL_SECONDS:
            self._remove(key)
            return None
        return body.read_bytes(), content_type

    @synchronized
    def put(self, url, body, content_type):
        self.cleanup()
        if len(body) > MAX_CACHE_BYTES:
            return  # Recognition still proceeds; caching is optional.
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        data_path, meta_path = self._paths(key)
        if data_path.is_symlink() or meta_path.is_symlink():
            raise OSError('Image cache files must not be symlinks')
        info = json.dumps({'created': self.clock(), 'content_type': content_type}, ensure_ascii=False).encode('utf-8')
        for destination, value in ((data_path, body), (meta_path, info)):
            temporary = self.root / (key + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('xb') as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(value)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        self.cleanup()
