"""core/log.py: the always-on `log` channel, LOGFATHER_DEBUG gating of `dbg`,
and the `timed` context manager's threshold."""
import time

import pytest

from logfather.core.log import dbg, debug_enabled, log, timed


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOGFATHER_DEBUG", raising=False)
    monkeypatch.delenv("LOGFATHER_DEBUG_PLAYHEAD", raising=False)


def test_log_always_prints_with_the_tag_prefix(capsys):
    log("elastic", "condition 1 collected 3 hits")
    assert capsys.readouterr().out == "[elastic] condition 1 collected 3 hits\n"


def test_dbg_is_silent_when_the_variable_is_unset(capsys):
    dbg("timeline", "_redraw_timeline")
    assert capsys.readouterr().out == ""
    assert debug_enabled("timeline") is False


@pytest.mark.parametrize("value", ["1", "all", "true", " 1 "])
def test_dbg_prints_every_tag_when_set_to_a_catch_all(monkeypatch, capsys, value):
    monkeypatch.setenv("LOGFATHER_DEBUG", value)
    dbg("timeline", "redraw")
    dbg("ocr", "verify")
    assert capsys.readouterr().out == "[timeline] redraw\n[ocr] verify\n"


def test_a_tag_list_enables_only_those_tags(monkeypatch, capsys):
    monkeypatch.setenv("LOGFATHER_DEBUG", "timeline, OCR")
    assert debug_enabled("timeline") and debug_enabled("ocr")
    assert not debug_enabled("viewer")
    dbg("viewer", "populate_log_list")
    dbg("timeline", "redraw")
    assert capsys.readouterr().out == "[timeline] redraw\n"


def test_empty_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("LOGFATHER_DEBUG", "   ")
    assert debug_enabled("timeline") is False


def test_legacy_playhead_variable_still_enables_the_playhead_tag(monkeypatch, capsys):
    monkeypatch.setenv("LOGFATHER_DEBUG_PLAYHEAD", "1")
    assert debug_enabled("playhead") and not debug_enabled("timeline")
    dbg("playhead", "set None")
    assert capsys.readouterr().out == "[playhead] set None\n"


def test_timed_reports_only_above_the_threshold(monkeypatch, capsys):
    monkeypatch.setenv("LOGFATHER_DEBUG", "1")
    with timed("viewer", "frame read", threshold_s=10.0):
        pass
    assert capsys.readouterr().out == ""
    with timed("viewer", "frame read"):
        time.sleep(0.02)
    out = capsys.readouterr().out
    assert out.startswith("[viewer] frame read: ") and out.endswith("ms\n")
    assert int(out.split(": ")[1][:-3]) >= 15


def test_timed_stays_quiet_without_the_variable(capsys):
    with timed("viewer", "frame read"):
        time.sleep(0.01)
    assert capsys.readouterr().out == ""


def test_timed_still_reports_when_the_block_raises(monkeypatch, capsys):
    monkeypatch.setenv("LOGFATHER_DEBUG", "shutdown")
    with pytest.raises(RuntimeError):
        with timed("shutdown", "'save'"):
            raise RuntimeError("boom")
    assert capsys.readouterr().out.startswith("[shutdown] 'save': ")
