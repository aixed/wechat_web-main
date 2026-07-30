"""Configuration for the WeChat backend server.

Reads from ../config.yaml and exposes settings for three modes:
  - local_hook       本地Hook (DLL注入本地微信)
  - remote_hook      远程客户端Hook (客户端DLL主动连接本后端WSS)
  - remote_protocol  远程协议 (服务器上的微信协议)
"""

import os
import sys
from typing import Any

import yaml

# Windows redirects stdout/stderr with the system code page by default.
# The backend logs Chinese text and status symbols, so force UTF-8 early.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─── Load config.yaml ─────────────────────────────────────────────

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

def _to_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default

def _load_yaml() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"[CONFIG] ⚠ config.yaml not found at {_CONFIG_PATH}, using defaults")
        return {}

_cfg = _load_yaml()

# ─── Login mode ────────────────────────────────────────────────────

_VALID_MODES = ("local_hook", "remote_hook", "remote_protocol")
_MODE_BY_NUMBER = {
    1: "local_hook",
    2: "remote_hook",
    3: "remote_protocol",
}

def _resolve_login_mode(cfg: dict) -> tuple[int, str]:
    raw_mode = cfg.get("wechat_mode")
    if raw_mode is None:
        raw_mode = cfg.get("login", 1)

    raw_text = str(raw_mode).strip().lower()
    if raw_text in ("1", "2", "3"):
        mode_number = int(raw_text)
        return mode_number, _MODE_BY_NUMBER[mode_number]

    if raw_text not in _VALID_MODES:
        raise ValueError(
            f"Invalid wechat_mode/login: {raw_mode!r}, "
            f"wechat_mode must be 1/2/3 or login must be one of {_VALID_MODES}"
        )

    for mode_number, mode_name in _MODE_BY_NUMBER.items():
        if mode_name == raw_text:
            return mode_number, mode_name

    raise ValueError(f"Invalid login mode: {raw_mode!r}")

WECHAT_MODE, LOGIN_MODE = _resolve_login_mode(_cfg)

IS_LOCAL_HOOK = LOGIN_MODE == "local_hook"
IS_REMOTE_HOOK = LOGIN_MODE == "remote_hook"
IS_REMOTE_PROTOCOL = LOGIN_MODE == "remote_protocol"

# Convenience: True when using Hook API (local or remote), False for protocol
IS_HOOK = IS_LOCAL_HOOK or IS_REMOTE_HOOK
IS_PROTOCOL = IS_REMOTE_PROTOCOL

# ─── Host & Ports (per mode) ──────────────────────────────────────

_prefix = LOGIN_MODE  # e.g. "local_hook", "remote_hook", "remote_protocol"

HOOK_HOST = str(_cfg.get(f"{_prefix}_host", "127.0.0.1"))
HOOK_PORT = int(_cfg.get(f"{_prefix}_api_port", 30001))
MGR_PORT = int(_cfg.get(f"{_prefix}_mgr_port", 29998 if IS_HOOK else 29999))

# ─── Public IP & RDV ──────────────────────────────────────────────

PUBLIC_IP = str(_cfg.get("ip", "127.0.0.1"))
RDV = str(_cfg.get("RDV", ""))

# ─── Derived URLs ──────────────────────────────────────────────────

HOOK_BASE_URL = f"http://{HOOK_HOST}:{HOOK_PORT}"
MGR_BASE_URL = f"http://{HOOK_HOST}:{MGR_PORT}"

# Backend server. Keep this on 127.0.0.1 when the frontend runs on the same
# machine and proxies /api + /agent; set to 0.0.0.0 only when exposing backend
# ports directly is intentional.
SERVER_HOST = str(_cfg.get("server_host", "127.0.0.1") or "127.0.0.1")
SERVER_PORT = int(_cfg.get("server_port", 5000))
DEFAULT_WEB_ACCESS_KEY = "admin"


def _resolve_web_access_key(document: dict[str, Any]) -> tuple[str, bool]:
    env_key = str(os.environ.get("WECHAT_WEB_ACCESS_KEY") or "").strip()
    raw_key = env_key or document.get("web_access_key")
    access_key = str(raw_key or "").strip() or DEFAULT_WEB_ACCESS_KEY
    return access_key, access_key == DEFAULT_WEB_ACCESS_KEY


WEB_ACCESS_KEY, WEB_ACCESS_KEY_IS_DEFAULT = _resolve_web_access_key(_cfg)
if WEB_ACCESS_KEY_IS_DEFAULT:
    print(
        "[CONFIG] WARNING: using default web_access_key=admin. "
        "Please edit config.yaml and change the web_access_key parameter.",
        flush=True,
    )

# AI smart replies. Secrets may live in the ignored config.yaml or environment.
AI_ACTIVE_PROFILE_ID = ""
AI_PROFILES: list[dict[str, Any]] = []
AI_BASE_URL = ""
AI_API_KEY = ""
AI_MODEL = ""
AI_TIMEOUT_SECONDS = max(5.0, float(_cfg.get("ai_timeout_seconds", 60)))
AI_MAX_CONCURRENCY = max(1, min(20, int(_cfg.get("ai_max_concurrency", 3))))


def _normalize_ai_profile(raw: Any, index: int = 0) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    profile_id = str(raw.get("id") or f"ai_profile_{index + 1}").strip()[:100]
    base_url = str(raw.get("base_url") or raw.get("api_base_url") or "").strip().rstrip("/")
    api_key = str(raw.get("api_key") or "").strip()
    model = str(raw.get("model") or "").strip()
    name = str(raw.get("name") or model or base_url or f"AI 配置 {index + 1}").strip()[:80]
    return {
        "id": profile_id or f"ai_profile_{index + 1}",
        "name": name or f"AI 配置 {index + 1}",
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }


def _legacy_ai_profile(document: dict[str, Any]) -> dict[str, Any] | None:
    base_url = str(document.get("ai_base_url", os.environ.get("OPENAI_BASE_URL", ""))).strip().rstrip("/")
    api_key = str(document.get("ai_api_key", os.environ.get("OPENAI_API_KEY", ""))).strip()
    model = str(document.get("ai_model", os.environ.get("OPENAI_MODEL", ""))).strip()
    if not (base_url or api_key or model):
        return None
    return {
        "id": "default",
        "name": str(document.get("ai_profile_name") or model or "默认配置").strip()[:80],
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }


def _resolve_ai_settings(document: dict[str, Any]) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    raw_profiles = document.get("ai_profiles")
    profiles: list[dict[str, Any]] = []
    if isinstance(raw_profiles, list):
        for index, raw in enumerate(raw_profiles):
            profile = _normalize_ai_profile(raw, index)
            if profile:
                profiles.append(profile)

    if not profiles:
        legacy = _legacy_ai_profile(document)
        if legacy:
            profiles.append(legacy)

    active_profile_id = str(document.get("ai_active_profile_id") or document.get("active_ai_profile_id") or "").strip()
    active = next((item for item in profiles if item.get("id") == active_profile_id), None)
    if active is None:
        active = next((item for item in profiles if item.get("base_url") and item.get("api_key") and item.get("model")), None)
    if active is None and profiles:
        active = profiles[0]
    if active is None:
        active = {"id": "", "name": "", "base_url": "", "api_key": "", "model": ""}
    active_profile_id = str(active.get("id") or "")
    return profiles, active_profile_id, active


def _apply_ai_settings(document: dict[str, Any]) -> dict:
    global AI_ACTIVE_PROFILE_ID, AI_PROFILES, AI_BASE_URL, AI_API_KEY, AI_MODEL
    profiles, active_profile_id, active = _resolve_ai_settings(document)
    AI_PROFILES = profiles
    AI_ACTIVE_PROFILE_ID = active_profile_id
    AI_BASE_URL = str(active.get("base_url") or "").strip().rstrip("/")
    AI_API_KEY = str(active.get("api_key") or "").strip()
    AI_MODEL = str(active.get("model") or "").strip()
    return {
        "configured": bool(AI_BASE_URL and AI_API_KEY and AI_MODEL),
        "base_url": AI_BASE_URL,
        "model": AI_MODEL,
        "active_profile_id": AI_ACTIVE_PROFILE_ID,
        "api_key_configured": bool(AI_API_KEY),
        "profiles": [
            {
                "id": str(profile.get("id") or ""),
                "name": str(profile.get("name") or ""),
                "base_url": str(profile.get("base_url") or ""),
                "model": str(profile.get("model") or ""),
                "configured": bool(profile.get("base_url") and profile.get("api_key") and profile.get("model")),
                "api_key_configured": bool(profile.get("api_key")),
            }
            for profile in AI_PROFILES
        ],
    }


_apply_ai_settings(_cfg)


def reload_ai_settings() -> dict:
    """Reload AI settings from config.yaml without restarting the backend."""
    document = _load_yaml()
    return _apply_ai_settings(document)


def save_ai_settings(
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    profiles: list[dict[str, Any]] | None = None,
    active_profile_id: str = "",
) -> dict:
    """Persist AI settings while preserving config.yaml comments and formatting."""
    from ruamel.yaml import YAML

    parser = YAML()
    parser.preserve_quotes = True
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as stream:
            document = parser.load(stream) or {}
    except FileNotFoundError:
        document = {}

    existing_profiles = {
        str(profile.get("id") or ""): profile
        for profile in _resolve_ai_settings(document)[0]
    }
    normalized_profiles: list[dict[str, Any]] = []
    if profiles is not None:
        seen_ids: set[str] = set()
        for index, raw_profile in enumerate(profiles):
            profile = _normalize_ai_profile(raw_profile, index)
            if not profile:
                continue
            if not profile["api_key"]:
                profile["api_key"] = str(existing_profiles.get(profile["id"], {}).get("api_key") or "")
            base_id = profile["id"] or f"ai_profile_{index + 1}"
            profile_id = base_id
            suffix = 2
            while profile_id in seen_ids:
                profile_id = f"{base_id}_{suffix}"
                suffix += 1
            profile["id"] = profile_id
            seen_ids.add(profile_id)
            normalized_profiles.append(profile)
    else:
        normalized_profiles.append({
            "id": active_profile_id or "default",
            "name": model or "默认配置",
            "base_url": str(base_url or "").strip().rstrip("/"),
            "api_key": str(api_key or "").strip(),
            "model": str(model or "").strip(),
        })

    if not normalized_profiles:
        normalized_profiles.append({
            "id": "default",
            "name": "默认配置",
            "base_url": "",
            "api_key": "",
            "model": "",
        })

    selected_id = str(active_profile_id or "").strip()
    active = next((item for item in normalized_profiles if item.get("id") == selected_id), None)
    if active is None:
        active = next((item for item in normalized_profiles if item.get("base_url") and item.get("api_key") and item.get("model")), None)
    if active is None:
        active = normalized_profiles[0]
    selected_id = str(active.get("id") or "")

    document["ai_profiles"] = normalized_profiles
    document["ai_active_profile_id"] = selected_id
    # Keep legacy keys in sync with the active profile so older deployments and
    # manual edits continue to work.
    document["ai_base_url"] = str(active.get("base_url") or "").strip().rstrip("/")
    document["ai_api_key"] = str(active.get("api_key") or "").strip()
    document["ai_model"] = str(active.get("model") or "").strip()
    temp_path = _CONFIG_PATH + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as stream:
            parser.dump(document, stream)
        os.replace(temp_path, _CONFIG_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return _apply_ai_settings(document)

# Hook/API request concurrency. QueryDB uses its own per-DB pool in wechat_api.
# Local Hook keeps the conservative single-call path by default; remote
# Hook/Protocol can handle parallel calls.
HOOK_API_CONCURRENCY = int(_cfg.get("hook_api_concurrency", 1 if IS_LOCAL_HOOK else 10))
if HOOK_API_CONCURRENCY < 1:
    print(f"[CONFIG] ⚠ invalid hook_api_concurrency={HOOK_API_CONCURRENCY!r}, using 1", flush=True)
    HOOK_API_CONCURRENCY = 1

# DLL agent WebSocket. The DLL connects outward to this backend, usually through
# TLS termination as wss://<public-ip>:<client_wss_port><agent_ws_path>.
AGENT_WS_ENABLED = _to_bool(_cfg.get("agent_ws_enabled", False), False)
AGENT_WS_PATH = str(_cfg.get("agent_ws_path", "/agent") or "/agent")
if not AGENT_WS_PATH.startswith("/"):
    AGENT_WS_PATH = "/" + AGENT_WS_PATH
CLIENT_WSS_PORT = int(_cfg.get("client_wss_port", 443))
CLIENT_WSS_SCHEME = str(_cfg.get("client_wss_scheme", "wss") or "wss").lower()
CLIENT_WSS_HOST = str(_cfg.get("client_wss_host", _cfg.get("ip", "127.0.0.1")) or "127.0.0.1")
AGENT_WS_REQUEST_TIMEOUT = float(_cfg.get("agent_ws_request_timeout", 30.0))

_client_wss_default_port = 443 if CLIENT_WSS_SCHEME == "wss" else 80
_client_wss_port_part = "" if CLIENT_WSS_PORT == _client_wss_default_port else f":{CLIENT_WSS_PORT}"
CLIENT_WSS_URL = f"{CLIENT_WSS_SCHEME}://{CLIENT_WSS_HOST}{_client_wss_port_part}{AGENT_WS_PATH}"

# Local Hook callbacks share the main backend listener. Remote Hook callbacks
# travel over CLIENT_WSS_URL and do not need a separately reachable HTTP URL.
CALLBACK_HOST = SERVER_HOST
CALLBACK_PORT = SERVER_PORT
CALLBACK_PATH = "/api/callback"
CALLBACK_URL = f"http://127.0.0.1:{SERVER_PORT}{CALLBACK_PATH}"
RECV_TYPE = int(_cfg.get("recvtype", _cfg.get("recv_type", 2)))
if RECV_TYPE not in (1, 2):
    print(f"[CONFIG] ⚠ invalid recv_type={RECV_TYPE!r}, using 2", flush=True)
    RECV_TYPE = 2

# Login flow behavior flags (used by backend/login_remote_hook.py)
RESTART_ON_BUTTON_LOGIN_FAIL = _to_bool(_cfg.get("restart_on_button_login_fail", True), True)
MAX_RESTARTS_AFTER_BUTTON_LOGIN_FAIL = int(_cfg.get("max_restarts_after_button_login_fail", 1))

# ─── Log loaded config ────────────────────────────────────────────

print(f"[CONFIG] wechat_mode={WECHAT_MODE}  mode={LOGIN_MODE}  host={HOOK_HOST}  "
      f"api_port={HOOK_PORT}  mgr_port={MGR_PORT}  "
      f"server={SERVER_HOST}:{SERVER_PORT}  "
      f"hook_api_concurrency={HOOK_API_CONCURRENCY}  "
      f"agent_ws={'on' if AGENT_WS_ENABLED else 'off'}  "
      f"client_wss={CLIENT_WSS_URL}  "
      f"recv_type={RECV_TYPE}  "
      f"ip={PUBLIC_IP}  RDV={RDV}  "
      f"ai={'on' if AI_BASE_URL and AI_API_KEY and AI_MODEL else 'off'}  "
      f"ai_model={AI_MODEL or '-'}  "
      f"restart_on_button_fail={RESTART_ON_BUTTON_LOGIN_FAIL}  "
      f"max_restart_button_fail={MAX_RESTARTS_AFTER_BUTTON_LOGIN_FAIL}", flush=True)
