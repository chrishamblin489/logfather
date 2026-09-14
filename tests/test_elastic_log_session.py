"""ui/elastic_log_session.py: the ISO helpers, and the session driven with
a fake fetch on a synchronous executor (the rows arrive through the Qt
event loop, so a QApplication pumps them)."""
import os
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from logfather.data.elastic_errors import ElasticFetchError
from logfather.ui.elastic_log_session import (
    ElasticLogSession,
    parse_iso,
    parse_window,
    request_key,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, times: int = 5) -> None:
    for _ in range(times):
        app.processEvents()


class SyncExecutor:
    """Runs the job at submit time and hands back a finished future."""

    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, fn, *args):
        # The session submits a partial carrying the fetch arguments, so
        # `args` is empty; the fake fetch itself records what it received.
        self.calls.append(args)
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - mirrors a worker thread
            future.set_exception(exc)
        return future


class PendingExecutor:
    """Hands back futures that stay pending until the test settles them."""

    def __init__(self):
        self.futures: list[Future] = []

    def submit(self, fn, *args):
        future: Future = Future()
        self.futures.append(future)
        return future


class Sink:
    def __init__(self, session: ElasticLogSession):
        self.ready: list[list] = []
        self.failed: list[str] = []
        session.ready.connect(self.ready.append)
        session.failed.connect(self.failed.append)


SETTINGS = object()
ROBOT = "35-2300-012"
ROWS = [(datetime(2026, 9, 14, 12, 0, 0), "robot", "RUNNING", "hello", "x")]


def make_session(executor, fetch, has_rows=lambda: False) -> ElasticLogSession:
    return ElasticLogSession(settings_provider=lambda: SETTINGS, fetch=fetch, executor=executor, has_rows=has_rows)


# --- pure helpers --------------------------------------------------------------

def test_parse_iso_reads_z_as_utc_and_keeps_naive_naive():
    assert parse_iso("2026-09-14T12:00:00Z") == datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    assert parse_iso(" 2026-09-14T12:00:00.250+01:00 ").utcoffset().total_seconds() == 3600
    assert parse_iso("2026-09-14T12:00:00") == datetime(2026, 9, 14, 12, 0)
    with pytest.raises(ValueError):
        parse_iso("yesterday-ish")


def test_parse_window_and_request_key():
    assert parse_window("2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z") == (
        datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc),
    )
    assert request_key(Path("Z:/public/PikPak012"), "a", "b") == ("Z:\\public\\PikPak012", "a", "b") or \
        request_key(Path("Z:/public/PikPak012"), "a", "b") == ("Z:/public/PikPak012", "a", "b")
    assert request_key("p", 1, None) == ("p", "1", "None")


# --- the session ---------------------------------------------------------------

def test_start_fetches_with_the_window_and_delivers_rows(app):
    executor = SyncExecutor()
    received: list[tuple[tuple, dict]] = []

    def fetch(*args, **kwargs):
        received.append((args, kwargs))
        return ROWS

    session = make_session(executor, fetch)
    sink = Sink(session)
    assert session.start("Z:/public/PikPak012", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z", robot_id=ROBOT)
    assert session.is_active() and session.active_key == ("Z:/public/PikPak012", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z")
    assert sink.ready == []  # nothing before the event loop turns
    pump(app)
    assert sink.ready == [ROWS] and sink.failed == []
    assert not session.is_active() and session.active_key is None
    assert session.loaded_key == ("Z:/public/PikPak012", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z")
    assert len(executor.calls) == 1
    (settings, root, start_dt, end_dt), kwargs = received[0]
    assert settings is SETTINGS and root == Path("Z:/public/PikPak012")
    assert (start_dt, end_dt) == (
        datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc),
    )
    # The robot id resolved on the UI thread reaches the fetch as-is
    # (review item 10: the worker never reads the picker's override).
    assert kwargs == {"robot_id": ROBOT}


def test_no_robot_id_is_passed_through_as_none(app):
    received: list[dict] = []

    def fetch(*_args, **kwargs):
        received.append(kwargs)
        return []

    session = make_session(SyncExecutor(), fetch)
    Sink(session)
    assert session.start("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z", robot_id=None)
    pump(app)
    assert received == [{"robot_id": None}]


def test_same_request_is_skipped_while_the_rows_are_held(app):
    held = {"rows": False}
    executor = SyncExecutor()
    session = make_session(executor, lambda *a, **k: ROWS, has_rows=lambda: held["rows"])
    Sink(session)
    args = ("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z")
    assert session.start(*args, robot_id=ROBOT)
    pump(app)
    held["rows"] = True
    assert session.is_satisfied(request_key(*args))
    assert not session.start(*args, robot_id=ROBOT) and len(executor.calls) == 1
    # Once the replay drops its rows (a new clip), the same range fetches again.
    held["rows"] = False
    session.forget()
    assert session.loaded_key is None
    assert session.start(*args, robot_id=ROBOT) and len(executor.calls) == 2


def test_same_request_in_flight_is_skipped_and_a_new_one_cancels_it(app):
    executor = PendingExecutor()
    session = make_session(executor, lambda *a, **k: ROWS)
    sink = Sink(session)
    args = ("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z")
    assert session.start(*args, robot_id=ROBOT)
    assert not session.start(*args, robot_id=ROBOT) and len(executor.futures) == 1
    pump(app)
    assert session.start("p", "2026-09-14T12:05:00Z", "2026-09-14T12:10:00Z", robot_id=ROBOT)
    assert executor.futures[0].cancelled()
    assert session.active_key == ("p", "2026-09-14T12:05:00Z", "2026-09-14T12:10:00Z")
    executor.futures[1].set_result(ROWS)
    pump(app)
    assert sink.ready == [ROWS]


def test_bad_timestamps_raise_before_anything_starts(app):
    executor = SyncExecutor()
    session = make_session(executor, lambda *a, **k: ROWS)
    with pytest.raises(ValueError):
        session.start("p", "not a time", "2026-09-14T12:05:00Z", robot_id=ROBOT)
    assert executor.calls == [] and not session.is_active()


def test_partial_failure_delivers_the_rows_then_the_message(app):
    def fetch(*_a, **_k):
        raise ElasticFetchError("timed out after 2 pages", items=ROWS)

    session = make_session(SyncExecutor(), fetch)
    sink = Sink(session)
    args = ("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z")
    session.start(*args, robot_id=ROBOT)
    pump(app)
    assert sink.ready == [ROWS] and sink.failed == ["timed out after 2 pages"]
    # The partial range counts as loaded, so it is not refetched on retrigger.
    assert session.loaded_key == request_key(*args)


def test_failure_without_rows_only_reports(app):
    def fetch(*_a, **_k):
        raise ElasticFetchError("nothing at all")

    session = make_session(SyncExecutor(), fetch)
    sink = Sink(session)
    session.start("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z", robot_id=ROBOT)
    pump(app)
    assert sink.ready == [] and sink.failed == ["nothing at all"]
    assert session.loaded_key is None


def test_any_other_error_is_reported_as_text(app):
    def fetch(*_a, **_k):
        raise RuntimeError("boom")

    session = make_session(SyncExecutor(), fetch)
    sink = Sink(session)
    session.start("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z", robot_id=ROBOT)
    pump(app)
    assert sink.failed == ["boom"] and not session.is_active()


def test_cancel_drops_the_result_that_arrives_later(app):
    executor = PendingExecutor()
    session = make_session(executor, lambda *a, **k: ROWS)
    sink = Sink(session)
    session.start("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z", robot_id=ROBOT)
    pump(app)
    session.cancel()
    assert not session.is_active() and session.active_key is None
    assert executor.futures[0].cancelled()
    pump(app, 20)
    assert sink.ready == [] and sink.failed == []


def test_own_executor_is_created_on_demand_and_shut_down(app):
    session = ElasticLogSession(settings_provider=lambda: SETTINGS, fetch=lambda *a, **k: ROWS)
    sink = Sink(session)
    assert session.start("p", "2026-09-14T12:00:00Z", "2026-09-14T12:05:00Z", robot_id=ROBOT)
    future = session._future
    future.result(timeout=5)
    for _ in range(50):
        pump(app)
        if sink.ready:
            break
    assert sink.ready == [ROWS]
    session.shutdown()
    assert session._executor is None
    session.shutdown()  # idempotent
