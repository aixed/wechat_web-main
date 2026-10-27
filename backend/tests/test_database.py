from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import database
from sqlite_cache import SqliteMessageCache


class DatabaseDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_dir = self.root / "db"
        self.legacy_dir = self.root / ".sqlite_cache"
        for name, value in (
            ("BACKEND_DIR", self.root), ("DB_DIR", self.db_dir),
            ("LEGACY_DB_DIR", self.legacy_dir), ("_initialized", False),
        ):
            patcher = patch.object(database, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_database(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE sample (value TEXT)")
            connection.execute("INSERT INTO sample VALUES (?)", (value,))

    def read_database(self, path):
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            return connection.execute("SELECT value FROM sample").fetchone()[0]

    def test_new_install_uses_sqlite_without_password_and_creates_parent(self):
        cache = SqliteMessageCache()
        self.assertEqual(self.db_dir / "wechat_cache.sqlite3", Path(cache.db_path))
        with closing(sqlite3.connect(cache.db_path)) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        nested = SqliteMessageCache(str(self.db_dir / "users" / "user.sqlite3"))
        self.assertTrue(Path(nested.db_path).is_file())
        self.assertFalse(self.legacy_dir.exists())

    def test_migrates_databases_users_and_related_metadata_without_losing_data(self):
        self.create_database(self.legacy_dir / "wechat_cache.sqlite3", "chat history")
        self.create_database(self.legacy_dir / "users" / "user.sqlite3", "private user")
        (self.legacy_dir / "hook_instances.json").write_text("[]", encoding="utf-8")
        (self.root / "rdv_wxid_mapping.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.db_dir, database.database_directory())
        self.assertEqual("chat history", self.read_database(self.db_dir / "wechat_cache.sqlite3"))
        self.assertEqual("private user", self.read_database(self.db_dir / "users" / "user.sqlite3"))
        self.assertEqual("[]", (self.db_dir / "hook_instances.json").read_text(encoding="utf-8"))
        self.assertTrue((self.db_dir / "rdv_wxid_mapping.json").is_file())
        self.assertFalse(self.legacy_dir.exists())
        database.database_directory()
        self.assertEqual("chat history", self.read_database(self.db_dir / "wechat_cache.sqlite3"))

    def test_existing_database_is_never_overwritten_and_keeps_old_wal_together(self):
        self.create_database(self.legacy_dir / "wechat_cache.sqlite3", "old data")
        old_data = (self.legacy_dir / "wechat_cache.sqlite3").read_bytes()
        self.create_database(self.db_dir / "wechat_cache.sqlite3", "current data")
        old_wal = self.legacy_dir / "wechat_cache.sqlite3-wal"
        old_wal.write_bytes(b"legacy sidecar")
        database.database_directory()
        self.assertEqual("current data", self.read_database(self.db_dir / "wechat_cache.sqlite3"))
        self.assertEqual(old_data, (self.db_dir / "legacy" / "wechat_cache.sqlite3").read_bytes())
        self.assertTrue((self.db_dir / "legacy" / old_wal.name).exists())
        self.assertFalse((self.db_dir / old_wal.name).exists())

    def test_migration_moves_wal_sidecars_with_the_database(self):
        self.legacy_dir.mkdir()
        (self.legacy_dir / "cache.sqlite3").write_bytes(b"database")
        for suffix in ("-wal", "-shm", "-journal"):
            (self.legacy_dir / ("cache.sqlite3" + suffix)).write_bytes(suffix.encode())
        database.database_directory()
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.assertTrue((self.db_dir / ("cache.sqlite3" + suffix)).is_file())
        self.assertFalse(self.legacy_dir.exists())


if __name__ == "__main__":
    unittest.main()
