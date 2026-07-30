"""Client for the PC WeChat 3.9.10.16 Go protocol service.

Login and protocol-session management live here so the FastAPI routes do not
mix Hook/DLL transport details with protocol-specific calls.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx

import config


SESSION_LIST_KEY = "admin@123"
_DEFAULT_TIMEOUT = 30.0
client = httpx.AsyncClient(base_url=config.HOOK_BASE_URL, timeout=_DEFAULT_TIMEOUT)
_req_id = 0


def safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text, "status_code": response.status_code}


def _preview(payload: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    return text if len(text) <= limit else text[:limit] + f"...(truncated,total_len={len(text)})"


def _candidate_base_urls() -> list[str]:
    primary = config.HOOK_BASE_URL.rstrip("/")
    parsed = urlparse(primary)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return [primary]
    scheme = parsed.scheme or "http"
    port = parsed.port or config.HOOK_PORT
    candidates = [
        primary,
        f"{scheme}://localhost:{port}",
        f"{scheme}://[::1]:{port}",
        f"{scheme}://127.0.0.1:{port}",
    ]
    result: list[str] = []
    for item in candidates:
        if item not in result:
            result.append(item)
    return result


def _is_route_not_found(data: dict[str, Any], path: str) -> bool:
    if not isinstance(data, dict):
        return False
    code = data.get("code") or data.get("status_code")
    msg = str(data.get("msg") or data.get("message") or data.get("error") or "").lower()
    route = str(data.get("route") or "").strip("/").lower()
    expected = path.strip("/").lower()
    return str(code) == "404" and ("route not found" in msg or route == expected)


async def close() -> None:
    await client.aclose()


async def _post(path: str, payload: dict[str, Any] | None = None, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    global _req_id
    _req_id += 1
    rid = _req_id
    payload = payload or {}
    last_data: dict[str, Any] = {}
    for index, base_url in enumerate(_candidate_base_urls()):
        started = time.time()
        url = f"{base_url}{path}"
        print(f"[PROTO #{rid}] -> POST {url} body={_preview(payload)}", flush=True)
        try:
            response = await client.post(url, json=payload, timeout=timeout)
        except Exception as exc:
            elapsed = int((time.time() - started) * 1000)
            print(f"[PROTO #{rid}] <- ERROR {type(exc).__name__}: {exc} time={elapsed}ms", flush=True)
            last_data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            continue
        elapsed = int((time.time() - started) * 1000)
        print(
            f"[PROTO #{rid}] <- status={response.status_code} time={elapsed}ms body={_preview(response.text)}",
            flush=True,
        )
        data = safe_json(response)
        if response.status_code >= 400 and "status_code" not in data:
            data["status_code"] = response.status_code
        last_data = data
        if _is_route_not_found(data, path) and index + 1 < len(_candidate_base_urls()):
            print(f"[PROTO #{rid}] route not found on {base_url}; trying next local protocol address", flush=True)
            continue
        return data
    return last_data


async def health() -> dict[str, Any]:
    try:
        response = await client.get("/health", timeout=5.0)
        data = safe_json(response)
        if response.status_code >= 400 and "status_code" not in data:
            data["status_code"] = response.status_code
        return data
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def get_session_list(not_started_after_seconds: int = 300) -> dict[str, Any]:
    return await _post(
        "/GetSessionList",
        {
            "key": SESSION_LIST_KEY,
            "not_started_after_seconds": int(not_started_after_seconds or 300),
        },
        timeout=10.0,
    )


async def start_wechat(
    *,
    rdv: str,
    callback_url: str = "",
    proxy_type: str = "",
    proxy_ip: str = "",
    proxy_port: str | int = "",
    proxy_usr: str = "",
    proxy_pwd: str = "",
) -> dict[str, Any]:
    return await _post(
        "/StartWechat",
        {
            "RDV": rdv,
            "CallBackURL": callback_url,
            "Proxy_Type": proxy_type,
            "Proxy_IP": proxy_ip,
            "Proxy_Port": str(proxy_port or ""),
            "Proxy_Usr": proxy_usr,
            "Proxy_Pwd": proxy_pwd,
        },
        timeout=35.0,
    )


async def get_login_qrcode(session_id: str) -> dict[str, Any]:
    return await _post(
        "/getloginqrcode",
        {"session_id": str(session_id or "").strip()},
        timeout=30.0,
    )


async def get_login_status(session_id: str) -> dict[str, Any]:
    return await _post(
        "/getloginstatus",
        {"session_id": str(session_id or "").strip()},
        timeout=10.0,
    )


async def push_login_url(session_id: str, wxid: str) -> dict[str, Any]:
    target = str(wxid or "").strip()
    return await _post(
        "/pushloginurl",
        {"session_id": str(session_id or "").strip(), "wxid": target, "userName": target},
        timeout=25.0,
    )


async def get_profile(session_id: str) -> dict[str, Any]:
    return await _post(
        "/getprofile",
        {"session_id": str(session_id or "").strip()},
        timeout=20.0,
    )


async def send_text(session_id: str, username: str, content: str) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    username = str(username or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id 不能为空"}
    if not username:
        return {"ok": False, "error": "username 不能为空"}
    return await _post(
        "/newsendmsg",
        {
            "session_id": session_id,
            "username": username,
            "content": str(content or ""),
        },
        timeout=30.0,
    )


async def terminate_session(session_id: str) -> dict[str, Any]:
    return await _post(
        "/TerminateThisWeChat",
        {"session_id": str(session_id or "").strip()},
        timeout=10.0,
    )
