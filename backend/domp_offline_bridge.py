"""Persist offline requests while the sender supplies or confirms the application city."""
from __future__ import annotations

from runtime_settings import prompt, setting

import json
import re
import sqlite3
import time
from contextlib import contextmanager

CITY_PROMPT = prompt('domp_offline_bridge.city_prompt')
OFFLINE_PROMPT_EXTENSION = prompt('domp_offline_bridge.offline_prompt_extension')


def offline_analysis_task(task, source):
    """Extend saved prompts for existing installations without discarding custom rules."""
    if re.search(r"(?<!\d)\d{4}(?!\d)", str(source)):
        from domp_unified_bridge import is_unified_task
        if is_unified_task(task) or task.get("mcp_tool_name") in {"process_data_maintenance", "query_data_maintenance", "approve_data_maintenance", "execute_data_maintenance"}:
            return {**task, "instruction": str(task.get("instruction") or "") + "\n\n" + OFFLINE_PROMPT_EXTENSION}
    return task


def is_offline_confirmation_prompt(source):
    """Confirmation instructions echoed by WeChat are never user commands."""
    text = str(source or "")
    if "数据运维区划选择：需求 " in text and "请选择城市：" in text:
        return True
    if re.search(r"线下单\s+\d{4}\s+未(?:识别|匹配)到城市或县区", text):
        return True
    return bool(re.search(r"(?:^|\s)(?:线下单\s+\d{4}\s*[:：]\s*)?待确认(?:区划|城市)[，,]", text)
        and re.search(r"匹配区划|尚未匹配|当前匹配到城市|当前识别/输入", text))


def city_input(source, identifier=""):
    if is_offline_confirmation_prompt(source):
        return "", ""
    text = str(source or "").strip().rstrip("。.!！")
    if identifier:
        text = re.sub(rf"^(?:线下单|需求|xzsb-)?\s*{re.escape(identifier)}[\s:：,，]+", "", text, flags=re.I)
    if text in {"是", "确认", "确定", "是的", "对", "没问题", "按识别结果", "确认填报"}:
        return "confirm", ""
    if text in {"取消", "停止", "不要处理", "先不处理"}:
        return "cancel", ""
    if len(text) > 100 or re.search(r"不要|不是|明天|以后|如果|假如|如何|怎么|SQL|UPDATE|DELETE|INSERT|SELECT", text, re.I):
        return "", ""
    text = re.sub(r"^(?:改成|改为|修改为|正确城市是|(?:行政区划|县区|区划|城市(?:或县区)?|申请城市)(?:是|为|[:：])?)\s*", "", text)
    return "input", text


class OfflineConfirmationStore:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS offline_city_confirmations (owner TEXT, chat TEXT, sender TEXT, payload TEXT, updated_at REAL, PRIMARY KEY(owner,chat,sender))")
                yield db
        finally:
            db.close()

    @staticmethod
    def key(context):
        return tuple(str(context.get(key) or "") for key in ("owner_wxid", "chat_id", "sender"))

    def get(self, context):
        if not all(self.key(context)):
            return None
        with self._db() as db:
            row = db.execute("SELECT payload,updated_at FROM offline_city_confirmations WHERE owner=? AND chat=? AND sender=?", self.key(context)).fetchone()
        if row and time.time() - row[1] <= 24 * 60 * 60:
            return json.loads(row[0])
        return None

    def save(self, context, pending):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO offline_city_confirmations VALUES(?,?,?,?,?)", (*self.key(context), json.dumps(pending, ensure_ascii=False), time.time()))

    def for_chat(self, context):
        with self._db() as db:
            rows = db.execute("SELECT sender,payload FROM offline_city_confirmations WHERE owner=? AND chat=? AND updated_at>?",
                (*self.key(context)[:2], time.time() - 24 * 60 * 60)).fetchall()
        return [(sender, json.loads(payload)) for sender, payload in rows]

    def delete(self, context):
        with self._db() as db:
            db.execute("DELETE FROM offline_city_confirmations WHERE owner=? AND chat=? AND sender=?", self.key(context))


def _area_province(area):
    path = area.get('area_path')
    if not isinstance(path, list):
        path = str(area.get('area_name') or '').split('.')
    first = str(path[0]).strip() if path else ''
    return first if re.search(r'(?:省|自治区|特别行政区)$', first) or first in {'北京市', '天津市', '上海市', '重庆市'} else ''


def supported_area_lines(pending):
    matched = pending.get('city_match') or {}
    groups = {}
    seen = set()
    for area in matched.get('supported_areas') or []:
        if not isinstance(area, dict) or not area.get('area_name') or not area.get('area_code'):
            continue
        name = str(area['area_name']).strip()
        province = _area_province(area)
        if name == province or str(area['area_code']) in seen:
            continue
        seen.add(str(area['area_code']))
        if province:
            name = name.removeprefix(province + '.')
        groups.setdefault(province, []).append(name)
    text = str(pending.get('candidate_text') or '')
    provinces = {province for province in groups if province and (
        province in text or re.sub(r'(?:省|自治区|特别行政区)$', '', province) in text)}
    if not provinces:
        provinces = {_area_province(area) for area in matched.get('matches') or [] if isinstance(area, dict)} - {''}
    lines = []
    for province, names in groups.items():
        if provinces and province not in provinces:
            continue
        lines.extend([f'系统支持的{province}市/县：', '、'.join(names)])
    if not groups and isinstance(matched.get('supported_cities'), list):
        names = list(dict.fromkeys(str(city) for city in matched['supported_cities'] if city))
        if names:
            lines.extend(['系统支持的城市：', '、'.join(names)])
    return lines


def confirmation_message(pending):
    args = pending["arguments"]
    owner_mention = pending.get("owner_mention") or setting('workflow', 'owner_display_name') or ''
    self_mention = next(iter(setting('workflow', 'self_mention_names', default=[])), '')
    owner_only = pending.get("owner_only")
    if pending.get('confirmed_city'):
        return f"线下单 {args['identifier']} 已按 {pending['confirmed_city']} 发起处理，请查询原流程结果，勿重复提交。"
    lines = [f"线下单 {args['identifier']}：待确认区划，尚未填报。"]
    if (pending.get("city_match") or {}).get("matched"):
        area = pending.get("matched_area_display") or pending["matched_city"]
        lines.append(f"匹配区划：{area}。是否确认填报？")
        lines.append(f"回复“@{self_mention} 是”确认。" if owner_only else f"确认：本人 @{self_mention} 是；其他人 @{owner_mention} 是。")
    else:
        lines = [f"线下单 {args['identifier']} 未识别到城市或县区"]
        if pending.get('city_input_provided'):
            lines = [f"线下单 {args['identifier']} 未匹配到城市或县区：{pending.get('candidate_text') or ''}"]
            lines.extend(supported_area_lines(pending))
        elif pending.get('candidate_text') or pending.get('department'):
            recognized = str(pending.get('candidate_text') or pending['department']).strip()
            regions = re.findall(r"[\u4e00-\u9fff]+?(?:自治州|地区|市|自治县|县|新区|区|旗|盟)", recognized)
            lines.append('当前识别/输入：' + (' / '.join(regions) if regions else recognized))
        mention = self_mention if owner_only else owner_mention
        lines.append(f"请输入城市或县区(示例：@{mention} 拉萨市 或 @{mention} 边坝县)")
        if pending.get("match_error"):
            lines.append("区划查询：" + pending["match_error"])
    return "\n".join(lines)


def offline_reply_chunks(reply):
    from domp_bridge import split_reply
    header, separator, body = str(reply).partition('\n')
    if separator and header.startswith('线下单 '):
        return tuple(header + '\n' + part for part in split_reply(body, limit=1800 - len(header) - 1))
    return split_reply(reply)
