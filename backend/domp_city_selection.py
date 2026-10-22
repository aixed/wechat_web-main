"""Numbered, server-verified city choices for a stopped SQL workflow."""
from __future__ import annotations

import re

CITY_SELECTION_PREFIX = "数据运维区划选择"


def city_selection_failure(payload):
    if not isinstance(payload, dict) or payload.get("dry_run") or payload.get("duplicate"):
        return False
    stage = payload.get("submission") or {}
    return bool(stage.get("status") == "填报失败"
        and not any(payload.get(key) for key in ("work_id", "work_num"))
        and not stage.get("demand_submitted") and not stage.get("sql_submitted")
        and all((payload.get(key) or {}).get("status") == "未开始" for key in ("approval", "execution"))
        and (stage.get("requires_city_selection") is True or re.search(
            r"行政区划匹配不唯一|行政区划不唯一|找不到行政区划|未匹配到.*行政区划", str(stage.get("message") or ""))))


def selection_options(payload, supported):
    """Use the failure's order, accepting only names/codes in the live catalog."""
    catalog = {}
    for area in supported:
        if isinstance(area, dict) and area.get("area_name") and area.get("area_code"):
            catalog.setdefault(str(area["area_name"]), []).append(area)
    stage = payload.get("submission") or {}
    candidates = stage.get("area_candidates")
    if not isinstance(candidates, list) or not candidates:
        match = re.search(r"(?:请手工选择|请选择)[：:]\s*(.+)", str(stage.get("message") or ""))
        candidates = [name.strip() for name in match[1].split("、")] if match else list(catalog)
    options, seen = [], set()
    for candidate in candidates:
        name = str(candidate.get("area_name") or "") if isinstance(candidate, dict) else str(candidate)
        matches = catalog.get(name, [])
        if len(matches) != 1:
            continue
        area = matches[0]
        if isinstance(candidate, dict) and candidate.get("area_code") != area["area_code"]:
            continue
        if area["area_code"] not in seen:
            options.append({"area_name": name, "area_code": area["area_code"]})
            seen.add(area["area_code"])
    return options


def selection_number(source, identifier):
    text = str(source or "").strip().rstrip("。.!！")
    text = re.sub(rf"^(?:需求|线下单|xzsb-)?\s*{re.escape(identifier)}[\s:：,，]+", "", text, flags=re.I)
    if re.fullmatch(r"[0-9]{1,3}", text):
        return int(text)
    return None


def selection_message(pending):
    if pending.get("confirmed_city"):
        return f"{CITY_SELECTION_PREFIX}：已按 {pending['confirmed_city']} 发起处理，请查询需求 {pending['arguments']['identifier']} 的结果，勿重复提交。"
    lines = [f"{CITY_SELECTION_PREFIX}：需求 {pending['arguments']['identifier']}，尚未填报。", "请选择城市："]
    lines.extend(f"{index}. {area['area_name']}" for index, area in enumerate(pending.get("city_options") or [], 1))
    mention = "空气" if pending.get("owner_only") else pending.get("owner_mention") or "Aixed"
    if pending.get("city_options"):
        lines.append(f"请 @{mention} 回复数字（例如：@{mention} 3），程序将按所选区划直接填报、审核、执行。")
    else:
        lines.append(f"区划选项暂未读取成功，请 @{mention} 回复“重试”重新读取。")
    lines.append(f"如不再处理，请 @{mention} 回复“取消”。")
    return "\n".join(lines)
