"""Read-only operation-ticket and DOMP query Agent routing and replies."""
from __future__ import annotations

from runtime_settings import prompt, setting

import json
import re
from collections import Counter
from domp_identifier import identifiers_in, is_ticket_identifier, base_identifier, BATCH_IDENTIFIER_PROMPT
from datetime import date, timedelta

from domp_bridge import SQL_START, split_reply

QUERY_TOOL = "query_data_maintenance"
TASK_QUERY_TOOL = "query_operation_ticket"
TASK_SUMMARY_TOOL = "query_task_processing"
RANGE_QUERY_TOOL = "query_operation_tickets"
QUERY_REPLY_PREFIX = setting('labels', 'domp_query_bridge.query_reply_prefix')
PROBLEM_QUERY_REPLY_PREFIX = setting('labels', 'domp_query_bridge.problem_query_reply_prefix')
EXECUTION_REPLY_PREFIX = setting('labels', 'domp_query_bridge.execution_reply_prefix')
QUERY_TYPES = {"task_processing", "problem_tickets", "execution", "pending_approvals", "pending_executions", "pending_maintenance", "pending_tasks", "today_assignments"}
QUERY_PROMPT = prompt('domp_query_bridge.query_prompt')


QUERY_PROMPT += BATCH_IDENTIFIER_PROMPT


def query_export_followup(source):
    """Recognize format-only requests; inherit scope only from this sender."""
    return bool(re.fullmatch(
        r"(?:(?:请|帮我|把|将|刚才的?|上面的?|这些|结果|查询结果|明细|数据)\s*)*"
        r"(?:(?:整理|导出|生成|转换|转|做|汇总)(?:一下|成|为)?|发(?:送)?(?:一下)?(?:个|份)?)?\s*"
        r"(?:表格|报表|Excel|xlsx)(?:文件)?\s*(?:(?:发(?:送)?(?:给)?我|给我|一下|吧|谢谢)\s*)*[。！!？?]*",
        str(source or "").strip(), re.I))


def count_only_requested(source):
    source = str(source or "")
    return bool(re.search(r"数量|多少|几(?:条|张|个|单)|总数|总计", source)) and not re.search(
        r"表格|图表|报表|Excel|xlsx|明细|详情|清单|列表|哪些|履历|处理时间|逐条|分布", source, re.I)


def query_followup(source):
    source = str(source or "").strip()
    return query_export_followup(source) or len(source) <= 300 and bool(re.search(r"呢[？?。!！]*$|刚才|上面|这(?:条|单|个)|那(?:条|单|个)|它|再查|继续查|详细|详情|明细|SQL\s*(?:原文|脚本)|完整\s*SQL|具体有哪些|下一页|异常|耗时", source, re.I))


def execution_details_requested(source):
    return bool(re.search(r"详情|明细|SQL\s*(?:原文|脚本)|(?:完整|具体|实际)\s*SQL", str(source or ""), re.I))


def execution_question(source):
    source = str(source or "")
    if re.search(r"待\s*执行", source):
        return False
    return bool(re.search(r"执行(?:成功|失败|完成|完毕|完)?(?:了|过)?(?:吗|没|没有)|是否(?:已|已经)?执行|有(?:没有|无)执行", source))


def pending_query_kind(source):
    """Resolve explicit pending-list questions, with bounded spelling aliases."""
    source = str(source or "").strip()
    if len(source) > 300 or SQL_START.search(source) or any(marker in source for marker in (QUERY_REPLY_PREFIX, EXECUTION_REPLY_PREFIX, "流程标识：")):
        return ""
    normalized = re.sub(r"代(?=\s*(?:审核|审批|执行))", "待", source)
    if not re.search(r"查|多少|有所少|几条|几个|有哪些|哪些|数量|清单|情况|状态|详情|明细|呢|有没有|还有|有无|吗", normalized):
        return ""
    # Do not reinterpret a modification request as a pending-list question.
    if re.search(r"填报|提交|删除|改派|通过这些|全部通过|(?<!待)执行\s*(?:下|一下)|(?<!待)审核\s*(?:下|一下)", normalized):
        return ""
    approval = bool(re.search(r"待\s*(?:审核|审批)", normalized))
    execution = bool(re.search(r"待\s*执行", normalized))
    if approval and execution:
        return "pending_maintenance"
    return "pending_approvals" if approval else "pending_executions" if execution else ""


def explicit_pending_analysis(source):
    kind = pending_query_kind(source)
    if kind and (kind == "pending_maintenance" or re.search(r"代\s*(?:审核|审批|执行)", str(source))):
        return {"matched": True, "confidence": 100, "result": json.dumps({"query_type": kind, "identifier": ""}), "reply": ""}
    return None


def _explicit_date_range(source, today):
    values = []
    for match in re.finditer(r"(?:(\d{4})[年./-])?(\d{1,2})[月./-](\d{1,2})日?", source):
        try:
            values.append(date(int(match.group(1) or today.year), int(match.group(2)), int(match.group(3))))
        except ValueError as exc:
            raise ValueError("消息中的日期无效，请使用正确日期") from exc
    if values:
        start, end = values[0], values[-1]
        if len(values) == 1 and re.search(r"(?:到|至)\s*(?:今天|今日|现在)|至今", source):
            end = today
        if start > end:
            raise ValueError("查询开始日期不能晚于结束日期")
        if (end-start).days > 366:
            raise ValueError("查询日期范围不能超过 366 天")
        return start.isoformat(), end.isoformat(), "指定日期"
    return "", "", ""


_CURRENT_YEAR = r"今年|本年(?:度)?|年初(?:以来|至今|到现在)"


def pending_date_range(source, today=None):
    """Current-message dates take priority over relative words and history."""
    source = str(source or "")
    today = today or date.today()
    explicit = _explicit_date_range(source, today)
    if explicit[2]:
        return explicit
    if re.search(_CURRENT_YEAR, source):
        return today.replace(month=1, day=1).isoformat(), today.isoformat(), "今年以来"
    if "前天" in source:
        day = today - timedelta(days=2); return day.isoformat(), day.isoformat(), "前天"
    if re.search(r"昨天|昨日", source):
        day = today - timedelta(days=1); return day.isoformat(), day.isoformat(), "昨天"
    if re.search(r"今天|今日", source):
        return today.isoformat(), today.isoformat(), "今天"
    if re.search(r"上周", source):
        end = today - timedelta(days=today.weekday()+1); start = end - timedelta(days=6)
        return start.isoformat(), end.isoformat(), "上周"
    if re.search(r"本周|这周", source):
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat(), "本周"
    if re.search(r"上(?:个)?月", source):
        end = today.replace(day=1) - timedelta(days=1); start = end.replace(day=1)
        return start.isoformat(), end.isoformat(), "上月"
    if "本月" in source:
        return today.replace(day=1).isoformat(), today.isoformat(), "本月"
    recent = re.search(r"(?:最近|过去)\s*(\d{1,3})\s*天", source)
    if recent:
        days = int(recent.group(1))
        if not 1 <= days <= 366:
            raise ValueError("最近天数必须为 1 到 366 天")
        return (today-timedelta(days=days-1)).isoformat(), today.isoformat(), f"最近{days}天"
    return "", "", ""


def explicit_data_platform(source):
    """Return whether the user explicitly selected the data-maintenance platform."""
    return bool(re.search(r"数据|SQL|填报|审核|审批|待\s*(?:审核|审批|执行)|影响条数|工作编号", str(source or ""), re.I))


def problem_range_query(source):
    """Detect a time-range/list question for the old problem platform."""
    source = str(source or "")
    if not source or explicit_data_platform(source):
        return False
    has_range = bool(re.search(
        _CURRENT_YEAR + r"|今天|今日|昨天|昨日|前天|本周|这周|上周|一周内|最近\s*(?:\d+\s*天|一周|一个月|一年)|过去\s*(?:\d+\s*天|一周|一个月|一年)|本月|上(?:个)?月|有史以来|全部(?:历史)?|所有(?:单据|运维单|工单)|能查到的所有|(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}日?",
        source,
    ))
    has_problem_words = bool(re.search(
        r"问题平台|旧问题平台|运维平台|运维单|工单|问题.*(?:单子|单据|状态|处理)|处理情况|处理状态|未完成|未完结|未处理|单子|单据",
        source,
    ))
    return has_range and has_problem_words


def explicit_problem_range_analysis(source):
    """Recognize explicit read-only statistics without model date guesses."""
    source = str(source or "")
    if (not problem_range_query(source) or not query_candidate(source)
            or not re.search(r"查|统计|数量|多少|几条|几个|清单|明细|导出|表格|报表", source)):
        return None
    return {"matched": True, "confidence": 100, "result": json.dumps({"query_type": "problem_tickets", "identifier": ""}), "reply": ""}


def problem_date_range(source, today=None):
    """Derive an inclusive rolling range for problem-platform list queries."""
    source = str(source or "")
    today = today or date.today()
    explicit = _explicit_date_range(source, today)
    if explicit[2]:
        return explicit
    if re.search(_CURRENT_YEAR, source):
        return today.replace(month=1, day=1).isoformat(), today.isoformat(), "今年以来"
    if re.search(r"有史以来|全部(?:历史)?|所有(?:单据|运维单|工单)|能查到的所有", source):
        return "", "", "全部历史"
    if re.search(r"(?:最近|过去)\s*(?:一个月|1\s*个月)\s*(?:到|至)\s*(?:上周|上一周)", source):
        previous_month = today.replace(day=1) - timedelta(days=1)
        start = previous_month.replace(day=min(today.day, previous_month.day))
        end = today - timedelta(days=today.weekday() + 1)
        if end < start:
            raise ValueError("查询结束日期早于开始日期，请明确起止日期")
        return start.isoformat(), end.isoformat(), "最近一个月至上一周"
    if re.search(r"最近\s*(?:一周|7\s*天)|过去\s*(?:一周|7\s*天)|一周内", source):
        return (today - timedelta(days=6)).isoformat(), today.isoformat(), "最近一周"
    if re.search(r"最近\s*(?:一个月|1\s*个月)|过去\s*(?:一个月|1\s*个月)|一个月内", source):
        previous_month = today.replace(day=1) - timedelta(days=1)
        start = previous_month.replace(day=min(today.day, previous_month.day))
        return start.isoformat(), today.isoformat(), "最近一个月"
    if re.search(r"最近\s*(?:一年|1\s*年)|过去\s*(?:一年|1\s*年)|一年内", source):
        return (today - timedelta(days=364)).isoformat(), today.isoformat(), "最近一年"
    date_from, date_to, label = pending_date_range(source, today=today)
    return date_from, date_to, label


def query_candidate(source, history=()):
    source = str(source or "").strip()
    # Bot results can arrive as self-message callbacks. Every query reply chunk
    # has this marker, and SQL workflow results remain excluded as well.
    if QUERY_REPLY_PREFIX in source or PROBLEM_QUERY_REPLY_PREFIX in source or EXECUTION_REPLY_PREFIX in source or "数据运维填报结果" in source or "流程标识：" in source or ("填报：" in source and "审核：" in source):
        return False
    if SQL_START.search(source):
        return False
    domain = re.search(r"执行|审核|审批|影响|需求|任务|处理|分配|派发|综合查询|受理人|履历|问题平台|运维单|SQL\s*(?:原文|脚本)|完整\s*SQL", source, re.I)
    # Let the semantic Agent see short questions even when a misspelling
    # removes every exact business keyword. It still must classify an allowed
    # read-only intent; an unrelated question never directly selects a tool.
    semantic_question = len(source) <= 300 and bool(re.search(
        r"查|多少|有所少|几条|几个|有哪些|哪些|有没有|有无|情况|结果|详情|明细|清单|还有.*(?:吗|么|没)", source))
    ambiguous_action = len(source) <= 300 and bool(re.search(r"单子|单据|单号|工单|运维|需求|任务", source) and re.search(r"搞(?:一下|下)|弄(?:一下|下)|安排一下", source))
    timed_pending = bool(re.search(r"今天|今日|昨天|昨日|前天|本周|这周|上周|本月|上(?:个)?月|最近\s*\d+\s*天|过去\s*\d+\s*天|(?:\d{4}[年./-])?\d{1,2}[月./-]\d{1,2}日?", source)
        and re.search(r"待\s*(?:审核|审批|执行)", source))
    return bool(problem_range_query(source) or semantic_question or ambiguous_action or timed_pending or pending_query_kind(source) or domain and re.search(
        r"查|看看|看下|多少|有所少|几条|几个|有哪些|哪些|数量|清单|情况|结果|状态|详情|明细|脚本|原文|谁|呢|有没有|还有", source)
        or execution_question(source)
        or query_followup(source) and (history or re.fullmatch(r"(?:那|这)?(?:它|这条|这单|这个|\d{4,5}|结果|影响条数|耗时|异常)(?:的)?呢[？?。]*", source)))


def query_analysis_content(source, history):
    if not history:
        return source
    return json.dumps({"current_message": source, "query_history": history[-12:]}, ensure_ascii=False)


def query_clarification_candidate(source):
    return query_candidate(source) and bool(re.search(r"单子|单据|单号|工单|运维|需求|任务", str(source or "")))


def query_arguments(source, analysis, history=(), *, today=None):
    if query_export_followup(source) and not history:
        raise ValueError("当前没有可承接的查询上下文，请先说明要查询的日期范围或单号，再要求导出表格。")
    if not query_candidate(source, history):
        raise ValueError("只支持任务与数据运维查询，不能修改需求")
    parsed = analysis.get("result")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict):
        raise ValueError("未取得明确的查询意图，请说明问题平台运维单范围、任务处理情况、执行情况或待审核数量")
    if parsed.get("needs_clarification") is True:
        raise ValueError("暂时无法确定你的意思，请说明要查询问题平台运维单、待审核、待执行、任务处理情况或某个单号的执行结果；如果需要执行，请明确单号和执行要求。")
    kind = parsed.get("query_type")
    if kind not in QUERY_TYPES:
        raise ValueError("只支持问题平台范围查询、任务处理情况、待处理清单、今日分配统计、执行情况和待审核/待执行查询")
    kind = pending_query_kind(source) or kind
    if execution_question(source) or re.search(r"执行(?:的)?(?:详情|明细)|SQL\s*(?:原文|脚本)|完整\s*SQL", str(source), re.I):
        kind = "execution"
    if history and query_followup(source) and not re.search(r"执行|审核|审批|影响|需求|任务|处理|分配|派发|综合查询|受理人|履历|SQL\s*(?:原文|脚本)|完整\s*SQL", str(source), re.I):
        kind = (history[-1].get("arguments") or {}).get("query_type")
        if kind not in QUERY_TYPES:
            raise ValueError("上一轮查询类型不明确，请补充要查询的内容")
    source_text = str(source or "")
    # A time-range/list question without an explicit data-platform word is
    # always the old problem platform. This rule runs before the generic
    # “任务/处理” compatibility rule below.
    if problem_range_query(source_text):
        kind = "problem_tickets"
    elif explicit_data_platform(source_text) and kind == "problem_tickets":
        raise ValueError("已明确选择数据平台；请说明数据运维的执行、待审核或待执行查询")
    # Explicit old-system wording must reach 综合查询 even if the model mixes
    # the two systems. Neither branch can invoke a business modification.
    if kind != "problem_tickets" and re.search(r"(?:今天|今日).*?(?:分配|派发)|(?:分配|派发).*?(?:今天|今日)", source_text):
        kind = "today_assignments"
    elif kind != "problem_tickets" and re.search(r"待(?:处理|分配|派发)|待办任务", source_text) and not re.search(r"执行|审核|审批|数据运维", source_text):
        kind = "pending_tasks"
    elif kind != "problem_tickets" and re.search(r"任务|处理|综合查询|受理人|履历", source_text) and not re.search(r"执行|审核|审批|影响|数据运维", source_text) and kind not in {"pending_tasks", "today_assignments"}:
        kind = "task_processing"
    elif kind == "task_processing" and re.search(r"执行|审核|审批|影响", source_text):
        raise ValueError("请分别查询任务处理情况和数据运维执行情况")
    identifier = ""
    identifier_text = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", "", source_text)
    if kind in QUERY_TYPES - {"execution", "task_processing", "problem_tickets"} and re.search(r"(?<!\d)\d{4,32}(?!\d)", identifier_text):
        raise ValueError("请说明这个单号要查询任务处理情况还是 SQL 执行情况")
    if kind in {"execution", "task_processing"}:
        if any(re.search(r"[0-9]", token) and re.search(r"[OoIl]", token) for token in re.findall(
            r"(?<![A-Za-z0-9])[0-9OoIl]{4,32}(?![A-Za-z0-9])", str(source))):
            raise ValueError("单号含容易混淆的字母，请重新发送正确的数字编号；不会自动纠正单号。")
        ids = identifiers_in(identifier_text, strict=True)
        if len(ids) > 1:
            raise ValueError("请注明唯一的需求编号，例如：帮我查一下 41123 执行情况")
        if ids:
            identifier = next(iter(ids))
        else:
            previous = (history[-1].get("arguments") or {}) if history else {}
            identifier = str(previous.get("identifier") or "")
            if not is_ticket_identifier(identifier) and not re.fullmatch(r"\d{6,32}", identifier):
                raise ValueError("当前会话没有明确的单号，请补充需求编号；超过一周会开始新会话")
        if kind == "task_processing" and not re.fullmatch(r"\d{5}(?:-[1-9]\d*)?", identifier):
            raise ValueError("综合查询需要五位任务标识，例如：查一下 41198 任务处理情况")
        actual = re.sub(r"^xzsb-", "", str(parsed.get("identifier") or ""), flags=re.I)
        if actual != identifier:
            raise ValueError("识别编号与原消息不一致，请重新说明需求编号")
        if kind == "task_processing":
            identifier = base_identifier(identifier)
    arguments = {"query_type": kind, "identifier": identifier}
    if kind == "problem_tickets":
        date_from, date_to, date_label = problem_date_range(source_text, today=today)
        previous = (history[-1].get("arguments") or {}) if history else {}
        inherit_scope = not date_label and query_followup(source_text) and previous.get("query_type") == kind
        if inherit_scope:
            date_from = str(previous.get("date_from") or "")
            date_to = str(previous.get("date_to") or "")
            date_label = str(previous.get("date_label") or "全部历史")
        incomplete_only = bool(re.search(r"未完成|未完结|未处理|还有.*(?:没|未).*(?:完成|完结|处理)", source_text))
        if not incomplete_only and previous.get("query_type") == kind and query_followup(source_text):
            incomplete_only = bool(previous.get("incomplete_only"))
        arguments.update(date_from=date_from, date_to=date_to, date_label=date_label or "全部历史",
                         incomplete_only=incomplete_only)
        if count_only_requested(source_text):
            arguments["count_only"] = True
        range_alias = {
            "今天": "today", "昨天": "yesterday", "最近一周": "week",
            "最近一个月": "month", "最近一年": "year", "全部历史": "all",
        }.get(date_label)
        if range_alias:
            arguments["time_range"] = range_alias
        elif inherit_scope and previous.get("time_range"):
            arguments["time_range"] = previous["time_range"]
        return arguments
    if kind in {"pending_approvals", "pending_executions", "pending_maintenance"}:
        date_from, date_to, date_label = pending_date_range(source, today=today)
        if date_from:
            arguments.update(date_from=date_from, date_to=date_to, date_label=date_label)
    if kind in {"pending_tasks", "today_assignments"}:
        if re.search(r"昨天|昨日|上周|本周|这周|本月|上(?:个)?月|前天|最近\d|过去|\d{4}[-年/]", source_text):
            raise ValueError("分配时间统计目前支持今天，请说明：今天分配了多少任务")
        offset = 0
        if "下一页" in str(source):
            previous = history[-1] if history else {}
            if (previous.get("arguments") or {}).get("query_type") != kind or not previous.get("page", {}).get("has_more"):
                raise ValueError("当前清单没有下一页，请先查询清单")
            offset = previous["page"]["offset"] + previous["page"]["shown_count"]
        arguments.update(offset=offset, limit=20)
    return arguments


def query_memory_turn(source, arguments, reply, *, message_id="", connection_id="", payload=None):
    # Keep bounded dialogue and validated query targets, never SQL scripts,
    # complete ticket personal data or credentials from the MCP response.
    turn = {"message_id": message_id, "connection_id": connection_id,
            "message": str(source)[:1500], "arguments": dict(arguments),
            "answer": str(reply)[:1500]}
    if isinstance(payload, dict):
        approval_payload = None
        if arguments.get("query_type") == "pending_approvals":
            approval_payload = payload
        elif arguments.get("query_type") == "pending_maintenance":
            approval_payload = next((part for part in payload.get("results", [])
                if part.get("query_type") == "pending_approvals" and not part.get("error")), None)
        if isinstance(approval_payload, dict):
            turn["approval_targets"] = {"count": approval_payload.get("count"), "items": [
                {k: str(item.get(k) or "") for k in ("identifier", "work_num", "work_id")}
                for item in (approval_payload.get("items") or [])[:20]]}
        execution_payload = None
        if arguments.get("query_type") == "pending_executions":
            execution_payload = payload
        elif arguments.get("query_type") == "pending_maintenance":
            execution_payload = next((part for part in payload.get("results", [])
                if part.get("query_type") == "pending_executions" and not part.get("error")), None)
        if isinstance(execution_payload, dict):
            turn["execution_targets"] = {"count": execution_payload.get("count"), "items": [
                {k: str(item.get(k) or "") for k in ("identifier", "work_num", "work_id")}
                for item in (execution_payload.get("items") or [])[:20]]}
        if arguments.get("query_type") in {"problem_tickets", "pending_tasks", "today_assignments"}:
            turn["page"] = {k: payload.get(k) for k in ("offset", "shown_count", "has_more")}
    return turn


def query_tool_call(arguments):
    if arguments["query_type"] == "pending_maintenance":
        raise ValueError("待审核和待执行需要分别进行两次只读查询")
    if arguments["query_type"] == "task_processing":
        return TASK_QUERY_TOOL, {"identifier": arguments["identifier"]}
    if arguments["query_type"] == "problem_tickets":
        if arguments.get("date_from") or arguments.get("date_to"):
            keys = ("date_from", "date_to", "incomplete_only")
        elif arguments.get("time_range"):
            keys = ("time_range", "incomplete_only")
        else:
            keys = ("date_from", "date_to", "incomplete_only")
        return RANGE_QUERY_TOOL, {k: arguments[k] for k in (*keys, "count_only") if k in arguments}
    if arguments["query_type"] in {"pending_tasks", "today_assignments"}:
        return TASK_SUMMARY_TOOL, {k: arguments[k] for k in ("query_type", "offset", "limit")}
    return QUERY_TOOL, {key: arguments[key] for key in ("query_type", "identifier", "date_from", "date_to") if key in arguments}


def query_reply(payload, *, include_sql=False):
    if not isinstance(payload, dict):
        return QUERY_REPLY_PREFIX + "：未取得结构化结果，查询失败，影响条数未知。"
    kind = payload.get("query_type")
    reply_prefix = PROBLEM_QUERY_REPLY_PREFIX if kind == "problem_tickets" else QUERY_REPLY_PREFIX
    lines = [reply_prefix]
    if kind == "pending_maintenance":
        results = {part.get("query_type"): part for part in payload.get("results", []) if isinstance(part, dict)}
        filtered = next((part for part in results.values() if part.get("date_from")), None)
        if filtered:
            lines.append(f"指定时间范围内产生且当前仍待处理的清单：{filtered['date_from']} 至 {filtered.get('date_to') or filtered['date_from']}。")
        else:
            lines.append("当前积压清单，包含以前提交的单据，不仅限今天新增。")
        for part_kind, label in (("pending_approvals", "待审核"), ("pending_executions", "待执行")):
            part = results.get(part_kind)
            if not part or part.get("error"):
                lines.append(f"目前{label}需求：未知；查询失败：{(part or {}).get('error') or '未取得结构化结果'}")
            else:
                lines.append(query_reply(part).removeprefix(QUERY_REPLY_PREFIX + "\n"))
    elif kind == "problem_tickets":
        range_label = payload.get("range_label") or "指定范围"
        if payload.get("date_from"):
            lines.append(f"时间范围：{range_label}（{payload['date_from']} 至 {payload.get('date_to') or payload['date_from']}）")
        else:
            lines.append(f"时间范围：{range_label}（平台当前账号可见的全部历史）")
        if payload.get("incomplete_only"):
            excluded = "、".join(str(item) for item in payload.get("excluded_statuses") or ("完成待验证", "关闭(归档)", "验证不通过", "科长意见给出", "提出"))
            lines.append(f"筛选条件：未完成，已排除：{excluded}")
        items = payload.get("items") or []
        count = payload.get("count")
        lines.append(f"符合条件：{count if count is not None else '未知'} 条")
        if payload.get("count_only"):
            lines.append("统计口径：按上述日期及筛选条件统计。本次仅查询数量；需要完整明细可回复“整理为表格发我”。")
        if len(items) > 10:
            status_counts = Counter(str(item.get("status") or "未知") for item in items if isinstance(item, dict))
            status_text = "；".join(f"{status} {total} 条" for status, total in status_counts.most_common(8))
            lines.append("当前处理进度：" + (status_text or "状态未知"))
            lines.append("完整明细已整理为表格文件发送，包含信息中心首次处理时间。")
        else:
            for index, item in enumerate(items, 1):
                item = item if isinstance(item, dict) else {}
                identifier = item.get("identifier") or item.get("id_identifier") or "未知"
                lines.append(f"{index}. 标识ID：{identifier}；问题ID：{item.get('problem_id') or item.get('iid') or '未知'}；状态：{item.get('status') or '未知'}")
                lines.append(f"概述：{item.get('summary') or '未知'}；填报时间：{item.get('submitted_at') or '未知'}；受理人：{item.get('assignee') or '未知'}")
                if "info_center_time_status" in item:
                    lines.append("信息中心处理时间：" + (item.get("info_center_processed_at") or
                        {"error": "查询失败", "missing_time": "时间缺失"}.get(item.get("info_center_time_status"), "暂无记录")))
        if (payload.get("info_center_time") or {}).get("failed_count"):
            lines.append(f"{payload['info_center_time']['failed_count']} 张单的信息中心处理时间查询失败，已标注。")
    elif kind in {"pending_tasks", "today_assignments"}:
        count = payload.get("count")
        label = "当前待处理任务" if kind == "pending_tasks" else f"今天（{payload.get('date') or '日期未知'}）本助手已记录成功分配任务"
        lines.append(f"{label}：{count if count is not None else '未知'} 条")
        if kind == "today_assignments":
            lines.append("记录启用时间：" + str(payload.get("recording_started_at") or "未知"))
            if payload.get("coverage_note"):
                lines.append("统计口径：" + str(payload["coverage_note"]))
        for index, item in enumerate(payload.get("items") or [], int(payload.get("offset") or 0)+1):
            lines.append(f"{index}. {item.get('identifier') or '未知'}；{item.get('summary') or '无摘要'}")
            if kind == "today_assignments":
                lines.append(f"分配时间：{item.get('assigned_at') or '未知'}；指派人员：{item.get('assignee') or '未知'}；结果：{item.get('result') or '未知'}")
                if item.get("decision"):
                    lines.append("命中规则：" + str(item["decision"]))
            else:
                lines.append(f"状态：{item.get('status') or '未知'}；{item.get('city') or ''} {item.get('reporting_unit') or ''}；优先级：{item.get('priority') or '未知'}")
        if payload.get("has_more"):
            lines.append("还有更多，回复“下一页”继续查询。")
    elif kind == "task_processing":
        lines.append("任务：" + str(payload.get("identifier") or "未知"))
        if not payload.get("found"):
            lines.append(str(payload.get("message") or "未取得任务处理情况。"))
            for item in payload.get("matches") or []:
                lines.append(f"问题 ID：{item.get('iid') or '未知'}；状态：{item.get('status') or '未知'}")
        else:
            lines.append("当前状态：" + str(payload.get("status") or "未知"))
            lines.append("受理人：" + str(payload.get("assignee") or "未知"))
            lines.append("受理机构：" + str(payload.get("assignee_org") or "未知"))
            if payload.get("summary"):
                lines.append("概述：" + str(payload["summary"]))
            history = payload.get("history") or []
            if history and history[-1].get("acceptance"):
                acceptance = str(history[-1]["acceptance"])
                lines.append("受理情况：" + {"未": "尚未受理", "已": "已受理"}.get(acceptance, acceptance))
            lines.append(f"处理履历：{len(history)} 条" if history else "处理履历：系统未提供")
            for index, entry in enumerate(history, 1):
                lines.append(f"{index}. {entry.get('initiated_at') or '时间未知'}；{entry.get('status') or '状态未知'}；{entry.get('initiator') or '发起人未知'} → {entry.get('assignee') or '受理人未知'}")
                if entry.get("assignee_org"):
                    lines.append("受理机构：" + str(entry["assignee_org"]))
                if entry.get("comment"):
                    lines.append("受理人评论：" + str(entry["comment"]))
                if entry.get("accepted_at"):
                    lines.append("受理时间：" + str(entry["accepted_at"]))
                if entry.get("acceptance"):
                    acceptance = str(entry["acceptance"])
                    lines.append("受理情况：" + {"未": "尚未受理", "已": "已受理"}.get(acceptance, acceptance))
            if payload.get("status") == "转现场处理":
                lines.append("该状态表示已转交现场人员，不表示任务已解决。")
    elif kind in {"pending_approvals", "pending_executions"}:
        label = "待审核" if kind == "pending_approvals" else "待执行"
        count = payload.get("count")
        lines.append(f"目前{label}需求：{count if count is not None else '未知'} 条")
        if payload.get("date_from"):
            lines.append(f"时间范围：{payload['date_from']} 至 {payload.get('date_to') or payload['date_from']}（按{payload.get('date_field') or '业务时间'}）")
        for index, item in enumerate(payload.get("items") or [], 1):
            lines.append(f"{index}. {item.get('identifier') or '未命名'}；工作编号 {item.get('work_num') or '未知'}；{item.get('status') or label}")
            if item.get("relevant_date"):
                lines.append("业务时间：" + str(item["relevant_date"]))
        if payload.get("has_more"):
            lines.append(f"以上展示前 {payload.get('shown_count', 20)} 条。")
    else:
        lines.append("需求：" + str(payload.get("identifier") or "未知"))
        if payload.get("status") in {"未找到", "需要选择"}:
            lines.append(str(payload.get("message") or payload["status"]))
            for item in payload.get("matches") or []:
                lines.append(f"工作编号：{item['work_num']}；填报时间：{item.get('created_at') or '未知'}")
        else:
            execution = payload.get("execution") or {}
            lines.append("工作编号：" + str(payload.get("work_num") or "未知"))
            lines.append("执行结果：" + str(execution.get("status") or "未知"))
            if execution.get("message"):
                lines.append(str(execution["message"]))
            count = execution.get("affected_rows_total")
            lines.append(f"影响条数合计：{count if count is not None else '未知'}（逐语句相加）")
            for index, row in enumerate(execution.get("details") or [], 1):
                count = row.get("affected_rows")
                elapsed = row.get("elapsed_seconds")
                if include_sql:
                    lines.append(f"SQL {index} 原文：\n{row.get('sql') or '系统未返回 SQL 原文'}")
                lines.append(f"SQL {index}：{row.get('status') or '未知'}，影响 {count if count is not None else '未知'} 条" + (f"，耗时 {elapsed} 秒" if elapsed not in (None, "") else ""))
                if row.get("exception"):
                    lines.append(f"SQL {index} 异常详情：{row['exception']}")
            if execution.get("exceptions"):
                for error in dict.fromkeys(str(x) for x in execution["exceptions"]):
                    if not any(str(row.get("exception") or "") == error for row in execution.get("details") or []):
                        lines.append("异常详情：" + error)
            elif execution.get("complete") and execution.get("status") == "执行成功":
                lines.append("异常详情：无")
            elif execution.get("status") == "执行失败":
                lines.append("异常详情：系统未返回异常文本")
            else:
                lines.append("异常详情：未知，尚未取得完整执行结果")
    if payload.get("scope"):
        lines.append("范围：" + str(payload["scope"]))
    if payload.get("queried_at"):
        lines.append("查询时间：" + str(payload["queried_at"]))
    return "\n".join(lines)


def query_reply_chunks(reply):
    # Prefix every chunk, including long exceptions, to prevent reply loops.
    reply = str(reply)
    prefix = PROBLEM_QUERY_REPLY_PREFIX if reply.startswith(PROBLEM_QUERY_REPLY_PREFIX) else QUERY_REPLY_PREFIX
    body = reply.removeprefix(prefix).lstrip("：\n")
    return tuple(prefix + "\n" + part for part in split_reply(body, 1800-len(prefix)-1))


def owner_query_config(config, message, owner, history=()):
    """Allow the logged-in owner's explicit queries through only this Agent."""
    if not config.get("enabled") or str(message.get("msgtype")) != "1" or not owner or str(message.get("fromid") or "") != owner:
        return None
    if not query_candidate(message.get("msg"), history):
        return None
    tasks = [t for t in config.get("ai_tasks") or [] if t.get("enabled") and t.get("mcp_enabled") and t.get("mcp_tool_name") == QUERY_TOOL and t.get("message_type", "text") == "text"]
    if not tasks:
        return None
    return {**config, "rules": [], "ai_tasks": tasks, "message_types": ["text"],
            "mention_only": False, "mention_message_types": [], "target_senders_by_type": {"text": [owner]}}
