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
from typing import Any

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


_RUN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".run")
_CALLBACK_SAMPLE_PATH = os.path.join(_RUN_DIR, "callback_samples.jsonl")
_CALLBACK_SAMPLE_ENABLED = os.environ.get("WECHAT_CALLBACK_SAMPLE_LOG", "0") == "1"


def _scrub_callback_payload(value, max_string: int = 500):
    """Return a log-friendly callback sample without huge binary fields."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"img_base64", "pb_msg", "voice_hex", "voice_data", "filedata"}:
                out[key] = f"<omitted len={len(str(item or ''))}>"
            else:
                out[key] = _scrub_callback_payload(item, max_string=max_string)
        return out
    if isinstance(value, list):
        return [_scrub_callback_payload(item, max_string=max_string) for item in value[:10]]
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string] + f"...(truncated,total_len={len(value)})"
    return value


def _log_callback_sample(data: dict) -> None:
    if not _CALLBACK_SAMPLE_ENABLED:
        return
    try:
        os.makedirs(_RUN_DIR, exist_ok=True)
        sample = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "top_keys": list(data.keys()) if isinstance(data, dict) else [],
            "sample": _scrub_callback_payload(data),
        }
        with open(_CALLBACK_SAMPLE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
    except Exception as e:
        _log(f"[CALLBACK_SAMPLE] write failed: {type(e).__name__}: {e}")


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


def _detect_callback_file_ext(data: bytes, default_ext: str = "bin") -> str:
    if not data:
        return default_ext
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20") and b"ftyp" in data[:16]:
        return "mp4"
    return default_ext


def _save_callback_base64_to_cache(data_b64: str, msg_id: str, media_kind: str, default_ext: str) -> tuple[str | None, int]:
    """Decode callback base64 and save to backend cache."""
    if not data_b64:
        return None, 0
    s = str(data_b64).strip()
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
    if len(blob) > 100 * 1024 * 1024:
        _log(f"[CALLBACK_BASE64] Skip too-large {media_kind}: {len(blob)} bytes")
        return None, len(blob)

    cb_dir = os.path.join(_IMG_CACHE_DIR, "callback", media_kind)
    os.makedirs(cb_dir, exist_ok=True)
    safe_id = "".join(c for c in (msg_id or "") if c.isalnum() or c in ("_", "-", "."))[:80] or f"cb_{int(time.time())}"
    ext = _detect_callback_file_ext(blob, default_ext)
    filepath = os.path.join(cb_dir, f"{safe_id}.{ext}")
    try:
        with open(filepath, "wb") as f:
            f.write(blob)
        return filepath, len(blob)
    except Exception as e:
        _log(f"[CALLBACK_BASE64] Save failed: {type(e).__name__}: {e}")
        return None, len(blob)


def _save_img_base64_to_cache(img_b64: str, msg_id: str) -> tuple[str | None, int]:
    """Decode img_base64 and save to backend cache. Returns (filepath_or_None, byte_len)."""
    return _save_callback_base64_to_cache(img_b64, msg_id, "image", "jpg")


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
    "contacts_loaded": False,
    "session_list_loaded": False,
}

message_store = MessageStore()
sqlite_cache = SqliteMessageCache()
_active_agent_id = ""
_account_runtimes: dict[str, dict] = {}
_self_wxid_to_agent_id: dict[str, str] = {}
_ACCOUNT_LOCK = asyncio.Lock()
_ACCOUNT_CARD_REFRESH_INTERVAL_SEC = 1.0
_account_card_refresh_at: dict[str, float] = {}
_account_card_refreshing: set[str] = set()
_initializing_agents: set[str] = set()
_agent_login_status_seen: dict[str, str] = {}
_CONTACT_INIT_LOCKS: dict[str, asyncio.Lock] = {}
_CONTACT_HYDRATING_OWNERS: set[str] = set()
_CONTACT_HYDRATION_PROGRESS: dict[str, dict] = {}


def _new_app_state() -> dict:
    return {
        "self_info": None,
        "contacts": None,
        "sessions": None,
        "last_messages": {},
        "avatar_urls": {},
        "initialized": False,
        "contacts_loaded": False,
        "session_list_loaded": False,
    }


def _safe_runtime_id(agent_id: str) -> str:
    safe = "".join(c for c in str(agent_id or "default") if c.isalnum() or c in ("_", "-", "."))
    return safe[:80] or "default"


def _runtime_for(agent_id: str) -> dict:
    key = str(agent_id or "default")
    if key not in _account_runtimes:
        _account_runtimes[key] = {
            "app_state": _new_app_state(),
            "message_store": MessageStore(),
            "sqlite_cache": sqlite_cache,
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


def _self_identity_from_response(data: dict, *, agent_id: str = "", current_wxid: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    source = nested or data
    profile = dict(source)
    wxid = str(
        source.get("wxid")
        or source.get("Wxid")
        or source.get("selfwxid")
        or source.get("selfWxid")
        or source.get("self_wxid")
        or data.get("wxid")
        or data.get("selfwxid")
        or current_wxid
        or ""
    ).strip()
    nickname = str(
        source.get("nickname")
        or source.get("NickName")
        or source.get("name")
        or data.get("nickname")
        or data.get("NickName")
        or ""
    ).strip()
    if nickname in {agent_id, wxid}:
        nickname = ""
    avatar = str(
        source.get("head_big")
        or source.get("headimgurl")
        or source.get("head_img")
        or source.get("head_small")
        or data.get("head_big")
        or data.get("headimgurl")
        or data.get("head_img")
        or ""
    ).strip()
    account = str(
        source.get("account")
        or source.get("alias")
        or source.get("Alias")
        or source.get("wechat_account")
        or source.get("userName")
        or ""
    ).strip()
    phone = str(
        source.get("tel")
        or source.get("Tel")
        or source.get("phone")
        or source.get("Phone")
        or source.get("mobile")
        or source.get("Mobile")
        or ""
    ).strip()
    country = str(source.get("country") or source.get("Country") or "").strip()
    province = str(source.get("province") or source.get("Province") or "").strip()
    city = str(source.get("city") or source.get("City") or "").strip()
    display_country = country if country and country.upper() != "CN" else ""
    region = " ".join(part for part in [display_country, province, city] if part).strip()
    signature = str(
        source.get("diy_sign")
        or source.get("signature")
        or source.get("Signature")
        or source.get("sign")
        or ""
    ).strip()
    return {
        "wxid": wxid,
        "nickname": nickname,
        "avatar": avatar,
        "account": account,
        "phone": phone,
        "region": region,
        "signature": signature,
        "profile": profile,
    }


def _login_status_from_response(data: dict) -> dict[str, str]:
    if not isinstance(data, dict):
        data = {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    source = nested or data
    return {
        "status": str(source.get("onlinestatus") or source.get("onlineStatus") or source.get("status") or "").strip(),
        "message": str(source.get("msg") or source.get("message") or source.get("retmsg") or "").strip(),
        "wxid": str(source.get("selfwxid") or source.get("selfWxid") or source.get("wxid") or "").strip(),
        "nickname": str(source.get("nickname") or source.get("NickName") or "").strip(),
    }


async def _refresh_agent_login_status(agent_id: str) -> dict[str, str]:
    """Poll /IsLoginStatus and update account-card metadata.

    Status 3 is the only state that may continue into expensive initialization.
    Status 5 means WeChat is at the "enter WeChat" screen; then GetSelfLoginInfo
    is safe and needed for showing avatar/name/wxid on the account card.
    """
    agent_id = str(agent_id or "").strip()
    if config.AGENT_WS_ENABLED and (not agent_id or not agent_manager.is_connected(agent_id)):
        return {"status": "", "message": "agent not connected", "wxid": "", "nickname": "", "avatar": ""}

    raw_status = await wechat_api.is_login_status()
    parsed = _login_status_from_response(raw_status)
    status = parsed["status"]
    message = parsed["message"]
    wxid = parsed["wxid"]
    nickname = parsed["nickname"]
    agent = agent_manager.get_agent(agent_id) or {}
    previous_status = str(agent.get("login_status") or "").strip()
    current_avatar = str(agent.get("avatar") or "").strip()
    avatar = current_avatar
    current_phone = str(agent.get("phone") or "").strip()
    current_region = str(agent.get("region") or "").strip()
    current_signature = str(agent.get("signature") or "").strip()
    current_account = str(agent.get("wechat_account") or "").strip()
    phone = current_phone
    region = current_region
    signature = current_signature
    wechat_account = current_account
    profile: dict[str, Any] = {}

    current_wxid = str(agent.get("wxid") or agent.get("account_id") or wxid or "")
    current_nickname = str(agent.get("nickname") or agent.get("name") or "").strip()

    needs_self_profile = status == "5" or (
        status == "3" and (
            not wxid
            or not nickname
            or not current_avatar
            or not current_phone
            or not current_region
            or not current_signature
            or not current_account
        )
    )
    if needs_self_profile:
        try:
            self_info = await wechat_api.get_self_info()
            identity = _self_identity_from_response(self_info, agent_id=agent_id, current_wxid=current_wxid)
            wxid = identity["wxid"] or wxid
            nickname = identity["nickname"] or nickname or current_nickname
            avatar = identity["avatar"] or current_avatar
            phone = identity["phone"] or current_phone
            region = identity["region"] or current_region
            signature = identity["signature"] or current_signature
            wechat_account = identity["account"] or current_account
            profile = identity.get("profile") or {}
        except Exception as e:
            _log(f"[LOGIN_STATUS] GetSelfLoginInfo failed agent={agent_id}: {type(e).__name__}: {e}")

    if wxid:
        _self_wxid_to_agent_id[wxid] = agent_id
    if nickname in {agent_id, wxid}:
        nickname = ""

    initialized = None if status == "3" else False
    if status == "3" and previous_status != "3":
        runtime = _runtime_for(agent_id)
        runtime_state = runtime["app_state"]
        runtime_state["session_list_loaded"] = False
        runtime_state["sessions"] = None
        runtime_state["last_messages"] = {}
        _log(f"[LOGIN_STATUS] agent={agent_id} entered login status 3; next chat entry will query native Session table")
    if status != "3":
        runtime = _runtime_for(agent_id)
        runtime["app_state"]["initialized"] = False
        runtime["app_state"]["session_list_loaded"] = False

    await agent_manager.update_account(
        agent_id,
        wxid=wxid,
        nickname=nickname,
        avatar=avatar,
        phone=phone,
        region=region,
        signature=signature,
        wechat_account=wechat_account,
        profile=profile,
        login_status=status,
        login_message=message,
        initialized=initialized,
    )

    status_key = f"{status}:{message}"
    if _agent_login_status_seen.get(agent_id) != status_key:
        _agent_login_status_seen[agent_id] = status_key
        _log(f"[LOGIN_STATUS] agent={agent_id} onlinestatus={status or '?'} msg={message or '-'} wxid={wxid or '-'}")

    return {"status": status, "message": message, "wxid": wxid, "nickname": nickname, "avatar": avatar}


def _put_self_info_field(key: str, value: str) -> None:
    value = str(value or "").strip()
    if not value:
        return
    if not isinstance(app_state.get("self_info"), dict):
        app_state["self_info"] = {"data": {}}
    self_info = app_state["self_info"]
    data = self_info.get("data")
    if isinstance(data, dict):
        if not data.get(key):
            data[key] = value
    if not self_info.get(key):
        self_info[key] = value


def _brief_entry_from_response(data: dict, wxid: str) -> dict:
    if not isinstance(data, dict) or not wxid:
        return {}

    members = data.get("members")
    if isinstance(members, dict):
        entry = members.get(wxid)
        if isinstance(entry, dict):
            return {
                "name": str(entry.get("name") or entry.get("nickname") or ""),
                "avatar": str(entry.get("avatar") or ""),
            }

    info_list = data.get("info") or data.get("data") or data.get("list") or []
    if isinstance(info_list, dict):
        info_list = info_list.get("info") or info_list.get("list") or []
    if not isinstance(info_list, list):
        return {}

    for info in info_list:
        if not isinstance(info, dict):
            continue
        item_wxid = str(info.get("wxid") or info.get("UserName") or info.get("userName") or "").strip()
        if item_wxid != wxid:
            continue
        return {
            "name": str(
                info.get("markname")
                or info.get("nickname")
                or info.get("NickName")
                or info.get("nick")
                or info.get("Remark")
                or ""
            ),
            "avatar": str(
                info.get("smallhead")
                or info.get("bighead")
                or info.get("SmallHeadImgUrl")
                or info.get("BigHeadImgUrl")
                or info.get("headimgurl")
                or info.get("avatar")
                or ""
            ),
        }
    return {}


async def _refresh_agent_account_brief(agent_id: str, wxid: str = "") -> dict:
    agent_id = str(agent_id or "").strip()
    agent = agent_manager.get_agent(agent_id) if agent_id else None
    wxid = str(wxid or (agent or {}).get("wxid") or _get_self_wxid() or "").strip()
    if not agent_id or not wxid:
        return {}

    try:
        data = await wechat_api.batch_get_contact_brief_info(wxid)
    except Exception as e:
        _log(f"[ACCOUNT] brief lookup failed wxid={wxid}: {type(e).__name__}: {e}")
        return {}

    entry = _brief_entry_from_response(data, wxid)
    name = str(entry.get("name") or "").strip()
    avatar = str(entry.get("avatar") or "").strip()
    if name == wxid or name == agent_id:
        name = ""

    if name or avatar:
        await agent_manager.update_account(agent_id, wxid=wxid, nickname=name, avatar=avatar)
        if name:
            _put_self_info_field("nickname", name)
            message_store.set_contact(wxid, name=name, avatar=avatar)
        if avatar:
            _put_self_info_field("head_big", avatar)
            app_state.setdefault("avatar_urls", {})[wxid] = avatar
            message_store.set_contact(wxid, name=name, avatar=avatar)

    return {"name": name, "avatar": avatar}


async def _refresh_account_card(agent_id: str) -> None:
    agent_id = str(agent_id or "").strip()
    if not agent_id or not agent_manager.is_connected(agent_id):
        return

    try:
        with wechat_api.use_agent(agent_id):
            await _refresh_agent_login_status(agent_id)
    except Exception as e:
        _log(f"[ACCOUNT] card refresh failed agent={agent_id}: {type(e).__name__}: {e}")
    finally:
        _account_card_refresh_at[agent_id] = time.time()
        _account_card_refreshing.discard(agent_id)


def _schedule_account_card_refresh() -> None:
    now = time.time()
    for account in agent_manager.agents():
        agent_id = str(account.get("id") or "")
        if not agent_id or agent_id in _account_card_refreshing or agent_id in _initializing_agents:
            continue
        last = _account_card_refresh_at.get(agent_id, 0.0)
        if now - last < _ACCOUNT_CARD_REFRESH_INTERVAL_SEC:
            continue
        _account_card_refreshing.add(agent_id)
        _account_card_refresh_at[agent_id] = now
        asyncio.create_task(_refresh_account_card(agent_id))


# ─── Contact brief cache (name + avatar URL) ─────────────────────────
# Used to avoid repeatedly calling BatchGetContactBriefInfo for the same wxids.
_CONTACT_BRIEF_CACHE: dict[str, dict] = {}  # owner::wxid -> {"name": str, "avatar": str, "ts": float}
_CONTACT_BRIEF_CACHE_TTL_SEC = 24 * 60 * 60  # 24h
_CONTACT_BRIEF_LOCK = asyncio.Lock()


# ─── Full contact profile cache ────────────────────────────────────
# Populated lazily via /GetContact for strangers / new chats.
_CONTACT_PROFILE_CACHE: dict[str, dict] = {}  # owner::wxid -> {"profile": dict, "ts": float}
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


def _contact_owner_wxid(self_wxid: str = "") -> str:
    owner = str(self_wxid or _get_self_wxid() or "").strip()
    if owner:
        return owner
    agent_id = str(_active_agent_id or agent_manager.active_id() or "").strip()
    agent = agent_manager.get_agent(agent_id) if agent_id else None
    agent_wxid = str((agent or {}).get("wxid") or (agent or {}).get("account_id") or "").strip()
    if agent_wxid and agent_wxid != agent_id:
        return agent_wxid
    return f"agent:{agent_id}" if agent_id else "default"


def _contact_cache_key(wxid: str, owner_wxid: str = "") -> str:
    return f"{_contact_owner_wxid(owner_wxid)}::{str(wxid or '').strip()}"


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
        "video_len": msg.get("video_len"),
        "voice_len": msg.get("voice_len"),
        "voice_hex": msg.get("voice_hex"),
        "voice_data": msg.get("voice_data"),
        "gif_path": msg.get("gif_path"),
        "gif_len": msg.get("gif_len"),
        "file_path": msg.get("file_path"),
        "file_len": msg.get("file_len"),
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
                    or _CONTACT_BRIEF_CACHE.get(_contact_cache_key(sender_wxid), {}).get("name", "")
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
        owner_wxid = _contact_owner_wxid()
        sqlite_cache.upsert_messages(chat_id, [msg], owner_wxid=owner_wxid)
        sqlite_cache.upsert_session_preview(
            chat_id,
            nickname=message_store.get_contact(chat_id).get("name", ""),
            content=preview,
            msg_type=str(msg.get("msgtype", "") or "1"),
            timestamp=msg_timestamp,
            unread_delta=1 if is_recv else 0,
            owner_wxid=owner_wxid,
        )
        _load_session_cache_into_state(owner_wxid)
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
        _initializing_agents.add(selected_agent)
    _log("=" * 60)
    _log(f"WeChat Backend starting...  [mode={config.LOGIN_MODE}] agent={selected_agent or 'default'}")
    _log("=" * 60)

    # Phase 0 — wait for Hook/Protocol API to be reachable and logged in
    _log("[INIT 0] Checking login status...")
    login_status: dict[str, str] = {}
    for attempt in range(15):  # up to 30 seconds
        try:
            # Reset circuit breaker for each attempt
            wechat_api._consecutive_failures = 0
            login_status = await _refresh_agent_login_status(selected_agent)
            _log(f"[INIT 0] ✓ IsLoginStatus onlinestatus={login_status.get('status') or '?'} msg={login_status.get('message') or '-'}")
            wechat_api._consecutive_failures = 0
            break
        except Exception as e:
            wechat_api._consecutive_failures = 0  # don't let CB trigger during wait
            _log(f"[INIT 0] Waiting for API... ({(attempt+1)*2}s) {type(e).__name__}")
            await asyncio.sleep(2)
    else:
        _log("[INIT 0] ⚠ API not reachable after 30s, skip initialization")
        app_state["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False, login_message="接口未就绪")
        _initializing_agents.discard(selected_agent)
        return False
    wechat_api._consecutive_failures = 0  # ensure clean slate for init

    if str(login_status.get("status") or "") != "3":
        _log(f"[INIT 0] ⏸ WeChat not logged in; skip initialization. status={login_status.get('status') or '?'} msg={login_status.get('message') or '-'}")
        app_state["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False)
        _initializing_agents.discard(selected_agent)
        return False

    # Phase 1 — run sequentially to avoid concurrent Hook access (Hook is NOT thread-safe)
    try:
        _log("[INIT 1/7] Loading self info...")
        app_state["self_info"] = await wechat_api.get_self_info()
        existing_agent = agent_manager.get_agent(selected_agent) or {}
        identity = _self_identity_from_response(
            app_state["self_info"] if isinstance(app_state["self_info"], dict) else {},
            agent_id=selected_agent,
            current_wxid=str(existing_agent.get("wxid") or existing_agent.get("account_id") or ""),
        )
        wxid = identity["wxid"]
        nickname = identity["nickname"]
        avatar = identity["avatar"]
        if wxid:
            _self_wxid_to_agent_id[wxid] = selected_agent
            _put_self_info_field("wxid", wxid)
            _put_self_info_field("selfwxid", wxid)
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
        owner_wxid = _contact_owner_wxid()
        cached_contact_count = sqlite_cache.count_contacts(owner_wxid=owner_wxid)
        app_state["contacts"] = _contacts_snapshot_from_db(owner_wxid)
        _log(f"[INIT 1/7] ✓ Contact init skipped during login; loaded {cached_contact_count} cached contacts")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ local contacts load failed: {e}")

    try:
        if selected_agent:
            brief = await _refresh_agent_account_brief(selected_agent)
            if brief.get("name") or brief.get("avatar"):
                _log(f"[INIT 1/7] ✓ Account brief loaded for {selected_agent}")
    except Exception as e:
        _log(f"[INIT 1/7] ✗ account brief lookup failed: {e}")
    # Reset circuit breaker so a slow optional contact refresh doesn't block subsequent requests
    wechat_api._consecutive_failures = 0
    _log("[INIT 1/7] done")

    try:
        owner_wxid = _contact_owner_wxid()
        _log("[INIT 2/4] Loading contacts from local SQLite cache...")
        contacts_snapshot = _contacts_snapshot_from_db(owner_wxid)
        app_state["contacts"] = contacts_snapshot
        friend_count = len(contacts_snapshot.get("friend") or [])
        room_count = len(contacts_snapshot.get("chatroom") or [])
        _log(f"[INIT 2/4] ✓ Cached contacts loaded: {friend_count} friends, {room_count} groups")
    except Exception as e:
        _log(f"[INIT 2/4] ✗ contacts load failed: {e}")

    try:
        app_state["sessions"] = app_state.get("sessions") or {"data": []}
        _log("[INIT 3/7] ✓ Session DB load skipped until Chats view opens")
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

    _log("[INIT 5/7] ✓ Session avatar batch skipped until sessions are requested")
    app_state["avatar_urls"] = avatar_urls

    app_state["last_messages"] = app_state.get("last_messages") or {}
    _log("[INIT 6/7] ✓ Last-message and chat-history preload skipped")

    if config.AGENT_WS_ENABLED and not agent_manager.is_connected(selected_agent):
        _log("[INIT 7/7] Agent disconnected during initialization; will retry on next connection.")
        app_state["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False)
        _initializing_agents.discard(selected_agent)
        return False

    _log("[INIT 7/7] ✓ Initialization complete.")
    app_state["initialized"] = True
    await agent_manager.update_account(selected_agent, initialized=True)
    _initializing_agents.discard(selected_agent)
    _log("=" * 60)
    _log(f"Backend ready at http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    _log(f"Login mode: {config.LOGIN_MODE}  |  API: {config.HOOK_BASE_URL}")
    if config.AGENT_WS_ENABLED:
        _log(f"Agent WS: {config.CLIENT_WSS_URL}  path={config.AGENT_WS_PATH}")
    _log(f"Callback URL: {config.CALLBACK_URL}")
    _log("=" * 60)
    return True

async def _run_initialization_after_agent():
    _log("[INIT] Automatic Hook initialization is disabled. Hook calls now run only from explicit UI actions.")
    while True:
        while not agent_manager.is_connected():
            _log(f"[INIT] Waiting for DLL agent on {config.AGENT_WS_PATH} ...")
            await asyncio.sleep(1)
        # Do not call IsLoginStatus/GetSelfLoginInfo/CDN_Init/InitContact here.
        # Account cards are refreshed by /api/accounts, and opening an account
        # only performs the single Session QueryDB requested by the UI.
        await asyncio.sleep(5)


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
    wxids: list[str] = []
    msg: str
    agent_ids: list[str] = []
    target_types: list[str] = []
    mode: str = "nosrc"
    concurrency_limit: int = 0


class MultiBroadcastTargetsRequest(BaseModel):
    wxids: list[str] = []
    agent_ids: list[str] = []
    target_types: list[str] = []


@app.post("/api/auth/login")
async def auth_login(req: AuthLoginRequest):
    if not config.WEB_ACCESS_KEY:
        return {"ok": False, "error": "access key is not configured"}
    if str(req.key or "") != config.WEB_ACCESS_KEY:
        return {"ok": False}
    return {"ok": True}


@app.get("/api/accounts")
async def list_accounts():
    _schedule_account_card_refresh()
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
        account = agent_manager.get_agent(agent_id) or {}
        cached_status = str(account.get("login_status") or "").strip()
        if cached_status != "3":
            return {
                "ok": False,
                "error": "wechat login status is not ready",
                "login_status": {
                    "status": cached_status,
                    "message": str(account.get("login_message") or "等待微信登录状态刷新"),
                    "wxid": str(account.get("wxid") or account.get("account_id") or ""),
                    "nickname": str(account.get("nickname") or ""),
                    "avatar": str(account.get("avatar") or ""),
                },
                "account": account,
            }
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
    return {"ok": True, "active_id": agent_id, "account": agent_manager.get_agent(agent_id)}


# ─── WeChat Hook Callback (receives messages from Hook) ────────────

@app.post("/api/callback")
async def wechat_callback(request: Request):
    """Receive messages from WeChat Hook and broadcast to frontend via WebSocket."""
    raw_body = await request.body()
    try:
        data = json.loads(raw_body.decode("utf-8-sig"))
    except Exception:
        try:
            form = await request.form()
            data = dict(form)
        except Exception:
            data = {"raw": raw_body.decode("utf-8", errors="replace")}
    if not isinstance(data, dict):
        data = {"raw": data}
    original_path = request.headers.get("x-original-callback-path", "")
    if original_path:
        data.setdefault("_callback_path", original_path)
    _log_callback_sample(data)

    # Log top-level callback keys for debugging CDN download callbacks
    top_keys = list(data.keys()) if isinstance(data, dict) else []
    _log(f"[CALLBACK] top_keys={top_keys}")

    sendorrecv = str(data.get("sendorrecv", "") or "")
    self_wxid = _extract_self_wxid(data) or _get_self_wxid()
    mapped_agent_id = (
        _agent_id_for_self_wxid(self_wxid)
        or agent_manager.agent_id_for_wxid(self_wxid)
    )
    callback_agent_id = str(
        mapped_agent_id
        or data.get("agent_id", "")
        or data.get("agentId", "")
        or ""
    )
    if not callback_agent_id and self_wxid:
        callback_agent_id = f"selfwxid_{self_wxid}"
        _log(f"[CALLBACK] selfwxid={self_wxid} has no agent mapping yet; using isolated runtime {callback_agent_id}")
    if not callback_agent_id and not self_wxid:
        callback_agent_id = _active_agent_id or agent_manager.active_id()
    if callback_agent_id:
        _activate_runtime(callback_agent_id)
    if self_wxid:
        _self_wxid_to_agent_id[self_wxid] = callback_agent_id or _active_agent_id or agent_manager.active_id()
        _put_self_info_field("wxid", self_wxid)
        _put_self_info_field("selfwxid", self_wxid)

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
    pre_messages: list[tuple[str, dict]] = []  # (chat_id, normalized) — stored after callback normalization

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

        # If media arrives as base64 (common in remote_hook), decode+cache and
        # convert to a local path. This keeps websocket payload small and lets
        # frontend reuse existing media rendering without querying WeChat DB.
        try:
            if isinstance(msg, dict):
                mid = str(msg.get("msgsvrid", "") or msg.get("clientmsgid", "") or f"cb_{int(time.time())}")
                media_specs = [
                    ("img_base64", "img_path", "image", "jpg"),
                    ("video_base64", "video_path", "video", "mp4"),
                    ("file_base64", "file_path", "file", "bin"),
                    ("gif_base64", "gif_path", "gif", "gif"),
                ]
            else:
                media_specs = []
                mid = ""

            for source_key, path_key, media_kind, default_ext in media_specs:
                media_b64 = msg.get(source_key)
                if not media_b64:
                    continue
                saved, blen = _save_callback_base64_to_cache(
                    str(media_b64),
                    f"{mid}_{media_kind}" if media_kind != "image" else mid,
                    media_kind,
                    default_ext,
                )
                if not saved:
                    continue
                msg[path_key] = saved
                if media_kind == "image":
                    msg["img_len"] = msg.get("img_len") or blen
                elif media_kind == "video":
                    msg["video_len"] = msg.get("video_len") or blen
                elif media_kind == "file":
                    msg["file_len"] = msg.get("file_len") or blen
                msg.pop(source_key, None)
                _log(f"[CALLBACK_BASE64] cached {media_kind} → {saved} ({blen} bytes)")

                if media_kind != "image":
                    continue

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
            _log(f"[CALLBACK_BASE64] decode error: {type(e).__name__}: {e}")

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

    # Callback payloads already contain either decoded content (RecvType=1)
    # or PB data (RecvType=2). Do not query WeChat DB from the callback path:
    # Hook QueryDB is not concurrency-safe and can crash the client.

    # ── Lazy profile hydration for new incoming senders ───────────
    profile_updates: dict[str, dict] = {}
    profile_wxids: list[str] = []
    openim_profile_wxids_by_gid: dict[str, list[str]] = {}
    for chat_id, msg in pre_messages:
        if str(msg.get("sendorrecv", "")) != "2":
            continue
        sender_wxid = str(msg.get("fromid", "") or "")
        if not sender_wxid or sender_wxid == self_wxid or sender_wxid.endswith("@chatroom"):
            continue
        if sender_wxid.endswith("@openim") and chat_id.endswith("@chatroom"):
            openim_profile_wxids_by_gid.setdefault(chat_id, []).append(sender_wxid)
        else:
            profile_wxids.append(sender_wxid)
    if profile_wxids:
        profile_updates = await _ensure_contact_profiles(profile_wxids, require_full=False)
    for gid, openim_wxids in openim_profile_wxids_by_gid.items():
        profile_updates.update(await _ensure_contact_profiles(openim_wxids, require_full=False, gid=gid))

    # ── Store & collect for broadcast ────────────────────────────
    for chat_id, normalized in pre_messages:
        normalized_messages.append(normalized)
        session_updates.append(_store_message_and_session(chat_id, normalized))

    if session_updates:
        _load_session_cache_into_state(_contact_owner_wxid())

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


_CALLBACK_PATH_ALIASES = {
    "/api/callback",
    config.CALLBACK_PATH,
    "/receiveChatBotMsg",
    "/receiveChatBotMsg/msg",
}
for _callback_alias in sorted(_CALLBACK_PATH_ALIASES):
    if _callback_alias != "/api/callback":
        app.add_api_route(
            _callback_alias,
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
            "contact_profiles": sqlite_cache.get_contacts(owner_wxid=_contact_owner_wxid()),
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
    """Refresh contacts incrementally from Hook when the user opens Contacts."""
    return await _refresh_contacts_incremental(list_type="0", init_if_empty=True)


@app.get("/api/contacts/refresh")
async def refresh_contacts():
    """Force refresh contacts from Hook without re-running InitContact."""
    return await _refresh_contacts_incremental(list_type="0", init_if_empty=True, force_details=True)


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
    gid: str = ""
    force: bool = False


def _normalize_wxids(wxids: list[str]) -> list[str]:
    """Normalize and de-dup contact ids, including chatroom gids."""
    out: list[str] = []
    seen: set[str] = set()
    for w in wxids or []:
        if not isinstance(w, str):
            continue
        w = w.strip()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


async def _query_session_list_from_db() -> dict:
    """Read the native WeChat Session table ordered by nOrder."""
    sql = "select * from Session order by nOrder desc"
    data = await wechat_api.query_db("MicroMsg.db", sql, timeout=8.0)
    rows = data.get("data") if isinstance(data, dict) else []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("rows") or []
    if not isinstance(rows, list):
        rows = []

    sessions: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        wxid = str(
            _row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid")
        ).strip()
        if not wxid or wxid in seen:
            continue
        nickname = str(
            _row_value(row, "strNickName", "StrNickName", "NickName", "nickname")
        )
        seen.add(wxid)
        session = dict(row)
        session.update({
            "strUsrName": wxid,
            "strNickName": nickname,
            "strContent": _row_value(row, "strContent", "StrContent", "content"),
            "nUnReadCount": _row_value(row, "nUnReadCount", "UnReadCount", "unread"),
            "othersAtMe": _row_value(row, "othersAtMe", "OthersAtMe", "atMe"),
            "nOrder": _row_value(row, "nOrder", "NOrder", "order"),
            "order": _row_value(row, "nOrder", "NOrder", "order"),
        })
        sessions.append(session)

    return {"data": sessions}


def _row_value(row: dict, *keys: str):
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return ""


def _to_int(value) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except Exception:
        return 0


def _session_rows_from_cache(owner_wxid: str = "") -> list[dict]:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    return sqlite_cache.get_sessions(owner_wxid=owner_wxid)


def _session_snapshot_from_cache(owner_wxid: str = "") -> dict:
    return {"data": _session_rows_from_cache(owner_wxid)}


def _last_messages_from_session_rows(rows: list[dict]) -> dict[str, dict]:
    last_messages: dict[str, dict] = {}
    for session in rows or []:
        if not isinstance(session, dict):
            continue
        wxid = str(_row_value(session, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
        content = str(_row_value(session, "strContent", "StrContent", "content", "lastMsg") or "")
        if not wxid:
            continue
        last_messages[wxid] = {
            "content": content,
            "type": str(_row_value(session, "nMsgType", "NMsgType", "msgType", "type") or "1"),
            "is_sender": _to_int(_row_value(session, "isSender", "IsSender", "is_sender")),
            "time": _to_int(_row_value(
                session,
                "nTime",
                "NTime",
                "nUpdateTime",
                "nCreateTime",
                "CreateTime",
                "timestamp",
                "lastTimestamp",
            )),
        }
    return last_messages


def _load_session_cache_into_state(owner_wxid: str = "") -> tuple[dict, dict]:
    snapshot = _session_snapshot_from_cache(owner_wxid)
    rows = snapshot.get("data") or []
    last_messages = _last_messages_from_session_rows(rows)
    app_state["sessions"] = snapshot
    app_state["last_messages"] = {**(app_state.get("last_messages") or {}), **last_messages}
    return snapshot, last_messages


def _contact_init_lock(owner_wxid: str) -> asyncio.Lock:
    key = owner_wxid or "__default__"
    lock = _CONTACT_INIT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CONTACT_INIT_LOCKS[key] = lock
    return lock


def _contact_profile_wxid(profile: dict) -> str:
    return str(
        profile.get("wxid")
        or profile.get("UserName")
        or profile.get("userName")
        or profile.get("strUsrName")
        or profile.get("username")
        or profile.get("gid")
        or profile.get("chatroomid")
        or profile.get("chatroom_id")
        or profile.get("describe")
        or profile.get("account")
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
        or profile.get("strNickName")
        or _contact_profile_wxid(profile)
        or ""
    )


def _contact_profile_explicit_name(profile: dict) -> str:
    return str(
        profile.get("Remark")
        or profile.get("remark")
        or profile.get("markname")
        or profile.get("NickName")
        or profile.get("nickname")
        or profile.get("nick")
        or profile.get("strNickName")
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
        or profile.get("HeadImgUrl")
        or profile.get("HeadUrl")
        or profile.get("smallHeadUrl")
        or profile.get("bigHeadUrl")
        or profile.get("smallheadimgurl")
        or profile.get("bigheadimgurl")
        or profile.get("avatar")
        or ""
    )


def _contact_payload(raw: dict | list) -> dict | list:
    """Unwrap common Hook response envelopes while keeping contact-shaped dicts intact."""
    if not isinstance(raw, dict):
        return raw
    current = raw
    for _ in range(3):
        if any(k in current for k in (
            "friend", "friends", "contact", "contacts",
            "chatroom", "chatrooms", "chat_room", "chat_rooms",
            "group", "groups", "group_chat", "group_chats",
            "batch", "batches",
        )):
            return current
        nested = current.get("data")
        if isinstance(nested, dict):
            current = nested
            continue
        break
    return current


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _extract_contact_list(payload: dict | list, *keys: str) -> list:
    payload = _contact_payload(payload)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for key in keys:
        for row in _as_list(payload.get(key)):
            if not isinstance(row, dict):
                continue
            wxid = _contact_profile_wxid(row) or str(id(row))
            if wxid in seen:
                continue
            seen.add(wxid)
            out.append(row)
    return out


def _extract_batch_contact_entries(payload: dict | list) -> list[dict]:
    payload = _contact_payload(payload)
    if not isinstance(payload, dict):
        return []
    batches = payload.get("batch") or payload.get("batches") or payload.get("Batch") or []
    if isinstance(batches, dict):
        batches = [batches]
    if not isinstance(batches, list):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        raw_values = (
            batch.get("list")
            or batch.get("wxids")
            or batch.get("wxidlist")
            or batch.get("usernames")
            or batch.get("users")
            or ""
        )
        if isinstance(raw_values, str):
            parts = raw_values.replace("\r", "\n").replace(";", ",").replace("\n", ",").split(",")
        elif isinstance(raw_values, list):
            parts = raw_values
        else:
            parts = []
        for raw_wxid in parts:
            wxid = str(raw_wxid or "").strip()
            if not wxid or wxid in seen:
                continue
            seen.add(wxid)
            out.append({
                "wxid": wxid,
                "gid": wxid if wxid.endswith("@chatroom") else "",
                "type": "chatroom" if wxid.endswith("@chatroom") else "friend",
            })
    return out


def _contacts_from_getcontact_response(data) -> list[dict]:
    """Normalize GetContact envelopes from local/remote Hook into contact rows."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []

    current = data
    for _ in range(4):
        rows = _extract_contact_list(
            current,
            "contacts", "contact", "info", "infos", "member", "members", "data",
        )
        if rows:
            return rows
        if _contact_profile_wxid(current):
            return [current]
        nested = current.get("data")
        if isinstance(nested, dict):
            current = nested
            continue
        break
    return []


def _all_raw_contact_entries(contacts: dict | list) -> list[dict]:
    contacts = _contact_payload(contacts)
    if isinstance(contacts, list):
        source = contacts
    elif isinstance(contacts, dict):
        source = [
            *_extract_contact_list(contacts, "friend", "friends", "contact", "contacts"),
            *_extract_contact_list(contacts, "chatroom", "chatrooms", "chat_room", "chat_rooms", "group", "groups", "group_chat", "group_chats"),
            *_extract_contact_list(contacts, "data"),
            *_extract_batch_contact_entries(contacts),
        ]
    else:
        source = []

    out: list[dict] = []
    seen: set[str] = set()
    for entry in source:
        if not isinstance(entry, dict):
            continue
        wxid = _contact_profile_wxid(entry)
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        out.append(entry)
    return out


def _with_contact_hydration_progress(snapshot: dict, owner_wxid: str = "") -> dict:
    payload = dict(snapshot or {})
    payload["hydration_progress"] = _CONTACT_HYDRATION_PROGRESS.get(owner_wxid or _contact_owner_wxid()) or {}
    return payload


def _contacts_snapshot_from_db(owner_wxid: str = "") -> dict:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    cached = sqlite_cache.get_contacts(owner_wxid=owner_wxid)
    friends: list[dict] = []
    rooms: list[dict] = []
    for wxid, entry in cached.items():
        if not isinstance(entry, dict):
            continue
        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
        raw = dict(profile)
        name = str(entry.get("name") or _contact_profile_name(raw) or wxid)
        avatar = str(entry.get("avatar") or _contact_profile_avatar(raw) or "")
        raw.update({
            "wxid": wxid,
            "nickname": raw.get("nickname") or raw.get("NickName") or name,
            "strNickName": raw.get("strNickName") or name,
            "smallhead": raw.get("smallhead") or raw.get("SmallHeadImgUrl") or avatar,
            "bighead": raw.get("bighead") or raw.get("BigHeadImgUrl") or avatar,
            "avatar": raw.get("avatar") or avatar,
        })
        if bool(entry.get("is_group")) or wxid.endswith("@chatroom"):
            rooms.append(raw)
        else:
            friends.append(raw)
    return {
        "count_friend": str(len(friends)),
        "count_chatroom": str(len(rooms)),
        "friend": friends,
        "chatroom": rooms,
        "source": "sqlite",
    }


def _find_openim_payload(data: dict) -> dict:
    """Find the OpenIM detail payload even when it is nested by the transport."""
    if not isinstance(data, dict):
        return {}

    stack = [data]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)

        if any(k in current for k in ("openim_wxid", "openim_nickname", "openim_head")):
            return current

        for key in ("data", "Data", "body", "Body", "result", "retdata"):
            child = current.get(key)
            if isinstance(child, dict):
                stack.append(child)
    return {}


def _parse_openim_detail(raw_detail) -> tuple[dict, str]:
    if not raw_detail:
        return {}, ""
    if isinstance(raw_detail, dict):
        detail = raw_detail
    else:
        try:
            detail = json.loads(str(raw_detail))
        except Exception:
            return {}, ""

    company = ""
    custom_info = detail.get("custom_info")
    if isinstance(custom_info, list):
        for item in custom_info:
            if not isinstance(item, dict):
                continue
            if str(item.get("title") or "") != "企业":
                continue
            rows = item.get("detail")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("desc"):
                    company = str(row.get("desc") or "")
                    break
            if company:
                break
    return detail, company


def _openim_profile_from_payload(payload: dict, requested_wxid: str, gid: str = "") -> dict:
    detail_raw = payload.get("openim_detail")
    detail, company = _parse_openim_detail(detail_raw)
    wxid = str(payload.get("openim_wxid") or requested_wxid or "").strip()
    nickname = str(payload.get("openim_nickname") or wxid or "").strip()
    avatar = str(payload.get("openim_head") or "").strip()

    return {
        "wxid": wxid,
        "UserName": wxid,
        "NickName": nickname,
        "nickname": nickname,
        "SmallHeadImgUrl": avatar,
        "BigHeadImgUrl": avatar,
        "avatar": avatar,
        "OpenIM": True,
        "OpenIMGid": str(gid or ""),
        "OpenIMCompany": company,
        "OpenIMDetail": detail,
        "openim_detail": detail_raw or "",
        "openim_invt": payload.get("openim_invt") or "",
        "SourceText": "企业微信",
    }


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


def _profile_has_useful_payload(profile: dict) -> bool:
    if not isinstance(profile, dict):
        return False
    return any(key != "wxid" and value not in ("", None, {}, []) for key, value in profile.items())


def _mark_getcontact_hydrated(profile: dict) -> dict:
    payload = dict(profile) if isinstance(profile, dict) else {}
    payload["_getcontact_hydrated"] = True
    return payload


def _contact_needs_detail_hydration(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
    if entry.get("avatar") or _contact_profile_avatar(profile):
        return False
    if profile.get("_getcontact_hydrated") is True:
        return False
    if str(profile.get("_getcontact_hydrated") or "").lower() in {"1", "true", "yes"}:
        return False
    return True


def _contact_detail_missing_ids(owner_wxid: str = "", candidates: list[str] | None = None) -> list[str]:
    cached = sqlite_cache.get_contacts(candidates, owner_wxid=owner_wxid or _contact_owner_wxid())
    missing: list[str] = []
    for wxid, entry in cached.items():
        if wxid and _contact_needs_detail_hydration(entry):
            missing.append(wxid)
    return missing


def _cache_raw_contacts(contacts: dict, *, owner_wxid: str = "") -> tuple[int, int]:
    contacts = _contact_payload(contacts)
    if not isinstance(contacts, dict):
        return 0, 0
    friend_list = _extract_contact_list(
        contacts,
        "friend", "friends", "contact", "contacts",
    )
    room_list = _extract_contact_list(
        contacts,
        "chatroom", "chatrooms", "chat_room", "chat_rooms",
        "group", "groups", "group_chat", "group_chats",
    )
    data_list = [
        *_extract_contact_list(contacts, "data"),
        *_extract_batch_contact_entries(contacts),
    ]
    if data_list:
        friend_wxids = {_contact_profile_wxid(c) for c in friend_list if isinstance(c, dict)}
        room_wxids = {_contact_profile_wxid(c) for c in room_list if isinstance(c, dict)}
        for entry in data_list:
            if not isinstance(entry, dict):
                continue
            wxid = _contact_profile_wxid(entry)
            if not wxid or wxid in friend_wxids or wxid in room_wxids:
                continue
            if wxid.endswith("@chatroom"):
                room_list.append(entry)
                room_wxids.add(wxid)
            else:
                friend_list.append(entry)
                friend_wxids.add(wxid)
    raw_contact_updates: dict[str, dict] = {}
    for c in [*friend_list, *room_list]:
        if not isinstance(c, dict):
            continue
        wxid = _contact_profile_wxid(c)
        if not wxid:
            continue
        name = _contact_profile_name(c)
        avatar = _contact_profile_avatar(c)
        message_store.set_contact(wxid, name=name, avatar=avatar)
        raw_contact_updates[wxid] = {
            "wxid": wxid,
            "name": name or wxid,
            "avatar": avatar,
            "is_group": wxid.endswith("@chatroom"),
            "profile": dict(c),
        }
    if raw_contact_updates:
        sqlite_cache.upsert_contacts(raw_contact_updates, owner_wxid=owner_wxid or _contact_owner_wxid())
    return len(friend_list), len(room_list)


async def _broadcast_contact_profile_updates(updates: dict[str, dict], *, account_id: str = "") -> None:
    if not updates:
        return
    await manager.broadcast({
        "type": "contact_profiles",
        "data": {
            "account_id": account_id or _active_agent_id or agent_manager.active_id(),
            "members": updates,
        },
    })


async def _broadcast_contacts_snapshot(snapshot: dict, *, account_id: str = "", owner_wxid: str = "") -> None:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    await manager.broadcast({
        "type": "contacts_snapshot",
        "data": {
            "account_id": account_id or _active_agent_id or agent_manager.active_id(),
            "contacts": snapshot,
            "contact_profiles": sqlite_cache.get_contacts(owner_wxid=owner_wxid),
            "hydration_progress": _CONTACT_HYDRATION_PROGRESS.get(owner_wxid) or {},
        },
    })


async def _broadcast_contact_hydration_progress(owner_wxid: str, account_id: str = "") -> None:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    await manager.broadcast({
        "type": "contacts_hydration_progress",
        "data": {
            "account_id": account_id or _active_agent_id or agent_manager.active_id(),
            "owner_wxid": owner_wxid,
            **(_CONTACT_HYDRATION_PROGRESS.get(owner_wxid) or {}),
        },
    })


async def _cache_contact_profiles(profiles: list[dict], *, owner_wxid: str = "") -> dict[str, dict]:
    """Save full profiles plus brief name/avatar caches. Returns frontend updates."""
    now = time.time()
    owner_wxid = owner_wxid or _contact_owner_wxid()
    updates: dict[str, dict] = {}
    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = app_state.setdefault("avatar_urls", {})
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                profile = _mark_getcontact_hydrated(profile)
                wxid = _contact_profile_wxid(profile)
                if not wxid:
                    continue
                summary = _contact_profile_summary(profile)
                name = summary.get("name", "")
                avatar = summary.get("avatar", "")
                cache_key = _contact_cache_key(wxid, owner_wxid)
                _CONTACT_PROFILE_CACHE[cache_key] = {"profile": profile, "ts": now}
                _CONTACT_BRIEF_CACHE[cache_key] = {"name": name, "avatar": avatar, "ts": now}
                if avatar:
                    avatar_urls[wxid] = avatar
                message_store.set_contact(wxid, name=name, avatar=avatar)
                updates[wxid] = summary
            if updates:
                sqlite_cache.upsert_contacts(updates, owner_wxid=owner_wxid)
    return updates


async def _fetch_and_cache_contact_details(
    wxids: list[str],
    *,
    broadcast_updates: bool = False,
    broadcast_progress: bool = False,
    owner_wxid: str = "",
    account_id: str = "",
) -> dict[str, dict]:
    targets: list[str] = []
    seen: set[str] = set()
    for wxid in wxids or []:
        wxid = str(wxid or "").strip()
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        targets.append(wxid)
    if not targets:
        return {}

    result: dict[str, dict] = {}
    owner_wxid = owner_wxid or _contact_owner_wxid()
    total = len(targets)
    total_batches = (total + 99) // 100
    processed = 0
    failed = 0
    updated_total = 0

    if broadcast_progress:
        _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
            "active": True,
            "phase": "GetContact",
            "batch": 0,
            "total_batches": total_batches,
            "processed": 0,
            "total": total,
            "updated": 0,
            "failed": 0,
        }
        await _broadcast_contact_hydration_progress(owner_wxid, account_id)

    for i in range(0, len(targets), 100):
        batch = targets[i:i + 100]
        batch_index = i // 100 + 1
        batch_update_count = 0
        try:
            data = await wechat_api.get_contact(batch)
            contacts = _contacts_from_getcontact_response(data)
            useful_contacts: list[dict] = []
            found: set[str] = set()
            empty_updates: dict[str, dict] = {}
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                wxid = _contact_profile_wxid(contact)
                if not wxid:
                    continue
                found.add(wxid)
                if _contact_profile_explicit_name(contact) or _contact_profile_avatar(contact):
                    useful_contacts.append(contact)
                    continue
                empty_profile = {
                    "wxid": wxid,
                    "gid": wxid if wxid.endswith("@chatroom") else "",
                    "type": "chatroom" if wxid.endswith("@chatroom") else str(contact.get("type") or ""),
                    "_getcontact_hydrated": True,
                }
                empty_updates[wxid] = {
                    "wxid": wxid,
                    "name": "",
                    "avatar": "",
                    "is_group": wxid.endswith("@chatroom"),
                    "profile": empty_profile,
                }

            for wxid in batch:
                if wxid in found:
                    continue
                empty_updates[wxid] = {
                    "wxid": wxid,
                    "name": "",
                    "avatar": "",
                    "is_group": wxid.endswith("@chatroom"),
                    "profile": {
                        "wxid": wxid,
                        "gid": wxid if wxid.endswith("@chatroom") else "",
                        "type": "chatroom" if wxid.endswith("@chatroom") else "",
                        "_getcontact_hydrated": True,
                    },
                }

            updates = await _cache_contact_profiles(useful_contacts, owner_wxid=owner_wxid)
            if empty_updates:
                sqlite_cache.upsert_contacts(empty_updates, owner_wxid=owner_wxid)
                updates.update({
                    wxid: {
                        "wxid": wxid,
                        "name": "",
                        "avatar": "",
                        "profile": payload.get("profile") or {"wxid": wxid},
                    }
                    for wxid, payload in empty_updates.items()
                })
            result.update(updates)
            batch_update_count = len(updates)
            updated_total += batch_update_count
            if broadcast_updates and updates:
                await _broadcast_contact_profile_updates(updates, account_id=account_id)
            _log(f"[CONTACTS] GetContact hydrated batch {batch_index}/{total_batches}: requested={len(batch)} updates={len(updates)}")
        except Exception as e:
            failed += len(batch)
            _log(f"[CONTACTS] GetContact detail batch failed ({len(batch)}): {type(e).__name__}: {e}")
        processed += len(batch)
        if broadcast_progress:
            _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
                "active": True,
                "phase": "GetContact",
                "batch": batch_index,
                "total_batches": total_batches,
                "processed": min(processed, total),
                "total": total,
                "updated": updated_total,
                "failed": failed,
                "current_batch_count": len(batch),
                "current_batch_updated": batch_update_count,
            }
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            if account_id:
                _runtime_for(account_id)["app_state"]["contacts"] = snapshot
            else:
                app_state["contacts"] = snapshot
            await _broadcast_contacts_snapshot(snapshot, account_id=account_id, owner_wxid=owner_wxid)
            await _broadcast_contact_hydration_progress(owner_wxid, account_id)
        await asyncio.sleep(0.05)
    if broadcast_progress:
        _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
            "active": False,
            "phase": "GetContact",
            "batch": total_batches,
            "total_batches": total_batches,
            "processed": total,
            "total": total,
            "updated": updated_total,
            "failed": failed,
        }
        await _broadcast_contact_hydration_progress(owner_wxid, account_id)
    return result


def _schedule_contact_detail_hydration(owner_wxid: str, wxids: list[str], account_id: str = "") -> None:
    ids = _normalize_wxids(wxids)
    if not ids or owner_wxid in _CONTACT_HYDRATING_OWNERS:
        return

    total = len(ids)
    total_batches = (total + 99) // 100
    _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
        "active": True,
        "phase": "GetContact",
        "batch": 0,
        "total_batches": total_batches,
        "processed": 0,
        "total": total,
        "updated": 0,
        "failed": 0,
    }

    async def _hydrate_details() -> None:
        _CONTACT_HYDRATING_OWNERS.add(owner_wxid)
        try:
            with wechat_api.use_agent(account_id):
                await _fetch_and_cache_contact_details(
                    ids,
                    broadcast_updates=True,
                    broadcast_progress=True,
                    owner_wxid=owner_wxid,
                    account_id=account_id,
                )
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            if account_id:
                _runtime_for(account_id)["app_state"]["contacts"] = snapshot
            else:
                app_state["contacts"] = snapshot
            await _broadcast_contacts_snapshot(snapshot, account_id=account_id, owner_wxid=owner_wxid)
            _log(f"[CONTACTS] GetContact hydration complete: owner={owner_wxid} ids={len(ids)}")
        except Exception as e:
            _log(f"[CONTACTS] GetContact hydration task failed: {type(e).__name__}: {e}")
        finally:
            _CONTACT_HYDRATING_OWNERS.discard(owner_wxid)

    _log(f"[CONTACTS] scheduling GetContact hydration: owner={owner_wxid} ids={len(ids)}, batch=100")
    task = asyncio.create_task(_hydrate_details())
    _track_background_send(task, "contacts_getcontact")


async def _refresh_contacts_incremental(*, list_type: str | int = "0", init_if_empty: bool = False, force_details: bool = False) -> dict:
    """Load the directory from local SQLite, initializing it once via InitContact.

    GetFriendAndChatRoomList is intentionally not used here; on remote Hook it can
    crash WeChat. InitContact provides the wxid/gid list, then this Contacts view
    hydrates details with /GetContact in 100-id batches.
    """
    owner_wxid = _contact_owner_wxid()
    account_id = _active_agent_id or agent_manager.active_id() or ""
    if app_state.get("contacts_loaded"):
        snapshot = _contacts_snapshot_from_db(owner_wxid)
        app_state["contacts"] = snapshot
        ids = list(sqlite_cache.get_contacts(owner_wxid=owner_wxid).keys()) if force_details else _contact_detail_missing_ids(owner_wxid)
        if ids:
            _schedule_contact_detail_hydration(owner_wxid, ids, account_id)
        return _with_contact_hydration_progress(snapshot, owner_wxid)

    lock = _contact_init_lock(owner_wxid)
    async with lock:
        if app_state.get("contacts_loaded"):
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            app_state["contacts"] = snapshot
            ids = list(sqlite_cache.get_contacts(owner_wxid=owner_wxid).keys()) if force_details else _contact_detail_missing_ids(owner_wxid)
            if ids:
                _schedule_contact_detail_hydration(owner_wxid, ids, account_id)
            return _with_contact_hydration_progress(snapshot, owner_wxid)

        cached_before = sqlite_cache.get_contacts(owner_wxid=owner_wxid)
        try:
            _log("[CONTACTS] first directory open: calling InitContact")
            contacts = await wechat_api.init_contact()
        except Exception as e:
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            app_state["contacts"] = snapshot
            if snapshot.get("friend") or snapshot.get("chatroom"):
                _log(f"[CONTACTS] InitContact failed; served local cache: {type(e).__name__}: {e}")
                return _with_contact_hydration_progress(snapshot, owner_wxid)
            raise

        raw_entries = _all_raw_contact_entries(contacts)
        if not raw_entries:
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            app_state["contacts"] = snapshot
            _log(
                f"[CONTACTS] InitContact returned no parseable contacts; "
                f"served local cache={len(cached_before)}"
            )
            return _with_contact_hydration_progress(snapshot, owner_wxid)

        friend_count, room_count = _cache_raw_contacts(contacts, owner_wxid=owner_wxid)
        app_state["contacts_loaded"] = True
        sqlite_cache.mark_contact_init_done_v2(owner_wxid=owner_wxid)
        snapshot = _contacts_snapshot_from_db(owner_wxid)
        app_state["contacts"] = snapshot
        _log(
            f"[CONTACTS] InitContact cached: friends={friend_count} groups={room_count} "
            f"entries={len(raw_entries)} cached_before={len(cached_before)}"
        )

        ids: list[str] = []
        seen: set[str] = set()
        for entry in raw_entries:
            wxid = _contact_profile_wxid(entry)
            if not wxid or wxid in seen:
                continue
            seen.add(wxid)
            ids.append(wxid)
        if ids:
            _schedule_contact_detail_hydration(owner_wxid, ids, account_id)

        return _with_contact_hydration_progress(snapshot, owner_wxid)


async def _ensure_contact_profiles(
    wxids: list[str],
    *,
    require_full: bool = True,
    gid: str = "",
    broadcast_updates: bool = False,
    fetch_missing: bool = False,
    force_refresh: bool = False,
) -> dict[str, dict]:
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
    owner_wxid = _contact_owner_wxid()
    account_id = _active_agent_id or agent_manager.active_id() or ""
    db_cached = sqlite_cache.get_contacts(wxids, owner_wxid=owner_wxid)

    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = app_state.get("avatar_urls", {}) or {}
            for wxid in wxids:
                cache_key = _contact_cache_key(wxid, owner_wxid)
                cached_profile = _CONTACT_PROFILE_CACHE.get(cache_key)
                profile_ok = bool(cached_profile) and (
                    now - float(cached_profile.get("ts", 0)) <= _CONTACT_PROFILE_CACHE_TTL_SEC
                )
                if profile_ok:
                    profile = cached_profile.get("profile", {}) or {}
                    summary = _contact_profile_summary(profile)
                    if summary.get("avatar"):
                        result[wxid] = summary
                        if not force_refresh:
                            continue

                cached_brief = _CONTACT_BRIEF_CACHE.get(cache_key)
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
                    if not force_refresh:
                        continue

                db_entry = db_cached.get(wxid)
                if db_entry:
                    db_profile = db_entry.get("profile") if isinstance(db_entry.get("profile"), dict) else {"wxid": wxid}
                    db_avatar = str(db_entry.get("avatar") or "")
                    db_name = str(db_entry.get("name") or wxid)
                    if db_avatar and (not require_full or _profile_has_useful_payload(db_profile)):
                        summary = {
                            "wxid": wxid,
                            "name": db_name,
                            "avatar": db_avatar,
                            "profile": db_profile,
                        }
                        result[wxid] = summary
                        _CONTACT_PROFILE_CACHE[cache_key] = {"profile": db_profile, "ts": now}
                        _CONTACT_BRIEF_CACHE[cache_key] = {"name": db_name, "avatar": db_avatar, "ts": now}
                        avatar_urls[wxid] = db_avatar
                        message_store.set_contact(wxid, name=db_name, avatar=db_avatar)
                        if not force_refresh:
                            continue

                missing.append(wxid)

    if broadcast_updates and result:
        await _broadcast_contact_profile_updates(result, account_id=account_id)

    if not (fetch_missing or force_refresh):
        for wxid in wxids:
            result.setdefault(wxid, {"wxid": wxid, "name": wxid, "avatar": "", "profile": {"wxid": wxid}})
        return result

    openim_missing = [wxid for wxid in missing if wxid.endswith("@openim")]
    regular_missing = [wxid for wxid in missing if not wxid.endswith("@openim")]

    if openim_missing:
        sem = asyncio.Semaphore(8)

        async def fetch_openim_profile(wxid: str) -> dict[str, dict]:
            async with sem:
                try:
                    data = await wechat_api.get_openim_contact(wxid, gid=gid)
                    payload = _find_openim_payload(data)
                    if not payload:
                        return {}
                    profile = _openim_profile_from_payload(payload, wxid, gid=gid)
                    try:
                        contact_data = await wechat_api.get_contact([wxid])
                        contacts = _contacts_from_getcontact_response(contact_data)
                        if contacts and isinstance(contacts[0], dict):
                            merged = dict(contacts[0])
                            merged.update({k: v for k, v in profile.items() if v not in ("", None, {})})
                            profile = merged
                    except Exception as e:
                        _log(f"[PROFILE] OpenIM GetContact merge skipped ({wxid}): {type(e).__name__}: {e}")
                    return await _cache_contact_profiles([profile], owner_wxid=owner_wxid)
                except Exception as e:
                    _log(f"[PROFILE] GetOpenIMContact failed ({wxid}): {type(e).__name__}: {e}")
                    return {}

        for updates in await asyncio.gather(*(fetch_openim_profile(wxid) for wxid in openim_missing)):
            result.update(updates)
            if broadcast_updates and updates:
                await _broadcast_contact_profile_updates(updates, account_id=account_id)

    if regular_missing:
        updates = await _fetch_and_cache_contact_details(
            regular_missing,
            broadcast_updates=broadcast_updates,
            owner_wxid=owner_wxid,
            account_id=account_id,
        )
        result.update(updates)

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
    owner_wxid = _contact_owner_wxid()
    members: dict[str, dict] = {}
    missing: list[str] = []
    db_cached = sqlite_cache.get_contacts(wxids, owner_wxid=owner_wxid)

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
            cache_key = _contact_cache_key(wxid, owner_wxid)
            cached = _CONTACT_BRIEF_CACHE.get(cache_key)
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

            db_entry = db_cached.get(wxid)
            if db_entry:
                db_name = str(db_entry.get("name") or "")
                db_avatar = direct_avatar or str(db_entry.get("avatar") or "")
                if db_name and db_name != wxid and db_avatar:
                    members[wxid] = {"name": db_name, "avatar": db_avatar}
                    _CONTACT_BRIEF_CACHE[cache_key] = {"name": db_name, "avatar": db_avatar, "ts": now}
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
                    contact_updates: dict[str, dict] = {}
                    async with _CONTACT_BRIEF_LOCK:
                        avatar_urls = app_state.get("avatar_urls", {}) or {}
                        for wxid, entry in found_in_batch.items():
                            if not entry.get("avatar") and avatar_urls.get(wxid):
                                entry["avatar"] = avatar_urls[wxid]
                            members[wxid] = entry
                            profile = {
                                "wxid": wxid,
                                "nickname": entry.get("name", ""),
                                "SmallHeadImgUrl": entry.get("avatar", ""),
                                "BigHeadImgUrl": entry.get("avatar", ""),
                            }
                            contact_updates[wxid] = {
                                "wxid": wxid,
                                "name": entry.get("name", "") or wxid,
                                "avatar": entry.get("avatar", ""),
                                "is_group": wxid.endswith("@chatroom"),
                                "profile": profile,
                            }
                            _CONTACT_BRIEF_CACHE[_contact_cache_key(wxid, owner_wxid)] = {
                                "name": entry.get("name", ""),
                                "avatar": entry.get("avatar", ""),
                                "ts": now,
                            }
                    if contact_updates:
                        sqlite_cache.upsert_contacts(contact_updates, owner_wxid=owner_wxid)
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
    members = await _ensure_contact_profiles(
        req.wxids,
        require_full=True,
        gid=req.gid,
        broadcast_updates=True,
        fetch_missing=req.force,
        force_refresh=req.force,
    )
    return {"members": members}


@app.get("/api/sessions")
async def get_sessions(request: Request):
    """Get current session (conversation) list from cache."""
    agent_id = _request_agent_id(request)
    if agent_id and agent_manager.is_connected(agent_id):
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
        _load_session_cache_into_state(_contact_owner_wxid())
    return app_state["sessions"] or {}


@app.get("/api/sessions/refresh")
async def refresh_sessions(request: Request):
    """Load session list once from native WeChat DB, then serve local SQLite cache."""
    t0 = time.time()
    agent_id = _request_agent_id(request)
    if agent_id and agent_manager.is_connected(agent_id):
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
    owner_wxid = _contact_owner_wxid()
    if not app_state.get("session_list_loaded"):
        try:
            with wechat_api.use_agent(agent_id or _active_agent_id or agent_manager.active_id()):
                _log("[REFRESH] querying native Session table: db=MicroMsg.db sql=select * from Session order by nOrder desc")
                db_sessions = await _query_session_list_from_db()
            session_rows = db_sessions.get("data", []) if isinstance(db_sessions, dict) else []
            if session_rows:
                sqlite_cache.upsert_sessions(session_rows, owner_wxid=owner_wxid)
                app_state["session_list_loaded"] = True
                _log(f"[REFRESH] native Session table cached: {len(session_rows)} rows")
            else:
                _log("[REFRESH] native Session table returned empty; falling back to local cache")
                app_state["session_list_loaded"] = True
        except Exception as e:
            _log(f"[REFRESH] Query Session table failed; using local cache: {type(e).__name__}: {e}")

    raw_sessions, last_messages = _load_session_cache_into_state(owner_wxid)
    session_list = raw_sessions.get("data", []) if isinstance(raw_sessions, dict) else []

    total_ms = int((time.time() - t0) * 1000)
    _log(f"[REFRESH] ✓ {len(session_list)} sessions from local cache; no per-session MSG query — {total_ms}ms")
    return {"sessions": raw_sessions, "last_messages": last_messages}


# ─── REST API: Messages (History via QueryDB) ─────────────────────

@app.get("/api/messages/{wxid}")
async def get_messages(wxid: str, limit: int = 50, db: str = "MSG0.db"):
    """Get chat history for a contact/group.
    Prefer local SQLite cache. Callback messages are persisted immediately, so
    reopening a chat can show cached content without touching Hook QueryDB.
    """
    owner_wxid = _contact_owner_wxid()
    initialized = sqlite_cache.has_initialized(wxid, owner_wxid=owner_wxid)
    cached = sqlite_cache.get_messages(wxid, limit, owner_wxid=owner_wxid)
    if cached:
        if initialized:
            message_store.add_history(wxid, cached)
            source = "sqlite"
        else:
            message_store.add_history_no_flag(wxid, cached)
            source = "sqlite_partial"
        return {"data": message_store.get_messages(wxid, limit), "source": source}

    if initialized:
        existing = message_store.get_messages(wxid, limit)
        if existing:
            return {"data": existing, "source": "memory"}
        return {"data": [], "source": "sqlite"}

    if message_store.is_db_loaded(wxid):
        existing = message_store.get_messages(wxid, limit)
        if existing:
            return {"data": existing, "source": "memory"}

    history = await wechat_api.get_chat_history(wxid, max(limit, 100))
    rows = history.get("data", []) if isinstance(history, dict) else []
    normalized = _normalize_history_rows(wxid, rows)
    message_store.add_history(wxid, normalized)
    try:
        if normalized:
            sqlite_cache.upsert_messages(wxid, normalized, mark_initialized=True, owner_wxid=owner_wxid)
        else:
            sqlite_cache.mark_initialized(wxid, owner_wxid=owner_wxid)
    except Exception as e:
        _log(f"[SQLITE_CACHE] history write failed for {wxid}: {type(e).__name__}: {e}")
    return {"data": message_store.get_messages(wxid, limit), "source": "hook_db"}


@app.get("/api/messages/{wxid}/older")
async def get_older_messages(wxid: str, before: int = 0, limit: int = 50):
    """Load older messages before a given timestamp (for infinite scroll).
    Returns messages with CreateTime < before, sorted chronologically."""
    if before <= 0:
        return {"data": []}
    owner_wxid = _contact_owner_wxid()
    cached = sqlite_cache.get_messages(wxid, limit, before=before, owner_wxid=owner_wxid)
    if cached:
        message_store.add_history_no_flag(wxid, cached)
        return {"data": cached, "source": "sqlite"}

    history = await wechat_api.get_chat_history(wxid, limit, before_time=before)
    rows = history.get("data", []) if isinstance(history, dict) else []
    normalized = _normalize_history_rows(wxid, rows)
    # Add to store so they persist in memory
    message_store.add_history_no_flag(wxid, normalized)
    try:
        sqlite_cache.upsert_messages(wxid, normalized, owner_wxid=owner_wxid)
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
    mode: str = "nosrc"
    concurrency_limit: int = 0

class RevokeRequest(BaseModel):
    msg_svrid: int
    to_wxid: str

class SessionActionRequest(BaseModel):
    wxid: str


_send_counter = 0
_background_send_tasks: set[asyncio.Task] = set()

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


def _send_api_parallelism(limit_override: int | None = None) -> int:
    try:
        override = int(limit_override or 0)
    except Exception:
        override = 0
    if override > 0:
        limit = override
    else:
        try:
            limit = int(getattr(config, "HOOK_API_CONCURRENCY", 10) or 10)
        except Exception:
            limit = 10
    return max(1, min(limit, 100))


def _track_background_send(task: asyncio.Task, label: str) -> None:
    _background_send_tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        _background_send_tasks.discard(done)
        try:
            done.result()
        except Exception as e:
            _log(f"[SEND_BG] {label} task crashed: {type(e).__name__}: {e}")

    task.add_done_callback(_done)


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


async def _broadcast_local_sent_for_agent(
    agent_id: str,
    chat_id: str,
    msg_type: str,
    content: str = "",
    extra: dict | None = None,
) -> None:
    if agent_id:
        async with _ACCOUNT_LOCK:
            previous_agent = _active_agent_id or agent_manager.active_id() or ""
            try:
                _activate_runtime(agent_id)
                await _broadcast_local_sent_message(chat_id, msg_type, content, extra)
            finally:
                if previous_agent and previous_agent != agent_id:
                    _activate_runtime(previous_agent)
        return
    await _broadcast_local_sent_message(chat_id, msg_type, content, extra)


def _queue_send_job(
    label: str,
    agent_id: str,
    chat_id: str,
    msg_type: str,
    content: str,
    extra: dict | None,
    send_factory,
) -> None:
    async def _runner():
        try:
            with wechat_api.use_agent(agent_id):
                result = await send_factory()
            if _send_result_ok(result):
                await _broadcast_local_sent_for_agent(agent_id, chat_id, msg_type, content, extra)
            else:
                _log(f"[SEND_BG] {label} failed for {chat_id}: {result}")
        except Exception as e:
            _log(f"[SEND_BG] {label} error for {chat_id}: {type(e).__name__}: {e}")

    _track_background_send(asyncio.create_task(_runner()), label)


def _send_queued_response(kind: str, wxid: str, **extra) -> dict:
    data = {"queued": True, "kind": kind, "wxid": wxid}
    data.update(extra)
    return data


@app.post("/api/send/text")
async def send_text(req: SendTextRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "text",
        agent_id,
        req.wxid,
        "1",
        req.msg,
        None,
        lambda: wechat_api.send_text(req.wxid, req.msg),
    )
    return _send_queued_response("text", req.wxid)


@app.post("/api/send/image")
async def send_image(req: SendImageRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "image",
        agent_id,
        req.wxid,
        "3",
        "",
        {"img_path": req.picpath},
        lambda: wechat_api.send_image(req.wxid, req.picpath, req.diyfilename, req.fileData),
    )
    return _send_queued_response("image", req.wxid)


@app.post("/api/send/file")
async def send_file(req: SendFileRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "file",
        agent_id,
        req.wxid,
        "49",
        "",
        {"file_path": req.filepath},
        lambda: wechat_api.send_file(req.wxid, req.filepath, req.fileData),
    )
    return _send_queued_response("file", req.wxid)


@app.post("/api/send/video")
async def send_video(req: SendVideoRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "video",
        agent_id,
        req.wxid,
        "43",
        "",
        {"video_path": req.videopath},
        lambda: wechat_api.send_video(req.wxid, req.videopath, req.fileData),
    )
    return _send_queued_response("video", req.wxid)


@app.post("/api/send/gif")
async def send_gif(req: SendGifRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "gif",
        agent_id,
        req.wxid,
        "47",
        "",
        {"gif_path": req.gifpath},
        lambda: wechat_api.send_gif(req.wxid, req.gifpath, req.fileData),
    )
    return _send_queued_response("gif", req.wxid)


@app.post("/api/send/quote")
async def send_quote(req: SendQuoteRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "quote",
        agent_id,
        req.towxid,
        "49",
        req.title,
        None,
        lambda: wechat_api.send_quote(
            req.towxid, req.title, req.svrid,
            req.fromusr, req.displayname, req.chatusr
        ),
    )
    return _send_queued_response("quote", req.towxid)


@app.post("/api/send/at")
async def send_at(req: SendAtRequest):
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "at",
        agent_id,
        req.gid,
        "1",
        req.msg,
        None,
        lambda: wechat_api.send_at(req.gid, req.wxidlist, req.nicknamelist, req.msg),
    )
    return _send_queued_response("at", req.gid)


_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "_uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)


@app.post("/api/broadcast/text")
async def broadcast_text(req: BroadcastTextRequest):
    """Broadcast text using NoSrc by default, or normal SendTextMsg when requested."""
    wxids = [w for w in req.wxids if w]
    agent_id = _active_agent_id or agent_manager.active_id() or ""
    normal_mode = str(req.mode or "").lower() in {"normal", "src", "regular"}
    sem = asyncio.Semaphore(_send_api_parallelism(req.concurrency_limit))

    async def _send_one(wxid: str) -> dict:
        async with sem:
            try:
                with wechat_api.use_agent(agent_id):
                    result = await (wechat_api.send_text(wxid, req.msg) if normal_mode else wechat_api.send_text_no_src(wxid, req.msg))
                ok = _send_result_ok(result)
                if ok:
                    await _broadcast_local_sent_for_agent(agent_id, wxid, "1", req.msg)
                return {"wxid": wxid, "ok": ok, "result": result}
            except Exception as e:
                return {"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*(_send_one(wxid) for wxid in wxids)) if wxids else []
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent
    return {"total": len(wxids), "sent": sent, "failed": failed, "results": results}


async def _broadcast_image_to_targets(
    target_wxids: list[str],
    cdn: dict,
    image_extra: dict,
    agent_id: str = "",
    concurrency_limit: int = 0,
) -> list[dict]:
    sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

    async def _send_one(wxid: str) -> dict:
        async with sem:
            try:
                with wechat_api.use_agent(agent_id):
                    result = await wechat_api.send_image_no_src(wxid, cdn)
                ok = _send_result_ok(result)
                if ok:
                    await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", image_extra)
                return {"wxid": wxid, "ok": ok, "result": result}
            except Exception as e:
                return {"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

    return await asyncio.gather(*(_send_one(wxid) for wxid in target_wxids)) if target_wxids else []


@app.post("/api/broadcast/image-upload")
async def broadcast_image_upload(
    wxids: str = Form(...),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
    file: UploadFile = File(...),
):
    """Broadcast an uploaded image through NoSrc CDN or normal SendPicMsg(fileData)."""
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

    agent_id = _active_agent_id or agent_manager.active_id() or ""
    if str(mode or "").lower() in {"normal", "src", "regular"}:
        db_image_id = sqlite_cache.put_media_blob(data, file.content_type or "image/*", file.filename or filename)
        sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

        async def _send_normal(wxid: str) -> dict:
            async with sem:
                try:
                    with wechat_api.use_agent(agent_id):
                        result = await wechat_api.send_image(wxid, filepath, "", data.hex())
                    ok = _send_result_ok(result)
                    if ok:
                        await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", {"db_image_id": db_image_id, "img_path": filepath})
                    return {"wxid": wxid, "ok": ok, "result": result}
                except Exception as e:
                    return {"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

        results = await asyncio.gather(*(_send_normal(wxid) for wxid in target_wxids)) if target_wxids else []
        sent = sum(1 for row in results if row.get("ok"))
        failed = len(results) - sent
        return {"total": len(target_wxids), "sent": sent, "failed": failed, "results": results, "mode": "normal"}

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

    with wechat_api.use_agent(agent_id):
        cdn = await wechat_api.cdn_upload_image(send_path, target_wxids[0])
    if cdn.get("error"):
        return {
            "total": len(target_wxids),
            "sent": 0,
            "failed": len(target_wxids),
            "results": [{"wxid": wxid, "ok": False, "error": cdn["error"]} for wxid in target_wxids],
            "cdn": {k: cdn.get(k) for k in _BROADCAST_IMG_CDN_KEYS},
        }

    results = await _broadcast_image_to_targets(target_wxids, cdn, {"img_path": filepath}, agent_id, concurrency_limit)
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent

    return {
        "total": len(target_wxids),
        "sent": sent,
        "failed": failed,
        "results": results,
        "cdn": {k: cdn.get(k) for k in _BROADCAST_IMG_CDN_KEYS},
    }


def _normalize_broadcast_target_types(raw_types) -> set[str]:
    aliases = {
        "friend": "friends",
        "friends": "friends",
        "personal": "friends",
        "person": "friends",
        "contacts": "friends",
        "contact": "friends",
        "好友": "friends",
        "个人": "friends",
        "group": "groups",
        "groups": "groups",
        "chatroom": "groups",
        "chatrooms": "groups",
        "群": "groups",
        "群聊": "groups",
    }
    out: set[str] = set()
    for value in raw_types or []:
        key = str(value or "").strip().lower()
        normalized = aliases.get(key)
        if normalized:
            out.add(normalized)
    return out


def _raw_contact_list(raw_contacts: dict | list, key: str) -> list:
    raw_contacts = _contact_payload(raw_contacts)
    if not raw_contacts:
        return []
    if isinstance(raw_contacts, list):
        return [
            entry for entry in raw_contacts
            if isinstance(entry, dict)
            and (key == "chatroom") == _contact_wxid(entry).endswith("@chatroom")
        ]
    if not isinstance(raw_contacts, dict):
        return []
    if key == "friend":
        value = [
            *_extract_contact_list(raw_contacts, "friend", "friends", "contact", "contacts"),
            *_extract_contact_list(raw_contacts, "data"),
            *_extract_batch_contact_entries(raw_contacts),
        ]
        value = [entry for entry in value if not _contact_wxid(entry).endswith("@chatroom")]
    else:
        value = [
            *_extract_contact_list(
                raw_contacts,
                "chatroom", "chatrooms", "chat_room", "chat_rooms",
                "group", "groups", "group_chat", "group_chats",
            ),
            *_extract_contact_list(raw_contacts, "friend", "friends", "contact", "contacts", "data"),
            *_extract_batch_contact_entries(raw_contacts),
        ]
        value = [entry for entry in value if _contact_wxid(entry).endswith("@chatroom")]
    seen: set[str] = set()
    out: list[dict] = []
    for entry in value:
        wxid = _contact_wxid(entry)
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        out.append(entry)
    return out


def _contact_wxid(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(
        entry.get("wxid")
        or entry.get("UserName")
        or entry.get("userName")
        or entry.get("strUsrName")
        or entry.get("username")
        or entry.get("gid")
        or entry.get("chatroomid")
        or entry.get("chatroom_id")
        or entry.get("account")
        or ""
    ).strip()


def _dedupe_targets(wxids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for wxid in wxids:
        wxid = str(wxid or "").strip()
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        out.append(wxid)
    return out


def _contact_counts(raw_contacts: dict | list) -> dict[str, int]:
    return {
        "friends": len([
            entry for entry in _raw_contact_list(raw_contacts, "friend")
            if _contact_wxid(entry) and not _contact_wxid(entry).endswith("@chatroom")
        ]),
        "groups": len([
            entry for entry in _raw_contact_list(raw_contacts, "chatroom")
            if _contact_wxid(entry) and _contact_wxid(entry).endswith("@chatroom")
        ]),
    }


def _resolve_targets_from_contacts(contacts: dict | list, target_types: set[str]) -> list[str]:
    targets: list[str] = []
    if "friends" in target_types:
        for entry in _raw_contact_list(contacts, "friend"):
            wxid = _contact_wxid(entry)
            if wxid and not wxid.endswith("@chatroom"):
                targets.append(wxid)
    if "groups" in target_types:
        for entry in _raw_contact_list(contacts, "chatroom"):
            wxid = _contact_wxid(entry)
            if wxid and wxid.endswith("@chatroom"):
                targets.append(wxid)
    return _dedupe_targets(targets)


async def _ensure_account_contacts(agent_id: str):
    async with _ACCOUNT_LOCK:
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
        with wechat_api.use_agent(agent_id):
            login_status = await _refresh_agent_login_status(agent_id)
            if str(login_status.get("status") or "") != "3":
                raise RuntimeError(f"wechat not logged in: {login_status.get('message') or login_status.get('status') or 'unknown'}")
            if not app_state.get("initialized"):
                await _run_backend_initialization(agent_id)
        owner_wxid = _contact_owner_wxid()
        cached_contacts = _contacts_snapshot_from_db(owner_wxid)
        if cached_contacts.get("friend") or cached_contacts.get("chatroom"):
            app_state["contacts"] = cached_contacts
            app_state["contacts_loaded"] = True
            counts = _contact_counts(cached_contacts)
            _log(f"[BROADCAST] local contacts for {owner_wxid}: friends={counts['friends']} groups={counts['groups']}")
            return cached_contacts
        with wechat_api.use_agent(agent_id):
            contacts = await _refresh_contacts_incremental(list_type="0", init_if_empty=True)
        app_state["contacts"] = contacts
        counts = _contact_counts(contacts)
        _log(f"[BROADCAST] initialized contacts for {owner_wxid}: friends={counts['friends']} groups={counts['groups']}")
        return contacts


async def _resolve_account_broadcast_targets(agent_id: str, target_types: set[str]) -> list[str]:
    if not target_types:
        return []
    contacts = await _ensure_account_contacts(agent_id)
    return _resolve_targets_from_contacts(contacts, target_types)


async def _prepare_multi_account_targets(
    agent_ids: list[str],
    direct_targets: list[str],
    target_types: set[str],
) -> tuple[list[str], list[tuple[str, str]], dict[str, int], dict[str, dict[str, int]], list[dict]]:
    connected_agents = [a for a in (agent_ids or []) if agent_manager.is_connected(a)]
    if not connected_agents:
        connected_agents = [a["id"] for a in agent_manager.agents() if a.get("id")]

    total = 0
    account_targets: dict[str, int] = {}
    account_counts: dict[str, dict[str, int]] = {}
    work_items: list[tuple[str, str]] = []
    skipped_results: list[dict] = []

    for agent_id in connected_agents:
        try:
            if direct_targets:
                with wechat_api.use_agent(agent_id):
                    login_status = await _refresh_agent_login_status(agent_id)
                if str(login_status.get("status") or "") != "3":
                    raise RuntimeError(f"wechat not logged in: {login_status.get('message') or login_status.get('status') or 'unknown'}")
                target_wxids = _dedupe_targets(direct_targets)
                account_counts[agent_id] = {"friends": 0, "groups": 0, "targets": len(target_wxids)}
            else:
                contacts = await _ensure_account_contacts(agent_id)
                counts = _contact_counts(contacts)
                target_wxids = _resolve_targets_from_contacts(contacts, target_types)
                account_counts[agent_id] = {**counts, "targets": len(target_wxids)}
        except Exception as e:
            target_wxids = []
            account_counts.setdefault(agent_id, {"friends": 0, "groups": 0, "targets": 0})
            skipped_results.append({"agent_id": agent_id, "wxid": "", "ok": False, "error": f"{type(e).__name__}: {e}"})
        account_targets[agent_id] = len(target_wxids)
        total += len(target_wxids)
        work_items.extend((agent_id, wxid) for wxid in target_wxids)

    return connected_agents, work_items, account_targets, account_counts, skipped_results


@app.post("/api/accounts/broadcast/targets")
async def multi_account_broadcast_targets(req: MultiBroadcastTargetsRequest):
    direct_targets = [w for w in req.wxids if w]
    target_types = _normalize_broadcast_target_types(req.target_types)
    selected_agents, work_items, account_targets, account_counts, skipped = await _prepare_multi_account_targets(
        req.agent_ids,
        direct_targets,
        target_types,
    )
    return {
        "accounts": len(selected_agents),
        "targets": len(work_items),
        "total": len(work_items),
        "account_targets": account_targets,
        "account_counts": account_counts,
        "results": skipped,
    }


@app.post("/api/accounts/broadcast/text")
async def multi_account_broadcast_text(req: MultiBroadcastTextRequest):
    direct_targets = [w for w in req.wxids if w]
    target_types = _normalize_broadcast_target_types(req.target_types)
    normal_mode = str(req.mode or "").lower() in {"normal", "src", "regular"}
    agent_ids, work_items, account_targets, account_counts, skipped_results = await _prepare_multi_account_targets(
        req.agent_ids,
        direct_targets,
        target_types,
    )
    total = len(work_items)

    sem = asyncio.Semaphore(_send_api_parallelism(req.concurrency_limit))

    async def _send_one(agent_id: str, wxid: str) -> dict:
        async with sem:
            try:
                with wechat_api.use_agent(agent_id):
                    result = await (wechat_api.send_text(wxid, req.msg) if normal_mode else wechat_api.send_text_no_src(wxid, req.msg))
                ok = _send_result_ok(result)
                if ok:
                    await _broadcast_local_sent_for_agent(agent_id, wxid, "1", req.msg)
                return {"agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result}
            except Exception as e:
                return {"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*(_send_one(agent_id, wxid) for agent_id, wxid in work_items)) if work_items else []
    results = skipped_results + results
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent

    return {
        "accounts": len(agent_ids),
        "targets": total,
        "account_targets": account_targets,
        "account_counts": account_counts,
        "total": total,
        "sent": sent,
        "failed": failed,
        "results": results,
    }


@app.post("/api/accounts/broadcast/image-upload")
async def multi_account_broadcast_image_upload(
    wxids: str = Form("[]"),
    agent_ids: str = Form("[]"),
    target_types: str = Form("[]"),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
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
    try:
        parsed_target_types = _normalize_broadcast_target_types(json.loads(target_types or "[]"))
    except Exception:
        parsed_target_types = _normalize_broadcast_target_types([x.strip() for x in str(target_types or "").split(",") if x.strip()])
    selected_agents, work_items, account_targets, account_counts, skipped_results = await _prepare_multi_account_targets(
        requested_agents,
        target_wxids,
        parsed_target_types,
    )
    if not target_wxids and not parsed_target_types:
        return {"accounts": len(selected_agents), "targets": 0, "total": 0, "sent": 0, "failed": 0, "results": [], "error": "no targets"}
    normal_mode = str(mode or "").lower() in {"normal", "src", "regular"}
    work_by_agent: dict[str, list[str]] = {}
    for agent_id, wxid in work_items:
        work_by_agent.setdefault(agent_id, []).append(wxid)

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
    failed = len(skipped_results)
    results = list(skipped_results)
    total_targets = len(work_items)
    for agent_id in selected_agents:
        agent_targets = work_by_agent.get(agent_id, [])
        if not agent_targets:
            continue
        async with _ACCOUNT_LOCK:
            await agent_manager.set_active(agent_id)
            _activate_runtime(agent_id)
            db_image_id = sqlite_cache.put_media_blob(upload_bytes, upload_mime, upload_name)
        if normal_mode:
            sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

            async def _send_normal(wxid: str) -> dict:
                async with sem:
                    try:
                        with wechat_api.use_agent(agent_id):
                            result = await wechat_api.send_image(wxid, upload_name, "", file_hex)
                        ok = _send_result_ok(result)
                        if ok:
                            await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", {"db_image_id": db_image_id})
                        return {"agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result}
                    except Exception as e:
                        return {"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

            send_results = await asyncio.gather(*(_send_normal(wxid) for wxid in agent_targets))
            sent += sum(1 for row in send_results if row.get("ok"))
            failed += sum(1 for row in send_results if not row.get("ok"))
            results.extend(send_results)
            continue
        with wechat_api.use_agent(agent_id):
            try:
                cdn = await wechat_api.cdn_upload_image(upload_name, agent_targets[0], file_data=file_hex)
            except Exception as e:
                cdn = {"error": f"{type(e).__name__}: {e}"}
            if cdn.get("error"):
                failed += len(agent_targets)
                for wxid in agent_targets:
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": False, "error": cdn["error"]})
                continue
            sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

            async def _send_one(wxid: str) -> dict:
                async with sem:
                    try:
                        with wechat_api.use_agent(agent_id):
                            result = await wechat_api.send_image_no_src(wxid, cdn)
                        ok = _send_result_ok(result)
                        if ok:
                            await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", {"db_image_id": db_image_id})
                        return {"agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result}
                    except Exception as e:
                        return {"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

            send_results = await asyncio.gather(*(_send_one(wxid) for wxid in agent_targets))
            sent += sum(1 for row in send_results if row.get("ok"))
            failed += sum(1 for row in send_results if not row.get("ok"))
            results.extend(send_results)

    return {
        "accounts": len(selected_agents),
        "targets": total_targets,
        "account_targets": account_targets,
        "account_counts": account_counts,
        "total": total_targets,
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

    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "image-upload",
        agent_id,
        wxid,
        "3",
        "",
        {"img_path": filepath},
        lambda: wechat_api.send_image(wxid, send_path),
    )
    return _send_queued_response("image-upload", wxid, path=filepath)


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

    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "file-upload",
        agent_id,
        wxid,
        "49",
        safe_name,
        {"file_path": filepath},
        lambda: wechat_api.send_file(wxid, filepath),
    )
    return _send_queued_response("file-upload", wxid, path=filepath)


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

    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "video-upload",
        agent_id,
        wxid,
        "43",
        "",
        {"video_path": filepath},
        lambda: wechat_api.send_video(wxid, filepath),
    )
    return _send_queued_response("video-upload", wxid, path=filepath)


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

    agent_id = _active_agent_id or agent_manager.active_id() or ""
    _queue_send_job(
        "gif-upload",
        agent_id,
        wxid,
        "47",
        "",
        {"gif_path": filepath},
        lambda: wechat_api.send_gif(wxid, filepath),
    )
    return _send_queued_response("gif-upload", wxid, path=filepath)


@app.post("/api/revoke")
async def revoke_msg(req: RevokeRequest):
    return await wechat_api.revoke_msg(req.msg_svrid, req.to_wxid)


@app.post("/api/mark-read/{wxid}")
async def mark_read(wxid: str):
    """Mark a chat as read: clear unread in store + broadcast to all frontends."""
    # Clear in our in-memory store
    message_store.mark_read(wxid)
    try:
        sqlite_cache.mark_session_read(wxid, owner_wxid=_contact_owner_wxid())
        _load_session_cache_into_state(_contact_owner_wxid())
    except Exception as e:
        _log(f"[MARK_READ] local session cache update failed for {wxid}: {e}")
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
            updated = sqlite_cache.update_image_path_by_msg_id(str(msg_id), path, owner_wxid=_contact_owner_wxid())
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


def _extract_group_member_list(data: dict) -> list[dict]:
    """Find a member list in direct or transport-wrapped Hook responses."""
    if not isinstance(data, dict):
        return []
    stack = [data]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        members = current.get("member") or current.get("members") or current.get("Member")
        if isinstance(members, list):
            return [m for m in members if isinstance(m, dict)]
        for key in ("data", "Data", "body", "Body", "result", "retdata", "payload"):
            child = current.get(key)
            if isinstance(child, dict):
                stack.append(child)
    return []


def _normalize_group_member(member: dict, gid: str) -> dict:
    wxid = str(
        member.get("wxid")
        or member.get("userName")
        or member.get("username")
        or member.get("UserName")
        or ""
    ).strip()
    if not wxid:
        return {}
    name = str(
        member.get("nickname")
        or member.get("displayname")
        or member.get("DisplayName")
        or member.get("markname")
        or member.get("name")
        or wxid
    ).strip()
    avatar = str(
        member.get("user_head_small")
        or member.get("user_head_big")
        or member.get("smallhead")
        or member.get("bighead")
        or member.get("SmallHeadImgUrl")
        or member.get("BigHeadImgUrl")
        or member.get("avatar")
        or ""
    ).strip()
    profile = dict(member)
    profile.update({
        "wxid": wxid,
        "UserName": wxid,
        "nickname": name,
        "NickName": name,
        "SmallHeadImgUrl": avatar,
        "BigHeadImgUrl": str(member.get("user_head_big") or avatar),
        "ChatRoomId": gid,
    })
    return {"wxid": wxid, "name": name or wxid, "avatar": avatar, "profile": profile}


def _frontend_group_members(members: dict[str, dict]) -> dict[str, dict]:
    return {
        wxid: {
            "wxid": wxid,
            "name": str(entry.get("name") or wxid),
            "avatar": str(entry.get("avatar") or ""),
        }
        for wxid, entry in members.items()
        if wxid
    }


async def _cache_group_member_details(gid: str, raw_members: list[dict]) -> dict[str, dict]:
    owner_wxid = _contact_owner_wxid()
    now = time.time()
    normalized: dict[str, dict] = {}
    contact_updates: dict[str, dict] = {}
    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = app_state.setdefault("avatar_urls", {})
            for member in raw_members:
                entry = _normalize_group_member(member, gid)
                wxid = str(entry.get("wxid") or "")
                if not wxid:
                    continue
                normalized[wxid] = entry
                name = str(entry.get("name") or wxid)
                avatar = str(entry.get("avatar") or "")
                cache_key = _contact_cache_key(wxid, owner_wxid)
                _CONTACT_BRIEF_CACHE[cache_key] = {"name": name, "avatar": avatar, "ts": now}
                if avatar:
                    avatar_urls[wxid] = avatar
                message_store.set_contact(wxid, name=name, avatar=avatar)
                contact_updates[wxid] = {
                    "wxid": wxid,
                    "name": name,
                    "avatar": avatar,
                    "is_group": False,
                    "profile": entry.get("profile") or {"wxid": wxid},
                }

    if normalized:
        sqlite_cache.upsert_group_members(gid, list(normalized.values()), owner_wxid=owner_wxid)
        sqlite_cache.upsert_contacts(contact_updates, owner_wxid=owner_wxid)
    return normalized


async def _fallback_group_member_details(gid: str) -> dict[str, dict]:
    detail = await wechat_api.get_friend_detail_info(gid)
    result: dict[str, dict] = {}
    member_wxids: list[str] = []

    if isinstance(detail, dict):
        members = detail.get("member", [])
        for m in members:
            wxid = m.get("wxid", "") if isinstance(m, dict) else ""
            nickname = m.get("nickname", "") if isinstance(m, dict) else ""
            if wxid:
                member_wxids.append(wxid)
                result[wxid] = {"wxid": wxid, "name": nickname or wxid, "avatar": "", "profile": {"wxid": wxid, "nickname": nickname}}

    if not member_wxids:
        return {}

    batch_size = 100
    for i in range(0, len(member_wxids), batch_size):
        batch = member_wxids[i:i + batch_size]
        try:
            data = await wechat_api.batch_get_contact_brief_info(",".join(batch))
            for info in data.get("info", []):
                wxid = info.get("wxid", "")
                if not wxid or wxid not in result:
                    continue
                avatar = info.get("smallhead", "") or info.get("bighead", "")
                name = info.get("nickname", "") or info.get("nick", "") or info.get("markname", "")
                if avatar:
                    result[wxid]["avatar"] = avatar
                if name and result[wxid]["name"] == wxid:
                    result[wxid]["name"] = name
                result[wxid]["profile"].update({
                    "SmallHeadImgUrl": result[wxid]["avatar"],
                    "BigHeadImgUrl": avatar,
                    "nickname": result[wxid]["name"],
                })
        except Exception as e:
            _log(f"[GROUP_DETAILS] fallback batch brief info failed: {e}")
        await asyncio.sleep(0.05)

    await _cache_group_member_details(gid, [entry.get("profile") or entry for entry in result.values()])
    return result


@app.get("/api/group/{gid}/member-details")
async def get_group_member_details(gid: str, force: bool = False):
    """Fetch names + avatar URLs for all members of a group and cache by owner wxid."""
    owner_wxid = _contact_owner_wxid()
    cached = sqlite_cache.get_group_members(gid, owner_wxid=owner_wxid)
    if cached and not force and all(entry.get("avatar") for entry in cached.values()):
        _log(f"[GROUP_DETAILS] {gid}: cache hit ({len(cached)} members)")
        return {"members": _frontend_group_members(cached), "cached": True}

    raw_members: list[dict] = []
    try:
        data = await wechat_api.get_chatroom_member_detail(gid)
        raw_members = _extract_group_member_list(data)
    except Exception as e:
        _log(f"[GROUP_DETAILS] GetChatrooMmemberDetail failed for {gid}: {type(e).__name__}: {e}")

    if raw_members:
        normalized = await _cache_group_member_details(gid, raw_members)
        _log(f"[GROUP_DETAILS] {gid}: stored {len(normalized)} members from GetChatrooMmemberDetail")
        return {"members": _frontend_group_members(normalized), "cached": False}

    if cached:
        _log(f"[GROUP_DETAILS] {gid}: returning sparse cache ({len(cached)} members)")
        return {"members": _frontend_group_members(cached), "cached": True}

    fallback = await _fallback_group_member_details(gid)
    if not fallback:
        _log(f"[GROUP_DETAILS] {gid}: no members found")
        return {"members": {}}

    for wxid, entry in fallback.items():
        if not entry.get("avatar"):
            entry["avatar"] = f"/api/avatar/{wxid}"
    _log(f"[GROUP_DETAILS] {gid}: fallback resolved {len(fallback)} members")
    return {"members": _frontend_group_members(fallback), "cached": False}


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

    async def _cb_proxy(request: _Req):
        """Forward the callback to the main app via HTTP (loopback).

        Retries with backoff because the callback server may start before
        the main uvicorn server is ready to accept connections.
        """
        import asyncio as _aio
        body = await request.body()
        ct = request.headers.get("content-type", "application/json")
        if not body and request.query_params:
            body = json.dumps(dict(request.query_params), ensure_ascii=False).encode("utf-8")
            ct = "application/json"
        _url = f"http://127.0.0.1:{config.SERVER_PORT}/api/callback"
        last_err = None
        for attempt in range(6):  # up to ~15s of retries
            try:
                resp = await _proxy_client.post(
                    _url,
                    content=body,
                    headers={
                        "content-type": ct,
                        "x-original-callback-path": request.url.path,
                    },
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

    cb_app.add_api_route(
        "/api/callback",
        _cb_proxy,
        methods=["GET", "POST"],
        include_in_schema=False,
    )

    for _callback_alias in sorted(_CALLBACK_PATH_ALIASES):
        if _callback_alias != "/api/callback":
            cb_app.add_api_route(
                _callback_alias,
                _cb_proxy,
                methods=["GET", "POST"],
                include_in_schema=False,
            )

    cb_app.add_api_route(
        "/{full_path:path}",
        _cb_proxy,
        methods=["GET", "POST"],
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
