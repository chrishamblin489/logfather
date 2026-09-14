"""The three progress-dialog shapes the app uses, each written once.

- ``BusyDialog``: an indeterminate, non-modal "working..." dialog with no
  Cancel button (timeline load, clip download, log fetch).
- ``StageProgress``: a determinate, window-modal dialog with Cancel for
  work that runs on the UI thread and pumps events between steps (the
  OCR scans, the readings table, the clip export). Creates nothing when
  there is no parent, so the same loop runs headless on a worker.
- ``job_progress``: a determinate dialog with Cancel bound to a
  ``qt_worker.JobSlot``: Cancel retires the slot, the job's progress
  payloads drive the label/maximum/value, and its result or error closes
  the dialog.

Before this module each site open-coded the same six QProgressDialog
lines (docs/CODE_REVIEW_2026-09-14.md, item 2c).
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget


def _pump() -> None:
    """Let a Cancel click (and the dialog's own repaint) land."""
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


class BusyDialog:
    """An indeterminate, non-modal dialog with no Cancel button.

    ``show(message)`` creates the dialog on first use (or relabels the
    one already up); ``hide()`` closes and drops it, so the next ``show``
    starts from a fresh indeterminate range. Both are idempotent.
    """

    def __init__(self, parent: QWidget | None, title: str, *, default_message: str = "Working..."):
        self._parent = parent
        self._title = title
        self._default_message = default_message
        self._dialog: QProgressDialog | None = None

    @property
    def dialog(self) -> QProgressDialog | None:
        return self._dialog

    def is_shown(self) -> bool:
        return self._dialog is not None

    def show(self, message: str | None = None) -> None:
        text = message or self._default_message
        if self._dialog is None:
            dlg = QProgressDialog(text, None, 0, 0, self._parent)
            dlg.setWindowTitle(self._title)
            dlg.setCancelButton(None)
            dlg.setWindowModality(Qt.NonModal)
            dlg.setMinimumDuration(0)
            dlg.setRange(0, 0)  # indefinite spinner
            self._dialog = dlg
        self._dialog.setLabelText(text)
        self._dialog.show()

    def set_progress(self, done: int, total: int, text: str | None = None) -> None:
        """Turn the spinner into a bar (per-mille) while a transfer reports
        its size; no-op when the dialog is not up."""
        dlg = self._dialog
        if dlg is None:
            return
        if total:
            dlg.setRange(0, 1000)
            dlg.setValue(min(1000, int(done * 1000 / total)))
        if text:
            dlg.setLabelText(text)

    def hide(self) -> None:
        dlg = self._dialog
        self._dialog = None
        if dlg is not None:
            dlg.close()


class StageProgress:
    """A determinate dialog with Cancel for a loop that runs on the UI
    thread. ``set(value)`` pumps events so the bar paints and a Cancel
    click lands; ``was_cancelled()`` pumps too, then reports Cancel (or
    ``should_abort``, the headless interruption) and tells ``on_cancel``
    once. With ``parent=None`` nothing is created: the loop runs silently
    and only ``should_abort`` can stop it.
    """

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        *,
        cancel_text: str = "Cancel",
        modality=Qt.WindowModal,
        should_abort: Callable[[], bool] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ):
        self._parent = parent
        self._title = title
        self._cancel_text = cancel_text
        self._modality = modality
        self._should_abort = should_abort
        self._on_cancel = on_cancel
        self._cancel_reported = False
        self._maximum = 0
        self._dialog: QProgressDialog | None = None

    @property
    def dialog(self) -> QProgressDialog | None:
        return self._dialog

    def begin(self, label: str, maximum: int) -> "StageProgress":
        """Show the dialog at 0 of ``maximum`` (at least 1) under ``label``;
        a second call relabels and re-ranges the dialog already up."""
        self._maximum = max(1, int(maximum))
        if self._parent is None:
            return self
        if self._dialog is None:
            dlg = QProgressDialog(label, self._cancel_text, 0, self._maximum, self._parent)
            dlg.setWindowTitle(self._title)
            dlg.setWindowModality(self._modality)
            dlg.setMinimumDuration(0)
            self._dialog = dlg
        else:
            self._dialog.setLabelText(label)
            self._dialog.setRange(0, self._maximum)
        self._dialog.setValue(0)
        self._dialog.show()
        return self

    def _gone(self) -> bool:
        """The dialog's C++ object can be destroyed underneath us while a
        loop pumps events: the parent window closes (the Sync CCTV Time
        window auto-closes once the offset is applied, 2026-09-14) and
        takes its child dialog with it. Treat that as a cancel."""
        self._dialog = None
        return True

    def set(self, value: int) -> None:
        if self._dialog is None:
            return
        try:
            self._dialog.setValue(min(self._maximum, int(value)))
        except RuntimeError:
            self._gone()
            return
        _pump()

    def set_label(self, text: str) -> None:
        if self._dialog is not None:
            try:
                self._dialog.setLabelText(text)
            except RuntimeError:
                self._gone()

    def was_cancelled(self) -> bool:
        if self._should_abort is not None and self._should_abort():
            return True
        if self._dialog is None:
            return False
        _pump()
        try:
            cancelled = self._dialog.wasCanceled()
        except RuntimeError:
            return self._gone()
        if not cancelled:
            return False
        if not self._cancel_reported:
            self._cancel_reported = True
            if self._on_cancel is not None:
                self._on_cancel()
        return True

    def close(self) -> None:
        dlg = self._dialog
        self._dialog = None
        if dlg is not None:
            try:
                dlg.close()
            except RuntimeError:
                pass  # already destroyed with its parent


def _default_parse_progress(payload) -> tuple[str | None, int, int] | None:
    """Payloads of ``(done, total)`` or ``(label, done, total)``; anything
    else is ignored."""
    try:
        if len(payload) == 2:
            done, total = payload
            return None, int(done), int(total)
        if len(payload) == 3:
            label, done, total = payload
            return (str(label) if label is not None else None), int(done), int(total)
    except (TypeError, ValueError):
        pass
    return None


class JobProgress:
    """A QProgressDialog bound to a JobSlot. Build it, then ``start(fn,
    ...)`` exactly as you would call ``slot.start``: the dialog is up while
    the job runs, its Cancel retires the slot, each progress payload is
    turned into (label, done, total) by ``parse_progress`` and shown, and
    the result or error closes the dialog before reaching your callback.
    ``on_done`` runs once however the job ends (cancel, result, error)."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        slot,
        *,
        label: str = "Working...",
        cancel_text: str = "Cancel",
        parse_progress: Callable[[object], tuple[str | None, int, int] | None] | None = None,
        on_done: Callable[[], None] | None = None,
    ):
        self._slot = slot
        self._parse_progress = parse_progress or _default_parse_progress
        self._on_done = on_done
        self._done = False
        self._cancelled = False
        dlg = QProgressDialog(label, cancel_text, 0, 0, parent)
        dlg.setWindowTitle(title)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        dlg.canceled.connect(self._on_canceled)
        self._dialog: QProgressDialog | None = dlg

    @property
    def dialog(self) -> QProgressDialog | None:
        return self._dialog

    def is_done(self) -> bool:
        return self._done

    def was_cancelled(self) -> bool:
        return self._cancelled

    def start(
        self,
        fn: Callable,
        *,
        on_result: Callable | None = None,
        on_error: Callable | None = None,
        on_progress: Callable | None = None,
    ):
        """Start ``fn`` on the slot; the callbacks run after the dialog has
        closed (result/error) or while it updates (progress)."""

        def _result(value):
            self._finish()
            if on_result is not None:
                on_result(value)

        def _error(message):
            self._finish()
            if on_error is not None:
                on_error(message)

        def _progress(payload):
            self._show_progress(payload)
            if on_progress is not None:
                on_progress(payload)

        if self._dialog is not None:
            self._dialog.show()
        return self._slot.start(fn, on_result=_result, on_error=_error, on_progress=_progress)

    def _show_progress(self, payload) -> None:
        dlg = self._dialog
        if dlg is None:
            return
        try:
            parsed = self._parse_progress(payload)
        except (TypeError, ValueError):
            parsed = None  # a payload the parser cannot unpack is not progress
        if parsed is None:
            return
        label, done, total = parsed
        if label:
            dlg.setLabelText(label)
        dlg.setMaximum(max(1, int(total)))
        dlg.setValue(int(done))

    def _on_canceled(self) -> None:
        if self._done:
            return
        self._cancelled = True
        self._slot.retire()
        self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        dlg = self._dialog
        self._dialog = None
        if dlg is not None:
            try:
                dlg.canceled.disconnect(self._on_canceled)
            except (RuntimeError, TypeError):
                pass
            dlg.close()
        if self._on_done is not None:
            self._on_done()


def job_progress(
    parent: QWidget | None,
    title: str,
    slot,
    *,
    label: str = "Working...",
    cancel_text: str = "Cancel",
    parse_progress: Callable[[object], tuple[str | None, int, int] | None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> JobProgress:
    """A progress dialog bound to ``slot``; call ``.start(fn, ...)`` on it.
    See ``JobProgress``."""
    return JobProgress(
        parent,
        title,
        slot,
        label=label,
        cancel_text=cancel_text,
        parse_progress=parse_progress,
        on_done=on_done,
    )
