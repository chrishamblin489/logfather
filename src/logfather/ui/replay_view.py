import sys
import os
import subprocess
import shutil
import argparse
import time
import json
import re
import tempfile
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from datetime import timedelta, datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from logfather.data.settings_store import Settings, DEFAULT_SETTINGS_PATH, CustomFilterPreset, FilterPreset
from logfather.data.elastic_loader import fetch_logs_for_range
from logfather.data.elastic_errors import ElasticFetchError
from logfather.ui.app_assets import load_placeholder_image as _load_placeholder_image
from logfather.core.frame_analysis import (
    compute_optical_flow_view,
    compute_pixel_diff_view,
)
from logfather.ui.annotated_video_widget import AnnotatedVideoWidget
from logfather.data.clip_cache import ClipCache
from logfather.data.ocr_offset_store import OcrOffsetStore
from logfather.data.ui_state_store import load_ui_state, update_ui_state
from logfather.core.time_alignment import plausible_ocr_offset, TimeAlignment
from logfather.ui import theme
from logfather.ui.icons import sync_icon
from logfather.ui.pulse import Pulser
from logfather.core.log_events import (
    LOCAL_TIMEZONE,
    MESSAGE_COLUMN,
    SOURCE_COLUMN,
    STATE_COLUMN,
    LogEvent,
    build_events_from_rows,
    format_timecode,
    _to_local_naive,
)
from logfather.ui.viewer_widgets import (
    ClipRangeSlider,
    DriftSlider,
    EventMarkerBar,
    LogListModel,
    SegmentDisplay,
    VideoFrameLabel,
)

import cv2
from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QMetaObject, Slot, QPoint, QPointF, QSize, Q_ARG, QVariantAnimation, QEasingCurve, QModelIndex
from PySide6.QtGui import QAction, QImage, QColor, QPainter, QPalette, QPixmap
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QFileDialog, QMessageBox,
    QSlider, QSizePolicy, QListView, QAbstractItemView,
    QCheckBox, QScrollArea, QProgressDialog, QTabWidget, QDialog,
    QLineEdit, QComboBox, QInputDialog, QMenu, QColorDialog,
    QToolButton, QButtonGroup, QStyleOptionSlider, QStyle, QLCDNumber
)

from logfather.ui.time_ocr import additional_camera_roi_key, analyze_video_offset, SyncCctvTimeWindow, parse_filename_datetime
from logfather.ui.qt_worker import JobSlot

SKIP_INITIAL_FRAME_RENDER = False
from logfather.ui.settings_dialog import SettingsPanel, SystemLayoutPanel, ReadmePanel
from logfather.core.app_version import format_version_label


TARGET_QUEUE_MESSAGE = "adding new target to queue"
PPM_ROLLING_WINDOW_SECONDS = 60.0




# Forward jumps up to this many frames are decoded via grab() instead of a
# CAP_PROP_POS_FRAMES seek: a seek on H.264 jumps to the previous keyframe and
# decodes forward, which usually costs more than grabbing a handful of frames.
MAX_GRAB_SKIP_FRAMES = 15


def _position_capture_sequential(cap, in_sequence: bool, next_frame: int, target_frame: int) -> bool:
    """Try to reach target_frame without seeking.

    Returns True if cap's next read() will deliver target_frame (already there,
    or reached by grabbing a few frames forward). Returns False if the caller
    must seek. `in_sequence` says whether next_frame is trustworthy for cap.
    """
    if not in_sequence:
        return False
    delta = target_frame - next_frame
    if delta == 0:
        return True
    if 0 < delta <= MAX_GRAB_SKIP_FRAMES:
        for _ in range(delta):
            if not cap.grab():
                return False
        return True
    return False







# -------- GUI APPLICATION --------

class ReplayView(QWidget):
    logs_ready = Signal(list)
    logs_failed = Signal(str)
    current_time_changed = Signal(object)
    annotation_status_changed = Signal(object, bool)
    cache_prefetch_done = Signal()
    cache_clip_ready = Signal(object)
    # Emitted with the ORIGINAL (share) path once a clip is open and seekable.
    clip_opened = Signal(object)
    clip_range_export_requested = Signal(float, float)
    settings_saved = Signal()
    close_gap_threshold_changed = Signal(float)
    # Activity-bar feed (rendered by MainWindow's bottom strip):
    # (key, label, done_bytes, total_bytes) — done/total None for busy stages.
    activity_progress = Signal(str, str, object, object)
    activity_cleared = Signal(str)
    playing_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("The Logfather")
        self.settings = Settings.load()
        # Construction is split into ordered sections (Stage 3). The call
        # order matters: later sections consume attributes from earlier ones.
        self._init_state()
        self._build_filter_panel()
        self._build_custom_filter_tab()
        self._build_video_and_playback()
        self._build_analysis_controls()
        self._build_middle_layout()
        self._build_right_tabs()
        self._assemble_and_wire()

    def _init_state(self):
        """Non-widget state: playback, caches, executors, timers, slots."""
        self._export_target_overlay_provider = None

        # Video state
        self.cap = None
        self.fps = 25.0
        self.frame_count = 0
        self.current_frame = 0
        self.playing = False
        # Sequential-read tracking: which capture we last read from without
        # seeking, and the frame index its next read() will deliver. Seeking
        # (CAP_PROP_POS_FRAMES) forces a keyframe jump + decode-forward on
        # H.264, so it must only happen when playback actually jumps.
        self._seq_cap = None
        self._seq_next_frame = -1
        self._seq_additional_cap = None
        self._seq_additional_next_frame = -1

        self.last_qimage: QImage | None = None
        # Decoded frames are kept as BGR references (cap.read allocates a
        # fresh buffer per frame and nothing mutates them in place); the RGB
        # versions the analysis views need are converted lazily and cached.
        self._cur_frame_bgr: np.ndarray | None = None
        self._cur_frame_rgb: np.ndarray | None = None
        self._prev_frame_bgr: np.ndarray | None = None
        self._prev_frame_rgb: np.ndarray | None = None
        self._last_frame_index: int | None = None
        self.current_video_path: str | None = None
        self.current_video_original_path: Path | None = None
        self.current_video_filename_dt: datetime | None = None

        # Frame analysis state (persists across clips for reference frame)
        self.analysis_ref_frame_rgb: np.ndarray | None = None
        self.analysis_ref_frame_index: int | None = None
        self.analysis_prev_frame_index: int | None = None

        # Secondary video state (AdditionalCCTV)
        self.additional_cap = None
        self.additional_fps = 25.0
        self.additional_frame_count = 0
        self.additional_current_frame = 0
        self.additional_last_qimage: QImage | None = None
        self.additional_video_path: str | None = None
        self.additional_video_original_path: Path | None = None
        self.additional_video_filename_dt: datetime | None = None
        self._pending_additional_original_path: Path | None = None
        self._pending_additional_poll = False
        self._pending_additional_timer = QTimer(self)
        self._pending_additional_timer.setInterval(500)
        self._pending_additional_timer.timeout.connect(self._poll_pending_additional_cache)
        self._pending_additional_last_size: int | None = None
        self._pending_additional_stable_count = 0
        self.additional_video_start_dt: datetime | None = None
        self.additional_ocr_offset_seconds: float | None = None
        self.additional_ocr_frame_offset = 0
        self.additional_manual_offset_frames = 0
        self._updating_video_label = False
        self._pending_video_label_update = False
        self._draw_additional_video = False
        self._popout_window: QWidget | None = None
        self._popout_label: AnnotatedVideoWidget | None = None
        self._popout_color_btn: QToolButton | None = None
        self._popout_tool_group: QButtonGroup | None = None
        self._clip_annotations: list[dict] = []
        self._pinned_annotations: list[dict] = []
        self._annotation_history: list[dict] = []
        self._annotation_tool = "line"
        self._annotation_color = QColor("#ffcc00")

        # All events/logs from CSV (before filtering)
        self.all_events: list[LogEvent] = []
        self.all_log_display_rows: list[str] = []
        self.all_source_keys: list[str] = []
        self.all_state_keys: list[str] = []
        self.all_message_keys: list[str] = []

        # Active (filtered) events/logs
        self.events: list[LogEvent] = []
        self.log_display_rows: list[str] = []

        # Filter checkboxes: key -> QCheckBox
        self.source_checkboxes: dict[str, QCheckBox] = {}
        self.state_checkboxes: dict[str, QCheckBox] = {}
        self.message_checkboxes: dict[str, QCheckBox] = {}

        # Time offsets
        self.sync_offset = 0.0      # coarse sync (sync logs to video)
        self.time_offset = 0.0      # fine-tune offset from spinbox
        self.gap_threshold = 0.50
        self.close_gap_threshold_min = 0.25
        self.close_gap_threshold_max = 1.00
        self.close_gap_threshold_step = 0.05
        self.first_log_dt: datetime | None = None
        self.video_start_dt: datetime | None = None
        self.ocr_offset_seconds: float | None = None
        self.ocr_frame_offset = 0
        self._ocr_sync_prompt_choice: bool | None = None
        self.ocr_settings_path: Path | None = None
        self.offset_store = OcrOffsetStore()
        self.additional_offset_store = OcrOffsetStore()
        self.pending_pikpak_path: str | None = None
        self.pending_start_iso: str | None = None
        self.pending_end_iso: str | None = None
        self.auto_load_clip_logs = True
        self._pending_log_request_key: tuple[str, str, str] | None = None
        self._active_log_request_key: tuple[str, str, str] | None = None
        self._loaded_log_request_key: tuple[str, str, str] | None = None
        self._pending_log_autoload_timer = QTimer(self)
        self._pending_log_autoload_timer.setSingleShot(True)
        self._pending_log_autoload_timer.setInterval(350)
        self._pending_log_autoload_timer.timeout.connect(self._auto_load_pending_logs)
        self._auto_ocr_attempted_key: str | None = None
        self._auto_additional_ocr_attempted_key: str | None = None

        # First log time (string like "HH:MM:SS.mmm")
        self.first_log_time_str: str | None = None

        # Timer for playback
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)
        self._log_executor = ThreadPoolExecutor(max_workers=1)
        # All clip copy/prefetch/prune machinery lives in ClipCache. The
        # click-download executor is aliased because other code submits its
        # own jobs to it (stop report thumbnails, secondary-clip copies).
        self.clip_cache = ClipCache(
            protected_paths_provider=lambda: (
                self.current_video_path,
                self.additional_video_path,
            )
        )
        self.clip_cache.clip_ready.connect(self.cache_clip_ready)
        self.clip_cache.transfer_finished.connect(self._on_cache_transfer_finished)
        self._cache_executor = self.clip_cache.executor
        self._cache_status_future: Future | None = None
        self._cache_status_pending = False
        # Async clip download: (generation, source path on Z:, cache target).
        # Generation invalidates a pending download when another clip is
        # chosen before the copy finishes.
        self._pending_video_load: tuple[int, Path, Path] | None = None
        # Seek requested while the clip was still downloading; replayed once
        # the download opens (generation, seconds, pause).
        self._pending_seek: tuple[int, float, bool] | None = None
        self._video_load_generation = 0
        self._video_load_t0 = 0.0
        self._video_busy_dialog: QProgressDialog | None = None
        self._log_future: Future | None = None
        self._log_future_id = 0
        self.logs_ready.connect(self._on_elastic_logs_ready)
        self.logs_failed.connect(self._on_elastic_logs_failed)
        self.log_markers: list[tuple[float, str]] = []
        self.log_markers_enabled = False
        self.external_markers: list[tuple[float, str]] = []
        self.external_marker_source: str | None = None
        self._sku_timeline_items: list[object] = []
        self._ppm_event_seconds: list[float] = []
        self._ppm_interval_prefix_sum: list[float] = []
        self._ocr_tool_dialog = None
        # OCR auto-sync runs off the UI thread (SMB copy + Tesseract);
        # one slot per video so main/secondary syncs can overlap.
        self._ocr_sync_slot = JobSlot(self)
        self._ocr_additional_sync_slot = JobSlot(self)

    def _build_filter_panel(self):
        """The Filters tab: source / state / message checkbox columns."""
        self.filters_loaded = False
        # Unticked filter keys survive a reload: rebuilt checkboxes start
        # from this memory, and filters auto-reload if they were loaded
        # before (Chris, 2026-09-05).
        self._remembered_unchecked: dict[str, set[str]] = {"source": set(), "state": set(), "message": set()}
        self._filters_wanted = False

        # Source filter
        self.source_label = QLabel(f"Filter by {SOURCE_COLUMN}")
        self.source_label.setWordWrap(True)

        self.source_container_widget = QWidget()
        self.source_layout_inner = QVBoxLayout(self.source_container_widget)
        self.source_layout_inner.addStretch(1)

        self.source_scroll = QScrollArea()
        self.source_scroll.setWidgetResizable(True)
        self.source_scroll.setWidget(self.source_container_widget)
        self.source_scroll.setMinimumWidth(160)

        self.source_all_btn = QPushButton("All")
        self.source_none_btn = QPushButton("None")
        self.source_all_btn.clicked.connect(self.select_all_sources)
        self.source_none_btn.clicked.connect(self.select_no_sources)

        source_header_layout = QHBoxLayout()
        source_header_layout.addWidget(self.source_label, 1)
        source_header_layout.addWidget(self.source_all_btn)
        source_header_layout.addWidget(self.source_none_btn)

        # State filter
        self.state_label = QLabel(f"Filter by {STATE_COLUMN}")
        self.state_label.setWordWrap(True)

        self.state_container_widget = QWidget()
        self.state_layout_inner = QVBoxLayout(self.state_container_widget)
        self.state_layout_inner.addStretch(1)

        self.state_scroll = QScrollArea()
        self.state_scroll.setWidgetResizable(True)
        self.state_scroll.setWidget(self.state_container_widget)
        self.state_scroll.setMinimumWidth(160)

        self.state_all_btn = QPushButton("All")
        self.state_none_btn = QPushButton("None")
        self.state_all_btn.clicked.connect(self.select_all_states)
        self.state_none_btn.clicked.connect(self.select_no_states)

        state_header_layout = QHBoxLayout()
        state_header_layout.addWidget(self.state_label, 1)
        state_header_layout.addWidget(self.state_all_btn)
        state_header_layout.addWidget(self.state_none_btn)

        # Message filter
        self.message_label = QLabel(f"Filter by {MESSAGE_COLUMN}")
        self.message_label.setWordWrap(True)

        self.message_container_widget = QWidget()
        self.message_layout_inner = QVBoxLayout(self.message_container_widget)
        self.message_layout_inner.addStretch(1)

        self.message_scroll = QScrollArea()
        self.message_scroll.setWidgetResizable(True)
        self.message_scroll.setWidget(self.message_container_widget)
        self.message_scroll.setMinimumWidth(160)

        self.message_all_btn = QPushButton("All")
        self.message_none_btn = QPushButton("None")
        self.message_all_btn.clicked.connect(self.select_all_messages)
        self.message_none_btn.clicked.connect(self.select_no_messages)

        message_header_layout = QHBoxLayout()
        message_header_layout.addWidget(self.message_label, 1)
        message_header_layout.addWidget(self.message_all_btn)
        message_header_layout.addWidget(self.message_none_btn)


        self.filter_panel_layout = QVBoxLayout()
        self.filter_panel_layout.addLayout(source_header_layout)
        self.filter_panel_layout.addWidget(self.source_scroll)
        self.filter_panel_layout.addSpacing(12)
        self.filter_panel_layout.addSpacing(12)
        self.filter_panel_layout.addLayout(state_header_layout)
        self.filter_panel_layout.addWidget(self.state_scroll)
        self.filter_panel_layout.addSpacing(12)
        self.filter_panel_layout.addLayout(message_header_layout)
        self.filter_panel_layout.addWidget(self.message_scroll)

        self.filter_panel = QWidget()
        self.filter_panel.setLayout(self.filter_panel_layout)
        self.filter_panel.setVisible(False)

        self.filter_container = QWidget()
        self.filter_container_layout = QVBoxLayout(self.filter_container)
        self.filter_container_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_container_layout.setSpacing(8)
        self.filter_container_layout.addWidget(self.filter_panel)

    def _build_custom_filter_tab(self):
        """The Custom tab: 15 preset buttons + 5 free-text filter blocks."""
        self.custom_filter_blocks: list[tuple[QPushButton, QLineEdit, QLineEdit, QLabel]] = []
        self.custom_filter_hint = QLabel("Empty entries are ignored. Use commas to separate terms.")
        self.custom_filter_hint.setStyleSheet(theme.DIM_LABEL)

        self.filter_preset_group: list[QPushButton] = []
        self.active_filter_preset_index: int | None = None
        self.active_filter_presets: set[int] = set()

        # Kept on self: inserted into the filter container during final
        # assembly (_assemble_and_wire).
        self._preset_container = preset_container = QWidget()
        preset_container_layout = QVBoxLayout(preset_container)
        preset_container_layout.setContentsMargins(0, 0, 0, 0)
        preset_container_layout.setSpacing(4)

        preset_index = 0
        for _row in range(3):
            preset_row = QHBoxLayout()
            preset_row.setSpacing(6)
            for _col in range(5):
                idx = preset_index + 1
                btn = QPushButton(f"Preset {idx}")
                btn.setCheckable(True)
                btn.clicked.connect(lambda _checked, i=preset_index: self._on_filter_preset_clicked(i))
                btn.setContextMenuPolicy(Qt.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda _pos, i=preset_index: self._on_filter_preset_menu(i)
                )
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                preset_row.addWidget(btn, 1)
                self.filter_preset_group.append(btn)
                preset_index += 1
            preset_container_layout.addLayout(preset_row)

        custom_tab = QWidget()
        self._custom_tab = custom_tab
        custom_layout = QVBoxLayout(custom_tab)
        custom_layout.setContentsMargins(8, 8, 8, 8)
        custom_layout.setSpacing(6)
        custom_layout.addWidget(QLabel("Custom filters (comma separated)."))

        custom_layout.addWidget(self.custom_filter_hint)

        for idx in range(1, 6):
            block = QWidget()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 6, 0, 6)
            block_layout.setSpacing(4)

            btn = QPushButton(f"Preset {idx}")
            btn.setCheckable(True)
            btn.toggled.connect(self._on_custom_filter_changed)
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos, i=idx - 1: self._on_custom_filter_menu(i)
            )
            in_edit = QLineEdit()
            in_edit.setPlaceholderText("Filter in (comma separated)")
            in_edit.textChanged.connect(lambda _text, b=btn: self._on_custom_filter_text_changed(b))
            in_edit.textChanged.connect(self._validate_custom_filter_inputs)
            out_edit = QLineEdit()
            out_edit.setPlaceholderText("Filter out (comma separated)")
            out_edit.textChanged.connect(lambda _text, b=btn: self._on_custom_filter_text_changed(b))
            out_edit.textChanged.connect(self._validate_custom_filter_inputs)
            count_label = QLabel("Matches: -")
            count_label.setStyleSheet(theme.DIM_LABEL)

            block_layout.addWidget(btn)
            block_layout.addWidget(in_edit)
            block_layout.addWidget(out_edit)
            block_layout.addWidget(count_label)
            custom_layout.addWidget(block)
            self.custom_filter_blocks.append((btn, in_edit, out_edit, count_label))

        custom_layout.addStretch(1)

    def _build_video_and_playback(self):
        """Video panes, sync buttons, seek slider, LCDs, playback bar,
        cache controls."""
        self._placeholder_image = _load_placeholder_image()
        self.video_label = AnnotatedVideoWidget("No video loaded")
        self.video_label.setMinimumSize(300, 200)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.set_scrub_callback(self._handle_scroll_wheel)
        self.video_label.set_tray_update_callback(self._refresh_birds_eye_if_open)
        self.video_label.set_editable(False)
        if self._placeholder_image is not None:
            self.video_label.set_placeholder_image(self._placeholder_image)
        self.video_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.video_label.customContextMenuRequested.connect(self._copy_main_frame_to_clipboard)
        self.video_label.installEventFilter(self)
        self.video_sync_btn = QPushButton("Sync")
        self.video_sync_btn.setIcon(sync_icon())
        self.video_sync_btn.setIconSize(QSize(18, 18))
        self.video_sync_btn.setToolTip("Sync the camera clock to the footage (OCR); shows the offset in use")
        self.video_sync_btn.setFixedWidth(135)
        self.video_sync_btn.setEnabled(False)
        # "Sync: ?" breathes gently while a clip has no sync (Chris,
        # 2026-09-12); a press opens the OCR window.
        self._sync_pulser = Pulser(self)
        self.video_sync_btn.clicked.connect(self.open_sync_cctv_time)
        self._main_sync_done = False

        self.additional_video_label = VideoFrameLabel("Additional CCTV not loaded")
        self.additional_video_label.setAlignment(Qt.AlignCenter)
        self.additional_video_label.setMinimumSize(300, 200)
        self.additional_video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.additional_video_label.setVisible(False)
        self.additional_video_label.set_scrub_callback(self._handle_additional_scroll_wheel)
        self.additional_video_label.setFocusPolicy(Qt.StrongFocus)
        self.additional_video_label.installEventFilter(self)
        self.additional_video_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.additional_video_label.customContextMenuRequested.connect(self._copy_additional_frame_to_clipboard)
        self.additional_sync_btn = QPushButton("Sync")
        self.additional_sync_btn.setIcon(sync_icon())
        self.additional_sync_btn.setIconSize(QSize(18, 18))
        self.additional_sync_btn.setFixedWidth(135)
        self.additional_sync_btn.setEnabled(False)
        self.additional_sync_btn.clicked.connect(self.open_additional_sync_cctv_time)
        self._additional_sync_done = False
        self.additional_lock_toggle = QLabel("--Lock--")
        self.additional_lock_toggle.setAlignment(Qt.AlignCenter)
        self.additional_lock_toggle.setEnabled(False)
        self.additional_lock_toggle.setStyleSheet(theme.DIM_LABEL)
        self.additional_lock_toggle.setCursor(Qt.PointingHandCursor)
        self.additional_lock_toggle.mousePressEvent = self._toggle_additional_lock
        self.additional_locked = True

        self.seek_slider = ClipRangeSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.on_slider_moved)
        self.seek_slider.sliderPressed.connect(self.pause)
        self.seek_slider.clip_range_export_requested.connect(self._emit_seek_range_export_requested)

        self.info_label = SegmentDisplay()
        self.info_label.setDigitCount(12)  # 00:00:00.000
        self.info_label.setSegmentStyle(QLCDNumber.Flat)
        self.info_label.display("00:00:00.000")
        self.info_label.setFixedWidth(170)
        self.info_label.setStyleSheet(theme.LCD_DISPLAY)

        self.calc_label = SegmentDisplay()
        self.calc_label.setDigitCount(12)  # 00:00:00.000
        self.calc_label.setSegmentStyle(QLCDNumber.Flat)
        self.calc_label.display("00:00:00.000")
        self.calc_label.setFixedWidth(170)
        self.calc_label.setStyleSheet(theme.LCD_DISPLAY)

        self.frame_label = SegmentDisplay()
        self.frame_label.setDigitCount(8)
        self.frame_label.setSegmentStyle(QLCDNumber.Flat)
        self.frame_label.display("0")
        self.frame_label.setFixedWidth(120)
        self.frame_label.setStyleSheet(theme.LCD_DISPLAY)

        self.drift_min = -2.0
        self.drift_max = 2.0
        self.drift_step = 0.05
        self._drift_slider_scale = 1000

        # The same play / pause glyph button as the conveyor calibration
        # window (Chris, 2026-09-11).
        from logfather.ui.icons import media_icon

        self._media_icons = {"play": media_icon("play"), "pause": media_icon("pause")}
        # The play button sits on the bottom row, level with the green
        # clock and centred under the picture (Chris, 2026-09-11): it is
        # placed by hand over the row so the clock on the left and the
        # report buttons on the right do not pull it off centre.
        self.play_pause_btn = QPushButton(self)
        self.play_pause_btn.setIcon(self._media_icons["play"])
        self.play_pause_btn.setIconSize(QSize(28, 28))
        self.play_pause_btn.setFixedSize(QSize(54, 44))
        self.play_pause_btn.setToolTip("Play / pause (space)")
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        self.annotate_btn = QPushButton("Annotate")
        self.annotate_btn.clicked.connect(self._open_annotation_popout)
        self.birds_eye_btn = QPushButton("Bird's Eye")
        self.birds_eye_btn.clicked.connect(self._open_birds_eye_window)
        self.analysis_main_alpha_label = QLabel("Overlay: 0.60")
        self.analysis_main_alpha_slider = QSlider(Qt.Horizontal)
        self.analysis_main_alpha_slider.setRange(0, 100)
        self.analysis_main_alpha_slider.setValue(60)
        self.analysis_main_alpha_slider.setFixedWidth(150)
        self.analysis_main_alpha_slider.valueChanged.connect(self._on_analysis_main_alpha_changed)

        self.cache_root = self.clip_cache.root
        settings_root = DEFAULT_SETTINGS_PATH.parent
        self.ocr_settings_path = settings_root / "ocr_settings.json"
        self.offset_store = OcrOffsetStore(self.cache_root / "ocr_offsets.json")
        self.additional_offset_store = OcrOffsetStore(self.cache_root / "ocr_offsets_additional.json")
        self._load_pinned_annotations()
        self.cache_status_label = QLabel("")
        self.cache_status_label.setStyleSheet(theme.DIM_LABEL)
        self.cache_status_label.setWordWrap(True)
        self.open_cache_btn = QPushButton("Open Cache Folder")
        self.open_cache_btn.clicked.connect(self.open_cache_folder)
        self.clear_cache_btn = QPushButton("Clear Cache")
        self.clear_cache_btn.clicked.connect(self.clear_cache)
        self.clear_elastic_cache_btn = QPushButton("Clear Event Cache")
        self.clear_elastic_cache_btn.clicked.connect(self.clear_elastic_event_cache)
        self.delete_cache_btn = QPushButton("Delete Current Cache Copy")
        self.delete_cache_btn.clicked.connect(self.delete_current_cache_copy)

        # Kept on self: mounted into the settings dialog in _build_right_tabs.
        self._cache_controls_layout = cache_controls_layout = QHBoxLayout()
        cache_controls_layout.addWidget(self.cache_status_label, 1)
        cache_controls_layout.addWidget(self.open_cache_btn)
        cache_controls_layout.addWidget(self.delete_cache_btn)
        cache_controls_layout.addWidget(self.clear_elastic_cache_btn)
        cache_controls_layout.addWidget(self.clear_cache_btn)

        # The playback bar keeps only always-useful transport controls;
        # sync and overlay tools live in strips these toggles reveal
        # (Chris, 2026-09-04: only the essential buttons on screen).
        self.sync_tools_btn = QPushButton("Sync")
        self.sync_tools_btn.setCheckable(True)
        self.overlay_tools_btn = QPushButton("Overlays")
        self.overlay_tools_btn.setCheckable(True)
        # The green pick-rate / SKU text drawn over the footage can be
        # switched off (Chris, 2026-09-10); the choice is remembered.
        self.info_text_btn = QPushButton("Info text")
        self.info_text_btn.setCheckable(True)
        self.info_text_btn.setChecked(bool(load_ui_state().get("viewer_status_text", True)))
        self.info_text_btn.setToolTip("Show the pick rate, SKU, tray and tool text over the CCTV image")
        self.info_text_btn.toggled.connect(self._on_info_text_toggled)
        self._last_status_lines: list[str] = []
        # Less around the picture (Chris, 2026-09-11): the playback row is
        # Play and the log-time clock; Sync, Overlays and Info text live in
        # a View menu at the top right of the CCTV image, with a switch for
        # the clip-time and frame counters (off by default).
        self.playback_layout = QHBoxLayout()
        self.playback_layout.addWidget(self.calc_label)
        # Additional CCTV loads via timeline selection.
        self.playback_layout.addStretch(1)
        for btn in (self.sync_tools_btn, self.overlay_tools_btn, self.info_text_btn):
            btn.hide()
        self._build_view_menu()

    def _build_view_menu(self) -> None:
        """The View menu button sits on the CCTV image, top right."""
        self.view_menu_btn = QToolButton(self.video_label)
        self.view_menu_btn.setText("View \u25be")
        self.view_menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.view_menu_btn.setCursor(Qt.PointingHandCursor)
        self.view_menu_btn.setToolTip("Sync tools, overlays, info text and the clip counters")
        self.view_menu_btn.setStyleSheet(
            "QToolButton { background: rgba(0, 0, 0, 150); color: #ecf0f4; border: 1px solid rgba(255, 255, 255, 70);"
            " border-radius: 4px; padding: 2px 8px; font-size: 12px; }"
            "QToolButton:hover { background: rgba(0, 0, 0, 210); }"
            "QToolButton::menu-indicator { image: none; width: 0px; }"
        )
        menu = QMenu(self.view_menu_btn)
        self._view_menu_actions: dict[str, QAction] = {}

        def bind(label: str, btn: QPushButton, tip: str) -> None:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(btn.isChecked())
            action.setToolTip(tip)
            action.toggled.connect(lambda on, b=btn: b.setChecked(on) if b.isChecked() != on else None)
            btn.toggled.connect(lambda on, a=action: a.setChecked(on) if a.isChecked() != on else None)
            menu.addAction(action)
            self._view_menu_actions[label] = action

        bind("Sync tools", self.sync_tools_btn, "Show the sync strip under the picture")
        bind("Overlay tools", self.overlay_tools_btn, "Show the overlay strip under the picture")
        bind("Info text", self.info_text_btn, "Show the pick rate, SKU, tray and tool text over the CCTV image")
        menu.addSeparator()
        counters = QAction("Clip time and frame counter", self)
        counters.setCheckable(True)
        counters.setChecked(bool(load_ui_state().get("viewer_clip_counters", False)))
        counters.toggled.connect(self._set_clip_counters_visible)
        menu.addAction(counters)
        self._view_menu_actions["counters"] = counters
        # Additional CCTV beside the main picture, when a clip covers this
        # time (Chris, 2026-09-11): tick to show it, untick to hide it. The
        # main window supplies the lookup (the timeline knows the clips).
        self.additional_cctv_resolver = None
        # Play at the end of a clip opens the next one and plays it (Chris,
        # 2026-09-11); the main window supplies the opener.
        self.next_clip_requester = None
        self._play_after_open = False
        additional = QAction("Additional CCTV", self)
        additional.setCheckable(True)
        additional.toggled.connect(self._on_additional_cctv_toggled)
        menu.addAction(additional)
        self._view_menu_actions["additional"] = additional
        menu.aboutToShow.connect(self._refresh_additional_cctv_action)
        # The OCR offset in use (Chris, 2026-09-12); clicking opens the
        # sync tools to change it.
        menu.addSeparator()
        ocr = QAction("OCR offset", self)
        ocr.triggered.connect(lambda: self.sync_tools_btn.setChecked(True))
        menu.addAction(ocr)
        self._view_menu_actions["ocr"] = ocr
        menu.aboutToShow.connect(self._refresh_ocr_offset_action)
        self.view_menu_btn.setMenu(menu)
        self.video_label.installEventFilter(self)
        self._set_clip_counters_visible(counters.isChecked(), remember=False)
        self._place_view_menu()

    def _refresh_ocr_offset_action(self) -> None:
        action = self._view_menu_actions.get("ocr")
        if action is None:
            return
        if self.cap is None:
            action.setText("OCR offset: no clip open")
        elif self.ocr_offset_seconds is None:
            action.setText("OCR offset: none (clip start taken from the filename)")
        else:
            action.setText(f"OCR offset: {self.ocr_offset_seconds:+.1f} s, {int(self.ocr_frame_offset or 0):+d} frames")

    def _current_clip_time(self):
        """Wall-clock time of the frame on screen, or None without a clip."""
        start = self.video_start_dt or self.current_video_filename_dt
        if start is None or self.cap is None:
            return None
        try:
            return start + timedelta(seconds=(self.current_frame or 0) / (self.fps or 25.0))
        except Exception:
            return None

    def _additional_cctv_available(self) -> Path | None:
        """The additional clip covering the current time, if the main
        window can find one."""
        resolver = self.additional_cctv_resolver
        moment = self._current_clip_time()
        if resolver is None or moment is None:
            return None
        try:
            found = resolver(moment)
        except Exception:
            return None
        return found if isinstance(found, Path) else None

    def _refresh_additional_cctv_action(self) -> None:
        action = self._view_menu_actions.get("additional")
        if action is None:
            return
        loaded = self.additional_cap is not None or self._pending_additional_original_path is not None
        available = loaded or self._additional_cctv_available() is not None
        action.blockSignals(True)
        action.setEnabled(available)
        action.setChecked(bool(loaded and self._draw_additional_video))
        action.setText("Additional CCTV" if available else "Additional CCTV (none for this time)")
        action.blockSignals(False)

    def _on_additional_cctv_toggled(self, on: bool) -> None:
        if on:
            if self.additional_cap is not None:
                self._draw_additional_video = True
                self._refresh_additional_visibility()
                return
            path = self._additional_cctv_available()
            if path is not None:
                self.load_additional_cctv_from_path(path)
            else:
                self._refresh_additional_cctv_action()
        else:
            self._draw_additional_video = False
            self._refresh_additional_visibility()

    def _set_clip_counters_visible(self, on: bool, remember: bool = True) -> None:
        for widget in (self.info_label, self.frame_label):
            widget.setVisible(bool(on))
        if remember:
            update_ui_state({"viewer_clip_counters": bool(on)})

    def _place_view_menu(self) -> None:
        if self.video_label is None:
            return
        btn = self.view_menu_btn
        if btn is not None:
            btn.adjustSize()
            btn.move(max(0, self.video_label.width() - btn.width() - 8), 8)
            btn.raise_()
        play = self.play_pause_btn
        clock = self.calc_label
        if play is not None and clock is not None and play.parent() is self:
            # Centred on the scroll bar, which spans the whole picture area,
            # so the button never shifts when a second camera appears
            # (Chris, 2026-09-11).
            slider = self.seek_slider
            anchor = slider if slider is not None and slider.width() > 0 else self.video_label
            centre_x = anchor.geometry().center().x()
            centre_y = clock.geometry().center().y()
            play.move(max(0, centre_x - play.width() // 2), max(0, centre_y - play.height() // 2))
            play.raise_()

    def _build_analysis_controls(self):
        """Frame-diff / optical-flow controls and the analysis view pane."""
        self.analysis_mode_combo = QComboBox()
        self.analysis_mode_combo.addItems(["Off", "Frame Diff", "Optical Flow"])
        self.analysis_mode_combo.currentIndexChanged.connect(self._on_analysis_mode_changed)

        self.analysis_display_combo = QComboBox()
        self.analysis_display_combo.addItems(["Main Overlay", "Main Side-by-side", "Popout"])
        self.analysis_display_combo.currentIndexChanged.connect(self._on_analysis_display_changed)

        self.analysis_pair_combo = QComboBox()
        self.analysis_pair_combo.addItems(["Reference -> Current", "Previous -> Current"])
        self.analysis_pair_combo.currentIndexChanged.connect(self._update_analysis_view)

        self.analysis_set_ref_btn = QPushButton("Set Reference")
        self.analysis_set_ref_btn.clicked.connect(self._set_analysis_reference)
        self.analysis_clear_ref_btn = QPushButton("Clear Reference")
        self.analysis_clear_ref_btn.clicked.connect(self._clear_analysis_reference)

        self.analysis_heatmap_cb = QCheckBox("Heatmap")
        self.analysis_heatmap_cb.setChecked(True)
        self.analysis_heatmap_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_overlay_cb = QCheckBox("Overlay")
        self.analysis_overlay_cb.setChecked(False)
        self.analysis_overlay_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_arrows_cb = QCheckBox("Flow arrows")
        self.analysis_arrows_cb.setChecked(False)
        self.analysis_arrows_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_arrows_cb.stateChanged.connect(self._update_analysis_controls_state)
        self.analysis_hide_zero_flow_cb = QCheckBox("Hide zero flow")
        self.analysis_hide_zero_flow_cb.setChecked(True)
        self.analysis_hide_zero_flow_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_hide_zero_flow_cb.stateChanged.connect(self._update_analysis_controls_state)
        self.analysis_zero_flow_label = QLabel("Min flow: 0.00")
        self.analysis_zero_flow_slider = QSlider(Qt.Horizontal)
        self.analysis_zero_flow_slider.setRange(0, 100)
        self.analysis_zero_flow_slider.setValue(1)
        self.analysis_zero_flow_slider.setFixedWidth(140)
        self.analysis_zero_flow_slider.valueChanged.connect(self._on_analysis_zero_flow_changed)
        self._update_analysis_zero_flow_label()

        self.analysis_gain_label = QLabel("Gain: 6x")
        self.analysis_gain_slider = QSlider(Qt.Horizontal)
        self.analysis_gain_slider.setRange(1, 30)
        self.analysis_gain_slider.setValue(6)
        self.analysis_gain_slider.valueChanged.connect(self._on_analysis_gain_changed)

        self.analysis_thresh_label = QLabel("Threshold / Min motion: 15")
        self.analysis_thresh_slider = QSlider(Qt.Horizontal)
        self.analysis_thresh_slider.setRange(0, 255)
        self.analysis_thresh_slider.setValue(15)
        self.analysis_thresh_slider.valueChanged.connect(self._on_analysis_thresh_changed)

        self.analysis_alpha_label = QLabel("Overlay alpha: 0.60")
        self.analysis_alpha_slider = QSlider(Qt.Horizontal)
        self.analysis_alpha_slider.setRange(0, 100)
        self.analysis_alpha_slider.setValue(60)
        self.analysis_alpha_slider.valueChanged.connect(self._on_analysis_alpha_changed)

        self.analysis_scale_label = QLabel("Compute scale: 100%")
        self.analysis_scale_slider = QSlider(Qt.Horizontal)
        self.analysis_scale_slider.setRange(25, 100)
        self.analysis_scale_slider.setValue(100)
        self.analysis_scale_slider.valueChanged.connect(self._on_analysis_scale_changed)

        self.analysis_arrow_step_label = QLabel("Arrow step: 20 px")
        self.analysis_arrow_step_slider = QSlider(Qt.Horizontal)
        self.analysis_arrow_step_slider.setRange(8, 60)
        self.analysis_arrow_step_slider.setValue(20)
        self.analysis_arrow_step_slider.valueChanged.connect(self._on_analysis_arrow_step_changed)

        self.analysis_arrow_scale_label = QLabel("Arrow length scale: 1.5x")
        self.analysis_arrow_scale_slider = QSlider(Qt.Horizontal)
        self.analysis_arrow_scale_slider.setRange(5, 50)
        self.analysis_arrow_scale_slider.setValue(15)
        self.analysis_arrow_scale_slider.valueChanged.connect(self._on_analysis_arrow_scale_changed)

        analysis_row1 = QHBoxLayout()
        analysis_row1.addWidget(QLabel("Analysis:"))
        analysis_row1.addWidget(self.analysis_mode_combo)
        analysis_row1.addSpacing(8)
        analysis_row1.addWidget(QLabel("Display:"))
        analysis_row1.addWidget(self.analysis_display_combo)
        analysis_row1.addStretch(1)

        analysis_row2 = QHBoxLayout()
        analysis_row2.addWidget(QLabel("Pairing:"))
        analysis_row2.addWidget(self.analysis_pair_combo)
        analysis_row2.addSpacing(8)
        analysis_row2.addWidget(self.analysis_set_ref_btn)
        analysis_row2.addWidget(self.analysis_clear_ref_btn)
        analysis_row2.addStretch(1)

        analysis_row3 = QHBoxLayout()
        analysis_row3.addWidget(self.analysis_heatmap_cb)
        analysis_row3.addWidget(self.analysis_overlay_cb)
        analysis_row3.addWidget(self.analysis_arrows_cb)
        analysis_row3.addWidget(self.analysis_hide_zero_flow_cb)
        analysis_row3.addWidget(self.analysis_zero_flow_label)
        analysis_row3.addWidget(self.analysis_zero_flow_slider)
        analysis_row3.addStretch(1)

        analysis_row4 = QHBoxLayout()
        analysis_row4.addWidget(self.analysis_gain_label)
        analysis_row4.addWidget(self.analysis_gain_slider)

        analysis_row5 = QHBoxLayout()
        analysis_row5.addWidget(self.analysis_thresh_label)
        analysis_row5.addWidget(self.analysis_thresh_slider)

        analysis_row6 = QHBoxLayout()
        analysis_row6.addWidget(self.analysis_alpha_label)
        analysis_row6.addWidget(self.analysis_alpha_slider)

        analysis_row7 = QHBoxLayout()
        analysis_row7.addWidget(self.analysis_scale_label)
        analysis_row7.addWidget(self.analysis_scale_slider)

        analysis_row8 = QHBoxLayout()
        analysis_row8.addWidget(self.analysis_arrow_step_label)
        analysis_row8.addWidget(self.analysis_arrow_step_slider)

        analysis_row9 = QHBoxLayout()
        analysis_row9.addWidget(self.analysis_arrow_scale_label)
        analysis_row9.addWidget(self.analysis_arrow_scale_slider)

        self.analysis_label = VideoFrameLabel("Analysis view")
        self.analysis_label.setAlignment(Qt.AlignCenter)
        self.analysis_label.setMinimumSize(480, 220)
        self.analysis_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.analysis_label.set_scrub_callback(self._handle_scroll_wheel)
        self.analysis_label.setVisible(False)
        self._analysis_window: QWidget | None = None
        self._analysis_window_label: VideoFrameLabel | None = None

        self.analysis_controls_panel = QWidget()
        analysis_controls_layout = QVBoxLayout(self.analysis_controls_panel)
        analysis_controls_layout.setContentsMargins(0, 0, 0, 0)
        analysis_controls_layout.setSpacing(6)
        analysis_controls_layout.addLayout(analysis_row1)
        analysis_controls_layout.addLayout(analysis_row2)
        analysis_controls_layout.addLayout(analysis_row3)
        analysis_controls_layout.addLayout(analysis_row4)
        analysis_controls_layout.addLayout(analysis_row5)
        analysis_controls_layout.addLayout(analysis_row6)
        analysis_controls_layout.addLayout(analysis_row7)
        analysis_controls_layout.addLayout(analysis_row8)
        analysis_controls_layout.addLayout(analysis_row9)
        analysis_controls_layout.addStretch(1)
        self.analysis_controls_panel.setMaximumWidth(330)

        for slider in (
            self.analysis_gain_slider,
            self.analysis_thresh_slider,
            self.analysis_alpha_slider,
            self.analysis_scale_slider,
            self.analysis_arrow_step_slider,
            self.analysis_arrow_scale_slider,
        ):
            slider.setFixedWidth(210)

        self.analysis_container = QWidget()
        analysis_layout = QVBoxLayout(self.analysis_container)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.addWidget(self.analysis_label, 1)
        self.analysis_container.setVisible(False)
        self._update_analysis_controls_state()
        self._update_analysis_output()

    def _build_middle_layout(self):
        """Stack the video row, marker bars, seek slider and playback bar;
        add the drift/gap sliders onto the playback bar."""
        # Kept on self: mounted into the root layout in _assemble_and_wire.
        self._middle_layout = middle_layout = QVBoxLayout()
        self.event_marker_bar = EventMarkerBar()
        self.timeline_marker_bar = EventMarkerBar()
        self.timeline_marker_bar.set_triangle_red_markers(True)
        # Clip position, log time and frame LCDs across the top (Chris,
        # 2026-09-08: the green log-time clock had moved into the Sync
        # strip and was missed); the sync buttons stay in the sync strip.
        lock_row = QHBoxLayout()
        lock_row.addWidget(self.info_label)
        lock_row.addSpacing(8)
        lock_row.addWidget(self.frame_label)
        lock_row.addStretch(1)
        middle_layout.addLayout(lock_row)
        video_row = QHBoxLayout()
        video_row.addWidget(self.video_label, 1)
        video_row.addWidget(self.additional_video_label, 1)
        video_row.addWidget(self.analysis_label, 1)
        middle_layout.addLayout(video_row)
        # The yellow log-marker bar is no longer shown (Chris, 2026-09-11);
        # the log list and the blue timeline bar carry the same events.
        self.event_marker_bar.hide()
        # Clip start (left) and end (right) to the minute, on the same
        # line as the scroll bar, which is shorter by their width (Chris,
        # 2026-09-11: more height for the picture).
        self.clip_start_label = QLabel("")
        self.clip_end_label = QLabel("")
        for lbl in (self.clip_start_label, self.clip_end_label):
            lbl.setStyleSheet(f"{theme.MUTED_LABEL} font-size: 11px;")
            lbl.setFixedWidth(36)
        self.clip_start_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.clip_end_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider_row = QHBoxLayout()
        slider_row.setContentsMargins(0, 0, 0, 0)
        slider_row.setSpacing(4)
        slider_row.addWidget(self.clip_start_label)
        slider_row.addWidget(self.seek_slider, 1)
        slider_row.addWidget(self.clip_end_label)
        middle_layout.addLayout(slider_row)
        middle_layout.addWidget(self.timeline_marker_bar)
        middle_layout.addLayout(self.playback_layout)
        QTimer.singleShot(0, self._update_marker_bar_padding)

        self.drift_caption = QLabel("Drift")
        self.drift_caption.setStyleSheet(theme.SLIDER_CAPTION)
        self.drift_slider = DriftSlider(Qt.Horizontal)
        self.drift_slider.setRange(
            int(self.drift_min * self._drift_slider_scale),
            int(self.drift_max * self._drift_slider_scale),
        )
        self.drift_slider.setSingleStep(int(self.drift_step * self._drift_slider_scale))
        self.drift_slider.setPageStep(int(0.25 * self._drift_slider_scale))
        self.drift_slider.valueChanged.connect(self._on_drift_slider_changed)
        self.drift_display = QLabel("+0.00s")
        self.drift_display.setMinimumWidth(48)
        self.drift_display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.drift_display.setStyleSheet(theme.SLIDER_VALUE)
        self.gap_caption = QLabel("Gap")
        self.gap_caption.setStyleSheet(theme.SLIDER_CAPTION)
        self.gap_slider = DriftSlider(Qt.Horizontal)
        self.gap_slider.setRange(
            int(round(self.close_gap_threshold_min * 100.0)),
            int(round(self.close_gap_threshold_max * 100.0)),
        )
        self.gap_slider.setSingleStep(int(round(self.close_gap_threshold_step * 100.0)))
        self.gap_slider.setPageStep(10)
        self.gap_slider.valueChanged.connect(self._on_close_gap_slider_changed)
        self.gap_display = QLabel("0.50x")
        self.gap_display.setMinimumWidth(40)
        self.gap_display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.gap_display.setStyleSheet(theme.SLIDER_VALUE)
        self._update_close_gap_threshold_display()

        # Sync strip: everything for aligning video and log time, revealed
        # by the playback bar's Sync toggle.
        self._sync_strip = QWidget()
        sync_strip_layout = QHBoxLayout(self._sync_strip)
        sync_strip_layout.setContentsMargins(0, 0, 0, 0)
        sync_strip_layout.addWidget(self.video_sync_btn)
        sync_strip_layout.addSpacing(8)
        # The drift tool lives in the main window's top bar, left of Sync,
        # so it is always visible (Chris, 2026-09-12): caption, slider and
        # readout in one small widget the main window mounts.
        self.drift_tool = QWidget()
        drift_layout = QHBoxLayout(self.drift_tool)
        drift_layout.setContentsMargins(0, 0, 0, 0)
        drift_layout.setSpacing(4)
        self.drift_slider.setFixedWidth(150)
        drift_layout.addWidget(self.drift_caption)
        drift_layout.addWidget(self.drift_slider)
        drift_layout.addWidget(self.drift_display)
        sync_strip_layout.addSpacing(6)
        sync_strip_layout.addWidget(self.gap_caption)
        sync_strip_layout.addWidget(self.gap_slider)
        sync_strip_layout.addWidget(self.gap_display)
        sync_strip_layout.addStretch(1)
        sync_strip_layout.addWidget(self.additional_lock_toggle)
        sync_strip_layout.addSpacing(8)
        sync_strip_layout.addWidget(self.additional_sync_btn)
        self._sync_strip.setVisible(False)
        middle_layout.addWidget(self._sync_strip)

        # Overlay strip: annotation and tray-view tools, revealed by the
        # Overlays toggle.
        self._overlay_strip = QWidget()
        overlay_strip_layout = QHBoxLayout(self._overlay_strip)
        overlay_strip_layout.setContentsMargins(0, 0, 0, 0)
        overlay_strip_layout.addWidget(self.annotate_btn)
        overlay_strip_layout.addWidget(self.birds_eye_btn)
        overlay_strip_layout.addSpacing(8)
        overlay_strip_layout.addWidget(self.analysis_main_alpha_label)
        overlay_strip_layout.addWidget(self.analysis_main_alpha_slider)
        overlay_strip_layout.addStretch(1)
        self._overlay_strip.setVisible(False)
        middle_layout.addWidget(self._overlay_strip)

        self.sync_tools_btn.toggled.connect(self._sync_strip.setVisible)
        self.overlay_tools_btn.toggled.connect(self._overlay_strip.setVisible)

    def _build_right_tabs(self):
        """The collapsible right panel: Logs / Filters / Custom / Settings /
        Systems / Readme tabs plus the pin button."""
        self.log_label = QLabel("Log entries")
        self._log_model = LogListModel(self)
        self.log_list = QListView()
        self.log_list.setModel(self._log_model)
        self.log_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.log_list.setUniformItemSizes(True)
        self.log_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.log_list.setStyleSheet(theme.LOG_LIST)
        self.log_list.clicked.connect(self._on_log_item_clicked)

        self.sync_start_btn = QPushButton("Sync logs to current video (first log)")
        self.sync_start_btn.clicked.connect(self.sync_logs_to_current_video_first_log)
        # No "Load logs" button (Chris, 2026-09-11): the clip's logs load on
        # their own when a clip opens.

        log_tab = QWidget()
        log_tab_layout = QVBoxLayout(log_tab)
        self.log_tab_layout = log_tab_layout
        log_tab_layout.addWidget(self.log_label)
        log_tab_layout.addWidget(self.log_list)

        settings_tab = QWidget()
        settings_tab_layout = QVBoxLayout(settings_tab)
        version_label = QLabel(f"Build: {format_version_label()}")
        version_label.setStyleSheet(theme.MUTED_LABEL)
        settings_tab_layout.addWidget(version_label)
        self.settings_panel = SettingsPanel(self.settings, settings_tab)
        settings_tab_layout.addWidget(self.settings_panel)
        self.save_settings_btn = QPushButton("Save Settings")
        self.save_settings_btn.clicked.connect(self._flush_settings_autosave)
        settings_tab_layout.addWidget(self.save_settings_btn)

        io_settings_layout = QHBoxLayout()
        self.export_settings_btn = QPushButton("Export…")
        self.export_settings_btn.setToolTip(
            "Save filters, conditions and presets to a shareable JSON file. "
            "Your Elastic API key and PikPak parent path are NOT included."
        )
        self.export_settings_btn.clicked.connect(self._on_export_settings)
        self.import_settings_btn = QPushButton("Import…")
        self.import_settings_btn.setToolTip(
            "Load filters, conditions and presets from a shared JSON file."
        )
        self.import_settings_btn.clicked.connect(self._on_import_settings)
        io_settings_layout.addWidget(self.export_settings_btn)
        io_settings_layout.addWidget(self.import_settings_btn)
        settings_tab_layout.addLayout(io_settings_layout)

        systems_tab = QWidget()
        systems_tab_layout = QVBoxLayout(systems_tab)
        self.system_layout_panel = SystemLayoutPanel(self.settings, systems_tab)
        systems_tab_layout.addWidget(self.system_layout_panel)
        settings_tab_layout.addStretch(1)
        settings_tab_layout.addWidget(self.sync_start_btn)
        settings_tab_layout.addLayout(self._cache_controls_layout)
        settings_tab_layout.addStretch(1)

        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(log_tab, "Logs")
        self.right_tabs.addTab(self.filter_container, "Filters")
        self.right_tabs.addTab(self._custom_tab, "Custom")

        # Settings/Systems/Readme are configuration, not daily use: they
        # open from the gear button as a dialog instead of living as
        # permanent tabs (Chris, 2026-09-04). The widgets and their
        # attributes are unchanged - only their home moved.
        self._config_tabs = QTabWidget()
        self._config_tabs.addTab(settings_tab, "Settings")
        self._config_tabs.addTab(systems_tab, "Systems")
        self._config_tabs.addTab(ReadmePanel(), "Readme")
        self._config_dialog = QDialog(self)
        self._config_dialog.setWindowTitle("Settings")
        # A real window with a title-bar close and a Close button, sized to
        # the screen when it opens (Chris, 2026-09-07: it ran off the page
        # and had no way to close).
        self._config_dialog.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self._config_dialog.setSizeGripEnabled(True)
        config_layout = QVBoxLayout(self._config_dialog)
        config_layout.setContentsMargins(8, 8, 8, 8)
        config_layout.addWidget(self._config_tabs, 1)
        config_buttons = QHBoxLayout()
        config_buttons.addStretch(1)
        config_close = QPushButton("Close")
        config_close.setDefault(True)
        config_close.clicked.connect(self._config_dialog.hide)
        config_buttons.addWidget(config_close)
        config_layout.addLayout(config_buttons)
        self._config_dialog.resize(560, 720)
        self._hover_reveal_enabled = True
        self._right_reveal_px = 12
        self._right_tabs_pinned = False
        self._right_tabs_expanded = True
        self._right_tabs_target_width = 600
        self.right_tabs.setMouseTracking(True)
        self.right_tabs.setMinimumWidth(0)
        self.right_tabs.setMaximumWidth(self._right_tabs_target_width)
        self._right_tabs_anim = QVariantAnimation(self)
        self._right_tabs_anim.setDuration(170)
        self._right_tabs_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._right_tabs_anim.valueChanged.connect(self._on_right_tabs_anim_step)

        self._pin_btn = QPushButton("📌")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setFixedSize(32, 28)
        self._pin_btn.setToolTip("Pin panel open")
        self._pin_btn.setStyleSheet(theme.PIN_BUTTON)
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(2)
        corner_layout.addWidget(self._pin_btn)
        self.right_tabs.setCornerWidget(corner, Qt.TopRightCorner)

    def add_right_panel_widget(self, widget: QWidget) -> None:
        """Mount a widget under the right-hand tabs."""
        self.right_extra_layout.addWidget(widget)

    def _open_config_dialog(self):
        dlg = self._config_dialog
        if not dlg.isVisible():
            # Fit the screen the main window is on and sit centred over it.
            screen = self.window().screen()
            if screen is not None:
                avail = screen.availableGeometry()
                width = min(640, max(420, avail.width() - 80))
                height = min(760, max(360, avail.height() - 80))
                dlg.resize(width, height)
                centre = self.window().frameGeometry().center()
                x = min(max(avail.left(), centre.x() - width // 2), avail.right() - width)
                y = min(max(avail.top(), centre.y() - height // 2), avail.bottom() - height)
                dlg.move(x, y)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def open_config_tab(self, name: str) -> None:
        """Open the Settings dialog on the named tab (gear menu, 2026-09-07)."""
        for i in range(self._config_tabs.count()):
            if self._config_tabs.tabText(i) == name:
                self._config_tabs.setCurrentIndex(i)
                break
        self._open_config_dialog()

    def open_data_sources(self) -> None:
        """CCTV share, Elastic and Grafana in one dialog; saving runs the
        same flush as the Settings tab so the main window reloads."""
        from logfather.ui.data_sources_dialog import DataSourcesDialog

        dlg = DataSourcesDialog(self.settings, self._flush_settings_autosave, self)
        dlg.exec()

    def _assemble_and_wire(self):
        """Mount everything into the root layout; final wiring that spans
        sections (event filters, autosave/debounce timers, saved pin state)."""
        root_layout = QHBoxLayout()
        root_layout.addLayout(self._middle_layout, stretch=3)
        # The right column: the tabs, then whatever the main window mounts
        # under them (the Data and Additional data boxes, Chris,
        # 2026-09-08). The hover-reveal animates the whole column.
        self.right_column = QWidget()
        column_layout = QVBoxLayout(self.right_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(6)
        column_layout.addWidget(self.right_tabs, 1)
        # The tabs give way first: the boxes mounted under them keep the
        # height their text needs, and the log tabs take whatever is left
        # (Chris, 2026-09-11), however little.
        tabs_policy = self.right_tabs.sizePolicy()
        tabs_policy.setVerticalPolicy(QSizePolicy.Ignored)
        self.right_tabs.setSizePolicy(tabs_policy)
        self.right_extra_layout = QVBoxLayout()
        self.right_extra_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.addLayout(self.right_extra_layout)
        self.right_column.setMaximumWidth(self._right_tabs_target_width)
        # The tab pages and the boxes under them add up to ~780 px of
        # minimum height, more than a laptop screen (Chris, on site,
        # 2026-09-08: the timeline and activity bar were pushed off the
        # bottom). Ignore that minimum: the column takes the row height.
        self.right_column.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        root_layout.addWidget(self.right_column, stretch=0)

        self.setLayout(root_layout)
        self.setMinimumSize(980, 560)
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.right_tabs.installEventFilter(self)
        self.right_column.installEventFilter(self)
        self._log_busy_dialog: QProgressDialog | None = None
        self._set_filter_tabs_enabled(False)

        self.filter_container_layout.insertWidget(0, self._preset_container)
        self._load_custom_filter_settings()
        self._load_filter_preset_settings()
        self._startup_maintenance_started = False
        self._settings_autosave_timer = QTimer(self)
        self._settings_autosave_timer.setSingleShot(True)
        self._settings_autosave_timer.setInterval(350)
        self._settings_autosave_timer.timeout.connect(self._save_settings_from_tab)
        self._filter_debounce_timer = QTimer(self)
        self._filter_debounce_timer.setSingleShot(True)
        self._filter_debounce_timer.setInterval(250)
        self._filter_debounce_timer.timeout.connect(self.apply_filters)
        self.settings_panel.changed.connect(self._schedule_settings_autosave)
        self.settings_panel.save_requested.connect(self._flush_settings_autosave)
        self.system_layout_panel.changed.connect(self._schedule_settings_autosave)

        if getattr(self.settings, "log_panel_pinned", False):
            self._pin_btn.setChecked(True)

    def _on_pin_toggled(self, pinned: bool) -> None:
        self._right_tabs_pinned = pinned
        self._hover_reveal_enabled = not pinned
        self._pin_btn.setToolTip("Unpin panel" if pinned else "Pin panel open")
        if pinned:
            self._set_right_tabs_visible(True)
        self.settings.log_panel_pinned = pinned
        self.settings.save()

    def start_background_maintenance(self):
        if self._startup_maintenance_started:
            return
        self._startup_maintenance_started = True
        QTimer.singleShot(0, self.prune_cache_if_needed)

    def _schedule_settings_autosave(self):
        self._settings_autosave_timer.start()

    # ---- Sync button label ----

    def eventFilter(self, obj, event):
        if obj is self.video_label and event.type() == QEvent.MouseButtonDblClick:
            self._toggle_video_popout()
            return True
        if obj is self.video_label and event.type() == QEvent.Resize:
            self._place_view_menu()
        if obj is self.additional_video_label and event.type() == QEvent.Wheel:
            self._handle_additional_scroll_wheel(event.angleDelta().y())
            return True
        if (
            self.video_label is not None
            and obj is getattr(self.video_label, "_birds_eye_window", None)
            and event.type() == QEvent.Resize
        ):
            self._refresh_birds_eye_if_open()
        if getattr(self, "_hover_reveal_enabled", False):  # the filter can fire during __init__ (2026-09-11)
            if event.type() == QEvent.MouseMove and obj is self:
                if not self._right_tabs_expanded:
                    pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                    if pos.x() >= self.width() - self._right_reveal_px:
                        self._set_right_tabs_visible(True)
            elif event.type() == QEvent.Leave and obj in (self.right_tabs, self.right_column):
                QTimer.singleShot(50, self._auto_hide_right_tabs)
        return super().eventFilter(obj, event)

    def _set_right_tabs_visible(self, visible: bool):
        visible = bool(visible)
        if visible == self._right_tabs_expanded:
            return
        self._right_tabs_expanded = visible
        if self._right_tabs_anim.state() == QVariantAnimation.Running:
            self._right_tabs_anim.stop()
        current = self.right_column.width()
        if current <= 0:
            current = 0 if not visible else self._right_tabs_target_width
        end = self._right_tabs_target_width if visible else 0
        self._right_tabs_anim.setStartValue(int(current))
        self._right_tabs_anim.setEndValue(int(end))
        self._right_tabs_anim.start()

    def _on_right_tabs_anim_step(self, value):
        width = max(0, int(value))
        self.right_column.setMinimumWidth(width)
        self.right_column.setMaximumWidth(width)

    def _auto_hide_right_tabs(self):
        if not self._right_tabs_expanded or self._right_tabs_pinned:
            return
        try:
            current_idx = self.right_tabs.currentIndex()
            current_label = self.right_tabs.tabText(current_idx) if current_idx >= 0 else ""
            if str(current_label).strip().lower() == "systems":
                return
        except Exception:
            pass
        pos = self.right_tabs.mapFromGlobal(self.cursor().pos())
        if not self.right_tabs.rect().contains(pos):
            self._set_right_tabs_visible(False)

    def _on_analysis_mode_changed(self, _index: int | None = None):
        enabled = self.analysis_mode_combo.currentText() != "Off"
        self.analysis_display_combo.setEnabled(enabled)
        self.analysis_main_alpha_label.setVisible(enabled)
        self.analysis_main_alpha_slider.setVisible(enabled)
        self._update_analysis_controls_state()
        self._update_analysis_output()
        self._update_analysis_view()

    def _on_analysis_display_changed(self, _index: int | None = None):
        self._update_analysis_output()
        self._update_analysis_view()

    def _update_analysis_output(self):
        enabled = self.analysis_mode_combo.currentText() != "Off"
        if not enabled:
            self.analysis_container.setVisible(False)
            self._hide_analysis_popout()
            self.analysis_label.setVisible(False)
            self._refresh_additional_visibility()
            self.analysis_main_alpha_label.setVisible(False)
            self.analysis_main_alpha_slider.setVisible(False)
            return
        display = self.analysis_display_combo.currentText()
        show_main_overlay = display == "Main Overlay"
        self.analysis_main_alpha_label.setVisible(show_main_overlay)
        self.analysis_main_alpha_slider.setVisible(show_main_overlay)
        if display == "Popout":
            self.analysis_container.setVisible(False)
            self._show_analysis_popout()
            self.analysis_label.setVisible(False)
            self._refresh_additional_visibility()
            return
        if display == "Main Side-by-side":
            self._hide_analysis_popout()
            self.analysis_container.setVisible(False)
            self.analysis_label.setVisible(True)
            self._refresh_additional_visibility()
            return
        # Main Overlay
        self._hide_analysis_popout()
        self.analysis_container.setVisible(False)
        self.analysis_label.setVisible(False)
        self._refresh_additional_visibility()

    def _update_analysis_controls_state(self, _state: int | None = None):
        mode = self.analysis_mode_combo.currentText()
        is_flow = mode == "Optical Flow"
        is_main_overlay = self.analysis_display_combo.currentText() == "Main Overlay"
        self.analysis_arrows_cb.setEnabled(is_flow)
        self.analysis_hide_zero_flow_cb.setEnabled(is_flow and self.analysis_arrows_cb.isChecked())
        zero_flow_enabled = (
            is_flow
            and self.analysis_arrows_cb.isChecked()
            and self.analysis_hide_zero_flow_cb.isChecked()
        )
        self.analysis_zero_flow_label.setEnabled(zero_flow_enabled)
        self.analysis_zero_flow_slider.setEnabled(zero_flow_enabled)
        self.analysis_arrow_step_slider.setEnabled(is_flow and self.analysis_arrows_cb.isChecked())
        self.analysis_arrow_scale_slider.setEnabled(is_flow and self.analysis_arrows_cb.isChecked())
        self.analysis_scale_slider.setEnabled(is_flow)
        self.analysis_arrow_step_label.setEnabled(is_flow)
        self.analysis_arrow_scale_label.setEnabled(is_flow)
        self.analysis_scale_label.setEnabled(is_flow)
        self.analysis_overlay_cb.setEnabled(not is_main_overlay)
        self.analysis_alpha_slider.setEnabled(not is_main_overlay)
        self.analysis_alpha_label.setEnabled(not is_main_overlay)

    def _show_analysis_popout(self):
        if self._analysis_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Analysis View")
            win.resize(800, 450)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            label = VideoFrameLabel("Analysis view")
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout.addWidget(label, 1)
            win.setLayout(layout)
            win.destroyed.connect(lambda _=None: self._clear_analysis_popout())
            self._analysis_window = win
            self._analysis_window_label = label
        self._analysis_window.show()

    def _hide_analysis_popout(self):
        if self._analysis_window is not None:
            self._analysis_window.hide()

    def _clear_analysis_popout(self):
        self._analysis_window = None
        self._analysis_window_label = None

    def _refresh_additional_visibility(self):
        display = self.analysis_display_combo.currentText()
        show_side_by_side = display == "Main Side-by-side" and self.analysis_mode_combo.currentText() != "Off"
        if show_side_by_side:
            self.additional_video_label.setVisible(False)
            return
        if self._draw_additional_video and self.additional_video_label is not None:
            self.additional_video_label.setVisible(True)
        else:
            self.additional_video_label.setVisible(False)
        QTimer.singleShot(0, self._place_view_menu)

    def _on_analysis_main_alpha_changed(self, v: int):
        a = v / 100.0
        self.analysis_main_alpha_label.setText(f"Overlay: {a:.2f}")
        self._request_video_label_update()

    def _set_analysis_reference(self):
        rgb = self._current_frame_rgb()
        if rgb is None:
            return
        # Safe to hold by reference: frame buffers are never mutated in place.
        self.analysis_ref_frame_rgb = rgb
        self.analysis_ref_frame_index = int(self.current_frame)
        self._update_analysis_view()

    def _clear_analysis_reference(self):
        self.analysis_ref_frame_rgb = None
        self.analysis_ref_frame_index = None
        self._update_analysis_view()

    def _on_analysis_gain_changed(self, v: int):
        self.analysis_gain_label.setText(f"Gain: {v}x")
        self._update_analysis_view()

    def _on_analysis_thresh_changed(self, v: int):
        self.analysis_thresh_label.setText(f"Threshold / Min motion: {v}")
        self._update_analysis_view()

    def _on_analysis_alpha_changed(self, v: int):
        a = v / 100.0
        self.analysis_alpha_label.setText(f"Overlay alpha: {a:.2f}")
        self._update_analysis_view()

    def _on_analysis_scale_changed(self, v: int):
        self.analysis_scale_label.setText(f"Compute scale: {v}%")
        self._update_analysis_view()

    def _on_analysis_arrow_step_changed(self, v: int):
        self.analysis_arrow_step_label.setText(f"Arrow step: {v} px")
        self._update_analysis_view()

    def _on_analysis_arrow_scale_changed(self, v: int):
        s = v / 10.0
        self.analysis_arrow_scale_label.setText(f"Arrow length scale: {s:.1f}x")
        self._update_analysis_view()

    def _analysis_zero_flow_value(self) -> float:
        return self.analysis_zero_flow_slider.value() / 20.0

    def _update_analysis_zero_flow_label(self):
        v = self._analysis_zero_flow_value()
        self.analysis_zero_flow_label.setText(f"Min flow: {v:.2f}")

    def _on_analysis_zero_flow_changed(self, _v: int):
        self._update_analysis_zero_flow_label()
        self._update_analysis_view()

    def _analysis_base_frame(self) -> tuple[np.ndarray | None, str]:
        pairing = self.analysis_pair_combo.currentText()
        if pairing.startswith("Reference"):
            if self.analysis_ref_frame_rgb is None:
                return None, "Set a reference frame first."
            label = "Reference frame"
            if self.analysis_ref_frame_index is not None:
                label += f": {self.analysis_ref_frame_index}"
            return self.analysis_ref_frame_rgb, label
        prev_rgb = self._previous_frame_rgb()
        if prev_rgb is None:
            return None, "No previous frame yet (scrub at least once)."
        label = "Previous frame"
        if self.analysis_prev_frame_index is not None:
            label += f": {self.analysis_prev_frame_index}"
        return prev_rgb, label

    def _compute_analysis_output(self) -> tuple[np.ndarray | None, str]:
        if self.analysis_mode_combo.currentText() == "Off":
            return None, ""
        frame_rgb = self._current_frame_rgb()
        if frame_rgb is None:
            return None, "Analysis view (no frame)"
        base_rgb, base_info = self._analysis_base_frame()
        if base_rgb is None:
            return None, f"Analysis view ({base_info})"
        if base_rgb.shape != frame_rgb.shape:
            h, w = frame_rgb.shape[:2]
            base_rgb = cv2.resize(base_rgb, (w, h), interpolation=cv2.INTER_AREA)
            base_info = f"{base_info} (resized)"

        gain = float(self.analysis_gain_slider.value())
        thresh = int(self.analysis_thresh_slider.value())
        heatmap = self.analysis_heatmap_cb.isChecked()
        overlay = self.analysis_overlay_cb.isChecked()
        if self.analysis_display_combo.currentText() == "Main Overlay":
            overlay = False
        alpha = self.analysis_alpha_slider.value() / 100.0
        compute_scale = self.analysis_scale_slider.value() / 100.0
        arrows = self.analysis_arrows_cb.isChecked()
        arrow_step = int(self.analysis_arrow_step_slider.value())
        arrow_scale = float(self.analysis_arrow_scale_slider.value()) / 10.0

        mode = self.analysis_mode_combo.currentText()
        if mode == "Frame Diff":
            out_rgb = compute_pixel_diff_view(
                frame_rgb=frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                threshold=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
            )
        else:
            out_rgb = compute_optical_flow_view(
                frame_rgb=frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                min_motion=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
                arrows=arrows,
                arrow_step=arrow_step,
                arrow_scale=arrow_scale,
                compute_scale=compute_scale,
                arrow_min_mag=(
                    self._analysis_zero_flow_value()
                    if self.analysis_hide_zero_flow_cb.isChecked()
                    else None
                ),
            )
        tooltip = f"{mode}\n{base_info}\nCurrent frame: {self.current_frame}"
        return out_rgb, tooltip

    def _update_analysis_view(self, _state: int | None = None):
        if self.analysis_mode_combo.currentText() == "Off":
            self.analysis_label.setText("Analysis view")
            self.analysis_label.setToolTip("")
            self.analysis_label.set_frame(None)
            if self._analysis_window_label is not None:
                self._analysis_window_label.setText("Analysis view")
                self._analysis_window_label.set_frame(None)
            return
        out_rgb, tooltip = self._compute_analysis_output()
        if out_rgb is None:
            msg = tooltip or "Analysis view"
            self.analysis_label.setText(msg)
            self.analysis_label.setToolTip("")
            self.analysis_label.set_frame(None)
            if self._analysis_window_label is not None:
                self._analysis_window_label.setText(msg)
                self._analysis_window_label.setToolTip("")
                self._analysis_window_label.set_frame(None)
            return

        h, w, ch = out_rgb.shape
        bytes_per_line = out_rgb.strides[0]
        qimg = QImage(out_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888).copy()
        if self.analysis_display_combo.currentText() == "Popout":
            self.analysis_label.set_frame(None)
            self.analysis_label.setToolTip("")
            if self._analysis_window_label is not None:
                self._analysis_window_label.set_frame(qimg)
                self._analysis_window_label.setToolTip(tooltip)
        else:
            self.analysis_label.set_frame(qimg)
            self.analysis_label.setToolTip(tooltip)
            if self._analysis_window_label is not None:
                self._analysis_window_label.set_frame(None)

    def update_sync_button_label(self):
        """Update the sync button text to include the first log time (if known)."""
        if self.first_log_time_str:
            self.sync_start_btn.setText(
                f"Sync logs to current video (first log: {self.first_log_time_str})"
            )
        else:
            self.sync_start_btn.setText("Sync logs to current video (first log)")

    def _save_settings_from_tab(self):
        if self._settings_autosave_timer.isActive():
            self._settings_autosave_timer.stop()
        t0 = time.perf_counter()
        self.settings_panel.apply_to(self.settings)
        self.system_layout_panel.apply_to(self.settings)
        t_apply = time.perf_counter()
        self.settings.save()
        t_save = time.perf_counter()
        self.settings_saved.emit()
        t_emit = time.perf_counter()
        if (t_emit - t0) > 0.1:
            print(
                f"[settings-save] apply={((t_apply - t0) * 1000):.0f}ms "
                f"save={((t_save - t_apply) * 1000):.0f}ms "
                f"reload-reaction={((t_emit - t_save) * 1000):.0f}ms",
                flush=True,
            )

    def _flush_settings_autosave(self):
        if self._settings_autosave_timer.isActive():
            self._settings_autosave_timer.stop()
        self._save_settings_from_tab()

    def _on_export_settings(self):
        # Flush any pending edits so the exported file reflects what's on screen.
        self._flush_settings_autosave()
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export settings",
            "logfather-settings.json",
            "JSON files (*.json)",
        )
        if not path_str:
            return
        try:
            self.settings.export_shareable(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export settings:\n{exc}")
            return
        QMessageBox.information(self, "Export complete", f"Settings exported to:\n{path_str}")

    def _on_import_settings(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import settings",
            "",
            "JSON files (*.json)",
        )
        if not path_str:
            return
        confirm = QMessageBox.question(
            self,
            "Import settings",
            "Importing will replace your current filters, conditions, presets, "
            "customers and system layouts.\n\n"
            "Your Elastic API key and PikPak parent folder will be kept.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.settings.import_shareable(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", f"Could not import settings:\n{exc}")
            return
        # Persist immediately and refresh the UI panels so the user sees the
        # imported values without needing to restart.
        self.settings.save()
        self.settings_panel.reload_from_settings()
        try:
            self.system_layout_panel.reload_from_settings()
        except AttributeError:
            pass
        self._load_custom_filter_settings()
        self._load_filter_preset_settings()
        QMessageBox.information(
            self,
            "Import complete",
            "Settings imported. Some changes (such as Elastic URL) may only "
            "take effect after reloading data.",
        )

    def _update_sync_button_style(self):
        """Green with the offset in the text once a sync is in force, e.g.
        "Sync: +1.3s" (Chris, 2026-09-12); plain "Sync" otherwise."""
        def label(done: bool, offset, clip_open: bool) -> str:
            if done and offset is not None:
                return f"Sync: {float(offset):+.1f}s"
            return "Sync: ?" if clip_open else "Sync"

        pulser = self._sync_pulser
        clip_open = self.cap is not None
        self.video_sync_btn.setText(label(self._main_sync_done, self.ocr_offset_seconds, clip_open))
        if self._main_sync_done:
            if pulser is not None:
                pulser.set_target(None)
            self.video_sync_btn.setStyleSheet(theme.SYNC_DONE_BUTTON)
        elif clip_open and pulser is not None:
            if pulser.target() is not self.video_sync_btn:
                self.video_sync_btn.setStyleSheet("")
                pulser.set_target(self.video_sync_btn)
        else:
            if pulser is not None:
                pulser.set_target(None)
            self.video_sync_btn.setStyleSheet("")
        second_open = self.additional_cap is not None
        self.additional_sync_btn.setText(label(self._additional_sync_done, self.additional_ocr_offset_seconds, second_open))
        self.additional_sync_btn.setStyleSheet(theme.SYNC_DONE_BUTTON if self._additional_sync_done else "")

    def _set_filter_tabs_enabled(self, enabled: bool):
        tab_bar = self.right_tabs.tabBar()
        default_color = self.palette().color(QPalette.WindowText)
        disabled_color = QColor("#888888")
        filter_idx = self.right_tabs.indexOf(self.filter_container)
        if filter_idx >= 0:
            self.right_tabs.setTabEnabled(filter_idx, enabled)
            tab_bar.setTabTextColor(filter_idx, default_color if enabled else disabled_color)
        custom_idx = self.right_tabs.indexOf(self._custom_tab)
        if custom_idx >= 0:
            self.right_tabs.setTabEnabled(custom_idx, enabled)
            tab_bar.setTabTextColor(custom_idx, default_color if enabled else disabled_color)

    def _load_custom_filter_settings(self):
        presets = getattr(self.settings, "custom_filters", [])
        if not presets:
            return
        for preset, block in zip(presets, self.custom_filter_blocks):
            btn, in_edit, out_edit, _count_label = block
            if preset.name:
                btn.setText(preset.name)
            in_edit.setText(preset.filter_in or "")
            out_edit.setText(preset.filter_out or "")
            btn.setChecked(bool(preset.enabled))
        self._update_custom_filter_counts()
        self._update_tab_highlights()

    def _save_custom_filter_settings(self):
        presets: list[CustomFilterPreset] = []
        for btn, in_edit, out_edit, _count_label in self.custom_filter_blocks:
            presets.append(
                CustomFilterPreset(
                    name=btn.text(),
                    filter_in=in_edit.text(),
                    filter_out=out_edit.text(),
                    enabled=btn.isChecked(),
                )
            )
        self.settings.custom_filters = presets
        self.settings.save()

    def _load_filter_preset_settings(self):
        presets = getattr(self.settings, "filter_presets", [])
        if not presets:
            return
        for preset, btn in zip(presets, self.filter_preset_group):
            if preset.name:
                btn.setText(preset.name)

    def _save_filter_preset_settings(self):
        presets: list[FilterPreset] = []
        for idx, btn in enumerate(self.filter_preset_group):
            if idx < len(self.settings.filter_presets):
                existing = self.settings.filter_presets[idx]
                presets.append(
                    FilterPreset(
                        name=btn.text(),
                        sources=list(existing.sources),
                        states=list(existing.states),
                        messages=list(existing.messages),
                    )
                )
            else:
                presets.append(FilterPreset(name=btn.text()))
        self.settings.filter_presets = presets
        self.settings.save()

    def set_pending_logs(self, pikpak_path: str, start_iso: str, end_iso: str):
        self.pending_pikpak_path = pikpak_path
        self.pending_start_iso = start_iso
        self.pending_end_iso = end_iso
        self._pending_log_request_key = (str(pikpak_path), str(start_iso), str(end_iso))
        if self.auto_load_clip_logs:
            self._pending_log_autoload_timer.start()

    def _auto_load_pending_logs(self):
        if not self.pending_pikpak_path or not self.pending_start_iso or not self.pending_end_iso:
            return
        request_key = (str(self.pending_pikpak_path), str(self.pending_start_iso), str(self.pending_end_iso))
        if self._loaded_log_request_key == request_key and self.all_events:
            return
        if self._active_log_request_key == request_key and self._log_future is not None:
            return
        self.load_logs_from_elastic(
            self.pending_pikpak_path,
            self.pending_start_iso,
            self.pending_end_iso,
            show_busy=False,
        )

    # ---- ffmpeg rewrap helper ----

    def try_rewrap_video_with_ffmpeg(self, file_path: str) -> str | None:
        """
        Use ffmpeg to losslessly rewrap the video:
        ffmpeg -i input -c copy output
        Returns the new path on success, or None on failure.
        """
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            return None

        in_path = Path(file_path)
        try:
            cache_path = self._cache_path_for(in_path)
        except Exception:
            cache_path = None

        if cache_path is None:
            out_path = in_path.with_name(in_path.stem + "_fixed" + in_path.suffix)
            stage_path = in_path
        else:
            out_path = cache_path
            stage_path = cache_path.with_name(cache_path.stem + "_source" + cache_path.suffix)

        # If we've already created it before, reuse it
        if out_path.exists():
            return str(out_path)

        # Ensure local staged copy before rewrap
        if stage_path != in_path:
            try:
                if stage_path.exists():
                    stage_path.unlink()
                shutil.copy2(in_path, stage_path)
            except Exception as exc:
                QMessageBox.warning(self, "Copy failed", f"Unable to stage video locally:\n{exc}")
                return None

        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(stage_path),
            "-c", "copy",
            str(out_path),
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                print(f"[viewer] video rewrapped with ffmpeg: {out_path.name}", flush=True)
                if stage_path != in_path:
                    stage_path.unlink(missing_ok=True)
                self.update_cache_status()
                return str(out_path)
            else:
                # Uncomment to debug ffmpeg errors:
                # QMessageBox.warning(self, "ffmpeg error", proc.stderr[:500])
                if stage_path != in_path:
                    stage_path.unlink(missing_ok=True)
                return None
        except Exception:
            if stage_path != in_path:
                stage_path.unlink(missing_ok=True)
            return None

    # ---- Video handling ----

    def load_video_from_path(self, file_path: str) -> bool:
        t0 = time.perf_counter()
        print(f"[viewer] load_video_from_path start: {file_path}", flush=True)
        # Supersede any download still pending from a previous clip choice.
        self._video_load_generation += 1
        self._pending_video_load = None
        self._pending_seek = None
        self._set_video_busy(False)
        if self.cap is not None:
            self.cap.release()
            self.cap = None

        path_obj = Path(file_path)
        if not path_obj.exists():
            QMessageBox.warning(self, "File not found", file_path)
            return False

        self.current_video_path = file_path

        # Prefer a local cached copy to avoid network read timeouts/crashes.
        cache_path = None
        try:
            cache_path = self._cache_path_for(path_obj)
        except Exception:
            cache_path = None
        if cache_path is not None and not self._is_cached_copy_current(path_obj, cache_path):
            # Download on the cache executor and finish loading when it lands
            # (ClipCache.transfer_finished) — copying from the CCTV share takes ~15-20s
            # per clip and must not freeze the UI. (Streaming straight from
            # the share while downloading was tried and reverted: too slow.)
            self._video_load_t0 = t0
            self._begin_async_video_download(path_obj, cache_path)
            return True
        if cache_path is not None and cache_path.exists():
            self._touch_cache_entry(cache_path)

        open_path = str(cache_path) if cache_path and cache_path.exists() else file_path
        return self._open_downloaded_video(open_path, path_obj, t0)

    def _begin_async_video_download(self, path_obj: Path, cache_path: Path) -> None:
        self._pending_video_load = (self._video_load_generation, path_obj, cache_path)
        self._set_video_busy(True, f"Downloading {path_obj.name} from the CCTV share...")
        self.clip_cache.download_with_priority(path_obj, cache_path)

    def _set_video_busy(self, busy: bool, message: str | None = None):
        if busy:
            if self._video_busy_dialog is None:
                dlg = QProgressDialog(message or "Working...", None, 0, 0, self)
                dlg.setWindowTitle("Loading clip")
                dlg.setCancelButton(None)
                dlg.setWindowModality(Qt.NonModal)
                dlg.setMinimumDuration(0)
                dlg.setRange(0, 0)
                self._video_busy_dialog = dlg
            self._video_busy_dialog.setLabelText(message or "Working...")
            self._video_busy_dialog.show()
        elif self._video_busy_dialog is not None:
            self._video_busy_dialog.close()
            self._video_busy_dialog = None

    def show_download_progress(self, source_path: str, done: int, total: int, text: str) -> None:
        """The 'Loading clip' dialog shows the same size, percentage and
        rate as the activity bar (Chris, 2026-09-08: the bar sits at the
        bottom of the window, off screen on a small laptop)."""
        pending = self._pending_video_load
        dlg = self._video_busy_dialog
        if pending is None or dlg is None or str(pending[1]) != source_path:
            return
        if total:
            dlg.setRange(0, 1000)
            dlg.setValue(min(1000, int(done * 1000 / total)))
        dlg.setLabelText(text)

    def _finish_pending_video_load(self, source_path: str, ok: bool) -> None:
        pending = self._pending_video_load
        if pending is None:
            return
        generation, p_source, p_cache = pending
        if str(p_source) != source_path:
            return
        self._pending_video_load = None
        self._set_video_busy(False)
        if generation != self._video_load_generation:
            return  # a different clip was chosen while this one downloaded
        print(
            f"[viewer] async cache copy finished (ok={ok}) after "
            f"{time.perf_counter() - self._video_load_t0:.2f}s",
            flush=True,
        )
        open_path = str(p_cache) if ok and p_cache.exists() else str(p_source)
        self._open_downloaded_video(open_path, p_source, self._video_load_t0)

    def _open_downloaded_video(self, open_path: str, path_obj: Path, t0: float) -> bool:
        self.current_video_path = open_path
        t_open = time.perf_counter()
        self.cap = cv2.VideoCapture(open_path)
        if not self.cap.isOpened():
            if self.cap is not None:
                self.cap.release()
            self.cap = None
            # Try to rewrap with ffmpeg only if direct open fails.
            fixed_path = self.try_rewrap_video_with_ffmpeg(open_path)
            if fixed_path:
                self.current_video_path = fixed_path
                self.cap = cv2.VideoCapture(fixed_path)
        if self.cap is None or not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Failed to open video:\n{self.current_video_path}")
            if self.cap is not None:
                self.cap.release()
            self.cap = None
            return False
        print(f"[viewer] video opened: {self.current_video_path}", flush=True)
        print(f"[viewer] VideoCapture open took {time.perf_counter() - t_open:.2f}s", flush=True)

        t_meta = time.perf_counter()
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        print(f"[viewer] metadata read took {time.perf_counter() - t_meta:.2f}s", flush=True)
        self.current_frame = 0
        self.time_offset = 0.0
        self.set_offset_value(0.0)
        self.seek_slider.setRange(0, max(0, self.frame_count - 1))
        # Defer first render to the event loop to avoid Qt widget crashes during load.
        if not SKIP_INITIAL_FRAME_RENDER:
            QTimer.singleShot(0, lambda: self.show_frame(self.current_frame))

        # Refresh sync button text (in case a CSV is already loaded)
        self.update_sync_button_label()
        self.update_cache_status()
        self.log_markers_enabled = False
        self._set_log_markers([])
        self.set_timeline_markers([])
        self.video_start_dt = None
        self.ocr_offset_seconds = None
        self.ocr_frame_offset = 0
        self._main_sync_done = False
        self._update_sync_button_style()
        self.video_sync_btn.setEnabled(False)
        self.current_video_filename_dt = parse_filename_datetime(path_obj)
        self.video_sync_btn.setEnabled(True)
        self._update_sync_button_style()
        # Must be set before _load_clip_annotations(): the annotations key is
        # derived from the original share path, and the fallback (the cache
        # copy path, or a stale previous clip) hashes to a different key.
        self.current_video_original_path = path_obj
        self._load_clip_annotations()
        key = self._offset_cache_key(Path(self.current_video_path))
        cached = self.offset_store.get(key)
        if cached:
            try:
                self.ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.ocr_offset_seconds = None
                self.ocr_frame_offset = 0
            if self.ocr_offset_seconds is not None and not plausible_ocr_offset(self.ocr_offset_seconds):
                print(f"[ocr] cached offset {self.ocr_offset_seconds:.0f}s for {key} is not plausible; dropped, using the filename time", flush=True)
                self.offset_store.remove(key)
                self.ocr_offset_seconds = None
                self.ocr_frame_offset = 0
            if self.ocr_offset_seconds is not None:
                filename_dt = parse_filename_datetime(self.current_video_path)
                if filename_dt:
                    self.video_start_dt = filename_dt + timedelta(seconds=self.ocr_offset_seconds)
                    self._apply_auto_sync_if_possible()
                    self._main_sync_done = True
                    self._update_sync_button_style()
        if self.ocr_offset_seconds is None:
            settings = Settings.load()
            if settings.auto_ocr_open_on_missing:
                self._auto_sync_with_ocr()
            elif self.first_log_dt is not None and self._confirm_ocr_sync():
                self._auto_sync_with_ocr()
        pending_seek = self._pending_seek
        self._pending_seek = None
        if pending_seek is not None and pending_seek[0] == self._video_load_generation:
            _, seek_seconds, seek_pause = pending_seek
            QTimer.singleShot(
                0, lambda: self.seek_to_seconds(seek_seconds, pause=seek_pause)
            )
        print(f"[viewer] load_video_from_path total {time.perf_counter() - t0:.2f}s", flush=True)
        if self._play_after_open:
            self._play_after_open = False
            QTimer.singleShot(0, self.play)
        self.clip_opened.emit(path_obj)
        return True

    # Prefetch caching disabled (was slowing clip switching)

    def _confirm_ocr_sync(self) -> bool:
        if self.current_video_path:
            key = self._offset_cache_key(Path(self.current_video_path))
            if self.offset_store.get(key):
                return True
        settings = Settings.load()
        if not settings.auto_ocr_sync:
            return False
        if self._ocr_sync_prompt_choice is not None:
            return self._ocr_sync_prompt_choice
        remember_cb = QCheckBox("Remember my choice for this session")
        msg = QMessageBox(self)
        msg.setWindowTitle("Auto OCR sync")
        msg.setText("Run OCR time sync for this video?")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setCheckBox(remember_cb)
        resp = msg.exec()
        choice = resp == QMessageBox.Yes
        if remember_cb.isChecked():
            self._ocr_sync_prompt_choice = choice
        return choice

    def set_footage_notice(self, text: str | None) -> None:
        """A warning in the video area instead of footage (Chris,
        2026-09-07: the chosen day is past the CCTV retention)."""
        self.video_label.set_notice(text)
        if self._popout_label is not None:
            self._popout_label.set_notice(text)

    def prepare_for_new_clip(self, show_loading: bool = True):
        self.pause()
        if show_loading:
            self.set_footage_notice(None)
        self._cancel_log_future()
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.current_video_path = None
        self.last_qimage = None
        self._cur_frame_bgr = None
        self._cur_frame_rgb = None
        self._prev_frame_bgr = None
        self._prev_frame_rgb = None
        self._last_frame_index = None
        self.analysis_prev_frame_index = None
        self.analysis_label.setText("Analysis view")
        self.analysis_label.setToolTip("")
        self.analysis_label.set_frame(None)
        placeholder = "Loading video..." if show_loading else "No video loaded"
        self.video_label.set_frame(None)
        self.video_label.set_placeholder_text(placeholder)
        if self._popout_label is not None:
            self._popout_label.set_frame(None)
            self._popout_label.set_placeholder_text(placeholder)
        self.seek_slider.setRange(0, 0)
        if hasattr(self.seek_slider, "clear_clip_range"):
            self.seek_slider.clear_clip_range()
        self.current_frame = 0
        self.frame_count = 0
        self.info_label.display("00:00:00.000")
        self.calc_label.display("00:00:00.000")
        self.frame_label.display("0")
        self.log_markers_enabled = False
        self.log_markers = []
        self.external_markers = []
        self.external_marker_source = None
        self.event_marker_bar.clear()
        self.timeline_marker_bar.clear()
        self._clip_annotations = []
        self._annotation_history = []
        self._refresh_annotation_view()
        self.events = []
        self._event_start_times: list[float] = []
        self.log_display_rows = []
        self.all_events = []
        self.all_log_display_rows = []
        self.all_source_keys = []
        self.all_state_keys = []
        self.all_message_keys = []
        self._sku_timeline_items = []
        self._rebuild_ppm_model()
        self.video_label.set_status_lines([])
        if self._popout_label is not None:
            self._popout_label.set_status_lines([])
        self.video_start_dt = None
        self.ocr_offset_seconds = None
        self.ocr_frame_offset = 0
        self._auto_ocr_attempted_key = None
        self.current_video_original_path = None
        self.current_video_filename_dt = None
        self._reset_additional_video()
        self.pending_pikpak_path = None
        self.pending_start_iso = None
        self.pending_end_iso = None
        self._pending_log_request_key = None
        self._active_log_request_key = None
        self._loaded_log_request_key = None
        self._pending_log_autoload_timer.stop()
        self.populate_log_list()
        self._reset_filter_state(show_busy=False)
        self._set_log_busy(False)
        self.filter_panel.setVisible(False)
        self._set_filter_tabs_enabled(False)

    def _apply_loaded_events(self, events, display_rows, source_keys, state_keys, message_keys, first_dt):
        print("[viewer] _apply_loaded_events start", flush=True)
        self._set_log_busy(True, "Processing Elastic events...")
        self.all_events = events or []
        self.all_log_display_rows = display_rows or []
        self.all_source_keys = source_keys or []
        self.all_state_keys = state_keys or []
        self.all_message_keys = message_keys or []
        self._rebuild_ppm_model()
        print(f"[viewer] array copies done (events={len(self.all_events)})", flush=True)

        self.sync_offset = 0.0
        self.time_offset = 0.0
        self.set_offset_value(0.0)

        print(f"[viewer] total events: {len(self.all_events)}, display rows: {len(self.all_log_display_rows)}", flush=True)
        self._reset_filter_state()
        self.events = list(self.all_events)
        self.log_display_rows = list(self.all_log_display_rows)
        self._rebuild_event_start_times()
        self.populate_log_list()

        self.first_log_dt = _to_local_naive(first_dt)
        if self.all_log_display_rows:
            first_row = self.all_log_display_rows[0]
            self.first_log_time_str = first_row.split("  |", 1)[0].strip()
        else:
            self.first_log_time_str = None
        self.update_sync_button_label()
        self._set_log_busy(False)
        self._update_timeline_markers()
        if not self.filters_loaded:
            self.load_filters_panel()
        self.apply_filters(manage_busy=False)
        self._apply_auto_sync_if_possible()
        if self.current_video_path and self.ocr_offset_seconds is None:
            settings = Settings.load()
            if settings.auto_ocr_open_on_missing or self._confirm_ocr_sync():
                self._auto_sync_with_ocr()
        self._update_tab_highlights()
        self._set_filter_tabs_enabled(True)

    def set_timeline_markers(self, markers: list[tuple[float, str]] | None, source: str | None = None):
        markers = markers or []
        self.external_markers = markers
        self.external_marker_source = source or "clip_relative"
        self._refresh_timeline_marker_bar()

    def set_clip_marker_fallback(self, markers: list[tuple[float, str]] | None):
        """Populate the lower marker bar until clip logs are loaded and synced."""
        if self.events or self.cap is None:
            return
        self._set_log_markers(markers or [])

    def _set_log_markers(self, markers: list[tuple[float, str]] | None):
        markers = markers or []
        self.log_markers = markers
        if markers:
            self.log_markers_enabled = True
        self._refresh_marker_bar()

    def _refresh_marker_bar(self):
        duration = 0.0
        if self.fps and self.fps > 0:
            duration = (self.frame_count or 0) / self.fps
        if (
            duration <= 0.0
            or not self.log_markers
            or not self.log_markers_enabled
        ):
            self.event_marker_bar.set_markers([])
            return
        ratios: list[tuple[float, str]] = []
        for offset, color in self.log_markers:
            try:
                offset_val = float(offset)
            except (TypeError, ValueError):
                continue
            if offset_val < 0.0 or offset_val > duration:
                continue
            ratio = offset_val / duration
            ratios.append((ratio, color))
        self.event_marker_bar.set_markers(ratios)

    def _refresh_timeline_marker_bar(self):
        duration = 0.0
        if self.fps and self.fps > 0:
            duration = (self.frame_count or 0) / self.fps
        if duration <= 0.0 or not self.external_markers:
            self.timeline_marker_bar.set_markers([])
            return
        ratios: list[tuple[float, str]] = []
        offset_adjust = 0.0
        if self.external_marker_source in {"absolute", "clip_relative"} and self.ocr_offset_seconds is not None:
            offset_adjust = -float(self.ocr_offset_seconds)
        for offset, color in self.external_markers:
            try:
                offset_val = float(offset) + offset_adjust
            except (TypeError, ValueError):
                continue
            if offset_val < 0.0 or offset_val > duration:
                continue
            ratio = offset_val / duration
            ratios.append((ratio, color))
        self.timeline_marker_bar.set_markers(ratios)

    def _rebuild_event_start_times(self) -> None:
        self._event_start_times = [ev.start.total_seconds() for ev in self.events]

    def _clear_events(self):
        self.all_events = []
        self.all_log_display_rows = []
        self.all_source_keys = []
        self.all_state_keys = []
        self.all_message_keys = []
        self._rebuild_ppm_model()
        self.events = []
        self._event_start_times = []
        self.log_display_rows = []
        self.populate_log_list()
        self._reset_filter_state(show_busy=False)
        self.first_log_time_str = None
        self.first_log_dt = None
        self.update_sync_button_label()
        self._set_log_busy(False)
        self.log_markers_enabled = False
        self.log_markers = []
        self._refresh_marker_bar()
        self._set_filter_tabs_enabled(False)

    # ---- Filter UI helpers ----
    # (unchanged from your version)

    def _remember_unchecked_filters(self):
        """Snapshot what the user has unticked before the panels are
        rebuilt. Empty dicts mean nothing was built - keep the old memory."""
        for kind, boxes in (
            ("source", self.source_checkboxes),
            ("state", self.state_checkboxes),
            ("message", self.message_checkboxes),
        ):
            if not boxes:
                continue
            unchecked: set[str] = set()
            for key, box in boxes.items():
                try:
                    if not box.isChecked():
                        unchecked.add(key)
                except RuntimeError:
                    continue
            self._remembered_unchecked[kind] = unchecked

    def clear_filter_checkboxes(self, show_busy: bool = True):
        self._remember_unchecked_filters()
        if show_busy:
            self._set_log_busy(True, "Resetting source filters...")
        self._reset_source_panel()
        if show_busy:
            QApplication.processEvents()
            self._set_log_busy(True, "Resetting state filters...")
        self._reset_state_panel()
        if show_busy:
            QApplication.processEvents()
            self._set_log_busy(True, "Resetting message filters...")
        self._reset_message_panel()
        if show_busy:
            QApplication.processEvents()
        self.source_checkboxes.clear()
        self.state_checkboxes.clear()
        self.message_checkboxes.clear()
        if show_busy:
            self._set_log_busy(False)

    def _reset_filter_state(self, show_busy: bool = False):
        if self.filters_loaded:
            self._filters_wanted = True
        self.filters_loaded = False
        self.clear_filter_checkboxes(show_busy=show_busy)
        self.filter_panel.setVisible(False)

    def _reset_source_panel(self):
        print("[viewer] resetting source panel", flush=True)
        if self.source_container_widget is not None:
            self.source_container_widget.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.source_container_widget = new_widget
        self.source_layout_inner = new_layout
        self.source_scroll.setWidget(new_widget)

    def _reset_message_panel(self):
        print("[viewer] resetting message panel", flush=True)
        if self.message_container_widget is not None:
            self.message_container_widget.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.message_container_widget = new_widget
        self.message_layout_inner = new_layout
        self.message_scroll.setWidget(new_widget)

    def _reset_state_panel(self):
        print("[viewer] resetting state panel", flush=True)
        if self.state_container_widget is not None:
            self.state_container_widget.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.state_container_widget = new_widget
        self.state_layout_inner = new_layout
        self.state_scroll.setWidget(new_widget)

    def build_filter_checkboxes(self):
        if not self.filters_loaded:
            return
        self.clear_filter_checkboxes()

        # ----- Sources: build once from all rows -----
        unique_sources = sorted({k for k in self.all_source_keys if k})
        if unique_sources:
            last = self.source_layout_inner.takeAt(self.source_layout_inner.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)

            for key in unique_sources:
                cb = QCheckBox(key)
                cb.setChecked(key not in self._remembered_unchecked["source"])
                cb.stateChanged.connect(self.on_source_checkbox_changed)
                self.source_layout_inner.addWidget(cb)
                self.source_checkboxes[key] = cb

            self.source_layout_inner.addStretch(1)

        # ----- States: build once from all rows -----
        unique_states = sorted({k if k else "(null)" for k in self.all_state_keys})
        if unique_states:
            last = self.state_layout_inner.takeAt(self.state_layout_inner.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)

            for key in unique_states:
                cb = QCheckBox(key)
                cb.setChecked(key not in self._remembered_unchecked["state"])
                cb.stateChanged.connect(self.on_state_checkbox_changed)
                self.state_layout_inner.addWidget(cb)
                self.state_checkboxes[key] = cb

            self.state_layout_inner.addStretch(1)

        # ----- Messages: build once from all rows -----
        unique_messages = sorted({k for k in self.all_message_keys if k})
        if unique_messages:
            last = self.message_layout_inner.takeAt(self.message_layout_inner.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)

            for key in unique_messages:
                cb = QCheckBox(key)
                cb.setChecked(key not in self._remembered_unchecked["message"])
                cb.stateChanged.connect(self.on_message_checkbox_changed)
                self.message_layout_inner.addWidget(cb)
                self.message_checkboxes[key] = cb

        self.message_layout_inner.addStretch(1)

        self.update_message_visibility_from_filters()

    def load_filters_panel(self):
        if not self.all_events:
            QMessageBox.information(self, "No logs", "Load a video/logs before loading filters.")
            return
        if self.filters_loaded:
            QMessageBox.information(self, "Filters already loaded", "Filters are already available.")
            return
        self.filters_loaded = True
        self._set_log_busy(True, "Resetting filters...")
        self.clear_filter_checkboxes(show_busy=False)
        self._set_log_busy(True, "Building filter lists...")
        self.build_filter_checkboxes()
        self._set_log_busy(True, "Applying filters and refreshing log list...")
        self.apply_filters(status_message="Applying filters...", manage_busy=False)
        self._set_log_busy(False)
        self.filter_panel.setVisible(True)
        print(
            f"[viewer] filter checkboxes built (sources={len(self.source_checkboxes)}, "
            f"states={len(self.state_checkboxes)}, messages={len(self.message_checkboxes)})",
            flush=True,
        )

    def update_message_visibility_from_filters(self):
        if (
            not self.filters_loaded
            or not self.all_events
            or not self.message_checkboxes
            or not self.state_checkboxes
        ):
            return

        source_filter_active = bool(self.source_checkboxes)
        state_filter_active = bool(self.state_checkboxes)
        include_empty_state = True
        allowed_sources = {
            key for key, cb in self.source_checkboxes.items() if cb.isChecked()
        } if source_filter_active else set()
        allowed_states = {
            key for key, cb in self.state_checkboxes.items() if cb.isChecked()
        } if state_filter_active else set()

        states_used = set()
        messages_used = set()
        for src, state, msg in zip(self.all_source_keys, self.all_state_keys, self.all_message_keys):
            if source_filter_active and src not in allowed_sources:
                continue
            state_val = state if state else "(null)"
            states_used.add(state_val)
            if state_filter_active and state_val not in allowed_states:
                continue
            if msg:
                messages_used.add(msg)

        for state_val, cb in self.state_checkboxes.items():
            if state_val == "(null)":
                cb.setVisible(True)
            else:
                cb.setVisible(state_val in states_used)
        for msg_val, cb in self.message_checkboxes.items():
            cb.setVisible(msg_val in messages_used)

    def on_source_checkbox_changed(self, _state):
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def on_state_checkbox_changed(self, _state):
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def on_message_checkbox_changed(self, _state):
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        self.apply_filters()

    def select_all_sources(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.source_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_no_sources(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.source_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_all_states(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.state_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_no_states(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.state_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_all_messages(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.message_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.apply_filters()

    def select_no_messages(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.message_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.apply_filters()

    def apply_filters(self, status_message: str | None = None, manage_busy: bool = True):
        print("[viewer] apply_filters start", flush=True)
        if not self.all_events:
            print("[viewer] apply_filters no events", flush=True)
            if manage_busy:
                self._set_log_busy(False)
            return
        if manage_busy:
            self._set_log_busy(True, status_message or "Applying filters...")

        if self.active_filter_presets:
            base_rows = self._collect_preset_filtered_rows()
        else:
            base_rows = self._collect_base_filtered_rows()
        self.events = []
        self.log_display_rows = []

        custom_filters = self._get_active_custom_filters()
        custom_mode = "OR"

        for ev, row_text in base_rows:
            if custom_filters:
                text = row_text
                if "  |  " in row_text:
                    text = row_text.split("  |  ", 1)[1]
                if not self._custom_filter_match(text, custom_filters, custom_mode):
                    continue
            self.events.append(ev)
            self.log_display_rows.append(row_text)

        self._rebuild_event_start_times()
        self.populate_log_list()

        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_time_and_overlay(t, self.current_frame)
            self.update_log_highlight(t)
        self._update_custom_filter_counts()
        self._update_timeline_markers()
        self._update_tab_highlights()
        if manage_busy:
            self._set_log_busy(False)
        print("[viewer] apply_filters done", flush=True)

    def _set_all_filters_checked(self):
        for cb in self.source_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        for cb in self.state_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        for cb in self.message_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()

    def _collect_base_filtered_rows(self) -> list[tuple[LogEvent, str]]:
        if not self.filters_loaded:
            return list(zip(self.all_events, self.all_log_display_rows))
        allowed_sources = {
            key for key, cb in self.source_checkboxes.items() if cb.isChecked()
        }
        allowed_states = {
            key for key, cb in self.state_checkboxes.items() if cb.isChecked()
        }
        allowed_messages = {
            key for key, cb in self.message_checkboxes.items()
            if cb.isChecked() and cb.isVisible()
        }

        source_filter_active = bool(self.source_checkboxes)
        state_filter_active = bool(self.state_checkboxes)
        message_filter_active = any(cb.isVisible() for cb in self.message_checkboxes.values())

        rows: list[tuple[LogEvent, str]] = []
        for ev, row_text, src, state, msg in zip(
            self.all_events,
            self.all_log_display_rows,
            self.all_source_keys,
            self.all_state_keys,
            self.all_message_keys,
        ):
            state_val = state if state else "(null)"
            if source_filter_active and src not in allowed_sources:
                continue
            if state_filter_active and state_val not in allowed_states:
                continue
            if message_filter_active and msg not in allowed_messages:
                continue
            rows.append((ev, row_text))
        return rows

    def _collect_preset_filtered_rows(self) -> list[tuple[LogEvent, str]]:
        presets = getattr(self.settings, "filter_presets", [])
        if not presets or not self.active_filter_presets:
            return list(zip(self.all_events, self.all_log_display_rows))
        active = [presets[i] for i in sorted(self.active_filter_presets) if i < len(presets)]
        rows: list[tuple[LogEvent, str]] = []
        for ev, row_text, src, state, msg in zip(
            self.all_events,
            self.all_log_display_rows,
            self.all_source_keys,
            self.all_state_keys,
            self.all_message_keys,
        ):
            state_val = state if state else "(null)"
            matched = False
            for preset in active:
                if preset.sources and src not in preset.sources:
                    continue
                if preset.states and state_val not in preset.states:
                    continue
                if preset.messages and msg not in preset.messages:
                    continue
                if not preset.sources and not preset.states and not preset.messages:
                    continue
                matched = True
                break
            if matched:
                rows.append((ev, row_text))
        return rows

    def _parse_custom_terms(self, text: str) -> list[str]:
        return [term.strip().lower() for term in text.split(",") if term.strip()]

    def _get_active_custom_filters(self) -> list[tuple[list[str], list[str]]]:
        filters: list[tuple[list[str], list[str]]] = []
        for btn, in_edit, out_edit, _count_label in self.custom_filter_blocks:
            if not btn.isChecked():
                continue
            in_terms = self._parse_custom_terms(in_edit.text())
            out_terms = self._parse_custom_terms(out_edit.text())
            if not in_terms and not out_terms:
                continue
            filters.append((in_terms, out_terms))
        return filters

    def _custom_filter_match(
        self,
        text: str,
        filters: list[tuple[list[str], list[str]]],
        mode: str,
    ) -> bool:
        if not filters:
            return True
        text_l = text.lower()
        for in_terms, out_terms in filters:
            include_ok = True
            if in_terms:
                if mode == "AND":
                    include_ok = all(term in text_l for term in in_terms)
                else:
                    include_ok = any(term in text_l for term in in_terms)
            if not include_ok:
                continue
            if out_terms and any(term in text_l for term in out_terms):
                continue
            return True
        return False

    def _on_custom_filter_changed(self, *_args):
        self._update_tab_highlights()
        if not self.all_events:
            return
        self.apply_filters()

    def _on_custom_filter_text_changed(self, button: QPushButton):
        if not self.all_events:
            return
        if not button.isChecked():
            return
        self._filter_debounce_timer.start()

    def _clear_active_filter_preset(self):
        if self.active_filter_preset_index is None and not self.active_filter_presets:
            return
        for btn in self.filter_preset_group:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        self.active_filter_preset_index = None
        self.active_filter_presets.clear()

    def _validate_custom_filter_inputs(self):
        for _btn, in_edit, out_edit, _count_label in self.custom_filter_blocks:
            for edit in (in_edit, out_edit):
                text = edit.text()
                has_empty = text.strip().startswith(",") or text.strip().endswith(",") or ",," in text
                if has_empty:
                    edit.setStyleSheet(theme.INPUT_WARNING_BORDER)
                    edit.setToolTip("Empty entries will be ignored.")
                else:
                    edit.setStyleSheet("")
                    edit.setToolTip("")

    def _on_custom_filter_menu(self, index: int):
        if index < 0 or index >= len(self.custom_filter_blocks):
            return
        btn, _in_edit, _out_edit, _count_label = self.custom_filter_blocks[index]
        menu = QMenu(self)
        save_action = menu.addAction("Save current selection")
        rename_action = menu.addAction("Rename")
        chosen = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen == rename_action:
            text, ok = QInputDialog.getText(self, "Rename preset", "Preset name:", text=btn.text())
            if ok and text.strip():
                btn.setText(text.strip())
                self._save_custom_filter_settings()
        elif chosen == save_action:
            self._save_custom_filter_settings()

    def _update_custom_filter_counts(self):
        if not self.all_events:
            for _btn, _in_edit, _out_edit, count_label in self.custom_filter_blocks:
                count_label.setText("Matches: -")
            return
        base_rows = self._collect_base_filtered_rows()
        mode = "OR"
        for idx, (btn, in_edit, out_edit, count_label) in enumerate(self.custom_filter_blocks, start=1):
            in_terms = self._parse_custom_terms(in_edit.text())
            out_terms = self._parse_custom_terms(out_edit.text())
            if not in_terms and not out_terms:
                count_label.setText("Matches: 0")
                continue
            custom_filters = [(in_terms, out_terms)]
            match_count = 0
            for _ev, row_text in base_rows:
                text = row_text.split("  |  ", 1)[1] if "  |  " in row_text else row_text
                if self._custom_filter_match(text, custom_filters, mode):
                    match_count += 1
            count_label.setText(f"Matches: {match_count}")

    def _on_filter_preset_clicked(self, index: int):
        if index < 0 or index >= len(self.filter_preset_group):
            return
        btn = self.filter_preset_group[index]
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            if btn.isChecked():
                self.active_filter_presets.add(index)
            else:
                self.active_filter_presets.discard(index)
            self.active_filter_preset_index = None
            for idx, b in enumerate(self.filter_preset_group):
                b.blockSignals(True)
                b.setChecked(idx in self.active_filter_presets)
                b.blockSignals(False)
            if not self.active_filter_presets:
                self._set_all_filters_checked()
            self.apply_filters()
            return
        if not btn.isChecked():
            self.active_filter_preset_index = None
            self.active_filter_presets.clear()
            self._set_all_filters_checked()
            self.apply_filters()
            return
        for idx, b in enumerate(self.filter_preset_group):
            b.blockSignals(True)
            b.setChecked(idx == index)
            b.blockSignals(False)
        self.active_filter_presets = {index}
        self.active_filter_preset_index = index
        self._apply_filter_preset(index)

    def _apply_filter_preset(self, index: int):
        if not self.all_events:
            return
        if not self.filters_loaded:
            self.load_filters_panel()
        if not self.filters_loaded:
            return
        presets = getattr(self.settings, "filter_presets", [])
        if index < 0 or index >= len(presets):
            return
        preset = presets[index]
        for key, cb in self.source_checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(key in preset.sources)
            cb.blockSignals(False)
        for key, cb in self.state_checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(key in preset.states)
            cb.blockSignals(False)
        for key, cb in self.message_checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(key in preset.messages)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def _on_filter_preset_menu(self, index: int):
        if index < 0 or index >= len(self.filter_preset_group):
            return
        menu = QMenu(self)
        save_action = menu.addAction("Save current selection")
        rename_action = menu.addAction("Rename")
        chosen = menu.exec(self.filter_preset_group[index].mapToGlobal(
            self.filter_preset_group[index].rect().bottomLeft()
        ))
        if chosen == rename_action:
            text, ok = QInputDialog.getText(
                self, "Rename preset", "Preset name:", text=self.filter_preset_group[index].text()
            )
            if ok and text.strip():
                self.filter_preset_group[index].setText(text.strip())
                self._save_filter_preset_settings()
        elif chosen == save_action:
            self._save_current_filter_selection(index)

    def _save_current_filter_selection(self, index: int):
        if not self.filters_loaded:
            return
        if index < 0 or index >= len(self.filter_preset_group):
            return
        sources = [k for k, cb in self.source_checkboxes.items() if cb.isChecked()]
        states = [k for k, cb in self.state_checkboxes.items() if cb.isChecked()]
        messages = [k for k, cb in self.message_checkboxes.items() if cb.isChecked() and cb.isVisible()]
        presets = getattr(self.settings, "filter_presets", [])
        while len(presets) < 15:
            presets.append(FilterPreset(name=f"Preset {len(presets) + 1}"))
        presets[index] = FilterPreset(
            name=self.filter_preset_group[index].text(),
            sources=sources,
            states=states,
            messages=messages,
        )
        self.settings.filter_presets = presets
        self.settings.save()

    def _update_tab_highlights(self):
        highlight = QColor("#ff4d4f")
        default_color = QApplication.palette().windowText().color()
        disabled_color = QColor("#888888")
        tab_bar = self.right_tabs.tabBar()
        # Filters tab highlight
        filter_idx = self.right_tabs.indexOf(self.filter_container)
        if filter_idx >= 0:
            if not self.right_tabs.isTabEnabled(filter_idx):
                self.right_tabs.setTabText(filter_idx, "Filters")
                tab_bar.setTabTextColor(filter_idx, disabled_color)
            else:
                active = False
                if self.active_filter_presets:
                    active = True
                if self.filters_loaded:
                    for cb in self.source_checkboxes.values():
                        if not cb.isChecked():
                            active = True
                            break
                    if not active:
                        for cb in self.state_checkboxes.values():
                            if not cb.isChecked():
                                active = True
                                break
                    if not active:
                        for cb in self.message_checkboxes.values():
                            if not cb.isChecked():
                                active = True
                                break
                self.right_tabs.setTabText(filter_idx, "Filters")
                tab_bar.setTabTextColor(filter_idx, highlight if active else default_color)
        # Custom tab highlight
        custom_idx = self.right_tabs.indexOf(self._custom_tab)
        if custom_idx >= 0:
            if not self.right_tabs.isTabEnabled(custom_idx):
                self.right_tabs.setTabText(custom_idx, "Custom")
                tab_bar.setTabTextColor(custom_idx, disabled_color)
            else:
                custom_active = any(btn.isChecked() for btn, _in, _out, _count in self.custom_filter_blocks)
                self.right_tabs.setTabText(custom_idx, "Custom")
                tab_bar.setTabTextColor(custom_idx, highlight if custom_active else default_color)

    def populate_log_list(self):
        print(f"[viewer] populate_log_list start (rows={len(self.log_display_rows)})", flush=True)
        self._log_model.reset_data(self.log_display_rows)
        print("[viewer] populate_log_list done", flush=True)

    def _event_seconds_to_video_seconds(self, event_seconds: float) -> float:
        return self.alignment.event_to_video(event_seconds)

    def _on_log_item_clicked(self, index: QModelIndex):
        if self.cap is None or not self.events:
            return
        row = index.row()
        if row < 0 or row >= len(self.events):
            return

        ev = self.events[row]
        t = self._event_seconds_to_video_seconds(ev.start.total_seconds())

        frame = int(round(t * self.fps)) if self.fps > 0 else 0
        if self.frame_count > 0:
            frame = max(0, min(self.frame_count - 1, frame))

        self.pause()
        self.current_frame = frame
        self.show_frame(self.current_frame)

    # ---- Playback control ----

    def toggle_play_pause(self):
        if self.cap is None:
            return
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.cap is None:
            return
        if not self.playing and self.frame_count > 0 and self.current_frame >= self.frame_count - 1:
            requester = self.next_clip_requester
            if requester is not None:
                self._play_after_open = True
                try:
                    started = bool(requester())
                except Exception:
                    started = False
                if started:
                    return
                self._play_after_open = False
        if not self.playing:
            self.playing = True
            self.play_pause_btn.setIcon(self._media_icons["pause"])
            interval_ms = int(1000 / self.fps) if self.fps > 0 else 40
            self.timer.start(interval_ms)
            self.playing_changed.emit(True)

    def pause(self):
        if self.playing:
            self.playing = False
            self.play_pause_btn.setIcon(self._media_icons["play"])
            self.timer.stop()
            self.playing_changed.emit(False)

    def _handle_scroll_wheel(self, delta_steps: int):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            step = 1 if delta_steps > 0 else -1
            for _ in range(abs(delta_steps)):
                self._jump_to_adjacent_event(step)
        elif modifiers & Qt.ShiftModifier:
            seconds = 1 if delta_steps > 0 else -1
            frames = int(round(seconds * self.fps)) if self.fps > 0 else seconds
            self.scrub_by_frames(frames)
        else:
            self.scrub_by_frames(delta_steps)


    def scrub_by_frames(self, delta_frames: int):
        if self.cap is None:
            return
        self.pause()
        new_frame = self.current_frame + delta_frames
        if self.frame_count > 0:
            new_frame = max(0, min(self.frame_count - 1, new_frame))
        else:
            new_frame = max(0, new_frame)
        self.current_frame = new_frame
        self.show_frame(self.current_frame)

    def _handle_additional_scroll_wheel(self, delta_steps: int):
        if self.additional_cap is None or self.additional_fps <= 0:
            return
        if self.additional_locked:
            modifiers = QApplication.keyboardModifiers()
            if abs(delta_steps) > 1:
                steps = max(1, int(round(abs(delta_steps) / 120)))
            else:
                steps = 1
            step = -1 if delta_steps > 0 else 1
            if modifiers & Qt.ControlModifier:
                for _ in range(steps):
                    self._jump_to_adjacent_event(step)
            elif modifiers & Qt.ShiftModifier:
                seconds = step * steps
                frames = int(round(seconds * self.fps)) if self.fps > 0 else seconds
                self.scrub_by_frames(frames)
            else:
                self.scrub_by_frames(step * steps)
            return
        if abs(delta_steps) > 1:
            steps = max(1, int(round(abs(delta_steps) / 120)))
        else:
            steps = 1
        step = -1 if delta_steps > 0 else 1
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            frames_per_step = int(round(self.additional_fps))
            if frames_per_step <= 0:
                frames_per_step = 1
            self.additional_manual_offset_frames += step * frames_per_step * steps
        else:
            for _ in range(steps):
                self.additional_manual_offset_frames += step
        t = self.current_frame / self.fps if self.fps > 0 else 0.0
        self._update_additional_frame_for_time(t)
        self._request_video_label_update()

    def _update_additional_lock_style(self):
        if self.additional_cap is None:
            self.additional_lock_toggle.setStyleSheet(theme.DIM_LABEL)
            return
        if self.additional_locked:
            self.additional_lock_toggle.setStyleSheet(theme.LOCK_ON_LABEL)
        else:
            self.additional_lock_toggle.setStyleSheet(theme.LOCK_OFF_LABEL)

    def _toggle_additional_lock(self, _event):
        if self.additional_cap is None:
            return
        if not self.additional_lock_toggle.isEnabled():
            return
        self.additional_locked = not self.additional_locked
        self._update_additional_lock_style()

    def _grab_annotated_frame_pixmap(self) -> QPixmap | None:
        if self.video_label is None or not self.video_label.isVisible():
            return None
        rect = None
        if hasattr(self.video_label, "_image_rect"):
            try:
                rect = self.video_label._image_rect()
            except Exception:
                rect = None
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            return self.video_label.grab()
        return self.video_label.grab(rect)

    def _copy_main_frame_to_clipboard(self, _pos):
        if self.video_label is not None and self.video_label.isVisible():
            try:
                pixmap = self._grab_annotated_frame_pixmap()
                if pixmap is not None and not pixmap.isNull():
                    QApplication.clipboard().setPixmap(pixmap)
                    QMessageBox.information(self, "Copied", "Annotated frame copied to clipboard.")
                    return
            except Exception:
                pass
        if self.last_qimage is None:
            return
        QApplication.clipboard().setImage(self.last_qimage)
        QMessageBox.information(self, "Copied", "Main frame copied to clipboard.")

    def _copy_additional_frame_to_clipboard(self, _pos):
        if self.additional_last_qimage is None:
            return
        QApplication.clipboard().setImage(self.additional_last_qimage)
        QMessageBox.information(self, "Copied", "Additional CCTV frame copied to clipboard.")

    def next_frame(self):
        if self.cap is None:
            return
        self.current_frame += 1
        if self.current_frame >= self.frame_count:
            self.pause()
            return
        self.show_frame(self.current_frame)

    def on_slider_moved(self, value: int):
        if self.cap is None:
            return
        self.pause()
        self.current_frame = int(value)
        self.show_frame(self.current_frame)

    def video_seconds_for_wall_time(self, wall_dt: datetime) -> float | None:
        """The video second at which the camera clock reads `wall_dt`,
        through the OCR-corrected start (Chris, 2026-09-12: a timeline
        click used to seek by the filename time and landed the OCR offset
        away from the moment the green clock then showed). None when no
        OCR start or fps is known, so callers fall back to the filename."""
        if self.video_start_dt is None or self.fps <= 0:
            return None
        start = _to_local_naive(self.video_start_dt)
        target = _to_local_naive(wall_dt)
        if start is None or target is None:
            return None
        return self.alignment.video_seconds_for_clock(start, target)

    def seek_to_wall_time(self, wall_dt: datetime, pause: bool = True) -> bool:
        """Seek to the frame where the clip shows `wall_dt`: through the
        OCR-corrected start when there is one, else the filename time.
        False when neither is known."""
        seconds = self.video_seconds_for_wall_time(wall_dt)
        if seconds is None:
            if not self.current_video_path:
                return False
            filename_dt = parse_filename_datetime(Path(self.current_video_path))
            target = _to_local_naive(wall_dt)
            if filename_dt is None or target is None:
                return False
            seconds = (target - filename_dt).total_seconds()
        self.seek_to_seconds(max(0.0, float(seconds)), pause=pause)
        return True

    def seek_to_seconds(self, seconds: float, pause: bool = True):
        if self.cap is None:
            # The clip may still be downloading; replay the seek once it opens.
            if self._pending_video_load is not None:
                self._pending_seek = (
                    self._pending_video_load[0], float(seconds), bool(pause)
                )
            return
        if self.fps <= 0:
            return
        if pause:
            self.pause()
        target = max(0, float(seconds))
        frame = int(round(target * self.fps))
        if self.frame_count > 0:
            frame = max(0, min(self.frame_count - 1, frame))
        self.current_frame = frame
        self.show_frame(self.current_frame)

    def _emit_seek_range_export_requested(self, start_frame: int, end_frame: int):
        if self.fps <= 0:
            return
        start_seconds = max(0.0, float(start_frame) / float(self.fps))
        end_seconds = max(0.0, float(end_frame) / float(self.fps))
        self.clip_range_export_requested.emit(start_seconds, end_seconds)

    def export_current_clip_with_overlays(
        self,
        source_path: Path,
        start_seconds: float,
        end_seconds: float,
        target_path: Path,
    ) -> tuple[bool, str]:
        if end_seconds <= start_seconds:
            return False, "Select a non-zero clip range first."
        if self.fps <= 0:
            return False, "No loaded clip is available for export."
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            return False, "ffmpeg was not found on PATH."
        cap = cv2.VideoCapture(str(source_path))
        if not cap.isOpened():
            return False, f"Failed to open source clip:\n{source_path}"
        fps = cap.get(cv2.CAP_PROP_FPS) or self.fps or 25.0
        start_frame = max(0, int(round(start_seconds * fps)))
        end_frame = max(start_frame + 1, int(round(end_seconds * fps)))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            cap.release()
            return False, "Unable to determine clip dimensions for export."
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        temp_dir = Path(tempfile.mkdtemp(prefix="logfather_export_"))
        temp_video = temp_dir / "video_no_audio.mp4"
        writer = cv2.VideoWriter(str(temp_video), fourcc, fps, (width, height))
        if not writer.isOpened():
            cap.release()
            return False, "Unable to create temporary export video."

        export_widget = AnnotatedVideoWidget()
        export_widget.resize(width, height)
        export_widget.set_editable(False)
        export_widget.set_fps(fps)
        export_widget.set_annotations(self._current_annotations())

        progress = QProgressDialog("Exporting clip with overlays...", "Cancel", 0, max(1, end_frame - start_frame), self)
        progress.setWindowTitle("Export Clip")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        try:
            for frame_idx in range(start_frame, end_frame):
                if progress.wasCanceled():
                    writer.release()
                    cap.release()
                    try:
                        temp_video.unlink(missing_ok=True)
                        temp_dir.rmdir()
                    except Exception:
                        pass
                    return False, "Export canceled."
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if not frame_rgb.flags["C_CONTIGUOUS"]:
                    frame_rgb = frame_rgb.copy()
                qimg = QImage(
                    frame_rgb.data,
                    width,
                    height,
                    frame_rgb.strides[0],
                    QImage.Format_RGB888,
                ).copy()
                export_widget.set_frame(qimg)
                export_widget.set_current_frame_index(frame_idx)
                t_seconds = frame_idx / fps if fps > 0 else 0.0
                overlay_lines, _ = self._overlay_context_for_time(t_seconds)
                export_widget.set_status_lines(overlay_lines)
                export_overlays = []
                if callable(self._export_target_overlay_provider):
                    try:
                        export_overlays = list(self._export_target_overlay_provider(t_seconds) or [])
                    except Exception:
                        export_overlays = []
                export_widget.set_target_overlays(export_overlays)
                rendered = QImage(width, height, QImage.Format_ARGB32)
                rendered.fill(Qt.black)
                painter = QPainter(rendered)
                export_widget.render(painter, QPoint(0, 0))
                painter.end()
                rendered = rendered.convertToFormat(QImage.Format_RGB888)
                bits = rendered.bits()
                frame_bytes = bits.tobytes()
                out_rgb = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
                out_bgr = cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
                writer.write(out_bgr)
                progress.setValue(frame_idx - start_frame + 1)
                QApplication.processEvents()
        finally:
            writer.release()
            cap.release()
            progress.close()

        temp_with_audio = temp_dir / "video_with_audio.mp4"
        mux_cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-i",
            str(temp_video),
            "-map",
            "1:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_with_audio),
        ]
        proc = subprocess.run(mux_cmd, capture_output=True, text=True)
        final_source = temp_with_audio if proc.returncode == 0 and temp_with_audio.exists() else temp_video
        try:
            if target_path.exists():
                target_path.unlink()
            shutil.move(str(final_source), str(target_path))
        except Exception as exc:
            return False, f"Export completed but saving failed:\n{exc}"
        try:
            if temp_video.exists():
                temp_video.unlink()
            if temp_with_audio.exists():
                temp_with_audio.unlink()
            temp_dir.rmdir()
        except Exception:
            pass
        if proc.returncode != 0:
            return True, "Clip exported with baked overlays, but audio could not be muxed back in."
        return True, ""

    def set_export_target_overlay_provider(self, provider) -> None:
        self._export_target_overlay_provider = provider

    # ---- Rendering ----
    # NOTE: your remaining methods (show_frame, update_video_label, resizeEvent,
    # update_time_and_overlay, update_log_highlight, offset_changed,
    # sync_logs_to_current_video_first_log) should remain exactly as they are
    # below this point in your file.

    def _current_frame_rgb(self) -> np.ndarray | None:
        """RGB view of the current frame, converted on first use per frame.
        Only the analysis views need RGB; display goes straight from BGR."""
        if self._cur_frame_rgb is None and self._cur_frame_bgr is not None:
            self._cur_frame_rgb = cv2.cvtColor(self._cur_frame_bgr, cv2.COLOR_BGR2RGB)
        return self._cur_frame_rgb

    def _previous_frame_rgb(self) -> np.ndarray | None:
        if self._prev_frame_rgb is None and self._prev_frame_bgr is not None:
            self._prev_frame_rgb = cv2.cvtColor(self._prev_frame_bgr, cv2.COLOR_BGR2RGB)
        return self._prev_frame_rgb

    def show_frame(self, frame_index):
        t_total = time.perf_counter()
        if self.cap is None:
            return

        if not self.video_label.isVisible():
            return

        if self._cur_frame_bgr is not None:
            # References, not copies: each decoded frame is a fresh buffer.
            self._prev_frame_bgr = self._cur_frame_bgr
            self._prev_frame_rgb = self._cur_frame_rgb
            self.analysis_prev_frame_index = self._last_frame_index

        if not _position_capture_sequential(
            self.cap, self._seq_cap is self.cap, self._seq_next_frame, frame_index
        ):
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        t_read = time.perf_counter()
        ret, frame = self.cap.read()
        read_dt = time.perf_counter() - t_read
        if read_dt > 0.5:
            print(f"[viewer] frame read took {read_dt:.2f}s", flush=True)
        if not ret or frame is None:
            self._seq_cap = None
            # The metadata frame_count often exceeds the frames that can
            # actually be decoded; without this, stepping past the real end
            # silently drifts current_frame beyond the last shown frame
            # (readout frozen, later back-steps skipping frames). Stay on
            # the last frame that displayed, and stop playback there.
            last_shown = self._last_frame_index
            self.pause()
            if last_shown is not None:
                self.current_frame = int(last_shown)
                # A failure near the advertised end is the file's real last
                # frame (clips are cut mid-GOP and the container rounds the
                # duration up). Adopt the discovered end so the frame
                # readouts and seek maths agree with reality.
                if 0 < self.frame_count and frame_index >= self.frame_count - 60:
                    real_count = int(last_shown) + 1
                    if real_count < self.frame_count:
                        print(
                            f"[viewer] end of stream at frame {last_shown}: "
                            f"frame_count {self.frame_count} -> {real_count}",
                            flush=True,
                        )
                        self.frame_count = real_count
                        self.seek_slider.blockSignals(True)
                        self.seek_slider.setRange(0, self.frame_count - 1)
                        self.seek_slider.blockSignals(False)
                        # Re-show the real last frame so readouts refresh.
                        self.show_frame(self.current_frame)
            return
        self._seq_cap = self.cap
        self._seq_next_frame = frame_index + 1
        if getattr(frame, "ndim", 0) != 3 or frame.shape[0] <= 0 or frame.shape[1] <= 0:
            return

        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        h, w, ch = frame.shape
        bytes_per_line = frame.strides[0]
        # BGR888 avoids the per-frame cvtColor for display entirely.
        qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format_BGR888).copy()
        self.last_qimage = qimg
        self._cur_frame_bgr = frame
        self._cur_frame_rgb = None
        self._last_frame_index = int(frame_index)
        t = frame_index / self.fps if self.fps > 0 else 0.0
        self._update_additional_frame_for_time(t)
        self._request_video_label_update()

        if self.frame_count > 0:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(frame_index)
            self.seek_slider.blockSignals(False)

        self.update_time_and_overlay(t, frame_index)
        self.update_log_highlight(t)
        dt_total = time.perf_counter() - t_total
        if dt_total > 0.5:
            print(f"[viewer] show_frame total took {dt_total:.2f}s", flush=True)

    def update_video_label(self):
        if self._updating_video_label:
            return
        self._pending_video_label_update = False
        self._updating_video_label = True
        t0 = time.perf_counter()
        try:
            if self.last_qimage is not None and self.video_label is not None:
                if not self.video_label.isVisible():
                    return
                if self.video_label.width() <= 1 or self.video_label.height() <= 1:
                    return
                self.video_label.set_fps(self.fps)
                self.video_label.set_current_frame_index(self.current_frame)
                frame_to_show = self.last_qimage
                if (
                    self.analysis_mode_combo.currentText() != "Off"
                    and self.analysis_display_combo.currentText() == "Main Overlay"
                ):
                    out_rgb, _tooltip = self._compute_analysis_output()
                    cur_rgb = self._current_frame_rgb()
                    if out_rgb is not None and cur_rgb is not None:
                        alpha = self.analysis_main_alpha_slider.value() / 100.0
                        try:
                            blended = cv2.addWeighted(cur_rgb, 1.0 - alpha, out_rgb, alpha, 0.0)
                            h, w, ch = blended.shape
                            bytes_per_line = blended.strides[0]
                            frame_to_show = QImage(
                                blended.data, w, h, bytes_per_line, QImage.Format_RGB888
                            ).copy()
                        except Exception:
                            frame_to_show = self.last_qimage
                self.video_label.set_frame(frame_to_show)
                self._refresh_birds_eye_if_open()
                if self._popout_label is not None:
                    self._popout_label.set_fps(self.fps)
                    self._popout_label.set_current_frame_index(self.current_frame)
                    self._popout_label.set_frame(self.last_qimage)
                    self._refresh_birds_eye_if_open()
            if (
                self._draw_additional_video
                and self.additional_last_qimage is not None
                and self.additional_video_label is not None
                and self.additional_video_label.isVisible()
                and self.additional_video_label.width() > 1
                and self.additional_video_label.height() > 1
            ):
                self.additional_video_label.set_frame(self.additional_last_qimage)
            self._update_analysis_view()
        finally:
            self._updating_video_label = False
            dt = time.perf_counter() - t0
            if dt > 0.5:
                print(f"[viewer] update_video_label took {dt:.2f}s", flush=True)

    def _request_video_label_update(self):
        if self._pending_video_label_update:
            return
        self._pending_video_label_update = True
        QTimer.singleShot(0, self.update_video_label)

    def _toggle_video_popout(self):
        if self._popout_window is not None and self._popout_window.isVisible():
            self._popout_window.close()
            return
        if self._popout_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Video Popout")
            win.resize(900, 600)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            toolbar = QHBoxLayout()
            tool_group = QButtonGroup(win)
            tool_group.setExclusive(True)
            for tool_key, label_text in (
                ("line", "Line"),
                ("arrow", "Arrow"),
                ("text", "Text"),
                ("measure", "Measure"),
                ("timed_line", "Timed Line"),
                ("tray", "Bird's Eye"),
            ):
                btn = QToolButton()
                btn.setText(label_text)
                btn.setCheckable(True)
                btn.setChecked(self._annotation_tool == tool_key)
                btn.clicked.connect(lambda _checked, t=tool_key: self._set_annotation_tool(t))
                tool_group.addButton(btn)
                toolbar.addWidget(btn)
            color_btn = QToolButton()
            color_btn.setText("Color")
            color_btn.clicked.connect(self._pick_annotation_color)
            self._set_color_button_style(color_btn, self._annotation_color)
            toolbar.addWidget(color_btn)
            undo_btn = QToolButton()
            undo_btn.setText("Undo")
            undo_btn.clicked.connect(self._undo_annotation)
            toolbar.addWidget(undo_btn)
            clear_btn = QToolButton()
            clear_btn.setText("Clear Clip")
            clear_btn.clicked.connect(self._clear_clip_annotations)
            toolbar.addWidget(clear_btn)
            toolbar.addStretch(1)
            layout.addLayout(toolbar)

            content_row = QHBoxLayout()
            label = AnnotatedVideoWidget("No video loaded")
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            label.set_editable(True)
            if self._placeholder_image is not None:
                label.set_placeholder_image(self._placeholder_image)
            label.annotation_created.connect(self._add_annotation)
            label.annotation_context_requested.connect(self._show_annotation_context_menu)
            label.annotation_updated.connect(self._on_annotation_updated)
            label.set_scrub_callback(self._handle_scroll_wheel)
            label.set_key_handler(self._handle_popout_key_event)
            label.set_tray_update_callback(self._refresh_birds_eye_if_open)
            label.set_tool(self._annotation_tool)
            label.set_color(self._annotation_color)
            label.set_annotations(self._current_annotations())
            label.setFocusPolicy(Qt.StrongFocus)
            content_row.addWidget(label, 1)

            self.analysis_controls_panel.setParent(win)
            self.analysis_controls_panel.setVisible(True)
            content_row.addWidget(self.analysis_controls_panel)
            layout.addLayout(content_row, 1)
            win.setLayout(layout)
            win.destroyed.connect(lambda _=None: self._clear_video_popout())
            self._popout_window = win
            self._popout_label = label
            self._popout_color_btn = color_btn
            self._popout_tool_group = tool_group
        if self.last_qimage is not None and self._popout_label is not None:
            self._popout_label.set_frame(self.last_qimage)
        self._refresh_annotation_view()
        self._popout_window.show()
        if self._popout_label is not None:
            QTimer.singleShot(0, self._popout_label.setFocus)

    def _open_annotation_popout(self):
        if self._popout_window is None or not self._popout_window.isVisible():
            self._toggle_video_popout()
        else:
            self._popout_window.raise_()
            self._popout_window.activateWindow()

    def _open_birds_eye_window(self):
        # Find latest tray annotation
        tray_ann = None
        for ann in reversed(self._current_annotations()):
            if ann.get("type") == "tray" and len(ann.get("points") or []) == 4:
                tray_ann = ann
                break
        if tray_ann is None or self.video_label is None:
            QMessageBox.information(self, "Bird's Eye", "No bird's eye region defined.")
            return
        pts = [QPointF(p[0], p[1]) for p in tray_ann.get("points", [])]
        birds_eye = self.video_label._build_birds_eye(pts)
        if birds_eye is None or birds_eye.isNull():
            QMessageBox.warning(self, "Bird's Eye", "Bird's Eye unavailable for current frame.")
            return
        self.video_label._update_birds_eye_popout(birds_eye)

    def _refresh_birds_eye_if_open(self):
        if self.video_label is None:
            return
        if self.video_label._birds_eye_window is None or not self.video_label._birds_eye_window.isVisible():
            return
        tray_ann = None
        for ann in reversed(self._current_annotations()):
            if ann.get("type") == "tray" and len(ann.get("points") or []) == 4:
                tray_ann = ann
                break
        if tray_ann is None:
            return
        pts = [QPointF(p[0], p[1]) for p in tray_ann.get("points", [])]
        birds_eye = self.video_label._build_birds_eye(pts)
        if birds_eye is not None and not birds_eye.isNull():
            self.video_label._update_birds_eye_popout(birds_eye)

    def _clear_video_popout(self):
        self._popout_window = None
        self._popout_label = None
        self._popout_color_btn = None
        self._popout_tool_group = None
        self._clear_birds_eye_popout()

    def _current_annotations(self) -> list[dict]:
        return list(self._pinned_annotations) + list(self._clip_annotations)

    def _annotations_dir(self) -> Path:
        return self.clip_cache.annotations_dir()

    def _clip_annotations_path(self) -> Path | None:
        base_path = None
        if self.current_video_original_path is not None:
            base_path = self.current_video_original_path
        elif self.current_video_path:
            base_path = Path(self.current_video_path)
        if base_path is None:
            return None
        try:
            cache_path = self._cache_path_for(Path(base_path))
        except Exception:
            cache_path = Path(base_path)
        filename = f"{cache_path.stem}.json"
        return self._annotations_dir() / filename

    def _pinned_annotations_path(self) -> Path:
        return self._annotations_dir() / "pinned.json"

    def _load_pinned_annotations(self):
        self._pinned_annotations = []
        path = self._pinned_annotations_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("annotations", [])
            if isinstance(items, list):
                self._pinned_annotations = [i for i in items if isinstance(i, dict)]
        except Exception:
            self._pinned_annotations = []

    def _save_pinned_annotations(self):
        path = self._pinned_annotations_path()
        payload = {"annotations": self._pinned_annotations}
        try:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_clip_annotations(self):
        self._clip_annotations = []
        path = self._clip_annotations_path()
        if path is None or not path.exists():
            self._annotation_history = []
            self._refresh_annotation_view()
            self._emit_clip_annotation_status()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("annotations", [])
            if isinstance(items, list):
                self._clip_annotations = [i for i in items if isinstance(i, dict)]
        except Exception:
            self._clip_annotations = []
        self._annotation_history = list(self._current_annotations())
        self._refresh_annotation_view()
        self._emit_clip_annotation_status()

    def _save_clip_annotations(self):
        path = self._clip_annotations_path()
        if path is None:
            return
        payload = {"annotations": self._clip_annotations}
        try:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._emit_clip_annotation_status()

    def _emit_clip_annotation_status(self):
        base_path = self.current_video_original_path or (Path(self.current_video_path) if self.current_video_path else None)
        if base_path is None:
            return
        has_annotations = bool(self._clip_annotations)
        self.annotation_status_changed.emit(base_path, has_annotations)

    def _save_annotations(self):
        self._save_clip_annotations()
        self._save_pinned_annotations()

    def _refresh_annotation_view(self):
        annotations = self._current_annotations()
        if self.video_label is not None:
            self.video_label.set_annotations(annotations)
            self.video_label.set_current_frame_index(self.current_frame)
            self.video_label.set_fps(self.fps)
        if self._popout_label is None:
            return
        self._popout_label.set_annotations(annotations)
        self._popout_label.set_tool(self._annotation_tool)
        self._popout_label.set_color(self._annotation_color)
        self._popout_label.set_current_frame_index(self.current_frame)
        self._popout_label.set_fps(self.fps)

    def _add_annotation(self, ann: dict):
        if ann.get("pinned"):
            self._pinned_annotations.append(ann)
        else:
            self._clip_annotations.append(ann)
        self._annotation_history.append(ann)
        self._save_annotations()
        self._refresh_annotation_view()

    def _set_annotation_tool(self, tool: str):
        self._annotation_tool = tool
        if self._popout_label is not None:
            self._popout_label.set_tool(tool)
        if self.video_label is not None:
            self.video_label.set_tool(tool)

    def _set_annotation_color(self, color: QColor):
        self._annotation_color = QColor(color)
        if self._popout_label is not None:
            self._popout_label.set_color(self._annotation_color)
        if self._popout_color_btn is not None:
            self._set_color_button_style(self._popout_color_btn, self._annotation_color)

    def _set_color_button_style(self, button: QToolButton, color: QColor):
        button.setStyleSheet(theme.solid_button(color.name()))

    def _pick_annotation_color(self):
        color = QColorDialog.getColor(self._annotation_color, self, "Select annotation color")
        if color.isValid():
            self._set_annotation_color(color)

    def _show_annotation_context_menu(self, idx: int, global_pos):
        annotations = self._current_annotations()
        if idx < 0 or idx >= len(annotations):
            return
        ann = annotations[idx]
        menu = QMenu(self)
        edit_action = menu.addAction("Edit annotation")
        pin_action = menu.addAction("Toggle pin across clips")
        frame_action = menu.addAction("Toggle pin to current frame")
        distance_action = None
        if ann.get("type") == "timed_line":
            distance_action = menu.addAction("Set distance (m)")
        delete_action = menu.addAction("Delete annotation")
        chosen = menu.exec(global_pos.toPoint())
        if chosen == edit_action:
            if ann.get("type") in ("line", "arrow", "measure", "tray"):
                if self._popout_label is not None:
                    current = self._popout_label.get_edit_index()
                    self._popout_label.set_edit_index(None if current == idx else idx)
        elif chosen == pin_action:
            if ann.get("pinned"):
                ann["pinned"] = False
                if ann in self._pinned_annotations:
                    self._pinned_annotations.remove(ann)
                if ann not in self._clip_annotations:
                    self._clip_annotations.append(ann)
            else:
                ann["pinned"] = True
                if ann in self._clip_annotations:
                    self._clip_annotations.remove(ann)
                if ann not in self._pinned_annotations:
                    self._pinned_annotations.append(ann)
            self._save_annotations()
            self._refresh_annotation_view()
        elif chosen == frame_action:
            frame_idx = self.current_frame
            if ann.get("frame_index") == frame_idx:
                ann.pop("frame_index", None)
            else:
                ann["frame_index"] = frame_idx
            self._save_annotations()
            self._refresh_annotation_view()
        elif distance_action is not None and chosen == distance_action:
            current = ann.get("distance_m")
            text, ok = QInputDialog.getText(
                self,
                "Set distance (m)",
                "Distance in meters:",
                text="" if current is None else str(current),
            )
            if ok and text.strip():
                cleaned = re.sub(r"[^0-9.+-eE]", "", text)
                try:
                    ann["distance_m"] = float(cleaned)
                except ValueError:
                    ann["distance_m"] = None
            elif ok and not text.strip():
                ann.pop("distance_m", None)
            self._save_annotations()
            self._refresh_annotation_view()
        elif chosen == delete_action:
            if ann in self._pinned_annotations:
                self._pinned_annotations.remove(ann)
            if ann in self._clip_annotations:
                self._clip_annotations.remove(ann)
            while ann in self._annotation_history:
                self._annotation_history.remove(ann)
            if self._popout_label is not None:
                self._popout_label.set_edit_index(None)
            self._save_annotations()
            self._refresh_annotation_view()

    def _on_annotation_updated(self, _idx: int, _ann: dict):
        self._save_annotations()
        self._refresh_annotation_view()

    def _handle_popout_key_event(self, event):
        self.keyPressEvent(event)

    def _undo_annotation(self):
        if not self._annotation_history:
            return
        ann = self._annotation_history.pop()
        if ann.get("pinned"):
            if ann in self._pinned_annotations:
                self._pinned_annotations.remove(ann)
        else:
            if ann in self._clip_annotations:
                self._clip_annotations.remove(ann)
        self._save_annotations()
        self._refresh_annotation_view()

    def _clear_clip_annotations(self):
        if not self._clip_annotations:
            return
        self._clip_annotations = []
        self._annotation_history = [a for a in self._annotation_history if a.get("pinned")]
        self._save_clip_annotations()
        self._refresh_annotation_view()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_video_label()
        self._update_marker_bar_padding()
        QTimer.singleShot(0, self._align_right_column_bottom)
        QTimer.singleShot(0, self._place_view_menu)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._align_right_column_bottom)

    def _align_right_column_bottom(self) -> None:
        """The boxes under the log tabs end level with the bottom of the
        Play button (Chris, 2026-09-11; the CCTV image before that): the
        column spans the whole row, so its layout gets a bottom margin
        equal to whatever sits below the play row."""
        column = self.right_column
        anchor = self.calc_label  # the bottom row: the play button is on the picture now
        if column is None or anchor is None or not anchor.isVisible():
            return
        try:
            anchor_bottom = anchor.mapTo(self, anchor.rect().bottomLeft()).y()
            column_bottom = column.mapTo(self, column.rect().bottomLeft()).y()
        except RuntimeError:
            return
        margin = max(0, column_bottom - anchor_bottom)
        lay = column.layout()
        if lay is not None and lay.contentsMargins().bottom() != margin:
            lay.setContentsMargins(0, 0, 0, margin)

    def _update_marker_bar_padding(self):
        slider = self.seek_slider
        if slider.width() <= 0:
            return
        opt = QStyleOptionSlider()
        slider.initStyleOption(opt)
        style = slider.style()
        groove = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, slider)
        handle = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, slider)
        if not groove.isValid() or not handle.isValid():
            self.event_marker_bar.set_track_padding(0, 0)
            return
        half = int(round(handle.width() / 2))
        left_pad = max(0, groove.left() + half)
        right_pad = max(0, slider.width() - 1 - (groove.right() - half))
        # The slider shares its row with the clip time labels, so the
        # full-width marker bars pad out by the slider's offset in the row.
        for bar in (self.event_marker_bar, self.timeline_marker_bar):
            if bar is None:
                continue
            dx_left = max(0, slider.geometry().left() - bar.geometry().left())
            dx_right = max(0, bar.geometry().right() - slider.geometry().right())
            bar.set_track_padding(left_pad + dx_left, right_pad + dx_right)

    def _rebuild_ppm_model(self):
        secs: list[float] = []
        for ev, msg in zip(self.all_events, self.all_message_keys):
            msg_text = str(msg or "").strip().lower()
            if TARGET_QUEUE_MESSAGE not in msg_text:
                continue
            try:
                sec = float(ev.start.total_seconds())
            except Exception:
                continue
            secs.append(sec)
        secs.sort()
        self._ppm_event_seconds = secs
        prefix = [0.0]
        for i in range(1, len(secs)):
            gap = max(0.0, secs[i] - secs[i - 1])
            prefix.append(prefix[-1] + gap)
        self._ppm_interval_prefix_sum = prefix

    def _ppm_overlay_lines(self, t_seconds: float) -> list[str]:
        if not self._ppm_event_seconds:
            return []
        # Pre-dates OCR sync: skips alignment.ocr_correction, unlike
        # video_to_event — PPM matching can lag the log highlight by the
        # OCR frame offset.
        t_log = float(t_seconds) - float(self.effective_offset())
        n = bisect_right(self._ppm_event_seconds, t_log)
        if n <= 0:
            return []

        lines: list[str] = []
        instant = None
        if n >= 2:
            dt = self._ppm_event_seconds[n - 1] - self._ppm_event_seconds[n - 2]
            if dt > 0:
                instant = 60.0 / dt
        if instant is not None:
            lines.append(f"Now: {instant:5.1f} ppm")

        win_start = t_log - PPM_ROLLING_WINDOW_SECONDS
        left = bisect_left(self._ppm_event_seconds, win_start)
        if (n - left) >= 2:
            span = self._ppm_event_seconds[n - 1] - self._ppm_event_seconds[left]
            if span > 0:
                roll = 60.0 * ((n - left) - 1) / span
                lines.append(f"Avg60s: {roll:5.1f} ppm")

        if n >= 2 and len(self._ppm_interval_prefix_sum) >= n:
            total_span = self._ppm_interval_prefix_sum[n - 1]
            if total_span > 0:
                overall = 60.0 * (n - 1) / total_span
                lines.append(f"AvgAll: {overall:5.1f} ppm")
        return lines

    def set_sku_timeline_items(self, items: list[object] | None):
        self._sku_timeline_items = list(items or [])

    @staticmethod
    def _format_sku_overlay_label(item) -> str:
        payload = item.payload if isinstance(getattr(item, "payload", None), dict) else {}
        if payload.get("_ui_manual"):
            return ""
        sku = str(payload.get("_ui_sku") or getattr(item, "label", "") or "").strip()
        tray = str(payload.get("_ui_tray") or "").strip()
        tool = str(payload.get("_ui_tool") or "").strip()
        parts = [part for part in (sku, tray, tool) if part]
        return " | ".join(parts) if parts else sku

    @staticmethod
    def _sku_overlay_lines_from_item(item) -> list[str]:
        payload = item.payload if isinstance(getattr(item, "payload", None), dict) else {}
        if payload.get("_ui_manual"):
            return []
        sku = str(payload.get("_ui_sku") or getattr(item, "label", "") or "").strip()
        tray = str(payload.get("_ui_tray") or "").strip()
        tool = str(payload.get("_ui_tool") or "").strip()
        lines: list[str] = []
        if sku:
            lines.append(f"SKU: {sku[:39]}..." if len(sku) > 44 else f"SKU: {sku}")
        if tray:
            lines.append(f"Tray: {tray[:38]}..." if len(tray) > 43 else f"Tray: {tray}")
        if tool:
            lines.append(f"Tool: {tool[:38]}..." if len(tool) > 43 else f"Tool: {tool}")
        return lines

    def _current_sku_overlay_lines(self, playback_dt: datetime | None) -> list[str]:
        if playback_dt is None or not self._sku_timeline_items:
            return []
        if playback_dt.tzinfo is None:
            playback_dt = playback_dt.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
        else:
            playback_dt = playback_dt.astimezone(timezone.utc)
        last_known_item = None
        for item in self._sku_timeline_items:
            start = getattr(item, "start", None)
            end = getattr(item, "end", None)
            if start is None or end is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            else:
                start = start.astimezone(timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            else:
                end = end.astimezone(timezone.utc)
            label = self._format_sku_overlay_label(item)
            if label:
                last_known_item = item
            if start <= playback_dt <= end:
                return self._sku_overlay_lines_from_item(item)
            if playback_dt < start:
                break
        if last_known_item is not None:
            return self._sku_overlay_lines_from_item(last_known_item)
        return []

    @property
    def alignment(self) -> TimeAlignment:
        """The playback-time maths for the primary video, snapshotting the
        viewer's mutable offset attrs. All conversions go through this —
        never re-derive the formulas at a call site."""
        return TimeAlignment(
            fps=self.fps,
            sync_offset=self.sync_offset,
            time_offset=self.time_offset,
            ocr_frame_offset=self.ocr_frame_offset,
        )

    @property
    def additional_alignment(self) -> TimeAlignment:
        """Alignment for the secondary camera (wall-clock slaved to the
        primary; sync/drift offsets do not apply to it)."""
        return TimeAlignment(
            fps=self.additional_fps,
            ocr_frame_offset=self.additional_ocr_frame_offset,
        )

    def effective_offset(self) -> float:
        return self.alignment.effective_offset

    def _overlay_context_for_time(self, t_seconds: float) -> tuple[list[str], datetime | None]:
        playback_dt = None
        if self.video_start_dt is not None and self.fps > 0:
            # Note: the burned-in clock time, deliberately without the
            # user drift — SKU matching follows the camera clock.
            playback_dt = self.alignment.clock_datetime(self.video_start_dt, t_seconds)
        elif self.current_video_filename_dt is not None:
            playback_dt = self.current_video_filename_dt + timedelta(seconds=t_seconds)
        ppm_lines = self._ppm_overlay_lines(t_seconds)
        sku_lines = self._current_sku_overlay_lines(playback_dt)
        if sku_lines:
            ppm_lines = list(ppm_lines) + sku_lines
        return ppm_lines, playback_dt

    def _refresh_clip_span_labels(self) -> None:
        """Clip start and end to the minute above the seek slider; the
        filename time until an OCR offset refines the start."""
        start_lbl = self.clip_start_label
        end_lbl = self.clip_end_label
        if start_lbl is None or end_lbl is None:
            return
        start = self.video_start_dt or self.current_video_filename_dt
        if start is None or self.cap is None:
            texts = ("", "")
        else:
            try:
                seconds = (self.frame_count or 0) / (self.fps or 25.0)
                end = start + timedelta(seconds=seconds)
                texts = (start.strftime("%H:%M"), end.strftime("%H:%M"))
            except Exception:
                texts = ("", "")
        if start_lbl.text() != texts[0]:
            start_lbl.setText(texts[0])
        if end_lbl.text() != texts[1]:
            end_lbl.setText(texts[1])


    def update_time_and_overlay(self, t_seconds: float, frame_index: int):
        self._refresh_clip_span_labels()
        td = timedelta(seconds=t_seconds)
        time_str = format_timecode(td).replace(",", ".")
        self.info_label.display(time_str)
        playback_dt = None
        if self.video_start_dt is not None and self.fps > 0:
            calc_dt = self.alignment.clock_datetime(self.video_start_dt, t_seconds)
            calc_str = calc_dt.strftime("%H:%M:%S.%f")[:-3]
            self.calc_label.display(calc_str)
            playback_dt = self.alignment.playback_datetime(self.video_start_dt, t_seconds)
        else:
            self.calc_label.display("00:00:00.000")
            if self.current_video_filename_dt is not None:
                playback_dt = self.alignment.playback_datetime_from_filename(
                    self.current_video_filename_dt, t_seconds
                )
        ppm_lines, playback_dt_from_helper = self._overlay_context_for_time(t_seconds)
        if playback_dt is None:
            playback_dt = playback_dt_from_helper
        self._last_status_lines = list(ppm_lines)
        self._apply_status_lines()
        self.frame_label.display(str(frame_index))
        self.current_time_changed.emit(playback_dt)

    def _apply_status_lines(self) -> None:
        lines = self._last_status_lines if self.info_text_btn.isChecked() else []
        self.video_label.set_status_lines(lines)
        if self._popout_label is not None:
            self._popout_label.set_status_lines(lines)

    def _on_info_text_toggled(self, on: bool) -> None:
        update_ui_state({"viewer_status_text": bool(on)})
        self._apply_status_lines()

    def update_log_highlight(self, t_seconds: float):
        if not self.events or self._log_model.rowCount() == 0:
            return

        t_secs = self.alignment.video_to_event(t_seconds)

        # Frame interval: [t_secs, t_secs + one_frame).  All log events whose
        # start falls inside this window are "between this frame and the next."
        one_frame = (1.0 / self.fps) if self.fps > 0 else 0.0
        frame_end = t_secs + one_frame

        left  = bisect_left(self._event_start_times, t_secs)
        right = bisect_left(self._event_start_times, frame_end)

        # Red: every event that starts within the current frame interval.
        active: set[int] = set(range(left, right))

        # Amber: the next event just beyond the frame interval (upper bound).
        nearest: int | None = right if right < len(self.events) else None

        if active:
            scroll_to = min(active)
        elif nearest is not None:
            scroll_to = nearest
        else:
            scroll_to = max(0, left - 1)

        self._log_model.set_highlights(active, nearest)
        if scroll_to != getattr(self, "_last_highlight_row", None):
            self._last_highlight_row = scroll_to
            self.log_list.scrollTo(
                self._log_model.index(scroll_to),
                QListView.EnsureVisible,
            )

    def set_offset_value(self, value: float):
        clamped = max(self.drift_min, min(self.drift_max, float(value)))
        if abs(clamped - self.time_offset) < 1e-6:
            return
        self.time_offset = clamped
        self._update_offset_display()
        self._apply_offset()

    def _update_offset_display(self):
        self.drift_display.setText(f"{self.time_offset:+.2f}s")
        slider_value = int(round(self.time_offset * self._drift_slider_scale))
        self.drift_slider.blockSignals(True)
        self.drift_slider.setValue(slider_value)
        self.drift_slider.blockSignals(False)

    def _on_drift_slider_changed(self, value: int):
        self.set_offset_value(float(value) / float(self._drift_slider_scale))

    def set_close_gap_threshold_value(self, value: float):
        clamped = max(self.close_gap_threshold_min, min(self.close_gap_threshold_max, float(value)))
        if abs(clamped - self.gap_threshold) < 1e-6:
            return
        self.gap_threshold = clamped
        self._update_close_gap_threshold_display()
        self.close_gap_threshold_changed.emit(self.gap_threshold)

    def _update_close_gap_threshold_display(self):
        self.gap_display.setText(f"{self.gap_threshold:.2f}x")
        slider_value = int(round(self.gap_threshold * 100.0))
        self.gap_slider.blockSignals(True)
        self.gap_slider.setValue(slider_value)
        self.gap_slider.blockSignals(False)

    def _on_close_gap_slider_changed(self, value: int):
        self.set_close_gap_threshold_value(float(value) / 100.0)

    def _apply_offset(self):
        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_time_and_overlay(t, self.current_frame)
            self.update_log_highlight(t)
        self._update_timeline_markers()

    def sync_logs_to_current_video_first_log(self):
        if not self.events:
            QMessageBox.warning(
                self,
                "No logs",
                "Load a CSV log and make sure at least one source/message filter is enabled."
            )
            return
        if self.cap is None:
            QMessageBox.warning(self, "No video", "Open a video file first.")
            return

        first_event = self.events[0]
        first_start_secs = first_event.start.total_seconds()
        t_current = self.current_frame / self.fps if self.fps > 0 else 0.0

        self.sync_offset = t_current - first_start_secs
        self.time_offset = 0.0
        self.set_offset_value(0.0)

        self.update_time_and_overlay(t_current, self.current_frame)
        self.update_log_highlight(t_current)
        self.log_markers_enabled = True
        self._update_timeline_markers()

        QMessageBox.information(
            self,
            "Logs synced",
            "Logs are now aligned so that the FIRST visible log entry matches the CURRENT video frame."
        )

    # ---- Cache helpers (thin forwarders onto ClipCache) ----

    def _cache_path_for(self, original_path: Path) -> Path:
        return self.clip_cache.cache_path_for(original_path)

    def _touch_cache_entry(self, cache_path: Path) -> None:
        self.clip_cache.touch_entry(cache_path)

    def _is_cached_copy_current(self, source_path: Path, cache_path: Path) -> bool:
        return self.clip_cache.is_cached_copy_current(source_path, cache_path)

    def _ensure_cached_copy(self, source_path: Path, cache_path: Path) -> bool:
        return self.clip_cache.ensure_cached_copy(source_path, cache_path)

    def get_valid_cached_path(self, original_path: Path) -> Path | None:
        return self.clip_cache.get_valid_cached_path(original_path)

    def prefetch_clips_to_cache(self, paths: list[Path]):
        self.clip_cache.prefetch(paths)

    def cancel_queued_prefetches(self) -> None:
        protected_key = None
        if self._pending_video_load is not None:
            # Never cancel the copy a click-triggered load is waiting on.
            protected_key = str(self._pending_video_load[2])
        self.clip_cache.cancel_queued_prefetches(protected_key=protected_key)

    def prune_cache_if_needed(self) -> None:
        self.clip_cache.prune()
        self.update_cache_status()

    def _on_cache_transfer_finished(self, source_path: str, ok: bool):
        if ok:
            self.update_cache_status()
        self._finish_pending_video_load(source_path, ok)

    @Slot()
    def update_cache_status(self):
        if self._cache_status_future is not None and not self._cache_status_future.done():
            self._cache_status_pending = True
            return
        self._cache_status_pending = False
        self._cache_status_future = self.clip_cache.executor.submit(self.clip_cache.calculate_stats)
        self._cache_status_future.add_done_callback(self._on_cache_status_ready)

    def _on_cache_status_ready(self, future: Future):
        try:
            count, total = future.result()
        except Exception as exc:
            QMetaObject.invokeMethod(
                self,
                "_finish_cache_status_error",
                Qt.QueuedConnection,
                Q_ARG(str, str(exc)),
            )
            return
        mb = total / (1024 * 1024) if total else 0.0
        QMetaObject.invokeMethod(
            self,
            "_finish_cache_status_update",
            Qt.QueuedConnection,
            Q_ARG(int, int(count)),
            Q_ARG(float, float(mb)),
        )

    @Slot(int, float)
    def _finish_cache_status_update(self, count: int, mb: float):
        self._cache_status_future = None
        self.cache_status_label.setText(f"Cache: {count} file(s), {mb:.1f} MB")
        if self._cache_status_pending:
            self._cache_status_pending = False
            self.update_cache_status()

    @Slot(str)
    def _finish_cache_status_error(self, message: str):
        self._cache_status_future = None
        self.cache_status_label.setText(f"Cache status unavailable ({message})")
        if self._cache_status_pending:
            self._cache_status_pending = False
            self.update_cache_status()

    def clear_cache(self):
        if not self.cache_root.exists():
            self.update_cache_status()
            return
        resp = QMessageBox.question(
            self,
            "Clear cache",
            "This will delete all locally cached videos. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        try:
            for entry in self.cache_root.iterdir():
                if entry.is_file():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry, ignore_errors=True)
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to clear cache: {exc}")
        finally:
            self.update_cache_status()

    def clear_elastic_event_cache(self):
        elastic_cache_root = self.cache_root / "elastic_events"
        if not elastic_cache_root.exists():
            QMessageBox.information(self, "Event cache", "No cached Elastic events found.")
            return
        resp = QMessageBox.question(
            self,
            "Clear event cache",
            "This will delete cached Elastic event results only. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        try:
            for entry in elastic_cache_root.iterdir():
                if entry.is_file():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry, ignore_errors=True)
            QMessageBox.information(self, "Event cache", "Cached Elastic events removed.")
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to clear Elastic event cache: {exc}")
        finally:
            self.update_cache_status()

    def delete_current_cache_copy(self):
        if not self.current_video_path:
            QMessageBox.information(self, "No video", "Load a cached video first.")
            return
        path = Path(self.current_video_path)
        if not self._is_path_in_cache(path):
            QMessageBox.information(
                self, "Not cached", "The current video is not stored in the cache."
            )
            return
        resp = QMessageBox.question(
            self,
            "Delete cached copy",
            "The currently loaded cached copy will be deleted and the video closed. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        if self.cap is not None:
            self.pause()
            self.cap.release()
            self.cap = None
            self.video_label.set_placeholder_text("No video loaded")
        try:
            path.unlink(missing_ok=True)
            QMessageBox.information(self, "Deleted", "Cached copy removed.")
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to delete cached copy: {exc}")
        self.current_video_path = None
        self.update_cache_status()

    def _is_path_in_cache(self, path: Path) -> bool:
        try:
            return Path(path).resolve().is_relative_to(self.cache_root.resolve())
        except Exception:
            return False

    def open_cache_folder(self):
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(self.cache_root))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.cache_root)])
            else:
                subprocess.Popen(["xdg-open", str(self.cache_root)])
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to open cache folder: {exc}")

    def _extract_pikpak_id(self, path: Path) -> str | None:
        for part in reversed(path.parts):
            match = re.search(r"(PikPak\d+)", part, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _find_pikpak_root(self, path: Path) -> Path | None:
        for idx, part in enumerate(path.parts):
            if re.match(r"^PikPak\d+$", part, flags=re.IGNORECASE):
                return Path(*path.parts[: idx + 1])
        return None

    def load_additional_cctv_from_path(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, "File not found", str(path))
            return
        self._reset_additional_video()
        self.additional_video_original_path = path
        self.additional_video_filename_dt = parse_filename_datetime(path)
        if self.additional_video_filename_dt is None:
            try:
                self.additional_video_filename_dt = datetime.fromtimestamp(path.stat().st_mtime)
            except Exception:
                self.additional_video_filename_dt = None
        cached_path = None
        try:
            cached_path = self.get_valid_cached_path(path)
        except Exception:
            cached_path = None
        if cached_path is None:
            self._pending_additional_original_path = path
            self._pending_additional_last_size = None
            self._pending_additional_stable_count = 0
            self.additional_video_label.setText("Caching Additional CCTV...")
            self.additional_video_label.setVisible(True)
            self._ensure_cached_copy_async(path)
            self._start_pending_additional_timer()
            return
        self._open_additional_from_path(cached_path, allow_rewrap=True)

    def _reset_additional_video(self):
        if self.additional_cap is not None:
            self.additional_cap.release()
            self.additional_cap = None
        self.additional_fps = 25.0
        self.additional_frame_count = 0
        self.additional_current_frame = 0
        self.additional_last_qimage = None
        self.additional_video_path = None
        self.additional_video_original_path = None
        self._pending_additional_original_path = None
        self._pending_additional_poll = False
        if self._pending_additional_timer.isActive():
            self._pending_additional_timer.stop()
        self._pending_additional_last_size = None
        self._pending_additional_stable_count = 0
        self.additional_video_filename_dt = None
        self.additional_video_start_dt = None
        self.additional_ocr_offset_seconds = None
        self.additional_ocr_frame_offset = 0
        self.additional_manual_offset_frames = 0
        self._auto_additional_ocr_attempted_key = None
        self._additional_sync_done = False
        self._update_sync_button_style()
        self.additional_video_label.setText("Additional CCTV not loaded")
        self.additional_video_label.setVisible(False)
        self._draw_additional_video = False
        self.additional_locked = True
        self.additional_lock_toggle.setEnabled(False)
        self._update_additional_lock_style()
        self.additional_sync_btn.setEnabled(False)

    def _ensure_cached_copy_async(self, path: Path):
        try:
            cache_path = self._cache_path_for(path)
        except Exception:
            return
        if self._is_cached_copy_current(path, cache_path):
            return
        def _copy():
            if not self._ensure_cached_copy(path, cache_path):
                return
            QMetaObject.invokeMethod(self, "_on_additional_cache_copy_complete", Qt.QueuedConnection)
            QMetaObject.invokeMethod(self, "update_cache_status", Qt.QueuedConnection)
        self._cache_executor.submit(_copy)

    @Slot()
    def _on_additional_cache_copy_complete(self):
        if self._pending_additional_original_path is None:
            return
        self._open_additional_cached(self._pending_additional_original_path)

    def _open_additional_cached(self, original_path: Path):
        if self._pending_additional_original_path != original_path:
            return
        try:
            cache_path = self._cache_path_for(original_path)
        except Exception:
            return
        if not self._is_cached_copy_current(original_path, cache_path):
            return
        if not self._open_additional_from_path(cache_path, allow_rewrap=False):
            self._start_pending_additional_timer()

    def _open_additional_from_path(self, path: Path, allow_rewrap: bool) -> bool:
        self.additional_video_path = str(path)
        self.additional_cap = cv2.VideoCapture(str(path))
        if not self.additional_cap.isOpened():
            if allow_rewrap:
                fixed_path = self.try_rewrap_video_with_ffmpeg(str(path))
                if fixed_path:
                    self.additional_cap.release()
                    self.additional_cap = cv2.VideoCapture(fixed_path)
                    self.additional_video_path = fixed_path
            if not self.additional_cap.isOpened():
                self.additional_cap = None
                return False
        self.additional_fps = self.additional_cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.additional_frame_count = int(self.additional_cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.additional_current_frame = 0
        self.additional_video_label.setVisible(True)
        self._draw_additional_video = True
        self._update_additional_frame_for_time(0.0)
        self._pending_additional_original_path = None
        self._pending_additional_poll = False
        if self._pending_additional_timer.isActive():
            self._pending_additional_timer.stop()
        self.additional_locked = True
        key_path = self.additional_video_original_path or Path(self.additional_video_path)
        key = self._offset_cache_key(key_path, tag="additional")
        cached = self.additional_offset_store.get(key)
        if isinstance(cached, dict) and cached.get("source") == "additional":
            try:
                self.additional_ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.additional_ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.additional_ocr_offset_seconds = None
                self.additional_ocr_frame_offset = 0
            if self.additional_ocr_offset_seconds is not None and not plausible_ocr_offset(self.additional_ocr_offset_seconds):
                print(f"[ocr] cached additional-camera offset {self.additional_ocr_offset_seconds:.0f}s for {key} is not plausible; dropped", flush=True)
                self.additional_offset_store.remove(key)
                self.additional_ocr_offset_seconds = None
                self.additional_ocr_frame_offset = 0
            if self.additional_ocr_offset_seconds is not None:
                filename_dt = self.additional_video_filename_dt
                if filename_dt is None:
                    filename_dt = parse_filename_datetime(key_path)
                if filename_dt is None and self.additional_video_original_path is not None:
                    try:
                        filename_dt = datetime.fromtimestamp(self.additional_video_original_path.stat().st_mtime)
                    except Exception:
                        filename_dt = None
                if filename_dt:
                    self.additional_video_start_dt = filename_dt + timedelta(seconds=self.additional_ocr_offset_seconds)
                    self._additional_sync_done = True
                    self._update_sync_button_style()
                    self._refresh_additional_after_sync()
        if self.additional_ocr_offset_seconds is None:
            settings = Settings.load()
            if settings.auto_ocr_open_on_missing or settings.auto_ocr_sync:
                self._auto_sync_additional_with_ocr()
        self.additional_lock_toggle.setEnabled(True)
        self._update_additional_lock_style()
        self.additional_sync_btn.setEnabled(True)
        self._update_sync_button_style()
        self.update_video_label()
        return True

    def _start_pending_additional_timer(self):
        if self._pending_additional_timer.isActive():
            return
        self._pending_additional_poll = True
        self._pending_additional_timer.start()

    def _poll_pending_additional_cache(self):
        if self._pending_additional_original_path is None:
            self._pending_additional_poll = False
            if self._pending_additional_timer.isActive():
                self._pending_additional_timer.stop()
            return
        try:
            cache_path = self._cache_path_for(self._pending_additional_original_path)
        except Exception:
            self._pending_additional_poll = False
            if self._pending_additional_timer.isActive():
                self._pending_additional_timer.stop()
            return
        if cache_path.exists():
            try:
                size = cache_path.stat().st_size
            except Exception:
                size = None
            if size is not None and size == self._pending_additional_last_size:
                self._pending_additional_stable_count += 1
            else:
                self._pending_additional_stable_count = 0
                self._pending_additional_last_size = size
            if self._pending_additional_stable_count >= 1:
                if self._open_additional_from_path(cache_path, allow_rewrap=False):
                    return

    def _get_video_duration_seconds(self, path: Path) -> float | None:
        target_path = path
        if not self._is_path_in_cache(path):
            try:
                cached = self._cache_path_for(path)
            except Exception:
                cached = None
            if cached is None or not cached.exists():
                return None
            target_path = cached
        cap = cv2.VideoCapture(str(target_path))
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        cap.release()
        if fps <= 0 or frame_count <= 0:
            return None
        return float(frame_count) / float(fps)

    def _update_additional_frame_for_time(self, t_seconds: float):
        t0 = time.perf_counter()
        if self.additional_cap is None:
            if self._pending_additional_original_path is not None:
                try:
                    cache_path = self._cache_path_for(self._pending_additional_original_path)
                except Exception:
                    return
                if cache_path.exists():
                    try:
                        size = cache_path.stat().st_size
                    except Exception:
                        size = None
                    if size is not None and size == self._pending_additional_last_size:
                        self._pending_additional_stable_count += 1
                    else:
                        self._pending_additional_stable_count = 0
                        self._pending_additional_last_size = size
                    if self._pending_additional_stable_count >= 1:
                        self._open_additional_from_path(cache_path, allow_rewrap=False)
            if self.additional_cap is None:
                dt = time.perf_counter() - t0
                if dt > 0.5:
                    print(f"[viewer] secondary update took {dt:.2f}s (no secondary)", flush=True)
                return
        if self.additional_fps <= 0:
            dt = time.perf_counter() - t0
            if dt > 0.5:
                print(f"[viewer] secondary update took {dt:.2f}s (no fps)", flush=True)
            return
        if self.video_start_dt is not None and self.additional_video_start_dt is not None:
            abs_time = self.alignment.clock_datetime(self.video_start_dt, t_seconds)
            t2 = self.additional_alignment.video_seconds_for_clock(
                self.additional_video_start_dt, abs_time
            )
        else:
            t2 = t_seconds
        frame_index = int(round(t2 * self.additional_fps)) + int(self.additional_manual_offset_frames)
        if self.additional_frame_count > 0:
            frame_index = max(0, min(self.additional_frame_count - 1, frame_index))
        else:
            frame_index = max(0, frame_index)
        if frame_index == self.additional_current_frame and self.additional_last_qimage is not None:
            return
        self.additional_current_frame = frame_index
        if not _position_capture_sequential(
            self.additional_cap,
            self._seq_additional_cap is self.additional_cap,
            self._seq_additional_next_frame,
            frame_index,
        ):
            self.additional_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        t_read = time.perf_counter()
        ret, frame = self.additional_cap.read()
        read_dt = time.perf_counter() - t_read
        if read_dt > 0.5:
            print(f"[viewer] secondary frame read took {read_dt:.2f}s", flush=True)
        if ret:
            self._seq_additional_cap = self.additional_cap
            self._seq_additional_next_frame = frame_index + 1
        else:
            self._seq_additional_cap = None
        if not ret:
            dt = time.perf_counter() - t0
            if dt > 0.5:
                print(f"[viewer] secondary update took {dt:.2f}s (read fail)", flush=True)
            return
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        h, w, ch = frame.shape
        bytes_per_line = frame.strides[0]
        self.additional_last_qimage = QImage(
            frame.data,
            w,
            h,
            bytes_per_line,
            QImage.Format_BGR888,
        ).copy()
        dt = time.perf_counter() - t0
        if dt > 0.5:
            print(f"[viewer] secondary update took {dt:.2f}s", flush=True)

    def _offset_cache_key(self, path: Path, *, tag: str | None = None) -> str:
        pikpak_id = self._extract_pikpak_id(path) or "unknown"
        ts = parse_filename_datetime(path)
        ts_key = ts.strftime("%Y%m%d%H%M%S") if ts else path.stem
        suffix = f":{tag}" if tag else ""
        return f"{pikpak_id}:{ts_key}{suffix}"

    def _plan_ocr_video_source(self, path: Path) -> tuple[Path, Path | None, bool]:
        """Decide on the UI thread where OCR should read the clip from.

        Returns (source, copy_to, wait_for_download): copy_to is the cache
        destination when the clip still needs copying off the share, else
        None. wait_for_download means the main clip download already owns
        that copy — the OCR worker must wait for it to land rather than
        start a second copy or read the share (a cv2 open on the share
        freezes for 30s+ stream timeouts). The copy/wait happens on a
        worker via _ocr_video_source().
        """
        if self._is_path_in_cache(path):
            return path, None, False
        try:
            cache_path = self._cache_path_for(path)
        except Exception:
            return path, None, False
        if self._pending_video_load is not None and path == self._pending_video_load[1]:
            return path, cache_path, True
        return path, cache_path, False

    def _ocr_video_source(
        self,
        src: Path,
        copy_to: Path | None,
        should_abort=None,
        on_progress=None,
        wait_for_download: bool = False,
    ) -> Path:
        """Worker-side: materialize the OCR source decided by
        _plan_ocr_video_source, falling back to the share on copy failure.
        The copy is chunked so shutdown can abort it mid-file (an
        uninterruptible SMB copy held the close for seconds).
        `on_progress(done_bytes, total_bytes)` is called per chunk."""
        if copy_to is None:
            return src
        if wait_for_download:
            # The main clip download owns this copy: poll until it is no
            # longer pending for this clip (finished, failed, or the user
            # moved on), then use the copy if it landed intact.
            while True:
                if should_abort is not None and should_abort():
                    raise InterruptedError("OCR wait for download aborted")
                pending = self._pending_video_load
                if pending is None or pending[1] != src:
                    break
                time.sleep(0.5)
            return copy_to if self._is_cached_copy_current(src, copy_to) else src
        tmp_path = copy_to.with_suffix(copy_to.suffix + ".part")
        try:
            if not copy_to.exists():
                t_copy = time.perf_counter()
                try:
                    total_bytes = int(src.stat().st_size)
                except Exception:
                    total_bytes = 0
                done_bytes = 0
                if on_progress is not None:
                    on_progress(0, total_bytes)
                with open(src, "rb") as fin, open(tmp_path, "wb") as fout:
                    while True:
                        if should_abort is not None and should_abort():
                            raise InterruptedError("OCR copy aborted")
                        chunk = fin.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)
                        done_bytes += len(chunk)
                        if on_progress is not None:
                            on_progress(done_bytes, total_bytes)
                shutil.copystat(src, tmp_path)
                tmp_path.replace(copy_to)
                copy_secs = time.perf_counter() - t_copy
                mb = done_bytes / (1024 * 1024)
                rate = mb / copy_secs if copy_secs > 0 else 0.0
                print(
                    f"[ocr] clip copy: {mb:.0f} MB in {copy_secs:.1f}s ({rate:.1f} MB/s)",
                    flush=True,
                )
            return copy_to
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return src

    def open_sync_cctv_time(self, auto_start: bool = True, auto_close_on_success: bool = False):
        if not self.current_video_path:
            QMessageBox.information(self, "No video", "Load a video first.")
            return
        pikpak_id = self._extract_pikpak_id(Path(self.current_video_path))
        if not pikpak_id:
            QMessageBox.information(self, "No PikPak ID", "Unable to detect PikPak ID.")
            return
        key = self._offset_cache_key(Path(self.current_video_path))
        dlg = None

        def _on_offset_approved(video_start_dt, offset_seconds, frame_offset):
            if not plausible_ocr_offset(offset_seconds):
                QMessageBox.warning(self, "OCR offset", f"An offset of {float(offset_seconds) / 60:+.0f} minutes is not plausible (the clock and the filename differ by seconds). Not applied.")
                return
            try:
                self.ocr_offset_seconds = float(offset_seconds)
                self.ocr_frame_offset = int(frame_offset)
                self.video_start_dt = video_start_dt
                self.offset_store.set(key, offset_seconds, frame_offset)
                self._apply_auto_sync_if_possible()
                self._main_sync_done = True
                self._update_sync_button_style()
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "OCR sync apply failed",
                    f"OCR found an offset, but applying it failed:\n{exc}",
                )
            finally:
                if auto_close_on_success and dlg is not None and self._ocr_tool_dialog is dlg:
                    QTimer.singleShot(0, dlg.close)

        if self._ocr_tool_dialog is not None:
            try:
                self._ocr_tool_dialog.close()
            except Exception:
                pass
        dlg = SyncCctvTimeWindow(
            settings_path=self.ocr_settings_path,
            settings_key=pikpak_id,
            auto_analyze=auto_start,
            on_offset_approved=_on_offset_approved,
        )
        dlg.video_label.setText("Preparing video...")
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda _=None: setattr(self, "_ocr_tool_dialog", None))
        dlg.resize(900, 600)
        # The dialog stays hidden until the clip copy lands (Chris,
        # 2026-09-04: the OCR window must not pop up mid-download); the
        # activity bar shows the download meanwhile.
        self._ocr_tool_dialog = dlg
        src, copy_to, wait_dl = self._plan_ocr_video_source(Path(self.current_video_path))

        def _open_when_ready(ready_path):
            if self._ocr_tool_dialog is not dlg:
                return
            dlg.show()
            dlg.open_video(str(ready_path))

        self._ocr_sync_slot.start(
            lambda job, src=src, copy_to=copy_to, wait_dl=wait_dl: self._ocr_video_source(
                src,
                copy_to,
                should_abort=job.interrupted,
                on_progress=lambda done, total: job.emit_progress(("ocr-copy", done, total)),
                wait_for_download=wait_dl,
            ),
            on_result=_open_when_ready,
            on_progress=self._on_ocr_sync_progress,
            on_finished=self._hide_ocr_sync_progress,
        )

    def open_additional_sync_cctv_time(self, auto_start: bool = True, auto_close_on_success: bool = False):
        if not self.additional_video_path:
            QMessageBox.information(self, "No video", "Load an additional CCTV clip first.")
            return
        key_path = self.additional_video_original_path or Path(self.additional_video_path)
        pikpak_id = self._extract_pikpak_id(key_path)
        if not pikpak_id:
            QMessageBox.information(self, "No PikPak ID", "Unable to detect PikPak ID.")
            return
        key = self._offset_cache_key(key_path, tag="additional")

        dlg = None

        def _on_offset_approved(video_start_dt, offset_seconds, frame_offset):
            if not plausible_ocr_offset(offset_seconds):
                QMessageBox.warning(self, "OCR offset", f"An offset of {float(offset_seconds) / 60:+.0f} minutes is not plausible for the additional camera (the clock and the filename differ by seconds). Not applied.")
                return
            try:
                self.additional_ocr_offset_seconds = float(offset_seconds)
                self.additional_ocr_frame_offset = int(frame_offset)
                self.additional_video_start_dt = video_start_dt
                self.additional_offset_store.set(
                    key, offset_seconds, frame_offset, source="additional"
                )
                self._refresh_additional_after_sync()
                self._additional_sync_done = True
                self._update_sync_button_style()
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Additional CCTV OCR apply failed",
                    f"OCR found an offset, but applying it failed:\n{exc}",
                )
            finally:
                if auto_close_on_success and dlg is not None and self._ocr_tool_dialog is dlg:
                    QTimer.singleShot(0, dlg.close)

        if self._ocr_tool_dialog is not None:
            try:
                self._ocr_tool_dialog.close()
            except Exception:
                pass
        dlg = SyncCctvTimeWindow(
            settings_path=self.ocr_settings_path,
            settings_key=additional_camera_roi_key(pikpak_id),
            auto_analyze=auto_start,
            on_offset_approved=_on_offset_approved,
        )
        dlg.video_label.setText("Preparing video...")
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda _=None: setattr(self, "_ocr_tool_dialog", None))
        dlg.resize(900, 600)
        # Hidden until the clip copy lands — see open_sync_cctv_time.
        self._ocr_tool_dialog = dlg
        src, copy_to, wait_dl = self._plan_ocr_video_source(Path(self.additional_video_path))

        def _open_when_ready(ready_path):
            if self._ocr_tool_dialog is not dlg:
                return
            dlg.show()
            dlg.open_video(str(ready_path))

        self._ocr_additional_sync_slot.start(
            lambda job, src=src, copy_to=copy_to, wait_dl=wait_dl: self._ocr_video_source(
                src,
                copy_to,
                should_abort=job.interrupted,
                on_progress=lambda done, total: job.emit_progress(("ocr-copy", done, total)),
                wait_for_download=wait_dl,
            ),
            on_result=_open_when_ready,
            on_progress=lambda payload: self._on_ocr_sync_progress(payload, cam_label=" (2nd cam)"),
            on_finished=lambda: self._hide_ocr_sync_progress(cam_label=" (2nd cam)"),
        )

    def _on_ocr_sync_progress(self, payload, cam_label: str = ""):
        """UI-thread handler for OCR sync worker progress: forwards copy
        byte counts and analysis stage names to the activity bar."""
        try:
            kind = payload[0]
        except Exception:
            return
        key = f"ocr{cam_label}"
        prefix = f"OCR sync{cam_label}"
        if kind == "ocr-copy":
            _, done, total = payload
            self.activity_progress.emit(
                key, f"{prefix}: downloading clip", int(done or 0), int(total or 0)
            )
        elif kind == "ocr-stage":
            self.activity_progress.emit(key, f"{prefix}: {payload[1]}", None, None)

    def _hide_ocr_sync_progress(self, cam_label: str = ""):
        self.activity_cleared.emit(f"ocr{cam_label}")

    def _auto_sync_with_ocr(self, force: bool = False):
        if not self.current_video_path:
            return
        path = Path(self.current_video_path)
        pikpak_id = self._extract_pikpak_id(path)
        key = self._offset_cache_key(path)
        settings = Settings.load()
        cached = None if force else self.offset_store.get(key)
        if cached:
            try:
                self.ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.ocr_offset_seconds = None
                self.ocr_frame_offset = 0
            if self.ocr_offset_seconds is not None and not plausible_ocr_offset(self.ocr_offset_seconds):
                print(f"[ocr] cached offset {self.ocr_offset_seconds:.0f}s is not plausible; dropped", flush=True)
                try:
                    self.offset_store.remove(self._offset_cache_key(Path(self.current_video_path)))
                except Exception:
                    pass
                self.ocr_offset_seconds = None
                self.ocr_frame_offset = 0
            if self.ocr_offset_seconds is not None:
                filename_dt = parse_filename_datetime(self.current_video_path)
                if filename_dt:
                    self.video_start_dt = filename_dt + timedelta(seconds=self.ocr_offset_seconds)
                    self._apply_auto_sync_if_possible()
                return
        if settings.auto_ocr_open_on_missing:
            if not pikpak_id:
                return
            if self._ocr_tool_dialog is not None:
                return
            if self._auto_ocr_attempted_key == key:
                return
            self._auto_ocr_attempted_key = key
            self.open_sync_cctv_time(auto_start=True, auto_close_on_success=True)
            return
        if not settings.auto_ocr_sync and not force:
            return
        if not pikpak_id:
            return
        # SMB copy + Tesseract run off the UI thread; the offset is applied
        # when the job lands, if this clip is still the one showing.
        src, copy_to, wait_dl = self._plan_ocr_video_source(path)
        settings_path = self.ocr_settings_path

        def _analyze(job, src=src, copy_to=copy_to, wait_dl=wait_dl, pikpak_id=pikpak_id):
            video_path = self._ocr_video_source(
                src,
                copy_to,
                should_abort=job.interrupted,
                on_progress=lambda done, total: job.emit_progress(("ocr-copy", done, total)),
                wait_for_download=wait_dl,
            )
            return analyze_video_offset(
                str(video_path),
                settings_path=settings_path,
                settings_key=pikpak_id,
                parent=None,
                should_abort=job.interrupted,
                on_stage=lambda label: job.emit_progress(("ocr-stage", label)),
            )

        def _apply(result, src=src, key=key):
            if not self.current_video_path or Path(self.current_video_path) != src:
                return  # user moved on to another clip
            if result is None:
                QMessageBox.information(
                    self,
                    "OCR failed",
                    "OCR sync failed. Please adjust the ROI and try again.",
                )
                self.open_sync_cctv_time(auto_start=False)
                return
            if not plausible_ocr_offset(result.offset_seconds):
                print(f"[ocr] automatic offset {result.offset_seconds:.0f}s is not plausible; ignored, using the filename time", flush=True)
                return
            self.ocr_offset_seconds = result.offset_seconds
            self.ocr_frame_offset = result.frame_offset
            self.video_start_dt = result.video_start_dt
            self.offset_store.set(key, result.offset_seconds, result.frame_offset)
            self._apply_auto_sync_if_possible()
            # The button goes green after an automatic sync too (Chris, 2026-09-12).
            self._main_sync_done = True
            self._update_sync_button_style()

        self._ocr_sync_slot.start(
            _analyze,
            on_result=_apply,
            on_error=lambda msg: print(f"[ocr] auto-sync failed: {msg}"),
            on_progress=self._on_ocr_sync_progress,
            on_finished=self._hide_ocr_sync_progress,
        )

    def _auto_sync_additional_with_ocr(self, force: bool = False):
        if not self.additional_video_path:
            return
        cache_path = Path(self.additional_video_path)
        key_path = self.additional_video_original_path or cache_path
        pikpak_id = self._extract_pikpak_id(key_path)
        key = self._offset_cache_key(key_path, tag="additional")
        settings = Settings.load()
        cached = None if force else self.additional_offset_store.get(key)
        if isinstance(cached, dict) and cached.get("source") != "additional":
            cached = None
        if cached:
            try:
                self.additional_ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.additional_ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.additional_ocr_offset_seconds = None
                self.additional_ocr_frame_offset = 0
            if self.additional_ocr_offset_seconds is not None and not plausible_ocr_offset(self.additional_ocr_offset_seconds):
                print(f"[ocr] cached additional-camera offset {self.additional_ocr_offset_seconds:.0f}s is not plausible; dropped", flush=True)
                self.additional_offset_store.remove(key)
                self.additional_ocr_offset_seconds = None
                self.additional_ocr_frame_offset = 0
            if self.additional_ocr_offset_seconds is not None:
                filename_dt = parse_filename_datetime(key_path)
                if filename_dt is None:
                    filename_dt = self.additional_video_filename_dt
                if filename_dt is None and self.additional_video_original_path is not None:
                    filename_dt = parse_filename_datetime(self.additional_video_original_path)
                if filename_dt is None and self.additional_video_original_path is not None:
                    try:
                        filename_dt = datetime.fromtimestamp(self.additional_video_original_path.stat().st_mtime)
                    except Exception:
                        filename_dt = None
                if filename_dt:
                    self.additional_video_start_dt = filename_dt + timedelta(seconds=self.additional_ocr_offset_seconds)
                    self._refresh_additional_after_sync()
                return
        if settings.auto_ocr_open_on_missing:
            if not pikpak_id:
                return
            if self._ocr_tool_dialog is not None:
                return
            if self._auto_additional_ocr_attempted_key == key:
                return
            self._auto_additional_ocr_attempted_key = key
            self.open_additional_sync_cctv_time(auto_start=True, auto_close_on_success=True)
            return
        if not settings.auto_ocr_sync and not force:
            return
        if not pikpak_id:
            return
        src, copy_to, wait_dl = self._plan_ocr_video_source(cache_path)
        settings_path = self.ocr_settings_path

        def _analyze(job, src=src, copy_to=copy_to, wait_dl=wait_dl, pikpak_id=pikpak_id):
            video_path = self._ocr_video_source(
                src,
                copy_to,
                should_abort=job.interrupted,
                on_progress=lambda done, total: job.emit_progress(("ocr-copy", done, total)),
                wait_for_download=wait_dl,
            )
            return analyze_video_offset(
                str(video_path),
                settings_path=settings_path,
                settings_key=additional_camera_roi_key(pikpak_id),
                parent=None,
                should_abort=job.interrupted,
                on_stage=lambda label: job.emit_progress(("ocr-stage", label)),
            )

        def _apply(result, src=src, key=key):
            if not self.additional_video_path or Path(self.additional_video_path) != src:
                return  # secondary clip changed while OCR ran
            if result is None:
                QMessageBox.information(
                    self,
                    "Additional CCTV OCR failed",
                    "OCR sync failed for the additional CCTV clip.",
                )
                return
            if not plausible_ocr_offset(result.offset_seconds):
                print(f"[ocr] automatic additional-camera offset {result.offset_seconds:.0f}s is not plausible; ignored", flush=True)
                return
            self.additional_ocr_offset_seconds = result.offset_seconds
            self.additional_ocr_frame_offset = result.frame_offset
            self.additional_video_start_dt = result.video_start_dt
            self.additional_offset_store.set(
                key, result.offset_seconds, result.frame_offset, source="additional"
            )
            self._refresh_additional_after_sync()
            self._additional_sync_done = True
            self._update_sync_button_style()

        self._ocr_additional_sync_slot.start(
            _analyze,
            on_result=_apply,
            on_error=lambda msg: print(f"[ocr] secondary auto-sync failed: {msg}"),
            on_progress=lambda payload: self._on_ocr_sync_progress(payload, cam_label=" (2nd cam)"),
            on_finished=lambda: self._hide_ocr_sync_progress(cam_label=" (2nd cam)"),
        )

    def _refresh_additional_after_sync(self):
        if self.additional_cap is None or self.additional_fps <= 0:
            return
        t = self.current_frame / self.fps if self.fps > 0 else 0.0
        self._update_additional_frame_for_time(t)
        self.update_video_label()

    def _apply_auto_sync_if_possible(self):
        if self.video_start_dt is None or self.first_log_dt is None:
            if self.external_markers and self.ocr_offset_seconds is not None:
                self._refresh_timeline_marker_bar()
            return
        local_video_start = _to_local_naive(self.video_start_dt)
        if local_video_start is None:
            return
        self.video_start_dt = local_video_start
        sync_offset = (self.first_log_dt - local_video_start).total_seconds()
        self.sync_offset = sync_offset
        self.time_offset = 0.0
        self.set_offset_value(0.0)
        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_time_and_overlay(t, self.current_frame)
            self.update_log_highlight(t)
        self.log_markers_enabled = True
        self._update_timeline_markers()
        self._refresh_timeline_marker_bar()

    def _update_timeline_markers(self):
        if not self.events or self.cap is None:
            self._set_log_markers([])
            return
        # Pre-dates OCR sync: skips alignment.ocr_correction, unlike
        # event_to_video — markers can sit an OCR frame offset away from
        # where clicking the log row seeks.
        offset = self.effective_offset()
        markers: list[tuple[float, str]] = []
        for ev in self.events:
            try:
                markers.append((ev.start.total_seconds() + offset, "#ffcc00"))
            except Exception:
                continue
        self._set_log_markers(markers)

    # ---- Elastic log loading ----

    def load_logs_from_elastic(self, pikpak_path: str, start_iso: str, end_iso: str, show_busy: bool = True):
        request_key = (str(pikpak_path), str(start_iso), str(end_iso))
        if self._loaded_log_request_key == request_key and self.all_events:
            return
        if self._active_log_request_key == request_key and self._log_future is not None:
            return
        try:
            start_dt = self._parse_iso(start_iso)
            end_dt = self._parse_iso(end_iso)
        except Exception:
            QMessageBox.warning(self, "Invalid time range", "Could not parse provided timestamps.")
            return
        self._cancel_log_future()
        self._active_log_request_key = request_key
        print("[viewer] load_logs_from_elastic starting", flush=True)
        self._log_future_id += 1
        fetch_id = self._log_future_id
        settings = Settings.load()
        if self._log_executor is None:
            self._log_executor = ThreadPoolExecutor(max_workers=1)
        self._log_future = self._log_executor.submit(
            fetch_logs_for_range,
            settings,
            Path(pikpak_path),
            start_dt,
            end_dt,
        )
        if show_busy:
            self._set_log_busy(True, "Fetching Elastic logs...")
        print(f"[viewer] scheduled log fetch id {fetch_id}", flush=True)
        self._poll_log_future(fetch_id)

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        val = value.strip()
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        return datetime.fromisoformat(val)

    def _set_log_busy(self, busy: bool, message: str | None = None):
        if busy:
            if self._log_busy_dialog is None:
                dlg = QProgressDialog(message or "Working...", None, 0, 0, self)
                dlg.setWindowTitle("Log Viewer")
                dlg.setCancelButton(None)
                dlg.setWindowModality(Qt.NonModal)
                dlg.setMinimumDuration(0)
                dlg.setRange(0, 0)
                self._log_busy_dialog = dlg
            self._log_busy_dialog.setLabelText(message or "Working...")
            self._log_busy_dialog.show()
            QApplication.processEvents()
        else:
            if self._log_busy_dialog is not None:
                self._log_busy_dialog.close()
                self._log_busy_dialog = None

    def _on_elastic_logs_ready(self, rows: list):
        print(f"[viewer] _on_elastic_logs_ready (rows={len(rows)})", flush=True)
        self._set_log_busy(False)
        if not rows:
            QMessageBox.information(self, "No events", "No Elastic events found for this clip timeframe.")
            self._clear_events()
            return
        self._apply_loaded_events(*build_events_from_rows(rows))
        # Avoid modal dialog here; it can re-enter UI updates during heavy redraw.
        print("[viewer] events loaded", flush=True)
        if self._filters_wanted and self.all_events and not self.filters_loaded:
            # Filters were loaded before this reload: rebuild them with the
            # remembered ticks and re-apply, no extra click needed.
            self._filters_wanted = False
            self.load_filters_panel()

    def _on_elastic_logs_failed(self, message: str):
        print(f"[viewer] _on_elastic_logs_failed: {message}", flush=True)
        self._set_log_busy(False)
        if message:
            QMessageBox.warning(self, "Elastic fetch failed", message)

    def _poll_log_future(self, fetch_id: int):
        future = self._log_future
        if future is None or fetch_id != self._log_future_id:
            return
        if future.done():
            self._log_future = None
            try:
                rows = future.result()
            except ElasticFetchError as exc:
                # Record the key BEFORE clearing it: assigning after the
                # clear stored None, so the same partial range was refetched
                # on every retrigger.
                request_key = self._active_log_request_key
                self._active_log_request_key = None
                print(f"[viewer] log future {fetch_id} partial failure: {exc}", flush=True)
                if exc.items:
                    print(
                        f"[viewer] delivering {len(exc.items)} partial rows despite failure",
                        flush=True,
                    )
                    self._loaded_log_request_key = request_key
                    self.logs_ready.emit(exc.items)
                self.logs_failed.emit(str(exc))
                return
            except Exception as exc:
                self._active_log_request_key = None
                print(f"[viewer] log future {fetch_id} failed: {exc}", flush=True)
                self.logs_failed.emit(str(exc))
                return
            else:
                self._loaded_log_request_key = self._active_log_request_key
                self._active_log_request_key = None
                print(f"[viewer] log future {fetch_id} completed with {len(rows)} rows", flush=True)
                print("[viewer] invoking _on_elastic_logs_ready", flush=True)
                self.logs_ready.emit(rows)
                print("[viewer] returned from _on_elastic_logs_ready", flush=True)
        else:
            QTimer.singleShot(100, lambda fid=fetch_id: self._poll_log_future(fid))

    def _cancel_log_future(self):
        future = self._log_future
        self._log_future = None
        self._active_log_request_key = None
        if future is None:
            return
        print("[viewer] cancelling prior log future", flush=True)
        future.cancel()

    def shutdown_workers(self):
        """Flush settings and stop all executors. Called by
        MainWindow.closeEvent — a child widget's closeEvent never fires when
        the app window closes. Sub-steps over 100ms print [shutdown]
        timings, same as the MainWindow-level steps."""

        def _close_tool_windows():
            # A lingering dialog (e.g. the OCR ROI tool) keeps the Qt event
            # loop alive after the main window closes, leaving a zombie
            # process with its console window open.
            for attr in ("_ocr_tool_dialog", "_popout_window", "_analysis_window"):
                window = getattr(self, attr, None)
                if window is not None:
                    window.close()

        def _stop_log_executor():
            if self._log_executor is not None:
                self._log_executor.shutdown(wait=False, cancel_futures=True)
                self._log_executor = None

        for label, step in (
            # Clip cache first: aborting an in-flight SMB copy frees the
            # link before anything below touches the share or settings.
            ("viewer: clip cache", self.clip_cache.shutdown),
            ("viewer: flush settings", self._flush_settings_autosave),
            ("viewer: cancel log fetch", self._cancel_log_future),
            ("viewer: close tool windows", _close_tool_windows),
            ("viewer: OCR sync slot", self._ocr_sync_slot.shutdown),
            ("viewer: secondary OCR slot", self._ocr_additional_sync_slot.shutdown),
            ("viewer: log executor", _stop_log_executor),
        ):
            t0 = time.perf_counter()
            try:
                step()
            except Exception as exc:
                print(f"[shutdown] '{label}' failed: {exc}", flush=True)
            dt_ms = (time.perf_counter() - t0) * 1000
            if dt_ms > 100:
                print(f"[shutdown] '{label}' took {dt_ms:.0f}ms", flush=True)

    def closeEvent(self, event):
        self.shutdown_workers()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.scrub_by_frames(-1)
            event.accept()
            return
        if event.key() == Qt.Key_Right:
            self.scrub_by_frames(1)
            event.accept()
            return
        if event.key() == Qt.Key_Up:
            self._jump_to_adjacent_event(-1)
            event.accept()
            return
        if event.key() == Qt.Key_Down:
            self._jump_to_adjacent_event(1)
            event.accept()
            return
        super().keyPressEvent(event)

    def _jump_to_adjacent_event(self, direction: int):
        if not self.events or self.cap is None or self._log_model.rowCount() == 0:
            return
        current_row = self.log_list.currentIndex().row()
        if current_row == -1:
            # No selection yet; pick the closest event to current time.
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            current_td = timedelta(seconds=self.alignment.video_to_event(t))
            try:
                closest = min(
                    range(len(self.events)),
                    key=lambda idx: abs((self.events[idx].start - current_td).total_seconds()),
                )
            except ValueError:
                return
            current_row = closest
        target = max(0, min(len(self.events) - 1, current_row + direction))
        target_index = self._log_model.index(target)
        self.log_list.setCurrentIndex(target_index)
        self._on_log_item_clicked(target_index)


def main():
    parser = argparse.ArgumentParser(description="Video + Log Viewer")
    parser.add_argument("--video", help="Video file to open on startup")
    parser.add_argument("--pikpak", help="Path to PikPak folder for Elastic lookups")
    parser.add_argument("--start", help="Clip start time (ISO) for Elastic query")
    parser.add_argument("--end", help="Clip end time (ISO) for Elastic query")
    args, qt_args = parser.parse_known_args()

    app = QApplication([sys.argv[0]] + qt_args)
    win = ReplayView()
    win.resize(1400, 700)
    win.show()
    if args.video:
        win.load_video_from_path(args.video)
    if args.pikpak and args.start and args.end:
        win.load_logs_from_elastic(args.pikpak, args.start, args.end)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
