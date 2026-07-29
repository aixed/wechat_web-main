"""Message filtering and matching for group smart replies."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import threading
import time
from typing import Any


DEDUP_TTL_SECONDS = 300.0
DEDUP_MAX_ENTRIES = 5000
CHAT_COOLDOWN_SECONDS = 1.5

_PURE_LETTERS = re.compile(r"^[A-Za-z]+$")
_PURE_DIGITS = re.compile(r"^[0-9]+$")
_PURE_CHINESE = re.compile(r"^[\u4e00-\u9fff]+$")
_IGNORED_PREFIXES = ("位置", "[位置]", "[地图]", "[location]", "[图片]")


@dataclass(frozen=True)
class SmartReplyDecision:
    reply: str = ""
    reason: str = ""
    disable_after_send: bool = False

    @property
    def should_send(self) -> bool:
        return bool(self.reply)


class SmartReplyEngine:
    """Evaluates messages while maintaining de-duplication and cooldown state."""

    def __init__(
        self,
        *,
        dedup_ttl: float = DEDUP_TTL_SECONDS,
        dedup_limit: int = DEDUP_MAX_ENTRIES,
        cooldown: float = CHAT_COOLDOWN_SECONDS,
    ) -> None:
        self.dedup_ttl = max(0.0, float(dedup_ttl))
        self.dedup_limit = max(1, int(dedup_limit))
        self.cooldown = max(0.0, float(cooldown))
        self._seen: OrderedDict[tuple[str, str, str, str], float] = OrderedDict()
        self._last_sent: dict[tuple[str, str], float] = {}
        self._lock = threading.RLock()

    def evaluate(
        self,
        *,
        owner_wxid: str,
        chat_id: str,
        self_wxid: str,
        message: dict[str, Any],
        config: dict[str, Any] | None,
        now: float | None = None,
    ) -> SmartReplyDecision:
        if not config or not bool(config.get("enabled")):
            return SmartReplyDecision(reason="disabled")
        if not chat_id.endswith("@chatroom") or str(config.get("chat_id") or "") != chat_id:
            return SmartReplyDecision(reason="chat_not_configured")

        sender = str(message.get("fromid") or "").strip()
        content = str(message.get("msg") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        is_self = (
            str(message.get("sendorrecv") or "") == "1"
            or int(message.get("isSender") or 0) == 1
            or bool(self_wxid and sender == self_wxid)
        )
        if is_self:
            return SmartReplyDecision(reason="self_message")
        if not sender or not content:
            return SmartReplyDecision(reason="empty_message")
        if str(message.get("msgtype") or "") != "1":
            return SmartReplyDecision(reason="non_text_message")

        target_senders = {
            str(wxid or "").strip()
            for wxid in (config.get("target_senders") or [])
            if str(wxid or "").strip()
        }
        if sender not in target_senders:
            return SmartReplyDecision(reason="sender_not_allowed")

        lowered = content.casefold()
        if any(lowered.startswith(prefix.casefold()) for prefix in _IGNORED_PREFIXES):
            return SmartReplyDecision(reason="ignored_prefix")

        lines = content.split("\n")
        line_count = len(lines)
        if line_count == 1 and (
            _PURE_LETTERS.fullmatch(content)
            or _PURE_DIGITS.fullmatch(content)
            or _PURE_CHINESE.fullmatch(content)
        ):
            return SmartReplyDecision(reason="pure_single_line")
        if line_count < 2:
            return SmartReplyDecision(reason="too_few_lines")

        if line_count == 2:
            candidate = SmartReplyDecision(reply="1", reason="two_lines", disable_after_send=True)
        else:
            candidate = SmartReplyDecision(reason="keyword_not_matched")
            for rule in config.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                keyword = str(rule.get("keyword") or "").strip()
                reply = str(rule.get("reply") or "").strip()
                if keyword and reply and keyword.casefold() in lowered:
                    candidate = SmartReplyDecision(reply=reply, reason="keyword_matched")
                    break
            if not candidate.should_send:
                return candidate

        current = time.monotonic() if now is None else float(now)
        dedup_key = (str(owner_wxid or ""), chat_id, sender, content)
        cooldown_key = (str(owner_wxid or ""), chat_id)
        with self._lock:
            self._purge_seen(current)
            if dedup_key in self._seen:
                return SmartReplyDecision(reason="duplicate")
            self._seen[dedup_key] = current
            self._seen.move_to_end(dedup_key)
            while len(self._seen) > self.dedup_limit:
                self._seen.popitem(last=False)

            last_sent = self._last_sent.get(cooldown_key)
            if last_sent is not None and current - last_sent < self.cooldown:
                return SmartReplyDecision(reason="cooldown")
            self._last_sent[cooldown_key] = current
        return candidate

    def _purge_seen(self, now: float) -> None:
        if self.dedup_ttl <= 0:
            self._seen.clear()
            return
        cutoff = now - self.dedup_ttl
        while self._seen:
            _, seen_at = next(iter(self._seen.items()))
            if seen_at > cutoff:
                break
            self._seen.popitem(last=False)
