from __future__ import annotations

import os

import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable, Optional, Dict, Tuple, List

from PySide6.QtCore import Qt, Signal, QEvent, QThread, QRectF, QPointF, QTimer, QSize

from logfather.ui.qt_worker import JobSlot
from logfather.ui import theme
from logfather.ui.icons import zoom_glyph_icon
from logfather.ui.overview_signals import SignalBoxes, COMPACT_BOX_STYLE, COMPACT_FONT_PX, add_label_backdrop, CollapsibleGroupBox
from logfather.data import grafana_client
from logfather.data.elastic_schema import robot_id_from_folder
from types import SimpleNamespace
from PySide6.QtGui import QAction, QBrush, QColor, QPen, QPolygonF, QFont, QFontMetrics, QPainterPath
from PySide6.QtWidgets import QApplication, QProgressDialog, QMessageBox, QMenu, QToolButton
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QGraphicsScene, QGraphicsView, QGraphicsRectItem, QGraphicsItem,
    QGraphicsPolygonItem, QGraphicsLineItem, QGroupBox, QGridLayout, QCheckBox, QGraphicsItemGroup,
    QSizePolicy
)


class _PinnedHeightPanel(QWidget):
    """A panel that is never shorter than its contents need (Chris,
    2026-09-11: the Data box text was squashed unreadable on a short
    screen). A layout hands out less than an item's minimum when the
    column is too short, but a widget is never resized below an explicit
    minimum, so the panel pins its own minimum height to what its layout
    needs and re-pins whenever the contents change."""

    def event(self, ev):
        handled = super().event(ev)  # lays the contents out first
        if ev.type() == QEvent.LayoutRequest:
            self.pin_height()
        return handled

    def pin_height(self) -> None:
        lay = self.layout()
        if lay is not None:
            self.setMinimumHeight(lay.minimumSize().height())
from logfather.data.ui_state_store import load_ui_state, update_ui_state

from logfather.data.elastic_errors import ElasticFetchError
# Re-exported for the many UI modules that import these from Time_Picker.
from logfather.core.timeline_model import (  # noqa: F401
    LAST_BLOCK_DURATION,
    LOCAL_TIMEZONE,
    MIN_BLOCK_DURATION,
    TimelineItem,
    VIDEO_COLOR_CACHED,
    VIDEO_COLOR_SELECTED,
    VIDEO_COLOR_UNCACHED,
    _annotations_path_for,
    _build_annotation_index,
    _build_cache_index,
    _cache_key_for,
    _has_annotations,
    _is_path_cached,
    _path_key,
    ensure_local,
    ensure_playhead_local,
    ensure_utc,
    format_local_time,
    format_uk_date,
    inferred_live_clip_end,
    local_day_end_utc,
    local_day_start_utc,
    parse_time_from_name,
)

TIMELINE_TIMING_LOGS = True
SHOW_TIMELINE_INFO_TEXT = False
SHOW_TIMELINE_TOP_BUTTONS = False
DAY_RATE_PROXY_BUCKET_SECONDS = 300
DAY_RATE_PROXY_TERMS = ("eject", "crate")


def _timeline_perf_log(message: str) -> None:
    if TIMELINE_TIMING_LOGS:
        print(f"[timeline-perf] {message}", flush=True)


# Condition tracks that share one row to save height (Chris, 2026-09-10):
# matched by the condition's name, case-insensitive. Each keeps its own
# tick colour; the row carries one combined label and count.
SHARED_ROW_NAMES = ("start", "operator stop", "estop", "e-stop")
SHARED_ROW_LABEL = "Start / Op stop / E-stop"
# Conditions that are normal operation, not errors (Chris, 2026-09-10): they
# are ticked in the Data box rather than the Errors box, and their row is
# shown by default whenever the day has any.
NORMAL_CONDITION_NAMES = ("eject crate",)
TRACK_SPACING = 20
# LOGFATHER_DEBUG_PLAYHEAD=1 traces the green playhead (2026-09-12).
_DEBUG_PLAYHEAD = bool(os.environ.get("LOGFATHER_DEBUG_PLAYHEAD"))


class _EventTickItem(QGraphicsRectItem):
    """An event mark on a condition track (Chris, 2026-09-08): a wider
    invisible hit area around the 2 px tick so it can be hovered and
    clicked; the tooltip carries the exact time and the full message, a
    click asks the picker to open the footage and logs at that moment."""

    def __init__(self, x: float, y_center: float, item: TimelineItem, picker: "TimePicker"):
        # 14 px tall, the same as the clip bars (Chris, 2026-09-11).
        super().__init__(QRectF(x - 5, y_center - 8, 10, 16))
        self._item = item
        self._picker = picker
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(QColor(0, 0, 0, 0)))
        pen = QPen(QColor(item.color))
        pen.setWidth(2)
        line = QGraphicsLineItem(x, y_center - 7, x, y_center + 7, self)
        line.setPen(pen)
        self.setData(0, item)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptHoverEvents(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._picker.event_clicked.emit(self._item)
            event.accept()
            return
        super().mousePressEvent(event)


class VideoRectItem(QGraphicsRectItem):
    def __init__(self, rect, timeline_item: TimelineItem, picker: "TimePicker"):
        super().__init__(rect)
        self._timeline_item = timeline_item
        self._picker = picker
        self._display_text = ""
        self._display_color = QColor("#1a1a1a")
        self._display_font: QFont | None = None
        # Thumbnails disabled to avoid crashes; keep simple solid-color blocks.
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)

    def set_display_text(self, text: str, color: QColor | None = None, font: QFont | None = None):
        self._display_text = text or ""
        if color is not None:
            self._display_color = QColor(color)
        if font is not None:
            self._display_font = font
        self.update()

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if not self._display_text:
            return
        painter.save()
        if self._display_font is not None:
            painter.setFont(self._display_font)
        painter.setPen(self._display_color)
        painter.drawText(self.rect(), Qt.AlignCenter, self._display_text)
        painter.restore()


class TimePicker(QWidget):
    time_selected = Signal(object)  # TimelineItem
    items_changed = Signal()
    event_clicked = Signal(object)  # a condition-track event: open the footage at its time
    moment_clicked = Signal(object)  # a click on empty chart: the moment under the pointer (Chris, 2026-09-12)

    def __init__(self, load_func: Optional[Callable[[Path, date], Iterable[Path]]] = None,
                 extra_loaders: Optional[list[Callable[[Path, date, Optional[datetime]], Iterable[TimelineItem]]]] = None,
                 static_tracks: Optional[List[Tuple[str, str, str]]] = None,
                 cache_root: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Time Picker")
        self._load_func = load_func
        self._extra_loaders = extra_loaders or []
        self._items: list[TimelineItem] = []
        # static_tracks entries: (kind, label, color_hex)
        self._static_tracks = static_tracks or [("video", "Video", "#cce5ff")]

        self.info = QLabel("Pick a date to list times.")
        self.info.setVisible(SHOW_TIMELINE_INFO_TEXT)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_clicked)
        self.fit_btn = QPushButton("Fit")
        self.fit_btn.clicked.connect(self._fit_to_items)


        top = QHBoxLayout()
        top.addStretch(1)
        top.addWidget(self.fit_btn)
        top.addWidget(self.refresh_btn)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        # Small enough to fit the collapsed timeline (165 px in the main
        # window); at 260 the view overflowed and its horizontal scrollbar
        # was clipped off the bottom (Chris, 2026-09-10).
        self.view.setMinimumHeight(110)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.scene.selectionChanged.connect(self._emit_selection_from_timeline)
        self.view.horizontalScrollBar().valueChanged.connect(lambda _v: self._reposition_track_labels())
        # The time scale stays in view while the strips scroll (Chris,
        # 2026-09-10): the ticks and hour labels sit in a group that is
        # shifted down by the vertical scroll, over a dark band.
        self._scale_group: Optional[QGraphicsItemGroup] = None
        self._scale_shift = 0.0
        self.view.verticalScrollBar().valueChanged.connect(lambda _v: self._on_vertical_scroll())
        # A View menu at the top right of the chart shows or hides the bars
        # (Chris, 2026-09-11); hidden static rows are remembered in
        # ui_state under replay_bars_hidden.
        stored_bars = load_ui_state().get("replay_bars_hidden")
        self._hidden_bars: set = {str(k) for k in stored_bars} if isinstance(stored_bars, list) else set()
        self._bars_menu_btn = QToolButton(self.view)
        self._bars_menu_btn.setText("View \u25be")
        self._bars_menu_btn.setPopupMode(QToolButton.InstantPopup)
        self._bars_menu_btn.setCursor(Qt.PointingHandCursor)
        self._bars_menu_btn.setToolTip("Show or hide the bars on the timeline")
        self._bars_menu_btn.setStyleSheet(
            "QToolButton { background: rgba(0, 0, 0, 150); color: #ecf0f4; border: 1px solid rgba(255, 255, 255, 70);"
            " border-radius: 4px; padding: 2px 8px; font-size: 12px; }"
            "QToolButton:hover { background: rgba(0, 0, 0, 210); }"
            "QToolButton::menu-indicator { image: none; width: 0px; }"
        )
        self._bars_menu = QMenu(self._bars_menu_btn)
        self._bars_menu.aboutToShow.connect(self._rebuild_bars_menu)
        self._bars_menu_btn.setMenu(self._bars_menu)
        # + and - to the left of View (Chris, 2026-09-13): stretch or
        # contract the timeline about the middle of the view. Only the
        # pixels-per-minute scale changes; text and bar heights stay.
        self._zoom_btns: list[QToolButton] = []
        for glyph, tip, factor in (
            ("minus", "Contract the timeline (the mouse wheel also zooms)", 1 / 1.25),
            ("plus", "Stretch the timeline (the mouse wheel also zooms)", 1.25),
        ):
            zb = QToolButton(self.view)
            zb.setIcon(zoom_glyph_icon(glyph, 40))  # much bigger glyphs (Chris, 2026-09-13)
            zb.setIconSize(QSize(30, 30))
            zb.setFixedSize(38, 38)
            zb.setCursor(Qt.PointingHandCursor)
            zb.setToolTip(tip)
            zb.setAutoRepeat(True)
            zb.setAutoRepeatInterval(160)
            zb.setStyleSheet(
                "QToolButton { background: rgba(0, 0, 0, 150); border: 1px solid rgba(255, 255, 255, 70); border-radius: 19px; }"
                "QToolButton:hover { background: rgba(0, 0, 0, 210); }"
            )
            zb.clicked.connect(lambda _checked=False, f=factor: self._zoom_about(f, None))
            self._zoom_btns.append(zb)
        self.view.installEventFilter(self)
        self._place_bars_menu_btn()

        # The Data and Additional data boxes, as on the Overview (Chris,
        # 2026-09-08): the ticked readings are drawn as strips under the
        # timeline tracks for the chosen system and day. The owner
        # interface SignalChannel needs: scene, view, settings,
        # status_label, _maybe_fetch_signals, _schedule_redraw, _redraw,
        # _drag_candidate, hide_thumbnail_preview, _fit_text.
        self.settings = None  # set by the main window
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #9aa0a6;")
        self._drag_candidate = None
        # Errors box (Chris, 2026-09-10): one line per condition with the
        # day's total and a tick to show or hide its row. A row is shown by
        # default when the day has more than one of that error; a tick the
        # user changes is remembered in ui_state under replay_rows.
        stored = load_ui_state().get("replay_rows")
        self._row_overrides: Dict[str, bool] = {str(k): bool(v) for k, v in stored.items()} if isinstance(stored, dict) else {}
        self._errors_box = CollapsibleGroupBox("Errors")
        self._errors_box.setStyleSheet(COMPACT_BOX_STYLE)
        self._errors_grid = QGridLayout(self._errors_box.body)
        self._errors_grid.setContentsMargins(4, 2, 4, 2)
        self._errors_grid.setHorizontalSpacing(6)
        self._errors_grid.setVerticalSpacing(0)
        self._error_checks: Dict[str, QCheckBox] = {}
        self._normal_rows_layout: Optional[QVBoxLayout] = None
        self._errors_box_updating = False
        self._last_condition_rows: list = []
        # The moment under the pointer at the last press on the timeline, so
        # a click on a clip opens it at that moment (Chris, 2026-09-10).
        self.last_click_time: Optional[datetime] = None
        self._signals = SignalBoxes(self, "replay", picks_default=True)
        self._strips_bottom: Optional[float] = None
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(6)
        if SHOW_TIMELINE_TOP_BUTTONS:
            layout.addLayout(top)
        layout.addWidget(self.view, 1)
        self.setLayout(layout)

        self._current_root: Optional[Path] = None
        self._current_date: Optional[date] = None
        self._cursor_line = None
        self._cursor_label = None
        self._cursor_marker_outer = None
        self._cursor_marker_inner = None
        self._playhead_line = None
        self._playhead_label = None
        self._playhead_time: Optional[datetime] = None
        self._ppm = 5
        self._day_start: Optional[datetime] = None
        # Telemetry row (Chris, 2026-09-07): one summary line (hottest motor)
        # across the day, fed by the main window once Grafana answers.
        self._telemetry_summary: Optional[tuple] = None
        self._tracks_bottom: Optional[float] = None
        # The Telemetry row is off unless ticked in Additional data (Chris,
        # 2026-09-11); remembered in ui_state.
        self._show_telemetry_row = bool(load_ui_state().get("replay_telemetry_row", False))
        self._baseline_y = 28
        self._scale_y = 4
        self._track_labels: Dict[str, object] = {}
        # A solid gutter behind the row labels, the full height of the
        # scene, as wide as the widest label (Chris, 2026-09-11): no chart
        # data shows under the labels when the timeline is scrolled.
        self._gutter_rect = None
        self._track_counts: Dict[str, object] = {}
        self._track_positions: Dict[str, float] = {}
        self._busy = False
        self._loading_rect = None
        self._loading_text = None
        self._loader_slot = JobSlot(self)
        self._progress: QProgressDialog | None = None
        self._last_cursor_x: Optional[float] = None
        self._cache_root = cache_root
        self._selected_video_item: Optional[TimelineItem] = None
        self._video_rects: dict[int, QGraphicsRectItem] = {}
        self._target_rate_day_buckets: list[dict] = []
        self._target_rate_clip_buckets: list[dict] = []
        self._target_rate_clip_start: Optional[datetime] = None
        self._target_rate_clip_end: Optional[datetime] = None
        self._suppress_selection_emit = False
        self._pending_time_selected = None
        self._time_selected_emit_scheduled = False
        # Rebuilding the scene on every resize event makes splitter drags
        # feel heavy; coalesce bursts into one redraw.
        self._resize_redraw_timer = QTimer(self)
        self._resize_redraw_timer.setSingleShot(True)
        self._resize_redraw_timer.setInterval(120)
        self._resize_redraw_timer.timeout.connect(self._fit_to_items)

    def set_loader(self, func: Callable[[Path, date], Iterable[Path]]):
        self._load_func = func

    @property
    def current_root(self) -> Optional[Path]:
        return self._current_root

    def show_times(self, pikpak_root: Optional[Path], day: Optional[date]):
        # Stop any in-flight loads so stale results can't repopulate after switching PikPak/day.
        self._stop_loader_thread()
        self._current_root = pikpak_root
        self._current_date = day
        self._items.clear()
        self.scene.clear()
        self._signals.reset_for_redraw()
        self._maybe_fetch_signals(force=True)
        self._cursor_line = None
        self._cursor_label = None
        self._cursor_marker_outer = None
        self._cursor_marker_inner = None
        self._playhead_line = None
        self._playhead_label = None
        # The playhead time survives a day load or Refresh (Chris,
        # 2026-09-12: the green line vanished after Refresh until playback
        # moved); the indicator hides itself if the day differs.
        self._track_positions = {}
        self._selected_video_item = None
        self._target_rate_day_buckets = []
        self._target_rate_clip_buckets = []
        self._target_rate_clip_start = None
        self._target_rate_clip_end = None

        if not day:
            self._set_info_text("Pick a date to list times.")
            return

        load_root = pikpak_root
        load_func = self._load_func
        if load_root is None:
            if not self._extra_loaders:
                self._set_info_text("Pick a date to list times.")
                return
            load_root = Path(".")
            load_func = lambda _root, _day: []
            self._set_info_text(f"Loading logs for {format_uk_date(day)}...")
        elif not load_func:
            self._set_info_text("Pick a date to list times.")
            return

        self._set_busy(True, f"Loading items for {format_uk_date(day)}...")
        extra_loaders = list(self._extra_loaders)
        cache_root = self._cache_root
        self._loader_slot.start(
            lambda job: _load_timeline_items(job, load_root, day, load_func, extra_loaders, cache_root),
            on_result=self._on_load_result,
            on_error=self._on_load_failed,
            on_progress=self._on_load_progress,
        )

    def _redraw_timeline(self):
        print("[timeline] _redraw_timeline", flush=True)
        self.scene.clear()
        self._video_rects = {}
        if not self._items or not self._current_date:
            return

        day_start = local_day_start_utc(self._current_date)
        self._day_start = day_start
        ppm = self._ppm
        total_minutes = 24 * 60
        height = 80
        baseline_y = self._baseline_y
        scale_y = self._scale_y

        # Background track (slimmer clip bars, Chris, 2026-09-11). The red
        # pick-rate heat strip is no longer drawn: the Picks strip shows it.
        self.scene.addRect(0, baseline_y - 7, total_minutes * ppm, 14, QPen(Qt.NoPen), QBrush(QColor("#2e2e2e")))

        # Time scale across the top (dynamic tick density).
        # Define step in minutes based on zoom.
        if ppm >= 20:
            step_min = 5
        elif ppm >= 12:
            step_min = 10
        elif ppm >= 8:
            step_min = 15
        elif ppm >= 5:
            step_min = 30
        else:
            step_min = 60

        # Label frequency adjusts with zoom to keep labels visible.
        if ppm >= 20:
            label_every_min = step_min  # label every tick
        elif ppm >= 12:
            label_every_min = step_min * 2
        elif ppm >= 8:
            label_every_min = step_min * 2
        elif ppm >= 5:
            label_every_min = step_min * 2
        else:
            label_every_min = 60  # hourly labels at low zoom
        minor_height = 6
        major_height = 12
        scale_group = QGraphicsItemGroup()
        scale_group.setZValue(4)
        scale_group.setHandlesChildEvents(False)
        band = self.scene.addRect(-60, scale_y - 30, total_minutes * ppm + 120, 44, QPen(Qt.NoPen), QBrush(QColor(20, 20, 20, 235)))
        scale_group.addToGroup(band)
        for minute in range(0, total_minutes + 1, step_min):
            x = minute * ppm
            is_major = (minute % label_every_min) == 0
            height_tick = major_height if is_major else minor_height
            pen = QPen(QColor("#666666") if is_major else QColor("#4d4d4d"))
            tick_item = self.scene.addLine(x, scale_y, x, scale_y + height_tick, pen)
            scale_group.addToGroup(tick_item)
            if is_major:
                hour = minute // 60
                label = self.scene.addText(f"{hour:02d}:{minute % 60:02d}")
                label.setDefaultTextColor(QColor("#cccccc"))
                label.setPos(x + 2, scale_y - 14)
                scale_group.addToGroup(label)
        self.scene.addItem(scale_group)
        self._scale_group = scale_group
        self._on_vertical_scroll()

        # Track stacking: keep video at baseline; stack other tracks below, tighter spacing.
        kinds = []
        label_map = {}
        color_map = {}
        for kind, label, color_hex in self._static_tracks:
            if kind not in kinds:
                kinds.append(kind)
                label_map[kind] = label
                if color_hex:
                    color_map[kind] = QColor(color_hex)
        seen = set(kinds)
        for item in self._items:
            if item.kind not in seen:
                seen.add(item.kind)
                kinds.append(item.kind)
            if item.kind not in label_map:
                label_map[item.kind] = item.track_label or item.kind.capitalize()
            if item.kind not in color_map:
                color_map[item.kind] = item.color
        # Condition rows are shown only when wanted (Chris, 2026-09-10): the
        # user's tick in the Errors box, else only if the day has more than
        # one of that error.
        all_counts: Dict[str, int] = {}
        for item in self._items:
            all_counts[item.kind] = all_counts.get(item.kind, 0) + 1
        hidden_kinds = {k for k in kinds if k.startswith("cond_") and not self._row_visible(str(label_map.get(k, k)), all_counts.get(k, 0))}
        if not self._show_telemetry_row:
            hidden_kinds.add("telemetry")
        # Static rows hidden from the View menu; "shared" covers the
        # Start / Stop / E-stop row.
        for k in kinds:
            if k in self._hidden_bars:
                hidden_kinds.add(k)
            elif "shared" in self._hidden_bars and k != "video" and str(label_map.get(k, "")).strip().lower() in SHARED_ROW_NAMES:
                hidden_kinds.add(k)
        kinds = [k for k in kinds if k not in hidden_kinds]
        self._refresh_errors_box(label_map, color_map, all_counts)

        track_map = {}
        spacing = TRACK_SPACING
        non_video_start = baseline_y + 32
        # Kinds whose label names them as part of the shared stop/start row.
        shared_kinds = [k for k in kinds if k != "video" and str(label_map.get(k, "")).strip().lower() in SHARED_ROW_NAMES]
        shared_y: Optional[float] = None
        if "video" in kinds:
            track_map["video"] = baseline_y
        next_row = non_video_start
        # The Picks strip sits just under the CCTV rows (Chris, 2026-09-08),
        # the other reading strips below every track.
        picks_h = self._signals.picks.strip_height(1.0) if self._current_root is not None else 0
        picks_anchor = "additional" if "additional" in kinds else "video"
        picks_y: Optional[float] = None
        if picks_h and picks_anchor == "video":
            picks_y = next_row - 8
            next_row += picks_h + 6
        for kind in kinds:
            if kind == "video":
                continue
            if kind in shared_kinds:
                if shared_y is None:
                    shared_y = next_row
                    next_row += spacing
                track_map[kind] = shared_y
                continue
            track_map[kind] = next_row
            next_row += spacing
            if picks_h and kind == picks_anchor:
                picks_y = next_row - 8
                next_row += picks_h + 6

        for item in self._items:
            if item.kind in hidden_kinds:
                continue
            start_offset_min = max(0, (item.start - day_start).total_seconds() / 60.0)
            end_offset_min = (item.end - day_start).total_seconds() / 60.0
            width = max(2.0, (end_offset_min - start_offset_min) * ppm)
            x = max(0.0, start_offset_min * ppm)

            y_center = track_map.get(item.kind, baseline_y)

            if item.kind not in ("video", "additional", "sku") or width <= 4:
                # Draw tick mark for events or near-zero duration.
                tick = _EventTickItem(x, y_center, item, self)
                tick.setToolTip(self._event_tooltip(item))
                tick.setZValue(2)
                self.scene.addItem(tick)
            else:
                bar_h = 24 if item.kind == "sku" else 14  # SKU boxes carry text; clip bars are slim
                rect = VideoRectItem(QRectF(x, y_center - bar_h / 2, width, bar_h), item, self)
                rect.setPen(QPen(QColor("#0b1a33") if item.kind == "video" else QColor("#444444")))
                if item.kind == "video":
                    rect.setBrush(QBrush(self._color_for_video_item(item)))
                else:
                    rect.setBrush(QBrush(QColor(item.color)))
                rect.setData(0, item)
                if item.kind == "sku":
                    display_text, tooltip = self._sku_box_text(item, rect.rect().width())
                    font = QFont()
                    font.setPointSize(9)
                    rect.set_display_text(display_text, QColor("#1a1a1a"), font)
                    rect.setToolTip(tooltip or "")
                else:
                    tooltip = f"{format_local_time(item.start)} - {format_local_time(item.end)}\n{item.label}"
                    extra = self._tooltip_extra_lines(item)
                    if extra:
                        tooltip = f"{tooltip}\n" + "\n".join(extra)
                    rect.setToolTip(tooltip)
                rect.setFlag(QGraphicsRectItem.ItemIsSelectable, True)
                self.scene.addItem(rect)
                if item.kind == "video" and item.annotated:
                    self._add_pen_icon(rect)
                self._video_rects[id(item)] = rect

        self._draw_telemetry_track(track_map.get("telemetry"), day_start, ppm, total_minutes * ppm)
        # Reading strips under the tracks (Chris, 2026-09-08), one per
        # ticked family, for this system over the whole day.
        self._signals.reset_for_redraw()
        self._strips_bottom = None
        strips_height = 0
        strip_specs = [(c, h) for c, h in self._signals.strip_heights(1.0) if c is not self._signals.picks]
        if self._current_root is not None and (strip_specs or picks_y is not None):
            state = SimpleNamespace(robot_id=robot_id_from_folder(self._current_root.name) or "", name=self._current_root.name)
            day_end = day_start + timedelta(days=1)
            if picks_y is not None:
                self._signals.picks.draw_strip(state, QRectF(0, picks_y, total_minutes * ppm, picks_h - 2), day_start, day_end, total_minutes * ppm, 0, title_x=-58, show_latest=False)
            strip_y = next_row + 6
            for channel, h in strip_specs:
                rect = QRectF(0, strip_y, total_minutes * ppm, h - 2)
                channel.draw_strip(state, rect, day_start, day_end, total_minutes * ppm, 0, title_x=-58, show_latest=False)
                strip_y += h
            strips_height = strip_y - next_row
            self._strips_bottom = strip_y if strip_specs else None
        # The bottom of the last row, so the green playhead runs through
        # every bar even with no readings strips (Chris, 2026-09-11).
        self._tracks_bottom = (max(track_map.values()) + 12) if track_map else None
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if strips_height else Qt.ScrollBarAlwaysOff)

        # Track labels on the left
        self._track_labels = {}
        self._track_counts = {}
        self._track_positions = track_map
        # Precompute counts per kind
        count_map: Dict[str, int] = {}
        for item in self._items:
            count_map[item.kind] = count_map.get(item.kind, 0) + 1

        for kind, y in track_map.items():
            if kind in shared_kinds and kind != shared_kinds[0]:
                continue  # the shared row is labelled once, on its first kind
            if kind in shared_kinds:
                text = SHARED_ROW_LABEL
                count_val = sum(count_map.get(k, 0) for k in shared_kinds)
            else:
                # The clips row is labelled like every other row (Chris,
                # 2026-09-11: it had no label, only "Additional CCTV" below).
                text = label_map.get(kind, kind.capitalize())
                count_val = count_map.get(kind, 0)
            label_item = self.scene.addText(text)
            if kind in shared_kinds:
                # Each name in its own colour (Chris, 2026-09-10): Start green,
                # Stop grey, E-stop red, taken from the conditions themselves.
                label_item.setHtml(self._shared_row_html(shared_kinds, label_map, color_map))
            else:
                label_item.setDefaultTextColor(color_map.get(kind, QColor("#cccccc")))
            label_item.setZValue(3)
            add_label_backdrop(label_item)
            self._track_labels[kind] = label_item

            count_item = self.scene.addText("" if kind == "telemetry" else f"{count_val}")
            count_item.setDefaultTextColor(color_map.get(kind, QColor("#cccccc")))
            count_item.setZValue(3)
            add_label_backdrop(count_item)
            self._track_counts[kind] = count_item
        self._reposition_track_labels()

        # Scene height adjusts to number of tracks
        total_tracks = max(1, len(set(track_map.values())))
        # Add top/side margin so cursor/time labels aren't clipped.
        self.scene.setSceneRect(-60, -40, total_minutes * ppm + 120, height + (total_tracks - 1) * spacing + 60 + strips_height)
        # Solid black (Chris, 2026-09-11), the full height of the scene.
        self._gutter_rect = self.scene.addRect(QRectF(0, 0, 1, 1), QPen(Qt.NoPen), QBrush(QColor("#000000")))
        self._gutter_rect.setZValue(2.5)
        self._gutter_rect.setAcceptedMouseButtons(Qt.NoButton)
        self._update_gutter()
        # Auto-scroll to the first item if it's off-screen.
        if self._items:
            first = self._items[0]
            first_x = max(0.0, (first.start - day_start).total_seconds() / 60.0 * ppm)
            self.view.centerOn(first_x, baseline_y)
        # Reset cursor overlay
        self._cursor_line = None
        self._cursor_label = None
        self._cursor_marker_outer = None
        self._cursor_marker_inner = None
        self._playhead_line = None
        self._playhead_label = None
        # The playhead time is kept across a redraw (Chris, 2026-09-11: the
        # green line was missing when the screen first loaded, because the
        # viewer had reported its time before the timeline drew, and the
        # redraw forgot it); the line is drawn again from it here.
        self._last_cursor_x = None
        self._update_playhead_indicator()
        self._on_vertical_scroll()  # scene rect is final now: pin the scale to the top

    def set_telemetry_summary(self, summary: Optional[tuple]) -> None:
        """(Track, unit) for the Telemetry row, or None to clear it."""
        self._telemetry_summary = summary
        if self._items and self._current_date:
            self._redraw_timeline()

    def _draw_telemetry_track(self, y_center: Optional[float], day_start: datetime, ppm: float, scene_width: float) -> None:
        if y_center is None or not self._telemetry_summary or ppm <= 0:
            return
        track, unit = self._telemetry_summary
        values = [v for v in track.values if v is not None]
        if not values:
            return
        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            lo, hi = lo - 1.0, hi + 1.0
        row_h = 18.0
        top = y_center - row_h / 2
        day0_ms = day_start.timestamp() * 1000.0
        path = QPainterPath()
        pen_down = False
        last_t = None
        for t_ms, v in zip(track.times_ms, track.values):
            if v is None:
                pen_down = False
                continue
            x = max(0.0, min(scene_width, (t_ms - day0_ms) / 60000.0 * ppm))
            y = top + row_h - (v - lo) / (hi - lo) * row_h
            # a gap longer than five minutes breaks the line (robot off)
            if pen_down and last_t is not None and t_ms - last_t > 5 * 60000:
                pen_down = False
            if pen_down:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
                pen_down = True
            last_t = t_ms
        pen = QPen(QColor("#ff8a65"))
        pen.setWidthF(1.3)
        pen.setCosmetic(True)
        item = self.scene.addPath(path, pen)
        item.setZValue(2)
        item.setAcceptedMouseButtons(Qt.NoButton)
        item.setToolTip(f"{track.name}: {lo:.0f}–{hi:.0f}{unit} through the day. Values at any time: Telemetry tab.")
        base = self.scene.addLine(0, top + row_h, scene_width, top + row_h, QPen(QColor("#3a3a3a")))
        base.setZValue(1)
        base.setAcceptedMouseButtons(Qt.NoButton)

    def _refresh_clicked(self):
        self.show_times(self._current_root, self._current_date)

    @staticmethod
    def _event_tooltip(item: TimelineItem) -> str:
        """Exact time to the millisecond, the full message, then the node
        and state it came from (Chris, 2026-09-08)."""
        import textwrap

        when = format_local_time(item.start, "%H:%M:%S.%f")[:-3]
        lines = [when]
        lines.extend(textwrap.wrap(str(item.label or ""), 110) or [""])
        payload = item.payload if isinstance(item.payload, dict) else {}
        bits = []
        source = payload.get("source")
        if source:
            bits.append("node " + str(source).rstrip("/").split("/")[-1])
        state = payload.get("state_name")
        if state:
            bits.append(f"state {state}")
        severity = payload.get("severity")
        if severity not in (None, ""):
            bits.append(f"severity {severity}")
        if bits:
            lines.append(" | ".join(bits))
        lines.extend(TimePicker._tooltip_extra_lines(item))
        lines.append("Click to open the footage and logs here")
        return "\n".join(lines)

    @staticmethod
    def _tooltip_extra_lines(item: TimelineItem) -> list[str]:
        payload = item.payload
        if not isinstance(payload, dict):
            return []
        lines: list[str] = []
        sku = payload.get("_ui_sku")
        tray = payload.get("_ui_tray")
        tool = payload.get("_ui_tool")
        if sku:
            lines.append(f"SKU: {sku}")
        if tray:
            lines.append(f"Tray: {tray}")
        if tool:
            lines.append(f"Tool: {tool}")
        return lines

    @staticmethod
    def _fit_text(text: str, max_width: float, metrics: QFontMetrics) -> str:
        if metrics.horizontalAdvance(text) <= max_width:
            return text
        ellipsis = "..."
        if metrics.horizontalAdvance(ellipsis) > max_width:
            return ""
        trimmed = text
        while trimmed and metrics.horizontalAdvance(trimmed + ellipsis) > max_width:
            trimmed = trimmed[:-1]
        return trimmed + ellipsis if trimmed else ""

    def _sku_box_text(self, item: TimelineItem, width: float) -> tuple[str, str]:
        payload = item.payload if isinstance(item.payload, dict) else {}
        font = QFont()
        font.setPointSize(9)
        metrics = QFontMetrics(font)
        is_manual = bool(payload.get("_ui_manual"))
        if is_manual:
            fitted = self._fit_text("Manual", width - 8, metrics)
            tooltip = "" if fitted == "Manual" else "Manual Mode"
            return fitted, tooltip
        sku = payload.get("_ui_sku") or item.label
        tray = payload.get("_ui_tray") or ""
        tool = payload.get("_ui_tool") or ""
        parts = [p for p in [sku, tray, tool] if p]
        full = " | ".join(parts)
        if full:
            fitted_full = self._fit_text(full, width - 8, metrics)
            if fitted_full == full:
                return fitted_full, ""
        fitted_sku = self._fit_text(str(sku), width - 8, metrics)
        tooltip_lines: list[str] = []
        if str(fitted_sku) != str(sku) and sku:
            tooltip_lines.append(f"SKU: {sku}")
        if tray:
            tooltip_lines.append(f"Tray: {tray}")
        if tool:
            tooltip_lines.append(f"Tool: {tool}")
        tooltip = "\n".join(tooltip_lines)
        return fitted_sku, tooltip

    @staticmethod
    def _is_day_rate_proxy_item(item: TimelineItem) -> bool:
        if item is None or not str(item.kind).startswith("cond_"):
            return False
        parts = [str(item.track_label or ""), str(item.label or "")]
        payload = item.payload if isinstance(item.payload, dict) else {}
        src = payload.get("_source") if isinstance(payload, dict) else None
        if isinstance(src, dict):
            parts.extend([
                str(src.get("message") or ""),
                str(src.get("state_name") or ""),
                str(src.get("source") or ""),
            ])
        haystack = " ".join(parts).lower()
        return all(term in haystack for term in DAY_RATE_PROXY_TERMS)

    def _build_day_rate_proxy_buckets(self, items: list[TimelineItem]) -> list[dict]:
        if not self._current_date:
            return []
        day_start = local_day_start_utc(self._current_date)
        day_end = day_start + timedelta(days=1)
        bucket_seconds = DAY_RATE_PROXY_BUCKET_SECONDS
        bucket_count = max(1, int((day_end - day_start).total_seconds() // bucket_seconds))
        counts = [0] * bucket_count
        matched = False
        for item in items:
            if not self._is_day_rate_proxy_item(item):
                continue
            matched = True
            offset_seconds = (ensure_utc(item.start) - day_start).total_seconds()
            if offset_seconds < 0:
                continue
            idx = int(offset_seconds // bucket_seconds)
            if 0 <= idx < bucket_count:
                counts[idx] += 1
        if not matched:
            return []
        buckets: list[dict] = []
        for idx, count in enumerate(counts):
            start = day_start + timedelta(seconds=idx * bucket_seconds)
            buckets.append({
                "start": start,
                "end": start + timedelta(seconds=bucket_seconds),
                "count": int(count),
            })
        return buckets

    @staticmethod
    def _heat_color(count: int, max_count: int, *, empty_alpha: int = 24, full_alpha: int = 220) -> QColor:
        if max_count <= 0 or count <= 0:
            return QColor(28, 44, 54, empty_alpha)
        ratio = min(1.0, max(0.0, float(count) / float(max_count)))
        ratio = math.sqrt(ratio)
        cold = QColor("#123047")
        warm = QColor("#f59e0b")
        hot = QColor("#ef4444")
        if ratio < 0.6:
            local = ratio / 0.6
            r = int(cold.red() + (warm.red() - cold.red()) * local)
            g = int(cold.green() + (warm.green() - cold.green()) * local)
            b = int(cold.blue() + (warm.blue() - cold.blue()) * local)
        else:
            local = (ratio - 0.6) / 0.4
            r = int(warm.red() + (hot.red() - warm.red()) * local)
            g = int(warm.green() + (hot.green() - warm.green()) * local)
            b = int(warm.blue() + (hot.blue() - warm.blue()) * local)
        alpha = int(empty_alpha + (full_alpha - empty_alpha) * ratio)
        return QColor(r, g, b, alpha)

    def _draw_day_rate_heat_strip(self, scene_width: float) -> None:
        if not self._target_rate_day_buckets or not self._day_start or self._ppm <= 0:
            return
        strip_y = self._scale_y + 10
        strip_h = 8
        max_count = max((int(bucket.get("count", 0)) for bucket in self._target_rate_day_buckets), default=0)
        for bucket in self._target_rate_day_buckets:
            start = ensure_utc(bucket["start"])
            end = ensure_utc(bucket["end"])
            count = int(bucket.get("count", 0) or 0)
            start_min = max(0.0, (start - self._day_start).total_seconds() / 60.0)
            end_min = max(start_min, (end - self._day_start).total_seconds() / 60.0)
            x = max(0.0, min(scene_width, start_min * self._ppm))
            width = max(1.0, min(scene_width - x, (end_min - start_min) * self._ppm))
            color = self._heat_color(count, max_count)
            rect = self.scene.addRect(QRectF(x, strip_y, width, strip_h), QPen(Qt.NoPen), QBrush(color))
            rect.setZValue(1.5)
            rect.setAcceptedMouseButtons(Qt.NoButton)
            rect.setToolTip(f"Day proxy {format_local_time(start)}  count={count}")
        # The strip carries no "Rate" label (Chris, 2026-09-11): it sat over
        # the CCTV row label and read as part of it.

    def _draw_selected_clip_rate_heat(self) -> None:
        item = self._selected_video_item
        if item is None or not self._target_rate_clip_buckets:
            return
        rect = self._video_rects.get(id(item))
        if rect is None:
            return
        clip_start = self._target_rate_clip_start or item.start
        clip_end = self._target_rate_clip_end or item.end
        if clip_start is None or clip_end is None or clip_end <= clip_start:
            return
        clip_start_utc = ensure_utc(clip_start)
        clip_end_utc = ensure_utc(clip_end)
        clip_seconds = max(1.0, (clip_end_utc - clip_start_utc).total_seconds())
        rect_geom = rect.rect()
        bar_y = rect_geom.bottom() - 6
        bar_h = 5
        max_count = max((int(bucket.get("count", 0)) for bucket in self._target_rate_clip_buckets), default=0)
        for bucket in self._target_rate_clip_buckets:
            start = max(clip_start_utc, ensure_utc(bucket["start"]))
            end = min(clip_end_utc, ensure_utc(bucket["end"]))
            if end <= start:
                continue
            start_ratio = (start - clip_start_utc).total_seconds() / clip_seconds
            end_ratio = (end - clip_start_utc).total_seconds() / clip_seconds
            x = rect_geom.x() + rect_geom.width() * start_ratio
            width = max(1.0, rect_geom.width() * (end_ratio - start_ratio))
            count = int(bucket.get("count", 0) or 0)
            color = self._heat_color(count, max_count, empty_alpha=30, full_alpha=235)
            child = QGraphicsRectItem(QRectF(x, bar_y, width, bar_h), rect)
            child.setPen(QPen(Qt.NoPen))
            child.setBrush(QBrush(color))
            child.setZValue(4)
            child.setAcceptedMouseButtons(Qt.NoButton)
            child.setToolTip(f"Clip detail {format_local_time(start)}  count={count}")

    def set_clip_target_rate_heat(
        self,
        clip_start: Optional[datetime],
        clip_end: Optional[datetime],
        buckets: list[dict] | None,
    ) -> None:
        self._target_rate_clip_start = clip_start
        self._target_rate_clip_end = clip_end
        self._target_rate_clip_buckets = list(buckets or [])
        if self._items and self._current_date:
            h_value = self.view.horizontalScrollBar().value()
            self._suppress_selection_emit = True
            try:
                self._redraw_timeline()
                self.view.horizontalScrollBar().setValue(h_value)
            finally:
                self._suppress_selection_emit = False

    def clear_clip_target_rate_heat(self) -> None:
        self.set_clip_target_rate_heat(None, None, [])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._items and self._current_date:
            self._resize_redraw_timer.start()

    def _set_busy(self, busy: bool, message: str | None = None):
        if busy == self._busy:
            return
        self._busy = busy
        if message:
            self._set_info_text(message)
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            if not self._items:
                self.view.setEnabled(False)
                self._show_loading_overlay()
            if self._progress is None:
                self._progress = QProgressDialog(message or "Loading...", None, 0, 0, self)
                self._progress.setWindowTitle("Loading timeline")
                self._progress.setWindowModality(Qt.NonModal)
                self._progress.setCancelButton(None)
                self._progress.setMinimumDuration(0)
                self._progress.setRange(0, 0)  # indefinite spinner/progress
                self._progress.show()
            QApplication.processEvents()
        else:
            QApplication.restoreOverrideCursor()
            self.view.setEnabled(True)
            if self._progress:
                self._progress.close()
                self._progress = None
            self._hide_loading_overlay()

    def _on_loaded(self, items: list[TimelineItem], day_loaded, root_loaded):
        # Ignore if stale
        if self._current_date != day_loaded or self._current_root != root_loaded:
            self._set_busy(False)
            return
        self._items = items
        self._target_rate_day_buckets = self._build_day_rate_proxy_buckets(self._items)
        self._fit_to_items()
        if items:
            self._set_info_text(f"{len(items)} items on {format_uk_date(day_loaded)}. Click timeline to select.")
        else:
            self._set_info_text(f"No files on {format_uk_date(day_loaded)}.")
        self.items_changed.emit()
        self._set_busy(False)

    def _on_partial_loaded(self, items: list[TimelineItem], day_loaded, append: bool, root_loaded):
        if self._current_date != day_loaded or self._current_root != root_loaded:
            return
        if not items:
            return
        if append:
            self._items.extend(items)
        else:
            self._items = list(items)
        self._target_rate_day_buckets = self._build_day_rate_proxy_buckets(self._items)
        self._fit_to_items()
        if self._items:
            self._set_info_text(
                f"{len(self._items)} items on {format_uk_date(day_loaded)}. Loading..."
            )
        self.items_changed.emit()
        self.view.setEnabled(True)
        self._hide_loading_overlay()

    def _on_load_failed(self, message: str):
        self._set_info_text(f"Load failed: {message}")
        self._set_busy(False)
        QMessageBox.warning(self, "Load failed", message)

    def _on_loader_warning(self, message: str):
        if not message:
            return
        QMessageBox.warning(self, "Elastic warning", message)

    def _set_info_text(self, text: str):
        if SHOW_TIMELINE_INFO_TEXT:
            self.info.setText(text)

    def shutdown_workers(self):
        """Stop background work. Called by MainWindow.closeEvent — Qt only
        delivers close events to the top-level window, so panel closeEvents
        never fire inside the app."""
        self._loader_slot.shutdown()

    def closeEvent(self, event):
        self.shutdown_workers()
        super().closeEvent(event)

    def _stop_loader_thread(self):
        self._loader_slot.retire()

    def is_loading(self) -> bool:
        return self._loader_slot.is_running()

    def _on_load_result(self, payload):
        if payload is None:
            self._set_busy(False)
            return
        items, day_loaded, root_loaded = payload
        self._on_loaded(items, day_loaded, root_loaded)

    def _on_load_progress(self, payload):
        kind = payload[0]
        if kind == "partial":
            _, items, day_loaded, append, root_loaded = payload
            self._on_partial_loaded(items, day_loaded, append, root_loaded)
        elif kind == "warning":
            self._on_loader_warning(payload[1])

    def _fit_to_items(self):
        t0 = perf_counter()
        print("[timeline] _fit_to_items", flush=True)
        if not self._items or not self._current_date:
            return
        day_start = local_day_start_utc(self._current_date)
        first = min(self._items, key=lambda i: i.start)
        last = max(self._items, key=lambda i: i.end)
        # Everything for the day, with half an hour of lead-in before the
        # first item so the start is clearly visible (Chris, 2026-09-10).
        start_min = max(0.0, (first.start - day_start).total_seconds() / 60.0 - 30.0)
        end_min = min(24 * 60.0, max(start_min + 0.1, (last.end - day_start).total_seconds() / 60.0))
        span_minutes = max(5.0, end_min - start_min)

        view_width = max(300, self.view.viewport().width() or 600)
        # Fractional scale, so a long day fits exactly rather than rounding
        # down to a whole pixel per minute and overflowing the view.
        ppm = max(view_width / (24 * 60.0), (view_width - 8) / span_minutes)
        self._ppm = ppm
        self._redraw_timeline()

        # Scroll so the span starts at the left edge of the view
        scene_width = self.scene.sceneRect().width()
        vp_width = self.view.viewport().width() or view_width
        target = start_min * self._ppm
        target = max(0.0, min(scene_width - vp_width, target))
        hbar = self.view.horizontalScrollBar()
        hbar.setValue(int(target))
        _timeline_perf_log(f"_fit_to_items redraw+center: {(perf_counter() - t0) * 1000:.0f}ms")

    def _show_loading_overlay(self):
        try:
            rect = self.scene.sceneRect()
            self._loading_rect = self.scene.addRect(rect, QPen(Qt.NoPen), QBrush(QColor(40, 40, 40, 180)))
            self._loading_rect.setZValue(10)
            self._loading_text = self.scene.addText("Loading...")
            self._loading_text.setDefaultTextColor(QColor("#ffddaa"))
            self._loading_text.setZValue(11)
            self._loading_text.setPos(rect.center().x() - 40, rect.center().y() - 10)
        except Exception:
            pass

    def _hide_loading_overlay(self):
        try:
            if self._loading_rect:
                self.scene.removeItem(self._loading_rect)
                self._loading_rect = None
            if self._loading_text:
                self.scene.removeItem(self._loading_text)
                self._loading_text = None
        except Exception:
            pass

    def _emit_selection_from_timeline(self):
        if self._suppress_selection_emit:
            return
        for item in self.scene.selectedItems():
            data = item.data(0)
            if data:
                if isinstance(data, TimelineItem) and data.kind == "video":
                    if self._selected_video_item is not data:
                        self._selected_video_item = data
                        self._apply_video_highlights()
                elif self._selected_video_item is not None:
                    self._selected_video_item = None
                    self._apply_video_highlights()
                # selectionChanged is emitted from inside the scene's mouse
                # dispatch; time_selected handlers may clear/rebuild this very
                # scene (heat-strip redraws, modal dialogs, video loads), which
                # deletes the item Qt is still dispatching on — a native
                # use-after-free crash. Deliver the signal on the next event
                # loop turn instead, coalescing rapid selections.
                self._pending_time_selected = data
                if not self._time_selected_emit_scheduled:
                    self._time_selected_emit_scheduled = True
                    QTimer.singleShot(0, self._flush_pending_time_selected)
                break

    def _flush_pending_time_selected(self):
        self._time_selected_emit_scheduled = False
        data = self._pending_time_selected
        self._pending_time_selected = None
        if data is not None:
            self.time_selected.emit(data)

    def _place_bars_menu_btn(self) -> None:
        btn = getattr(self, "_bars_menu_btn", None)
        if btn is None:
            return
        btn.adjustSize()
        btn.move(max(0, self.view.viewport().width() - btn.width() - 6), 6)
        btn.raise_()
        x = btn.x() - 6
        for zb in reversed(getattr(self, "_zoom_btns", [])):
            x -= zb.width()
            zb.move(max(0, x), 6 + (btn.height() - zb.height()) // 2)
            zb.raise_()
            x -= 4

    def _rebuild_bars_menu(self) -> None:
        """One tick per bar: the static rows, Telemetry, then the condition
        rows (the same ticks as the Errors box)."""
        menu = self._bars_menu
        menu.clear()
        counts: Dict[str, int] = {}
        for item in self._items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        for kind, label in (("video", "CCTV"), ("additional", "Additional CCTV"), ("sku", "SKU"), ("shared", "Start / Stop / E-stop")):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(kind not in self._hidden_bars)
            action.toggled.connect(lambda on, k=kind: self._on_bar_toggled(k, on))
            menu.addAction(action)
        telemetry = QAction("Telemetry", menu)
        telemetry.setCheckable(True)
        telemetry.setChecked(self._show_telemetry_row)
        telemetry.toggled.connect(self._on_telemetry_row_toggled)
        menu.addAction(telemetry)
        conds = list(getattr(self.settings, "conditions", None) or [])
        rows = []
        for idx, cond in enumerate(conds):
            name = (cond.name or "").strip()
            if not getattr(cond, "query", "") or not name or name.lower() in SHARED_ROW_NAMES:
                continue
            rows.append((name, counts.get(f"cond_{idx}", 0)))
        if rows:
            menu.addSeparator()
            for name, count in rows:
                action = QAction(f"{name}  ({count})", menu)
                action.setCheckable(True)
                action.setChecked(self._row_visible(name, count))
                action.toggled.connect(lambda on, n=name: self._on_error_row_toggled(n, on))
                menu.addAction(action)

    def _on_bar_toggled(self, kind: str, on: bool) -> None:
        if on:
            self._hidden_bars.discard(kind)
        else:
            self._hidden_bars.add(kind)
        update_ui_state({"replay_bars_hidden": sorted(self._hidden_bars)})
        self._schedule_redraw()

    def eventFilter(self, obj, event):
        if obj is self.view and event.type() == QEvent.Resize:
            self._place_bars_menu_btn()
            return False
        if obj is self.view.viewport():
            if event.type() == QEvent.Wheel and self._handle_wheel(event):
                return True
            if event.type() == QEvent.MouseButtonPress and self._day_start is not None:
                pos = self._event_viewport_pos(event)
                self._press_pos = pos
                if pos is not None and self._ppm:
                    minute = max(0.0, min(24 * 60.0, self.view.mapToScene(pos).x() / self._ppm))
                    self.last_click_time = self._day_start + timedelta(minutes=minute)
            if self._signals.handle_resize(event):
                return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and self._day_start is not None:
                # A click anywhere on the chart that is not on a clip, SKU
                # box or event tick sets the time (Chris, 2026-09-12).
                pos = self._event_viewport_pos(event)
                press = getattr(self, "_press_pos", None)
                if pos is not None and press is not None and (pos - press).manhattanLength() < 4 and self._ppm:
                    hit = self.view.itemAt(pos)
                    while hit is not None and not isinstance(hit.data(0), TimelineItem):
                        hit = hit.parentItem()
                    if hit is None:
                        minute = max(0.0, min(24 * 60.0, self.view.mapToScene(pos).x() / self._ppm))
                        self.moment_clicked.emit(self._day_start + timedelta(minutes=minute))
            if event.type() == QEvent.MouseMove:
                self._update_cursor_indicator(event)
        return super().eventFilter(obj, event)

    # ---- wheel: scroll along the day, Ctrl to zoom (Chris, 2026-09-10) ----
    def _handle_wheel(self, event) -> bool:
        """Plain wheel zooms about the cursor, the same as the + and -
        buttons (Chris, 2026-09-13; it scrolled up and down before).
        Shift+wheel scrolls left and right along the day, Ctrl+wheel
        scrolls up and down."""
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return False
        mods = event.modifiers()
        if not (mods & (Qt.ControlModifier | Qt.ShiftModifier)):
            pos = self._event_viewport_pos(event)
            self._zoom_about(1.25 if delta > 0 else 1 / 1.25, pos)
            return True
        bar = self.view.horizontalScrollBar() if mods & Qt.ShiftModifier else self.view.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False
        bar.setValue(bar.value() - int(delta / 120 * max(40, self.view.viewport().width() // 8)))
        return True

    def _zoom_about(self, factor: float, viewport_pos) -> None:
        """Change the pixels-per-minute scale, keeping the moment under the
        cursor where it is. Zoomed out no further than the whole day fitting
        the view; zoomed in no further than one second per pixel."""
        if not self._items or not self._current_date:
            return
        vp_width = max(1, self.view.viewport().width())
        min_ppm = max(1.0, vp_width / (24 * 60))
        new_ppm = max(min_ppm, min(60.0, self._ppm * factor))
        if abs(new_ppm - self._ppm) < 1e-6:
            return
        hbar = self.view.horizontalScrollBar()
        anchor_view_x = float(viewport_pos.x()) if viewport_pos is not None else vp_width / 2.0
        anchor_minute = (hbar.value() + anchor_view_x) / self._ppm
        self._ppm = new_ppm
        self._redraw_timeline()
        hbar.setValue(int(round(anchor_minute * new_ppm - anchor_view_x)))

    # ---- reading strips: the owner interface for SignalChannel -------------
    def _line_bottom(self) -> float:
        candidates = [self._baseline_y + 14]
        if getattr(self, "_tracks_bottom", None) is not None:
            candidates.append(self._tracks_bottom)
        if self._strips_bottom is not None:
            candidates.append(self._strips_bottom)
        return max(candidates)

    def hide_thumbnail_preview(self) -> None:
        pass

    def signal_boxes_widget(self) -> QWidget:
        """The Data and Additional data boxes, for the main window to mount
        under the log tabs (Chris, 2026-09-08)."""
        holder = _PinnedHeightPanel()
        holder.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._side_panel = holder
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(self._errors_box)
        column.addLayout(self._signals.row_layout())
        # Normal-operation conditions (eject crate) sit under the readings in
        # the Data box, each with a tick and the day's total.
        self._normal_rows_layout = QVBoxLayout()
        self._normal_rows_layout.setContentsMargins(0, 1, 0, 0)
        self._normal_rows_layout.setSpacing(0)
        box_layout = self._signals.data_box.body.layout()
        if box_layout is not None:
            box_layout.addLayout(self._normal_rows_layout)
        column.addWidget(self.status_label)
        self.status_label.setVisible(False)
        # Telemetry row as an Additional data option (Chris, 2026-09-11).
        menu = self._signals.additional_btn.menu()
        if menu is not None:
            telemetry_action = QAction("Telemetry row on the timeline", self)
            telemetry_action.setCheckable(True)
            telemetry_action.setChecked(self._show_telemetry_row)
            telemetry_action.toggled.connect(self._on_telemetry_row_toggled)
            first = menu.actions()[0] if menu.actions() else None
            menu.insertAction(first, telemetry_action)
            menu.insertSeparator(first)
        # The arrows after the titles fold each box away (Chris, 2026-09-11);
        # Additional data has no arrow and follows the Data box.
        self._errors_box.set_collapsible("errors")
        self._signals.data_box.set_collapsible("data")
        self._signals.data_box.collapsed_changed.connect(lambda on: self._signals.additional_box.set_collapsed(on, remember=False))
        self._signals.additional_box.set_collapsed(self._signals.data_box.is_collapsed(), remember=False)
        holder.pin_height()
        return holder

    @staticmethod
    def _shared_row_html(shared_kinds: list, label_map: Dict[str, str], color_map: Dict[str, QColor]) -> str:
        short = {"start": "Start", "operator stop": "Stop", "estop": "E-stop", "e-stop": "E-stop"}
        order = ["start", "operator stop", "estop", "e-stop"]
        parts = []
        for want in order:
            for kind in shared_kinds:
                name = str(label_map.get(kind, "")).strip().lower()
                if name == want:
                    colour = color_map.get(kind)
                    hexc = colour.name() if isinstance(colour, QColor) else "#cccccc"
                    parts.append(f'<span style="color:{hexc}">{short.get(name, name)}</span>')
        return ' <span style="color:#777777">/</span> '.join(parts)

    # ---- Errors box ---------------------------------------------------------
    def _row_visible(self, name: str, count: int) -> bool:
        key = name.strip().lower()
        if key in self._row_overrides:
            return self._row_overrides[key]
        if key in NORMAL_CONDITION_NAMES:
            return count > 0
        return count > 1

    def _refresh_errors_box(self, label_map: Dict[str, str], color_map: Dict[str, QColor], counts: Dict[str, int]) -> None:
        """One line per condition: a tick (show the row), the name in its
        colour, the day's total. Conditions come from Settings when known,
        so a condition with no events today still appears with 0."""
        rows = []
        conds = list(getattr(self.settings, "conditions", None) or [])
        if conds:
            for idx, cond in enumerate(conds):
                if not getattr(cond, "query", ""):
                    continue
                kind = f"cond_{idx}"
                rows.append((kind, cond.name or f"Cond {idx + 1}", cond.color or "#cccccc", counts.get(kind, 0)))
        else:
            for kind, label in label_map.items():
                if kind.startswith("cond_"):
                    c = color_map.get(kind)
                    rows.append((kind, label, c.name() if isinstance(c, QColor) else "#cccccc", counts.get(kind, 0)))
        if rows == self._last_condition_rows:
            return
        self._last_condition_rows = rows
        normal_rows = [r for r in rows if r[1].strip().lower() in NORMAL_CONDITION_NAMES]
        rows = [r for r in rows if r[1].strip().lower() not in NORMAL_CONDITION_NAMES]
        self._errors_box_updating = True
        try:
            while self._errors_grid.count():
                w = self._errors_grid.takeAt(0).widget()
                if w is not None:
                    w.deleteLater()
            self._error_checks = {}
            if self._normal_rows_layout is not None:
                while self._normal_rows_layout.count():
                    item = self._normal_rows_layout.takeAt(0)
                    sub = item.layout()
                    if sub is not None:
                        while sub.count():
                            w = sub.takeAt(0).widget()
                            if w is not None:
                                w.deleteLater()
                    elif item.widget() is not None:
                        item.widget().deleteLater()
                for kind, name, color, count in normal_rows:
                    line = QHBoxLayout()
                    line.setSpacing(6)
                    cb = QCheckBox(name)
                    cb.setChecked(self._row_visible(name, count))
                    cb.setStyleSheet(f"color: {color}; font-size: {COMPACT_FONT_PX}px;")
                    cb.setToolTip(f"Show the {name} row on the timeline (normal operation)")
                    cb.toggled.connect(lambda on, n=name: self._on_error_row_toggled(n, on))
                    total = QLabel(str(count))
                    total.setStyleSheet(f"color: {color}; font-weight: 600; font-size: {COMPACT_FONT_PX}px;")
                    line.addWidget(cb)
                    line.addWidget(total)
                    line.addStretch(1)
                    self._normal_rows_layout.addLayout(line)
                    self._error_checks[name] = cb
            # Three columns of (tick, total) pairs (Chris, 2026-09-11; two
            # since 2026-09-10) so the box stays short; filled down the
            # first column, then the second, then the third.
            per_col = max(1, (len(rows) + 2) // 3)
            for i, (kind, name, color, count) in enumerate(rows):
                col, r = 2 * (i // per_col), i % per_col
                cb = QCheckBox(name)
                cb.setChecked(self._row_visible(name, count))
                cb.setStyleSheet(f"color: {color}; font-size: {COMPACT_FONT_PX}px;")
                cb.setToolTip(f"Show the {name} row on the timeline")
                cb.toggled.connect(lambda on, n=name: self._on_error_row_toggled(n, on))
                total = QLabel(str(count))
                total.setStyleSheet(f"color: {color}; font-weight: 600; font-size: {COMPACT_FONT_PX}px;")
                total.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                total.setMinimumWidth(26)
                self._errors_grid.addWidget(cb, r, col)
                self._errors_grid.addWidget(total, r, col + 1)
                self._error_checks[name] = cb
            for col in (0, 2, 4):
                self._errors_grid.setColumnStretch(col, 1)
                self._errors_grid.setColumnMinimumWidth(col + 1, 26)
            self._errors_grid.setHorizontalSpacing(6)
            self._errors_box.setVisible(bool(rows))
        finally:
            self._errors_box_updating = False
        if getattr(self, "_side_panel", None) is not None:
            QTimer.singleShot(0, self._pin_side_panel)

    def _pin_side_panel(self) -> None:
        """Re-pin the side panel's height once the new rows are in place;
        the boxes' geometry is refreshed first so the pin sees them."""
        panel = getattr(self, "_side_panel", None)
        if panel is None:
            return
        try:
            self._errors_box.updateGeometry()
            self._signals.data_box.updateGeometry()
            panel.pin_height()
        except RuntimeError:
            pass

    def _on_telemetry_row_toggled(self, on: bool) -> None:
        self._show_telemetry_row = bool(on)
        update_ui_state({"replay_telemetry_row": self._show_telemetry_row})
        self._schedule_redraw()

    def _on_error_row_toggled(self, name: str, on: bool) -> None:
        if self._errors_box_updating:
            return
        self._row_overrides[name.strip().lower()] = bool(on)
        update_ui_state({"replay_rows": dict(self._row_overrides)})
        cb = self._error_checks.get(name)
        if cb is not None and cb.isChecked() != bool(on):
            self._errors_box_updating = True
            try:
                cb.setChecked(bool(on))
            finally:
                self._errors_box_updating = False
        self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        if self._items and self._current_date:
            self._redraw_timeline()

    def _redraw(self) -> None:
        self._schedule_redraw()

    def _maybe_fetch_signals(self, force: bool = False) -> None:
        if self.settings is None or self._current_date is None or not self._signals.any_active:
            return
        if not grafana_client.is_configured(self.settings):
            self.status_label.setText("Readings need Grafana: gear menu, Data sources")
            self.status_label.setVisible(True)
            return
        self.status_label.setText("")
        self.status_label.setVisible(False)
        start = local_day_start_utc(self._current_date)
        now_utc = datetime.now(timezone.utc)
        end = min(now_utc, start + timedelta(days=1))
        live = self._current_date == datetime.now().date()
        self._signals.maybe_fetch((start, max(end, start + timedelta(minutes=1))), live, timedelta(minutes=5), datetime.now().astimezone(), force)

    def _update_cursor_indicator(self, event):
        if not self._day_start or self._busy:
            return
        self._ensure_cursor_items_valid()
        viewport_pos = self._event_viewport_pos(event)
        if viewport_pos is None:
            return
        scene_pos = self.view.mapToScene(viewport_pos)
        minute = max(0.0, min(24 * 60, scene_pos.x() / self._ppm))
        cursor_time = self._day_start + timedelta(minutes=minute)
        x = minute * self._ppm

        # Draw/update cursor line
        if self._cursor_line is None:
            pen = QPen(QColor("#ff9900"))
            pen.setWidth(1)
            self._cursor_line = self.scene.addLine(x, self._scale_y, x, self._line_bottom(), pen)
            self._cursor_line.setZValue(3)
        else:
            self._cursor_line.setLine(x, self._scale_y, x, self._line_bottom())
        self._signals.clear_hover()
        self._signals.draw_hover(x, cursor_time, float(scene_pos.y()), 0.0, 24 * 60 * self._ppm, self._scale_y)

        # Draw/update cursor label above the ticks
        label_text = format_local_time(cursor_time)
        if self._cursor_label is None:
            self._cursor_label = self.scene.addText(label_text)
            self._cursor_label.setDefaultTextColor(QColor("#ffddaa"))
            self._cursor_label.setZValue(7)
        else:
            self._cursor_label.setPlainText(label_text)
        self._cursor_label.setPos(x + 4, self._scale_y + self._scale_shift - 26)

        marker_center_y = self._scale_y + self._scale_shift - 2
        outer_radius = 6
        inner_radius = 3
        outer_rect = QRectF(
            x - outer_radius,
            marker_center_y - outer_radius,
            outer_radius * 2,
            outer_radius * 2,
        )
        inner_rect = QRectF(
            x - inner_radius,
            marker_center_y - inner_radius,
            inner_radius * 2,
            inner_radius * 2,
        )
        if self._cursor_marker_outer is None:
            self._cursor_marker_outer = self.scene.addEllipse(
                outer_rect,
                QPen(QColor("#000000")),
                QBrush(QColor("#000000")),
            )
            self._cursor_marker_outer.setZValue(8)
        else:
            self._cursor_marker_outer.setRect(outer_rect)
        if self._cursor_marker_inner is None:
            self._cursor_marker_inner = self.scene.addEllipse(
                inner_rect,
                QPen(QColor("#ffffff")),
                QBrush(QColor("#ffffff")),
            )
            self._cursor_marker_inner.setZValue(9)
        else:
            self._cursor_marker_inner.setRect(inner_rect)
        self._last_cursor_x = x
        self._reposition_track_labels(cursor_x=x)

    @staticmethod
    def _event_viewport_pos(event) -> QPointF | None:
        if hasattr(event, "position"):
            try:
                pos = event.position()
                if pos is not None:
                    return pos.toPoint()
            except Exception:
                pass
        if hasattr(event, "pos"):
            try:
                return event.pos()
            except Exception:
                pass
        return None

    def set_playhead_datetime(self, dt: Optional[datetime]):
        if _DEBUG_PLAYHEAD:
            print(f"[playhead] set {dt!r} day={self._current_date} day_start={self._day_start}", flush=True)
        self._playhead_time = dt
        if self._playhead_line is None and dt is None:
            return
        self._update_playhead_indicator()

    def _ensure_cursor_items_valid(self):
        for attr in (
            "_cursor_line",
            "_cursor_label",
            "_cursor_marker_outer",
            "_cursor_marker_inner",
        ):
            item = getattr(self, attr, None)
            if item is None:
                continue
            try:
                if item.scene() is None:
                    setattr(self, attr, None)
            except RuntimeError:
                setattr(self, attr, None)

    def _remove_playhead_items(self) -> None:
        for attr in ("_playhead_line", "_playhead_label"):
            item = getattr(self, attr, None)
            if item is not None:
                try:
                    self.scene.removeItem(item)
                except Exception:
                    pass
            setattr(self, attr, None)

    def _playhead_label_y(self) -> float:
        # Just under the tick marks, at the top of the green line; moves
        # with the pinned time scale.
        return self._scale_y + self._scale_shift + 10

    def _update_playhead_indicator(self):
        for attr in ("_playhead_line", "_playhead_label"):
            item = getattr(self, attr, None)
            if item is not None:
                try:
                    if item.scene() is None:
                        setattr(self, attr, None)
                except RuntimeError:
                    setattr(self, attr, None)
        if not self._day_start or not self._current_date or self._playhead_time is None:
            if _DEBUG_PLAYHEAD:
                print(f"[playhead] hidden: day_start={self._day_start} date={self._current_date} time={self._playhead_time!r}", flush=True)
            self._remove_playhead_items()
            return
        play_local = ensure_playhead_local(self._playhead_time)
        if play_local.date() != self._current_date:
            if _DEBUG_PLAYHEAD:
                print(f"[playhead] hidden: local date {play_local.date()} != timeline date {self._current_date}", flush=True)
            self._remove_playhead_items()
            return
        play_dt = ensure_utc(play_local)
        minute = max(0.0, min(24 * 60, (play_dt - self._day_start).total_seconds() / 60.0))
        x = minute * self._ppm
        if self._playhead_line is None:
            pen = QPen(QColor("#2ecc71"))
            pen.setWidth(2)
            self._playhead_line = self.scene.addLine(x, self._scale_y, x, self._line_bottom(), pen)
            self._playhead_line.setZValue(3)
        else:
            self._playhead_line.setLine(x, self._scale_y, x, self._line_bottom())
        # The current time, hours and minutes, at the top of the line
        # (Chris, 2026-09-11).
        text = play_local.strftime("%H:%M")
        if self._playhead_label is None:
            font = QFont()
            font.setPointSize(8)
            font.setBold(True)
            self._playhead_label = self.scene.addText(text, font)
            self._playhead_label.setDefaultTextColor(QColor("#2ecc71"))
            self._playhead_label.setZValue(8)
            self._playhead_label.setAcceptedMouseButtons(Qt.NoButton)
            add_label_backdrop(self._playhead_label)
        elif self._playhead_label.toPlainText() != text:
            self._playhead_label.setPlainText(text)
        self._playhead_label.setPos(x + 3, self._playhead_label_y())

    def _on_vertical_scroll(self) -> None:
        """Keep the time scale at the top of the view whatever the vertical
        scroll, and move the cursor marker with it."""
        # Pin the band's top edge to the top of the viewport, so there is no
        # gap above the scale whether scrolled or not (Chris, 2026-09-10).
        try:
            top = float(self.view.mapToScene(0, 0).y())
        except Exception:
            top = float(self._scale_y - 30)
        self._scale_shift = top - (self._scale_y - 30)
        if self._scale_group is not None:
            try:
                self._scale_group.setPos(0, self._scale_shift)
            except RuntimeError:
                self._scale_group = None
        if self._playhead_label is not None:
            try:
                self._playhead_label.setPos(self._playhead_label.pos().x(), self._playhead_label_y())
            except RuntimeError:
                self._playhead_label = None
        for item, dy in ((self._cursor_label, -26), (self._cursor_marker_outer, None), (self._cursor_marker_inner, None)):
            if item is None:
                continue
            try:
                if dy is not None:
                    item.setPos(item.pos().x(), self._scale_y + self._scale_shift + dy)
                else:
                    r = item.rect()
                    centre = self._scale_y + self._scale_shift - 2
                    item.setRect(r.x(), centre - r.height() / 2, r.width(), r.height())
            except RuntimeError:
                pass

    def _reposition_track_labels(self, cursor_x: Optional[float] = None):
        if not self._track_positions:
            return
        if cursor_x is None:
            cursor_x = self._last_cursor_x
        if cursor_x is None:
            cursor_x = 0.0
        h_offset = self.view.horizontalScrollBar().value()
        left_x = h_offset + 4.0
        right_x = h_offset + max(120.0, self.view.viewport().width() - 20.0)
        # Reading-strip titles and axis labels follow the scroll too.
        for channel in self._signals.channels:
            for item, kind, y in channel.label_items:
                try:
                    if item.scene() is None:
                        continue
                    item.setPos(left_x if kind == "title" else left_x + 74, y)
                except RuntimeError:
                    continue
        for kind, y in self._track_positions.items():
            label_item = self._track_labels.get(kind)
            if label_item:
                try:
                    h = label_item.boundingRect().height()
                    # If item was deleted, boundingRect may raise.
                    if label_item.scene() is None:
                        continue
                    label_item.setPos(left_x, y - h / 2)
                except RuntimeError:
                    continue
            count_item = self._track_counts.get(kind)
            if count_item:
                try:
                    rect = count_item.boundingRect()
                    h2 = rect.height()
                    if count_item.scene() is None:
                        continue
                    count_item.setPos(right_x - rect.width(), y - h2 / 2)
                except RuntimeError:
                    continue
        self._update_gutter()

    def _update_gutter(self) -> None:
        """Size and place the label gutter: the viewport's left edge, the
        scene's full height, as wide as the widest row or strip label."""
        gutter = self._gutter_rect
        if gutter is None:
            return
        try:
            if gutter.scene() is None:
                self._gutter_rect = None
                return
        except RuntimeError:
            self._gutter_rect = None
            return
        widest = 0.0
        for label_item in list(self._track_labels.values()):
            try:
                if label_item.scene() is not None and label_item.toPlainText().strip():
                    widest = max(widest, label_item.boundingRect().width())
            except RuntimeError:
                continue
        for channel in self._signals.channels:
            for item, kind, _y in channel.label_items:
                if kind != "title":
                    continue
                try:
                    if item.scene() is not None:
                        widest = max(widest, item.boundingRect().width())
                except RuntimeError:
                    continue
        if widest <= 0:
            gutter.setVisible(False)
            return
        gutter.setVisible(True)
        h_offset = self.view.horizontalScrollBar().value()
        scene_rect = self.scene.sceneRect()
        gutter.setRect(QRectF(h_offset, scene_rect.top(), widest + 6, scene_rect.height()))

    def collect_event_markers(self, video_item: TimelineItem) -> list[tuple[float, str]]:
        markers: list[tuple[float, str]] = []
        if video_item is None or video_item.start is None or video_item.end is None:
            return markers
        for item in self._items:
            if item.kind == "video":
                continue
            if item.start is None or item.end is None:
                continue
            if item.end <= video_item.start or item.start >= video_item.end:
                continue
            ts = max(item.start, video_item.start)
            offset_sec = (ts - video_item.start).total_seconds()
            color_val = item.color
            if isinstance(color_val, QColor):
                color_hex = color_val.name()
            else:
                color_hex = str(color_val or "#ffffff")
            markers.append((offset_sec, color_hex))
        markers.sort(key=lambda t: t[0])
        return markers

    def video_paths(self) -> list[Path]:
        """All video clip paths on the loaded day, in timeline order."""
        items = [
            itm for itm in self._items
            if itm.kind == "video" and isinstance(itm.payload, Path)
        ]
        items.sort(key=lambda itm: itm.start)
        return [itm.payload for itm in items]

    def get_adjacent_video_items(self, current_item: TimelineItem) -> tuple[TimelineItem | None, TimelineItem | None]:
        if current_item is None or current_item.kind != "video":
            return None, None
        if not self._items:
            return None, None
        current_key = current_item.path_key
        if current_key is None and isinstance(current_item.payload, Path):
            current_key = _path_key(current_item.payload)
        video_items = [itm for itm in self._items if itm.kind == "video"]
        if not video_items:
            return None, None
        video_items.sort(key=lambda itm: itm.start)
        idx = None
        if current_key is not None:
            for i, itm in enumerate(video_items):
                if itm.path_key is None and isinstance(itm.payload, Path):
                    itm.path_key = _path_key(itm.payload)
                if itm.path_key == current_key:
                    idx = i
                    break
        if idx is None:
            try:
                idx = video_items.index(current_item)
            except ValueError:
                return None, None
        prev_item = video_items[idx - 1] if idx > 0 else None
        next_item = video_items[idx + 1] if idx + 1 < len(video_items) else None
        return prev_item, next_item

    def mark_video_cached(self, video_path: Path):
        if not self._cache_root or not self._items:
            return
        target_key = _path_key(video_path)
        for item in self._items:
            if item.kind != "video" or not isinstance(item.payload, Path):
                continue
            if item.path_key is None:
                item.path_key = _path_key(item.payload)
            if item.path_key == target_key and not item.cached:
                item.cached = True
                item.color = VIDEO_COLOR_CACHED
                rect = self._video_rects.get(id(item))
                if rect:
                    rect.setBrush(QBrush(self._color_for_video_item(item)))
                return

    def mark_video_annotated(self, video_path: Path, annotated: bool):
        if not self._items:
            return
        changed = False
        for item in self._items:
            if item.kind != "video" or not isinstance(item.payload, Path):
                continue
            if item.payload == video_path:
                if item.annotated != annotated:
                    item.annotated = annotated
                    changed = True
                break
        if changed:
            self._redraw_timeline()

    def _apply_video_highlights(self):
        if not self._video_rects:
            return
        for item in self._items:
            if item.kind != "video":
                continue
            rect = self._video_rects.get(id(item))
            if not rect:
                continue
            rect.setBrush(QBrush(self._color_for_video_item(item)))

    def _add_pen_icon(self, rect: QGraphicsRectItem):
        if rect.rect().width() < 12:
            return
        color = QColor("#ffcc00")
        pen = QPen(color)
        pen.setWidth(2)
        x = rect.rect().x() + rect.rect().width() - 12
        y = rect.rect().y() + 4
        line = QGraphicsLineItem(x, y + 6, x + 6, y, rect)
        line.setPen(pen)
        tip = QPolygonF([
            QPointF(x + 6, y),
            QPointF(x + 10, y + 2),
            QPointF(x + 8, y + 6),
        ])
        tri = QGraphicsPolygonItem(tip, rect)
        tri.setPen(pen)
        tri.setBrush(color)
        line.setZValue(3)
        tri.setZValue(3)

    def _color_for_video_item(self, item: TimelineItem) -> QColor:
        if self._selected_video_item is item:
            return QColor(VIDEO_COLOR_SELECTED)
        color = item.color
        if isinstance(color, QColor):
            return QColor(color)
        if not color:
            color = VIDEO_COLOR_UNCACHED
        return QColor(color)



def _load_timeline_items(job, root: Path, day: date, load_func, extra_loaders, cache_root: Optional[Path]):
    """Worker for the day timeline: the Elastic extra loaders run
    CONCURRENTLY with the video scan of the share (each emits a partial as
    it completes; all partials append, and show_times cleared the items).
    The loaders receive a zero-arg resolver for the day's last-video-end —
    fetch_sku_items needs that value only for its final band-capping step,
    so its Elastic queries overlap the scan and the resolver blocks only
    if the scan hasn't finished by then. Progress payloads are tagged
    tuples: ("partial", items, day, append, root) and ("warning", message).
    """
    t_total_start = perf_counter()

    last_video_end_box: dict[str, object] = {}
    last_video_end_ready = threading.Event()

    def _resolve_last_video_end():
        last_video_end_ready.wait()
        return last_video_end_box.get("value")

    executor: ThreadPoolExecutor | None = None
    future_to_name: dict = {}
    future_to_start: dict = {}
    extra_started = perf_counter()
    if extra_loaders:
        executor = ThreadPoolExecutor(max_workers=max(1, len(extra_loaders)))
        for loader in extra_loaders:
            future = executor.submit(loader, root, day, _resolve_last_video_end)
            future_to_name[future] = getattr(loader, "__name__", repr(loader))
            future_to_start[future] = perf_counter()

    try:
        return _scan_videos_and_collect(
            job,
            root,
            day,
            load_func,
            cache_root,
            t_total_start,
            last_video_end_box,
            last_video_end_ready,
            future_to_name,
            future_to_start,
            extra_started,
        )
    finally:
        # Never leave a loader blocked on the resolver (early return on
        # interruption, or a scan failure); abandoned loaders finish on
        # their own request timeouts.
        last_video_end_ready.set()
        if executor is not None:
            executor.shutdown(wait=False)


def _scan_videos_and_collect(
    job,
    root: Path,
    day: date,
    load_func,
    cache_root: Optional[Path],
    t_total_start: float,
    last_video_end_box: dict,
    last_video_end_ready: threading.Event,
    future_to_name: dict,
    future_to_start: dict,
    extra_started: float,
):
    t_video_start = perf_counter()
    paths = list(load_func(root, day))
    cache_index = _build_cache_index(cache_root) if cache_root else set()
    ann_index = _build_annotation_index(cache_root) if cache_root else set()
    video_entries: list[tuple[Path, datetime]] = []
    stat_fallback_count = 0
    stat_fallback_ms = 0.0
    for p in paths:
        if job.interrupted():
            return None
        parsed_dt = parse_time_from_name(p)
        if parsed_dt is not None:
            start_dt = parsed_dt
        else:
            t_stat = perf_counter()
            try:
                stat = p.stat()
            except FileNotFoundError:
                continue
            stat_fallback_ms += (perf_counter() - t_stat) * 1000.0
            stat_fallback_count += 1
            start_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        video_entries.append((p, ensure_utc(start_dt)))

    video_entries.sort(key=lambda tpl: tpl[1])
    items: list[TimelineItem] = []
    for idx, (path_obj, start_dt) in enumerate(video_entries):
        if idx + 1 < len(video_entries):
            next_start = video_entries[idx + 1][1]
            end_dt = next_start
            if (end_dt - start_dt) < MIN_BLOCK_DURATION:
                end_dt = start_dt + MIN_BLOCK_DURATION
        else:
            end_dt = inferred_live_clip_end(path_obj, start_dt)
        cached = _is_path_cached(path_obj, cache_root, cache_index) if cache_root else False
        annotated = _has_annotations(path_obj, cache_root, ann_index) if cache_root else False
        items.append(
            TimelineItem(
                start=start_dt,
                end=end_dt,
                label=path_obj.name,
                kind="video",
                color=VIDEO_COLOR_CACHED if cached else VIDEO_COLOR_UNCACHED,
                payload=path_obj,
                track_label="CCTV",
                cached=bool(cached),
                annotated=bool(annotated),
                path_key=_path_key(path_obj),
            )
        )
    # The end of the last clip (inferred_live_clip_end) — published to the
    # extra loaders' resolver so fetch_sku_items need not re-list this same
    # day folder on the share (~5s per scan on the WAN share).
    last_video_end_box["value"] = items[-1].end if items else None
    last_video_end_ready.set()
    if items:
        job.emit_progress(("partial", items, day, True, root))
    _timeline_perf_log(
        f"video loader: files={len(paths)} items={len(items)} "
        f"time={(perf_counter() - t_video_start) * 1000:.0f}ms"
    )
    _timeline_perf_log(
        f"video loader stat fallback: count={stat_fallback_count} "
        f"time={stat_fallback_ms:.0f}ms"
    )

    # Collect the extra loaders (e.g., Elastic) that have been running since
    # before the scan. Each loader already enforces its own HTTP/request
    # timeout so we don't apply another hard timeout here; that was causing
    # results to be dropped for larger queries that legitimately take longer
    # than a few seconds.
    warnings: list[str] = []
    if future_to_name:
        for fut in as_completed(future_to_name):
            if job.interrupted():
                return None
            loader_name = future_to_name.get(fut, "extra_loader")
            t_loader = future_to_start.get(fut, perf_counter())
            try:
                res = fut.result()
            except ElasticFetchError as exc:
                warnings.append(str(exc))
                res = exc.items
            except Exception as exc:
                warnings.append(str(exc))
                continue
            _timeline_perf_log(
                f"extra loader {loader_name}: items={len(res) if res else 0} "
                f"time={(perf_counter() - t_loader) * 1000:.0f}ms"
            )
            if res:
                for itm in res:
                    items.append(itm)
                job.emit_progress(("partial", list(res), day, True, root))
        _timeline_perf_log(f"extra loaders total: {(perf_counter() - extra_started) * 1000:.0f}ms")

    t_finalize = perf_counter()
    for itm in items:
        itm.start = ensure_utc(itm.start)
        itm.end = ensure_utc(itm.end)
    items.sort(key=lambda s: s.start)
    _timeline_perf_log(f"finalize+sort: {(perf_counter() - t_finalize) * 1000:.0f}ms")
    if job.interrupted():
        return None
    if warnings:
        for msg in warnings:
            job.emit_progress(("warning", msg))
    _timeline_perf_log(f"load thread total: {(perf_counter() - t_total_start) * 1000:.0f}ms")
    return items, day, root
