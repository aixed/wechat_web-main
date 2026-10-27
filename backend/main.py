"""
WeChat Web Client - Backend Server
FastAPI server that bridges the WeChat Hook API with the frontend.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles
from datetime import datetime

import json
import os
import sys
import asyncio
import time
import httpx
import base64
import io
import shutil
import re
import html
import subprocess
import importlib
import hashlib
import mimetypes
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

import config
import access_store
import protocol_api
import hook_login
import wechat_api
from agent_ws import agent_manager
from ws_manager import manager
from message_store import MessageStore
from sqlite_cache import SqliteMessageCache
from database import database_directory, database_path
from pb_parser import parse_raw_pb
from smart_reply import SmartReplyEngine, _mention_targets
from ai_service import AiService, AiServiceError
from mcp_service import McpService, McpServiceError, requires_bearer_token
from domp_bridge import SQL_START, WORKFLOW_TOOL, workflow_arguments, workflow_reply, split_reply, strip_chat_mentions
from domp_quote import sql_quote
from domp_offline_bridge import OfflineConfirmationStore, CITY_PROMPT, city_input, confirmation_message, offline_analysis_task, is_offline_confirmation_prompt, offline_reply_chunks
from domp_city_selection import (CITY_SELECTION_PREFIX, city_selection_failure, selection_options,
    selection_number, selection_message)
from domp_query_bridge import QUERY_TOOL, QUERY_REPLY_PREFIX, PROBLEM_QUERY_REPLY_PREFIX, query_candidate, query_followup, query_export_followup, query_analysis_content, query_arguments, query_tool_call, query_reply, query_reply_chunks, query_memory_turn, owner_query_config, execution_details_requested, explicit_pending_analysis, explicit_problem_range_analysis, query_clarification_candidate
from domp_execution_bridge import EXECUTION_TOOL, EXECUTION_REPLY_PREFIX, execution_candidate, execution_target, execute_target, execution_reply_chunks, owner_execution_config, approval_execution_candidate, approval_execution_targets, approve_execute_targets
from domp_approval_bridge import APPROVAL_TOOL, APPROVAL_REPLY_PREFIX, approval_candidate, approval_target, approve_target, approval_reply_chunks, owner_approval_config
from domp_submission_bridge import SUBMISSION_TOOL, SUBMISSION_REPLY_PREFIX, SUBMISSION_PROMPT, submission_candidate, submission_arguments, submission_reply, submission_reply_chunks, owner_submission_config
from domp_unified_bridge import UNIFIED_SKILL_NAME, is_unified_task, submission_workflow_identifier, unified_candidate, unified_route, owner_unified_config, owner_filehelper_config
from domp_identifier import identifiers_in
from query_progress import progress_text, update_progress
from daily_log import append_daily_log


def _write_log(category: str, msg: str) -> None:
    print(msg, flush=True)
    now = datetime.now()
    append_daily_log(category, f"[{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}]{msg}\n", when=now)


def _log(msg: str) -> None:
    """Write a general backend log entry."""
    _write_log("main", msg)


def _smart_reply_log(msg: str) -> None:
    """Write only to the dedicated smart-reply daily log."""
    _write_log("smart_reply", msg)


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

    cb_dir = os.path.join(_scoped_dir(_IMG_CACHE_DIR), "callback", media_kind)
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

# When we call CDN /download, we register a pending entry.
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
        _bucket('_pending_cdn_images', _pending_cdn_images)[key] = existing_fut
    return existing_fut


async def _fulfill_cdn_pending(key: str, file_path: str) -> bool:
    """Try to fulfill a pending CDN download. Returns True if matched."""
    async with _pending_cdn_lock:
        fut = _bucket('_pending_cdn_images', _pending_cdn_images).pop(key, None)
    if fut and not fut.done():
        fut.set_result(file_path)
        return True
    return False


async def _fulfill_all_cdn_pending(file_path: str):
    """Fulfill ALL pending CDN downloads with this file path (best-effort).
    Called when an img_base64 arrives and we can't match by specific ID."""
    async with _pending_cdn_lock:
        keys = list(_bucket('_pending_cdn_images', _pending_cdn_images).keys())
        for key in keys:
            fut = _bucket('_pending_cdn_images', _pending_cdn_images).pop(key, None)
            if fut and not fut.done():
                fut.set_result(file_path)


async def _cleanup_cdn_pending(key: str):
    """Remove a pending entry (e.g. on timeout)."""
    async with _pending_cdn_lock:
        _bucket('_pending_cdn_images', _pending_cdn_images).pop(key, None)


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
_SQLITE_CACHE_DIR = str(database_directory())
_SQLITE_CACHE_FILENAME = "wechat_protocol_cache.sqlite3" if config.IS_PROTOCOL else "wechat_cache.sqlite3"
sqlite_cache = SqliteMessageCache(os.path.join(_SQLITE_CACHE_DIR, _SQLITE_CACHE_FILENAME))
smart_reply_engine = SmartReplyEngine()
ai_service = AiService(
    base_url=config.AI_BASE_URL,
    api_key=config.AI_API_KEY,
    model=config.AI_MODEL,
    timeout_seconds=config.AI_TIMEOUT_SECONDS,
    max_concurrency=config.AI_MAX_CONCURRENCY,
)
mcp_service = McpService(timeout_seconds=config.MCP_TIMEOUT_SECONDS)
_active_agent_id = ""
_account_runtimes: dict[str, dict] = {}
_self_wxid_to_agent_id: dict[str, str] = {}
_ACCOUNT_LOCK = asyncio.Lock()
_PROTOCOL_OWNERSHIP_LOCK = asyncio.Lock()
_ACCOUNT_CARD_REFRESH_FAST_INTERVAL_SEC = 1.0
_ACCOUNT_CARD_REFRESH_LOGGED_IN_INTERVAL_SEC = 10.0
_account_card_refresh_at: dict[str, float] = {}
_account_card_refreshing: set[str] = set()
_initializing_agents: set[str] = set()
_agent_login_status_seen: dict[str, str] = {}
_agent_self_profile_refreshed: set[str] = set()
_agent_polling_paused_until: dict[str, float] = {}
_CONTACT_INIT_LOCKS: dict[str, asyncio.Lock] = {}
_CONTACT_HYDRATING_OWNERS: set[str] = set()
_SESSION_CONTACT_HYDRATING_OWNERS: set[str] = set()
_CONTACT_HYDRATION_PROGRESS: dict[str, dict] = {}
_RDV_MAPPING_PATH = str(database_path("rdv_wxid_mapping.json"))
_OLD_RDV_MAPPING_PATH = r"D:\xweixin\前端\backend\rdv_wxid_mapping.json"
_RDV_MIN = int("01000000", 16)
_RDV_MAX = int("7fffffff", 16)
_RDV_RE = re.compile(r"^[0-9a-fA-F]{8}$")
_PROTOCOL_ACCOUNT_PROFILE_CACHE: dict[str, tuple[dict, float]] = {}
_PROTOCOL_ACCOUNT_PROFILE_TTL_SEC = 15.0
_LOCAL_HOOK_ACCOUNT_ID = "default"
_LOCAL_HOOK_STARTED_AT = time.time()
_local_hook_last_seen_at = 0.0
_LOCAL_HOOK_PROBE_LOCK = asyncio.Lock()
_runtime_init_task: asyncio.Task | None = None


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
            "sqlite_cache": _user_cache(access_store.owner(key)),
        }
    if key == 'default':
        _account_runtimes[key].update(app_state=app_state, message_store=message_store, sqlite_cache=sqlite_cache)
    return _account_runtimes[key]


def _activate_runtime(agent_id: str) -> str:
    selected = str(agent_id or access_store.current_account.get() or "default")
    _runtime_for(selected)
    access_store.current_account.set(selected)
    return selected


_user_caches: dict[str, SqliteMessageCache] = {}


def _user_cache(user_id):
    if not user_id or user_id == access_store.legacy_user():
        return sqlite_cache
    if user_id not in _user_caches:
        os.makedirs(os.path.join(_SQLITE_CACHE_DIR, 'users'), exist_ok=True)
        _user_caches[user_id] = SqliteMessageCache(os.path.join(_SQLITE_CACHE_DIR, 'users', user_id + '.sqlite3'))
    return _user_caches[user_id]


def _current_agent_id():
    return access_store.current_account.get() or _active_agent_id


def _state():
    account = access_store.current_account.get()
    return _runtime_for(account)['app_state'] if account else app_state


def _messages():
    account = access_store.current_account.get()
    return _runtime_for(account)['message_store'] if account else message_store


def _cache():
    account = access_store.current_account.get()
    return _user_cache(access_store.owner(account) or access_store.current_user.get())


def _bucket(name, fallback):
    account = access_store.current_account.get()
    return _runtime_for(account).setdefault(name, {}) if account and access_store.owner(account) else fallback


def _scoped_dir(base):
    account = access_store.current_account.get()
    user = access_store.owner(account) or access_store.current_user.get()
    path = os.path.join(base, 'users', user, _safe_runtime_id(account)) if user and user != access_store.legacy_user() else base
    os.makedirs(path, exist_ok=True)
    return path


def _wechat_files_base():
    profile = access_store.profile(access_store.current_account.get(), access_store.owner(access_store.current_account.get()))
    if profile and profile.get('rdv'):
        return os.path.join(os.environ.get('APPDATA', ''), 'WxDirDataPath', profile['rdv'], 'WeChat Files')
    return _WECHAT_FILES_BASE


def _require_owned_path(path):
    if not access_store.current_user.get() or not path:
        return
    path = os.path.normcase(os.path.realpath(path))
    bases = [_UPLOAD_DIR, _IMG_CACHE_DIR, _MEDIA_CACHE_DIR, _VOICE_CACHE_DIR, _AVATAR_CACHE_DIR, _STICKER_CACHE_DIR]
    user = access_store.current_user.get()
    def within(root):
        try:
            return os.path.commonpath([path, os.path.normcase(os.path.realpath(root))]) == os.path.normcase(os.path.realpath(root))
        except ValueError:
            return False
    for base in bases:
        if within(os.path.join(base, 'users')):
            if within(os.path.join(base, 'users', user)):
                return
            raise HTTPException(403, '不能访问其他密钥用户的文件')
        if user == access_store.legacy_user() and within(base):
            return
    if access_store.current_account.get() and within(_wechat_files_base()):
        return
    raise HTTPException(403, '此文件不属于当前密钥用户')


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
    protocol_profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    source = protocol_profile or nested or data
    profile = dict(source)
    wxid = str(
        source.get("wxid")
        or source.get("Wxid")
        or source.get("selfwxid")
        or source.get("selfWxid")
        or source.get("self_wxid")
        or source.get("user_name")
        or source.get("UserName")
        or data.get("wxid")
        or data.get("selfwxid")
        or current_wxid
        or ""
    ).strip()
    nickname = str(
        source.get("nickname")
        or source.get("NickName")
        or source.get("nick_name")
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
        or source.get("big_head_url")
        or source.get("small_head_url")
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
        or source.get("AliasName")
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


def _validate_protocol_rdv(raw: str) -> str:
    rdv = str(raw or "").strip().lower()
    if rdv.startswith("0x"):
        rdv = rdv[2:]
    if not rdv:
        raise HTTPException(status_code=400, detail="RDV 不能为空")
    if not _RDV_RE.fullmatch(rdv):
        raise HTTPException(
            status_code=400,
            detail="RDV 必须是 01000000 到 7fffffff 范围内的 8 位十六进制",
        )
    value = int(rdv, 16)
    if value < _RDV_MIN or value > _RDV_MAX:
        raise HTTPException(
            status_code=400,
            detail="RDV 必须是 01000000 到 7fffffff 范围内的 8 位十六进制",
        )
    return rdv


def _protocol_nested_payload(data: dict, key: str) -> dict:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _protocol_value_from_payload(data: dict, *keys: str) -> Any:
    if not isinstance(data, dict):
        return ""
    scopes = [
        data,
        _protocol_nested_payload(data, "data"),
        _protocol_nested_payload(data, "result"),
        _protocol_nested_payload(data, "session"),
    ]
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if value not in (None, ""):
                return value
    return ""


def _protocol_session_id_from_payload(data: dict) -> str:
    return str(_protocol_value_from_payload(data, "session_id", "sessionId", "SessionID", "sessionID")).strip()


def _protocol_start_port_from_payload(data: dict) -> Any:
    return _protocol_value_from_payload(data, "start_port", "startPort", "StartPort")


def _protocol_scope_key() -> str:
    base = f"{config.HOOK_HOST}:{config.HOOK_PORT}"
    user = access_store.current_user.get()
    return f'{base}:user:{user}' if user and user != access_store.legacy_user() else base


def _empty_rdv_mapping_doc() -> dict:
    return {"version": 2, "scopes": {}}


def _load_rdv_mapping_doc() -> dict:
    for path in (_RDV_MAPPING_PATH, _OLD_RDV_MAPPING_PATH):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if not isinstance(data.get("scopes"), dict):
                    data = {"version": 2, "scopes": {_protocol_scope_key(): data}}
                data.setdefault("version", 2)
                data.setdefault("scopes", {})
                return data
        except Exception as e:
            _log(f"[RDV] failed to load mapping {path}: {type(e).__name__}: {e}")
    return _empty_rdv_mapping_doc()


def _save_rdv_mapping_doc(data: dict) -> None:
    os.makedirs(os.path.dirname(_RDV_MAPPING_PATH), exist_ok=True)
    with open(_RDV_MAPPING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _stable_protocol_wxid(value: Any, session_id: str = "") -> str:
    wxid = str(value or "").strip()
    current_session_id = str(session_id or "").strip()
    if not wxid:
        return ""
    if current_session_id and wxid.casefold() == current_session_id.casefold():
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{32}", wxid):
        return ""
    return wxid


def _normalize_rdv_mapping_entry(entry: Any) -> dict:
    if isinstance(entry, dict):
        proxy = entry.get("proxy") if isinstance(entry.get("proxy"), dict) else {}
        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
        wxid = _stable_protocol_wxid(entry.get("wxid") or entry.get("userName"))
        return {
            "wxid": wxid,
            "port": str(entry.get("port") or entry.get("StartPort") or entry.get("startPort") or ""),
            "nickname": str(entry.get("nickname") or entry.get("NickName") or ""),
            "avatar": str(entry.get("avatar") or entry.get("head_big") or entry.get("headimgurl") or ""),
            "account": str(entry.get("account") or entry.get("alias") or entry.get("wechat_account") or ""),
            "phone": str(entry.get("phone") or entry.get("tel") or ""),
            "region": str(entry.get("region") or ""),
            "signature": str(entry.get("signature") or entry.get("sign") or ""),
            "profile": profile,
            "proxy": {
                "Proxy_Type": str(proxy.get("Proxy_Type") or proxy.get("proxy_type") or ""),
                "Proxy_IP": str(proxy.get("Proxy_IP") or proxy.get("proxy_ip") or ""),
                "Proxy_Port": str(proxy.get("Proxy_Port") or proxy.get("proxy_port") or ""),
                "Proxy_Usr": str(proxy.get("Proxy_Usr") or proxy.get("proxy_usr") or ""),
                "Proxy_Pwd": str(proxy.get("Proxy_Pwd") or proxy.get("proxy_pwd") or ""),
            },
        }
    return {"wxid": _stable_protocol_wxid(entry), "port": "", "proxy": {}}


def _current_rdv_mapping() -> dict[str, dict]:
    data = _load_rdv_mapping_doc()
    scopes = data.get("scopes") if isinstance(data.get("scopes"), dict) else {}
    scoped = dict(scopes.get(_protocol_scope_key(), {}) or {})
    if not scoped:
        for value in scopes.values():
            if isinstance(value, dict):
                for rdv, entry in value.items():
                    scoped.setdefault(str(rdv), entry)
    mapping: dict[str, dict] = {}
    for raw_rdv, raw_entry in scoped.items():
        try:
            rdv = _validate_protocol_rdv(str(raw_rdv or ""))
        except HTTPException:
            continue
        entry = _normalize_rdv_mapping_entry(raw_entry)
        if not entry.get("wxid"):
            continue
        mapping[rdv] = entry
    return mapping


def _upsert_rdv_mapping(
    rdv: str,
    wxid: str,
    port: str = "",
    proxy: dict | None = None,
    identity: dict | None = None,
) -> dict[str, dict]:
    rdv = _validate_protocol_rdv(rdv)
    raw_wxid = str(wxid or "").strip()
    wxid = _stable_protocol_wxid(raw_wxid)
    if not wxid:
        detail = "wxid 不能使用每次启动都会变化的 session_id" if raw_wxid else "wxid 不能为空"
        raise HTTPException(status_code=400, detail=detail)
    identity = identity or {}
    data = _load_rdv_mapping_doc()
    scopes = data.setdefault("scopes", {})
    scoped = scopes.setdefault(_protocol_scope_key(), {})
    existing = _normalize_rdv_mapping_entry(scoped.get(rdv, {}))
    same_account = existing.get("wxid") == wxid
    previous = existing if same_account else {}
    profile = (
        identity.get("profile")
        if isinstance(identity.get("profile"), dict) and identity.get("profile")
        else previous.get("profile", {})
    )
    scoped[rdv] = {
        "wxid": wxid,
        "port": str(port or ""),
        "nickname": str(identity.get("nickname") or previous.get("nickname") or ""),
        "avatar": str(identity.get("avatar") or previous.get("avatar") or ""),
        "account": str(identity.get("account") or previous.get("account") or ""),
        "phone": str(identity.get("phone") or previous.get("phone") or ""),
        "region": str(identity.get("region") or previous.get("region") or ""),
        "signature": str(identity.get("signature") or previous.get("signature") or ""),
        "profile": profile,
        "proxy": _normalize_rdv_mapping_entry({"proxy": proxy or {}})["proxy"],
    }
    _save_rdv_mapping_doc(data)
    return _current_rdv_mapping()


def _delete_rdv_mapping(rdv: str) -> dict[str, dict]:
    rdv = _validate_protocol_rdv(rdv)
    data = _load_rdv_mapping_doc()
    scopes = data.setdefault("scopes", {})
    for scoped in scopes.values():
        if isinstance(scoped, dict):
            scoped.pop(rdv, None)
    _save_rdv_mapping_doc(data)
    return _current_rdv_mapping()


def _parse_protocol_time(value: Any) -> float:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _protocol_profile_logged_in(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if not data.get("ok"):
        return False
    if str(data.get("state") or "").strip() != "logged_in":
        return False
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    base = profile.get("base_response") if isinstance(profile.get("base_response"), dict) else {}
    ret = base.get("ret", base.get("ret_signed", 0))
    try:
        return int(ret or 0) == 0
    except Exception:
        return True


async def _protocol_profile_for_session(session_id: str, *, force: bool = False) -> dict:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    now = time.time()
    cached = _PROTOCOL_ACCOUNT_PROFILE_CACHE.get(session_id)
    if cached and not force and now - cached[1] < _PROTOCOL_ACCOUNT_PROFILE_TTL_SEC:
        return cached[0]
    data = await protocol_api.get_profile(session_id)
    if isinstance(data, dict) and data.get("ok"):
        _PROTOCOL_ACCOUNT_PROFILE_CACHE[session_id] = (data, now)
    return data if isinstance(data, dict) else {}


def _protocol_status_from_session(item: dict) -> str:
    state = str(item.get("state") or "").strip()
    if item.get("online") or state == "logged_in":
        return "3"
    if (
        item.get("login_started")
        or item.get("qrcode_ready")
        or state in {"login_starting", "qrcode_ready", "polling", "scanned", "confirmed", "authenticating"}
    ):
        return "2"
    return ""


def _protocol_message_from_session(item: dict, status: str) -> str:
    for key in ("error", "post_login_error"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    state = str(item.get("state") or "").strip()
    if status == "3":
        return "已登录"
    if item.get("qrcode_ready"):
        return "二维码待确认"
    if status == "2":
        return "登录中"
    return state or "未登录"


_PROTOCOL_ACCOUNT_TAB_EXCLUDED_STATES = frozenset({"failed"})


def _protocol_session_visible_in_account_tabs(item: dict) -> bool:
    return str(item.get("state") or "").strip().lower() not in _PROTOCOL_ACCOUNT_TAB_EXCLUDED_STATES


def _protocol_session_tab_key(item: dict) -> str:
    rdv = str(item.get("rdv") or "").strip().lower()
    if rdv:
        return f"rdv:{rdv}"
    return f"session:{str(item.get('session_id') or '').strip()}"


def _protocol_session_tab_rank(item: dict) -> tuple[int, float, float]:
    state = str(item.get("state") or "").strip().lower()
    return (
        1 if state == "logged_in" or item.get("online") else 0,
        _parse_protocol_time(item.get("updated_at")),
        _parse_protocol_time(item.get("created_at")),
    )


def _dedupe_protocol_session_tabs(items: list[dict]) -> list[dict]:
    selected: dict[str, dict] = {}
    for item in items:
        key = _protocol_session_tab_key(item)
        current = selected.get(key)
        if current is None or _protocol_session_tab_rank(item) > _protocol_session_tab_rank(current):
            selected[key] = item
    return list(selected.values())


def _protocol_account_from_identity(session_id: str, item: dict, identity: dict) -> dict:
    wxid = str(identity.get("wxid") or "").strip()
    nickname = str(identity.get("nickname") or "").strip()
    avatar = str(identity.get("avatar") or "").strip()
    status = _protocol_status_from_session(item)
    created_at = _parse_protocol_time(item.get("created_at")) or time.time()
    updated_at = _parse_protocol_time(item.get("updated_at")) or created_at
    profile = identity.get("profile") if isinstance(identity.get("profile"), dict) else {}
    return {
        "id": session_id,
        "account_id": wxid or session_id,
        "wxid": wxid,
        "nickname": nickname,
        "avatar": avatar,
        "phone": identity.get("phone", ""),
        "region": identity.get("region", ""),
        "signature": identity.get("signature", ""),
        "wechat_account": identity.get("account", ""),
        "profile": profile,
        "server_port": str(item.get("start_port") or config.HOOK_PORT),
        "peer": f"{config.HOOK_HOST}:{config.HOOK_PORT}",
        "connected_at": created_at,
        "last_seen_at": updated_at,
        "pending": 0,
        "initialized": status == "3",
        "login_status": status,
        "login_message": _protocol_message_from_session(item, status),
        "login_status_updated_at": updated_at,
        "active": session_id == _current_agent_id(),
        "source": "protocol",
        "session_id": session_id,
        "protocol_state": str(item.get("state") or "").strip().lower(),
        "rdv": str(item.get("rdv") or ""),
        "start_port": item.get("start_port") or config.HOOK_PORT,
        "callback_url": str(item.get("callback_url") or ""),
        "qrcode_ready": bool(item.get("qrcode_ready")),
    }


async def _protocol_session_to_account(item: dict, rdv_mapping: dict[str, dict] | None = None) -> dict:
    session_id = str(item.get("session_id") or "").strip()
    profile_raw: dict = {}
    if session_id and _protocol_status_from_session(item) == "3":
        try:
            profile_raw = await _protocol_profile_for_session(session_id)
        except Exception as e:
            _log(f"[PROTO_ACCOUNT] getprofile failed session={session_id}: {type(e).__name__}: {e}")
    identity = _self_identity_from_response(profile_raw, agent_id=session_id)
    rdv = str(item.get("rdv") or "").strip().lower()
    mapped = _normalize_rdv_mapping_entry((rdv_mapping or {}).get(rdv, {}))
    live_wxid = _stable_protocol_wxid(identity.get("wxid"), session_id)
    mapped_wxid = _stable_protocol_wxid(mapped.get("wxid"))
    identity["wxid"] = live_wxid
    if not live_wxid and mapped_wxid:
        for key in ("wxid", "nickname", "avatar", "account", "phone", "region", "signature"):
            if not identity.get(key) and mapped.get(key):
                identity[key] = mapped[key]
        if not identity.get("profile") and mapped.get("profile"):
            identity["profile"] = mapped["profile"]
    elif live_wxid and live_wxid == mapped_wxid:
        for key in ("nickname", "avatar", "account", "phone", "region", "signature"):
            if not identity.get(key) and mapped.get(key):
                identity[key] = mapped[key]
        if not identity.get("profile") and mapped.get("profile"):
            identity["profile"] = mapped["profile"]
    return _protocol_account_from_identity(session_id, item, identity)


def _protocol_account_from_mapping(rdv: str, entry: dict) -> dict:
    normalized = _normalize_rdv_mapping_entry(entry)
    wxid = str(normalized.get("wxid") or "").strip()
    nickname = str(normalized.get("nickname") or "").strip()
    avatar = str(normalized.get("avatar") or "").strip()
    profile = normalized.get("profile") if isinstance(normalized.get("profile"), dict) else {}
    account_id = wxid or rdv
    try:
        start_port = int(normalized.get("port") or config.HOOK_PORT)
    except Exception:
        start_port = config.HOOK_PORT
    return {
        "id": f"rdv:{rdv}",
        "account_id": account_id,
        "wxid": wxid,
        "nickname": nickname or wxid or f"RDV {rdv}",
        "avatar": avatar,
        "phone": normalized.get("phone", ""),
        "region": normalized.get("region", ""),
        "signature": normalized.get("signature", ""),
        "wechat_account": normalized.get("account", ""),
        "profile": profile,
        "server_port": str(normalized.get("port") or config.HOOK_PORT),
        "peer": "saved rdv",
        "connected_at": 0,
        "last_seen_at": 0,
        "pending": 0,
        "initialized": False,
        "login_status": "",
        "login_message": "可使用老号登录",
        "login_status_updated_at": 0,
        "active": False,
        "source": "protocol",
        "session_id": "",
        "rdv": rdv,
        "start_port": start_port,
        "callback_url": "",
        "qrcode_ready": False,
        "saved_mapping": True,
        "proxy": normalized.get("proxy") if isinstance(normalized.get("proxy"), dict) else {},
    }


async def _list_protocol_accounts() -> dict:
    sessions_raw = await protocol_api.get_session_list()
    items = sessions_raw.get("sessions") if isinstance(sessions_raw, dict) else []
    if not isinstance(items, list):
        items = []
    if access_store.current_user.get():
        items = [item for item in items if isinstance(item, dict) and access_store.owns(item.get('session_id'))]
        sessions_raw = {'sessions': items, 'ok': bool(sessions_raw.get('ok', True))}
    visible_items = _dedupe_protocol_session_tabs([
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("session_id") or "").strip()
        and _protocol_session_visible_in_account_tabs(item)
    ])
    rdv_mapping = _current_rdv_mapping()
    accounts = await asyncio.gather(*(
        _protocol_session_to_account(item, rdv_mapping)
        for item in visible_items
    )) if visible_items else []
    accounts.sort(key=lambda row: (row.get("login_status") != "3", row.get("connected_at") or 0), reverse=False)
    active_id = _current_agent_id() if any(row.get("id") == _current_agent_id() for row in accounts) else ""
    return {
        "active_id": active_id,
        "accounts": accounts,
        "source": "protocol",
        "upstream": sessions_raw,
    }


async def _activate_protocol_account(session_id: str) -> dict:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id 不能为空"}
    profile_raw = await _protocol_profile_for_session(session_id, force=True)
    if not _protocol_profile_logged_in(profile_raw):
        return {
            "ok": False,
            "error": "wechat login status is not ready",
            "login_status": {
                "status": "",
                "message": str(profile_raw.get("error") or profile_raw.get("state") or "等待微信登录"),
                "wxid": "",
                "nickname": "",
                "avatar": "",
            },
            "upstream": profile_raw,
        }
    _activate_runtime(session_id)
    _state()["self_info"] = profile_raw
    identity = _self_identity_from_response(profile_raw, agent_id=session_id)
    wxid = identity["wxid"]
    if wxid:
        _self_wxid_to_agent_id[wxid] = session_id
        _put_self_info_field("wxid", wxid)
        _put_self_info_field("selfwxid", wxid)
    owner_wxid = _contact_owner_wxid(wxid)
    _load_session_cache_into_state(owner_wxid)
    _state()["contacts"] = _contacts_snapshot_from_db(owner_wxid)
    _state()["initialized"] = True
    item = {
        "session_id": session_id,
        "state": "logged_in",
        "online": True,
        "created_at": time.time(),
        "updated_at": time.time(),
        "start_port": config.HOOK_PORT,
    }
    account = _protocol_account_from_identity(session_id, item, identity)
    return {"ok": True, "active_id": session_id, "account": account}


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


async def _probe_local_hook_account() -> dict[str, Any]:
    """Probe the configured local Hook HTTP port and build its account card."""
    global _local_hook_last_seen_at
    state = _state() if _current_agent_id() == _LOCAL_HOOK_ACCOUNT_ID else _runtime_for(_LOCAL_HOOK_ACCOUNT_ID)["app_state"]

    async with _LOCAL_HOOK_PROBE_LOCK:
        probe_started_at = time.perf_counter()
        checked_at = time.time()
        port_alive = False
        parsed = {"status": "", "message": "", "wxid": "", "nickname": ""}
        probe_error = ""

        try:
            raw_status = await wechat_api.is_login_status()
            parsed = _login_status_from_response(raw_status)
            port_alive = True
            _local_hook_last_seen_at = checked_at
        except Exception as exc:
            probe_error = f"{type(exc).__name__}: {exc}"

        profile_raw = state.get("self_info") if isinstance(state.get("self_info"), dict) else {}
        cached_identity = _self_identity_from_response(
            profile_raw,
            agent_id=_LOCAL_HOOK_ACCOUNT_ID,
            current_wxid=parsed["wxid"],
        )
        cached_wxid = str(cached_identity.get("wxid") or "").strip()
        has_complete_profile = bool(
            cached_wxid
            and cached_identity.get("nickname")
            and cached_identity.get("avatar")
        )
        if not port_alive or parsed["status"] != "3":
            _agent_self_profile_refreshed.discard(_LOCAL_HOOK_ACCOUNT_ID)
        needs_profile = (
            port_alive
            and parsed["status"] == "3"
            and (
                _LOCAL_HOOK_ACCOUNT_ID not in _agent_self_profile_refreshed
                or not has_complete_profile
                or (parsed["wxid"] and cached_wxid != parsed["wxid"])
            )
        )
        if needs_profile:
            try:
                fetched_profile = await wechat_api.get_self_info()
                fetched_identity = _self_identity_from_response(
                    fetched_profile,
                    agent_id=_LOCAL_HOOK_ACCOUNT_ID,
                    current_wxid=parsed["wxid"],
                )
                if not isinstance(fetched_profile, dict) or not any(
                    fetched_identity.get(field) for field in ("wxid", "nickname", "avatar", "account")
                ):
                    raise ValueError("empty profile response")
                profile_raw = fetched_profile
                state["self_info"] = fetched_profile
                _agent_self_profile_refreshed.add(_LOCAL_HOOK_ACCOUNT_ID)
                profile_wxid = str(fetched_identity.get("wxid") or parsed["wxid"] or "").strip()
                profile_avatar = str(fetched_identity.get("avatar") or "").strip()
                if profile_wxid and profile_avatar:
                    state.setdefault("avatar_urls", {})[profile_wxid] = profile_avatar
                _log(f"[LOCAL_HOOK] GetSelfProfile loaded wxid={profile_wxid or '-'}")
            except Exception as exc:
                probe_error = probe_error or f"GetSelfProfile {type(exc).__name__}: {exc}"
                _log(f"[LOCAL_HOOK] GetSelfProfile failed: {type(exc).__name__}: {exc}")

        identity = _self_identity_from_response(
            profile_raw,
            agent_id=_LOCAL_HOOK_ACCOUNT_ID,
            current_wxid=parsed["wxid"],
        )
        if not port_alive or parsed['status'] != '3':
            local_owner = access_store.owner(_LOCAL_HOOK_ACCOUNT_ID)
            remembered = access_store.profile(_LOCAL_HOOK_ACCOUNT_ID, local_owner) if local_owner else None
            for field in ('wxid', 'nickname', 'avatar'):
                if not identity.get(field) and (remembered or {}).get(field):
                    identity[field] = remembered[field]
        wxid = str(identity.get("wxid") or parsed["wxid"] or "").strip()
        nickname = str(identity.get("nickname") or parsed["nickname"] or ("" if wxid else "本地微信")).strip()
        if wxid:
            local_owner = access_store.owner(_LOCAL_HOOK_ACCOUNT_ID)
            if local_owner and not access_store.claim('wxid:' + wxid, local_owner):
                raise HTTPException(403, '该微信已属于另一密钥用户，请使用原密钥登录')
            _remember_hook_identity({'id': 'default', 'port': config.HOOK_PORT, 'rdv': config.RDV,
                'pid': '', 'transport': 'http', 'host': config.HOOK_HOST, 'mode': config.LOGIN_MODE}, 'default', wxid, local_owner, identity,
                logged_in=port_alive and parsed['status'] == '3')
            _self_wxid_to_agent_id[wxid] = _LOCAL_HOOK_ACCOUNT_ID

        status = parsed["status"] if port_alive else ""
        login_message = parsed["message"] if port_alive else "本地 Hook 接口不可用"
        if port_alive and not login_message:
            login_message = "已登录" if status == "3" else "接口已连接"
        probe_ms = (time.perf_counter() - probe_started_at) * 1000
        peer = f"{config.HOOK_HOST}:{config.HOOK_PORT}"
        return {
            "id": _LOCAL_HOOK_ACCOUNT_ID,
            "account_id": wxid or _LOCAL_HOOK_ACCOUNT_ID,
            "wxid": wxid,
            "nickname": nickname,
            "avatar": str(identity.get("avatar") or ""),
            "phone": str(identity.get("phone") or ""),
            "region": str(identity.get("region") or ""),
            "signature": str(identity.get("signature") or ""),
            "wechat_account": str(identity.get("account") or ""),
            "profile": identity.get("profile") if isinstance(identity.get("profile"), dict) else {},
            "peer": peer,
            "server_port": str(config.HOOK_PORT),
            "start_port": config.HOOK_PORT,
            "api_host": config.HOOK_HOST,
            "api_port": config.HOOK_PORT,
            "transport": "http",
            "connected": port_alive,
            "port_alive": port_alive,
            "probe_latency_ms": round(probe_ms, 1),
            "probe_error": probe_error,
            "connected_at": _LOCAL_HOOK_STARTED_AT,
            "last_seen_at": _local_hook_last_seen_at,
            "pending": 0,
            "initialized": bool(status == "3" and state.get("initialized")),
            "login_status": status,
            "login_message": login_message,
            "login_status_updated_at": checked_at,
            "active": _current_agent_id() == _LOCAL_HOOK_ACCOUNT_ID,
            "source": "hook",
        }


async def _refresh_agent_login_status(agent_id: str) -> dict[str, str]:
    """Poll /IsLoginStatus and update account-card metadata.

    Status 3 is the only state that may continue into expensive initialization.
    GetSelfProfile is only called after status 3. Earlier login states can
    leave WeChat half-ready and should not receive profile/detail calls.
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

    entered_logged_in = status == "3" and previous_status != "3"
    needs_self_profile = status == "3" and (
        entered_logged_in
        or (
            agent_id not in _agent_self_profile_refreshed
            and (not wxid or not nickname or not current_avatar or not current_account)
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
            _agent_self_profile_refreshed.add(agent_id)
        except Exception as e:
            _log(f"[LOGIN_STATUS] GetSelfProfile failed agent={agent_id}: {type(e).__name__}: {e}")

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
        _agent_self_profile_refreshed.discard(agent_id)
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
    if not isinstance(_state().get("self_info"), dict):
        _state()["self_info"] = {"data": {}}
    self_info = _state()["self_info"]
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

    for info in _contacts_from_batch_brief_response(data):
        if not isinstance(info, dict):
            continue
        item_wxid = _contact_profile_wxid(info)
        if item_wxid != wxid:
            continue
        return {
            "name": _contact_profile_explicit_name(info),
            "avatar": _contact_profile_avatar(info),
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
            _messages().set_contact(wxid, name=name, avatar=avatar)
        if avatar:
            _put_self_info_field("head_big", avatar)
            _state().setdefault("avatar_urls", {})[wxid] = avatar
            _messages().set_contact(wxid, name=name, avatar=avatar)

    return {"name": name, "avatar": avatar}


async def _refresh_account_card(agent_id: str) -> None:
    agent_id = str(agent_id or "").strip()
    if config.IS_PROTOCOL:
        _account_card_refreshing.discard(agent_id)
        return
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
    if config.IS_PROTOCOL or config.IS_LOCAL_HOOK:
        return
    now = time.time()
    for account in agent_manager.agents():
        agent_id = str(account.get("id") or "")
        if not agent_id or agent_id in _account_card_refreshing or agent_id in _initializing_agents:
            continue
        if now < _agent_polling_paused_until.get(agent_id, 0.0):
            continue
        last = _account_card_refresh_at.get(agent_id, 0.0)
        status = str(account.get("login_status") or "").strip()
        interval = (
            _ACCOUNT_CARD_REFRESH_LOGGED_IN_INTERVAL_SEC
            if status == "3"
            else _ACCOUNT_CARD_REFRESH_FAST_INTERVAL_SEC
        )
        if now - last < interval:
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
# Populated lazily via /GetContact for explicit profile opens and detail refreshes.
_CONTACT_PROFILE_CACHE: dict[str, dict] = {}  # owner::wxid -> {"profile": dict, "ts": float}
_CONTACT_PROFILE_CACHE_TTL_SEC = 24 * 60 * 60
_CONTACT_PROFILE_LOCK = asyncio.Lock()
def _hook_api_parallelism(limit_override: int | None = None) -> int:
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



# ─── Contact label cache ───────────────────────────────────────────
# Maps WeChat label ids from GetContact.LabelTag to readable names.
_CONTACT_LABEL_CACHE: dict[str, object] = {"map": {}, "ts": 0.0}
_CONTACT_LABEL_CACHE_TTL_SEC = 24 * 60 * 60
_CONTACT_LABEL_LOCK = asyncio.Lock()


def _get_self_wxid() -> str:
    self_info = _state().get("self_info")
    if not isinstance(self_info, dict):
        return ""
    data = self_info.get("data", {})
    if isinstance(data, dict) and data.get("wxid"):
        return data.get("wxid", "")
    profile = self_info.get("profile", {})
    if isinstance(profile, dict):
        wxid = (
            profile.get("wxid")
            or profile.get("user_name")
            or profile.get("UserName")
            or profile.get("selfwxid")
        )
        if wxid:
            return str(wxid)
    return self_info.get("wxid", "")


def _contact_owner_wxid(self_wxid: str = "") -> str:
    owner = str(self_wxid or _get_self_wxid() or "").strip()
    if owner:
        return owner
    agent_id = str(_current_agent_id() or agent_manager.active_id() or "").strip()
    agent = agent_manager.get_agent(agent_id) if agent_id else None
    agent_wxid = str((agent or {}).get("wxid") or (agent or {}).get("account_id") or "").strip()
    if agent_wxid and agent_wxid != agent_id:
        return agent_wxid
    return f"agent:{agent_id}" if agent_id else "default"


def _contact_cache_key(wxid: str, owner_wxid: str = "") -> str:
    return f"{_contact_owner_wxid(owner_wxid)}::{str(wxid or '').strip()}"


def _protocol_history_available(owner_wxid: str = "") -> bool:
    if not config.IS_PROTOCOL:
        return True
    return _cache().has_message_history(owner_wxid=_contact_owner_wxid(owner_wxid))


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
    if t == "50":
        return "[语音聊天]"
    if t in ("10000", "10002"):
        return "[系统消息]"
    return (content or "")[:30] or "[消息]"


def _time_to_hhmm(time_text: str) -> str:
    if not time_text:
        return ""
    if " " in time_text:
        return (time_text.split(" ", 1)[1] or "")[:5]
    return time_text[:5]


def _callback_value(msg: dict, *keys: str) -> str:
    """Read a callback field while tolerating Hook naming variants."""
    for key in keys:
        value = msg.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _xml_tag_value(content: str, tag_name: str) -> str:
    """Extract a simple text field from Hook XML when a top-level field is absent."""
    if not content:
        return ""
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, TypeError):
        return ""
    wanted = str(tag_name or "").casefold()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() == wanted:
            value = str(element.text or "").strip()
            if value:
                return value
    return ""


def _normalize_callback_message(msg: dict, sendorrecv: str, self_wxid: str) -> tuple[str, dict] | tuple[None, None]:
    chat_id = ""
    from_gid = _callback_value(
        msg,
        "fromgid",
        "from_gid",
        "groupid",
        "group_id",
        "chatroomid",
        "chatroom_id",
    )
    from_id = _callback_value(
        msg,
        "fromid",
        "from_id",
        "sender_wxid",
        "senderid",
        "sender_id",
    )
    to_id = _callback_value(msg, "toid", "to_id", "receiver_wxid", "receiver_id")
    # Some Hook builds omit fromid for app/file messages but include it in XML.
    if from_gid and (not from_id or from_id == from_gid):
        from_id = _xml_tag_value(str(msg.get("msg", "") or ""), "fromusername") or from_id
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
    message_direction = _callback_value(msg, "sendorrecv", "send_or_recv") or str(sendorrecv or "")
    is_self_sent = (
        message_direction == "1" or
        (from_id == self_wxid and self_wxid and not from_gid)
    )
    # For group messages sent from mobile: fromid=self in a group context
    if from_gid and from_id == self_wxid and self_wxid:
        is_self_sent = True

    callback_path = str(msg.get("path", "") or "").strip()
    typed_path: dict[str, str] = {}
    if callback_path:
        path_field = {
            "3": "img_path",
            "34": "voice_path",
            "43": "video_path",
            "62": "video_path",
            "47": "gif_path",
            "49": "file_path",
        }.get(msgtype)
        if path_field:
            typed_path[path_field] = callback_path

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
        "sendorrecv": "1" if is_self_sent else message_direction,
        "isSender": 1 if is_self_sent else 0,
        "img_path": msg.get("img_path") or typed_path.get("img_path"),
        "img_len": msg.get("img_len"),
        "video_path": msg.get("video_path") or typed_path.get("video_path"),
        "video_len": msg.get("video_len"),
        "voice_len": msg.get("voice_len"),
        "voice_hex": msg.get("voice_hex"),
        "voice_data": msg.get("voice_data"),
        "voice_path": msg.get("voice_path") or typed_path.get("voice_path"),
        "clientmsgid": msg.get("clientmsgid"),
        "gif_path": msg.get("gif_path") or typed_path.get("gif_path"),
        "gif_len": msg.get("gif_len"),
        "file_path": msg.get("file_path") or typed_path.get("file_path"),
        "file_len": msg.get("file_len"),
        "info": msg.get("info"),
        "msgsource": msg.get("msgsource"),
        "atuserlist": (
            msg.get("atuserlist")
            or msg.get("atUserList")
            or msg.get("at_user_list")
            or msg.get("atlist")
            or msg.get("at_list")
        ),
    }
    return chat_id, normalized


def _is_callback_status_echo(msg: dict, sendorrecv: str, self_wxid: str) -> bool:
    """Filter Hook status callbacks that are not user-visible messages."""
    if not isinstance(msg, dict):
        return False

    msgtype = str(msg.get("msgtype", "") or "")
    content = str(msg.get("msg", "") or "").strip()
    if (
        config.IS_HOOK
        and msgtype == "1"
        and content.casefold() == "system message revoke/voice call"
    ):
        return True

    from_id = str(msg.get("fromid", "") or "")
    from_gid = str(msg.get("fromgid", "") or "")
    is_self_echo = (
        str(sendorrecv) == "1"
        or (self_wxid and from_id == self_wxid and not from_gid)
    )
    if not is_self_echo:
        return False

    # A send-success callback can carry the real message ID and local media
    # path. Keep it as a message update so history refreshes do not lose the
    # path supplied by RecvType=1.
    if any(msg.get(key) for key in ("path", "img_path", "video_path", "file_path", "gif_path", "voice_path")):
        return False

    if msgtype == "1" and not content:
        return True
    if msgtype == "3" and content in {"PC发图片消息成功", "发图片消息成功"}:
        return True
    return False


def _is_preliminary_local_hook_file_callback(msg: dict) -> bool:
    """Identify RecvType=1's pathless notification before a file finishes downloading."""
    if (
        not config.IS_LOCAL_HOOK
        or config.RECV_TYPE != 1
        or not isinstance(msg, dict)
        or str(msg.get("msgtype", "") or "") != "49"
    ):
        return False
    if any(msg.get(key) for key in ("path", "file_path")):
        return False

    root = _xml_root(str(msg.get("msg", "") or ""))
    if root is None:
        return False
    appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
    return appmsg is not None and str(appmsg.findtext("type") or "").strip() == "74"


def _is_protocol_internal_callback_message(msg: dict) -> bool:
    """Filter protocol sync events that must not become chat sessions."""
    if not config.IS_PROTOCOL or not isinstance(msg, dict):
        return False

    msgtype = str(msg.get("msgtype", "") or "")
    content = str(msg.get("msg", "") or "").strip()
    if msgtype == "9999" and content.casefold() == "chatroom member changed":
        return True

    from_id = str(msg.get("fromid", "") or "").strip().casefold()
    return msgtype == "10002" and from_id == "weixin" and content.casefold().startswith("<sysmsg")


def _recalled_message_id(content: str) -> str:
    match = re.search(r"<newmsgid>\s*(?:<!\[CDATA\[)?([^<\]\s]+)", str(content or ""), re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _message_struct_content(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    content = str(payload.get("content") or data.get("content") or "")
    msgtype = str(payload.get("msgtype") or data.get("msgtype") or "")
    return content, msgtype


def _is_editable_recalled_text(content: str, msgtype: str) -> bool:
    text = str(content or "").strip()
    lowered = text.casefold()
    return (
        str(msgtype or "") == "1"
        and bool(text)
        and "<revokemsg" not in lowered
        and lowered != "system message revoke/voice call"
    )


def _cached_recalled_text(chat_id: str, msg_id: str, timestamp: int = 0) -> str:
    if not chat_id or not msg_id:
        return ""

    messages: list[dict] = []
    try:
        messages.extend(_messages().get_messages(chat_id))
    except Exception:
        pass
    try:
        messages.extend(_cache().get_messages(
            chat_id,
            200,
            owner_wxid=_contact_owner_wxid(),
        ))
    except Exception:
        pass

    for message in messages:
        if str(message.get("id", "") or "") != msg_id:
            continue
        content = str(message.get("msg", "") or "")
        if _is_editable_recalled_text(content, str(message.get("msgtype", "") or "")):
            return content

    recall_timestamp = int(timestamp or 0)
    for message in messages:
        if _recalled_message_id(str(message.get("msg", "") or "")) != msg_id:
            continue
        recall_timestamp = max(
            recall_timestamp,
            int(message.get("timestamp") or message.get("time_unix") or 0),
        )

    candidates: list[dict] = []
    for message in messages:
        content = str(message.get("msg", "") or "")
        if not _is_editable_recalled_text(content, str(message.get("msgtype", "") or "")):
            continue
        if int(message.get("isSender") or 0) != 1 and str(message.get("sendorrecv", "") or "") != "1":
            continue
        message_timestamp = int(message.get("timestamp") or message.get("time_unix") or 0)
        if recall_timestamp and not (recall_timestamp - 120 <= message_timestamp <= recall_timestamp + 2):
            continue
        candidates.append(message)

    if not candidates:
        return ""
    candidates.sort(key=lambda message: int(message.get("timestamp") or message.get("time_unix") or 0), reverse=True)
    return str(candidates[0].get("msg", "") or "")


def _store_message_and_session(chat_id: str, msg: dict) -> dict:
    _messages().add_message(chat_id, msg)
    preview = _format_preview(str(msg.get("msgtype", "")), str(msg.get("msg", "") or ""))
    # For group chats, prefix the preview with the sender's nickname
    if "@chatroom" in chat_id:
        if msg.get("isSender") == 1 or str(msg.get("sendorrecv", "")) == "1":
            preview = f"我: {preview}"
        else:
            sender_wxid = str(msg.get("fromid", "") or "")
            if sender_wxid:
                sender_name = (
                    _messages().get_contact(sender_wxid).get("name", "")
                    or _CONTACT_BRIEF_CACHE.get(_contact_cache_key(sender_wxid), {}).get("name", "")
                    or sender_wxid
                )
                preview = f"{sender_name}: {preview}"
    msg_timestamp = int(msg.get("time_unix") or msg.get("timestamp") or int(time.time()))
    time_str = _time_to_hhmm(str(msg.get("time", "") or ""))
    is_recv = str(msg.get("sendorrecv", "")) == "2"
    snapshot = _messages().update_session(
        chat_id,
        last_msg=preview,
        last_time=time_str,
        unread_delta=1 if is_recv else 0,
        last_timestamp=msg_timestamp,
    )
    _state()["last_messages"][chat_id] = {
        "content": str(msg.get("msg", "") or ""),
        "type": str(msg.get("msgtype", "") or "1"),
        "is_sender": 1 if str(msg.get("sendorrecv", "")) == "1" else 0,
        "time": msg_timestamp,
    }
    try:
        owner_wxid = _contact_owner_wxid()
        _cache().upsert_messages(chat_id, [msg], owner_wxid=owner_wxid)
        _cache().upsert_session_preview(
            chat_id,
            nickname=_messages().get_contact(chat_id).get("name", ""),
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


_MEDIA_FIELDS_TO_PRESERVE = (
    "img_path", "img_len", "db_image_id",
    "video_path", "video_len",
    "file_path", "file_len",
    "gif_path", "gif_len",
    "voice_hex", "voice_data", "voice_len", "voice_path", "clientmsgid",
)


def _merge_message_media_fields(message: dict, fallback: dict | None) -> dict:
    merged = dict(message)
    if not isinstance(fallback, dict):
        return merged
    for key in _MEDIA_FIELDS_TO_PRESERVE:
        if not merged.get(key) and fallback.get(key):
            merged[key] = fallback[key]
    return merged


def _find_history_image_media(message: dict, candidates: list[dict]) -> dict | None:
    if str(message.get("msgtype", "")) != "3" or message.get("img_path"):
        return None
    content = str(message.get("msg", "") or "")
    length_match = re.search(r'\blength="(\d+)"', content)
    expected_length = int(length_match.group(1)) if length_match else 0
    md5_match = re.search(r'\bmd5="([a-fA-F0-9]{32})"', content)
    expected_md5 = md5_match.group(1).lower() if md5_match else ""
    timestamp = int(message.get("timestamp") or message.get("time_unix") or 0)
    direction = str(message.get("sendorrecv", "") or "")
    best: tuple[int, dict] | None = None

    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("img_path"):
            continue
        if str(candidate.get("msgtype", "")) != "3":
            continue
        if str(candidate.get("sendorrecv", "") or "") != direction:
            continue
        candidate_ts = int(candidate.get("timestamp") or candidate.get("time_unix") or 0)
        delta = abs(timestamp - candidate_ts) if timestamp and candidate_ts else 999999
        if delta > 120:
            continue

        candidate_path = str(candidate.get("img_path") or "")
        candidate_content = str(candidate.get("msg", "") or "").strip().lower()
        same_md5 = bool(expected_md5 and candidate_content == expected_md5)
        same_size = False
        if expected_length and candidate_path:
            try:
                same_size = os.path.isfile(candidate_path) and os.path.getsize(candidate_path) == expected_length
            except OSError:
                same_size = False
        if expected_length and not same_size and not same_md5:
            continue
        if not expected_length and not same_md5 and delta > 2:
            continue

        score = (1000 if same_md5 else 0) + (500 if same_size else 0) - delta
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def _find_history_file_media(message: dict, candidates: list[dict]) -> dict | None:
    """Match RecvType=1 media callbacks whose ID differs from the MSG database row."""
    msg_type = str(message.get("msgtype", "") or "")
    path_field = {
        "34": "voice_path",
        "43": "video_path",
        "62": "video_path",
        "47": "gif_path",
        "49": "file_path",
    }.get(msg_type)
    if not path_field or message.get(path_field):
        return None

    content = str(message.get("msg", "") or "")
    timestamp = int(message.get("timestamp") or message.get("time_unix") or 0)
    direction = str(message.get("sendorrecv", "") or "")
    length_match = re.search(r'(?:\blength="|<totallen>)(\d+)', content, re.IGNORECASE)
    expected_length = int(length_match.group(1)) if length_match else 0
    title_match = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", content, re.IGNORECASE | re.DOTALL)
    expected_title = html.unescape(title_match.group(1).strip()) if title_match else ""
    best: tuple[int, dict] | None = None

    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get(path_field):
            continue
        if str(candidate.get("msgtype", "") or "") != msg_type:
            continue
        if str(candidate.get("sendorrecv", "") or "") != direction:
            continue
        candidate_ts = int(candidate.get("timestamp") or candidate.get("time_unix") or 0)
        delta = abs(timestamp - candidate_ts) if timestamp and candidate_ts else 999999
        if delta > 120:
            continue

        candidate_path = str(candidate.get(path_field) or "")
        same_name = bool(expected_title and os.path.basename(candidate_path).casefold() == expected_title.casefold())
        same_size = False
        if expected_length:
            try:
                same_size = os.path.isfile(candidate_path) and os.path.getsize(candidate_path) == expected_length
            except OSError:
                pass
        if expected_title and not same_name and not same_size:
            continue
        if expected_length and not same_size and not same_name:
            continue
        if not expected_title and not expected_length and delta > 2:
            continue

        score = (1000 if same_name else 0) + (500 if same_size else 0) - delta
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def _normalize_history_rows(wxid: str, rows: list[dict]) -> list[dict]:
    self_wxid = _get_self_wxid()
    is_group = "@chatroom" in wxid
    existing_messages: list[dict] = []
    try:
        existing_messages.extend(_messages().get_messages(wxid))
    except Exception:
        pass
    try:
        existing_messages.extend(
            _cache().get_messages(wxid, 200, owner_wxid=_contact_owner_wxid())
        )
    except Exception:
        pass
    row_timestamps = [
        int(row.get("CreateTime") or 0)
        for row in rows
        if isinstance(row, dict) and row.get("CreateTime")
    ]
    if row_timestamps:
        try:
            existing_messages.extend(_cache().get_media_messages(
                wxid,
                min(row_timestamps) - 120,
                max(row_timestamps) + 120,
                owner_wxid=_contact_owner_wxid(),
            ))
        except Exception:
            pass
    existing_by_id: dict[str, dict] = {}
    for existing in existing_messages:
        if not isinstance(existing, dict):
            continue
        existing_id = str(existing.get("id", "") or "")
        if existing_id and existing_id not in existing_by_id:
            existing_by_id[existing_id] = existing
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
        normalized = {
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
        }
        normalized = _merge_message_media_fields(
            normalized,
            existing_by_id.get(str(normalized.get("id", ""))),
        )
        media_fallback = _find_history_image_media(normalized, existing_messages)
        normalized = _merge_message_media_fields(normalized, media_fallback)
        file_fallback = _find_history_file_media(normalized, existing_messages)
        normalized = _merge_message_media_fields(normalized, file_fallback)
        out.append(normalized)
    out.sort(key=lambda m: (int(m.get("timestamp") or 0), str(m.get("id", ""))))
    return out


def _sync_session_preview_from_message(wxid: str, msg: dict, *, owner_wxid: str = "") -> None:
    wxid = str(wxid or "").strip()
    if not wxid or not isinstance(msg, dict):
        return
    owner = _contact_owner_wxid(owner_wxid)
    msg_timestamp = int(msg.get("timestamp") or msg.get("time_unix") or 0)
    current_last = (_state().get("last_messages") or {}).get(wxid) or {}
    current_time = int(current_last.get("time") or 0)
    if msg_timestamp and current_time and msg_timestamp < current_time:
        return

    preview = _format_preview(str(msg.get("msgtype", "") or "1"), str(msg.get("msg", "") or ""))
    if "@chatroom" in wxid:
        if int(msg.get("isSender") or 0) == 1 or str(msg.get("sendorrecv", "") or "") == "1":
            preview = f"\u6211: {preview}"
        else:
            sender_wxid = str(msg.get("fromid", "") or "").strip()
            if sender_wxid:
                sender_name = (
                    _messages().get_contact(sender_wxid).get("name", "")
                    or _CONTACT_BRIEF_CACHE.get(_contact_cache_key(sender_wxid, owner), {}).get("name", "")
                    or sender_wxid
                )
                preview = f"{sender_name}: {preview}"

    last_message = {
        "content": str(msg.get("msg", "") or ""),
        "type": str(msg.get("msgtype", "") or "1"),
        "is_sender": 1 if str(msg.get("sendorrecv", "") or "") == "1" or int(msg.get("isSender") or 0) == 1 else 0,
        "time": msg_timestamp,
    }
    _state()["last_messages"][wxid] = last_message
    _cache().upsert_last_messages({wxid: last_message}, owner_wxid=owner)
    _cache().upsert_session_preview(
        wxid,
        nickname=_messages().get_contact(wxid).get("name", ""),
        content=preview,
        msg_type=str(msg.get("msgtype", "") or "1"),
        timestamp=msg_timestamp,
        unread_delta=0,
        owner_wxid=owner,
    )
    _load_session_cache_into_state(owner)


# ─── Startup / Shutdown ────────────────────────────────────────────

async def _run_protocol_backend_initialization(session_id: str | None = None) -> bool:
    selected = _activate_runtime(session_id or _current_agent_id() or "default")
    if not session_id:
        _log("=" * 60)
        _log(f"WeChat Backend starting...  [mode={config.LOGIN_MODE}]")
        _log("Protocol mode is ready; start a WeChat instance from the account page.")
        _log(f"Protocol API: {config.HOOK_BASE_URL}")
        _log("=" * 60)
        return True

    _log("=" * 60)
    _log(f"WeChat Backend activating protocol session={selected}")
    _log("=" * 60)
    profile_raw = await _protocol_profile_for_session(selected, force=True)
    if not _protocol_profile_logged_in(profile_raw):
        _log(f"[PROTO_INIT] session not logged in: {profile_raw}")
        _state()["initialized"] = False
        return False

    _state()["self_info"] = profile_raw
    identity = _self_identity_from_response(profile_raw, agent_id=selected)
    wxid = identity["wxid"]
    if wxid:
        _self_wxid_to_agent_id[wxid] = selected
        _put_self_info_field("wxid", wxid)
        _put_self_info_field("selfwxid", wxid)

    owner_wxid = _contact_owner_wxid(wxid)
    _state()["contacts"] = _contacts_snapshot_from_db(owner_wxid)
    _load_session_cache_into_state(owner_wxid)
    _state()["initialized"] = True
    _log(f"[PROTO_INIT] active wxid={wxid or '-'} protocol cache ready")
    return True


async def _run_backend_initialization(agent_id: str | None = None) -> bool:
    """Initialize cached state from the Hook/Protocol API."""
    if config.IS_PROTOCOL:
        return await _run_protocol_backend_initialization(agent_id)

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
        _state()["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False, login_message="接口未就绪")
        _initializing_agents.discard(selected_agent)
        return False
    wechat_api._consecutive_failures = 0  # ensure clean slate for init

    if str(login_status.get("status") or "") != "3":
        _log(f"[INIT 0] ⏸ WeChat not logged in; skip initialization. status={login_status.get('status') or '?'} msg={login_status.get('message') or '-'}")
        _state()["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False)
        _initializing_agents.discard(selected_agent)
        return False

    # Phase 1 — run sequentially to avoid concurrent Hook access (Hook is NOT thread-safe)
    try:
        _log("[INIT 1/7] Loading self info...")
        _state()["self_info"] = await wechat_api.get_self_info()
        existing_agent = agent_manager.get_agent(selected_agent) or {}
        identity = _self_identity_from_response(
            _state()["self_info"] if isinstance(_state()["self_info"], dict) else {},
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
        await wechat_api.configure_pic_download_path(_scoped_dir(_IMG_CACHE_DIR))
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
        cached_contact_count = _cache().count_contacts(owner_wxid=owner_wxid)
        _state()["contacts"] = _contacts_snapshot_from_db(owner_wxid)
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
        _state()["contacts"] = contacts_snapshot
        friend_count = len(contacts_snapshot.get("friend") or [])
        room_count = len(contacts_snapshot.get("chatroom") or [])
        _log(f"[INIT 2/4] ✓ Cached contacts loaded: {friend_count} friends, {room_count} groups")
    except Exception as e:
        _log(f"[INIT 2/4] ✗ contacts load failed: {e}")

    try:
        _state()["sessions"] = _state().get("sessions") or {"data": []}
        _log("[INIT 3/7] ✓ Session DB load skipped until Chats view opens")
    except Exception as e:
        _log(f"[INIT 3/7] ✗ sessions load failed: {e}")

    # 4. Add self avatar explicitly
    avatar_urls: dict[str, str] = {}
    if _state()["self_info"] and isinstance(_state()["self_info"], dict):
        si = _state()["self_info"]
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
    _state()["avatar_urls"] = avatar_urls

    _state()["last_messages"] = _state().get("last_messages") or {}
    _log("[INIT 6/7] ✓ Last-message and chat-history preload skipped")

    if config.AGENT_WS_ENABLED and not agent_manager.is_connected(selected_agent):
        _log("[INIT 7/7] Agent disconnected during initialization; will retry on next connection.")
        _state()["initialized"] = False
        await agent_manager.update_account(selected_agent, initialized=False)
        _initializing_agents.discard(selected_agent)
        return False

    _log("[INIT 7/7] ✓ Initialization complete.")
    _state()["initialized"] = True
    await agent_manager.update_account(selected_agent, initialized=True)
    _initializing_agents.discard(selected_agent)
    _log("=" * 60)
    _log(f"Backend ready at http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    _log(f"Login mode: {config.LOGIN_MODE}  |  API: {config.HOOK_BASE_URL}")
    if config.AGENT_WS_ENABLED:
        _log(f"Agent WS: {config.CLIENT_WSS_URL}  path={config.AGENT_WS_PATH}")
        _log("Callback transport: Agent WebSocket")
    else:
        _log(f"Callback URL: {config.CALLBACK_URL}")
    _log("=" * 60)
    return True

async def _run_initialization_after_agent():
    _log("[INIT] Automatic Hook initialization is disabled. Hook calls now run only from explicit UI actions.")
    while True:
        while not agent_manager.is_connected():
            _log(f"[INIT] Waiting for DLL agent on {config.AGENT_WS_PATH} ...")
            await asyncio.sleep(1)
        # Do not call IsLoginStatus/GetSelfProfile/CDN_Init/InitContact here.
        # Account cards are refreshed by /api/accounts, and opening an account
        # only performs the single Session QueryDB requested by the UI.
        await asyncio.sleep(5)


async def _cancel_runtime_init_task() -> None:
    global _runtime_init_task
    if not _runtime_init_task:
        return
    _runtime_init_task.cancel()
    try:
        await _runtime_init_task
    except asyncio.CancelledError:
        pass
    _runtime_init_task = None


async def _reload_runtime_config() -> None:
    global config, wechat_api, protocol_api
    old_wechat_client = getattr(wechat_api, "client", None)
    old_protocol_client = getattr(protocol_api, "client", None)
    config = importlib.reload(config)
    wechat_api = importlib.reload(wechat_api)
    protocol_api = importlib.reload(protocol_api)
    ai_service.configure(
        base_url=config.AI_BASE_URL,
        api_key=config.AI_API_KEY,
        model=config.AI_MODEL,
    )
    for client in (old_wechat_client, old_protocol_client):
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def _start_runtime_after_config() -> dict[str, Any]:
    global _runtime_init_task
    await _cancel_runtime_init_task()
    reset_payload = _new_app_state()
    _state().update(reset_payload)
    _account_runtimes.clear()
    _self_wxid_to_agent_id.clear()
    _agent_login_status_seen.clear()
    _agent_self_profile_refreshed.clear()
    _account_card_refresh_at.clear()
    _account_card_refreshing.clear()
    _initializing_agents.clear()
    _agent_polling_paused_until.clear()

    if config.IS_PROTOCOL:
        ready = await _run_protocol_backend_initialization()
        return {"runtime": "protocol", "ready": ready}

    if config.AGENT_WS_ENABLED:
        _log("=" * 60)
        _log(f"WeChat Backend configured.  [mode={config.LOGIN_MODE}]")
        _log(f"Agent WS enabled: {config.CLIENT_WSS_URL}  path={config.AGENT_WS_PATH}")
        _log("=" * 60)
        _runtime_init_task = asyncio.create_task(_run_initialization_after_agent())
        return {"runtime": "agent_ws", "ready": True}

    _runtime_init_task = asyncio.create_task(_run_backend_initialization())
    return {"runtime": "hook", "ready": True}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize on startup, cleanup on shutdown."""
    init_task = None
    if not config.config_file_exists():
        _log("=" * 60)
        _log("config.yaml not found. Web setup is waiting for initial configuration.")
        _log("Open the frontend, enter the access key, then complete the setup form.")
        _log("=" * 60)
    elif config.IS_PROTOCOL:
        await _run_protocol_backend_initialization()
    elif config.AGENT_WS_ENABLED:
        _log("=" * 60)
        _log(f"WeChat Backend starting...  [mode={config.LOGIN_MODE}]")
        _log(f"Agent WS enabled: {config.CLIENT_WSS_URL}  path={config.AGENT_WS_PATH}")
        _log("Hook initialization will run after the DLL agent connects.")
        _log("=" * 60)
        init_task = asyncio.create_task(_run_initialization_after_agent())
    else:
        await _run_backend_initialization()

    yield

    await _cancel_runtime_init_task()
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
    await protocol_api.close()
    await ai_service.close()
    await mcp_service.close()
    await hook_login.client.aclose()
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
    key = (
        request.headers.get("X-Access-Key")
        or request.query_params.get("key")
        or request.cookies.get("wechat_web_key")
        or ""
    )
    access_store.legacy_user()
    user = access_store.login(key)
    request.state.user_id = user
    return bool(user)


def _request_agent_id(request: Request) -> str:
    return str(request.headers.get("X-Agent-Id") or request.query_params.get("agent_id") or "").strip()


def _is_public_http_path(path: str) -> bool:
    return (
        path in {
            "/api/auth/login",
            "/api/setup/status",
            "/api/setup/initial-config",
            "/api/callback",
            config.CALLBACK_PATH,
        }
        or path == config.AGENT_WS_PATH
    )


@app.middleware("http")
async def require_access_key(request: Request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if (path.startswith("/api/") or path.startswith('/uploads/')) and not _is_public_http_path(path):
        if not _request_access_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not config.config_file_exists():
            return JSONResponse({"error": "setup_required"}, status_code=428)
        access_store.migrate_hook(hook_login.instances())
        user_token = access_store.current_user.set(request.state.user_id)
        account_token = access_store.current_account.set('')
        try:
            return await _authorized_request(request, call_next)
        except HTTPException as exc:
            return JSONResponse({'error': exc.detail}, status_code=exc.status_code)
        finally:
            access_store.current_account.reset(account_token)
            access_store.current_user.reset(user_token)
    return await call_next(request)


async def _authorized_request(request, call_next):
    path = request.url.path
    if config.IS_PROTOCOL and path.startswith(('/api/protocol/', '/api/accounts')):
        await _migrate_protocol_ownership()
    agent_id = _request_agent_id(request)
    if agent_id:
        access_store.require_account(agent_id)
    if path.startswith("/api/protocol/") and not config.IS_PROTOCOL:
        return JSONResponse({"ok": False, "error": "当前为 Hook 模式，请使用启动微信入口"}, status_code=409)
    if (path.startswith('/api/ai/') or path.startswith('/api/mcp/')) and access_store.current_user.get() != access_store.legacy_user():
        raise HTTPException(403, '只有原配置密钥用户可以管理服务器 AI/MCP 配置')
    portal = path.startswith(('/api/accounts', '/api/hook/', '/api/protocol/')) or path == '/api/agent/status'
    if not agent_id and not portal and not path.startswith(('/api/ai/', '/api/mcp/')):
        candidates = access_store.account_ids()
        agent_id = next(iter(candidates), '')
        if not agent_id:
            raise HTTPException(409, '请先启动并选择自己的微信账号')
    if path.startswith('/uploads/'):
        _require_owned_path(os.path.join(_UPLOAD_DIR, path[len('/uploads/'):]))
    if request.method in {'POST', 'PUT'} and 'application/json' in request.headers.get('content-type', ''):
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, '请求 JSON 格式无效') from exc
        if isinstance(body, dict):
            for field in ('path', 'local_path', 'img_path', 'voice_path', 'file_path', 'picpath', 'filepath', 'videopath', 'gifpath'):
                if isinstance(body.get(field), str) and body[field]:
                    # File routes validate after the selected account's context is installed.
                    with wechat_api.use_agent(agent_id):
                        _require_owned_path(body[field])
    if agent_id:
        _activate_runtime(agent_id)
        with wechat_api.use_agent(agent_id):
            return await call_next(request)
    return await call_next(request)


class AuthLoginRequest(BaseModel):
    key: str


class InitialSetupRequest(BaseModel):
    key: str = ""
    web_access_key: str = "admin"
    wechat_mode: int = Field(default=1, ge=1, le=3)
    host: str = ""
    api_port: int = Field(default=30001, ge=1, le=65535)
    mgr_port: int = Field(default=29999, ge=1, le=65535)
    public_host: str = ""


class ActivateAccountRequest(BaseModel):
    agent_id: str


class DeleteAccountRequest(BaseModel):
    confirmed: bool = False


class ProtocolStartWechatRequest(BaseModel):
    rdv: str
    callback_url: str = ""
    proxy_type: str = ""
    proxy_ip: str = ""
    proxy_port: str = ""
    proxy_usr: str = ""
    proxy_pwd: str = ""


class HookStartWechatRequest(BaseModel):
    start_port: int | None = Field(default=None, ge=1, le=65535)
    rdv: str = ""
    wechat_path: str = ""
    remote_ws: str = ""
    instance_id: str = ""
    restart: bool = False
    new: bool = False
    proxy: dict | None = None


class HookQRCodeRequest(BaseModel):
    refresh: bool = False


class ProtocolSessionRequest(BaseModel):
    session_id: str
    uuid: str = ""


class ProtocolPushLoginRequest(BaseModel):
    session_id: str
    wxid: str


class ProtocolRdvMappingRequest(BaseModel):
    rdv: str
    wxid: str = ""
    port: str = ""
    nickname: str = ""
    avatar: str = ""
    account: str = ""
    phone: str = ""
    region: str = ""
    signature: str = ""
    profile: dict[str, Any] = Field(default_factory=dict)
    proxy: dict[str, Any] = Field(default_factory=dict)


_SMART_REPLY_ALLOWED_MESSAGE_TYPES = {
    "text", "image", "gif", "voice", "video", "file",
    "xml", "system", "recall", "quote",
}
_SMART_REPLY_RULE_MESSAGE_TYPES = {"text", "image"}
_SMART_REPLY_AI_MESSAGE_TYPES = {"text", "image", "file"}


class SmartReplyRuleRequest(BaseModel):
    id: str = ""
    message_type: str = "text"
    keyword: str
    reply: str = ""
    use_regex: bool = False
    reply_with_matched_line: bool = False


class SmartReplyAiTaskRequest(BaseModel):
    id: str = ""
    message_type: str = "text"
    name: str = ""
    enabled: bool = True
    skill_type: str = "custom"
    skill_id: str = ""
    instruction: str = ""
    confidence: int = Field(default=85, ge=0, le=100)
    output_mode: str = "result"
    reply_template: str = "{{result}}"
    preserve_formatting: bool = True
    send_items_separately: bool = False
    max_parallel: int = Field(default=1, ge=1, le=10)
    mcp_enabled: bool = False
    mcp_connection_id: str = ""
    mcp_tool_name: str = ""
    mcp_arguments_template: str = "{}"
    mcp_reply_template: str = "{{mcp_text}}"


class SmartReplyConfigRequest(BaseModel):
    chat_name: str = ""
    avatar: str = ""
    enabled: bool = True
    mention_only: bool = False
    mention_message_types: list[str] | None = None
    use_no_src: bool = False
    message_types: list[str] = Field(default_factory=lambda: ["text"])
    file_types: list[str] = Field(default_factory=lambda: ["txt"])
    target_senders: list[str] = Field(default_factory=list)
    target_senders_by_type: dict[str, list[str]] | None = None
    rules: list[SmartReplyRuleRequest] = Field(default_factory=list)
    ai_tasks: list[SmartReplyAiTaskRequest] = Field(default_factory=list)


class AiProfileRequest(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class McpConnectionRequest(BaseModel):
    id: str = ""
    name: str = ""
    url: str = ""
    token: str = ""
    enabled: bool = True


class AiSettingsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    active_profile_id: str = ""
    profiles: list[AiProfileRequest] = Field(default_factory=list)
    mcp_connections: list[McpConnectionRequest] | None = None


class AiAnalyzeRequest(BaseModel):
    message: str
    task: SmartReplyAiTaskRequest


class MultiBroadcastTextRequest(BaseModel):
    wxids: list[str] = []
    msg: str
    agent_ids: list[str] = []
    target_types: list[str] = []
    mode: str = "nosrc"
    concurrency_limit: int = 0
    batch_size: int = 100
    batch_interval: float = 5


class MultiBroadcastTargetsRequest(BaseModel):
    wxids: list[str] = []
    agent_ids: list[str] = []
    target_types: list[str] = []


@app.post("/api/auth/login")
async def auth_login(req: AuthLoginRequest):
    access_store.migrate_hook(hook_login.instances())
    user_id = access_store.login(req.key, create=True)
    if not user_id:
        return {"ok": False, 'error': '请输入非空密钥，最多 256 个字符'}
    setup_required = not config.config_file_exists()
    return {"ok": True, 'user_id': user_id, "setup_required": setup_required, "config_exists": not setup_required}


@app.get("/api/setup/status")
async def setup_status():
    defaults = config.initial_setup_defaults()
    return {
        "ok": True,
        "config_exists": defaults["config_exists"],
        "setup_required": not defaults["config_exists"],
        "default_web_access_key": defaults["default_web_access_key"],
        "defaults": defaults["defaults"],
        "modes": defaults["modes"],
    }


@app.post("/api/setup/initial-config")
async def setup_initial_config(req: InitialSetupRequest):
    if str(req.key or "") != config.WEB_ACCESS_KEY:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if config.config_file_exists():
        return {"ok": False, "error": "already_configured"}
    try:
        saved = config.save_initial_setup_config(
            wechat_mode=req.wechat_mode,
            web_access_key=req.web_access_key,
            host=req.host,
            api_port=req.api_port,
            mgr_port=req.mgr_port,
            public_host=req.public_host,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except FileExistsError:
        return {"ok": False, "error": "already_configured"}

    await _reload_runtime_config()
    runtime = await _start_runtime_after_config()
    return {
        "ok": True,
        "setup_required": False,
        "config_exists": True,
        "config": saved,
        "runtime": runtime,
        "web_access_key": saved["web_access_key"],
    }


@app.get("/api/accounts")
async def list_accounts():
    if config.IS_PROTOCOL:
        return await _list_protocol_accounts()
    if config.IS_LOCAL_HOOK or (config.IS_HOOK and not config.AGENT_WS_ENABLED):
        account = None
        if not access_store.current_user.get() or access_store.owns(_LOCAL_HOOK_ACCOUNT_ID):
            with wechat_api.use_agent(_LOCAL_HOOK_ACCOUNT_ID):
                account = await _probe_local_hook_account()
        extras = await asyncio.gather(*(_probe_started_hook_account(record) for record in hook_login.instances()
            if record["transport"] == "http" and record["id"] != _LOCAL_HOOK_ACCOUNT_ID
            and (not access_store.current_user.get() or access_store.owns(record['id']))))
        return {
            "active_id": _current_agent_id(),
            "accounts": [*([account] if account else []), *extras],
            "source": "hook",
            "transport": "http",
            "configured_endpoint": f"http://{config.HOOK_HOST}:{config.HOOK_PORT}",
        }
    _schedule_account_card_refresh()
    return {
        "active_id": _current_agent_id() or agent_manager.active_id(),
        "accounts": _owned_agents(),
        "source": "hook",
    }


@app.post("/api/accounts/activate")
async def activate_account(req: ActivateAccountRequest):
    access_store.require_account(req.agent_id)
    # The request header can still name the previously selected account.
    # All probes/initialization must use the account selected in this request.
    with wechat_api.use_agent(str(req.agent_id or "").strip()):
        return await _activate_account(req)


async def _activate_account(req: ActivateAccountRequest):
    agent_id = str(req.agent_id or "").strip()
    if config.IS_PROTOCOL:
        async with _ACCOUNT_LOCK:
            return await _activate_protocol_account(agent_id)
    instance = hook_login.http_instance(agent_id)
    if instance and agent_id != _LOCAL_HOOK_ACCOUNT_ID:
        async with _ACCOUNT_LOCK:
            account = await _probe_started_hook_account(instance)
            if account.get("login_status") != "3":
                return {"ok": False, "error": "微信尚未登录完成", "account": account}
            _activate_runtime(agent_id)
            with wechat_api.use_agent(agent_id):
                if not _state().get("initialized") and not await _run_backend_initialization(agent_id):
                    return {"ok": False, "error": "Hook 初始化未完成", "account": account}
            return {"ok": True, "active_id": agent_id, "account": await _probe_started_hook_account(instance)}
    if config.IS_LOCAL_HOOK or (config.IS_HOOK and not config.AGENT_WS_ENABLED):
        if agent_id != _LOCAL_HOOK_ACCOUNT_ID:
            return {"ok": False, "error": "unknown local Hook account"}
        async with _ACCOUNT_LOCK:
            account = await _probe_local_hook_account()
            if str(account.get("login_status") or "") != "3":
                return {
                    "ok": False,
                    "error": "wechat login status is not ready",
                    "login_status": {
                        "status": str(account.get("login_status") or ""),
                        "message": str(account.get("login_message") or "本地 Hook 接口不可用"),
                        "wxid": str(account.get("wxid") or ""),
                        "nickname": str(account.get("nickname") or ""),
                        "avatar": str(account.get("avatar") or ""),
                    },
                    "account": account,
                }
            _activate_runtime(_LOCAL_HOOK_ACCOUNT_ID)
            if not _state().get("initialized"):
                ready = await _run_backend_initialization(_LOCAL_HOOK_ACCOUNT_ID)
                if not ready:
                    account = await _probe_local_hook_account()
                    return {
                        "ok": False,
                        "error": "local Hook initialization failed",
                        "account": account,
                    }
            account = await _probe_local_hook_account()
            return {"ok": True, "active_id": _LOCAL_HOOK_ACCOUNT_ID, "account": account}
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


def _require_hook_login():
    if not config.IS_HOOK:
        raise HTTPException(status_code=409, detail="当前为协议模式，请使用扫码上号或老号登录")


def _owned_agents():
    agents = agent_manager.agents()
    for agent in agents:
        record = hook_login.instance_for_port(agent.get('server_port'))
        owner = access_store.owner(record['id']) if record else None
        if not owner and str(agent.get('server_port')) == str(config.HOOK_PORT):
            owner = access_store.owner('default')
        if owner:
            access_store.claim(agent['id'], owner)
    return [a for a in agents if not access_store.current_user.get() or access_store.owns(a['id'])]


def _hook_login_instance(instance_id):
    _require_hook_login()
    access_store.require_account(instance_id)
    record = hook_login.instance(instance_id)
    if not record and instance_id == _LOCAL_HOOK_ACCOUNT_ID and not config.AGENT_WS_ENABLED:
        record = {"id": instance_id, "host": config.HOOK_HOST, "port": config.HOOK_PORT,
            "mode": config.LOGIN_MODE, "rdv": config.RDV, "transport": "http"}
    if not record and agent_manager.is_connected(instance_id):
        record = hook_login.instance_for_port((agent_manager.get_agent(instance_id) or {}).get("server_port"))
    if not record:
        raise HTTPException(status_code=404, detail="未找到此 Hook 启动实例，请重新读取账号")
    return record


def _remember_hook_identity(record, account_id, wxid, user_id, identity=None, *, logged_in=False):
    if not user_id or not wxid:
        return
    saved = access_store.profile(record['id'], user_id) or {**hook_login.defaults(), **record, 'profile_id': record['id']}
    saved.update(wxid=wxid, wxids=sorted({*saved.get('wxids', []), wxid}),
                 aliases=sorted({*saved.get('aliases', []), record['id'], account_id}))
    if logged_in:
        saved['ever_logged_in'] = True
    for field in ('nickname', 'avatar'):
        if (identity or {}).get(field):
            saved[field] = identity[field]
    access_store.save_profile(saved, user_id)
    access_store.unretire('wxid:' + wxid)


async def _probe_started_hook_account(record):
    agent = hook_login.connected_agent(record) if record["transport"] == "ws" else None
    account_id = str((agent or {}).get("id") or record["id"])
    record_owner = access_store.owner(record['id'])
    if record_owner:
        access_store.claim(account_id, record_owner)
    state = _runtime_for(account_id)["app_state"]
    parsed = {"status": "", "message": "等待微信启动及 Hook 接口就绪", "wxid": "", "nickname": ""}
    connected = False
    identity = {}
    try:
        response = await hook_login.post(record, "/IsLoginStatus")
        parsed = _login_status_from_response(response.json())
        connected = True
        if parsed["status"] == "3":
            profile = state.get("self_info")
            if not profile:
                profile = (await hook_login.post(record, "/GetSelfProfile")).json()
                state["self_info"] = profile
            identity = _self_identity_from_response(profile, agent_id=account_id, current_wxid=parsed["wxid"])
        else:
            state["self_info"] = None
            state["initialized"] = False
    except (httpx.HTTPError, ConnectionError, TimeoutError, ValueError):
        pass
    wxid = str(identity.get("wxid") or parsed.get("wxid") or "")
    if wxid and record_owner and not access_store.claim('wxid:' + wxid, record_owner):
        raise HTTPException(403, '该微信已属于另一密钥用户，请使用原密钥登录')
    if wxid:
        _self_wxid_to_agent_id[wxid] = account_id
        _remember_hook_identity(record, account_id, wxid, record_owner, identity, logged_in=connected and parsed['status'] == '3')
    if record_owner and connected and parsed['status'] == '3':
        saved = access_store.profile(record['id'], record_owner) or {**record, 'profile_id': record['id']}
        saved['ever_logged_in'] = True
        access_store.save_profile(saved, record_owner)
    remembered = access_store.profile(record['id'], record_owner) if record_owner else {}
    return {"id": account_id, "account_id": wxid or account_id, "source": "hook", "wxid": wxid,
        "nickname": identity.get("nickname") or parsed.get("nickname") or (remembered or {}).get('nickname') or "微信待登录",
        "avatar": identity.get("avatar") or (remembered or {}).get('avatar') or "", "profile": identity.get("profile") or {},
        "connected": connected, "port_alive": connected, "login_status": parsed["status"],
        "login_message": '微信已退出，点击卡片重新启动' if not connected and (remembered or {}).get('stopped_at') else parsed["message"], "initialized": bool(state.get("initialized")),
        "transport": record["transport"], "api_host": record["host"], "api_port": record["port"],
        "start_port": record["port"], "server_port": str(record["port"]), "rdv": record["rdv"],
        "peer": f"{record['host']}:{record['port']}", "active": _current_agent_id() == account_id}


@app.get("/api/hook/login-settings")
async def get_hook_login_settings(instance_id: str = ''):
    _require_hook_login()
    if access_store.current_user.get():
        if instance_id and agent_manager.is_connected(instance_id):
            instance_id = _hook_login_instance(instance_id)['id']
        profile = hook_login.user_settings(instance_id)
        if profile['id'] == 'default' and not profile.get('pid'):
            # Adopt the original local instance once; its actual RDV takes precedence over old YAML defaults.
            try:
                response = await hook_login.client.post(config.MGR_BASE_URL + '/Get_WeChatProcessNumber', json={})
                response.raise_for_status()
                matches = [p for p in response.json().get('List', []) if str(p.get('Port')) == str(profile['port'])]
                if len(matches) == 1:
                    match = re.search(r'(?:^|[&\s])RDV=\"?([0-9a-f]{8})(?:\"|[&\s]|$)', str(matches[0].get('Par') or ''), re.I)
                    if match:
                        profile.update(rdv=match.group(1).lower(), pid=str(matches[0]['PID']))
                        access_store.save_profile(profile)
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        return {"ok": True, **profile}
    return {"ok": True, **hook_login.defaults()}


@app.post("/api/hook/start-wechat")
async def start_hook_wechat(req: HookStartWechatRequest):
    _require_hook_login()
    try:
        if access_store.current_user.get():
            record = await hook_login.start_for_user(instance_id=req.instance_id, restart=req.restart,
                rdv=req.rdv, proxy=req.proxy, new=req.new)
            if req.restart:
                _account_runtimes.pop(record['id'], None)
        else:
            record = await hook_login.start(start_port=req.start_port, rdv=req.rdv,
                wechat_path=req.wechat_path, remote_ws=req.remote_ws, proxy=req.proxy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(status_code=502, detail="管理器请求未确认成功，请检查管理服务及账号列表，勿连续重复启动。" + str(exc)) from exc
    return {"ok": True, "instance_id": record["id"], "process_id": record["pid"],
        "start_port": record["port"], "rdv": record["rdv"], "mode": record["mode"]}


@app.get("/api/hook/instances/{instance_id}/status")
async def hook_login_status(instance_id: str):
    record = _hook_login_instance(instance_id)
    return {"ok": True, "account": await _probe_started_hook_account(record)}


@app.post("/api/hook/instances/{instance_id}/qrcode")
async def hook_login_qrcode(instance_id: str, req: HookQRCodeRequest):
    record = _hook_login_instance(instance_id)
    try:
        if req.refresh:
            await hook_login.post(record, "/RefreshLoginQRCode")
        response = await hook_login.post(record, "/GetLoginQRCode")
        return {"ok": True, "qrcode": hook_login.qr_image(response)}
    except (httpx.HTTPError, OSError, ValueError, ConnectionError, TimeoutError):
        return {"ok": False, "error": "Hook 二维码尚未就绪，请稍后刷新或在微信窗口完成登录"}


async def _cancel_account_work(account_ids):
    current = asyncio.current_task()
    tasks = {task for account_id in account_ids for task in _runtime_for(account_id).get('tasks', set())
             if task is not current and not task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _clear_account_runtime(account_id):
    global app_state, message_store
    _account_runtimes.pop(account_id, None)
    if account_id == 'default':
        app_state = _new_app_state()
        message_store = MessageStore()


async def _manage_hook_account(account_id, *, delete=False):
    access_store.require_account(account_id)
    # Resolve/adopt the original instance's actual RDV before verifying its process.
    settings = await get_hook_login_settings(account_id)
    if not delete and not (settings.get('ever_logged_in') or settings.get('wxid') or settings.get('wxids')):
        # A scan may have completed since the last browser poll. Observe it before
        # deciding that this is a disposable instance which has never logged in.
        await _probe_started_hook_account(_hook_login_instance(account_id))
    removed = delete
    had_login = False
    stopped_aliases = set()
    async def after_stop(profile):
        nonlocal removed, had_login, stopped_aliases
        aliases = {account_id, profile['id'], *profile.get('aliases', [])}
        stopped_aliases = aliases
        await _cancel_account_work(aliases)
        had_login = bool(profile.get('ever_logged_in') or profile.get('wxid') or profile.get('wxids') or any(
            _extract_self_wxid(_runtime_for(alias)['app_state'].get('self_info') or {}) for alias in aliases))
        removed = delete or not had_login
        deleted = {}
        if delete:
            cache = _user_cache(access_store.current_user.get())
            owners = {*aliases, *profile.get('wxids', []), *([profile['wxid']] if profile.get('wxid') else [])}
            for alias in aliases:
                info = _runtime_for(alias)['app_state'].get('self_info') or {}
                wxid = _self_identity_from_response(info, agent_id=alias).get('wxid')
                if wxid:
                    owners.add(wxid)
            for owner_wxid in owners:
                deleted[owner_wxid] = cache.delete_account_data(owner_wxid)
                _self_wxid_to_agent_id.pop(owner_wxid, None)
                for brief_cache in (_CONTACT_BRIEF_CACHE, _CONTACT_PROFILE_CACHE):
                    for key in list(brief_cache):
                        if key.startswith(owner_wxid + '::'):
                            brief_cache.pop(key, None)
                _CONTACT_HYDRATION_PROGRESS.pop(owner_wxid, None)
            profile['wxids'] = sorted(owners)
        if removed:
            access_store.forget_profile(profile, aliases)
            hook_login.forget(profile['id'])
        for alias in aliases:
            _clear_account_runtime(alias)
        return deleted
    try:
        await hook_login.stop_for_user(settings['id'], after_stop=after_stop)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(502, '微信管理器未确认操作成功，请稍后重试') from exc
    return {'ok': True, 'account_id': account_id, 'deleted': removed, 'ever_logged_in': had_login,
            'removed_account_ids': sorted(stopped_aliases) if removed else []}


@app.post('/api/accounts/{account_id}/logout')
async def logout_hook_account(account_id: str):
    _require_hook_login()
    return await _manage_hook_account(account_id)


@app.delete('/api/accounts/{account_id}')
async def delete_hook_account(account_id: str, req: DeleteAccountRequest):
    _require_hook_login()
    access_store.require_account(account_id)
    if not req.confirmed:
        raise HTTPException(400, '删除会永久清除这个微信的本地记录，请先确认删除警告')
    return await _manage_hook_account(account_id, delete=True)


@app.get("/api/protocol/health")
async def get_protocol_health():
    return await protocol_api.health()


async def _migrate_protocol_ownership():
    marker = 'protocol_migrated:' + access_store.scope()
    async with _PROTOCOL_OWNERSHIP_LOCK:
        with access_store.connect() as db:
            if db.execute('SELECT 1 FROM access_metadata WHERE name=?', (marker,)).fetchone():
                return
        response = await protocol_api.get_session_list()
        if not isinstance(response, dict) or response.get('ok') is False:
            raise HTTPException(502, '协议服务不可用，尚未完成原微信账号归属迁移')
        user = access_store.legacy_user()
        for item in response.get('sessions', []):
            if isinstance(item, dict) and item.get('session_id'):
                access_store.claim(item['session_id'], user)
                if item.get('rdv'):
                    access_store.claim('rdv:' + str(item['rdv']).lower(), user)
        with access_store.connect() as db:
            db.execute('INSERT OR IGNORE INTO access_metadata VALUES(?,?)', (marker, '1'))


@app.get("/api/protocol/rdv-mapping")
async def get_protocol_rdv_mapping():
    return {
        "ok": True,
        "scope": _protocol_scope_key(),
        "mapping": _current_rdv_mapping(),
        "rdv_rule": "自定义取值范围（十六进制）01000000~7fffffff",
    }


@app.post("/api/protocol/rdv-mapping")
async def save_protocol_rdv_mapping(req: ProtocolRdvMappingRequest):
    mapping = _upsert_rdv_mapping(
        req.rdv,
        req.wxid,
        req.port,
        req.proxy,
        {
            "nickname": req.nickname,
            "avatar": req.avatar,
            "account": req.account,
            "phone": req.phone,
            "region": req.region,
            "signature": req.signature,
            "profile": req.profile,
        },
    )
    return {"ok": True, "scope": _protocol_scope_key(), "mapping": mapping}


@app.delete("/api/protocol/rdv-mapping/{rdv}")
async def delete_protocol_rdv_mapping(rdv: str):
    mapping = _delete_rdv_mapping(rdv)
    return {"ok": True, "scope": _protocol_scope_key(), "mapping": mapping}


@app.post("/api/protocol/start-wechat")
async def start_protocol_wechat(req: ProtocolStartWechatRequest):
    rdv = _validate_protocol_rdv(req.rdv)
    if access_store.current_user.get() and not access_store.claim('rdv:' + rdv):
        raise HTTPException(403, '此 RDV 已属于其他密钥用户')
    callback_url = str(req.callback_url or "").strip() or config.CALLBACK_URL
    data = await protocol_api.start_wechat(
        rdv=rdv,
        callback_url=callback_url,
        proxy_type=str(req.proxy_type or "").strip(),
        proxy_ip=str(req.proxy_ip or "").strip(),
        proxy_port=str(req.proxy_port or "").strip(),
        proxy_usr=str(req.proxy_usr or "").strip(),
        proxy_pwd=str(req.proxy_pwd or "").strip(),
    )
    if isinstance(data, dict):
        session_id = _protocol_session_id_from_payload(data)
        start_port = _protocol_start_port_from_payload(data)
        if session_id:
            access_store.claim(session_id)
            data["session_id"] = session_id
        if start_port not in (None, ""):
            data["start_port"] = start_port
        if data.get("ok") and session_id:
            data["rdv"] = str(data.get("rdv") or rdv)
            data["callback_url"] = str(data.get("callback_url") or callback_url)
    return data


@app.post("/api/protocol/get-qrcode")
async def get_protocol_login_qrcode(req: ProtocolSessionRequest):
    session_id = str(req.session_id or "").strip()
    access_store.require_account(session_id)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    return await protocol_api.get_login_qrcode(session_id)


@app.post("/api/protocol/get-login-status")
async def get_protocol_login_status(req: ProtocolSessionRequest):
    session_id = str(req.session_id or "").strip()
    access_store.require_account(session_id)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    data = await protocol_api.get_login_status(session_id)
    state = str(data.get("state") or data.get("raw_state") or "").strip().lower()
    if state == "logged_in" or data.get("logged_in") is True:
        try:
            profile = await _protocol_profile_for_session(session_id, force=True)
            data["profile_result"] = profile
            if _protocol_profile_logged_in(profile):
                identity = _self_identity_from_response(profile, agent_id=session_id)
                data["account"] = _protocol_account_from_identity(
                    session_id,
                    {
                        "session_id": session_id,
                        "state": "logged_in",
                        "online": True,
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    },
                    identity,
                )
        except Exception as e:
            data["profile_error"] = f"{type(e).__name__}: {e}"
    return data


@app.post("/api/protocol/push-login-url")
async def push_protocol_login_url(req: ProtocolPushLoginRequest):
    session_id = str(req.session_id or "").strip()
    access_store.require_account(session_id)
    wxid = str(req.wxid or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    if not wxid:
        raise HTTPException(status_code=400, detail="wxid 不能为空")
    return await protocol_api.push_login_url(session_id, wxid)


@app.post("/api/protocol/get-profile")
async def get_protocol_profile(req: ProtocolSessionRequest):
    session_id = str(req.session_id or "").strip()
    access_store.require_account(session_id)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    data = await _protocol_profile_for_session(session_id, force=True)
    if _protocol_profile_logged_in(data):
        _activate_runtime(session_id)
        _state()["self_info"] = data
        identity = _self_identity_from_response(data, agent_id=session_id)
        wxid = identity["wxid"]
        if wxid:
            _self_wxid_to_agent_id[wxid] = session_id
            _put_self_info_field("wxid", wxid)
            _put_self_info_field("selfwxid", wxid)
            profile = identity.get("profile") if isinstance(identity.get("profile"), dict) else {}
            if profile:
                await _cache_contact_profiles([profile], owner_wxid=wxid)
    return data


@app.post("/api/protocol/terminate")
async def terminate_protocol_session(req: ProtocolSessionRequest):
    session_id = str(req.session_id or "").strip()
    access_store.require_account(session_id)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    _PROTOCOL_ACCOUNT_PROFILE_CACHE.pop(session_id, None)
    return await protocol_api.terminate_session(session_id)


def _normalize_ai_task(task: SmartReplyAiTaskRequest, index: int = 0) -> dict[str, Any]:
    message_type = str(task.message_type or "text").strip().lower()
    if message_type not in _SMART_REPLY_AI_MESSAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"message type does not support AI Skills: {message_type}",
        )
    allowed_output_modes = {"result", "template", "silent"}
    output_mode = str(task.output_mode or "result").strip().lower()
    if output_mode not in allowed_output_modes:
        raise HTTPException(status_code=400, detail=f"unsupported AI output mode: {output_mode}")
    task_id = str(task.id or f"ai_skill_{index + 1}")[:100]
    skill_id = str(task.skill_id or task_id).strip()
    instruction = str(task.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="custom Skills require an instruction")
    reply_template = str(task.reply_template or "").strip()
    if output_mode == "template" and not reply_template:
        raise HTTPException(status_code=400, detail="template AI replies require a reply template")
    mcp_enabled = bool(task.mcp_enabled)
    mcp_connection_id = str(task.mcp_connection_id or "").strip()
    mcp_tool_name = str(task.mcp_tool_name or "").strip()
    mcp_arguments_template = str(task.mcp_arguments_template or "{}").strip() or "{}"
    mcp_reply_template = str(task.mcp_reply_template or "{{mcp_text}}").strip()
    try:
        parsed_mcp_arguments = json.loads(mcp_arguments_template)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="MCP arguments must be a valid JSON object") from exc
    if not isinstance(parsed_mcp_arguments, dict):
        raise HTTPException(status_code=400, detail="MCP arguments must be a JSON object")
    if mcp_enabled and not mcp_connection_id:
        raise HTTPException(status_code=400, detail="enabled MCP Skills require a connection")
    return {
        "id": task_id,
        "message_type": message_type,
        "name": str(task.name or f"Skill {index + 1}").strip()[:80],
        "enabled": bool(task.enabled),
        "skill_type": "custom",
        "skill_id": skill_id[:120],
        "instruction": instruction[:4000],
        "confidence": max(0, min(100, int(task.confidence))),
        "output_mode": output_mode,
        "reply_template": reply_template[:4000],
        "preserve_formatting": bool(task.preserve_formatting),
        "send_items_separately": bool(task.send_items_separately),
        "max_parallel": max(1, min(10, int(task.max_parallel))),
        "mcp_enabled": mcp_enabled,
        "mcp_connection_id": mcp_connection_id[:100],
        "mcp_tool_name": mcp_tool_name[:200],
        "mcp_arguments_template": json.dumps(parsed_mcp_arguments, ensure_ascii=False),
        "mcp_reply_template": mcp_reply_template[:4000],
    }


def _ai_settings_payload(*, include_api_key: bool = False) -> dict[str, Any]:
    status = ai_service.status()
    profile_keys = {
        str(profile.get("id") or ""): str(profile.get("api_key") or "")
        for profile in config.AI_PROFILES
    }
    profiles = []
    for profile in config.AI_PROFILES:
        profile_payload = {
            "id": str(profile.get("id") or ""),
            "name": str(profile.get("name") or ""),
            "base_url": str(profile.get("base_url") or ""),
            "model": str(profile.get("model") or ""),
            "configured": bool(profile.get("base_url") and profile.get("api_key") and profile.get("model")),
            "api_key_configured": bool(profile.get("api_key")),
        }
        if include_api_key:
            profile_payload["api_key"] = profile_keys.get(profile_payload["id"], "")
        profiles.append(profile_payload)
    payload = {
        **status,
        "api_key_configured": bool(config.AI_API_KEY),
        "active_profile_id": config.AI_ACTIVE_PROFILE_ID,
        "profiles": profiles,
    }
    if include_api_key:
        payload["api_key"] = config.AI_API_KEY
    mcp_connections: list[dict[str, Any]] = []
    for connection in config.MCP_CONNECTIONS:
        connection_payload = {
            "id": str(connection.get("id") or ""),
            "name": str(connection.get("name") or ""),
            "url": str(connection.get("url") or ""),
            "enabled": bool(connection.get("enabled", True)),
            "token_configured": bool(connection.get("token")),
        }
        if include_api_key:
            connection_payload["token"] = str(connection.get("token") or "")
        mcp_connections.append(connection_payload)
    payload["mcp_connections"] = mcp_connections
    return payload


@app.get("/api/ai/settings")
async def get_ai_settings(include_api_key: bool = False):
    config.reload_ai_settings()
    ai_service.configure(
        base_url=config.AI_BASE_URL,
        api_key=config.AI_API_KEY,
        model=config.AI_MODEL,
    )
    payload = _ai_settings_payload(include_api_key=include_api_key)
    if include_api_key:
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    return payload


def _profile_payload_from_request(req: AiSettingsRequest) -> dict[str, str]:
    if req.profiles:
        existing_profiles = {
            str(profile.get("id") or ""): profile
            for profile in config.AI_PROFILES
        }
        active_id = str(req.active_profile_id or "").strip()
        raw_profile = next((profile for profile in req.profiles if str(profile.id or "").strip() == active_id), None)
        if raw_profile is None:
            raw_profile = next(
                (
                    profile for profile in req.profiles
                    if str(profile.base_url or "").strip()
                    and str(profile.model or "").strip()
                    and (
                        str(profile.api_key or "").strip()
                        or str(existing_profiles.get(str(profile.id or "").strip(), {}).get("api_key") or "")
                    )
                ),
                req.profiles[0],
            )
        profile_id = str(raw_profile.id or "").strip()
        api_key = str(raw_profile.api_key or "").strip()
        if not api_key:
            api_key = str(existing_profiles.get(profile_id, {}).get("api_key") or "")
        return {
            "id": profile_id,
            "name": str(raw_profile.name or "").strip(),
            "base_url": str(raw_profile.base_url or "").strip().rstrip("/"),
            "api_key": api_key,
            "model": str(raw_profile.model or "").strip(),
        }
    return {
        "id": str(req.active_profile_id or "default").strip(),
        "name": str(req.model or "默认配置").strip(),
        "base_url": str(req.base_url or "").strip().rstrip("/"),
        "api_key": str(req.api_key or "").strip() or config.AI_API_KEY,
        "model": str(req.model or "").strip(),
    }


def _normalize_ai_settings_request(req: AiSettingsRequest) -> tuple[str, str, str]:
    profile = _profile_payload_from_request(req)
    base_url = str(profile.get("base_url") or "").strip().rstrip("/")
    model = str(profile.get("model") or "").strip()
    api_key = str(profile.get("api_key") or "").strip()
    try:
        parsed_url = httpx.URL(base_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid AI API URL") from exc
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
        raise HTTPException(status_code=400, detail="AI API URL must use http or https")
    if not api_key:
        raise HTTPException(status_code=400, detail="AI API key is required")
    if not model:
        raise HTTPException(status_code=400, detail="AI model is required")
    return base_url, api_key, model


@app.post("/api/ai/settings/validate")
async def validate_ai_settings(req: AiSettingsRequest):
    base_url, api_key, model = _normalize_ai_settings_request(req)
    candidate = AiService(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=config.AI_TIMEOUT_SECONDS,
        max_concurrency=1,
    )
    try:
        result = await candidate.probe()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"AI connection failed: {exc}") from exc
    finally:
        await candidate.close()
    return {"ok": True, "model": model, "models_url": result.get("models_url", "")}


def _normalize_mcp_connection_request(
    req: McpConnectionRequest,
    index: int = 0,
) -> dict[str, Any]:
    connection_id = str(req.id or f"mcp_connection_{index + 1}").strip()[:100]
    url = str(req.url or "").strip().rstrip("/")
    try:
        parsed_url = httpx.URL(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid MCP URL") from exc
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
        raise HTTPException(status_code=400, detail="MCP URL must use http or https")
    existing = next(
        (item for item in config.MCP_CONNECTIONS if str(item.get("id") or "") == connection_id),
        {},
    )
    token = str(req.token or "").strip() or str(existing.get("token") or "")
    if req.enabled and requires_bearer_token(url) and not token.strip():
        raise HTTPException(status_code=400, detail="数据运维 MCP 必须配置 Bearer Token，请填写服务器 api_token")
    return {
        "id": connection_id or f"mcp_connection_{index + 1}",
        "name": str(req.name or url or f"MCP 连接 {index + 1}").strip()[:80],
        "url": url,
        "token": token,
        "enabled": bool(req.enabled),
    }


def _mcp_connection(connection_id: str) -> dict[str, Any] | None:
    normalized_id = str(connection_id or "").strip()
    return next(
        (
            connection
            for connection in config.MCP_CONNECTIONS
            if str(connection.get("id") or "") == normalized_id
        ),
        None,
    )


@app.post("/api/mcp/validate")
async def validate_mcp_connection(req: McpConnectionRequest):
    connection = _normalize_mcp_connection_request(req)
    try:
        discovery = await mcp_service.discover(
            url=str(connection.get("url") or ""),
            token=str(connection.get("token") or ""),
        )
    except McpServiceError as exc:
        raise HTTPException(status_code=400, detail=f"MCP connection failed: {exc}") from exc
    return {"ok": True, **discovery}


@app.get("/api/mcp/connections/{connection_id}/tools")
async def list_mcp_connection_tools(connection_id: str):
    config.reload_ai_settings()
    connection = _mcp_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="MCP connection not found")
    if not bool(connection.get("enabled", True)):
        raise HTTPException(status_code=400, detail="MCP connection is disabled")
    try:
        discovery = await mcp_service.discover(
            url=str(connection.get("url") or ""),
            token=str(connection.get("token") or ""),
        )
    except McpServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "connection_id": connection_id, **discovery}


@app.put("/api/ai/settings")
async def save_ai_settings(req: AiSettingsRequest):
    _normalize_ai_settings_request(req)
    normalized_mcp_connections = (
        [
            _normalize_mcp_connection_request(connection, index)
            for index, connection in enumerate(req.mcp_connections)
        ]
        if req.mcp_connections is not None
        else None
    )
    if req.profiles:
        existing_profiles = {
            str(profile.get("id") or ""): profile
            for profile in config.AI_PROFILES
        }
        profiles: list[dict[str, str]] = []
        for index, profile in enumerate(req.profiles):
            profile_id = str(profile.id or f"ai_profile_{index + 1}").strip()
            profiles.append({
                "id": profile_id,
                "name": str(profile.name or profile.model or f"AI 配置 {index + 1}").strip(),
                "base_url": str(profile.base_url or "").strip().rstrip("/"),
                "api_key": str(profile.api_key or "").strip() or str(existing_profiles.get(profile_id, {}).get("api_key") or ""),
                "model": str(profile.model or "").strip(),
            })
        config.save_ai_settings(
            profiles=profiles,
            active_profile_id=str(req.active_profile_id or "").strip(),
            mcp_connections=normalized_mcp_connections,
        )
    else:
        profile = _profile_payload_from_request(req)
        config.save_ai_settings(
            base_url=str(profile.get("base_url") or ""),
            api_key=str(profile.get("api_key") or ""),
            model=str(profile.get("model") or ""),
            mcp_connections=normalized_mcp_connections,
        )
    ai_service.configure(
        base_url=config.AI_BASE_URL,
        api_key=config.AI_API_KEY,
        model=config.AI_MODEL,
    )
    return {"ok": True, **_ai_settings_payload()}


@app.post("/api/ai/analyze")
async def analyze_ai_message(req: AiAnalyzeRequest):
    message = str(req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    task = offline_analysis_task(_normalize_ai_task(req.task), message)
    try:
        result = await ai_service.analyze(message, task)
    except AiServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "result": result}


def _normalized_smart_reply_config(chat_id: str, req: SmartReplyConfigRequest) -> dict[str, Any]:
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat id is required")
    is_group = chat_id.endswith("@chatroom")
    owner_wxid = _contact_owner_wxid()
    if chat_id == "filehelper" and not owner_wxid:
        raise HTTPException(status_code=400, detail="请先登录微信账号，再配置文件传输助手")

    message_types: list[str] = []
    for value in req.message_types:
        message_type = str(value or "").strip().lower()
        if message_type not in _SMART_REPLY_ALLOWED_MESSAGE_TYPES:
            raise HTTPException(status_code=400, detail=f"unsupported smart reply message type: {message_type}")
        if message_type not in message_types:
            message_types.append(message_type)
    if not message_types:
        raise HTTPException(status_code=400, detail="at least one message type is required")

    def normalize_senders(values: list[str]) -> list[str]:
        senders: list[str] = []
        seen: set[str] = set()
        for value in values:
            sender = str(value or "").strip()
            if sender and sender not in seen:
                seen.add(sender)
                senders.append(sender)
        if len(senders) > 10000:
            raise HTTPException(status_code=400, detail="too many target senders")
        return senders

    legacy_target_senders = normalize_senders(req.target_senders)
    if req.target_senders_by_type is None:
        target_senders_by_type = {
            message_type: list(legacy_target_senders)
            for message_type in message_types
        }
    else:
        target_senders_by_type = {}
        for raw_message_type, raw_senders in req.target_senders_by_type.items():
            message_type = str(raw_message_type or "").strip().lower()
            if message_type not in _SMART_REPLY_ALLOWED_MESSAGE_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"unsupported smart reply message type: {message_type}",
                )
            target_senders_by_type[message_type] = normalize_senders(raw_senders)

    if not is_group:
        stored_message_types = list(dict.fromkeys([
            *message_types,
            *target_senders_by_type.keys(),
        ]))
        target_senders_by_type = {
            message_type: [owner_wxid if chat_id == "filehelper" else chat_id]
            for message_type in stored_message_types
        }
    else:
        for message_type in message_types:
            if not target_senders_by_type.get(message_type):
                raise HTTPException(
                    status_code=400,
                    detail=f"at least one target sender is required for message type: {message_type}",
                )

    target_senders: list[str] = []
    seen_target_senders: set[str] = set()
    for message_type in [*message_types, *target_senders_by_type.keys()]:
        for sender in target_senders_by_type.get(message_type, []):
            if sender not in seen_target_senders:
                seen_target_senders.add(sender)
                target_senders.append(sender)

    raw_mention_message_types = (
        req.mention_message_types
        if req.mention_message_types is not None
        else (["text"] if req.mention_only else [])
    )
    mention_message_types: list[str] = []
    for value in raw_mention_message_types:
        message_type = str(value or "").strip().lower()
        if message_type != "text":
            raise HTTPException(
                status_code=400,
                detail=f"message type does not support mention filtering: {message_type}",
            )
        if message_type not in mention_message_types:
            mention_message_types.append(message_type)

    allowed_file_types = {"txt", "pdf", "xlsx", "docx"}
    file_types: list[str] = []
    for value in req.file_types:
        file_type = str(value or "").strip().lower().lstrip(".")
        if file_type not in allowed_file_types:
            raise HTTPException(status_code=400, detail=f"unsupported smart reply file type: {file_type}")
        if file_type not in file_types:
            file_types.append(file_type)

    rules: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}
    for index, rule in enumerate(req.rules):
        message_type = str(rule.message_type or "text").strip().lower()
        if message_type not in _SMART_REPLY_RULE_MESSAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"message type does not support keyword rules: {message_type}",
            )
        rule_counts[message_type] = rule_counts.get(message_type, 0) + 1
        if rule_counts[message_type] > 100:
            raise HTTPException(
                status_code=400,
                detail=f"too many keyword rules for message type: {message_type}",
            )
        keyword = str(rule.keyword or "").strip()
        reply = str(rule.reply or "").strip()
        reply_with_matched_line = bool(rule.reply_with_matched_line)
        if not keyword or (not reply_with_matched_line and not reply):
            raise HTTPException(status_code=400, detail="every rule requires a keyword and reply")
        if len(keyword) > 200 or len(reply) > 4000:
            raise HTTPException(status_code=400, detail="a keyword or reply is too long")
        if rule.use_regex:
            try:
                re.compile(keyword, re.IGNORECASE | re.MULTILINE)
            except re.error as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid regular expression in rule {index + 1}: {exc}",
                ) from exc
        rules.append({
            "id": str(rule.id or f"rule_{index + 1}")[:100],
            "message_type": message_type,
            "keyword": keyword,
            "reply": reply,
            "use_regex": bool(rule.use_regex),
            "reply_with_matched_line": reply_with_matched_line,
        })
    ai_tasks = [_normalize_ai_task(task, index) for index, task in enumerate(req.ai_tasks)]
    ai_task_counts: dict[str, int] = {}
    for task in ai_tasks:
        task_message_type = str(task.get("message_type") or "text")
        ai_task_counts[task_message_type] = ai_task_counts.get(task_message_type, 0) + 1
        if ai_task_counts[task_message_type] > 20:
            raise HTTPException(
                status_code=400,
                detail=f"too many AI Skills for message type: {task_message_type}",
            )
    usable_mcp_connection_ids = {
        str(connection.get("id") or "")
        for connection in config.MCP_CONNECTIONS
        if bool(connection.get("enabled", True))
    }
    for task in ai_tasks:
        task_connection_id = str(task.get("mcp_connection_id") or "")
        if bool(task.get("mcp_enabled")) and task_connection_id not in usable_mcp_connection_ids:
            raise HTTPException(
                status_code=400,
                detail=f"MCP connection is missing or disabled: {task_connection_id}",
            )
    if not rules and not ai_tasks:
        raise HTTPException(status_code=400, detail="at least one keyword rule or AI task is required")

    cached_contact = _cache().get_contacts([chat_id], owner_wxid=owner_wxid).get(chat_id, {})
    return {
        "chat_id": chat_id,
        "chat_name": str(req.chat_name or cached_contact.get("name") or chat_id).strip()[:200],
        "avatar": str(req.avatar or cached_contact.get("avatar") or "").strip()[:2000],
        "enabled": bool(req.enabled),
        "mention_only": is_group and "text" in mention_message_types,
        "mention_message_types": mention_message_types if is_group else [],
        "use_no_src": bool(req.use_no_src),
        "message_types": message_types,
        "file_types": file_types,
        "target_senders": target_senders,
        "target_senders_by_type": target_senders_by_type,
        "rules": rules,
        "ai_tasks": ai_tasks,
    }


@app.get("/api/smart-replies")
async def list_smart_replies():
    owner_wxid = _contact_owner_wxid()
    return {"configs": _cache().list_smart_reply_configs(owner_wxid=owner_wxid)}


@app.get("/api/smart-replies/{chat_id}")
async def get_smart_reply(chat_id: str):
    owner_wxid = _contact_owner_wxid()
    config_row = _cache().get_smart_reply_config(chat_id, owner_wxid=owner_wxid)
    if config_row is None:
        raise HTTPException(status_code=404, detail="smart reply config not found")
    return {"config": config_row}


@app.put("/api/smart-replies/{chat_id}")
async def save_smart_reply(chat_id: str, req: SmartReplyConfigRequest):
    owner_wxid = _contact_owner_wxid()
    config_row = _cache().upsert_smart_reply_config(
        _normalized_smart_reply_config(chat_id, req),
        owner_wxid=owner_wxid,
    )
    await manager.broadcast({"type": "smart_reply_updated", "data": {"config": config_row}})
    return {"ok": True, "config": config_row}


@app.delete("/api/smart-replies/{chat_id}")
async def delete_smart_reply(chat_id: str):
    owner_wxid = _contact_owner_wxid()
    deleted = _cache().delete_smart_reply_config(chat_id, owner_wxid=owner_wxid)
    await manager.broadcast({"type": "smart_reply_updated", "data": {"chat_id": chat_id, "deleted": deleted}})
    return {"ok": True, "deleted": deleted}


def _render_ai_template(template: str, result: dict[str, Any], item: str = "") -> str:
    replacements = {
        "{{result}}": item or str(result.get("result") or result.get("reply") or ""),
        "{{reply}}": str(result.get("reply") or ""),
        "{{item}}": item,
    }
    rendered = str(template or "")
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered.strip()


def _ai_result_replies(task: dict[str, Any], result: dict[str, Any]) -> tuple[str, ...]:
    if not bool(result.get("matched")):
        return ()
    if int(result.get("confidence") or 0) < int(task.get("confidence") or 0):
        return ()
    output_mode = str(task.get("output_mode") or "result")
    if output_mode == "silent":
        return ()
    items = [str(item).strip() for item in (result.get("items") or []) if str(item or "").strip()]
    if bool(task.get("send_items_separately")) and items:
        if output_mode == "template":
            replies = tuple(_render_ai_template(str(task.get("reply_template") or "{{item}}"), result, item) for item in items)
        else:
            replies = tuple(items)
    elif output_mode == "template":
        replies = (_render_ai_template(str(task.get("reply_template") or "{{result}}"), result),)
    else:
        replies = (str(result.get("reply") or result.get("result") or "").strip(),)
    if not bool(task.get("preserve_formatting", True)):
        replies = tuple(" ".join(reply.split()) for reply in replies)
    return tuple(reply for reply in replies if reply)


def _ai_result_matches(task: dict[str, Any], result: dict[str, Any]) -> bool:
    return bool(result.get("matched")) and int(result.get("confidence") or 0) >= int(
        task.get("confidence") or 0
    )


def _extract_skill_identifier(result: dict[str, Any]) -> str:
    candidates = [
        str(result.get("result") or ""),
        *(str(item or "") for item in (result.get("items") or [])),
        str(result.get("reply") or ""),
    ]
    for candidate in candidates:
        matched = re.search(r"(?i)(?:xzsb\s*[-_:：]?\s*)?(?<!\d)(\d{4,5})(?!\d)", candidate)
        if matched:
            return matched.group(1)
    return ""


def _replace_mcp_argument_tokens(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {str(key): _replace_mcp_argument_tokens(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_mcp_argument_tokens(item, replacements) for item in value]
    if not isinstance(value, str):
        return value
    if value in replacements:
        return replacements[value]
    rendered = value
    for marker, replacement in replacements.items():
        if marker in rendered:
            replacement_text = (
                json.dumps(replacement, ensure_ascii=False)
                if isinstance(replacement, (dict, list))
                else str(replacement)
            )
            rendered = rendered.replace(marker, replacement_text)
    return rendered


def _render_mcp_arguments(task: dict[str, Any], result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    identifier = _extract_skill_identifier(result)
    replacements: dict[str, Any] = {
        "{{result}}": str(result.get("result") or ""),
        "{{reply}}": str(result.get("reply") or ""),
        "{{items}}": result.get("items") or [],
        "{{identifier}}": identifier,
    }
    try:
        template = json.loads(str(task.get("mcp_arguments_template") or "{}"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise McpServiceError("MCP arguments are not valid JSON") from exc
    if not isinstance(template, dict):
        raise McpServiceError("MCP arguments must be a JSON object")
    arguments = _replace_mcp_argument_tokens(template, replacements)
    if not isinstance(arguments, dict):
        raise McpServiceError("MCP arguments must resolve to a JSON object")
    if "{{identifier}}" in str(task.get("mcp_arguments_template") or "") and not identifier:
        raise McpServiceError("AI result did not contain a 4 or 5 digit SQL identifier")
    return arguments, identifier


def _flatten_mcp_values(value: Any, prefix: str = "") -> dict[str, str]:
    flattened: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            nested = _flatten_mcp_values(item, path)
            flattened.update(nested)
            if not isinstance(item, (dict, list)):
                flattened.setdefault(key_text, str(item if item is not None else ""))
        return flattened
    if isinstance(value, list):
        for index, item in enumerate(value):
            flattened.update(_flatten_mcp_values(item, f"{prefix}.{index}" if prefix else str(index)))
        return flattened
    if prefix:
        flattened[prefix] = str(value if value is not None else "")
    return flattened


def _render_mcp_reply(
    task: dict[str, Any],
    ai_result: dict[str, Any],
    mcp_result: dict[str, Any],
    identifier: str,
) -> str:
    structured = mcp_result.get("structured")
    text = str(mcp_result.get("text") or "").strip()
    try:
        mcp_json = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
    except (TypeError, ValueError):
        mcp_json = ""
    values = {
        "result": str(ai_result.get("result") or ""),
        "reply": str(ai_result.get("reply") or ""),
        "identifier": identifier,
        "mcp_text": text,
        "mcp_json": mcp_json,
        **_flatten_mcp_values(structured),
    }
    rendered = str(task.get("mcp_reply_template") or "{{mcp_text}}").strip()
    for key, value in sorted(values.items(), key=lambda item: len(item[0]), reverse=True):
        rendered = rendered.replace(f"{{{{mcp.{key}}}}}", value)
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered.strip()


def _mcp_reply_required_values(
    arguments: dict[str, Any],
    ai_result: dict[str, Any],
    mcp_result: dict[str, Any],
) -> tuple[str, str]:
    flattened = _flatten_mcp_values(mcp_result.get("structured"))
    identifier = (
        _extract_skill_identifier(ai_result)
        or str(arguments.get("identifier") or arguments.get("identifier_id") or "").strip()
        or flattened.get("identifier", "").strip()
    )
    work_num = (
        flattened.get("work_num", "").strip()
        or flattened.get("workNum", "").strip()
        or flattened.get("work_number", "").strip()
    )
    return identifier, work_num


def _ensure_mcp_reply_values(reply: str, identifier: str, work_num: str) -> str:
    text = str(reply or "").strip()
    missing: list[str] = []
    if identifier and identifier not in text:
        missing.append(f"需求 {identifier}")
    if work_num and work_num not in text:
        missing.append(f"需求编号：{work_num}")
    if missing:
        prefix = "，".join(missing)
        text = f"{prefix}\n{text}" if text else prefix
    return text


def _fallback_mcp_reply(
    task: dict[str, Any],
    result: dict[str, Any],
    called: dict[str, Any],
    identifier: str,
    work_num: str,
) -> str:
    parts: list[str] = []
    rendered = _render_mcp_reply(task, result, called, identifier)
    if rendered and rendered not in {"{{mcp_text}}", "{{mcp_json}}"}:
        parts.append(rendered)
    for skill_reply in _ai_result_replies(task, result):
        if skill_reply and not any(skill_reply in part for part in parts):
            parts.append(skill_reply)
    return _ensure_mcp_reply_values("\n".join(parts), identifier, work_num)


def _xlsx_xml_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _xlsx_xml_attr(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _xlsx_inline_cell(reference: str, value: Any, style_id: int = 0) -> str:
    text = _xlsx_xml_text(value)
    style = f' s="{style_id}"' if style_id else ""
    return (
        f'<c r="{_xlsx_xml_attr(reference)}" t="inlineStr"{style}>'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _problem_ticket_table_requested(source: Any) -> bool:
    return bool(re.search(r"表格|图表|报表|xlsx|excel", str(source or ""), re.IGNORECASE))


def _create_problem_ticket_table(payload: dict[str, Any], *, force: bool = False) -> dict[str, str] | None:
    """Create a styled OOXML workbook without using a spreadsheet object model."""
    items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    if len(items) <= 10 and not force:
        return None

    headers = ["ID标识", "概述", "状态", "填报时间", "信息中心处理时间", "受理人", "问题ID", "受理单位"]
    rows = [
        [
            item.get("identifier") or item.get("id_identifier") or item.get("id") or "",
            item.get("summary") or "",
            item.get("status") or "",
            item.get("submitted_at") or item.get("submit_time") or "",
            item.get("info_center_processed_at") or {"not_found": "暂无记录", "missing_time": "时间缺失",
                "error": "查询失败"}.get(item.get("info_center_time_status"), "未提供"),
            item.get("assignee") or "",
            item.get("problem_id") or item.get("iid") or "",
            item.get("assignee_unit") or "",
        ]
        for item in items
    ]
    excluded = "、".join(str(item) for item in payload.get("excluded_statuses") or ()) or "无"
    range_label = str(payload.get("range_label") or "指定范围")
    period = (
        f"{range_label}（{payload.get('date_from')} 至 {payload.get('date_to') or payload.get('date_from')}）"
        if payload.get("date_from")
        else f"{range_label}（平台当前账号可见的全部历史）"
    )
    title = "问题平台提单统计"
    subtitle = f"筛选：{'未完成；已排除：' + excluded if payload.get('incomplete_only') else '全部状态'}；时间范围：{period}；共 {len(rows)} 条；信息中心处理时间取首次履历发起时间"

    upload_dir = os.path.join(os.path.dirname(__file__), "_uploads", "smart_reply")
    os.makedirs(upload_dir, exist_ok=True)
    start = str(payload.get("date_from") or "")
    end = str(payload.get("date_to") or start)
    filename_range = (start if start == end else f"{start}至{end}") if start else "全部历史"
    filename_range = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename_range)
    filename = f"{filename_range}_问题平台提单统计.xlsx"
    report_dir = tempfile.mkdtemp(prefix="problem_tickets_report_", dir=upload_dir)
    filepath = os.path.join(report_dir, filename)

    def row_xml(row_number: int, cells: list[str], styles: list[int], height: int) -> str:
        parts = [f'<row r="{row_number}" ht="{height}" customHeight="1">']
        for column_number, (value, style_id) in enumerate(zip(cells, styles), 1):
            reference = f"{chr(64 + column_number)}{row_number}"
            parts.append(_xlsx_inline_cell(reference, value, style_id))
        parts.append("</row>")
        return "".join(parts)

    last_row = 4 + len(rows)
    body_xml = []
    for index, row in enumerate(rows, 5):
        alt = index % 2 == 0
        body_xml.append(row_xml(
            index,
            row,
            [7 if alt else 6, 5 if alt else 4, 9 if alt else 8, 7 if alt else 6,
             7 if alt else 6, 5 if alt else 4, 7 if alt else 6, 5 if alt else 4],
            36,
        ))

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <fileVersion appName="xl" lastEdited="7" lowestEdited="7" rupBuild="1"/>
  <workbookPr defaultThemeVersion="164011"/>
  <bookViews><workbookView xWindow="0" yWindow="0" windowWidth="24000" windowHeight="12000"/></bookViews>
  <sheets><sheet name="问题平台运维单" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="10"/><name val="Microsoft YaHei"/><family val="2"/></font>
    <font><b/><sz val="14"/><name val="Microsoft YaHei"/><family val="2"/></font>
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9EAF7"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF3F7FB"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left style="thin"><color rgb="FFD9E2F3"/></left><right style="thin"><color rgb="FFD9E2F3"/></right><top style="thin"><color rgb="FFD9E2F3"/></top><bottom style="thin"><color rgb="FFD9E2F3"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="10">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="0" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    worksheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:H{last_row}"/>
  <sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A5" sqref="A5"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="20"/>
  <cols>
    <col min="1" max="1" width="19" customWidth="1"/>
    <col min="2" max="2" width="44" customWidth="1"/>
    <col min="3" max="3" width="19" customWidth="1"/>
    <col min="4" max="4" width="24" customWidth="1"/>
    <col min="5" max="5" width="24" customWidth="1"/>
    <col min="6" max="6" width="18" customWidth="1"/>
    <col min="7" max="7" width="24" customWidth="1"/>
    <col min="8" max="8" width="30" customWidth="1"/>
  </cols>
  <sheetData>
    {row_xml(1, [title], [1], 30)}
    {row_xml(2, [subtitle], [2], 40)}
    <row r="3" ht="8" customHeight="1"/>
    {row_xml(4, headers, [3] * len(headers), 28)}
    {''.join(body_xml)}
  </sheetData>
  <autoFilter ref="A4:H{last_row}"/>
  <mergeCells count="2"><mergeCell ref="A1:H1"/><mergeCell ref="A2:H2"/></mergeCells>
  <pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>
</worksheet>'''
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>问题平台运维单清单</dc:title><dc:creator>Aixed</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{_xlsx_xml_text(now)}</dcterms:created></cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Microsoft Excel Compatible OOXML</Application><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop><HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant><vt:variant><vt:i4>1</vt:i4></vt:variant></vt:vector></HeadingPairs><TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>问题平台运维单</vt:lpstr></vt:vector></TitlesOfParts></Properties>'''

    try:
        with zipfile.ZipFile(filepath, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in {
                "[Content_Types].xml": content_types,
                "_rels/.rels": root_rels,
                "xl/workbook.xml": workbook,
                "xl/_rels/workbook.xml.rels": workbook_rels,
                "xl/styles.xml": styles,
                "xl/worksheets/sheet1.xml": worksheet,
                "docProps/core.xml": core,
                "docProps/app.xml": app,
            }.items():
                archive.writestr(name, content.encode("utf-8"))
    except Exception:
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise
    return {"filepath": filepath, "filename": filename, "cleanup_dir": report_dir}


async def _mcp_task_reply(
    task: dict[str, Any],
    result: dict[str, Any],
    *,
    attachments_out: list[dict[str, str]] | None = None,
) -> tuple[str, ...]:
    if attachments_out is None and isinstance(task.get("_attachments_out"), list):
        attachments_out = task["_attachments_out"]
    if is_unified_task(task) and not task.get("_unified_routed"):
        if not bool(result.get("matched")):
            return ()
        try:
            tool_name, routed_result = unified_route(task.get("_source_content"), result)
        except ValueError as exc:
            return (UNIFIED_SKILL_NAME + "：" + str(exc),)
        routed_task = {**task, "mcp_tool_name": tool_name, "_unified_routed": True}
        return await _mcp_task_reply(routed_task, routed_result, attachments_out=attachments_out)
    if str(task.get("mcp_tool_name") or "") == SUBMISSION_TOOL and "_source_content" in task:
        if not _ai_result_matches(task, result):
            return (SUBMISSION_REPLY_PREFIX + "：未识别到足够明确的填报请求，未提交。",)
        try:
            arguments = submission_arguments(task.get("_source_content"), result)
        except ValueError as exc:
            return submission_reply_chunks(SUBMISSION_REPLY_PREFIX + "：" + str(exc))
        context = task.get("_source_context") or {}
        if not all(context.get(key) for key in ("owner_wxid", "chat_id", "sender", "message_id")):
            return (SUBMISSION_REPLY_PREFIX + f"：已识别单号 {arguments['identifier']}；此为测试，未提交。",)
        connection = _mcp_connection(str(task.get("mcp_connection_id") or ""))
        if not connection or not bool(connection.get("enabled", True)):
            return (SUBMISSION_REPLY_PREFIX + "：数据运维连接未启用，未提交。",)
        try:
            called = await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""), tool_name=SUBMISSION_TOOL, arguments=arguments)
        except McpServiceError as exc:
            return submission_reply_chunks(SUBMISSION_REPLY_PREFIX + f"：需求 {arguments['identifier']} 提交调用响应异常，结果待核对，请勿重复提交：{exc}")
        return submission_reply_chunks(submission_reply(called.get("structured"), arguments["identifier"]))
    if str(task.get("mcp_tool_name") or "") == APPROVAL_TOOL:
        if not bool(result.get("matched")) or not _ai_result_matches(task, result):
            return (APPROVAL_REPLY_PREFIX + "：未识别到足够明确的审核指令，未审核。请说明：审核数据运维单 41366。",)
        context = task.get("_source_context") or {}
        history = _query_history_for(context, str(task.get("mcp_connection_id") or ""))
        try:
            action, target = approval_target(task.get("_source_content"), result, history)
        except ValueError as exc:
            return approval_reply_chunks(APPROVAL_REPLY_PREFIX + "：" + str(exc))
        connection = _mcp_connection(str(task.get("mcp_connection_id") or ""))
        if not connection or not bool(connection.get("enabled", True)):
            return (APPROVAL_REPLY_PREFIX + "：数据运维连接未启用，未审核。",)
        async def call_tool(name, arguments):
            return await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""), tool_name=name, arguments=arguments)
        try:
            dry_run = not all(context.get(key) for key in ("owner_wxid", "chat_id", "sender", "message_id"))
            reply = await approve_target(action, target, call_tool, dry_run=dry_run)
        except (ValueError, McpServiceError) as exc:
            return approval_reply_chunks(APPROVAL_REPLY_PREFIX + "：未完成审核请求：" + str(exc))
        return approval_reply_chunks(reply)
    if str(task.get("mcp_tool_name") or "") == EXECUTION_TOOL:
        if not bool(result.get("matched")) or not _ai_result_matches(task, result):
            return (EXECUTION_REPLY_PREFIX + "：未识别到足够明确的执行指令，未执行。请说明：执行 41184。",)
        context = task.get("_source_context") or {}
        history = _query_history_for(context, str(task.get("mcp_connection_id") or ""))
        batch = approval_execution_candidate(task.get("_source_content"))
        try:
            target = (approval_execution_targets(task.get("_source_content"), result, history) if batch else
                execution_target(task.get("_source_content"), result, history))
        except ValueError as exc:
            return (EXECUTION_REPLY_PREFIX + "：" + str(exc),)
        connection = _mcp_connection(str(task.get("mcp_connection_id") or ""))
        if not connection or not bool(connection.get("enabled", True)):
            return (EXECUTION_REPLY_PREFIX + "：数据运维连接未启用，未执行。",)
        async def call_tool(name, arguments):
            return await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""), tool_name=name, arguments=arguments)
        try:
            dry_run = not all(context.get(k) for k in ("owner_wxid", "chat_id", "sender", "message_id"))
            if batch:
                reply = await approve_execute_targets(target, call_tool, dry_run=dry_run)
                work_num = ""
            else:
                reply, work_num = await execute_target(target, call_tool, dry_run=dry_run)
        except (ValueError, McpServiceError) as exc:
            return execution_reply_chunks(EXECUTION_REPLY_PREFIX + "：未完成执行请求：" + str(exc))
        if work_num and all(context.get(k) for k in ("owner_wxid", "chat_id", "sender")):
            _cache().append_query_turn(context["chat_id"], context["sender"],
                query_memory_turn(f"需求 {target['identifier']} 的执行请求结果", {"query_type": "execution", "identifier": work_num}, reply,
                    message_id=context.get("message_id", ""), connection_id=str(task.get("mcp_connection_id") or "")), owner_wxid=context["owner_wxid"])
        return execution_reply_chunks(reply)
    if str(task.get("mcp_tool_name") or "") == QUERY_TOOL:
        if not bool(result.get("matched")):
            if query_clarification_candidate(task.get("_source_content")):
                return (QUERY_REPLY_PREFIX + "：暂时无法确定你的意思，请说明要查询哪类单据或任务，例如：有哪些待审核单子、有哪些待执行单子、查41184执行结果。需要执行时请明确单号和执行要求。",)
            return ()
        if not _ai_result_matches(task, result):
            return (QUERY_REPLY_PREFIX + "：查询意图未满足置信度要求，请重新说明。",)
        context = task.get("_source_context") or {}
        history = _query_history_for(context, str(task.get("mcp_connection_id") or "")) if all(context.get(k) for k in ("owner_wxid", "chat_id", "sender")) else task.get("_query_history") or []
        if not history and query_followup(task.get("_source_content")) and not re.search(r"执行|审核|审批|影响|需求|任务|处理|受理|履历|SQL\s*(?:原文|脚本)|完整\s*SQL", str(task.get("_source_content") or ""), re.I):
            return (QUERY_REPLY_PREFIX + "：当前没有可承接的查询上下文（会话最多保留一周），请补充单号和查询类型，例如：查一下 41198 任务处理情况。",)
        try:
            arguments = query_arguments(task.get("_source_content"), result, history)
        except ValueError as exc:
            return (QUERY_REPLY_PREFIX + "：" + str(exc),)
        query_prefix = PROBLEM_QUERY_REPLY_PREFIX if arguments.get("query_type") == "problem_tickets" else QUERY_REPLY_PREFIX
        connection = _mcp_connection(str(task.get("mcp_connection_id") or ""))
        if not connection or not bool(connection.get("enabled", True)):
            return (query_prefix + "：查询连接未启用，未取得结果。",)
        payload = None
        try:
            if arguments["query_type"] == "pending_maintenance":
                payload = {"query_type": "pending_maintenance", "results": []}
                for kind in ("pending_approvals", "pending_executions"):
                    try:
                        part_arguments = {"query_type": kind, "identifier": ""}
                        part_arguments.update({key: arguments[key] for key in ("date_from", "date_to") if key in arguments})
                        called = await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""),
                            tool_name=QUERY_TOOL, arguments=part_arguments)
                        part = called.get("structured")
                        if not isinstance(part, dict) or part.get("query_type") != kind:
                            raise McpServiceError("未取得该类别的结构化查询结果")
                    except McpServiceError as exc:
                        part = {"query_type": kind, "error": str(exc)}
                    payload["results"].append(part)
            else:
                tool_name, tool_arguments = query_tool_call(arguments)
                progress_state = (task.get("_source_context") or {}).get("_query_progress")
                call_options = {"url": str(connection.get("url") or ""), "token": str(connection.get("token") or ""),
                    "tool_name": tool_name, "arguments": tool_arguments}
                if isinstance(progress_state, dict):
                    update_progress(progress_state, {"phase": "remote"})
                if (arguments["query_type"] == "problem_tickets" and isinstance(progress_state, dict)
                        and callable(getattr(mcp_service, "call_tool_with_progress", None))):
                    called = await mcp_service.call_tool_with_progress(**call_options,
                        progress_callback=lambda snapshot: update_progress(progress_state, snapshot))
                else:
                    called = await mcp_service.call_tool(**call_options)
                payload = called.get("structured")
                if arguments["query_type"] == "problem_tickets":
                    if (not isinstance(payload, dict) or payload.get("query_type") != "problem_tickets"
                            or any(str(payload.get(key) or "") != str(arguments.get(key) or "")
                                for key in ("date_from", "date_to"))):
                        raise McpServiceError("问题平台返回的日期范围与本次请求不一致，已停止生成表格，请重新查询。")
                    payload = {**payload, "range_label": arguments["date_label"]}
        except McpServiceError as exc:
            payload = None
            reply = query_prefix + "：查询失败，处理情况、执行结果和数量未知：" + str(exc)
        else:
            reply = query_reply(payload, include_sql=arguments["query_type"] == "execution" and execution_details_requested(task.get("_source_content")))
            if (arguments["query_type"] == "problem_tickets" and isinstance(payload, dict)
                    and not payload.get("count_only") and attachments_out is not None):
                progress_state = (task.get("_source_context") or {}).get("_query_progress")
                if isinstance(progress_state, dict):
                    update_progress(progress_state, {"phase": "formatting"})
                attachment = await asyncio.to_thread(_create_problem_ticket_table, payload,
                    force=_problem_ticket_table_requested(task.get("_source_content")))
                if attachment:
                    attachments_out.append(attachment)
                    if len(payload.get("items") or []) <= 10 and "完整明细已整理为表格文件发送" not in reply:
                        reply += "\n完整明细已整理为表格文件发送。"
                    _smart_reply_log(
                        f"[MCP_REPLY] created problem query table file={attachment['filename']} "
                        f"rows={len(payload.get('items') or [])}"
                    )
        context = task.get("_source_context") or {}
        if all(context.get(k) for k in ("owner_wxid", "chat_id", "sender")):
            _cache().append_query_turn(context["chat_id"], context["sender"],
                query_memory_turn(task.get("_source_content"), arguments, query_reply(payload) if isinstance(payload, dict) else reply,
                    message_id=context.get("message_id", ""), connection_id=str(task.get("mcp_connection_id") or ""), payload=payload),
                owner_wxid=context["owner_wxid"])
        return query_reply_chunks(reply)
    if str(task.get("mcp_tool_name") or "") == WORKFLOW_TOOL:
        if not bool(result.get("matched")):
            return ()
        try:
            arguments = workflow_arguments(task.get("_source_content"), result, task)
        except ValueError as exc:
            return (str(exc),)
        if not _ai_result_matches(task, result):
            return ("SQL 分析未满足置信度要求，未填报、审核或执行。",)
        connection = _mcp_connection(str(task.get("mcp_connection_id") or ""))
        if not connection or not bool(connection.get("enabled", True)):
            return ("数据运维连接未启用，未启动流程。",)
        if len(arguments["identifier"]) == 4 and not arguments["dry_run"]:
            try:
                return offline_reply_chunks(await _prepare_offline_city_confirmation(task, arguments))
            except ValueError as exc:
                return (f"线下单 {arguments['identifier']}：{exc}；未填报、审核或执行。",)
        context = task.get("_source_context") or {}
        pending = OfflineConfirmationStore(_cache().db_path).get(context)
        if pending and pending.get("numbered_city_selection") and not arguments["dry_run"]:
            return split_reply(selection_message(pending))
        try:
            called = await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""),
                tool_name=WORKFLOW_TOOL, arguments=arguments)
        except McpServiceError as exc:
            return (f"需求 {arguments['identifier']} 数据运维流程未完成：{exc}\n结果待核对，请勿重新提交或执行。流程标识：{arguments['request_id']}",)
        reply = workflow_reply(called.get("structured"), arguments)
        if not arguments["dry_run"] and city_selection_failure(called.get("structured")):
            return split_reply(await _prepare_workflow_city_selection(task, arguments, called["structured"]))
        if not arguments.get("dry_run") and all(context.get(k) for k in ("owner_wxid", "chat_id", "sender")):
            _cache().append_query_turn(context["chat_id"], context["sender"],
                query_memory_turn(f"需求 {arguments['identifier']} 的 SQL 流程结果", {"query_type": "execution", "identifier": arguments["identifier"]}, reply,
                    message_id=context.get("message_id", ""), connection_id=str(task.get("mcp_connection_id") or "")), owner_wxid=context["owner_wxid"])
        return split_reply(reply)
    if not _ai_result_matches(task, result):
        return ()
    connection_id = str(task.get("mcp_connection_id") or "")
    connection = _mcp_connection(connection_id)
    if connection is None:
        raise McpServiceError(f"MCP connection not found: {connection_id}")
    if not bool(connection.get("enabled", True)):
        raise McpServiceError(f"MCP connection is disabled: {connection_id}")
    tool_name = str(task.get("mcp_tool_name") or "")
    if tool_name:
        arguments, identifier = _render_mcp_arguments(task, result)
    else:
        discovered = await mcp_service.discover(
            url=str(connection.get("url") or ""),
            token=str(connection.get("token") or ""),
        )
        tools = [tool for tool in (discovered.get("tools") or []) if isinstance(tool, dict)]
        if not hasattr(ai_service, "select_mcp_tool"):
            raise McpServiceError("AI service does not support automatic MCP tool selection")
        try:
            selected = await ai_service.select_mcp_tool(
                str(task.get("_source_content") or ""),
                task,
                result,
                tools,
            )
        except AiServiceError as exc:
            raise McpServiceError(f"AI MCP tool selection failed: {exc}") from exc
        if not bool(selected.get("should_call")):
            return _ai_result_replies(task, result)
        tool_name = str(selected.get("tool_name") or "").strip()
        available_names = {str(tool.get("name") or "") for tool in tools}
        if not tool_name or tool_name not in available_names:
            raise McpServiceError(f"AI selected an unavailable MCP tool: {tool_name or '(empty)'}")
        arguments = selected.get("arguments")
        if not isinstance(arguments, dict):
            raise McpServiceError("AI MCP tool arguments must be a JSON object")
        identifier = _extract_skill_identifier(result)
    called = await mcp_service.call_tool(
        url=str(connection.get("url") or ""),
        token=str(connection.get("token") or ""),
        tool_name=tool_name,
        arguments=arguments,
    )
    structured = called.get("structured")
    if (
        tool_name == "submit_data_maintenance"
        and isinstance(structured, dict)
        and structured.get("submitted") is False
    ):
        raise McpServiceError("MCP data maintenance request was not submitted")
    if str(task.get("output_mode") or "result") == "silent":
        return ()
    identifier, work_num = _mcp_reply_required_values(arguments, result, called)
    reply = ""
    if hasattr(ai_service, "compose_mcp_reply") and bool(getattr(ai_service, "configured", False)):
        try:
            reply = await ai_service.compose_mcp_reply(
                message=str(task.get("_source_content") or ""),
                task=task,
                analysis=result,
                tool_name=tool_name,
                arguments=arguments,
                mcp_result={"text": called.get("text"), "structured": called.get("structured")},
                identifier=identifier,
                work_num=work_num,
            )
        except AiServiceError as exc:
            _smart_reply_log(
                f"[MCP_REPLY] final AI composition failed id={task.get('id', '')}: {type(exc).__name__}: {exc}"
            )
    if not reply:
        reply = _fallback_mcp_reply(task, result, called, identifier, work_num)
    reply = _ensure_mcp_reply_values(reply, identifier, work_num)
    if not bool(task.get("preserve_formatting", True)):
        reply = " ".join(reply.split())
    return (reply,) if reply else ()


def _preview_text(value: Any, limit: int = 80) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


_SMART_REPLY_TXT_MAX_BYTES = 2 * 1024 * 1024
_SMART_REPLY_AI_INPUT_MAX_CHARS = 20_000
_SMART_REPLY_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_LOCAL_MEDIA_SETTLE_SECONDS = 6.0
_LOCAL_MEDIA_SETTLE_INTERVAL_SECONDS = 0.2


def _read_smart_reply_txt(path: str) -> tuple[str, str]:
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", "missing_file"
    if size <= 0:
        return "", "empty_file"
    if size > _SMART_REPLY_TXT_MAX_BYTES:
        return "", "oversized_file"
    try:
        with open(path, "rb") as source:
            payload = source.read(_SMART_REPLY_TXT_MAX_BYTES + 1)
    except OSError:
        return "", "unreadable_file"
    if len(payload) > _SMART_REPLY_TXT_MAX_BYTES:
        return "", "oversized_file"

    decoded: str | None = None
    encodings = ("utf-16",) if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            decoded = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        return "", "decode_failed"
    if "\x00" in decoded:
        return "", "invalid_file_content"
    decoded = decoded.strip()
    if not decoded:
        return "", "empty_file"
    return decoded, "ok"


async def _smart_reply_file_ai_content(
    message: dict[str, Any],
    enabled_file_types: list[str] | None,
) -> tuple[str, str]:
    msg_xml = str(message.get("msg") or "")
    local_path = str(message.get("file_path") or message.get("path") or "").strip()
    filename_hint = str(message.get("filename") or "").strip()
    if not filename_hint and local_path:
        filename_hint = os.path.basename(local_path)
    params = _parse_media_download_params("49", msg_xml, "")
    parsed_filename = os.path.basename(str(params.get("filename") or "").replace("\\", "/"))
    filename = filename_hint if parsed_filename == "wechat_media.bin" else parsed_filename
    filename = os.path.basename(str(filename or filename_hint or "wechat_media.bin").replace("\\", "/"))
    extension = os.path.splitext(filename)[1].lower().lstrip(".")
    configured_types = {
        str(value or "").strip().lower().lstrip(".")
        for value in (enabled_file_types if isinstance(enabled_file_types, list) else ["txt"])
    }
    if extension != "txt" or "txt" not in configured_types:
        return "", f"unsupported_file_type:{extension or 'unknown'}"

    declared_size = message.get("file_len")
    try:
        if declared_size not in (None, "") and int(declared_size) > _SMART_REPLY_TXT_MAX_BYTES:
            return "", "oversized_file"
    except (TypeError, ValueError):
        pass

    try:
        path, resolved_filename = await _resolve_media_path(
            msg_id=str(message.get("id") or ""),
            msg_type="49",
            local_path=local_path,
            msg_xml=msg_xml,
            filename=filename,
        )
    except Exception as exc:
        try:
            path_exists = os.path.exists(local_path)
            path_size = os.path.getsize(local_path) if path_exists else 0
        except OSError:
            path_exists = False
            path_size = 0
        detail = str(getattr(exc, "detail", exc) or "")
        _smart_reply_log(
            f"[SMART_REPLY] file resolve failed message_id={message.get('id', '')} "
            f"path={local_path!r} exists={path_exists} size={path_size} "
            f"error={type(exc).__name__}: {detail}"
        )
        return "", f"resolve_failed:{type(exc).__name__}"
    filename = os.path.basename(str(resolved_filename or filename).replace("\\", "/")) or filename
    content, reason = await asyncio.to_thread(_read_smart_reply_txt, path)
    if reason != "ok":
        return "", reason

    prefix = f"文件名：{filename}\n文件内容：\n"
    available_chars = max(0, _SMART_REPLY_AI_INPUT_MAX_CHARS - len(prefix))
    if len(content) > available_chars:
        return "", "input_too_large"
    return prefix + content, "ok"


def _ocr_response_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = re.sub(r"[^a-z]", "", str(key).casefold())
            if normalized_key in {"rettext", "ocrtext", "text"}:
                text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                if text:
                    return text
        for value in payload.values():
            nested = _ocr_response_text(value)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _ocr_response_text(value)
            if nested:
                return nested
    return ""


async def _smart_reply_image_ocr(
    message: dict[str, Any],
    agent_id: str,
) -> tuple[str, str, str]:
    msg_xml = str(message.get("msg") or "")
    params = _parse_media_download_params("3", msg_xml, "")
    if (not params["aeskey"] or not params["fileid"]) and message.get("id"):
        try:
            with wechat_api.use_agent(agent_id), wechat_api.use_log_category("smart_reply"):
                structure = await wechat_api.get_msg_struct(str(message.get("id") or ""))
            structure_xml, structure_type = _message_struct_content(structure)
            if structure_xml and structure_type in {"", "3"}:
                msg_xml = structure_xml
                params = _parse_media_download_params("3", msg_xml, "")
        except Exception as exc:
            return "", msg_xml, f"message_struct_failed:{type(exc).__name__}"
    if not params["aeskey"] or not params["fileid"]:
        return "", msg_xml, "missing_ocr_parameters"
    try:
        with wechat_api.use_agent(agent_id), wechat_api.use_log_category("smart_reply"):
            result = await wechat_api.ocr_recognizes(params["aeskey"], params["fileid"])
    except Exception as exc:
        return "", msg_xml, f"ocr_failed:{type(exc).__name__}"
    text = _ocr_response_text(result)
    return text, msg_xml, "ok" if text else "ocr_empty"


def _image_data_url_from_path(path: str) -> tuple[str, str]:
    path = _ensure_browser_image_file(path)
    if not _nonempty_file(path):
        return "", "missing_image"
    try:
        size = os.path.getsize(path)
        if size > _SMART_REPLY_IMAGE_MAX_BYTES:
            return "", "oversized_image"
        with open(path, "rb") as source:
            payload = source.read(_SMART_REPLY_IMAGE_MAX_BYTES + 1)
    except OSError:
        return "", "unreadable_image"
    if len(payload) > _SMART_REPLY_IMAGE_MAX_BYTES:
        return "", "oversized_image"
    media_type = _image_media_type_for_path(path)
    if not media_type.startswith("image/"):
        return "", "unsupported_image"
    return f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}", "ok"


async def _smart_reply_image_data_url(
    message: dict[str, Any],
    msg_xml: str,
    agent_id: str,
) -> tuple[str, str]:
    try:
        with wechat_api.use_agent(agent_id), wechat_api.use_log_category("smart_reply"):
            path, _ = await _resolve_media_path(
                msg_id=str(message.get("id") or ""),
                msg_type="3",
                local_path=str(message.get("img_path") or message.get("path") or ""),
                msg_xml=msg_xml,
                filename="wechat_image.jpg",
            )
    except Exception as exc:
        return "", f"resolve_failed:{type(exc).__name__}"
    return await asyncio.to_thread(_image_data_url_from_path, path)


async def _offline_quote_image(context):
    quote = sql_quote(context.get("source_message") or {})
    if not quote or not quote["image"]:
        return ""
    image = quote["image"]
    cached = _cache().get_message_by_id(image["id"], owner_wxid=context["owner_wxid"]) if image["id"] else None
    if cached and str(cached.get("msgtype")) == "3":
        image = {**cached, **{k: v for k, v in image.items() if v}}
    if image.get("db_image_id"):
        blob = _cache().get_media_blob(image["db_image_id"])
        if blob and blob.get("data"):
            if len(blob["data"]) > _SMART_REPLY_IMAGE_MAX_BYTES:
                raise ValueError("引用图片超过 8 MiB，请重新提供原图")
            return "data:" + str(blob.get("mime_type") or "image/png") + ";base64," + base64.b64encode(blob["data"]).decode("ascii")
    local = str(image.get("img_path") or "")
    if local:
        data_url, reason = await asyncio.to_thread(_image_data_url_from_path, local)
        if data_url:
            return data_url
    try:
        with wechat_api.use_agent(str(context.get("agent_id") or "")), wechat_api.use_log_category("smart_reply"):
            resolved = await _download_image_payload({"msg_id": image["id"], "msg_xml": image["msg"],
                "bytes_extra_hex": image.get("bytesExtraHex") or "", "quoted": True}, str(context.get("agent_id") or ""))
        if not isinstance(resolved, FileResponse):
            raise ValueError(str((resolved or {}).get("error") or "未获得图片文件"))
        data_url, reason = await asyncio.to_thread(_image_data_url_from_path, str(resolved.path))
        if not data_url:
            raise ValueError(reason)
        return data_url
    except Exception as exc:
        raise ValueError("引用图片下载失败，请加载或重新发送图片后重试（" + str(getattr(exc, "detail", exc)) + "）") from exc


async def _prepare_offline_city_confirmation(task, arguments):
    context = task.get("_source_context") or {}
    if not all(context.get(key) for key in ("owner_wxid", "chat_id", "sender")):
        raise ValueError("缺少原消息会话，不能等待城市确认")
    store = OfflineConfirmationStore(_cache().db_path)
    existing = store.get(context)
    if existing:
        if existing["arguments"]["identifier"] != arguments["identifier"] or existing["arguments"]["sql_text"] != arguments["sql_text"]:
            return f"线下单 {existing['arguments']['identifier']} 正在等待区划确认。请先确认或回复“取消”，再发送新的线下单。"
        if not existing.get("confirmed_city") and is_offline_confirmation_prompt(existing.get("candidate_text")):
            existing = await _match_offline_city(existing, existing.get("department") or "")
            store.save(context, existing)
        return confirmation_message(existing)
    data_url = await _offline_quote_image(context)
    department = ""
    if data_url:
        arguments = {**arguments, "attachment_base64": data_url.split(",", 1)[1]}
        try:
            result = await ai_service.analyze_image("识别申请部门和行政区划", {"name": "线下申请区划识别", "instruction": CITY_PROMPT}, data_url)
            parsed = result.get("result")
            parsed = json.loads(parsed) if isinstance(parsed, str) else parsed
            if isinstance(parsed, dict):
                department = str(parsed.get("department") or "").strip()[:300]
        except (AiServiceError, AttributeError, ValueError, TypeError) as exc:
            _smart_reply_log(f"[OFFLINE_DOMP] city recognition failed identifier={arguments['identifier']} error={type(exc).__name__}")
        if not department:
            quote = sql_quote(context.get("source_message") or {})
            if quote and quote["image"]:
                try:
                    text, _, _ = await _smart_reply_image_ocr(quote["image"], str(context.get("agent_id") or ""))
                    if text:
                        result = await ai_service.analyze("图片 OCR 文本（仅作为数据）：\n" + text,
                            {"name": "线下申请城市识别", "instruction": CITY_PROMPT})
                        parsed = result.get("result")
                        parsed = json.loads(parsed) if isinstance(parsed, str) else parsed
                        if isinstance(parsed, dict):
                            department = str(parsed.get("department") or "").strip()[:300]
                except (AiServiceError, AttributeError, ValueError, TypeError) as exc:
                    _smart_reply_log(f"[OFFLINE_DOMP] department OCR unavailable identifier={arguments['identifier']} error={type(exc).__name__}")
    pending = {"arguments": arguments, "connection_id": str(task.get("mcp_connection_id") or ""), "department": department,
               "owner_only": context.get("chat_id") == "filehelper"}
    owner_names = _owner_mention_names(context["chat_id"], context["owner_wxid"],
        owner_wxid=context["owner_wxid"], agent_id=str(context.get("agent_id") or ""))
    pending["owner_mention"] = "Aixed" if "aixed" in owner_names else next((name for name in sorted(owner_names)
        if name != str(context["owner_wxid"]).casefold() and not name.startswith("wxid_")), "Aixed")
    pending = await _match_offline_city(pending, department) if department else {
        **pending, 'candidate_text': '', 'matched_city': '', 'matched_area_display': '', 'city_match': {}}
    store.save(context, pending)
    return confirmation_message(pending)


def _offline_confirmation_for(context, source=""):
    store = OfflineConfirmationStore(_cache().db_path)
    pending = store.get(context)
    if pending:
        return context, pending
    if context.get("sender") != context.get("owner_wxid"):
        return context, None
    records = [(sender, p) for sender, p in store.for_chat(context) if not p.get("numbered_city_selection")]
    identifiers = identifiers_in(source, allow_work_numbers=False)
    if identifiers:
        records = [(sender, p) for sender, p in records if p["arguments"]["identifier"] in identifiers]
    if len(records) == 1:
        sender, pending = records[0]
        return {**context, "sender": sender}, pending
    return context, None


async def _match_offline_city(pending, city_text):
    connection = _mcp_connection(pending["connection_id"])
    pending = {**pending, "candidate_text": city_text, "matched_city": "", "matched_area_display": "", "city_match": {}, "match_error": ""}
    if not connection or not connection.get("enabled", True):
        pending["match_error"] = "数据运维连接未启用，请恢复后输入城市或县区"
        return pending
    try:
        called = await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""),
            tool_name="match_data_maintenance_city", arguments={"city_text": city_text})
        matched = called.get("structured")
        if not isinstance(matched, dict) or not any(isinstance(matched.get(key), list) for key in ("supported_areas", "supported_cities")):
            raise ValueError("未取得系统支持的行政区划，请确认 MCP 服务已更新")
        area = str(matched.get("area_name") or matched.get("city") or "")
        if matched.get("matched") is True:
            if isinstance(matched.get("supported_areas"), list):
                supported = [record for record in matched["supported_areas"] if isinstance(record, dict)
                    and record.get("area_name") == area and record.get("area_code") == matched.get("area_code")]
                verified = len(supported) == 1
            else:
                verified = area in matched["supported_cities"]
            if not area or not matched.get("area_code") or not verified:
                raise ValueError("区划匹配结果未包含可核对的系统名称和编码")
        pending.update(city_match=matched, matched_city=area if matched.get("matched") is True else "",
            matched_area_display=str(matched.get("area_display") or area) if matched.get("matched") is True else "")
    except (McpServiceError, ValueError) as exc:
        pending["match_error"] = str(exc)
    return pending


async def _resume_offline_city_confirmation(source, context, pending, tasks):
    if pending.get("numbered_city_selection"):
        return await _resume_workflow_city_selection(source, context, pending, tasks)
    store = OfflineConfirmationStore(_cache().db_path)
    action, city_text = city_input(source, pending["arguments"]["identifier"])
    if action == "cancel":
        store.delete(context)
        if pending.get("confirmed_city"):
            return f"已结束线下单 {pending['arguments']['identifier']} 的本次会话等待。此前已发起流程，请查询结果；不能据此认定业务操作已取消。"
        return f"已取消线下单 {pending['arguments']['identifier']}，未填报、审核或执行。"
    if not action:
        return confirmation_message(pending)
    if pending.get("confirmed_city"):
        return confirmation_message(pending)
    connection_id = pending["connection_id"]
    if not any(task.get("mcp_connection_id") == connection_id and task.get("mcp_enabled") and (
        is_unified_task(task) or task.get("mcp_tool_name") == WORKFLOW_TOOL) for task in tasks):
        return "原线下单的自动处理配置已变化，请恢复原连接或取消后重新发送。未填报、审核或执行。"
    connection = _mcp_connection(connection_id)
    if not connection or not connection.get("enabled", True):
        return "数据运维连接未启用，区划确认已保留，未启动流程。"
    if action == "confirm" and not pending.get("confirmed_city") and is_offline_confirmation_prompt(pending.get("candidate_text")):
        # Recover waiting records polluted by an automatic SendAtMsg callback.
        # Show the restored target before accepting a fresh confirmation.
        pending = await _match_offline_city(pending, pending.get("department") or "")
        store.save(context, pending)
        return confirmation_message(pending)
    if action == "input" or not (pending.get("city_match") or {}).get("matched"):
        pending = await _match_offline_city(pending, city_text if action == "input" else pending.get("candidate_text") or pending.get("department") or "")
        if action == 'input':
            pending['city_input_provided'] = True
        store.save(context, pending)
        if action != 'input' or not (pending.get('city_match') or {}).get('matched'):
            return confirmation_message(pending)
    if "supported_areas" not in pending["city_match"] and not pending.get("confirmed_city"):
        # Old waiting records may have collapsed a county to its parent city.
        previous_code = pending["city_match"].get("area_code")
        pending = await _match_offline_city(pending, pending.get("candidate_text") or pending.get("department") or pending["matched_city"])
        store.save(context, pending)
        if not pending["city_match"].get("matched") or (action != 'input' and pending["city_match"].get("area_code") != previous_code):
            return confirmation_message(pending)
    city = pending["matched_city"]
    arguments = {**pending["arguments"], "city": city}
    store.save(context, {**pending, "confirmed_city": city})
    try:
        called = await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""),
            tool_name=WORKFLOW_TOOL, arguments=arguments)
    except McpServiceError as exc:
        return f"线下单 {arguments['identifier']} 区划已确认为 {pending.get('matched_area_display') or city}。流程结果待核对：{exc}。请查询流程标识 {arguments['request_id']}，勿重复提交。"
    reply = f"线下单行政区划：{pending.get('matched_area_display') or city}\n" + workflow_reply(called.get("structured"), arguments)
    if city_selection_failure(called.get("structured")):
        task = {"mcp_connection_id": connection_id, "_source_context": context}
        store.delete(context)
        return await _prepare_workflow_city_selection(task, arguments, called["structured"])
    store.delete(context)
    if isinstance(called.get("structured"), dict):
        _cache().append_query_turn(context["chat_id"], context["sender"],
            query_memory_turn(f"需求 {arguments['identifier']} 的 SQL 流程结果", {"query_type": "execution", "identifier": arguments["identifier"]}, reply,
                message_id=context.get("message_id", ""), connection_id=connection_id), owner_wxid=context["owner_wxid"])
    return reply


async def _prepare_workflow_city_selection(task, arguments, payload):
    context = task.get("_source_context") or {}
    if not all(context.get(key) for key in ("owner_wxid", "chat_id", "sender")):
        return workflow_reply(payload, arguments)
    store = OfflineConfirmationStore(_cache().db_path)
    existing = store.get(context)
    if existing:
        return selection_message(existing) if existing.get("numbered_city_selection") else confirmation_message(existing)
    names = _owner_mention_names(context["chat_id"], context["owner_wxid"],
        owner_wxid=context["owner_wxid"], agent_id=str(context.get("agent_id") or ""))
    pending = {"numbered_city_selection": True, "arguments": arguments,
        "connection_id": str(task.get("mcp_connection_id") or ""), "failure": payload,
        "owner_only": context["chat_id"] == "filehelper",
        "owner_mention": "Aixed" if "aixed" in names else next((name for name in sorted(names)
            if name != str(context["owner_wxid"]).casefold() and not name.startswith("wxid_")), "Aixed")}
    pending = await _refresh_workflow_city_options(pending)
    store.save(context, pending)
    return selection_message(pending)


async def _refresh_workflow_city_options(pending):
    # The selection list comes from a read-only platform catalog, never from AI.
    matched = await _match_offline_city(pending, "")
    options = selection_options(pending["failure"], (matched.get("city_match") or {}).get("supported_areas") or [])
    return {**matched, "city_options": options}


async def _resume_workflow_city_selection(source, context, pending, tasks):
    store = OfflineConfirmationStore(_cache().db_path)
    action, _ = city_input(source, pending["arguments"]["identifier"])
    if action == "cancel":
        store.delete(context)
        return CITY_SELECTION_PREFIX + ("：已结束等待；此前已发起流程，请先查询结果。" if pending.get("confirmed_city")
            else "：已取消，本次未填报、审核或执行。")
    if pending.get("confirmed_city"):
        return CITY_SELECTION_PREFIX + f"：已按 {pending['confirmed_city']} 发起处理，请查询需求 {pending['arguments']['identifier']} 的结果，勿重复提交。"
    connection_id = pending["connection_id"]
    if not any(task.get("mcp_connection_id") == connection_id and task.get("mcp_enabled") and task.get("enabled")
        and (is_unified_task(task) or task.get("mcp_tool_name") == WORKFLOW_TOOL) for task in tasks):
        return CITY_SELECTION_PREFIX + "：原流程配置已变化，未继续填报，请恢复原连接后再选择。"
    connection = _mcp_connection(connection_id)
    if not connection or not connection.get("enabled", True):
        return CITY_SELECTION_PREFIX + "：数据运维连接未启用，已保留选项，未启动流程。"
    number = selection_number(source, pending["arguments"]["identifier"])
    options = pending.get("city_options") or []
    if not options:
        pending = await _refresh_workflow_city_options(pending)
        store.save(context, pending)
        return selection_message(pending)
    if number is None:
        return selection_message(pending)
    if not 1 <= number <= len(options):
        return f"{CITY_SELECTION_PREFIX}：编号无效，请选择 1—{len(options)}。\n" + selection_message(pending)
    selected = options[number - 1]
    verified = await _match_offline_city(pending, selected["area_name"])
    match = verified.get("city_match") or {}
    if not (match.get("matched") is True and match.get("area_name") == selected["area_name"]
            and match.get("area_code") == selected["area_code"]):
        # Keep published numbering stable on network errors; never retarget a number.
        return CITY_SELECTION_PREFIX + "：所选区划暂未通过系统名称和编码校验，未填报，请稍后重新选择。\n" + selection_message(pending)
    arguments = {**pending["arguments"], "city": selected["area_name"]}
    pending = {**pending, "confirmed_city": selected["area_name"]}
    store.save(context, pending)
    try:
        called = await mcp_service.call_tool(url=str(connection.get("url") or ""), token=str(connection.get("token") or ""),
            tool_name=WORKFLOW_TOOL, arguments=arguments)
    except McpServiceError:
        return CITY_SELECTION_PREFIX + f"：已选择 {selected['area_name']}，流程结果待核对，请查询需求 {arguments['identifier']}，勿重复提交。"
    payload = called.get("structured")
    if city_selection_failure(payload):
        pending = await _refresh_workflow_city_options({**pending, "failure": payload, "confirmed_city": ""})
        store.save(context, pending)
        return CITY_SELECTION_PREFIX + "：服务端未完成区划续办，请确认远程 MCP 已同步更新。\n" + selection_message(pending)
    reply = CITY_SELECTION_PREFIX + f"：已选择 {selected['area_name']}。\n" + workflow_reply(payload, arguments)
    if isinstance(payload, dict) and (payload.get("execution") or {}).get("complete"):
        store.delete(context)
    _cache().append_query_turn(context["chat_id"], context["sender"],
        query_memory_turn(f"需求 {arguments['identifier']} 的 SQL 流程结果", {"query_type": "execution", "identifier": arguments["identifier"]}, reply,
            message_id=context.get("message_id", ""), connection_id=connection_id), owner_wxid=context["owner_wxid"])
    return reply


def _query_history_for(context, connection_id=""):
    if not context or not all(context.get(k) for k in ("owner_wxid", "chat_id", "sender")):
        return []
    session = _cache().get_query_conversation(context["chat_id"], context["sender"], owner_wxid=context["owner_wxid"])
    return [t for t in (session or {}).get("turns", []) if t.get("connection_id", "") == connection_id]


_RECENT_SQL_FILE_MAX_AGE_SECONDS = 24 * 60 * 60


def _recent_chat_sql_identifier(source: str) -> str:
    """Read the explicit offline or old ticket id from the SQL header."""
    source = str(source or "")
    match = SQL_START.search(source)
    if not match:
        return ""
    identifiers = identifiers_in(source[:match.start()], allow_work_numbers=False)
    return next(iter(identifiers)) if len(identifiers) == 1 else ""


_SQL_MENTION_REFRESH_TTL_SECONDS = 300.0
_sql_mention_refresh_at: dict[tuple[str, str], float] = {}


async def _recent_sql_workflow_content(source, context):
    """Bind an explicit combined request to recent, exact-id TXT or chat SQL."""
    identifier = submission_workflow_identifier(source)
    if not identifier or not context or not all(context.get(key) for key in ("owner_wxid", "chat_id", "sender")):
        return "", "not_combined_or_missing_context"
    try:
        current_timestamp = int(context.get("message_timestamp") or time.time())
    except (TypeError, ValueError):
        current_timestamp = int(time.time())
    messages = _cache().get_messages(
        context["chat_id"], limit=100, before=current_timestamp + 1,
        owner_wxid=context["owner_wxid"],
    )
    filename_pattern = re.compile(
        rf"^(?:(?:需求|xzsb-)\s*)?{re.escape(identifier)}(?:\s*\(\d+\))?\.txt$",
        re.I,
    )
    last_reason = "matching_sql_content_not_found"
    for message in reversed(messages):
        try:
            message_timestamp = int(message.get("timestamp") or message.get("time_unix") or 0)
        except (TypeError, ValueError):
            message_timestamp = 0
        if message_timestamp and current_timestamp - message_timestamp > _RECENT_SQL_FILE_MAX_AGE_SECONDS:
            break
        if (message_timestamp > current_timestamp
                or str(message.get("fromid") or "") != str(context["sender"])):
            continue
        msg_type = str(message.get("msgtype") or "")
        quote = sql_quote(message)
        if msg_type == "49" and quote is None:
            local_name = os.path.basename(str(message.get("file_path") or "").replace("\\", "/"))
            title_match = re.search(r"<title>(.*?)</title>", str(message.get("msg") or ""), re.I | re.S)
            xml_name = html.unescape(title_match.group(1).strip()) if title_match else ""
            if not any(filename_pattern.fullmatch(name.strip()) for name in (xml_name, local_name) if name):
                continue
            content, reason = await _smart_reply_file_ai_content(message, ["txt"])
            if not content:
                last_reason = reason
                continue
            if not SQL_START.search(content):
                last_reason = "matching_txt_has_no_sql"
                continue
            return f"操作要求：填报运维单 {identifier}，审核通过后执行。\n{content}", "ok"

        if msg_type != "1" and quote is None:
            continue
        sql_message = {**message, "msgtype": "1", "msg": quote["text"]} if quote else message
        content = str(sql_message.get("msg") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        content = strip_chat_mentions(
            content,
            await _resolve_sql_chat_mention_names(
                sql_message,
                str(context["chat_id"]),
                str(context["owner_wxid"]),
            ),
        )
        if not SQL_START.search(content):
            continue
        if _recent_chat_sql_identifier(content) != identifier:
            last_reason = "matching_chat_sql_identifier_not_found"
            continue
        prefix = f"操作要求：填报运维单 {identifier}，审核通过后执行。\n聊天内容：\n"
        if len(content) > max(0, _SMART_REPLY_AI_INPUT_MAX_CHARS - len(prefix)):
            last_reason = "input_too_large"
            continue
        context["source_message"] = message
        return prefix + content, "ok"
    return "", last_reason


_SMART_REPLY_AGENT_MENTION_NAMES = frozenset({"空气"})
_SMART_REPLY_AGENT_MENTION_PATTERN = re.compile(r"@空气(?=$|[\s\u2005,，:：])", re.I)


def _sql_chat_mention_names(message, chat_id, owner_wxid):
    targets = _mention_targets(message)
    if str(message.get("msgtype")) != "1" or not chat_id.endswith("@chatroom"):
        return []
    members = _cache().get_group_members(chat_id, owner_wxid=owner_wxid)
    if targets:
        verified_targets = [target for target in targets if target in members]
        contacts = _cache().get_contacts(verified_targets, owner_wxid=owner_wxid)
        records = [
            record
            for target in verified_targets
            for record in (members.get(target) or {}, contacts.get(target) or {})
        ]
        names = set(verified_targets)
    else:
        if "@" not in str(message.get("msg") or ""):
            return []
        records = list(members.values())
        names = set()
    for record in records:
        for data in (record, record.get("profile") or {}):
            if isinstance(data, dict):
                names.update(
                    str(data[key])
                    for key in (
                        "name", "nickname", "nick_name", "NickName", "display_name", "displayname",
                        "DisplayName", "remark", "Remark", "markname", "MarkName",
                    )
                    if data.get(key)
                )
    names.update(_SMART_REPLY_AGENT_MENTION_NAMES)
    return list(names)


def _identity_aliases(record: dict[str, Any] | None) -> set[str]:
    """Return wxid/name aliases from a cached contact or group-member row."""
    if not isinstance(record, dict):
        return set()
    aliases: set[str] = set()
    for data in (record, record.get("profile") or {}, record.get("data") or {}):
        if not isinstance(data, dict):
            continue
        for key in (
            "wxid", "userName", "username", "UserName", "name", "nickname", "nick_name", "NickName",
            "display_name", "displayname", "DisplayName", "remark", "Remark", "markname", "MarkName",
            "alias", "Alias", "account", "wechat_account",
        ):
            value = str(data.get(key) or "").strip()
            if value:
                aliases.add(value.casefold())
    return aliases


def _mentions_smart_reply_agent(
    message: dict[str, Any],
    chat_id: str,
    self_wxid: str,
    *,
    owner_wxid: str,
    agent_id: str,
) -> bool:
    """Require the assistant's @空气 trigger; group metadata verifies the target."""
    if str(message.get("msgtype") or "") != "1":
        return False
    targets = {target.casefold() for target in _mention_targets(message)}
    content = str(message.get("msg") or "")
    explicitly_addressed = bool(_SMART_REPLY_AGENT_MENTION_PATTERN.search(content))
    if SQL_START.search(content):
        # @空气 inside a SQL literal/comment is data, not an owner command.
        explicitly_addressed = strip_chat_mentions(content, _SMART_REPLY_AGENT_MENTION_NAMES) != content
    if not chat_id.endswith("@chatroom"):
        return explicitly_addressed
    if not explicitly_addressed and not targets & _SMART_REPLY_AGENT_MENTION_NAMES:
        return False
    if not targets:
        return True  # Some Hook callbacks only include the visible @ text.
    identities = {str(value).strip() for value in (self_wxid, owner_wxid) if value}
    aliases = {value.casefold() for value in identities} | _SMART_REPLY_AGENT_MENTION_NAMES
    agent = agent_manager.get_agent(agent_id) if agent_id else None
    agent_aliases = _identity_aliases(agent) | _SMART_REPLY_AGENT_MENTION_NAMES
    aliases.update(agent_aliases)
    members = _cache().get_group_members(chat_id, owner_wxid=owner_wxid)
    contacts = _cache().get_contacts(list(identities), owner_wxid=owner_wxid)
    for record in contacts.values():
        aliases.update(_identity_aliases(record))
    for wxid, member in members.items():
        member_aliases = _identity_aliases(member)
        if str(wxid).casefold() in aliases or member_aliases & _SMART_REPLY_AGENT_MENTION_NAMES:
            aliases.add(str(wxid).casefold())
            aliases.update(member_aliases)

    # Real mention metadata takes precedence over literal @names in text.
    return bool((targets - {"notify@all", "@all"}) & aliases)


def _owner_mention_names(chat_id, self_wxid, *, owner_wxid, agent_id):
    """Only aliases belonging to the logged-in account can address its owner."""
    identities = {str(value).strip().casefold() for value in (self_wxid, owner_wxid) if value}
    aliases = set(identities)
    members = _cache().get_group_members(chat_id, owner_wxid=owner_wxid)
    for wxid, member in members.items():
        if str(wxid).casefold() in identities:
            aliases.update(_identity_aliases(member))
    contacts = _cache().get_contacts(list(identities), owner_wxid=owner_wxid)
    for wxid, contact in contacts.items():
        if str(wxid).casefold() in identities:
            aliases.update(_identity_aliases(contact))
    self_info = _state().get("self_info") if isinstance(_state(), dict) else None
    agent = agent_manager.get_agent(agent_id) if agent_id else None
    for record in (self_info, agent):
        record_aliases = _identity_aliases(record)
        if record_aliases & identities:
            aliases.update(record_aliases)
    return aliases


def _mentions_owner_account(message, chat_id, self_wxid, *, owner_wxid, agent_id):
    """Others must @ the actual logged-in account; metadata outranks text."""
    identities = {str(value).strip().casefold() for value in (self_wxid, owner_wxid) if value}
    targets = {target.casefold() for target in _mention_targets(message)}
    if targets:
        return bool((targets - {"notify@all", "@all"}) & identities)
    if str(message.get("msgtype") or "") != "1":
        return False
    aliases = _owner_mention_names(chat_id, self_wxid, owner_wxid=owner_wxid, agent_id=agent_id)
    content = str(message.get("msg") or "")
    visible_names = {match.group(1) for name in aliases if name for match in re.finditer(
        r"@(" + re.escape(name) + r")(?=$|[\s\u2005,，:：])", content, re.I)}
    if SQL_START.search(content):
        return strip_chat_mentions(content, visible_names) != content
    return bool(visible_names)


def _mentions_other_group_member(
    message,
    chat_id: str,
    self_wxid: str,
    *,
    owner_wxid: str = "",
    agent_id: str = "",
) -> bool:
    """Return whether a real group @ targets somebody outside the Agent identity.

    ``atuserlist``/``msgsource`` is the authoritative target list. We never
    infer a mention from every literal ``@`` in message text: SQL database
    links and ordinary text can contain ``@`` too. Cached group-member and
    Agent profile names are used to recognize targets such as ``@空气`` or
    ``@Aixed`` even when the callback gives a wxid instead of the visible name.
    """
    if str(message.get("msgtype") or "") != "1" or not str(chat_id or "").endswith("@chatroom"):
        return False
    targets = {
        str(target or "").strip().casefold()
        for target in _mention_targets(message)
        if str(target or "").strip()
    }
    if not targets:
        return False

    ignored_targets = {"notify@all", "@all"}
    targets.difference_update(ignored_targets)
    if not targets:
        return False

    self_id_values = {
        str(value or "").strip()
        for value in (self_wxid, owner_wxid)
        if str(value or "").strip()
    }
    self_ids = {value.casefold() for value in self_id_values}
    allowed_aliases = set(self_ids)
    members = _cache().get_group_members(chat_id, owner_wxid=owner_wxid or self_wxid)
    members_by_id = {
        str(wxid or "").strip().casefold(): record
        for wxid, record in members.items()
        if str(wxid or "").strip()
    }
    sender_value = str(message.get("fromid") or "").strip()
    participant_values = set(self_id_values)
    if sender_value:
        participant_values.add(sender_value)
        allowed_aliases.add(sender_value.casefold())
    contacts = _cache().get_contacts(list(participant_values), owner_wxid=owner_wxid or self_wxid)
    self_info = _state().get("self_info") if isinstance(_state(), dict) else None
    allowed_aliases.update(_identity_aliases(self_info))
    for self_id in self_ids:
        allowed_aliases.update(_identity_aliases(members_by_id.get(self_id) or {}))
    if sender_value:
        allowed_aliases.update(_identity_aliases(members_by_id.get(sender_value.casefold()) or {}))
    for participant in participant_values:
        allowed_aliases.update(_identity_aliases(contacts.get(participant) or {}))

    # The Agent may have a different visible nickname from the logged-in
    # account. Include both its registered identity and the corresponding
    # cached group member aliases, so a target such as @空气 is allowed.
    agent = agent_manager.get_agent(str(agent_id or "")) if agent_id else None
    agent_aliases = _identity_aliases(agent) | _SMART_REPLY_AGENT_MENTION_NAMES
    allowed_aliases.update(agent_aliases)
    for wxid, member in members.items():
        if _identity_aliases(member) & agent_aliases:
            allowed_aliases.add(str(wxid).strip().casefold())

    return any(target not in allowed_aliases for target in targets)


async def _resolve_sql_chat_mention_names(message, chat_id, owner_wxid):
    """Resolve visible @names from verified group members before SQL analysis."""
    names = _sql_chat_mention_names(message, chat_id, owner_wxid)
    content = str(message.get("msg") or "")
    if str(message.get("msgtype")) != "1" or not chat_id.endswith("@chatroom") or "@" not in content:
        return names
    if strip_chat_mentions(content, names) != content:
        return names

    refresh_key = (str(owner_wxid or ""), str(chat_id or ""))
    now = time.monotonic()
    if now - _sql_mention_refresh_at.get(refresh_key, 0.0) < _SQL_MENTION_REFRESH_TTL_SECONDS:
        return names
    _sql_mention_refresh_at[refresh_key] = now
    try:
        detail = await wechat_api.get_chatroom_member_detail(chat_id)
        raw_members = _extract_group_member_list(detail)
        if raw_members:
            await _cache_group_member_details(chat_id, raw_members, owner_wxid=owner_wxid)
            names = _sql_chat_mention_names(message, chat_id, owner_wxid)
    except Exception as exc:
        _smart_reply_log(
            f"[SMART_REPLY] group member resolution failed chat={chat_id} "
            f"error={type(exc).__name__}: {exc}"
        )
    return names


QUERY_PROGRESS_INTERVAL_SECONDS = 60.0


async def _send_query_progress(context, message_id, text):
    chat = context["chat_id"]
    smart_reply_engine.remember_reply(context["owner_wxid"], chat, text)
    try:
        with wechat_api.use_agent(str(context.get("agent_id") or "")), wechat_api.use_log_category("smart_reply"):
            if chat.endswith("@chatroom"):
                sender = str(context.get("sender") or "")
                member = _cache().get_group_members(chat, owner_wxid=context["owner_wxid"]).get(sender) or {}
                nickname = str(member.get("nickname") or (context.get("source_message") or {}).get("fromname") or sender)
                await wechat_api.send_at(chat, sender, nickname, text)
            else:
                await wechat_api.send_text(chat, text)
    except Exception as exc:
        _smart_reply_log(f"[SMART_REPLY] progress send failed message_id={message_id}: {type(exc).__name__}: {exc}")


async def _evaluate_ai_tasks(content, tasks, *, message_id="-", image_data_url="", source_context=None, attachments_out=None):
    context = source_context or {}
    if context.get("message_type") == "text":
        content = strip_chat_mentions(content, context.get("mention_names") or [])
        if context.get("agent_mentioned") and not SQL_START.search(content):
            for name in context.get("control_mention_names") or ["空气"]:
                content = re.sub(r"@" + re.escape(name) + r"(?=$|[\s\u2005,，:：])", "", content, flags=re.I).strip()
    enabled = (all(context.get(key) for key in ("chat_id", "owner_wxid", "sender"))
        and not SQL_START.search(content) and (query_candidate(content) or query_export_followup(content))
        and any(isinstance(task, dict) and task.get("enabled") and task.get("mcp_enabled") and
                (is_unified_task(task) or task.get("mcp_tool_name") == QUERY_TOOL) for task in tasks))
    state = {"phase": "analysis"}
    started = time.monotonic()
    async def heartbeat():
        while True:
            await asyncio.sleep(QUERY_PROGRESS_INTERVAL_SECONDS)
            await _send_query_progress(context, message_id, progress_text(state, time.monotonic() - started))
    timer = asyncio.create_task(heartbeat()) if enabled else None
    try:
        return await _evaluate_ai_tasks_impl(content, tasks, message_id=message_id, image_data_url=image_data_url,
            source_context={**context, "_query_progress": state} if enabled else source_context, attachments_out=attachments_out)
    finally:
        if timer:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)


async def _evaluate_ai_tasks_impl(
    content: str,
    tasks: list[dict[str, Any]],
    *,
    message_id: str = "-",
    image_data_url: str = "",
    source_context: dict[str, Any] | None = None,
    attachments_out: list[dict[str, str]] | None = None,
) -> tuple[str, ...]:
    enabled_tasks = [task for task in tasks if isinstance(task, dict) and bool(task.get("enabled"))]
    if source_context and all(source_context.get(k) for k in ("owner_wxid", "chat_id", "sender")) and any(
        task.get("mcp_enabled") and (is_unified_task(task) or task.get("mcp_tool_name") == WORKFLOW_TOOL) for task in enabled_tasks
    ):
        pending_context, pending = _offline_confirmation_for(source_context, content)
        if pending and source_context.get("agent_mentioned") and not SQL_START.search(content) and not re.search(r"查询|查一下|结果|详情|履历", content):
            return offline_reply_chunks(await _resume_offline_city_confirmation(content, pending_context, pending, enabled_tasks))
    combined_identifier = submission_workflow_identifier(content)
    if combined_identifier and any(is_unified_task(task) for task in enabled_tasks):
        content, reason = await _recent_sql_workflow_content(content, source_context or {})
        if not content:
            return (UNIFIED_SKILL_NAME + f"：未找到同一发送人 24 小时内编号为 {combined_identifier} 的可读取 TXT 或聊天 SQL（{reason}），未填报、审核或执行。请先发送包含完整 SQL 的 TXT 或聊天消息，再发送本指令。",)
    prepared = []
    for task in enabled_tasks:
        if is_unified_task(task):
            history = _query_history_for(source_context, str(task.get("mcp_connection_id") or ""))
            if not unified_candidate(content, history, message_type=str(task.get("message_type") or "text")):
                continue
            prepared.append({**task, "_query_history": history})
            continue
        if task.get("mcp_tool_name") == SUBMISSION_TOOL:
            if not submission_candidate(content):
                continue
        if task.get("mcp_tool_name") == APPROVAL_TOOL:
            if not approval_candidate(content):
                continue
            task = {**task, "_query_history": _query_history_for(source_context, str(task.get("mcp_connection_id") or ""))}
        if task.get("mcp_tool_name") == EXECUTION_TOOL:
            if not execution_candidate(content):
                continue
            task = {**task, "_query_history": _query_history_for(source_context, str(task.get("mcp_connection_id") or ""))}
        if task.get("mcp_tool_name") == QUERY_TOOL:
            history = _query_history_for(source_context, str(task.get("mcp_connection_id") or ""))
            if not query_candidate(content, history):
                continue
            task = {**task, "_query_history": history}
        prepared.append(task)
    # A query follow-up stays in the read-only Agent, even when a SQL workflow
    # Skill is configured in the same group.
    has_unified = any(is_unified_task(t) for t in prepared)
    has_query = any(t.get("mcp_tool_name") == QUERY_TOOL for t in prepared)
    has_approval = any(t.get("mcp_tool_name") == APPROVAL_TOOL for t in prepared)
    has_execution = any(t.get("mcp_tool_name") == EXECUTION_TOOL for t in prepared)
    has_submission = any(t.get("mcp_tool_name") == SUBMISSION_TOOL for t in prepared)
    enabled_tasks = ([next(t for t in prepared if is_unified_task(t))] if has_unified else
        [next(t for t in prepared if t.get("mcp_tool_name") == SUBMISSION_TOOL)] if has_submission else
        [next(t for t in prepared if t.get("mcp_tool_name") == APPROVAL_TOOL)] if has_approval else
        [next(t for t in prepared if t.get("mcp_tool_name") == EXECUTION_TOOL)] if has_execution else
        [t for t in prepared if not has_query or t.get("mcp_tool_name") != WORKFLOW_TOOL])
    if not enabled_tasks:
        _smart_reply_log(f"[AI_REPLY] skipped message_id={message_id}: no enabled AI tasks")
        return ()
    if not ai_service.configured:
        _smart_reply_log(f"[AI_REPLY] skipped message_id={message_id}: AI service is not configured")
        if has_submission:
            return (SUBMISSION_REPLY_PREFIX + "：分析服务未配置，未提交。",)
        if has_approval:
            return (APPROVAL_REPLY_PREFIX + "：分析服务未配置，未审核。",)
        if has_execution:
            return (EXECUTION_REPLY_PREFIX + "：分析服务未配置，未执行。",)
        if has_unified:
            return (UNIFIED_SKILL_NAME + "：分析服务未配置，未调用数据运维工具。",)
        if any(task.get("mcp_tool_name") == QUERY_TOOL for task in enabled_tasks):
            return (QUERY_REPLY_PREFIX + "：分析服务未配置，未取得查询结果。",)
        if any(task.get("mcp_tool_name") == WORKFLOW_TOOL for task in enabled_tasks):
            return ("SQL 分析服务未配置，未启动数据运维流程。",)
        return ()
    # Acknowledge eligible MCP work before model analysis or remote calls.
    # Callback deduplication has already claimed this message at this point.
    context = source_context or {}
    if any(task.get("mcp_enabled") and (task.get("mcp_tool_name") != WORKFLOW_TOOL or SQL_START.search(content)) for task in enabled_tasks) and context.get("chat_id") and context.get("owner_wxid"):
        await _send_query_progress(context, message_id, "思考中...")

    async def _analyze_task(task: dict[str, Any]) -> dict[str, Any]:
        task = offline_analysis_task(task, content)
        history = task.get("_query_history") or []
        if (is_unified_task(task) or task.get("mcp_tool_name") == QUERY_TOOL) and query_export_followup(content) and history:
            previous = history[-1].get("arguments") or {}
            if previous.get("query_type") == "problem_tickets":
                parsed = {"query_type": "problem_tickets", "identifier": ""}
                if is_unified_task(task):
                    parsed["intent"] = "query"
                return {"matched": True, "confidence": 100, "result": json.dumps(parsed), "reply": ""}
        if is_unified_task(task):
            explicit = explicit_problem_range_analysis(content) or explicit_pending_analysis(content)
            if explicit:
                parsed = json.loads(explicit["result"])
                parsed.update(intent="query", sql_purpose="", risk_level="", risk_notes=[], needs_clarification=False)
                return {**explicit, "result": json.dumps(parsed, ensure_ascii=False)}
            history = task.get("_query_history") or []
            analysis_content = content if SQL_START.search(content) else query_analysis_content(content, history)
            return await ai_service.analyze(analysis_content, task)
        if task.get("mcp_tool_name") in {APPROVAL_TOOL, EXECUTION_TOOL}:
            return await ai_service.analyze(query_analysis_content(content, task.get("_query_history") or []), task)
        if task.get("mcp_tool_name") == QUERY_TOOL:
            history = task.get("_query_history") or []
            explicit = explicit_problem_range_analysis(content) or explicit_pending_analysis(content)
            if explicit:
                return explicit
            if not history and query_followup(content) and not re.search(r"执行|审核|审批|影响|需求|任务|处理|受理|履历|SQL\s*(?:原文|脚本)|完整\s*SQL", content, re.I):
                return {"matched": True, "confidence": 100, "result": "{}", "reply": ""}
            return await ai_service.analyze(query_analysis_content(content, history), task)
        if image_data_url:
            try:
                return await ai_service.analyze_image(content, task, image_data_url)
            except (AiServiceError, AttributeError) as exc:
                _smart_reply_log(
                    f"[AI_REPLY] image input unavailable message_id={message_id} "
                    f"id={task.get('id', '')}: {type(exc).__name__}: {exc}; falling back to OCR text"
                )
        return await ai_service.analyze(content, task)

    results = await asyncio.gather(
        *(_analyze_task(task) for task in enabled_tasks),
        return_exceptions=True,
    )
    replies: list[str] = []
    for task, result in zip(enabled_tasks, results):
        if isinstance(result, BaseException):
            _smart_reply_log(
                f"[AI_REPLY] task failed message_id={message_id} id={task.get('id', '')}: "
                f"{type(result).__name__}: {result}"
            )
            if task.get("mcp_tool_name") == SUBMISSION_TOOL:
                replies.append(SUBMISSION_REPLY_PREFIX + "：填报意图分析失败，未提交。")
            elif is_unified_task(task):
                replies.append(UNIFIED_SKILL_NAME + "：意图分析失败，未调用数据运维工具。")
            elif task.get("mcp_tool_name") == APPROVAL_TOOL:
                replies.append(APPROVAL_REPLY_PREFIX + "：审核意图分析失败，未审核。")
            elif task.get("mcp_tool_name") == EXECUTION_TOOL:
                replies.append(EXECUTION_REPLY_PREFIX + "：执行意图分析失败，未执行。")
            elif task.get("mcp_tool_name") == QUERY_TOOL:
                replies.append(QUERY_REPLY_PREFIX + "：查询意图分析失败，未取得查询结果。")
            elif task.get("mcp_tool_name") == WORKFLOW_TOOL:
                replies.append("SQL 风险分析失败，未填报、审核或执行。请检查分析服务后重试。")
            continue
        if bool(task.get("mcp_enabled")):
            try:
                mcp_task = {**task, "_source_content": content, "_source_context": source_context or {}}
                if attachments_out is not None:
                    mcp_task["_attachments_out"] = attachments_out
                task_replies = await _mcp_task_reply(mcp_task, result)
            except McpServiceError as exc:
                _smart_reply_log(
                    f"[MCP_REPLY] task failed message_id={message_id} id={task.get('id', '')} "
                    f"tool={task.get('mcp_tool_name') or 'auto'}: {exc}"
                )
                task_replies = ((APPROVAL_REPLY_PREFIX + "：审核请求结果待核对，请查询单据状态，不要重复审核。",) if task.get("mcp_tool_name") == APPROVAL_TOOL else
                    (EXECUTION_REPLY_PREFIX + "：执行请求结果待核对，请查询单据执行情况，不要重复执行。",) if task.get("mcp_tool_name") == EXECUTION_TOOL else _ai_result_replies(task, result))
            _smart_reply_log(
                f"[MCP_REPLY] task completed message_id={message_id} id={task.get('id', '')} "
                f"tool={task.get('mcp_tool_name') or 'auto'} replies={len(task_replies)}"
            )
        else:
            task_replies = _ai_result_replies(task, result)
        _smart_reply_log(
            f"[AI_REPLY] task result message_id={message_id} id={task.get('id', '')} "
            f"matched={result.get('matched')} "
            f"confidence={result.get('confidence')} replies={len(task_replies)}"
        )
        replies.extend(task_replies)
    return tuple(replies)


_smart_reply_conversation_locks: dict[tuple[str, str, str], list[Any]] = {}


async def _process_smart_reply_message(*, owner_wxid: str, agent_id: str, self_wxid: str, chat_id: str, message: dict[str, Any], received_at: float | None = None) -> None:
    """Preserve each sender's arrival order while allowing others to run."""
    key = (owner_wxid, chat_id, str(message.get("fromid") or ""))
    entry = _smart_reply_conversation_locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    received_at = time.perf_counter() if received_at is None else received_at
    try:
        async with entry[0]:
            await _process_smart_reply_message_unlocked(owner_wxid=owner_wxid, agent_id=agent_id, self_wxid=self_wxid, chat_id=chat_id, message=message, received_at=received_at)
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _smart_reply_conversation_locks.pop(key, None)


async def _process_smart_reply_message_unlocked(
    *,
    owner_wxid: str,
    agent_id: str,
    self_wxid: str,
    chat_id: str,
    message: dict[str, Any],
    received_at: float | None = None,
) -> None:
    process_started_at = time.perf_counter()
    message_received_at = process_started_at if received_at is None else received_at
    queue_ms = max(0.0, (process_started_at - message_received_at) * 1000)
    config_row = _cache().get_smart_reply_config(chat_id, owner_wxid=owner_wxid)
    if not config_row:
        return
    source_message = message
    # Quote title is the sender's current SQL; quoted image text is never an instruction.
    quote = sql_quote(message)
    if quote and quote["text"].strip() and any(
        task.get("enabled") and task.get("mcp_enabled") and (is_unified_task(task) or task.get("mcp_tool_name") in {
            WORKFLOW_TOOL, QUERY_TOOL, SUBMISSION_TOOL, APPROVAL_TOOL, EXECUTION_TOOL})
        for task in config_row.get("ai_tasks") or []
    ):
        message = {**message, "msgtype": "1", "msg": quote["text"]}
    is_self = (
        str(message.get("sendorrecv") or "") == "1"
        or int(message.get("isSender") or 0) == 1
        or str(message.get("fromid") or "") in {value for value in (self_wxid, owner_wxid) if value}
    )
    if is_self and (smart_reply_engine.is_reply_echo(owner_wxid, chat_id, message.get("msg"))
            or is_offline_confirmation_prompt(message.get("msg"))):
        _smart_reply_log(f"[SMART_REPLY] skipped message_id={message.get('id') or '-'} chat={chat_id} reason=automatic_reply_echo")
        return
    agent_mentioned = (_mentions_smart_reply_agent if is_self else _mentions_owner_account)(
        message, chat_id, self_wxid, owner_wxid=owner_wxid, agent_id=agent_id)
    control_mention_names = list(_SMART_REPLY_AGENT_MENTION_NAMES if is_self else _owner_mention_names(
        chat_id, self_wxid, owner_wxid=owner_wxid, agent_id=agent_id))
    protected_data_task = any(
        task.get("enabled") and task.get("mcp_enabled") and (is_unified_task(task) or task.get("mcp_tool_name") in {
            WORKFLOW_TOOL, QUERY_TOOL, SUBMISSION_TOOL, APPROVAL_TOOL, EXECUTION_TOOL}) for task in config_row.get("ai_tasks") or []
    )
    if protected_data_task and not agent_mentioned:
        reason = "self_message_without_agent_mention" if is_self else "data_agent_without_owner_mention"
        _smart_reply_log(f"[SMART_REPLY] skipped message_id={message.get('id') or '-'} chat={chat_id} reason={reason}")
        return
    owner_city_reply = False
    if is_self and str(message.get("fromid") or "") == owner_wxid and any(
        task.get("enabled") and task.get("mcp_enabled") and (is_unified_task(task) or task.get("mcp_tool_name") == WORKFLOW_TOOL)
        for task in config_row.get("ai_tasks") or []
    ):
        city_text = _SMART_REPLY_AGENT_MENTION_PATTERN.sub("", str(message.get("msg") or "")).strip()
        _, pending = _offline_confirmation_for({"owner_wxid": owner_wxid, "chat_id": chat_id, "sender": owner_wxid}, city_text)
        owner_city_reply = bool(agent_mentioned and pending and city_input(city_text, pending["arguments"]["identifier"])[0])
    # Owner-specific Agent routes below clear the self-sender flags. Enforce
    # the mention requirement before any route can bypass the normal filter.
    if is_self and not _mentions_smart_reply_agent(
        message, chat_id, self_wxid, owner_wxid=owner_wxid, agent_id=agent_id,
    ):
        _smart_reply_log(
            f"[SMART_REPLY] skipped message_id={message.get('id') or '-'} chat={chat_id} "
            "reason=self_message_without_agent_mention"
        )
        return
    if _mentions_other_group_member(
        message,
        chat_id,
        self_wxid,
        owner_wxid=owner_wxid,
        agent_id=agent_id,
    ):
        _smart_reply_log(
            f"[SMART_REPLY] skipped message_id={message.get('id') or '-'} chat={chat_id} "
            "reason=mentioned_other_group_member"
        )
        return
    owner_message = {
        **message,
        "msg": _SMART_REPLY_AGENT_MENTION_PATTERN.sub("", str(message.get("msg") or "")).strip(),
    } if is_self else message
    query_task = next((t for t in config_row.get("ai_tasks") or [] if t.get("enabled") and (t.get("mcp_tool_name") == QUERY_TOOL or is_unified_task(t))), {})
    query_history = _query_history_for({"owner_wxid": owner_wxid, "chat_id": chat_id, "sender": str(message.get("fromid") or "")}, str(query_task.get("mcp_connection_id") or "")) if query_task else []
    query_config = ((owner_filehelper_config(config_row, owner_message, owner_wxid, query_history)
        or owner_unified_config(config_row, owner_message, owner_wxid, query_history)
        or owner_submission_config(config_row, owner_message, owner_wxid) or owner_approval_config(config_row, owner_message, owner_wxid)
        or owner_execution_config(config_row, owner_message, owner_wxid) or owner_query_config(config_row, owner_message, owner_wxid, query_history)) if owner_wxid == self_wxid else None)
    if owner_city_reply:
        query_config = {**config_row, "rules": [], "message_types": ["text"], "mention_only": False, "mention_message_types": [],
            "target_senders_by_type": {"text": [owner_wxid]}, "ai_tasks": [task for task in config_row.get("ai_tasks") or []
                if task.get("enabled") and task.get("mcp_enabled") and task.get("message_type", "text") == "text"
                and (is_unified_task(task) or task.get("mcp_tool_name") == WORKFLOW_TOOL)]}
    if query_config is not None:
        config_row = query_config
        message = {**owner_message, "isSender": 0, "sendorrecv": "2"}
        self_wxid = ""  # Only the selected Agent for this explicit request.
    message_id = str(message.get("id") or "-")
    _smart_reply_log(
        f"[SMART_REPLY] received message_id={message_id} chat={chat_id} "
        f"sender={message.get('fromid', '')} msgtype={message.get('msgtype', '')} "
        f"queue_ms={queue_ms:.1f} text={_preview_text(message.get('msg'))!r}"
    )
    decision = smart_reply_engine.evaluate(
        owner_wxid=owner_wxid,
        chat_id=chat_id,
        self_wxid=self_wxid,
        message=message,
        config=config_row,
        # Reuse the verified owner @ check when Hook omits mention metadata.
        mention_verified=protected_data_task and agent_mentioned,
    )
    reply_attachments: list[dict[str, str]] = []
    if not decision.should_send:
        msg_type = str(message.get("msgtype") or "")
        ai_message_type = {"1": "text", "3": "image", "49": "file"}.get(msg_type, "")
        ai_tasks = [
            task
            for task in (config_row.get("ai_tasks") or [])
            if isinstance(task, dict)
            and str(task.get("message_type") or "text").strip() == ai_message_type
        ] if isinstance(config_row, dict) else []
        ai_candidate_reasons = {"keyword_not_matched", "too_few_lines", "pure_single_line"}
        if decision.reason not in ai_candidate_reasons:
            _smart_reply_log(
                f"[SMART_REPLY] skipped message_id={message_id} chat={chat_id} "
                f"sender={message.get('fromid', '')} "
                f"reason={decision.reason} msgtype={message.get('msgtype', '')} ai_tasks={len(ai_tasks or [])}"
            )
            return
        if not smart_reply_engine.claim_ai_message(owner_wxid, chat_id, message):
            _smart_reply_log(f"[SMART_REPLY] skipped message_id={message_id} chat={chat_id} reason=duplicate_callback")
            return
        raw_content = ""
        image_data_url = ""
        if msg_type == "3":
            ocr_text, image_xml, ocr_reason = await _smart_reply_image_ocr(message, agent_id)
            if ocr_text:
                extracted_message = dict(message)
                extracted_message["_smart_reply_content"] = ocr_text
                decision = smart_reply_engine.evaluate(
                    owner_wxid=owner_wxid,
                    chat_id=chat_id,
                    self_wxid=self_wxid,
                    message=extracted_message,
                    config=config_row,
                )
                if decision.should_send:
                    _smart_reply_log(
                        f"[SMART_REPLY] image OCR rule matched message_id={message_id} "
                        f"chat={chat_id} text={_preview_text(ocr_text)!r}"
                    )
                elif decision.reason not in ai_candidate_reasons:
                    _smart_reply_log(
                        f"[SMART_REPLY] skipped image message_id={message_id} chat={chat_id} "
                        f"reason={decision.reason} after_ocr=yes"
                    )
                    return
            if not decision.should_send:
                if not ai_tasks:
                    _smart_reply_log(
                        f"[SMART_REPLY] skipped image message_id={message_id} chat={chat_id} "
                        f"reason={ocr_reason if not ocr_text else decision.reason} ai_tasks=0"
                    )
                    return
                image_data_url, image_reason = await _smart_reply_image_data_url(
                    message,
                    image_xml,
                    agent_id,
                )
                if not ocr_text and not image_data_url:
                    _smart_reply_log(
                        f"[SMART_REPLY] skipped image message_id={message_id} chat={chat_id} "
                        f"ocr_reason={ocr_reason} image_reason={image_reason}"
                    )
                    return
                raw_content = (
                    f"图片 OCR 文字：\n{ocr_text}"
                    if ocr_text
                    else "图片消息（OCR 未识别到文字，请直接识别图片内容）"
                )
                _smart_reply_log(
                    f"[SMART_REPLY] image prepared message_id={message_id} chat={chat_id} "
                    f"ocr_reason={ocr_reason} image_reason={image_reason} "
                    f"vision={'yes' if image_data_url else 'no'} text={_preview_text(ocr_text)!r}"
                )
        elif msg_type == "49":
            if not ai_tasks:
                _smart_reply_log(
                    f"[SMART_REPLY] skipped file message_id={message_id} chat={chat_id} reason=no_ai_tasks"
                )
                return
            raw_content, file_reason = await _smart_reply_file_ai_content(
                message,
                config_row.get("file_types") if isinstance(config_row, dict) else None,
            )
            if not raw_content:
                _smart_reply_log(
                    f"[SMART_REPLY] skipped file message_id={message_id} chat={chat_id} "
                    f"sender={message.get('fromid', '')} reason={file_reason}"
                )
                if not any(task.get("enabled") and task.get("mcp_enabled") and (task.get("mcp_tool_name") == WORKFLOW_TOOL or is_unified_task(task)) for task in ai_tasks):
                    return
                reason_labels = {"input_too_large": "TXT 超过完整分析的长度限制", "oversized_file": "TXT 文件过大", "decode_failed": "TXT 编码无法识别", "missing_file": "TXT 文件未下载成功", "invalid_file_content": "TXT 包含无效字符"}
                decision = smart_reply_engine.reserve_ai_replies(owner_wxid=owner_wxid, chat_id=chat_id, message=message,
                    replies=(f"{reason_labels.get(file_reason, 'TXT 无法完整读取')}（{file_reason}），未填报、审核或执行。",))
                if not decision.should_send:
                    return
        elif msg_type == "1":
            if not ai_tasks:
                _smart_reply_log(
                    f"[SMART_REPLY] skipped message_id={message_id} chat={chat_id} reason=no_ai_tasks"
                )
                return
            raw_content = str(message.get("msg") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        else:
            _smart_reply_log(
                f"[SMART_REPLY] skipped message_id={message_id} chat={chat_id} "
                f"reason=unsupported_async_message_type msgtype={msg_type}"
            )
            return
        mention_names = _sql_chat_mention_names(message, chat_id, owner_wxid)
        if ai_message_type == "text":
            mention_names = await _resolve_sql_chat_mention_names(message, chat_id, owner_wxid)
            if agent_mentioned:
                mention_names = list(set(mention_names) | set(control_mention_names))
                for name in control_mention_names:
                    mention_names.extend(match.group(1) for match in re.finditer(
                        r"@(" + re.escape(name) + r")(?=$|[\s\u2005,，:：])", raw_content, re.I))
        if not decision.should_send:
            _smart_reply_log(
                f"[SMART_REPLY] evaluating AI message_id={message_id} chat={chat_id} "
                f"sender={message.get('fromid', '')} "
                f"reason={decision.reason} text={_preview_text(raw_content)!r}"
            )
            ai_replies = await _evaluate_ai_tasks(
                raw_content,
                ai_tasks,
                message_id=message_id,
                image_data_url=image_data_url,
                source_context={"owner_wxid": owner_wxid, "chat_id": chat_id, "sender": str(message.get("fromid") or ""), "message_id": str(message.get("id") or ""),
                    "message_type": ai_message_type, "message_timestamp": message.get("timestamp") or message.get("time_unix") or int(time.time()),
                    "mention_names": mention_names, "control_mention_names": control_mention_names,
                    "source_message": source_message, "agent_id": agent_id, "agent_mentioned": agent_mentioned},
                attachments_out=reply_attachments,
            )
            decision = smart_reply_engine.reserve_ai_replies(
                owner_wxid=owner_wxid,
                chat_id=chat_id,
                message=message,
                replies=ai_replies,
                conversation=bool(ai_replies) and all(reply.startswith((QUERY_REPLY_PREFIX, EXECUTION_REPLY_PREFIX, SUBMISSION_REPLY_PREFIX, APPROVAL_REPLY_PREFIX,
                    "线下单 ", "线下单行政区划：", CITY_SELECTION_PREFIX)) for reply in ai_replies),
            )
            if not decision.should_send:
                _smart_reply_log(
                    f"[SMART_REPLY] AI produced no send message_id={message_id} chat={chat_id} "
                    f"sender={message.get('fromid', '')} "
                    f"reason={decision.reason} replies={len(ai_replies)}"
                )
                for attachment in reply_attachments:
                    try:
                        os.remove(str(attachment.get("filepath") or ""))
                    except OSError:
                        pass
                return
            _smart_reply_log(
                f"[SMART_REPLY] AI reserved message_id={message_id} chat={chat_id} "
                f"sender={message.get('fromid', '')} "
                f"count={len(decision.replies)}"
            )
    evaluation_finished_at = time.perf_counter()
    evaluate_ms = (evaluation_finished_at - process_started_at) * 1000
    try:
        use_no_src = bool(config_row.get("use_no_src"))
        workflow_reply_to_sender = any(
            isinstance(task, dict) and task.get("enabled") and task.get("mcp_enabled")
            and (task.get("mcp_tool_name") in {WORKFLOW_TOOL, QUERY_TOOL, APPROVAL_TOOL, EXECUTION_TOOL, SUBMISSION_TOOL} or is_unified_task(task))
            and task.get("message_type", "text") == {"1": "text", "49": "file"}.get(str(message.get("msgtype")))
            for task in config_row.get("ai_tasks") or []
        )

        async def _send_one(reply: str):
            smart_reply_engine.remember_reply(owner_wxid, chat_id, reply)
            with wechat_api.use_agent(agent_id), wechat_api.use_log_category("smart_reply"):
                if workflow_reply_to_sender and chat_id.endswith("@chatroom"):
                    sender = str(message.get("fromid") or "")
                    member = _cache().get_group_members(chat_id, owner_wxid=owner_wxid).get(sender) or {}
                    nickname = str(member.get("name") or member.get("nickname") or message.get("fromname") or sender)
                    return await wechat_api.send_at(chat_id, sender, nickname, reply)
                if use_no_src:
                    return await wechat_api.send_text_no_src(chat_id, reply)
                return await wechat_api.send_text(chat_id, reply)

        send_started_at = time.perf_counter()
        attachment_results = []
        for attachment in reply_attachments:
            filepath = str(attachment.get("filepath") or "")
            if not filepath:
                continue
            try:
                with wechat_api.use_agent(agent_id), wechat_api.use_log_category("smart_reply"):
                    attachment_results.append(await wechat_api.send_file(chat_id, filepath, filename=attachment.get("filename") or ""))
            except Exception as exc:
                attachment_results.append(exc)
            # Hook accepts sends asynchronously. Keep the report on disk so
            # the WeChat client can finish opening and transferring the file.
        attachment_send_failed = False
        for attachment, result in zip(reply_attachments, attachment_results):
            if isinstance(result, BaseException) or not _send_result_ok(result):
                attachment_send_failed = True
                _smart_reply_log(
                    f"[SMART_REPLY] table file send failed message_id={message_id} chat={chat_id} "
                    f"file={attachment.get('filename') or attachment.get('filepath') or ''} result={result}"
                )
            else:
                _smart_reply_log(
                    f"[SMART_REPLY] table file sent message_id={message_id} chat={chat_id} "
                    f"file={attachment.get('filename') or attachment.get('filepath') or ''}"
                )
        replies_to_send = decision.replies
        if attachment_send_failed:
            replies_to_send = tuple(
                reply.replace("完整明细已整理为表格文件发送", "完整明细表格文件发送失败，请稍后重试")
                for reply in replies_to_send
            )
        if workflow_reply_to_sender:
            results = []
            for reply in replies_to_send:
                try: results.append(await _send_one(reply))
                except Exception as exc: results.append(exc)
        else:
            results = await asyncio.gather(
                *(_send_one(reply) for reply in replies_to_send),
                return_exceptions=True,
            )
        send_finished_at = time.perf_counter()
        send_ms = (send_finished_at - send_started_at) * 1000
        total_ms = max(0.0, (send_finished_at - message_received_at) * 1000)
        sent_replies: list[str] = []
        for reply, result in zip(replies_to_send, results):
            if isinstance(result, BaseException) or not _send_result_ok(result):
                _smart_reply_log(
                    f"[SMART_REPLY] send failed message_id={message_id} chat={chat_id} "
                    f"reason={decision.reason} "
                    f"reply={reply!r} result={result}"
                )
                continue
            sent_replies.append(reply)
        if not sent_replies:
            _smart_reply_log(
                f"[SMART_REPLY] send completed message_id={message_id} chat={chat_id} "
                f"sender={message.get('fromid', '')} "
                f"reason={decision.reason} count=0 mode={'no_src' if use_no_src else 'normal'} "
                f"queue_ms={queue_ms:.1f} evaluate_ms={evaluate_ms:.1f} "
                f"send_ms={send_ms:.1f} total_ms={total_ms:.1f}"
            )
            return
        _smart_reply_log(
            f"[SMART_REPLY] sent message_id={message_id} chat={chat_id} "
            f"sender={message.get('fromid', '')} "
            f"reason={decision.reason} count={len(sent_replies)} mode={'no_src' if use_no_src else 'normal'} "
            f"disabled={decision.disable_after_send} "
            f"queue_ms={queue_ms:.1f} evaluate_ms={evaluate_ms:.1f} "
            f"send_ms={send_ms:.1f} total_ms={total_ms:.1f}"
        )
        updated = _cache().record_smart_reply_trigger(
            chat_id,
            owner_wxid=owner_wxid,
            disable=decision.disable_after_send,
            increment=len(sent_replies),
        )
        for reply in sent_replies:
            await _broadcast_local_sent_for_agent(agent_id, chat_id, "1", reply)
        await manager.broadcast({"type": "smart_reply_updated", "data": {"config": updated}})
    except Exception as exc:
        _smart_reply_log(
            f"[SMART_REPLY] error message_id={message_id} chat={chat_id}: "
            f"{type(exc).__name__}: {exc}"
        )


def _schedule_smart_reply_message(
    *,
    owner_wxid: str,
    agent_id: str,
    self_wxid: str,
    chat_id: str,
    message: dict[str, Any],
    received_at: float | None = None,
) -> None:
    task = asyncio.create_task(_process_smart_reply_message(
        owner_wxid=owner_wxid,
        agent_id=agent_id,
        self_wxid=self_wxid,
        chat_id=chat_id,
        message=dict(message),
        received_at=received_at,
    ))
    _track_background_send(task, "smart_reply")


# ─── WeChat Hook Callback (receives messages from Hook) ────────────

@app.post("/api/callback")
async def wechat_callback(request: Request):
    """Receive messages from WeChat Hook and broadcast to frontend via WebSocket."""
    callback_received_at = time.perf_counter()
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
    return await _process_wechat_callback(data, received_at=callback_received_at)


async def _process_wechat_callback(
    data: dict[str, Any],
    *,
    received_at: float | None = None,
) -> dict[str, str]:
    """Process a callback received through HTTP or the agent WebSocket."""
    callback_received_at = time.perf_counter() if received_at is None else received_at
    _log_callback_sample(data)

    # Log top-level callback keys for debugging CDN download callbacks
    top_keys = list(data.keys()) if isinstance(data, dict) else []
    _log(f"[CALLBACK] top_keys={top_keys}")

    sendorrecv = str(data.get("sendorrecv", "") or "")
    self_wxid = _extract_self_wxid(data) or _get_self_wxid()
    protocol_session_id = str(
        data.get("session_id", "")
        or data.get("sessionId", "")
        or data.get("SessionID", "")
        or ""
    ).strip()
    mapped_agent_id = (
        _agent_id_for_self_wxid(self_wxid)
        or agent_manager.agent_id_for_wxid(self_wxid)
    )
    hook_instance = hook_login.instance_for_port(data.get("ServerPort") or data.get("server_port")) if config.IS_HOOK else None
    callback_agent_id = str(
        (protocol_session_id if config.IS_PROTOCOL else "")
        or (hook_instance["id"] if hook_instance and hook_instance['transport'] == 'http' else "")
        or mapped_agent_id
        or data.get("agent_id", "")
        or data.get("agentId", "")
        or ""
    )
    if hook_instance and callback_agent_id and access_store.owner(hook_instance['id']):
        access_store.claim(callback_agent_id, access_store.owner(hook_instance['id']))
    if config.IS_HOOK and not callback_agent_id and str(data.get('ServerPort') or data.get('server_port')) == str(config.HOOK_PORT):
        callback_agent_id = 'default'
    if not callback_agent_id and self_wxid:
        callback_agent_id = f"selfwxid_{self_wxid}"
        _log(f"[CALLBACK] selfwxid={self_wxid} has no agent mapping yet; using isolated runtime {callback_agent_id}")
    if not callback_agent_id and not self_wxid:
        callback_agent_id = _current_agent_id() or agent_manager.active_id()
    if callback_agent_id:
        _activate_runtime(callback_agent_id)
        if access_store.retired(callback_agent_id) or access_store.retired('wxid:' + self_wxid):
            return {'status': 'ok'}
        task = asyncio.current_task()
        if task:
            tasks = _runtime_for(callback_agent_id).setdefault('tasks', set())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    if self_wxid:
        _self_wxid_to_agent_id[self_wxid] = callback_agent_id or _current_agent_id() or agent_manager.active_id()
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
    callback_owner_wxid = _contact_owner_wxid(self_wxid)

    # Pending CDN state is irrelevant to ordinary text callbacks. Avoid even
    # briefly queuing those callbacks behind an in-progress media operation.
    has_embedded_media = any(
        isinstance(msg, dict)
        and any(msg.get(key) for key in ("img_base64", "video_base64", "file_base64", "gif_base64"))
        for msg in msglist
    )
    has_pending_cdn = False
    pending_keys_dbg: list[str] = []
    if has_embedded_media:
        async with _pending_cdn_lock:
            has_pending_cdn = len(_bucket('_pending_cdn_images', _pending_cdn_images)) > 0
            pending_keys_dbg = list(_bucket('_pending_cdn_images', _pending_cdn_images).keys())
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
                # this might be a CDN /download callback with a different msgsvrid.
                # Since we serialize CDN downloads (1 at a time), fulfilling the
                # single pending download is safe.
                if not matched and has_pending_cdn:
                    _log(f"[IMG_BASE64] No msgsvrid match, trying to fulfill pending CDN downloads...")
                    await _fulfill_all_cdn_pending(saved)
                    # Also resolve inflight dedup futures
                    async with _inflight_cdn_lock:
                        for k, ifut in list(_bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).items()):
                            if not ifut.done():
                                ifut.set_result(saved)
                        _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).clear()
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
        if _is_protocol_internal_callback_message(msg):
            _log(f"[CALLBACK] Skip protocol internal event type={msgtype} from={source} content={content!r}")
            continue
        if _is_callback_status_echo(msg, sendorrecv, self_wxid):
            _log(f"[CALLBACK] Skip status echo type={msgtype} content={content!r}")
            continue
        if _is_preliminary_local_hook_file_callback(msg):
            _log(
                f"[CALLBACK] Skip preliminary file notification "
                f"svrid={msg.get('msgsvrid', '')}; waiting for callback with file_path"
            )
            continue
        chat_id, normalized = _normalize_callback_message(msg, sendorrecv, self_wxid)
        if not chat_id or not normalized:
            continue
        pre_messages.append((chat_id, normalized))
        _schedule_smart_reply_message(
            owner_wxid=callback_owner_wxid,
            agent_id=callback_agent_id,
            self_wxid=self_wxid,
            chat_id=chat_id,
            message=normalized,
            received_at=callback_received_at,
        )
        # Let this individual reply reach its first I/O wait before parsing the
        # next message in the same callback batch.
        await asyncio.sleep(0)

    # Callback payloads already contain either decoded content (RecvType=1)
    # or PB data (RecvType=2). Avoid extra QueryDB work in the callback path so
    # chat-history requests can use the DB connection pool.

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


async def _receive_agent_callback(data: dict[str, Any]) -> None:
    await _process_wechat_callback(data)


agent_manager.set_callback_handler(_receive_agent_callback)


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
    if access_store.current_user.get():
        payload = await list_accounts()
        return {'enabled': config.AGENT_WS_ENABLED, 'accounts': payload.get('accounts', []),
                'connected': any(a.get('connected') for a in payload.get('accounts', []))}
    if config.IS_LOCAL_HOOK:
        account = await _probe_local_hook_account()
        return {
            "enabled": False,
            "connected": bool(account.get("port_alive")),
            "transport": "http",
            "host": config.HOOK_HOST,
            "port": config.HOOK_PORT,
            "peer": account.get("peer", ""),
            "login_status": account.get("login_status", ""),
            "login_message": account.get("login_message", ""),
            "probe_latency_ms": account.get("probe_latency_ms", 0),
            "account": account,
        }
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
    access_store.legacy_user()
    user_id = access_store.login(ws_key)
    if not user_id:
        await websocket.close(code=1008, reason="unauthorized")
        return
    agent_id = str(websocket.query_params.get("agent_id") or "").strip()
    if not access_store.owns(agent_id, user_id):
        await websocket.close(code=1008, reason='account forbidden')
        return
    access_store.current_user.set(user_id)
    _activate_runtime(agent_id)
    await manager.connect(websocket, user_id=user_id, account_id=agent_id)

    # Send initial state on connect
    await websocket.send_text(json.dumps({
        "type": "init",
        "data": {
            "account_id": _current_agent_id() or agent_manager.active_id(),
            "self_info": _state()["self_info"],
            "contacts": _state()["contacts"],
            "sessions": _state()["sessions"],
            "last_messages": _state()["last_messages"],
            "avatar_urls": _state().get("avatar_urls", {}),
            "contact_profiles": _cache().get_contacts(owner_wxid=_contact_owner_wxid()),
            "session_cache": _messages().get_sessions(),
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
    if not _state()["self_info"]:
        _state()["self_info"] = await wechat_api.get_self_info()
    return _state()["self_info"]


# ─── REST API: Contacts ───────────────────────────────────────────

@app.get("/api/contacts")
async def get_contacts():
    """Refresh contacts incrementally from Hook when the user opens Contacts."""
    return await _refresh_contacts_incremental(list_type="0", init_if_empty=True)


@app.get("/api/contacts/refresh")
async def refresh_contacts():
    """Force refresh contacts from Hook without re-running InitContact."""
    return await _refresh_contacts_incremental(list_type="0", init_if_empty=True, force_details=True)


def _search_contact_item(row: dict, *, is_group: bool | None = None) -> dict:
    wxid = str(_row_value(row, "UserName", "userName", "username", "wxid", "gid") or "").strip()
    remark = str(_row_value(row, "Remark", "remark", "markname") or "").strip()
    nickname = str(_row_value(row, "NickName", "nickname", "gname", "name") or "").strip()
    alias = str(_row_value(row, "Alias", "alias", "account") or "").strip()
    avatar = str(_row_value(
        row,
        "SmallHeadImgUrl", "smallHeadImgUrl", "avatar", "BigHeadImgUrl", "bigHeadImgUrl",
    ) or "").strip()
    group = wxid.endswith("@chatroom") if is_group is None else is_group
    if not avatar and wxid:
        avatar = f"/api/avatar/{wxid}"
    return {
        "wxid": wxid,
        "name": remark or nickname or alias or wxid,
        "nickname": nickname,
        "remark": remark,
        "account": alias or (wxid if not wxid.startswith("wxid_") else ""),
        "avatar": avatar,
        "is_group": bool(group),
    }


def _search_matches_contact(contact: dict, keyword: str) -> bool:
    folded = keyword.casefold()
    profile = contact.get("profile") if isinstance(contact.get("profile"), dict) else {}
    values = [
        contact.get("wxid"), contact.get("name"),
        profile.get("Alias"), profile.get("alias"),
        profile.get("Remark"), profile.get("remark"),
        profile.get("NickName"), profile.get("nickname"),
        profile.get("PYInitial"), profile.get("QuanPin"),
        profile.get("RemarkPYInitial"), profile.get("RemarkQuanPin"),
    ]
    return any(folded in str(value or "").casefold() for value in values)


def _cached_search_results(keyword: str, limit: int, owner_wxid: str) -> tuple[list[dict], list[dict], list[dict]]:
    cached_contacts = _cache().get_contacts(owner_wxid=owner_wxid)
    matched = [
        contact for contact in cached_contacts.values()
        if _search_matches_contact(contact, keyword)
    ][:limit]
    contacts = [_search_contact_item(contact, is_group=False) for contact in matched if not contact.get("is_group")]
    groups_by_id = {
        str(contact.get("wxid") or ""): {
            **_search_contact_item(contact, is_group=True),
            "matched_members": [],
        }
        for contact in matched
        if contact.get("is_group")
    }
    for member in _cache().search_group_members(keyword, limit * 4, owner_wxid=owner_wxid):
        gid = str(member.get("gid") or "")
        if not gid:
            continue
        group_contact = cached_contacts.get(gid) or {"wxid": gid, "name": gid, "is_group": True}
        group = groups_by_id.setdefault(gid, {
            **_search_contact_item(group_contact, is_group=True),
            "matched_members": [],
        })
        group["matched_members"].append({
            "wxid": str(member.get("wxid") or ""),
            "name": str(member.get("name") or member.get("wxid") or ""),
            "account": str(member.get("wxid") or ""),
        })

    message_hits = _cache().search_all_messages(keyword, limit, owner_wxid=owner_wxid)
    sessions_by_id = {
        str(row.get("strUsrName") or ""): row
        for row in _cache().get_sessions(owner_wxid=owner_wxid)
    }
    messages = []
    for hit in message_hits:
        wxid = str(hit.get("wxid") or "")
        contact = cached_contacts.get(wxid) or {}
        session = sessions_by_id.get(wxid) or {}
        item = _search_contact_item(contact or {
            "wxid": wxid,
            "name": session.get("strNickName") or wxid,
        })
        messages.append({
            **item,
            "match_count": int(hit.get("match_count") or 0),
            "latest_timestamp": int(hit.get("latest_timestamp") or 0),
        })
    return contacts, list(groups_by_id.values())[:limit], messages


@app.get("/api/search")
async def search_local_data(q: str = "", limit: int = 30):
    keyword = str(q or "").strip()
    if not keyword:
        return {"query": "", "contacts": [], "groups": [], "messages": []}
    limit = max(1, min(int(limit or 30), 50))
    owner_wxid = _contact_owner_wxid()

    if config.IS_PROTOCOL:
        contacts, groups, messages = _cached_search_results(keyword, limit, owner_wxid)
        return {"query": keyword, "contacts": contacts, "groups": groups, "messages": messages, "source": "cache"}

    escaped = (
        keyword
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace("'", "''")
    )
    pattern = f"%{escaped}%"
    contact_fields = (
        "UserName", "Alias", "Remark", "NickName",
        "PYInitial", "QuanPin", "RemarkPYInitial", "RemarkQuanPin",
    )
    contact_where = " OR ".join(f"contact.{field} LIKE '{pattern}' ESCAPE '\\'" for field in contact_fields)
    member_where = " OR ".join(f"member.{field} LIKE '{pattern}' ESCAPE '\\'" for field in contact_fields)
    contact_sql = (
        "SELECT contact.UserName AS UserName, contact.Alias AS Alias, "
        "contact.Remark AS Remark, contact.NickName AS NickName, "
        "contact.PYInitial AS PYInitial, contact.QuanPin AS QuanPin, "
        "contact.RemarkPYInitial AS RemarkPYInitial, contact.RemarkQuanPin AS RemarkQuanPin, "
        "COALESCE(NULLIF(head.smallHeadImgUrl, ''), NULLIF(head.bigHeadImgUrl, ''), "
        "NULLIF(contact.SmallHeadImgUrl, ''), contact.BigHeadImgUrl, '') AS SmallHeadImgUrl, "
        "contact.Type AS Type, contact.VerifyFlag AS VerifyFlag "
        "FROM Contact AS contact "
        "LEFT JOIN ContactHeadImgUrl AS head ON head.usrName = contact.UserName "
        f"WHERE {contact_where} LIMIT {limit * 4}"
    )
    member_sql = (
        "SELECT room.ChatRoomName, group_contact.Remark AS GroupRemark, "
        "group_contact.NickName AS GroupNickname, "
        "COALESCE(NULLIF(group_head.smallHeadImgUrl, ''), NULLIF(group_head.bigHeadImgUrl, ''), "
        "NULLIF(group_contact.SmallHeadImgUrl, ''), group_contact.BigHeadImgUrl, '') AS GroupAvatar, "
        "member.UserName AS MemberWxid, member.Alias AS MemberAlias, "
        "member.Remark AS MemberRemark, member.NickName AS MemberNickname "
        "FROM Contact AS member "
        "JOIN ChatRoom AS room ON instr('^G' || room.UserNameList || '^G', '^G' || member.UserName || '^G') > 0 "
        "LEFT JOIN Contact AS group_contact ON group_contact.UserName = room.ChatRoomName "
        "LEFT JOIN ContactHeadImgUrl AS group_head ON group_head.usrName = room.ChatRoomName "
        f"WHERE {member_where} LIMIT {limit * 8}"
    )

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
    with wechat_api.use_agent(agent_id):
        contact_payload, member_payload, native_message_hits = await asyncio.gather(
            wechat_api.query_db("MicroMsg.db", contact_sql, timeout=30.0),
            wechat_api.query_db("MicroMsg.db", member_sql, timeout=30.0),
            wechat_api.search_local_messages(keyword, limit * 3),
        )

    contact_rows = contact_payload.get("data", []) if isinstance(contact_payload, dict) else []
    member_rows = member_payload.get("data", []) if isinstance(member_payload, dict) else []
    if not isinstance(contact_rows, list):
        contact_rows = []
    if not isinstance(member_rows, list):
        member_rows = []

    contacts: list[dict] = []
    groups_by_id: dict[str, dict] = {}
    for row in contact_rows:
        if not isinstance(row, dict):
            continue
        item = _search_contact_item(row)
        if not item["wxid"]:
            continue
        if item["is_group"]:
            groups_by_id[item["wxid"]] = {**item, "matched_members": []}
        elif item["wxid"] != owner_wxid:
            contacts.append(item)

    for row in member_rows:
        if not isinstance(row, dict):
            continue
        gid = str(_row_value(row, "ChatRoomName", "chatRoomName", "gid") or "").strip()
        if not gid:
            continue
        group_remark = str(_row_value(row, "GroupRemark") or "").strip()
        group_nickname = str(_row_value(row, "GroupNickname") or "").strip()
        group = groups_by_id.setdefault(gid, {
            "wxid": gid,
            "name": group_remark or group_nickname or gid,
            "nickname": group_nickname,
            "remark": group_remark,
            "account": "",
            "avatar": str(_row_value(row, "GroupAvatar") or f"/api/avatar/{gid}"),
            "is_group": True,
            "matched_members": [],
        })
        member_wxid = str(_row_value(row, "MemberWxid") or "")
        member_alias = str(_row_value(row, "MemberAlias") or "")
        member_name = str(
            _row_value(row, "MemberRemark")
            or _row_value(row, "MemberNickname")
            or member_alias
            or member_wxid
        )
        group["matched_members"].append({
            "wxid": member_wxid,
            "name": member_name,
            "account": member_alias or (member_wxid if not member_wxid.startswith("wxid_") else ""),
        })

    cached_contacts = _cache().get_contacts(
        [str(hit.get("wxid") or "") for hit in native_message_hits],
        owner_wxid=owner_wxid,
    )
    sessions_by_id = {
        str(row.get("strUsrName") or ""): row
        for row in _cache().get_sessions(owner_wxid=owner_wxid)
    }
    messages: list[dict] = []
    for hit in native_message_hits[:limit]:
        wxid = str(hit.get("wxid") or "")
        contact = cached_contacts.get(wxid) or {}
        session = sessions_by_id.get(wxid) or {}
        item = _search_contact_item(contact or {
            "wxid": wxid,
            "name": session.get("strNickName") or wxid,
        })
        messages.append({
            **item,
            "match_count": int(hit.get("match_count") or 0),
            "latest_timestamp": int(hit.get("latest_timestamp") or 0),
        })

    return {
        "query": keyword,
        "contacts": contacts[:limit],
        "groups": list(groups_by_id.values())[:limit],
        "messages": messages,
        "source": "hook_db",
    }


@app.get("/api/local-contacts")
async def get_local_contacts():
    """Read the native local directory and merge ContactHeadImgUrl avatars."""
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
    with wechat_api.use_agent(agent_id):
        contacts_raw = await wechat_api.get_friend_and_chatroom_list("0")
        avatars_raw = await wechat_api.query_db(
            "MicroMsg.db",
            "select * from ContactHeadImgUrl",
            timeout=60.0,
        )

    if not isinstance(contacts_raw, dict) or contacts_raw.get("error"):
        return {
            "error": str((contacts_raw or {}).get("error") or "GetFriendAndChatRoomList failed"),
            "categories": {"groups": [], "official": [], "service": [], "openim": [], "friends": []},
        }

    contacts = _contact_payload(contacts_raw)
    if not isinstance(contacts, dict):
        contacts = contacts_raw

    avatar_rows = avatars_raw.get("data") if isinstance(avatars_raw, dict) else []
    avatar_warning = str(avatars_raw.get("error") or "").strip() if isinstance(avatars_raw, dict) else ""
    if isinstance(avatar_rows, dict):
        avatar_rows = avatar_rows.get("data") or avatar_rows.get("rows") or []
    if not isinstance(avatar_rows, list):
        avatar_rows = []
    avatar_map: dict[str, str] = {}
    for row in avatar_rows:
        if not isinstance(row, dict):
            continue
        wxid = str(_row_value(row, "usrName", "UsrName", "username", "wxid") or "").strip()
        avatar = str(
            _row_value(
                row,
                "smallHeadImgUrl", "SmallHeadImgUrl", "smallheadimgurl",
                "bigHeadImgUrl", "BigHeadImgUrl", "bigheadimgurl",
            ) or ""
        ).strip()
        if wxid and avatar:
            avatar_map[wxid] = avatar

    owner_wxid = _contact_owner_wxid()
    cached = _cache().get_contacts(owner_wxid=owner_wxid)
    categories: dict[str, list[dict]] = {
        "groups": [], "official": [], "service": [], "openim": [], "friends": [],
    }
    seen: set[str] = set()

    def add_entry(raw: dict, category: str, *, group: bool = False) -> None:
        if not isinstance(raw, dict):
            return
        wxid = str(
            raw.get("gid")
            or raw.get("wxid")
            or raw.get("UserName")
            or raw.get("userName")
            or ""
        ).strip()
        if not wxid or wxid in seen:
            return
        seen.add(wxid)
        markname = str(raw.get("markname") or raw.get("Remark") or raw.get("remark") or "").strip()
        nickname = str(
            raw.get("gname")
            or raw.get("nickname")
            or raw.get("NickName")
            or raw.get("name")
            or ""
        ).strip()
        account = str(raw.get("account") or raw.get("Alias") or raw.get("alias") or "").strip()
        name = markname or nickname or account or wxid
        avatar = avatar_map.get(wxid, "")
        profile = dict(raw)
        profile.update({
            "wxid": wxid,
            "markname": markname,
            "nickname": nickname,
            "account": account,
            "SmallHeadImgUrl": avatar,
            "BigHeadImgUrl": avatar,
        })
        categories[category].append({
            "wxid": wxid,
            "name": name,
            "markname": markname,
            "nickname": nickname,
            "account": account,
            "avatar": avatar,
            "is_group": group,
            "category": category,
            "profile": profile,
        })

    for raw in contacts.get("chatroom", []) if isinstance(contacts.get("chatroom"), list) else []:
        add_entry(raw, "groups", group=True)
    for raw in contacts.get("friend", []) if isinstance(contacts.get("friend"), list) else []:
        add_entry(raw, "friends")
    for raw in contacts.get("openim", []) if isinstance(contacts.get("openim"), list) else []:
        add_entry(raw, "openim")
    for raw in contacts.get("gh", []) if isinstance(contacts.get("gh"), list) else []:
        if not isinstance(raw, dict):
            continue
        wxid = str(raw.get("wxid") or "").strip()
        cached_entry = cached.get(wxid) if wxid else None
        cached_profile = cached_entry.get("profile") if isinstance(cached_entry, dict) and isinstance(cached_entry.get("profile"), dict) else {}
        category = _contact_directory_category({**raw, **cached_profile}, wxid)
        if category in {"official", "service"}:
            add_entry(raw, category)

    counts = {key: len(value) for key, value in categories.items()}
    return {
        "categories": categories,
        "counts": counts,
        "source_counts": {
            "chatroom": _to_int(contacts.get("count_chatroom")),
            "friend": _to_int(contacts.get("count_friend")),
            "gh": _to_int(contacts.get("count_gh")),
            "openim": _to_int(contacts.get("count_openim")),
        },
        "avatar_count": len(avatar_map),
        "warning": f"头像查询失败：{avatar_warning}" if avatar_warning else "",
    }


@app.get("/api/contacts/{wxid}")
async def get_contact_detail(wxid: str):
    """Get detailed info for a specific contact."""
    return await wechat_api.get_friend_detail_info(wxid)


@app.get("/api/contacts/{wxid}/avatar")
async def get_contact_avatar(wxid: str):
    """Return pre-loaded avatar URL for a contact."""
    url = _state().get("avatar_urls", {}).get(wxid, "")
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


def _real_contact_wxid(wxid: str) -> str:
    value = str(wxid or "").strip()
    if not value or value == "default" or value.startswith("agent:"):
        return ""
    return value


def _prioritize_wxids(wxids: list[str], *priority: str) -> list[str]:
    ordered = [_real_contact_wxid(wxid) for wxid in priority]
    ordered.extend(wxids or [])
    return _normalize_wxids(ordered)


async def _query_session_list_from_db() -> dict:
    """Read the native WeChat Session table ordered by nOrder."""
    # Keep the /agent QueryDB payload narrow. The remote WS path has crashed
    # when returning Session.* because bytesXml can contain invalid/binary text.
    sql = (
        "select strUsrName, strNickName, strContent, nMsgType, nTime, "
        "nOrder, nUnReadCount, othersAtMe, nIsSend as isSender "
        "from Session order by nOrder desc"
    )
    data = await wechat_api.query_db("MicroMsg.db", sql, timeout=20.0)
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


_PROTOCOL_CONTACT_PIN_BIT = 0x800
_PROTOCOL_STATUSNOTIFY_ORDER_BASE = 100_000_000_000
_PROTOCOL_PINNED_ORDER_BASE = 1_000_000_000_000


def _statusnotify_payload(data) -> dict:
    if not isinstance(data, dict):
        return {}
    payload = data.get("statusnotify")
    if isinstance(payload, dict):
        return payload
    nested = data.get("data")
    if isinstance(nested, dict):
        payload = nested.get("statusnotify")
        if isinstance(payload, dict):
            return payload
    return data if "chat_contact_list" in data else {}


def _statusnotify_response_ok(data) -> bool:
    if not isinstance(data, dict) or data.get("ok") is False or data.get("error"):
        return False
    payload = _statusnotify_payload(data)
    if not payload:
        return False
    base = payload.get("base_response")
    if isinstance(base, dict):
        ret = base.get("ret_signed", base.get("ret"))
        if ret not in (None, "") and _to_int(ret) != 0:
            return False
    return True


def _statusnotify_contact_ids(data) -> list[str]:
    payload = _statusnotify_payload(data)
    raw_ids = payload.get("chat_contact_list") if isinstance(payload, dict) else []
    if not isinstance(raw_ids, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for raw_wxid in raw_ids:
        wxid = str(raw_wxid or "").strip()
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        ids.append(wxid)
    return ids


def _contact_summary_profile(summary: dict | None) -> dict:
    if not isinstance(summary, dict):
        return {}
    profile = summary.get("profile")
    return profile if isinstance(profile, dict) else summary


def _contact_summary_is_pinned(summary: dict | None) -> bool:
    profile = _contact_summary_profile(summary)
    bit_val = _contact_profile_int(profile, "bit_val", "BitVal", "bitval") or 0
    return bool(bit_val & _PROTOCOL_CONTACT_PIN_BIT)


def _protocol_contact_display_name(wxid: str, summary: dict | None) -> str:
    profile = _contact_summary_profile(summary)
    explicit_name = _contact_profile_explicit_name(profile).strip()
    if explicit_name and explicit_name != wxid:
        return explicit_name
    cached_name = str((summary or {}).get("name") or "").strip() if isinstance(summary, dict) else ""
    if cached_name and cached_name != wxid:
        return cached_name
    return explicit_name or cached_name or wxid


def _protocol_session_is_pinned(row: dict | None, summary: dict | None = None) -> bool:
    if _contact_summary_is_pinned(summary):
        return True
    if not isinstance(row, dict):
        return False
    flag = row.get("pinned")
    if isinstance(flag, str):
        flag = flag.strip().lower() in {"1", "true", "yes", "on"}
    return bool(flag) or _to_int(_row_value(row, "nOrder", "NOrder", "order")) >= _PROTOCOL_PINNED_ORDER_BASE


def _protocol_local_session_row(row: dict, summary: dict | None = None) -> dict:
    merged = dict(row)
    wxid = str(_row_value(merged, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
    contact_name = _protocol_contact_display_name(wxid, summary)
    if contact_name and contact_name != wxid:
        merged["strNickName"] = contact_name
    order_value = _to_int(_row_value(merged, "nOrder", "NOrder", "order"))
    timestamp = _to_int(_row_value(
        merged,
        "nTime",
        "NTime",
        "nUpdateTime",
        "nCreateTime",
        "CreateTime",
        "timestamp",
        "lastTimestamp",
    ))
    pinned = _protocol_session_is_pinned(merged, summary)
    if pinned:
        if order_value < _PROTOCOL_PINNED_ORDER_BASE:
            local_rank = order_value if 0 < order_value < _PROTOCOL_STATUSNOTIFY_ORDER_BASE else timestamp
            order_value = _PROTOCOL_PINNED_ORDER_BASE + max(local_rank, timestamp, 1)
    elif order_value >= _PROTOCOL_STATUSNOTIFY_ORDER_BASE:
        order_value = timestamp
    merged.update({
        "nOrder": order_value,
        "order": order_value,
        "pinned": pinned,
        "statusnotify_recent": False,
    })
    merged.pop("statusnotify_index", None)
    return merged


def _protocol_session_rows(
    wxids: list[str],
    contact_profiles: dict[str, dict],
    cached_rows: list[dict],
) -> list[dict]:
    cached_by_wxid = {
        str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip(): row
        for row in cached_rows
        if isinstance(row, dict)
    }
    count = len(wxids)
    rows: list[dict] = []
    for index, wxid in enumerate(wxids):
        summary = contact_profiles.get(wxid) if isinstance(contact_profiles, dict) else {}
        profile = _contact_summary_profile(summary)
        name = _protocol_contact_display_name(wxid, summary)
        bit_val = _contact_profile_int(profile, "bit_val", "BitVal", "bitval") or 0
        row = dict(cached_by_wxid.get(wxid) or {})
        rank = count - index
        pinned = _protocol_session_is_pinned(row, summary)
        existing_order = _to_int(_row_value(row, "nOrder", "NOrder", "order"))
        if pinned:
            order_value = max(
                existing_order if existing_order >= _PROTOCOL_PINNED_ORDER_BASE else 0,
                _PROTOCOL_PINNED_ORDER_BASE + _PROTOCOL_STATUSNOTIFY_ORDER_BASE + rank,
            )
        else:
            order_value = _PROTOCOL_STATUSNOTIFY_ORDER_BASE + rank
        row.update({
            "strUsrName": wxid,
            "strNickName": name,
            "strContent": _row_value(row, "strContent", "StrContent", "content", "lastMsg"),
            "nMsgType": str(_row_value(row, "nMsgType", "NMsgType", "msgType", "type") or "1"),
            "nUnReadCount": _to_int(_row_value(row, "nUnReadCount", "UnReadCount", "unread")),
            "othersAtMe": _to_int(_row_value(row, "othersAtMe", "OthersAtMe", "atMe")),
            "nTime": _to_int(_row_value(
                row,
                "nTime",
                "NTime",
                "nUpdateTime",
                "nCreateTime",
                "CreateTime",
                "timestamp",
                "lastTimestamp",
            )),
            "nOrder": order_value,
            "order": order_value,
            "pinned": pinned,
            "bit_val": bit_val,
            "statusnotify_index": index,
            "statusnotify_recent": True,
        })
        rows.append(row)
    return rows


def _merge_protocol_session_rows(
    status_rows: list[dict],
    cached_rows: list[dict],
    contact_profiles: dict[str, dict],
) -> list[dict]:
    recent_wxids = {
        str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
        for row in status_rows
        if isinstance(row, dict)
    }
    merged = [dict(row) for row in status_rows if isinstance(row, dict)]
    for cached in cached_rows:
        if not isinstance(cached, dict):
            continue
        wxid = str(_row_value(cached, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
        if not wxid or wxid in recent_wxids:
            continue
        merged.append(_protocol_local_session_row(cached, contact_profiles.get(wxid)))

    merged.sort(key=lambda row: (
        1 if _protocol_session_is_pinned(row, contact_profiles.get(str(row.get("strUsrName") or ""))) else 0,
        1 if row.get("statusnotify_recent") else 0,
        _to_int(_row_value(row, "nOrder", "NOrder", "order")),
        _to_int(_row_value(row, "nTime", "NTime", "timestamp", "lastTimestamp")),
    ), reverse=True)
    return merged


async def _refresh_protocol_session_list(owner_wxid: str, account_id: str) -> dict:
    with wechat_api.use_agent(account_id):
        status_data = await wechat_api.status_notify(
            code=3,
            function_name="",
            function_arg="",
        )
    if not _statusnotify_response_ok(status_data):
        raise RuntimeError(str(
            (status_data or {}).get("error")
            or (status_data or {}).get("message")
            or "statusnotify returned a non-success response"
        ))

    wxids = _statusnotify_contact_ids(status_data)
    if wxids:
        with wechat_api.use_agent(account_id):
            await _fetch_and_cache_contact_details(
                wxids,
                broadcast_updates=False,
                owner_wxid=owner_wxid,
                account_id=account_id,
            )

    cached_rows = _session_rows_from_cache(owner_wxid)
    cached_wxids = [
        str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
        for row in cached_rows
        if isinstance(row, dict)
    ]
    merged_wxids = _normalize_wxids([*wxids, *cached_wxids])
    contact_profiles = _cache().get_contacts(merged_wxids, owner_wxid=owner_wxid) if merged_wxids else {}
    status_rows = _protocol_session_rows(wxids, contact_profiles, cached_rows)
    session_rows = _merge_protocol_session_rows(status_rows, cached_rows, contact_profiles)
    if session_rows:
        _cache().upsert_sessions(session_rows, owner_wxid=owner_wxid)

    snapshot = {"data": session_rows}
    last_messages = _last_messages_from_session_rows(session_rows)
    _state()["sessions"] = snapshot
    _state()["last_messages"] = last_messages
    _state()["session_list_loaded"] = True
    _log(
        f"[REFRESH] protocol statusnotify recent={len(status_rows)} merged={len(session_rows)} "
        f"pinned={sum(1 for row in session_rows if row.get('pinned'))}"
    )
    return {
        "sessions": snapshot,
        "last_messages": last_messages,
        "contact_profiles": contact_profiles,
        "source": "protocol_statusnotify",
        "upstream": {
            "chat_contact_count": len(wxids),
            "msg_id": _statusnotify_payload(status_data).get("msg_id"),
            "new_msg_id": _statusnotify_payload(status_data).get("new_msg_id"),
        },
    }


def _session_rows_from_cache(owner_wxid: str = "") -> list[dict]:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    return _cache().get_sessions(owner_wxid=owner_wxid)


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
    _state()["sessions"] = snapshot
    _state()["last_messages"] = {**(_state().get("last_messages") or {}), **last_messages}
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
        or profile.get("id")
        or profile.get("user_name")
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
        profile.get("markname")
        or profile.get("Remark")
        or profile.get("remark")
        or profile.get("nick_name")
        or profile.get("nickname")
        or profile.get("NickName")
        or profile.get("nick")
        or profile.get("strNickName")
        or _contact_profile_wxid(profile)
        or ""
    )


def _contact_profile_explicit_name(profile: dict) -> str:
    return str(
        profile.get("markname")
        or profile.get("Remark")
        or profile.get("remark")
        or profile.get("nick_name")
        or profile.get("nickname")
        or profile.get("NickName")
        or profile.get("nick")
        or profile.get("strNickName")
        or ""
    )


def _contact_profile_avatar(profile: dict) -> str:
    return str(
        profile.get("SmallHeadImgUrl")
        or profile.get("BigHeadImgUrl")
        or profile.get("small_head_url")
        or profile.get("big_head_url")
        or profile.get("small_head_img_url")
        or profile.get("big_head_img_url")
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


def _contact_profile_int(profile: dict, *keys: str) -> int | None:
    for key in keys:
        if key not in profile:
            continue
        value = profile.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                continue
    return None


def _contact_profile_account_category(profile: dict, wxid: str = "") -> str:
    wxid = str(wxid or _contact_profile_wxid(profile) or "").strip()
    if not wxid.startswith("gh_"):
        return ""

    service_type = _contact_profile_int(profile, "ServiceType", "serviceType", "service_type")
    if service_type is not None:
        if service_type == 0:
            return "official"
        if service_type == 1:
            return "service"
    return ""


def _contact_profile_needs_account_type(profile: dict, wxid: str = "") -> bool:
    wxid = str(wxid or _contact_profile_wxid(profile) or "").strip()
    if not wxid.startswith("gh_"):
        return False
    if _contact_profile_int(profile, "ServiceType", "serviceType", "service_type") is not None:
        return False
    return not _contact_profile_account_category(profile, wxid)


def _contact_directory_category(profile: dict, wxid: str = "") -> str:
    """Match the Contacts page buckets: personal, groups, official, service, openim."""
    if not isinstance(profile, dict):
        profile = {}
    wxid = str(wxid or _contact_profile_wxid(profile) or "").strip()
    if not wxid:
        return ""
    if wxid.endswith("@chatroom"):
        return "groups"
    if (
        wxid.endswith("@openim")
        or bool(profile.get("OpenIM") or profile.get("OpenIMDetail") or profile.get("openim_detail"))
    ):
        return "openim"
    if wxid.startswith("gh_"):
        return _contact_profile_account_category(profile, wxid)
    return "personal"


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


def _protocol_initcontact_entries(payload: dict | list) -> list[dict]:
    """Flatten the protocol /initcontact paged wxid matrix into contact rows."""
    payload = _contact_payload(payload)
    if not isinstance(payload, dict):
        return []

    raw_groups = payload.get("initcontact")
    if not isinstance(raw_groups, list):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    stack = list(reversed(raw_groups))
    while stack:
        value = stack.pop()
        if isinstance(value, (list, tuple)):
            stack.extend(reversed(value))
            continue
        if isinstance(value, dict):
            wxid = _contact_profile_wxid(value)
            entry = dict(value)
        else:
            wxid = str(value or "").strip()
            entry = {"user_name": wxid}
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        entry.setdefault("wxid", wxid)
        entry.setdefault("gid", wxid if wxid.endswith("@chatroom") else "")
        entry.setdefault("type", "chatroom" if wxid.endswith("@chatroom") else "friend")
        out.append(entry)
    return out


def _contacts_from_batch_brief_response(data) -> list[dict]:
    """Normalize Hook and protocol BatchGetContactBriefInfo response envelopes."""
    if isinstance(data, list):
        source = data
    elif isinstance(data, dict):
        current = data
        for _ in range(4):
            nested = current.get("batchgetcontactbriefinfo")
            if isinstance(nested, dict):
                current = nested
                continue
            nested = current.get("data")
            if isinstance(nested, dict) and not any(
                isinstance(current.get(key), list) for key in ("info", "contacts", "list")
            ):
                current = nested
                continue
            break
        source = current.get("info") or current.get("contacts") or current.get("list") or []
    else:
        source = []

    if not isinstance(source, list):
        return []

    contacts: list[dict] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        nested_contact = item.get("contact")
        profile = dict(nested_contact) if isinstance(nested_contact, dict) else dict(item)
        wxid = _contact_profile_wxid(profile) or str(
            item.get("user_name") or item.get("userName") or item.get("UserName") or ""
        ).strip()
        if not wxid:
            continue
        if not _contact_profile_wxid(profile):
            profile["user_name"] = wxid
        contacts.append(profile)
    return contacts


def _contacts_from_getcontact_response(data) -> list[dict]:
    """Normalize GetContact envelopes from local/remote Hook into contact rows."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []

    current = data
    for _ in range(4):
        nested_getcontact = current.get("getcontact")
        if isinstance(nested_getcontact, dict):
            current = nested_getcontact
            continue
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


def _getcontact_response_ok(data) -> bool:
    if isinstance(data, list):
        return True
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return False
    for key in ("code", "ret", "Ret"):
        if key not in data:
            continue
        try:
            value = int(data.get(key) or 0)
        except Exception:
            continue
        if key == "code" and value != 0:
            return False
        if key.lower() == "ret" and value <= 0:
            return False
    status = data.get("status")
    if status is not None:
        try:
            if int(status) not in (0, 200):
                return False
        except Exception:
            pass
    text = str(data.get("retmsg") or data.get("msg") or data.get("message") or "").lower()
    if text and any(token in text for token in ("fail", "failed", "error", "异常", "失败")):
        return False
    return True


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
            *_protocol_initcontact_entries(contacts),
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
    cached = _cache().get_contacts(owner_wxid=owner_wxid)
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
            "name": raw.get("name") or name,
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
        cached = _bucket('_CONTACT_LABEL_CACHE', _CONTACT_LABEL_CACHE).get("map")
        cached_ts = float(_bucket('_CONTACT_LABEL_CACHE', _CONTACT_LABEL_CACHE).get("ts", 0) or 0)
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
            _bucket('_CONTACT_LABEL_CACHE', _CONTACT_LABEL_CACHE)["map"] = label_map
            _bucket('_CONTACT_LABEL_CACHE', _CONTACT_LABEL_CACHE)["ts"] = now
        cached = _bucket('_CONTACT_LABEL_CACHE', _CONTACT_LABEL_CACHE).get("map")
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


def _contact_needs_detail_hydration(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
    if entry.get("avatar") or _contact_profile_avatar(profile):
        return _contact_profile_needs_account_type(profile, str(entry.get("wxid") or ""))
    return True


def _contact_cache_needs_hydration(wxid: str, entry: dict | None) -> bool:
    if not wxid or not isinstance(entry, dict):
        return True
    profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
    avatar = str(entry.get("avatar") or _contact_profile_avatar(profile) or "").strip()
    if not avatar:
        return True
    return _contact_profile_needs_account_type(profile, wxid)


def _contact_detail_missing_ids(owner_wxid: str = "", candidates: list[str] | None = None) -> list[str]:
    owner = owner_wxid or _contact_owner_wxid()
    candidate_list = [str(wxid or "").strip() for wxid in (candidates or []) if str(wxid or "").strip()] if candidates is not None else None
    cached = _cache().get_contacts(candidate_list, owner_wxid=owner)
    missing: list[str] = []
    if candidate_list is None:
        for wxid, entry in cached.items():
            if wxid and _contact_needs_detail_hydration(entry):
                missing.append(wxid)
        return missing

    for wxid in candidate_list:
        entry = cached.get(wxid)
        if _contact_cache_needs_hydration(wxid, entry):
            missing.append(wxid)
    return missing


def _contact_account_category_missing_ids(owner_wxid: str = "", candidates: list[str] | None = None) -> list[str]:
    owner = owner_wxid or _contact_owner_wxid()
    candidate_list = [str(wxid or "").strip() for wxid in (candidates or []) if str(wxid or "").strip()] if candidates is not None else None
    cached = _cache().get_contacts(candidate_list, owner_wxid=owner)
    missing: list[str] = []
    wxids = candidate_list if candidate_list is not None else list(cached.keys())
    for wxid in wxids:
        if not wxid.startswith("gh_"):
            continue
        entry = cached.get(wxid) or {}
        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
        if _contact_profile_needs_account_type(profile, wxid):
            missing.append(wxid)
    return missing


def _contact_cache_missing_avatar(wxid: str, entry: dict | None) -> bool:
    return _contact_cache_needs_hydration(wxid, entry)


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
        *_protocol_initcontact_entries(contacts),
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
        _messages().set_contact(wxid, name=name, avatar=avatar)
        raw_contact_updates[wxid] = {
            "wxid": wxid,
            "name": name or wxid,
            "avatar": avatar,
            "is_group": wxid.endswith("@chatroom"),
            "profile": dict(c),
        }
    if raw_contact_updates:
        _cache().upsert_contacts(raw_contact_updates, owner_wxid=owner_wxid or _contact_owner_wxid())
    return len(friend_list), len(room_list)


async def _broadcast_contact_profile_updates(updates: dict[str, dict], *, account_id: str = "") -> None:
    if not updates:
        return
    await manager.broadcast({
        "type": "contact_profiles",
        "data": {
            "account_id": account_id or _current_agent_id() or agent_manager.active_id(),
            "members": updates,
        },
    })


async def _broadcast_contacts_snapshot(snapshot: dict, *, account_id: str = "", owner_wxid: str = "") -> None:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    await manager.broadcast({
        "type": "contacts_snapshot",
        "data": {
            "account_id": account_id or _current_agent_id() or agent_manager.active_id(),
            "contacts": snapshot,
            "contact_profiles": _cache().get_contacts(owner_wxid=owner_wxid),
            "hydration_progress": _CONTACT_HYDRATION_PROGRESS.get(owner_wxid) or {},
        },
    })


async def _broadcast_contact_hydration_progress(owner_wxid: str, account_id: str = "") -> None:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    await manager.broadcast({
        "type": "contacts_hydration_progress",
        "data": {
            "account_id": account_id or _current_agent_id() or agent_manager.active_id(),
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
            avatar_urls = _state().setdefault("avatar_urls", {})
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                profile = dict(profile)
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
                _messages().set_contact(wxid, name=name, avatar=avatar)
                updates[wxid] = summary
            if updates:
                _cache().upsert_contacts(updates, owner_wxid=owner_wxid)
    return updates


async def _fetch_and_cache_openim_profiles(
    wxids: list[str],
    *,
    gid: str = "",
    broadcast_updates: bool = False,
    owner_wxid: str = "",
    account_id: str = "",
) -> dict[str, dict]:
    targets: list[str] = []
    seen: set[str] = set()
    for wxid in wxids or []:
        wxid = str(wxid or "").strip()
        if not wxid or wxid in seen or not wxid.endswith("@openim"):
            continue
        seen.add(wxid)
        targets.append(wxid)
    if not targets:
        return {}

    owner_wxid = owner_wxid or _contact_owner_wxid()
    result: dict[str, dict] = {}
    sem = asyncio.Semaphore(_hook_api_parallelism())

    async def fetch_one(wxid: str) -> dict[str, dict]:
        async with sem:
            try:
                data = await wechat_api.get_openim_contact(wxid, gid=gid)
                payload = _find_openim_payload(data)
                if not payload:
                    return {}
                profile = _openim_profile_from_payload(payload, wxid, gid=gid)
                return await _cache_contact_profiles([profile], owner_wxid=owner_wxid)
            except Exception as e:
                _log(f"[PROFILE] GetOpenIMContact failed ({wxid}): {type(e).__name__}: {e}")
                return {}

    for updates in await asyncio.gather(*(fetch_one(wxid) for wxid in targets)):
        result.update(updates)
        if broadcast_updates and updates:
            await _broadcast_contact_profile_updates(updates, account_id=account_id)

    return result


async def _fetch_and_cache_contact_details(
    wxids: list[str],
    *,
    broadcast_updates: bool = False,
    broadcast_progress: bool = False,
    owner_wxid: str = "",
    account_id: str = "",
    gid: str = "",
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
    openim_targets = [wxid for wxid in targets if wxid.endswith("@openim")]
    regular_targets = [wxid for wxid in targets if not wxid.endswith("@openim")]
    if openim_targets:
        openim_updates = await _fetch_and_cache_openim_profiles(
            openim_targets,
            gid=gid,
            broadcast_updates=broadcast_updates,
            owner_wxid=owner_wxid,
            account_id=account_id,
        )
        result.update(openim_updates)
    targets = regular_targets
    if not targets:
        return result

    total = len(targets)
    total_batches = (total + 99) // 100
    processed = 0
    failed = 0
    updated_total = 0

    if broadcast_progress:
        _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
            "active": True,
            "phase": "BatchGetContactBriefInfo",
            "batch": 0,
            "total_batches": total_batches,
            "processed": 0,
            "total": total,
            "updated": 0,
            "failed": 0,
        }
        await _broadcast_contact_hydration_progress(owner_wxid, account_id)

    batches = [targets[i:i + 100] for i in range(0, len(targets), 100)]
    max_concurrency = max(1, min(int(getattr(config, "HOOK_API_CONCURRENCY", 1)), len(batches)))
    fetch_semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_batch(batch_index: int, batch: list[str]) -> tuple[int, list[str], dict | None, Exception | None]:
        async with fetch_semaphore:
            try:
                wxid_str = ",".join(batch)
                data = await wechat_api.batch_get_contact_brief_info(wxid_str)
                return batch_index, batch, data, None
            except Exception as exc:
                return batch_index, batch, None, exc

    _log(
        f"[CONTACTS] BatchGetContactBriefInfo fetching {len(batches)} batches concurrently: "
        f"owner={owner_wxid} concurrency={max_concurrency} batch_size=100"
    )
    fetched_batches = await asyncio.gather(*(
        _fetch_batch(index, batch)
        for index, batch in enumerate(batches, start=1)
    ))

    for batch_index, batch, data, fetch_error in fetched_batches:
        batch_update_count = 0
        try:
            if fetch_error is not None:
                raise fetch_error
            if not _getcontact_response_ok(data):
                raise RuntimeError(
                    f"batch returned non-success ({len(batch)}): {str(data)[:300]}"
                )

            info_list = _contacts_from_batch_brief_response(data)
            found: set[str] = set()
            updates: dict[str, dict] = {}
            for info in info_list:
                if not isinstance(info, dict):
                    continue
                wxid = _contact_profile_wxid(info)
                if not wxid:
                    continue
                found.add(wxid)
                profile = dict(info)
                name = _contact_profile_name(profile)
                avatar = _contact_profile_avatar(profile)
                updates[wxid] = {
                    "wxid": wxid,
                    "name": name or wxid,
                    "avatar": avatar,
                    "is_group": wxid.endswith("@chatroom"),
                    "profile": profile,
                }

            for wxid in batch:
                if wxid in found:
                    continue
                updates[wxid] = {
                    "wxid": wxid,
                    "name": wxid,
                    "avatar": "",
                    "is_group": wxid.endswith("@chatroom"),
                    "profile": {
                        "wxid": wxid,
                        "gid": wxid if wxid.endswith("@chatroom") else "",
                        "type": "chatroom" if wxid.endswith("@chatroom") else "friend",
                    },
                }

            if updates:
                _cache().upsert_contacts(updates, owner_wxid=owner_wxid)
                now = time.time()
                async with _CONTACT_PROFILE_LOCK:
                    async with _CONTACT_BRIEF_LOCK:
                        avatar_urls = _state().setdefault("avatar_urls", {})
                        for wxid, payload in updates.items():
                            profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {"wxid": wxid}
                            cache_key = _contact_cache_key(wxid, owner_wxid)
                            _CONTACT_PROFILE_CACHE[cache_key] = {"profile": profile, "ts": now}
                            _CONTACT_BRIEF_CACHE[cache_key] = {
                                "name": payload.get("name", "") or wxid,
                                "avatar": payload.get("avatar", "") or "",
                                "ts": now,
                            }
                            if payload.get("avatar"):
                                avatar_urls[wxid] = payload["avatar"]
                            _messages().set_contact(wxid, name=payload.get("name", "") or wxid, avatar=payload.get("avatar", "") or "")

            result.update(updates)
            batch_update_count = len(updates)
            updated_total += batch_update_count
            if broadcast_updates and updates:
                await _broadcast_contact_profile_updates(updates, account_id=account_id)
            _log(
                f"[CONTACTS] BatchGetContactBriefInfo hydrated batch {batch_index}/{total_batches}: "
                f"owner={owner_wxid} requested={len(batch)} updates={len(updates)}"
            )
        except Exception as e:
            failed += len(batch)
            _log(f"[CONTACTS] BatchGetContactBriefInfo batch failed ({len(batch)}): {type(e).__name__}: {e}")
        processed += len(batch)
        if broadcast_progress:
            _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
                "active": True,
                "phase": "BatchGetContactBriefInfo",
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
                _state()["contacts"] = snapshot
            await _broadcast_contacts_snapshot(snapshot, account_id=account_id, owner_wxid=owner_wxid)
            await _broadcast_contact_hydration_progress(owner_wxid, account_id)
    if broadcast_progress:
        _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
            "active": False,
            "phase": "BatchGetContactBriefInfo",
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
    ids = _prioritize_wxids(wxids, owner_wxid, _get_self_wxid())
    if not ids or owner_wxid in _CONTACT_HYDRATING_OWNERS:
        return

    total = len(ids)
    total_batches = (total + 99) // 100
    _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
        "active": True,
        "phase": "BatchGetContactBriefInfo",
        "batch": 0,
        "total_batches": total_batches,
        "processed": 0,
        "total": total,
        "updated": 0,
        "failed": 0,
    }

    async def _hydrate_details() -> None:
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
                _state()["contacts"] = snapshot
            await _broadcast_contacts_snapshot(snapshot, account_id=account_id, owner_wxid=owner_wxid)
            _log(f"[CONTACTS] BatchGetContactBriefInfo hydration complete: owner={owner_wxid} ids={len(ids)}")
        except Exception as e:
            _log(f"[CONTACTS] BatchGetContactBriefInfo hydration task failed: {type(e).__name__}: {e}")
        finally:
            _CONTACT_HYDRATING_OWNERS.discard(owner_wxid)

    _log(f"[CONTACTS] scheduling BatchGetContactBriefInfo hydration: owner={owner_wxid} ids={len(ids)}, batch=100")
    _CONTACT_HYDRATING_OWNERS.add(owner_wxid)
    task = asyncio.create_task(_hydrate_details())
    _track_background_send(task, "contacts_briefinfo")


def _schedule_session_contact_hydration(owner_wxid: str, wxids: list[str], account_id: str = "") -> None:
    self_wxid = str(_get_self_wxid() or "").strip()
    ids = _prioritize_wxids(wxids, owner_wxid, self_wxid)
    cached = _cache().get_contacts(ids, owner_wxid=owner_wxid) if ids else {}
    missing_ids = [wxid for wxid in ids if _contact_cache_missing_avatar(wxid, cached.get(wxid))]
    self_missing = [wxid for wxid in missing_ids if wxid == self_wxid]
    brief_ids = [wxid for wxid in missing_ids if wxid != self_wxid]
    if not self_missing and (not brief_ids or owner_wxid in _SESSION_CONTACT_HYDRATING_OWNERS):
        return

    async def _hydrate_session_contacts() -> None:
        _SESSION_CONTACT_HYDRATING_OWNERS.add(owner_wxid)
        try:
            with wechat_api.use_agent(account_id):
                if self_missing:
                    self_updates = await _fetch_and_cache_contact_profiles_via_getcontact(
                        self_missing,
                        broadcast_updates=True,
                        owner_wxid=owner_wxid,
                        account_id=account_id,
                    )
                    _log(
                        f"[SESSIONS] GetContact hydrated self recent-session profile: "
                        f"owner={owner_wxid} ids={len(self_missing)} updates={len(self_updates)}"
                    )
                if brief_ids:
                    openim_brief_ids = [wxid for wxid in brief_ids if wxid.endswith("@openim")]
                    regular_brief_ids = [wxid for wxid in brief_ids if not wxid.endswith("@openim")]
                    updates = await _fetch_and_cache_contact_details(
                        brief_ids,
                        broadcast_updates=True,
                        owner_wxid=owner_wxid,
                        account_id=account_id,
                    )
                    if regular_brief_ids:
                        _log(
                            f"[SESSIONS] BatchGetContactBriefInfo hydrated recent sessions: "
                            f"owner={owner_wxid} ids={len(regular_brief_ids)} updates={len(updates)}"
                        )
                    if openim_brief_ids:
                        _log(
                            f"[SESSIONS] GetOpenIMContact hydrated recent sessions: "
                            f"owner={owner_wxid} ids={len(openim_brief_ids)} updates={len(updates)}"
                        )
        except Exception as e:
            _log(f"[SESSIONS] recent session contact hydration failed: {type(e).__name__}: {e}")
        finally:
            _SESSION_CONTACT_HYDRATING_OWNERS.discard(owner_wxid)

    if self_missing:
        _log(f"[SESSIONS] scheduling self GetContact hydration before brief batch: owner={owner_wxid} ids={len(self_missing)}")
    if brief_ids:
        _log(f"[SESSIONS] scheduling recent-session BatchGetContactBriefInfo hydration: owner={owner_wxid} ids={len(brief_ids)}, batch=100")
    task = asyncio.create_task(_hydrate_session_contacts())
    _track_background_send(task, "sessions_briefinfo")


async def _fetch_and_cache_contact_profiles_via_getcontact(
    wxids: list[str],
    *,
    broadcast_updates: bool = False,
    owner_wxid: str = "",
    account_id: str = "",
) -> dict[str, dict]:
    """Fetch detailed profiles via /GetContact and refresh caches/database."""
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

    owner_wxid = owner_wxid or _contact_owner_wxid()
    result: dict[str, dict] = {}

    for i in range(0, len(targets), 100):
        batch = targets[i:i + 100]
        try:
            data = await wechat_api.get_contact(batch)
            if not _getcontact_response_ok(data):
                _log(
                    f"[PROFILE] GetContact batch returned non-success "
                    f"({len(batch)}): {str(data)[:300]}"
                )
                continue
            contacts = _contacts_from_getcontact_response(data)
            profiles: list[dict] = []
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                wxid = _contact_profile_wxid(contact)
                if not wxid:
                    continue
                profiles.append(dict(contact))
            if not profiles:
                continue
            updates = await _cache_contact_profiles(profiles, owner_wxid=owner_wxid)
            result.update(updates)
            if broadcast_updates and updates:
                await _broadcast_contact_profile_updates(updates, account_id=account_id)
            _log(
                f"[PROFILE] GetContact refreshed batch: owner={owner_wxid} "
                f"requested={len(batch)} updates={len(updates)}"
            )
        except Exception as e:
            _log(f"[PROFILE] GetContact batch failed ({len(batch)}): {type(e).__name__}: {e}")
        await asyncio.sleep(0.02)

    return result


async def _refresh_contacts_incremental(
    *,
    list_type: str | int = "0",
    init_if_empty: bool = False,
    force_details: bool = False,
    hydrate_details: bool = True,
) -> dict:
    """Load the directory from local SQLite, initializing it once via InitContact.

    GetFriendAndChatRoomList is intentionally not used here; on remote Hook it can
    crash WeChat. InitContact provides the wxid/gid list, then this Contacts view
    hydrates details with /BatchGetContactBriefInfo in 100-id batches.
    """
    owner_wxid = _contact_owner_wxid()
    account_id = _current_agent_id() or agent_manager.active_id() or ""
    if not _state().get("contacts_loaded"):
        cached_contacts = _cache().get_contacts(owner_wxid=owner_wxid)
        if cached_contacts and _cache().has_contact_init_done_v2(owner_wxid=owner_wxid):
            _state()["contacts_loaded"] = True
            _log(
                f"[CONTACTS] restored completed InitContact state from SQLite: "
                f"owner={owner_wxid} cached={len(cached_contacts)}"
            )
    if _state().get("contacts_loaded"):
        snapshot = _contacts_snapshot_from_db(owner_wxid)
        _state()["contacts"] = snapshot
        ids = (
            list(_cache().get_contacts(owner_wxid=owner_wxid).keys())
            if force_details
            else _contact_detail_missing_ids(owner_wxid)
        )
        if hydrate_details and ids:
            _schedule_contact_detail_hydration(owner_wxid, ids, account_id)
        return _with_contact_hydration_progress(snapshot, owner_wxid)

    lock = _contact_init_lock(owner_wxid)
    async with lock:
        if _state().get("contacts_loaded"):
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            _state()["contacts"] = snapshot
            ids = (
                list(_cache().get_contacts(owner_wxid=owner_wxid).keys())
                if force_details
                else _contact_detail_missing_ids(owner_wxid)
            )
            if hydrate_details and ids:
                _schedule_contact_detail_hydration(owner_wxid, ids, account_id)
            return _with_contact_hydration_progress(snapshot, owner_wxid)

        cached_before = _cache().get_contacts(owner_wxid=owner_wxid)
        try:
            _log("[CONTACTS] first directory open: calling InitContact")
            _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
                "active": True,
                "phase": "InitContact",
                "batch": 0,
                "total_batches": 0,
                "processed": 0,
                "total": 0,
                "updated": 0,
                "failed": 0,
            }
            await _broadcast_contact_hydration_progress(owner_wxid, account_id)
            contacts = await wechat_api.init_contact()
        except Exception as e:
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            _state()["contacts"] = snapshot
            _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
                "active": False,
                "phase": "InitContact",
                "batch": 0,
                "total_batches": 0,
                "processed": 0,
                "total": 0,
                "updated": 0,
                "failed": 1,
            }
            await _broadcast_contact_hydration_progress(owner_wxid, account_id)
            if snapshot.get("friend") or snapshot.get("chatroom"):
                _log(f"[CONTACTS] InitContact failed; served local cache: {type(e).__name__}: {e}")
                return _with_contact_hydration_progress(snapshot, owner_wxid)
            raise

        raw_entries = _all_raw_contact_entries(contacts)
        if not raw_entries:
            snapshot = _contacts_snapshot_from_db(owner_wxid)
            _state()["contacts"] = snapshot
            _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
                "active": False,
                "phase": "InitContact",
                "batch": 0,
                "total_batches": 0,
                "processed": 0,
                "total": 0,
                "updated": 0,
                "failed": 0,
            }
            await _broadcast_contact_hydration_progress(owner_wxid, account_id)
            _log(
                f"[CONTACTS] InitContact returned no parseable contacts; "
                f"served local cache={len(cached_before)}"
            )
            return _with_contact_hydration_progress(snapshot, owner_wxid)

        friend_count, room_count = _cache_raw_contacts(contacts, owner_wxid=owner_wxid)
        _state()["contacts_loaded"] = True
        _cache().mark_contact_init_done_v2(owner_wxid=owner_wxid)
        snapshot = _contacts_snapshot_from_db(owner_wxid)
        _state()["contacts"] = snapshot
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
        missing_ids = _contact_detail_missing_ids(owner_wxid, ids) if hydrate_details else []
        if hydrate_details and missing_ids:
            _log(
                f"[CONTACTS] scheduling avatar hydration after InitContact: "
                f"missing={len(missing_ids)} total={len(ids)}"
            )
            _schedule_contact_detail_hydration(owner_wxid, missing_ids, account_id)
        else:
            _CONTACT_HYDRATION_PROGRESS[owner_wxid] = {
                "active": False,
                "phase": "BatchGetContactBriefInfo" if hydrate_details else "InitContact",
                "batch": 0,
                "total_batches": 0,
                "processed": len(ids) if not hydrate_details else 0,
                "total": len(ids) if not hydrate_details else 0,
                "updated": 0,
                "failed": 0,
            }
            await _broadcast_contact_hydration_progress(owner_wxid, account_id)

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
    """Return contact profiles/summaries from cache, calling Hook detail APIs for misses.

    require_full=True is used by explicit profile opens and refreshes, and will
    fetch detailed data via /GetContact. require_full=False is used by passive
    hydration paths and only fetches when we have no usable avatar.
    """
    wxids = _normalize_wxids(wxids)
    if not wxids:
        return {}

    now = time.time()
    result: dict[str, dict] = {}
    missing: list[str] = []
    owner_wxid = _contact_owner_wxid()
    account_id = _current_agent_id() or agent_manager.active_id() or ""
    db_cached = _cache().get_contacts(wxids, owner_wxid=owner_wxid)

    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = _state().get("avatar_urls", {}) or {}
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
                store_contact = _messages().get_contact(wxid)
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

                if not require_full and avatar:
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
                        _messages().set_contact(wxid, name=db_name, avatar=db_avatar)
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
        updates = await _fetch_and_cache_openim_profiles(
            openim_missing,
            gid=gid,
            broadcast_updates=broadcast_updates,
            owner_wxid=owner_wxid,
            account_id=account_id,
        )
        result.update(updates)

    if regular_missing:
        if require_full:
            updates = await _fetch_and_cache_contact_profiles_via_getcontact(
                regular_missing,
                broadcast_updates=broadcast_updates,
                owner_wxid=owner_wxid,
                account_id=account_id,
            )
        else:
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
    db_cached = _cache().get_contacts(wxids, owner_wxid=owner_wxid)

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

        avatar_urls = _state().get("avatar_urls", {}) or {}
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
        openim_missing = [wxid for wxid in missing if wxid.endswith("@openim")]
        regular_missing = [wxid for wxid in missing if not wxid.endswith("@openim")]

        if openim_missing:
            openim_updates = await _fetch_and_cache_openim_profiles(
                openim_missing,
                broadcast_updates=False,
                owner_wxid=owner_wxid,
            )
            for wxid, entry in openim_updates.items():
                members[wxid] = {
                    "name": str(entry.get("name") or wxid),
                    "avatar": str(entry.get("avatar") or ""),
                }

        batch_size = 100
        for i in range(0, len(regular_missing), batch_size):
            batch = regular_missing[i:i + batch_size]
            wxid_str = ",".join(batch)
            try:
                data = await wechat_api.batch_get_contact_brief_info(wxid_str)
                info_list = _contacts_from_batch_brief_response(data)
                found_in_batch: dict[str, dict] = {}
                for info in info_list:
                    if not isinstance(info, dict):
                        continue
                    wxid = _contact_profile_wxid(info)
                    if not wxid:
                        continue
                    name = _contact_profile_explicit_name(info)
                    avatar = _contact_profile_avatar(info)
                    found_in_batch[wxid] = {"name": name, "avatar": avatar}

                if found_in_batch:
                    contact_updates: dict[str, dict] = {}
                    async with _CONTACT_BRIEF_LOCK:
                        avatar_urls = _state().get("avatar_urls", {}) or {}
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
                        _cache().upsert_contacts(contact_updates, owner_wxid=owner_wxid)
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
        fetch_missing=config.IS_PROTOCOL or req.force,
        force_refresh=req.force,
    )
    return {"members": members}


@app.get("/api/sessions")
async def get_sessions(request: Request):
    """Get current session (conversation) list from cache."""
    agent_id = _request_agent_id(request)
    if config.IS_PROTOCOL:
        if agent_id:
            _activate_runtime(agent_id)
        owner_wxid = _contact_owner_wxid()
        raw_sessions, last_messages = _load_session_cache_into_state(owner_wxid)
        session_list = raw_sessions.get("data", []) if isinstance(raw_sessions, dict) else []
        profile_wxids = [
            str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
            for row in session_list
            if isinstance(row, dict)
        ]
        contact_profiles = _cache().get_contacts(profile_wxids, owner_wxid=owner_wxid) if profile_wxids else {}
        return {
            "sessions": raw_sessions,
            "last_messages": last_messages,
            "contact_profiles": contact_profiles,
            "source": "protocol_sqlite",
        }
    elif agent_id and agent_manager.is_connected(agent_id):
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
        _load_session_cache_into_state(_contact_owner_wxid())
    return _state()["sessions"] or {}


@app.get("/api/sessions/refresh")
async def refresh_sessions(request: Request):
    """Load session list once from native WeChat DB, then serve local SQLite cache."""
    t0 = time.time()
    agent_id = _request_agent_id(request)
    if agent_id and config.IS_PROTOCOL:
        _activate_runtime(agent_id)
    elif agent_id and agent_manager.is_connected(agent_id):
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
    owner_wxid = _contact_owner_wxid()
    if config.IS_PROTOCOL:
        target_agent_id = agent_id or _current_agent_id() or agent_manager.active_id() or ""
        try:
            return await _refresh_protocol_session_list(owner_wxid, target_agent_id)
        except Exception as e:
            _log(f"[REFRESH] protocol statusnotify failed; using local session cache: {type(e).__name__}: {e}")
            raw_sessions, last_messages = _load_session_cache_into_state(owner_wxid)
            session_list = raw_sessions.get("data", []) if isinstance(raw_sessions, dict) else []
            profile_wxids = [
                str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
                for row in session_list
                if isinstance(row, dict)
            ]
            contact_profiles = _cache().get_contacts(profile_wxids, owner_wxid=owner_wxid) if profile_wxids else {}
            return {
                "sessions": raw_sessions,
                "last_messages": last_messages,
                "contact_profiles": contact_profiles,
                "source": "protocol_sqlite_fallback",
                "warning": str(e),
            }
    target_agent_id = agent_id or _current_agent_id() or agent_manager.active_id()
    if target_agent_id:
        _agent_polling_paused_until[target_agent_id] = time.time() + 20.0
    try:
        with wechat_api.use_agent(target_agent_id):
            _log(
                "[REFRESH] querying native Session table via /agent: "
                "db=MicroMsg.db sql=select strUsrName, strNickName, strContent, "
                "nMsgType, nTime, nOrder, nUnReadCount, othersAtMe, nIsSend as isSender "
                "from Session order by nOrder desc"
            )
            db_sessions = await _query_session_list_from_db()
        session_rows = db_sessions.get("data", []) if isinstance(db_sessions, dict) else []
        if session_rows:
            _cache().upsert_sessions(session_rows, owner_wxid=owner_wxid)
            session_wxids = [
                str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
                for row in session_rows
                if isinstance(row, dict)
            ]
            _schedule_session_contact_hydration(owner_wxid, session_wxids, target_agent_id or "")
            _state()["session_list_loaded"] = True
            _log(f"[REFRESH] native Session table cached: {len(session_rows)} rows")
        else:
            _log("[REFRESH] native Session table returned empty; falling back to local cache")
    except Exception as e:
        _log(f"[REFRESH] Query Session table failed; using local cache: {type(e).__name__}: {e}")
    finally:
        if target_agent_id:
            _agent_polling_paused_until[target_agent_id] = time.time() + 1.0

    raw_sessions, last_messages = _load_session_cache_into_state(owner_wxid)
    session_list = raw_sessions.get("data", []) if isinstance(raw_sessions, dict) else []
    profile_wxids = [
        str(_row_value(row, "strUsrName", "StrUsrName", "UserName", "userName", "wxid") or "").strip()
        for row in session_list
        if isinstance(row, dict)
    ]
    profile_wxids = _prioritize_wxids(profile_wxids, owner_wxid, _get_self_wxid())
    contact_profiles = _cache().get_contacts(profile_wxids, owner_wxid=owner_wxid) if profile_wxids else {}

    total_ms = int((time.time() - t0) * 1000)
    _log(f"[REFRESH] ✓ {len(session_list)} sessions from local cache; no per-session MSG query — {total_ms}ms")
    return {"sessions": raw_sessions, "last_messages": last_messages, "contact_profiles": contact_profiles}


# ─── REST API: Messages (History via QueryDB) ─────────────────────

@app.get("/api/messages/{wxid}")
async def get_messages(wxid: str, limit: int = 20, db: str = "MSG0.db"):
    """Get chat history for a contact/group.
    Opening a chat should show the freshest recent rows from the native MSG DB.
    The frontend requests 20 rows for the initial view; older rows are fetched
    only when the user scrolls to the top.
    """
    limit = max(1, min(int(limit or 20), 100))
    owner_wxid = _contact_owner_wxid()
    if config.IS_PROTOCOL:
        if not _protocol_history_available(owner_wxid):
            existing = _messages().get_messages(wxid, limit)
            return {
                "data": existing,
                "source": "protocol_memory" if existing else "protocol_empty",
            }
        cached = _cache().get_messages(wxid, limit, owner_wxid=owner_wxid)
        if cached:
            _messages().add_history_no_flag(wxid, cached)
            return {"data": cached, "source": "protocol_sqlite"}
        existing = _messages().get_messages(wxid, limit)
        return {
            "data": existing,
            "source": "protocol_memory" if existing else "protocol_sqlite",
        }

    normalized: list[dict] = []
    try:
        history = await wechat_api.get_chat_history(wxid, limit, dbs=[db])
        rows = history.get("data", []) if isinstance(history, dict) else []
        normalized = _normalize_history_rows(wxid, rows)
    except Exception as e:
        _log(f"[HISTORY] QueryDB latest failed for {wxid}: {type(e).__name__}: {e}")

    if normalized:
        _messages().add_history(wxid, normalized)
        try:
            _cache().upsert_messages(wxid, normalized, mark_initialized=True, owner_wxid=owner_wxid)
            _sync_session_preview_from_message(wxid, normalized[-1], owner_wxid=owner_wxid)
        except Exception as e:
            _log(f"[SQLITE_CACHE] history write failed for {wxid}: {type(e).__name__}: {e}")
        return {"data": normalized[-limit:], "source": "hook_db"}

    cached = _cache().get_messages(wxid, limit, owner_wxid=owner_wxid)
    if cached:
        _messages().add_history_no_flag(wxid, cached)
        return {"data": cached, "source": "sqlite_fallback"}

    existing = _messages().get_messages(wxid, limit)
    if existing:
        return {"data": existing, "source": "memory_fallback"}

    try:
        _cache().mark_initialized(wxid, owner_wxid=owner_wxid)
    except Exception as e:
        _log(f"[SQLITE_CACHE] history init mark failed for {wxid}: {type(e).__name__}: {e}")
    return {"data": [], "source": "hook_db"}


@app.get("/api/messages/{wxid}/older")
async def get_older_messages(wxid: str, before: int = 0, limit: int = 100, db: str = "MSG0.db"):
    """Load older messages before a given timestamp (for infinite scroll).
    Returns messages with CreateTime < before, sorted chronologically."""
    if before <= 0:
        return {"data": []}
    limit = max(1, min(int(limit or 100), 100))
    owner_wxid = _contact_owner_wxid()
    if config.IS_PROTOCOL:
        if not _protocol_history_available(owner_wxid):
            return {"data": [], "source": "protocol_empty"}
        cached = _cache().get_messages(wxid, limit, before=before, owner_wxid=owner_wxid)
        if cached:
            _messages().add_history_no_flag(wxid, cached)
        return {"data": cached, "source": "protocol_sqlite"}

    cached = _cache().get_messages(wxid, limit, before=before, owner_wxid=owner_wxid)
    # Older caches written before sender extraction was fixed may identify the
    # chatroom itself as the sender.  Do not serve those rows forever: re-query
    # the native DB so the real member wxid/profile can replace them.
    unresolved_group_sender = wxid.endswith("@chatroom") and any(
        str(message.get("sendorrecv", "")) == "2"
        and str(message.get("fromid", "") or "") in ("", wxid)
        for message in cached
        if isinstance(message, dict)
    )
    if cached and not unresolved_group_sender:
        _messages().add_history_no_flag(wxid, cached)
        return {"data": cached, "source": "sqlite"}

    history = await wechat_api.get_chat_history(wxid, limit, before_time=before, dbs=[db])
    rows = history.get("data", []) if isinstance(history, dict) else []
    normalized = _normalize_history_rows(wxid, rows)
    # Add to store so they persist in memory
    _messages().add_history_no_flag(wxid, normalized)
    try:
        _cache().upsert_messages(wxid, normalized, owner_wxid=owner_wxid)
    except Exception as e:
        _log(f"[SQLITE_CACHE] older-history write failed for {wxid}: {type(e).__name__}: {e}")
    return {"data": normalized, "source": "hook_db"}


@app.get("/api/messages/{wxid}/query")
async def query_messages(wxid: str, keyword: str, limit: int = 50, db: str = "MSG0.db"):
    """Search messages by keyword."""
    if config.IS_PROTOCOL:
        owner_wxid = _contact_owner_wxid()
        if not _protocol_history_available(owner_wxid):
            return {"data": [], "source": "protocol_empty"}
        return {
            "data": _cache().search_messages(wxid, keyword, limit, owner_wxid=owner_wxid),
            "source": "protocol_sqlite",
        }
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
    batch_size: int = 100
    batch_interval: float = 5

class RevokeRequest(BaseModel):
    msg_svrid: int
    to_wxid: str

class MessageStructRequest(BaseModel):
    msg_id: str
    chat_id: str = ""
    timestamp: int = 0

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
    # Hook text endpoints explicitly return {"SendTextMsg": "1"} (or the
    # NoSrc equivalent).  A returned "0" means the Hook rejected the send,
    # even though the HTTP request itself completed successfully.
    for marker in ("SendTextMsg", "SendTextMsg_NoSrc", "SendAtMsg"):
        if marker in result:
            return str(result.get(marker) or "").strip().casefold() in {
                "1", "true", "ok", "success",
            }
    if result.get("ok") is False:
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


def _extract_msg_svr_id(result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("MsgSvrID", "msgsvrid", "msgSvrID", "msgSvrId", "msg_id", "msgid", "field3"):
        value = result.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("data", "result", "Data", "Result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            value = _extract_msg_svr_id(nested)
            if value:
                return value
    return ""


def _extract_send_msgsource(result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("msgsource", "msgSource", "MsgSource", "msg_source"):
        value = result.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("data", "result", "upload", "Data", "Result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            value = _extract_send_msgsource(nested)
            if value:
                return value
    return ""


def _broadcast_sender_wxid(agent_id: str = "") -> str:
    agent = agent_manager.get_agent(agent_id) if agent_id else None
    wxid = str((agent or {}).get("wxid") or (agent or {}).get("account_id") or "").strip()
    if wxid and wxid != agent_id and not wxid.startswith("agent:"):
        return wxid
    return str(_get_self_wxid() or "").strip()


def _build_image_relay_msgsource(msg_svr_id: str, sender_wxid: str, source_msgsource: str) -> str:
    uuid_match = re.search(r"<uuid>\s*([^<]+?)\s*</uuid>", str(source_msgsource or ""), flags=re.IGNORECASE)
    uuid = uuid_match.group(1).strip() if uuid_match else ""
    if not msg_svr_id or not sender_wxid or not uuid:
        return ""
    publisher_id = f"msg_{msg_svr_id}|{sender_wxid}|filehelper"
    return (
        "<msgsource>\n"
        "    <tmp_node>\n"
        f"        <publisher-id>{html.escape(publisher_id, quote=False)}</publisher-id>\n"
        "    </tmp_node>\n"
        "    <sec_msg_node>\n"
        "        <alnode>\n"
        "            <fr>4</fr>\n"
        "        </alnode>\n"
        f"        <uuid>{html.escape(uuid, quote=False)}</uuid>\n"
        "    </sec_msg_node>\n"
        "</msgsource>"
    )


async def _bootstrap_broadcast_image_relay(
    picpath: str,
    file_hex: str,
    agent_id: str = "",
) -> dict:
    initial_msgsource = "<msgsource><sec_msg_node><alnode><fr>1</fr></alnode></sec_msg_node></msgsource>"
    try:
        blob = bytes.fromhex(file_hex) if file_hex else b""
    except ValueError:
        blob = b""
    if not blob:
        try:
            with open(picpath, "rb") as source:
                blob = source.read()
            file_hex = blob.hex()
        except Exception as exc:
            return {"error": f"cannot read bootstrap image: {type(exc).__name__}: {exc}"}

    try:
        with wechat_api.use_agent(agent_id):
            result = await wechat_api.send_image_file_no_src(
                "filehelper", picpath, file_hex, msgsource=initial_msgsource,
            )
    except Exception as exc:
        return {"error": f"filehelper bootstrap failed: {type(exc).__name__}: {exc}"}
    if not _send_result_ok(result):
        return {"error": str(result.get("error") or result.get("retmsg") or "filehelper bootstrap failed"), "bootstrap": result}

    msg_svr_id = _extract_msg_svr_id(result)
    source_msgsource = _extract_send_msgsource(result)
    sender_wxid = _broadcast_sender_wxid(agent_id)
    relay_msgsource = _build_image_relay_msgsource(msg_svr_id, sender_wxid, source_msgsource)
    fields = wechat_api.cdn_fields_from_image_send(result, picpath, blob)
    if not fields.get("fileid"):
        return {"error": "filehelper response did not contain upload.fileid", "bootstrap": result}
    if not relay_msgsource:
        missing = []
        if not msg_svr_id:
            missing.append("MsgSvrID")
        if not sender_wxid:
            missing.append("sender wxid")
        if not re.search(r"<uuid>\s*[^<]+\s*</uuid>", source_msgsource, flags=re.IGNORECASE):
            missing.append("msgsource.uuid")
        return {"error": f"filehelper response cannot build msgsource: missing {', '.join(missing)}", "bootstrap": result}
    fields.update({
        "relay_msgsource": relay_msgsource,
        "bootstrap_msgsvrid": msg_svr_id,
        "bootstrap_msgsource": source_msgsource,
        "bootstrap_result": result,
    })
    return fields


def _send_api_parallelism(limit_override: int | None = None) -> int:
    return _hook_api_parallelism(limit_override)


def _broadcast_batch_size(value: int | None) -> int:
    try:
        return max(1, min(int(value or 100), 10000))
    except Exception:
        return 100


def _broadcast_batch_interval(value: float | None) -> float:
    try:
        return max(0.0, min(float(5 if value is None else value), 3600.0))
    except Exception:
        return 5.0


async def _gather_broadcast_batches(items: list, send_one, batch_size: int = 100, batch_interval: float = 5) -> list:
    """Run one account's targets in batches, pausing only between non-empty batches."""
    size = _broadcast_batch_size(batch_size)
    interval = _broadcast_batch_interval(batch_interval)
    results: list = []
    for offset in range(0, len(items), size):
        batch = items[offset:offset + size]
        results.extend(await asyncio.gather(*(send_one(item) for item in batch)))
        if offset + size < len(items) and interval > 0:
            await asyncio.sleep(interval)
    return results


def _track_background_send(task: asyncio.Task, label: str) -> None:
    _background_send_tasks.add(task)
    tasks = _runtime_for(_current_agent_id()).setdefault('tasks', set())
    tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        _background_send_tasks.discard(done)
        tasks.discard(done)
        if done.cancelled():
            return
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
            "account_id": _current_agent_id() or agent_manager.active_id(),
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
            previous_agent = _current_agent_id() or agent_manager.active_id() or ""
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
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    _require_owned_path(req.picpath)
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    _require_owned_path(req.filepath)
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    _require_owned_path(req.videopath)
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    _require_owned_path(req.gifpath)
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
app.mount("/uploads", StaticFiles(directory=_UPLOAD_DIR), name="uploads")


@app.post("/api/broadcast/text")
async def broadcast_text(req: BroadcastTextRequest):
    """Broadcast text using NoSrc by default, or normal SendTextMsg when requested."""
    wxids = [w for w in req.wxids if w]
    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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

    results = await _gather_broadcast_batches(wxids, _send_one, req.batch_size, req.batch_interval) if wxids else []
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent
    return {"total": len(wxids), "sent": sent, "failed": failed, "results": results}


async def _broadcast_image_to_targets(
    target_wxids: list[str],
    cdn: dict,
    image_extra: dict,
    agent_id: str = "",
    concurrency_limit: int = 0,
    batch_size: int = 100,
    batch_interval: float = 5,
) -> list[dict]:
    sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

    async def _send_one(wxid: str) -> dict:
        async with sem:
            try:
                if wxid == "filehelper" and isinstance(cdn.get("bootstrap_result"), dict):
                    return {
                        "wxid": wxid, "ok": True, "result": cdn["bootstrap_result"],
                        "bootstrap": True, "source_msgsvrid": cdn.get("bootstrap_msgsvrid", ""),
                    }
                with wechat_api.use_agent(agent_id):
                    result = await wechat_api.send_image_no_src(wxid, cdn, str(cdn.get("relay_msgsource") or ""))
                ok = _send_result_ok(result)
                if ok:
                    await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", image_extra)
                return {"wxid": wxid, "ok": ok, "result": result, "source_msgsvrid": cdn.get("bootstrap_msgsvrid", "")}
            except Exception as e:
                return {"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

    return await _gather_broadcast_batches(target_wxids, _send_one, batch_size, batch_interval) if target_wxids else []


async def _send_image_file_no_src_to_target(
    wxid: str,
    picpath: str,
    file_hex: str,
    image_extra: dict,
    agent_id: str = "",
) -> dict:
    try:
        with wechat_api.use_agent(agent_id):
            result = await wechat_api.send_image_file_no_src(wxid, picpath, file_hex)
        ok = _send_result_ok(result)
        msg_svr_id = _extract_msg_svr_id(result)
        if ok:
            extra = {**image_extra}
            if msg_svr_id:
                extra["msgsvrid"] = msg_svr_id
            await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", extra)
        row = {"wxid": wxid, "ok": ok, "result": result}
        if msg_svr_id:
            row["msgsvrid"] = msg_svr_id
        if agent_id:
            row["agent_id"] = agent_id
        return row
    except Exception as e:
        row = {"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        if agent_id:
            row["agent_id"] = agent_id
        return row


async def _send_image_file_no_src_to_targets(
    target_wxids: list[str],
    picpath: str,
    file_hex: str,
    image_extra: dict,
    agent_id: str = "",
    concurrency_limit: int = 0,
    batch_size: int = 100,
    batch_interval: float = 5,
) -> list[dict]:
    sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

    async def _send_one(wxid: str) -> dict:
        async with sem:
            return await _send_image_file_no_src_to_target(wxid, picpath, file_hex, image_extra, agent_id)

    return await _gather_broadcast_batches(target_wxids, _send_one, batch_size, batch_interval) if target_wxids else []


async def _send_file_no_src_to_target(
    wxid: str,
    filepath: str,
    file_hex: str,
    file_extra: dict,
    agent_id: str = "",
) -> dict:
    try:
        with wechat_api.use_agent(agent_id):
            result = await wechat_api.send_file_no_src(wxid, filepath, file_hex)
        ok = _send_result_ok(result)
        msg_svr_id = _extract_msg_svr_id(result)
        if ok:
            extra = {**file_extra}
            if msg_svr_id:
                extra["msgsvrid"] = msg_svr_id
            await _broadcast_local_sent_for_agent(agent_id, wxid, "49", str(file_extra.get("file_name") or ""), extra)
        row = {"wxid": wxid, "ok": ok, "result": result}
        if msg_svr_id:
            row["msgsvrid"] = msg_svr_id
        if agent_id:
            row["agent_id"] = agent_id
        return row
    except Exception as e:
        row = {"wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        if agent_id:
            row["agent_id"] = agent_id
        return row


async def _send_file_no_src_to_targets(
    target_wxids: list[str],
    filepath: str,
    file_hex: str,
    file_extra: dict,
    agent_id: str = "",
    concurrency_limit: int = 0,
    batch_size: int = 100,
    batch_interval: float = 5,
) -> list[dict]:
    sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

    async def _send_one(wxid: str) -> dict:
        async with sem:
            return await _send_file_no_src_to_target(wxid, filepath, file_hex, file_extra, agent_id)

    return await _gather_broadcast_batches(target_wxids, _send_one, batch_size, batch_interval) if target_wxids else []


@app.post("/api/broadcast/image-upload")
async def broadcast_image_upload(
    wxids: str = Form(...),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
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
    filepath = os.path.join(_scoped_dir(_UPLOAD_DIR), filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[BROADCAST] Saved image: {filepath} ({len(data)} bytes)")

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
    if str(mode or "").lower() in {"normal", "src", "regular"}:
        db_image_id = _cache().put_media_blob(data, file.content_type or "image/*", file.filename or filename, owner_wxid=_contact_owner_wxid())
        results = await _send_image_file_no_src_to_targets(
            target_wxids,
            filepath,
            data.hex(),
            {"db_image_id": db_image_id, "img_path": filepath},
            agent_id,
            concurrency_limit,
            batch_size,
            batch_interval,
        )
        sent = sum(1 for row in results if row.get("ok"))
        failed = len(results) - sent
        return {
            "total": len(target_wxids),
            "sent": sent,
            "failed": failed,
            "results": results,
            "mode": "normal",
            "strategy": "direct_nosrc",
        }

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

    try:
        with open(send_path, "rb") as relay_file:
            relay_data = relay_file.read()
    except Exception as exc:
        return {
            "total": len(target_wxids), "sent": 0, "failed": len(target_wxids),
            "results": [{"wxid": wxid, "ok": False, "error": f"cannot read relay image: {exc}"} for wxid in target_wxids],
        }
    cdn = await _bootstrap_broadcast_image_relay(send_path, relay_data.hex(), agent_id)
    if cdn.get("error"):
        return {
            "total": len(target_wxids),
            "sent": 0,
            "failed": len(target_wxids),
            "results": [{"wxid": wxid, "ok": False, "error": cdn["error"]} for wxid in target_wxids],
            "cdn": {k: cdn.get(k) for k in _BROADCAST_IMG_CDN_KEYS},
        }

    results = await _broadcast_image_to_targets(
        target_wxids, cdn, {"img_path": filepath}, agent_id, concurrency_limit, batch_size, batch_interval,
    )
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent

    return {
        "total": len(target_wxids),
        "sent": sent,
        "failed": failed,
        "results": results,
        "cdn": {k: cdn.get(k) for k in _BROADCAST_IMG_CDN_KEYS},
    }


@app.post("/api/broadcast/file-upload")
async def broadcast_file_upload(
    wxids: str = Form(...),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
    file: UploadFile = File(...),
):
    try:
        target_wxids = [w for w in json.loads(wxids) if w]
    except Exception:
        target_wxids = [w.strip() for w in wxids.split(",") if w.strip()]
    if not target_wxids:
        return {"total": 0, "sent": 0, "failed": 0, "results": [], "error": "no targets"}

    safe_name = (file.filename or "file").replace("\\", "_").replace("/", "_")
    upload_dir = os.path.join(_scoped_dir(_UPLOAD_DIR), f"broadcast_file_{int(time.time() * 1000)}")
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, safe_name)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[BROADCAST] Saved file: {filepath} ({len(data)} bytes)")

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
    results = await _send_file_no_src_to_targets(
        target_wxids,
        filepath,
        data.hex(),
        {"file_path": filepath, "file_name": safe_name, "file_size": len(data)},
        agent_id,
        concurrency_limit,
        batch_size,
        batch_interval,
    )
    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent
    return {
        "total": len(target_wxids),
        "sent": sent,
        "failed": failed,
        "results": results,
        "mode": "normal",
        "strategy": "direct_nosrc",
    }


def _mixed_content_order(order: str, has_text: bool, attachment_parts: list[dict]) -> list[dict]:
    """Build one recipient's sequence. Text is first unless explicitly configured otherwise."""
    text_part = [{"type": "text"}] if has_text else []
    if str(order or "").strip().lower() in {"attachment_first", "media_first", "image_first", "file_first"}:
        return [*attachment_parts, *text_part]
    return [*text_part, *attachment_parts]


async def _store_mixed_uploads(images: list[UploadFile], attachment: UploadFile | None) -> tuple[list[dict], dict | None]:
    stamp = int(time.time() * 1000)
    image_parts: list[dict] = []
    for index, upload in enumerate(images):
        data = await upload.read()
        original_name = os.path.basename(upload.filename or f"image_{index + 1}.png") or f"image_{index + 1}.png"
        ext = os.path.splitext(original_name)[1] or ".png"
        filepath = os.path.join(_scoped_dir(_UPLOAD_DIR), f"mixed_{stamp}_{index + 1}{ext}")
        with open(filepath, "wb") as output:
            output.write(data)
        image_parts.append({
            "type": "image",
            "path": filepath,
            "name": original_name,
            "data": data,
            "hex": data.hex(),
            "mime": upload.content_type or "image/*",
        })

    file_part: dict | None = None
    if attachment is not None and attachment.filename:
        data = await attachment.read()
        safe_name = os.path.basename(attachment.filename).replace("\\", "_").replace("/", "_") or "file"
        upload_dir = os.path.join(_scoped_dir(_UPLOAD_DIR), f"mixed_file_{stamp}")
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, safe_name)
        with open(filepath, "wb") as output:
            output.write(data)
        file_part = {
            "type": "file",
            "path": filepath,
            "name": safe_name,
            "data": data,
            "hex": data.hex(),
            "size": len(data),
        }
    return image_parts, file_part


async def _prepare_mixed_parts_for_agent(
    agent_id: str,
    first_wxid: str,
    image_parts: list[dict],
    file_part: dict | None,
    normal_mode: bool,
) -> list[dict]:
    prepared: list[dict] = []
    for image_part in image_parts:
        db_image_id = _cache().put_media_blob(image_part["data"], image_part["mime"], image_part["name"], owner_wxid=_contact_owner_wxid())
        item = {**image_part, "db_image_id": db_image_id}
        if not normal_mode:
            try:
                cdn = await _bootstrap_broadcast_image_relay(item["path"], item["hex"], agent_id)
                if cdn.get("error"):
                    item["prepare_error"] = str(cdn["error"])
                else:
                    item["cdn"] = {key: cdn[key] for key in _BROADCAST_IMG_CDN_KEYS if key in cdn}
                    item["cdn"]["relay_msgsource"] = cdn["relay_msgsource"]
                    item["bootstrap_msgsvrid"] = cdn["bootstrap_msgsvrid"]
                    item["bootstrap_result"] = cdn["bootstrap_result"]
            except Exception as exc:
                item["prepare_error"] = f"{type(exc).__name__}: {exc}"
        prepared.append(item)
    if file_part:
        prepared.append(file_part)
    return prepared


async def _send_mixed_to_target(
    agent_id: str,
    wxid: str,
    message: str,
    parts: list[dict],
    normal_mode: bool,
) -> dict:
    part_results: list[dict] = []
    try:
        for part in parts:
            part_type = part["type"]
            if part_type == "text":
                with wechat_api.use_agent(agent_id):
                    result = await (wechat_api.send_text(wxid, message) if normal_mode else wechat_api.send_text_no_src(wxid, message))
                ok = _send_result_ok(result)
                if ok:
                    await _broadcast_local_sent_for_agent(agent_id, wxid, "1", message)
            elif part_type == "image":
                if part.get("prepare_error"):
                    raise RuntimeError(part["prepare_error"])
                if not normal_mode and wxid == "filehelper" and isinstance(part.get("bootstrap_result"), dict):
                    result = part["bootstrap_result"]
                    ok = True
                elif normal_mode:
                    row = await _send_image_file_no_src_to_target(
                        wxid, part["path"], part["hex"], {"db_image_id": part["db_image_id"]}, agent_id,
                    )
                    ok, result = bool(row.get("ok")), row.get("result", row.get("error"))
                else:
                    with wechat_api.use_agent(agent_id):
                        result = await wechat_api.send_image_no_src(
                            wxid, part["cdn"], str(part["cdn"].get("relay_msgsource") or ""),
                        )
                    ok = _send_result_ok(result)
                    if ok:
                        await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", {"db_image_id": part["db_image_id"]})
            else:
                row = await _send_file_no_src_to_target(
                    wxid,
                    part["path"],
                    part["hex"],
                    {"file_path": part["path"], "file_name": part["name"], "file_size": part["size"]},
                    agent_id,
                )
                ok, result = bool(row.get("ok")), row.get("result", row.get("error"))

            part_results.append({"type": part_type, "ok": ok, "result": result})
            if not ok:
                return {"agent_id": agent_id, "wxid": wxid, "ok": False, "parts": part_results}
        return {"agent_id": agent_id, "wxid": wxid, "ok": True, "parts": part_results}
    except Exception as exc:
        return {
            "agent_id": agent_id,
            "wxid": wxid,
            "ok": False,
            "parts": part_results,
            "error": f"{type(exc).__name__}: {exc}",
        }


@app.post("/api/broadcast/mixed-upload")
async def broadcast_mixed_upload(
    wxids: str = Form(...),
    msg: str = Form(""),
    order: str = Form("text_first"),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
    images: list[UploadFile] = File(default=[]),
    attachment: UploadFile | None = File(default=None),
):
    """Send every recipient's complete mixed payload before moving that worker to another recipient."""
    try:
        target_wxids = _dedupe_targets(json.loads(wxids))
    except Exception:
        target_wxids = _dedupe_targets(str(wxids or "").split(","))
    message = str(msg or "").strip()
    image_parts, file_part = await _store_mixed_uploads(images, attachment)
    if not target_wxids or (not message and not image_parts and not file_part):
        return {"total": len(target_wxids), "sent": 0, "failed": 0, "results": [], "error": "no targets or content"}

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
    normal_mode = str(mode or "").lower() in {"normal", "src", "regular"}
    attachments = await _prepare_mixed_parts_for_agent(agent_id, target_wxids[0], image_parts, file_part, normal_mode)
    parts = _mixed_content_order(order, bool(message), attachments)
    sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

    async def send_one(wxid: str) -> dict:
        async with sem:
            return await _send_mixed_to_target(agent_id, wxid, message, parts, normal_mode)

    results = await _gather_broadcast_batches(target_wxids, send_one, batch_size, batch_interval)
    sent = sum(1 for row in results if row.get("ok"))
    return {"total": len(results), "sent": sent, "failed": len(results) - sent, "results": results, "order": order, "mode": mode}


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
        "所有个人": "friends",
        "所有好友": "friends",
        "group": "groups",
        "groups": "groups",
        "chatroom": "groups",
        "chatrooms": "groups",
        "群": "groups",
        "群聊": "groups",
        "所有群": "groups",
        "所有群聊": "groups",
        "official": "official",
        "officials": "official",
        "official_account": "official",
        "official_accounts": "official",
        "public": "official",
        "public_account": "official",
        "public_accounts": "official",
        "公众号": "official",
        "所有公众号": "official",
        "service": "service",
        "services": "service",
        "service_account": "service",
        "service_accounts": "service",
        "服务号": "service",
        "所有服务号": "service",
        "openim": "openim",
        "wecom": "openim",
        "wework": "openim",
        "enterprise": "openim",
        "企微": "openim",
        "企业微信": "openim",
        "所有企微": "openim",
        "所有企业微信": "openim",
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
    target_category = {
        "friend": "personal",
        "friends": "personal",
        "personal": "personal",
        "chatroom": "groups",
        "chatrooms": "groups",
        "group": "groups",
        "groups": "groups",
        "official": "official",
        "service": "service",
        "openim": "openim",
    }.get(str(key or "").strip().lower(), str(key or "").strip().lower())
    if isinstance(raw_contacts, list):
        value = [entry for entry in raw_contacts if isinstance(entry, dict)]
    elif isinstance(raw_contacts, dict):
        value = [
            *_extract_contact_list(raw_contacts, "friend", "friends", "contact", "contacts"),
            *_extract_contact_list(
                raw_contacts,
                "chatroom", "chatrooms", "chat_room", "chat_rooms",
                "group", "groups", "group_chat", "group_chats",
            ),
            *_extract_contact_list(raw_contacts, "data"),
            *_extract_batch_contact_entries(raw_contacts),
        ]
    else:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for entry in value:
        wxid = _contact_wxid(entry)
        if not wxid or wxid in seen:
            continue
        if _contact_directory_category(entry, wxid) != target_category:
            continue
        seen.add(wxid)
        out.append(entry)
    return out


def _contact_wxid(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(
        entry.get("wxid")
        or entry.get("id")
        or entry.get("UserName")
        or entry.get("userName")
        or entry.get("strUsrName")
        or entry.get("username")
        or entry.get("gid")
        or entry.get("chatroomid")
        or entry.get("chatroom_id")
        or entry.get("describe")
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
        "friends": len(_raw_contact_list(raw_contacts, "friends")),
        "groups": len(_raw_contact_list(raw_contacts, "groups")),
        "official": len(_raw_contact_list(raw_contacts, "official")),
        "service": len(_raw_contact_list(raw_contacts, "service")),
        "openim": len(_raw_contact_list(raw_contacts, "openim")),
    }


def _empty_contact_counts() -> dict[str, int]:
    return {"friends": 0, "groups": 0, "official": 0, "service": 0, "openim": 0}


def _resolve_targets_from_contacts(contacts: dict | list, target_types: set[str]) -> list[str]:
    targets: list[str] = []
    for target_type in ("friends", "groups", "official", "service", "openim"):
        if target_type not in target_types:
            continue
        for entry in _raw_contact_list(contacts, target_type):
            wxid = _contact_wxid(entry)
            if wxid:
                targets.append(wxid)
    return _dedupe_targets(targets)


async def _ensure_broadcast_contact_categories(contacts: dict | list, owner_wxid: str, account_id: str) -> dict:
    gh_wxids: list[str] = []
    seen: set[str] = set()
    for entry in _all_raw_contact_entries(contacts):
        wxid = _contact_profile_wxid(entry)
        if not wxid or not wxid.startswith("gh_") or wxid in seen:
            continue
        seen.add(wxid)
        gh_wxids.append(wxid)
    missing = _contact_account_category_missing_ids(owner_wxid, gh_wxids)
    if missing:
        _log(f"[BROADCAST] hydrating official/service category fields: owner={owner_wxid} ids={len(missing)}")
        with wechat_api.use_agent(account_id):
            await _fetch_and_cache_contact_details(
                missing,
                broadcast_updates=False,
                broadcast_progress=False,
                owner_wxid=owner_wxid,
                account_id=account_id,
            )
        contacts = _contacts_snapshot_from_db(owner_wxid)
        _state()["contacts"] = contacts
    return contacts


async def _ensure_account_contacts(agent_id: str):
    if config.IS_PROTOCOL:
        async with _ACCOUNT_LOCK:
            _activate_runtime(agent_id)
            login_ok = await _run_protocol_backend_initialization(agent_id)
            if not login_ok:
                raise RuntimeError("wechat not logged in")
            owner_wxid = _contact_owner_wxid()
            if _state().get("contacts_loaded"):
                cached_contacts = _contacts_snapshot_from_db(owner_wxid)
                _state()["contacts"] = cached_contacts
                cached_contacts = await _ensure_broadcast_contact_categories(cached_contacts, owner_wxid, agent_id)
                return cached_contacts
            with wechat_api.use_agent(agent_id):
                await _refresh_contacts_incremental(list_type="0", init_if_empty=True, hydrate_details=False)
            contacts = _contacts_snapshot_from_db(owner_wxid)
            _state()["contacts"] = contacts
            contacts = await _ensure_broadcast_contact_categories(contacts, owner_wxid, agent_id)
            return contacts

    async with _ACCOUNT_LOCK:
        await agent_manager.set_active(agent_id)
        _activate_runtime(agent_id)
        with wechat_api.use_agent(agent_id):
            login_status = await _refresh_agent_login_status(agent_id)
            if str(login_status.get("status") or "") != "3":
                raise RuntimeError(f"wechat not logged in: {login_status.get('message') or login_status.get('status') or 'unknown'}")
            if not _state().get("initialized"):
                await _run_backend_initialization(agent_id)
        owner_wxid = _contact_owner_wxid()
        if _state().get("contacts_loaded"):
            cached_contacts = _contacts_snapshot_from_db(owner_wxid)
            _state()["contacts"] = cached_contacts
            cached_contacts = await _ensure_broadcast_contact_categories(cached_contacts, owner_wxid, agent_id)
            counts = _contact_counts(cached_contacts)
            _log(
                f"[BROADCAST] local contacts for {owner_wxid}: "
                f"friends={counts['friends']} groups={counts['groups']} "
                f"official={counts['official']} service={counts['service']} openim={counts['openim']}"
            )
            return cached_contacts
        with wechat_api.use_agent(agent_id):
            await _refresh_contacts_incremental(list_type="0", init_if_empty=True, hydrate_details=False)
        contacts = _contacts_snapshot_from_db(owner_wxid)
        _state()["contacts"] = contacts
        contacts = await _ensure_broadcast_contact_categories(contacts, owner_wxid, agent_id)
        counts = _contact_counts(contacts)
        _log(
            f"[BROADCAST] initialized contacts for {owner_wxid}: "
            f"friends={counts['friends']} groups={counts['groups']} "
            f"official={counts['official']} service={counts['service']} openim={counts['openim']}"
        )
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
    for account_id in agent_ids or []:
        access_store.require_account(account_id)
    if config.IS_PROTOCOL:
        accounts_payload = await _list_protocol_accounts()
        online_ids = [
            str(a.get("id") or "")
            for a in accounts_payload.get("accounts", [])
            if str(a.get("id") or "") and str(a.get("login_status") or "") == "3"
        ]
        requested = [str(a or "").strip() for a in (agent_ids or []) if str(a or "").strip()]
        connected_agents = [a for a in requested if a in online_ids] if requested else online_ids
    else:
        connected_agents = [a for a in (agent_ids or []) if agent_manager.is_connected(a)]
        if not connected_agents:
            connected_agents = [a["id"] for a in _owned_agents() if a.get("id")]

    total = 0
    account_targets: dict[str, int] = {}
    account_counts: dict[str, dict[str, int]] = {}
    work_items: list[tuple[str, str]] = []
    skipped_results: list[dict] = []

    for agent_id in connected_agents:
        try:
            if direct_targets:
                if config.IS_PROTOCOL:
                    profile_raw = await _protocol_profile_for_session(agent_id)
                    if not _protocol_profile_logged_in(profile_raw):
                        raise RuntimeError(str(profile_raw.get("error") or profile_raw.get("state") or "wechat not logged in"))
                else:
                    with wechat_api.use_agent(agent_id):
                        login_status = await _refresh_agent_login_status(agent_id)
                    if str(login_status.get("status") or "") != "3":
                        raise RuntimeError(f"wechat not logged in: {login_status.get('message') or login_status.get('status') or 'unknown'}")
                target_wxids = _dedupe_targets(direct_targets)
                account_counts[agent_id] = {**_empty_contact_counts(), "targets": len(target_wxids)}
            else:
                contacts = await _ensure_account_contacts(agent_id)
                counts = _contact_counts(contacts)
                target_wxids = _resolve_targets_from_contacts(contacts, target_types)
                account_counts[agent_id] = {**counts, "targets": len(target_wxids)}
        except Exception as e:
            target_wxids = []
            account_counts.setdefault(agent_id, {**_empty_contact_counts(), "targets": 0})
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

    work_by_agent: dict[str, list[str]] = {}
    for agent_id, wxid in work_items:
        work_by_agent.setdefault(agent_id, []).append(wxid)

    async def _send_agent(agent_id: str, targets: list[str]) -> list[dict]:
        return await _gather_broadcast_batches(
            targets,
            lambda wxid: _send_one(agent_id, wxid),
            req.batch_size,
            req.batch_interval,
        )

    account_results = await asyncio.gather(*(
        _send_agent(agent_id, targets) for agent_id, targets in work_by_agent.items()
    )) if work_by_agent else []
    results = skipped_results + [row for rows in account_results for row in rows]
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


@app.post("/api/accounts/broadcast/mixed-upload")
async def multi_account_broadcast_mixed_upload(
    wxids: str = Form("[]"),
    agent_ids: str = Form("[]"),
    target_types: str = Form("[]"),
    msg: str = Form(""),
    order: str = Form("text_first"),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
    images: list[UploadFile] = File(default=[]),
    attachment: UploadFile | None = File(default=None),
):
    try:
        direct_targets = _dedupe_targets(json.loads(wxids or "[]"))
    except Exception:
        direct_targets = _dedupe_targets(str(wxids or "").split(","))
    try:
        requested_agents = [value for value in json.loads(agent_ids or "[]") if value]
    except Exception:
        requested_agents = [value.strip() for value in str(agent_ids or "").split(",") if value.strip()]
    try:
        parsed_target_types = _normalize_broadcast_target_types(json.loads(target_types or "[]"))
    except Exception:
        parsed_target_types = _normalize_broadcast_target_types(str(target_types or "").split(","))

    selected_agents, work_items, account_targets, account_counts, skipped_results = await _prepare_multi_account_targets(
        requested_agents, direct_targets, parsed_target_types,
    )
    message = str(msg or "").strip()
    image_parts, file_part = await _store_mixed_uploads(images, attachment)
    if not work_items or (not message and not image_parts and not file_part):
        return {
            "accounts": len(selected_agents), "targets": len(work_items), "total": len(work_items),
            "sent": 0, "failed": len(skipped_results), "results": skipped_results,
            "account_targets": account_targets, "account_counts": account_counts,
            "error": "no targets or content",
        }

    normal_mode = str(mode or "").lower() in {"normal", "src", "regular"}
    work_by_agent: dict[str, list[str]] = {}
    for agent_id, wxid in work_items:
        work_by_agent.setdefault(agent_id, []).append(wxid)

    results = list(skipped_results)
    for agent_id in selected_agents:
        targets = work_by_agent.get(agent_id, [])
        if not targets:
            continue
        async with _ACCOUNT_LOCK:
            if not config.IS_PROTOCOL:
                await agent_manager.set_active(agent_id)
            _activate_runtime(agent_id)
        attachments = await _prepare_mixed_parts_for_agent(agent_id, targets[0], image_parts, file_part, normal_mode)
        parts = _mixed_content_order(order, bool(message), attachments)
        sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

        async def send_one(wxid: str) -> dict:
            async with sem:
                return await _send_mixed_to_target(agent_id, wxid, message, parts, normal_mode)

        results.extend(await _gather_broadcast_batches(targets, send_one, batch_size, batch_interval))

    sent = sum(1 for row in results if row.get("ok"))
    failed = len(results) - sent
    completed_counts: dict[str, dict] = {key: dict(value) for key, value in account_counts.items()}
    for row in results:
        row_agent = str(row.get("agent_id") or "")
        if not row_agent:
            continue
        counts = completed_counts.setdefault(row_agent, {**_empty_contact_counts(), "targets": account_targets.get(row_agent, 0)})
        bucket = "sent" if row.get("ok") else "failed"
        counts[bucket] = int(counts.get(bucket) or 0) + 1
    return {
        "accounts": len(selected_agents),
        "targets": len(work_items),
        "account_targets": account_targets,
        "account_counts": completed_counts,
        "total": len(work_items),
        "sent": sent,
        "failed": failed,
        "results": results,
        "order": order,
        "mode": mode,
    }


@app.post("/api/accounts/broadcast/image-upload")
async def multi_account_broadcast_image_upload(
    wxids: str = Form("[]"),
    agent_ids: str = Form("[]"),
    target_types: str = Form("[]"),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
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
            if not config.IS_PROTOCOL:
                await agent_manager.set_active(agent_id)
            _activate_runtime(agent_id)
            db_image_id = _cache().put_media_blob(upload_bytes, upload_mime, upload_name, owner_wxid=_contact_owner_wxid())
        if normal_mode:
            send_results = await _send_image_file_no_src_to_targets(
                agent_targets,
                upload_name,
                file_hex,
                {"db_image_id": db_image_id},
                agent_id,
                concurrency_limit,
                batch_size,
                batch_interval,
            )
            sent += sum(1 for row in send_results if row.get("ok"))
            failed += sum(1 for row in send_results if not row.get("ok"))
            results.extend(send_results)
            continue
        try:
            cdn = await _bootstrap_broadcast_image_relay(upload_name, file_hex, agent_id)
        except Exception as e:
            cdn = {"error": f"{type(e).__name__}: {e}"}
        with wechat_api.use_agent(agent_id):
            if cdn.get("error"):
                failed += len(agent_targets)
                for wxid in agent_targets:
                    results.append({"agent_id": agent_id, "wxid": wxid, "ok": False, "error": cdn["error"]})
                continue
            sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

            async def _send_one(wxid: str) -> dict:
                async with sem:
                    try:
                        if wxid == "filehelper" and isinstance(cdn.get("bootstrap_result"), dict):
                            return {
                                "agent_id": agent_id, "wxid": wxid, "ok": True,
                                "result": cdn["bootstrap_result"], "bootstrap": True,
                                "source_msgsvrid": cdn.get("bootstrap_msgsvrid", ""),
                            }
                        with wechat_api.use_agent(agent_id):
                            result = await wechat_api.send_image_no_src(
                                wxid, cdn, str(cdn.get("relay_msgsource") or ""),
                            )
                        ok = _send_result_ok(result)
                        if ok:
                            await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", {"db_image_id": db_image_id})
                        return {
                            "agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result,
                            "source_msgsvrid": cdn.get("bootstrap_msgsvrid", ""),
                        }
                    except Exception as e:
                        return {"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

            send_results = await _gather_broadcast_batches(agent_targets, _send_one, batch_size, batch_interval)
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


@app.post("/api/accounts/broadcast/image-upload-stream")
async def multi_account_broadcast_image_upload_stream(
    wxids: str = Form("[]"),
    agent_ids: str = Form("[]"),
    target_types: str = Form("[]"),
    mode: str = Form("nosrc"),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
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
    total_targets = len(work_items)

    if not target_wxids and not parsed_target_types:
        async def _empty_events():
            yield json.dumps({
                "type": "done",
                "accounts": len(selected_agents),
                "targets": 0,
                "account_targets": account_targets,
                "account_counts": account_counts,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "results": [],
                "error": "no targets",
            }, ensure_ascii=False) + "\n"

        return StreamingResponse(_empty_events(), media_type="application/x-ndjson")

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

    async def _events():
        progress_counts: dict[str, dict] = {agent_id: dict(counts) for agent_id, counts in account_counts.items()}
        for agent_id in selected_agents:
            item = progress_counts.setdefault(agent_id, {**_empty_contact_counts(), "targets": account_targets.get(agent_id, 0)})
            item.setdefault("targets", account_targets.get(agent_id, 0))
            item["sent"] = 0
            item["failed"] = 0

        sent = 0
        failed = 0
        results: list[dict] = []

        def line(payload: dict) -> str:
            return json.dumps(payload, ensure_ascii=False) + "\n"

        def snapshot(event_type: str, row: dict | None = None, **extra) -> dict:
            payload = {
                "type": event_type,
                "accounts": len(selected_agents),
                "targets": total_targets,
                "account_targets": account_targets,
                "account_counts": progress_counts,
                "total": total_targets,
                "sent": sent,
                "failed": failed,
            }
            if row is not None:
                payload["row"] = row
            payload.update(extra)
            return payload

        def record_result(row: dict) -> None:
            nonlocal sent, failed
            results.append(row)
            ok = bool(row.get("ok"))
            if ok:
                sent += 1
            else:
                failed += 1
            agent_id = str(row.get("agent_id") or "")
            if agent_id:
                current = progress_counts.setdefault(agent_id, {**_empty_contact_counts(), "targets": account_targets.get(agent_id, 0)})
                current["sent" if ok else "failed"] = int(current.get("sent" if ok else "failed") or 0) + 1

        yield line(snapshot("plan"))

        for row in skipped_results:
            record_result(row)
            yield line(snapshot("progress", row=row))

        for agent_id in selected_agents:
            agent_targets = work_by_agent.get(agent_id, [])
            if not agent_targets:
                continue
            async with _ACCOUNT_LOCK:
                if not config.IS_PROTOCOL:
                    await agent_manager.set_active(agent_id)
                _activate_runtime(agent_id)
                db_image_id = _cache().put_media_blob(upload_bytes, upload_mime, upload_name, owner_wxid=_contact_owner_wxid())
            if normal_mode:
                sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

                async def _send_normal(wxid: str) -> dict:
                    async with sem:
                        return await _send_image_file_no_src_to_target(wxid, upload_name, file_hex, {"db_image_id": db_image_id}, agent_id)

                size = _broadcast_batch_size(batch_size)
                interval = _broadcast_batch_interval(batch_interval)
                for offset in range(0, len(agent_targets), size):
                    batch = agent_targets[offset:offset + size]
                    tasks = [asyncio.create_task(_send_normal(wxid)) for wxid in batch]
                    for task in asyncio.as_completed(tasks):
                        row = await task
                        record_result(row)
                        yield line(snapshot("progress", row=row))
                    if offset + size < len(agent_targets) and interval > 0:
                        await asyncio.sleep(interval)
                continue

            try:
                cdn = await _bootstrap_broadcast_image_relay(upload_name, file_hex, agent_id)
            except Exception as e:
                cdn = {"error": f"{type(e).__name__}: {e}"}
            with wechat_api.use_agent(agent_id):
                if cdn.get("error"):
                    for wxid in agent_targets:
                        row = {"agent_id": agent_id, "wxid": wxid, "ok": False, "error": cdn["error"]}
                        record_result(row)
                        yield line(snapshot("progress", row=row))
                    continue
                relay_msgsource = str(cdn.get("relay_msgsource") or "")
                bootstrap_result = cdn.get("bootstrap_result")
                bootstrap_msgsvrid = str(cdn.get("bootstrap_msgsvrid") or "")
                cdn = {k: cdn[k] for k in _BROADCAST_IMG_CDN_KEYS if k in cdn}
                cdn["relay_msgsource"] = relay_msgsource
                cdn["toWxid"] = agent_targets[0]
                sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

                async def _send_one(wxid: str) -> dict:
                    async with sem:
                        try:
                            if wxid == "filehelper" and isinstance(bootstrap_result, dict):
                                return {
                                    "agent_id": agent_id, "wxid": wxid, "ok": True,
                                    "result": bootstrap_result, "bootstrap": True,
                                    "source_msgsvrid": bootstrap_msgsvrid,
                                }
                            with wechat_api.use_agent(agent_id):
                                result = await wechat_api.send_image_no_src(wxid, cdn, relay_msgsource)
                            ok = _send_result_ok(result)
                            if ok:
                                await _broadcast_local_sent_for_agent(agent_id, wxid, "3", "", {"db_image_id": db_image_id})
                            return {
                                "agent_id": agent_id, "wxid": wxid, "ok": ok, "result": result,
                                "source_msgsvrid": bootstrap_msgsvrid,
                            }
                        except Exception as e:
                            return {"agent_id": agent_id, "wxid": wxid, "ok": False, "error": f"{type(e).__name__}: {e}"}

                size = _broadcast_batch_size(batch_size)
                interval = _broadcast_batch_interval(batch_interval)
                for offset in range(0, len(agent_targets), size):
                    batch = agent_targets[offset:offset + size]
                    tasks = [asyncio.create_task(_send_one(wxid)) for wxid in batch]
                    for task in asyncio.as_completed(tasks):
                        row = await task
                        record_result(row)
                        yield line(snapshot("progress", row=row))
                    if offset + size < len(agent_targets) and interval > 0:
                        await asyncio.sleep(interval)

        yield line(snapshot("done", results=results))

    return StreamingResponse(_events(), media_type="application/x-ndjson")


@app.post("/api/accounts/broadcast/file-upload-stream")
async def multi_account_broadcast_file_upload_stream(
    wxids: str = Form("[]"),
    agent_ids: str = Form("[]"),
    target_types: str = Form("[]"),
    concurrency_limit: int = Form(0),
    batch_size: int = Form(100),
    batch_interval: float = Form(5),
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
    total_targets = len(work_items)

    if not target_wxids and not parsed_target_types:
        async def _empty_events():
            yield json.dumps({
                "type": "done",
                "accounts": len(selected_agents),
                "targets": 0,
                "account_targets": account_targets,
                "account_counts": account_counts,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "results": [],
                "error": "no targets",
            }, ensure_ascii=False) + "\n"

        return StreamingResponse(_empty_events(), media_type="application/x-ndjson")

    work_by_agent: dict[str, list[str]] = {}
    for agent_id, wxid in work_items:
        work_by_agent.setdefault(agent_id, []).append(wxid)

    data = await file.read()
    original_name = (file.filename or "file").replace("\\", "/").split("/")[-1] or "file"
    safe_name = original_name.replace("\\", "_").replace("/", "_")
    upload_dir = os.path.join(_scoped_dir(_UPLOAD_DIR), f"multi_broadcast_file_{int(time.time() * 1000)}")
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, safe_name)
    with open(filepath, "wb") as f:
        f.write(data)
    file_hex = data.hex()
    _log(f"[MULTI_BROADCAST] Saved file: {filepath} ({len(data)} bytes)")

    async def _events():
        progress_counts: dict[str, dict] = {agent_id: dict(counts) for agent_id, counts in account_counts.items()}
        for agent_id in selected_agents:
            item = progress_counts.setdefault(agent_id, {**_empty_contact_counts(), "targets": account_targets.get(agent_id, 0)})
            item.setdefault("targets", account_targets.get(agent_id, 0))
            item["sent"] = 0
            item["failed"] = 0

        sent = 0
        failed = 0
        results: list[dict] = []

        def line(payload: dict) -> str:
            return json.dumps(payload, ensure_ascii=False) + "\n"

        def snapshot(event_type: str, row: dict | None = None, **extra) -> dict:
            payload = {
                "type": event_type,
                "accounts": len(selected_agents),
                "targets": total_targets,
                "account_targets": account_targets,
                "account_counts": progress_counts,
                "total": total_targets,
                "sent": sent,
                "failed": failed,
            }
            if row is not None:
                payload["row"] = row
            payload.update(extra)
            return payload

        def record_result(row: dict) -> None:
            nonlocal sent, failed
            results.append(row)
            ok = bool(row.get("ok"))
            if ok:
                sent += 1
            else:
                failed += 1
            row_agent_id = str(row.get("agent_id") or "")
            if row_agent_id:
                current = progress_counts.setdefault(row_agent_id, {**_empty_contact_counts(), "targets": account_targets.get(row_agent_id, 0)})
                current["sent" if ok else "failed"] = int(current.get("sent" if ok else "failed") or 0) + 1

        yield line(snapshot("plan", file_name=safe_name, file_size=len(data)))

        for row in skipped_results:
            record_result(row)
            yield line(snapshot("progress", row=row))

        file_extra = {"file_path": filepath, "file_name": safe_name, "file_size": len(data)}
        for agent_id in selected_agents:
            agent_targets = work_by_agent.get(agent_id, [])
            if not agent_targets:
                continue
            async with _ACCOUNT_LOCK:
                if not config.IS_PROTOCOL:
                    await agent_manager.set_active(agent_id)
                _activate_runtime(agent_id)

            sem = asyncio.Semaphore(_send_api_parallelism(concurrency_limit))

            async def _send_normal(wxid: str) -> dict:
                async with sem:
                    return await _send_file_no_src_to_target(wxid, filepath, file_hex, file_extra, agent_id)

            size = _broadcast_batch_size(batch_size)
            interval = _broadcast_batch_interval(batch_interval)
            for offset in range(0, len(agent_targets), size):
                batch = agent_targets[offset:offset + size]
                tasks = [asyncio.create_task(_send_normal(wxid)) for wxid in batch]
                for task in asyncio.as_completed(tasks):
                    row = await task
                    record_result(row)
                    yield line(snapshot("progress", row=row))
                if offset + size < len(agent_targets) and interval > 0:
                    await asyncio.sleep(interval)

        yield line(snapshot("done", results=results, file_name=safe_name, file_size=len(data)))

    return StreamingResponse(_events(), media_type="application/x-ndjson")


@app.post("/api/send/image-upload")
async def send_image_upload(wxid: str = Form(...), file: UploadFile = File(...)):
    """Upload an image from the browser and send it via WeChat."""
    ext = os.path.splitext(file.filename or "img.png")[1] or ".png"
    filename = f"img_{int(time.time())}_{wxid}{ext}"
    filepath = os.path.join(_scoped_dir(_UPLOAD_DIR), filename)
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

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    filepath = os.path.join(_scoped_dir(_UPLOAD_DIR), filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved file: {filepath} ({len(data)} bytes)")

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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
    filepath = os.path.join(_scoped_dir(_UPLOAD_DIR), filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved video: {filepath} ({len(data)} bytes)")

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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


def _convert_recording_to_silk(input_path: str, output_path: str) -> None:
    pcm_path = output_path + ".pcm"
    converted = _run_media_command([
        _ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
        "-i", input_path, "-vn", "-ac", "1", "-ar", "24000",
        "-f", "s16le", "-y", pcm_path,
    ], timeout=120)
    if converted.returncode != 0 or not _nonempty_file(pcm_path):
        error = converted.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Recording conversion failed: {error[:300]}")
    encoded: subprocess.CompletedProcess | None = None
    try:
        encoder = _silk_codec_executable("encoder")
        encoded = _run_media_command([
            encoder, pcm_path, output_path,
            "-tencent", "-Fs_API", "24000", "-Fs_maxInternal", "24000",
            "-packetlength", "20", "-quiet",
        ], timeout=120)
    finally:
        try:
            os.unlink(pcm_path)
        except OSError:
            pass
    if encoded is None or encoded.returncode != 0 or not _nonempty_file(output_path):
        error = encoded.stderr.decode("utf-8", errors="replace").strip() if encoded else "encoder unavailable"
        raise RuntimeError(f"Silk encode failed: {error[:300]}")


@app.post("/api/send/voice-upload")
async def send_voice_upload(
    wxid: str = Form(...),
    duration_ms: int = Form(...),
    file: UploadFile = File(...),
):
    """Convert a browser recording to Tencent Silk v3 and send it through /SendVoiceMsg."""
    duration_ms = max(300, min(int(duration_ms or 0), 60_000))
    data = await file.read()
    if not data or len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Invalid voice recording")
    extension = os.path.splitext(file.filename or "recording.webm")[1] or ".webm"
    stem = f"voice_{int(time.time() * 1000)}_{_safe_media_filename_part(wxid, 'chat', 40)}"
    input_path = os.path.join(_scoped_dir(_VOICE_CACHE_DIR), stem + extension)
    silk_path = os.path.join(_scoped_dir(_VOICE_CACHE_DIR), stem + ".silk")
    with open(input_path, "wb") as output:
        output.write(data)
    try:
        await asyncio.to_thread(_convert_recording_to_silk, input_path, silk_path)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
    _queue_send_job(
        "voice-upload",
        agent_id,
        wxid,
        "34",
        "",
        {"voice_path": silk_path, "voice_len": str(duration_ms)},
        lambda: wechat_api.send_voice(wxid, silk_path, duration_ms),
    )
    return _send_queued_response(
        "voice-upload",
        wxid,
        path=silk_path,
        duration_ms=duration_ms,
    )


@app.post("/api/send/gif-upload")
async def send_gif_upload(wxid: str = Form(...), file: UploadFile = File(...)):
    """Upload a GIF/sticker file from the browser and send it via WeChat."""
    safe_name = (file.filename or "emoji.gif").replace("\\", "_").replace("/", "_")
    filename = f"{int(time.time())}_{safe_name}"
    filepath = os.path.join(_scoped_dir(_UPLOAD_DIR), filename)
    data = await file.read()
    with open(filepath, "wb") as f:
        f.write(data)
    _log(f"[UPLOAD] Saved GIF: {filepath} ({len(data)} bytes)")

    agent_id = _current_agent_id() or agent_manager.active_id() or ""
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


@app.post("/api/message-struct")
async def get_message_struct(req: MessageStructRequest):
    msg_id = str(req.msg_id or "").strip()
    if not msg_id:
        return {"ok": False, "error": "missing_msg_id"}
    recall_timestamp = int(req.timestamp or 0)
    now = int(time.time())
    if recall_timestamp <= 0 or now - recall_timestamp > 120 or recall_timestamp - now > 5:
        return {
            "ok": False,
            "msg_id": msg_id,
            "content": "",
            "error": "re_edit_expired",
        }

    structure: dict = {}
    try:
        structure = await wechat_api.get_msg_struct(msg_id)
    except Exception as e:
        _log(f"[GET_MSG_STRUCT] failed msg_id={msg_id}: {type(e).__name__}: {e}")

    content, msgtype = _message_struct_content(structure)
    if _is_editable_recalled_text(content, msgtype):
        return {
            "ok": True,
            "msg_id": msg_id,
            "msgtype": msgtype,
            "content": content,
            "source": "message_struct",
        }

    cached_content = _cached_recalled_text(req.chat_id.strip(), msg_id, req.timestamp)
    if cached_content:
        return {
            "ok": True,
            "msg_id": msg_id,
            "msgtype": "1",
            "content": cached_content,
            "source": "message_cache",
        }

    return {
        "ok": False,
        "msg_id": msg_id,
        "msgtype": msgtype,
        "content": "",
        "error": "message_content_unavailable",
    }


@app.post("/api/mark-read/{wxid}")
async def mark_read(wxid: str):
    """Mark a chat as read: clear unread in store + broadcast to all frontends."""
    # Clear in our in-memory store
    _messages().mark_read(wxid)
    try:
        _cache().mark_session_read(wxid, owner_wxid=_contact_owner_wxid())
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
    _require_owned_path(path)
    if os.path.exists(path):
        return FileResponse(path)
    return {"error": "File not found"}


def _media_path_field(msg_type: str) -> str:
    return {
        "3": "img_path",
        "34": "voice_path",
        "43": "video_path",
        "62": "video_path",
        "47": "gif_path",
        "49": "file_path",
    }.get(str(msg_type or ""), "file_path")


def _media_file_response(path: str, filename: str = "", *, download: bool = False) -> FileResponse:
    resolved_name = os.path.basename(filename or path) or "media"
    media_type = mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"
    if download:
        return FileResponse(path, media_type=media_type, filename=resolved_name)
    return FileResponse(path, media_type=media_type)


def _persist_media_path(msg_id: str, msg_type: str, path: str) -> None:
    if not msg_id or not path:
        return
    try:
        _cache().update_media_path_by_msg_id(
            str(msg_id),
            _media_path_field(msg_type),
            path,
            owner_wxid=_contact_owner_wxid(),
        )
    except Exception as exc:
        _log(f"[MEDIA] path persistence failed for {msg_id}: {type(exc).__name__}: {exc}")


def _cached_media_path(msg_id: str, msg_type: str) -> str:
    if not msg_id:
        return ""
    try:
        cached = _cache().get_message_by_id(str(msg_id), owner_wxid=_contact_owner_wxid())
    except Exception:
        return ""
    return str((cached or {}).get(_media_path_field(msg_type)) or "")


@app.get("/api/media/file")
async def serve_local_media(path: str, filename: str = "", download: bool = False):
    """Serve an already-known local media path through the authenticated backend."""
    _require_owned_path(path)
    if not _nonempty_file(path):
        raise HTTPException(status_code=404, detail="File not found")
    return _media_file_response(path, filename, download=download)


@app.get("/api/media/db-image/{media_id}")
async def serve_db_image(media_id: str):
    """Serve an image blob stored in the per-account SQLite cache."""
    row = _cache().get_media_blob(media_id)
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
def _discover_wechat_files_base() -> str:
    explicit = str(os.environ.get("WECHAT_FILES_BASE") or "").strip()
    if explicit:
        return explicit

    data_root = os.path.join(os.environ.get("APPDATA", ""), "WxDirDataPath")
    configured = os.path.join(data_root, config.RDV or "default", "WeChat Files")
    if os.path.isdir(configured):
        return configured

    discovered: list[str] = []
    try:
        for entry in os.scandir(data_root):
            candidate = os.path.join(entry.path, "WeChat Files")
            if entry.is_dir() and os.path.isdir(candidate):
                discovered.append(candidate)
    except OSError:
        pass
    if not discovered:
        return configured
    discovered.sort(key=lambda path: os.path.getmtime(os.path.dirname(path)), reverse=True)
    selected = discovered[0]
    _log(f"[MEDIA] Auto-detected WeChat Files base: {selected}")
    return selected


_WECHAT_FILES_BASE = _discover_wechat_files_base()

# Cache dir for decoded .dat images
_IMG_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".img_cache")
os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
_MEDIA_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".media_cache")
_VOICE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".voice_cache")
os.makedirs(_MEDIA_CACHE_DIR, exist_ok=True)
os.makedirs(_VOICE_CACHE_DIR, exist_ok=True)


def _image_file_response(path: str, msg_id: str = ""):
    response_path = _ensure_browser_image_file(path) or path
    if msg_id and path:
        try:
            updated = _cache().update_image_path_by_msg_id(str(msg_id), response_path, owner_wxid=_contact_owner_wxid())
            if updated:
                _log(f"[SQLITE_CACHE] image path cached for msg_id={msg_id}: {response_path}")
        except Exception as e:
            _log(f"[SQLITE_CACHE] image path update failed: {type(e).__name__}: {e}")
    return FileResponse(response_path, media_type=_image_media_type_for_path(response_path))


def _image_magic(data: bytes) -> str:
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"wxgf":
        return "wxgf"
    return ""


def _image_media_type_for_path(path: str) -> str:
    try:
        with open(path, "rb") as f:
            kind = _image_magic(f.read(16))
    except Exception:
        kind = ""
    return {
        "gif": "image/gif",
        "png": "image/png",
        "jpg": "image/jpeg",
        "webp": "image/webp",
    }.get(kind, "application/octet-stream")


def _ffmpeg_executable() -> str:
    configured = os.environ.get("FFMPEG_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _find_wxgf_partitions(data: bytes) -> list[tuple[int, int, float]]:
    """Find HEVC Annex B partitions inside WeChat wxgf/wxam data."""
    if len(data) < 15 or data[:4] != b"wxgf":
        return []
    header_len = data[4]
    if header_len >= len(data):
        return []

    for pattern in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        partitions: list[tuple[int, int, float]] = []
        offset = 0
        while header_len + offset <= len(data):
            idx = data.find(pattern, header_len + offset)
            if idx < 0:
                break
            if idx < 4:
                offset = idx - header_len + 1
                continue
            size = int.from_bytes(data[idx - 4:idx], "big", signed=False)
            if 0 < size and idx + size <= len(data):
                partitions.append((idx, size, size / len(data)))
                offset = idx - header_len + size
            else:
                offset = idx - header_len + 1
        if partitions:
            return partitions
    return []


def _convert_wxgf_to_jpeg(data: bytes) -> bytes:
    partitions = _find_wxgf_partitions(data)
    if not partitions:
        raise ValueError("no wxgf HEVC partition found")
    offset, size, _ratio = max(partitions, key=lambda item: item[1])
    hevc_data = data[offset:offset + size]
    cmd = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-loglevel", "error",
        "-f", "hevc",
        "-i", "pipe:0",
        "-frames:v", "1",
        "-c:v", "mjpeg",
        "-q:v", "4",
        "-f", "image2pipe",
        "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        input=hevc_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {stderr[:300]}")
    if _image_magic(proc.stdout[:16]) != "jpg":
        raise RuntimeError("ffmpeg did not return jpeg data")
    return proc.stdout


def _ensure_browser_image_file(path: str) -> str:
    if not _nonempty_file(path):
        return ""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        kind = _image_magic(head)
        if kind and kind != "wxgf":
            return path
        if kind != "wxgf":
            return path

        decoded_path = os.path.splitext(path)[0] + ".decoded.jpg"
        if _nonempty_file(decoded_path):
            with open(decoded_path, "rb") as f:
                if _image_magic(f.read(16)) == "jpg":
                    return decoded_path

        with open(path, "rb") as f:
            data = f.read()
        jpeg_data = _convert_wxgf_to_jpeg(data)
        tmp_path = decoded_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(jpeg_data)
        os.replace(tmp_path, decoded_path)
        _log(f"[WXGF] converted {path} -> {decoded_path} ({len(jpeg_data)} bytes)")
        return decoded_path
    except Exception as e:
        _log(f"[WXGF] convert failed for {path}: {type(e).__name__}: {e}")
        return path


def _safe_media_filename_part(value: str, fallback: str = "media", limit: int = 80) -> str:
    safe = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in str(value or ""))
    safe = safe.strip("._")
    return (safe[:limit] or fallback)


def _nonempty_file(path: str) -> bool:
    try:
        return bool(path and os.path.isfile(path) and os.path.getsize(path) > 0)
    except Exception:
        return False


async def _wait_for_local_media_path(
    msg_id: str,
    msg_type: str,
    candidates: list[str],
) -> str:
    tracked = [str(candidate or "").strip() for candidate in candidates if str(candidate or "").strip()]
    waitable = [
        candidate
        for candidate in tracked
        if os.path.isdir(os.path.dirname(os.path.abspath(candidate)))
    ]
    if not waitable:
        return ""

    deadline = time.monotonic() + _LOCAL_MEDIA_SETTLE_SECONDS
    while True:
        cached = _cached_media_path(msg_id, msg_type)
        for candidate in [*waitable, cached]:
            if _nonempty_file(candidate):
                return candidate
        if time.monotonic() >= deadline:
            return ""
        await asyncio.sleep(_LOCAL_MEDIA_SETTLE_INTERVAL_SECONDS)


def _xml_root(xml_text: str) -> ET.Element | None:
    raw = str(xml_text or "").strip()
    starts = [pos for token in ("<?xml", "<msg", "<appmsg", "<videomsg", "<img") if (pos := raw.find(token)) >= 0]
    if starts:
        raw = raw[min(starts):]
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def _parse_media_download_params(msg_type: str, msg_xml: str, filename: str = "") -> dict[str, Any]:
    """Extract Hook /download fields from image, video, or file message XML."""
    msg_type = str(msg_type or "")
    result: dict[str, Any] = {
        "aeskey": "",
        "fileid": "",
        "filename": os.path.basename(str(filename or "").replace("\\", "/")),
        "file_type": 1 if msg_type == "3" else 2,
        "large_video": 0,
    }
    root = _xml_root(msg_xml)
    if root is not None:
        if msg_type == "3":
            node = root.find(".//img") if root.tag != "img" else root
            if node is not None:
                result["aeskey"] = node.get("aeskey", "") or node.get("cdnthumbaeskey", "")
                result["fileid"] = (
                    node.get("cdnmidimgurl", "")
                    or node.get("cdnbigimgurl", "")
                    or node.get("cdnthumburl", "")
                )
        elif msg_type in {"43", "62"}:
            node = root.find(".//videomsg") if root.tag != "videomsg" else root
            if node is not None:
                result["aeskey"] = node.get("aeskey", "") or node.get("cdnrawvideoaeskey", "")
                result["fileid"] = node.get("cdnvideourl", "") or node.get("cdnrawvideourl", "")
                result["large_video"] = 1 if node.get("cdnrawvideourl", "") else 0
        elif msg_type == "49":
            title = (root.findtext(".//appmsg/title") or root.findtext(".//title") or "").strip()
            attach = root.find(".//appattach")
            if title and not result["filename"]:
                result["filename"] = os.path.basename(title.replace("\\", "/"))
            if attach is not None:
                result["aeskey"] = (attach.findtext("aeskey") or "").strip()
                result["fileid"] = (attach.findtext("cdnattachurl") or "").strip()
                attach_id = (attach.findtext("attachid") or "").strip()
                if not result["fileid"] and attach_id and not attach_id.startswith("@cdn_"):
                    result["fileid"] = attach_id

    if not result["aeskey"] or not result["fileid"]:
        if msg_type == "3":
            image_params = _parse_img_xml_cdn_params(msg_xml)
            result["aeskey"] = result["aeskey"] or image_params.get("decode_key", "")
            result["fileid"] = result["fileid"] or image_params.get("file_id", "")
        else:
            aes_match = re.search(r'(?:\baeskey="|<aeskey>)([^"<]+)', str(msg_xml or ""), re.IGNORECASE)
            file_match = re.search(
                r'(?:\bcdnvideourl="|<cdnattachurl>)(?:<!\[CDATA\[)?([^"<\]]+)',
                str(msg_xml or ""),
                re.IGNORECASE,
            )
            result["aeskey"] = result["aeskey"] or (aes_match.group(1).strip() if aes_match else "")
            result["fileid"] = result["fileid"] or (file_match.group(1).strip() if file_match else "")

    if not result["filename"]:
        extension = {"3": ".jpg", "43": ".mp4", "62": ".mp4"}.get(msg_type, ".bin")
        result["filename"] = f"wechat_media{extension}"
    return result


async def _resolve_media_path(
    *,
    msg_id: str,
    msg_type: str,
    local_path: str,
    msg_xml: str,
    filename: str,
) -> tuple[str, str]:
    """Resolve local media first, then use Hook CDN and persist the new path."""
    candidates = [str(local_path or "").strip(), _cached_media_path(msg_id, msg_type)]
    for candidate in candidates:
        if _nonempty_file(candidate):
            _persist_media_path(msg_id, msg_type, candidate)
            return candidate, os.path.basename(filename or candidate)

    settled_path = await _wait_for_local_media_path(msg_id, msg_type, candidates)
    if settled_path:
        _persist_media_path(msg_id, msg_type, settled_path)
        return settled_path, os.path.basename(filename or settled_path)

    if not config.IS_LOCAL_HOOK:
        raise HTTPException(status_code=404, detail="Local media file is unavailable")
    params = _parse_media_download_params(msg_type, msg_xml, filename)
    if (not params["aeskey"] or not params["fileid"]) and msg_id:
        try:
            structure = await wechat_api.get_msg_struct(msg_id)
            structure_xml, structure_type = _message_struct_content(structure)
            params = _parse_media_download_params(
                structure_type if structure_type in {"3", "43", "49", "62"} else msg_type,
                structure_xml,
                filename,
            )
        except Exception as exc:
            _log(f"[MEDIA] GetMsgStruct fallback failed for {msg_id}: {type(exc).__name__}: {exc}")
    if not params["aeskey"] or not params["fileid"]:
        raise HTTPException(status_code=404, detail="No usable local path or CDN parameters")

    msg_part = _safe_media_filename_part(msg_id, "nomsg", 64)
    source_name = os.path.basename(str(params["filename"] or "media").replace("\\", "/"))
    extension = os.path.splitext(source_name)[1]
    cache_name = f"{msg_part}_{hashlib.sha256(params['fileid'].encode('utf-8')).hexdigest()[:12]}{extension}"
    cache_path = os.path.abspath(os.path.join(_scoped_dir(_MEDIA_CACHE_DIR), cache_name))
    if _nonempty_file(cache_path):
        _persist_media_path(msg_id, msg_type, cache_path)
        return cache_path, source_name

    async with _cdn_download_sem:
        if not _nonempty_file(cache_path):
            result = await wechat_api.cdn_download(
                params["aeskey"],
                params["fileid"],
                cache_path,
                file_type=int(params["file_type"]),
                large_video=int(params["large_video"]),
            )
            returned_path = _extract_download_save_path(result)
            for _ in range(20):
                if _nonempty_file(returned_path) or _nonempty_file(cache_path):
                    break
                await asyncio.sleep(0.1)

            ready_path = returned_path if _nonempty_file(returned_path) else cache_path
            if not _nonempty_file(ready_path):
                requested_path = str((result or {}).get("requested_save_path") or "") if isinstance(result, dict) else ""
                downloaded = await wechat_api.read_local_download(requested_path)
                if downloaded:
                    temp_path = cache_path + ".tmp"
                    with open(temp_path, "wb") as output:
                        output.write(downloaded)
                    os.replace(temp_path, cache_path)
                    ready_path = cache_path
            if not _nonempty_file(ready_path):
                error = str((result or {}).get("error") or (result or {}).get("retmsg") or "CDN download failed")
                raise HTTPException(status_code=502, detail=error)
        else:
            ready_path = cache_path

    _persist_media_path(msg_id, msg_type, ready_path)
    return ready_path, source_name


@app.post("/api/media/resolve")
async def resolve_media(request: Request):
    body = await request.json()
    _require_owned_path(str(body.get('path') or ''))
    msg_type = str(body.get("msgtype", "") or "")
    path, filename = await _resolve_media_path(
        msg_id=str(body.get("msg_id", "") or ""),
        msg_type=msg_type,
        local_path=str(body.get("path", "") or ""),
        msg_xml=str(body.get("msg_xml", "") or ""),
        filename=str(body.get("filename", "") or ""),
    )
    return _media_file_response(path, filename, download=bool(body.get("download", False)))


def _silk_codec_executable(kind: str) -> str:
    filename = f"silk_v3_{kind}.exe" if os.name == "nt" else f"silk_v3_{kind}"
    configured = str(os.environ.get(f"SILK_{kind.upper()}_PATH") or "").strip()
    candidates = [configured, shutil.which(filename) or ""]
    backend_dir = os.path.dirname(__file__)
    candidates.extend([
        os.path.join(backend_dir, "tools", filename),
        os.path.join(os.path.dirname(backend_dir), "tools", filename),
        os.path.join(os.path.expanduser("~"), "Desktop", "OtherVersion", "3.7.6.44", filename),
    ])
    versions_dir = os.path.join(os.path.expanduser("~"), "Desktop", "OtherVersion")
    try:
        candidates.extend(
            os.path.join(entry.path, filename)
            for entry in os.scandir(versions_dir)
            if entry.is_dir()
        )
    except OSError:
        pass
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise RuntimeError(f"{filename} was not found; configure SILK_{kind.upper()}_PATH")


def _run_media_command(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        creationflags=creation_flags,
    )


def _binary_from_text(value: Any) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        return b""
    compact = re.sub(r"\s+", "", raw)
    if len(compact) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", compact):
        try:
            return bytes.fromhex(compact)
        except ValueError:
            pass
    if "," in compact and compact.lower().startswith("data:"):
        compact = compact.split(",", 1)[1]
    try:
        return base64.b64decode(compact, validate=True)
    except Exception:
        return b""


def _voice_source_from_result(result: Any) -> tuple[bytes, str]:
    stack = [result]
    found_path = ""
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                lowered = str(key).replace("_", "").lower()
                if lowered in {"voicehex", "voicedata", "filedata", "hex"}:
                    payload = _binary_from_text(value)
                    if payload:
                        return payload, found_path
                if lowered in {"voicepath", "filepath", "savepath", "path"} and isinstance(value, str):
                    found_path = value.strip() or found_path
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return b"", found_path


def _decode_silk_to_wav(silk_data: bytes, msg_id: str = "") -> tuple[str, str]:
    if not silk_data or len(silk_data) > 25 * 1024 * 1024:
        raise ValueError("Invalid Silk payload")
    if b"#!SILK_V3" not in silk_data[:16]:
        raise ValueError("Voice payload is not Silk v3")

    digest = hashlib.sha256(silk_data).hexdigest()
    stem = f"{_safe_media_filename_part(msg_id, 'voice', 48)}_{digest[:16]}"
    silk_path = os.path.join(_scoped_dir(_VOICE_CACHE_DIR), f"{stem}.silk")
    pcm_path = os.path.join(_scoped_dir(_VOICE_CACHE_DIR), f"{stem}.pcm")
    wav_path = os.path.join(_scoped_dir(_VOICE_CACHE_DIR), f"{stem}.wav")
    if _nonempty_file(wav_path):
        return wav_path, silk_path

    if not _nonempty_file(silk_path):
        temp_silk = silk_path + ".tmp"
        with open(temp_silk, "wb") as output:
            output.write(silk_data)
        os.replace(temp_silk, silk_path)

    decoder = _silk_codec_executable("decoder")
    decoded = _run_media_command([
        decoder, silk_path, pcm_path, "-Fs_API", "24000", "-quiet",
    ])
    if decoded.returncode != 0 or not _nonempty_file(pcm_path):
        error = decoded.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Silk decode failed: {error[:300]}")

    converted = _run_media_command([
        _ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", pcm_path,
        "-y", wav_path,
    ])
    try:
        os.unlink(pcm_path)
    except OSError:
        pass
    if converted.returncode != 0 or not _nonempty_file(wav_path):
        error = converted.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Voice conversion failed: {error[:300]}")
    return wav_path, silk_path


@app.post("/api/media/voice")
async def resolve_voice(request: Request):
    body = await request.json()
    msg_id = str(body.get("msg_id", "") or "")
    cached = _cache().get_message_by_id(msg_id, owner_wxid=_contact_owner_wxid()) if msg_id else None
    local_path = str(body.get("path", "") or (cached or {}).get("voice_path") or "")
    if _nonempty_file(local_path):
        if os.path.splitext(local_path)[1].lower() in {".wav", ".mp3", ".ogg", ".m4a", ".webm"}:
            return _media_file_response(local_path)
        with open(local_path, "rb") as source:
            silk_data = source.read(25 * 1024 * 1024 + 1)
    else:
        silk_data = b""

    if not silk_data:
        for value in (
            body.get("voice_hex"), body.get("voice_data"),
            (cached or {}).get("voice_hex"), (cached or {}).get("voice_data"),
        ):
            silk_data = _binary_from_text(value)
            if silk_data:
                break

    if not silk_data and msg_id:
        try:
            voice_length = int(body.get("length") or (cached or {}).get("voice_len") or 0)
        except (TypeError, ValueError):
            voice_length = 0
        hook_result = await wechat_api.download_voice(
            str(body.get("clientmsgid") or (cached or {}).get("clientmsgid") or ""),
            voice_length,
            str(body.get("fromgid") or (cached or {}).get("fromgid") or ""),
            msg_id,
        )
        silk_data, downloaded_path = _voice_source_from_result(hook_result)
        if not silk_data and _nonempty_file(downloaded_path):
            with open(downloaded_path, "rb") as source:
                silk_data = source.read(25 * 1024 * 1024 + 1)

    if not silk_data:
        raise HTTPException(status_code=404, detail="Voice data is unavailable")
    try:
        wav_path, silk_path = await asyncio.to_thread(_decode_silk_to_wav, silk_data, msg_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _persist_media_path(msg_id, "34", silk_path)
    return FileResponse(wav_path, media_type="audio/wav")


def _extract_download_save_path(result: Any) -> str:
    """Find savePath/file path returned by the Hook /download API."""
    stack: list[Any] = [result]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                lowered = str(key).lower()
                if lowered in {"savepath", "save_path", "filepath", "file_path", "path"}:
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def _extract_protocol_cdn_base64(result: Any) -> str:
    """Find the decrypted file payload returned by the Go /cdndownload API."""
    stack: list[Any] = [result]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                lowered = str(key).replace("_", "").lower()
                if lowered in {"imagebase64", "filedata"} and isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def _cache_downloaded_image_file(source_path: str, cache_path: str) -> str:
    """Copy a locally accessible Hook download into the backend image cache."""
    if not _nonempty_file(source_path):
        return ""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        source_abs = os.path.abspath(source_path)
        cache_abs = os.path.abspath(cache_path)
        if os.path.normcase(source_abs) != os.path.normcase(cache_abs):
            shutil.copyfile(source_path, cache_path)
        if _nonempty_file(cache_path):
            return cache_path
    except Exception as e:
        _log(f"[IMG_DL] cache copy failed: {type(e).__name__}: {e}")
    return source_path


def _cache_downloaded_image_bytes(data: bytes, cache_path: str) -> str:
    if not data or len(data) < 16 or len(data) > 100 * 1024 * 1024:
        return ""
    if not _image_magic(data[:16]):
        return ""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        temp_path = cache_path + ".tmp"
        with open(temp_path, "wb") as file:
            file.write(data)
        os.replace(temp_path, cache_path)
        return cache_path if _nonempty_file(cache_path) else ""
    except OSError as exc:
        _log(f"[IMG_DL] cache write failed: {type(exc).__name__}: {exc}")
        return ""


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
        dat_full = os.path.join(_wechat_files_base(), rel)
        jpg_full = os.path.splitext(dat_full)[0] + ".jpg"
        if os.path.exists(jpg_full) and os.path.getsize(jpg_full) > 0:
            return img_dat_paths, thumb_dat_paths, jpg_full
    return img_dat_paths, thumb_dat_paths, None


async def _try_decode_dat(dat_paths: list[bytes], label: str) -> str | None:
    """Try to decode .dat files via /DecodePic. Returns decoded path or None."""
    for p in dat_paths:
        rel = p.decode('ascii', errors='replace')
        dat_full = os.path.join(_wechat_files_base(), rel)
        if not os.path.exists(dat_full):
            continue
        cache_name = os.path.splitext(os.path.basename(rel))[0]
        decoded_path = os.path.join(_scoped_dir(_IMG_CACHE_DIR), f"{cache_name}.jpg")
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
    try:
        async with asyncio.timeout(110):
            return await _download_image_payload(await request.json(), _request_agent_id(request))
    except TimeoutError:
        return {"error": "图片下载超时，请稍后点击重试"}


async def _download_quoted_image_by_id(msg_id, cache_path, account_id):
    """Use the documented svrid download API and cache only this image's result."""
    key = f"quote:{account_id}:{msg_id}"
    async with _inflight_cdn_lock:
        future = _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).get(key)
        owns = future is None or future.done()
        if owns:
            future = asyncio.get_running_loop().create_future()
            _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads)[key] = future
    if not owns:
        return await asyncio.wait_for(asyncio.shield(future), timeout=50)
    path = ""
    try:
        async with _cdn_download_sem:
            path = _cached_media_path(str(msg_id), "3")
            if not _nonempty_file(path):
                result = await wechat_api.down_file_or_pic(str(msg_id))
                path = _cache_downloaded_image_file(_extract_download_save_path(result), cache_path)
                if not path:
                    for _ in range(40):
                        path = _cached_media_path(str(msg_id), "3")
                        if _nonempty_file(path):
                            break
                        await asyncio.sleep(0.25)
        path = path if _nonempty_file(path) else ""
        return path
    except Exception as exc:
        _log(f"[IMG_DL] quote svrid download failed msg_id={msg_id}: {type(exc).__name__}")
        return ""
    finally:
        if not future.done():
            future.set_result(path if _nonempty_file(path) else "")
        async with _inflight_cdn_lock:
            if _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).get(key) is future:
                _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).pop(key, None)


async def _download_image_payload(body: dict, account_id: str = ""):
    """Resolve a type-3 image.

    Local hook strategy:
      1. Check local decoded .jpg files (from BytesExtra Image paths)
      2. Decode local .dat files via /DecodePic (Image then Thumb)
      3. Call CDN /download and read its relative savePath through /localfile

    Protocol strategy:
      1. Parse aeskey and fileid from the image message XML
      2. Call the Go protocol /cdndownload endpoint
      3. Save the synchronous imageBase64/fileData response locally

    Remote hook strategy:
      1. Check callback cache (base64 images saved from callbacks)
      2. Call /download; the image arrives later through an img_base64 callback
      3. Wait for the callback to deliver the image
    """
    import hashlib
    bytes_extra_hex = body.get("bytes_extra_hex", "")
    msg_xml = body.get("msg_xml", "")
    msg_id = body.get("msg_id", "")  # MsgSvrID for matching callback
    cached = _cache().get_message_by_id(str(msg_id), owner_wxid=_contact_owner_wxid()) if msg_id else None
    if cached and str(cached.get("msgtype") or "") == "3":
        cached_path = str(cached.get("img_path") or "")
        if _nonempty_file(cached_path):
            return _image_file_response(cached_path, str(msg_id))
        msg_xml = msg_xml or str(cached.get("msg") or "")
        bytes_extra_hex = bytes_extra_hex or str(cached.get("bytesExtraHex") or "")
    # Quote image XML may contain only a placeholder; recover the original image.
    parameters = _parse_img_xml_cdn_params(msg_xml)
    if msg_id and not (parameters["decode_key"] and parameters["file_id"]):
        try:
            structure_xml, structure_type = _message_struct_content(await wechat_api.get_msg_struct(str(msg_id)))
            if structure_xml and structure_type in {"", "3"} and "<img" in structure_xml:
                msg_xml = structure_xml
        except Exception as exc:
            _log(f"[IMG_DL] original image structure unavailable msg_id={msg_id}: {type(exc).__name__}")

    raw = b""
    if bytes_extra_hex:
        try:
            raw = bytes.fromhex(bytes_extra_hex)
        except Exception:
            pass

    cdn_params = _parse_img_xml_cdn_params(msg_xml)
    has_cdn_params = bool(cdn_params["decode_key"] and cdn_params["file_id"])
    xml_hash = hashlib.md5((msg_xml or msg_id or "").encode("utf-8", errors="replace")).hexdigest()
    file_id_hash = hashlib.md5(cdn_params["file_id"].encode("utf-8", errors="replace")).hexdigest()[:12]
    msg_part = _safe_media_filename_part(str(msg_id or ""), "nomsg", 64)
    download_filename = f"wximg_{msg_part}_{xml_hash[:12]}_{file_id_hash}.jpg"
    cache_path = os.path.join(_scoped_dir(_IMG_CACHE_DIR), "cdn", download_filename)
    legacy_cache_path = os.path.join(_scoped_dir(_IMG_CACHE_DIR), f"{xml_hash}.jpg")
    cache_candidates = (cache_path, legacy_cache_path)

    # Check the callback cache first. This is shared by Hook modes.
    if msg_id:
        cb_dir = os.path.join(_scoped_dir(_IMG_CACHE_DIR), "callback")
        safe_id = "".join(c for c in str(msg_id) if c.isalnum() or c in ("_", "-", "."))[:80]
        for ext in ("jpg", "png", "gif", "webp"):
            cached = os.path.join(cb_dir, f"{safe_id}.{ext}")
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                return _image_file_response(cached, msg_id)

    if body.get("quoted") and msg_id and config.IS_HOOK:
        path = await _download_quoted_image_by_id(str(msg_id), cache_path, account_id)
        if path:
            return _image_file_response(path, str(msg_id))

    # Protocol mode: download and decrypt CDN data synchronously through the Go service.
    if config.IS_PROTOCOL:
        for cached_path in cache_candidates:
            if _nonempty_file(cached_path):
                return _image_file_response(cached_path, msg_id)
        if not has_cdn_params:
            return {"error": "No CDN params in image XML for protocol download"}

        account_id = account_id or _current_agent_id() or agent_manager.active_id() or ""
        if not account_id:
            return {"error": "No active protocol session for image download"}

        async with _cdn_download_sem:
            for cached_path in cache_candidates:
                if _nonempty_file(cached_path):
                    return _image_file_response(cached_path, msg_id)
            _log(
                f"[IMG_DL] protocol /cdndownload: aeskey={cdn_params['decode_key'][:16]}... "
                f"file_id={cdn_params['file_id'][:32]}... msg_id={msg_id}"
            )
            cdn_result = await protocol_api.download_cdn_image(
                account_id,
                cdn_params["decode_key"],
                cdn_params["file_id"],
            )
            if not isinstance(cdn_result, dict) or cdn_result.get("ok") is False:
                error = str((cdn_result or {}).get("error") or (cdn_result or {}).get("retmsg") or "CDN download failed")
                _log(f"[IMG_DL] protocol /cdndownload failed: {error}")
                return {"error": error}
            image_base64 = _extract_protocol_cdn_base64(cdn_result)
            if not image_base64:
                _log(f"[IMG_DL] protocol /cdndownload returned no image data: {_scrub_callback_payload(cdn_result)}")
                return {"error": "Protocol CDN response did not contain image data"}
            saved_path, byte_len = _save_img_base64_to_cache(image_base64, str(msg_id or xml_hash))
            ready_path = _cache_downloaded_image_file(saved_path or "", cache_path)
            if not ready_path:
                return {"error": "Protocol CDN image could not be saved"}
            _log(f"[IMG_DL] protocol image ready: {ready_path} ({byte_len} bytes)")
            return _image_file_response(ready_path, msg_id)

    # Remote Hook mode: /download triggers an asynchronous img_base64 callback.
    if not config.IS_LOCAL_HOOK:

        if has_cdn_params:
            # Already downloaded before?
            for cached_path in cache_candidates:
                if _nonempty_file(cached_path):
                    return _image_file_response(cached_path, msg_id)

            # Also check callback cache by msgsvrid
            if msg_id:
                cb_dir = os.path.join(_scoped_dir(_IMG_CACHE_DIR), "callback")
                safe_id = "".join(c for c in str(msg_id) if c.isalnum() or c in ("_", "-", "."))[:80]
                for ext in ("jpg", "png", "gif", "webp"):
                    cached = os.path.join(cb_dir, f"{safe_id}.{ext}")
                    if os.path.exists(cached) and os.path.getsize(cached) > 0:
                        return _image_file_response(cached, msg_id)

            # Reuse an in-flight remote Hook download for the same image.
            async with _inflight_cdn_lock:
                existing = _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).get(xml_hash)
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
                for cached_path in cache_candidates:
                    if _nonempty_file(cached_path):
                        return _image_file_response(cached_path, msg_id)

            # Start a serialized remote Hook CDN download.
            loop = asyncio.get_event_loop()
            inflight_fut: asyncio.Future = loop.create_future()
            async with _inflight_cdn_lock:
                _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads)[xml_hash] = inflight_fut

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
                            img_filename=download_filename,
                        )
                        _log(f"[IMG_DL] CDN response: {cdn_result}")
                        save_path = _extract_download_save_path(cdn_result)
                        ready_path = _cache_downloaded_image_file(save_path, cache_path)
                        if ready_path:
                            _log(f"[IMG_DL] ✓ Image ready from /download savePath: {ready_path}")
                            await _fulfill_cdn_pending(pending_key, ready_path)
                            if msg_id:
                                await _fulfill_cdn_pending(f"msgsvrid:{msg_id}", ready_path)
                            if not inflight_fut.done():
                                inflight_fut.set_result(ready_path)
                    except Exception as e:
                        _log(f"[IMG_DL] CDN /download error: {e}")

                # Wait for callback only if /download did not expose a local savePath.
                if fut.done():
                    _log(f"[IMG_DL] Image result already fulfilled by /download savePath")
                else:
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
                    _bucket('_inflight_cdn_downloads', _inflight_cdn_downloads).pop(xml_hash, None)
                if not inflight_fut.done():
                    inflight_fut.set_exception(asyncio.TimeoutError("CDN callback timed out"))

            # Last resort: check callback cache (callback may have arrived late)
            if msg_id:
                cb_dir2 = os.path.join(_scoped_dir(_IMG_CACHE_DIR), "callback")
                safe_id2 = "".join(c for c in str(msg_id) if c.isalnum() or c in ("_", "-", "."))[:80]
                for ext in ("jpg", "png", "gif", "webp"):
                    cached = os.path.join(cb_dir2, f"{safe_id2}.{ext}")
                    if os.path.exists(cached) and os.path.getsize(cached) > 0:
                        return _image_file_response(cached, msg_id)
            for cached_path in cache_candidates:
                if _nonempty_file(cached_path):
                    return _image_file_response(cached_path, msg_id)

            return {"error": "CDN download timed out — image may still arrive via callback, retry later"}

        # Fallback: if no CDN params, return error
        if not raw:
            return {"error": "No CDN params in image XML for remote download"}

    # Local Hook fallback: inspect and decode files from the WeChat data directory.
    img_dat_paths: list[bytes] = []
    thumb_dat_paths: list[bytes] = []

    # Phase 1: check local decoded .jpg files.
    if raw:
        img_dat_paths, thumb_dat_paths, found = _find_local_image(raw)
        if found:
            return _image_file_response(found, msg_id)

    # Phase 2: decode local .dat files.
    if img_dat_paths:
        result = await _try_decode_dat(img_dat_paths, "Image")
        if result:
            return _image_file_response(result, msg_id)
    if thumb_dat_paths:
        result = await _try_decode_dat(thumb_dat_paths, "Thumb")
        if result:
            return _image_file_response(result, msg_id)

    # Phase 3: history messages and RecvType=2 use the CDN /download endpoint.
    if msg_xml and ("<img" in msg_xml or "<msg>" in msg_xml):
        if not has_cdn_params:
            return {"error": "No CDN params in image XML for local download"}

        for cached_path in cache_candidates:
            if _nonempty_file(cached_path):
                return _image_file_response(cached_path, msg_id)

        pending_key = f"cdn:{xml_hash}"
        pending_future: asyncio.Future | None = None
        if config.RECV_TYPE == 2:
            pending_future = await _register_cdn_pending(pending_key)
            if msg_id:
                await _register_cdn_pending(f"msgsvrid:{msg_id}", existing_fut=pending_future)

        try:
            async with _cdn_download_sem:
                for cached_path in cache_candidates:
                    if _nonempty_file(cached_path):
                        return _image_file_response(cached_path, msg_id)

                _log(
                    f"[IMG_DL] local CDN /download: decode_key={cdn_params['decode_key'][:16]}... "
                    f"file_id={cdn_params['file_id'][:32]}... msg_id={msg_id}"
                )
                result = await wechat_api.cdn_download_pic(
                    decode_key=cdn_params["decode_key"],
                    file_id=cdn_params["file_id"],
                    img_filename=download_filename,
                )
                _log(f"[IMG_DL] local CDN response: {result}")

                returned_path = _extract_download_save_path(result)
                ready_path = _cache_downloaded_image_file(returned_path, cache_path)
                if ready_path:
                    return _image_file_response(ready_path, msg_id)

                relative_path = str(
                    result.get("requested_save_path", f"downloads/{download_filename}")
                    if isinstance(result, dict)
                    else f"downloads/{download_filename}"
                )
                downloaded = await wechat_api.read_local_download(relative_path)
                ready_path = _cache_downloaded_image_bytes(downloaded, cache_path)
                if ready_path:
                    _log(f"[IMG_DL] local CDN image ready: {ready_path}")
                    return _image_file_response(ready_path, msg_id)

            if pending_future:
                try:
                    callback_path = await asyncio.wait_for(pending_future, timeout=10.0)
                    return _image_file_response(callback_path, msg_id)
                except asyncio.TimeoutError:
                    _log(f"[IMG_DL] local CDN callback timed out for msg_id={msg_id}")
        except Exception as exc:
            _log(f"[IMG_DL] local CDN /download error: {type(exc).__name__}: {exc}")
            return {"error": str(exc)}
        finally:
            if pending_future:
                await _cleanup_cdn_pending(pending_key)
                if msg_id:
                    await _cleanup_cdn_pending(f"msgsvrid:{msg_id}")

        return {"error": "CDN /download did not produce an image"}

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
        cached = os.path.join(_scoped_dir(_STICKER_CACHE_DIR), f"{safe_md5}.{ext}")
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
        cache_path = os.path.join(_scoped_dir(_STICKER_CACHE_DIR), f"{safe_md5}.{ext}")
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
                cache_path = os.path.join(_scoped_dir(_STICKER_CACHE_DIR), f"{safe_md5}.{ext}")
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
    cache_path = os.path.join(_scoped_dir(_AVATAR_CACHE_DIR), safe_name)

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
    cached = _bucket('_GROUP_NAMES_CACHE', _GROUP_NAMES_CACHE).get(gid)
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
        _bucket('_GROUP_NAMES_CACHE', _GROUP_NAMES_CACHE)[gid] = (result, now)
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


async def _cache_group_member_details(gid: str, raw_members: list[dict], *, owner_wxid: str = "") -> dict[str, dict]:
    owner_wxid = owner_wxid or _contact_owner_wxid()
    now = time.time()
    normalized: dict[str, dict] = {}
    contact_updates: dict[str, dict] = {}
    async with _CONTACT_PROFILE_LOCK:
        async with _CONTACT_BRIEF_LOCK:
            avatar_urls = _state().setdefault("avatar_urls", {})
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
                _messages().set_contact(wxid, name=name, avatar=avatar)
                contact_updates[wxid] = {
                    "wxid": wxid,
                    "name": name,
                    "avatar": avatar,
                    "is_group": False,
                    "profile": entry.get("profile") or {"wxid": wxid},
                }

    if normalized:
        _cache().upsert_group_members(gid, list(normalized.values()), owner_wxid=owner_wxid)
        _cache().upsert_contacts(contact_updates, owner_wxid=owner_wxid)
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
            for info in _contacts_from_batch_brief_response(data):
                wxid = _contact_profile_wxid(info)
                if not wxid or wxid not in result:
                    continue
                avatar = _contact_profile_avatar(info)
                name = _contact_profile_explicit_name(info)
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
    cached = _cache().get_group_members(gid, owner_wxid=owner_wxid)
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

    _log(f"[CALLBACK_SERVER] Starting on {config.CALLBACK_HOST}:{config.CALLBACK_PORT} ...")
    uvicorn.run(
        cb_app,
        host=config.CALLBACK_HOST,
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
        _log(f"[STARTUP] Starting separate callback listener on {config.CALLBACK_HOST}:{config.CALLBACK_PORT}")
        cb_thread = threading.Thread(target=_run_callback_server, daemon=True)
        cb_thread.start()

    # NOTE:
    # - In dev, uvicorn reload can be useful.
    # - But writing logs will trigger reload loops unless excluded.
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
            "backend/logs/*",
            "backend/logs/**/*",
            "backend/_uploads/*",
            "db/*",
            "db/**/*",
            ".img_cache/*",
            ".avatar_cache/*",
            ".sticker_cache/*",
        ] if reload_enabled else None,
    )
