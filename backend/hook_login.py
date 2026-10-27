"""Hook manager launches and login polling, independent of protocol sessions."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time

import httpx
from PIL import Image

import config
import access_store
from agent_ws import agent_manager
from database import database_path

REGISTRY_PATH = database_path("hook_instances.json")
client = httpx.AsyncClient(timeout=10, trust_env=False)
_start_lock = asyncio.Lock()
_profile_lock = asyncio.Lock()


def instances():
    try:
        records = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return [record for record in records if record.get("mode") == config.LOGIN_MODE and record.get("host") == config.HOOK_HOST]


def instance(instance_id):
    return next((record for record in instances() if record["id"] == instance_id), None)


def http_instance(instance_id):
    record = instance(instance_id) if config.IS_HOOK else None
    return record if record and record["transport"] == "http" else None


def instance_for_port(port):
    return next((record for record in instances() if str(record["port"]) == str(port)), None)


def defaults():
    used = {config.HOOK_PORT, *(int(record["port"]) for record in instances())}
    used.update(int(agent["server_port"]) for agent in agent_manager.agents() if str(agent.get("server_port") or "").isdigit())
    port = config.HOOK_PORT + 1
    while port in used and port < 65535:
        port += 1
    return {"mode": config.LOGIN_MODE, "host": config.HOOK_HOST, "mgr_port": config.MGR_PORT,
        "start_port": port, "wechat_path": config.HOOK_WECHAT_PATH,
        "remote_ws": config.CLIENT_WSS_URL if config.AGENT_WS_ENABLED else ""}


async def start(*, start_port, rdv, wechat_path="", remote_ws="", proxy=None):
    if not config.IS_HOOK:
        raise ValueError("当前为协议模式，请使用协议登录")
    rdv = access_store.validate_rdv(rdv)
    if not 1 <= int(start_port) <= 65535:
        raise ValueError("接口端口必须在 1—65535 之间")
    if remote_ws and not config.AGENT_WS_ENABLED:
        raise ValueError("当前 Hook 使用 HTTP 接口，不能设置 RemoteWS")
    body = {"StartPort": str(start_port), "RDV": rdv, "RecvType": str(config.RECV_TYPE)}
    proxy = validate_proxy(proxy or {})
    if proxy['enabled']:
        body.update(Proxy_IP=proxy['ip'], Proxy_Port=proxy['port'], Proxy_Usr=proxy['username'], Proxy_Pwd=proxy['password'])
    path = str(wechat_path or config.HOOK_WECHAT_PATH).strip()
    if path:
        body["WeChatPath"] = path
    if config.AGENT_WS_ENABLED:
        remote_ws = str(remote_ws or config.CLIENT_WSS_URL).strip()
        parsed = httpx.URL(remote_ws)
        if parsed.scheme not in {"ws", "wss"} or not parsed.host:
            raise ValueError("RemoteWS 必须是有效的 ws:// 或 wss:// 地址")
        body["RemoteWS"] = remote_ws
    else:
        body["CallBackURL"] = config.CALLBACK_URL
    async with _start_lock:
        if await _port_is_occupied(start_port):
            raise ValueError("该接口端口已被占用或无法确认，请选择其他端口，或刷新已有微信账号")
        for previous in instances():
            if previous["rdv"] == rdv and previous["port"] != int(start_port):
                if await _port_is_occupied(previous["port"]):
                    raise ValueError("该 RDV 已用于运行中的微信实例，请为新实例填写不同 RDV")
        response = await client.post(config.MGR_BASE_URL + "/StartWechat", json=body, timeout=30)
        response.raise_for_status()
        data = response.json()
        pid = str(data.get("进程ID") or "")
        if str(data.get("success")) != "1" or not pid.isdigit() or int(pid) <= 0:
            raise ValueError(str(data.get("error") or data.get("msg") or "管理器未返回成功标志和有效进程 ID"))
        record = {"id": "default" if int(start_port) == config.HOOK_PORT and not config.AGENT_WS_ENABLED else f"hook_{start_port}", "mode": config.LOGIN_MODE, "host": config.HOOK_HOST,
            "port": int(start_port), "rdv": rdv, "pid": pid, "started_at": time.time(),
            "transport": "ws" if config.AGENT_WS_ENABLED else "http"}
        try:
            records = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            records = []
        records = [old for old in records if not (old.get("mode") == record["mode"] and old.get("host") == record["host"]
            and (old.get("port") == record["port"] or old.get("rdv") == record["rdv"]))]
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = REGISTRY_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps([*records, record], ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, REGISTRY_PATH)
        return record


def validate_proxy(proxy):
    result = {"enabled": bool(proxy.get('enabled')), 'ip': str(proxy.get('ip') or '').strip(),
              'port': str(proxy.get('port') or '').strip(), 'username': str(proxy.get('username') or ''),
              'password': str(proxy.get('password') or '')}
    if result['enabled'] and (not result['ip'] or not result['port'].isdigit() or not 1 <= int(result['port']) <= 65535):
        raise ValueError('启用代理时，请填写有效的地址和 1—65535 端口')
    if any(len(v) > 512 or any(c in v for c in '\r\n\x00"&') for v in result.values() if isinstance(v, str)):
        raise ValueError('代理参数包含不支持的字符或长度过长')
    return result


def user_settings(instance_id='', *, new=False):
    user_id = access_store.current_user.get()
    existing = None if new else access_store.profile(instance_id, user_id)
    if not new and not instance_id and not existing and access_store.owns('default', user_id):
        instance_id = 'default'
    if instance_id and not existing:
        access_store.require_account(instance_id)
        record = instance(instance_id)
        if record:
            existing = {**record, 'profile_id': record['id']}
        elif instance_id == 'default':
            existing = {'id': 'default', 'profile_id': 'default', 'port': config.HOOK_PORT,
                        'rdv': config.RDV or access_store.new_rdv(), 'pid': '', 'transport': 'http'}
    if not existing:
        port = defaults()['start_port']
        with access_store.connect() as db:
            reserved = {int(json.loads(r[0]).get('port') or 0) for r in db.execute(
                'SELECT settings FROM hook_profiles WHERE scope=?', (access_store.scope(),))}
        while port in reserved and port < 65535:
            port += 1
        existing = {'id': f'hook_{port}', 'profile_id': f'hook_{port}', 'port': port,
                    'rdv': access_store.new_rdv(), 'pid': '', 'transport': 'ws' if config.AGENT_WS_ENABLED else 'http'}
        if port in reserved:
            raise ValueError('没有可用的微信接口端口')
    result = {**defaults(), **existing, 'proxy': validate_proxy(existing.get('proxy') or {}),
              'start_port': existing['port']}
    access_store.save_profile(result, user_id)
    access_store.claim(result['id'], user_id)
    return result


async def _wechat_processes():
    response = await client.post(config.MGR_BASE_URL + '/Get_WeChatProcessNumber', json={})
    response.raise_for_status()
    payload = response.json()
    processes = payload.get('List') if isinstance(payload, dict) else None
    if not isinstance(processes, list) or any(
        not isinstance(p, dict) or not str(p.get('PID', '')).isdigit() or int(p['PID']) <= 0 for p in processes
    ):
        raise ValueError('管理器未返回微信进程列表，无法安全确认原实例')
    return processes


def _process_uses_port(process, port):
    return str(process.get('Port')) == str(port) or bool(re.search(
        r'(?:^|[&\s])StartPort=' + re.escape(str(port)) + r'(?:[&\s]|$)', str(process.get('Par') or ''), re.I))


async def _tcp_port_is_listening(port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(config.HOOK_HOST, int(port)), timeout=4)
    except ConnectionRefusedError:
        return False
    except (OSError, asyncio.TimeoutError) as exc:
        raise ValueError('无法确认微信接口端口是否已释放，请检查 Hook 主机网络后重试') from exc
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _port_is_occupied(port):
    response = await client.post(config.MGR_BASE_URL + '/Get_PortOccupiedInfo', json={'CheckPort': str(port)})
    response.raise_for_status()
    payload = response.json()
    occupied = str(payload.get('Occupied')) if isinstance(payload, dict) else ''
    if occupied == '0':
        return False
    if occupied != '1':
        raise ValueError('管理器未返回有效端口状态，无法确认接口端口是否可用')
    if any(_process_uses_port(p, port) for p in await _wechat_processes()):
        return True
    # Older managers count closed TCP connections (TIME_WAIT) as occupied.
    # Ignore that residue only when no WeChat uses the port and TCP explicitly
    # refuses a connection; a timeout or another listening service still blocks launch.
    return await _tcp_port_is_listening(port)


async def _process_for(profile):
    processes = await _wechat_processes()
    matches = [p for p in processes if _process_uses_port(p, profile['port'])]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('微信进程端口不唯一，无法确认原实例')
    process = matches[0]
    # Both port and stored RDV must agree; PID alone may have been reused by Windows.
    rdv = re.search(r'(?:^|[&\s])RDV\s*[=:]\s*"?([0-9a-f]{8})(?:"|[&\s]|$)', str(process.get('Par') or ''), re.I)
    if not rdv or rdv.group(1).lower() != str(profile['rdv']).lower():
        raise ValueError('原微信进程的 RDV 与保存记录不符，未关闭任何进程')
    if profile.get('pid') and str(profile['pid']) != str(process.get('PID')):
        raise ValueError('原微信进程 ID 已变化，未关闭任何进程')
    return process


async def start_for_user(*, instance_id='', restart=False, rdv='', proxy=None, new=False):
    async with _profile_lock:
        pending = access_store.profile() if new else None
        if pending and not pending.get('pid') and not pending.get('stopped_at') and not pending.get('wxid') and not instance(pending['id']):
            instance_id, new = pending['id'], False
        profile = user_settings(instance_id, new=new)
        access_store.require_account(profile['id'])
        next_rdv = access_store.validate_rdv(rdv or profile['rdv'])
        next_proxy = validate_proxy(profile['proxy'] if proxy is None else proxy)
        # Reserve RDVs across users, including stopped profiles.
        with access_store.connect() as db:
            for row in db.execute('SELECT user_id, profile_id, settings FROM hook_profiles WHERE scope=?', (access_store.scope(),)):
                if (row['user_id'], row['profile_id']) != (access_store.current_user.get(), profile['profile_id']) and json.loads(row['settings']).get('rdv') == next_rdv:
                    raise ValueError('该 RDV 已属于其他微信，请使用不同 RDV')
        process = await _process_for(profile)
        if process and not restart:
            profile['pid'] = str(process['PID'])
            access_store.save_profile(profile)
            return profile
        if process:
            await _stop_process(profile, process)
        profile.update(rdv=next_rdv, proxy=next_proxy, pid='', stopped_at=None)
        access_store.save_profile(profile)
        # Save before launch: even a timeout retains the user's RDV and chosen settings.
        record = await start(start_port=profile['port'], rdv=next_rdv, proxy=next_proxy,
                             wechat_path=profile.get('wechat_path', ''), remote_ws=profile.get('remote_ws', ''))
        access_store.claim(record['id'])
        access_store.unretire(record['id'])
        result = {**profile, **record}
        access_store.save_profile(result)
        return result


async def _stop_process(profile, process):
    try:
        await post(profile, '/TerminateThisWeChat')
    except (httpx.HTTPError, ConnectionError, TimeoutError):
        # The instance may close its HTTP/WS connection before it can return a response.
        # Confirm termination from the manager's process list, even if TCP retains TIME_WAIT.
        pass
    for _ in range(20):
        if not any(str(p['PID']) == str(process['PID']) for p in await _wechat_processes()):
            return
        await asyncio.sleep(.2)
    raise ValueError('原微信尚未关闭，请稍后重试')


async def stop_for_user(instance_id, *, after_stop=None):
    async with _profile_lock:
        access_store.require_account(instance_id)
        profile = user_settings(instance_id)
        process = await _process_for(profile)
        if process:
            await _stop_process(profile, process)
        profile.update(pid='', stopped_at=time.time())
        access_store.save_profile(profile)
        if after_stop:
            await after_stop(profile)
        return profile


def forget(instance_id):
    try:
        records = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return
    records = [r for r in records if not (r.get('id') == instance_id and r.get('mode') == config.LOGIN_MODE and r.get('host') == config.HOOK_HOST)]
    temp = REGISTRY_PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, REGISTRY_PATH)


def connected_agent(record):
    matches = [agent for agent in agent_manager.agents() if str(agent.get("server_port") or "") == str(record["port"])]
    return matches[0] if len(matches) == 1 else None


async def post(record, endpoint, body=None):
    if record["transport"] == "ws":
        agent = connected_agent(record)
        if not agent:
            raise ConnectionError("等待微信 Hook 连接后端")
        result = await agent_manager.request(endpoint.strip("/"), body or {}, agent_id=agent["id"], timeout=10,
            inject_agent_id=False, include_method=False)
        response = httpx.Response(result.status, content=result.body, headers={"content-type": result.content_type},
            request=httpx.Request("POST", "http://agent.local" + endpoint))
    else:
        response = await client.post(f"http://{record['host']}:{record['port']}{endpoint}", json=body or {})
    response.raise_for_status()
    return response


def qr_image(response):
    if "image/" in response.headers.get("content-type", ""):
        raw = response.content
    else:
        payload = response.json()
        for _ in range(3):
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                payload = payload["data"]
        if not isinstance(payload, dict):
            raise ValueError("Hook 尚未返回二维码图片")
        encoded = next((payload.get(key) for key in ("base64", "img_base64", "qrcode") if isinstance(payload.get(key), str) and payload[key]), "")
        if encoded:
            raw = base64.b64decode(encoded.split(",", 1)[-1] if encoded.startswith("data:") else encoded, validate=True)
        else:
            encoded = next((payload.get(key) for key in ("img_hex", "qr_hex", "hex", "data") if isinstance(payload.get(key), str) and payload[key]), "")
            raw = bytes.fromhex(encoded)
    if not raw or len(raw) > 4 * 1024 * 1024:
        raise ValueError("Hook 尚未返回有效二维码图片")
    with Image.open(io.BytesIO(raw)) as picture:
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif"}.get(picture.format)
        picture.verify()
    if not mime:
        raise ValueError("Hook 二维码图片格式不支持")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
