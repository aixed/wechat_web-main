"""Explicit execution of an existing approved demand, pinned to trusted IDs."""
from __future__ import annotations

from runtime_settings import prompt, setting

import json
import re
from domp_identifier import identifiers_in, is_ticket_identifier, TICKET_PATTERN, BATCH_IDENTIFIER_PROMPT

from domp_bridge import (SQL_START, split_reply, write_capability_question,
    write_meta_discussion)
from domp_query_bridge import QUERY_TOOL, QUERY_REPLY_PREFIX, EXECUTION_REPLY_PREFIX, execution_question, query_reply
from mcp_service import McpServiceError

EXECUTION_TOOL = "execute_data_maintenance"
EXECUTION_PROMPT = prompt('domp_execution_bridge.execution_prompt')


EXECUTION_PROMPT += BATCH_IDENTIFIER_PROMPT


def approval_execution_candidate(source):
    source = str(source or "").strip()
    if len(source) > 300 or SQL_START.search(source) or any(p in source for p in
        (QUERY_REPLY_PREFIX, EXECUTION_REPLY_PREFIX, "数据运维填报结果", "流程标识：")):
        return False
    if write_meta_discussion(source) or write_capability_question(source):
        return False
    if re.search(r"不要|别|不(?:要|用|能)?\s*(?:审核|审批|执行)|停止|取消|如果|假如|明天|以后|计划|打算|怎么|如何|(?:他|她|别人|有人)说|转述|转发|引用|例如|比如|示例|假设|提示词", source):
        return False
    return bool(re.search(r"全部|所有|都|上面(?:的)?(?:这些)?|这些", source)
        and re.search(r"审核|审批", source) and re.search(r"执行", source))


def execution_candidate(source):
    source = str(source or "").strip()
    if approval_execution_candidate(source):
        return True
    if len(source) > 300 or SQL_START.search(source) or any(p in source for p in (QUERY_REPLY_PREFIX, EXECUTION_REPLY_PREFIX, "数据运维填报结果", "流程标识：")):
        return False
    if write_meta_discussion(source) or write_capability_question(source):
        return False
    if execution_question(source) or re.search(r"执行(?:的)?(?:结果|情况|状态|详情|明细|脚本|耗时|影响)|待执行|不要|别\s*执行|不\s*执行|不能\s*(?:再|直接)?\s*执行|不用\s*(?:再|帮我|帮忙)?\s*执行|停止|取消|如果|假如|明天|以后|计划|打算|怎么执行|如何执行|(?:他|她|别人|有人)说|转述|转发|引用|例如|比如|示例|假设|提示词", source):
        return False
    # Route clear requests inside natural speech, including greetings and
    # mentions. The model still confirms the action, and IDs are validated
    # against the original message before the single-purpose MCP route.
    request = r"(?:帮我|帮忙|替我|麻烦(?:你|您)?|劳驾(?:你|您)?|请(?:你|您)?)\s*"
    clause_start = r"(?:^|[，,。；;])\s*"
    modifiers = r"(?:(?:先|再|现在|尽快|马上|直接|把|将)\s*)*"
    target = r"(?:(?:需求|单号|数据运维(?:单)?|单据)?\s*(?:xzsb-)?(?:" + TICKET_PATTERN + r"|\d{6,32})\s*|(?:这|那)(?:条|单|个)(?:需求|单据)?\s*)?"
    return bool(re.search(r"(?:" + clause_start + r"|" + request + r")" + modifiers + target + r"(?:的)?\s*(?:数据运维)?执行", source))


def approval_execution_targets(source, analysis, history):
    if not approval_execution_candidate(source):
        raise ValueError("请明确说明：把上一轮待审核单据都审核并执行。")
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict) or parsed.get("action") != "approve_execute_all" or parsed.get("identifier"):
        raise ValueError("未识别到明确的批量审核执行指令，未处理。")
    last = history[-1] if history else {}
    if (last.get("arguments") or {}).get("query_type") not in {"pending_approvals", "pending_maintenance"}:
        raise ValueError("当前没有可承接的待审核清单，请先查询待审核单据。会话超过一周会重建。")
    targets = last.get("approval_targets")
    if targets is None:
        answer = str(last.get("answer") or "")
        count_match = re.search(r"目前待审核需求：(\d+) 条(?:\n|$)", answer)
        rows = re.findall(r"^\d+\. ([^；\n]+)；工作编号 (\d{6,32})；[^\n]+$", answer, re.M)
        if answer.startswith(QUERY_REPLY_PREFIX) and count_match and int(count_match.group(1)) == len(rows):
            targets = {"count": len(rows), "items": [
                {"identifier": identifier, "work_num": work_num, "work_id": ""}
                for identifier, work_num in rows]}
    if not isinstance(targets, dict) or not isinstance(targets.get("items"), list):
        raise ValueError("上一轮没有经过工具验证的待审核目标，未处理。")
    count, items = targets.get("count"), targets["items"]
    if not isinstance(count, int) or count < 1:
        raise ValueError("上一轮待审核清单为空，未处理。")
    if count > 20 or len(items) != count:
        raise ValueError("待审核清单超过 20 条或未完整展示，请缩小范围后再操作。")
    normalized = []
    seen = set()
    for item in items:
        target = {key: str(item.get(key) or "") for key in ("identifier", "work_id", "work_num")}
        key = target["work_num"]
        if (not target["identifier"] or not re.fullmatch(r"\d{6,32}", target["work_num"])
            or (target["work_id"] and not re.fullmatch(r"\d{6,32}", target["work_id"])) or key in seen):
            raise ValueError("上一轮待审核清单缺少唯一稳定编号，未处理。")
        seen.add(key); normalized.append(target)
    return normalized


def context_execution_target(history):
    last = history[-1] if history else {}
    if (last.get("arguments") or {}).get("query_type") not in {"pending_executions", "pending_maintenance"}:
        raise ValueError("当前没有明确待执行单据，请先查询待执行清单或说明：执行 41184。会话超过一周会重建。")
    targets = last.get("execution_targets")
    if targets is None:
        # Legacy sessions contain the program's canonical reply, not the model
        # output. Pin its work number; never substitute a fresh singleton.
        answer = str(last.get("answer") or "")
        rows = re.findall(r"^\d+\. (" + TICKET_PATTERN + r")；工作编号 (\d{6,32})；[^\n]+$", answer, re.M)
        if answer.startswith(QUERY_REPLY_PREFIX) and re.search(r"目前待执行需求：1 条(?:\n|$)", answer) and len(rows) == 1:
            targets = {"count": 1, "items": [{"identifier": rows[0][0], "work_num": rows[0][1]}]}
    if not isinstance(targets, dict) or targets.get("count") != 1 or len(targets.get("items") or []) != 1:
        raise ValueError("上一轮待执行清单不是唯一单据，请明确要执行的需求编号。不会批量执行。")
    target = targets["items"][0]
    if not re.fullmatch(r"\d{6,32}", str(target.get("work_num") or "")):
        raise ValueError("上一轮没有有效的工作编号，请重新查询待执行清单。")
    return target


def execution_target(source, analysis, history):
    if not execution_candidate(source):
        raise ValueError("请明确说明需要执行的已有审批通过单据。")
    if re.search(r"全部|所有|批量|一起|都执行|逐个|分别", str(source)):
        raise ValueError("请指定唯一的需求编号，不支持批量执行。")
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict) or parsed.get("action") != "execute":
        raise ValueError("未识别到明确执行指令，请说明：执行 41184。")
    ids = identifiers_in(source, strict=True)
    if len(ids) > 1:
        raise ValueError("请指定唯一的需求编号，不支持批量执行。")
    if ids:
        identifier = next(iter(ids))
        actual = re.sub(r"^xzsb-", "", str(parsed.get("identifier") or ""), flags=re.I)
        if actual != identifier:
            raise ValueError("识别编号与当前消息不一致，未执行。")
        return {"identifier": identifier, "work_num": ""}
    target = context_execution_target(history)
    actual = str(parsed.get("identifier") or "")
    if actual and actual not in {target.get("identifier"), target["work_num"]}:
        raise ValueError("识别目标与上一轮待执行单据不一致，未执行。")
    return {**target, "identifier": target.get("identifier") or target["work_num"]}


def execution_reply(payload, identifier, work_num="", note=""):
    if isinstance(payload, dict) and "execution" not in payload and "status" in payload:
        payload = {"query_type": "execution", "identifier": identifier, "work_num": work_num, "execution": payload}
    elif isinstance(payload, dict):
        payload = {**payload, "identifier": payload.get("identifier") or identifier, "work_num": payload.get("work_num") or work_num}
    body = query_reply(payload).removeprefix(QUERY_REPLY_PREFIX).lstrip("：\n")
    return EXECUTION_REPLY_PREFIX + "\n" + (note+"\n" if note else "") + body


def execution_reply_chunks(reply):
    body = str(reply).removeprefix(EXECUTION_REPLY_PREFIX).lstrip("：\n")
    return tuple(EXECUTION_REPLY_PREFIX+"\n"+part for part in split_reply(body, 1800-len(EXECUTION_REPLY_PREFIX)-1))


async def execute_target(target, call_tool, *, dry_run=False):
    """Exactly one write call; subsequent verification is always read-only."""
    identifier, pinned_num = target["identifier"], target.get("work_num") or ""
    called = await call_tool("list_data_maintenance_executions", {})
    pending = called.get("structured")
    if not isinstance(pending, dict) or not isinstance(pending.get("items"), list):
        raise ValueError("未取得待执行清单，未执行。")
    matches = [item for item in pending["items"] if (
        str(item.get("work_num") or "") == pinned_num if pinned_num else
        str(item.get("work_name") or "") == identifier if is_ticket_identifier(identifier) else
        str(item.get("work_num") or "") == identifier)]
    if len(matches) > 1:
        raise ValueError("当前存在多条同名需求，请指定完整工作编号，未执行。")
    if not matches:
        called = await call_tool("query_data_maintenance", {"query_type": "execution", "identifier": pinned_num or identifier})
        return execution_reply(called.get("structured"), identifier, pinned_num,
            "该单据不在当前待执行清单，本次未发起执行；以下为只读核对结果。"), None
    item = matches[0]
    work_id, work_num = str(item.get("work_id") or ""), str(item.get("work_num") or "")
    if not work_id or not work_num or str(item.get("work_status")) != "2":
        raise ValueError("未取得审批通过单据的稳定编号，未执行。")
    if target.get("work_id") and target["work_id"] != work_id:
        raise ValueError("上一轮单据 ID 与当前清单不一致，未执行。")
    if dry_run:
        return EXECUTION_REPLY_PREFIX+f"：已定位需求 {identifier}，工作编号 {work_num}。Skill 测试仅查询，未发起执行。", None
    try:
        called = await call_tool(EXECUTION_TOOL, {"work_id": work_id, "work_num": work_num})
    except McpServiceError as exc:
        try:
            checked = await call_tool("get_data_maintenance_execution_result", {"work_id": work_id, "work_num": work_num})
            payload = checked.get("structured")
            # An empty pre-execution view cannot resolve an uncertain write.
            if not isinstance(payload, dict) or not payload.get("complete"):
                raise ValueError("未取得完整执行明细")
        except (McpServiceError, ValueError):
            payload = {"status": "待核对", "complete": False, "affected_rows_total": None,
                       "details": [], "exceptions": [str(exc)]}
        return execution_reply(payload, identifier, work_num,
            "执行调用响应异常，已只读核对，未重发执行。"), work_num
    return execution_reply(called.get("structured"), identifier, work_num), work_num


def _batch_result_lines(index, target, approval, execution=None):
    lines = [f"{index}. 需求 {target['identifier']}；工作编号 {target['work_num']}",
             "审核：" + str((approval or {}).get("status") or "未知")]
    if (approval or {}).get("message"):
        lines.append("审核说明：" + str(approval["message"]))
    if execution is None:
        lines.append("执行：未开始")
        lines.append("影响条数合计：未知")
        return lines
    lines.append("执行：" + str(execution.get("status") or "未知"))
    total = execution.get("affected_rows_total")
    lines.append("影响条数合计：" + ("未知" if total is None else str(total)))
    for detail_index, detail in enumerate(execution.get("details") or [], 1):
        count, elapsed = detail.get("affected_rows"), detail.get("elapsed_seconds")
        line = f"SQL {detail_index}：{detail.get('status') or '未知'}，影响 {count if count is not None else '未知'} 条"
        if elapsed not in (None, ""):
            line += f"，耗时 {elapsed} 秒"
        lines.append(line)
        if detail.get("exception"):
            lines.append(f"SQL {detail_index} 异常详情：{detail['exception']}")
    exceptions = [str(item) for item in execution.get("exceptions") or [] if item]
    if exceptions:
        lines.append("异常详情：" + "；".join(exceptions))
    elif execution.get("complete"):
        lines.append("异常详情：无")
    else:
        lines.append("异常详情：未知，尚未取得完整执行结果")
    return lines


async def approve_execute_targets(targets, call_tool, *, dry_run=False):
    """Revalidate a pinned list, then make at most one approval and execution call per target."""
    called = await call_tool(QUERY_TOOL, {"query_type": "pending_approvals", "identifier": ""})
    pending = called.get("structured")
    if not isinstance(pending, dict) or pending.get("query_type") != "pending_approvals" or not isinstance(pending.get("items"), list):
        raise ValueError("未取得当前待审核清单，未处理。")
    resolved = []
    for target in targets:
        matches = [item for item in pending["items"] if str(item.get("work_num") or "") == target["work_num"]
            and str(item.get("identifier") or item.get("work_name") or "") == target["identifier"]]
        if len(matches) != 1:
            raise ValueError("上一轮清单中有单据已不在当前待审核列表，未发起批量操作，请重新查询。")
        current_id = str(matches[0].get("work_id") or "")
        if not re.fullmatch(r"\d{6,32}", current_id) or (target["work_id"] and target["work_id"] != current_id):
            raise ValueError("上一轮单据与当前稳定编号不一致，未发起批量操作，请重新查询。")
        resolved.append({**target, "work_id": current_id})
    targets = resolved
    if dry_run:
        return EXECUTION_REPLY_PREFIX + f"：已核对 {len(targets)} 条待审核单据。Skill 测试仅查询，未审核、未执行。"
    lines = [EXECUTION_REPLY_PREFIX, f"批量目标：{len(targets)} 条（来自上一轮待审核清单）"]
    for index, target in enumerate(targets, 1):
        arguments = {key: target[key] for key in ("work_id", "work_num")}
        try:
            approved = await call_tool("approve_data_maintenance", arguments)
            approval = approved.get("structured")
            if not isinstance(approval, dict):
                approval = {"status": "待核对", "message": "审核工具未返回结构化结果"}
        except McpServiceError as exc:
            approval = {"status": "待核对", "message": "审核调用响应异常，未重试：" + str(exc)}
        if approval.get("status") != "审核通过":
            lines.extend(_batch_result_lines(index, target, approval))
            continue
        try:
            executed = await call_tool(EXECUTION_TOOL, arguments)
            execution = executed.get("structured")
            if not isinstance(execution, dict):
                execution = {"status": "待核对", "complete": False, "affected_rows_total": None,
                    "details": [], "exceptions": ["执行工具未返回结构化结果"]}
        except McpServiceError as exc:
            try:
                checked = await call_tool("get_data_maintenance_execution_result", arguments)
                execution = checked.get("structured")
                if not isinstance(execution, dict) or not execution.get("complete"):
                    raise ValueError("未取得完整执行明细")
            except (McpServiceError, ValueError):
                execution = {"status": "待核对", "complete": False, "affected_rows_total": None,
                    "details": [], "exceptions": ["执行调用响应异常，未重试：" + str(exc)]}
        lines.extend(_batch_result_lines(index, target, approval, execution))
    return "\n".join(lines)


def owner_execution_config(config, message, owner):
    if not config.get("enabled") or str(message.get("msgtype")) != "1" or not owner or str(message.get("fromid") or "") != owner or not execution_candidate(message.get("msg")):
        return None
    tasks = [t for t in config.get("ai_tasks") or [] if t.get("enabled") and t.get("mcp_enabled") and t.get("mcp_tool_name") == EXECUTION_TOOL and t.get("message_type", "text") == "text"]
    if not tasks:
        return None
    return {**config, "rules": [], "ai_tasks": tasks, "message_types": ["text"],
            "mention_only": False, "mention_message_types": [], "target_senders_by_type": {"text": [owner]}}
