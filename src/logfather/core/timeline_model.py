"""Timeline data model and day/clip helpers — no Qt.

Extracted from replay_timeline so the data layer (elastic_loader, sku_timeline,
stop_report, ...) no longer imports a widget module for TimelineItem and the
timezone/day helpers. replay_timeline re-exports everything here, so UI-side
imports are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

if ZoneInfo is not None:
    try:
        LOCAL_TIMEZONE = ZoneInfo("Europe/London")
    except Exception:
        LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
else:
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc


_TIME_FROM_NAME_RE = re.compile(r"(\d{8})(\d{6})")


# Looks for YYYYMMDDHHMMSS in filename; falls back to mtime if missing.
def parse_time_from_name(path: Path) -> Optional[datetime]:
    m = _TIME_FROM_NAME_RE.search(path.stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=LOCAL_TIMEZONE)
    except ValueError:
        return None


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TIMEZONE)
    return dt.astimezone(LOCAL_TIMEZONE)


def ensure_playhead_local(dt: datetime) -> datetime:
    # Viewer playback timestamps are local wall-clock times when naive.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TIMEZONE)
    return dt.astimezone(LOCAL_TIMEZONE)


def local_day_start_utc(day: date) -> datetime:
    return ensure_utc(datetime(day.year, day.month, day.day, tzinfo=LOCAL_TIMEZONE))


def local_day_end_utc(day: date) -> datetime:
    return local_day_start_utc(day) + timedelta(days=1) - timedelta(milliseconds=1)


def format_local_time(dt: datetime, fmt: str = "%H:%M:%S") -> str:
    return ensure_local(dt).strftime(fmt)


def format_uk_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def load_day_files(pikpak_root: Path, day: date) -> Iterable[Path]:
    day_dir = pikpak_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    if not day_dir.exists():
        return []
    allowed = {".mp4", ".mov", ".mkv"}
    return [p for p in day_dir.iterdir() if p.is_file() and p.suffix.lower() in allowed]


MIN_BLOCK_DURATION = timedelta(seconds=30)
LAST_BLOCK_DURATION = timedelta(minutes=5)


def inferred_live_clip_end(path: Path, start_dt: datetime) -> datetime:
    start_utc = ensure_utc(start_dt)
    fallback_end = start_utc + LAST_BLOCK_DURATION
    try:
        stat = path.stat()
        mtime_end = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    except Exception:
        return fallback_end
    if mtime_end < start_utc + MIN_BLOCK_DURATION:
        return max(fallback_end, start_utc + MIN_BLOCK_DURATION)
    return max(fallback_end, ensure_utc(mtime_end))


@dataclass(slots=True)
class TimelineItem:
    start: datetime
    end: datetime
    label: str
    kind: str  # e.g., "video", "event"
    color: object  # QColor or hex string
    payload: object  # Path or event metadata
    track_label: str | None = None
    cached: bool = False
    annotated: bool = False
    path_key: str | None = None


def _cache_key_for(path: Path, cache_root: Path) -> Path:
    key = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
    filename = f"{path.stem}_{key}{path.suffix}"
    return cache_root / filename


def _path_key(path: Path) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return str(path)


def _build_cache_index(cache_root: Path | None) -> set[str]:
    if cache_root is None:
        return set()
    try:
        return {p.name for p in cache_root.iterdir() if p.is_file()}
    except Exception:
        return set()


def _build_annotation_index(cache_root: Path | None) -> set[str]:
    if cache_root is None:
        return set()
    ann_dir = cache_root / "annotations"
    try:
        return {p.name for p in ann_dir.iterdir() if p.is_file() and p.suffix.lower() == ".json"}
    except Exception:
        return set()


def _is_path_cached(path: Path, cache_root: Path | None, cache_index: set[str] | None = None) -> bool:
    if cache_root is None:
        return False
    try:
        cache_file = _cache_key_for(path, cache_root)
        if cache_index is not None:
            return cache_file.name in cache_index
        return cache_file.exists()
    except Exception:
        return False


def _annotations_path_for(path: Path, cache_root: Path | None) -> Path | None:
    if cache_root is None:
        return None
    try:
        cache_path = _cache_key_for(path, cache_root)
    except Exception:
        return None
    return cache_root / "annotations" / f"{cache_path.stem}.json"


def _has_annotations(path: Path, cache_root: Path | None, ann_index: set[str] | None = None) -> bool:
    ann_path = _annotations_path_for(path, cache_root)
    if ann_path is None:
        return False
    if ann_index is not None:
        if ann_path.name not in ann_index:
            return False
    elif not ann_path.exists():
        return False
    try:
        data = json.loads(ann_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    items = data.get("annotations", [])
    return isinstance(items, list) and len(items) > 0


VIDEO_COLOR_UNCACHED = "#70757f"
VIDEO_COLOR_CACHED = "#5e9bff"
VIDEO_COLOR_SELECTED = "#b2e5b2"
