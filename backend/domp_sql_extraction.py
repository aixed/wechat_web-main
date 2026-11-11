"""Verify AI-selected SQL against the message before any business operation."""
from __future__ import annotations

import re
from runtime_settings import prompt


SQL_COMMAND = re.compile(
    r"\b(?:UPDATE|SELECT|INSERT|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|"
    r"REVOKE|BEGIN|DECLARE|CALL|EXECUTE|EXEC|WITH|COMMIT|ROLLBACK|SAVEPOINT|"
    r"SET|USE|EXPLAIN|REPLACE|UPSERT|PREPARE|COPY|LOAD|VACUUM|ANALYZE)\b", re.I)

SQL_EXTRACTION_PROMPT = prompt('domp_sql_extraction.sql_extraction_prompt')


def _sql_code(text):
    """Mask comments and quoted values without changing source offsets."""
    pattern = re.compile(
        r"--[^\n]*|/\*[\s\S]*?(?:\*/|$)|"
        r"'(?:''|[^'])*(?:'|$)|\"(?:\"\"|[^\"])*(?:\"|$)|"
        r"`(?:``|[^`])*(?:`|$)|\[(?:\]\]|[^\]])*(?:\]|$)")
    return pattern.sub(lambda match: " " * len(match[0]), text)


def extracted_sql_source(source, blocks):
    # Old saved analysis responses have no selection. Preserve their original
    # input for the MCP parser; never guess new SQL from an absent model result.
    if blocks is None:
        return source
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("AI 未提取到完整 SQL，未填报、审核或执行")
    code = _sql_code(source)
    spans, position, selected = [], 0, []
    for block in blocks:
        if not isinstance(block, str) or not block.strip():
            raise ValueError("AI SQL 提取格式不完整，未启动流程")
        block = block.strip()
        start = source.find(block, position)
        if start < 0:
            raise ValueError("AI 提取的 SQL 与原文不一致或顺序改变，未启动流程")
        end = start + len(block)
        block_code = code[start:end].strip()
        if not SQL_COMMAND.match(block_code):
            raise ValueError("AI 提取片段不是完整 SQL，未启动流程")
        # A selection must end at the original statement delimiter. Without a
        # delimiter it may only end at the source's last non-comment character.
        if not block_code.endswith(";") and code[end:].strip():
            raise ValueError("AI 提取的 SQL 可能遗漏后续条件；请用分号分隔 SQL 与聊天文字")
        spans.append((start, end))
        selected.append(block)
        position = end
    for match in SQL_COMMAND.finditer(code):
        if not any(start <= match.start() < end for start, end in spans):
            raise ValueError("AI 提取结果可能遗漏 SQL 或语句片段，未启动流程；请将 SQL 与聊天说明分行")
    return "\n".join(selected)
