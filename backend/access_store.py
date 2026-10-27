"""SQLite identities and account ownership. Access keys are never stored in plain text."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from functools import lru_cache
from contextlib import contextmanager

import config
from database import database_path

DB_PATH = database_path('access_users.sqlite3')
current_user: ContextVar[str] = ContextVar('access_user', default='')
current_account: ContextVar[str] = ContextVar('access_account', default='')


def scope():
    return f'{config.LOGIN_MODE}:{config.HOOK_HOST}:{config.HOOK_PORT}'


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS access_users (
            id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL, last_login_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS user_accounts (
            scope TEXT NOT NULL, account_id TEXT NOT NULL, user_id TEXT NOT NULL,
            created_at REAL NOT NULL, PRIMARY KEY(scope, account_id));
        CREATE TABLE IF NOT EXISTS hook_profiles (
            scope TEXT NOT NULL, user_id TEXT NOT NULL, profile_id TEXT NOT NULL,
            settings TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(scope, user_id, profile_id));
        CREATE TABLE IF NOT EXISTS access_metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    try:
        with db:
            yield db
    finally:
        db.close()


def digest(key):
    return hashlib.sha256(str(key).encode('utf-8')).hexdigest()


def login(key, *, create=False):
    key = str(key or '')
    if not key.strip() or len(key) > 256:
        return None
    with connect() as db:
        if create:
            db.execute('INSERT OR IGNORE INTO access_users VALUES(?,?,?,?)',
                       (uuid.uuid4().hex, digest(key), time.time(), time.time()))
        row = db.execute('SELECT id FROM access_users WHERE key_hash=?', (digest(key),)).fetchone()
        if row and create:
            db.execute('UPDATE access_users SET last_login_at=? WHERE id=?', (time.time(), row['id']))
        return row['id'] if row else None


def legacy_user():
    return _legacy_user(str(DB_PATH), config.WEB_ACCESS_KEY)


@lru_cache(maxsize=32)
def _legacy_user(path, key):
    return login(key, create=True) if key else None


def owner(account_id):
    if not account_id:
        return None
    with connect() as db:
        row = db.execute('SELECT user_id FROM user_accounts WHERE scope=? AND account_id=?',
                         (scope(), str(account_id))).fetchone()
        return row['user_id'] if row else None


def claim(account_id, user_id=None):
    user_id = user_id or current_user.get()
    if not user_id or not account_id:
        return False
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO user_accounts VALUES(?,?,?,?)',
                   (scope(), str(account_id), user_id, time.time()))
        row = db.execute('SELECT user_id FROM user_accounts WHERE scope=? AND account_id=?',
                         (scope(), str(account_id))).fetchone()
        return row['user_id'] == user_id


def owns(account_id, user_id=None):
    user_id = user_id or current_user.get()
    return bool(user_id and owner(account_id) == user_id)


def require_account(account_id):
    # Internal callbacks/background jobs have no user credential; HTTP/WS always set it.
    if current_user.get() and not owns(account_id):
        from fastapi import HTTPException
        raise HTTPException(403, '此微信不属于当前密钥用户')


def account_ids(user_id=None):
    with connect() as db:
        return [row[0] for row in db.execute('SELECT account_id FROM user_accounts WHERE scope=? AND user_id=?',
                (scope(), user_id or current_user.get())).fetchall() if not row[0].startswith(('wxid:', 'rdv:'))]


def migrate_hook(records):
    user_id = legacy_user()
    if not user_id:
        return
    with connect() as db:
        marker = 'hook_migrated:' + scope()
        if db.execute('SELECT 1 FROM access_metadata WHERE name=?', (marker,)).fetchone():
            return
        from agent_ws import agent_manager
        for account_id in ['default', *(r['id'] for r in records), *(a['id'] for a in agent_manager.agents())]:
            db.execute('INSERT OR IGNORE INTO user_accounts VALUES(?,?,?,?)', (scope(), account_id, user_id, time.time()))
        db.execute('INSERT INTO access_metadata VALUES(?,?)', (marker, '1'))
    for record in records:
        if owns(record['id'], user_id):
            save_profile({**record, 'profile_id': record['id']}, user_id)


def profiles(user_id=None):
    with connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            'SELECT settings FROM hook_profiles WHERE scope=? AND user_id=? ORDER BY updated_at DESC',
            (scope(), user_id or current_user.get())).fetchall()]


def profile(profile_id='', user_id=None):
    items = profiles(user_id)
    return next((p for p in items if p['profile_id'] == profile_id or p.get('id') == profile_id), None) if profile_id else next(iter(items), None)


def save_profile(settings, user_id=None):
    user_id = user_id or current_user.get()
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO hook_profiles VALUES(?,?,?,?,?)',
            (scope(), user_id, settings['profile_id'], json.dumps(settings, ensure_ascii=False), time.time()))


def retired(account_id):
    with connect() as db:
        return bool(db.execute('SELECT 1 FROM access_metadata WHERE name=?', ('deleted:' + scope() + ':' + str(account_id),)).fetchone())


def unretire(account_id):
    with connect() as db:
        db.execute('DELETE FROM access_metadata WHERE name=?', ('deleted:' + scope() + ':' + str(account_id),))


def forget_profile(profile, aliases):
    user = current_user.get()
    require_account(profile['id'])
    with connect() as db:
        db.execute('DELETE FROM hook_profiles WHERE scope=? AND user_id=? AND profile_id=?', (scope(), user, profile['profile_id']))
        for account_id in {profile['id'], *aliases, *('wxid:' + wxid for wxid in profile.get('wxids', [])),
                           *(['wxid:' + profile['wxid']] if profile.get('wxid') else [])}:
            db.execute('DELETE FROM user_accounts WHERE scope=? AND user_id=? AND account_id=?', (scope(), user, account_id))
            db.execute('INSERT OR REPLACE INTO access_metadata VALUES(?,?)', ('deleted:' + scope() + ':' + account_id, str(time.time())))


def new_rdv():
    return f'{0x01000000 + secrets.randbelow(0x7fffffff - 0x01000000 + 1):08x}'


def validate_rdv(value):
    import re
    value = str(value or '').strip().lower()
    if not re.fullmatch('[0-9a-f]{8}', value) or not 0x01000000 <= int(value, 16) <= 0x7fffffff:
        raise ValueError('RDV 必须是 01000000—7fffffff 范围内的 8 位小写十六进制值')
    return value
