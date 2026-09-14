import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QEvent, QVariantAnimation, QEasingCurve, QPoint, QSize
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QSplitter,
    QToolButton,
    QSizePolicy,
    QPushButton,
    QLabel,
    QStackedWidget,
    QFileDialog,
    QProgressBar,
)

from logfather.ui import theme
from logfather.ui.Date_Picker_frontend import DatePicker
from logfather.core.app_version import is_newer, latest_available_version, load_version_info
from logfather.core.retention import FOOTAGE_DELETED_NOTICE, footage_expired
from logfather.paths import REPO_ROOT
from logfather.ui.day_popup import DayPopup
from logfather.ui.system_filter import SystemPickerPopup, funnel_icon
from logfather.ui.icons import refresh_icon, calendar_icon, conveyor_icon, punnet_icon, first_product_icon
from logfather.ui.window_placement import show_over_parent
from logfather.ui.gear_menu import build_gear_button
from logfather.ui.day_selection import DaySelection
from logfather.ui.telemetry_strip import TelemetryPanel
from logfather.data import grafana_client
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.data.telemetry_loader import fetch_telemetry_day
from logfather.core.telemetry import summary_track
from logfather.ui.pulse import Pulser
from logfather.ui.replay_timeline import (
    ReplayTimeline,
    TimelineItem,
    parse_time_from_name,
    ensure_utc,
    ensure_playhead_local,
    MIN_BLOCK_DURATION,
    inferred_live_clip_end,
    VIDEO_COLOR_CACHED,
    VIDEO_COLOR_UNCACHED,
    _is_path_cached,
    _build_cache_index,
    _path_key,
)
from logfather.core.app_version import load_version_info
from logfather.data.day_listing_cache import load_day_files_cached
from logfather.data.elastic_loader import fetch_events, set_system_id_override
from logfather.ui.qt_worker import JobSlot
from logfather.ui.progress import job_progress
from logfather.ui.stop_report import (
    StopReportDialog,
    StopReportEntry,
    build_stop_report_entries,
    collect_stop_report_data,
)
from logfather.ui.target_overlay_controller import TargetOverlayController
from logfather.data.settings_store import Settings, display_customer_name, display_line_name, system_group_sort_key
from logfather.ui.replay_view import ReplayView
from logfather.ui.data_inventory_dialog import DataInventoryDialog
from logfather.ui.software_window import SoftwareWindow
from logfather.ui.errors_stops_window import ErrorsStopsWindow
from logfather.ui.overview_widget import OverviewWidget
from logfather.ui.fleetwide_elastic_search_widget import FleetwideElasticSearchWidget
from logfather.ui.target_buffer_widget import TargetBufferWidget


DEBUG_CLIP_TIMING = True
ENABLE_CACHE_COLOR_UPDATE = True
ENABLE_EVENT_MARKERS = True
ENABLE_PREFETCH_ADJACENT = True
# Day-wide prefetch is off: HiDrive copies share the internet connection with
# Elastic Cloud, and saturating it made every timeline/log fetch crawl.
ENABLE_DAY_PREFETCH = False
ENABLE_LOG_BUTTON = True
TIMELINE_MIN_HEIGHT = 165
# More room for the timeline and its reading strips (Chris, 2026-09-08).
TIMELINE_MAX_HEIGHT = 520
TIMELINE_EXPAND_DELAY_MS = 1500


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        # Version in the title so it's always obvious which build is being
        # tested (Chris, 2026-09-03). Dev runs derive it live from git
        # (0.<commit count>); frozen builds read the stamped version.json.
        version = str(load_version_info().get("version") or "dev")
        self.setWindowTitle(f"The Logfather - version {version}")
        self._post_show_started = False
        self._shutdown_in_progress = False

        self.system_id_override: str | None = None
        self.settings = Settings.load()
        self._last_layout_snapshot = self._layout_settings_snapshot(self.settings)
        if self.settings.load_warning:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Settings recovered", self.settings.load_warning
                ),
            )
        # Construction is split into ordered sections (Stage 3); later
        # sections consume attributes from earlier ones.
        self._build_panels()
        self._build_splitters()
        self._build_top_bar_and_layout()
        self._wire_signals()

    def _build_panels(self):
        """The five content panels, their workers, and the overlay
        controller: viewer, overview, date/time pickers, fleetwide,
        target-buffer."""
        self.viewer = ReplayView()
        cache_root = self.viewer.cache_root
        self.overview_widget = OverviewWidget(
            self.settings,
            cache_root=cache_root,
            prefetch_clips=self._prefetch_overview_clips,
            parent=self,
        )
        self.overview_widget.open_requested.connect(self._open_system_from_overview)
        self._pending_overview_navigation: dict | None = None
        # Failsafe: drop a navigation that never completes (e.g. the target
        # day turns out to have no clips) so it can't fire much later.
        self._overview_nav_failsafe = QTimer(self)
        self._overview_nav_failsafe.setSingleShot(True)
        self._overview_nav_failsafe.setInterval(120_000)
        self._overview_nav_failsafe.timeout.connect(self._cancel_overview_navigation)
        self.viewer.settings_saved.connect(self._reload_settings_from_viewer)
        # One date selection for the Overview, Errors / Stops and Data
        # windows (Chris, 2026-09-07).
        self.day_selection = DaySelection(self)
        self.overview_widget.set_day_selection(self.day_selection)
        # Telemetry tab (Chris, 2026-09-07): the day's temperatures and
        # motor currents from Grafana, playhead drawn across them.
        self.telemetry_panel = TelemetryPanel()
        self.viewer.right_tabs.addTab(self.telemetry_panel, "Telemetry")
        self.viewer.current_time_changed.connect(self.telemetry_panel.set_playhead)
        self._telemetry_slot = JobSlot(self)
        # Allow timeline expansion in non-maximised windows by reducing
        # the viewer's minimum height constraint.
        self.viewer.setMinimumSize(980, 120)
        self.date_picker = DatePicker()
        self.date_picker.set_system_layout_settings(self.settings)
        # Build static tracks: video + additional + condition rows
        static_tracks = self._build_static_tracks()

        # Extra loaders: Elastic events + additional CCTV clips. The third
        # argument is the day's last-video-end from the timeline scan, so
        # the SKU fetch doesn't re-list the share.
        extra_loaders = [
            lambda root, day, last_video_end: fetch_events(
                self.settings, root, day, last_video_end=last_video_end
            ),
            lambda root, day, last_video_end: self._load_additional_cctv_items(root, day, cache_root),
        ]

        self.replay_timeline = ReplayTimeline(
            load_day_files_cached,
            extra_loaders=extra_loaders,
            static_tracks=static_tracks,
            cache_root=cache_root,
        )
        # The timeline's reading strips need the settings for Grafana / Elastic;
        # their boxes sit under the log tabs (Chris, 2026-09-08).
        self.replay_timeline.settings = self.settings
        self.viewer.add_right_panel_widget(self.replay_timeline.signal_boxes_widget())
        self.viewer.additional_cctv_resolver = self._additional_clip_covering
        self.viewer.next_clip_requester = self._open_next_clip
        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.viewer)
        self.content_stack.addWidget(self.overview_widget)
        self.fleetwide_search_widget = FleetwideElasticSearchWidget(self.settings, parent=self)
        self.fleetwide_search_widget.settings_saved.connect(self._sync_settings_from_fleetwide_search)
        self.content_stack.addWidget(self.fleetwide_search_widget)
        self.stop_report_btn = QPushButton("Stop Report")
        self.stop_report_btn.clicked.connect(self.open_stop_report)
        # Stop report and Fit live in the gear menu (Chris, 2026-09-11);
        # the buttons stay hidden as the enabled-state holders.
        self.stop_report_action = QAction("Stop report\u2026", self)
        self.stop_report_action.triggered.connect(self.open_stop_report)
        self.fit_timeline_action = QAction("Fit timeline to the day", self)
        self.fit_timeline_action.triggered.connect(self.replay_timeline._fit_to_items)
        self._stop_report_slot = JobSlot(self)
        self._stop_report_progress = None
        # One exclusive Overview/Viewer/Fleetwide mode switcher (Chris,
        # 2026-09-04: only the controls for the current mode on screen;
        # Overview first and the startup default).
        self.overview_btn = QToolButton()
        self.overview_btn.setText("Overview")
        self.overview_btn.setCheckable(True)
        self.overview_btn.setChecked(True)
        self.overview_btn.setStyleSheet(theme.SEGMENT_LEFT)
        self.replay_btn = QToolButton()
        self.replay_btn.setText("PikPak Replay")
        self.replay_btn.setCheckable(True)
        self.replay_btn.setStyleSheet(theme.SEGMENT_MID)
        self.search_btn = QToolButton()
        self.search_btn.setText("Search")
        self.search_btn.setCheckable(True)
        # Icons on the mode buttons (Chris, 2026-09-11): rows for the
        # Overview, a play triangle for PikPak Replay, a magnifier for Search.
        from logfather.ui.icons import overview_icon, play_icon, search_icon

        for btn, icon in ((self.overview_btn, overview_icon()), (self.replay_btn, play_icon()), (self.search_btn, search_icon())):
            btn.setIcon(icon)
            btn.setIconSize(QSize(18, 18))
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.search_btn.setStyleSheet(theme.SEGMENT_RIGHT)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for btn in (self.overview_btn, self.replay_btn, self.search_btn):
            self._mode_group.addButton(btn)
        self._mode_group.buttonToggled.connect(self._on_mode_button_toggled)
        self.current_system_label = QLabel("")
        self.current_system_label.setStyleSheet(theme.TOP_BAR_LABEL)
        self.current_system_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Viewer top bar (Chris, 2026-09-05): Choose system (a menu of
        # every system grouped by customer) and Choose date (a one-day
        # calendar popup). Once chosen, the buttons read the selection,
        # so the Customer / Line / System label is retired.
        self.choose_system_btn = QToolButton()
        self.choose_system_btn.setText("Choose system")
        self.choose_system_btn.setIcon(funnel_icon())
        self.choose_system_btn.setIconSize(QSize(18, 18))
        self.choose_system_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.choose_system_btn.setToolTip("Pick the system to view")
        self.choose_system_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.choose_system_btn.clicked.connect(self._show_system_menu)
        self.choose_date_btn = QToolButton()
        self.choose_date_btn.setText("Choose date")
        self.choose_date_btn.setIcon(calendar_icon())
        self.choose_date_btn.setIconSize(QSize(18, 18))
        self.choose_date_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.choose_date_btn.setToolTip("Pick the day to look at")
        self.choose_date_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.choose_date_btn.clicked.connect(self._show_day_popup)
        # A day may be chosen before a system (Chris, 2026-09-07); it is
        # held here and applied when the system is chosen.
        self._pending_day: date | None = None
        self._day_popup = DayPopup(self)
        self._day_popup.day_chosen.connect(self._on_popup_day_chosen)
        # In System Replay the next choice pulses (Chris, 2026-09-07): the
        # system button until a system is chosen, then the date button
        # until a day is chosen.
        self._chooser_pulser = Pulser(self)
        self._chosen_root: Path | None = None
        self._chosen_day: date | None = None
        self.stop_report_btn.hide()
        self.replay_timeline.fit_btn.hide()
        self.replay_timeline.setMinimumHeight(TIMELINE_MIN_HEIGHT)
        self._timeline_min_height = TIMELINE_MIN_HEIGHT
        self._timeline_max_height = TIMELINE_MAX_HEIGHT
        self._timeline_expanded = False
        self.date_picker.setMaximumWidth(380)

        # Pick-target buffer panel
        self.targets_panel = TargetBufferWidget()
        self.targets_panel.setMinimumWidth(220)
        self.targets_panel.setMaximumWidth(400)
        self._targets_panel_visible = False
        self._buffer_panel_target_width = 280
        self._day_prefetch_timer: QTimer | None = None
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setInterval(60_000)
        self._session_save_timer.timeout.connect(self._save_last_session)
        self._session_save_timer.start()
        # Buffer events, gap classification, calibration and per-frame
        # overlays live in the controller.
        self._overlay_controller = TargetOverlayController(
            viewer=self.viewer,
            buffer_widget=self.targets_panel,
            replay_timeline=self.replay_timeline,
            settings_provider=lambda: self.settings,
            calibration_system_id_provider=self._current_calibration_system_id,
            parent_widget=self,
        )

    def _build_splitters(self):
        """Panel toggles, the horizontal/vertical splitters, and their
        show/hide animations."""
        self.date_picker_toggle = QToolButton()
        self.date_picker_toggle.setText("Hide Date Picker")
        self.date_picker_toggle.setCheckable(True)
        # The left-hand calendar / system panel is retired (Chris,
        # 2026-09-07): Choose system and Choose date in the top bar do its
        # job. The DatePicker object stays as the logic behind those
        # buttons but is never shown, never revealed by hovering the edge.
        self.date_picker_toggle.setChecked(False)
        self.date_picker_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.date_picker_toggle.setVisible(False)
        self._hover_reveal_enabled = False
        self._left_panel_retired = True
        self._left_reveal_px = 12

        self.targets_toggle = QToolButton()
        self.targets_toggle.setText("Targets")
        self.targets_toggle.setCheckable(True)
        self.targets_toggle.setChecked(False)
        self.targets_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.targets_toggle.toggled.connect(self._set_targets_panel_visible)

        horizontal_splitter = QSplitter(Qt.Horizontal)
        horizontal_splitter.addWidget(self.date_picker)
        self.date_picker.setVisible(False)
        horizontal_splitter.addWidget(self.content_stack)
        horizontal_splitter.addWidget(self.targets_panel)
        horizontal_splitter.setStretchFactor(0, 2)
        horizontal_splitter.setStretchFactor(1, 8)
        horizontal_splitter.setStretchFactor(2, 0)
        self._horizontal_splitter = horizontal_splitter
        self._left_panel_target_width = 380
        self._left_panel_visible = False
        self._left_panel_anim = QVariantAnimation(self)
        self._left_panel_anim.setDuration(170)
        self._left_panel_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._left_panel_anim.valueChanged.connect(self._on_left_panel_anim_step)
        self._left_panel_anim.finished.connect(self._on_left_panel_anim_finished)
        self._targets_panel_anim = QVariantAnimation(self)
        self._targets_panel_anim.setDuration(170)
        self._targets_panel_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._targets_panel_anim.valueChanged.connect(self._on_targets_panel_anim_step)
        self._targets_panel_anim.finished.connect(self._on_targets_panel_anim_finished)
        # Start with buffer panel hidden
        self.targets_panel.setVisible(False)

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.addWidget(horizontal_splitter)
        main_splitter.addWidget(self.replay_timeline)
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 2)
        self._main_splitter = main_splitter
        self._timeline_anim = QVariantAnimation(self)
        self._timeline_anim.setDuration(170)
        self._timeline_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timeline_anim.valueChanged.connect(self._on_timeline_anim_step)
        self._timeline_expand_timer = QTimer(self)
        self._timeline_expand_timer.setSingleShot(True)
        self._timeline_expand_timer.setInterval(TIMELINE_EXPAND_DELAY_MS)
        self._timeline_expand_timer.timeout.connect(lambda: self._set_timeline_expanded(True))

    def _build_top_bar_and_layout(self):
        """Top control strip (view toggles left, tool buttons right) and the
        root layout; installs the hover-reveal event filters."""
        top_controls = QHBoxLayout()
        top_controls.addWidget(self.date_picker_toggle, 0, Qt.AlignLeft)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        mode_row.addWidget(self.overview_btn)
        mode_row.addWidget(self.replay_btn)
        mode_row.addWidget(self.search_btn)
        top_controls.addLayout(mode_row)
        top_controls.addSpacing(12)
        top_controls.addWidget(self.choose_system_btn, 0, Qt.AlignLeft)
        top_controls.addWidget(self.choose_date_btn, 0, Qt.AlignLeft)
        top_controls.addWidget(self.current_system_label, 0, Qt.AlignLeft)
        self.current_system_label.setVisible(False)
        self.conveyor_btn = QToolButton()
        self.conveyor_btn.setText("Conveyor")  # was "Calibrate" (Chris, 2026-09-12)
        self.conveyor_btn.setIcon(conveyor_icon())
        self.conveyor_btn.setIconSize(QSize(18, 18))
        self.conveyor_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.conveyor_btn.setToolTip("Conveyor calibration: the tracking line the product overlays follow")
        self.conveyor_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.conveyor_btn.clicked.connect(self._overlay_controller.open_calibration_dialog)

        self.track_toggle = QToolButton()
        self.track_toggle.setText("Track")
        self.track_toggle.setIcon(punnet_icon())
        self.track_toggle.setIconSize(QSize(18, 18))
        self.track_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.track_toggle.setToolTip("Draw the tracked products on the picture")
        self.track_toggle.setCheckable(True)
        self.track_toggle.setChecked(True)
        self.track_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.track_toggle.toggled.connect(self._on_track_toggled)
        # Track cannot be selected until the conveyor has been calibrated
        # for the system (Chris, 2026-09-13): ticking it without one asks
        # whether to calibrate now. It follows the calibration as systems
        # change and when the Conveyor dialog saves.
        self._track_wanted = True
        self._overlay_controller.calibration_ready.connect(self._apply_track_availability)
        self._apply_track_availability(self._overlay_controller.has_calibration())

        # Data window: fleet data inventory (Chris, 2026-09-05). Not mode
        # gated - it is about the whole fleet.
        self.data_btn = QToolButton()
        self.data_btn.setText("Data")
        self.data_btn.setToolTip("How much data exists per system, from Elastic and the CCTV share")
        self.data_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.data_btn.clicked.connect(self._open_data_dialog)
        self._data_dialog: DataInventoryDialog | None = None
        # Software window: versions + commits per system over time (Chris,
        # 2026-09-05).
        self.software_btn = QToolButton()
        self.software_btn.setText("Software")
        self.software_btn.setToolTip("Package versions and git commits per system, over time")
        self.software_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.software_btn.clicked.connect(self._open_software_window)
        self._software_window: SoftwareWindow | None = None
        # Errors / Stops window (Chris, 2026-09-05): stoppages and errors
        # per day, same filter and day picker as the Overview.
        self.errors_stops_btn = QToolButton()
        self.errors_stops_btn.setText("Errors / Stops")
        self.errors_stops_btn.setToolTip("Line stoppages and errors per day, by system")
        self.errors_stops_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.errors_stops_btn.clicked.connect(self._open_errors_window)
        self._errors_window: ErrorsStopsWindow | None = None

        # One gear top-right on every window (Chris, 2026-09-07): Data
        # sources, Settings, Systems, Readme, the zoom row and About. The
        # zoom shortcuts (Ctrl+= / Ctrl+- / Ctrl+0) stay as hidden
        # application-wide actions so they work without opening the menu.
        self.gear_btn = build_gear_button(self, self, extra_actions=[self.stop_report_action, self.fit_timeline_action])
        # Refresh as an icon button at the top right, next to the gear
        # (Chris, 2026-09-11).
        self.refresh_btn = self.replay_timeline.refresh_btn
        self.refresh_btn.setText("")
        self.refresh_btn.setIcon(refresh_icon())
        self.refresh_btn.setIconSize(QSize(20, 20))
        self.refresh_btn.setFixedSize(QSize(34, 30))
        self.refresh_btn.setToolTip("Refresh the day's data")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        for delta, shortcut in (
            (theme.ZOOM_STEP, "Ctrl+="),
            (-theme.ZOOM_STEP, "Ctrl+-"),
            (0.0, "Ctrl+0"),
        ):
            action = QAction(self)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.ApplicationShortcut)
            action.triggered.connect(lambda _checked=False, d=delta: self._change_zoom(d))
            self.addAction(action)

        top_controls.addStretch(1)
        # The main camera's Sync Time button sits in the top bar, left of
        # Conveyor (Chris, 2026-09-12); it used to live in the sync strip.
        self.viewer.video_sync_btn.setFixedWidth(128)
        strip = self.viewer.video_sync_btn.parentWidget()
        if strip is not None and strip.layout() is not None:
            strip.layout().removeWidget(self.viewer.video_sync_btn)
        # Jump to the first product seen on the clip - the best moment to
        # set the drift (Chris, 2026-09-14). Left of the drift tool.
        self.first_product_btn = QToolButton()
        self.first_product_btn.setIcon(first_product_icon(22))
        self.first_product_btn.setIconSize(QSize(22, 22))
        self.first_product_btn.setToolTip("Go to the first product seen on this clip - the best moment to set the drift")
        self.first_product_btn.setCursor(Qt.PointingHandCursor)
        self.first_product_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.first_product_btn.clicked.connect(self._go_to_first_product)
        top_controls.addWidget(self.first_product_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.viewer.drift_tool, 0, Qt.AlignRight)
        top_controls.addWidget(self.viewer.video_sync_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.conveyor_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.track_toggle, 0, Qt.AlignRight)
        top_controls.addWidget(self.targets_toggle, 0, Qt.AlignRight)
        top_controls.addWidget(self.data_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.errors_stops_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.software_btn, 0, Qt.AlignRight)
        # Newer-version pill (Chris, 2026-09-07): an instance left open
        # checks every ten minutes whether the checkout or origin/main has
        # moved on, and offers a restart into the new version.
        self.update_btn = QPushButton("")
        self.update_btn.setStyleSheet(theme.UPDATE_PILL)
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.clicked.connect(self._restart_for_update)
        self.update_btn.hide()
        top_controls.addWidget(self.update_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.refresh_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.gear_btn, 0, Qt.AlignRight)
        self._update_slot = JobSlot(self)
        self._update_info: dict | None = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(10 * 60 * 1000)
        self._update_timer.timeout.connect(self._check_for_update)
        self._update_timer.start()
        QTimer.singleShot(90_000, self._check_for_update)

        # Activity bar: a persistent strip at the very bottom showing what
        # the app is waiting on — clip downloads with size and a green
        # progress bar, OCR sync stages (Chris, 2026-09-04).
        self._activity_items: dict[str, tuple[str, int | None, int | None, float]] = {}
        # key -> (t0, bytes0): first progress report of a transfer, for the
        # rate behind the time-remaining estimate.
        self._activity_rate_anchors: dict[str, tuple[float, int]] = {}
        self._activity_bar = QWidget()
        self._activity_bar.setFixedHeight(34)
        self._activity_bar.setStyleSheet(theme.ACTIVITY_BAR)
        # Text on top, a slim full-width progress bar underneath (Chris).
        activity_layout = QVBoxLayout(self._activity_bar)
        activity_layout.setContentsMargins(8, 3, 8, 3)
        activity_layout.setSpacing(3)
        self._activity_label = QLabel("")
        self._activity_label.setStyleSheet(theme.ACTIVITY_BAR_TEXT)
        self._activity_progress = QProgressBar()
        self._activity_progress.setFixedHeight(5)
        self._activity_progress.setTextVisible(False)
        self._activity_progress.setStyleSheet(theme.ACTIVITY_PROGRESS)
        self._activity_progress.hide()
        activity_layout.addWidget(self._activity_label)
        activity_layout.addWidget(self._activity_progress)
        # Sweep entries whose source stopped reporting (a worker that died
        # without a finished signal) so the bar can never stick.
        self._activity_sweep_timer = QTimer(self)
        self._activity_sweep_timer.setInterval(5000)
        self._activity_sweep_timer.timeout.connect(self._sweep_stale_activity)
        self._activity_sweep_timer.start()

        layout = QVBoxLayout()
        layout.addLayout(top_controls)
        layout.addWidget(self._main_splitter, 1)
        layout.addWidget(self._activity_bar, 0)
        self.setLayout(layout)
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.date_picker.installEventFilter(self)
        self.replay_timeline.installEventFilter(self)
        self.replay_timeline.view.viewport().installEventFilter(self)

    # ---- activity bar -----------------------------------------------------

    @staticmethod
    def _mb(n: int) -> str:
        return f"{n / (1024 * 1024):.0f}"

    def _set_activity(self, key: str, label: str, done: int | None, total: int | None):
        now = time.monotonic()
        if total:
            anchor = self._activity_rate_anchors.get(key)
            # Re-anchor when a new transfer reuses the key (bytes went down).
            if anchor is None or int(done or 0) < anchor[1]:
                self._activity_rate_anchors[key] = (now, int(done or 0))
        self._activity_items[key] = (label, done, total, now)
        self._update_activity_bar()

    def _clear_activity(self, key: str):
        self._activity_rate_anchors.pop(key, None)
        if self._activity_items.pop(key, None) is not None:
            self._update_activity_bar()

    def _activity_rate(self, key: str, done: int) -> float | None:
        """Observed transfer rate in bytes/sec, None until measurable."""
        anchor = self._activity_rate_anchors.get(key)
        if anchor is None:
            return None
        t0, bytes0 = anchor
        elapsed = time.monotonic() - t0
        if elapsed < 1.0 or done <= bytes0:
            return None
        return (done - bytes0) / elapsed

    @staticmethod
    def _eta_text(remaining_bytes: float, rate_bytes_per_sec: float) -> str:
        secs = remaining_bytes / rate_bytes_per_sec
        if secs < 5:
            return "a few seconds left"
        if secs < 90:
            return f"~{int(round(secs))}s left"
        minutes, seconds = divmod(int(round(secs)), 60)
        return f"~{minutes}m {seconds:02d}s left"

    def _on_clip_transfer_progress(self, source_path: str, done, total):
        label = f"Downloading {Path(source_path).name}"
        self._set_activity(f"clip:{source_path}", label, int(done or 0), int(total or 0))
        self.viewer.show_download_progress(source_path, int(done or 0), int(total or 0), self._activity_label.text())

    def _on_clip_transfer_finished(self, source_path: str, _ok: bool):
        self._clear_activity(f"clip:{source_path}")

    def _sweep_stale_activity(self):
        cutoff = time.monotonic() - 15.0
        stale = [k for k, v in self._activity_items.items() if v[3] < cutoff]
        for key in stale:
            self._activity_items.pop(key, None)
            self._activity_rate_anchors.pop(key, None)
        if stale:
            self._update_activity_bar()

    def _update_activity_bar(self):
        downloads = {k: v for k, v in self._activity_items.items() if v[2]}
        busy = {k: v for k, v in self._activity_items.items() if not v[2]}
        if downloads:
            done = sum(v[1] or 0 for v in downloads.values())
            total = sum(v[2] or 0 for v in downloads.values())
            pct = f" ({int(done * 100 / total)}%)" if total else ""
            if len(downloads) == 1:
                (label, d, t, _ts) = next(iter(downloads.values()))
                text = f"{label} — {self._mb(d or 0)} / {self._mb(t)} MB{pct}"
            else:
                text = (
                    f"Downloading {len(downloads)} files — "
                    f"{self._mb(done)} / {self._mb(total)} MB{pct}"
                )
            rate = sum(
                r
                for k, v in downloads.items()
                if (r := self._activity_rate(k, v[1] or 0)) is not None
            )
            remaining = total - done
            if rate > 0:
                text += f" — {rate / (1024 * 1024):.1f} MB/s"
            if rate > 0 and remaining > 0:
                text += f" — {self._eta_text(remaining, rate)}"
            self._activity_label.setText(text)
            self._activity_progress.setRange(0, 1000)
            self._activity_progress.setValue(int(done * 1000 / total) if total else 0)
            self._activity_progress.show()
        elif busy:
            label = list(busy.values())[-1][0]
            self._activity_label.setText(label)
            self._activity_progress.setRange(0, 0)  # busy indicator
            self._activity_progress.show()
        else:
            self._activity_label.setText("")
            self._activity_progress.hide()

    def _wire_signals(self):
        """Cross-panel signal wiring and the deferred startup steps."""
        self.viewer.clip_cache.transfer_progress.connect(self._on_clip_transfer_progress)
        self.viewer.clip_cache.transfer_finished.connect(self._on_clip_transfer_finished)
        self.viewer.activity_progress.connect(self._set_activity)
        self.overview_widget.activity_progress.connect(self._set_activity)
        self.overview_widget.activity_cleared.connect(self._clear_activity)
        self.viewer.activity_cleared.connect(self._clear_activity)
        self.date_picker.date_selected.connect(self.on_date_selected)
        self.date_picker.system_id_selected.connect(self._set_system_id_override)
        # Settings button removed from DatePicker UI
        self.replay_timeline.time_selected.connect(self.on_time_chosen)
        self.replay_timeline.event_clicked.connect(self._on_timeline_event_clicked)
        self.replay_timeline.moment_clicked.connect(self._on_timeline_moment_clicked)
        self.replay_timeline.items_changed.connect(self._sync_viewer_sku_overlay)
        self.replay_timeline.items_changed.connect(self._on_items_changed_for_navigation)
        self.viewer.clip_opened.connect(self._on_clip_opened_for_navigation)
        self._viewer_tools_available = False
        self.viewer.clip_opened.connect(self._on_first_clip_opened)
        if ENABLE_DAY_PREFETCH:
            self.replay_timeline.items_changed.connect(self._prefetch_day_clips)
        self.viewer.current_time_changed.connect(self.replay_timeline.set_playhead_datetime)
        self.viewer.clip_range_export_requested.connect(self._export_current_viewer_clip_range)
        self.viewer.annotation_status_changed.connect(self.replay_timeline.mark_video_annotated)
        self.viewer.cache_clip_ready.connect(self.replay_timeline.mark_video_cached)
        self.viewer.current_time_changed.connect(self._overlay_controller.on_playhead)
        self.viewer.close_gap_threshold_changed.connect(
            self._overlay_controller.on_gap_threshold_changed
        )
        self.viewer.set_export_target_overlay_provider(
            self._overlay_controller.export_overlays_for
        )
        self.date_picker_toggle.toggled.connect(
            lambda checked: self._set_date_picker_visible(checked, self._horizontal_splitter)
        )

        # Apply last parent if available
        if self.settings.last_parent:
            p = Path(self.settings.last_parent)
            if p.exists():
                self.date_picker.set_parent_dir(p)
                self.overview_widget.set_parent_dir(p)
                self.fleetwide_search_widget.set_parent_dir(p)
        QTimer.singleShot(0, self._apply_initial_timeline_size)
        QTimer.singleShot(600, self._maybe_resume_last_session)
        QTimer.singleShot(0, self._sync_overview_mode)
        QTimer.singleShot(
            0,
            lambda: self._overlay_controller.set_tracking_enabled(self.track_toggle.isChecked()),
        )

    def _change_zoom(self, delta: float) -> None:
        target = 1.0 if delta == 0.0 else theme.zoom_factor() + delta
        theme.set_zoom(QApplication.instance(), target)
        self.overview_widget.refresh_layout()

    # ---- gear menu host (every window forwards here) --------------------
    def open_data_sources(self) -> None:
        self.viewer.open_data_sources()

    def open_settings(self) -> None:
        self.viewer.open_config_tab("Settings")

    def open_systems(self) -> None:
        self.viewer.open_config_tab("Systems")

    def open_readme(self) -> None:
        self.viewer.open_config_tab("Readme")

    def change_zoom(self, delta: float) -> None:
        self._change_zoom(delta)

    def open_about(self) -> None:
        self._open_about_dialog()

    def _open_data_dialog(self):
        if self._data_dialog is None:
            self._data_dialog = DataInventoryDialog(
                settings_provider=lambda: self.settings,
                parent_dir_provider=lambda: self.date_picker.parent_dir,
                parent=self,
                gear_host=self,
                day_selection=self.day_selection,
            )
        show_over_parent(self._data_dialog, 1180, 720)
        self._data_dialog.start_if_needed()

    def _shutdown_data_dialog(self):
        if self._data_dialog is not None:
            self._data_dialog.shutdown()
        if self._software_window is not None:
            self._software_window.shutdown()
        if self._errors_window is not None:
            self._errors_window.shutdown()

    def _open_errors_window(self):
        if self._errors_window is None:
            self._errors_window = ErrorsStopsWindow(
                settings_provider=lambda: self.settings,
                known_systems_provider=self.overview_widget._known_system_names,
                parent=self,
                open_system=self._open_system_from_errors,
                gear_host=self,
                day_selection=self.day_selection,
            )
        show_over_parent(self._errors_window, 1280, 880)
        self._errors_window.start_if_needed()

    def _open_software_window(self):
        if self._software_window is None:
            self._software_window = SoftwareWindow(settings_provider=lambda: self.settings, parent=self, gear_host=self)
        show_over_parent(self._software_window, 1320, 820)
        self._software_window.start_if_needed()

    # ---- newer version available ------------------------------------------

    def _check_for_update(self) -> None:
        if self._update_slot.is_running() or getattr(self, "_shutdown_in_progress", False):
            return
        self._update_slot.start(
            lambda job: latest_available_version(),
            on_result=self._on_update_result,
            on_error=lambda _m: None,
        )

    def _on_update_result(self, info) -> None:
        if not isinstance(info, dict):
            return
        current = str(load_version_info().get("version") or "")
        latest = str(info.get("version") or "")
        if not is_newer(latest, current):
            return
        self._update_info = info
        where = "on GitHub" if str(info.get("source", "")).startswith("origin") else "in this checkout"
        self.update_btn.setText(f"v{latest} available · Restart")
        self.update_btn.setToolTip(
            f"You are running v{current}; v{latest} ({info.get('git_sha') or '?'}) is {where}. "
            "Click to close this instance and start the new one."
        )
        self.update_btn.show()
        self._set_activity("update", f"Version {latest} is available. Click Restart at the top right to update.", None, None)

    def _restart_for_update(self) -> None:
        info = self._update_info or {}
        if str(info.get("source", "")).startswith("origin"):
            # The new code is only on the remote: bring it in first.
            try:
                subprocess.run(["git", "pull", "--ff-only", "--quiet"], cwd=str(REPO_ROOT),
                               timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as exc:
                self._set_activity("update", f"Could not pull the new version: {exc}", None, None)
                return
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._spawn_new_instance)
        self.close()

    @staticmethod
    def _spawn_new_instance() -> None:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, *sys.argv[1:]]
        else:
            cmd = [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(cmd, cwd=os.getcwd(), creationflags=flags, close_fds=True)
        except Exception as exc:
            print(f"[update] could not start the new instance: {exc}")

    def _open_about_dialog(self):
        from logfather.ui.about_page import AboutDialog

        dlg = AboutDialog(self)
        dlg.exec()

    def _current_calibration_system_id(self) -> str:
        if self.system_id_override:
            return str(self.system_id_override)
        top_dir = self.date_picker.top_dir
        if isinstance(top_dir, Path):
            return str(top_dir.name)
        active_name = self.date_picker.active_pikpak_name
        if isinstance(active_name, str) and active_name and active_name != "__SIM__":
            return active_name
        return ""

    def showEvent(self, event):
        super().showEvent(event)
        if self._post_show_started:
            return
        self._post_show_started = True
        QTimer.singleShot(0, self.viewer.start_background_maintenance)

    def _apply_initial_timeline_size(self):
        sizes = self._main_splitter.sizes()
        if len(sizes) < 2:
            return
        total = max(1, int(sum(sizes)))
        bottom = max(self._timeline_min_height, min(self._timeline_max_height, sizes[1]))
        if total > 1:
            bottom = min(bottom, total - 1)
        self._main_splitter.setSizes([max(1, total - bottom), bottom])

    def _build_static_tracks(self) -> list[tuple[str, str, str]]:
        static_tracks = [
            ("video", "CCTV", "#cce5ff"),
            ("additional", "Additional CCTV", "#9fb3c8"),
            ("sku", "SKU", "#8fd19e"),
            ("telemetry", "Telemetry", "#ff8a65"),
        ]
        for idx, cond in enumerate(self.settings.conditions):
            kind = f"cond_{idx}"
            label = cond.name or f"Cond {idx+1}"
            color = cond.color or ""
            static_tracks.append((kind, label, color))
        return static_tracks

    def on_date_selected(self, pikpak_root: Path | None, day: date | None):
        self.viewer.prepare_for_new_clip(show_loading=False)
        # Past the share's retention there is nothing to play: say so
        # where the footage would be (Chris, 2026-09-07).
        self.viewer.set_footage_notice(FOOTAGE_DELETED_NOTICE if footage_expired(day) else None)
        self.replay_timeline.show_times(pikpak_root, day)
        self._update_current_system_label(pikpak_root, day)
        self._load_telemetry(pikpak_root, day)
        if self.date_picker.parent_dir:
            self.overview_widget.set_parent_dir(self.date_picker.parent_dir)
        self._overlay_controller.clear()
        self._overlay_controller.reload_calibration()

    def _load_telemetry(self, pikpak_root: Path | None, day: date | None) -> None:
        panel = self.telemetry_panel
        panel.set_playhead(None)
        self.replay_timeline.set_telemetry_summary(None)
        if not isinstance(pikpak_root, Path) or day is None:
            panel.set_data(None, "Choose a system and a day.")
            return
        robot = robot_id_from_folder(pikpak_root.name)
        if not robot:
            panel.set_data(None, f"No robot id for {pikpak_root.name}.")
            return
        if not grafana_client.is_configured(self.settings):
            panel.set_data(None, "Grafana is not set up: gear menu, Data sources, then a Grafana token.")
            return
        panel.set_data(None, f"Loading telemetry for {robot} on {day:%a %d %b}…")
        settings = self.settings
        self._telemetry_slot.start(
            lambda job: fetch_telemetry_day(settings, robot, day, job),
            on_result=self._on_telemetry_loaded,
            on_error=lambda message: panel.set_data(None, f"Telemetry failed: {message}"),
        )

    def _on_telemetry_loaded(self, data) -> None:
        self.telemetry_panel.set_data(data)
        self.replay_timeline.set_telemetry_summary(summary_track(data))

    def _update_current_system_label(self, pikpak_root: Path | None, day: date | None = None):
        if not isinstance(pikpak_root, Path):
            if self.system_id_override:
                self.current_system_label.setText(self.system_id_override)
            else:
                self.current_system_label.setText("")
            self._refresh_chooser_buttons(None, None)
            return
        system_name = pikpak_root.name
        customer = display_customer_name(self.settings, system_name)
        line = display_line_name(self.settings, system_name)
        parts = [customer]
        if line:
            parts.append(line)
        parts.append(system_name)
        self.current_system_label.setText(" / ".join([part for part in parts if part]))
        self._refresh_chooser_buttons(pikpak_root, day)

    def _refresh_chooser_buttons(self, pikpak_root: Path | None, day: date | None) -> None:
        self._chosen_root = pikpak_root if isinstance(pikpak_root, Path) else None
        self._chosen_day = day if isinstance(day, date) else self._pending_day
        self._update_chooser_pulse()
        if isinstance(pikpak_root, Path):
            # Customer and system only; no line name (Chris, 2026-09-07).
            customer = display_customer_name(self.settings, pikpak_root.name)
            self.choose_system_btn.setText(f"{customer} / {pikpak_root.name}" if customer else pikpak_root.name)
            self.choose_date_btn.setEnabled(True)
            self.choose_date_btn.setText(f"{day:%a %d %b %Y}" if day else "Choose date")
        else:
            self.choose_system_btn.setText(self.system_id_override or "Choose system")
            pending = self._pending_day
            self.choose_date_btn.setText(f"{pending:%a %d %b %Y}" if pending else "Choose date")

    def _update_chooser_pulse(self) -> None:
        in_viewer = self.content_stack.currentWidget() is self.viewer
        target = None
        if in_viewer and not self.system_id_override:
            if self._chosen_root is None:
                target = self.choose_system_btn
            elif self._chosen_day is None:
                target = self.choose_date_btn
        self._chooser_pulser.set_target(target)

    def _show_system_menu(self) -> None:
        """The same grouped box as the Overview's Systems filter, choosing
        one system (Chris, 2026-09-07); built from the cached share
        listing so it opens at once."""
        parent_dir = self.date_picker.parent_dir
        groups: list[tuple[str, list[str]]] = []
        if isinstance(parent_dir, Path):
            names = sorted(
                self.overview_widget._known_system_names(),
                key=lambda n: system_group_sort_key(self.settings, n),
            )
            for name in names:
                customer = str(display_customer_name(self.settings, name) or "")
                if groups and groups[-1][0] == customer:
                    groups[-1][1].append(name)
                else:
                    groups.append((customer, [name]))
        if not groups:
            groups = [("", ["No systems found - set the CCTV parent folder in Settings"])]
        popup = SystemPickerPopup(
            groups,
            self.date_picker.active_pikpak_name,
            lambda name: self._choose_system(parent_dir / name) if isinstance(parent_dir, Path) else None,
            parent=self,
        )
        self._system_picker = popup
        popup.move(self.choose_system_btn.mapToGlobal(QPoint(0, self.choose_system_btn.height())))
        popup.show()

    def _choose_system(self, path: Path) -> None:
        self.replay_btn.setChecked(True)
        pending = self._pending_day
        if pending is not None:
            self._pending_day = None
            self.date_picker.select_pikpak_folder_and_day(path, pending)
        else:
            self.date_picker.use_pikpak_folder(path)

    def _show_day_popup(self) -> None:
        top_dir = self.date_picker.top_dir
        if not isinstance(top_dir, Path):
            # No system yet: any past day; footage days are only known
            # once a system is chosen.
            self._day_popup.open_for(
                "Choose a day (then a system)",
                set(),
                self._pending_day,
                self.choose_date_btn.mapToGlobal(QPoint(0, self.choose_date_btn.height())),
            )
            return
        self._day_popup.open_for(
            self.current_system_label.text() or top_dir.name,
            self.date_picker.available_dates,
            self.date_picker.active_day,
            self.choose_date_btn.mapToGlobal(QPoint(0, self.choose_date_btn.height())),
            scanning=self.date_picker._scan_slot.is_running(),
        )

    def _on_popup_day_chosen(self, day) -> None:
        if not isinstance(day, date):
            return
        if isinstance(self.date_picker.top_dir, Path):
            self.date_picker.select_day(day)
        else:
            self._pending_day = day
            self._refresh_chooser_buttons(None, None)

    # ------------------------------------------------------------------
    # Pick-buffer panel
    # ------------------------------------------------------------------

    def _set_targets_panel_visible(self, visible: bool) -> None:
        self._targets_panel_visible = bool(visible)
        self._overlay_controller.panel_visible = self._targets_panel_visible
        if self._targets_panel_visible and self._overlay_controller._last_playhead_dt:
            self.targets_panel.update_for_time(self._overlay_controller._last_playhead_dt)
        if self._targets_panel_visible:
            self.targets_panel.setVisible(True)
            self._animate_targets_panel(self._buffer_panel_target_width)
        else:
            self._animate_targets_panel(0)

    def _animate_targets_panel(self, end_width: int) -> None:
        splitter = self._horizontal_splitter
        sizes = splitter.sizes()
        current = sizes[2] if len(sizes) > 2 else 0
        if int(current) == int(end_width):
            if end_width == 0:
                self.targets_panel.setVisible(False)
            return
        if self._targets_panel_anim.state() == QVariantAnimation.Running:
            self._targets_panel_anim.stop()
        self._targets_panel_anim.setStartValue(int(current))
        self._targets_panel_anim.setEndValue(int(end_width))
        self._targets_panel_anim.start()

    def _on_targets_panel_anim_step(self, value: int) -> None:
        try:
            right = max(0, int(value))
            splitter = self._horizontal_splitter
            sizes = splitter.sizes()
            if len(sizes) < 3:
                return
            total = sum(sizes)
            left = sizes[0]
            centre = max(1, total - left - right)
            splitter.setSizes([left, centre, right])
        except Exception:
            pass

    def _on_targets_panel_anim_finished(self) -> None:
        if not self._targets_panel_visible:
            self.targets_panel.setVisible(False)

    def _set_system_id_override(self, system_id: str | None):
        self.system_id_override = system_id or None
        set_system_id_override(self.system_id_override)
        self._overlay_controller.reload_calibration()

    def _set_date_picker_visible(self, visible: bool, splitter: QSplitter):
        _ = splitter
        if getattr(self, "_left_panel_retired", False):
            return
        self._left_panel_visible = bool(visible)
        if self._left_panel_visible:
            self.date_picker_toggle.setText("Hide Date Picker")
            if not self.date_picker.isVisible():
                # Ensure the panel starts collapsed, otherwise splitter may
                # restore old width instantly and skip visible animation.
                self.date_picker.setVisible(True)
                sizes = self._horizontal_splitter.sizes()
                total = max(1, sum(sizes) or self.width())
                buf = sizes[2] if len(sizes) > 2 else 0
                self._horizontal_splitter.setSizes([0, max(1, total - buf), buf])
            self._animate_left_panel(self._left_panel_target_width)
        else:
            self.date_picker_toggle.setText("Show Date Picker")
            self._animate_left_panel(0)

    def _animate_left_panel(self, end_width: int):
        splitter = self._horizontal_splitter
        sizes = splitter.sizes()
        current = sizes[0] if sizes else (self._left_panel_target_width if self.date_picker.isVisible() else 0)
        if int(current) == int(end_width):
            if end_width == 0:
                self.date_picker.setVisible(False)
            return
        if self._left_panel_anim.state() == QVariantAnimation.Running:
            self._left_panel_anim.stop()
        self._left_panel_anim.setStartValue(int(current))
        self._left_panel_anim.setEndValue(int(end_width))
        self._left_panel_anim.start()

    def _on_left_panel_anim_step(self, value):
        try:
            left = max(0, int(value))
            splitter = self._horizontal_splitter
            sizes = splitter.sizes()
            total = sum(sizes) or max(self.width(), left + 1000)
            buf = sizes[2] if len(sizes) > 2 else 0
            centre = max(1, total - left - buf)
            if len(sizes) > 2:
                splitter.setSizes([left, centre, buf])
            else:
                splitter.setSizes([left, centre])
        except Exception:
            pass

    def _on_left_panel_anim_finished(self):
        if not self._left_panel_visible:
            self.date_picker.setVisible(False)

    def eventFilter(self, obj, event):
        if not self._hover_reveal_enabled:
            return super().eventFilter(obj, event)
        if event.type() == QEvent.MouseMove:
            if not self._left_panel_visible:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                if pos.x() <= self._left_reveal_px:
                    self._set_date_picker_visible(True, self._horizontal_splitter)
            if self._is_timeline_obj(obj):
                self._schedule_timeline_expand()
        elif event.type() == QEvent.Leave and obj is self.date_picker:
            QTimer.singleShot(50, self._auto_hide_if_outside)
        elif event.type() == QEvent.Leave and self._is_timeline_obj(obj):
            self._cancel_timeline_expand()
            QTimer.singleShot(60, self._auto_contract_timeline)
        return super().eventFilter(obj, event)

    def _is_timeline_obj(self, obj) -> bool:
        if obj is self.replay_timeline:
            return True
        return obj is self.replay_timeline.view.viewport()

    def _set_timeline_expanded(self, expanded: bool):
        expanded = bool(expanded)
        if expanded == self._timeline_expanded:
            return
        self._timeline_expanded = expanded
        target = self._timeline_max_height if expanded else self._timeline_min_height
        self._animate_timeline_height(target)

    def _animate_timeline_height(self, target_height: int):
        sizes = self._main_splitter.sizes()
        if len(sizes) < 2:
            return
        bottom_current = max(0, int(sizes[1]))
        total = max(1, int(sum(sizes)))
        target = max(self._timeline_min_height, min(self._timeline_max_height, int(target_height)))
        if total > 1:
            target = min(target, total - 1)
        if bottom_current == target:
            return
        if self._timeline_anim.state() == QVariantAnimation.Running:
            self._timeline_anim.stop()
        self._timeline_anim.setStartValue(bottom_current)
        self._timeline_anim.setEndValue(target)
        self._timeline_anim.start()

    def _on_timeline_anim_step(self, value):
        sizes = self._main_splitter.sizes()
        if len(sizes) < 2:
            return
        total = max(1, int(sum(sizes)))
        bottom = max(0, int(value))
        if total > 1:
            bottom = min(bottom, total - 1)
        top = max(1, total - bottom)
        self._main_splitter.setSizes([top, bottom])

    def _schedule_timeline_expand(self):
        if self._timeline_expanded:
            return
        if not self._timeline_expand_timer.isActive():
            self._timeline_expand_timer.start()

    def _cancel_timeline_expand(self):
        if self._timeline_expand_timer.isActive():
            self._timeline_expand_timer.stop()

    def _auto_contract_timeline(self):
        pos = self.replay_timeline.mapFromGlobal(self.cursor().pos())
        if self.replay_timeline.rect().contains(pos):
            return
        self._cancel_timeline_expand()
        self._set_timeline_expanded(False)


    def _auto_hide_if_outside(self):
        if not self.date_picker.isVisible():
            return
        if self.date_picker.active_day is None:
            return
        # Hide if mouse is not over date picker anymore.
        pos = self.date_picker.mapFromGlobal(self.cursor().pos())
        if not self.date_picker.rect().contains(pos):
            self._set_date_picker_visible(False, self._horizontal_splitter)

    def _capture_window_geometry(self) -> None:
        """Write the current geometry + maximized flag onto self.settings.
        normalGeometry() when maximized, so un-maximizing after a restart
        returns to a sensible size instead of the full-screen rect."""
        try:
            geo = self.normalGeometry() if self.isMaximized() else self.geometry()
            self.settings.window_geometry = {
                "x": geo.x(),
                "y": geo.y(),
                "w": geo.width(),
                "h": geo.height(),
                "maximized": bool(self.isMaximized()),
            }
        except Exception:
            pass

    def _save_last_session(self, playhead_override: datetime | None = None) -> None:
        """Remember system/day/playhead so startup can offer to resume.

        Saved on every clip open and once a minute (not just at close), so a
        killed process still resumes close to where the user was — and the
        window geometry rides along, because a killed process never runs
        closeEvent (Chris, 2026-09-05: maximized state kept getting lost)."""
        self._capture_window_geometry()
        root = self.replay_timeline.current_root
        day = self.replay_timeline._current_date
        if root is None or day is None:
            # Nothing to resume (e.g. an overview-only session), but the
            # geometry must still persist.
            try:
                self.settings.save()
            except Exception:
                pass
            return
        playhead = (
            playhead_override
            if playhead_override is not None
            else self._overlay_controller._last_playhead_dt
        )
        playhead_iso = None
        if isinstance(playhead, datetime):
            playhead_iso = ensure_playhead_local(playhead).astimezone(timezone.utc).isoformat()
        self.settings.last_session = {
            "root": str(root),
            "day": day.isoformat(),
            "playhead": playhead_iso,
            "mode": self._current_mode_name(),
        }
        self.settings.save()
        print(f"[main] session saved: {root.name} {day.isoformat()} @ {playhead_iso}", flush=True)

    def _current_mode_name(self) -> str:
        if self.overview_btn.isChecked():
            return "overview"
        if self.search_btn.isChecked():
            return "search"
        return "replay"

    def _maybe_resume_last_session(self) -> None:
        # Always asks; there is deliberately no "remember my choice"
        # (Chris, 2026-09-03 — a remembered "never" was too easy to
        # set once and impossible to discover later).
        session = self.settings.last_session
        if not isinstance(session, dict):
            return
        try:
            root = Path(str(session.get("root")))
            day = date.fromisoformat(str(session.get("day")))
        except Exception:
            return
        target_dt = None
        playhead_raw = session.get("playhead")
        if isinstance(playhead_raw, str):
            try:
                target_dt = datetime.fromisoformat(playhead_raw)
            except ValueError:
                target_dt = None
        if not root.exists():
            return
        when = (
            target_dt.astimezone().strftime("%H:%M:%S")
            if target_dt is not None
            else "start of day"
        )
        box = QMessageBox(self)
        box.setWindowTitle("Resume session")
        box.setText(
            "Resume where you left off?\n\n"
            f"{root.name} on {day.strftime('%d/%m/%Y')} at {when}"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        # Reuse the signal-driven jump: select system+day, open the clip
        # containing the playhead moment, sync and seek once it is open.
        self._open_system_from_overview(root, day, target_dt)
        # That jump switches to Viewer; restore the screen the session
        # was actually on — the app stays on Overview unless the saved
        # session was elsewhere (Chris, 2026-09-04). Sessions saved
        # before the mode field default to viewer, matching old
        # behaviour; the clip still loads in the background either way.
        saved_mode = str(session.get("mode") or "replay")
        # Older sessions saved the pre-2026-09-14 names.
        saved_mode = {"viewer": "replay", "fleetwide": "search"}.get(saved_mode, saved_mode)
        if saved_mode == "overview":
            self.overview_btn.setChecked(True)
        elif saved_mode == "search":
            self.search_btn.setChecked(True)

    def closeEvent(self, event):
        # Qt delivers close events only to the top-level window: the panels'
        # own closeEvents never fire inside the app, so every worker thread
        # must be stopped from here or it races Qt teardown and crashes.
        # A small always-on-top popup narrates the steps (Chris, 2026-09-03:
        # closing could take seconds with no sign anything was happening),
        # and each step's duration is printed so slow ones are attributable.
        if getattr(self, "_shutdown_in_progress", False):
            event.accept()
            return
        self._shutdown_in_progress = True
        t_shutdown = time.perf_counter()
        popup = QWidget(
            None,
            Qt.SplashScreen | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint,
        )
        popup.setStyleSheet(theme.SHUTDOWN_POPUP)
        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(24, 18, 24, 18)
        popup_title = QLabel("Shutting down...")
        popup_title.setStyleSheet(theme.POPUP_TITLE)
        popup_step = QLabel("")
        popup_step.setStyleSheet(theme.POPUP_STEP)

        steps = (
            ("Stopping target-overlay worker", self._overlay_controller.shutdown),
            ("Stopping stop-report worker", self._stop_report_slot.shutdown),
            ("Stopping update check", self._update_slot.shutdown),
            ("Stopping date scan", self.date_picker.stop_scan_thread),
            ("Stopping timeline loader", self.replay_timeline.shutdown_workers),
            ("Stopping overview loader", self.overview_widget.shutdown_workers),
            ("Stopping fleetwide search", self.fleetwide_search_widget.shutdown_workers),
            ("Stopping data inventory", self._shutdown_data_dialog),
            ("Saving settings, stopping viewer workers", self.viewer.shutdown_workers),
        )
        popup_bar = QProgressBar()
        popup_bar.setRange(0, len(steps) + 1)
        popup_bar.setTextVisible(False)
        popup_layout.addWidget(popup_title)
        popup_layout.addWidget(popup_step)
        popup_layout.addWidget(popup_bar)
        popup.setMinimumWidth(360)

        def _step(label: str, done: int):
            popup_step.setText(label)
            popup_bar.setValue(done)
            popup.show()
            QApplication.processEvents()

        for done, (label, shutdown) in enumerate(steps):
            _step(label, done)
            t0 = time.perf_counter()
            try:
                shutdown()
            except Exception as exc:
                print(f"[main] shutdown step failed: {exc}", flush=True)
            dt_ms = (time.perf_counter() - t0) * 1000
            if dt_ms > 100:
                print(f"[shutdown] '{label}' took {dt_ms:.0f}ms", flush=True)
        _step("Saving session", len(steps))
        # Geometry capture and session save must come AFTER
        # viewer.shutdown_workers: its settings flush emits settings_saved,
        # which makes _reload_settings_from_viewer REPLACE self.settings —
        # anything written onto the old object before that point is lost.
        # normalGeometry() when maximized, so un-maximizing after a restart
        # returns to a sensible size instead of the full-screen rect.
        self._capture_window_geometry()
        self._save_last_session()
        # _save_last_session early-returns without saving when nothing was
        # open; the window geometry must persist regardless.
        try:
            self.settings.save()
        except Exception:
            pass
        popup_bar.setValue(len(steps) + 1)
        print(
            f"[shutdown] total {(time.perf_counter() - t_shutdown) * 1000:.0f}ms",
            flush=True,
        )
        popup.close()
        super().closeEvent(event)

    def on_time_chosen(self, item: TimelineItem):
        if item.kind == "video" and isinstance(item.payload, Path):
            # Open the clip at the moment that was clicked, not at its start
            # (Chris, 2026-09-10): same route as a click on an event tick.
            clicked = getattr(self.replay_timeline, "last_click_time", None)
            root = self.replay_timeline.current_root
            day = self.replay_timeline._current_date
            if (isinstance(clicked, datetime) and isinstance(item.start, datetime) and isinstance(item.end, datetime)
                    and ensure_utc(item.start) <= ensure_utc(clicked) < ensure_utc(item.end)
                    and isinstance(root, Path) and day is not None):
                # The green line jumps to the click straight away, before
                # the clip has loaded (Chris, 2026-09-11).
                self.replay_timeline.set_playhead_datetime(ensure_utc(clicked))
                self._pending_overview_navigation = {
                    "root": root,
                    "day": day,
                    "target_dt": ensure_utc(clicked),
                    "stage": "load_timeline",
                }
                self._overview_nav_failsafe.start()
                self._on_items_changed_for_navigation()
                return
            if isinstance(item.start, datetime):
                self.replay_timeline.set_playhead_datetime(ensure_utc(item.start))
            self.open_in_viewer(item)
        elif item.kind == "additional" and isinstance(item.payload, Path):
            self.load_additional_in_viewer(item.payload)
        else:
            QMessageBox.information(self, "Selected item", item.label)

    def open_in_viewer(self, item: TimelineItem):
        video_path = item.payload
        if not isinstance(video_path, Path) or not video_path.exists():
            QMessageBox.warning(self, "File not found", str(video_path))
            return
        t0 = time.perf_counter()
        print(f"[main] Opening video: {video_path}", flush=True)
        self.viewer.prepare_for_new_clip()
        if DEBUG_CLIP_TIMING:
            print(f"[main] prepare_for_new_clip took {time.perf_counter() - t0:.2f}s", flush=True)
        if not self.viewer.load_video_from_path(str(video_path)):
            return
        print("[main] Video loaded OK", flush=True)
        if DEBUG_CLIP_TIMING:
            print(f"[main] load_video_from_path total {time.perf_counter() - t0:.2f}s", flush=True)
        self._sync_viewer_sku_overlay()
        # Keep cache color updates, but avoid log marker updates while logs are disabled.
        # Keep cache colors in the timeline; do it off the critical path.
        if ENABLE_CACHE_COLOR_UPDATE:
            # Only update cached color in-place; avoid full timeline redraw.
            QTimer.singleShot(0, lambda: self.replay_timeline.mark_video_cached(video_path))
            if DEBUG_CLIP_TIMING:
                QTimer.singleShot(
                    0,
                    lambda: print(f"[main] timeline cache update at +{time.perf_counter() - t0:.2f}s", flush=True),
                )
        elif DEBUG_CLIP_TIMING:
            QTimer.singleShot(
                0,
                lambda: print(f"[main] timeline cache update skipped at +{time.perf_counter() - t0:.2f}s", flush=True),
            )
        if DEBUG_CLIP_TIMING:
            QTimer.singleShot(0, lambda: print(f"[main] UI tick +{time.perf_counter() - t0:.2f}s", flush=True))
            QTimer.singleShot(200, lambda: print(f"[main] UI tick +{time.perf_counter() - t0:.2f}s", flush=True))
        if ENABLE_EVENT_MARKERS:
            def _apply_markers():
                markers = self.replay_timeline.collect_event_markers(item)
                self.viewer.set_timeline_markers(markers)
                if DEBUG_CLIP_TIMING:
                    print(f"[main] timeline markers set at +{time.perf_counter() - t0:.2f}s", flush=True)
            QTimer.singleShot(0, _apply_markers)
        if ENABLE_PREFETCH_ADJACENT:
            QTimer.singleShot(0, lambda: self._prefetch_adjacent_clips(item))
        if item.start is not None:
            self._save_last_session(playhead_override=item.start)
        current_root = self.replay_timeline.current_root
        if current_root and item.start and item.end:
            self._overlay_controller.load_buffer_events(current_root, item.start, item.end)

        if ENABLE_LOG_BUTTON:
            current_root = self.replay_timeline.current_root
            if current_root and item.start and item.end:
                start_iso = item.start.isoformat()
                end_iso = (item.end + timedelta(minutes=1)).isoformat()
                if DEBUG_CLIP_TIMING:
                    print(f"[main] Logs pending for {start_iso} -> {end_iso}", flush=True)
                self.viewer.set_pending_logs(str(current_root), start_iso, end_iso)

    def _open_next_clip(self) -> bool:
        """Open the clip after the one in the viewer (Chris, 2026-09-11:
        Play at the end of a clip rolls into the next). True when a next
        clip was found and its load started."""
        current = self.viewer.current_video_original_path
        if current is None:
            return False
        try:
            key = _path_key(Path(current))
        except Exception:
            return False
        items = [it for it in (getattr(self.replay_timeline, "_items", []) or []) if it.kind == "video" and isinstance(it.payload, Path)]
        current_item = next((it for it in items if (it.path_key or _path_key(it.payload)) == key), None)
        if current_item is None:
            return False
        _prev, nxt = self.replay_timeline.get_adjacent_video_items(current_item)
        if nxt is None or not isinstance(nxt.payload, Path):
            return False
        self._cancel_overview_navigation()
        if isinstance(nxt.start, datetime):
            self.replay_timeline.set_playhead_datetime(ensure_utc(nxt.start))
        self.open_in_viewer(nxt)
        return True

    def _additional_clip_covering(self, moment) -> Path | None:
        """The day's Additional CCTV clip that covers `moment`, for the
        viewer's View menu (Chris, 2026-09-11)."""
        try:
            items = list(getattr(self.replay_timeline, "_items", []) or [])
        except Exception:
            return None

        def local_naive(dt):
            # Clip items carry UTC; the viewer's clock is naive local. Compare
            # everything as local wall time (a bare tz strip was an hour out
            # in summer and picked the wrong clip, 2026-09-11).
            return dt if dt.tzinfo is None else dt.astimezone().replace(tzinfo=None)

        try:
            when = local_naive(moment)
        except Exception:
            return None
        for item in items:
            if getattr(item, "kind", "") != "additional" or not isinstance(getattr(item, "payload", None), Path):
                continue
            start, end = item.start, item.end
            if start is None or end is None:
                continue
            try:
                if local_naive(start) <= when <= local_naive(end):
                    return item.payload
            except Exception:
                continue
        return None

    def load_additional_in_viewer(self, video_path: Path):
        if not isinstance(video_path, Path) or not video_path.exists():
            QMessageBox.warning(self, "File not found", str(video_path))
            return
        self.viewer.load_additional_cctv_from_path(video_path)

    def _prefetch_day_clips(self):
        # items_changed fires several times while a day loads; coalesce.
        timer = self._day_prefetch_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(1000)
            timer.timeout.connect(self._run_day_prefetch)
            self._day_prefetch_timer = timer
        timer.start()

    def _run_day_prefetch(self):
        paths = self.replay_timeline.video_paths()
        if paths:
            # Stop downloading a previously viewed day before queueing this one.
            self.viewer.cancel_queued_prefetches()
            print(f"[main] day prefetch: queueing {len(paths)} clips", flush=True)
            self.viewer.prefetch_clips_to_cache(paths)

    def _prefetch_adjacent_clips(self, item: TimelineItem):
        prev_item, next_item = self.replay_timeline.get_adjacent_video_items(item)
        paths: list[Path] = []
        if prev_item and isinstance(prev_item.payload, Path):
            paths.append(prev_item.payload)
        if next_item and isinstance(next_item.payload, Path):
            paths.append(next_item.payload)
        if paths:
            self.viewer.prefetch_clips_to_cache(paths)

    def _prefetch_overview_clips(self, paths: list[Path]):
        if not paths:
            return
        self.viewer.prefetch_clips_to_cache(paths)

    def _sync_viewer_sku_overlay(self):
        sku_items = [
            itm
            for itm in self.replay_timeline._items
            if itm.kind == "sku" and itm.start is not None and itm.end is not None
        ]
        sku_items.sort(key=lambda itm: itm.start)
        self.viewer.set_sku_timeline_items(sku_items)

    def _load_additional_cctv_items(self, pikpak_root: Path, day: date, cache_root: Path | None):
        additional_root = pikpak_root / "AdditionalCCTV"
        paths = list(load_day_files_cached(additional_root, day))
        if not paths:
            return []
        cache_index = _build_cache_index(cache_root) if cache_root else set()
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
            entries.append((p, ensure_utc(start_dt)))
        entries.sort(key=lambda tpl: tpl[1])
        items: list[TimelineItem] = []
        for idx, (path_obj, start_dt) in enumerate(entries):
            if idx + 1 < len(entries):
                next_start = entries[idx + 1][1]
                end_dt = next_start
                if (end_dt - start_dt) < MIN_BLOCK_DURATION:
                    end_dt = start_dt + MIN_BLOCK_DURATION
            else:
                end_dt = inferred_live_clip_end(path_obj, start_dt)
            cached = _is_path_cached(path_obj, cache_root, cache_index) if cache_root else False
            items.append(
                TimelineItem(
                    start=start_dt,
                    end=end_dt,
                    label=path_obj.name,
                    kind="additional",
                    color=VIDEO_COLOR_CACHED if cached else VIDEO_COLOR_UNCACHED,
                    payload=path_obj,
                    cached=cached,
                    path_key=_path_key(path_obj),
                    track_label="Additional CCTV",
                )
            )
        return items

    def open_stop_report(self):
        # The collect phase (Elastic fallback fetch, SMB clip copies, cv2
        # thumbnail decodes) runs on a worker; only the QPixmap conversion
        # and the dialog happen here. Starting a new build retires a
        # running one (JobSlot semantics).
        items = list(self.replay_timeline._items or [])
        settings = self.settings
        day = self.replay_timeline._current_date
        root = self.replay_timeline.current_root
        clip_cache = self.viewer.clip_cache

        self.stop_report_btn.setEnabled(False)
        self.stop_report_action.setEnabled(False)

        def _done():
            self.stop_report_btn.setEnabled(True)
            self.stop_report_action.setEnabled(True)
            if self._stop_report_progress is progress:
                self._stop_report_progress = None

        def _describe(payload):
            phase, done, total = payload
            label = "Copying report clips..." if phase == "copies" else "Reading stop thumbnails..."
            return label, done, total

        def _on_result(data):
            if not data:
                QMessageBox.information(self, "Stop Report", "No stop events found for this day.")
                return
            entries = build_stop_report_entries(data)
            dlg = StopReportDialog(entries, self)
            dlg.open_requested.connect(self._open_report_entry)
            dlg.exec()

        def _on_error(message):
            QMessageBox.warning(self, "Stop Report", f"Stop report build failed:\n{message}")

        progress = job_progress(
            self,
            "Stop Report",
            self._stop_report_slot,
            label="Building stop report...",
            parse_progress=_describe,
            on_done=_done,
        )
        self._stop_report_progress = progress
        progress.start(
            lambda job: collect_stop_report_data(
                items,
                settings=settings,
                day=day,
                root=root,
                clip_cache=clip_cache,
                job=job,
            ),
            on_result=_on_result,
            on_error=_on_error,
        )

    def _open_report_entry(self, entry: StopReportEntry):
        if entry.video_item is None or entry.video_path is None:
            return
        self.open_in_viewer(entry.video_item)
        self.viewer.seek_to_seconds(entry.seek_seconds, pause=True)

    def _export_source_path(self, original_path: Path) -> Path | None:
        viewer_original = self.viewer.current_video_original_path
        viewer_loaded = self.viewer.current_video_path
        if viewer_original is not None and Path(viewer_original) == original_path and viewer_loaded:
            viewer_loaded_path = Path(viewer_loaded)
            if viewer_loaded_path.exists():
                return viewer_loaded_path
        try:
            cached = self.viewer.get_valid_cached_path(original_path)
            if cached and cached.exists():
                return cached
            cache_path = self.viewer._cache_path_for(original_path)
            if self.viewer._ensure_cached_copy(original_path, cache_path) and cache_path.exists():
                return cache_path
        except Exception:
            pass
        return original_path if original_path.exists() else None

    def _export_current_viewer_clip_range(self, start_seconds: float, end_seconds: float):
        if end_seconds <= start_seconds:
            QMessageBox.information(self, "Export Clip", "Select a non-zero clip range first.")
            return
        viewer_original = self.viewer.current_video_original_path
        if viewer_original is None:
            QMessageBox.information(self, "Export Clip", "No video is currently loaded.")
            return
        source_path = self._export_source_path(Path(viewer_original))
        if source_path is None or not source_path.exists():
            QMessageBox.warning(self, "Export Clip", "Unable to access the source clip for export.")
            return
        start_seconds = max(0.0, float(start_seconds))
        end_seconds = max(start_seconds, float(end_seconds))
        clip_duration_seconds = end_seconds - start_seconds
        if clip_duration_seconds <= 0.0:
            QMessageBox.information(self, "Export Clip", "Select a non-zero clip range first.")
            return
        default_name = (
            f"{source_path.stem}_"
            f"{int(start_seconds // 3600):02d}{int((start_seconds % 3600) // 60):02d}{int(start_seconds % 60):02d}_"
            f"{int(end_seconds // 3600):02d}{int((end_seconds % 3600) // 60):02d}{int(end_seconds % 60):02d}.mp4"
        )
        target_path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Clip",
            str(source_path.with_name(default_name)),
            "MP4 Files (*.mp4);;All Files (*)",
        )
        if not target_path_str:
            return
        ffmpeg_path = shutil.which("ffmpeg")
        target_path = Path(target_path_str)
        ok, message = self.viewer.export_current_clip_with_overlays(
            source_path,
            start_seconds,
            end_seconds,
            target_path,
        )
        if not ok:
            QMessageBox.warning(self, "Export Clip", message or "Clip export failed.")
            return
        if message:
            QMessageBox.information(self, "Export Clip", f"Clip exported to:\n{target_path}\n\n{message}")
            return
        QMessageBox.information(self, "Export Clip", f"Clip exported to:\n{target_path}")

    @staticmethod
    def _layout_settings_snapshot(settings: Settings) -> dict:
        # Everything except the volatile per-session fields: when this is
        # unchanged, no widget rebuilt below can look any different.
        snapshot = asdict(settings)
        for key in ("last_session", "window_geometry", "load_warning"):
            snapshot.pop(key, None)
        return snapshot

    def _reload_settings_from_viewer(self):
        # Every settings save lands here via settings_saved. The rebuild
        # below re-lists the Z: share on the UI thread (seconds when a clip
        # copy saturates the link), so it runs only when a field the
        # pickers/filters consume actually changed — and never during
        # shutdown. The Settings.load() must happen unconditionally: the
        # close path saves geometry/session onto self.settings afterwards,
        # and a stale object would clobber the viewer's flush.
        self.settings = Settings.load()
        if self._shutdown_in_progress:
            return
        snapshot = self._layout_settings_snapshot(self.settings)
        if snapshot == self._last_layout_snapshot:
            return
        self._last_layout_snapshot = snapshot

        marks: list[tuple[str, float]] = [("start", time.perf_counter())]

        def mark(label: str):
            marks.append((label, time.perf_counter()))
        self.date_picker.set_system_layout_settings(self.settings)
        mark("date_picker layout")
        self.overview_widget.set_system_layout_settings(self.settings)
        mark("overview layout")
        self.fleetwide_search_widget.set_settings(self.settings)
        mark("fleetwide settings")
        self.replay_timeline._static_tracks = self._build_static_tracks()
        self.replay_timeline.settings = self.settings
        mark("static tracks")
        current_parent = self.date_picker.parent_dir
        target_parent = Path(self.settings.last_parent) if self.settings.last_parent else None
        if target_parent:
            # String comparison first: .resolve()/.exists() on the Z: share
            # run on the UI thread and stall for seconds while a clip copy
            # saturates the link — this fires on EVERY settings autosave.
            same_parent = current_parent is not None and _path_key(
                current_parent
            ) == _path_key(target_parent)
            if same_parent:
                self.overview_widget.set_parent_dir(target_parent)
                self.fleetwide_search_widget.set_parent_dir(target_parent)
                mark("parent dirs (same)")
            elif target_parent.exists():  # network stat only on a real change
                mark("target_parent.exists")
                self.date_picker.set_parent_dir(target_parent)
                self.overview_widget.set_parent_dir(target_parent)
                self.fleetwide_search_widget.set_parent_dir(target_parent)
                mark("parent dirs (changed)")
            else:
                mark("target_parent.exists (missing)")
        self.overview_widget.refresh_layout()
        mark("refresh_layout")
        if (marks[-1][1] - marks[0][1]) > 0.1:
            steps = " ".join(
                f"{label}={((t - prev) * 1000):.0f}ms"
                for (label, t), (_, prev) in zip(marks[1:], marks[:-1])
            )
            print(f"[settings-reload] {steps}", flush=True)

    def _sync_settings_from_fleetwide_search(self):
        # Keep the viewer's embedded settings panels on the same settings
        # object so a later autosave cannot overwrite fleetwide searches.
        self.viewer.settings = self.settings
        self.viewer.settings_panel.settings = self.settings
        self.viewer.system_layout_panel.settings = self.settings

    def _should_show_overview(self) -> bool:
        return self.overview_btn.isChecked()

    def _on_track_toggled(self, checked: bool) -> None:
        """Track only draws with a conveyor calibration. Ticking it without
        one unticks it again and offers the Conveyor dialog."""
        if checked and not self._overlay_controller.has_calibration():
            btn = self.track_toggle
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
            self._track_wanted = True  # tick it once a calibration lands
            answer = QMessageBox.warning(
                self,
                "Conveyor not calibrated",
                "Products cannot be tracked on this system yet: the conveyor has not been "
                "calibrated.\n\n"
                "Do you want to calibrate the conveyor now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self._overlay_controller.open_calibration_dialog()
            return
        self._track_wanted = checked
        self._overlay_controller.set_tracking_enabled(checked)

    def _apply_track_availability(self, ready: bool) -> None:
        """Track follows the calibration: without one it is unticked (so
        nothing is drawn) and its tooltip says why; the user's tick comes
        back once the conveyor is calibrated."""
        btn = self.track_toggle
        if ready:
            btn.setToolTip("Draw the tracked products on the picture")
            if self._track_wanted and not btn.isChecked():
                btn.setChecked(True)
            return
        btn.blockSignals(True)
        btn.setChecked(False)
        btn.blockSignals(False)
        self._overlay_controller.set_tracking_enabled(False)
        btn.setToolTip("Calibrate the conveyor first (the Conveyor button) to track products")

    def _go_to_first_product(self) -> None:
        """Seek the viewer to the moment the first product was seen on the
        loaded clip (the earliest target_added event in its span)."""
        first_dt = self._overlay_controller.first_product_time()
        if first_dt is None:
            QMessageBox.information(
                self,
                "First product",
                "No product has been seen on this clip yet. The clip's product events may still be loading, or none were logged in its span.",
            )
            return
        if not self.viewer.seek_to_wall_time(first_dt, pause=True):
            QMessageBox.information(self, "First product", "The clip's start time is not known, so the moment cannot be found.")

    def _update_viewer_tool_visibility(self):
        """Conveyor/Track/Targets belong to replay mode WITH a clip
        loaded (Chris: only the essential buttons at any point). There is
        no unload event, so once the first clip lands they stay available
        for the session, still following the mode."""
        in_viewer = self.content_stack.currentWidget() is self.viewer
        show = in_viewer and self._viewer_tools_available
        self.conveyor_btn.setVisible(show)
        self.viewer.video_sync_btn.setVisible(show)
        self.first_product_btn.setVisible(show)
        self.viewer.drift_tool.setVisible(show)
        self.track_toggle.setVisible(show)
        self.targets_toggle.setVisible(show)
        # The Customer/Line/System label describes the viewer's selection;
        # in the fleet-wide modes it is wrong, not just redundant (Chris,
        # 2026-09-05: overview showed one system's name).
        self.current_system_label.setVisible(False)
        self.choose_system_btn.setVisible(in_viewer)
        self.choose_date_btn.setVisible(in_viewer)
        # Data, Errors / Stops and Software are fleet views: only on the
        # Overview (Chris, 2026-09-11).
        on_overview = self._should_show_overview()
        for btn in (self.data_btn, self.errors_stops_btn, self.software_btn):
            btn.setVisible(on_overview)
        self._update_chooser_pulse()

    def _on_first_clip_opened(self, _path) -> None:
        if not self._viewer_tools_available:
            self._viewer_tools_available = True
            self._update_viewer_tool_visibility()

    def _on_mode_button_toggled(self, _button, checked: bool):
        # The exclusive group fires once for the unchecked and once for the
        # checked button; syncing on the checked edge runs the switch once.
        if checked:
            self._sync_overview_mode()

    def _sync_overview_mode(self):
        show_overview = self._should_show_overview()
        show_fleetwide_search = self.search_btn.isChecked()
        if show_overview:
            current_page = self.overview_widget
        elif show_fleetwide_search:
            current_page = self.fleetwide_search_widget
        else:
            current_page = self.viewer
        self.content_stack.setCurrentWidget(current_page)
        self._update_viewer_tool_visibility()
        self.overview_widget.set_parent_dir(self.date_picker.parent_dir)
        self.overview_widget.activate(show_overview)
        self.fleetwide_search_widget.set_parent_dir(self.date_picker.parent_dir)
        self.fleetwide_search_widget.activate(show_fleetwide_search)
        if show_overview or show_fleetwide_search:
            self._hover_reveal_enabled = False
            self._cancel_timeline_expand()
            self._timeline_expanded = False
            self.viewer.setMinimumSize(320, 120)
            self.content_stack.setMinimumWidth(320)
            self.date_picker.setVisible(False)
            self.replay_timeline.setVisible(False)
            self._horizontal_splitter.setSizes([0, max(1, sum(self._horizontal_splitter.sizes()) or self.width())])
            self._main_splitter.setSizes([max(1, sum(self._main_splitter.sizes()) or self.height()), 0])
        else:
            self._hover_reveal_enabled = not getattr(self, "_left_panel_retired", False)
            self.viewer.setMinimumSize(980, 120)
            self.content_stack.setMinimumWidth(980)
            if self.date_picker_toggle.isChecked():
                self.date_picker.setVisible(True)
                self._animate_left_panel(self._left_panel_target_width)
            self.replay_timeline.setVisible(True)
            self._apply_initial_timeline_size()

    def _open_system_from_errors(self, folder_name: str, day: date) -> None:
        """A bar in the Errors / Stops window: that system and day in the
        viewer, main window brought to the front (Chris, 2026-09-06)."""
        parent_dir = self.date_picker.parent_dir
        if not isinstance(parent_dir, Path):
            return
        self._open_system_from_overview(parent_dir / folder_name, day)
        self.raise_()
        self.activateWindow()

    def _open_system_from_overview(self, pikpak_root: Path | None, selected_day: date | None, target_dt: datetime | None = None):
        if not isinstance(pikpak_root, Path):
            return
        # PikPak Replay is always one day (Chris, 2026-09-11): when the
        # Overview has a span of days and no moment was clicked, its day
        # says nothing, so the replay keeps its own day (or today).
        shared = getattr(self.day_selection, "range", None)
        multi_day = isinstance(shared, tuple) and shared[0] != shared[1]
        if multi_day and not isinstance(target_dt, datetime):
            selected_day = self.date_picker.active_day or date.today()
        if selected_day is None:
            return
        self.replay_btn.setChecked(True)
        if isinstance(target_dt, datetime):
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=timezone.utc)
            else:
                target_dt = target_dt.astimezone(timezone.utc)
            self._pending_overview_navigation = {
                "root": pikpak_root,
                "day": selected_day,
                "target_dt": target_dt,
                "stage": "load_timeline",
            }
            self._overview_nav_failsafe.start()
        else:
            self._cancel_overview_navigation()
        self.date_picker.select_pikpak_folder_and_day(pikpak_root, selected_day)

    def _on_timeline_event_clicked(self, item: TimelineItem) -> None:
        """A click on an event tick opens the clip covering that moment and
        seeks to it; the logs follow the playhead (Chris, 2026-09-08)."""
        root = self.replay_timeline.current_root
        day = self.replay_timeline._current_date
        if not isinstance(root, Path) or day is None or not isinstance(item.start, datetime):
            return
        self.replay_timeline.set_playhead_datetime(ensure_utc(item.start))
        self._pending_overview_navigation = {
            "root": root,
            "day": day,
            "target_dt": ensure_utc(item.start),
            "stage": "load_timeline",
        }
        self._overview_nav_failsafe.start()
        self._on_items_changed_for_navigation()

    def _on_timeline_moment_clicked(self, moment) -> None:
        """A click on empty chart sets the time (Chris, 2026-09-12): the
        playhead moves at once, and the clip covering that moment opens
        there when there is one."""
        if not isinstance(moment, datetime):
            return
        moment = ensure_utc(moment)
        self.replay_timeline.set_playhead_datetime(moment)
        root = self.replay_timeline.current_root
        day = self.replay_timeline._current_date
        if not isinstance(root, Path) or day is None:
            return
        covering = next(
            (it for it in (getattr(self.replay_timeline, "_items", []) or [])
             if it.kind == "video" and isinstance(it.payload, Path) and isinstance(it.start, datetime) and isinstance(it.end, datetime)
             and ensure_utc(it.start) <= moment < ensure_utc(it.end)),
            None,
        )
        if covering is None:
            return
        self._pending_overview_navigation = {
            "root": root,
            "day": day,
            "target_dt": moment,
            "stage": "load_timeline",
        }
        self._overview_nav_failsafe.start()
        self._on_items_changed_for_navigation()

    def _cancel_overview_navigation(self) -> None:
        self._pending_overview_navigation = None
        self._overview_nav_failsafe.stop()

    def _on_items_changed_for_navigation(self) -> None:
        """Stage 1: once the target day's clips are on the timeline, open the
        clip containing the target moment. Signal-driven replacement for the
        old 150 ms polling state machine."""
        pending = self._pending_overview_navigation
        if not pending or pending.get("stage") != "load_timeline":
            return
        if self.replay_timeline.current_root != pending["root"]:
            return
        if self.replay_timeline._current_date != pending["day"]:
            return
        target_dt = pending["target_dt"]
        items = list(self.replay_timeline._items or [])
        video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
        if not video_items:
            return  # video partial not in yet; a later items_changed will bring it
        clip_item = None
        previous_item = None
        for itm in video_items:
            if itm.start <= target_dt < itm.end:
                clip_item = itm
                break
            if itm.start <= target_dt:
                previous_item = itm
            elif target_dt < itm.start:
                clip_item = previous_item or itm
                break
        if clip_item is None:
            clip_item = previous_item or video_items[0]
        pending["clip_item"] = clip_item
        pending["stage"] = "await_clip"
        self.open_in_viewer(clip_item)
        # A cached clip opens synchronously, in which case clip_opened has
        # already fired inside open_in_viewer and cleared the pending state.

    def _on_clip_opened_for_navigation(self, opened_path: Path) -> None:
        """Stage 2: the clip is open (possibly after an async download) —
        force OCR sync and seek to the target moment."""
        pending = self._pending_overview_navigation
        if not pending or pending.get("stage") != "await_clip":
            return
        clip_item = pending.get("clip_item")
        if not isinstance(clip_item, TimelineItem):
            self._cancel_overview_navigation()
            return
        if Path(opened_path) != Path(clip_item.payload):
            # The user opened something else; abandon the navigation.
            self._cancel_overview_navigation()
            return
        target_dt = pending["target_dt"]
        clip_start_dt = ensure_utc(clip_item.start)
        clip_end_dt = ensure_utc(clip_item.end)
        # Through the OCR-corrected clock when the clip has one, so the
        # green clock then reads the clicked moment (Chris, 2026-09-12);
        # by the filename time otherwise.
        seek_seconds = self.viewer.video_seconds_for_wall_time(target_dt)
        if seek_seconds is None:
            seek_seconds = (target_dt - clip_start_dt).total_seconds()
        clip_duration_seconds = max(0.0, (clip_end_dt - clip_start_dt).total_seconds())
        if clip_duration_seconds > 0.0:
            seek_seconds = min(seek_seconds, clip_duration_seconds)
        seek_seconds = max(0.0, seek_seconds)
        self._cancel_overview_navigation()
        # No forced OCR here (Chris, 2026-09-10): the clip load has already
        # applied any cached OCR offset; a timeline click must not start a
        # copy-and-Tesseract run of its own.
        self.viewer.seek_to_seconds(seek_seconds, pause=True)


if __name__ == "__main__":
    from logfather.ui.app_main import main

    main()
