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
from domp_submission_bridge import SUBMISSION_PROMPT, SUBMISSION_TOOL
from sqlite_cache import SqliteMessageCache


def configure(owner, chat, db_path=None, *, add_query_agent=False, add_execution_agent=False, add_submission_agent=False):
    cache = SqliteMessageCache(str(db_path) if db_path else None)
    row = cache.get_smart_reply_config(chat, owner_wxid=owner)
    if not row:
        raise ValueError("目标群尚未配置智能回复，请先在页面选择发送人")
    tasks = [x for x in row.get("ai_tasks") or [] if x.get("mcp_enabled") and ("sql" in x.get("name", "").casefold() or x.get("mcp_tool_name") == WORKFLOW_TOOL)]
    if not tasks:
        raise ValueError("目标群没有现有 SQL Skill，未修改配置")
    backup = Path(cache.db_path).parent / f"domp_skill_before_{int(time())}.json"
    backup.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    for task in tasks:
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
    parser.add_argument("--add-execution-agent", action="store_true")
    parser.add_argument("--add-submission-agent", action="store_true")
    args = parser.parse_args()
    print(configure(args.owner, args.chat, add_query_agent=args.add_query_agent, add_execution_agent=args.add_execution_agent, add_submission_agent=args.add_submission_agent))
