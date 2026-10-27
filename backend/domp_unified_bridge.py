"""One visible Skill that safely dispatches data-maintenance intents."""
from __future__ import annotations

from runtime_settings import prompt, setting

import json
import re
from domp_identifier import identifiers_in, is_ticket_identifier, BATCH_IDENTIFIER_PROMPT
from typing import Any

from domp_approval_bridge import APPROVAL_TOOL, approval_candidate
from domp_bridge import (SQL_START, WORKFLOW_TOOL, write_capability_question,
    write_meta_discussion)
from domp_execution_bridge import EXECUTION_TOOL, execution_candidate
from domp_query_bridge import QUERY_TOOL, QUERY_TYPES, query_candidate
from domp_submission_bridge import SUBMISSION_TOOL, submission_candidate


UNIFIED_SKILL_ID = "domp_unified_agent"
UNIFIED_SKILL_NAME = setting('labels', 'domp_unified_bridge.unified_skill_name')
UNIFIED_INTENTS = {
    "sql_workflow",
    "submit",
    "query",
    "approve",
    "approve_execute",
    "execute",
    "approve_execute_all",
}

UNIFIED_PROMPT = prompt('domp_unified_bridge.unified_prompt')


UNIFIED_PROMPT += BATCH_IDENTIFIER_PROMPT


def submission_workflow_identifier(source: str) -> str:
    """Return the explicit ticket id for submit+approve+execute, otherwise empty."""
    source = str(source or "").strip()
    if not source or len(source) > 300 or SQL_START.search(source):
        return ""
    if write_meta_discussion(source) or write_capability_question(source):
        return ""
    if re.search(r"不要|别(?:再)?|不(?:要|用|能)?\s*(?:提交|填报|上报|审核|审批|执行)|停止|取消|如果|假如|明天|以后|计划|打算|怎么|如何|例如|比如|示例|假设|转述|转发|引用", source):
        return ""
    if not (re.search(r"提交|填报|上报", source) and re.search(r"审核|审批", source) and re.search(r"执行", source)):
        return ""
    if not re.search(r"数据\s*运维|运维(?:单|需求)|填报单", source):
        return ""
    identifiers = identifiers_in(source)
    if len(identifiers) != 1:
        return ""
    identifier = next(iter(identifiers))
    return identifier if is_ticket_identifier(identifier) else ""


def is_unified_task(task: dict[str, Any]) -> bool:
    """Recognize the protected auto-select Skill without relying on tool name."""
    skill_id = str(task.get("skill_id") or "").strip()
    task_id = str(task.get("id") or "").strip()
    return skill_id == UNIFIED_SKILL_ID or task_id.startswith(UNIFIED_SKILL_ID + "_")


def unified_candidate(source: str, history=(), *, message_type: str = "text", allow_sql: bool = True) -> bool:
    """Cheap prefilter; the model and each fixed bridge still validate intent."""
    source = str(source or "").strip()
    if not source:
        return False
    has_sql = bool(SQL_START.search(source))
    if has_sql:
        return bool(allow_sql and message_type in {"text", "file"})
    if message_type != "text":
        return False
    return any((
        submission_workflow_identifier(source),
        submission_candidate(source),
        approval_candidate(source),
        execution_candidate(source),
        query_candidate(source, history),
    ))


def _parsed_result(analysis: dict[str, Any]) -> dict[str, Any]:
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            parsed = None
    if not isinstance(parsed, dict):
        raise ValueError("统一 Agent 没有返回有效的业务意图")
    return parsed


def unified_route(source: str, analysis: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Convert a bounded intent into an existing, independently validated route."""
    parsed = _parsed_result(analysis)
    intent = str(parsed.get("intent") or "").strip()
    if intent not in UNIFIED_INTENTS:
        raise ValueError("未识别到可安全处理的数据运维请求")
    has_sql = bool(SQL_START.search(str(source or "")))
    if has_sql and intent != "sql_workflow":
        raise ValueError("消息包含 SQL，但意图分析不一致，未调用任何业务工具")
    if not has_sql and intent == "sql_workflow":
        raise ValueError("没有识别到 SQL，未启动数据运维流程")

    common = {
        "matched": bool(analysis.get("matched")),
        "confidence": int(analysis.get("confidence") or 0),
        "items": [],
        "reply": "",
    }
    identifier = str(parsed.get("identifier") or "")
    if intent == "sql_workflow":
        routed = {
            "identifier": identifier,
            "sql_purpose": str(parsed.get("sql_purpose") or ""),
            "risk_level": str(parsed.get("risk_level") or "unknown"),
            "risk_notes": [str(item) for item in (parsed.get("risk_notes") or [])],
        }
        return WORKFLOW_TOOL, {**common, "result": json.dumps(routed, ensure_ascii=False)}
    if intent == "submit":
        routed = {"action": "submit", "identifier": identifier}
        return SUBMISSION_TOOL, {**common, "result": json.dumps(routed, ensure_ascii=False)}
    if intent == "query":
        routed = {"identifier": identifier}
        if parsed.get("needs_clarification") is True:
            routed["needs_clarification"] = True
        else:
            query_type = str(parsed.get("query_type") or "")
            if query_type not in QUERY_TYPES:
                raise ValueError("未识别到支持的查询类型")
            routed["query_type"] = query_type
        return QUERY_TOOL, {**common, "result": json.dumps(routed, ensure_ascii=False)}
    if intent in {"approve", "approve_execute"}:
        routed = {"action": intent, "identifier": identifier}
        return APPROVAL_TOOL, {**common, "result": json.dumps(routed, ensure_ascii=False)}
    action = "approve_execute_all" if intent == "approve_execute_all" else "execute"
    routed = {"action": action, "identifier": identifier}
    return EXECUTION_TOOL, {**common, "result": json.dumps(routed, ensure_ascii=False)}


def owner_unified_config(config, message, owner, history=()):
    """Allow the logged-in owner's explicit text request through this one Skill."""
    if (not config.get("enabled") or str(message.get("msgtype")) != "1" or not owner
            or str(message.get("fromid") or "") != owner
            or not unified_candidate(message.get("msg"), history, allow_sql=False)):
        return None
    tasks = [task for task in config.get("ai_tasks") or [] if task.get("enabled") and task.get("mcp_enabled")
             and is_unified_task(task) and task.get("message_type", "text") == "text"]
    if not tasks:
        return None
    return {**config, "rules": [], "ai_tasks": tasks, "message_types": ["text"],
            "mention_only": False, "mention_message_types": [],
            "target_senders_by_type": {"text": [owner]}}


def owner_filehelper_config(config, message, owner, history=()):
    """Route the owner's addressed SQL through configured data tasks in filehelper.

    The caller verifies the configured assistant mention before using this route. Ordinary conversations,
    other senders and keyword/custom tasks retain their usual self filtering.
    """
    if (config.get("chat_id") != "filehelper" or not config.get("enabled")
            or "text" not in config.get("message_types", [])
            or not owner or str(message.get("fromid") or "") != owner
            or str(message.get("msgtype")) != "1"
            or not unified_candidate(message.get("msg"), history)):
        return None
    tasks = [task for task in config.get("ai_tasks") or []
             if task.get("enabled") and task.get("mcp_enabled")
             and task.get("message_type", "text") == "text"
             and (is_unified_task(task) or task.get("mcp_tool_name") == WORKFLOW_TOOL)]
    if not tasks:
        return None
    return {**config, "rules": [], "ai_tasks": tasks, "message_types": ["text"],
            "mention_only": False, "mention_message_types": [],
            "target_senders_by_type": {"text": [owner]}}
