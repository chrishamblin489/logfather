"""ui/progress.py: the three progress-dialog helpers, offscreen."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from logfather.ui.progress import BusyDialog, JobProgress, StageProgress, job_progress


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def parent(app):
    w = QWidget()
    yield w
    w.close()
    w.deleteLater()


# --- BusyDialog -----------------------------------------------------------

def test_busy_dialog_show_creates_indeterminate_no_cancel_dialog(parent):
    busy = BusyDialog(parent, "Loading timeline", default_message="Loading...")
    assert not busy.is_shown()
    busy.show()
    dlg = busy.dialog
    assert dlg is not None and busy.is_shown()
    assert dlg.windowTitle() == "Loading timeline"
    assert dlg.labelText() == "Loading..."
    assert (dlg.minimum(), dlg.maximum()) == (0, 0)
    assert dlg.isVisible()
    busy.hide()


def test_busy_dialog_show_twice_reuses_and_relabels(parent):
    busy = BusyDialog(parent, "Log Viewer")
    busy.show("Fetching Elastic logs...")
    first = busy.dialog
    busy.show("Still fetching...")
    assert busy.dialog is first
    assert first.labelText() == "Still fetching..."
    busy.hide()


def test_busy_dialog_hide_is_idempotent(parent):
    busy = BusyDialog(parent, "Loading clip")
    busy.hide()  # never shown: nothing to do
    busy.show("Downloading...")
    dlg = busy.dialog
    busy.hide()
    assert busy.dialog is None and not busy.is_shown()
    assert not dlg.isVisible()
    busy.hide()  # second hide is a no-op
    assert busy.dialog is None


def test_busy_dialog_set_progress_turns_spinner_into_bar(parent):
    busy = BusyDialog(parent, "Loading clip")
    busy.set_progress(5, 10, "ignored while hidden")  # no dialog: no-op
    busy.show("Downloading...")
    busy.set_progress(250, 1000, "25% of 1 MB")
    dlg = busy.dialog
    assert (dlg.minimum(), dlg.maximum()) == (0, 1000)
    assert dlg.value() == 250
    assert dlg.labelText() == "25% of 1 MB"
    busy.set_progress(3, 0)  # unknown size: label/range untouched
    assert dlg.value() == 250
    busy.hide()


# --- StageProgress --------------------------------------------------------

def test_stage_progress_without_parent_creates_nothing(app):
    progress = StageProgress(None, "OCR Analysis")
    progress.begin("Analyzing first 10s...", 42)
    assert progress.dialog is None
    progress.set(7)
    assert progress.was_cancelled() is False
    progress.close()


def test_stage_progress_without_parent_honours_should_abort(app):
    flags = {"abort": False}
    progress = StageProgress(None, "OCR Analysis", should_abort=lambda: flags["abort"])
    progress.begin("Scanning...", 3)
    assert progress.was_cancelled() is False
    flags["abort"] = True
    assert progress.was_cancelled() is True


def test_stage_progress_with_parent_shows_and_tracks(parent):
    progress = StageProgress(parent, "Camera date")
    progress.begin("Comparing displayed date to filename date...", 100)
    dlg = progress.dialog
    assert dlg is not None
    assert dlg.windowTitle() == "Camera date"
    assert dlg.labelText() == "Comparing displayed date to filename date..."
    assert dlg.maximum() == 100
    assert dlg.isVisible()
    progress.set(40)
    assert dlg.value() == 40
    assert progress.was_cancelled() is False
    progress.set(500)  # clamped to the maximum, which auto-resets (hides) the dialog
    assert not dlg.isVisible()
    progress.close()
    assert progress.dialog is None
    progress.close()  # idempotent


def test_stage_progress_cancel_reports_once(parent):
    seen = []
    progress = StageProgress(parent, "OCR Analysis", on_cancel=lambda: seen.append(1))
    progress.begin("Analyzing...", 10)
    progress.dialog.cancel()
    assert progress.was_cancelled() is True
    assert progress.was_cancelled() is True
    assert seen == [1]
    progress.close()


def test_stage_progress_begin_twice_relabels(parent):
    progress = StageProgress(parent, "Readings")
    progress.begin("first", 5)
    dlg = progress.dialog
    progress.begin("second", 9)
    assert progress.dialog is dlg
    assert dlg.labelText() == "second" and dlg.maximum() == 9 and dlg.value() == 0
    progress.close()


# --- job_progress ---------------------------------------------------------

class FakeSlot:
    def __init__(self):
        self.started = None
        self.retired = 0

    def start(self, fn, on_result=None, on_error=None, on_progress=None, on_finished=None):
        self.started = {"fn": fn, "on_result": on_result, "on_error": on_error, "on_progress": on_progress}
        return "job"

    def retire(self):
        self.retired += 1


def _stop_report_parse(payload):
    phase, done, total = payload
    label = "Copying report clips..." if phase == "copies" else "Reading stop thumbnails..."
    return label, done, total


def test_job_progress_cancel_retires_slot_and_closes(parent):
    slot = FakeSlot()
    done = []
    jp = job_progress(parent, "Stop Report", slot, label="Building stop report...", on_done=lambda: done.append(1))
    assert isinstance(jp, JobProgress)
    dlg = jp.dialog
    assert dlg.windowTitle() == "Stop Report" and dlg.labelText() == "Building stop report..."
    jp.start(lambda job: None, on_result=lambda v: done.append(("result", v)))
    assert slot.started is not None and dlg.isVisible()
    dlg.canceled.emit()  # what the Cancel button does
    assert slot.retired == 1
    assert jp.was_cancelled() and jp.is_done()
    assert jp.dialog is None and not dlg.isVisible()
    assert done == [1]
    dlg.canceled.emit()  # a second cancel is ignored
    assert slot.retired == 1 and done == [1]


def test_job_progress_result_closes_before_callback(parent):
    slot = FakeSlot()
    order = []
    jp = job_progress(parent, "Stop Report", slot, on_done=lambda: order.append("done"))
    jp.start(lambda job: None, on_result=lambda v: order.append(("result", v, jp.dialog is None)))
    dlg = jp.dialog
    slot.started["on_result"]({"x": 1})
    assert order == ["done", ("result", {"x": 1}, True)]
    assert not dlg.isVisible() and jp.is_done() and not jp.was_cancelled()
    assert slot.retired == 0


def test_job_progress_error_closes_before_callback(parent):
    slot = FakeSlot()
    order = []
    jp = job_progress(parent, "Stop Report", slot, on_done=lambda: order.append("done"))
    jp.start(lambda job: None, on_error=lambda m: order.append(("error", m)))
    slot.started["on_error"]("boom")
    assert order == ["done", ("error", "boom")]
    assert jp.dialog is None


def test_job_progress_progress_drives_label_maximum_value(parent):
    slot = FakeSlot()
    seen = []
    jp = job_progress(parent, "Stop Report", slot, parse_progress=_stop_report_parse)
    jp.start(lambda job: None, on_progress=seen.append)
    dlg = jp.dialog
    slot.started["on_progress"](("copies", 2, 8))
    assert dlg.labelText() == "Copying report clips..."
    assert dlg.maximum() == 8 and dlg.value() == 2
    slot.started["on_progress"](("thumbs", 1, 3))
    assert dlg.labelText() == "Reading stop thumbnails..."
    slot.started["on_progress"]("junk the parser cannot unpack")  # ignored, still forwarded
    assert dlg.labelText() == "Reading stop thumbnails..." and dlg.value() == 1
    assert seen == [("copies", 2, 8), ("thumbs", 1, 3), "junk the parser cannot unpack"]
    slot.started["on_result"](None)


def test_job_progress_default_parser_accepts_pairs_and_ignores_junk(parent):
    slot = FakeSlot()
    jp = job_progress(parent, "Export", slot)
    jp.start(lambda job: None)
    dlg = jp.dialog
    slot.started["on_progress"]((3, 12))
    assert dlg.maximum() == 12 and dlg.value() == 3
    slot.started["on_progress"]("not a payload")
    assert dlg.maximum() == 12 and dlg.value() == 3
    slot.started["on_progress"](("Halfway", 6, 12))
    assert dlg.labelText() == "Halfway" and dlg.value() == 6
    slot.started["on_result"](None)
