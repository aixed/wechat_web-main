from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import access_store
import config


class ProfileAccessKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'access.sqlite3'
        for patcher in (
            patch.object(access_store, 'DB_PATH', self.path),
            patch.object(access_store, '_known_access_keys', {}),
            patch.object(config, 'WEB_ACCESS_KEY', 'legacy-test-key'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, user_id, account='hook_30002', **extra):
        settings = {'id': account, 'profile_id': account, 'port': 30002,
                    'rdv': '01000001', 'wxid': 'wxid_test', **extra}
        access_store.save_profile(settings, user_id)
        return settings

    def test_old_schema_migrates_and_backfills_known_legacy_key(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE access_users (id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL, last_login_at REAL NOT NULL);
                CREATE TABLE hook_profiles (scope TEXT NOT NULL, user_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL, settings TEXT NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(scope,user_id,profile_id));
            ''')
            db.execute('INSERT INTO access_users VALUES(?,?,?,?)',
                       ('original-user', access_store.digest('legacy-test-key'), 1, 2))
            db.execute('INSERT INTO hook_profiles VALUES(?,?,?,?,?)',
                       (access_store.scope(), 'original-user', 'hook_30002', '{"id":"hook_30002"}', 3))
        with access_store.connect() as db:
            row = db.execute('SELECT * FROM hook_profiles').fetchone()
            self.assertEqual('legacy-test-key', row['access_key'])
            self.assertEqual('original-user', row['user_id'])
            self.assertEqual(3, row['updated_at'])
        with access_store.connect() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM hook_profiles').fetchone()[0])

    def test_login_backfills_previously_unknown_key_without_creating_another_user(self):
        user_id = access_store.login('alice-test-key', create=True)
        self.save(user_id)
        with access_store.connect() as db:
            db.execute("UPDATE hook_profiles SET access_key=''")
        access_store._known_access_keys.clear()
        self.assertEqual(user_id, access_store.login('alice-test-key', create=True))
        with access_store.connect() as db:
            self.assertEqual('alice-test-key', db.execute('SELECT access_key FROM hook_profiles').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM access_users').fetchone()[0])

    def test_saves_correct_owner_key_and_keeps_it_after_restart_and_update(self):
        alice = access_store.login('alice-test-key', create=True)
        bob = access_store.login('bob-test-key', create=True)
        self.save(alice, 'hook_30002')
        self.save(bob, 'hook_30003', port=30003, rdv='01000002')
        access_store._known_access_keys.clear()
        self.save(alice, 'hook_30002', nickname='updated')
        with access_store.connect() as db:
            keys = {row['user_id']: row['access_key'] for row in db.execute('SELECT user_id,access_key FROM hook_profiles')}
            self.assertEqual({alice: 'alice-test-key', bob: 'bob-test-key'}, keys)
            self.assertNotIn('alice-test-key', db.execute('SELECT settings FROM hook_profiles WHERE user_id=?', (alice,)).fetchone()[0])
        self.assertNotIn('access_key', access_store.profile('hook_30002', alice))

    def test_login_restores_missing_account_bindings_from_saved_profile(self):
        user_id = access_store.login('alice-test-key', create=True)
        self.save(user_id)
        with access_store.connect() as db:
            db.execute('DELETE FROM user_accounts')
        self.assertEqual(user_id, access_store.login('alice-test-key', create=True))
        for account in ('hook_30002', 'wxid:wxid_test', 'rdv:01000001'):
            self.assertEqual(user_id, access_store.owner(account))

    def test_saved_key_restores_original_identity_after_identity_row_is_lost(self):
        user_id = access_store.login('alice-test-key', create=True)
        self.save(user_id)
        with access_store.connect() as db:
            db.execute('DELETE FROM access_users')
            db.execute('DELETE FROM user_accounts')
        access_store._known_access_keys.clear()
        self.assertEqual(user_id, access_store.login('alice-test-key'))
        self.assertEqual(user_id, access_store.owner('hook_30002'))
        self.assertIsNotNone(access_store.profile('hook_30002', user_id))

    def test_recovery_preserves_other_owner_and_does_not_restore_deleted_account(self):
        alice = access_store.login('alice-test-key', create=True)
        bob = access_store.login('bob-test-key', create=True)
        self.save(alice, 'shared-account')
        access_store.claim('shared-account', bob)
        self.save(alice, 'deleted-account', rdv='01000002')
        with access_store.connect() as db:
            db.execute('INSERT INTO access_metadata VALUES(?,?)',
                       ('deleted:' + access_store.scope() + ':deleted-account', '1'))
        access_store.login('alice-test-key')
        self.assertEqual(bob, access_store.owner('shared-account'))
        self.assertIsNone(access_store.owner('deleted-account'))

    def test_mismatched_plaintext_label_cannot_override_another_users_identity(self):
        alice = access_store.login('alice-test-key', create=True)
        self.save(alice)
        with access_store.connect() as db:
            db.execute('UPDATE hook_profiles SET access_key=?', ('unregistered-test-key',))
        self.assertIsNone(access_store.login('unregistered-test-key', create=True))
        self.assertEqual(alice, access_store.login('alice-test-key'))

    def test_ambiguous_saved_key_is_not_assigned_to_an_arbitrary_user(self):
        alice = access_store.login('alice-test-key', create=True)
        bob = access_store.login('bob-test-key', create=True)
        self.save(alice, 'hook_30002')
        self.save(bob, 'hook_30003', port=30003)
        with access_store.connect() as db:
            db.execute('UPDATE hook_profiles SET access_key=?', ('lost-test-key',))
        self.assertIsNone(access_store.login('lost-test-key', create=True))


if __name__ == '__main__':
    unittest.main()
