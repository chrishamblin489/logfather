"""The Data window: how much data the fleet has, per system per day.

Opened from the top bar's Data button (Chris, 2026-09-05). Two workers
run in parallel - Elastic aggregations and the CCTV share scan - and each
fills its half of the window as it lands. A metric toggle switches the
stacked per-day bar chart between Elastic document counts / storage and
CCTV clip counts / storage; hovering a bar segment shows every metric
for that system on that day.
"""
from __future__ import annotations

import os
import webbrowser
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
)

from logfather.data.data_inventory import (
    CctvInventory,
    ElasticInventory,
    INVENTORY_DAYS,
    fetch_elastic_inventory,
    clamp_range,
    format_bytes,
    format_count,
    generation_rank,
    cache_saved_at,
    inventory_days,
    inventory_from_cache,
    load_inventory_cache,
    kibana_discover_url,
    scan_cctv_inventory,
)
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.data.settings_store import display_customer_name, system_group_sort_key
from logfather.data.ui_state_store import load_ui_state, update_ui_state
from logfather.ui import theme
from logfather.ui.gear_menu import build_gear_button
from logfather.ui.chart_scroll import ChartScroller
from logfather.ui.charts import StackedBarChart
from logfather.ui.elastic_catalog_dialog import ElasticCatalogDialog
from logfather.ui.grafana_catalog_dialog import GrafanaCatalogDialog
from logfather.data import grafana_client
from logfather.data.grafana_inventory import GrafanaInventory, RETENTION_DAYS, fetch_grafana_inventory
from logfather.ui.icons import calendar_icon, question_block_icon
from logfather.ui.day_range_dialog import DayRangeDialog
from logfather.ui.day_selection import DaySelection
from logfather.ui.pulse import Pulser
from logfather.ui.qt_worker import JobSlot
from logfather.ui.system_filter import SystemFilterPopup, funnel_icon

_HIDDEN_SYSTEMS_KEY = "data_hidden_systems"
_LABELS_KEY = "data_chart_labels"
# The chart opens on the last 14 days; scrolling past the left edge loads
# seven more, up to this many (Chris, 2026-09-07).
MAX_INVENTORY_DAYS = 90
EXTEND_DAYS = 7

_METRICS = (
    ("elastic", "Elastic documents"),
    ("elastic_bytes", "Elastic size"),
    ("grafana", "Grafana samples"),
    ("grafana_bytes", "Grafana size"),
    ("clips", "CCTV clips"),
    ("bytes", "CCTV size"),
)

def _series_colour(index: int) -> QColor:
    """Distinct pastel colours: golden-angle hue steps at low saturation
    (Chris, 2026-09-05: the saturated set read as neon on the dark
    ground). Alternating lightness keeps neighbours apart."""
    hue = (index * 137.508) % 360.0
    colour = QColor()
    colour.setHsvF(hue / 360.0, 0.32, 0.86 if index % 2 == 0 else 0.74)
    return colour


class DataInventoryDialog(QDialog):
    def __init__(
        self,
        settings_provider: Callable,
        parent_dir_provider: Callable[[], Path | None],
        parent=None,
        gear_host=None,
        day_selection: DaySelection | None = None,
    ):
        super().__init__(parent)
        self._gear_host = gear_host
        self._day_selection = day_selection
        self.setWindowTitle("Data — fleet inventory")
        # A real resizable window with minimise/maximise, not a fixed
        # dialog (Chris, 2026-09-05); the chart stretches to fill it.
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint
        )
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 480)
        self.resize(1180, 720)
        self._settings_provider = settings_provider
        self._parent_dir_provider = parent_dir_provider
        self._elastic: ElasticInventory | None = None
        self._cctv: CctvInventory | None = None
        self._grafana: GrafanaInventory | None = None
        self._elastic_slot = JobSlot(self)
        self._cctv_slot = JobSlot(self)
        self._grafana_slot = JobSlot(self)
        self._metric = "elastic"
        self._started = False
        # robot id -> system folder, once the CCTV scan names the folders.
        self._robot_to_system: dict[str, str] = {}
        # Systems un-ticked in the filter; per-user, remembered.
        stored = load_ui_state().get(_HIDDEN_SYSTEMS_KEY)
        self._hidden_systems: set[str] = (
            {str(s) for s in stored if str(s).strip()} if isinstance(stored, list) else set()
        )
        self._filter_popup: SystemFilterPopup | None = None
        # How many trailing days are loaded; grows when the user scrolls
        # past the oldest day. Pending days are drawn hatched meanwhile.
        self._days_span = INVENTORY_DAYS
        # Chosen end day (Chris, 2026-09-07: choose a date); None = today.
        self._end_day: date | None = None
        if day_selection is not None and day_selection.range is not None:
            self._adopt_range(day_selection.range)
        self._pending: set[date] = set()
        self._extending = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        intro = QLabel(
            "Here is an overview of the data stored by PikPak systems. "
            "Elastic logs are stored continuously. "
            "Grafana telemetry is kept for 13 months. "
            "CCTV footage is stored for the last 30 days only (currently)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        layout.addWidget(intro)

        # Two headline tiles (Chris, 2026-09-05): Elastic total since its
        # oldest record, CCTV total currently on the share.
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        # A big ? in the Elastic tile's corner opens the catalogue of what
        # Elastic stores (Chris, 2026-09-06).
        self._help_btn = self._make_help_button("What kinds of data are stored in Elastic", self._open_catalog)
        self._catalog_dialog: ElasticCatalogDialog | None = None
        # Grafana between Elastic and CCTV (Chris, 2026-09-07), with its
        # own ? opening the metric catalogue.
        self._grafana_help_btn = self._make_help_button("What is stored in Grafana", self._open_grafana_catalog)
        self._grafana_catalog_dialog: GrafanaCatalogDialog | None = None
        self._elastic_tile, self._elastic_tile_value, self._elastic_tile_sub, self._elastic_tile_foot = self._make_tile("Elastic total")
        self._grafana_tile, self._grafana_tile_value, self._grafana_tile_sub, self._grafana_tile_foot = self._make_tile("Grafana total")
        self._cctv_tile, self._cctv_tile_value, self._cctv_tile_sub, self._cctv_tile_foot = self._make_tile("CCTV total")
        # The ? floats in the tile's corner, outside the layout, so all
        # tiles keep identical spacing (Chris, 2026-09-07).
        for btn, tile in ((self._help_btn, self._elastic_tile), (self._grafana_help_btn, self._grafana_tile)):
            btn.setParent(tile)
            btn.raise_()
            tile.installEventFilter(self)
        tiles.addWidget(self._elastic_tile, 1)
        tiles.addWidget(self._grafana_tile, 1)
        tiles.addWidget(self._cctv_tile, 1)
        layout.addLayout(tiles)

        controls = QHBoxLayout()
        self._filter_btn = QToolButton()
        self._filter_btn.setText("PikPaks")
        self._filter_btn.setIcon(funnel_icon())
        self._filter_btn.setIconSize(QSize(18, 18))
        self._filter_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._filter_btn.setToolTip("Choose which PikPaks to show")
        # Never elide the "(N hidden)" suffix (Chris, 2026-09-07).
        self._filter_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._filter_btn.clicked.connect(self._open_filter_popup)
        if self._hidden_systems:
            self._filter_btn.setText(f"PikPaks ({len(self._hidden_systems)} hidden)")
        controls.addWidget(self._filter_btn)
        controls.addSpacing(12)
        # Live (the last 14 days ending today) or a chosen day / span
        # (Chris, 2026-09-07); the choice is shared with the Overview and
        # Errors / Stops windows.
        # They sit in a "Days" box on the second row so the date is never
        # squeezed (Chris, 2026-09-07: the row cut part of it off).
        self._live_btn = QPushButton(f"Last {INVENTORY_DAYS} days")
        self._live_btn.setCheckable(True)
        self._live_btn.setToolTip(f"The last {INVENTORY_DAYS} days ending today")
        self._live_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._live_btn.clicked.connect(self._on_live_clicked)
        self._pick_days_btn = QPushButton("Choose days…")
        self._pick_days_btn.setIcon(calendar_icon())
        self._pick_days_btn.setIconSize(QSize(18, 18))
        self._pick_days_btn.setCheckable(True)
        self._pick_days_btn.setToolTip("Choose a day or a span of days")
        self._pick_days_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._pick_days_btn.setMinimumWidth(self._pick_days_btn.fontMetrics().horizontalAdvance("00/00 – 00/00/0000") + 56)
        self._pick_days_btn.clicked.connect(self._on_pick_days_clicked)
        self._refresh_day_labels()
        controls.addWidget(QLabel("Show:"))
        self._metric_group = QButtonGroup(self)
        self._metric_group.setExclusive(True)
        self._metric_buttons: dict[str, QPushButton] = {}
        for key, label in _METRICS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, k=key: self._set_metric(k))
            self._metric_group.addButton(btn)
            self._metric_buttons[key] = btn
            controls.addWidget(btn)
        self._metric_buttons["elastic"].setChecked(True)
        controls.addStretch(1)
        if self._gear_host is not None:
            controls.addWidget(build_gear_button(self, self._gear_host))
        # Normalise (Chris, 2026-09-07): the data volume follows the picks
        # and the hours run, so per pick / per hour show what differs. On
        # their own row in a "Y axis" box (Chris, 2026-09-07).
        y_axis_box = QGroupBox("Y axis")
        y_axis_box.setStyleSheet(
            f"QGroupBox {{ font-weight: normal; margin-top: 12px; padding: 8px 8px 6px 8px;"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {theme.TEXT_MUTED}; }}"
        )
        y_axis_layout = QHBoxLayout(y_axis_box)
        y_axis_layout.setContentsMargins(6, 4, 6, 4)
        y_axis_layout.setSpacing(6)
        self._norm_group = QButtonGroup(self)
        self._norm_group.setExclusive(True)
        self._norm_buttons: dict[str, QPushButton] = {}
        for key, label, tip in (
            ("total", "Total", "Each day's whole figure"),
            ("pick", "Per pick", "Divided by that system's pick movements that day"),
            ("hour", "Per hour on", "Divided by the hours that system was switched on that day, idle time included (five-minute slots with any log)"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _checked=False: self._rebuild_views())
            self._norm_group.addButton(btn)
            self._norm_buttons[key] = btn
            y_axis_layout.addWidget(btn)
        self._norm_buttons["total"].setChecked(True)
        days_box = QGroupBox("Days")
        days_box.setStyleSheet(y_axis_box.styleSheet())
        days_layout = QHBoxLayout(days_box)
        days_layout.setContentsMargins(6, 4, 6, 4)
        days_layout.setSpacing(6)
        days_layout.addWidget(self._live_btn)
        days_layout.addWidget(self._pick_days_btn)
        y_axis_row = QHBoxLayout()
        y_axis_row.addWidget(days_box)
        y_axis_row.addSpacing(12)
        y_axis_row.addWidget(y_axis_box)
        y_axis_row.addStretch(1)
        # Second row (Chris, 2026-09-07: one row squeezed the buttons until
        # their text was cut off): hint, status, Last updated, Refresh, Zoom.
        controls2 = QHBoxLayout()
        controls2.setContentsMargins(0, 0, 0, 0)
        self._days_label = QLabel("hover a bar for details · click a bar to open it")
        self._days_label.setStyleSheet(theme.MUTED_LABEL)
        controls2.addWidget(self._days_label)
        controls2.addStretch(1)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(theme.MUTED_LABEL)
        controls2.addWidget(self._status_label)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(140)
        self._progress.setFixedHeight(8)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)
        self._progress.hide()
        controls2.addWidget(self._progress)
        # Last-updated stamp beside Refresh; the button pulses when the
        # figures are not from today (Chris, 2026-09-05).
        self._updated_label = QLabel("")
        self._updated_label.setStyleSheet(theme.MUTED_LABEL)
        controls2.addWidget(self._updated_label)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.start)
        self._pulser = Pulser(self)
        controls2.addWidget(self._refresh_btn)
        # The 14-day section - filter, metric toggle, key and chart - sits
        # in one framed box (Chris, 2026-09-05).
        summary_box = QGroupBox(f"{INVENTORY_DAYS} day summary")
        self._summary_box = summary_box
        self._refresh_day_labels()  # the title follows a range adopted before the box existed
        summary_box.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; margin-top: 16px; padding: 10px 8px 8px 8px;"
            f" border: 1px solid {theme.BORDER_LIGHT}; border-radius: 6px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px;"
            f" color: {theme.TEXT_BRIGHT}; }}"
        )
        box_layout = QVBoxLayout(summary_box)
        box_layout.setSpacing(8)
        box_layout.addLayout(controls)
        box_layout.addLayout(y_axis_row)
        box_layout.addLayout(controls2)

        self._legend = QLabel("")
        self._legend.setWordWrap(True)
        box_layout.addWidget(self._legend)

        if self._day_selection is not None:
            self._day_selection.changed.connect(self._on_shared_day_range)
        self._chart = StackedBarChart()
        self._chart.set_detail_provider(self._detail_for)
        self._chart.set_click_handler(None)
        # Right-click a bar to add / remove a label naming its system
        # (Chris, 2026-09-07); labels are remembered per user.
        self._chart.set_labels_enabled(True)
        stored_labels = load_ui_state().get(_LABELS_KEY)
        labels = {}
        for entry in stored_labels if isinstance(stored_labels, list) else []:
            try:
                dx = float(entry[2]) if len(entry) > 2 else 0.0
                dy = float(entry[3]) if len(entry) > 3 else 0.0
                labels[(str(entry[0]), date.fromisoformat(str(entry[1])))] = (dx, dy)
            except (TypeError, ValueError, IndexError):
                continue
        self._chart.set_labels(labels)
        # Saved with the drag offset, so a moved label stays where it was put.
        self._chart.labels_changed.connect(
            lambda labels: update_ui_state({_LABELS_KEY: sorted(
                [n, d.isoformat(), round(off[0], 1), round(off[1], 1)] for (n, d), off in labels.items()
            )})
        )
        # Sideways scrolling with arrows and zoom, as on Errors / Stops
        # (Chris, 2026-09-07); the left edge loads older days.
        self._scroller = ChartScroller([self._chart], self._rebuild_views, self)
        self._scroller.edge_reached.connect(self._on_edge)
        chart_row = QHBoxLayout()
        chart_row.setSpacing(4)
        chart_row.addWidget(self._scroller.arrow_button("left", "Back in time (loads earlier days at the start)"))
        chart_row.addWidget(self._chart, 1)
        chart_row.addWidget(self._scroller.arrow_button("right", "Forward in time"))
        box_layout.addLayout(chart_row, 1)
        box_layout.addWidget(self._scroller.scrollbar)
        layout.addWidget(summary_box, 1)
        zoom_label = QLabel("Zoom")
        zoom_label.setStyleSheet(theme.MUTED_LABEL)
        self._zoom_out_btn = self._scroller.zoom_button("minus", "Fewer pixels per day: more days on screen", -1)
        self._zoom_in_btn = self._scroller.zoom_button("plus", "More pixels per day: fewer days on screen", +1)
        controls2.addSpacing(12)
        controls2.addWidget(zoom_label)
        controls2.addWidget(self._zoom_out_btn)
        controls2.addWidget(self._zoom_in_btn)
        # No button in either row may shrink below its text.
        for row in (controls, controls2):
            for i in range(row.count()):
                w = row.itemAt(i).widget()
                if isinstance(w, (QPushButton, QToolButton)):
                    w.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _on_edge(self, direction: str) -> None:
        """Past the oldest day: load seven more (the newest day is today,
        so the right edge has nothing to add)."""
        if direction != "older" or self._extending:
            return
        if self._days_span >= MAX_INVENTORY_DAYS:
            self._status_label.setText(f"Showing the maximum of {MAX_INVENTORY_DAYS} days")
            return
        new_span = min(MAX_INVENTORY_DAYS, self._days_span + EXTEND_DAYS)
        old_days = set(self._day_list())
        self._days_span = new_span
        self._pending = set(self._day_list()) - old_days
        self._extending = True
        self._refresh_day_labels()
        self._rebuild_views()
        self._scroller.shift_prepended(len(self._pending))
        self.start(keep_view=True)

    # ---- day choice --------------------------------------------------------

    def _today(self) -> date:
        return datetime.now().date()

    def _day_list(self) -> list[date]:
        return inventory_days(self._end_day or self._today(), self._days_span)

    def _current_range(self) -> tuple[date, date]:
        days = self._day_list()
        return days[0], days[-1]

    def _adopt_range(self, day_range: tuple[date, date] | None) -> None:
        """Set the window's days from a (start, end) or back to live."""
        if day_range is None:
            self._end_day = None
            self._days_span = INVENTORY_DAYS
            return
        start, end = clamp_range(day_range[0], day_range[1], self._today(), MAX_INVENTORY_DAYS)
        self._end_day = None if end >= self._today() else end
        self._days_span = (end - start).days + 1

    def _refresh_day_labels(self) -> None:
        live = self._end_day is None and self._days_span == INVENTORY_DAYS
        self._live_btn.setChecked(live)
        self._pick_days_btn.setChecked(not live)
        start, end = self._current_range()
        if live:
            self._pick_days_btn.setText("Choose days…")
            title = f"{INVENTORY_DAYS} day summary"
        elif start == end:
            self._pick_days_btn.setText(start.strftime("%d/%m/%Y"))
            title = f"{start:%a %d %b %Y}"
        else:
            self._pick_days_btn.setText(f"{start:%d/%m} – {end:%d/%m/%Y}")
            title = f"{start:%d %b} – {end:%d %b %Y} ({self._days_span} days)"
        if hasattr(self, "_summary_box"):
            self._summary_box.setTitle(title)

    def _reload_for_days(self) -> None:
        self._elastic = None
        self._cctv = None
        self._grafana = None
        self._pending = set()
        self._extending = False
        self._refresh_day_labels()
        self._rebuild_views()
        self.start()

    def _on_live_clicked(self) -> None:
        was_live = self._end_day is None and self._days_span == INVENTORY_DAYS
        self._adopt_range(None)
        self._refresh_day_labels()
        if not was_live:
            self._reload_for_days()
        self._broadcast(None)

    def _on_pick_days_clicked(self) -> None:
        dialog = DayRangeDialog(self._current_range(), self)
        if dialog.exec() != QDialog.Accepted:
            self._refresh_day_labels()
            return
        start, end = clamp_range(*dialog.selected_range(), self._today(), MAX_INVENTORY_DAYS)
        self._adopt_range((start, end))
        self._reload_for_days()
        self._broadcast((start, end))

    def _broadcast(self, day_range: tuple[date, date] | None) -> None:
        if self._day_selection is not None:
            self._day_selection.set(day_range, self)

    def _on_shared_day_range(self, day_range, source) -> None:
        if source is self:
            return
        before = (self._end_day, self._days_span)
        self._adopt_range(day_range)
        if (self._end_day, self._days_span) != before:
            if self._started:
                self._reload_for_days()
            else:
                self._refresh_day_labels()

    def _make_help_button(self, tip: str, handler) -> QToolButton:
        btn = QToolButton()
        btn.setIcon(question_block_icon(32))  # same size as the Sync CCTV Time window (Chris, 2026-09-12)
        btn.setIconSize(QSize(32, 32))
        btn.setToolTip(tip)
        btn.setFixedSize(36, 36)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 0; }"
            f"QToolButton:hover {{ background: {theme.BG_HOVER}; border-radius: 6px; }}"
        )
        btn.clicked.connect(handler)
        return btn

    @staticmethod
    def _make_tile(title: str):
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_RAISED}; border: 1px solid {theme.BORDER};"
            " border-radius: 6px; }"
            "QLabel { border: none; background: transparent; }"
        )
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 12, 18, 12)
        box.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-weight: bold;")
        value_label = QLabel("—")
        value_font = QFont()
        value_font.setPointSizeF(value_font.pointSizeF() * 2.2)
        value_font.setBold(True)
        value_label.setFont(value_font)
        value_label.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        sub_label = QLabel("loading...")
        sub_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        # One short line about the last 14 days at the foot of the tile
        # (Chris, 2026-09-07), in place of the long summaries below.
        foot_label = QLabel("")
        foot_label.setStyleSheet(f"color: {theme.TEXT};")
        box.addWidget(title_label)
        box.addWidget(value_label)
        box.addWidget(sub_label)
        box.addSpacing(6)
        box.addWidget(foot_label)
        return frame, value_label, sub_label, foot_label

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Resize, QEvent.Show):
            if obj is self._elastic_tile:
                self._help_btn.move(self._elastic_tile.width() - self._help_btn.width() - 12, 10)
            elif obj is self._grafana_tile:
                self._grafana_help_btn.move(self._grafana_tile.width() - self._grafana_help_btn.width() - 12, 10)
        return super().eventFilter(obj, event)

    def _open_grafana_catalog(self) -> None:
        if self._grafana_catalog_dialog is None:
            self._grafana_catalog_dialog = GrafanaCatalogDialog(self._settings_provider, parent=self)
        self._grafana_catalog_dialog.show()
        self._grafana_catalog_dialog.raise_()
        self._grafana_catalog_dialog.activateWindow()
        self._grafana_catalog_dialog.start_if_needed()

    def _open_catalog(self) -> None:
        if self._catalog_dialog is None:
            self._catalog_dialog = ElasticCatalogDialog(self._settings_provider, parent=self)
        self._catalog_dialog.show()
        self._catalog_dialog.raise_()
        self._catalog_dialog.activateWindow()
        self._catalog_dialog.start_if_needed()

    # ---- lifecycle --------------------------------------------------------

    def start_if_needed(self) -> None:
        if self._started:
            return
        # Open on the saved figures when there are any; Refresh (pulsing
        # if they are not from today) fetches the days since.
        cache = load_inventory_cache()
        cached = inventory_from_cache(cache, self._day_list())
        if cached is None:
            self.start()
            return
        self._started = True
        self._on_elastic_result(cached)
        self._set_last_updated(cache_saved_at(cache))
        self._progress.show()
        self._start_cctv_scan()
        self._start_grafana()
        self._on_any_finished()

    def start(self, keep_view: bool = False) -> None:
        self._started = True
        if not keep_view:
            self._scroller.fit_next()
        settings = self._settings_provider()
        span = self._days_span
        end_day = self._end_day
        self._elastic_tile_foot.setText("Loading...")
        self._progress.show()
        self._status_label.setText("Querying Elastic and the CCTV share...")
        self._elastic_slot.start(
            lambda job: fetch_elastic_inventory(settings, span, progress=job.emit_progress, end_day=end_day),
            on_result=self._on_elastic_fetched,
            on_error=self._on_elastic_error,
            on_progress=self._on_progress,
            on_finished=self._on_any_finished,
        )
        self._start_cctv_scan()
        self._start_grafana()

    def _start_grafana(self) -> None:
        settings = self._settings_provider()
        if not grafana_client.is_configured(settings):
            self._grafana_tile_value.setText("—")
            self._grafana_tile_sub.setText("not set up")
            self._grafana_tile_foot.setText("Gear menu, Data sources: add a Grafana token")
            return
        self._grafana_tile_foot.setText("Querying Grafana...")
        span = self._days_span
        end_day = self._end_day
        self._grafana_slot.start(
            lambda job: fetch_grafana_inventory(settings, span, progress=job.emit_progress, end_day=end_day),
            on_result=self._on_grafana_result,
            on_error=lambda m: self._grafana_tile_foot.setText(f"Failed: {m}"),
            on_progress=self._on_progress,
            on_finished=self._on_any_finished,
        )

    def _on_grafana_result(self, inventory: GrafanaInventory) -> None:
        self._grafana = inventory
        recent = set(inventory.days[-INVENTORY_DAYS:])
        recent_samples = sum(n for per_day in inventory.samples.values() for day, n in per_day.items() if day in recent)
        self._grafana_tile_foot.setText(f"Last {INVENTORY_DAYS} days: {format_count(recent_samples)} samples ≈ {format_bytes(inventory.bytes_for(recent_samples))}")
        if inventory.retained_bytes is not None:
            self._grafana_tile_value.setText(format_bytes(inventory.retained_bytes))
            since = f"{inventory.retained_since:%d %b %Y}" if inventory.retained_since else f"{RETENTION_DAYS // 30} months"
            bits = [f"since {since}", f"{format_count(inventory.retained_samples)} samples"]
            if inventory.active_series:
                bits.append(f"{inventory.active_series:,} active series")
            if inventory.metric_count:
                bits.append(f"{inventory.metric_count} metrics")
            bits.append("estimated")
            self._grafana_tile_sub.setText(" · ".join(bits))
        else:
            self._grafana_tile_value.setText("—")
            self._grafana_tile_sub.setText("stack usage unavailable")
        self._rebuild_views()

    def _on_elastic_fetched(self, inventory: ElasticInventory) -> None:
        self._on_elastic_result(inventory)
        self._set_last_updated(datetime.now().astimezone())

    def _set_last_updated(self, when: datetime | None) -> None:
        if when is None:
            self._updated_label.setText("Last updated: never")
            self._set_pulse(True)
            return
        today = datetime.now().date()
        stamp = f"today {when:%H:%M}" if when.date() == today else f"{when:%d/%m/%Y %H:%M}"
        self._updated_label.setText(f"Last updated: {stamp}")
        self._set_pulse(when.date() != today)

    def _set_pulse(self, on: bool) -> None:
        self._pulser.set_target(self._refresh_btn if on else None)

    def _start_cctv_scan(self) -> None:
        parent_dir = self._parent_dir_provider()
        if parent_dir is None:
            self._cctv_tile_foot.setText("No CCTV parent folder configured")
        else:
            self._cctv_tile_foot.setText("Scanning the share...")
            span = self._days_span
            end_day = self._end_day
            self._cctv_slot.start(
                lambda job: scan_cctv_inventory(
                    parent_dir,
                    span,
                    progress=job.emit_progress,
                    interrupted=job.interrupted,
                    end_day=end_day,
                ),
                on_result=self._on_cctv_result,
                on_error=self._on_cctv_error,
                on_progress=self._on_progress,
                on_finished=self._on_any_finished,
            )

    def shutdown(self) -> None:
        self._elastic_slot.shutdown()
        self._cctv_slot.shutdown()
        self._grafana_slot.shutdown()
        if self._catalog_dialog is not None:
            self._catalog_dialog.shutdown()
        if self._grafana_catalog_dialog is not None:
            self._grafana_catalog_dialog.shutdown()

    def closeEvent(self, event):
        # Hide rather than destroy: reopening shows the last results.
        event.ignore()
        self.hide()

    # ---- worker callbacks -------------------------------------------------

    def _on_progress(self, message) -> None:
        self._status_label.setText(str(message or ""))

    def _on_any_finished(self) -> None:
        if not self._elastic_slot.is_running() and not self._cctv_slot.is_running() and not self._grafana_slot.is_running():
            self._progress.hide()
            self._status_label.setText("Done")

    def _on_elastic_error(self, message: str) -> None:
        self._elastic_tile_foot.setText(f"Failed: {message}")

    def _on_cctv_error(self, message: str) -> None:
        self._cctv_tile_foot.setText(f"Failed: {message}")

    def _on_elastic_result(self, inventory: ElasticInventory) -> None:
        self._elastic = inventory
        self._pending = set()
        self._extending = False
        recent = set(inventory.days[-INVENTORY_DAYS:])
        recent_docs = sum(n for per_day in inventory.counts.values() for day, n in per_day.items() if day in recent)
        recent_bytes = sum(
            n * inventory.bytes_factor(robot)
            for robot, per_day in inventory.counts.items() for day, n in per_day.items() if day in recent
        ) if inventory.bytes_per_doc else 0.0
        foot = f"Last {INVENTORY_DAYS} days: {format_count(recent_docs)} documents"
        if recent_bytes:
            foot += f" ≈ {format_bytes(recent_bytes)}"
        self._elastic_tile_foot.setText(foot)
        if inventory.total_bytes:
            self._elastic_tile_value.setText(format_bytes(inventory.total_bytes))
            # First and last day with data (Chris, 2026-09-06), the last
            # being the newest day in the window with any documents.
            newest = max(
                (day for per_day in inventory.counts.values() for day, n in per_day.items() if n > 0),
                default=None,
            )
            if inventory.oldest_ts is not None:
                since = f"{inventory.oldest_ts.astimezone():%d %b %Y}"
                if newest is not None:
                    since += f" – {newest:%d %b %Y}"
            else:
                since = "all records"
            est = "" if inventory.bytes_basis.startswith("index store") else " · estimated"
            self._elastic_tile_sub.setText(f"{since} · {format_count(inventory.total_docs or 0)} documents{est}")
        else:
            self._elastic_tile_value.setText("—")
            self._elastic_tile_sub.setText("size unavailable")
        self._rebuild_views()

    def _on_cctv_result(self, inventory: CctvInventory) -> None:
        self._cctv = inventory
        self._robot_to_system = {
            robot: system for system, robot in inventory.robot_ids.items() if robot
        }
        total_clips = sum(sum(v.values()) for v in inventory.clips.values())
        total_bytes = sum(sum(v.values()) for v in inventory.est_bytes.values())
        folder_counts = sorted(c for c in inventory.day_folders.values() if c)
        recent = set(inventory.days[-INVENTORY_DAYS:])
        recent_clips = sum(n for per_day in inventory.clips.values() for day, n in per_day.items() if day in recent)
        recent_bytes = sum(n for per_day in inventory.est_bytes.values() for day, n in per_day.items() if day in recent)
        self._cctv_tile_foot.setText(f"Last {INVENTORY_DAYS} days: {recent_clips:,} clips ≈ {format_bytes(recent_bytes)}")
        # Share total: each system's average bytes per day-with-clips in
        # the window, times the day folders it actually has on the share
        # (the retained ~30 days), summed across systems.
        share_total = 0.0
        for system, per_day in inventory.est_bytes.items():
            days_with_clips = sum(1 for v in inventory.clips.get(system, {}).values() if v)
            if not days_with_clips:
                continue
            per_day_avg = sum(per_day.values()) / days_with_clips
            share_total += per_day_avg * max(days_with_clips, inventory.day_folders.get(system, 0))
        folder_note = f"last {folder_counts[len(folder_counts) // 2]} days" if folder_counts else "current retention"
        self._cctv_tile_value.setText(format_bytes(share_total))
        self._cctv_tile_sub.setText(f"on the share ({folder_note}) · {len(inventory.clips)} systems · estimated")
        self._rebuild_views()

    # ---- system filter ----------------------------------------------------

    def _known_systems(self) -> list[str]:
        names: set[str] = set()
        if self._cctv is not None:
            names.update(self._cctv.clips.keys())
        if self._elastic is not None:
            names.update(self._robot_to_system.get(r, r) for r in self._elastic.counts)
        settings = self._settings_provider()
        return sorted(names, key=lambda n: system_group_sort_key(settings, n))

    def _system_groups(self) -> list[tuple[str, list[str]]]:
        settings = self._settings_provider()
        groups: list[tuple[str, list[str]]] = []
        for name in self._known_systems():
            customer = str(display_customer_name(settings, name) or "")
            if groups and groups[-1][0] == customer:
                groups[-1][1].append(name)
            else:
                groups.append((customer, [name]))
        return groups

    def _open_filter_popup(self) -> None:
        popup = SystemFilterPopup(
            self._system_groups(),
            self._hidden_systems,
            on_change=self._on_system_toggled,
            on_all=self._on_all_systems,
            parent=self,
        )
        self._filter_popup = popup
        anchor = self._filter_btn.mapToGlobal(QPoint(0, self._filter_btn.height()))
        popup.move(anchor)
        popup.show()

    def _on_system_toggled(self, name: str, visible: bool) -> None:
        if visible:
            self._hidden_systems.discard(name)
        else:
            self._hidden_systems.add(name)
        self._persist_hidden()
        self._rebuild_views()

    def _on_all_systems(self, visible: bool) -> None:
        if visible:
            self._hidden_systems.clear()
        else:
            self._hidden_systems.update(self._known_systems())
        self._persist_hidden()
        self._rebuild_views()

    def _persist_hidden(self) -> None:
        update_ui_state({_HIDDEN_SYSTEMS_KEY: sorted(self._hidden_systems)})
        count = len(self._hidden_systems)
        self._filter_btn.setText("PikPaks" if not count else f"PikPaks ({count} hidden)")

    # ---- views ------------------------------------------------------------

    def _set_metric(self, key: str) -> None:
        if key == self._metric:
            return
        self._metric = key
        self._rebuild_views()

    def _elastic_rows(self, as_bytes: bool) -> tuple[list[date], dict[str, dict[date, float]]]:
        rows: dict[str, dict[date, float]] = {}
        inv = self._elastic
        if inv is None or (as_bytes and not inv.bytes_per_doc):
            return self._day_list(), rows
        for robot, per_day in inv.counts.items():
            factor = inv.bytes_factor(robot) if as_bytes else 1.0
            name = self._robot_to_system.get(robot, robot)
            target = rows.setdefault(name, {})
            for day, count in per_day.items():
                target[day] = target.get(day, 0.0) + float(count) * factor
        return list(inv.days), rows

    def _row_names_and_values(self) -> tuple[list[date], list[tuple[str, dict[date, float]]]]:
        """Rows keyed by system folder name (robot ids from Elastic are
        joined onto their folder; unknown robots keep their id)."""
        if self._metric in ("elastic", "elastic_bytes"):
            days, rows = self._elastic_rows(as_bytes=self._metric == "elastic_bytes")
        elif self._metric in ("grafana", "grafana_bytes"):
            days = self._day_list()
            rows = {}
            if self._grafana is not None:
                days = list(self._grafana.days)
                factor = self._grafana.bytes_for(1.0) if self._metric == "grafana_bytes" else 1.0
                for robot, per_day in self._grafana.samples.items():
                    name = self._robot_to_system.get(robot, robot)
                    target = rows.setdefault(name, {})
                    for day, n in per_day.items():
                        target[day] = target.get(day, 0.0) + n * factor
        else:
            days = self._day_list()
            rows = {}
            if self._cctv is not None:
                days = list(self._cctv.days)
                source = self._cctv.clips if self._metric == "clips" else self._cctv.est_bytes
                for system, per_day in source.items():
                    rows[system] = {day: float(v) for day, v in per_day.items()}
        settings = self._settings_provider()
        # Argus 1 systems first, then Argus 2, then the usual customer
        # grouping within each (Chris, 2026-09-07).
        ordered = sorted(
            ((name, values) for name, values in rows.items() if name not in self._hidden_systems),
            key=lambda kv: (self._generation_rank(kv[0]), system_group_sort_key(settings, kv[0])),
        )
        return days, ordered

    def _generation_rank(self, name: str) -> int:
        if self._elastic is None:
            return 2
        robot = name if name.startswith("35-2300-") else robot_id_from_folder(name)
        if not robot:
            for r, system in self._robot_to_system.items():
                if system == name:
                    robot = r
        return generation_rank(self._elastic.generation.get(robot or ""))

    def _value_formatter(self) -> Callable[[float], str]:
        suffix = {"pick": "/pick", "hour": "/h"}.get(self._norm_mode(), "")
        if self._metric in ("bytes", "elastic_bytes", "grafana_bytes"):
            return lambda v: format_bytes(v) + suffix
        if self._metric == "clips":
            return (lambda v: f"{v:,.2f}{suffix}") if suffix else (lambda v: f"{int(round(v)):,}")
        return (lambda v: f"{v:,.0f}{suffix}") if suffix else (lambda v: format_count(v))

    def _norm_mode(self) -> str:
        for key, btn in self._norm_buttons.items():
            if btn.isChecked():
                return key
        return "total"

    def _robot_for(self, name: str) -> str | None:
        robot = name if name.startswith("35-2300-") else robot_id_from_folder(name)
        if not robot:
            for r, system in self._robot_to_system.items():
                if system == name:
                    robot = r
        return robot

    def _running_minutes_for(self, name: str, day: date) -> int:
        if self._elastic is None:
            return 0
        return int(self._elastic.running.get(self._robot_for(name) or "", {}).get(day, 0))

    def _picks_for(self, name: str, day: date) -> int:
        """Pick movements for a system (folder or robot id) on a day."""
        if self._elastic is None:
            return 0
        robot = name if name.startswith("35-2300-") else robot_id_from_folder(name)
        if not robot:
            for r, system in self._robot_to_system.items():
                if system == name:
                    robot = r
        return int(self._elastic.picks.get(robot or "", {}).get(day, 0))

    def _normalise(self, rows: list[tuple[str, dict[date, float]]], mode: str) -> list[tuple[str, dict[date, float]]]:
        out = []
        for name, values in rows:
            scaled = {}
            for day, value in values.items():
                if mode == "pick":
                    divisor = float(self._picks_for(name, day))
                else:
                    divisor = self._running_minutes_for(name, day) / 60.0
                if divisor > 0:
                    scaled[day] = value / divisor
            out.append((name, scaled))
        return out

    def _rebuild_views(self) -> None:
        days, rows = self._row_names_and_values()
        mode = self._norm_mode()
        if mode != "total":
            rows = self._normalise(rows, mode)
        fmt = self._value_formatter()
        series = [
            (name, _series_colour(i), values) for i, (name, values) in enumerate(rows)
        ]
        empty = {
            "elastic": "Elastic data not loaded",
            "elastic_bytes": "Elastic size unavailable",
            "grafana": "Grafana data not loaded",
            "grafana_bytes": "Grafana data not loaded",
            "clips": "CCTV data not loaded",
            "bytes": "CCTV data not loaded",
        }[self._metric]
        # Days still loading (after scrolling past the oldest) stay in the
        # axis as hatched columns until the loaders return.
        if self._pending:
            days = sorted(set(days) | self._pending)
        self._scroller.apply(days, max(1, len(series)), self._pending)
        self._chart.set_data(days, series, fmt, empty_text=empty)
        # CCTV bars open that system's day folder in Explorer on click;
        # Elastic bars open Kibana Discover on that system and day
        # (Chris, 2026-09-05).
        self._chart.set_click_handler(
            self._open_day_folder if self._metric in ("clips", "bytes")
            else self._open_in_grafana if self._metric in ("grafana", "grafana_bytes")
            else self._open_in_kibana
        )
        legend_bits = [
            f'<span style="background-color:{colour.name()};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{name}'
            for name, colour, _values in series
        ]
        self._legend.setText("&nbsp;&nbsp;&nbsp;".join(legend_bits))

    def _open_day_folder(self, name: str, day: date) -> None:
        parent_dir = self._parent_dir_provider()
        if parent_dir is None:
            return
        folder = Path(parent_dir) / name / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if not folder.is_dir():
            self._status_label.setText(f"No folder on the share for {name} on {day:%d/%m/%Y}")
            return
        try:
            os.startfile(str(folder))
            self._status_label.setText(f"Opened {folder}")
        except OSError as exc:
            self._status_label.setText(f"Could not open {folder}: {exc}")

    def _open_in_kibana(self, name: str, day: date) -> None:
        robot = self._system_to_robot(name)
        if not robot:
            self._status_label.setText(f"No robot id known for {name}")
            return
        settings = self._settings_provider()
        url = kibana_discover_url(settings.elastic_url or "", robot, day)
        try:
            webbrowser.open(url)
            self._status_label.setText(f"Opened Kibana for {name} on {day:%d/%m/%Y}")
        except Exception as exc:
            self._status_label.setText(f"Could not open the browser: {exc}")

    def _open_in_grafana(self, name: str, day: date) -> None:
        """The Actuators issues dashboard on that system and day."""
        robot = self._system_to_robot(name)
        if not robot:
            self._status_label.setText(f"No robot id known for {name}")
            return
        settings = self._settings_provider()
        start = datetime(day.year, day.month, day.day).astimezone()
        from_ms = int(start.timestamp() * 1000)
        to_ms = from_ms + 86_400_000 - 1
        url = f"{grafana_client.base_url(settings)}/d/ge98whx/actuators-issues?from={from_ms}&to={to_ms}&var-SystemId={robot}"
        try:
            webbrowser.open(url)
            self._status_label.setText(f"Opened Grafana for {name} on {day:%d/%m/%Y}")
        except Exception as exc:
            self._status_label.setText(f"Could not open the browser: {exc}")

    def _system_to_robot(self, name: str) -> str | None:
        for robot, system in self._robot_to_system.items():
            if system == name:
                return robot
        if name.startswith("35-2300-"):
            return name
        return robot_id_from_folder(name)

    def _detail_for(self, name: str, day: date) -> str:
        """Every metric for one system on one day, for the hover tooltip."""
        # Just the system and its figures: the day and the share of the
        # day are already visible on the chart (Chris, 2026-09-05).
        lines = [f"<b>{name}</b>"]
        # Only the source the chart is showing (Chris, 2026-09-05): an
        # Elastic view says nothing about CCTV, and vice versa.
        showing_elastic = self._metric in ("elastic", "elastic_bytes")
        inv = self._elastic if showing_elastic else None
        if inv is not None:
            docs = 0
            est_bytes = 0.0
            factor_used = 0.0
            for robot, per_day in inv.counts.items():
                if self._robot_to_system.get(robot, robot) == name:
                    count = int(per_day.get(day, 0))
                    docs += count
                    factor_used = inv.bytes_factor(robot)
                    est_bytes += count * factor_used
            line = f"Elastic: {format_count(docs)} documents"
            if est_bytes and docs:
                line += f" ≈ {format_bytes(est_bytes)} (avg {factor_used:.0f} B/doc)"
            lines.append(line)
            picks = self._picks_for(name, day)
            if picks:
                per = f"{docs / picks:,.0f} documents"
                if est_bytes:
                    per += f" ≈ {format_bytes(est_bytes / picks)}"
                lines.append(f"{picks:,} picks · {per} per pick")
            minutes = self._running_minutes_for(name, day)
            if minutes:
                hours = minutes / 60.0
                per = f"{docs / hours:,.0f} documents"
                if est_bytes:
                    per += f" ≈ {format_bytes(est_bytes / hours)}"
                lines.append(f"{minutes // 60}h {minutes % 60:02d}m on · {per} per hour")
            if docs:
                lines.append("<i>Click to open these documents in Kibana</i>")
        if self._metric in ("grafana", "grafana_bytes") and self._grafana is not None:
            samples = 0
            for robot, per_day in self._grafana.samples.items():
                if self._robot_to_system.get(robot, robot) == name:
                    samples += int(per_day.get(day, 0))
            lines.append(f"Grafana: {format_count(samples)} samples ≈ {format_bytes(self._grafana.bytes_for(samples))} (est.)")
            robot = self._robot_for(name)
            series = self._grafana.series_now.get(robot or "")
            if series:
                lines.append(f"{series:,} series reporting today")
            if samples:
                lines.append("<i>Click to open this day in Grafana</i>")
        cctv = self._cctv if self._metric in ("clips", "bytes") else None
        if cctv is not None and name in cctv.clips:
            clips = int(cctv.clips[name].get(day, 0))
            est = int(cctv.est_bytes.get(name, {}).get(day, 0))
            lines.append(f"CCTV: {clips:,} clips ≈ {format_bytes(est)} (est.)")
            picks = self._picks_for(name, day)
            if picks and est:
                lines.append(f"{picks:,} picks · {format_bytes(est / picks)} per pick")
            minutes = self._running_minutes_for(name, day)
            if minutes and est:
                lines.append(f"{minutes // 60}h {minutes % 60:02d}m on · {format_bytes(est / (minutes / 60.0))} per hour")
            lines.append("<i>Click to open this day's folder</i>")
            oldest = cctv.oldest_day.get(name)
            folders = cctv.day_folders.get(name)
            if oldest is not None or folders:
                extra = []
                if folders:
                    extra.append(f"{folders} day folders on the share")
                if oldest is not None:
                    extra.append(f"oldest {oldest:%d/%m/%Y}")
                lines.append(" · ".join(extra))
        lines.append(
            "<i>Right-click to remove the label</i>" if self._chart.has_label(name, day)
            else "<i>Right-click to add a label</i>"
        )
        return "<br>".join(lines)
