"""The Stop Report: one row per stop event on the loaded day, with a
clip thumbnail that jumps the viewer to that moment.

Extracted from Main_Window (it was ~500 lines of the hub). The build is
split in two so the expensive half runs on a worker thread:

- collect_stop_report_data(): Elastic fallback fetch, SMB clip copies,
  cv2 thumbnail decodes. No QPixmap/QWidget use — safe off the UI thread.
- build_stop_report_entries(): converts the collected RGB frames to
  QPixmaps. QPixmap is GUI-thread-only, so this half must stay there.
"""
from __future__ import annotations

from concurrent.futures import as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from logfather.core.timeline_model import (
    TimelineItem,
    format_local_time,
    local_day_end_utc,
    local_day_start_utc,
)
from logfather.data.elastic_loader import fetch_logs_for_range
from logfather.ui import theme
from logfather.data.settings_store import Settings

STOP_THUMB_SIZE = (352, 198)


@dataclass
class StopReportEntry:
    event_time: datetime
    category: str
    label: str
    video_item: TimelineItem | None
    video_path: Path | None
    seek_seconds: float
    thumbnail: QPixmap
    source: str = ""
    state_name: str = ""
    sku_info: str = ""


class StopReportDialog(QDialog):
    open_requested = Signal(object)  # StopReportEntry

    def __init__(self, entries: list[StopReportEntry], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stop Report")
        self.resize(1050, 700)
        self._entries = list(entries)
        self._row_widgets: list[tuple[QWidget, str]] = []
        self._filter_buttons: dict[str, QPushButton] = {}
        self._media_content_size = QSize(STOP_THUMB_SIZE[0] - 20, STOP_THUMB_SIZE[1] - 20)

        root = QVBoxLayout(self)
        self._intro = QLabel()
        root.addWidget(self._intro)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_label = QLabel("Filters:")
        filter_row.addWidget(filter_label)
        for label, key in (
            ("Caution", "caution"),
            ("E-stop", "estop"),
            ("Operator stop", "operator"),
            ("Manual stop", "manual"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.toggled.connect(self._apply_filters)
            filter_row.addWidget(btn)
            self._filter_buttons[key] = btn
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(6)
        content_layout.setContentsMargins(6, 6, 6, 6)

        for entry in entries:
            row = QWidget()
            bg_color, border_color = self._entry_colors(entry)
            row.setStyleSheet(theme.report_row_style(bg_color.name(), border_color.name()))
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 4, 4, 4)
            row_layout.setSpacing(10)

            media_frame = self._build_media_frame(entry)
            row_layout.addWidget(media_frame)

            time_only = format_local_time(entry.event_time)
            text_col = QVBoxLayout()
            text_col.setSpacing(4)

            title = QLabel(f"{time_only} | {entry.category}")
            title_font = QFont(self.font())
            title_font.setPointSize(title_font.pointSize() + 3)
            title_font.setBold(True)
            title.setFont(title_font)
            title.setWordWrap(True)
            text_col.addWidget(title)

            detail = QLabel(
                f"{entry.state_name or entry.label}\n"
                f"SKU: {entry.sku_info or '-'}"
            )
            detail.setWordWrap(True)
            text_col.addWidget(detail)
            text_col.addStretch(1)
            row_layout.addLayout(text_col, 1)
            content_layout.addWidget(row)
            self._row_widgets.append((row, self._entry_filter_key(entry)))

        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        root.addWidget(close_btn, 0, Qt.AlignRight)
        self._apply_filters()

    def _request_open(self, entry: StopReportEntry):
        self.open_requested.emit(entry)
        self.accept()

    def _build_media_frame(self, entry: StopReportEntry) -> QWidget:
        holder = QWidget()
        holder.setFixedSize(STOP_THUMB_SIZE[0], STOP_THUMB_SIZE[1])
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        holder.setStyleSheet(theme.MEDIA_HOLDER)
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(10, 10, 10, 10)
        holder_layout.setSpacing(0)

        thumb_btn = QPushButton()
        thumb_btn.setFixedSize(self._media_content_size)
        thumb_btn.setStyleSheet(theme.THUMB_BUTTON)
        if entry.video_path is None:
            thumb_btn.setText(f"{entry.category}\n{format_local_time(entry.event_time)}")
            thumb_btn.setEnabled(False)
        else:
            thumb_btn.setIcon(QIcon(entry.thumbnail))
            thumb_btn.setIconSize(self._media_content_size)
            thumb_btn.clicked.connect(lambda _checked=False, e=entry: self._request_open(e))
        holder_layout.addWidget(thumb_btn, 0, Qt.AlignCenter)
        return holder

    def _entry_colors(self, entry: StopReportEntry) -> tuple[QColor, QColor]:
        key = StopReportDialog._entry_filter_key(entry)
        base = self.palette().color(QPalette.Window)
        if key == "estop":
            accent = QColor(theme.STOP_ACCENT_ESTOP)
        elif key == "caution":
            accent = QColor(theme.STOP_ACCENT_CAUTION)
        elif key in ("operator", "manual"):
            accent = QColor(theme.STOP_ACCENT_OPERATOR)
        else:
            accent = self.palette().color(QPalette.Mid)
        bg_color = self._blend_colors(base, accent, 0.22)
        border_color = self._blend_colors(base, accent, 0.45)
        return bg_color, border_color

    @staticmethod
    def _blend_colors(base: QColor, accent: QColor, amount: float) -> QColor:
        amount = max(0.0, min(1.0, amount))
        inv = 1.0 - amount
        return QColor(
            int(base.red() * inv + accent.red() * amount),
            int(base.green() * inv + accent.green() * amount),
            int(base.blue() * inv + accent.blue() * amount),
        )

    @staticmethod
    def _entry_filter_key(entry: StopReportEntry) -> str:
        category = (entry.category or "").strip().lower()
        state_name = (entry.state_name or "").strip().lower()
        label = (entry.label or "").strip().lower()
        combined = f"{category} {state_name} {label}"
        if "caution" in combined:
            return "caution"
        if "manual" in combined:
            return "manual"
        if "operator" in combined:
            return "operator"
        if "emergency" in combined or "estop" in combined or "e-stop" in combined:
            return "estop"
        return "other"

    def _apply_filters(self):
        enabled = {key for key, btn in self._filter_buttons.items() if btn.isChecked()}
        visible_count = 0
        for row, key in self._row_widgets:
            is_visible = key not in self._filter_buttons or key in enabled
            row.setVisible(is_visible)
            if is_visible:
                visible_count += 1
        total = len(self._entries)
        if visible_count == total:
            self._intro.setText(f"{total} stop events for selected day")
        else:
            self._intro.setText(f"Showing {visible_count} of {total} stop events for selected day")



@dataclass
class StopEventData:
    """Worker-side product: a StopReportEntry minus the QPixmap.
    thumb_rgb is a cover-scaled HxWx3 uint8 RGB array, or None for the
    text placeholder."""

    event_time: datetime
    category: str
    label: str
    video_item: TimelineItem | None
    video_path: Path | None
    seek_seconds: float
    thumb_rgb: object | None
    source: str = ""
    state_name: str = ""
    sku_info: str = ""


def collect_stop_report_data(
    items: list[TimelineItem],
    *,
    settings: Settings,
    day,
    root: Path | None,
    robot_id: str | None,
    clip_cache,
    job=None,
) -> list[StopEventData]:
    """Gather the day's stop report data: Elastic fallback fetch, clip
    copies, thumbnail decodes. Worker-safe (no QPixmap/QWidget).

    `robot_id` is the system the fallback fetch queries, resolved by the
    caller on the UI thread. `clip_cache` is the viewer's ClipCache; `job`
    (a qt_worker.Job) gets ("copies"|"thumbs", done, total) progress and
    is polled for interruption between steps."""
    if not items:
        return []
    has_operator_stop_in_timeline = any(_is_operator_stop_item(itm) for itm in items)
    video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
    video_items.sort(key=lambda i: i.start)
    sku_items = [itm for itm in items if itm.kind == "sku" and itm.start is not None and itm.end is not None]
    sku_items.sort(key=lambda i: i.start)
    stop_items: list[tuple[TimelineItem, str, dict]] = []
    required_paths: set[Path] = set()
    for itm in items:
        if itm.kind in ("video", "additional"):
            continue
        category = _categorize_stop_event(itm)
        if category is None:
            continue
        src = {}
        if isinstance(itm.payload, dict):
            src_val = itm.payload.get("_source")
            if isinstance(src_val, dict):
                src = src_val
        stop_items.append((itm, category, src))
        video_item = _find_video_item_for_time(video_items, itm.start)
        if video_item and isinstance(video_item.payload, Path):
            required_paths.add(video_item.payload)
    if required_paths:
        _cache_paths_for_report(clip_cache, sorted(required_paths, key=lambda p: str(p)), job)
    if job is not None and job.interrupted():
        return []

    data: list[StopEventData] = []
    thumb_cache: dict[tuple[str, int], object] = {}
    seen_keys: set[tuple[int, str]] = set()
    thumb_total = len(stop_items)
    for thumb_done, (itm, category, src) in enumerate(stop_items, start=1):
        if job is not None and job.interrupted():
            return []
        state_name = str(src.get("state_name") or "").strip()
        source = str(src.get("source") or "").strip()
        video_item = _find_video_item_for_time(video_items, itm.start)
        video_path = video_item.payload if (video_item and isinstance(video_item.payload, Path)) else None
        seek_seconds = 0.0
        if video_item is not None:
            seek_seconds = max(0.0, (itm.start - video_item.start).total_seconds())
        thumb_rgb = _thumbnail_rgb_for_event(clip_cache, video_path, seek_seconds, thumb_cache)
        if job is not None:
            job.emit_progress(("thumbs", thumb_done, thumb_total))
        key = (int(itm.start.timestamp()), state_name.lower() or category.lower())
        seen_keys.add(key)
        sku_info = _sku_for_time(sku_items, itm.start)
        data.append(
            StopEventData(
                event_time=itm.start,
                category=category,
                label=itm.label,
                video_item=video_item,
                video_path=video_path,
                seek_seconds=seek_seconds,
                thumb_rgb=thumb_rgb,
                source=source,
                state_name=state_name,
                sku_info=sku_info,
            )
        )

    # Ensure behaviour-node operator_stop entries are included even when not
    # represented by configured timeline conditions.
    if not has_operator_stop_in_timeline:
        for ts, source, state_name, message in _fetch_operator_stop_events(settings, day, root, robot_id):
            if job is not None and job.interrupted():
                return []
            key = (int(ts.timestamp()), state_name.lower() or "operator_stop")
            if key in seen_keys:
                continue
            video_item = _find_video_item_for_time(video_items, ts)
            video_path = video_item.payload if (video_item and isinstance(video_item.payload, Path)) else None
            seek_seconds = 0.0
            if video_item is not None:
                seek_seconds = max(0.0, (ts - video_item.start).total_seconds())
            thumb_rgb = _thumbnail_rgb_for_event(clip_cache, video_path, seek_seconds, thumb_cache)
            sku_info = _sku_for_time(sku_items, ts)
            data.append(
                StopEventData(
                    event_time=ts,
                    category="Operator Stop",
                    label=message or state_name or "operator_stop",
                    video_item=video_item,
                    video_path=video_path,
                    seek_seconds=seek_seconds,
                    thumb_rgb=thumb_rgb,
                    source=source,
                    state_name=state_name,
                    sku_info=sku_info,
                )
            )
            seen_keys.add(key)

    data.sort(key=lambda d: d.event_time)
    return data


def build_stop_report_entries(data: list[StopEventData]) -> list[StopReportEntry]:
    """UI-thread half: turn collected RGB frames into QPixmap entries."""
    entries: list[StopReportEntry] = []
    pixmap_by_frame: dict[int, QPixmap] = {}
    for d in data:
        if d.thumb_rgb is not None:
            pix = pixmap_by_frame.get(id(d.thumb_rgb))
            if pix is None:
                rgb = d.thumb_rgb
                h, w = rgb.shape[:2]
                qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()
                pix = QPixmap.fromImage(qimg)
                pixmap_by_frame[id(d.thumb_rgb)] = pix
        else:
            # Placeholder when clip/frame unavailable.
            pix = QPixmap(STOP_THUMB_SIZE[0], STOP_THUMB_SIZE[1])
            pix.fill(QColor("#2d2d2d"))
            painter = QPainter(pix)
            painter.setPen(QColor("#dddddd"))
            painter.drawText(
                pix.rect(),
                Qt.AlignCenter,
                f"{d.category}\n{format_local_time(d.event_time)}",
            )
            painter.end()
        entries.append(
            StopReportEntry(
                event_time=d.event_time,
                category=d.category,
                label=d.label,
                video_item=d.video_item,
                video_path=d.video_path,
                seek_seconds=d.seek_seconds,
                thumbnail=pix,
                source=d.source,
                state_name=d.state_name,
                sku_info=d.sku_info,
            )
        )
    return entries

def _fetch_operator_stop_events(
    settings: Settings, day, root: Path | None, robot_id: str | None
) -> list[tuple[datetime, str, str, str]]:
    if day is None:
        return []
    start_dt = local_day_start_utc(day)
    end_dt = local_day_end_utc(day)
    try:
        rows = fetch_logs_for_range(
            settings,
            root,
            start_dt,
            end_dt,
            max_hits=30000,
            robot_id=robot_id,
        )
    except Exception:
        return []
    matches: list[tuple[datetime, str, str, str]] = []
    for ts, _text, source, state_name, message in rows:
        s_state = str(state_name or "").strip().lower()
        s_source = str(source or "").strip().lower()
        if s_state != "operator_stop":
            continue
        if "behaviour_node" not in s_source:
            continue
        matches.append((ts, str(source or ""), str(state_name or ""), str(message or "")))
    return matches

def _is_operator_stop_item(item: TimelineItem) -> bool:
    payload = item.payload if isinstance(item.payload, dict) else {}
    src = payload.get("_source") if isinstance(payload.get("_source"), dict) else {}
    state_name = str(src.get("state_name") or "").strip().lower()
    source = str(src.get("source") or "").strip().lower()
    return state_name == "operator_stop" and "behaviour_node" in source

def _cache_paths_for_report(clip_cache, paths: list[Path], job=None):
    """Worker-side: stage the report's clips into the local cache via the
    ClipCache executor, reporting ("copies", done, total) progress."""
    if not paths:
        return
    futures = []
    executor = clip_cache.executor
    for src_path in paths:
        try:
            cache_path = clip_cache.cache_path_for(src_path)
        except Exception:
            continue
        if cache_path.exists():
            continue
        futures.append(executor.submit(clip_cache.copy_to_cache, src_path, cache_path))
    total = len(futures)
    if job is not None and total:
        job.emit_progress(("copies", 0, total))
    completed = 0
    for fut in as_completed(futures):
        completed += 1
        if job is not None:
            job.emit_progress(("copies", completed, total))
            if job.interrupted():
                for pending in futures:
                    pending.cancel()
                break
        try:
            _ = fut.result()
        except Exception:
            continue

def _find_video_item_for_time(video_items: list[TimelineItem], ts: datetime) -> TimelineItem | None:
    for itm in video_items:
        if itm.start <= ts < itm.end:
            return itm
    return None

def _sku_for_time(sku_items: list[TimelineItem], ts: datetime) -> str:
    # Prefer an active SKU interval, then fall back to the most recent
    # known SKU at/just before the stop boundary.
    last_known_sku = ""
    for itm in sku_items:
        if itm.start is None or itm.end is None:
            continue
        payload = itm.payload if isinstance(itm.payload, dict) else {}
        is_manual = bool(payload.get("_ui_manual"))
        label = _format_sku_label(itm)
        if not is_manual and label:
            last_known_sku = label
        # Inclusive end boundary so stop events that close a SKU run at the
        # same timestamp still resolve to that SKU.
        if itm.start <= ts <= itm.end:
            if is_manual:
                return "Manual"
            return label or last_known_sku
        if ts < itm.start:
            break
    if last_known_sku:
        return last_known_sku
    return ""

def _format_sku_label(item: TimelineItem) -> str:
    payload = item.payload if isinstance(item.payload, dict) else {}
    sku = str(payload.get("_ui_sku") or item.label or "").strip()
    tray = str(payload.get("_ui_tray") or "").strip()
    tool = str(payload.get("_ui_tool") or "").strip()
    parts = [p for p in (sku, tray, tool) if p]
    return " | ".join(parts) if parts else sku

def _categorize_stop_event(item: TimelineItem) -> str | None:
    # SKU manual segments are explicit stop periods.
    if item.kind == "sku" and isinstance(item.payload, dict) and item.payload.get("_ui_manual"):
        return "Manual Stop"

    state_name = ""
    message = item.label or ""
    if isinstance(item.payload, dict):
        src = item.payload.get("_source")
        if isinstance(src, dict):
            state_name = str(src.get("state_name") or "").strip().lower()
            message = str(src.get("message") or message).strip()
    track = (item.track_label or "").strip().lower()
    text = f"{track} {state_name} {message}".lower()
    if "go_home_check" in text:
        return None
    if "start_pnp" in text or "automatic_mode" in text or "start" == track:
        return None
    # Include any stop-like condition.
    if "manual_mode" in text or "manual" in text:
        return "Manual Stop"
    if "caution" in text:
        return "Caution Stop"
    if "emergency" in text or "estop" in text or "protective" in text:
        return "E-stop"
    if "manual" in text:
        return "Manual Stop"
    if "stop" in text:
        return "Normal Stop"
    return None

def _thumbnail_rgb_for_event(
    clip_cache,
    video_path: Path | None,
    seek_seconds: float,
    thumb_cache: dict[tuple[str, int], object],
):
    """Worker-side: decode the stop moment's frame from the cached clip and
    cover-scale it to the thumbnail size. Returns an RGB array or None
    (placeholder drawn on the UI thread)."""
    cache_key = None
    if video_path is not None:
        cache_key = (str(video_path), int(round(seek_seconds * 10)))
        if cache_key in thumb_cache:
            return thumb_cache[cache_key]
    rgb_scaled = None
    source_path = _thumbnail_source_path(clip_cache, video_path)
    if source_path is not None and source_path.exists():
        cap = cv2.VideoCapture(str(source_path))
        try:
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                frame_idx = max(0, int(round(seek_seconds * fps)))
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ok, frame = cap.read()
                if ok and frame is not None:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    h, w = rgb.shape[:2]
                    # Cover-scale (like Qt's KeepAspectRatioByExpanding).
                    scale = max(STOP_THUMB_SIZE[0] / w, STOP_THUMB_SIZE[1] / h)
                    rgb_scaled = cv2.resize(
                        rgb,
                        (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                        interpolation=cv2.INTER_AREA,
                    )
        finally:
            cap.release()
    if cache_key is not None:
        thumb_cache[cache_key] = rgb_scaled
    return rgb_scaled

def _thumbnail_source_path(clip_cache, video_path: Path | None) -> Path | None:
    if video_path is None:
        return None
    # Avoid network reads in the UI thread: only use already-cached local files.
    try:
        cached = clip_cache.get_valid_cached_path(video_path)
        if cached and isinstance(cached, Path):
            return cached
        cached = clip_cache.cache_path_for(video_path)
        if cached and isinstance(cached, Path) and cached.exists():
            return cached
    except Exception:
        return None
    return None

