"""Persistent SQLite cache for chat messages and session previews."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from threading import RLock
from typing import Any


class SqliteMessageCache:
    """Small synchronous SQLite cache keyed by wxid.

    The backend is async, but the cache operations are short local disk writes.
    A single lock keeps SQLite access simple and predictable.
    """

    def __init__(self, db_path: str | None = None) -> None:
        base_dir = os.path.dirname(__file__)
        cache_dir = os.path.join(base_dir, ".sqlite_cache")
        os.makedirs(cache_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(cache_dir, "wechat_cache.sqlite3")
        self._lock = RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    wxid TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    timestamp INTEGER NOT NULL DEFAULT 0,
                    message_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (wxid, msg_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_wxid_ts
                    ON messages (wxid, timestamp);

                CREATE TABLE IF NOT EXISTS history_state (
                    wxid TEXT PRIMARY KEY,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    oldest_ts INTEGER NOT NULL DEFAULT 0,
                    newest_ts INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS last_messages (
                    wxid TEXT PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '',
                    msg_type TEXT NOT NULL DEFAULT '1',
                    is_sender INTEGER NOT NULL DEFAULT 0,
                    time INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_blobs (
                    media_id TEXT PRIMARY KEY,
                    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    filename TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    data BLOB NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )

    @staticmethod
    def _message_id(msg: dict[str, Any]) -> str:
        msg_id = str(msg.get("id", "") or "").strip()
        if msg_id:
            return msg_id
        timestamp = int(msg.get("timestamp") or msg.get("time_unix") or 0)
        msgtype = str(msg.get("msgtype", "") or "")
        content = str(msg.get("msg", "") or "")
        digest = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()
        return f"cache_{timestamp}_{msgtype}_{digest}"

    @staticmethod
    def _timestamp(msg: dict[str, Any]) -> int:
        try:
            return int(msg.get("timestamp") or msg.get("time_unix") or 0)
        except Exception:
            return 0

    @staticmethod
    def _last_from_message(msg: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": str(msg.get("msg", "") or ""),
            "type": str(msg.get("msgtype", "") or "1"),
            "is_sender": 1 if str(msg.get("sendorrecv", "")) == "1" or msg.get("isSender") == 1 else 0,
            "time": SqliteMessageCache._timestamp(msg),
        }

    def upsert_messages(self, wxid: str, msgs: list[dict[str, Any]], *, mark_initialized: bool = False) -> int:
        if not wxid or not msgs:
            if wxid and mark_initialized:
                self.mark_initialized(wxid)
            return 0
        now = int(time.time())
        rows = []
        newest: dict[str, Any] | None = None
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            msg_id = self._message_id(msg)
            ts = self._timestamp(msg)
            rows.append((wxid, msg_id, ts, json.dumps(msg, ensure_ascii=False), now, now))
            if newest is None or ts >= self._timestamp(newest):
                newest = msg
        if not rows:
            return 0

        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO messages (wxid, msg_id, timestamp, message_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wxid, msg_id) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    message_json=excluded.message_json,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            if newest:
                self._upsert_last_message_locked(conn, wxid, self._last_from_message(newest), now)
            self._refresh_state_locked(conn, wxid, mark_initialized=mark_initialized, now=now)
        return len(rows)

    def mark_initialized(self, wxid: str) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            self._refresh_state_locked(conn, wxid, mark_initialized=True, now=now)

    def has_initialized(self, wxid: str) -> bool:
        if not wxid:
            return False
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT initialized FROM history_state WHERE wxid = ?",
                (wxid,),
            ).fetchone()
            return bool(row and int(row["initialized"] or 0) == 1)

    def get_messages(self, wxid: str, limit: int = 50, before: int = 0) -> list[dict[str, Any]]:
        if not wxid:
            return []
        params: list[Any] = [wxid]
        where = "wxid = ?"
        if before > 0:
            where += " AND timestamp < ?"
            params.append(before)
        sql = (
            f"SELECT message_json FROM messages WHERE {where} "
            "ORDER BY timestamp DESC, msg_id DESC"
        )
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        messages = [json.loads(row["message_json"]) for row in rows]
        messages.reverse()
        return messages

    def update_image_path_by_msg_id(self, msg_id: str, img_path: str) -> int:
        if not msg_id or not img_path:
            return 0
        now = int(time.time())
        updated = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT wxid, msg_id, message_json FROM messages WHERE msg_id = ?",
                (str(msg_id),),
            ).fetchall()
            for row in rows:
                try:
                    msg = json.loads(row["message_json"])
                except Exception:
                    continue
                msg["img_path"] = img_path
                conn.execute(
                    """
                    UPDATE messages
                    SET message_json = ?, updated_at = ?
                    WHERE wxid = ? AND msg_id = ?
                    """,
                    (json.dumps(msg, ensure_ascii=False), now, row["wxid"], row["msg_id"]),
                )
                updated += 1
        return updated

    def put_media_blob(self, data: bytes, mime_type: str = "", filename: str = "") -> str:
        if not data:
            return ""
        digest = hashlib.sha256(data).hexdigest()
        media_id = f"blob_{digest}"
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_blobs (media_id, mime_type, filename, size, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    mime_type=excluded.mime_type,
                    filename=excluded.filename,
                    size=excluded.size
                """,
                (
                    media_id,
                    mime_type or "application/octet-stream",
                    filename or "",
                    len(data),
                    sqlite3.Binary(data),
                    now,
                ),
            )
        return media_id

    def get_media_blob(self, media_id: str) -> dict[str, Any] | None:
        if not media_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT media_id, mime_type, filename, size, data FROM media_blobs WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "media_id": row["media_id"],
            "mime_type": row["mime_type"] or "application/octet-stream",
            "filename": row["filename"] or "",
            "size": int(row["size"] or 0),
            "data": bytes(row["data"]),
        }

    def get_last_messages(self, wxids: list[str]) -> dict[str, dict[str, Any]]:
        if not wxids:
            return {}
        placeholders = ",".join("?" for _ in wxids)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT wxid, content, msg_type, is_sender, time FROM last_messages "
                f"WHERE wxid IN ({placeholders})",
                wxids,
            ).fetchall()
        return {
            row["wxid"]: {
                "content": row["content"],
                "type": row["msg_type"],
                "is_sender": int(row["is_sender"] or 0),
                "time": int(row["time"] or 0),
            }
            for row in rows
        }

    def upsert_last_messages(self, messages: dict[str, dict[str, Any]]) -> None:
        if not messages:
            return
        now = int(time.time())
        with self._lock, self._connect() as conn:
            for wxid, msg in messages.items():
                self._upsert_last_message_locked(conn, wxid, msg, now)

    def _upsert_last_message_locked(
        self,
        conn: sqlite3.Connection,
        wxid: str,
        msg: dict[str, Any],
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO last_messages (wxid, content, msg_type, is_sender, time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(wxid) DO UPDATE SET
                content=excluded.content,
                msg_type=excluded.msg_type,
                is_sender=excluded.is_sender,
                time=excluded.time,
                updated_at=excluded.updated_at
            """,
            (
                wxid,
                str(msg.get("content", "") or ""),
                str(msg.get("type", "1") or "1"),
                int(msg.get("is_sender", 0) or 0),
                int(msg.get("time", 0) or 0),
                now,
            ),
        )

    def _refresh_state_locked(
        self,
        conn: sqlite3.Connection,
        wxid: str,
        *,
        mark_initialized: bool,
        now: int,
    ) -> None:
        row = conn.execute(
            "SELECT COUNT(*) AS c, MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts "
            "FROM messages WHERE wxid = ?",
            (wxid,),
        ).fetchone()
        count = int(row["c"] or 0) if row else 0
        oldest_ts = int(row["min_ts"] or 0) if row else 0
        newest_ts = int(row["max_ts"] or 0) if row else 0
        conn.execute(
            """
            INSERT INTO history_state (wxid, initialized, message_count, oldest_ts, newest_ts, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(wxid) DO UPDATE SET
                initialized=MAX(history_state.initialized, excluded.initialized),
                message_count=excluded.message_count,
                oldest_ts=excluded.oldest_ts,
                newest_ts=excluded.newest_ts,
                updated_at=excluded.updated_at
            """,
            (wxid, 1 if mark_initialized else 0, count, oldest_ts, newest_ts, now),
        )
