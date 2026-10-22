"""Explicit approval of an existing data-maintenance demand."""
from __future__ import annotations

import json
import re
from domp_identifier import identifiers_in, is_ticket_identifier, BATCH_IDENTIFIER_PROMPT

from domp_bridge import (SQL_START, split_reply, write_capability_question,
    write_meta_discussion)
from domp_execution_bridge import EXECUTION_TOOL, _batch_result_lines
from domp_query_bridge import QUERY_TOOL, QUERY_REPLY_PREFIX
from mcp_service import McpServiceError

APPROVAL_TOOL = "approve_data_maintenance"
APPROVAL_REPLY_PREFIX = "数据运维审核结果"
APPROVAL_PROMPT = """你是数据运维审核 Agent，只识别当前消息明确要求审核已有数据运维单的指令。
“审核一下 41366 运维单”“帮我审批数据运维单41366”“把这张运维单审核通过”属于只审核，输出 action="approve"；“审核和执行一下运维单”“审核并执行 41366”“把这张单审批后执行”属于单张审核后执行，输出 action="approve_execute"。
输入可包含 current_message 和 query_history。历史仅为同一账号、同一群、同一发送人七天内经工具验证的查询结果；当前消息没有单号时，程序只允许承接最近一轮待审核清单中的唯一单据。模型不得从历史生成或猜测编号：当前消息没有明确编号时 identifier 必须为空。
明确请求使用 matched=true、confidence>=90，result 为 JSON 对象编码成的字符串，字段为 action 和 identifier。当前消息有唯一编号时原样提取四位线下编号、五位需求名称或完整工作编号；没有则为空。reply 不声称审核或执行成功。
待审核查询、审核了吗、审核结果如何、普通陈述、否定、未来计划、引用示例、SQL 原文、填报请求均不匹配。“都/全部/所有单据审核并执行”由批量流程处理，本 Agent 不匹配；单纯要求批量审核也不匹配。
程序会先重新读取当前待审核清单并精确核对稳定 work_id、work_num。只审核仅调用 approve_data_maintenance；审核后执行只有在返回“审核通过”后才调用 execute_data_maintenance。MCP 端继续检查系统内申请 SQL 风险及防重记录，不 force、不重试写操作。回复真实审核状态；若执行则同时回复逐条影响行数、耗时和异常。页面 Skill 测试仅查询定位，不审核、不执行、不发送微信。"""


APPROVAL_PROMPT += BATCH_IDENTIFIER_PROMPT


def approval_candidate(source):
    source = str(source or "").strip()
    if len(source) > 300 or SQL_START.search(source) or any(marker in source for marker in
        (APPROVAL_REPLY_PREFIX, QUERY_REPLY_PREFIX, "数据运维执行结果", "数据运维填报结果", "流程标识：")):
        return False
    if re.search(r"全部|所有|批量|都(?:要|给|去|来|审核|审批|执行)|上面(?:的)?(?:这些)?|这些(?:单|需求)", source):
        return False
    if write_meta_discussion(source) or write_capability_question(source):
        return False
    if re.search(r"不要|别|不(?:要|用|能)?\s*(?:审核|审批|执行)|停止|取消|如果|假如|明天|以后|计划|打算|怎么|如何|(?:他|她|别人|有人)说|转述|转发|引用|例如|比如|示例|假设|提示词", source):
        return False
    if re.search(r"(?:审核|审批)(?:了|过)?(?:吗|没|没有)|(?:审核|审批)(?:的)?(?:结果|情况|状态|详情)|待(?:审核|审批)|查(?:询|一下)?.*(?:审核|审批)", source):
        return False
    if not re.search(r"审核|审批", source):
        return False
    forward = r"(?:审核|审批)(?:通过)?(?:一下|下)?\s*(?:(?:并|和|后)\s*执行(?:一下|下)?\s*)?(?:(?:数据\s*)?运维|需求|单据?|(?:xzsb-)?\d{4,32})"
    reverse = r"(?:(?:数据\s*)?运维|需求|单据?|(?:xzsb-)?\d{4,32}).{0,40}(?:审核|审批)(?:通过|一下|下)?(?:\s*(?:并|和|后)\s*执行(?:一下|下)?)?"
    return bool(re.search(forward, source) or re.search(reverse, source))


def approval_target(source, analysis, history):
    if not approval_candidate(source):
        raise ValueError("请明确说明：审核数据运维单 41366；如需随后执行，请说明审核并执行。")
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            parsed = None
    expected_action = "approve_execute" if re.search(r"执行", str(source)) else "approve"
    if not isinstance(parsed, dict) or parsed.get("action") != expected_action:
        raise ValueError("未识别到明确且一致的审核动作，未处理。")
    identifiers = identifiers_in(source, strict=True)
    if len(identifiers) > 1:
        raise ValueError("请指定唯一的需求编号，未审核。")
    model_identifier = re.sub(r"^xzsb-", "", str(parsed.get("identifier") or ""), flags=re.I)
    if identifiers:
        identifier = next(iter(identifiers))
        if model_identifier != identifier:
            raise ValueError("识别编号与当前消息不一致，未审核。")
        return expected_action, {"identifier": identifier, "work_id": "", "work_num": ""}
    if model_identifier:
        raise ValueError("当前消息没有单号，模型不得从历史补充编号，未审核。")
    last = history[-1] if history else {}
    if (last.get("arguments") or {}).get("query_type") not in {"pending_approvals", "pending_maintenance"}:
        raise ValueError("当前消息没有单号，也没有可承接的待审核清单。请先查询或直接提供单号。")
    targets = last.get("approval_targets")
    if not isinstance(targets, dict) or targets.get("count") != 1 or len(targets.get("items") or []) != 1:
        raise ValueError("上一轮待审核清单不是唯一单据，请明确提供需求编号。")
    target = {key: str(targets["items"][0].get(key) or "") for key in ("identifier", "work_id", "work_num")}
    if (not target["identifier"] or not re.fullmatch(r"\d{6,32}", target["work_num"])
        or (target["work_id"] and not re.fullmatch(r"\d{6,32}", target["work_id"]))):
        raise ValueError("上一轮待审核清单缺少稳定编号，请重新查询。")
    return expected_action, target


def _approval_reply(target, approval, execution=None, *, include_execution=False):
    lines = [APPROVAL_REPLY_PREFIX]
    if include_execution:
        lines.extend(_batch_result_lines(1, target, approval, execution))
    else:
        lines.extend([f"需求：{target['identifier']}", f"工作编号：{target['work_num']}",
                      "审核：" + str(approval.get("status") or "未知")])
        if approval.get("message"):
            lines.append("审核说明：" + str(approval["message"]))
    return "\n".join(lines)


async def approve_target(action, target, call_tool, *, dry_run=False):
    """Resolve one pending approval and make no more than one call per requested write."""
    called = await call_tool(QUERY_TOOL, {"query_type": "pending_approvals", "identifier": ""})
    pending = called.get("structured")
    if not isinstance(pending, dict) or pending.get("query_type") != "pending_approvals" or not isinstance(pending.get("items"), list):
        raise ValueError("未取得当前待审核清单，未审核。")
    pinned_num = target.get("work_num") or ""
    identifier = target["identifier"]
    matches = [item for item in pending["items"] if (
        str(item.get("work_num") or "") == pinned_num if pinned_num else
        str(item.get("identifier") or item.get("work_name") or "") == identifier if is_ticket_identifier(identifier) else
        identifier in {str(item.get("work_num") or ""), str(item.get("work_id") or "")})]
    if len(matches) > 1:
        raise ValueError("当前存在多条同名待审核需求，请提供完整工作编号。")
    if not matches:
        raise ValueError("该单据不在当前待审核清单，未审核；请先查询最新状态。")
    item = matches[0]
    resolved = {"identifier": str(item.get("identifier") or item.get("work_name") or identifier),
                "work_id": str(item.get("work_id") or ""), "work_num": str(item.get("work_num") or "")}
    if not re.fullmatch(r"\d{6,32}", resolved["work_id"]) or not re.fullmatch(r"\d{6,32}", resolved["work_num"]):
        raise ValueError("待审核单据缺少稳定工作编号，未审核。")
    if (target.get("work_id") and target["work_id"] != resolved["work_id"]) or (pinned_num and pinned_num != resolved["work_num"]):
        raise ValueError("上一轮单据与当前稳定编号不一致，未审核。")
    if dry_run:
        return APPROVAL_REPLY_PREFIX + f"：已定位需求 {resolved['identifier']}，工作编号 {resolved['work_num']}。Skill 测试仅查询，未审核、未执行。"
    arguments = {key: resolved[key] for key in ("work_id", "work_num")}
    try:
        approved = await call_tool(APPROVAL_TOOL, arguments)
        approval = approved.get("structured")
        if not isinstance(approval, dict):
            approval = {"status": "待核对", "message": "审核工具未返回结构化结果"}
    except McpServiceError as exc:
        approval = {"status": "待核对", "message": "审核调用响应异常，未重试：" + str(exc)}
    if action == "approve" or approval.get("status") != "审核通过":
        return _approval_reply(resolved, approval, include_execution=action == "approve_execute")
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
    return _approval_reply(resolved, approval, execution, include_execution=True)


def approval_reply_chunks(reply):
    body = str(reply).removeprefix(APPROVAL_REPLY_PREFIX).lstrip("：\n")
    return tuple(APPROVAL_REPLY_PREFIX + "\n" + part for part in split_reply(body, 1800-len(APPROVAL_REPLY_PREFIX)-1))


def owner_approval_config(config, message, owner):
    if not config.get("enabled") or str(message.get("msgtype")) != "1" or not owner or str(message.get("fromid") or "") != owner or not approval_candidate(message.get("msg")):
        return None
    tasks = [task for task in config.get("ai_tasks") or [] if task.get("enabled") and task.get("mcp_enabled")
             and task.get("mcp_tool_name") == APPROVAL_TOOL and task.get("message_type", "text") == "text"]
    if not tasks:
        return None
    return {**config, "rules": [], "ai_tasks": tasks, "message_types": ["text"],
            "mention_only": False, "mention_message_types": [], "target_senders_by_type": {"text": [owner]}}
