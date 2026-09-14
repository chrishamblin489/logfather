"""About dialog: which build is running (linked to its GitHub commit), the
schematic of the code, and a plain-English summary of every module, grouped
the way the code is laid out (core / data / ui) and mapped to the screens.

Rewritten 2026-09-14 after the package split and the rename of modules to
the names the screens use (replay_view, replay_timeline, data_boxes,
SyncCctvTimeWindow). Keep FILE_SUMMARIES in step with the tree: the
architecture doc's table is generated from it (tools/gen_architecture_table.py).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from logfather.core.app_version import load_version_info
from logfather.ui import theme
from logfather.ui.app_assets import resolve_asset_path
from logfather.paths import REPO_ROOT

GITHUB_REPO_URL = "https://github.com/chrishamblin489/logfather"

# Which screen or window each part of the code is. (screen, modules, what it is)
SCREEN_MAP: list[tuple[str, str, str]] = [
    ("Overview", "ui/overview_widget.py, ui/data_boxes.py",
     "One row per PikPak: SKU runs, manual periods, stops and CCTV coverage for the chosen days, "
     "with the Data and Additional data strips underneath."),
    ("PikPak Replay", "ui/replay_view.py, ui/replay_timeline.py, ui/annotated_video_widget.py, ui/target_buffer_widget.py",
     "A clip playing beside its logs, frame-aligned by the camera clock; the timeline chart under it; "
     "the Targets panel; the Drift, Gap, Sync, Conveyor and Track controls in the top bar."),
    ("Search", "ui/fleetwide_elastic_search_widget.py",
     "Saved Elastic searches run across every PikPak over a range of days, with per-system counts and charts."),
    ("Sync CCTV Time", "ui/time_ocr.py",
     "Reads the burnt-in date and time boxes with Tesseract, finds the camera's date sync and the exact second "
     "changes, and applies the clip's true start time (steps A-H on its flowchart)."),
    ("Conveyor", "ui/conveyor_calibration_dialog.py, data/conveyor_calibration.py",
     "The two-click tracking-line calibration that lets Track draw products moving along the belt."),
    ("Errors / Stops", "ui/errors_stops_window.py, data/errors_stops.py",
     "Stops per day and errors per day by category, clustered so a cascade counts once."),
    ("Software", "ui/software_window.py, data/software_history.py",
     "Package versions and commits per PikPak over time."),
    ("Data", "ui/data_inventory_dialog.py, data/data_inventory.py, ui/elastic_catalog_dialog.py, ui/grafana_catalog_dialog.py",
     "How much data exists per PikPak in Elastic, Grafana and on the CCTV share, with the two ? catalogues."),
    ("Stop report", "ui/stop_report.py",
     "Every stop of the day with a thumbnail, categorised, opening the clip at that moment."),
    ("Settings and Data sources", "ui/settings_dialog.py, ui/data_sources_dialog.py, data/settings_store.py",
     "Connection details, the 15 condition presets, the customer/PikPak layout, and the connection testers."),
]

# (display name, repo path, plain-English summary), grouped by layer.
FILE_GROUPS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("Entry", "How the program starts.", [
        ("src/Main_Window.py", "src/Main_Window.py",
         "The 21-line entry shim: puts src/ on the path and hands over to logfather.ui.app_main. Keeps the "
         "run command, the desktop shortcut and the PyInstaller specs working."),
        ("logfather/paths.py", "src/logfather/paths.py",
         "The three folder anchors (package, src, repo) and the bundle root in a frozen build."),
        ("ui/app_main.py", "src/logfather/ui/app_main.py",
         "Application start-up: the fade-in splash, single-instance guard, start-up geometry, and the "
         "desktop shortcut renamed to the running version."),
    ]),
    ("Core - pure logic, no Qt and no network", "Models and maths that everything else builds on. All unit-tested.", [
        ("core/timeline_model.py", "src/logfather/core/timeline_model.py",
         "The TimelineItem (a clip or an event on the day) plus the day, clip-name and timezone helpers "
         "and the clip-cache index."),
        ("core/time_alignment.py", "src/logfather/core/time_alignment.py",
         "The three-timeline maths in one dataclass: video seconds, log-event seconds and the camera's "
         "wall clock, including the OCR correction and the plausibility limit on offsets."),
        ("core/log_events.py", "src/logfather/core/log_events.py",
         "Turns fetched Elastic rows into the LogEvent list the replay plays against."),
        ("core/sku_timeline.py", "src/logfather/core/sku_timeline.py",
         "The SKU and manual-mode band state machine behind the Overview rows."),
        ("core/telemetry.py", "src/logfather/core/telemetry.py",
         "Which Prometheus metrics the app shows (temperatures, currents, pressure, picks) and how their "
         "series become tracks, groups and summaries."),
        ("core/grafana.py", "src/logfather/core/grafana.py",
         "Pure parsing of Grafana dashboard JSON: panels, queries, template variables, frames to series."),
        ("core/frame_analysis.py", "src/logfather/core/frame_analysis.py",
         "Pixel-difference and optical-flow views for the replay's Analysis tab (numpy and OpenCV only)."),
        ("core/app_version.py", "src/logfather/core/app_version.py",
         "Which build is running (version.json or git), and the check for a newer commit on GitHub."),
        ("core/retention.py", "src/logfather/core/retention.py",
         "The 30-day CCTV retention rule and the 'footage deleted' notice."),
    ]),
    ("Data - Elastic, Grafana, caches and stores", "Everything that talks to a server or a file. No GUI imports.", [
        ("data/elastic_client.py", "src/logfather/data/elastic_client.py",
         "Shared HTTP plumbing for Elastic: sessions, URLs, headers, the search_after pagination and retry ladder."),
        ("data/elastic_schema.py", "src/logfather/data/elastic_schema.py",
         "The one place that knows the Argus 1 vs Argus 2 log schema: robot ids (leap_robot_id / system_id), "
         "state names, what counts as manual, automatic, shutdown or a stop."),
        ("data/elastic_loader.py", "src/logfather/data/elastic_loader.py",
         "The gateway to Elastic: the query builders, the day events fetch with its on-disk cache (past days "
         "never expire; bump EVENTS_CACHE_SCHEMA_VERSION when the logic changes), SKU items, raw logs for a "
         "clip, the Overview chunks and the Search histograms."),
        ("data/elastic_errors.py", "src/logfather/data/elastic_errors.py",
         "One exception that carries the rows a half-failed query did manage to fetch."),
        ("data/errors_stops.py", "src/logfather/data/errors_stops.py",
         "Errors and line stops per day per PikPak: stop kinds, error categories, and the 2-second clustering."),
        ("data/software_history.py", "src/logfather/data/software_history.py",
         "Package and commit history per PikPak, built into version spans, with a local raw cache."),
        ("data/data_inventory.py", "src/logfather/data/data_inventory.py",
         "The Data window's numbers: Elastic volume per day, running days, CCTV clips on the share, with a cache."),
        ("data/elastic_catalog.py", "src/logfather/data/elastic_catalog.py",
         "What kinds of documents Elastic holds, and the values a field takes - the Elastic ? catalogue."),
        ("data/event_counts.py", "src/logfather/data/event_counts.py",
         "Running totals of a logged event per PikPak (Motor overcurrent trips, crate change errors) as strips."),
        ("data/pick_rate.py", "src/logfather/data/pick_rate.py",
         "Picks per minute from Elastic for PikPaks that Grafana does not cover."),
        ("data/target_buffer_loader.py", "src/logfather/data/target_buffer_loader.py",
         "Replays the 'new pick target' log messages to rebuild the robot's pick queue at any instant of a clip."),
        ("data/grafana_client.py", "src/logfather/data/grafana_client.py",
         "The Grafana HTTP client (service-account token): health, dashboards, Prometheus queries."),
        ("data/grafana_inventory.py", "src/logfather/data/grafana_inventory.py",
         "How much telemetry Grafana holds per PikPak per day."),
        ("data/grafana_catalog.py", "src/logfather/data/grafana_catalog.py",
         "What metrics Grafana holds and what each looks like - the Grafana ? catalogue."),
        ("data/telemetry_loader.py", "src/logfather/data/telemetry_loader.py",
         "The six PromQL queries for one PikPak-day, and the fleet signals for the Data strips (routing the "
         "Elastic-derived ones to pick_rate and event_counts)."),
        ("data/clip_cache.py", "src/logfather/data/clip_cache.py",
         "The local CCTV clip cache: copies from the share, prefetches the next clips, prunes by age and size."),
        ("data/day_listing_cache.py", "src/logfather/data/day_listing_cache.py",
         "Caches each past day's clip listing so the share is not walked twice."),
        ("data/overview_event_cache.py", "src/logfather/data/overview_event_cache.py",
         "Today's raw Overview events on disk, so a restart fetches only the tail."),
        ("data/settings_store.py", "src/logfather/data/settings_store.py",
         "Everything the app remembers: video root, Elastic and Grafana connection, the 15 condition presets, "
         "customers and PikPak layout, fleetwide searches - saved to ~/.cctv_picker_settings.json."),
        ("data/ui_state_store.py", "src/logfather/data/ui_state_store.py",
         "Per-user window state (ticks, collapsed boxes, hidden systems) kept out of Settings so a stale "
         "instance cannot clobber it."),
        ("data/ocr_offset_store.py", "src/logfather/data/ocr_offset_store.py",
         "The per-camera JSON of OCR clock offsets: atomic writes, corrupt files set aside rather than replaced."),
        ("data/conveyor_calibration.py", "src/logfather/data/conveyor_calibration.py",
         "The belt model: the tracking line and its speed per PikPak, saved under ~/.logfather/calibrations."),
    ]),
    ("UI - the hub and the shared pieces", "The main window that wires every screen together, and the helpers they share.", [
        ("ui/Main_Window.py", "src/logfather/ui/Main_Window.py",
         "The hub: builds the Overview / PikPak Replay / Search stack, the top bar (PikPak and date choosers, "
         "first-product, Drift, Sync, Conveyor, Track, Targets, Data, Errors / Stops, Software, gear), the "
         "date picker and timeline splitters, session resume, clip opening and prefetch, and the jump from "
         "the Overview to a moment in a clip."),
        ("ui/target_overlay_controller.py", "src/logfather/ui/target_overlay_controller.py",
         "Loads the clip's pick-queue events, classifies tight and wide gaps, owns the conveyor calibration "
         "and the Conveyor dialog, and builds the product overlays the Track button draws."),
        ("ui/qt_worker.py", "src/logfather/ui/qt_worker.py",
         "The one background-job pattern (Job on a QThread, JobSlot to retire stale results) used by every loader."),
        ("ui/progress.py", "src/logfather/ui/progress.py",
         "The shared busy and progress dialogs: BusyDialog (indeterminate), StageProgress (with Cancel, "
         "nothing shown headless) and job_progress (a dialog bound to a background JobSlot)."),
        ("ui/gear_menu.py", "src/logfather/ui/gear_menu.py",
         "The gear dropdown shared by the windows: Data sources, Settings, Stop report, Fit, zoom, About."),
        ("ui/day_selection.py", "src/logfather/ui/day_selection.py",
         "The one shared day range that the Overview, Errors / Stops and Data windows follow together."),
        ("ui/day_range_dialog.py", "src/logfather/ui/day_range_dialog.py",
         "The From / To day picker with presets."),
        ("ui/day_popup.py", "src/logfather/ui/day_popup.py",
         "The single-day calendar popup behind the top bar's date button."),
        ("ui/Date_Picker_frontend.py", "src/logfather/ui/Date_Picker_frontend.py",
         "The left panel: PikPak buttons grouped by customer with logos, and a calendar of days with footage."),
        ("ui/system_filter.py", "src/logfather/ui/system_filter.py",
         "The PikPaks filter popup (tick many) and the PikPak picker (choose one)."),
        ("ui/charts.py", "src/logfather/ui/charts.py",
         "The stacked per-day bar chart with hover detail used by Errors / Stops and Data."),
        ("ui/chart_scroll.py", "src/logfather/ui/chart_scroll.py",
         "One scrollbar and zoom shared by several day charts, with an edge signal to load more days."),
        ("ui/theme.py", "src/logfather/ui/theme.py",
         "Every colour token and stylesheet, the app zoom, and the one place a restyle should happen."),
        ("ui/icons.py", "src/logfather/ui/icons.py",
         "The painted icons (no image files): gear, calendar, conveyor, punnet, sync, first product, question block..."),
        ("ui/pulse.py", "src/logfather/ui/pulse.py",
         "The gentle breathing highlight on a button that needs attention (Sync: ?)."),
        ("ui/pane_animator.py", "src/logfather/ui/pane_animator.py",
         "The one splitter-pane slide: the Targets panel, the date picker, the timeline and the log tabs "
         "open and close through it (show before, hide after, restart from wherever a slide is)."),
        ("ui/window_placement.py", "src/logfather/ui/window_placement.py",
         "Keeps secondary windows on screen and over their parent."),
        ("ui/app_assets.py", "src/logfather/ui/app_assets.py",
         "Finds bundled assets (logo, diagram, placeholder) in a source checkout or a frozen build."),
        ("ui/about_page.py", "src/logfather/ui/about_page.py",
         "This dialog: the version linked to its GitHub commit, the schematic, and these summaries."),
    ]),
    ("UI - the screens and windows", "One module per thing you can open.", [
        ("ui/overview_widget.py", "src/logfather/ui/overview_widget.py",
         "The Overview: one row per PikPak drawn on a graphics scene (SKU runs, manual, stops, CCTV "
         "coverage), the day range, the PikPaks filter, drag to reorder, hover thumbnails, and the "
         "incremental refresh with its on-disk cache."),
        ("ui/data_boxes.py", "src/logfather/ui/data_boxes.py",
         "The Data and Additional data boxes and their reading strips (a SignalChannel per Grafana or "
         "Elastic reading), shared by the Overview and the PikPak Replay timeline."),
        ("ui/replay_view.py", "src/logfather/ui/replay_view.py",
         "The PikPak Replay: video playback with the log list, the Elastic log load, the OCR offset "
         "applied to the main and the Additional CCTV, the Sync and Overlay tool strips, the Analysis "
         "tab, annotations, Bird's Eye, and export with overlays burnt in."),
        ("ui/log_filter_panel.py", "src/logfather/ui/log_filter_panel.py",
         "The replay's Filters and Custom tabs: the source / state / message checkbox columns, the 15 "
         "filter presets, the five custom filter-in / filter-out blocks, their Settings persistence, "
         "and the row matching the log list is filtered by."),
        ("ui/replay_timeline.py", "src/logfather/ui/replay_timeline.py",
         "The timeline chart under the replay: the day's clips, event ticks, SKU bands, the Errors box rows, "
         "the label gutter, the playhead, the View menu, zoom and the Data strips."),
        ("ui/annotated_video_widget.py", "src/logfather/ui/annotated_video_widget.py",
         "The video canvas: the frame, drawing and measuring annotations, the info text, the product "
         "overlays and the Bird's Eye tray view."),
        ("ui/viewer_widgets.py", "src/logfather/ui/viewer_widgets.py",
         "Small replay widgets: the seek and clip-range sliders, the marker bars, the log list model, the drift slider."),
        ("ui/target_buffer_widget.py", "src/logfather/ui/target_buffer_widget.py",
         "The Targets panel: one card per product in the robot's queue, updating as the clip plays."),
        ("ui/telemetry_strip.py", "src/logfather/ui/telemetry_strip.py",
         "The Telemetry tab in the replay: the day's Grafana tracks in groups."),
        ("ui/time_ocr.py", "src/logfather/ui/time_ocr.py",
         "The Sync CCTV Time window and the OCR engine behind it: the draggable Date and Time boxes, the "
         "date procedure A-H, the second-boundary search, the readings table with its 60 s drift checks, "
         "the help flowchart, and the headless analysis the automatic sync runs."),
        ("ui/ocr_channel.py", "src/logfather/ui/ocr_channel.py",
         "One camera's OCR clock sync (main or Additional CCTV): its offset store, worker slot, key tag, "
         "ROI settings key and per-clip offset, plus the cached-offset read with the plausibility drop and "
         "the filename fallback ladder the replay runs for both pictures."),
        ("ui/conveyor_calibration_dialog.py", "src/logfather/ui/conveyor_calibration_dialog.py",
         "The Conveyor window: click the same belt landmark on two frames to set the tracking line and speed."),
        ("ui/fleetwide_elastic_search_widget.py", "src/logfather/ui/fleetwide_elastic_search_widget.py",
         "The Search screen: saved searches over every PikPak for a day range, cards and graphs per system."),
        ("ui/errors_stops_window.py", "src/logfather/ui/errors_stops_window.py",
         "The Errors / Stops window: stops per day and errors per day by category, with the PikPaks filter."),
        ("ui/software_window.py", "src/logfather/ui/software_window.py",
         "The Software window: version spans per PikPak on a timeline."),
        ("ui/data_inventory_dialog.py", "src/logfather/ui/data_inventory_dialog.py",
         "The Data window: Elastic, Grafana and CCTV volume per PikPak per day, and the two ? buttons."),
        ("ui/elastic_catalog_dialog.py", "src/logfather/ui/elastic_catalog_dialog.py",
         "The Elastic ? catalogue window and the field-values drill-down."),
        ("ui/grafana_catalog_dialog.py", "src/logfather/ui/grafana_catalog_dialog.py",
         "The Grafana ? catalogue window and the metric detail."),
        ("ui/data_sources_dialog.py", "src/logfather/ui/data_sources_dialog.py",
         "Data sources: the CCTV share, Elastic and Grafana connections, each with a test button."),
        ("ui/settings_dialog.py", "src/logfather/ui/settings_dialog.py",
         "The Settings tabs inside the replay: connection, the condition presets, the customer/PikPak layout, "
         "and the read-me."),
        ("ui/stop_report.py", "src/logfather/ui/stop_report.py",
         "The Stop report: gathers the day's stops off the GUI thread, then builds the thumbnailed list."),
    ]),
    ("Tools and tests", "Outside the app.", [
        ("tools/smoke_test.py", "tools/smoke_test.py",
         "The after-every-edit check: imports every module under logfather and builds the main window offscreen."),
        ("tools/elastic_api_check.py", "tools/elastic_api_check.py",
         "Proves the Elastic connection works with the app's own settings."),
        ("tools/elastic_volume_check.py", "tools/elastic_volume_check.py",
         "Elastic volume per day and per machine for the last 30 days."),
        ("tools/grafana_check.py", "tools/grafana_check.py",
         "Grafana version, org, datasources and one dashboard's queries."),
        ("tools/elastic-log-download.py", "tools/elastic-log-download.py",
         "Standalone CSV download of a robot's logs through Kibana Reporting (API key from the environment)."),
        ("tools/Vid_Frame_Differencing.py", "tools/Vid_Frame_Differencing.py",
         "The original motion-analysis prototype; its maths now lives in core/frame_analysis.py."),
        ("tools/logs_to_srt.py", "tools/logs_to_srt.py",
         "Legacy: a CSV log export turned into subtitles. Superseded by the replay."),
        ("tests/", "tests",
         "26 pytest modules over the pure logic: parsing, caches, alignment, the OCR engine, errors and stops, "
         "telemetry, Grafana, software history, the offset store."),
        ("build.ps1 + spec/iss", "build.ps1",
         "The release pipeline: stamp version.json, PyInstaller-bundle The Logfather, build the installer."),
    ]),
]

# Flat view, kept for anything that iterated the old list.
FILE_SUMMARIES: list[tuple[str, str, str]] = [entry for _g, _d, entries in FILE_GROUPS for entry in entries]


def _repo_root() -> Path:
    return REPO_ROOT


def _current_commit() -> str | None:
    """Full SHA of the running code: live git in a source checkout, else the
    build-time SHA baked into version.json (frozen builds have no git)."""
    if not getattr(sys, "_MEIPASS", None):
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(_repo_root()),
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            pass
    sha = str(load_version_info().get("git_sha") or "").strip()
    return sha or None


def _version_text() -> str:
    info = load_version_info()
    version = str(info.get("version") or "dev")
    if getattr(sys, "_MEIPASS", None):
        return f"v{version}"
    if version != "dev":
        return f"v{version} (source)"
    return "source checkout"


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("About The Logfather")
        self.resize(1160, 820)
        self.setStyleSheet(theme.ABOUT_PAGE)

        header = QHBoxLayout()
        logo_path = resolve_asset_path("logfather.png")
        if logo_path:
            logo = QLabel()
            logo.setPixmap(
                QPixmap(str(logo_path)).scaled(
                    72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )
            header.addWidget(logo)
        title_col = QVBoxLayout()
        title = QLabel("The Logfather")
        title.setStyleSheet(theme.TITLE_LABEL)
        title_col.addWidget(title)
        subtitle = QLabel("CCTV video + Elastic log viewer")
        subtitle.setStyleSheet(theme.ABOUT_MUTED)
        title_col.addWidget(subtitle)
        title_col.addWidget(self._build_version_label())
        header.addLayout(title_col)
        header.addStretch(1)

        tabs = QTabWidget()
        tabs.addTab(self._build_diagram_tab(), "How it fits together")
        tabs.addTab(self._build_screens_tab(), "Where each screen lives")
        tabs.addTab(self._build_files_tab(), "What each file does")

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)

        layout = QVBoxLayout()
        layout.addLayout(header)
        layout.addWidget(tabs, 1)
        layout.addLayout(close_row)
        self.setLayout(layout)

    def _build_version_label(self) -> QLabel:
        sha = _current_commit()
        parts = [f"Version: {_version_text()}"]
        if sha:
            short = sha[:7]
            parts.append(
                f'commit <a style="color:#5b9bd5" '
                f'href="{GITHUB_REPO_URL}/commit/{sha}">{short}</a>'
            )
        else:
            parts.append("commit unknown")
        label = QLabel(" &nbsp;&middot;&nbsp; ".join(parts))
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        label.setStyleSheet(theme.ABOUT_MUTED)
        return label

    def _build_diagram_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        diagram_path = resolve_asset_path("logfather_architecture.svg")
        if diagram_path:
            svg = QSvgWidget(str(diagram_path))
            size = svg.renderer().defaultSize()
            if size.isValid() and size.width() > 0:
                svg.setFixedSize(size)
            else:
                svg.setFixedSize(1180, 940)
            scroll.setWidget(svg)
        else:
            missing = QLabel("Architecture diagram not found (logfather_architecture.svg).")
            missing.setAlignment(Qt.AlignCenter)
            scroll.setWidget(missing)
        return scroll

    @staticmethod
    def _link(repo_path: str, text: str) -> str:
        return (f'<a style="color:#5b9bd5; font-weight:bold; text-decoration:none;" '
                f'href="{GITHUB_REPO_URL}/blob/main/{repo_path}">{text}</a>')

    def _build_screens_tab(self) -> QWidget:
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        rows = []
        for screen, modules, what in SCREEN_MAP:
            links = ", ".join(
                self._link(f"src/logfather/{m.strip()}", m.strip()) for m in modules.split(",")
            )
            rows.append(
                f'<p style="margin: 8px 0;"><span style="color:{theme.TEXT_BRIGHT}; font-weight:bold; font-size:14px;">{screen}</span>'
                f'<br/><span style="color:#b8c4d0;">{what}</span>'
                f'<br/><span style="color:#9fb0c0;">Code: {links}</span></p>'
            )
        browser.setHtml(
            '<div style="font-size: 13px;">'
            '<p style="color:#9fb0c0;">The name on the screen is the name in the code: the PikPak Replay is '
            'replay_view.py with replay_timeline.py under it, the Sync CCTV Time window is SyncCctvTimeWindow, '
            'the Data boxes are data_boxes.py, and the top-bar buttons are conveyor_btn, track_toggle, '
            'targets_toggle, search_btn and so on.</p>' + "".join(rows) + "</div>"
        )
        return browser

    def _build_files_tab(self) -> QWidget:
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        parts = [
            '<div style="font-size: 13px;">'
            f'<p style="color:#9fb0c0;">Click a file name to open it on GitHub. The layering rule: ui may import '
            f'data and core; data may import core; never the reverse. The full write-up lives in '
            f'{self._link("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.md")}.</p>'
        ]
        for group, blurb, entries in FILE_GROUPS:
            parts.append(
                f'<h3 style="color:{theme.TEXT_BRIGHT}; margin: 14px 0 2px 0;">{group}</h3>'
                f'<p style="color:#9fb0c0; margin: 0 0 6px 0;">{blurb}</p>'
            )
            for name, repo_path, summary in entries:
                parts.append(
                    f'<p style="margin: 5px 0;">{self._link(repo_path, name)}<br/>'
                    f'<span style="color:#b8c4d0;">{summary}</span></p>'
                )
        parts.append("</div>")
        browser.setHtml("".join(parts))
        return browser
