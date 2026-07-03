"""Configuration for the WeChat backend server.

Reads from ../config.yaml and exposes settings for three modes:
  - local_hook       本地Hook (DLL注入本地微信)
  - remote_hook      远程客户端Hook (客户端DLL主动连接本后端WSS)
  - remote_protocol  远程协议 (服务器上的微信协议)
"""

import os
import sys
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

# Backend server — bind to 0.0.0.0 so remote Hook callbacks can reach us
SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(_cfg.get("server_port", 5000))

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

# Callback (use public IP/domain so remote client DLLs can reach us outbound)
CALLBACK_PORT = int(_cfg.get("callback_port", SERVER_PORT))
CALLBACK_PATH = str(_cfg.get("callback_path", "/api/callback"))
CALLBACK_URL = f"http://{PUBLIC_IP}:{CALLBACK_PORT}{CALLBACK_PATH}"
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
      f"server_port={SERVER_PORT}  callback={CALLBACK_URL}  "
      f"agent_ws={'on' if AGENT_WS_ENABLED else 'off'}  "
      f"client_wss={CLIENT_WSS_URL}  "
      f"recv_type={RECV_TYPE}  "
      f"ip={PUBLIC_IP}  RDV={RDV}  "
      f"restart_on_button_fail={RESTART_ON_BUTTON_LOGIN_FAIL}  "
      f"max_restart_button_fail={MAX_RESTARTS_AFTER_BUTTON_LOGIN_FAIL}", flush=True)
