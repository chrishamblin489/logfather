"""The Elastic log fetch for one clip: submit, poll, deliver.

Split out of replay_view.py on 2026-09-14 (code review item 9). An
``ElasticLogSession`` runs ``fetch_logs_for_range`` on its own
single-thread executor and polls the future from the Qt event loop, so
the rows land on the UI thread through ``ready`` (and a failure through
``failed``); the replay connects those to its existing apply path and
keeps the busy dialog on its side.

The session remembers which (clip, start, end) request is in flight and
which one last delivered rows, so a repeated request for the same range
is a no-op while the rows are still held (``is_satisfied``), and a new
request cancels the old future first. The log lines the diagnostics
rely on ("cancelling prior log future", "log future N completed with M
rows", the partial-failure delivery) are unchanged.

``parse_iso`` and ``request_key`` are pure and tested on their own.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from logfather.core.log import dbg, log
from logfather.data.elastic_errors import ElasticFetchError
from logfather.data.elastic_loader import fetch_logs_for_range
from logfather.data.settings_store import Settings

RequestKey = tuple[str, str, str]

POLL_INTERVAL_MS = 100


def parse_iso(value: str) -> datetime:
    """An ISO 8601 timestamp; a trailing ``Z`` is read as UTC (Python's
    fromisoformat only learned ``Z`` in 3.11)."""
    val = value.strip()
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    return datetime.fromisoformat(val)


def parse_window(start_iso: str, end_iso: str) -> tuple[datetime, datetime]:
    """The fetch window; raises ValueError when either stamp is malformed."""
    return parse_iso(start_iso), parse_iso(end_iso)


def request_key(pikpak_path, start_iso, end_iso) -> RequestKey:
    return (str(pikpak_path), str(start_iso), str(end_iso))


class ElasticLogSession(QObject):
    ready = Signal(list)    # the rows for the request that just completed
    failed = Signal(str)    # the message to show (after ``ready`` for a partial failure)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        settings_provider: Callable[[], Settings] = Settings.load,
        fetch: Callable = fetch_logs_for_range,
        executor=None,
        has_rows: Callable[[], bool] = lambda: False,
    ):
        """``has_rows`` says whether the replay still holds the rows of the
        last delivery (a request for the same range is skipped while it
        does). ``executor`` is for tests; by default the session owns a
        one-thread pool, created on first use and dropped by ``shutdown``."""
        super().__init__(parent)
        self._settings_provider = settings_provider
        self._fetch = fetch
        self._has_rows = has_rows
        self._executor = executor
        self._owns_executor = executor is None
        self._future: Future | None = None
        self._future_id = 0
        self._active_key: RequestKey | None = None
        self._loaded_key: RequestKey | None = None

    # ---- state ----------------------------------------------------------------

    @property
    def active_key(self) -> RequestKey | None:
        """The request in flight, or None."""
        return self._active_key if self._future is not None else None

    @property
    def loaded_key(self) -> RequestKey | None:
        """The request whose rows were delivered last, or None."""
        return self._loaded_key

    def is_active(self) -> bool:
        return self._future is not None

    def is_satisfied(self, key: RequestKey) -> bool:
        """True when ``key`` already delivered (and the rows are still held)
        or is in flight, so a new request for it would be a no-op."""
        if self._loaded_key == key and self._has_rows():
            return True
        return self._active_key == key and self._future is not None

    # ---- lifecycle ------------------------------------------------------------

    def start(self, pikpak_path, start_iso: str, end_iso: str) -> bool:
        """Fetch the rows for the clip at ``pikpak_path`` between the two
        ISO stamps. Returns False (nothing started) when the same request
        is satisfied already; raises ValueError for stamps it cannot parse.
        Any earlier request is cancelled first. The first poll runs on the
        next event-loop turn, so the caller can show its busy dialog before
        a result can arrive."""
        key = request_key(pikpak_path, start_iso, end_iso)
        if self.is_satisfied(key):
            return False
        start_dt, end_dt = parse_window(start_iso, end_iso)
        self.cancel()
        self._active_key = key
        dbg("viewer", "load_logs_from_elastic starting")
        self._future_id += 1
        fetch_id = self._future_id
        settings = self._settings_provider()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1)
            self._owns_executor = True
        self._future = self._executor.submit(self._fetch, settings, Path(pikpak_path), start_dt, end_dt)
        dbg("viewer", f"scheduled log fetch id {fetch_id}")
        QTimer.singleShot(0, lambda fid=fetch_id: self._poll(fid))
        return True

    def cancel(self) -> None:
        """Drop the request in flight (its result, if it still arrives, is
        ignored)."""
        future = self._future
        self._future = None
        self._active_key = None
        if future is None:
            return
        log("viewer", "cancelling prior log future")
        future.cancel()

    def forget(self) -> None:
        """Forget what was delivered (the replay cleared its rows for a new
        clip); does not touch a request in flight."""
        self._loaded_key = None

    def shutdown(self) -> None:
        """Stop the executor (own pools are shut down without waiting)."""
        executor = self._executor
        self._executor = None
        if executor is not None and self._owns_executor:
            executor.shutdown(wait=False, cancel_futures=True)

    # ---- polling ------------------------------------------------------------------

    def _poll(self, fetch_id: int) -> None:
        future = self._future
        if future is None or fetch_id != self._future_id:
            return
        if not future.done():
            QTimer.singleShot(POLL_INTERVAL_MS, lambda fid=fetch_id: self._poll(fid))
            return
        self._future = None
        try:
            rows = future.result()
        except ElasticFetchError as exc:
            # Record the key BEFORE clearing it: assigning after the clear
            # stored None, so the same partial range was refetched on every
            # retrigger.
            key = self._active_key
            self._active_key = None
            log("viewer", f"log future {fetch_id} partial failure: {exc}")
            if exc.items:
                log("viewer", f"delivering {len(exc.items)} partial rows despite failure")
                self._loaded_key = key
                self.ready.emit(exc.items)
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self._active_key = None
            log("viewer", f"log future {fetch_id} failed: {exc}")
            self.failed.emit(str(exc))
            return
        self._loaded_key = self._active_key
        self._active_key = None
        log("viewer", f"log future {fetch_id} completed with {len(rows)} rows")
        dbg("viewer", "invoking _on_elastic_logs_ready")
        self.ready.emit(rows)
        dbg("viewer", "returned from _on_elastic_logs_ready")
