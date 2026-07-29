"""Message filtering and matching for group smart replies."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import threading
import time
from typing import Any
from xml.etree import ElementTree


DEDUP_TTL_SECONDS = 300.0
DEDUP_MAX_ENTRIES = 5000
CHAT_COOLDOWN_SECONDS = 1.5

_PURE_LETTERS = re.compile(r"^[A-Za-z]+$")
_PURE_DIGITS = re.compile(r"^[0-9]+$")
_PURE_CHINESE = re.compile(r"^[\u4e00-\u9fff]+$")
_IGNORED_PREFIXES = ("位置", "[位置]", "[地图]", "[location]")
_DEFAULT_MESSAGE_TYPES = {"text"}
_MESSAGE_PLACEHOLDERS = {
    "image": "[图片]",
    "gif": "[GIF]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


def _app_message_type(content: str) -> str:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return ""
    app_message = root if root.tag.casefold() == "appmsg" else root.find(".//appmsg")
    if app_message is None:
        return ""
    return str(app_message.findtext("type") or "").strip()


def _message_category(msg_type: str, content: str) -> str:
    lowered = content.casefold()
    media_prefixes = (
        ("image", ("[图片]",)),
        ("gif", ("[gif]", "[表情]")),
        ("voice", ("[语音]",)),
        ("video", ("[视频]",)),
        ("file", ("[文件]",)),
    )
    for category, prefixes in media_prefixes:
        if any(lowered.startswith(prefix.casefold()) for prefix in prefixes):
            return category
    if msg_type == "1":
        return "text"
    if msg_type == "3":
        return "image"
    if msg_type == "34":
        return "voice"
    if msg_type == "43":
        return "video"
    if msg_type == "47":
        return "gif"
    if msg_type in {"10000", "10002"}:
        if (
            "revokemsg" in lowered
            or "撤回了一条消息" in content
            or "recalled a message" in lowered
        ):
            return "recall"
        return "system"
    if msg_type == "49":
        app_message_type = _app_message_type(content)
        if app_message_type == "57" or "<refermsg" in lowered:
            return "quote"
        if app_message_type == "6":
            return "file"
        return "xml"
    if content.lstrip().startswith("<"):
        return "xml"
    return ""


def _xml_match_content(content: str) -> str:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return content
    parts: list[str] = []
    for value in root.itertext():
        normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if normalized:
            parts.append(normalized)
    return "\n".join(parts) or content


@dataclass(frozen=True)
class SmartReplyDecision:
    replies: tuple[str, ...] = ()
    reason: str = ""
    disable_after_send: bool = False

    @property
    def should_send(self) -> bool:
        return bool(self.replies)

    @property
    def reply(self) -> str:
        """Keep the first-reply accessor for existing single-reply callers."""
        return self.replies[0] if self.replies else ""


def _compile_rule_matcher(keyword: str, use_regex: bool):
    if use_regex:
        try:
            pattern = re.compile(keyword, re.IGNORECASE | re.MULTILINE)
        except re.error:
            return None
        return pattern.search

    folded_keyword = keyword.casefold()
    return lambda value: folded_keyword in value.casefold()


def _strip_trailing_digits(value: str) -> str:
    return re.sub(r"\d+\s*$", "", value).rstrip()


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
        raw_content = str(message.get("msg") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        is_self = (
            str(message.get("sendorrecv") or "") == "1"
            or int(message.get("isSender") or 0) == 1
            or bool(self_wxid and sender == self_wxid)
        )
        if is_self:
            return SmartReplyDecision(reason="self_message")
        if not sender:
            return SmartReplyDecision(reason="empty_message")

        message_category = _message_category(str(message.get("msgtype") or ""), raw_content)
        if not message_category:
            return SmartReplyDecision(reason="non_text_message")
        configured_message_types = config.get("message_types")
        enabled_message_types = {
            str(value or "").strip()
            for value in configured_message_types
            if str(value or "").strip()
        } if isinstance(configured_message_types, list) else set(_DEFAULT_MESSAGE_TYPES)
        if message_category not in enabled_message_types:
            return SmartReplyDecision(reason="message_type_not_enabled")

        if message_category in {"xml", "system", "recall", "quote", "file"}:
            content = _xml_match_content(raw_content)
        else:
            content = raw_content
        content = content or _MESSAGE_PLACEHOLDERS.get(message_category, "")
        if not content:
            return SmartReplyDecision(reason="empty_message")

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
        candidate = SmartReplyDecision(reason="keyword_not_matched")
        matched_lines_by_index: dict[int, str] = {}
        fixed_replies: list[str] = []
        # Each message category has its own rule surface. Only text rules are
        # implemented today; enabled non-text categories remain inert until
        # their dedicated configuration is added.
        message_rules = (config.get("rules") or []) if message_category == "text" else []
        for rule in message_rules:
            if not isinstance(rule, dict):
                continue
            keyword = str(rule.get("keyword") or "").strip()
            reply = str(rule.get("reply") or "").strip()
            reply_with_matched_line = bool(rule.get("reply_with_matched_line"))
            if not keyword or (not reply_with_matched_line and not reply):
                continue
            matcher = _compile_rule_matcher(keyword, bool(rule.get("use_regex")))
            if matcher is None:
                continue
            if reply_with_matched_line:
                for line_index, line in enumerate(lines):
                    if not matcher(line):
                        continue
                    cleaned = _strip_trailing_digits(line.strip())
                    if cleaned:
                        matched_lines_by_index.setdefault(line_index, cleaned)
            elif matcher(content):
                fixed_replies.append(reply)

        matched_line_replies = tuple(
            matched_lines_by_index[index]
            for index in sorted(matched_lines_by_index)
        )
        if matched_line_replies or fixed_replies:
            candidate = SmartReplyDecision(
                replies=matched_line_replies + tuple(fixed_replies),
                reason="matched_lines" if matched_line_replies else "keyword_matched",
            )

        # Configured keywords take priority over the legacy line-count rules so a
        # match at any position, including the final line, always triggers.
        if not candidate.should_send:
            if message_category != "text":
                return candidate
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
                candidate = SmartReplyDecision(replies=("1",), reason="two_lines", disable_after_send=True)
            else:
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
