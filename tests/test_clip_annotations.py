"""ui/clip_annotations.py: the files and the set (no Qt), then the
ClipAnnotations controller offscreen against a temp annotations folder."""
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from logfather.ui.clip_annotations import (
    DEFAULT_COLOR,
    DEFAULT_TOOL,
    TOOLS,
    AnnotationSet,
    AnnotationStore,
    ClipAnnotations,
    parse_distance,
    read_annotations,
    write_annotations,
)


# --- files ---------------------------------------------------------------------

def test_read_annotations_keeps_only_dict_items(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"annotations": [{"type": "line"}, "junk", 3, {"type": "text"}]}), encoding="utf-8")
    assert read_annotations(path) == [{"type": "line"}, {"type": "text"}]


def test_read_annotations_is_empty_for_missing_bad_or_odd_files(tmp_path):
    assert read_annotations(tmp_path / "missing.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_annotations(bad) == []
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"annotations": {"type": "line"}}), encoding="utf-8")
    assert read_annotations(odd) == []


def test_write_annotations_round_trips_and_never_raises(tmp_path):
    path = tmp_path / "a.json"
    assert write_annotations(path, [{"type": "line", "pinned": True}])
    assert json.loads(path.read_text(encoding="utf-8")) == {"annotations": [{"type": "line", "pinned": True}]}
    assert not write_annotations(tmp_path / "no" / "dir" / "a.json", [])


def test_store_names_the_files_after_the_cache_copy(tmp_path):
    store = AnnotationStore(lambda: tmp_path)
    assert store.pinned_path == tmp_path / "pinned.json"
    cache_copy = Path("C:/cache/PikPak012_20260914120000_ab12cd34.mp4")
    assert store.clip_path(cache_copy) == tmp_path / "PikPak012_20260914120000_ab12cd34.json"
    store.save_clip(cache_copy, [{"type": "arrow"}])
    store.save_pinned([{"type": "text", "pinned": True}])
    assert store.load_clip(cache_copy) == [{"type": "arrow"}]
    assert store.load_pinned() == [{"type": "text", "pinned": True}]


def test_parse_distance():
    assert parse_distance("1.25") == 1.25
    assert parse_distance(" 3 m ") == 3.0
    assert parse_distance("two") is None
    assert parse_distance("") is None


# --- the set -------------------------------------------------------------------

def test_pinned_come_first_and_add_sorts_by_pin_flag():
    s = AnnotationSet()
    s.add({"type": "line"})
    s.add({"type": "text", "pinned": True})
    assert s.all() == [{"type": "text", "pinned": True}, {"type": "line"}]
    assert s.history == [{"type": "line"}, {"type": "text", "pinned": True}]


def test_undo_takes_back_the_latest_addition_from_its_list():
    s = AnnotationSet()
    a, b = {"type": "line", "id": 1}, {"type": "text", "pinned": True, "id": 2}
    s.add(a)
    s.add(b)
    assert s.undo() is b and s.pinned == [] and s.clip == [a]
    assert s.undo() is a and s.clip == []
    assert s.undo() is None


def test_loading_a_clip_makes_everything_undoable_and_reset_clears():
    s = AnnotationSet(pinned=[{"type": "text", "pinned": True}])
    s.set_clip([{"type": "line"}])
    assert s.history == [{"type": "text", "pinned": True}, {"type": "line"}]
    s.reset_clip()
    assert s.clip == [] and s.history == [] and s.pinned == [{"type": "text", "pinned": True}]


def test_delete_removes_from_every_list():
    s = AnnotationSet()
    a = {"type": "line"}
    s.add(a)
    s.history.append(a)  # a stale duplicate on the undo stack
    s.delete(a)
    assert s.clip == [] and s.history == []


def test_clear_clip_keeps_the_pinned_history():
    s = AnnotationSet()
    p = {"type": "text", "pinned": True}
    s.add(p)
    s.add({"type": "line"})
    assert s.clear_clip()
    assert s.clip == [] and s.pinned == [p] and s.history == [p]
    assert not s.clear_clip()


def test_toggle_pin_moves_between_the_lists():
    s = AnnotationSet()
    a = {"type": "line"}
    s.add(a)
    s.toggle_pin(a)
    assert a["pinned"] is True and s.pinned == [a] and s.clip == []
    s.toggle_pin(a)
    assert a["pinned"] is False and s.pinned == [] and s.clip == [a]


def test_toggle_frame_pin_and_set_distance():
    a = {"type": "timed_line"}
    AnnotationSet.toggle_frame_pin(a, 12)
    assert a["frame_index"] == 12
    AnnotationSet.toggle_frame_pin(a, 12)
    assert "frame_index" not in a
    AnnotationSet.set_distance(a, "2.5")
    assert a["distance_m"] == 2.5
    AnnotationSet.set_distance(a, "abc")
    assert a["distance_m"] is None
    AnnotationSet.set_distance(a, "  ")
    assert "distance_m" not in a


# --- the controller ---------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ctl(app, tmp_path):
    parent = QWidget()
    store = AnnotationStore(lambda: tmp_path)
    c = ClipAnnotations(store, dialog_parent=parent, current_frame=lambda: 42, popout_label=lambda: None)
    yield c
    parent.close()
    parent.deleteLater()


CLIP = Path("Z:/public/PikPak012/2026/09/14/20260914120000.mp4")
CACHE = Path("C:/cache/20260914120000_ab12cd34.mp4")


def test_defaults(ctl):
    assert ctl.tool == DEFAULT_TOOL == "line"
    assert ctl.color == QColor(DEFAULT_COLOR) == QColor("#ffcc00")
    assert ctl.annotations() == [] and not ctl.has_clip_annotations()


def test_load_clip_reads_the_file_and_reports_status(ctl, tmp_path):
    ctl.store.save_clip(CACHE, [{"type": "line"}])
    ctl.store.save_pinned([{"type": "text", "pinned": True}])
    ctl.load_pinned()
    changed, status = [], []
    ctl.changed.connect(lambda: changed.append(True))
    ctl.clip_status_changed.connect(lambda p, has: status.append((p, has)))
    ctl.load_clip(CLIP, CACHE)
    assert ctl.annotations() == [{"type": "text", "pinned": True}, {"type": "line"}]
    assert changed and status == [(CLIP, True)]
    assert ctl.items.history == ctl.annotations()


def test_load_clip_without_a_file_starts_empty(ctl):
    status = []
    ctl.clip_status_changed.connect(lambda p, has: status.append((p, has)))
    ctl.load_clip(CLIP, CACHE)
    assert ctl.annotations() == [] and status == [(CLIP, False)]
    ctl.load_clip(None, None)
    assert status == [(CLIP, False)]  # no path, no status


def test_add_saves_both_files_and_signals(ctl, tmp_path):
    ctl.load_clip(CLIP, CACHE)
    changed, status = [], []
    ctl.changed.connect(lambda: changed.append(True))
    ctl.clip_status_changed.connect(lambda p, has: status.append(has))
    ctl.add({"type": "line"})
    ctl.add({"type": "text", "pinned": True})
    assert ctl.store.load_clip(CACHE) == [{"type": "line"}]
    assert ctl.store.load_pinned() == [{"type": "text", "pinned": True}]
    assert len(changed) == 2 and status == [True, True]
    ctl.undo()
    ctl.undo()
    assert ctl.store.load_clip(CACHE) == [] and ctl.store.load_pinned() == []
    assert status[-1] is False


def test_clear_for_new_clip_stops_saving_until_the_next_load(ctl):
    ctl.load_clip(CLIP, CACHE)
    ctl.add({"type": "line"})
    ctl.clear_for_new_clip()
    assert ctl.annotations() == []
    ctl.add({"type": "arrow"})  # nowhere to save the clip set yet
    assert ctl.store.load_clip(CACHE) == [{"type": "line"}]


def test_clear_clip_keeps_pinned_and_rewrites_the_clip_file(ctl):
    ctl.load_clip(CLIP, CACHE)
    ctl.add({"type": "text", "pinned": True})
    ctl.add({"type": "line"})
    ctl.clear_clip()
    assert ctl.annotations() == [{"type": "text", "pinned": True}]
    assert ctl.store.load_clip(CACHE) == []


def test_tool_and_color_signal_and_restyle_the_toolbar(ctl, app):
    tools, colors = [], []
    ctl.tool_changed.connect(tools.append)
    ctl.color_changed.connect(colors.append)
    host = QWidget()
    toolbar = ctl.build_toolbar(host)
    buttons = toolbar.tool_group.buttons()
    assert [b.text() for b in buttons] == [label for _key, label in TOOLS]
    assert buttons[0].isChecked()  # "Line" is the default tool
    buttons[1].click()
    assert ctl.tool == "arrow" and tools == ["arrow"]
    ctl.set_color(QColor("#00ff00"))
    assert colors == [QColor("#00ff00")]
    assert "#00ff00" in toolbar.color_button.styleSheet()
    ctl.drop_toolbar()
    ctl.set_color(QColor("#0000ff"))  # no toolbar to restyle; still signals
    assert colors[-1] == QColor("#0000ff")
    host.close()
