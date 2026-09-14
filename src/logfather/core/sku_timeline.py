"""SKU/manual band construction: the state machine that turns a robot's
operation-state event stream into timeline bands.

Pure logic extracted from elastic_loader.fetch_sku_items (it previously
lived inline in a 350-line network function, with a drifted dead twin
that Stage 1 deleted).

Events are (timestamp, kind, selection|None, state_name) tuples where kind
is one of: "start" (start_pnp), "stop" (stop-like states), "manual",
"auto", "select" (SKU selection without a start). Later events at the
same timestamp are ordered stop < auto < manual < start < select.
"""
from __future__ import annotations

from datetime import datetime

from logfather.core.log import dbg
from logfather.core.timeline_model import TimelineItem


def build_sku_bands(
    events: list[tuple[datetime, str, dict | None, str]],
    cap_end: datetime,
) -> list[TimelineItem]:
    if not events:
        return []

    manual_event_count = sum(1 for _ts, kind, _data, _state in events if kind == "manual")
    if manual_event_count:
        dbg("sku-debug", f"manual events={manual_event_count}")

    order = {"stop": 0, "auto": 1, "manual": 2, "start": 3, "select": 4}
    events.sort(key=lambda item: (item[0], order.get(item[1], 9)))
    items: list[TimelineItem] = []
    current_kind: str | None = None
    current_data: dict | None = None
    current_start: datetime | None = None
    last_sku_data: dict | None = None

    def _sku_key(payload: dict | None) -> tuple[str, str, str]:
        if not isinstance(payload, dict):
            return ("", "", "")
        return (
            str(payload.get("sku") or ""),
            str(payload.get("tray") or ""),
            str(payload.get("tool") or ""),
        )

    def _close_current(end_ts: datetime):
        nonlocal current_kind, current_data, current_start
        if not current_kind or not current_start:
            return
        if end_ts <= current_start:
            current_kind = None
            current_data = None
            current_start = None
            return
        if current_kind == "sku":
            sku = current_data.get("sku") if current_data else ""
            tray = current_data.get("tray") if current_data else ""
            tool = current_data.get("tool") if current_data else ""
            items.append(
                TimelineItem(
                    start=current_start,
                    end=end_ts,
                    label=sku or "SKU",
                    kind="sku",
                    color="#8fd19e",
                    payload={"_ui_sku": sku, "_ui_tray": tray, "_ui_tool": tool},
                    track_label="SKU",
                )
            )
        else:
            items.append(
                TimelineItem(
                    start=current_start,
                    end=end_ts,
                    label="Manual",
                    kind="sku",
                    color="#f59e0b",
                    payload={"_ui_manual": True},
                    track_label="SKU",
                )
            )
        current_kind = None
        current_data = None
        current_start = None

    for ts, kind, data, _state in events:
        if ts >= cap_end:
            break
        if kind == "stop":
            # Stop-like states end SKU runs, but should not collapse an active manual span.
            if current_kind == "sku":
                _close_current(min(ts, cap_end))
            continue
        if kind == "auto":
            # Automatic mode ends manual periods.
            if current_kind == "manual":
                _close_current(min(ts, cap_end))
            continue
        if kind == "manual":
            if current_kind == "manual":
                continue
            _close_current(min(ts, cap_end))
            current_kind = "manual"
            current_data = None
            current_start = ts
            continue
        if kind == "select" and data:
            last_sku_data = data
            if current_kind == "sku" and _sku_key(current_data) == _sku_key(data):
                continue
            if current_kind == "sku" and current_start:
                _close_current(min(ts, cap_end))
                current_kind = "sku"
                current_data = data
                current_start = ts
            continue
        if kind == "start":
            if data:
                last_sku_data = data
            start_data = data or last_sku_data
            _close_current(min(ts, cap_end))
            if start_data is None and last_sku_data:
                start_data = last_sku_data
            if start_data is None:
                start_data = {}
            current_kind = "sku"
            current_data = start_data
            current_start = ts
    if current_kind and current_start:
        _close_current(cap_end)
    manual_item_count = sum(1 for itm in items if isinstance(itm.payload, dict) and itm.payload.get("_ui_manual"))
    if manual_item_count:
        dbg("sku-debug", f"manual items={manual_item_count}")
    return items
