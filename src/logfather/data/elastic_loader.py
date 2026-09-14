from __future__ import annotations

import re
import os
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterable, List

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from logfather.core.timeline_model import (
    TimelineItem,
    parse_time_from_name,
    inferred_live_clip_end,
    local_day_start_utc,
    local_day_end_utc,
)
from logfather.data.settings_store import Settings, Condition
from logfather.data.elastic_errors import ElasticFetchError
from logfather.core.sku_timeline import build_sku_bands
from logfather.data.day_listing_cache import load_day_files_cached
from logfather.data.elastic_client import (
    PageOutcome,
    api_headers,
    get_thread_session as _get_thread_session,
    msearch_first_pages,
    msearch_url as _msearch_url,
    paginate,
    search_url as _search_url,
)
from logfather.data.elastic_schema import (
    TRANSITION_STATES,
    extract_hit_robot_id as _extract_hit_robot_id,
    extract_service_name as _extract_service_name,
    extract_ui_selection as _extract_ui_selection,
    identity_filter,
    is_automatic_state as _is_automatic_state,
    is_manual_state as _is_manual_state,
    is_stop_like_event as _is_stop_like_event,
    robot_id_from_folder,
)

# Defaults (can be overridden in settings dialog). Index must be provided by user if default is incorrect.
KIBANA_BASE_DEFAULT = "https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243"
DISCOVER_INDEX_ID_DEFAULT = None
ELASTIC_INDEX_PATTERN = "logstash-*,pikpak,pikpak-*"
ELASTIC_TIMESTAMP_FIELDS = ["@timestamp_ros", "@timestamp"]
SYSTEM_ID_OVERRIDE: str | None = None
ELASTIC_EVENT_MAX_WORKERS = 4
# Sentinel: distinguishes "caller didn't supply last_video_end" (fall back
# to scanning the share) from an explicit None ("no videos that day").
_LAST_VIDEO_END_UNSET: object = object()
ELASTIC_EVENT_MAX_PAGES = 20
# Part of the events-cache digest; bump on any change to TimelineItem
# (de)serialization or the SKU/event extraction logic.
# 3: condition clause changed (state-change documents count only on their own
# state_name, 2026-09-11); cached days still held the doubled counts.
EVENTS_CACHE_SCHEMA_VERSION = 3

ELASTIC_EVENT_PAGE_SIZE = 1500
ELASTIC_EVENT_MIN_PAGE_SIZE = 300
ELASTIC_EVENT_TIMEOUT_SEC = 12
ELASTIC_TIMING_LOGS = True
FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS = 30



def set_system_id_override(system_id: str | None) -> None:
    global SYSTEM_ID_OVERRIDE
    SYSTEM_ID_OVERRIDE = system_id or None


def _normalize_index_id(_index_id: str | None) -> str | None:
    return ELASTIC_INDEX_PATTERN


def _default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache"
    return Path.home() / ".videolog_cache"




def _events_cache_path_for_robot(
    settings: Settings,
    robot_id: str,
    day,
    pikpak_root: Path | None = None,
) -> Path | None:
    day_key = f"{day:%Y%m%d}"
    index_id = _normalize_index_id(None) or ""
    conds = [
        {"name": c.name or "", "query": c.query or "", "color": c.color or ""}
        for c in settings.conditions
        if c.query
    ]
    pikpak_key = ""
    if pikpak_root is not None:
        try:
            pikpak_key = str(pikpak_root.resolve()).lower()
        except Exception:
            pikpak_key = str(pikpak_root).lower()
    payload = {
        # Bump EVENTS_CACHE_SCHEMA_VERSION whenever the serialized item shape
        # or extraction logic changes: past-day caches never expire, so
        # without this a code change is invisible to every existing install.
        "schema_version": EVENTS_CACHE_SCHEMA_VERSION,
        "robot_id": robot_id,
        "day": day_key,
        "pikpak_root": pikpak_key,
        "elastic_url": settings.elastic_url or "",
        "elastic_index": index_id,
        "elastic_ts_field": settings.elastic_timestamp_field or "",
        "conditions": conds,
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    cache_root = _default_cache_root() / "elastic_events"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    filename = f"events_{robot_id}_{day_key}_{digest}.json"
    return cache_root / filename


def _events_cache_path(settings: Settings, pikpak_root: Path, day) -> Path | None:
    robot_id = _extract_robot_id(pikpak_root)
    if not robot_id:
        return None
    return _events_cache_path_for_robot(settings, robot_id, day, pikpak_root=pikpak_root)


def _extract_robot_id(pikpak_root: Path) -> str | None:
    """PikPak folder -> robot id (canonical rule lives in elastic_schema)."""
    return robot_id_from_folder(pikpak_root.name)


def _get_robot_id(pikpak_root: Path | None) -> str | None:
    if SYSTEM_ID_OVERRIDE:
        return SYSTEM_ID_OVERRIDE
    if pikpak_root is None:
        return None
    return _extract_robot_id(pikpak_root)


def _iso_range_for_day(day: datetime.date) -> tuple[str, str]:
    start = local_day_start_utc(day)
    end = local_day_end_utc(day)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime | None:
    try:
        val = value.replace("Z", "+00:00")
        return datetime.fromisoformat(val)
    except Exception:
        return None


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_robot_filters(robot_id: str) -> dict:
    return identity_filter([robot_id])


def _build_multi_robot_filters(robot_ids: list[str]) -> dict:
    return identity_filter(robot_ids)


def _build_query(
    cond: Condition,
    robot_id: str,
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    size: int = 2000,
    search_after: list[str] | None = None,
) -> dict:
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )

    body = {
        "size": size,
        "track_total_hits": False,
        "_source": [
            "@timestamp_ros",
            "message",
            "state_name",
            "source",
            "leap_robot_id",
            "system_id",
            "data_collection.sku_name",
            "data_collection",
            "sku.name",
            "sku.tray",
            "sku.tool",
            "sku",
            "json_request.params",
        ],
        "sort": [{"@timestamp_ros": {"order": "asc", "format": "strict_date_optional_time"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_robot_filters(robot_id),
                    {"bool": {"should": ts_should, "minimum_should_match": 1}},
                ],
                "must": [condition_clause(cond.query)],
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def condition_clause(query: str) -> dict:
    """The Elastic clause for one timeline condition. A free-text query is
    matched across every field, but a state-change document only counts
    when its own state_name matches: the state that follows an error
    carries the error in previous_state_name and active_package_states,
    which doubled every crate change error and operator stop (Chris,
    2026-09-11: PikPak 010 on 22 August showed 12 for 6 events)."""
    free_text = {
        "query_string": {
            "query": query,
            "default_field": "*",
            "default_operator": "AND",
            "analyze_wildcard": True,
            "lenient": True,
        }
    }
    own_state = {
        "query_string": {
            "query": query,
            "fields": ["state_name", "state_name.keyword"],
            "default_operator": "AND",
            "analyze_wildcard": True,
            "lenient": True,
        }
    }
    return {
        "bool": {
            "must": [free_text],
            "must_not": [
                {
                    "bool": {
                        # match_phrase, not a keyword term: older indices carry no message.keyword
                        "filter": [{"bool": {"should": [{"match_phrase": {"message": "New system state"}}, {"match_phrase": {"message": "New node state"}}], "minimum_should_match": 1}}],
                        "must_not": [own_state],
                    }
                }
            ],
        }
    }


def _build_overview_query(
    robot_ids: list[str],
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    sort_field: str,
    size: int = 3000,
    search_after: list[str] | None = None,
) -> dict:
    transition_states = TRANSITION_STATES
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )

    body = {
        "size": size,
        "track_total_hits": False,
        "_source": [
            "@timestamp_ros",
            "@timestamp",
            "message",
            "state_name",
            "source",
            "leap_robot_id",
            "system_id",
            "data_collection.sku_name",
            "data_collection",
            "sku.name",
            "sku.tray",
            "sku.tool",
            "sku",
            "json_request.service_name",
            "json_request",
            "json_request.params",
        ],
        "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_multi_robot_filters(robot_ids),
                    {
                        "bool": {
                            "should": ts_should,
                            "minimum_should_match": 1,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"terms": {"state_name.keyword": transition_states}},
                                {"terms": {"state_name": transition_states}},
                                {
                                    "bool": {
                                        "filter": [
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"term": {"source.keyword": "/leap/manip1/ui_node"}},
                                                        {"term": {"source.keyword": "/leap/manip1/behaviour_node"}},
                                                        {"term": {"source": "/leap/manip1/ui_node"}},
                                                        {"term": {"source": "/leap/manip1/behaviour_node"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"exists": {"field": "data_collection.user_selection"}},
                                                        {"exists": {"field": "data_collection.sku_name"}},
                                                        {"exists": {"field": "sku.name"}},
                                                        {"exists": {"field": "json_request.params"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                        ]
                                    }
                                },
                                {"term": {"message.keyword": "Shutting down system"}},
                                {"term": {"message": "Shutting down system"}},
                                {"match_phrase": {"message": "Shutting down system"}},
                                {"term": {"json_request.service_name.keyword": "system_shutdown"}},
                                {"term": {"json_request.service_name": "system_shutdown"}},
                                {"match_phrase": {"json_request.service_name": "system_shutdown"}},
                            ],
                            "minimum_should_match": 1,
                        },
                    },
                ]
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def fetch_overview_event_chunks(
    settings: Settings,
    system_roots: list[Path],
    start_dt: datetime,
    end_dt: datetime,
    chunk_minutes: int = 10,
) -> Iterable[dict[str, list[dict]]]:
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key or not index_id:
        return []

    robot_to_root: dict[str, Path] = {}
    for root in system_roots:
        robot_id = _extract_robot_id(root)
        if robot_id:
            robot_to_root[robot_id] = root
    if not robot_to_root:
        return []

    start_dt = _ensure_utc(start_dt)
    end_dt = _ensure_utc(end_dt)
    if end_dt <= start_dt:
        return []
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    sort_field = ts_fields[0] if ts_fields else "@timestamp"
    headers = api_headers(api_key)
    search_endpoint = _search_url(url, index_id)
    session = _get_thread_session()
    chunk_delta = timedelta(minutes=max(1, int(chunk_minutes)))
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(end_dt, chunk_start + chunk_delta)
        start_iso = chunk_start.isoformat().replace("+00:00", "Z")
        end_iso = chunk_end.isoformat().replace("+00:00", "Z")
        grouped: dict[str, list[dict]] = {robot_id: [] for robot_id in robot_to_root}
        try:
            outcome = paginate(
                lambda size, search_after: _build_overview_query(
                    list(robot_to_root.keys()),
                    start_iso,
                    end_iso,
                    ts_fields,
                    sort_field,
                    size=size,
                    search_after=search_after,
                ),
                session=session,
                endpoint=search_endpoint,
                headers=headers,
                page_size=3000,
                max_pages=60,
                timeout_sec=15,
                label="overview query",
            )
        except ElasticFetchError as exc:
            raise ElasticFetchError(str(exc), []) from exc
        for hit in outcome.hits:
            src = hit.get("_source", {})
            if not isinstance(src, dict):
                continue
            robot_id = _extract_hit_robot_id(src)
            if not robot_id or robot_id not in grouped:
                continue
            ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
            ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
            if not ts:
                continue
            grouped[robot_id].append(
                {
                    "ts": _ensure_utc(ts),
                    "state_name": str(src.get("state_name") or "").strip(),
                    "message": str(src.get("message") or "").strip(),
                    "service_name": _extract_service_name(src),
                    "source": str(src.get("source") or "").strip(),
                    "selection": _extract_ui_selection(src),
                }
            )

        for items in grouped.values():
            items.sort(key=lambda item: item.get("ts") or datetime.min.replace(tzinfo=timezone.utc))
        yield grouped
        chunk_start = chunk_end




def _serialize_timeline_item(item: TimelineItem) -> dict:
    return {
        "start": _ensure_utc(item.start).isoformat(),
        "end": _ensure_utc(item.end).isoformat(),
        "label": item.label,
        "kind": item.kind,
        "color": str(item.color),
        "payload": item.payload,
        "track_label": item.track_label,
        "cached": bool(item.cached),
        "annotated": bool(item.annotated),
        "path_key": item.path_key,
    }


def _deserialize_timeline_item(data: dict) -> TimelineItem | None:
    try:
        start = _parse_ts(str(data.get("start", "")))
        end = _parse_ts(str(data.get("end", "")))
        if not start or not end:
            return None
        return TimelineItem(
            start=_ensure_utc(start),
            end=_ensure_utc(end),
            label=str(data.get("label", "")),
            kind=str(data.get("kind", "")),
            color=data.get("color", "#fa8c16"),
            payload=data.get("payload"),
            track_label=data.get("track_label"),
            cached=bool(data.get("cached", False)),
            annotated=bool(data.get("annotated", False)),
            path_key=data.get("path_key"),
        )
    except Exception:
        return None


def _is_past_day(day) -> bool:
    # `day` is a LOCAL calendar day (see replay_timeline.local_day_start_utc), so
    # it must be compared to the local date. Comparing against the UTC date
    # classified the in-progress local day as "past" (making its cache
    # permanent mid-afternoon) for any user west of UTC.
    return day < datetime.now().date()


def _events_cache_is_fresh(cache_path: Path, day) -> bool:
    # Past days are effectively immutable for timeline purposes.
    if _is_past_day(day):
        return True
    try:
        age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - cache_path.stat().st_mtime)
    except Exception:
        return False
    return age_seconds <= 120


def _load_events_cache(
    cache_path: Path | None, day
) -> tuple[list[TimelineItem], bool] | None:
    """Returns (items, sku_complete) on a cache hit, else None. sku_complete
    means the SKU/manual items were fully merged when this cache was saved,
    so a past day needs no live SKU refresh."""
    if cache_path is None or not cache_path.exists():
        return None
    if not _events_cache_is_fresh(cache_path, day):
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None
    items: list[TimelineItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _deserialize_timeline_item(raw)
        if item is not None:
            items.append(item)
    return items, bool(data.get("sku_complete"))


def _save_events_cache(
    cache_path: Path | None, items: list[TimelineItem], sku_complete: bool = False
) -> None:
    if cache_path is None:
        return
    try:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sku_complete": bool(sku_complete),
            "items": [_serialize_timeline_item(i) for i in items],
        }
        cache_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass


def _merge_sku_items(base_items: list[TimelineItem], sku_items: list[TimelineItem]) -> list[TimelineItem]:
    merged = [itm for itm in base_items if itm.kind != "sku"]
    if sku_items:
        merged.extend(sku_items)
    merged.sort(key=lambda it: it.start)
    return merged


def _perf_log(message: str) -> None:
    if ELASTIC_TIMING_LOGS:
        print(f"[elastic-perf] {message}", flush=True)


def _build_sku_query(
    robot_id: str,
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    sort_field: str,
    size: int = 2000,
    search_after: list[str] | None = None,
) -> dict:
    transition_states = TRANSITION_STATES
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    body = {
        "size": size,
        "track_total_hits": False,
        "_source": [
            "@timestamp_ros",
            "@timestamp",
            "message",
            "state_name",
            "source",
            "leap_robot_id",
            "system_id",
            "data_collection.sku_name",
            "data_collection",
            "sku.name",
            "sku.tray",
            "sku.tool",
            "sku",
            "json_request.service_name",
            "json_request",
            "json_request.params",
        ],
        "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_robot_filters(robot_id),
                    {
                        "bool": {
                            "should": ts_should,
                            "minimum_should_match": 1,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"terms": {"state_name.keyword": transition_states}},
                                {"terms": {"state_name": transition_states}},
                                {
                                    "bool": {
                                        "filter": [
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"term": {"source.keyword": "/leap/manip1/ui_node"}},
                                                        {"term": {"source.keyword": "/leap/manip1/behaviour_node"}},
                                                        {"term": {"source": "/leap/manip1/ui_node"}},
                                                        {"term": {"source": "/leap/manip1/behaviour_node"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"exists": {"field": "data_collection.user_selection"}},
                                                        {"exists": {"field": "data_collection.sku_name"}},
                                                        {"exists": {"field": "sku.name"}},
                                                        {"exists": {"field": "json_request.params"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                        ]
                                    }
                                },
                                {"term": {"message.keyword": "Shutting down system"}},
                                {"term": {"message": "Shutting down system"}},
                                {"match_phrase": {"message": "Shutting down system"}},
                                {"term": {"json_request.service_name.keyword": "system_shutdown"}},
                                {"term": {"json_request.service_name": "system_shutdown"}},
                                {"match_phrase": {"json_request.service_name": "system_shutdown"}},
                            ],
                            "minimum_should_match": 1,
                        },
                    },
                ]
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def fetch_events(
    settings: Settings,
    pikpak_root: Path | None,
    day,
    last_video_end: object = _LAST_VIDEO_END_UNSET,
) -> Iterable[TimelineItem]:
    """last_video_end: the inferred end of the day's last clip, when the
    caller (the timeline loader) has already scanned the share for it —
    re-listing a day folder on the WAN share costs ~5s. May also be a
    zero-arg callable (resolver) that fetch_sku_items calls only at its
    final band-capping step, letting the queries overlap the scan."""
    t_fetch_start = perf_counter()
    if not day or (pikpak_root is None and SYSTEM_ID_OVERRIDE is None):
        print("[elastic] No PikPak or day selected; skipping event fetch.")
        return []
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key:
        print("[elastic] Missing URL or API key; skipping event fetch.")
        return []
    if not index_id:
        print("[elastic] Missing index/pattern; set it in Settings.")
        return []


    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []
    cache_path = _events_cache_path_for_robot(settings, robot_id, day, pikpak_root=pikpak_root)
    t_cache_read_start = perf_counter()
    cached = _load_events_cache(cache_path, day)
    _perf_log(f"cache read took {(perf_counter() - t_cache_read_start) * 1000:.0f}ms")
    if cached is not None:
        cached_items, sku_complete = cached
        print(f"[elastic] Using cached events ({len(cached_items)} items).")
        if sku_complete and _is_past_day(day):
            # Past days are immutable and this cache already holds the SKU
            # merge — no reason to re-query Elastic (~4s saved per load).
            _perf_log(
                f"fetch_events total (cache hit, sku complete): "
                f"{(perf_counter() - t_fetch_start) * 1000:.0f}ms"
            )
            return cached_items
        t_sku_refresh = perf_counter()
        sku_ok = True
        try:
            sku_items = list(
                fetch_sku_items(settings, pikpak_root, day, last_video_end=last_video_end)
            )
        except ElasticFetchError as exc:
            sku_ok = False
            sku_items = list(exc.items) if exc.items else []
        except Exception:
            sku_ok = False
            sku_items = []
        merged_items = _merge_sku_items(list(cached_items), sku_items)
        if sku_items or (sku_ok and _is_past_day(day)):
            _save_events_cache(
                cache_path, merged_items, sku_complete=sku_ok and _is_past_day(day)
            )
        _perf_log(f"cache-hit SKU refresh took {(perf_counter() - t_sku_refresh) * 1000:.0f}ms")
        _perf_log(f"fetch_events total (cache hit): {(perf_counter() - t_fetch_start) * 1000:.0f}ms")
        return merged_items

    start_iso, end_iso = _iso_range_for_day(day)
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    headers = api_headers(api_key)

    items: List[TimelineItem] = []
    any_condition = False
    search_endpoint = _search_url(url, index_id)
    active_conditions = [(idx, cond) for idx, cond in enumerate(settings.conditions) if cond.query]
    _perf_log(f"active conditions: {len(active_conditions)} / {len(settings.conditions)}")

    # Fast path: one _msearch fetches page 1 of every condition in a single
    # round trip (per-request latency dominates: ~0.3-0.5s each even for
    # zero hits). A condition whose first page came back full may have more
    # pages, so it falls through to paginate() below; so does everything on
    # any msearch failure. Typical day: 1 HTTP request instead of 12.
    first_pages: dict[int, list[dict]] = {}
    if active_conditions:
        t_msearch = perf_counter()
        bodies = [
            _build_query(
                cond,
                robot_id,
                start_iso,
                end_iso,
                ts_fields,
                size=ELASTIC_EVENT_PAGE_SIZE,
                search_after=None,
            )
            for _idx, cond in active_conditions
        ]
        msearch_results = msearch_first_pages(
            bodies,
            session=_get_thread_session(),
            endpoint=_msearch_url(url, index_id),
            headers=headers,
            timeout_sec=ELASTIC_EVENT_TIMEOUT_SEC,
            label="conditions msearch",
        )
        if msearch_results is not None:
            for (idx, _cond), hits in zip(active_conditions, msearch_results):
                if hits is None:
                    continue
                if len(hits) >= ELASTIC_EVENT_PAGE_SIZE and hits[-1].get("sort"):
                    continue  # may have further pages: refetch via paginate()
                first_pages[idx] = hits
        _perf_log(
            f"conditions msearch: queries={len(bodies)} satisfied={len(first_pages)} "
            f"time={(perf_counter() - t_msearch) * 1000:.0f}ms"
        )

    def _condition_items_from_outcome(
        idx: int, cond: Condition, outcome: PageOutcome, t_cond_start: float
    ) -> tuple[List[TimelineItem], str | None]:
        warning_msg: str | None = outcome.warning
        hits_collected: List[TimelineItem] = []
        for hit in outcome.hits:
            src = hit.get("_source", {})
            ts_val = src.get("@timestamp_ros") or src.get("@timestamp")
            ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
            if not ts:
                continue
            selection = _extract_ui_selection(src)
            if selection:
                hit["_ui_sku"] = selection.get("sku")
                hit["_ui_tray"] = selection.get("tray")
                hit["_ui_tool"] = selection.get("tool")
            label = src.get("message") or cond.name or cond.query
            hits_collected.append(
                TimelineItem(
                    start=ts,
                    end=ts + timedelta(seconds=1),
                    label=label,
                    kind=f"cond_{idx}",
                    color=cond.color or "#fa8c16",
                    payload=hit,
                    track_label=cond.name or f"Cond {idx+1}",
                )
            )
        if outcome.truncated and warning_msg is None:
            # Surface truncation as a warning so the day is neither cached
            # nor silently presented as complete.
            warning_msg = (
                f"[elastic] condition {idx+1} ({cond.name or cond.query}) truncated "
                f"at {ELASTIC_EVENT_MAX_PAGES} pages ({len(hits_collected)} hits); day view incomplete"
            )
            print(warning_msg)
        print(f"[elastic] condition {idx+1} ({cond.name or cond.query}) collected {len(hits_collected)} hits")
        _perf_log(
            f"condition {idx+1} requests={outcome.requests_made} hits={len(hits_collected)} "
            f"time={(perf_counter() - t_cond_start) * 1000:.0f}ms"
        )
        return hits_collected, warning_msg

    def fetch_for_condition(idx: int, cond: Condition) -> tuple[List[TimelineItem], str | None]:
        t_cond_start = perf_counter()
        first_page = first_pages.get(idx)
        if first_page is not None:
            outcome = PageOutcome(hits=list(first_page), pages=1, requests_made=0)
        else:
            outcome = paginate(
                lambda size, search_after: _build_query(
                    cond,
                    robot_id,
                    start_iso,
                    end_iso,
                    ts_fields,
                    size=size,
                    search_after=search_after,
                ),
                session=_get_thread_session(),
                endpoint=search_endpoint,
                headers=headers,
                page_size=ELASTIC_EVENT_PAGE_SIZE,
                min_page_size=ELASTIC_EVENT_MIN_PAGE_SIZE,
                max_pages=ELASTIC_EVENT_MAX_PAGES,
                timeout_sec=ELASTIC_EVENT_TIMEOUT_SEC,
                label=f"condition {idx+1}",
                on_error="warn",
            )
        return _condition_items_from_outcome(idx, cond, outcome, t_cond_start)

    warnings: list[str] = []
    tasks = []
    with ThreadPoolExecutor(max_workers=max(1, min(ELASTIC_EVENT_MAX_WORKERS, len(active_conditions)))) as executor:
        for idx, cond in active_conditions:
            any_condition = True
            tasks.append(executor.submit(fetch_for_condition, idx, cond))
        for future in as_completed(tasks):
            cond_items, warning = future.result()
            if cond_items:
                items.extend(cond_items)
            if warning:
                warnings.append(warning)

    if not any_condition:
        print("[elastic] No condition queries configured; nothing to fetch.")
    if not items:
        print("[elastic] No events returned for selected day/robot.")
    t_sku_start = perf_counter()
    sku_ok = True
    try:
        sku_items = list(
            fetch_sku_items(settings, pikpak_root, day, last_video_end=last_video_end)
        )
    except ElasticFetchError as exc:
        warnings.append(str(exc))
        sku_ok = False
        sku_items = list(exc.items) if exc.items else []
    except Exception as exc:
        warnings.append(str(exc))
        sku_ok = False
        sku_items = []
    _perf_log(f"sku fetch took {(perf_counter() - t_sku_start) * 1000:.0f}ms")
    items = _merge_sku_items(items, sku_items)
    t_cache_write_start = perf_counter()
    if warnings:
        # A partly-failed day must not be cached: past-day caches never
        # expire, so persisting here would serve the truncated timeline
        # forever. Skipping the save means the next load retries in full.
        print(
            f"[elastic] day fetch had {len(warnings)} warning(s); not caching",
            flush=True,
        )
    else:
        _save_events_cache(cache_path, items, sku_complete=sku_ok and _is_past_day(day))
    _perf_log(f"cache write took {(perf_counter() - t_cache_write_start) * 1000:.0f}ms")
    _perf_log(f"fetch_events total: {(perf_counter() - t_fetch_start) * 1000:.0f}ms (items={len(items)})")
    if warnings:
        raise ElasticFetchError("\n".join(warnings), items)
    return items


def _last_video_end(pikpak_root: Path, day) -> datetime | None:
    try:
        paths = list(load_day_files_cached(pikpak_root, day))
    except Exception:
        return None
    entries: list[tuple[Path, datetime]] = []
    for p in paths:
        parsed_dt = parse_time_from_name(p)
        if parsed_dt is not None:
            start_dt = parsed_dt
        else:
            try:
                stat = p.stat()
            except FileNotFoundError:
                continue
            start_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        entries.append((p, _ensure_utc(start_dt)))
    if not entries:
        return None
    entries.sort(key=lambda tpl: tpl[1])
    last_path, last_start = entries[-1]
    return _ensure_utc(inferred_live_clip_end(last_path, last_start))


def fetch_sku_items(
    settings: Settings,
    pikpak_root: Path | None,
    day,
    last_video_end: object = _LAST_VIDEO_END_UNSET,
) -> Iterable[TimelineItem]:
    print("[sku-debug] fetch_sku_items start", flush=True)
    if not day or (pikpak_root is None and SYSTEM_ID_OVERRIDE is None):
        print("[elastic] No PikPak or day selected; skipping SKU fetch.")
        return []
    if last_video_end is _LAST_VIDEO_END_UNSET:
        last_video_end = _last_video_end(pikpak_root, day)
    day_start = local_day_start_utc(day)
    day_end = local_day_end_utc(day) + timedelta(milliseconds=1)
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key:
        print("[elastic] Missing URL or API key; skipping SKU fetch.")
        return []
    if not index_id:
        print("[elastic] Missing index/pattern; set it in Settings.")
        return []

    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []

    start_iso, end_iso = _iso_range_for_day(day)
    headers = api_headers(api_key)
    search_endpoint = _search_url(url, index_id)
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    sort_field = ts_fields[0] if ts_fields else "@timestamp"
    session = _get_thread_session()
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )

    events: list[tuple[datetime, str, dict | None, str]] = []
    total_hits = 0
    start_hits = 0
    try:
        outcome = paginate(
            lambda size, search_after: _build_sku_query(
                robot_id,
                start_iso,
                end_iso,
                ts_fields,
                sort_field,
                size=size,
                search_after=search_after,
            ),
            session=session,
            endpoint=search_endpoint,
            headers=headers,
            page_size=2000,
            max_pages=20,
            timeout_sec=8,
            label="SKU query",
        )
    except ElasticFetchError as exc:
        # Callers expect exc.items to hold TimelineItems, not raw hits.
        raise ElasticFetchError(str(exc), []) from exc
    for hit in outcome.hits:
        total_hits += 1
        src = hit.get("_source", {})
        ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
        ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
        if not ts:
            continue
        ts = _ensure_utc(ts)
        state_name = str(src.get("state_name") or "").strip()
        message = str(src.get("message") or "")
        service_name = _extract_service_name(src)
        if state_name == "start_pnp":
            start_hits += 1
            sku_dbg = src.get("data_collection") or src.get("sku") or {}
            print(
                f"[sku-debug] start_pnp {ts.isoformat()} system_id={src.get('system_id')} "
                f"sku={sku_dbg}"
            )
        if _is_manual_state(state_name):
            events.append((ts, "manual", None, state_name))
        if _is_automatic_state(state_name):
            events.append((ts, "auto", None, state_name))
        if _is_stop_like_event(state_name, message, service_name):
            events.append((ts, "stop", None, state_name))
        selection = _extract_ui_selection(src)
        if state_name == "start_pnp":
            events.append((ts, "start", selection, state_name))
        elif selection:
            events.append((ts, "select", selection, state_name))
    if outcome.truncated:
        print("[sku-debug] reached page cap; results may be truncated", flush=True)

    if not events:
        if total_hits:
            print(f"[sku-debug] hits={total_hits} start_pnp={start_hits} (no SKU events created)", flush=True)

    manual_event_count = sum(1 for _ts, kind, _data, _state in events if kind == "manual")
    if manual_event_count == 0:
        # Defensive fallback: some mappings/indexes can miss manual states in the broader SKU query.
        manual_fallback_body = {
            "size": 5000,
            "track_total_hits": False,
            "_source": [
                "@timestamp_ros",
                "@timestamp",
                "state_name",
                "source",
                "system_id",
            ],
            "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
            "query": {
                "bool": {
                    "filter": [
                        _build_robot_filters(robot_id),
                        {"bool": {"should": ts_should, "minimum_should_match": 1}},
                        {
                            "bool": {
                                "should": [
                                    {"term": {"state_name.keyword": "controller_node_manual_mode"}},
                                    {"term": {"state_name": "controller_node_manual_mode"}},
                                    {"match_phrase": {"state_name": "controller_node_manual_mode"}},
                                    {"term": {"state_name.keyword": "controller_node_automatic_mode"}},
                                    {"term": {"state_name": "controller_node_automatic_mode"}},
                                    {"match_phrase": {"state_name": "controller_node_automatic_mode"}},
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            },
        }
        try:
            resp = session.post(search_endpoint, json=manual_fallback_body, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits:
                src = hit.get("_source", {})
                state_name = str(src.get("state_name") or "").strip()
                ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
                ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
                if not ts:
                    continue
                ts = _ensure_utc(ts)
                if _is_manual_state(state_name):
                    events.append((ts, "manual", None, state_name))
                elif _is_automatic_state(state_name):
                    events.append((ts, "auto", None, state_name))
            if hits:
                print(f"[sku-debug] manual fallback hits={len(hits)}", flush=True)
        except Exception as exc:
            print(f"[sku-debug] manual fallback query failed: {exc}", flush=True)

    if start_hits <= 1:
        # Fallback: explicitly fetch start_pnp entries (Argus 2 nodes emit these on behaviour_node).
        fallback_body = {
            "size": 2000,
            "_source": [
                "@timestamp_ros",
                "@timestamp",
                "state_name",
                "system_id",
                "data_collection.sku_name",
                "data_collection",
                "sku.name",
                "sku.tray",
                "sku.tool",
                "sku",
            ],
            "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
            "query": {
                "bool": {
                    "filter": [
                        _build_robot_filters(robot_id),
                        {"term": {"state_name": "start_pnp"}},
                        {"bool": {"should": ts_should, "minimum_should_match": 1}},
                    ]
                }
            },
        }
        try:
            resp = session.post(search_endpoint, json=fallback_body, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            if hits:
                print(f"[sku-debug] fallback start_pnp hits={len(hits)}", flush=True)
            for hit in hits:
                src = hit.get("_source", {})
                ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
                ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
                if not ts:
                    continue
                ts = _ensure_utc(ts)
                selection = _extract_ui_selection(src)
                events.append((ts, "start", selection, "start_pnp"))
        except Exception as exc:
            print(f"[sku-debug] fallback start_pnp query failed: {exc}", flush=True)

    if callable(last_video_end):
        # A resolver from the timeline loader: blocks until the concurrent
        # share scan publishes the value. Resolved here — after all the
        # Elastic queries — so they overlap the scan.
        last_video_end = last_video_end()
    cap_end = _ensure_utc(last_video_end) if last_video_end else day_end
    return build_sku_bands(events, cap_end)


def actuator_detail(src: dict) -> str:
    """The detail the actuator controller attaches to a fault or warning:
    "servo 5: Current limit exceeded :: Current over limit" for a Fault on
    motor, the update_info text for a Warning update, else ""."""
    fail = str(src.get("fail_type", "") or "").strip()
    servo = str(src.get("servo_id", "") or "").strip()
    info = str(src.get("update_info", "") or "").strip()
    bits = []
    if servo and (fail or info):
        bits.append(f"servo {servo}")
    if fail:
        bits.append(fail)
    if info:
        bits.append(info)
    return ": ".join(bits[:1] + [", ".join(bits[1:])]) if len(bits) > 1 else (bits[0] if bits else "")


def _fetch_logs_range_raw(
    settings: Settings,
    pikpak_root: Path | None,
    start_dt: datetime,
    end_dt: datetime,
    max_hits: int = 50000,
) -> list[tuple[datetime, str, str, str, str]]:
    if not pikpak_root and not SYSTEM_ID_OVERRIDE:
        print("[elastic] No PikPak root provided for log fetch.")
        return []
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key or not index_id:
        print("[elastic] Missing URL/API key/index; cannot fetch logs.")
        return []
    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []

    start_iso = _ensure_utc(start_dt).isoformat().replace("+00:00", "Z")
    end_iso = _ensure_utc(end_dt).isoformat().replace("+00:00", "Z")
    headers = api_headers(api_key)
    search_endpoint = _search_url(url, index_id)
    ts_field = ELASTIC_TIMESTAMP_FIELDS[0]
    session = _get_thread_session()

    def build_body(size: int, search_after: list | None) -> dict:
        body = {
            "size": size,
            "track_total_hits": False,
            "_source": [
                "@timestamp_ros",
                "@timestamp",
                "message",
                "state_name",
                "source",
                "leap_robot_id",
                "system_id",
                "json_request.params",
                # The detail behind actuator faults and warnings (Chris,
                # 2026-09-10): "Fault on motor" alone said nothing.
                "fail_type",
                "servo_id",
                "update_info",
                "error_code",
            ],
            "sort": [{ts_field: {"order": "asc", "format": "strict_date_optional_time"}}],
            "query": {
                "bool": {
                    "filter": [
                        _build_robot_filters(robot_id),
                        {
                            "range": {
                                ts_field: {
                                    "gte": start_iso,
                                    "lte": end_iso,
                                    "format": "strict_date_optional_time",
                                }
                            }
                        },
                    ]
                }
            },
        }
        if search_after:
            body["search_after"] = search_after
        return body

    def hits_to_rows(hits: list[dict]) -> list[tuple[datetime, str, str, str, str]]:
        rows: list[tuple[datetime, str, str, str, str]] = []
        for hit in hits:
            src = hit.get("_source", {})
            ts_val = src.get("@timestamp_ros") or src.get(ts_field) or src.get("@timestamp")
            ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
            if not ts:
                continue
            source_key = str(src.get("source", "") or "").strip()
            message_key = str(src.get("message", "") or "").strip()
            state_val = str(src.get("state_name", "") or "").strip()
            detail = actuator_detail(src)
            if detail:
                message_key = f"{message_key} | {detail}" if message_key else detail
            parts = [p for p in [source_key, state_val, message_key] if p]
            text = " | ".join(parts) if parts else message_key or state_val or source_key or "(event)"
            rows.append((ts, text, source_key, state_val, message_key))
        return rows

    page_size = 2000
    try:
        outcome = paginate(
            build_body,
            session=session,
            endpoint=search_endpoint,
            headers=headers,
            page_size=page_size,
            max_pages=max(1, -(-max_hits // page_size)),
            timeout_sec=6,
            max_hits=max_hits,
            label="log range query",
        )
    except ElasticFetchError as exc:
        # Callers use exc.items as processed rows, not raw hits.
        raise ElasticFetchError(str(exc), hits_to_rows(exc.items)) from exc
    return hits_to_rows(outcome.hits)


def fetch_logs_for_range(
    settings: Settings,
    pikpak_root: Path | None,
    start_dt: datetime,
    end_dt: datetime,
    max_hits: int = 50000,
) -> list[tuple[datetime, str, str, str, str]]:
    return _fetch_logs_range_raw(settings, pikpak_root, start_dt, end_dt, max_hits=max_hits)


def fetch_fleetwide_search_histogram(
    settings: Settings,
    pikpak_root: Path,
    query: str | list[str],
    start_dt: datetime,
    end_dt: datetime,
    bucket_seconds: int,
) -> dict:
    """Return all/operating counts and histograms for one system/search pair."""
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    robot_id = _extract_robot_id(pikpak_root)
    if not url or not api_key or not index_id:
        raise ElasticFetchError("[elastic] Missing URL/API key/index for fleetwide search.", [])
    if not robot_id:
        raise ElasticFetchError(f"[elastic] Could not derive robot id from {pikpak_root.name}", [])
    raw_queries = query if isinstance(query, list) else [query]
    queries = [str(value or "").strip() for value in raw_queries if str(value or "").strip()]
    if not queries:
        raise ElasticFetchError("[elastic] Fleetwide search query is blank.", [])
    query_clauses = []
    for query_index, query_text in enumerate(queries):
        if query_text.startswith("{"):
            try:
                parsed_query = json.loads(query_text)
            except json.JSONDecodeError as exc:
                raise ElasticFetchError(f"[elastic] Invalid fleetwide JSON query: {exc}", []) from exc
            if not isinstance(parsed_query, dict) or not parsed_query:
                raise ElasticFetchError("[elastic] Fleetwide JSON query must be a non-empty object.", [])
            clause = parsed_query
        else:
            clause = {
                "query_string": {
                    "query": query_text,
                    "default_field": "*",
                    "default_operator": "AND",
                    "analyze_wildcard": True,
                    "lenient": True,
                }
            }
        query_clauses.append(
            {
                "bool": {
                    "must": [clause],
                    "_name": f"fleet_search_{query_index}",
                }
            }
        )
    if len(query_clauses) == 1:
        query_clause = query_clauses[0]
    else:
        query_clause = {
            "bool": {
                "should": query_clauses,
                "minimum_should_match": 1,
            }
        }

    start_dt = _ensure_utc(start_dt)
    end_dt = _ensure_utc(end_dt)
    if end_dt <= start_dt:
        return {
            "robot_id": robot_id,
            "total": 0,
            "operational_total": 0,
            "non_operational_total": 0,
            "buckets": [],
        }

    start_iso = start_dt.isoformat().replace("+00:00", "Z")
    end_iso = end_dt.isoformat().replace("+00:00", "Z")
    ts_field = settings.elastic_timestamp_field or ELASTIC_TIMESTAMP_FIELDS[0]
    bucket_seconds = max(1, int(bucket_seconds))
    headers = api_headers(api_key)
    session = _get_thread_session()
    search_endpoint = _search_url(url, index_id)

    def fetch_matching_hits(
        clause: dict,
        range_start: datetime,
        source_fields: list[str],
        *,
        endpoint: str = search_endpoint,
        robot_filter: dict | None = None,
        query_label: str = "search",
    ) -> list[dict]:
        range_start_iso = _ensure_utc(range_start).isoformat().replace("+00:00", "Z")

        def build_body(size: int, search_after: list | None) -> dict:
            body = {
                "size": size,
                "track_total_hits": False,
                "_source": source_fields,
                "sort": [{ts_field: {"order": "asc", "format": "strict_date_optional_time"}}],
                "query": {
                    "bool": {
                        "filter": [
                            robot_filter or _build_robot_filters(robot_id),
                            {
                                "range": {
                                    ts_field: {
                                        "gte": range_start_iso,
                                        "lte": end_iso,
                                        "format": "strict_date_optional_time",
                                    }
                                }
                            },
                        ],
                        "must": [clause],
                    }
                },
            }
            if search_after:
                body["search_after"] = search_after
            return body

        try:
            outcome = paginate(
                build_body,
                session=session,
                endpoint=endpoint,
                headers=headers,
                page_size=1500,
                min_page_size=300,
                max_pages=100,
                timeout_sec=15,
                label=f"fleetwide {query_label} for {robot_id}",
            )
        except ElasticFetchError as exc:
            raise ElasticFetchError(str(exc), []) from exc
        if outcome.truncated:
            raise ElasticFetchError(
                f"[elastic] Fleetwide {query_label} exceeded the pagination limit for {robot_id}.",
                [],
            )
        return outcome.hits

    error_hits = fetch_matching_hits(
        query_clause,
        start_dt,
        [ts_field, "@timestamp_ros", "@timestamp", "leap_robot_id", "system_id"],
        query_label="occurrence query",
    )
    error_records: list[tuple[datetime, tuple[str, ...]]] = []
    for hit in error_hits:
        source = hit.get("_source", {})
        timestamp = _parse_ts(str(source.get(ts_field) or source.get("@timestamp_ros") or source.get("@timestamp") or ""))
        if timestamp is not None:
            matched_queries = hit.get("matched_queries")
            if isinstance(matched_queries, list):
                match_keys = tuple(sorted(str(value) for value in matched_queries if str(value)))
            else:
                match_keys = ()
            error_records.append((_ensure_utc(timestamp), match_keys or ("fleet_search_all",)))
    error_records.sort(key=lambda item: item[0])
    raw_error_count = len(error_records)
    cooldown = timedelta(seconds=FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS)
    deduplicated_error_times: list[datetime] = []
    last_counted_by_search: dict[str, datetime] = {}
    for timestamp, match_keys in error_records:
        if all(
            key in last_counted_by_search and timestamp - last_counted_by_search[key] < cooldown
            for key in match_keys
        ):
            continue
        deduplicated_error_times.append(timestamp)
        for key in match_keys:
            last_counted_by_search[key] = timestamp
    error_times = deduplicated_error_times

    # Match Elasticsearch fixed_interval alignment: buckets are anchored to
    # epoch boundaries, not to the moving "now minus N days" query start.
    # Without this, a daily bucket started at (for example) 14:20 and morning
    # events appeared under the preceding date.
    bucket_origin_epoch = int(start_dt.timestamp() // bucket_seconds) * bucket_seconds
    bucket_origin = datetime.fromtimestamp(bucket_origin_epoch, tz=timezone.utc)
    bucket_span_seconds = max(1.0, (end_dt - bucket_origin).total_seconds())
    bucket_count = max(1, int((bucket_span_seconds + bucket_seconds - 1) // bucket_seconds))
    buckets = [
        {
            "timestamp": bucket_origin + timedelta(seconds=index * bucket_seconds),
            "end": bucket_origin + timedelta(seconds=(index + 1) * bucket_seconds),
            "count": 0,
            "operational_count": 0,
            "non_operational_count": 0,
        }
        for index in range(bucket_count)
    ]
    if not error_times:
        return {
            "robot_id": robot_id,
            "total": 0,
            "operational_total": 0,
            "non_operational_total": 0,
            "operation_grace_seconds": 60,
            "raw_total": raw_error_count,
            "suppressed_count": raw_error_count,
            "cooldown_seconds": FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS,
            "buckets": buckets,
        }

    error_indices = {str(hit.get("_index") or "") for hit in error_hits}
    uses_pikpak = any(name == "pikpak" or name.startswith("pikpak-") for name in error_indices)
    uses_logstash = any(name.startswith("logstash-") for name in error_indices)
    if uses_pikpak and not uses_logstash:
        transition_index_pattern = "pikpak,pikpak-*"
    elif uses_logstash and not uses_pikpak:
        transition_index_pattern = "logstash-*"
    else:
        transition_index_pattern = index_id
    transition_endpoint = _search_url(url, transition_index_pattern)

    identity_fields = set()
    for hit in error_hits:
        source = hit.get("_source", {})
        if source.get("system_id"):
            identity_fields.add("system_id")
        if source.get("leap_robot_id"):
            identity_fields.add("leap_robot_id")
    if len(identity_fields) == 1:
        identity_field = next(iter(identity_fields))
        transition_robot_filter = {
            "bool": {
                "should": [
                    {"term": {f"{identity_field}.keyword": robot_id}},
                    {"term": {identity_field: robot_id}},
                    {"match_phrase": {identity_field: robot_id}},
                ],
                "minimum_should_match": 1,
            }
        }
    else:
        transition_robot_filter = _build_robot_filters(robot_id)
    transition_states = TRANSITION_STATES
    transition_clause = {
        "bool": {
            "should": [
                {"terms": {"state_name.keyword": transition_states}},
                {"terms": {"state_name": transition_states}},
                {"match_phrase": {"message": "Shutting down system"}},
                {"match_phrase": {"json_request.service_name": "system_shutdown"}},
            ],
            "minimum_should_match": 1,
        }
    }
    transition_hits = fetch_matching_hits(
        transition_clause,
        start_dt,
        [ts_field, "@timestamp_ros", "@timestamp", "state_name", "message", "json_request.service_name", "json_request"],
        endpoint=transition_endpoint,
        robot_filter=transition_robot_filter,
        query_label="operation-state query",
    )

    prior_body = {
        "size": 1,
        "track_total_hits": False,
        "_source": [ts_field, "@timestamp_ros", "@timestamp", "state_name", "message", "json_request.service_name", "json_request"],
        "sort": [{ts_field: {"order": "desc", "format": "strict_date_optional_time"}}],
        "query": {
            "bool": {
                "filter": [
                    transition_robot_filter,
                    {"range": {ts_field: {"lt": start_iso, "format": "strict_date_optional_time"}}},
                ],
                "must": [transition_clause],
            }
        },
    }
    prior_hits = None
    for prior_timeout in (15, 25):
        try:
            prior_response = session.post(
                transition_endpoint,
                json=prior_body,
                headers=headers,
                timeout=prior_timeout,
            )
            prior_response.raise_for_status()
            prior_hits = prior_response.json().get("hits", {}).get("hits", [])
            break
        except requests.exceptions.Timeout as exc:
            if prior_timeout == 25:
                raise ElasticFetchError(
                    f"[elastic] Fleetwide prior-state query timed out for {robot_id} after retries.",
                    [],
                ) from exc
        except Exception as exc:
            err_text = ""
            if isinstance(exc, requests.RequestException) and exc.response is not None:
                try:
                    err_text = exc.response.text
                except Exception:
                    pass
            message = f"[elastic] fleetwide prior-state query failed for {robot_id}: {exc} {err_text}".strip()
            raise ElasticFetchError(message, []) from exc
    prior_hits = prior_hits or []
    transition_hits = list(prior_hits) + transition_hits

    transitions: list[tuple[datetime, str, str, str]] = []
    for hit in transition_hits:
        source = hit.get("_source", {})
        timestamp = _parse_ts(str(source.get(ts_field) or source.get("@timestamp_ros") or source.get("@timestamp") or ""))
        if timestamp is None:
            continue
        transitions.append(
            (
                _ensure_utc(timestamp),
                str(source.get("state_name") or "").strip(),
                str(source.get("message") or ""),
                _extract_service_name(source),
            )
        )
    transitions.sort(key=lambda item: item[0])

    operation_grace = timedelta(seconds=60)
    active_after: datetime | None = None
    transition_index = 0
    operational_total = 0
    for timestamp in error_times:
        while transition_index < len(transitions) and transitions[transition_index][0] <= timestamp:
            transition_ts, state_name, message, service_name = transitions[transition_index]
            lower_state = state_name.lower()
            if state_name == "start_pnp":
                active_after = transition_ts + operation_grace
            elif (
                state_name == "controller_node_manual_mode"
                or ("manual" in lower_state and "mode" in lower_state)
                or _is_stop_like_event(state_name, message, service_name)
            ):
                active_after = None
            transition_index += 1
        operational = active_after is not None and timestamp >= active_after
        index = int((timestamp - bucket_origin).total_seconds() // bucket_seconds)
        if index < 0 or index >= len(buckets):
            continue
        buckets[index]["count"] += 1
        if operational:
            buckets[index]["operational_count"] += 1
            operational_total += 1
        else:
            buckets[index]["non_operational_count"] += 1
    return {
        "robot_id": robot_id,
        "total": len(error_times),
        "operational_total": operational_total,
        "non_operational_total": len(error_times) - operational_total,
        "operation_grace_seconds": int(operation_grace.total_seconds()),
        "raw_total": raw_error_count,
        "suppressed_count": raw_error_count - len(error_times),
        "cooldown_seconds": FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS,
        "buckets": buckets,
    }
