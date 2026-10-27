"""Ticket names preserve explicit supplementary batch suffixes."""
from __future__ import annotations

from runtime_settings import prompt, setting

import re

TICKET_PATTERN = r"(?:\d{4}|\d{5}(?:-[1-9]\d*)?)"
WORK_PATTERN = r"\d{6,32}"


def is_ticket_identifier(value):
    return bool(re.fullmatch(TICKET_PATTERN, str(value or "")))


def identifiers_in(source, *, allow_work_numbers=True, strict=False):
    # Consume the whole numeric token before validation, never truncate -0/-1-2.
    if strict:
        batches = re.findall(r"(?<!\d)(\d{4,5}-[\d-]*)", str(source or ""))
        if any(not is_ticket_identifier(value) for value in batches):
            raise ValueError("补充批次编号格式不正确，请使用 42157-1、42157-2 等正整数后缀")
    tokens = re.findall(r"(?<!\d)(\d+(?:-\d+)*)(?![\d-])", str(source or ""))
    return {token for token in tokens if is_ticket_identifier(token)
            or allow_work_numbers and re.fullmatch(WORK_PATTERN, token)}


def base_identifier(value):
    return str(value or "").split("-", 1)[0]


def resubmission_hint(identifier, used_identifiers):
    base = base_identifier(identifier)
    if len(base) == 4:
        return {"next_identifier": "", "message": f"线下单 {identifier} 已有填报记录，请核对原流程；新的线下申请请使用新的四位线下编号。"}
    suffixes = [int(value[len(base) + 1:]) for value in used_identifiers
                if re.fullmatch(re.escape(base) + r"-[1-9]\d*", value)]
    next_identifier = base + "-" + str(max(suffixes, default=0) + 1)
    return {"next_identifier": next_identifier,
        "message": f"该编号已有填报记录。如需重新填报，请将消息首行改为：\n--- xzsb-{next_identifier}\n并附上本批次完整 SQL 后重新发送；后续使用新的递增批次编号（-1、-2、-3 等）。"}


BATCH_IDENTIFIER_PROMPT = prompt('domp_identifier.batch_identifier_prompt')
