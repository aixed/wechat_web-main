"""Shared directory for standard, password-free SQLite databases and metadata."""

from __future__ import annotations

from pathlib import Path
from threading import RLock


BACKEND_DIR = Path(__file__).resolve().parent
DB_DIR = BACKEND_DIR / "db"
LEGACY_DB_DIR = BACKEND_DIR / ".sqlite_cache"
_SIDECARS = ("-wal", "-shm", "-journal")
_lock = RLock()
_initialized = False


def _merge_legacy(source: Path, destination: Path) -> None:
    """Move complete SQLite file groups; retain conflicting originals as backups."""
    for path in (source, destination):
        if not path.resolve().is_relative_to(DB_DIR.resolve()):
            raise ValueError("Database migration paths must stay inside backend/db")
    for entry in sorted(source.iterdir()):
        target = destination / entry.name
        if entry.is_dir():
            if not target.exists():
                entry.rename(target)
            elif target.is_dir():
                _merge_legacy(entry, target)
            continue
        if entry.name.endswith(_SIDECARS):
            continue
        files = [entry]
        if entry.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            files.extend(Path(str(entry) + suffix) for suffix in _SIDECARS)
        files = [path for path in files if path.exists()]
        if any((destination / path.name).exists() for path in files):
            continue
        for path in files:
            path.rename(destination / path.name)
    if not any(source.iterdir()):
        source.rmdir()


def database_directory() -> Path:
    """Prepare backend/db and migrate the old directory on backend startup."""
    global _initialized
    with _lock:
        if _initialized:
            return DB_DIR
        for path in (DB_DIR, LEGACY_DB_DIR):
            if not path.resolve().is_relative_to(BACKEND_DIR.resolve()):
                raise ValueError("Database directories must stay inside the backend directory")
        DB_DIR.mkdir(parents=True, exist_ok=True)
        if LEGACY_DB_DIR.is_dir():
            backup = DB_DIR / "legacy"
            suffix = 1
            while backup.exists():
                backup = DB_DIR / f"legacy-{suffix}"
                suffix += 1
            # A normal backend restart closes SQLite handles before this move.
            # Move the directory intact first so WAL/journal files stay with it.
            LEGACY_DB_DIR.rename(backup)
            _merge_legacy(backup, DB_DIR)
        legacy_mapping = BACKEND_DIR / "rdv_wxid_mapping.json"
        target_mapping = DB_DIR / legacy_mapping.name
        if legacy_mapping.is_file() and not target_mapping.exists():
            legacy_mapping.rename(target_mapping)
        _initialized = True
        return DB_DIR


def database_path(filename: str) -> Path:
    return database_directory() / filename
