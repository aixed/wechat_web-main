"""Update an existing group's SQL Skills while preserving sender allowlists."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from domp_bridge import PROMPT, WORKFLOW_TOOL
from domp_query_bridge import QUERY_PROMPT, QUERY_TOOL
from domp_execution_bridge import EXECUTION_PROMPT, EXECUTION_TOOL
from domp_approval_bridge import APPROVAL_PROMPT, APPROVAL_TOOL
from domp_submission_bridge import SUBMISSION_PROMPT, SUBMISSION_TOOL
from domp_unified_bridge import UNIFIED_PROMPT, UNIFIED_SKILL_ID, UNIFIED_SKILL_NAME, is_unified_task
from sqlite_cache import SqliteMessageCache


MANAGED_TOOLS = {WORKFLOW_TOOL, QUERY_TOOL, APPROVAL_TOOL, EXECUTION_TOOL, SUBMISSION_TOOL}


def configure(owner, chat, db_path=None, *, add_query_agent=False, add_approval_agent=False,
              add_execution_agent=False, add_submission_agent=False, unify_agent=False):
    cache = SqliteMessageCache(str(db_path) if db_path else None)
    row = cache.get_smart_reply_config(chat, owner_wxid=owner)
    if not row:
        raise ValueError("目标群尚未配置智能回复，请先在页面选择发送人")
    all_tasks = [task for task in row.get("ai_tasks") or [] if isinstance(task, dict)]
    legacy_sql_tasks = [x for x in all_tasks if x.get("mcp_enabled") and
                        ("sql" in x.get("name", "").casefold() or x.get("mcp_tool_name") == WORKFLOW_TOOL)]
    tasks = legacy_sql_tasks or [x for x in all_tasks if x.get("mcp_enabled") and is_unified_task(x)]
    if not tasks:
        raise ValueError("目标群没有现有 SQL Skill，未修改配置")
    backup = Path(cache.db_path).parent / f"domp_skill_before_{int(time())}.json"
    backup.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    if unify_agent:
        managed = [task for task in all_tasks if task.get("mcp_tool_name") in MANAGED_TOOLS or is_unified_task(task)]
        text_template = next((task for task in managed if task.get("message_type", "text") == "text"), tasks[0])
        unified_tasks = []
        for message_type in ("text", "file"):
            template = next((task for task in managed if task.get("message_type", "text") == message_type), None)
            if template is None:
                continue
            unified_tasks.append({**template,
                "id": f"{UNIFIED_SKILL_ID}_{message_type}",
                "skill_id": UNIFIED_SKILL_ID,
                "message_type": message_type,
                "name": UNIFIED_SKILL_NAME,
                "instruction": UNIFIED_PROMPT,
                "enabled": True,
                "confidence": 90,
                "output_mode": "result",
                "reply_template": "{{result}}",
                "preserve_formatting": True,
                "send_items_separately": False,
                "max_parallel": 1,
                "mcp_enabled": True,
                "mcp_connection_id": str(template.get("mcp_connection_id") or text_template.get("mcp_connection_id") or ""),
                "mcp_tool_name": "",
                "mcp_arguments_template": "{}",
                "mcp_reply_template": "{{mcp_text}}",
            })
        if not unified_tasks:
            raise ValueError("没有可迁移的数据运维 Skill")
        row["ai_tasks"] = [task for task in all_tasks if task not in managed] + unified_tasks
        row.update(mention_only=False, mention_message_types=[])
        cache.upsert_smart_reply_config(row, owner_wxid=owner)
        return backup
    if not legacy_sql_tasks:
        raise ValueError("目标群已使用统一数据运维 Agent；请使用 --unify-agent 更新统一配置")
    for task in legacy_sql_tasks:
        task.update(name="SQL 风险分析及数据运维填报审核执行", instruction=PROMPT,
                    mcp_tool_name=WORKFLOW_TOOL, mcp_arguments_template="{}", mcp_reply_template="{{mcp_text}}",
                    output_mode="result", preserve_formatting=True, send_items_separately=False, max_parallel=1)
    if add_query_agent:
        query_tasks = [x for x in row.get("ai_tasks") or [] if x.get("mcp_tool_name") == QUERY_TOOL]
        if not query_tasks:
            query_task = {**tasks[0], "id": "domp_readonly_query_agent_text", "skill_id": "domp_readonly_query_agent_text", "message_type": "text", "enabled": True}
            row["ai_tasks"].append(query_task)
            query_tasks = [query_task]
        for task in query_tasks:
            task.update(name="任务与数据运维查询 Agent", instruction=QUERY_PROMPT, mcp_tool_name=QUERY_TOOL,
                        mcp_arguments_template="{}", mcp_reply_template="{{mcp_text}}", max_parallel=1)
    if add_execution_agent:
        execution_tasks = [x for x in row.get("ai_tasks") or [] if x.get("mcp_tool_name") == EXECUTION_TOOL]
        if not execution_tasks:
            template = next((t for t in tasks if t.get("message_type", "text") == "text"), tasks[0])
            execution_task = {**template, "id": "domp_execution_agent_text", "skill_id": "domp_execution_agent_text", "message_type": "text", "enabled": True}
            row["ai_tasks"].append(execution_task)
            execution_tasks = [execution_task]
        for task in execution_tasks:
            task.update(name="数据运维执行 Agent", instruction=EXECUTION_PROMPT, mcp_tool_name=EXECUTION_TOOL,
                        mcp_arguments_template="{}", mcp_reply_template="{{mcp_text}}", max_parallel=1)
    if add_approval_agent:
        approval_tasks = [task for task in row.get("ai_tasks") or [] if task.get("mcp_tool_name") == APPROVAL_TOOL]
        if not approval_tasks:
            template = next((task for task in tasks if task.get("message_type", "text") == "text"), tasks[0])
            approval_task = {**template, "id": "domp_approval_agent_text", "skill_id": "domp_approval_agent_text",
                             "message_type": "text", "enabled": True}
            row["ai_tasks"].append(approval_task)
            approval_tasks = [approval_task]
        for task in approval_tasks:
            task.update(name="数据运维审核 Agent", instruction=APPROVAL_PROMPT, mcp_tool_name=APPROVAL_TOOL,
                        mcp_arguments_template="{}", mcp_reply_template="{{mcp_text}}", max_parallel=1)
    if add_submission_agent:
        submission_tasks = [task for task in row.get("ai_tasks") or [] if task.get("mcp_tool_name") == SUBMISSION_TOOL]
        if not submission_tasks:
            template = next((task for task in tasks if task.get("message_type", "text") == "text"), tasks[0])
            submission_task = {**template, "id": "domp_submission_agent_text", "skill_id": "domp_submission_agent_text", "message_type": "text", "enabled": True}
            row["ai_tasks"].append(submission_task)
            submission_tasks = [submission_task]
        for task in submission_tasks:
            task.update(name="数据运维填报 Agent", instruction=SUBMISSION_PROMPT, mcp_tool_name=SUBMISSION_TOOL,
                mcp_arguments_template="{}", mcp_reply_template="{{mcp_text}}", max_parallel=1)
    # An authorized sender's text/TXT is the trigger; preserve per-type lists.
    row.update(mention_only=False, mention_message_types=[])
    cache.upsert_smart_reply_config(row, owner_wxid=owner)
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--add-query-agent", action="store_true")
    parser.add_argument("--add-approval-agent", action="store_true")
    parser.add_argument("--add-execution-agent", action="store_true")
    parser.add_argument("--add-submission-agent", action="store_true")
    parser.add_argument("--unify-agent", action="store_true")
    args = parser.parse_args()
    print(configure(args.owner, args.chat, add_query_agent=args.add_query_agent, add_approval_agent=args.add_approval_agent,
                    add_execution_agent=args.add_execution_agent, add_submission_agent=args.add_submission_agent,
                    unify_agent=args.unify_agent))
