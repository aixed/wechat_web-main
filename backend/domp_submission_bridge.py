"""Explicit, SQL-free migration and submission of an existing operation ticket."""
from __future__ import annotations

from runtime_settings import prompt, setting

import json
import re
from domp_identifier import identifiers_in, BATCH_IDENTIFIER_PROMPT

from domp_bridge import (SQL_START, split_reply, write_capability_question,
    write_meta_discussion)

SUBMISSION_TOOL = "submit_data_maintenance"
SUBMISSION_REPLY_PREFIX = setting('labels', 'domp_submission_bridge.submission_reply_prefix')
SUBMISSION_PROMPT = prompt('domp_submission_bridge.submission_prompt')


SUBMISSION_PROMPT += BATCH_IDENTIFIER_PROMPT


def submission_candidate(source):
    source = str(source or "").strip()
    if len(source) > 300 or SQL_START.search(source) or any(marker in source for marker in
        (SUBMISSION_REPLY_PREFIX, "数据运维查询结果", "数据运维执行结果", "流程标识：")):
        return False
    if write_meta_discussion(source) or write_capability_question(source):
        return False
    if re.search(r"审核|审批|执行|不要|不用\s*(?:再|帮我|帮忙)?\s*(?:提交|填报|上报)|不\s*(?:提交|填报|上报)|别\s*(?:提交|填报|上报)|停止|取消|如果|假如|明天|以后|计划|打算|怎么|如何|已经|已提交|有没有|提交(?:了|过)?(?:吗|没)|提交(?:的)?(?:情况|结果|状态)|(?:他|她|别人|有人)说|转述|转发|引用|例如|比如|示例|假设|提示词|强制|force|重新提交|重复提交", source, re.I):
        return False
    domain = r"(?:数据\s*运维(?:需求|填报)?(?:单|申请|需求)?|运维(?:单|需求)|填报单)"
    request = r"(?:^|[，,。；;]|帮我|帮忙|替我|麻烦(?:你|您)?|劳驾(?:你|您)?|请(?:你|您)?)\s*"
    return bool(re.search(request + r"(?:把|将)?\s*(?:(?:提交|填报|上报)\s*(?:一下|下)?\s*" + domain +
        r"|" + domain + r"\s*(?:xzsb-)?\d{5}(?:-[1-9]\d*)?\s*(?:保存并)?提交)", source, re.I))


def submission_arguments(source, analysis):
    if not submission_candidate(source):
        raise ValueError("请明确说明：提交数据运维填报单 41356。")
    identifiers = identifiers_in(source, strict=True)
    if len(identifiers) != 1 or not re.fullmatch(r"\d{5}(?:-[1-9]\d*)?", next(iter(identifiers))):
        raise ValueError("请指定唯一的五位旧运维单标识，可带 -1 等补充批次；不会批量提交或从历史猜单号。")
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            parsed = None
    identifier = next(iter(identifiers))
    if not isinstance(parsed, dict) or parsed.get("action") != "submit" or re.sub(r"^xzsb-", "", str(parsed.get("identifier") or ""), flags=re.I) != identifier:
        raise ValueError("识别的填报动作或单号与原消息不一致，未提交。")
    return {"identifier": identifier, "force": False}


def submission_reply(payload, identifier):
    lines = [SUBMISSION_REPLY_PREFIX, "需求：" + identifier]
    if not isinstance(payload, dict) or str(payload.get("identifier") or "").removeprefix("xzsb-") != identifier:
        lines.append("未取得该单据的结构化结果，填报状态待核对，请勿重复提交。")
    elif payload.get("submitted") is True:
        lines.append("填报：保存并提交成功")
        lines.append("需求编号：" + str(payload.get("work_num") or "系统未返回"))
        if payload.get("area_name"):
            lines.append("行政区划：" + str(payload["area_name"]))
        if payload.get("reused"):
            lines.append("已复用本服务此前的成功提交结果。")
            if (payload.get("resubmission") or {}).get("message"):
                lines.append(str(payload["resubmission"]["message"]))
    else:
        lines.append("填报：未确认提交成功；" + str(payload.get("message") or "系统未返回提交成功状态"))
    return "\n".join(lines)


def submission_reply_chunks(reply):
    return tuple(SUBMISSION_REPLY_PREFIX + "\n" + part for part in split_reply(str(reply).removeprefix(SUBMISSION_REPLY_PREFIX + "\n"), limit=1800-len(SUBMISSION_REPLY_PREFIX)-1))


def owner_submission_config(config, message, owner):
    if not config.get("enabled") or str(message.get("msgtype")) != "1" or not owner or str(message.get("fromid") or "") != owner or not submission_candidate(message.get("msg")):
        return None
    tasks = [task for task in config.get("ai_tasks") or [] if task.get("enabled") and task.get("mcp_enabled") and task.get("mcp_tool_name") == SUBMISSION_TOOL and task.get("message_type", "text") == "text"]
    if not tasks:
        return None
    return {**config, "rules": [], "ai_tasks": tasks, "message_types": ["text"],
        "mention_only": False, "mention_message_types": [], "target_senders_by_type": {"text": [owner]}}
