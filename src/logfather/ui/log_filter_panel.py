"""The PikPak Replay's log filters: the Filters tab (15 preset buttons over
the source / state / message checkbox columns) and the Custom tab (five
free-text filter-in / filter-out blocks), with the pure row-matching
helpers they run on.

Split out of replay_view.py on 2026-09-14 (code review item 4). The panel
owns the checkbox state, the presets and their persistence in Settings;
the replay view keeps the log list and playback, and re-reads
``filtered_rows()`` whenever ``filters_changed`` fires. The busy dialog
stays with the view too: the panel only asks for it via ``busy_changed``.

The module-level functions (``parse_custom_terms``, ``custom_filter_match``,
``collect_base_filtered_rows``, ``collect_preset_filtered_rows``,
``visible_state_and_message_keys``) take plain lists and sets so they run
without Qt in the tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from logfather.core.log_events import MESSAGE_COLUMN, SOURCE_COLUMN, STATE_COLUMN, LogEvent
from logfather.data.settings_store import CustomFilterPreset, FilterPreset, Settings
from logfather.ui import theme

# The three checkbox columns, in the order they sit on the Filters tab.
FILTER_KINDS: tuple[str, ...] = ("source", "state", "message")
_COLUMN_TITLES = {"source": SOURCE_COLUMN, "state": STATE_COLUMN, "message": MESSAGE_COLUMN}

# Rows with an empty state are listed (and filtered) under this key.
NULL_STATE = "(null)"

PRESET_COUNT = 15
CUSTOM_FILTER_COUNT = 5

# The custom filters combine their filter-in terms with OR: a row passes a
# block when ANY of its terms appears. custom_filter_match also implements
# "AND" (all terms must appear) but nothing in the UI selects it.
CUSTOM_FILTER_MODE = "OR"

# A display row is "HH:MM:SS.mmm  |  source ..."; the custom filters only
# look at the part after the timecode.
_ROW_SEPARATOR = "  |  "

CustomFilter = tuple[list[str], list[str]]  # (filter-in terms, filter-out terms), lower-cased


# ---------------------------------------------------------------------------
# Pure helpers (no Qt)
# ---------------------------------------------------------------------------

def state_key(state: str | None) -> str:
    """The checkbox key for a row's state: the state, or NULL_STATE when empty."""
    return state if state else NULL_STATE


def row_match_text(row_text: str) -> str:
    """The part of a display row the custom filters see (after the timecode)."""
    if _ROW_SEPARATOR in row_text:
        return row_text.split(_ROW_SEPARATOR, 1)[1]
    return row_text


def parse_custom_terms(text: str) -> list[str]:
    """Split a comma-separated entry into stripped, lower-cased terms;
    empty entries are dropped."""
    return [term.strip().lower() for term in text.split(",") if term.strip()]


def custom_filter_match(text: str, filters: Sequence[CustomFilter], mode: str = "OR") -> bool:
    """True when ``text`` passes at least one of ``filters``.

    A filter passes when its filter-in terms match (any of them in "OR"
    mode, all of them in "AND" mode; an empty list always matches) and
    none of its filter-out terms appear. No filters at all means everything
    passes. Terms are expected lower-cased (parse_custom_terms).
    """
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


@dataclass(frozen=True)
class FilterSelection:
    """What the checkbox columns let through. ``None`` for a column means
    that column is not filtering (it has no checkboxes to tick), so every
    row passes it; otherwise only rows whose key is in the set pass."""

    sources: frozenset[str] | None = None
    states: frozenset[str] | None = None
    messages: frozenset[str] | None = None


def collect_base_filtered_rows(
    events: Sequence[LogEvent],
    display_rows: Sequence[str],
    source_keys: Sequence[str],
    state_keys: Sequence[str],
    message_keys: Sequence[str],
    selection: FilterSelection,
) -> list[tuple[LogEvent, str]]:
    """The (event, display row) pairs that pass the checkbox selection."""
    rows: list[tuple[LogEvent, str]] = []
    for ev, row_text, src, state, msg in zip(events, display_rows, source_keys, state_keys, message_keys):
        if selection.sources is not None and src not in selection.sources:
            continue
        if selection.states is not None and state_key(state) not in selection.states:
            continue
        if selection.messages is not None and msg not in selection.messages:
            continue
        rows.append((ev, row_text))
    return rows


def collect_preset_filtered_rows(
    events: Sequence[LogEvent],
    display_rows: Sequence[str],
    source_keys: Sequence[str],
    state_keys: Sequence[str],
    message_keys: Sequence[str],
    presets: Sequence[FilterPreset],
) -> list[tuple[LogEvent, str]]:
    """The (event, display row) pairs matching ANY of the active presets.

    A preset matches a row when each of its non-empty lists contains the
    row's key; a preset with all three lists empty matches nothing (it
    was never saved from a selection).
    """
    rows: list[tuple[LogEvent, str]] = []
    for ev, row_text, src, state, msg in zip(events, display_rows, source_keys, state_keys, message_keys):
        state_val = state_key(state)
        for preset in presets:
            if preset.sources and src not in preset.sources:
                continue
            if preset.states and state_val not in preset.states:
                continue
            if preset.messages and msg not in preset.messages:
                continue
            if not preset.sources and not preset.states and not preset.messages:
                continue
            rows.append((ev, row_text))
            break
    return rows


def visible_state_and_message_keys(
    source_keys: Sequence[str],
    state_keys: Sequence[str],
    message_keys: Sequence[str],
    allowed_sources: Iterable[str] | None,
    allowed_states: Iterable[str] | None,
) -> tuple[set[str], set[str]]:
    """Which state keys occur under the allowed sources, and which message
    keys occur under the allowed sources AND states. ``None`` means the
    column is not filtering. Drives the checkbox hiding on the Filters tab."""
    src_allowed = None if allowed_sources is None else set(allowed_sources)
    state_allowed = None if allowed_states is None else set(allowed_states)
    states_used: set[str] = set()
    messages_used: set[str] = set()
    for src, state, msg in zip(source_keys, state_keys, message_keys):
        if src_allowed is not None and src not in src_allowed:
            continue
        state_val = state_key(state)
        states_used.add(state_val)
        if state_allowed is not None and state_val not in state_allowed:
            continue
        if msg:
            messages_used.add(msg)
    return states_used, messages_used


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class _FilterColumn:
    """One checkbox column of the Filters tab: the "Filter by X" label with
    its All / None buttons over a scrolling list of checkboxes."""

    def __init__(self, kind: str):
        self.kind = kind
        self.label = QLabel(f"Filter by {_COLUMN_TITLES[kind]}")
        self.label.setWordWrap(True)
        self.container: QWidget | None = None
        self.inner_layout: QVBoxLayout | None = None
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumWidth(160)
        self.all_btn = QPushButton("All")
        self.none_btn = QPushButton("None")
        self.header = QHBoxLayout()
        self.header.addWidget(self.label, 1)
        self.header.addWidget(self.all_btn)
        self.header.addWidget(self.none_btn)
        self.reset()

    def add_to(self, layout: QVBoxLayout) -> None:
        layout.addLayout(self.header)
        layout.addWidget(self.scroll)

    def reset(self) -> None:
        """Throw the checkbox list away and start from an empty one."""
        if self.container is not None:
            self.container.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.container = new_widget
        self.inner_layout = new_layout
        self.scroll.setWidget(new_widget)

    def add_checkboxes(self, keys: Sequence[str], unchecked: set[str], on_change) -> dict[str, QCheckBox]:
        """Fill the column with one checkbox per key (ticked unless the key
        is in ``unchecked``); returns key -> checkbox."""
        boxes: dict[str, QCheckBox] = {}
        layout = self.inner_layout
        if keys:
            last = layout.takeAt(layout.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)
            for key in keys:
                cb = QCheckBox(key)
                cb.setChecked(key not in unchecked)
                cb.stateChanged.connect(on_change)
                layout.addWidget(cb)
                boxes[key] = cb
            layout.addStretch(1)
        return boxes


class LogFilterPanel(QWidget):
    """The Filters tab (this widget) and the Custom tab (``custom_tab``).

    Usage from the replay view::

        panel = LogFilterPanel(settings)
        panel.filters_changed.connect(view.refresh_rows)   # then panel.filtered_rows()
        panel.busy_changed.connect(view.set_busy)          # (busy, message)
        panel.add_to_tabs(right_tabs)                      # adds "Filters" and "Custom"
        panel.set_events(...)                              # when logs load
        panel.load_filters_panel()                         # build the checkboxes
        panel.apply_filters()                              # re-filter now
    """

    # The set of rows that pass changed: call filtered_rows() for the new one.
    filters_changed = Signal()
    # (busy, message): show / hide the view's "Log Viewer" progress dialog.
    busy_changed = Signal(bool, str)

    def __init__(self, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self._settings = settings
        self._tabs: QTabWidget | None = None

        self.filters_loaded = False
        # Unticked filter keys survive a reload: rebuilt checkboxes start
        # from this memory, and filters auto-reload if they were loaded
        # before (Chris, 2026-09-05).
        self._remembered_unchecked: dict[str, set[str]] = {kind: set() for kind in FILTER_KINDS}
        self._filters_wanted = False

        # The loaded logs, one entry per row (set_events).
        self._events: list[LogEvent] = []
        self._display_rows: list[str] = []
        self._source_keys: list[str] = []
        self._state_keys: list[str] = []
        self._message_keys: list[str] = []

        # kind -> (key -> checkbox); empty until load_filters_panel builds them.
        self._checkboxes: dict[str, dict[str, QCheckBox]] = {kind: {} for kind in FILTER_KINDS}

        self._build_checkbox_panel()
        self._build_presets()
        self._build_custom_tab()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._preset_container)
        layout.addWidget(self._checkbox_panel)

        # Typing in an enabled custom filter re-filters after a short pause.
        self._filter_debounce_timer = QTimer(self)
        self._filter_debounce_timer.setSingleShot(True)
        self._filter_debounce_timer.setInterval(250)
        self._filter_debounce_timer.timeout.connect(self.apply_filters)

        self.reload_from_settings()

    # ---- construction -----------------------------------------------------

    def _build_checkbox_panel(self) -> None:
        """The source / state / message columns (hidden until filters load)."""
        self._columns: dict[str, _FilterColumn] = {}
        panel_layout = QVBoxLayout()
        for kind in FILTER_KINDS:
            column = _FilterColumn(kind)
            column.all_btn.clicked.connect(lambda _checked=False, k=kind: self._set_group_checked(k, True))
            column.none_btn.clicked.connect(lambda _checked=False, k=kind: self._set_group_checked(k, False))
            self._columns[kind] = column
            if kind != "source":
                panel_layout.addSpacing(12)
            if kind == "state":
                # The gap above the state column has always been 24 px.
                panel_layout.addSpacing(12)
            column.add_to(panel_layout)
        self._checkbox_panel = QWidget()
        self._checkbox_panel.setLayout(panel_layout)
        self._checkbox_panel.setVisible(False)

    def _build_presets(self) -> None:
        """The 3 x 5 grid of preset buttons above the columns."""
        self.filter_preset_group: list[QPushButton] = []
        self.active_filter_preset_index: int | None = None
        self.active_filter_presets: set[int] = set()

        self._preset_container = QWidget()
        container_layout = QVBoxLayout(self._preset_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(4)

        preset_index = 0
        for _row in range(3):
            preset_row = QHBoxLayout()
            preset_row.setSpacing(6)
            for _col in range(PRESET_COUNT // 3):
                btn = QPushButton(f"Preset {preset_index + 1}")
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
            container_layout.addLayout(preset_row)

    def _build_custom_tab(self) -> None:
        """The Custom tab: five (button, filter-in, filter-out, count) blocks."""
        self.custom_filter_blocks: list[tuple[QPushButton, QLineEdit, QLineEdit, QLabel]] = []
        self.custom_filter_hint = QLabel("Empty entries are ignored. Use commas to separate terms.")
        self.custom_filter_hint.setStyleSheet(theme.DIM_LABEL)

        self.custom_tab = QWidget()
        custom_layout = QVBoxLayout(self.custom_tab)
        custom_layout.setContentsMargins(8, 8, 8, 8)
        custom_layout.setSpacing(6)
        custom_layout.addWidget(QLabel("Custom filters (comma separated)."))
        custom_layout.addWidget(self.custom_filter_hint)

        for idx in range(1, CUSTOM_FILTER_COUNT + 1):
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
            out_edit = QLineEdit()
            out_edit.setPlaceholderText("Filter out (comma separated)")
            for edit in (in_edit, out_edit):
                edit.textChanged.connect(lambda _text, b=btn: self._on_custom_filter_text_changed(b))
                edit.textChanged.connect(self._validate_custom_filter_inputs)
            count_label = QLabel("Matches: -")
            count_label.setStyleSheet(theme.DIM_LABEL)

            block_layout.addWidget(btn)
            block_layout.addWidget(in_edit)
            block_layout.addWidget(out_edit)
            block_layout.addWidget(count_label)
            custom_layout.addWidget(block)
            self.custom_filter_blocks.append((btn, in_edit, out_edit, count_label))

        custom_layout.addStretch(1)

    # ---- tabs -------------------------------------------------------------

    def add_to_tabs(self, tabs: QTabWidget) -> None:
        """Add the Filters and Custom tabs to ``tabs`` and drive their
        enabled state and highlight colour from here on."""
        self._tabs = tabs
        tabs.addTab(self, "Filters")
        tabs.addTab(self.custom_tab, "Custom")

    def set_tabs_enabled(self, enabled: bool) -> None:
        """Grey both tabs out (no logs loaded) or enable them."""
        if self._tabs is None:
            return
        tab_bar = self._tabs.tabBar()
        default_color = self.palette().color(QPalette.WindowText)
        disabled_color = QColor("#888888")
        for page in (self, self.custom_tab):
            idx = self._tabs.indexOf(page)
            if idx >= 0:
                self._tabs.setTabEnabled(idx, enabled)
                tab_bar.setTabTextColor(idx, default_color if enabled else disabled_color)

    def update_tab_highlights(self) -> None:
        """Red tab text while a filter is narrowing the log list."""
        if self._tabs is None:
            return
        highlight = QColor("#ff4d4f")
        default_color = QApplication.palette().windowText().color()
        disabled_color = QColor("#888888")
        tab_bar = self._tabs.tabBar()
        for page, label, active in (
            (self, "Filters", self._checkbox_filter_active),
            (self.custom_tab, "Custom", self._custom_filter_active),
        ):
            idx = self._tabs.indexOf(page)
            if idx < 0:
                continue
            self._tabs.setTabText(idx, label)
            if not self._tabs.isTabEnabled(idx):
                tab_bar.setTabTextColor(idx, disabled_color)
            else:
                tab_bar.setTabTextColor(idx, highlight if active() else default_color)

    def _checkbox_filter_active(self) -> bool:
        if self.active_filter_presets:
            return True
        if not self.filters_loaded:
            return False
        return any(
            not cb.isChecked()
            for kind in FILTER_KINDS
            for cb in self._checkboxes[kind].values()
        )

    def _custom_filter_active(self) -> bool:
        return any(btn.isChecked() for btn, _in, _out, _count in self.custom_filter_blocks)

    # ---- events in / rows out ---------------------------------------------

    def set_events(
        self,
        events: Sequence[LogEvent],
        display_rows: Sequence[str],
        source_keys: Sequence[str],
        state_keys: Sequence[str],
        message_keys: Sequence[str],
    ) -> None:
        """Take a freshly loaded set of logs. The checkbox columns are
        cleared (remembering what was unticked) and hidden until
        load_filters_panel rebuilds them."""
        self._events = list(events or [])
        self._display_rows = list(display_rows or [])
        self._source_keys = list(source_keys or [])
        self._state_keys = list(state_keys or [])
        self._message_keys = list(message_keys or [])
        self._reset_filter_state()

    def clear_events(self) -> None:
        """No logs: clear the columns and hide them."""
        self.set_events([], [], [], [], [])

    def reload_filters_if_wanted(self) -> None:
        """After a reload of the same logs: rebuild the filters with the
        remembered ticks if they were loaded before, no extra click needed."""
        if self._filters_wanted and self._events and not self.filters_loaded:
            self._filters_wanted = False
            self.load_filters_panel()

    def filtered_rows(self) -> list[tuple[LogEvent, str]]:
        """The (event, display row) pairs passing the presets or checkboxes
        and then the enabled custom filters."""
        if self.active_filter_presets:
            base_rows = self._collect_preset_filtered_rows()
        else:
            base_rows = self._collect_base_filtered_rows()
        custom_filters = self._get_active_custom_filters()
        if not custom_filters:
            return base_rows
        return [
            (ev, row_text)
            for ev, row_text in base_rows
            if custom_filter_match(row_match_text(row_text), custom_filters, CUSTOM_FILTER_MODE)
        ]

    def apply_filters(self, status_message: str | None = None, manage_busy: bool = True) -> None:
        """Re-filter: tell the view (filters_changed), then refresh the
        match counts and tab colours. With manage_busy the busy dialog is
        shown around the work; the callers that already show it pass False."""
        print("[viewer] apply_filters start", flush=True)
        if not self._events:
            print("[viewer] apply_filters no events", flush=True)
            if manage_busy:
                self.busy_changed.emit(False, "")
            return
        if manage_busy:
            self.busy_changed.emit(True, status_message or "Applying filters...")
        self.filters_changed.emit()
        self._update_custom_filter_counts()
        self.update_tab_highlights()
        if manage_busy:
            self.busy_changed.emit(False, "")
        print("[viewer] apply_filters done", flush=True)

    def _current_selection(self) -> FilterSelection:
        """What the checkboxes allow right now. A message checkbox only
        counts while it is on screen (isVisible): the hidden ones are the
        messages that cannot occur under the ticked sources and states."""
        sources = self._checkboxes["source"]
        states = self._checkboxes["state"]
        messages = self._checkboxes["message"]
        message_filter_active = any(cb.isVisible() for cb in messages.values())
        return FilterSelection(
            sources=frozenset(k for k, cb in sources.items() if cb.isChecked()) if sources else None,
            states=frozenset(k for k, cb in states.items() if cb.isChecked()) if states else None,
            messages=(
                frozenset(k for k, cb in messages.items() if cb.isChecked() and cb.isVisible())
                if message_filter_active else None
            ),
        )

    def _collect_base_filtered_rows(self) -> list[tuple[LogEvent, str]]:
        if not self.filters_loaded:
            return list(zip(self._events, self._display_rows))
        return collect_base_filtered_rows(
            self._events, self._display_rows,
            self._source_keys, self._state_keys, self._message_keys,
            self._current_selection(),
        )

    def _collect_preset_filtered_rows(self) -> list[tuple[LogEvent, str]]:
        presets = getattr(self._settings, "filter_presets", [])
        if not presets or not self.active_filter_presets:
            return list(zip(self._events, self._display_rows))
        active = [presets[i] for i in sorted(self.active_filter_presets) if i < len(presets)]
        return collect_preset_filtered_rows(
            self._events, self._display_rows,
            self._source_keys, self._state_keys, self._message_keys,
            active,
        )

    # ---- checkbox columns -------------------------------------------------

    def _remember_unchecked_filters(self) -> None:
        """Snapshot what the user has unticked before the panels are
        rebuilt. Empty dicts mean nothing was built - keep the old memory."""
        for kind in FILTER_KINDS:
            boxes = self._checkboxes[kind]
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

    def clear_filter_checkboxes(self, show_busy: bool = True) -> None:
        self._remember_unchecked_filters()
        for kind in FILTER_KINDS:
            if show_busy:
                self.busy_changed.emit(True, f"Resetting {kind} filters...")
            self._reset_filter_panel(kind)
            if show_busy:
                QApplication.processEvents()
        for kind in FILTER_KINDS:
            self._checkboxes[kind].clear()
        if show_busy:
            self.busy_changed.emit(False, "")

    def _reset_filter_state(self, show_busy: bool = False) -> None:
        if self.filters_loaded:
            self._filters_wanted = True
        self.filters_loaded = False
        self.clear_filter_checkboxes(show_busy=show_busy)
        self._checkbox_panel.setVisible(False)

    def _reset_filter_panel(self, kind: str) -> None:
        print(f"[viewer] resetting {kind} panel", flush=True)
        self._columns[kind].reset()

    def _column_keys(self, kind: str) -> list[str]:
        """The sorted checkbox keys for a column: sources and messages skip
        empty values, states list them as NULL_STATE."""
        if kind == "source":
            return sorted({k for k in self._source_keys if k})
        if kind == "state":
            return sorted({state_key(k) for k in self._state_keys})
        return sorted({k for k in self._message_keys if k})

    def build_filter_checkboxes(self) -> None:
        if not self.filters_loaded:
            return
        self.clear_filter_checkboxes()
        for kind in FILTER_KINDS:
            self._checkboxes[kind] = self._columns[kind].add_checkboxes(
                self._column_keys(kind),
                self._remembered_unchecked[kind],
                lambda _state, k=kind: self._on_checkbox_changed(k),
            )
        self.update_message_visibility_from_filters()

    def load_filters_panel(self) -> None:
        if not self._events:
            QMessageBox.information(self, "No logs", "Load a video/logs before loading filters.")
            return
        if self.filters_loaded:
            QMessageBox.information(self, "Filters already loaded", "Filters are already available.")
            return
        self.filters_loaded = True
        self.busy_changed.emit(True, "Resetting filters...")
        self.clear_filter_checkboxes(show_busy=False)
        self.busy_changed.emit(True, "Building filter lists...")
        self.build_filter_checkboxes()
        self.busy_changed.emit(True, "Applying filters and refreshing log list...")
        self.apply_filters(status_message="Applying filters...", manage_busy=False)
        self.busy_changed.emit(False, "")
        self._checkbox_panel.setVisible(True)
        print(
            "[viewer] filter checkboxes built ("
            + ", ".join(f"{kind}s={len(self._checkboxes[kind])}" for kind in FILTER_KINDS)
            + ")",
            flush=True,
        )

    def update_message_visibility_from_filters(self) -> None:
        """Hide the state and message checkboxes that cannot occur under
        the ticked sources (and, for messages, the ticked states)."""
        sources = self._checkboxes["source"]
        states = self._checkboxes["state"]
        messages = self._checkboxes["message"]
        if not self.filters_loaded or not self._events or not messages or not states:
            return
        states_used, messages_used = visible_state_and_message_keys(
            self._source_keys, self._state_keys, self._message_keys,
            {k for k, cb in sources.items() if cb.isChecked()} if sources else None,
            {k for k, cb in states.items() if cb.isChecked()} if states else None,
        )
        for state_val, cb in states.items():
            cb.setVisible(state_val == NULL_STATE or state_val in states_used)
        for msg_val, cb in messages.items():
            cb.setVisible(msg_val in messages_used)

    def _on_checkbox_changed(self, kind: str) -> None:
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        if kind != "message":
            # Message ticks do not change which messages are possible.
            self.update_message_visibility_from_filters()
        self.apply_filters()

    def _tick_group(self, kind: str, checked: bool) -> None:
        """Tick or untick every checkbox of a column without firing its
        change handler."""
        for cb in list(self._checkboxes[kind].values()):
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)

    def _set_group_checked(self, kind: str, checked: bool) -> None:
        """The All / None buttons of a column."""
        if not self.filters_loaded:
            return
        self._tick_group(kind, checked)
        if kind != "message":
            self.update_message_visibility_from_filters()
        self.apply_filters()

    def _set_all_filters_checked(self) -> None:
        for kind in FILTER_KINDS:
            self._tick_group(kind, True)
        self.update_message_visibility_from_filters()

    # ---- presets ----------------------------------------------------------

    def _clear_active_filter_preset(self) -> None:
        if self.active_filter_preset_index is None and not self.active_filter_presets:
            return
        for btn in self.filter_preset_group:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        self.active_filter_preset_index = None
        self.active_filter_presets.clear()

    def _on_filter_preset_clicked(self, index: int) -> None:
        if index < 0 or index >= len(self.filter_preset_group):
            return
        btn = self.filter_preset_group[index]
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            # Ctrl-click adds to / removes from the active set.
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

    def _apply_filter_preset(self, index: int) -> None:
        """Tick the checkboxes the preset saved (building them first if needed)."""
        if not self._events:
            return
        if not self.filters_loaded:
            self.load_filters_panel()
        if not self.filters_loaded:
            return
        presets = getattr(self._settings, "filter_presets", [])
        if index < 0 or index >= len(presets):
            return
        preset = presets[index]
        for kind, allowed in (
            ("source", preset.sources),
            ("state", preset.states),
            ("message", preset.messages),
        ):
            for key, cb in self._checkboxes[kind].items():
                cb.blockSignals(True)
                cb.setChecked(key in allowed)
                cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def _on_filter_preset_menu(self, index: int) -> None:
        if index < 0 or index >= len(self.filter_preset_group):
            return
        btn = self.filter_preset_group[index]
        menu = QMenu(self)
        save_action = menu.addAction("Save current selection")
        rename_action = menu.addAction("Rename")
        chosen = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen == rename_action:
            text, ok = QInputDialog.getText(self, "Rename preset", "Preset name:", text=btn.text())
            if ok and text.strip():
                btn.setText(text.strip())
                self._save_filter_preset_settings()
        elif chosen == save_action:
            self._save_current_filter_selection(index)

    def _save_current_filter_selection(self, index: int) -> None:
        if not self.filters_loaded:
            return
        if index < 0 or index >= len(self.filter_preset_group):
            return
        sources = [k for k, cb in self._checkboxes["source"].items() if cb.isChecked()]
        states = [k for k, cb in self._checkboxes["state"].items() if cb.isChecked()]
        messages = [k for k, cb in self._checkboxes["message"].items() if cb.isChecked() and cb.isVisible()]
        presets = getattr(self._settings, "filter_presets", [])
        while len(presets) < PRESET_COUNT:
            presets.append(FilterPreset(name=f"Preset {len(presets) + 1}"))
        presets[index] = FilterPreset(
            name=self.filter_preset_group[index].text(),
            sources=sources,
            states=states,
            messages=messages,
        )
        self._settings.filter_presets = presets
        self._settings.save()

    # ---- custom filters ---------------------------------------------------

    def _get_active_custom_filters(self) -> list[CustomFilter]:
        filters: list[CustomFilter] = []
        for btn, in_edit, out_edit, _count_label in self.custom_filter_blocks:
            if not btn.isChecked():
                continue
            in_terms = parse_custom_terms(in_edit.text())
            out_terms = parse_custom_terms(out_edit.text())
            if not in_terms and not out_terms:
                continue
            filters.append((in_terms, out_terms))
        return filters

    def _on_custom_filter_changed(self, *_args) -> None:
        self.update_tab_highlights()
        if not self._events:
            return
        self.apply_filters()

    def _on_custom_filter_text_changed(self, button: QPushButton) -> None:
        if not self._events:
            return
        if not button.isChecked():
            return
        self._filter_debounce_timer.start()

    def _validate_custom_filter_inputs(self) -> None:
        """Amber border on an entry with an empty term (leading, trailing
        or doubled comma)."""
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

    def _on_custom_filter_menu(self, index: int) -> None:
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

    def _update_custom_filter_counts(self) -> None:
        """"Matches: N" under each block: how many of the checkbox-filtered
        rows that block alone would keep."""
        if not self._events:
            for _btn, _in_edit, _out_edit, count_label in self.custom_filter_blocks:
                count_label.setText("Matches: -")
            return
        base_rows = self._collect_base_filtered_rows()
        for btn, in_edit, out_edit, count_label in self.custom_filter_blocks:
            in_terms = parse_custom_terms(in_edit.text())
            out_terms = parse_custom_terms(out_edit.text())
            if not in_terms and not out_terms:
                count_label.setText("Matches: 0")
                continue
            custom_filters = [(in_terms, out_terms)]
            match_count = sum(
                1 for _ev, row_text in base_rows
                if custom_filter_match(row_match_text(row_text), custom_filters, CUSTOM_FILTER_MODE)
            )
            count_label.setText(f"Matches: {match_count}")

    # ---- persistence ------------------------------------------------------

    def reload_from_settings(self) -> None:
        """Re-read the preset names and the custom filters from Settings
        (construction, and after a settings import)."""
        self._load_custom_filter_settings()
        self._load_filter_preset_settings()

    def _load_custom_filter_settings(self) -> None:
        presets = getattr(self._settings, "custom_filters", [])
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
        self.update_tab_highlights()

    def _save_custom_filter_settings(self) -> None:
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
        self._settings.custom_filters = presets
        self._settings.save()

    def _load_filter_preset_settings(self) -> None:
        presets = getattr(self._settings, "filter_presets", [])
        if not presets:
            return
        for preset, btn in zip(presets, self.filter_preset_group):
            if preset.name:
                btn.setText(preset.name)

    def _save_filter_preset_settings(self) -> None:
        presets: list[FilterPreset] = []
        for idx, btn in enumerate(self.filter_preset_group):
            if idx < len(self._settings.filter_presets):
                existing = self._settings.filter_presets[idx]
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
        self._settings.filter_presets = presets
        self._settings.save()
