from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from logfather.core.log import dbg, log
from logfather.data.elastic_client import (
    api_headers,
    get_thread_session as _get_thread_session,
    paginate,
    search_url as _search_url,
)
from logfather.data.elastic_loader import (
    ELASTIC_TIMESTAMP_FIELDS,
    KIBANA_BASE_DEFAULT,
    _build_robot_filters,
    _ensure_utc,
    _normalize_index_id,
    _parse_ts,
)
from logfather.data.settings_store import Settings


@dataclass(slots=True)
class PickTarget:
    target_id: str
    added_at: datetime
    source_doc: dict


@dataclass(slots=True)
class BufferEvent:
    timestamp: datetime
    event_type: str  # 'target_added'
    buffer_snapshot: list[PickTarget] = field(default_factory=list)
    source_doc: dict = field(default_factory=dict)


def _build_buffer_query(
    robot_id: str,
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    sort_field: str,
    size: int = 1000,
    search_after: list | None = None,
) -> dict:
    ts_should = [
        {"range": {f: {"gte": start_iso, "lte": end_iso, "format": "strict_date_optional_time"}}}
        for f in ts_fields
    ] or [{"range": {"@timestamp": {"gte": start_iso, "lte": end_iso, "format": "strict_date_optional_time"}}}]

    body = {
        "size": size,
        "track_total_hits": False,
        "_source": True,
        "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_robot_filters(robot_id),
                    {"bool": {"should": ts_should, "minimum_should_match": 1}},
                    {
                        "bool": {
                            "should": [
                                # targeting_node: rich target data (position, SKU, metrics)
                                {
                                    "bool": {
                                        "must": [
                                            {"wildcard": {"source": "*targeting_node*"}},
                                            {"match_phrase": {"message": "Received new unique pick target"}},
                                        ]
                                    }
                                },
                                # motion_control_node: queue event; product_id links to target_index
                                {
                                    "bool": {
                                        "must": [
                                            {"wildcard": {"source": "*motion_control_node*"}},
                                            {"match_phrase": {"message": "Adding new target to queue"}},
                                        ]
                                    }
                                },
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                ]
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def fetch_buffer_events(
    settings: Settings,
    pikpak_root: Path | None,
    clip_start: datetime,
    clip_end: datetime,
    lookback_minutes: int = 60,
    *,
    robot_id: str | None,
) -> list[BufferEvent]:
    """
    Fetch pick-target queue events for a clip window and simulate the buffer state machine.

    Queries from (clip_start - lookback_minutes) to clip_end so the buffer state
    at the start of the clip is reconstructed without loading an entire day.

    robot_id is the system to query, resolved by the caller on the UI thread
    (elastic_schema.resolve_robot_id); None means nothing to fetch.

    Combines two message types:
    - targeting_node  "Received new unique pick target"  — position, SKU, metrics
    - motion_control_node "Adding new target to queue"   — queue timestamp, product_id

    The two are joined by product_id (motion_control) == target_index (targeting).
    Each BufferEvent carries a merged source_doc with fields from both messages.
    """
    from datetime import timedelta

    dbg("buffer", f"robot_id={robot_id!r}  pikpak_root={pikpak_root}")
    if not robot_id:
        log("buffer", "no robot_id - aborting")
        return []
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    dbg("buffer", f"url={url!r}  index_id={index_id!r}  api_key={'set' if api_key else 'MISSING'}")
    if not url or not api_key or not index_id:
        log("buffer", "missing url/api_key/index_id - aborting")
        return []

    window_start = _ensure_utc(clip_start) - timedelta(minutes=lookback_minutes)
    window_end   = _ensure_utc(clip_end)
    start_iso = window_start.isoformat().replace("+00:00", "Z")
    end_iso   = window_end.isoformat().replace("+00:00", "Z")
    dbg("buffer", f"window {start_iso} -> {end_iso}")
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    sort_field = ts_fields[0] if ts_fields else "@timestamp"
    headers = api_headers(api_key)
    search_endpoint = _search_url(url, index_id)
    session = _get_thread_session()

    # Errors keep whatever pages arrived: partial buffer state beats none.
    outcome = paginate(
        lambda size, search_after: _build_buffer_query(
            robot_id, start_iso, end_iso, ts_fields, sort_field,
            size=size, search_after=search_after,
        ),
        session=session,
        endpoint=search_endpoint,
        headers=headers,
        page_size=1000,
        max_pages=20,
        timeout_sec=12,
        label="buffer query",
        on_error="warn",
    )

    # Separate buckets for the two message types
    targeting: dict[int, dict] = {}        # target_index -> source_doc
    motion: list[tuple[datetime, dict]] = []

    for hit in outcome.hits:
        src = hit.get("_source", {})
        ts_val = src.get("@timestamp_ros") or src.get("@timestamp")
        ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
        if not ts:
            continue
        ts = _ensure_utc(ts)
        source_str = str(src.get("source") or "").lower()
        message = str(src.get("message") or "").lower()

        if "targeting_node" in source_str and "received new unique pick target" in message:
            idx = src.get("target_index")
            if idx is not None:
                try:
                    targeting[int(idx)] = src
                except (TypeError, ValueError):
                    pass

        elif "motion_control_node" in source_str and "adding new target to queue" in message:
            motion.append((ts, src))

    motion.sort(key=lambda x: x[0])
    log(
        "buffer",
        f"{len(targeting)} targeting events, {len(motion)} queue events for {start_iso} -> {end_iso}",
    )

    # Build PickTargets.
    #
    # Argus 2.0: motion_control_node "Adding new target to queue" carries a
    # product_id that matches target_index in the targeting_node event.
    #
    # Argus 1.0: motion_control_node has no product_id.  The two events share
    # an identical camera-space position (targeting x/y/z == motion
    # camera_space_position), so we join on a rounded position key.
    #
    # Fallback: if there are no motion events at all, build from targeting
    # events directly (very old Argus 1.0 or incomplete data).
    queue: list[PickTarget] = []
    events: list[BufferEvent] = []

    if not motion and targeting:
        sorted_targeting = sorted(
            targeting.items(), key=lambda kv: (
                kv[1].get("@timestamp_ros") or kv[1].get("@timestamp") or ""
            )
        )
        for idx, src in sorted_targeting:
            ts_val = src.get("@timestamp_ros") or src.get("@timestamp")
            ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
            if not ts:
                continue
            ts = _ensure_utc(ts)
            queue.append(PickTarget(target_id=str(idx), added_at=ts, source_doc=src))
            events.append(BufferEvent(
                timestamp=ts, event_type="target_added",
                buffer_snapshot=list(queue), source_doc=src,
            ))
        return events

    # Decide join strategy from the first motion event that has / lacks product_id
    use_product_id = any(src.get("product_id") is not None for _, src in motion)

    if not use_product_id:
        # Argus 1.0: build position index from targeting events
        # Key: (x, y, z) rounded to 4 dp to absorb float noise
        pos_index: dict[tuple, dict] = {}
        for idx, src in targeting.items():
            x, y, z = src.get("x"), src.get("y"), src.get("z")
            if None not in (x, y, z):
                key = (round(float(x), 4), round(float(y), 4), round(float(z), 4))
                pos_index[key] = src

    for ts, mc_src in motion:
        if use_product_id:
            # Argus 2.0 join
            product_id = mc_src.get("product_id")
            targeting_src = {}
            if product_id is not None:
                try:
                    targeting_src = targeting.get(int(product_id), {})
                except (TypeError, ValueError):
                    pass
            tid = str(product_id) if product_id is not None else str(len(queue) + 1)
        else:
            # Argus 1.0 join by camera position
            csp = mc_src.get("camera_space_position")
            targeting_src = {}
            if csp and len(csp) >= 3:
                key = (round(float(csp[0]), 4), round(float(csp[1]), 4), round(float(csp[2]), 4))
                targeting_src = pos_index.get(key, {})
            tid = str(targeting_src.get("target_index", len(queue) + 1))

        # targeting_node fields first; motion_control fields win on conflict
        merged = {**targeting_src, **mc_src}
        queue.append(PickTarget(target_id=tid, added_at=ts, source_doc=merged))
        events.append(BufferEvent(
            timestamp=ts, event_type="target_added",
            buffer_snapshot=list(queue), source_doc=merged,
        ))

    return events


def get_cam_pos(src: dict) -> list[float] | None:
    """
    Extract [x, y, z] camera-space position from a merged target source document.

    Handles two formats:
    - top-level x/y/z floats  (targeting_node on newer systems)
    - camera_space_position: [x, y, z] array  (motion_control_node / older systems)
    """
    x = src.get("x")
    y = src.get("y")
    z = src.get("z")
    if x is not None and y is not None and z is not None:
        try:
            return [float(x), float(y), float(z)]
        except (TypeError, ValueError):
            pass

    csp = src.get("camera_space_position")
    if isinstance(csp, (list, tuple)) and len(csp) >= 3:
        try:
            return [float(csp[0]), float(csp[1]), float(csp[2])]
        except (TypeError, ValueError):
            pass

    return None


def buffer_state_at(events: list[BufferEvent], dt: datetime) -> tuple[list[PickTarget], BufferEvent | None]:
    """Return the buffer snapshot and the triggering event at or before `dt`."""
    if not events:
        return [], None
    dt = _ensure_utc(dt)
    idx = bisect_right(events, dt, key=lambda e: e.timestamp) - 1
    if idx < 0:
        return [], None
    ev = events[idx]
    return list(ev.buffer_snapshot), ev
