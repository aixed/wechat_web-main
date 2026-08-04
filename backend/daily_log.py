"""Non-blocking, category-based daily file logs."""

from __future__ import annotations

import atexit
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import threading
import time


LOG_ROOT = Path(__file__).resolve().parent / "logs"

_CATEGORY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOG_QUEUE: queue.Queue[tuple[datetime, str, str]] = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_WORKER_PID = 0


def daily_log_path(category: str, *, when: datetime | None = None) -> Path:
    """Return logs/<category>/YYYY-MM-DD.log for the supplied local time."""
    if not _CATEGORY_RE.fullmatch(str(category or "")):
        raise ValueError(f"invalid log category: {category!r}")
    current = when or datetime.now()
    return LOG_ROOT / category / f"{current:%Y-%m-%d}.log"


def _write_entry(when: datetime, category: str, text: str) -> None:
    try:
        path = daily_log_path(category, when=when)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)
            if not text.endswith("\n"):
                log_file.write("\n")
    except Exception:
        # Logging must never break message handling or API requests.
        pass


def _worker_main() -> None:
    while True:
        entry = _LOG_QUEUE.get()
        try:
            _write_entry(*entry)
        finally:
            _LOG_QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER, _WORKER_PID
    current_pid = os.getpid()
    if _WORKER_PID == current_pid and _WORKER is not None and _WORKER.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER_PID == current_pid and _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER_PID = current_pid
        _WORKER = threading.Thread(target=_worker_main, name="daily-log-writer", daemon=True)
        _WORKER.start()


def append_daily_log(category: str, text: str, *, when: datetime | None = None) -> None:
    """Queue one log entry without waiting for disk I/O."""
    current = when or datetime.now()
    try:
        daily_log_path(category, when=current)
        _ensure_worker()
        _LOG_QUEUE.put_nowait((current, category, str(text)))
    except Exception:
        pass


def flush_daily_logs(timeout: float = 2.0) -> bool:
    """Wait briefly for queued entries; intended for tests and clean shutdown."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _LOG_QUEUE.all_tasks_done:
        while _LOG_QUEUE.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _LOG_QUEUE.all_tasks_done.wait(remaining)
    return True


atexit.register(flush_daily_logs)
