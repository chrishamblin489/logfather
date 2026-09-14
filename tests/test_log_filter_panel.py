"""The replay's log filters (ui/log_filter_panel.py).

The first half covers the module-level helpers with plain lists (no Qt);
the second drives a LogFilterPanel offscreen with a stand-in Settings to
check the checkbox columns, the All / None buttons, the presets and the
custom filters end to end.
"""
from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass

import pytest

from logfather.data.settings_store import CustomFilterPreset, FilterPreset
from logfather.ui import log_filter_panel as lfp
from logfather.ui.log_filter_panel import (
    CUSTOM_FILTER_MODE,
    NULL_STATE,
    FilterSelection,
    collect_base_filtered_rows,
    collect_preset_filtered_rows,
    custom_filter_match,
    parse_custom_terms,
    row_match_text,
    state_key,
    visible_state_and_message_keys,
)


@dataclass
class Ev:
    """Stand-in for LogEvent: the filters pass events through untouched."""
    n: int


# One row per event: (source, state, message, display row).
ROWS = [
    ("robot", "RUNNING", "Target queued", "10:00:00.000  |  robot  |  RUNNING  |  Target queued"),
    ("robot", "", "Gripper fault", "10:00:01.000  |  robot  |  |  Gripper fault"),
    ("vision", "IDLE", "Frame dropped", "10:00:02.000  |  vision  |  IDLE  |  Frame dropped"),
    ("vision", "RUNNING", "Target queued", "10:00:03.000  |  vision  |  RUNNING  |  Target queued"),
    ("plc", "STOPPED", "E-stop pressed", "10:00:04.000  |  plc  |  STOPPED  |  E-stop pressed"),
]
EVENTS = [Ev(i) for i in range(len(ROWS))]
DISPLAY = [r[3] for r in ROWS]
SOURCES = [r[0] for r in ROWS]
STATES = [r[1] for r in ROWS]
MESSAGES = [r[2] for r in ROWS]


def _passing(rows):
    return [ev.n for ev, _row in rows]


# ---------------------------------------------------------------------------
# Term parsing and matching
# ---------------------------------------------------------------------------

def test_parse_custom_terms_strips_lowercases_and_drops_empties():
    assert parse_custom_terms(" Gripper , FAULT ,, ,e-stop, ") == ["gripper", "fault", "e-stop"]
    assert parse_custom_terms("") == []
    assert parse_custom_terms(" , , ") == []


def test_row_match_text_skips_the_timecode():
    assert row_match_text("10:00:00.000  |  robot  |  RUNNING  |  x") == "robot  |  RUNNING  |  x"
    assert row_match_text("no separator here") == "no separator here"


def test_state_key_maps_empty_state_to_null():
    assert state_key("") == NULL_STATE
    assert state_key(None) == NULL_STATE
    assert state_key("RUNNING") == "RUNNING"


def test_custom_filter_match_no_filters_passes_everything():
    assert custom_filter_match("anything", []) is True


def test_custom_filter_match_or_mode_needs_any_in_term():
    filters = [(["gripper", "e-stop"], [])]
    assert custom_filter_match("Gripper fault", filters, "OR") is True
    assert custom_filter_match("E-STOP pressed", filters, "OR") is True
    assert custom_filter_match("Frame dropped", filters, "OR") is False


def test_custom_filter_match_out_terms_exclude():
    filters = [(["target"], ["queued"])]
    assert custom_filter_match("Target picked", filters, "OR") is True
    assert custom_filter_match("Target queued", filters, "OR") is False
    # Filter-out on its own (no filter-in) keeps everything else.
    assert custom_filter_match("Frame dropped", [([], ["fault"])], "OR") is True
    assert custom_filter_match("Gripper fault", [([], ["fault"])], "OR") is False


def test_custom_filter_match_blocks_are_ored_together():
    filters = [(["gripper"], []), (["frame"], ["dropped"])]
    assert custom_filter_match("Gripper fault", filters, "OR") is True
    assert custom_filter_match("Frame ok", filters, "OR") is True
    assert custom_filter_match("Frame dropped", filters, "OR") is False


def test_custom_filter_match_and_mode_needs_every_in_term():
    """The AND branch works when asked for directly."""
    filters = [(["gripper", "fault"], [])]
    assert custom_filter_match("Gripper fault", filters, "AND") is True
    assert custom_filter_match("Gripper ok", filters, "AND") is False
    assert custom_filter_match("Gripper ok", filters, "OR") is True
    assert custom_filter_match("Gripper fault", [(["gripper", "fault"], ["fault"])], "AND") is False


def test_and_mode_is_not_reachable_from_the_panel():
    """The old review asked whether the AND branch is reachable. It is
    only by calling custom_filter_match directly: the panel runs every
    match with CUSTOM_FILTER_MODE, and that is "OR"."""
    assert CUSTOM_FILTER_MODE == "OR"
    source = inspect.getsource(lfp)
    calls = [m for m in re.finditer(r"custom_filter_match\(", source)]
    assert len(calls) >= 3  # the def plus the two panel call sites
    call_sites = [source[m.end():m.end() + 120] for m in calls[1:]]
    assert call_sites, "the panel should call custom_filter_match"
    for snippet in call_sites:
        assert "CUSTOM_FILTER_MODE" in snippet, snippet
        assert '"AND"' not in snippet


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------

def _base(selection):
    return _passing(collect_base_filtered_rows(EVENTS, DISPLAY, SOURCES, STATES, MESSAGES, selection))


def test_base_rows_pass_everything_when_no_column_filters():
    assert _base(FilterSelection()) == [0, 1, 2, 3, 4]


def test_base_rows_filter_by_source():
    assert _base(FilterSelection(sources=frozenset({"robot"}))) == [0, 1]
    assert _base(FilterSelection(sources=frozenset())) == []


def test_base_rows_filter_by_state_with_null_key():
    assert _base(FilterSelection(states=frozenset({NULL_STATE}))) == [1]
    assert _base(FilterSelection(states=frozenset({"RUNNING", "IDLE"}))) == [0, 2, 3]


def test_base_rows_filter_by_message_and_combine_columns():
    assert _base(FilterSelection(messages=frozenset({"Target queued"}))) == [0, 3]
    assert _base(FilterSelection(sources=frozenset({"vision"}), messages=frozenset({"Target queued"}))) == [3]


def test_base_rows_keep_event_and_row_pairing():
    rows = collect_base_filtered_rows(EVENTS, DISPLAY, SOURCES, STATES, MESSAGES, FilterSelection(sources=frozenset({"plc"})))
    assert rows == [(EVENTS[4], DISPLAY[4])]


def _preset(presets):
    return _passing(collect_preset_filtered_rows(EVENTS, DISPLAY, SOURCES, STATES, MESSAGES, presets))


def test_preset_rows_match_any_active_preset():
    robot = FilterPreset(name="robot", sources=["robot"])
    stopped = FilterPreset(name="stopped", states=["STOPPED"])
    assert _preset([robot]) == [0, 1]
    assert _preset([robot, stopped]) == [0, 1, 4]


def test_preset_rows_constrain_by_every_non_empty_list():
    p = FilterPreset(name="p", sources=["robot", "vision"], states=["RUNNING"], messages=["Target queued"])
    assert _preset([p]) == [0, 3]
    p2 = FilterPreset(name="p2", sources=["robot"], states=[NULL_STATE])
    assert _preset([p2]) == [1]


def test_empty_preset_matches_nothing():
    assert _preset([FilterPreset(name="unsaved")]) == []
    assert _preset([]) == []


def test_visible_state_and_message_keys_follow_ticked_sources_and_states():
    states, messages = visible_state_and_message_keys(SOURCES, STATES, MESSAGES, None, None)
    assert states == {"RUNNING", NULL_STATE, "IDLE", "STOPPED"}
    assert messages == {"Target queued", "Gripper fault", "Frame dropped", "E-stop pressed"}
    states, messages = visible_state_and_message_keys(SOURCES, STATES, MESSAGES, {"vision"}, None)
    assert states == {"IDLE", "RUNNING"}
    assert messages == {"Frame dropped", "Target queued"}
    states, messages = visible_state_and_message_keys(SOURCES, STATES, MESSAGES, {"vision"}, {"IDLE"})
    assert states == {"IDLE", "RUNNING"}  # states are listed under the sources alone
    assert messages == {"Frame dropped"}


# ---------------------------------------------------------------------------
# The panel (offscreen Qt)
# ---------------------------------------------------------------------------

class FakeSettings:
    def __init__(self):
        self.filter_presets = [FilterPreset(name=f"Preset {i + 1}") for i in range(15)]
        self.custom_filters = [CustomFilterPreset(name=f"Preset {i + 1}") for i in range(5)]
        self.saves = 0

    def save(self):
        self.saves += 1


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    from PySide6.QtWidgets import QTabWidget

    settings = FakeSettings()
    p = lfp.LogFilterPanel(settings)
    tabs = QTabWidget()
    p.add_to_tabs(tabs)
    tabs.setCurrentWidget(p)
    tabs.show()  # isVisible() on the message checkboxes needs the tab on screen
    changed: list[int] = []
    busy: list[tuple[bool, str]] = []
    p.filters_changed.connect(lambda: changed.append(1))
    p.busy_changed.connect(lambda b, m: busy.append((b, m)))
    p.set_events(EVENTS, DISPLAY, SOURCES, STATES, MESSAGES)
    p.load_filters_panel()
    qapp.processEvents()
    p._settings = settings
    p.test_changed = changed
    p.test_busy = busy
    yield p
    tabs.close()
    tabs.deleteLater()
    qapp.processEvents()


def test_panel_builds_sorted_checkbox_columns(panel):
    assert panel.filters_loaded is True
    assert list(panel._checkboxes["source"]) == ["plc", "robot", "vision"]
    assert list(panel._checkboxes["state"]) == [NULL_STATE, "IDLE", "RUNNING", "STOPPED"]
    assert list(panel._checkboxes["message"]) == ["E-stop pressed", "Frame dropped", "Gripper fault", "Target queued"]
    assert all(cb.isChecked() for kind in lfp.FILTER_KINDS for cb in panel._checkboxes[kind].values())
    assert _passing(panel.filtered_rows()) == [0, 1, 2, 3, 4]
    # load_filters_panel applied once and framed it with the busy dialog.
    assert panel.test_changed == [1]
    assert panel.test_busy[0] == (True, "Resetting filters...")
    assert panel.test_busy[-1] == (False, "")


def test_panel_none_and_all_buttons_refilter(panel):
    panel._columns["source"].none_btn.click()
    assert _passing(panel.filtered_rows()) == []
    panel._checkboxes["source"]["vision"].setChecked(True)
    assert _passing(panel.filtered_rows()) == [2, 3]
    # Only the states and messages vision uses stay visible.
    assert panel._checkboxes["state"]["STOPPED"].isHidden()
    assert not panel._checkboxes["state"]["IDLE"].isHidden()
    assert panel._checkboxes["message"]["E-stop pressed"].isHidden()
    panel._columns["source"].all_btn.click()
    assert _passing(panel.filtered_rows()) == [0, 1, 2, 3, 4]
    assert not panel._checkboxes["message"]["E-stop pressed"].isHidden()


def test_panel_message_none_narrows_without_hiding(panel):
    """The messages All / None pair never re-ran the visibility pass
    (message ticks cannot change which messages are possible)."""
    panel._columns["message"].none_btn.click()
    assert _passing(panel.filtered_rows()) == []
    assert not any(cb.isHidden() for cb in panel._checkboxes["message"].values())
    panel._checkboxes["message"]["Gripper fault"].setChecked(True)
    assert _passing(panel.filtered_rows()) == [1]


def test_panel_remembers_unticked_keys_across_a_reload(panel, qapp):
    panel._checkboxes["source"]["plc"].setChecked(False)
    assert _passing(panel.filtered_rows()) == [0, 1, 2, 3]
    panel.set_events(EVENTS, DISPLAY, SOURCES, STATES, MESSAGES)
    assert panel.filters_loaded is False
    assert _passing(panel.filtered_rows()) == [0, 1, 2, 3, 4]  # nothing built: everything passes
    panel.reload_filters_if_wanted()
    qapp.processEvents()
    assert panel.filters_loaded is True
    assert not panel._checkboxes["source"]["plc"].isChecked()
    assert _passing(panel.filtered_rows()) == [0, 1, 2, 3]


def test_panel_saves_and_applies_a_preset(panel):
    settings = panel._settings
    panel._columns["source"].none_btn.click()
    panel._checkboxes["source"]["plc"].setChecked(True)
    panel._save_current_filter_selection(2)
    saved = settings.filter_presets[2]
    assert saved.sources == ["plc"]
    assert saved.states == [NULL_STATE, "IDLE", "RUNNING", "STOPPED"]
    assert saved.messages == ["E-stop pressed"]  # only the visible ones
    assert settings.saves == 1

    panel._columns["source"].all_btn.click()
    assert _passing(panel.filtered_rows()) == [0, 1, 2, 3, 4]
    panel.filter_preset_group[2].click()
    assert panel.active_filter_presets == {2}
    assert _passing(panel.filtered_rows()) == [4]
    assert not panel._checkboxes["source"]["robot"].isChecked()
    # Ticking a checkbox by hand drops the preset again.
    panel._checkboxes["source"]["robot"].setChecked(True)
    assert panel.active_filter_presets == set()
    assert not panel.filter_preset_group[2].isChecked()


def test_panel_custom_filter_block_narrows_and_counts(panel, qapp):
    btn, in_edit, out_edit, count_label = panel.custom_filter_blocks[0]
    in_edit.setText("target, e-stop")
    out_edit.setText("vision")
    # Typing into a disabled block does not re-filter: the count from the
    # last apply (empty terms -> 0) stands until the block is enabled.
    assert count_label.text() == "Matches: 0"
    btn.setChecked(True)  # toggled -> apply
    assert _passing(panel.filtered_rows()) == [0, 4]
    assert count_label.text() == "Matches: 2"
    in_edit.setText("gripper")
    panel._filter_debounce_timer.stop()
    panel.apply_filters()
    assert _passing(panel.filtered_rows()) == [1]


def test_panel_persists_custom_filters_and_reloads_them(panel):
    settings = panel._settings
    btn, in_edit, out_edit, _count = panel.custom_filter_blocks[1]
    in_edit.setText("frame")
    btn.setChecked(True)
    panel._save_custom_filter_settings()
    assert settings.custom_filters[1] == CustomFilterPreset(name="Preset 2", filter_in="frame", filter_out="", enabled=True)
    settings.custom_filters[1].name = "Vision"
    panel.reload_from_settings()
    assert btn.text() == "Vision"
    assert btn.isChecked()
    assert _passing(panel.filtered_rows()) == [2]
