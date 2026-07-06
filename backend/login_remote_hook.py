"""Remote Hook 登录脚本 (自动选择免扫码 / 扫码)

流程:
  1. POST /StartWechat (mgr_port)  → 启动微信实例
  2. POST /ClickLoginButton (api_port) → 尝试免扫码登录
  3. 轮询 /IsLoginStatus，若短时间内登录成功 → 结束
  4. 免扫码失败 → 回退到扫码登录:
     a. POST /RefreshLoginQRCode → 刷新二维码
     b. POST /GetLoginQRCode    → 获取二维码图片
     c. 弹窗显示二维码，等待扫码
     d. 轮询 /IsLoginStatus 直到登录成功
"""

import sys
import os
import io
import time
import tempfile
import threading
import base64
import requests

# ─── 加载 config ───────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from config import (
    HOOK_HOST, HOOK_PORT, MGR_PORT,
    RDV, CALLBACK_URL,
    RECV_TYPE,
    RESTART_ON_BUTTON_LOGIN_FAIL, MAX_RESTARTS_AFTER_BUTTON_LOGIN_FAIL,
)

MGR_URL = f"http://{HOOK_HOST}:{MGR_PORT}"
API_URL = f"http://{HOOK_HOST}:{HOOK_PORT}"

TIMEOUT = 15  # seconds per request
STARTUP_WAIT = 90  # 最多等90秒让API端口就绪
START_RETRIES = 3  # StartWechat 最多重试次数
POST_START_DELAY = 5  # StartWechat 后等几秒让 Hook DLL 注入初始化
POST_CLEANUP_DELAY = 5  # 清理进程后额外等几秒确保端口释放

# ─── 日志文件 ──────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "login.log")
_log_fp = None


def _open_log():
    global _log_fp
    if _log_fp is None:
        _log_fp = open(LOG_FILE, "a", encoding="utf-8")
        _log_fp.write(f"\n{'=' * 60}\n")
        _log_fp.write(f"  Login session started at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _log_fp.write(f"{'=' * 60}\n\n")
        _log_fp.flush()


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _log_fp:
        _log_fp.write(line + "\n")
        _log_fp.flush()


def _post(url: str, body: dict = None, *, timeout: float = TIMEOUT,
          log_response: bool = True) -> requests.Response:
    """统一的 HTTP POST 包装，记录完整调用链到日志。

    Parameters:
        url:          请求地址
        body:         JSON body（None 则不发 body）
        timeout:      超时秒数
        log_response: 是否打印响应 raw（轮询类调用可设 False 减少噪音）

    Returns:
        requests.Response

    Raises:
        原样抛出 requests 异常（由调用者处理）
    """
    # ── 记录请求 ──
    log(f"POST {url}")
    if body is not None:
        log(f"  >>> data: {body}")

    r = requests.post(url, json=body if body is not None else None, timeout=timeout)

    # ── 记录响应 ──
    if log_response:
        log(f"  <<< status={r.status_code}  raw={r.text[:800]}")
    return r


def wait_for_api(max_wait: int = STARTUP_WAIT) -> bool:
    """StartWechat 后等待 API 端口就绪，每2秒试一次。

    每 10 秒检查进程状态辅助诊断:
      - Port > 0 → Hook 成功，继续等待 API 响应
      - Port = -858993460 (0xCCCCCCCC) → Hook 注入失败 (端口被占用)，提前返回
      - Port = 0 → Hook 正在初始化，继续等待
    """
    url = f"{API_URL}/IsLoginStatus"
    log(f"等待 API 端口就绪 ({API_URL}) ... 最多 {max_wait}s")
    for i in range(max_wait // 2):
        time.sleep(2)
        elapsed = (i + 1) * 2
        try:
            r = _post(url, timeout=5, log_response=False)
            log(f"  ✅ API 端口已就绪 (第{elapsed}秒)")
            return True
        except requests.ConnectionError:
            # 每 10 秒检查进程 Port 状态
            if elapsed % 10 == 0:
                count_info = get_wechat_process_count()
                total = count_info.get("total_num") or count_info.get("count") or "?"
                proc_list = count_info.get("List") or count_info.get("list") or []

                # 检查是否有 Hook 注入失败的进程 (Port = -858993460 = 0xCCCCCCCC)
                hook_failed = False
                for proc in proc_list:
                    port = proc.get("Port", 0)
                    pid = proc.get("PID") or proc.get("pid")
                    if port == -858993460:
                        log(f"  ❌ 进程 PID={pid} Hook 注入失败 (Port=0xCCCCCCCC，端口被占用)")
                        hook_failed = True

                if hook_failed:
                    log(f"  → Hook 注入失败，继续等待无意义，将清理并重试")
                    return False  # 返回 False 触发上层重试逻辑

                log(f"  等待中... ({elapsed}s) — 微信进程数: {total}")
            else:
                log(f"  等待中... ({elapsed}s)")
        except Exception as e:
            log(f"  等待中 ({type(e).__name__})... ({elapsed}s)")
    log(f"  ❌ 等了{max_wait}秒，API 端口仍未就绪")
    return False


# ═══════════════════════════════════════════════════════════════════
# Helpers: 管理端口恢复等待 / 状态检查 / 进程管理
# ═══════════════════════════════════════════════════════════════════

def _wait_for_mgr_port(max_wait: int = 30) -> bool:
    """等待管理端口恢复 (终止进程后管理服务可能重启)。"""
    url = f"{MGR_URL}/Get_WeChatProcessNumber"
    for i in range(max_wait // 2):
        time.sleep(2)
        try:
            r = _post(url, timeout=5, log_response=False)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        elapsed = (i + 1) * 2
        if elapsed % 10 == 0:
            log(f"  等待管理端口恢复... ({elapsed}s)")
    return False


def check_login_status() -> dict | None:
    """检查当前 API 端口是否有已登录的微信。返回状态 dict 或 None（端口不通）。"""
    url = f"{API_URL}/IsLoginStatus"
    try:
        r = _post(url, timeout=5)
        return r.json()
    except requests.ConnectionError:
        return None
    except Exception as e:
        log(f"  检查登录状态出错: {e}")
        return None


def get_port_occupied_info() -> dict:
    """查询管理端口，获取占用 API 端口的进程信息。"""
    url = f"{MGR_URL}/Get_PortOccupiedInfo"
    body = {"CheckPort": HOOK_PORT}
    try:
        r = _post(url, body, timeout=TIMEOUT)
        return r.json() or {}
    except Exception as e:
        log(f"  查询进程信息出错: {e}")
        return {}


def ensure_port_free(max_attempts: int = 3) -> bool:
    """确保 API 端口 (HOOK_PORT) 未被占用。

    检查端口占用情况，如果有进程占用则终止它，等待端口释放。
    返回 True 表示端口已空闲，False 表示无法释放。
    """
    for attempt in range(1, max_attempts + 1):
        log(f"[端口检查] 检查端口 {HOOK_PORT} 占用情况 (第{attempt}次)...")
        info = get_port_occupied_info()

        # 判断端口是否空闲
        # 已知返回格式: {"CheckPort":30001, "PID":0, "占用进程":"", "MsgContent":"该端口未被使用"}
        # 或: {"CheckPort":30001, "PID":7892, "占用进程":"WeChat.exe", ...}
        pid = info.get("PID") or info.get("pid") or 0
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            pid = 0

        if pid == 0:
            log(f"[端口检查] ✅ 端口 {HOOK_PORT} 空闲")
            return True

        proc_name = info.get("占用进程") or info.get("ProcessName") or "unknown"
        log(f"[端口检查] ⚠ 端口 {HOOK_PORT} 被 PID={pid} ({proc_name}) 占用")

        # 终止占用进程
        log(f"[端口检查] 终止占用进程 PID={pid}...")
        terminate_wechat(pid)

        # 等待端口释放
        log(f"[端口检查] 等待端口释放 ({POST_CLEANUP_DELAY}s)...")
        time.sleep(POST_CLEANUP_DELAY)

        # 等管理端口恢复
        _wait_for_mgr_port(max_wait=15)

    # 最终验证
    log(f"[端口检查] 最终验证端口 {HOOK_PORT}...")
    final_info = get_port_occupied_info()
    final_pid = final_info.get("PID") or final_info.get("pid") or 0
    try:
        final_pid = int(final_pid)
    except (ValueError, TypeError):
        final_pid = 0

    if final_pid == 0:
        log(f"[端口检查] ✅ 端口已释放")
        return True

    log(f"[端口检查] ❌ 端口仍被占用 (PID={final_pid})，无法释放")
    return False


def get_wechat_process_count() -> dict:
    """获取远程服务器上的微信实例总数和列表。

    返回格式示例:
        {"total_num": "2", "List": [
            {"Index": 1, "PID": 4880, "Port": -858993460, ...},
            {"Index": 2, "PID": 7408, "Port": 0, ...}
        ]}
    """
    url = f"{MGR_URL}/Get_WeChatProcessNumber"
    try:
        r = _post(url, timeout=TIMEOUT)
        return r.json() or {}
    except Exception as e:
        log(f"  获取微信总数出错: {e}")
        return {}


def terminate_wechat(pid: int | str) -> dict:
    """终止指定PID的微信进程。

    注意: 终止进程后管理端口可能会短暂不可用 (服务重启)，这是正常的。
    """
    url = f"{MGR_URL}/TerminateThisWeChat"
    body = {"PID": str(pid)}
    try:
        r = _post(url, body, timeout=TIMEOUT)
        return r.json() or {}
    except (requests.ConnectionError, ConnectionResetError):
        # 终止进程可能导致管理服务短暂重启，连接被断开是正常的
        log(f"  ⚠ 连接被断开 (管理服务可能在重启，属正常现象)")
        return {"terminated": True}


def cleanup_all_wechat_processes() -> int:
    """查询所有微信进程并全部终止，返回终止的进程数。"""
    info = get_wechat_process_count()
    proc_list = info.get("List") or info.get("list") or []
    total = int(info.get("total_num", 0) or 0)

    if total == 0 and not proc_list:
        return 0

    killed = 0
    for proc in proc_list:
        pid = proc.get("PID") or proc.get("pid")
        port = proc.get("Port", "?")
        if pid:
            log(f"  终止微信进程: PID={pid}  Port={port}")
            terminate_wechat(pid)
            killed += 1

    if killed > 0:
        log(f"  等待管理端口恢复...")
        _wait_for_mgr_port()
        # 额外等待确保端口完全释放
        log(f"  等待端口释放 ({POST_CLEANUP_DELAY}s)...")
        time.sleep(POST_CLEANUP_DELAY)
        log(f"  ✅ 已终止 {killed} 个微信进程")

    return killed


def precheck() -> str:
    """登录前预检查。

    Returns:
        "logged_in"    — 已经登录，不需要再登录
        "cleaned"      — 有残留进程已清理，需要重新启动
        "running_but_api_down" — 有微信进程在跑，但 API 暂时不通（可能网络抖动/Hook卡顿），先别清理
        "clean"        — 没有残留进程，可以直接启动
    """
    log("[预检查] 检查登录状态...")

    # 1. 先看 API 端口是否通、是否已登录
    status = None
    # API 偶发 ConnectionReset/短暂不可达时，避免误判为“端口不通”然后把已登录进程全杀掉。
    for i in range(3):
        status = check_login_status()
        if status is not None:
            break
        if i < 2:
            time.sleep(1.5)
    if status is not None and _is_logged_in(status):
        wxid = status.get("selfwxid", "?")
        log(f"[预检查] ✅ 已登录: {wxid}，无需重复登录")
        return "logged_in"

    # 2. API 端口通但没登录 → 有进程但没登录上，全部清理
    if status is not None:
        log("[预检查] API 端口有响应但未登录，清理所有微信进程...")
        killed = cleanup_all_wechat_processes()
        if killed > 0:
            # 确认端口已释放
            ensure_port_free()
            return "cleaned"
        log("[预检查] API 有响应但无进程可清理，继续...")
        return "clean"

    # 3. API 端口不通 → 查询微信进程
    log("[预检查] API 端口不通，查询微信进程...")
    info = get_wechat_process_count()
    total = int(info.get("total_num", 0) or 0)
    proc_list = info.get("List") or info.get("list") or []

    if total > 0 or proc_list:
        # 更保守策略：如果有进程在跑，先尝试等待 API 恢复；不要立即清理，否则会导致手机频繁收到“电脑登录提醒”。
        log(f"[预检查] 发现 {total} 个微信进程，但 API 暂时不通：先等待 API 恢复 (不清理)...")
        for proc in proc_list:
            pid = proc.get("PID") or proc.get("pid")
            port = proc.get("Port", "?")
            if pid:
                log(f"  → 现有进程: PID={pid}  Port={port}")
        return "running_but_api_down"

    log("[预检查] ✅ 没有残留进程")

    # 4. 最后确认端口没有被占用
    log("[预检查] 检查端口占用...")
    if not ensure_port_free():
        log("[预检查] ⚠ 端口无法释放，但仍尝试启动...")
    return "clean"


# ═══════════════════════════════════════════════════════════════════
# Step 1: StartWechat
# ═══════════════════════════════════════════════════════════════════

def start_wechat() -> dict | None:
    """调用管理端口启动微信实例，返回响应JSON。

    使用 JSON body (application/json)，与 Postman 行为一致。
    """
    url = f"{MGR_URL}/StartWechat"
    body = {
        "StartPort": str(HOOK_PORT),
        "CallBackURL": CALLBACK_URL,
        "RDV": RDV,
        "RecvType": str(RECV_TYPE),
    }
    try:
        r = _post(url, body, timeout=TIMEOUT)
    except requests.ConnectionError as e:
        log(f"  ❌ 连接失败 (管理端口不通): {e}")
        return None
    except requests.Timeout:
        log(f"  ❌ 请求超时 ({TIMEOUT}s)")
        return None
    except Exception as e:
        log(f"  ❌ 请求异常: {type(e).__name__}: {e}")
        return None

    try:
        data = r.json() or {}
    except Exception:
        data = {"raw": r.text}
    return data


def start_wechat_with_retry() -> dict:
    """尝试最多 START_RETRIES 次启动微信，每次间隔递增。

    返回值: StartWechat 的响应 dict (可能为空)。
    如果启动成功 (有 PID) 立即返回；
    如果返回 null/空但 API 端口已就绪，也认为已经在运行；
    全部失败后仍返回最后一次的结果供调用方判断。
    """
    last_result = {}
    for attempt in range(1, START_RETRIES + 1):
        if attempt > 1:
            delay = attempt * 3  # 6s, 9s
            log(f"\n[重试] 第 {attempt}/{START_RETRIES} 次尝试启动微信 (等待 {delay}s)...")
            time.sleep(delay)

        # ── 启动前: 确保端口空闲 ──
        log(f"[启动前检查] 确保端口 {HOOK_PORT} 空闲...")
        if not ensure_port_free():
            log(f"  ⚠ 端口 {HOOK_PORT} 仍被占用，尝试启动可能失败")

        result = start_wechat()
        if result is None:
            # 管理端口不通 — 可能是刚清理完进程，管理服务在重启
            log("  → 管理端口不通，等待恢复...")
            if _wait_for_mgr_port():
                log("  → 管理端口已恢复，重新尝试启动...")
                result = start_wechat()
            else:
                log("  → 管理端口长时间不可用，跳过本次")

        result = result or {}
        last_result = result

        # 检查是否成功 — 同时兼容 "success":"1" 和 直接返回 "PID" 的格式
        success = result.get("success", 0)
        pid = result.get("进程ID") or result.get("pid") or result.get("PID")
        if success == 1 or success == "1" or pid:
            log(f"✅ 微信进程已创建，进程ID: {pid or 'unknown'}")
            # 给 Hook DLL 注入和初始化一些时间
            log(f"  等待 Hook DLL 初始化 ({POST_START_DELAY}s)...")
            time.sleep(POST_START_DELAY)
            return result

        # StartWechat 返回 null/空 — 检查一下是不是实例已经在运行
        log(f"⚠ StartWechat 未返回 PID (success={success})")

        # 快速探测 API 端口是否通
        try:
            r = _post(f"{API_URL}/IsLoginStatus", timeout=5)
            log(f"  → API 端口已通，微信实例可能已在运行")
            return result  # API 通了就不用再重试了
        except requests.ConnectionError:
            pass

        # API 不通 — 查询进程列表，清理僵尸进程再重试
        log("  → API 端口不通，检查并清理僵尸进程...")
        try:
            info = get_wechat_process_count()
        except Exception:
            log("  → 管理端口也不通，等待恢复...")
            if _wait_for_mgr_port():
                info = get_wechat_process_count()
            else:
                log("  → 管理端口长时间不可用")
                info = {}

        total = int(info.get("total_num", 0) or 0)
        proc_list = info.get("List") or info.get("list") or []
        log(f"  → 当前微信进程数: {total}")

        # 终止所有僵尸进程 (Port 不正常的)
        killed = 0
        for proc in proc_list:
            p = proc.get("PID") or proc.get("pid")
            port = proc.get("Port", 0)
            # 正常 Port 应该是 > 0 的端口号, 例如 30001
            # Port = -858993460 (0xCCCCCCCC) → Hook 注入失败
            # Port = 0 → Hook 还在初始化 (可能变好也可能卡住)
            if p and (port < 0 or port == 0):
                log(f"  → 清理僵尸进程: PID={p}  Port={port}")
                terminate_wechat(p)
                killed += 1

        if killed > 0:
            log(f"  → 已清理 {killed} 个僵尸进程")
            # 终止进程后管理端口可能短暂不可用，等它恢复
            log("  → 等待管理端口恢复...")
            _wait_for_mgr_port()

        if attempt < START_RETRIES:
            log(f"  → 将重试...")

    log(f"⚠ {START_RETRIES} 次启动均未返回 PID，继续等待 API 端口...")
    return last_result


# ═══════════════════════════════════════════════════════════════════
# Step 2: 尝试免扫码登录 (ClickLoginButton)
# ═══════════════════════════════════════════════════════════════════

RELOGIN_POLL_SECONDS = 30  # 免扫码登录最多等 30 秒


def click_login() -> dict:
    """调用点击登录按钮 (免扫码)。"""
    url = f"{API_URL}/ClickLoginButton"
    r = _post(url, timeout=TIMEOUT)
    return r.json()


def try_relogin() -> bool:
    """尝试免扫码登录: 点击登录按钮 → 短时间内轮询是否成功。

    返回 True 表示免扫码登录成功，False 表示需要回退到扫码。
    """
    log("[免扫码] 尝试点击登录按钮...")
    try:
        click_login()
    except Exception as e:
        log(f"[免扫码] ClickLoginButton 调用失败: {e}")
        return False

    url = f"{API_URL}/IsLoginStatus"
    log(f"[免扫码] 轮询登录状态 (最多 {RELOGIN_POLL_SECONDS}s)...")
    for i in range(RELOGIN_POLL_SECONDS // 3):
        time.sleep(3)
        try:
            r = _post(url, timeout=TIMEOUT)
            data = r.json()
            log(f"  登录状态: {data}")
            if _is_logged_in(data):
                log("[免扫码] ✅ 免扫码登录成功！")
                return True
            # 检测到"请扫码登录" → 免扫码已失败，立即切换
            if _needs_qr_scan(data):
                log("[免扫码] → 服务器要求扫码，免扫码登录失败，立即切换到扫码登录")
                return False
        except Exception as e:
            log(f"  轮询出错: {e}")

    log("[免扫码] ⏰ 未能在短时间内登录，将回退到扫码登录")
    return False


# ═══════════════════════════════════════════════════════════════════
# Step 2.x helper: 按钮登录失败后的“结束进程→重启→扫码”
# ═══════════════════════════════════════════════════════════════════

def restart_wechat_before_qr(reason: str) -> bool:
    """按钮登录失败时，重启微信实例，保证后续扫码流程 API 端口可用。"""
    log(f"\n[重启] {reason}")
    log("[重启] 结束微信进程...")
    try:
        cleanup_all_wechat_processes()
    except Exception as e:
        log(f"  ⚠ 清理进程异常: {e}")

    # 确保端口释放后再启动
    try:
        ensure_port_free()
    except Exception as e:
        log(f"  ⚠ 端口释放检查异常: {e}")

    log("[重启] 重新启动微信实例...")
    try:
        start_wechat_with_retry()
    except Exception as e:
        log(f"  ❌ 启动微信异常: {e}")
        return False

    log("[重启] 等待 API 端口就绪...")
    if not wait_for_api():
        log("  ❌ API 端口仍未就绪，无法继续扫码登录")
        return False
    log("  ✅ 重启完成，继续扫码登录流程")
    return True


# ═══════════════════════════════════════════════════════════════════
# Step 3 (fallback): RefreshLoginQRCode
# ═══════════════════════════════════════════════════════════════════

def refresh_qr() -> dict:
    """刷新登陆二维码。"""
    url = f"{API_URL}/RefreshLoginQRCode"
    r = _post(url, timeout=TIMEOUT)
    return r.json()


# ═══════════════════════════════════════════════════════════════════
# Step 4 (fallback): GetLoginQRCode → 获取图片数据
# ═══════════════════════════════════════════════════════════════════

def get_qr_image_bytes() -> bytes | None:
    """获取二维码图片原始字节。返回 PNG/JPG bytes 或 None。"""
    url = f"{API_URL}/GetLoginQRCode"
    r = _post(url, timeout=TIMEOUT)

    content_type = r.headers.get("content-type", "")

    # 如果直接返回图片二进制
    if "image" in content_type:
        log(f"  收到图片 (content-type={content_type}, {len(r.content)} bytes)")
        return r.content

    # 尝试解析 JSON
    try:
        data = r.json()
    except Exception:
        # 不是JSON，当作原始二进制
        if len(r.content) > 100:
            log(f"  非JSON，尝试当作原始二进制图片: {len(r.content)} bytes")
            return r.content
        log(f"  ⚠ 无法解析响应 (status={r.status_code}, len={len(r.content)})")
        return None

    log(f"  response keys: {list(data.keys())}")
    log(f"  response: {str(data)[:200]}...")

    # ── 优先检查 base64 ──
    b64_data = (
        data.get("base64")
        or data.get("img_base64")
        or data.get("qrcode")
        or ""
    )
    if b64_data:
        # 去掉可能的 data URI 前缀: "data:image/png;base64,..."
        if "," in b64_data[:80]:
            b64_data = b64_data.split(",", 1)[1]
        # 去掉空白字符
        b64_data = b64_data.strip()
        try:
            img_bytes = base64.b64decode(b64_data)
            log(f"  ✅ 解码 base64 图片: {len(img_bytes)} bytes")
            return img_bytes
        except Exception as e:
            log(f"  ⚠ base64 解码失败: {e}")
            # 可能是 hex 而不是 base64，继续往下

    # ── 检查 hex ──
    hex_data = (
        data.get("img_hex")
        or data.get("qr_hex")
        or data.get("hex")
        or data.get("data")
        or ""
    )
    if hex_data:
        hex_clean = hex_data.strip()
        try:
            img_bytes = bytes.fromhex(hex_clean)
            log(f"  ✅ 解码 hex 图片: {len(img_bytes)} bytes")
            return img_bytes
        except Exception as e:
            log(f"  ⚠ hex 解码失败: {e}")

    log(f"  ❌ 无法从响应中提取图片")
    return None


# ═══════════════════════════════════════════════════════════════════
# Step 5 (fallback): 弹窗显示二维码
# ═══════════════════════════════════════════════════════════════════

def show_qr_window(img_bytes: bytes):
    """用 tkinter 弹窗显示二维码图片，扫码后手动关闭窗口。"""
    try:
        import tkinter as tk
        from PIL import Image, ImageTk
    except ImportError:
        # 没有 PIL，就保存到文件并用系统默认程序打开
        _show_qr_fallback(img_bytes)
        return

    img = Image.open(io.BytesIO(img_bytes))

    root = tk.Tk()
    root.title("微信登录 - 扫描二维码")
    root.attributes("-topmost", True)

    # 缩放到合适大小
    max_size = 400
    w, h = img.size
    if w > max_size or h > max_size:
        ratio = min(max_size / w, max_size / h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    photo = ImageTk.PhotoImage(img)

    label = tk.Label(root, image=photo)
    label.pack(padx=10, pady=10)

    hint = tk.Label(root, text="请用微信扫描二维码，登录成功后窗口自动关闭", font=("Microsoft YaHei", 10))
    hint.pack(pady=(0, 10))

    # 保存引用防止GC
    root._photo = photo

    # 让轮询线程能关闭窗口
    root._should_close = False

    def check_close():
        if root._should_close:
            root.destroy()
        else:
            root.after(500, check_close)

    root.after(500, check_close)
    root.mainloop()


def _show_qr_fallback(img_bytes: bytes):
    """保存二维码到临时文件并打开。"""
    tmp = os.path.join(tempfile.gettempdir(), "wechat_login_qr.png")
    with open(tmp, "wb") as f:
        f.write(img_bytes)
    log(f"二维码已保存到: {tmp}")
    os.startfile(tmp)  # Windows
    log("请扫描二维码...")


# ═══════════════════════════════════════════════════════════════════
# Step 6 (fallback): 轮询登录状态
# ═══════════════════════════════════════════════════════════════════

def poll_login_status(qr_root=None) -> bool:
    """每3秒检查一次登录状态，登录成功返回True。"""
    url = f"{API_URL}/IsLoginStatus"
    log("开始轮询登录状态...")

    for attempt in range(120):  # 最多等6分钟
        time.sleep(3)
        try:
            r = _post(url, timeout=TIMEOUT)
            data = r.json()

            # 打印状态
            status = data.get("status") or data.get("success") or data.get("is_login")
            log(f"  登录状态: {data}")

            # 判断是否登录成功 (多种可能的响应格式)
            if _is_logged_in(data):
                log("✅ 登录成功！")
                if qr_root is not None:
                    qr_root._should_close = True
                return True

        except Exception as e:
            log(f"  轮询出错: {e}")

    log("⏰ 超时，未检测到登录成功")
    if qr_root is not None:
        qr_root._should_close = True
    return False


def _is_logged_in(data: dict) -> bool:
    """判断响应是否表示已登录。

    已知返回格式:
      {'onlinestatus': '3', 'msg': '登陆完成！', 'login_loading': '100%',
       'selfwxid': 'wxid_xxx', 'nickname': '...'}
    """
    # onlinestatus == "3" 表示在线 (实际返回的格式)
    if str(data.get("onlinestatus", "")) == "3":
        return True
    # msg 包含 "登陆完成"
    if "登陆完成" in str(data.get("msg", "")):
        return True
    # login_loading 100%
    if str(data.get("login_loading", "")) == "100%":
        return True
    # selfwxid 有值说明已登录
    if data.get("selfwxid"):
        return True
    # 备用判断
    if data.get("success") == 1 or data.get("success") == "1":
        return True
    if data.get("is_login") == 1 or data.get("is_login") is True:
        return True
    return False


def _needs_qr_scan(data: dict) -> bool:
    """判断响应是否表示需要扫码登录 (免扫码已失败)。

    已知返回格式:
      {'onlinestatus': '0', 'msg': '请扫码登录！', 'login_loading': '0%',
       'selfwxid': '', 'nickname': ''}
    """
    msg = str(data.get("msg", ""))
    status = str(data.get("onlinestatus", ""))
    # onlinestatus=0 + "请扫码" → 免扫码失败，需要扫码
    if status == "0" and "扫码" in msg:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# 登录后验证
# ═══════════════════════════════════════════════════════════════════

def _post_login_verify():
    """登录后验证: 等待几秒，然后确认进程还活着、获取登录信息。"""
    log("\n[登录后验证] 等待 3 秒后检查...")
    time.sleep(3)

    # 1. 检查进程是否还在
    log("[登录后验证] 检查微信进程...")
    info = get_wechat_process_count()
    total = int(info.get("total_num", 0) or 0)
    proc_list = info.get("List") or info.get("list") or []

    if total == 0:
        log("[登录后验证] ⚠ 微信进程已消失！登录可能不稳定")
        log("  → 可能原因: 微信登录后自动退出、Hook DLL 不兼容、服务器端异常")
        log("  → 建议: 联系服务器管理员 (Xed) 检查微信进程日志")
        log("\n脚本结束 (登录后进程消失)。")
        return

    # 打印进程状态
    for proc in proc_list:
        pid = proc.get("PID") or proc.get("pid")
        port = proc.get("Port", "?")
        log(f"  → 微信进程: PID={pid}  Port={port}")

    # 2. 检查 API 端口是否还通
    log("[登录后验证] 检查 API 端口...")
    try:
        r = _post(f"{API_URL}/IsLoginStatus", timeout=5)
        data = r.json()
        wxid = data.get("selfwxid", "")
        nickname = data.get("nickname", "")
        status = data.get("onlinestatus", "?")
        msg = data.get("msg", "")
        log(f"  → 登录状态: onlinestatus={status}  msg={msg}")
        if wxid:
            log(f"  → 已登录: wxid={wxid}  昵称={nickname}")
        else:
            log(f"  → ⚠ selfwxid 为空 (可能需要等更久)")
    except Exception as e:
        log(f"  → ❌ API 端口不通: {e}")
        log("  → 微信进程可能在登录后崩溃了")

    # 3. 尝试获取详细登录信息 (GetSelfLoginInfo)
    try:
        log("[登录后验证] 获取详细登录信息...")
        r = _post(f"{API_URL}/GetSelfLoginInfo", body={}, timeout=10)
        data = r.json()
        log(f"  → GetSelfLoginInfo: {data}")
    except Exception as e:
        log(f"  → ⚠ 获取登录信息失败: {e}")

    log("\n脚本结束。")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main():
    _open_log()
    log("=" * 50)
    log("Remote Hook 登录 (自动选择免扫码 / 扫码)")
    log(f"  管理端口: {MGR_URL}")
    log(f"  API 端口: {API_URL}")
    log(f"  回调地址: {CALLBACK_URL}")
    log(f"  RDV: {RDV}")
    log("=" * 50)

    # Step 0: 预检查
    log("\n[Step 0] 预检查...")
    check_result = precheck()
    if check_result == "logged_in":
        log("\n已登录，无需操作。脚本结束。")
        return

    # 如果进程在跑但 API 暂时不通，先等待一会儿，避免误杀已登录进程导致手机反复提示"电脑登录"。
    if check_result == "running_but_api_down":
        log("\n[Step 0.5] 进程存在但 API 不通 → 等待 API 恢复 (最多 30s)...")
        t0 = time.time()
        api_ok = False
        while time.time() - t0 < 30:
            if check_login_status() is not None:
                api_ok = True
                break
            time.sleep(2)
        if api_ok:
            log("  ✅ API 已恢复，继续后续登录判断 (不重启微信)")
        else:
            # API 30s 仍不通 — 检查进程 Port 状态，若 Port=0 说明 Hook 从未注入，等再久也没用
            log("  ⚠ API 仍不通，检查进程 Hook 注入状态...")
            info = get_wechat_process_count()
            proc_list = info.get("List") or info.get("list") or []
            all_port_zero = all(
                (proc.get("Port", 0) == 0) for proc in proc_list
            ) if proc_list else False

            if all_port_zero:
                log("  ⚠ 所有微信进程 Port=0 — Hook 从未注入，等待无意义")
                log("  → 清理所有进程后重新启动...")
                cleanup_all_wechat_processes()
                ensure_port_free()
                check_result = "cleaned"  # 改为 cleaned，使 Step 1 走 StartWechat 路径
            else:
                log("  ⚠ API 仍不通，继续按原流程尝试启动/修复")

    # Step 1: 启动微信 (带重试) + 等待 API 就绪
    # 如果 Hook 注入失败 (端口被占用)，会自动清理并重试整个启动流程
    MAX_FULL_RETRIES = 2  # 完整启动流程最多重试次数
    api_ready = False

    for full_attempt in range(1, MAX_FULL_RETRIES + 1):
        if full_attempt > 1:
            log(f"\n{'═' * 50}")
            log(f"[完整重试] 第 {full_attempt}/{MAX_FULL_RETRIES} 次完整启动流程")
            log(f"{'═' * 50}")

        # 如果是 running_but_api_down 且第一次尝试，优先尝试等待 API 就绪，避免重复 StartWechat
        # 第二次及以后总是走 StartWechat（说明上一轮等待失败了，进程已被清理）
        if check_result == "running_but_api_down" and full_attempt == 1:
            log("\n[Step 1] 跳过 StartWechat：等待现有实例 API 就绪...")
        else:
            log("\n[Step 1] 启动微信实例...")
            result = start_wechat_with_retry()

        # Step 1.5: 等待API端口就绪
        log("\n[Step 1.5] 等待微信实例 API 端口就绪...")
        if wait_for_api():
            api_ready = True
            break

        # API 端口不通 — 诊断并决定是否重试
        log("─" * 40)
        log("⚠ API 端口未就绪，诊断中...")
        count_info = get_wechat_process_count()
        total = int(count_info.get("total_num", 0) or 0)
        proc_list = count_info.get("List") or count_info.get("list") or []

        if total == 0:
            log("  → 服务器上没有微信进程，启动失败")
            log("  → 可能原因: 服务器缺少 C++ 运行库、内存不足、或微信版本问题")
            log("─" * 40)
            break  # 没进程了，重试也没用

        # 检查是否有 Hook 注入失败的进程
        has_bad_proc = False
        for proc in proc_list:
            port = proc.get("Port", 0)
            pid = proc.get("PID") or proc.get("pid")
            if port == -858993460 or port < 0:
                has_bad_proc = True
                log(f"  → 清理 Hook 注入失败的进程: PID={pid} Port={port}")
                terminate_wechat(pid)
            elif port == 0:
                has_bad_proc = True
                log(f"  → 清理未初始化的进程: PID={pid} Port={port}")
                terminate_wechat(pid)

        if has_bad_proc and full_attempt < MAX_FULL_RETRIES:
            log(f"  → 已清理异常进程，等待管理端口恢复...")
            _wait_for_mgr_port()
            log(f"  → 等待端口释放 ({POST_CLEANUP_DELAY}s)...")
            time.sleep(POST_CLEANUP_DELAY)
            log(f"  → 将进行完整重试...")
            log("─" * 40)
            continue

        log(f"  → 有 {total} 个微信进程但 API 端口 ({HOOK_PORT}) 不通")
        log("  → 可能原因: 端口被占用导致Hook通讯失败、防火墙拦截")
        log("  → 建议: 检查服务器端口占用情况")
        log("─" * 40)

    if not api_ready:
        log("❌ 多次尝试均无法启动微信实例，退出")
        sys.exit(1)

    # Step 2: 根据 IsLoginStatus 的 onlinestatus 决定按钮登录还是扫码登录
    # 规则: onlinestatus == 5 → ClickLoginButton；否则 → 直接扫码登录
    log("\n[Step 2] 获取登录状态 (决定按钮登录/扫码)...")
    status = None
    try:
        status = check_login_status()
        log(f"  → IsLoginStatus: {status}")
    except Exception as e:
        log(f"  ⚠ 获取登录状态失败: {e}")

    # 已经登录直接结束
    if status is not None and _is_logged_in(status):
        wxid = status.get("selfwxid", "?")
        log(f"\n✅ 已登录: {wxid}，脚本结束。")
        _post_login_verify()
        return

    online_status = str((status or {}).get("onlinestatus", "")).strip()
    restarts_after_button_fail = 0
    if online_status == "5":
        # onlinestatus=5 → 走按钮登录
        log("\n[Step 2.1] onlinestatus=5 → 尝试按钮登录 (ClickLoginButton)...")
        if try_relogin():
            log("\n✅ 按钮登录完成！")
            _post_login_verify()
            return
        log("\n[Step 2.2] 按钮登录未成功 → 回退到扫码登录")
        if RESTART_ON_BUTTON_LOGIN_FAIL and restarts_after_button_fail < MAX_RESTARTS_AFTER_BUTTON_LOGIN_FAIL:
            restarts_after_button_fail += 1
            ok = restart_wechat_before_qr("按钮登录失败，按配置先结束进程并重启，然后扫码登录...")
            if not ok:
                log("❌ 重启失败，退出")
                sys.exit(1)
    else:
        log(f"\n[Step 2.1] onlinestatus={online_status or '未知'} → 直接扫码登录 (跳过 ClickLoginButton)")

    # Step 3: 扫码登录 — 刷新二维码
    log("\n[Step 3] 刷新登陆二维码...")
    try:
        refresh_qr()
    except Exception as e:
        log(f"  ⚠ 刷新二维码失败: {e}")
        log("  → 继续尝试获取二维码...")

    # Step 4: 获取二维码图片
    log("\n[Step 4] 获取二维码图片...")
    img_bytes = None
    for qr_attempt in range(3):
        try:
            img_bytes = get_qr_image_bytes()
            if img_bytes:
                break
        except Exception as e:
            log(f"  ⚠ 获取二维码失败 (第{qr_attempt+1}次): {e}")
            if qr_attempt < 2:
                time.sleep(2)
    if not img_bytes:
        log("❌ 无法获取二维码图片，退出")
        sys.exit(1)

    # Step 5 & 6: 弹窗 + 轮询 (并行)
    log("\n[Step 5] 显示二维码，请扫码...")

    # 尝试用 tkinter 显示
    qr_root = None
    try:
        import tkinter as tk
        from PIL import Image, ImageTk

        img = Image.open(io.BytesIO(img_bytes))

        qr_root = tk.Tk()
        qr_root.title("微信登录 - 扫描二维码")
        qr_root.attributes("-topmost", True)

        max_size = 400
        w, h = img.size
        if w > max_size or h > max_size:
            ratio = min(max_size / w, max_size / h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        photo = ImageTk.PhotoImage(img)
        label = tk.Label(qr_root, image=photo)
        label.pack(padx=10, pady=10)
        hint = tk.Label(qr_root, text="请用微信扫描二维码\n登录成功后窗口自动关闭", font=("Microsoft YaHei", 10))
        hint.pack(pady=(0, 10))
        qr_root._photo = photo
        qr_root._should_close = False

        def check_close():
            if qr_root._should_close:
                qr_root.destroy()
            else:
                qr_root.after(500, check_close)

        qr_root.after(500, check_close)

        # 在后台线程中轮询登录状态
        poll_thread = threading.Thread(target=poll_login_status, args=(qr_root,), daemon=True)
        poll_thread.start()

        qr_root.mainloop()

        # 窗口关闭后等待线程
        poll_thread.join(timeout=3)

    except ImportError:
        log("⚠ 缺少 PIL (Pillow)，使用系统图片查看器显示二维码")
        _show_qr_fallback(img_bytes)
        poll_login_status()

    # ── 登录后验证 ──
    _post_login_verify()


if __name__ == "__main__":
    main()
