"""Deterministic SQL workflow routing and truthful business-result replies."""
from __future__ import annotations

from runtime_settings import prompt, setting

import hashlib
import json
import re
from domp_identifier import identifiers_in

WORKFLOW_TOOL = "process_data_maintenance"
SQL_START = re.compile(r"\b(?:UPDATE|SELECT|INSERT|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|BEGIN|DECLARE|CALL|EXECUTE)\b", re.I)

_POLITE_WRITE_REQUEST = re.compile(
    r"(?:能不能|能否|可否|可不可以|是否可以|可以不可以|可以)?\s*"
    r"(?:帮我|帮忙|替我|麻烦(?:你|您)?|劳驾(?:你|您)?|请(?:你|您)?|把|将)",
    re.I,
)
_WRITE_CAPABILITY_QUESTION = re.compile(
    r"能否|可否|能不能|可不可以|是否(?:可以|能够|能)?|可以.{0,40}(?:吗|么|不)[？?。!！]*$",
    re.I,
)
_WRITE_META_DISCUSSION = re.compile(
    r"提示词|prompt|(?:识别|理解|支持|触发).{0,30}(?:吗|么|规则|指令|说法|请求)|"
    r"(?:这|那|这个|那个)(?:句|段|句话|说法|指令|请求|消息).{0,40}(?:识别|理解|支持|触发|处理)",
    re.I | re.S,
)


def polite_write_request(source):
    """Return whether a question-shaped phrase still directly asks us to act."""
    return bool(_POLITE_WRITE_REQUEST.search(str(source or "")))


def write_capability_question(source):
    """Separate capability questions from polite imperative write requests."""
    source = str(source or "")
    return bool(_WRITE_CAPABILITY_QUESTION.search(source)) and not polite_write_request(source)


def write_meta_discussion(source):
    """Reject tests or discussions about wording before any write-intent model runs."""
    return bool(_WRITE_META_DISCUSSION.search(str(source or "")))

def _ends_with_sql_semicolon(source):
    state, quote, last, index = "normal", "", "", 0
    while index < len(source):
        char = source[index]
        pair = source[index:index + 2]
        if state == "line":
            if char == "\n":
                state = "normal"
        elif state == "block":
            if pair == "*/":
                state = "normal"
                index += 1
        elif state == "quote":
            if char == quote:
                if source[index + 1:index + 2] == quote:
                    index += 1
                else:
                    state = "normal"
            elif char == "\\":
                index += 1
        elif pair in {"--", "/*"}:
            state = "line" if pair == "--" else "block"
            index += 1
        elif char in "'\"`[":
            state, quote, last = "quote", "]" if char == "[" else char, char
        elif not char.isspace():
            last = char
        index += 1
    return state in {"normal", "line"} and last == ";"


def strip_chat_mentions(source, mention_names):
    """Remove confirmed message mentions, preserving SQL parameters and data."""
    source = str(source or "")
    names = sorted({str(name) for name in mention_names or [] if name and not re.search(r"[\r\n]", str(name))}, key=len, reverse=True)
    if not names:
        return source
    mention = r"@(?:" + "|".join(re.escape(name) for name in names) + r")"
    while True:
        prefix = re.match(r"^[\s\u2005]*" + mention + r"(?=[\s\u2005]|$)", source)
        if not prefix:
            break
        source = source[prefix.end():].lstrip(" \t\r\n\u2005")
    suffix_patterns = (
        r"(?m)^[ \t\u2005]*(?:" + mention + r"(?:[\s\u2005]+|\Z))+\Z",
        r"[ \t\u2005]+(?:" + mention + r"(?:[\s\u2005]+|\Z))+\Z",
    )
    for suffix_pattern in suffix_patterns:
        suffix = re.search(suffix_pattern, source)
        if suffix and _ends_with_sql_semicolon(source[:suffix.start()]):
            source = source[:suffix.start()].rstrip()
            break
    return source


PROMPT = prompt('domp_bridge.prompt')


def workflow_arguments(source, analysis, task):
    source = str(source or "").strip()
    match = SQL_START.search(source)
    if not match:
        raise ValueError("没有识别到 SQL，未启动数据运维流程")
    if len(source) > 20_000:
        raise ValueError("原文超过 20000 字符，不能截断 SQL 后执行，请缩小文件")
    header = source[:match.start()]
    ids = identifiers_in(header, allow_work_numbers=False, strict=True)
    if len(ids) != 1:
        raise ValueError("缺少唯一的线下编号或需求编号，请在文本开头或 TXT 文件名中注明，例如 --- xzsb-42157-1")
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try: parsed = json.loads(parsed)
        except ValueError: parsed = None
    if not isinstance(parsed, dict):
        raise ValueError("风险分析结果不完整，未填报、审核或执行")
    level = str(parsed.get("risk_level") or "unknown").casefold()
    if level not in {"low", "medium"}:
        notes = parsed.get("risk_notes") or []
        raise ValueError("SQL 风险较高或不明确，需人工处理。" + "；".join(str(x) for x in notes))
    identifier = next(iter(ids))
    if str(parsed.get("identifier") or "") != identifier:
        raise ValueError("分析需求编号与原消息不一致，已停止流程")
    purpose = str(parsed.get("sql_purpose") or "").strip()
    if not purpose:
        raise ValueError("没有取得 SQL 用途分析，已停止流程")
    context = task.get("_source_context") or {}
    context_key = "|".join(str(context.get(k) or "") for k in ("owner_wxid", "chat_id", "sender", "message_id"))
    if not context.get("message_id"):
        context_key += "|" + source
    request_id = "wechat:" + hashlib.sha256(context_key.encode("utf-8")).hexdigest()
    return {"identifier": identifier, "sql_text": source, "sql_purpose": purpose,
            "risk_level": level, "request_id": request_id, "dry_run": not bool(context)}


def workflow_reply(payload, arguments):
    if not isinstance(payload, dict):
        return "数据运维工具没有返回结构化结果。结果待核对，请勿重复提交或执行。"
    identifier = payload.get("identifier") or arguments.get("identifier") or "未知"
    lines = [f"需求 {identifier}"]
    if payload.get("duplicate"):
        lines.append("该编号已有填报记录，本次未填报、审核或执行。")
        if payload.get("existing_work_num"):
            lines.append("原需求编号：" + str(payload["existing_work_num"]))
        lines.append(str((payload.get("resubmission") or {}).get("message") or "请使用新的补充批次编号重新发送完整 SQL。"))
        return "\n".join(lines)
    validation = payload.get("validation") or {}
    if validation.get("status") == "未通过":
        lines.append("SQL 校验未通过：" + str(validation.get("message") or "请检查 SQL 是否完整"))
        lines.append("本次请求未填报、审核或执行，请修正 SQL 后重新发送。")
        return "\n".join(lines)
    if payload.get("work_num"):
        lines.append(f"需求编号：{payload['work_num']}")
    lines.append("SQL 用途：" + str(arguments.get("sql_purpose") or payload.get("sql_purpose") or "未知"))
    risk = payload.get("risk") or {}
    lines.append("风险：" + {"low": "低", "medium": "中", "high": "高", "unknown": "未知"}.get(str(risk.get("risk_level") or arguments.get("risk_level")), "未知"))
    for name, label in (("submission", "填报"), ("approval", "审核"), ("execution", "执行")):
        stage = payload.get(name) or {}
        lines.append(f"{label}：{stage.get('status') or '未取得结果'}" + (f"；{stage['message']}" if stage.get("message") else ""))
    execution = payload.get("execution") or {}
    total = execution.get("affected_rows_total")
    lines.append("影响条数合计：" + ("未知" if total is None else str(total)))
    for index, detail in enumerate(execution.get("details") or [], 1):
        count = detail.get("affected_rows")
        elapsed = detail.get("elapsed_seconds")
        lines.append(f"SQL {index}：{detail.get('status') or '未知'}，影响 {count if count is not None else '未知'} 条" + (f"，耗时 {elapsed} 秒" if elapsed not in (None, "") else ""))
        if detail.get("exception"):
            lines.append(f"SQL {index} 异常详情：{detail['exception']}")
    if not execution.get("exceptions") and execution.get("complete"):
        lines.append("异常详情：无")
    elif not execution.get("details"):
        lines.append("异常详情：" + ("；".join(str(x) for x in execution.get("exceptions") or []) or "尚未取得执行结果"))
    for note in risk.get("risk_notes") or []:
        lines.append("风险说明：" + str(note))
    if payload.get("dry_run"):
        lines.append("此为测试，只分析，未修改业务系统。")
    if payload.get("reused") and (payload.get("resubmission") or {}).get("message"):
        lines.append(str(payload["resubmission"]["message"]))
    if execution.get("status") == "待核对" or (payload.get("submission") or {}).get("status") == "待核对":
        lines.append("结果待核对，请勿重复执行。流程标识：" + str(payload.get("request_id") or arguments.get("request_id") or ""))
    return "\n".join(lines)


def split_reply(reply, limit=1800):
    # Preserve exception text in full and send long results in order.
    return tuple(reply[i:i+limit] for i in range(0, len(reply), limit))
