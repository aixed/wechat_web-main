"""In-memory message/contact/session store for one backend runtime."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass
class SessionSnapshot:
    wxid: str
    last_msg: str = ""
    last_time: str = ""
    last_timestamp: int = 0   # Unix epoch seconds
    unread: int = 0


class MessageStore:
    """Single runtime source of truth for messages and lightweight chat metadata."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self._message_ids: dict[str, set[str]] = {}
        self.db_loaded: set[str] = set()
        self.contact_info: dict[str, dict[str, str]] = {}
        self.sessions: dict[str, SessionSnapshot] = {}

    def set_contact(self, wxid: str, name: str = "", avatar: str = "") -> None:
        if not wxid:
            return
        with self._lock:
            current = self.contact_info.get(wxid, {"name": "", "avatar": ""})
            if name:
                current["name"] = name
            if avatar:
                current["avatar"] = avatar
            self.contact_info[wxid] = current

    def get_contact(self, wxid: str) -> dict[str, str]:
        with self._lock:
            return dict(self.contact_info.get(wxid, {"name": "", "avatar": ""}))

    def set_db_loaded(self, wxid: str) -> None:
        if not wxid:
            return
        with self._lock:
            self.db_loaded.add(wxid)

    def is_db_loaded(self, wxid: str) -> bool:
        with self._lock:
            return wxid in self.db_loaded

    def add_history(self, wxid: str, msgs: list[dict[str, Any]]) -> int:
        """Insert history messages for a chat and mark DB loaded.
        Uses replace=True so DB data (authoritative for IsSender/CreateTime)
        overwrites any earlier callback versions of the same message."""
        added = 0
        for msg in msgs:
            if self.add_message(wxid, msg, replace=True):
                added += 1
        self.set_db_loaded(wxid)
        return added

    def add_history_no_flag(self, wxid: str, msgs: list[dict[str, Any]]) -> int:
        """Insert older history messages without changing the db_loaded flag."""
        added = 0
        for msg in msgs:
            if self.add_message(wxid, msg):
                added += 1
        return added

    def add_message(self, wxid: str, msg: dict[str, Any], replace: bool = False) -> bool:
        """Append message to a chat if it is not a duplicate.
        If replace=True and a message with the same ID exists, replace it
        (used when DB history is more authoritative than callback data).
        Also replaces synthetic ``send_...`` placeholders when the real
        callback / DB message with a proper ``msgsvrid`` arrives."""
        if not wxid or not isinstance(msg, dict):
            return False
        msg_id = str(msg.get("id", "")).strip()
        with self._lock:
            if wxid not in self.messages:
                self.messages[wxid] = []
                self._message_ids[wxid] = set()

            # When a real (non-synthetic) self-sent message arrives, remove the
            # matching synthetic "send_..." placeholder that was created for
            # instant local display in _broadcast_local_sent_message().
            if msg_id and not msg_id.startswith("send_"):
                self._remove_synthetic_match(wxid, msg)

            if msg_id and msg_id in self._message_ids[wxid]:
                if replace:
                    # Replace the existing message with the new (DB) version
                    for i, existing in enumerate(self.messages[wxid]):
                        if str(existing.get("id", "")).strip() == msg_id:
                            self.messages[wxid][i] = msg
                            break
                    self._sort_messages_in_place(wxid)
                    return True
                return False
            if msg_id:
                self._message_ids[wxid].add(msg_id)
            self.messages[wxid].append(msg)
            self._sort_messages_in_place(wxid)
            return True

    def _remove_synthetic_match(self, wxid: str, real_msg: dict[str, Any]) -> None:
        """Remove a synthetic ``send_...`` placeholder that matches *real_msg*.

        Match criteria:
        - The existing message has an id starting with ``send_``
        - Same ``msgtype``
        - Timestamps within 120 seconds of each other
        - For text messages (type 1), the content must also match
        """
        is_self = (
            str(real_msg.get("sendorrecv", "")) == "1"
            or real_msg.get("isSender") == 1
            or real_msg.get("isSender") == "1"
            or real_msg.get("IsSender") == 1
        )
        if not is_self:
            return

        msg_type = str(real_msg.get("msgtype", "") or "")
        msg_content = str(real_msg.get("msg", "") or "")
        msg_time = 0
        try:
            msg_time = int(real_msg.get("timestamp") or real_msg.get("time_unix") or 0)
        except (ValueError, TypeError):
            pass

        for i, existing in enumerate(self.messages[wxid]):
            eid = str(existing.get("id", ""))
            if not eid.startswith("send_"):
                continue
            e_type = str(existing.get("msgtype", "") or "")
            if e_type != msg_type:
                continue
            e_time = 0
            try:
                e_time = int(existing.get("timestamp") or existing.get("time_unix") or 0)
            except (ValueError, TypeError):
                pass
            if msg_time and e_time and abs(e_time - msg_time) > 120:
                continue
            # For text messages require content match to avoid false positives
            if msg_type == "1":
                e_content = str(existing.get("msg", "") or "")
                if e_content != msg_content:
                    continue
            # Found a match — remove the synthetic placeholder
            self._message_ids[wxid].discard(eid)
            self.messages[wxid].pop(i)
            return

    def get_messages(self, wxid: str, limit: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.messages.get(wxid, []))
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def get_all_messages(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {wxid: list(rows) for wxid, rows in self.messages.items()}

    def update_session(self, wxid: str, last_msg: str, last_time: str,
                       unread_delta: int = 0, last_timestamp: int = 0) -> SessionSnapshot:
        with self._lock:
            snapshot = self.sessions.get(wxid)
            if snapshot is None:
                snapshot = SessionSnapshot(wxid=wxid)
            if last_msg:
                snapshot.last_msg = last_msg
            if last_time:
                snapshot.last_time = last_time
            if last_timestamp:
                snapshot.last_timestamp = last_timestamp
            if unread_delta > 0:
                snapshot.unread += unread_delta
            self.sessions[wxid] = snapshot
            return snapshot

    def mark_read(self, wxid: str) -> None:
        """Clear unread count for a chat."""
        with self._lock:
            snapshot = self.sessions.get(wxid)
            if snapshot:
                snapshot.unread = 0

    def get_sessions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                wxid: {
                    "wxid": snap.wxid,
                    "lastMsg": snap.last_msg,
                    "lastTime": snap.last_time,
                    "lastTimestamp": snap.last_timestamp,
                    "unread": snap.unread,
                }
                for wxid, snap in self.sessions.items()
            }

    def _sort_messages_in_place(self, wxid: str) -> None:
        def _msg_sort_key(row: dict[str, Any]) -> tuple[int, str]:
            time_val = row.get("timestamp") or row.get("time_unix") or 0
            try:
                time_num = int(time_val)
            except Exception:
                time_num = 0
            return (time_num, str(row.get("id", "")))

        self.messages[wxid].sort(key=_msg_sort_key)

