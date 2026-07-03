"""
WeChat Web Client - Backend Server
FastAPI server that bridges the WeChat Hook API with the frontend.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from datetime import datetime

import json
import os
import sys
import asyncio
import time
import httpx
import base64
import io

import config
import wechat_api
from agent_ws import agent_manager
from ws_manager import manager
from message_store import MessageStore
from sqlite_cache import SqliteMessageCache
from pb_parser import parse_raw_pb


def _log(msg: str):
    """Flush-safe print."""
    print(msg, flush=True)


def _detect_callback_image_ext(data: bytes) -> str:
    """Detect common image formats from magic bytes. Defaults to jpg."""
    if not data:
        return "jpg"
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _save_img_base64_to_cache(img_b64: str, msg_id: str) -> tuple[str | None, int]:
    """Decode img_base64 and save to backend cache. Returns (filepath_or_None, byte_len)."""
    if not img_b64:
        return None, 0
    s = str(img_b64).strip()
    # Strip possible data URI prefix: data:image/png;base64,...
    if "," in s[:100] and "base64" in s[:100].lower():
        s = s.split(",", 1)[1].strip()
    # Remove whitespace/newlines
    s = "".join(s.split())
    try:
        blob = base64.b64decode(s, validate=False)
    except Exception:
        return None, 0
    if not blob or len(blob) < 16:
        return None, 0
    # Guardrail: avoid accidental huge payloads
    if len(blob) > 30 * 1024 * 1024:
        _log(f"[IMG_BASE64] Skip too-large payload: {len(blob)} bytes")
        return None, len(blob)

    cb_dir = os.path.join(_IMG_CACHE_DIR, "callback")
    os.makedirs(cb_dir, exist_ok=True)
    safe_id = "".join(c for c in (msg_id or "") if c.isalnum() or c in ("_", "-", "."))[:80] or f"cb_{int(time.time())}"
    ext = _detect_callback_image_ext(blob)
    filepath = os.path.join(cb_dir, f"{safe_id}.{ext}")
    try:
        with open(filepath, "wb") as f:
            f.write(blob)
        return filepath, len(blob)
    except Exception as e:
        _log(f"[IMG_BASE64] Save failed: {type(e).__name__}: {e}")
        return None, len(blob)


# ─── Pending CDN image downloads (bridge callback → download endpoint) ──

# When we call CDN_Download_Pic, we register a pending entry.
# The callback handler checks for img_base64 and fulfills the Future.
_pending_cdn_images: dict[str, asyncio.Future] = {}   # key → Future[str]  (str = cached file path)
_pending_cdn_lock = asyncio.Lock()

# De-dup concurrent CDN downloads for the same image (keyed by xml_hash).
# Multiple frontend requests for the same image share a single CDN call + Future.
_inflight_cdn_downloads: dict[str, asyncio.Future] = {}  # xml_hash → Future[str]
_inflight_cdn_lock = asyncio.Lock()

# Semaphore to serialize CDN downloads — only 1 at a time to avoid
# overloading the Hook server (which caused ReadTimeout + circuit breaker)
_cdn_download_sem = asyncio.Semaphore(1)


async def _register_cdn_pending(key: str, existing_fut: asyncio.Future = None) -> asyncio.Future:
    """Register a pending CDN download and return a Future to await.
    Pass existing_fut to share the same future across multiple keys."""
    if existing_fut is None:
        loop = asyncio.get_event_loop()
        existing_fut = loop.create_future()
    async with _pending_cdn_lock:
        _pending_cdn_images[key] = existing_fut
    return existing_fut


async def _fulfill_cdn_pending(key: str, file_path: str) -> bool:
    """Try to fulfill a pending CDN download. Returns True if matched."""
    async with _pending_cdn_lock:
        fut = _pending_cdn_images.pop(key, None)
    if fut and not fut.done():
        fut.set_result(file_path)
        return True
    return False


async def _fulfill_all_cdn_pending(file_path: str):
    """Fulfill ALL pending CDN downloads with this file path (best-effort).
    Called when an img_base64 arrives and we can't match by specific ID."""
    async with _pending_cdn_lock:
        keys = list(_pending_cdn_images.keys())
        for key in keys:
            fut = _pending_cdn_images.pop(key, None)
            if fut and not fut.done():
                fut.set_result(file_path)


async def _cleanup_cdn_pending(key: str):
    """Remove a pending entry (e.g. on timeout)."""
    async with _pending_cdn_lock:
        _pending_cdn_images.pop(key, None)


# ─── App State ──────────────────────────────────────────────────────

app_state = {
    "self_info": None,
    "contacts": None,
    "sessions": None,
    "last_messages": {},    # {wxid: {content, type, is_sender, time}}
    "avatar_urls": {},      # {wxid: direct_url} extracted from contact data
    "initialized": False,
}

message_store = MessageStore()
sqlite_cache = SqliteMessageCache()
_active_agent_id = ""
_account_runtimes: dict[str, dict] = {}
_self_wxid_to_agent_id: dict[str, str] = {}
_ACCOUNT_LOCK = asyncio.Lock()


def _new_app_state() -> dict:
    return {
        "self_info": None,
        "contacts": None,
        "sessions": None,
        "last_messages": {},
        "avatar_urls": {},
        "initialized": False,
    }


def _safe_runtime_id(agent_id: str) -> str:
    safe = "".join(c for c in str(agent_id or "default") if c.isalnum() or c in ("_", "-", "."))
    return safe[:80] or "default"


def _runtime_for(agent_id: str) -> dict:
    key = str(agent_id or "default")
    if key not in _account_runtimes:
        cache_path = os.path.join(os.path.dirname(__file__), ".sqlite_cache", f"{_safe_runtime_id(key)}.sqlite3")
        _account_runtimes[key] = {
            "app_state": _new_app_state(),
            "message_store": MessageStore(),
            "sqlite_cache": SqliteMessageCache(cache_path),
        }
    return _account_runtimes[key]


def _activate_runtime(agent_id: str) -> str:
    global app_state, message_store, sqlite_cache, _active_agent_id
    selected = str(agent_id or agent_manager.active_id() or _active_agent_id or "default")
    runtime = _runtime_for(selected)
    app_state = runtime["app_state"]
    message_store = runtime["message_store"]
    sqlite_cache = runtime["sqlite_cache"]
    _active_agent_id = selected
    return selected


def _agent_id_for_self_wxid(wxid: str) -> str:
    return _self_wxid_to_agent_id.get(str(wxid or ""), "")


def _extract_self_wxid(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    return str(
        data.get("selfwxid")
        or data.get("selfWxid")
        or data.get("self_wxid")
        or data.get("wxid")
        or nested.get("selfwxid")
        or nested.get("selfWxid")
        or nested.get("self_wxid")
        or nested.get("wxid")
        or ""
    )


# ─── Contact brief cache (name + avatar URL) ─────────────────────────
# Used to avoid repeatedly calling BatchGetContactBriefInfo for the same wxids.
_CONTACT_BRIEF_CACHE: dict[str, dict] = {}  # wxid -> {"name": str, "avatar": str, "ts": float}
_CONTACT_BRIEF_CACHE_TTL_SEC = 24 * 60 * 60  # 24h
_CONTACT_BRIEF_LOCK = asyncio.Lock()


# ─── Full contact profile cache ────────────────────────────────────
# Populated lazily via /GetContact for strangers / new chats.
_CONTACT_PROFILE_CACHE: dict[str, dict] = {}  # wxid -> {"profile": dict, "ts": float}
_CONTACT_PROFILE_CACHE_TTL_SEC = 24 * 60 * 60
_CONTACT_PROFILE_LOCK = asyncio.Lock()

# ─── Contact label cache ───────────────────────────────────────────
# Maps WeChat label ids from GetContact.LabelTag to readable names.
_CONTACT_LABEL_CACHE: dict[str, object] = {"map": {}, "ts": 0.0}
_CONTACT_LABEL_CACHE_TTL_SEC = 24 * 60 * 60
_CONTACT_LABEL_LOCK = asyncio.Lock()


def _get_self_wxid() -> str:
    self_info = app_state.get("self_info")
    if not isinstance(self_info, dict):
        return ""
    data = self_info.get("data", {})
    if isinstance(data, dict) and data.get("wxid"):
        return data.get("wxid", "")
    return self_info.get("wxid", "")


def _format_preview(msg_type: str, content: str) -> str:
    t = str(msg_type)
    if t == "1":
        return (content or "")[:50]
    if t == "3":
        return "[图片]"
    if t == "34":
        return "[语音]"
    if t == "43":
        return "[视频]"
    if t == "47":
        return "[表情]"
    if t == "48":
        return "[位置]"
    if t == "49":
        return "[链接/文件]"
    if t in ("10000", "10002"):
        return "[系统消息]"
    return (content or "")[:30] or "[消息]"


def _time_to_hhmm(time_text: str) -> str:
    if not time_text:
        return ""
    if " " in time_text:
        return (time_text.split(" ", 1)[1] or "")[:5]
    return time_text[:5]


def _normalize_callback_message(msg: dict, sendorrecv: str, self_wxid: str) -> tuple[str, dict] | tuple[None, None]:
    chat_id = ""
    from_gid = str(msg.get("fromgid", "") or "")
    from_id = str(msg.get("fromid", "") or "")
    to_id = str(msg.get("toid", "") or "")
    if from_gid:
        chat_id = from_gid
    elif from_id and from_id == self_wxid:
        chat_id = to_id
    else:
        chat_id = from_id
    if not chat_id:
        return None, None

    now_unix = int(time.time())
    raw_time = str(msg.get("time", "") or "")
    time_text = raw_time or datetime.fromtimestamp(now_unix).strftime("%Y-%m-%d %H:%M:%S")

    # Parse the actual message timestamp from the time field (format: "2026-02-25 08:18:13")
    # instead of always using now() — callbacks may arrive late for older messages
    msg_unix = now_unix
    if raw_time:
        try:
            msg_unix = int(datetime.strptime(raw_time[:19], "%Y-%m-%d %H:%M:%S").timestamp())
        except (ValueError, TypeError):
            pass

    msgtype = str(msg.get("msgtype", "") or "")

    # Detect self-sent messages: either sendorrecv="1", or fromid matches self
    # (mobile-sent messages arrive with sendorrecv="2" but fromid = self_wxid)
    is_self_sent = (
        str(sendorrecv) == "1" or
        (from_id == self_wxid and self_wxid and not from_gid)
    )
    # For group messages sent from mobile: fromid=self in a group context
    if from_gid and from_id == self_wxid and self_wxid:
        is_self_sent = True

    normalized = {
        "id": str(msg.get("msgsvrid", "") or msg.get("clientmsgid", "") or f"cb_{msg_unix}_{chat_id}"),
        "msgtype": msgtype,
        "time": time_text,
        "timestamp": msg_unix,
        "time_unix": msg_unix,
        "fromid": from_id or self_wxid,
        "toid": to_id,
        "fromgid": from_gid,
        "fromtype": str(msg.get("fromtype", "2" if from_gid else "1")),
        "msg": str(msg.get("msg", "") or ""),
        "sendorrecv": "1" if is_self_sent else str(sendorrecv or ""),
        "isSender": 1 if is_self_sent else 0,
        "img_path": msg.get("img_path"),
        "img_len": msg.get("img_len"),
        "video_path": msg.get("video_path"),
        "voice_len": msg.get("voice_len"),
        "voice_hex": msg.get("voice_hex"),
        "voice_data": msg.get("voice_data"),
        "gif_path": msg.get("gif_path"),
        "file_path": msg.get("file_path"),
        "info": msg.get("info"),
        "msgsource": msg.get("msgsource"),
    }
    return chat_id, normalized


def _is_callback_status_echo(msg: dict, sendorrecv: str, self_wxid: str) -> bool:
    """Filter Hook status callbacks that are not user-visible messages."""
    if not isinstance(msg, dict):
        return False

    msgtype = str(msg.get("msgtype", "") or "")
    content = str(msg.get("msg", "") or "").strip()
    from_id = str(msg.get("fromid", "") or "")
    from_gid = str(msg.get("fromgid", "") or "")
    is_self_echo = (
        str(sendorrecv) == "1"
        or (self_wxid and from_id == self_wxid and not from_gid)
    )
    if not is_self_echo:
        return False

    if msgtype == "1" and not content:
        return True
    if msgtype == "3" and content in {"PC发图片消息成功", "发图片消息成功"}:
        return True
    return False


def _store_message_and_session(chat_id: str, msg: dict) -> dict:
    message_store.add_message(chat_id, msg)
    preview = _format_preview(str(msg.get("msgtype", "")), str(msg.get("msg", "") or ""))
    # For group chats, prefix the preview with the sender's nickname
    if "@chatroom" in chat_id:
        if msg.get("isSender") == 1 or str(msg.get("sendorrecv", "")) == "1":
            preview = f"我: {preview}"
        else:
            sender_wxid = str(msg.get("fromid", "") or "")
            if sender_wxid:
                sender_name = (
                    message_store.get_contact(sender_wxid).get("name", "")
                    or _CONTACT_BRIEF_CACHE.get(sender_wxid, {}).get("name", "")
                    or sender_wxid
                )
                preview = f"{sender_name}: {preview}"
    msg_timestamp = int(msg.get("time_unix") or msg.get("timestamp") or int(time.time()))
    time_str = _time_to_hhmm(str(msg.get("time", "") or ""))
    is_recv = str(msg.get("sendorrecv", "")) == "2"
    snapshot = message_store.update_session(
        chat_id,
        last_msg=preview,
        last_time=time_str,
        unread_delta=1 if is_recv else 0,
        last_timestamp=msg_timestamp,
    )
    app_state["last_messages"][chat_id] = {
        "content": str(msg.get("msg", "") or ""),
        "type": str(msg.get("msgtype", "") or "1"),
        "is_sender": 1 if str(msg.get("sendorrecv", "")) == "1" else 0,
        "time": msg_timestamp,
    }
    try:
        sqlite_cache.upsert_messages(chat_id, [msg])
    except Exception as e:
        _log(f"[SQLITE_CACHE] realtime write failed for {chat_id}: {type(e).__name__}: {e}")
    return {
        "wxid": snapshot.wxid,
        "lastMsg": snapshot.last_msg,
        "lastTime": snapshot.last_time,
        "lastTimestamp": snapshot.last_timestamp,
        "unread": snapshot.unread,
    }


def _normalize_history_rows(wxid: str, rows: list[dict]) -> list[dict]:
    self_wxid = _get_self_wxid()
    is_group = "@chatroom" in wxid
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        msg_type = str(row.get("Type", "1"))
        is_sender = 1 if str(row.get("IsSender", 0)) == "1" else 0
        create_time = int(row.get("CreateTime", 0) or 0)
        from_id = self_wxid if is_sender else str(row.get("StrTalker", "") or wxid)
        if is_group and not is_sender:
            sender = str(row.get("SenderWxid", "") or "")
            if sender:
                from_id = sender
        time_text = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else ""
        out.append({
            "id": str(row.get("MsgSvrID", "") or f"db_{create_time}_{len(out)}"),
            "msgtype": msg_type,
            "time": time_text,
            "timestamp": create_time,
            "time_unix": create_time,
            "fromid": from_id,
            "toid": "" if is_group else (wxid if is_sender else self_wxid),
            "fromgid": wxid if is_group else "",
            "fromtype": "2" if is_group else "1",
            "msg": str(row.get("StrContent", "") or ""),
            "sendorrecv": "1" if is_sender else "2",
            "isSender": is_sender,
            "bytesExtraHex": row.get("BytesExtraHex", "") if msg_type == "3" else "",
        })
    out.sort(key=lambda m: (int(m.get("timestamp") or 0), str(m.get("id", ""))))
    return out


# ─── Startup / Shutdown ────────────────────────────────────────────

async def _run_backend_initialization(agent_id: str | None = None) -> bool:
    """Initialize cached state from the Hook/Protocol API."""
    selected_agent = _activate_runtime(agent_id or agent_manager.active_id())
    if selected_agent:
        await agent_manager.set_active(selected_agent)
    _log("=" * 60)
    _log(f"WeChat Backend starting...  [mode={config.LOGIN_MODE}] agent={selected_agent or 'default'}")
    _log("=" * 60)

    # Phase 0 — wait for Hook/Protocol API to be reachable
    _log("[INIT 0] Checking API connectivity...")
    for attempt in range(15):  # up to 30 seconds
        try:
            # Reset circuit breaker for each attempt
            wechat_api._consecutive_failures = 0
            test = await wechat_api._post("/IsLoginStatus", json={})
            _log(f"[INIT 0] ✓ API reachable (status={test.status_code})")
            wechat_api._consecutive_failures = 0
            break
        except Exception as e:
            wechat_api._consecutive_failures = 0  # don't let CB trigger during wait
            _log(f"[INIT 0] Waiting for API... ({(attempt+1)*2}s) {type(e).__name__}")
            await asyncio.sleep(2)
    else:
        _log("[INIT 0] ⚠ API not reachable after 30s, continuing anyway...")
    wechat_api._consecutive_failures = 0  # ensure clean slate for init

    # Phase 1 — run sequentially to avoid concurrent Hook access (Hook is NOT thread-safe)
    try:
        _log("[INIT 1/7] Loading self info...")
        app_state["self_info"] = await wechat_api.get_self_info()
        si = app_state["self_info"] if isinstance(app_state["self_info"], dict) else {}
        si_data = si.get("data", {}) if isinstance(si.get("data"), dict) else si
        wxid = str(
            si_data.get("wxid")
            or si_data.get("Wxid")
            or si_data.get("selfwxid")
            or si_data.get("selfWxid")
            or si_data.get("self_wxid")
            or si.get("wxid")
            or si.get("selfwxid")
            or si.get("selfWxid")
            or si.get("self_wxid")
            or ""
        )
        nickname = str(
            si_data.get("nickname") or si_data.get("NickName") or si_data.get("name") or
            si.get("nickname") or si.get("NickName") or wxid or selected_agent
        )
        avatar = str(
            si_data.get("head_big") or si_data.get("headimgurl") or si_data.get("head_img") or
            si_data.get("head_small") or si.get("head_big") or si.get("headimgurl") or ""
        )
        if wxid:
            _self_wxid_to_agent_id[wxid] = selected_agent
        await agent_manager.update_account(selected_agent, wxid=wxid, nickname=nickname, avatar=avatar)
        _log("[INIT 1/7] ✓ Self info loaded")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ self info failed: {e}")

    try:
        _log(f"[INIT 1/7] Configuring callback... recv_type={config.RECV_TYPE}")
        await wechat_api.configure_msg_receive(True, config.CALLBACK_URL, config.RECV_TYPE)
        _log("[INIT 1/7] ✓ Callback configured")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ callback config failed: {e}")

    try:
        _log("[INIT 1/7] Enabling anywhere download...")
        await wechat_api.enable_anywhere_download()
        _log("[INIT 1/7] ✓ Anywhere download enabled")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ anywhere download failed: {e}")

    try:
        _log("[INIT 1/7] Configuring pic download path...")
        await wechat_api.configure_pic_download_path(r"C:\Users\Administrator\Desktop\pic")
        _log("[INIT 1/7] ✓ Pic download path configured")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ Pic download path config failed: {e}")

    try:
        _log("[INIT 1/7] Initializing CDN subsystem...")
        cdn_result = await wechat_api.cdn_init()
        _log(f"[INIT 1/7] ✓ CDN initialized: {cdn_result}")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ CDN init failed: {e}")

    try:
        _log("[INIT 1/7] Initializing contacts (may take a while on remote)...")
        await wechat_api.init_contact()
        _log("[INIT 1/7] ✓ Contacts initialized")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ InitContact failed: {e}")
    # Reset circuit breaker so a slow InitContact doesn't block subsequent requests
    wechat_api._consecutive_failures = 0
    _log("[INIT 1/7] done")

    try:
        _log("[INIT 2/4] Loading contacts...")
        contacts = await wechat_api.get_friend_and_chatroom_list()
        app_state["contacts"] = contacts
        friend_list = contacts.get("friend", []) if isinstance(contacts, dict) else []
        for c in friend_list:
            if not isinstance(c, dict):
                continue
            wxid = c.get("wxid") or c.get("UserName") or ""
            name = c.get("markname") or c.get("nickname") or c.get("NickName") or ""
            avatar = (
                c.get("headimgurl") or c.get("head_img") or c.get("head_big") or
                c.get("head_small") or c.get("headimg") or c.get("avatar") or ""
            )
            if wxid:
                message_store.set_contact(wxid, name=name, avatar=avatar)
        _log(f"[INIT 2/4] ✓ Contacts loaded: {len(friend_list)}")
    except Exception as e:
        _log(f"[INIT 2/4] ✗ contacts load failed: {e}")

    try:
        _log("[INIT 3/7] Loading sessions...")
        app_state["sessions"] = await wechat_api.get_current_session()
        count = len(app_state["sessions"].get("data", [])) if isinstance(app_state["sessions"], dict) else 0
        _log(f"[INIT 3/7] ✓ Sessions loaded: {count}")
    except Exception as e:
        _log(f"[INIT 3/7] ✗ sessions load failed: {e}")

    # 4. Add self avatar explicitly
    avatar_urls: dict[str, str] = {}
    if app_state["self_info"] and isinstance(app_state["self_info"], dict):
        si = app_state["self_info"]
        # self_info may be {data: {...}} or flat dict
        si_data = si.get("data", {}) if isinstance(si.get("data"), dict) else si
        self_head = (
            si_data.get("head_big", "") or si_data.get("headimgurl", "") or
            si_data.get("head_img", "") or si_data.get("head_small", "") or
            si.get("head_big", "") or si.get("headimgurl", "") or si.get("head_img", "")
        )
        self_wxid = _get_self_wxid()
        if self_head and self_wxid:
            avatar_urls[self_wxid] = self_head
            _log(f"[INIT 4/7] ✓ Self avatar added: {self_wxid}")
        else:
            _log(f"[INIT 4/7] ⊘ No self avatar found in self_info (wxid={self_wxid}, keys={list(si_data.keys())})")
    else:
        _log("[INIT 4/7] ⊘ No self_info available")

    # 5. Batch-load avatars synchronously (before init fires)
    session_wxids_for_avatars: list[str] = []
    if app_state["sessions"] and isinstance(app_state["sessions"].get("data"), list):
        session_wxids_for_avatars = [
            s.get("strUsrName", "") for s in app_state["sessions"]["data"]
            if s.get("strUsrName")
        ]
    if session_wxids_for_avatars:
        batch_size = 100
        _log(f"[INIT 5/7] Loading avatars for {len(session_wxids_for_avatars)} sessions...")
        for i in range(0, len(session_wxids_for_avatars), batch_size):
            batch = session_wxids_for_avatars[i:i + batch_size]
            try:
                wxid_str = ",".join(batch)
                data = await wechat_api.batch_get_contact_brief_info(wxid_str)
                info_list = data.get("info", []) if isinstance(data, dict) else []
                for info in info_list:
                    if not isinstance(info, dict):
                        continue
                    wxid = info.get("wxid", "")
                    url = info.get("smallhead", "") or info.get("bighead", "")
                    name = info.get("markname", "") or info.get("nickname", "") or info.get("nick", "") or ""
                    if wxid and url:
                        avatar_urls[wxid] = url
                    if wxid:
                        message_store.set_contact(wxid, name=name, avatar=url)
                _log(f"[INIT 5/7]   batch {i//batch_size+1}: got {len(info_list)} avatars")
            except Exception as e:
                _log(f"[INIT 5/7]   batch {i//batch_size+1} failed: {e}")
            await asyncio.sleep(0.1)
        _log(f"[INIT 5/7] ✓ Got avatar URLs for {len(avatar_urls)} contacts.")
    else:
        _log("[INIT 5/7] ⊘ No sessions to load avatars for.")
    app_state["avatar_urls"] = avatar_urls

    # 6. Load last messages. Prefer local SQLite cache; only missings hit Hook DB.
    if session_wxids_for_avatars:
        cached_last = sqlite_cache.get_last_messages(session_wxids_for_avatars)
        if cached_last:
            app_state["last_messages"] = dict(cached_last)
            _log(f"[INIT 6/7] ✓ Loaded {len(cached_last)} last messages from local SQLite cache.")
        missing_last_wxids = [w for w in session_wxids_for_avatars if w not in cached_last]
        try:
            if missing_last_wxids:
                _log(f"[INIT 6/7] Loading last messages for {len(missing_last_wxids)} uncached sessions...")
                # Remote Hook servers can stall on QueryDB; don't block startup forever.
                last_msgs = await asyncio.wait_for(
                    wechat_api.get_last_messages_bulk(missing_last_wxids),
                    timeout=35.0,
                )
                app_state["last_messages"].update(last_msgs)
                sqlite_cache.upsert_last_messages(last_msgs)
                _log(f"[INIT 6/7] ✓ Last messages loaded from Hook DB for {len(last_msgs)} sessions.")
            else:
                _log("[INIT 6/7] ✓ All session last messages came from local SQLite cache.")
        except asyncio.TimeoutError:
            _log("[INIT 6/7] ⚠ bulk last messages timed out, continuing without DB preload")
        except Exception as e:
            _log(f"[INIT 6/7] ✗ bulk last messages failed: {e}")

        try:
            preload_chats = 0
            preload_msgs = 0
            for wxid in session_wxids_for_avatars:
                cached_msgs = sqlite_cache.get_messages(wxid, 50)
                if cached_msgs:
                    message_store.add_history(wxid, cached_msgs)
                    preload_chats += 1
                    preload_msgs += len(cached_msgs)
            _log(f"[INIT 6/7] ✓ Preloaded {preload_msgs} cached messages for {preload_chats} chats from SQLite.")
        except Exception as e:
            _log(f"[INIT 6/7] ✗ SQLite message preload failed: {type(e).__name__}: {e}")
    else:
        _log("[INIT 6/7] ⊘ No sessions to load last messages for.")

    if config.AGENT_WS_ENABLED and not agent_manager.is_connected(selected_agent):
        _log("[INIT 7/7] Agent disconnected during initialization; will retry on next connection.")
        app_state["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False)
        return False

    _log("[INIT 7/7] ✓ Initialization complete.")
    app_state["initialized"] = True
    await agent_manager.update_account(selected_agent, initialized=True)
    _log("=" * 60)
    _log(f"Backend ready at http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    _log(f"Login mode: {config.LOGIN_MODE}  |  API: {config.HOOK_BASE_URL}")
    if config.AGENT_WS_ENABLED:
        _log(f"Agent WS: {config.CLIENT_WSS_URL}  path={config.AGENT_WS_PATH}")
    _log(f"Callback URL: {config.CALLBACK_URL}")
    _log("=" * 60)
    return True

async def _run_initialization_after_agent():
    while True:
        _log(f"[INIT] Waiting for DLL agent on {config.AGENT_WS_PATH} ...")
        while not agent_manager.is_connected():
            await asyncio.sleep(1)
        for agent_id in agent_manager.uninitialized_agent_ids():
            _log(f"[INIT] DLL agent connected; starting Hook initialization for {agent_id}")
            async with _ACCOUNT_LOCK:
                with wechat_api.use_agent(agent_id):
                    await _run_backend_initialization(agent_id)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize on startup, cleanup on shutdown."""
    init_task = None
    if config.AGENT_WS_ENABLED:
        _log("=" * 60)
        _log(f"WeChat Backend starting...  [mode={config.LOGIN_MODE}]")
        _log(f"Agent WS enabled: {config.CLIENT_WSS_URL}  path={config.AGENT_WS_PATH}")
        _log("Hook initialization will run after the DLL agent connects.")
        _log("=" * 60)
        init_task = asyncio.create_task(_run_initialization_after_agent())
    else:
        await _run_backend_initialization()

    yield

    if init_task:
        init_task.cancel()
        try:
            await init_task
        except asyncio.CancelledError:
            pass

    # Shutdown
    _log("[SHUTDOWN] Disabling message callback...")
    try:
        await wechat_api.configure_msg_receive(False, "", config.RECV_TYPE)
    except Exception:
        pass
    await agent_manager.close()
    await wechat_api.client.aclose()
    _log("[SHUTDOWN] Done.")


# ─── FastAPI App ────────────────────────────────────────────────────

app = FastAPI(title="WeChat Web Client", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _request_access_ok(request: Request) -> bool:
    if not config.WEB_ACCESS_KEY:
        return False
    key = (
        request.headers.get("X-Access-Key")
        or request.query_params.get("key")
        or request.cookies.get("wechat_web_key")
        or ""
    )
    return str(key) == config.WEB_ACCESS_KEY


def _request_agent_id(request: Request) -> str:
    return str(request.headers.get("X-Agent-Id") or request.query_params.get("agent_id") or "").strip()


def _is_public_http_path(path: str) -> bool:
    return (
        path in {"/api/auth/login", "/api/callback", config.CALLBACK_PATH}
        or path == config.AGENT_WS_PATH
        or path.startswith("/uploads/")
    )


@app.middleware("http")
async def require_access_key(request: Request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not _is_public_http_path(path) and not _request_access_ok(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    agent_id = _request_agent_id(request)
    if agent_id and agent_manager.is_connected(agent_id):
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
        with wechat_api.use_agent(agent_id):
            return await call_next(request)
    return await call_next(request)


class AuthLoginRequest(BaseModel):
    key: str


class ActivateAccountRequest(BaseModel):
    agent_id: str


class MultiBroadcastTextRequest(BaseModel):
    wxids: list[str]
    msg: str
    agent_ids: list[str] = []


@app.post("/api/auth/login")
async def auth_login(req: AuthLoginRequest):
    if not config.WEB_ACCESS_KEY:
        return {"ok": False, "error": "access key is not configured"}
    if str(req.key or "") != config.WEB_ACCESS_KEY:
        return {"ok": False}
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts():
    return {
        "active_id": _active_agent_id or agent_manager.active_id(),
        "accounts": agent_manager.agents(),
    }


@app.post("/api/accounts/activate")
async def activate_account(req: ActivateAccountRequest):
    agent_id = str(req.agent_id or "").strip()
    if not agent_id or not agent_manager.is_connected(agent_id):
        return {"ok": False, "error": "agent not connected"}
    async with _ACCOUNT_LOCK:
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
        if not app_state.get("initialized"):
            with wechat_api.use_agent(agent_id):
                await _run_backend_initialization(agent_id)
    return {"ok": True, "active_id": agent_id, "account": agent_manager.get_agent(agent_id)}


# ─── Direction verification (DB-based) ────────────────────────────

async def _verify_msg_directions(
    messages: list[tuple[str, dict]], self_wxid: str
) -> None:
    """Verify and correct message direction using the DB ``IsSender`` flag.

    The Hook DLL (RecvType=1) may incorrectly report phone-sent *sync*
    messages as *received*, with ``fromid`` set to the conversation partner
    instead of self.  This function queries the WeChat local DB for the
    true ``IsSender`` value and corrects in-place if needed.
    """
    if not self_wxid or not messages:
        return

    # Collect non-group, non-self-sent messages with a real MsgSvrID
    verify: list[tuple[int, str]] = []
    for i, (_chat_id, msg) in enumerate(messages):
        if (msg.get("isSender") == 0
                and not msg.get("fromgid")
                and msg.get("id")
                and not str(msg["id"]).startswith("cb_")):
            verify.append((i, str(msg["id"])))

    if not verify:
        return

    svrids = [s for _, s in verify]
    _log(f"[DIR_VERIFY] Checking {len(svrids)} messages: {svrids}")

    # Batch query across all MSG*.db files in parallel
    ids_sql = ",".join(f"'{s}'" for s in svrids)
    sql = f"SELECT MsgSvrID, IsSender FROM MSG WHERE MsgSvrID IN ({ids_sql})"

    is_sender_map: dict[str, bool] = {}
    try:
        tasks = [
            wechat_api.query_db(db, sql, timeout=2.0)
            for db in ["MSG0.db", "MSG1.db", "MSG2.db", "MSG3.db"]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                continue
            if isinstance(r, dict):
                rows = r.get("data", [])
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict):
                            svrid = str(row.get("MsgSvrID", ""))
                            if svrid and str(row.get("IsSender", "0")) == "1":
                                is_sender_map[svrid] = True
    except Exception as e:
        _log(f"[DIR_VERIFY] DB query error: {e}")
        return

    # Correct messages where DB says self-sent
    for idx, svrid in verify:
        if is_sender_map.get(svrid):
            _chat_id, msg = messages[idx]
            old_from = msg["fromid"]
            msg["sendorrecv"] = "1"
            msg["isSender"] = 1
            msg["fromid"] = self_wxid
            msg["toid"] = old_from
            # For self-sent DM: chat_id = the conversation partner (recipient)
            new_chat_id = old_from
            messages[idx] = (new_chat_id, msg)
            _log(f"[DIR_FIX] ✓ {svrid}: corrected to self-sent (was from={old_from})")

    if is_sender_map:
        _log(f"[DIR_FIX] Corrected {len(is_sender_map)} message(s)")


# ─── WeChat Hook Callback (receives messages from Hook) ────────────

@app.post("/api/callback")
async def wechat_callback(request: Request):
    """Receive messages from WeChat Hook and broadcast to frontend via WebSocket."""
    data = await request.json()

    # Log top-level callback keys for debugging CDN download callbacks
    top_keys = list(data.keys()) if isinstance(data, dict) else []
    _log(f"[CALLBACK] top_keys={top_keys}")

    sendorrecv = str(data.get("sendorrecv", "") or "")
    self_wxid = _extract_self_wxid(data) or _get_self_wxid()
    callback_agent_id = str(
        data.get("agent_id", "")
        or data.get("agentId", "")
        or _agent_id_for_self_wxid(self_wxid)
        or agent_manager.agent_id_for_wxid(self_wxid)
        or ""
    )
    if not callback_agent_id and self_wxid:
        callback_agent_id = f"selfwxid_{self_wxid}"
        _log(f"[CALLBACK] selfwxid={self_wxid} has no agent mapping yet; using isolated runtime {callback_agent_id}")
    if not callback_agent_id and not self_wxid:
        callback_agent_id = _active_agent_id or agent_manager.active_id()
    if callback_agent_id:
        _activate_runtime(callback_agent_id)

    # ── RecvType=2: raw protobuf → parse into msglist ──────────
    pb_msg = data.get("pb_msg")
    if pb_msg and not data.get("msglist"):
        _log(f"[CALLBACK] RecvType=2 raw protobuf detected (len={len(pb_msg)}), parsing...")
        try:
            msglist = parse_raw_pb(str(pb_msg), self_wxid)
            _log(f"[CALLBACK] Parsed {len(msglist)} messages from raw protobuf")
        except Exception as e:
            _log(f"[CALLBACK] ✗ protobuf parse error: {type(e).__name__}: {e}")
            msglist = []
    else:
        msglist = data.get("msglist", []) or []
    normalized_messages: list[dict] = []
    session_updates: list[dict] = []
    pre_messages: list[tuple[str, dict]] = []  # (chat_id, normalized) — stored after direction verify

    # Check if there are pending CDN downloads (for smarter callback matching)
    async with _pending_cdn_lock:
        has_pending_cdn = len(_pending_cdn_images) > 0
        pending_keys_dbg = list(_pending_cdn_images.keys())
    if has_pending_cdn:
        _log(f"[CALLBACK] {len(pending_keys_dbg)} pending CDN downloads: {pending_keys_dbg}")

    for msg in msglist:
        # Debug: log all message keys to help diagnose CDN callback format
        if isinstance(msg, dict):
            keys = list(msg.keys())
            msgtype_dbg = msg.get("msgtype", "?")
            has_b64 = "img_base64" in msg
            has_path = "img_path" in msg
            svrid = msg.get("msgsvrid", "")
            _log(f"[CB_MSG] keys={keys} type={msgtype_dbg} svrid={svrid} has_b64={has_b64} has_path={has_path}")

        # If image arrives as base64 (common in remote_hook), decode+cache and convert to img_path.
        # This keeps websocket payload small and lets frontend reuse existing img_path rendering.
        try:
            img_b64 = msg.get("img_base64") if isinstance(msg, dict) else None
            if img_b64:
                mid = str(msg.get("msgsvrid", "") or msg.get("clientmsgid", "") or f"cb_{int(time.time())}")
                saved, blen = _save_img_base64_to_cache(str(img_b64), mid)
                if saved:
                    msg["img_path"] = saved
                    msg["img_len"] = msg.get("img_len") or blen
                    msg.pop("img_base64", None)  # don't broadcast giant base64
                    _log(f"[IMG_BASE64] cached → {saved} ({blen} bytes)")
                    # Try to fulfill pending CDN download by msgsvrid
                    matched = await _fulfill_cdn_pending(f"msgsvrid:{mid}", saved)
                    if matched:
                        _log(f"[IMG_BASE64] ✓ Fulfilled pending CDN by msgsvrid:{mid}")
                    # If no match by msgsvrid AND there are pending CDN downloads,
                    # this might be a CDN_Download_Pic callback with a different msgsvrid.
                    # Since we serialize CDN downloads (1 at a time), fulfilling the
                    # single pending download is safe.
                    if not matched and has_pending_cdn:
                        _log(f"[IMG_BASE64] No msgsvrid match, trying to fulfill pending CDN downloads...")
                        await _fulfill_all_cdn_pending(saved)
                        # Also resolve inflight dedup futures
                        async with _inflight_cdn_lock:
                            for k, ifut in list(_inflight_cdn_downloads.items()):
                                if not ifut.done():
                                    ifut.set_result(saved)
                            _inflight_cdn_downloads.clear()
        except Exception as e:
            _log(f"[IMG_BASE64] decode error: {type(e).__name__}: {e}")

        msgtype = str(msg.get("msgtype", ""))
        fromid = msg.get("fromid", "")
        fromgid = msg.get("fromgid", "")
        toid_dbg = msg.get("toid", "")
        content = msg.get("msg", "")[:80]

        # Log (include toid + self_wxid for direction debugging)
        source = fromgid if fromgid else fromid
        _log(f"[MSG] type={msgtype} from={source} to={toid_dbg} self={self_wxid} sendorrecv={sendorrecv} | {content}")

        if msgtype == "9994":
            continue
        if _is_callback_status_echo(msg, sendorrecv, self_wxid):
            _log(f"[CALLBACK] Skip status echo type={msgtype} content={content!r}")
            continue
        chat_id, normalized = _normalize_callback_message(msg, sendorrecv, self_wxid)
        if not chat_id or not normalized:
            continue
        pre_messages.append((chat_id, normalized))

    # ── DB-based direction verification ──────────────────────────
    # Hook DLL (RecvType=1) may report phone-sent sync messages as received
    # with fromid = conversation partner.  Check DB IsSender to correct.
    if pre_messages and self_wxid:
        await _verify_msg_directions(pre_messages, self_wxid)

    # ── Lazy profile hydration for new incoming senders ───────────
    profile_updates: dict[str, dict] = {}
    profile_wxids: list[str] = []
    for _chat_id, msg in pre_messages:
        if str(msg.get("sendorrecv", "")) != "2":
            continue
        sender_wxid = str(msg.get("fromid", "") or "")
        if sender_wxid and sender_wxid != self_wxid and not sender_wxid.endswith("@chatroom"):
            profile_wxids.append(sender_wxid)
    if profile_wxids:
        profile_updates = await _ensure_contact_profiles(profile_wxids, require_full=False)

    # ── Store & collect for broadcast ────────────────────────────
    for chat_id, normalized in pre_messages:
        normalized_messages.append(normalized)
        session_updates.append(_store_message_and_session(chat_id, normalized))

    # Update the cached session list with any new chat_ids from callbacks
    # so they'll be included in future /api/sessions/refresh DB queries
    if session_updates and isinstance(app_state.get("sessions"), dict):
        cached_data = app_state["sessions"].get("data", [])
        cached_wxids = {s.get("strUsrName", "") for s in cached_data}
        for su in session_updates:
            wxid = su.get("wxid", "")
            if wxid and wxid not in cached_wxids:
                cached_data.append({"strUsrName": wxid, "strNickName": ""})
                cached_wxids.add(wxid)
                _log(f"[SESSIONS] Added new session to cache: {wxid}")

    # Broadcast normalized callback data to all connected frontends
    await manager.broadcast({
        "type": "wechat_message",
        "data": {
            "account_id": callback_agent_id,
            "sendorrecv": sendorrecv,
            "selfwxid": self_wxid,
            "messages": normalized_messages,
            "msglist": normalized_messages,
            "session_updates": session_updates,
            "contact_updates": profile_updates,
        },
    })

    return {"status": "success"}


if config.CALLBACK_PATH != "/api/callback":
    app.add_api_route(
        config.CALLBACK_PATH,
        wechat_callback,
        methods=["POST"],
        include_in_schema=False,
    )


# ─── WebSocket (frontend connection) ───────────────────────────────

@app.websocket(config.AGENT_WS_PATH)
async def agent_websocket_endpoint(websocket: WebSocket):
    await agent_manager.handle(websocket)


@app.get("/api/agent/status")
async def agent_status():
    return {
        **agent_manager.status(),
        "enabled": config.AGENT_WS_ENABLED,
        "path": config.AGENT_WS_PATH,
        "client_wss_url": config.CLIENT_WSS_URL,
        "client_wss_port": config.CLIENT_WSS_PORT,
    }


@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_key = str(websocket.query_params.get("key") or "")
    if not config.WEB_ACCESS_KEY or ws_key != config.WEB_ACCESS_KEY:
        await websocket.close(code=1008, reason="unauthorized")
        return
    agent_id = str(websocket.query_params.get("agent_id") or "").strip()
    if agent_id and agent_manager.is_connected(agent_id):
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
    await manager.connect(websocket)

    # Send initial state on connect
    await websocket.send_text(json.dumps({
        "type": "init",
        "data": {
            "account_id": _active_agent_id or agent_manager.active_id(),
            "self_info": app_state["self_info"],
            "contacts": app_state["contacts"],
            "sessions": app_state["sessions"],
            "last_messages": app_state["last_messages"],
            "avatar_urls": app_state.get("avatar_urls", {}),
            "messages_cache": message_store.get_all_messages(),
            "session_cache": message_store.get_sessions(),
        }
    }, ensure_ascii=False))

    try:
        while True:
            # Keep connection alive, listen for frontend commands
            text = await websocket.receive_text()
            data = json.loads(text)
            msg_type = data.get("type", "unknown")
            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            else:
                _log(f"[WS] Received from frontend: {msg_type}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ─── REST API: Self Info ───────────────────────────────────────────

@app.get("/api/self")
async def get_self():
    """Return cached self info, or refresh from Hook."""
    if not app_state["self_info"]:
        app_state["self_info"] = await wechat_api.get_self_info()
    return app_state["self_info"]


# ─── REST API: Contacts ───────────────────────────────────────────

@app.get("/api/contacts")
async def get_contacts():
    """Return cached contacts, or refresh from Hook."""
    if not app_state["contacts"]:
        await wechat_api.init_contact()
        app_state["contacts"] = await wechat_api.get_friend_and_chatroom_list()
    return app_state["contacts"]


@app.get("/api/contacts/refresh")
async def refresh_contacts():
    """Force refresh contacts from Hook."""
    await wechat_api.init_contact()
    app_state["contacts"] = await wechat_api.get_friend_and_chatroom_list()
    return app_state["contacts"]


@app.get("/api/contacts/{wxid}")
async def get_contact_detail(wxid: str):
    """Get detailed info for a specific contact."""
    return await wechat_api.get_friend_detail_info(wxid)


@app.get("/api/contacts/{wxid}/avatar")
async def get_contact_avatar(wxid: str):
    """Return pre-loaded avatar URL for a contact."""
    url = app_state.get("avatar_urls", {}).get(wxid, "")
    return {"wxid": wxid, "url": url}


class BriefBatchRequest(BaseModel):
    wxids: list[str] = []


class ProfileBatchRequest(BaseModel):
    wxids: list[str] = []


def _normalize_wxids(wxids: list[str]) -> list[str]:
    """Normalize, de-dup, and filter out invalid/non-contact ids."""
    out: list[str] = []
    seen: set[str] = set()
    for w in wxids or []:
        if not isinstance(w, str):
            continue
        w = w.strip()
        if not w or w in seen:
            continue
        # Not a contact
        if w.endswith("@chatroom"):
            continue
        seen.add(w)
        out.append(w)
    return out


async def _query_session_list_from_db() -> dict:
    """Read the native WeChat Session table ordered by nOrder."""
    sql = "select strUsrName, strNickName from Session order by nOrder desc"
    data = await wechat_api.query_db("MicroMsg.db", sql, timeout=8.0)
    rows = data.get("data") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        rows = []

    sessions: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        wxid = str(
            row.get("strUsrName")
            or row.get("StrUsrName")
            or row.get("UserName")
            or row.get("wxid")
            or ""
        ).strip()
        if not wxid or wxid in seen:
            continue
        nickname = str(
            row.get("strNickName")
            or row.get("StrNickName")
            or row.get("NickName")
            or row.get("nickname")
            or ""
        )
        seen.add(wxid)
        sessions.append({"strUsrName": wxid, "strNickName": nickname})

    return {"data": sessions}


def _contact_profile_wxid(profile: dict) -> str:
    return str(
        profile.get("wxid")
        or profile.get("UserName")
        or profile.get("userName")
        or profile.get("describe")
        or ""
    )


def _contact_profile_name(profile: dict) -> str:
    return str(
        profile.get("Remark")
        or profile.get("remark")
        or profile.get("markname")
        or profile.get("NickName")
        or profile.get("nickname")
        or profile.get("nick")
        or _contact_profile_wxid(profile)
        or ""
    )


def _contact_profile_avatar(profile: dict) -> str:
    return str(
        profile.get("SmallHeadImgUrl")
        or profile.get("BigHeadImgUrl")
        or profile.get("smallhead")
        or profile.get("bighead")
        or profile.get("headimgurl")
        or profile.get("head_img")
        or profile.get("head_big")
        or profile.get("head_small")
        or profile.get("avatar")
        or ""
    )


def _parse_label_map(data: dict) -> dict[str, str]:
    """Parse /GetContactLabelList response into {label_id: label_name}."""
    if not isinstance(data, dict):
        return {}
    labels = (
        data.get("label")
        or data.get("labels")
        or data.get("data")
        or data.get("list")
        or data.get("ContactLabel")
        or []
    )
    if isinstance(labels, dict):
        labels = labels.get("label") or labels.get("items") or labels.get("list") or []
    if not isinstance(labels, list):
        return {}

    out: dict[str, str] = {}
    for item in labels:
        if not isinstance(item, dict):
            continue
        label_id = str(
            item.get("id")
            or item.get("labelid")
            or item.get("LabelID")
            or item.get("LabelId")
            or ""
        ).strip()
        name = str(
            item.get("name")
            or item.get("labelname")
            or item.get("LabelName")
            or item.get("Name")
            or ""
        ).strip()
        if label_id and name:
            out[label_id] = name
    return out


async def _ensure_contact_label_map() -> dict[str, str]:
    """Return cached label map, refreshing from hook when stale."""
    now = time.time()
    async with _CONTACT_LABEL_LOCK:
        cached = _CONTACT_LABEL_CACHE.get("map")
        cached_ts = float(_CONTACT_LABEL_CACHE.get("ts", 0) or 0)
        if isinstance(cached, dict) and cached and now - cached_ts <= _CONTACT_LABEL_CACHE_TTL_SEC:
            return {str(k): str(v) for k, v in cached.items()}

    try:
        data = await wechat_api.get_contact_label_list()
        label_map = _parse_label_map(data)
    except Exception as e:
        _log(f"[PROFILE] GetContactLabelList failed: {type(e).__name__}: {e}")
        label_map = {}

    async with _CONTACT_LABEL_LOCK:
        if label_map:
            _CONTACT_LABEL_CACHE["map"] = label_map
            _CONTACT_LABEL_CACHE["ts"] = now
        cached = _CONTACT_LABEL_CACHE.get("map")
        if isinstance(cached, dict):
            return {str(k): str(v) for k, v in cached.items()}
    return {}


def _split_contact_label_ids(raw_value) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, (list, tuple, set)):
        pieces = []
        for value in raw_value:
            pieces.extend(_split_contact_label_ids(value))
        return pieces

    text = str(raw_value).strip()
    if not text:
        return []
    for sep in [";", "|", " "]:
        text = text.replace(sep, ",")

    seen: set[str] = set()
    ids: list[str] = []
    for part in text.split(","):
        label_id = part.strip()
        if not label_id or label_id == "0" or label_id in seen:
            continue
        seen.add(label_id)
        ids.append(label_id)
    return ids


def _contact_profile_label_text(profile: dict, label_map: dict[str, str]) -> str:
    explicit = str(
        profile.get("LabelText")
        or profile.get("LabelName")
        or profile.get("LabelNames")
        or profile.get("labelText")
        or profile.get("labelname")
        or ""
    ).strip()
    if explicit:
        return explicit

    label_ids = _split_contact_label_ids(
        profile.get("LabelTag")
        or profile.get("labeltag")
        or profile.get("LabelIDList")
        or profile.get("labelidlist")
    )
    if not label_ids:
        return ""

    names: list[str] = []
    for label_id in label_ids:
        names.append(label_map.get(label_id) or label_id)
    return " ".join(names)


def _enrich_contact_profile_summaries(summaries: dict[str, dict], label_map: dict[str, str]) -> None:
    for summary in summaries.values():
        if not isinstance(summary, dict):
            continue
        profile = summary.get("profile")
        if not isinstance(profile, dict):
            continue
        label_text = _contact_profile_label_text(profile, label_map)
        if not label_text:
            continue
        enriched = dict(profile)
        enriched["LabelText"] = label_text
        summary["profile"] = enriched
        summary["label"] = label_text


def _contact_profile_summary(profile: dict) -> dict:
    wxid = _contact_profile_wxid(profile)
    return {
        "wxid": wxid,
        "name": _contact_profile_name(profile),
        "avatar": _contact_profile_avatar(profile),
        "profile": profile,
    }


async def _cache_contact_profiles(profiles: list[dict]) -> dict[str, dict]:
    """Save full profiles plus brief name/avatar caches. Returns frontend updates."""
    now = time.time()
    updates: dict[str, dict] = {}
    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = app_state.setdefault("avatar_urls", {})
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                wxid = _contact_profile_wxid(profile)
                if not wxid:
                    continue
                summary = _contact_profile_summary(profile)
                name = summary.get("name", "")
                avatar = summary.get("avatar", "")
                _CONTACT_PROFILE_CACHE[wxid] = {"profile": profile, "ts": now}
                _CONTACT_BRIEF_CACHE[wxid] = {"name": name, "avatar": avatar, "ts": now}
                if avatar:
                    avatar_urls[wxid] = avatar
                message_store.set_contact(wxid, name=name, avatar=avatar)
                updates[wxid] = summary
    return updates


async def _ensure_contact_profiles(wxids: list[str], *, require_full: bool = True) -> dict[str, dict]:
    """Return contact profiles/summaries from cache, calling /GetContact for misses.

    require_full=True is used by the profile card and fetches when the full
    profile cache is absent. require_full=False is used by callbacks and only
    fetches when we have no usable avatar/name for a new sender.
    """
    wxids = _normalize_wxids(wxids)
    if not wxids:
        return {}

    now = time.time()
    result: dict[str, dict] = {}
    missing: list[str] = []

    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = app_state.get("avatar_urls", {}) or {}
            for wxid in wxids:
                cached_profile = _CONTACT_PROFILE_CACHE.get(wxid)
                profile_ok = bool(cached_profile) and (
                    now - float(cached_profile.get("ts", 0)) <= _CONTACT_PROFILE_CACHE_TTL_SEC
                )
                if profile_ok:
                    profile = cached_profile.get("profile", {}) or {}
                    result[wxid] = _contact_profile_summary(profile)
                    continue

                cached_brief = _CONTACT_BRIEF_CACHE.get(wxid)
                brief_ok = bool(cached_brief) and (
                    now - float(cached_brief.get("ts", 0)) <= _CONTACT_BRIEF_CACHE_TTL_SEC
                )
                direct_avatar = avatar_urls.get(wxid, "")
                store_contact = message_store.get_contact(wxid)
                name = (
                    (cached_brief or {}).get("name", "")
                    or store_contact.get("name", "")
                    or ""
                )
                avatar = (
                    direct_avatar
                    or (cached_brief or {}).get("avatar", "")
                    or store_contact.get("avatar", "")
                    or ""
                )

                has_real_name = bool(name and name != wxid)
                if not require_full and has_real_name and avatar:
                    result[wxid] = {"wxid": wxid, "name": name or wxid, "avatar": avatar, "profile": {}}
                    continue

                missing.append(wxid)

    if missing:
        batch_size = 100
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            try:
                data = await wechat_api.get_contact(batch)
                contacts = data.get("contacts") if isinstance(data, dict) else []
                if not isinstance(contacts, list):
                    contacts = [data] if isinstance(data, dict) and _contact_profile_wxid(data) else []
                updates = await _cache_contact_profiles(contacts)
                result.update(updates)
            except Exception as e:
                _log(f"[PROFILE] GetContact failed ({len(batch)}): {type(e).__name__}: {e}")
            await asyncio.sleep(0.05)

    if any(
        _split_contact_label_ids((summary.get("profile") or {}).get("LabelTag") or (summary.get("profile") or {}).get("labeltag"))
        for summary in result.values()
        if isinstance(summary, dict) and isinstance(summary.get("profile"), dict)
    ):
        label_map = await _ensure_contact_label_map()
        _enrich_contact_profile_summaries(result, label_map)

    # Always return an entry for requested wxids so the UI can display a fallback.
    for wxid in wxids:
        result.setdefault(wxid, {"wxid": wxid, "name": wxid, "avatar": "", "profile": {"wxid": wxid}})

    return result


@app.post("/api/contacts/brief-batch")
async def post_contacts_brief_batch(req: BriefBatchRequest):
    """Resolve {name, avatar} for a list of wxids.

    Designed for group chats: instead of fetching ALL group members, the frontend
    can request brief info only for the senders that appear in loaded messages.

    Uses a TTL cache to avoid repeated BatchGetContactBriefInfo calls.
    """
    wxids = _normalize_wxids(req.wxids)
    if not wxids:
        return {"members": {}}

    now = time.time()
    members: dict[str, dict] = {}
    missing: list[str] = []

    # 1) Serve from cache or preloaded avatar_urls
    async with _CONTACT_BRIEF_LOCK:
        # Opportunistic prune of expired entries (keep it cheap)
        if len(_CONTACT_BRIEF_CACHE) > 5000:
            expired = [
                k for k, v in _CONTACT_BRIEF_CACHE.items()
                if now - float(v.get("ts", 0)) > _CONTACT_BRIEF_CACHE_TTL_SEC
            ]
            for k in expired[:2000]:
                _CONTACT_BRIEF_CACHE.pop(k, None)

        avatar_urls = app_state.get("avatar_urls", {}) or {}
        for wxid in wxids:
            cached = _CONTACT_BRIEF_CACHE.get(wxid)
            cached_ok = bool(cached) and (now - float(cached.get("ts", 0)) <= _CONTACT_BRIEF_CACHE_TTL_SEC)
            direct_avatar = avatar_urls.get(wxid, "")

            if cached_ok:
                name = cached.get("name", "") or ""
                avatar = cached.get("avatar", "") or ""
                if direct_avatar:
                    avatar = direct_avatar
                if name and name != wxid:
                    members[wxid] = {"name": name, "avatar": avatar}
                    continue

            missing.append(wxid)

    # 2) Ask Hook only for the missing ones (max 100 per call)
    if missing:
        batch_size = 100
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            wxid_str = ",".join(batch)
            try:
                data = await wechat_api.batch_get_contact_brief_info(wxid_str)
                info_list = data.get("info", []) if isinstance(data, dict) else []
                found_in_batch: dict[str, dict] = {}
                for info in info_list:
                    if not isinstance(info, dict):
                        continue
                    wxid = info.get("wxid", "") or ""
                    if not wxid:
                        continue
                    name = (
                        info.get("markname", "") or
                        info.get("nickname", "") or
                        info.get("nick", "") or
                        info.get("WXAccount", "") or
                        ""
                    )
                    avatar = info.get("smallhead", "") or info.get("bighead", "") or ""
                    found_in_batch[wxid] = {"name": name, "avatar": avatar}

                if found_in_batch:
                    async with _CONTACT_BRIEF_LOCK:
                        avatar_urls = app_state.get("avatar_urls", {}) or {}
                        for wxid, entry in found_in_batch.items():
                            if not entry.get("avatar") and avatar_urls.get(wxid):
                                entry["avatar"] = avatar_urls[wxid]
                            members[wxid] = entry
                            _CONTACT_BRIEF_CACHE[wxid] = {"name": entry.get("name", ""), "avatar": entry.get("avatar", ""), "ts": now}
            except Exception as e:
                _log(f"[BRIEF] batch brief info failed ({len(batch)}): {e}")

            # Small spacing to avoid overwhelming Hook
            await asyncio.sleep(0.05)

    # 3) Fallback: provide /api/avatar proxy for anyone still missing an avatar
    for wxid in wxids:
        entry = members.get(wxid)
        if not entry:
            members[wxid] = {"name": "", "avatar": ""}
            entry = members[wxid]
        if not entry.get("avatar"):
            entry["avatar"] = f"/api/avatar/{wxid}"
        if not entry.get("name"):
            entry["name"] = wxid

    return {"members": members}


@app.post("/api/contacts/profile-batch")
async def post_contacts_profile_batch(req: ProfileBatchRequest):
    """Resolve full contact profiles via /GetContact with a runtime cache."""
    members = await _ensure_contact_profiles(req.wxids, require_full=True)
    return {"members": members}


@app.get("/api/sessions")
async def get_sessions():
    """Get current session (conversation) list from cache."""
    return app_state["sessions"] or {}


@app.get("/api/sessions/refresh")
async def refresh_sessions():
    """Refresh session list from MicroMsg.db Session ordered by nOrder desc."""
    t0 = time.time()
    try:
        db_sessions = await _query_session_list_from_db()
        if db_sessions.get("data"):
            app_state["sessions"] = db_sessions
    except Exception as e:
        _log(f"[REFRESH] Query Session table failed: {type(e).__name__}: {e}")

    raw_sessions = app_state["sessions"]
    session_list = raw_sessions.get("data", []) if isinstance(raw_sessions, dict) else []
    wxids = [s.get("strUsrName", "") for s in session_list if s.get("strUsrName")]

    # Start with real-time callback data (always up-to-date for active chats)
    last_messages = dict(app_state.get("last_messages", {}))

    # Only query DB for sessions NOT already covered by callback data or SQLite.
    missing_wxids = [w for w in wxids if w not in last_messages]
    cache_count = 0
    if missing_wxids:
        try:
            cached_messages = sqlite_cache.get_last_messages(missing_wxids)
            for wxid, msg in cached_messages.items():
                if wxid not in last_messages:
                    last_messages[wxid] = msg
                    app_state["last_messages"][wxid] = msg
                    cache_count += 1
        except Exception as e:
            _log(f"[REFRESH] SQLite last-message cache failed: {type(e).__name__}: {e}")
    missing_wxids = [w for w in wxids if w not in last_messages]
    db_count = 0
    if missing_wxids:
        try:
            db_messages = await wechat_api.get_last_messages_bulk(missing_wxids)
            for wxid, msg in db_messages.items():
                if wxid not in last_messages:
                    last_messages[wxid] = msg
                    # Cache DB results so we don't re-query next time
                    app_state["last_messages"][wxid] = msg
                    db_count += 1
            sqlite_cache.upsert_last_messages(db_messages)
        except Exception:
            pass

    total_ms = int((time.time() - t0) * 1000)
    _log(f"[REFRESH] ✓ {len(wxids)} sessions from Session table, {len(last_messages)} msgs "
         f"(callback={len(last_messages) - cache_count - db_count}, sqlite={cache_count}, db={db_count}, "
         f"skipped={len(wxids) - len(missing_wxids)}) — {total_ms}ms")
    return {"sessions": raw_sessions, "last_messages": last_messages}


# ─── REST API: Messages (History via QueryDB) ─────────────────────

@app.get("/api/messages/{wxid}")
async def get_messages(wxid: str, limit: int = 50, db: str = "MSG0.db"):
    """Get chat history for a contact/group.
    Prefer local SQLite cache once a wxid has been initialized; query Hook DB
    only for first-time cache warmup.
    """
    if sqlite_cache.has_initialized(wxid):
        cached = sqlite_cache.get_messages(wxid, limit)
        if cached:
            message_store.add_history(wxid, cached)
        return {"data": message_store.get_messages(wxid, limit), "source": "sqlite"}

    history = await wechat_api.get_chat_history(wxid, max(limit, 100))
    rows = history.get("data", []) if isinstance(history, dict) else []
    normalized = _normalize_history_rows(wxid, rows)
    message_store.add_history(wxid, normalized)
    try:
        if normalized:
            sqlite_cache.upsert_messages(wxid, normalized, mark_initialized=True)
        else:
            sqlite_cache.mark_initialized(wxid)
    except Exception as e:
        _log(f"[SQLITE_CACHE] history write failed for {wxid}: {type(e).__name__}: {e}")
    return {"data": message_store.get_messages(wxid, limit), "source": "hook_db"}


@app.get("/api/messages/{wxid}/older")
async def get_older_messages(wxid: str, before: int = 0, limit: int = 50):
    """Load older messages before a given timestamp (for infinite scroll).
    Returns messages with CreateTime < before, sorted chronologically."""
    if before <= 0:
        return {"data": []}
    cached = sqlite_cache.get_messages(wxid, limit, before=before)
    if cached:
        message_store.add_history_no_flag(wxid, cached)
        return {"data": cached, "source": "sqlite"}

    history = await wechat_api.get_chat_history(wxid, limit, before_time=before)
    rows = history.get("data", []) if isinstance(history, dict) else []
    normalized = _normalize_history_rows(wxid, rows)
    # Add to store so they persist in memory
    message_store.add_history_no_flag(wxid, normalized)
    try:
        sqlite_cache.upsert_messages(wxid, normalized)
    except Exception as e:
        _log(f"[SQLITE_CACHE] older-history write failed for {wxid}: {type(e).__name__}: {e}")
    return {"data": normalized, "source": "hook_db"}


@app.get("/api/messages/{wxid}/query")
async def query_messages(wxid: str, keyword: str, limit: int = 50, db: str = "MSG0.db"):
    """Search messages by keyword."""
    sql = (
        f"SELECT TalkerId, CreateTime, StrTalker, StrContent, MsgSvrID, Type "
        f"FROM MSG WHERE StrTalker = '{wxid}' AND StrContent LIKE '%{keyword}%' "
        f"ORDER BY localId DESC LIMIT {limit}"
    )
    return await wechat_api.query_db(db, sql)


@app.get("/api/unread")
async def get_unread():
    """Get unread message count."""
    return await wechat_api.get_unread_msg_num()


# ─── REST API: Send Messages ──────────────────────────────────────

class SendTextRequest(BaseModel):
    wxid: str
    msg: str

class SendImageRequest(BaseModel):
    wxid: str
    picpath: str
    diyfilename: str = ""
    fileData: str = ""

class SendFileRequest(BaseModel):
    wxid: str
    filepath: str
    fileData: str = ""

class SendVideoRequest(BaseModel):
    wxid: str
    videopath: str
    fileData: str = ""

class SendGifRequest(BaseModel):
    wxid: str
    gifpath: str
    fileData: str = ""

class SendQuoteRequest(BaseModel):
    towxid: str
    title: str
    svrid: str
    fromusr: str
    displayname: str
    chatusr: str

class SendAtRequest(BaseModel):
    gid: str
    wxidlist: str
    nicknamelist: str
    msg: str

class BroadcastTextRequest(BaseModel):
    wxids: list[str]
    msg: str

class RevokeRequest(BaseModel):
    msg_svrid: int
    to_wxid: str

class SessionActionRequest(BaseModel):
    wxid: str


_send_counter = 0

_BROADCAST_IMG_CDN_KEYS = (
    "fileid",
    "authkey",
    "filemd5",
    "filesize",
    "filecrc32",
    "rawmidimgsize",
    "rawthumbsize",
    "thumbheight",
    "thumbwidth",
)


def _send_result_ok(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    status_code = result.get("status_code")
    if status_code is not None:
        try:
            if int(status_code) >= 400:
                return False
        except Exception:
            pass
    code = result.get("code")
    if code is not None:
        try:
            if int(code) < 0:
                return False
        except Exception:
            pass
    ret = result.get("ret")
    if ret is not None:
        try:
            if int(ret) <= 0 and not result.get("MsgSvrID"):
                return False
        except Exception:
            pass
    retmsg = str(result.get("retmsg") or "").lower()
    if retmsg and retmsg not in {"success", "ok"} and "error" in retmsg:
        return False
    return True


async def _broadcast_local_sent_message(chat_id: str, msg_type: str, content: str = "", extra: dict | None = None) -> None:
    global _send_counter
    _send_counter += 1
    self_wxid = _get_self_wxid()
    now_unix = int(time.time())
    now_ms = int(time.time() * 1000)
    time_text = datetime.fromtimestamp(now_unix).strftime("%Y-%m-%d %H:%M:%S")
    msg = {
        "id": f"send_{now_ms}_{_send_counter}_{chat_id}_{msg_type}",
        "msgtype": msg_type,
        "time": time_text,
        "timestamp": now_unix,
        "time_unix": now_unix,
        "fromid": self_wxid,
        "toid": chat_id,
        "fromgid": chat_id if chat_id.endswith("@chatroom") else "",
        "fromtype": "2" if chat_id.endswith("@chatroom") else "1",
        "msg": content,
        "sendorrecv": "1",
        "isSender": 1,
    }
    if extra:
        msg.update(extra)
    session_update = _store_message_and_session(chat_id, msg)
    await manager.broadcast({
        "type": "message_sent",
        "data": {
            "account_id": _active_agent_id or agent_manager.active_id(),
            "chat_id": chat_id,
            "message": msg,
            "session_update": session_update,
        }
    })


@app.post("/api/send/text")
async def send_text(req: SendTextRequest):
    result = await wechat_api.send_text(req.wxid, req.msg)
    await _broadcast_local_sent_message(req.wxid, "1", req.msg)
    return result


@app.post("/api/send/image")
async def send_image(req: SendImageRequest):
    result = await wechat_api.send_image(req.wxid, req.picpath, req.diyfilename, req.fileData)
    await _broadcast_local_sent_message(req.wxid, "3", "", {"img_path": req.picpath})
    return result


@app.post("/api/send/file")
async def send_file(req: SendFileRequest):
    result = await wechat_api.send_file(req.wxid, req.filepath, req.fileData)
    await _broadcast_local_sent_message(req.wxid, "49", "", {"file_path": req.filepath})
    return result


@app.post("/api/send/video")
async def send_video(req: SendVideoRequest):
    result = await wechat_api.send_video(req.wxid, req.videopath, req.fileData)
    await _broadcast_local_sent_message(req.wxid, "43", "", {"video_path": req.videopath})
    return result


@app.post("/api/send/gif")
async def send_gif(req: SendGifRequest):
    result = await wechat_api.send_gif(req.wxid, req.gifpath, req.fileData)
    await _broadcast_local_sent_message(req.wxid, "47", "", {"gif_path": req.gifpath})
    return result


@app.post("/api/send/quote")
async def send_quote(req: SendQuoteRequest):
    result = await wechat_api.send_quote(
        req.towxid, req.title, req.svrid,
        req.fromusr, req.displayname, req.chatusr
    )
    await _broadcast_local_sent_message(req.towxid, "49", req.title)
    return result


@app.post("/api/send/at")
async def send_at(req: SendAtRequest):
    result = await wechat_api.send_at(req.gid, req.wxidlist, req.nicknamelist, req.msg)
    await _broadcast_local_sent_message(req.gid, "1", req.msg)
    return result


_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "_uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)


@app.post("/api/broadcast/text")
async def broadcast_text(req: BroadcastTextRequest):
    """Broadcast text using the low-level NoSrc endpoint and patch local UI cache."""
    wxids = [w for w in req.wxids if w]
    results = []
    sent = 0
    failed = 0
    for wxid in wxids:
        try:
            result = await wechat_api.send_text_no_src(wxid, req.msg)
            ok = _send_result_ok(result)
            if ok:
                sent += 1
                await _broadcast_local_sent_message(wxid, "1", req.msg)
            else:
                failed += 1
            results.append({"wxid": wxid, "ok": ok, "result": result})
        except Exception as e:
            failed += 1
            results.append({"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return {"total": len(wxids), "sent": sent, "failed": failed, "results": results}


@app.post("/api/broadcast/image-upload")
async def broadcast_image_upload(wxids: str = Form(...), file: UploadFile = File(...)):
    """Upload one image to CDN once, then broadcast it via SendImgMsg_NoSrc."""
    try:
        target_wxids = [w for w in json.loads(wxids) if w]
    except Exception:
        target_wxids = [w.strip() for w in wxids.split(",") if w.strip()]
    if not target_wxids:
        return {"total": 0, "sent": 0, "failed": 0, "results": [], "error": "no targets"}

    ext = os.path.splitext(file.filename or "img.png")[1] or ".png"
    filename = f"broadcast_img_{int(time.time())}_{target_wxids[0]}{ext}"
    filepath = os.path.join(_UPLOAD_DIR, filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[BROADCAST] Saved image: {filepath} ({len(data)} bytes)")

    send_path = filepath
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(data))
        max_dim = 1920
        max_bytes = 500_000
        needs_compress = (
            img.mode == "RGBA"
            or ext.lower() == ".png"
            or len(data) > max_bytes
            or max(img.size) > max_dim
        )
        if needs_compress:
            rgb = img.convert("RGB") if img.mode != "RGB" else img
            w, h = rgb.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                rgb = rgb.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
            jpg_path = os.path.splitext(filepath)[0] + ".jpg"
            for quality in (85, 70, 55):
                rgb.save(jpg_path, "JPEG", quality=quality)
                if os.path.getsize(jpg_path) <= max_bytes:
                    break
            send_path = jpg_path
            _log(f"[BROADCAST] Compressed image for CDN: {send_path}")
    except Exception as e:
        _log(f"[BROADCAST] Image compression skipped: {e}")

    cdn = await wechat_api.cdn_upload_image(send_path, target_wxids[0])
    if cdn.get("error"):
        return {
            "total": len(target_wxids),
            "sent": 0,
            "failed": len(target_wxids),
            "results": [{"wxid": wxid, "ok": False, "error": cdn["error"]} for wxid in target_wxids],
            "cdn": {k: cdn.get(k) for k in _BROADCAST_IMG_CDN_KEYS},
        }

    results = []
    sent = 0
    failed = 0
    for wxid in target_wxids:
        try:
            result = await wechat_api.send_image_no_src(wxid, cdn)
            ok = _send_result_ok(result)
            if ok:
                sent += 1
                await _broadcast_local_sent_message(wxid, "3", "", {"img_path": filepath})
            else:
                failed += 1
            results.append({"wxid": wxid, "ok": ok, "result": result})
        except Exception as e:
            failed += 1
            results.append({"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"})

    return {
        "total": len(target_wxids),
        "sent": sent,
        "failed": failed,
        "results": results,
        "cdn": {k: cdn.get(k) for k in _BROADCAST_IMG_CDN_KEYS},
    }


@app.post("/api/accounts/broadcast/text")
async def multi_account_broadcast_text(req: MultiBroadcastTextRequest):
    target_wxids = [w for w in req.wxids if w]
    agent_ids = [a for a in (req.agent_ids or []) if agent_manager.is_connected(a)]
    if not agent_ids:
        agent_ids = [a["id"] for a in agent_manager.agents() if a.get("id")]
    total = len(agent_ids) * len(target_wxids)
    sent = 0
    failed = 0
    results = []

    for agent_id in agent_ids:
        async with _ACCOUNT_LOCK:
            await agent_manager.set_active(agent_id)
            _activate_runtime(agent_id)
        with wechat_api.use_agent(agent_id):
            for wxid in target_wxids:
                try:
                    result = await wechat_api.send_text_no_src(wxid, req.msg)
                    ok = _send_result_ok(result)
                    if ok:
                        sent += 1
                        async with _ACCOUNT_LOCK:
                            _activate_runtime(agent_id)
                            await _broadcast_local_sent_message(wxid, "1", req.msg)
                    else:
                        failed += 1
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result})
                except Exception as e:
                    failed += 1
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"})

    return {"accounts": len(agent_ids), "targets": len(target_wxids), "total": total, "sent": sent, "failed": failed, "results": results}


@app.post("/api/accounts/broadcast/image-upload")
async def multi_account_broadcast_image_upload(
    wxids: str = Form(...),
    agent_ids: str = Form("[]"),
    file: UploadFile = File(...),
):
    try:
        target_wxids = [w for w in json.loads(wxids) if w]
    except Exception:
        target_wxids = [w.strip() for w in wxids.split(",") if w.strip()]
    try:
        requested_agents = [a for a in json.loads(agent_ids or "[]") if a]
    except Exception:
        requested_agents = [a.strip() for a in str(agent_ids or "").split(",") if a.strip()]
    selected_agents = [a for a in requested_agents if agent_manager.is_connected(a)]
    if not selected_agents:
        selected_agents = [a["id"] for a in agent_manager.agents() if a.get("id")]
    if not target_wxids:
        return {"accounts": len(selected_agents), "targets": 0, "total": 0, "sent": 0, "failed": 0, "results": [], "error": "no targets"}

    data = await file.read()
    original_name = os.path.basename(file.filename or "multi_broadcast.png") or "multi_broadcast.png"
    ext = os.path.splitext(original_name)[1] or ".png"
    upload_name = original_name
    upload_bytes = data
    upload_mime = file.content_type or f"image/{ext.lstrip('.').lower() or 'png'}"
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(data))
        max_dim = 1920
        max_bytes = 500_000
        if img.mode == "RGBA" or ext.lower() == ".png" or len(data) > max_bytes or max(img.size) > max_dim:
            rgb = img.convert("RGB") if img.mode != "RGB" else img
            w, h = rgb.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                rgb = rgb.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
            upload_name = os.path.splitext(original_name)[0] + ".jpg"
            for quality in (85, 70, 55):
                output = io.BytesIO()
                rgb.save(output, "JPEG", quality=quality)
                upload_bytes = output.getvalue()
                upload_mime = "image/jpeg"
                if len(upload_bytes) <= max_bytes:
                    break
    except Exception as e:
        _log(f"[MULTI_BROADCAST] Image compression skipped: {e}")
    file_hex = upload_bytes.hex()

    sent = 0
    failed = 0
    results = []
    for agent_id in selected_agents:
        async with _ACCOUNT_LOCK:
            await agent_manager.set_active(agent_id)
            _activate_runtime(agent_id)
            db_image_id = sqlite_cache.put_media_blob(upload_bytes, upload_mime, upload_name)
        with wechat_api.use_agent(agent_id):
            try:
                cdn = await wechat_api.cdn_upload_image(upload_name, target_wxids[0], file_data=file_hex)
            except Exception as e:
                cdn = {"error": f"{type(e).__name__}: {e}"}
            if cdn.get("error"):
                failed += len(target_wxids)
                for wxid in target_wxids:
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": False, "error": cdn["error"]})
                continue
            for wxid in target_wxids:
                try:
                    result = await wechat_api.send_image_no_src(wxid, cdn)
                    ok = _send_result_ok(result)
                    if ok:
                        sent += 1
                        async with _ACCOUNT_LOCK:
                            _activate_runtime(agent_id)
                            await _broadcast_local_sent_message(wxid, "3", "", {"db_image_id": db_image_id})
                    else:
                        failed += 1
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result})
                except Exception as e:
                    failed += 1
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"})

    return {
        "accounts": len(selected_agents),
        "targets": len(target_wxids),
        "total": len(selected_agents) * len(target_wxids),
        "sent": sent,
        "failed": failed,
        "results": results,
    }


@app.post("/api/send/image-upload")
async def send_image_upload(wxid: str = Form(...), file: UploadFile = File(...)):
    """Upload an image from the browser and send it via WeChat."""
    ext = os.path.splitext(file.filename or "img.png")[1] or ".png"
    filename = f"img_{int(time.time())}_{wxid}{ext}"
    filepath = os.path.join(_UPLOAD_DIR, filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved image: {filepath} ({len(data)} bytes)")

    # Compress & resize all images to keep file size reasonable.
    # Large images (3-5MB+) cause the remote Hook to take minutes to download.
    _MAX_DIM = 1920   # max pixels on longest side
    _MAX_BYTES = 500_000  # target max ~500KB
    send_path = filepath
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(data))
        needs_compress = (
            img.mode == "RGBA"
            or ext.lower() == ".png"
            or len(data) > _MAX_BYTES
            or max(img.size) > _MAX_DIM
        )
        if needs_compress:
            rgb = img.convert("RGB") if img.mode != "RGB" else img
            # Resize if too large
            w, h = rgb.size
            if max(w, h) > _MAX_DIM:
                ratio = _MAX_DIM / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                rgb = rgb.resize(new_size, PILImage.LANCZOS)
                _log(f"[UPLOAD] Resized {w}x{h} → {new_size[0]}x{new_size[1]}")
            jpg_path = os.path.splitext(filepath)[0] + ".jpg"
            # Adaptive quality: start at 85, reduce if still too large
            for quality in (85, 70, 55):
                rgb.save(jpg_path, "JPEG", quality=quality)
                jpg_size = os.path.getsize(jpg_path)
                if jpg_size <= _MAX_BYTES:
                    break
            _log(f"[UPLOAD] Compressed to JPG: {jpg_path} "
                 f"({len(data)} → {jpg_size} bytes, q={quality})")
            send_path = jpg_path
    except Exception as e:
        _log(f"[UPLOAD] Image compression skipped: {e}")

    result = await wechat_api.send_image(wxid, send_path)
    await _broadcast_local_sent_message(wxid, "3", "", {"img_path": filepath})
    return result


@app.post("/api/send/file-upload")
async def send_file_upload(wxid: str = Form(...), file: UploadFile = File(...)):
    """Upload a file from the browser and send it via WeChat."""
    safe_name = (file.filename or "file").replace("\\", "_").replace("/", "_")
    filename = f"{int(time.time())}_{safe_name}"
    filepath = os.path.join(_UPLOAD_DIR, filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved file: {filepath} ({len(data)} bytes)")

    result = await wechat_api.send_file(wxid, filepath)
    await _broadcast_local_sent_message(wxid, "49", safe_name, {"file_path": filepath})
    return result


@app.post("/api/send/video-upload")
async def send_video_upload(wxid: str = Form(...), file: UploadFile = File(...)):
    """Upload a video from the browser and send it via WeChat."""
    safe_name = (file.filename or "video.mp4").replace("\\", "_").replace("/", "_")
    filename = f"{int(time.time())}_{safe_name}"
    filepath = os.path.join(_UPLOAD_DIR, filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved video: {filepath} ({len(data)} bytes)")

    result = await wechat_api.send_video(wxid, filepath)
    await _broadcast_local_sent_message(wxid, "43", "", {"video_path": filepath})
    return result


@app.post("/api/send/gif-upload")
async def send_gif_upload(wxid: str = Form(...), file: UploadFile = File(...)):
    """Upload a GIF/sticker file from the browser and send it via WeChat."""
    safe_name = (file.filename or "emoji.gif").replace("\\", "_").replace("/", "_")
    filename = f"{int(time.time())}_{safe_name}"
    filepath = os.path.join(_UPLOAD_DIR, filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved GIF: {filepath} ({len(data)} bytes)")

    result = await wechat_api.send_gif(wxid, filepath)
    await _broadcast_local_sent_message(wxid, "47", "", {"gif_path": filepath})
    return result


@app.post("/api/revoke")
async def revoke_msg(req: RevokeRequest):
    return await wechat_api.revoke_msg(req.msg_svrid, req.to_wxid)


@app.post("/api/mark-read/{wxid}")
async def mark_read(wxid: str):
    """Mark a chat as read: clear unread in store + broadcast to all frontends."""
    # Clear in our in-memory store
    message_store.mark_read(wxid)
    # Also tell the WeChat hook to clear the native unread badge
    result = {}
    try:
        result = await wechat_api.mark_as_read(wxid)
    except Exception as e:
        _log(f"[MARK_READ] hook call failed for {wxid}: {e}")
    # Broadcast to all frontends so every client clears the badge
    await manager.broadcast({
        "type": "mark_read",
        "data": {"wxid": wxid},
    })
    return result


@app.post("/api/session/sticky")
async def sticky_session(req: SessionActionRequest):
    return await wechat_api.sticky_chat(req.wxid)


@app.post("/api/session/unpin")
async def unpin_session(req: SessionActionRequest):
    return await wechat_api.unpin_chat(req.wxid)


@app.post("/api/session/mark-unread")
async def mark_session_unread(req: SessionActionRequest):
    return await wechat_api.mark_as_unread(req.wxid)


@app.post("/api/session/mute")
async def mute_session(req: SessionActionRequest):
    return await wechat_api.turn_on_do_not_disturb(req.wxid)


@app.post("/api/session/unmute")
async def unmute_session(req: SessionActionRequest):
    return await wechat_api.turn_off_do_not_disturb(req.wxid)


# ─── REST API: Media ──────────────────────────────────────────────

@app.get("/api/media/image")
async def serve_image(path: str):
    """Serve a local image file (from Hook's auto-download path)."""
    if os.path.exists(path):
        return FileResponse(path)
    return {"error": "File not found"}


@app.get("/api/media/db-image/{media_id}")
async def serve_db_image(media_id: str):
    """Serve an image blob stored in the per-account SQLite cache."""
    row = sqlite_cache.get_media_blob(media_id)
    if not row:
        return Response("not found", status_code=404)
    return Response(
        content=row["data"],
        media_type=row.get("mime_type") or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=31536000"},
    )


# ─── WeChat image file resolution ─────────────────────────────
# WeChat stores images under MsgAttach\<hash>\Image\<YYYY-MM>\<hash>.[jpg|dat]
# The .jpg files are already decoded (by Hook callback). The .dat files need /DecodePic.
# BytesExtra in the DB contains the relative paths to both Image and Thumb .dat files.
_WECHAT_FILES_BASE = os.environ.get("WECHAT_FILES_BASE") or os.path.join(
    os.environ.get("APPDATA", ""),
    "WxDirDataPath",
    config.RDV or "default",
    "WeChat Files",
)

# Cache dir for decoded .dat images
_IMG_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".img_cache")
os.makedirs(_IMG_CACHE_DIR, exist_ok=True)


def _image_file_response(path: str, msg_id: str = ""):
    if msg_id and path:
        try:
            updated = sqlite_cache.update_image_path_by_msg_id(str(msg_id), path)
            if updated:
                _log(f"[SQLITE_CACHE] image path cached for msg_id={msg_id}: {path}")
        except Exception as e:
            _log(f"[SQLITE_CACHE] image path update failed: {type(e).__name__}: {e}")
    return FileResponse(path)


# Concurrency control for /DownPic:
# - Local:  Lock (serialize to protect Hook DLL)
# - Remote: Semaphore (allow a few concurrent CDN downloads)
if config.IS_LOCAL_HOOK:
    _download_pic_lock: asyncio.Lock | asyncio.Semaphore = asyncio.Lock()
else:
    _download_pic_lock = asyncio.Semaphore(3)


def _find_local_image(raw: bytes) -> tuple[list[bytes], list[bytes], str | None]:
    """Extract BytesExtra paths and try to find a local decoded image.
    Returns (img_dat_paths, thumb_dat_paths, found_file_or_None)."""
    import re
    img_dat_paths = re.findall(
        rb'(wxid_[a-zA-Z0-9_]+\\[^\x00\x01-\x1f]{5,}?\\Image\\[^\x00\x01-\x1f]{5,}?\.dat)', raw
    )
    thumb_dat_paths = re.findall(
        rb'(wxid_[a-zA-Z0-9_]+\\[^\x00\x01-\x1f]{5,}?\\Thumb\\[^\x00\x01-\x1f]{5,}?\.dat)', raw
    )
    # Check decoded .jpg files (same basename as Image .dat, but .jpg)
    for p in img_dat_paths:
        rel = p.decode('ascii', errors='replace')
        dat_full = os.path.join(_WECHAT_FILES_BASE, rel)
        jpg_full = os.path.splitext(dat_full)[0] + ".jpg"
        if os.path.exists(jpg_full) and os.path.getsize(jpg_full) > 0:
            return img_dat_paths, thumb_dat_paths, jpg_full
    return img_dat_paths, thumb_dat_paths, None


async def _try_decode_dat(dat_paths: list[bytes], label: str) -> str | None:
    """Try to decode .dat files via /DecodePic. Returns decoded path or None."""
    for p in dat_paths:
        rel = p.decode('ascii', errors='replace')
        dat_full = os.path.join(_WECHAT_FILES_BASE, rel)
        if not os.path.exists(dat_full):
            continue
        cache_name = os.path.splitext(os.path.basename(rel))[0]
        decoded_path = os.path.join(_IMG_CACHE_DIR, f"{cache_name}.jpg")
        if os.path.exists(decoded_path) and os.path.getsize(decoded_path) > 0:
            return decoded_path
        try:
            await wechat_api.decode_pic(dat_full, decoded_path)
            for _ in range(6):
                if os.path.exists(decoded_path) and os.path.getsize(decoded_path) > 0:
                    return decoded_path
                await asyncio.sleep(0.5)
        except Exception as e:
            _log(f"[DECODE_PIC] {label} error: {e}")
    return None


def _parse_img_xml_cdn_params(msg_xml: str) -> dict:
    """Extract CDN download parameters from a type-3 image message XML.

    Returns dict with keys: decode_key, file_id, i_key, md5, originsourcemd5,
    cdnthumblength, cdnthumburl (may be empty).
    """
    import re
    result = {
        "decode_key": "", "file_id": "", "i_key": "",
        "md5": "", "originsourcemd5": "", "cdnthumblength": 0,
        "cdnthumburl": "",
    }
    if not msg_xml:
        return result
    # aeskey → decode_key
    m = re.search(r'aeskey="([^"]+)"', msg_xml)
    if m:
        result["decode_key"] = m.group(1)
    # Prefer cdnmidimgurl (full size), fallback to cdnthumburl
    m = re.search(r'cdnmidimgurl="([^"]+)"', msg_xml)
    if m and m.group(1):
        result["file_id"] = m.group(1)
    else:
        m = re.search(r'cdnthumburl="([^"]+)"', msg_xml)
        if m:
            result["file_id"] = m.group(1)
    # cdnthumburl (always save even if we prefer cdnmidimgurl)
    m = re.search(r'cdnthumburl="([^"]+)"', msg_xml)
    if m:
        result["cdnthumburl"] = m.group(1)
    # md5
    m = re.search(r'\bmd5="([^"]+)"', msg_xml)
    if m:
        result["md5"] = m.group(1)
    # originsourcemd5
    m = re.search(r'originsourcemd5="([^"]+)"', msg_xml)
    if m:
        result["originsourcemd5"] = m.group(1)
    # cdnthumblength
    m = re.search(r'cdnthumblength="(\d+)"', msg_xml)
    if m:
        result["cdnthumblength"] = int(m.group(1))
    return result


@app.post("/api/media/download-image")
async def download_image(request: Request):
    """Resolve a type-3 image.

    Local hook strategy:
      1. Check local decoded .jpg files (from BytesExtra Image paths)
      2. Decode local .dat files via /DecodePic (Image then Thumb)
      3. Call /DownPic to trigger WeChat CDN download, then re-check local paths

    Remote hook strategy:
      1. Check callback cache (base64 images saved from callbacks)
      2. Call /download → image arrives via callback as base64
      3. Wait for the callback to deliver the image
    """
    import hashlib
    body = await request.json()
    bytes_extra_hex = body.get("bytes_extra_hex", "")
    msg_xml = body.get("msg_xml", "")
    msg_id = body.get("msg_id", "")  # MsgSvrID for matching callback

    raw = b""
    if bytes_extra_hex:
        try:
            raw = bytes.fromhex(bytes_extra_hex)
        except Exception:
            pass

    # ─── Check callback cache first (works for both local & remote) ──
    if msg_id:
        cb_dir = os.path.join(_IMG_CACHE_DIR, "callback")
        safe_id = "".join(c for c in str(msg_id) if c.isalnum() or c in ("_", "-", "."))[:80]
        for ext in ("jpg", "png", "gif", "webp"):
            cached = os.path.join(cb_dir, f"{safe_id}.{ext}")
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                return _image_file_response(cached, msg_id)

    # ═══ Remote Hook: CDN protocol download ════════════════════════
    if not config.IS_LOCAL_HOOK:
        cdn_params = _parse_img_xml_cdn_params(msg_xml)
        has_cdn_params = bool(cdn_params["decode_key"] and cdn_params["file_id"])

        if has_cdn_params:
            xml_hash = hashlib.md5((msg_xml or msg_id or "").encode("utf-8", errors="replace")).hexdigest()
            cache_path = os.path.join(_IMG_CACHE_DIR, f"{xml_hash}.jpg")

            # Already downloaded before?
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                return _image_file_response(cache_path, msg_id)

            # Also check callback cache by msgsvrid
            if msg_id:
                cb_dir = os.path.join(_IMG_CACHE_DIR, "callback")
                safe_id = "".join(c for c in str(msg_id) if c.isalnum() or c in ("_", "-", "."))[:80]
                for ext in ("jpg", "png", "gif", "webp"):
                    cached = os.path.join(cb_dir, f"{safe_id}.{ext}")
                    if os.path.exists(cached) and os.path.getsize(cached) > 0:
                        return _image_file_response(cached, msg_id)

            # ── De-dup: if another request is already downloading this image, just wait ──
            async with _inflight_cdn_lock:
                existing = _inflight_cdn_downloads.get(xml_hash)
                if existing and not existing.done():
                    _log(f"[IMG_DL] Joining inflight download for {xml_hash[:12]}...")
                    shared_fut = existing
                else:
                    shared_fut = None

            if shared_fut:
                try:
                    file_path = await asyncio.wait_for(asyncio.shield(shared_fut), timeout=70.0)
                    return _image_file_response(file_path, msg_id)
                except (asyncio.TimeoutError, Exception):
                    pass
                # Re-check cache after wait
                if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                    return _image_file_response(cache_path, msg_id)

            # ── Start new download (serialized: one CDN download at a time) ──
            loop = asyncio.get_event_loop()
            inflight_fut: asyncio.Future = loop.create_future()
            async with _inflight_cdn_lock:
                _inflight_cdn_downloads[xml_hash] = inflight_fut

            # Register pending download so callback handler can fulfill it.
            # BOTH keys share the SAME Future so either match resolves the wait.
            pending_key = f"cdn:{xml_hash}"
            fut = await _register_cdn_pending(pending_key)
            if msg_id:
                await _register_cdn_pending(f"msgsvrid:{msg_id}", existing_fut=fut)

            try:
                # Acquire the CDN semaphore — only 1 CDN download at a time
                # to avoid overloading the Hook server
                async with _cdn_download_sem:
                    _log(f"[IMG_DL] CDN /download: decode_key={cdn_params['decode_key'][:16]}... "
                         f"file_id={cdn_params['file_id'][:32]}... msg_id={msg_id}")
                    try:
                        cdn_result = await wechat_api.cdn_download_pic(
                            decode_key=cdn_params["decode_key"],
                            file_id=cdn_params["file_id"],
                            img_filename="down.jpg",
                        )
                        _log(f"[IMG_DL] CDN response: {cdn_result}")
                    except Exception as e:
                        _log(f"[IMG_DL] CDN /download error: {e}")

                # Wait for callback to deliver the base64 image (up to 60s)
                _log(f"[IMG_DL] Waiting for callback (60s)...")
                try:
                    file_path = await asyncio.wait_for(fut, timeout=60.0)
                    _log(f"[IMG_DL] ✓ Image received via callback: {file_path}")
                    if not inflight_fut.done():
                        inflight_fut.set_result(file_path)
                    return _image_file_response(file_path, msg_id)
                except asyncio.TimeoutError:
                    _log(f"[IMG_DL] ✗ Timed out waiting for callback (60s)")

            finally:
                # Cleanup pending keys and inflight
                await _cleanup_cdn_pending(pending_key)
                if msg_id:
                    await _cleanup_cdn_pending(f"msgsvrid:{msg_id}")
                async with _inflight_cdn_lock:
                    _inflight_cdn_downloads.pop(xml_hash, None)
                if not inflight_fut.done():
                    inflight_fut.set_exception(asyncio.TimeoutError("CDN callback timed out"))

            # Last resort: check callback cache (callback may have arrived late)
            if msg_id:
                cb_dir2 = os.path.join(_IMG_CACHE_DIR, "callback")
                safe_id2 = "".join(c for c in str(msg_id) if c.isalnum() or c in ("_", "-", "."))[:80]
                for ext in ("jpg", "png", "gif", "webp"):
                    cached = os.path.join(cb_dir2, f"{safe_id2}.{ext}")
                    if os.path.exists(cached) and os.path.getsize(cached) > 0:
                        return _image_file_response(cached, msg_id)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                return _image_file_response(cache_path, msg_id)

            return {"error": "CDN download timed out — image may still arrive via callback, retry later"}

        # Fallback: if no CDN params, return error
        if not raw:
            return {"error": "No CDN params in image XML for remote download"}

    # ═══ Local Hook: original strategy ═══════════════════════════════
    img_dat_paths: list[bytes] = []
    thumb_dat_paths: list[bytes] = []

    # ─── Phase 1: Check local decoded .jpg ────────────────────────
    if raw:
        img_dat_paths, thumb_dat_paths, found = _find_local_image(raw)
        if found:
            return _image_file_response(found, msg_id)

    # ─── Phase 2: Decode local .dat files ─────────────────────────
    if img_dat_paths:
        result = await _try_decode_dat(img_dat_paths, "Image")
        if result:
            return _image_file_response(result, msg_id)
    if thumb_dat_paths:
        result = await _try_decode_dat(thumb_dat_paths, "Thumb")
        if result:
            return _image_file_response(result, msg_id)

    # ─── Phase 3: /DownPic → trigger download → re-check local ───
    if msg_xml and ("<img" in msg_xml or "<msg>" in msg_xml):
        xml_hash = hashlib.md5(msg_xml.encode("utf-8", errors="replace")).hexdigest()
        cache_path = os.path.join(_IMG_CACHE_DIR, f"{xml_hash}.jpg")

        # Already downloaded before?
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            return _image_file_response(cache_path, msg_id)

        # Serialize DownPic calls
        async with _download_pic_lock:
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                return _image_file_response(cache_path, msg_id)
            try:
                result = await wechat_api.download_pic(msg_xml, cache_path)
                _log(f"[DOWNLOAD_PIC] result: {result}")
            except Exception as e:
                _log(f"[DOWNLOAD_PIC] error: {e}")
                return {"error": str(e)}

            # Poll: DownPic downloads to WeChat's cache asynchronously.
            # Check our topath, WeChat's .jpg paths, AND try decoding new .dat files.
            for i in range(20):  # ~10s total
                # Check our specified topath
                if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                    _log(f"[DOWNLOAD_PIC] Found at topath after {i*0.5:.1f}s")
                    return _image_file_response(cache_path, msg_id)
                # Re-check WeChat local .jpg paths
                if raw:
                    _, _, found = _find_local_image(raw)
                    if found:
                        _log(f"[DOWNLOAD_PIC] Found local .jpg after {i*0.5:.1f}s")
                        return _image_file_response(found, msg_id)
                # Every 2s, also try decoding newly-appeared .dat files
                if i > 0 and i % 4 == 0:
                    if img_dat_paths:
                        decoded = await _try_decode_dat(img_dat_paths, "Image-poll")
                        if decoded:
                            _log(f"[DOWNLOAD_PIC] Decoded .dat after {i*0.5:.1f}s")
                            return _image_file_response(decoded, msg_id)
                    if thumb_dat_paths:
                        decoded = await _try_decode_dat(thumb_dat_paths, "Thumb-poll")
                        if decoded:
                            _log(f"[DOWNLOAD_PIC] Decoded thumb after {i*0.5:.1f}s")
                            return _image_file_response(decoded, msg_id)
                await asyncio.sleep(0.5)

            _log(f"[DOWNLOAD_PIC] Timed out for xml_hash={xml_hash}")

        return {"error": "Download timed out"}

    return {"error": "No image data provided"}


@app.post("/api/media/voice2text")
async def voice2text(request: Request):
    body = await request.json()
    return await wechat_api.voice_to_text(body.get("voice_hex", ""))


@app.post("/api/media/gif-url")
async def gif_url(request: Request):
    body = await request.json()
    return await wechat_api.get_gif_url(body.get("msg_xml", ""))


# ─── Sticker / Emoji serving ─────────────────────────────────────
_STICKER_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".sticker_cache")
os.makedirs(_STICKER_CACHE_DIR, exist_ok=True)


@app.get("/api/media/sticker/{md5}")
async def serve_sticker(md5: str, cdnurl: str = "", thumburl: str = ""):
    """Serve a sticker image by its MD5 hash.

    Lookup order:
      1. Local file cache (.sticker_cache/)
      2. Emotion.db  → EmotionItem.Data   BLOB  (subscribed pack stickers)
      3. Emotion.db  → CustomEmotion.Data  BLOB  (user-added / favourited stickers)
      4. CDN download from cdnurl (for sticker packs not yet downloaded locally)
      5. CDN download from thumburl (thumbnail fallback)
    """
    import re
    safe_md5 = re.sub(r"[^a-fA-F0-9]", "", md5).upper()
    if not safe_md5:
        return {"error": "invalid md5"}

    # 1. Check file cache
    for ext in ("gif", "png", "jpg", "webp"):
        cached = os.path.join(_STICKER_CACHE_DIR, f"{safe_md5}.{ext}")
        if os.path.exists(cached) and os.path.getsize(cached) > 0:
            return FileResponse(cached, media_type=f"image/{ext}")

    # Helper: save a BLOB from DB and return a FileResponse (or None)
    def _save_and_serve(hex_str: str, source: str):
        if not hex_str:
            return None
        blob = bytes.fromhex(hex_str)
        if len(blob) < 10:
            return None
        ext = _detect_image_ext(blob)
        cache_path = os.path.join(_STICKER_CACHE_DIR, f"{safe_md5}.{ext}")
        with open(cache_path, "wb") as f:
            f.write(blob)
        _log(f"[STICKER] Cached from {source}: {safe_md5}.{ext} ({len(blob)}b)")
        return FileResponse(cache_path, media_type=f"image/{ext}")

    # 2. Query EmotionItem (subscribed sticker packs)
    for md5_variant in (safe_md5, safe_md5.lower()):
        try:
            data = await wechat_api.query_db(
                "Emotion.db",
                f"SELECT hex(Data) as DataHex FROM EmotionItem WHERE MD5='{md5_variant}'"
            )
            rows = data.get("data", []) if isinstance(data, dict) else []
            if rows:
                row = rows[0]
                hex_str = row.get("DataHex", "") if isinstance(row, dict) else (row[0] if isinstance(row, list) else "")
                result = _save_and_serve(hex_str, "EmotionItem")
                if result:
                    return result
        except Exception as e:
            _log(f"[STICKER] EmotionItem query error ({md5_variant}): {e}")

    # 3. Query CustomEmotion (user-added / favourited stickers)
    #    Try both Data and Thumbnail columns; try both MD5 cases
    for col in ("Data", "Thumbnail"):
        for md5_variant in (safe_md5, safe_md5.lower()):
            try:
                data = await wechat_api.query_db(
                    "Emotion.db",
                    f"SELECT hex({col}) as DataHex FROM CustomEmotion WHERE MD5='{md5_variant}'"
                )
                rows = data.get("data", []) if isinstance(data, dict) else []
                if rows:
                    row = rows[0]
                    hex_str = row.get("DataHex", "") if isinstance(row, dict) else (row[0] if isinstance(row, list) else "")
                    result = _save_and_serve(hex_str, f"CustomEmotion.{col}")
                    if result:
                        return result
            except Exception:
                pass  # Column may not exist, that's fine

    # 4. Download from CDN (for sticker packs not yet downloaded locally)
    for url_label, url in [("cdnurl", cdnurl), ("thumburl", thumburl)]:
        if not url or not url.startswith("http"):
            continue
        try:
            _log(f"[STICKER] Downloading from {url_label}: {url[:120]}")
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cdn_client:
                resp = await cdn_client.get(url)
            if resp.status_code == 200 and len(resp.content) > 100:
                blob = resp.content
                ext = _detect_image_ext(blob)
                cache_path = os.path.join(_STICKER_CACHE_DIR, f"{safe_md5}.{ext}")
                with open(cache_path, "wb") as f:
                    f.write(blob)
                _log(f"[STICKER] Cached from {url_label}: {safe_md5}.{ext} ({len(blob)}b)")
                return FileResponse(cache_path, media_type=f"image/{ext}")
            else:
                _log(f"[STICKER] {url_label} download failed: status={resp.status_code} len={len(resp.content)}")
        except Exception as e:
            _log(f"[STICKER] {url_label} download error: {e}")

    _log(f"[STICKER] Not found: {safe_md5}")
    return {"error": "sticker not found"}


def _detect_image_ext(data: bytes) -> str:
    """Detect image format from magic bytes."""
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "gif"  # default for WeChat stickers


# ─── REST API: Group ──────────────────────────────────────────────

@app.get("/api/group/{gid}")
async def get_group_detail(gid: str):
    return await wechat_api.get_chatroom_detail(gid)


@app.get("/api/group/{gid}/members")
async def get_group_members(gid: str):
    return await wechat_api.get_chatroom_members(gid)


@app.get("/api/group/{gid}/member/{wxid}/nickname")
async def get_member_nickname(gid: str, wxid: str):
    return await wechat_api.get_chatroom_member_nickname(gid, wxid)


_AVATAR_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".avatar_cache")
os.makedirs(_AVATAR_CACHE_DIR, exist_ok=True)


@app.get("/api/avatar/{wxid}")
async def serve_avatar(wxid: str):
    """Serve a cached avatar image for a wxid.
    If not cached yet, fetches via GetHeadIMG and caches to disk."""
    safe_name = "".join(c for c in wxid if c.isalnum() or c in "_-") + ".jpg"
    cache_path = os.path.join(_AVATAR_CACHE_DIR, safe_name)

    # Serve from cache
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return FileResponse(cache_path, media_type="image/jpeg")

    # Fetch from Hook
    try:
        img_bytes = await wechat_api.get_avatar_bytes(wxid)
        if img_bytes and len(img_bytes) > 100:
            with open(cache_path, "wb") as f:
                f.write(img_bytes)
            return FileResponse(cache_path, media_type="image/jpeg")
    except Exception as e:
        _log(f"[AVATAR] fetch failed for {wxid}: {e}")

    return {"error": "avatar not found"}


# ─── Group member names cache (avoids 3s+ API calls on re-entry) ──
_GROUP_NAMES_CACHE: dict[str, tuple[dict, float]] = {}  # gid -> ({wxid: name}, timestamp)
_GROUP_NAMES_CACHE_TTL = 300  # 5 minutes


@app.get("/api/group/{gid}/member-names")
async def get_group_member_names(gid: str):
    """Fast endpoint: return {wxid: name} for all group members.
    Uses only GetFriendOrChatroomDetailInfo (single call, no BatchGetContactBriefInfo).
    Designed to be called on group enter for nickname resolution.
    Results are cached for 5 minutes to avoid repeated 3s+ API calls."""
    now = time.time()
    cached = _GROUP_NAMES_CACHE.get(gid)
    if cached and (now - cached[1]) < _GROUP_NAMES_CACHE_TTL:
        _log(f"[GROUP_NAMES] {gid}: cache hit ({len(cached[0])} members)")
        return {"names": cached[0]}

    detail = await wechat_api.get_friend_detail_info(gid)
    result: dict[str, str] = {}  # wxid -> name
    missing_wxids: list[str] = []  # 本地没昵称的成员

    if isinstance(detail, dict):
        members = detail.get("member", [])
        for m in members:
            wxid = m.get("wxid", "") if isinstance(m, dict) else ""
            nickname = m.get("nickname", "") if isinstance(m, dict) else ""
            if wxid and nickname:
                result[wxid] = nickname
            elif wxid:
                missing_wxids.append(wxid)

    # 对本地没昵称的成员，fallback 到 GetChatroomMemberDetailInfo
    if missing_wxids:
        _log(f"[GROUP_NAMES] {gid}: {len(missing_wxids)} members missing nickname, trying GetChatroomMemberDetailInfo...")
        for wxid in missing_wxids[:50]:  # 限制最多补查 50 个
            try:
                detail_info = await wechat_api.get_chatroom_member_detail_info(gid, wxid)
                # 优先 markname，其次 nickname
                name = (detail_info.get("markname", "")
                        or detail_info.get("nickname", "")
                        or detail_info.get("data", {}).get("markname", "")
                        or detail_info.get("data", {}).get("nickname", ""))
                if name:
                    result[wxid] = name
            except Exception as e:
                _log(f"[GROUP_NAMES] fallback failed for {wxid}: {e}")

    if result:
        _GROUP_NAMES_CACHE[gid] = (result, now)
    _log(f"[GROUP_NAMES] {gid}: resolved {len(result)} member names")
    return {"names": result}


@app.get("/api/group/{gid}/member-details")
async def get_group_member_details(gid: str):
    """Fetch names + avatar URLs for all members of a group.
    Step 1: GetFriendOrChatroomDetailInfo → member wxids + nicknames.
    Step 2: BatchGetContactBriefInfo → avatar URLs.
    Step 3: For members still missing avatars, provide /api/avatar/{wxid} URL."""
    # 1. Get member list with nicknames via GetFriendOrChatroomDetailInfo
    detail = await wechat_api.get_friend_detail_info(gid)
    result: dict[str, dict] = {}  # wxid -> {name, avatar}
    member_wxids: list[str] = []

    if isinstance(detail, dict):
        members = detail.get("member", [])
        for m in members:
            wxid = m.get("wxid", "") if isinstance(m, dict) else ""
            nickname = m.get("nickname", "") if isinstance(m, dict) else ""
            if wxid:
                member_wxids.append(wxid)
                result[wxid] = {"name": nickname, "avatar": ""}

    if not member_wxids:
        _log(f"[GROUP_DETAILS] {gid}: no members found in detail response")
        return {"members": {}}

    _log(f"[GROUP_DETAILS] {gid}: got {len(member_wxids)} members from detail, fetching avatars...")

    # 2. Batch fetch avatar URLs via BatchGetContactBriefInfo — max 100 per call
    batch_size = 100
    avatars_found = 0
    for i in range(0, len(member_wxids), batch_size):
        batch = member_wxids[i:i + batch_size]
        try:
            wxid_str = ",".join(batch)
            data = await wechat_api.batch_get_contact_brief_info(wxid_str)
            for info in data.get("info", []):
                wxid = info.get("wxid", "")
                if not wxid or wxid not in result:
                    continue
                url = info.get("smallhead", "") or info.get("bighead", "")
                if url:
                    result[wxid]["avatar"] = url
                    avatars_found += 1
                # Also fill in name from brief info if detail didn't provide one
                if not result[wxid]["name"]:
                    name = info.get("nickname", "") or info.get("nick", "") or info.get("markname", "")
                    if name:
                        result[wxid]["name"] = name
        except Exception as e:
            _log(f"[GROUP_DETAILS] batch brief info failed: {e}")
        await asyncio.sleep(0.05)

    # 3. For members still missing an avatar, provide the /api/avatar/{wxid} proxy URL
    missing_avatar_count = 0
    for wxid, entry in result.items():
        if not entry["avatar"]:
            entry["avatar"] = f"/api/avatar/{wxid}"
            missing_avatar_count += 1

    names_resolved = sum(1 for e in result.values() if e["name"])
    _log(f"[GROUP_DETAILS] {gid}: {len(member_wxids)} members, "
         f"{names_resolved} names, {avatars_found} avatar URLs, "
         f"{missing_avatar_count} using /api/avatar proxy")
    return {"members": result}


# ─── Entry Point ──────────────────────────────────────────────────

def _run_callback_server():
    """Run a lightweight callback-only server on CALLBACK_PORT.

    Only started when callback_port differs from server_port,
    so the remote Hook server can POST to a separately forwarded port.
    """
    import uvicorn
    from fastapi import FastAPI as _FastAPI
    from starlette.requests import Request as _Req

    cb_app = _FastAPI()

    # We must NOT call wechat_callback() directly here because this server
    # runs in a separate thread with its own event loop.  The main app's
    # asyncio primitives (httpx client, Locks, Semaphores) are bound to the
    # main event loop and will raise "bound to a different event loop" errors.
    # Instead, proxy the raw HTTP request to the main server via loopback.
    import httpx as _httpx
    _proxy_client = _httpx.AsyncClient(timeout=60.0)

    @cb_app.post("/api/callback")
    async def _cb_proxy(request: _Req):
        """Forward the callback to the main app via HTTP (loopback).

        Retries with backoff because the callback server may start before
        the main uvicorn server is ready to accept connections.
        """
        import asyncio as _aio
        body = await request.body()
        ct = request.headers.get("content-type", "application/json")
        _url = f"http://127.0.0.1:{config.SERVER_PORT}/api/callback"
        last_err = None
        for attempt in range(6):  # up to ~15s of retries
            try:
                resp = await _proxy_client.post(
                    _url, content=body, headers={"content-type": ct},
                )
                from starlette.responses import Response as _Resp
                return _Resp(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type="application/json",
                )
            except Exception as e:
                last_err = e
                if attempt < 5:
                    delay = min(1.0 * (attempt + 1), 5.0)  # 1,2,3,4,5s
                    _log(f"[CALLBACK_SERVER] proxy retry {attempt+1}/5 in {delay}s "
                         f"({type(e).__name__})")
                    await _aio.sleep(delay)
        _log(f"[CALLBACK_SERVER] proxy failed after retries: "
             f"{type(last_err).__name__}: {last_err}")
        return {"error": str(last_err)}

    if config.CALLBACK_PATH != "/api/callback":
        cb_app.add_api_route(
            config.CALLBACK_PATH,
            _cb_proxy,
            methods=["POST"],
            include_in_schema=False,
        )

    @cb_app.get("/api/media/image")
    async def _cb_serve_image(path: str):
        """Serve uploaded images so the remote Hook can download them."""
        from starlette.responses import FileResponse as _FR
        if os.path.exists(path):
            return _FR(path)
        return {"error": "File not found"}

    # Serve _uploads/ as static files so the remote Hook can fetch images
    # via a clean URL like /uploads/img_xxx.jpg
    from starlette.staticfiles import StaticFiles as _SF
    cb_app.mount("/uploads", _SF(directory=_UPLOAD_DIR), name="uploads")

    @cb_app.get("/")
    async def _health():
        return {"status": "callback_server_ok", "port": config.CALLBACK_PORT}

    _log(f"[CALLBACK_SERVER] Starting on 0.0.0.0:{config.CALLBACK_PORT} ...")
    uvicorn.run(
        cb_app,
        host="0.0.0.0",
        port=config.CALLBACK_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    import uvicorn
    import threading
    os.environ["PYTHONUNBUFFERED"] = "1"

    # If callback_port differs from server_port, start a separate listener
    # so the remote Hook can reach us on the forwarded port.
    if config.CALLBACK_PORT != config.SERVER_PORT:
        _log(f"[STARTUP] Callback port ({config.CALLBACK_PORT}) != server port ({config.SERVER_PORT})")
        _log(f"[STARTUP] Starting separate callback listener on 0.0.0.0:{config.CALLBACK_PORT}")
        cb_thread = threading.Thread(target=_run_callback_server, daemon=True)
        cb_thread.start()

    # NOTE:
    # - In dev, uvicorn reload can be useful.
    # - But writing logs (e.g. backend/main.log) will trigger reload loops unless excluded.
    # Default: reload OFF. Enable by setting WECHAT_RELOAD=1.
    reload_enabled = os.environ.get("WECHAT_RELOAD", "0") == "1"
    uvicorn.run(
        "main:app",
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        reload=reload_enabled,
        # Avoid reload loops caused by log/caches being written continuously.
        reload_excludes=[
            "*.log",
            "**/*.log",
            "backend/*.log",
            "backend/main.log",
            "backend/_uploads/*",
            ".img_cache/*",
            ".avatar_cache/*",
            ".sticker_cache/*",
        ] if reload_enabled else None,
    )
