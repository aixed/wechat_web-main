"""WeChat API client - proxies requests to the Hook or Protocol server.

Supports three modes via config.yaml:
  - local_hook       本地Hook DLL endpoints like /SendTextMsg
  - remote_hook      远程客户端Hook (client DLL connects to this backend via /agent)
  - remote_protocol  远程协议 endpoints like /newsendmsg/
"""

import httpx
import asyncio
import contextvars
from contextlib import contextmanager
import time
import sys
import os
import hashlib
import zlib
import json as _json
from config import (
    AGENT_WS_ENABLED,
    AGENT_WS_REQUEST_TIMEOUT,
    HOOK_BASE_URL,
    HOOK_API_CONCURRENCY,
    IS_HOOK,
    IS_PROTOCOL,
    LOGIN_MODE,
)
from agent_ws import agent_manager

from config import IS_LOCAL_HOOK

# Remote servers need longer timeouts due to network latency
_DEFAULT_TIMEOUT = 10.0 if IS_LOCAL_HOOK else 30.0
client = httpx.AsyncClient(base_url=HOOK_BASE_URL, timeout=_DEFAULT_TIMEOUT)

# Request counter
_req_id = 0
_CURRENT_AGENT_ID: contextvars.ContextVar[str] = contextvars.ContextVar("wechat_agent_id", default="")
_query_db_locks: dict[str, asyncio.Lock] = {}


class _AgentHookGate:
    """Per-agent reader/writer gate for fragile Hook routes.

    Normal Hook APIs enter as shared readers. QueryDB enters as an exclusive
    writer, so once a Session-table query is requested no new Hook calls can
    start for that same WeChat process until QueryDB returns.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    async def acquire_shared(self) -> None:
        async with self._cond:
            while self._writer or self._writers_waiting > 0:
                await self._cond.wait()
            self._readers += 1

    async def release_shared(self) -> None:
        async with self._cond:
            self._readers = max(0, self._readers - 1)
            if self._readers == 0:
                self._cond.notify_all()

    async def acquire_exclusive(self) -> None:
        async with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers > 0:
                    await self._cond.wait()
                self._writer = True
            finally:
                self._writers_waiting = max(0, self._writers_waiting - 1)

    async def release_exclusive(self) -> None:
        async with self._cond:
            self._writer = False
            self._cond.notify_all()


_agent_hook_gates: dict[str, _AgentHookGate] = {}


def _agent_gate_key(agent_id: str) -> str:
    return str(agent_id or "__default__")


def _agent_hook_gate(agent_id: str) -> _AgentHookGate:
    key = _agent_gate_key(agent_id)
    gate = _agent_hook_gates.get(key)
    if gate is None:
        gate = _AgentHookGate()
        _agent_hook_gates[key] = gate
    return gate


def _is_query_db_endpoint(endpoint: str) -> bool:
    return str(endpoint or "").strip().strip("/").split("?", 1)[0].lower() == "querydb"


@contextmanager
def use_agent(agent_id: str | None):
    token = _CURRENT_AGENT_ID.set(str(agent_id or ""))
    try:
        yield
    finally:
        _CURRENT_AGENT_ID.reset(token)

# Concurrency control:
#  - Local Hook defaults to 1 (serialize everything — DLL injection can be fragile)
#  - Remote Hook/Protocol defaults to 10 so slow calls don't block profile/avatar/send APIs
if HOOK_API_CONCURRENCY <= 1:
    _hook_lock: asyncio.Lock | asyncio.Semaphore = asyncio.Lock()
else:
    _hook_lock = asyncio.Semaphore(HOOK_API_CONCURRENCY)

# ─── Circuit breaker ───────────────────────────────────────────────
# After consecutive failures, back off to avoid hammering a dying Hook.
_consecutive_failures = 0
_last_failure_time = 0.0
_BACKOFF_SECONDS = [0, 2, 5, 10, 30]  # indexed by min(failures, len-1)

_MAIN_LOG_PATH = os.path.join(os.path.dirname(__file__), "main.log")
_MAIN_LOG_LOCK = asyncio.Lock()


def _ts() -> str:
    """Timestamp with milliseconds."""
    t = time.time()
    base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    ms = int((t - int(t)) * 1000)
    return f"{base}.{ms:03d}"


def _pretty_json(obj) -> str:
    try:
        return _json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def _truncate(text: str, limit: int = 8000) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...(truncated,total_len={len(s)})"


def _indent_multiline(text: str, prefix: str) -> str:
    """Indent all lines after the first with spaces equal to prefix length."""
    if text is None:
        text = ""
    lines = str(text).splitlines()
    if not lines:
        return prefix
    if len(lines) == 1:
        return prefix + lines[0]
    pad = " " * len(prefix)
    return prefix + ("\n" + pad).join(lines)


async def _append_main_log(block: str) -> None:
    """Append a log block to backend/main.log (best-effort)."""
    try:
        async with _MAIN_LOG_LOCK:
            with open(_MAIN_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(block)
                if not block.endswith("\n"):
                    f.write("\n")
    except Exception:
        # Never break API calls because of logging.
        pass


def _log(msg: str):
    """Flush-safe logging."""
    print(msg, flush=True)


def _read_file_hex(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return f.read().hex()


def _attach_file_data(body: dict, path_key: str, file_data: str | None = None) -> dict:
    """Attach hex fileData for Hook file-send APIs while keeping the path field."""
    body["fileData"] = file_data or ""
    if body["fileData"]:
        return body

    filepath = str(body.get(path_key) or "")
    if not filepath or filepath.lower().startswith(("http://", "https://")):
        return body

    try:
        if os.path.isfile(filepath):
            body["fileData"] = _read_file_hex(filepath)
        else:
            _log(f"[API] fileData skipped: file not found: {filepath}")
    except Exception as e:
        _log(f"[API] fileData read failed: {filepath}: {type(e).__name__}: {e}")
    return body


def _scrub_payload_for_log(obj):
    if isinstance(obj, dict):
        scrubbed = {}
        for key, value in obj.items():
            if str(key).lower() == "filedata":
                scrubbed[key] = f"<hex len={len(str(value or ''))}>"
            else:
                scrubbed[key] = _scrub_payload_for_log(value)
        return scrubbed
    if isinstance(obj, list):
        return [_scrub_payload_for_log(item) for item in obj]
    return obj


def _console_payload(obj, limit: int = 1200) -> str:
    return _truncate(_pretty_json(obj), limit).replace("\n", " ")


def _circuit_open() -> bool:
    """Check if circuit breaker is open (should skip request)."""
    if _consecutive_failures == 0:
        return False
    backoff = _BACKOFF_SECONDS[min(_consecutive_failures, len(_BACKOFF_SECONDS) - 1)]
    elapsed = time.time() - _last_failure_time
    if elapsed < backoff:
        return True
    return False


def safe_json(response: httpx.Response) -> dict:
    """Parse JSON response, return empty dict if response is not valid JSON."""
    try:
        return response.json()
    except Exception:
        return {"raw": response.text, "status_code": response.status_code}


async def _post(endpoint: str, json: dict = None, timeout: float = None,
                *, bypass_circuit_breaker: bool = False) -> httpx.Response:
    """Logged POST wrapper with mode-aware concurrency control."""
    global _req_id, _consecutive_failures, _last_failure_time

    use_agent_ws = AGENT_WS_ENABLED and IS_HOOK
    agent_id = _CURRENT_AGENT_ID.get() or ""
    full_url = f"agent-ws://{agent_id or 'active'}/{endpoint.lstrip('/')}" if use_agent_ws else f"{HOOK_BASE_URL}{endpoint}"
    hook_gate = _agent_hook_gate(agent_id) if IS_HOOK else None
    query_db_call = _is_query_db_endpoint(endpoint)

    if not bypass_circuit_breaker and _circuit_open():
        backoff = _BACKOFF_SECONDS[min(_consecutive_failures, len(_BACKOFF_SECONDS) - 1)]
        log_json = _scrub_payload_for_log(json or {})
        _log(f"[API] ⏸ Circuit breaker OPEN — skipping {full_url} (failures={_consecutive_failures}, backoff={backoff}s)")
        await _append_main_log(
            f"[{_ts()}]POST {full_url}\n"
            f"          agent_id={agent_id or '-'} endpoint={endpoint}\n"
            f"{_indent_multiline(_truncate(_pretty_json(log_json)), '          << ')}\n"
            f"{_indent_multiline(f'ERROR Circuit breaker open (failures={_consecutive_failures}, backoff={backoff}s)', '          >> ')}\n"
            f"          error=circuit_open\n"
        )
        raise ConnectionError(f"Circuit breaker open after {_consecutive_failures} failures")

    if hook_gate:
        if query_db_call:
            _log(f"[API-GATE] QueryDB waiting for exclusive hook access agent={agent_id or '-'}")
            await hook_gate.acquire_exclusive()
            _log(f"[API-GATE] QueryDB acquired exclusive hook access agent={agent_id or '-'}")
        else:
            await hook_gate.acquire_shared()

    try:
        async with _hook_lock:
            _req_id += 1
            rid = _req_id
            log_json = _scrub_payload_for_log(json or {})
            transport = "AGENT" if use_agent_ws else "POST"
            _log(f"[API #{rid}] → {transport} {full_url} agent={agent_id or '-'} body={_console_payload(log_json)}")
            t0 = time.time()
            try:
                if use_agent_ws:
                    route = endpoint.strip("/")
                    if query_db_call:
                        _log("[API] QueryDB via /agent with clean body (no body.agent_id injection)")
                    agent_response = await agent_manager.request(
                        route,
                        json or {},
                        timeout=timeout or AGENT_WS_REQUEST_TIMEOUT,
                        agent_id=_CURRENT_AGENT_ID.get() or None,
                        inject_agent_id=not query_db_call,
                        include_method=not query_db_call,
                    )
                    r = httpx.Response(
                        status_code=agent_response.status,
                        content=agent_response.body,
                        headers={"content-type": agent_response.content_type},
                        request=httpx.Request("POST", f"http://agent.local/{route}"),
                    )
                else:
                    r = await client.post(endpoint, json=json, timeout=timeout)
                ms = int((time.time() - t0) * 1000)
                body_preview = _truncate(r.text if r.text else "(empty)", 1200).replace("\n", " ")
                _log(f"[API #{rid}] ← {transport} {full_url} status={r.status_code} time={ms}ms len={len(r.text)} body={body_preview}")
                await _append_main_log(
                    f"[{_ts()}]POST {full_url}\n"
                    f"          request_id={rid} agent_id={agent_id or '-'} endpoint={endpoint}\n"
                    f"{_indent_multiline(_truncate(_pretty_json(log_json)), '          << ')}\n"
                    f"{_indent_multiline(_truncate(r.text), '          >> ')}\n"
                    f"          time_used={ms}ms\n"
                )
                # Success — reset circuit breaker
                if _consecutive_failures > 0:
                    _log(f"[API] ✓ Circuit breaker RESET (was at {_consecutive_failures} failures)")
                _consecutive_failures = 0
                return r
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                _log(f"[API #{rid}] ✗ {transport} {full_url} agent={agent_id or '-'} ERROR time={ms}ms {type(e).__name__}: {e}")
                _consecutive_failures += 1
                _last_failure_time = time.time()
                backoff = _BACKOFF_SECONDS[min(_consecutive_failures, len(_BACKOFF_SECONDS) - 1)]
                _log(f"[API] ⚠ Consecutive failures: {_consecutive_failures} — next backoff: {backoff}s")
                await _append_main_log(
                    f"[{_ts()}]POST {full_url}\n"
                    f"          request_id={rid} agent_id={agent_id or '-'} endpoint={endpoint}\n"
                    f"{_indent_multiline(_truncate(_pretty_json(log_json)), '          << ')}\n"
                    f"{_indent_multiline(_truncate(f'{type(e).__name__}: {e}', 2000), '          >> ')}\n"
                    f"          error={type(e).__name__} time_used={ms}ms\n"
                )
                raise
    finally:
        if hook_gate:
            if query_db_call:
                await hook_gate.release_exclusive()
                _log(f"[API-GATE] QueryDB released exclusive hook access agent={agent_id or '-'}")
            else:
                await hook_gate.release_shared()


# ═══════════════════════════════════════════════════════════════════
# Endpoint / parameter mapping for local vs remote
# ═══════════════════════════════════════════════════════════════════
#
# Local  (PC微信接口):   /PascalCase       params: {wxid, msg, gid, ...}
# Remote (PC微信协议):   /lowercase/       params: {userName, content, ...}
# ═══════════════════════════════════════════════════════════════════


# ─── Self / Contacts ───────────────────────────────────────────────

async def get_self_info() -> dict:
    """Get current logged-in user info."""
    if IS_HOOK:
        r = await _post("/GetSelfLoginInfo", json={})
    else:
        r = await _post("/getprofile/", json={})
    return safe_json(r)


async def is_login_status() -> dict:
    """Return WeChat login status before any initialization-heavy Hook calls."""
    if IS_HOOK:
        r = await _post("/IsLoginStatus", json={}, timeout=5.0, bypass_circuit_breaker=True)
        return safe_json(r)
    return {"msg": "登陆完成！", "onlinestatus": "3"}


async def init_contact() -> dict:
    """Initialize contact list (must call before GetFriendAndChatRoomList).

    This can take a long time on remote servers — use 60s timeout.
    """
    if IS_HOOK:
        r = await _post("/InitContact", json={}, timeout=60.0)
    else:
        r = await _post("/initcontact/", json={"contactSeq": 0, "chatRoomContactSeq": 0}, timeout=60.0)
    return safe_json(r)


async def get_friend_and_chatroom_list(list_type: str | int = "1") -> dict:
    """Get all friends and chatrooms."""
    if IS_HOOK:
        r = await _post("/GetFriendAndChatRoomList", json={"type": str(list_type)})
        return safe_json(r)
    else:
        # Remote: initcontact returns the contact list directly
        r = await _post("/initcontact/", json={"contactSeq": 0, "chatRoomContactSeq": 0})
        return safe_json(r)


async def batch_get_contact_brief_info(wxid_list: str) -> dict:
    """Batch get contact brief info (max 100)."""
    if IS_HOOK:
        r = await _post("/BatchGetContactBriefInfo", json={"wxidlist": wxid_list})
    else:
        # Remote expects array of userNames
        names = [w.strip() for w in wxid_list.split(",") if w.strip()]
        r = await _post("/batchgetcontactbriefinfo/", json={"userNames": names})
    return safe_json(r)


async def get_contact(wxids: list[str]) -> dict:
    """Get full contact profiles for one or more wxids.

    Hook /GetContact is only safe up to 100 wxids per request.
    """
    names = [str(w).strip() for w in (wxids or []) if str(w).strip()]
    if not names:
        return {"contacts": []}
    if len(names) > 100:
        combined: list[dict] = []
        last_extra: dict = {}
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            data = await get_contact(chunk)
            if isinstance(data, dict):
                contacts = data.get("contacts")
                if isinstance(contacts, list):
                    combined.extend([c for c in contacts if isinstance(c, dict)])
                elif contacts:
                    combined.append(contacts)
                last_extra = {k: v for k, v in data.items() if k != "contacts"}
            await asyncio.sleep(0.02)
        return {**last_extra, "contacts": combined}
    if IS_HOOK:
        r = await _post("/GetContact", json={"wxids": names})
    else:
        r = await _post("/getcontact/", json={"userNames": names, "chatRoomUserName": ""})
    return safe_json(r)


async def get_openim_contact(wxid: str, gid: str = "") -> dict:
    """Get Enterprise WeChat/OpenIM contact details from Hook."""
    name = str(wxid or "").strip()
    if not name:
        return {}
    if not IS_HOOK:
        return {"error": "GetOpenIMContact is only supported in hook mode"}
    r = await _post("/GetOpenIMContact", json={"gid": str(gid or ""), "wxid": name})
    return safe_json(r)


async def get_contact_label_list() -> dict:
    """Get contact label id/name list."""
    if IS_HOOK:
        r = await _post("/GetContactLabelList", json={})
    else:
        r = await _post("/getcontactlabellist", json={})
    return safe_json(r)


async def get_head_img(wxid: str) -> dict:
    """Get contact avatar URL."""
    if IS_HOOK:
        r = await _post("/GetHeadIMG", json={"wxid": wxid})
    else:
        # Remote: use getcontact to get avatar info
        r = await _post("/getcontact/", json={"userNames": [wxid], "chatRoomUserName": ""})
    return safe_json(r)


async def get_friend_detail_info(wxid_or_gid: str) -> dict:
    """Get detailed friend/chatroom info from network."""
    if IS_HOOK:
        r = await _post("/GetFriendOrChatroomDetailInfo", json={"wxidorgid": wxid_or_gid})
    else:
        r = await _post("/getcontact/", json={"userNames": [wxid_or_gid], "chatRoomUserName": ""})
    return safe_json(r)


async def get_current_session() -> dict:
    """Get current session (conversation) list."""
    if IS_HOOK:
        # Remote hook may occasionally hang/slow on this endpoint; keep it bounded.
        r = await _post("/GetCurrentSession", json={}, timeout=20.0 if IS_LOCAL_HOOK else 25.0)
        return safe_json(r)
    else:
        # Remote doesn't have a direct session list endpoint.
        # Return empty structure; sessions will be built from contacts + callbacks.
        _log("[API] Remote mode: GetCurrentSession not available, returning empty")
        return {"data": []}


# ─── Send Messages ─────────────────────────────────────────────────

async def send_text(wxid: str, msg: str) -> dict:
    """Send text message."""
    if IS_HOOK:
        r = await _post("/SendTextMsg", json={"wxid": wxid, "msg": msg})
    else:
        r = await _post("/newsendmsg/", json={
            "userName": wxid, "content": msg, "msgType": 1
        })
    return safe_json(r)


async def send_text_no_src(wxidorgid: str, msg: str) -> dict:
    """Send text through the lower-level no-source Hook endpoint."""
    if IS_HOOK:
        r = await _post("/SendTextMsg_NoSrc", json={"wxidorgid": wxidorgid, "msg": msg})
        return safe_json(r)
    return await send_text(wxidorgid, msg)


async def send_image(wxid: str, picpath: str, diyfilename: str = "", file_data: str | None = None) -> dict:
    """Send image (local path, URL, or hex fileData)."""
    if IS_HOOK:
        body = {"wxid": wxid, "picpath": picpath}
        if diyfilename:
            body["diyfilename"] = diyfilename
        body = _attach_file_data(body, "picpath", file_data)
        r = await _post("/SendPicMsg", json=body, timeout=90.0 if IS_LOCAL_HOOK else 180.0)
    else:
        # Remote: uploadmsgimg requires CDN pre-upload params.
        # For simple cases, we try sending as-is; the server handles upload.
        r = await _post("/uploadmsgimg/", json={
            "userName": wxid,
            "picpath": picpath,
        })
    return safe_json(r)


def _first_nested_dict(data: dict, *path: str) -> dict:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key, {})
    return cur if isinstance(cur, dict) else {}


def _cdn_upload_fields(raw: dict, filepath: str, blob: bytes, aeskey: str) -> dict:
    data = _first_nested_dict(raw, "data", "data") or _first_nested_dict(raw, "data") or raw
    fileid = (
        data.get("fileid")
        or data.get("file_id")
        or data.get("fileId")
        or data.get("fileID")
        or ""
    )
    authkey = data.get("authkey") or data.get("aeskey") or aeskey
    filemd5 = data.get("filemd5") or data.get("md5") or data.get("rawfilemd5") or hashlib.md5(blob).hexdigest()
    filesize = data.get("filesize") or data.get("fileSize") or data.get("rawtotalsize") or len(blob)
    filecrc32 = data.get("filecrc32") or data.get("filecrc") or data.get("crc32") or (zlib.crc32(blob) & 0xffffffff)
    return {
        "fileid": str(fileid),
        "authkey": str(authkey),
        "filemd5": str(filemd5),
        "filesize": str(filesize),
        "filecrc32": str(filecrc32),
        "rawmidimgsize": str(data.get("rawmidimgsize") or data.get("rawtotalsize") or filesize),
        "rawthumbsize": str(data.get("rawthumbsize") or ""),
        "thumbheight": str(data.get("thumbheight") or ""),
        "thumbwidth": str(data.get("thumbwidth") or ""),
        "filepath": filepath,
        "raw": raw,
    }


async def cdn_upload_image(filepath: str, user_name: str = "filehelper", file_data: str | None = None) -> dict:
    """Upload one image to CDN and return fields usable by SendImgMsg_NoSrc."""
    if not IS_HOOK:
        return {"error": "CDN upload is only supported in hook mode"}

    blob = bytes.fromhex(file_data) if file_data else b""
    if not blob:
        with open(filepath, "rb") as f:
            blob = f.read()
        file_data = blob.hex()

    aeskey = os.urandom(16).hex()
    body = {
        "fileType": 2,
        "aeskey": aeskey,
        "filePath": filepath,
        "fileData": file_data,
        "userName": user_name or "filehelper",
        "chatType": 1 if str(user_name).endswith("@chatroom") else 0,
    }
    r = await _post("/upload", json=body, timeout=180.0 if IS_LOCAL_HOOK else 300.0)
    raw = safe_json(r)
    fields = _cdn_upload_fields(raw, filepath, blob, aeskey)
    if not fields.get("fileid"):
        fields["error"] = raw.get("retmsg") or raw.get("error") or "CDN upload did not return fileid"
    return fields


async def send_image_no_src(wxidorgid: str, cdn_fields: dict) -> dict:
    """Send an already-uploaded CDN image through the lower-level no-source endpoint."""
    if not IS_HOOK:
        return {"error": "SendImgMsg_NoSrc is only supported in hook mode"}
    body = {
        "wxidorgid": wxidorgid,
        "fileid": str(cdn_fields.get("fileid", "")),
        "authkey": str(cdn_fields.get("authkey", "")),
        "filemd5": str(cdn_fields.get("filemd5", "")),
        "filesize": str(cdn_fields.get("filesize", "")),
        "filecrc32": str(cdn_fields.get("filecrc32", "")),
    }
    for key in ("rawmidimgsize", "rawthumbsize", "thumbheight", "thumbwidth"):
        value = cdn_fields.get(key)
        if value not in (None, ""):
            body[key] = str(value)
    r = await _post("/SendImgMsg_NoSrc", json=body, timeout=90.0 if IS_LOCAL_HOOK else 180.0)
    return safe_json(r)


async def send_file(wxid: str, filepath: str, file_data: str | None = None) -> dict:
    """Send file."""
    if IS_HOOK:
        body = _attach_file_data({"wxid": wxid, "filepath": filepath}, "filepath", file_data)
        r = await _post("/SendFileMsg", json=body, timeout=180.0 if IS_LOCAL_HOOK else 300.0)
    else:
        r = await _post("/sendfileuploadmsg/", json={
            "userName": wxid,
            "filepath": filepath,
        })
    return safe_json(r)


async def send_video(wxid: str, videopath: str, file_data: str | None = None) -> dict:
    """Send video."""
    if IS_HOOK:
        body = _attach_file_data({"wxid": wxid, "videopath": videopath}, "videopath", file_data)
        r = await _post("/SendVideoMsg", json=body, timeout=300.0 if IS_LOCAL_HOOK else 600.0)
    else:
        r = await _post("/uploadvideo/", json={
            "userName": wxid,
            "videopath": videopath,
        })
    return safe_json(r)


async def send_voice(wxid: str, voice_file: str, time_ms: int, file_data: str | None = None) -> dict:
    """Send voice (SILK format)."""
    if IS_HOOK:
        body = _attach_file_data({
            "wxid": wxid, "voice_file": voice_file, "time_ms": time_ms
        }, "voice_file", file_data)
        r = await _post("/SendVoiceMsg", json=body, timeout=120.0 if IS_LOCAL_HOOK else 180.0)
        return safe_json(r)
    else:
        _log("[API] Remote mode: SendVoiceMsg not directly supported")
        return {"error": "SendVoiceMsg not supported in remote mode"}


async def send_gif(wxid: str, gifpath: str, file_data: str | None = None) -> dict:
    """Send GIF."""
    if IS_HOOK:
        body = _attach_file_data({"wxid": wxid, "gifpath": gifpath}, "gifpath", file_data)
        r = await _post("/SendGIFMsg", json=body, timeout=120.0 if IS_LOCAL_HOOK else 180.0)
    else:
        r = await _post("/sendemoji/", json={
            "userName": wxid,
            "gifpath": gifpath,
        })
    return safe_json(r)


async def send_quote(towxid: str, title: str, svrid: str,
                     fromusr: str, displayname: str, chatusr: str) -> dict:
    """Send quote/reply message."""
    if IS_HOOK:
        r = await _post("/SendQuoteMsg", json={
            "towxid": towxid, "title": title, "svrid": svrid,
            "fromusr": fromusr, "displayname": displayname, "chatusr": chatusr
        })
    else:
        # Remote: use sendappmsg with quote XML
        r = await _post("/sendappmsg/", json={
            "userName": towxid,
            "content": title,
            "msgType": 57,
        })
    return safe_json(r)


async def send_at(gid: str, wxidlist: str, nicknamelist: str, msg: str) -> dict:
    """Send @mention message in group."""
    if IS_HOOK:
        r = await _post("/SendAtMsg", json={
            "gid": gid, "wxidlist": wxidlist,
            "nicknamelist": nicknamelist, "msg": msg
        })
    else:
        # Remote: use newsendmsg with @mention syntax in content
        r = await _post("/newsendmsg/", json={
            "userName": gid,
            "content": msg,
            "msgType": 1,
        })
    return safe_json(r)


async def send_pat(wxid: str, gid: str = "") -> dict:
    """Send pat message."""
    if IS_HOOK:
        r = await _post("/SendPatMsg", json={"wxid": wxid, "gid": gid})
    else:
        r = await _post("/sendpat/", json={
            "userName": wxid, "chatRoomUserName": gid
        })
    return safe_json(r)


async def send_card(towxid: str, fromwxid: str, nickname: str) -> dict:
    """Forward contact card."""
    if IS_HOOK:
        r = await _post("/SendCardMsg", json={
            "towxid": towxid, "fromwxid": fromwxid, "nickname": nickname
        })
        return safe_json(r)
    else:
        _log("[API] Remote mode: SendCardMsg not directly supported")
        return {"error": "SendCardMsg not supported in remote mode"}


async def send_location(wxid: str, msg: str) -> dict:
    """Send location message (msg is XML)."""
    if IS_HOOK:
        r = await _post("/SendLocationMsg", json={"wxid": wxid, "msg": msg})
        return safe_json(r)
    else:
        _log("[API] Remote mode: SendLocationMsg not directly supported")
        return {"error": "SendLocationMsg not supported in remote mode"}


# ─── Message Management ────────────────────────────────────────────

async def revoke_msg(msg_svrid: int, to_wxid: str) -> dict:
    """Revoke a sent message (within 2 minutes)."""
    if IS_HOOK:
        r = await _post("/RevokeMsg", json={
            "msg_svrid": msg_svrid, "to_wxid": to_wxid
        })
    else:
        r = await _post("/revokemsg/", json={
            "toUserName": to_wxid, "msgSvrId": msg_svrid
        })
    return safe_json(r)


async def voice_to_text(voice_hex: str) -> dict:
    """Convert voice to text."""
    if IS_HOOK:
        r = await _post("/Voice2Text", json={"voice_hex": voice_hex})
        return safe_json(r)
    else:
        _log("[API] Remote mode: Voice2Text not directly supported")
        return {"error": "Voice2Text not supported in remote mode"}


async def get_gif_url(msg_xml: str) -> dict:
    """Get GIF download URL from msg XML."""
    if IS_HOOK:
        r = await _post("/GetGIFURL", json={"msg_xml": msg_xml})
        return safe_json(r)
    else:
        _log("[API] Remote mode: GetGIFURL not directly supported")
        return {"error": "GetGIFURL not supported in remote mode"}


async def get_unread_msg_num() -> dict:
    """Get total unread message count."""
    if IS_HOOK:
        r = await _post("/GetUnReadMsgNum", json={})
        return safe_json(r)
    else:
        _log("[API] Remote mode: GetUnReadMsgNum not available")
        return {"count": 0}


async def mark_as_read(gid_or_wxid: str) -> dict:
    """Mark a session as read."""
    if IS_HOOK:
        r = await _post("/MarkAsReadSession", json={"gidorwxid": gid_or_wxid})
        return safe_json(r)
    else:
        _log("[API] Remote mode: MarkAsReadSession not available")
        return {}


async def mark_as_unread(gid_or_wxid: str) -> dict:
    """Mark a session as unread."""
    if IS_HOOK:
        r = await _post("/MarkAsNoReadSession", json={"gidorwxid": gid_or_wxid})
        return safe_json(r)
    else:
        _log("[API] Remote mode: MarkAsNoReadSession not available")
        return {}


async def sticky_chat(gid_or_wxid: str) -> dict:
    """Pin a chat to the top of the session list."""
    if IS_HOOK:
        r = await _post("/StickyChat", json={"gidorwxid": gid_or_wxid})
        return safe_json(r)
    else:
        _log("[API] Remote mode: StickyChat not available")
        return {}


async def unpin_chat(gid_or_wxid: str) -> dict:
    """Unpin a chat from the top of the session list."""
    if IS_HOOK:
        r = await _post("/UnpinChat", json={"gidorwxid": gid_or_wxid})
        return safe_json(r)
    else:
        _log("[API] Remote mode: UnpinChat not available")
        return {}


async def turn_on_do_not_disturb(gid_or_wxid: str) -> dict:
    """Enable do-not-disturb for a session."""
    if IS_HOOK:
        r = await _post("/TurnOnDoNotDisturb", json={"gidorwxid": gid_or_wxid})
        return safe_json(r)
    else:
        _log("[API] Remote mode: TurnOnDoNotDisturb not available")
        return {}


async def turn_off_do_not_disturb(gid_or_wxid: str) -> dict:
    """Disable do-not-disturb for a session."""
    if IS_HOOK:
        r = await _post("/TurnOffDoNotDisturb", json={"gidorwxid": gid_or_wxid})
        return safe_json(r)
    else:
        _log("[API] Remote mode: TurnOffDoNotDisturb not available")
        return {}


# ─── Database Query ────────────────────────────────────────────────

async def query_db(dbname: str, sql: str, timeout: float | None = None) -> dict:
    """Query WeChat local SQLite database.

    The Hook QueryDB route is not safe for concurrent calls on the same
    WeChat process. Serialize per agent to avoid crashing the client when
    history, session, and sticker queries overlap.
    """
    if IS_HOOK:
        # Keep QueryDB bounded; remote servers sometimes stall.
        if timeout is None:
            timeout = 10.0 if IS_LOCAL_HOOK else 15.0
        agent_key = _CURRENT_AGENT_ID.get() or "__default__"
        lock = _query_db_locks.setdefault(agent_key, asyncio.Lock())
        async with lock:
            r = await _post("/QueryDB", json={"dbname": dbname, "sql": sql}, timeout=timeout)
        return safe_json(r)
    else:
        _log(f"[API] Remote mode: QueryDB not available (db={dbname})")
        return {"data": []}


def _decompress_content(hex_str: str) -> str:
    """Try to extract readable content from CompressContent hex data.
    WeChat 4.x stores type 49 message XML in CompressContent as LZ4-compressed data.
    Format: first 4 bytes = uncompressed size (little-endian), rest = LZ4 block data."""
    if not hex_str:
        return ""
    try:
        import re
        import zlib
        raw = bytes.fromhex(hex_str)

        # 1. Try LZ4 decompression (most common for WeChat 4.x)
        #    Format: [4-byte uncompressed_size LE] [LZ4 compressed data]
        try:
            import lz4.block
            # Try with 4-byte size header (standard WeChat format)
            if len(raw) > 4:
                uncompressed_size = int.from_bytes(raw[:4], 'little')
                # Sanity check: uncompressed size should be reasonable (< 1MB)
                if 10 < uncompressed_size < 1_000_000:
                    try:
                        decompressed = lz4.block.decompress(raw[4:], uncompressed_size=uncompressed_size)
                        text = decompressed.decode('utf-8', errors='ignore')
                        if '<msg>' in text:
                            _log(f"[DECOMPRESS] LZ4 with 4-byte header succeeded, len={len(text)}")
                            return text
                    except Exception as e:
                        _log(f"[DECOMPRESS] LZ4 with header failed: {e}")

            # Try without header, various uncompressed sizes
            for usize in [0x10000, 0x8000, 0x20000]:
                try:
                    decompressed = lz4.block.decompress(raw, uncompressed_size=usize)
                    text = decompressed.decode('utf-8', errors='ignore')
                    if '<msg>' in text:
                        _log(f"[DECOMPRESS] LZ4 raw succeeded, usize={usize}, len={len(text)}")
                        return text
                except Exception:
                    continue
        except ImportError:
            _log("[DECOMPRESS] lz4 package not available!")

        # 2. Try zlib decompression (various wbits)
        for wbits in [15, -15, 31, 47]:
            try:
                decompressed = zlib.decompress(raw, wbits)
                text = decompressed.decode('utf-8', errors='ignore')
                if '<msg>' in text:
                    _log(f"[DECOMPRESS] zlib succeeded, wbits={wbits}")
                    return text
            except Exception:
                continue

        # 3. Last resort: check if raw bytes are directly readable UTF-8 XML
        try:
            text = raw.decode('utf-8', errors='strict')
            if '<msg>' in text and '</msg>' in text:
                start = text.find('<msg>')
                end = text.find('</msg>')
                if start >= 0 and end > start:
                    _log(f"[DECOMPRESS] Raw UTF-8 XML found")
                    return text[start:end + 6]
        except Exception:
            pass

        _log(f"[DECOMPRESS] All methods failed for {len(raw)} bytes")

    except Exception as e:
        _log(f"[DECOMPRESS] Error: {e}")
    return ""


def _read_varint(data: bytes, pos: int) -> tuple:
    """Read a protobuf varint from data at position pos. Returns (value, new_pos)."""
    val = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos


def _parse_bytes_extra_item(data: bytes, target_type: int) -> str:
    """Parse a BytesExtraItem submessage { int32 type=1; bytes value=2; }.
    Returns value as UTF-8 string if type == target_type."""
    pos = 0
    item_type = -1
    item_value = b""
    while pos < len(data):
        if pos >= len(data):
            break
        tag = data[pos]
        pos += 1
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            val, pos = _read_varint(data, pos)
            if field_num == 1:
                item_type = val
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            if field_num == 2:
                item_value = data[pos:pos + length]
            pos += length
        else:
            break  # unknown wire type

    if item_type == target_type and item_value:
        try:
            return item_value.decode('utf-8')
        except Exception:
            pass
    return ""


def _extract_sender_from_bytes_extra(hex_str: str) -> str:
    """Extract sender wxid from BytesExtra protobuf hex data.
    WeChat 4.x BytesExtra structure:
        message BytesExtra { int32 version=1; repeated Item items=3; }
        message Item { int32 type=1; bytes value=2; }
    Sender wxid is in the Item where type=1."""
    if not hex_str:
        return ""
    try:
        raw = bytes.fromhex(hex_str)
        pos = 0
        while pos < len(raw):
            tag = raw[pos]
            pos += 1
            field_num = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:  # varint
                _, pos = _read_varint(raw, pos)
            elif wire_type == 2:  # length-delimited
                length, pos = _read_varint(raw, pos)
                if field_num == 3:
                    # This is a repeated Item submessage
                    sub_data = raw[pos:pos + length]
                    sender = _parse_bytes_extra_item(sub_data, target_type=1)
                    if sender and not sender.endswith("@chatroom"):
                        return sender
                pos += length
            else:
                break

    except Exception as e:
        _log(f"[BYTES_EXTRA] Parse error: {e}")
    return ""


async def _query_db_parallel(dbs: list[str], sql: str) -> list:
    """Query multiple DB files sequentially.

    Hook QueryDB is not concurrency-safe on the same WeChat process. Keep this
    helper sequential even in remote-hook mode.
    """
    all_rows: list = []
    for db in dbs:
        try:
            data = await query_db(db, sql)
            if data and isinstance(data.get("data"), list):
                all_rows.extend(data["data"])
        except Exception:
            continue
    return all_rows


_ALL_DBS = ["MSG0.db", "MSG1.db", "MSG2.db", "MSG3.db"]


async def get_chat_history(wxid: str, limit: int = 50, before_time: int = 0) -> dict:
    """Get chat history for a specific contact/group, searching across all DB files.
    Returns messages sorted newest-first (DESC by CreateTime), limited to `limit`.
    If before_time > 0, only returns messages with CreateTime < before_time (for pagination).
    For group chats, also extracts sender wxid from BytesExtra."""
    if IS_PROTOCOL:
        _log("[API] Remote mode: get_chat_history via QueryDB not available")
        return {"data": []}

    is_group = "@chatroom" in wxid
    # Always include BytesExtra (for group sender extraction + image paths)
    # and CompressContent (for type 49 fallback)
    time_filter = f"AND CreateTime < {before_time}" if before_time > 0 else ""
    sql = (
        f"SELECT TalkerId, CreateTime, StrTalker, StrContent, MsgSvrID, Type, IsSender, "
        f"hex(BytesExtra) as BytesExtraHex, hex(CompressContent) as CompressHex "
        f"FROM MSG WHERE StrTalker = '{wxid}' {time_filter} "
        f"ORDER BY localId DESC LIMIT {limit}"
    )
    all_rows = await _query_db_parallel(_ALL_DBS, sql)

    # Sort all rows by CreateTime DESC
    def get_create_time(row):
        if isinstance(row, list) and len(row) > 1:
            return row[1] or 0
        elif isinstance(row, dict):
            return row.get("CreateTime", 0)
        return 0

    all_rows.sort(key=get_create_time, reverse=True)
    result_rows = all_rows[:limit]

    # Post-process rows
    for row in result_rows:
        if not isinstance(row, dict):
            continue

        msg_type = str(row.get("Type", ""))
        str_content = row.get("StrContent", "") or ""

        # For type 49 (app messages: quotes, links, files), StrContent may be empty
        # in WeChat 4.x — actual XML is in CompressContent
        if msg_type == "49" and not str_content.strip():
            compress_hex = row.get("CompressHex", "")
            if compress_hex:
                decompressed = _decompress_content(compress_hex)
                if decompressed:
                    row["StrContent"] = decompressed
                    _log(f"[DECOMPRESS] Recovered type 49 content: {decompressed[:100]}")

        # For group chats, extract sender wxid from BytesExtra
        hex_data = row.get("BytesExtraHex", "")
        if is_group and hex_data:
            is_sender_val = str(row.get("IsSender", "0"))
            if is_sender_val != "1":
                sender = _extract_sender_from_bytes_extra(hex_data)
                if sender:
                    row["SenderWxid"] = sender
                else:
                    _log(f"[SENDER] Failed to extract sender for type={msg_type} msgid={row.get('MsgSvrID', '?')}")

        # Keep BytesExtraHex for type 3 (image) messages — frontend needs it to find the file
        if msg_type != "3":
            row.pop("BytesExtraHex", None)

        # Remove CompressHex from response to save bandwidth
        row.pop("CompressHex", None)

    return {"data": result_rows}


def _parse_bulk_row(row) -> tuple[str, dict] | tuple[None, None]:
    """Parse a single row from the bulk last-messages query."""
    talker = ""
    content = ""
    msg_type = "1"
    is_sender = 0
    create_time = 0

    if isinstance(row, list) and len(row) >= 5:
        talker = str(row[0]) if row[0] else ""
        content = str(row[1]) if row[1] else ""
        msg_type = str(row[2]) if row[2] else "1"
        is_sender = int(row[3]) if row[3] else 0
        create_time = row[4] if row[4] else 0
    elif isinstance(row, dict):
        talker = row.get("StrTalker", "")
        content = row.get("StrContent", "")
        msg_type = str(row.get("Type", "1"))
        is_sender = int(row.get("IsSender", 0))
        create_time = row.get("CreateTime", 0)

    if not talker:
        return None, None
    return talker, {"content": content, "type": msg_type, "is_sender": is_sender, "time": create_time}


async def get_last_messages_bulk(wxids: list[str]) -> dict:
    """Get the last message for each session from DB (across all DB files).
    Uses lightweight queries (no BytesExtra, no nested subquery) to avoid
    overloading the Hook DLL which can crash WeChat.
    Local: sequential with bail-on-error. Remote: limited parallelism to avoid
    hammering freshly logged-in Hook clients during startup."""
    if IS_PROTOCOL:
        _log("[API] Remote mode: get_last_messages_bulk via QueryDB not available")
        return {}

    results: dict = {}
    if not wxids:
        return results

    safe_wxids = [w.replace("'", "''") for w in wxids]

    # Larger batches for remote (server can handle it), smaller for local
    BATCH_SIZE = 20 if not IS_LOCAL_HOOK else 10
    batches = [safe_wxids[i:i + BATCH_SIZE] for i in range(0, len(safe_wxids), BATCH_SIZE)]

    def _build_sql(batch: list[str]) -> str:
        wxid_str = "', '".join(batch)
        return (
            f"SELECT StrTalker, StrContent, Type, IsSender, MAX(CreateTime) as CreateTime "
            f"FROM MSG WHERE StrTalker IN ('{wxid_str}') "
            f"GROUP BY StrTalker"
        )

    def _merge_rows(data: dict) -> None:
        if not data or not isinstance(data.get("data"), list):
            return
        for row in data["data"]:
            talker, parsed = _parse_bulk_row(row)
            if not talker:
                continue
            existing = results.get(talker)
            if not existing or parsed["time"] > existing.get("time", 0):
                results[talker] = parsed

    if not IS_LOCAL_HOOK:
        # ─── Remote: sequential QueryDB ─────────────────────────────
        # QueryDB is not safe for concurrent calls on one Hook client.
        for batch in batches:
            sql = _build_sql(batch)
            for db in _ALL_DBS:
                try:
                    data = await asyncio.wait_for(query_db(db, sql), timeout=18.0)
                    _merge_rows(data)
                except Exception:
                    continue
    else:
        # ─── Local: sequential with bail-on-error ──────────────────
        bail = False
        for batch in batches:
            if bail:
                break
            sql = _build_sql(batch)
            for db in _ALL_DBS:
                if bail:
                    break
                try:
                    data = await query_db(db, sql)
                    _merge_rows(data)
                except Exception as e:
                    _log(f"[BULK_MSG] Error querying {db}: {e} — bailing out")
                    bail = True
                    break

    _log(f"[BULK_MSG] Got last messages for {len(results)} sessions")
    return results


async def get_avatar_bytes(wxid: str) -> bytes | None:
    """Get avatar image bytes for a wxid."""
    if IS_HOOK:
        try:
            data = await get_head_img(wxid)
            img_hex = data.get("img_hex", "")
            if img_hex:
                return bytes.fromhex(img_hex)
        except Exception as e:
            _log(f"[AVATAR] Error getting avatar for {wxid}: {e}")
    else:
        # Remote: getcontact may return avatar URL, but not raw bytes
        _log(f"[AVATAR] Remote mode: raw avatar bytes not available for {wxid}")
    return None


# ─── Group Management ──────────────────────────────────────────────

async def get_chatroom_detail(gid: str) -> dict:
    """Get chatroom detail info."""
    if IS_HOOK:
        r = await _post("/GetFriendOrChatroomDetailInfo", json={"wxidorgid": gid})
    else:
        r = await _post("/getchatroominfodetail/", json={"chatRoomUserName": gid})
    return safe_json(r)


async def get_chatroom_members(gid: str) -> dict:
    """Get chatroom member wxid list."""
    if IS_HOOK:
        r = await _post("/BatchGetChatRoomMemberWxid", json={"gid": gid})
    else:
        r = await _post("/getchatroommemberdetail/", json={
            "chatRoomUserName": gid, "memberInfoVersion": 0
        })
    return safe_json(r)


async def get_chatroom_member_detail(gid: str) -> dict:
    """Get all chatroom members with nickname/avatar info."""
    if IS_HOOK:
        r = await _post("/GetChatrooMmemberDetail", json={"gid": gid}, timeout=60.0 if IS_LOCAL_HOOK else 90.0)
    else:
        r = await _post("/getchatroommemberdetail/", json={
            "chatRoomUserName": gid, "memberInfoVersion": 0
        }, timeout=60.0)
    return safe_json(r)


async def get_chatroom_member_nickname(gid: str, wxid: str) -> dict:
    """Get a member's nickname in chatroom.

    先查本地缓存 (QueryChatRoomMemberNickName)，如果没有结果
    再调用网络接口 (GetChatroomMemberDetailInfo) 获取 markname。
    """
    if IS_HOOK:
        r = await _post("/QueryChatRoomMemberNickName", json={"gid": gid, "wxid": wxid})
        data = safe_json(r)
        # 本地没缓存 → fallback 到网络接口
        nickname = data.get("nickname", "") or data.get("data", {}).get("nickname", "")
        if not nickname:
            _log(f"[API] QueryChatRoomMemberNickName 无结果，fallback 到 GetChatroomMemberDetailInfo")
            data = await get_chatroom_member_detail_info(gid, wxid)
        return data
    else:
        # Remote: use getcontact with chatRoom context
        r = await _post("/getcontact/", json={
            "userNames": [wxid], "chatRoomUserName": gid
        })
        return safe_json(r)


async def get_chatroom_member_detail_info(gid: str, wxid: str) -> dict:
    """从网络获取群成员详细信息 (包含 markname)。

    当 QueryChatRoomMemberNickName 本地缓存没有时使用此接口。
    """
    if IS_HOOK:
        r = await _post("/GetChatroomMemberDetailInfo", json={"gid": gid, "wxid": wxid})
    else:
        r = await _post("/getcontact/", json={
            "userNames": [wxid], "chatRoomUserName": gid
        })
    return safe_json(r)


# ─── Callback Config ───────────────────────────────────────────────

async def configure_msg_receive(enable: bool, url: str, recv_type: int = 2) -> dict:
    """Enable/disable message callback."""
    if IS_HOOK:
        if not enable:
            return {}
        r = await _post("/ConfigureMsgReciveFullURL", json={
            "RecvType": recv_type,
            "CallBackURL": url,
        })
        return safe_json(r)
    else:
        # Remote: callback is configured at StartWechat time, not dynamically
        _log(f"[API] Remote mode: callback configured at startup, skipping ConfigureMsgRecive")
        return {}


async def configure_pic_download_path(path: str) -> dict:
    """Configure auto-download path for images."""
    if IS_HOOK:
        r = await _post("/Cfg_PicDownPath", json={"downpath": path})
        return safe_json(r)
    else:
        return {}


async def enable_anywhere_download() -> dict:
    """Enable downloading images/files from any time period.
    Useful for downloading older history images/files (otherwise some Hook builds
    only allow recent media).
    """
    if IS_HOOK:
        r = await _post("/AnywhereDownPicOrFile", json={"AnywhereDownPicOrFile": "1"})
        return safe_json(r)
    # Protocol mode: not supported / not needed
    return {}


async def cdn_init() -> dict:
    """Initialize the CDN subsystem via /CDN_Init.
    MUST be called once before using CDN protocol upload/download endpoints.
    """
    if IS_HOOK:
        r = await _post("/CDN_Init", json={})
        return safe_json(r)
    return {}


async def download_pic(msg_xml: str, save_path: str) -> dict:
    """Download an image using the message XML via /DownPic.
    For history messages, Hook may require explicit /DownPic to pull from CDN.
    """
    if IS_HOOK:
        r = await _post("/DownPic", json={"topath": save_path, "msg_xml": msg_xml})
        return safe_json(r)
    return {}


async def down_file_or_pic(msg_id: str) -> dict:
    """Trigger WeChat to download a file/image by message server ID.
    Uses /DownFileorPic — this goes through WeChat's internal download
    pipeline and may trigger a callback with img_base64.
    """
    if IS_HOOK:
        r = await _post("/DownFileorPic", json={"msg_id": msg_id}, timeout=30.0)
        return safe_json(r)
    return {}


async def down_pic_4id(
    aeskey: str,
    fileid: str,
    save_path: str,
    *,
    originsourcemd5: str = "",
    md5: str = "",
    cdnthumblength: int = 0,
    fromwxid: str = "",
) -> dict:
    """CDN download via /DownPic4ID — the newer CDN download method.
    Downloads image using aeskey + fileid and saves to save_path on Hook server.
    May trigger a callback with the downloaded image data.
    """
    if IS_HOOK:
        body: dict = {
            "topath": save_path,
            "aeskey": aeskey,
            "fileid": fileid,
        }
        if originsourcemd5:
            body["originsourcemd5"] = originsourcemd5
        if md5:
            body["md5"] = md5
        if cdnthumblength:
            body["cdnthumblength"] = cdnthumblength
        if fromwxid:
            body["fromwxid"] = fromwxid
        r = await _post("/DownPic4ID", json=body, timeout=30.0)
        return safe_json(r)
    return {}


# Remote download path for CDN images (on the Hook server)
_CDN_DOWNLOAD_DIR = r"C:\Users\Administrator\Desktop\pic"


async def cdn_download_pic(
    decode_key: str,
    file_id: str,
    i_key: str = "",
    img_filename: str = "down.jpg",
) -> dict:
    """Download an image from WeChat CDN via /download.

    The downloaded image is saved on the remote Hook server's disk.
    After CDN_Init, the Hook may deliver the image via callback (img_base64).

    Parameters from the <img> XML of a type-3 message:
      - decode_key  ← aeskey attribute
      - file_id     ← cdnmidimgurl / cdnthumburl attribute
      - i_key       ← optional, for certain image types
    """
    if IS_HOOK:
        img_path = f"{_CDN_DOWNLOAD_DIR}\\{img_filename}"
        body: dict = {
            "savePath": img_path,
            "aeskey": decode_key,
            "fileid": file_id,
            "chatType": 0,
            "largesVideo": 0,
            "fileType": 2,
        }
        if i_key:
            body["i_key"] = i_key
        r = await _post("/download", json=body, timeout=30.0,
                        bypass_circuit_breaker=True)
        return safe_json(r)
    return {}


async def decode_pic(ori_path: str, save_path: str) -> dict:
    """Decode an encrypted .dat image file via /DecodePic."""
    if IS_HOOK:
        r = await _post("/DecodePic", json={"oripath": ori_path, "savepath": save_path})
        return safe_json(r)
    else:
        _log("[API] Remote mode: DecodePic not available (no local .dat files)")
        return {"error": "DecodePic not available in remote mode"}
