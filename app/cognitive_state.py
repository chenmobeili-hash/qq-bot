"""Scoped context summaries and a single UTC event ledger. No network at import."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time

FILE = 'cognitive_state.sqlite3'
MAX_EVENTS = 12000


def valid_scope(key):
    return isinstance(key, str) and bool(re.fullmatch(r'(?:group_\d+(?:_\d+)?|private_\d+)', key))


def room(key):
    if not valid_scope(key):
        raise ValueError('Invalid scope')
    return '_'.join(key.split('_')[:2]) if key.startswith('group_') else key


def clean(text, limit=1200):
    # Signed URLs, CQ payloads, and raw/base64 media are never durable context.
    text = re.sub(r'\[CQ:[^\]]*\]', '[附件或提醒]', str(text))
    text = re.sub(r'https?://\S+|(?:data:|base64://)\S+', '[链接或媒体已省略]', text)
    text = re.sub(r'(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_-]?key|token|password|密码|密钥)\s*[:=：]\s*\S+)', '[凭证已省略]', text)
    return text[:limit]


def digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, root, clock=time.time):
        self.path, self.clock = Path(root) / FILE, clock

    @contextmanager
    def connect(self):
        if self.path.is_symlink():
            raise ValueError('State cannot be a symlink')
        db = sqlite3.connect(self.path, timeout=2)
        os.chmod(self.path, 0o600)
        db.row_factory = sqlite3.Row
        db.executescript('''
          PRAGMA journal_mode=DELETE;
          PRAGMA secure_delete=ON;
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL, at REAL NOT NULL,
            received REAL NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
            text TEXT NOT NULL, expires REAL NOT NULL, dedup TEXT UNIQUE);
          CREATE INDEX IF NOT EXISTS event_scope ON events(scope,at,id);
          CREATE TABLE IF NOT EXISTS summaries (
            scope TEXT PRIMARY KEY, text TEXT NOT NULL, covered TEXT NOT NULL,
            updated REAL NOT NULL, method TEXT NOT NULL);
        ''')
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self, db):
        now = self.clock()
        db.execute("UPDATE events SET text='' WHERE expires<=? AND text!=''", (now,))
        db.execute('DELETE FROM events WHERE received<?', (now - 30 * 86400,))
        db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)', (MAX_EVENTS,))
        db.execute('DELETE FROM summaries WHERE updated<?', (now - 180 * 86400,))

    def event(self, scope, kind, text='', status='received', at=None, dedup=None, expires=None):
        if not valid_scope(scope):
            return
        now = self.clock()
        # Untrusted bridge timestamps cannot move an event arbitrarily into the future/past.
        try:
            stamp = float(at) if at is not None else now
        except (TypeError, ValueError):
            stamp = now
        if not now - 30 * 86400 <= stamp <= now + 60:
            stamp = now
        with self.connect() as db:
            self.prune(db)
            expiry = min(now + 5 * 3600, float(expires)) if expires is not None else now + 5 * 3600
            db.execute('INSERT OR IGNORE INTO events(scope,at,received,kind,status,text,expires,dedup) VALUES(?,?,?,?,?,?,?,?)',
                       (room(scope), stamp, now, str(kind)[:40], str(status)[:20], clean(text), expiry, dedup))
            self.prune(db)

    def recent(self, scope, limit=12):
        if not valid_scope(scope):
            return []
        with self.connect() as db:
            self.prune(db)
            rows = db.execute('SELECT id,at,kind,status,text FROM events WHERE scope=? ORDER BY at DESC,id DESC LIMIT ?',
                              (room(scope), min(30, max(1, limit)))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def summary(self, scope):
        if not valid_scope(scope) or scope.startswith('group_') and len(scope.split('_')) != 3:
            return None
        with self.connect() as db:
            self.prune(db)
            row = db.execute('SELECT * FROM summaries WHERE scope=?', (scope,)).fetchone()
        return dict(row) if row else None

    def save_summary(self, scope, text, covered, method='model'):
        if not valid_scope(scope) or len(text) > 2600 or not text.strip():
            raise ValueError('Invalid summary')
        with self.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0]
            if count >= 256 and not db.execute('SELECT 1 FROM summaries WHERE scope=?', (scope,)).fetchone():
                raise ValueError('Summary scope capacity reached')
            db.execute('INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?)',
                       (scope, clean(text, 2600), covered, self.clock(), method))
