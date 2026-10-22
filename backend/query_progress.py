"""Truthful progress text for long-running read-only queries."""
from time import monotonic


def update_progress(state, snapshot):
    state.clear()
    state.update(snapshot, received_at=monotonic())


def progress_text(state, elapsed_seconds):
    phase = state.get("phase", "analysis")
    labels = {"analysis": "正在识别查询条件", "remote": "正在等待查询服务返回",
              "queued": "正在排队，等待前一项查询完成", "list": "正在读取单据列表",
              "history": "正在补全单据处理时间", "count": "正在汇总数量",
              "formatting": "正在整理查询结果和表格", "completed": "查询已完成，正在整理回复",
              "failed": "查询服务报告失败，正在读取错误详情",
              "unavailable": "暂时无法取得服务进度，正在等待原查询响应"}
    lines = [f"查询处理进度（已耗时 {max(1, int(elapsed_seconds))} 秒）", labels.get(phase, "正在等待查询服务返回")]
    if phase == "list" and state.get("page"):
        lines.append(f"列表页数：{state['page']} / {state.get('pages') or '待确认'}")
    if state.get("source_total") is not None:
        lines.append(f"已读取 {state.get('scanned_count', 0)} / {state['source_total']} 条单据，符合条件 {state.get('matched_count', '待确认')} 条")
    if phase == "history":
        lines.append(f"处理时间已补全 {state.get('history_done', 0)} / {state.get('history_total', '待确认')} 条")
    idle = float(state.get("idle_seconds") or 0)
    if state.get("received_at"):
        idle += max(0, monotonic() - state["received_at"])
    if idle >= 90 and phase not in {"completed", "failed", "unavailable"}:
        lines.append(f"最近 {int(idle)} 秒没有新增结果，正在等待平台响应。")
    return "\n".join(lines)
