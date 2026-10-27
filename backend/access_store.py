"""SQLite identities and ownership, with readable access keys on Hook profiles."""
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
_known_access_keys: dict[tuple[str, str], str] = {}


def scope():
    return f'{config.LOGIN_MODE}:{config.HOOK_HOST}:{config.HOOK_PORT}'


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    try:
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
            access_key TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(scope, user_id, profile_id));
        CREATE TABLE IF NOT EXISTS access_metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(hook_profiles)')}
        if 'access_key' not in columns:
            try:
                db.execute("ALTER TABLE hook_profiles ADD COLUMN access_key TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                if 'access_key' not in {row['name'] for row in db.execute('PRAGMA table_info(hook_profiles)')}:
                    raise
        db.execute('CREATE INDEX IF NOT EXISTS idx_hook_profiles_access_key ON hook_profiles(access_key, user_id)')
        with db:
            key = str(config.WEB_ACCESS_KEY or '')
            if key and db.execute('''SELECT 1 FROM hook_profiles WHERE access_key=''
                    AND user_id IN (SELECT id FROM access_users WHERE key_hash=?) LIMIT 1''', (digest(key),)).fetchone():
                db.execute('''UPDATE hook_profiles SET access_key=? WHERE access_key=''
                    AND user_id IN (SELECT id FROM access_users WHERE key_hash=?)''', (key, digest(key)))
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
        key_hash = digest(key)
        row = db.execute('SELECT id FROM access_users WHERE key_hash=?', (key_hash,)).fetchone()
        if row is None:
            # Keep the original user ID/cache directory if its identity row was lost.
            owners = db.execute('SELECT DISTINCT user_id FROM hook_profiles WHERE access_key=?', (key,)).fetchall()
            if len(owners) > 1:
                return None
            if owners:
                user_id = owners[0]['user_id']
                previous = db.execute('SELECT key_hash FROM access_users WHERE id=?', (user_id,)).fetchone()
                if previous and previous['key_hash'] != key_hash:
                    return None
                db.execute('INSERT OR IGNORE INTO access_users (id,key_hash,created_at,last_login_at) VALUES(?,?,?,?)',
                           (user_id, key_hash, time.time(), time.time()))
        if create:
            db.execute('INSERT OR IGNORE INTO access_users (id,key_hash,created_at,last_login_at) VALUES(?,?,?,?)',
                       (uuid.uuid4().hex, key_hash, time.time(), time.time()))
        row = db.execute('SELECT id FROM access_users WHERE key_hash=?', (key_hash,)).fetchone()
        if not row:
            return None
        user_id = row['id']
        _known_access_keys[(str(DB_PATH), user_id)] = key
        if db.execute('SELECT 1 FROM hook_profiles WHERE user_id=? AND access_key<>? LIMIT 1', (user_id, key)).fetchone():
            db.execute('UPDATE hook_profiles SET access_key=? WHERE user_id=? AND access_key<>?', (key, user_id, key))
        _restore_profile_accounts(db, user_id, key)
        if row and create:
            db.execute('UPDATE access_users SET last_login_at=? WHERE id=?', (time.time(), row['id']))
        return user_id


def _restore_profile_accounts(db, user_id, key):
    for row in db.execute('SELECT scope,profile_id,settings FROM hook_profiles WHERE user_id=? AND access_key=?',
                          (user_id, key)).fetchall():
        settings = json.loads(row['settings'])
        account_ids = {str(settings.get('id') or row['profile_id'])}
        wxids = [settings.get('wxid'), *(settings.get('wxids') or [])]
        account_ids.update('wxid:' + str(wxid) for wxid in wxids if wxid)
        if settings.get('rdv'):
            account_ids.add('rdv:' + str(settings['rdv']).lower())
        for account_id in account_ids:
            marker = 'deleted:' + row['scope'] + ':' + account_id
            bound = db.execute('SELECT 1 FROM user_accounts WHERE scope=? AND account_id=?',
                               (row['scope'], account_id)).fetchone()
            if not bound and not db.execute('SELECT 1 FROM access_metadata WHERE name=?', (marker,)).fetchone():
                db.execute('INSERT OR IGNORE INTO user_accounts (scope,account_id,user_id,created_at) VALUES(?,?,?,?)',
                           (row['scope'], account_id, user_id, time.time()))


def _profile_access_key(db, user_id):
    identity = db.execute('SELECT key_hash FROM access_users WHERE id=?', (user_id,)).fetchone()
    if not identity:
        return ''
    candidates = [_known_access_keys.get((str(DB_PATH), user_id), ''), config.WEB_ACCESS_KEY]
    candidates.extend(row['access_key'] for row in db.execute(
        "SELECT DISTINCT access_key FROM hook_profiles WHERE user_id=? AND access_key<>''", (user_id,)))
    return next((str(key) for key in candidates if key and digest(key) == identity['key_hash']), '')


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
        db.execute('''INSERT OR REPLACE INTO hook_profiles
            (scope,user_id,profile_id,settings,updated_at,access_key) VALUES(?,?,?,?,?,?)''',
            (scope(), user_id, settings['profile_id'], json.dumps(settings, ensure_ascii=False), time.time(),
             _profile_access_key(db, user_id)))


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
