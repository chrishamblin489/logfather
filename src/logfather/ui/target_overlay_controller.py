"""Pick-buffer + conveyor-overlay controller.

Owns the buffer event loading, the gap (tight/wide) classification, the
conveyor calibration and its dialog, and the per-frame overlay building —
~400 lines that previously lived inside the Main_Window hub. This is the
piece most likely to change with belt-model work, so it now changes in
one file.

Wiring: the viewer's current_time_changed feeds on_playhead(); the hub
forwards panel visibility, the tracking toggle, threshold changes, and
clip selection (load_buffer_events).
"""
from __future__ import annotations

import re
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from logfather.core.timeline_model import ensure_utc
from logfather.data.conveyor_calibration import ConveyorCalibration, load_calibration
from logfather.ui.conveyor_calibration_dialog import ConveyorCalibrationDialog
from logfather.ui.qt_worker import JobSlot
from logfather.data.target_buffer_loader import buffer_state_at, fetch_buffer_events
from logfather.ui.target_buffer_widget import _detail_rows, _display_target_id, _summary_rows


def choose_clip_target_rate_bucket_seconds(clip_start: datetime, clip_end: datetime) -> int:
    span_seconds = max(1.0, (ensure_utc(clip_end) - ensure_utc(clip_start)).total_seconds())
    raw = span_seconds / 240.0
    candidates = [1, 2, 5, 10, 15, 30, 60]
    for candidate in candidates:
        if raw <= candidate:
            return candidate
    return 60


def clip_target_rate_buckets_from_buffer_events(
    events: list,
    clip_start: datetime,
    clip_end: datetime,
) -> list[dict]:
    clip_start_utc = ensure_utc(clip_start)
    clip_end_utc = ensure_utc(clip_end)
    if clip_end_utc <= clip_start_utc:
        return []
    bucket_seconds = choose_clip_target_rate_bucket_seconds(clip_start_utc, clip_end_utc)
    span_seconds = (clip_end_utc - clip_start_utc).total_seconds()
    bucket_count = max(1, int((span_seconds + bucket_seconds - 1) // bucket_seconds))
    counts = [0] * bucket_count
    for ev in events:
        if ev.event_type != "target_added":
            continue
        ts = ev.timestamp
        if not isinstance(ts, datetime):
            continue
        ts = ensure_utc(ts)
        if ts < clip_start_utc or ts >= clip_end_utc:
            continue
        idx = int((ts - clip_start_utc).total_seconds() // bucket_seconds)
        if 0 <= idx < bucket_count:
            counts[idx] += 1
    buckets: list[dict] = []
    for idx, count in enumerate(counts):
        start = clip_start_utc + timedelta(seconds=idx * bucket_seconds)
        end = min(clip_end_utc, start + timedelta(seconds=bucket_seconds))
        buckets.append({
            "start": start,
            "end": end,
            "count": int(count),
        })
    return buckets


def compute_gap_target_ids(events: list, threshold: float) -> tuple[set[str], set[str]]:
    """Tight/wide gap detection over the rolling 60s average add rate."""
    close_flagged: set[str] = set()
    wide_flagged: set[str] = set()
    add_times: list[float] = []
    last_add_time: float | None = None
    threshold = float(threshold)
    for ev in events:
        if ev.event_type != "target_added":
            continue
        if not ev.buffer_snapshot:
            continue
        target = ev.buffer_snapshot[-1]
        current_dt = ev.timestamp
        if current_dt.tzinfo is None:
            current_dt = current_dt.astimezone(timezone.utc)
        else:
            current_dt = current_dt.astimezone(timezone.utc)
        current_ts = current_dt.timestamp()
        add_times.append(current_ts)
        if last_add_time is None or len(add_times) < 2:
            last_add_time = current_ts
            continue
        left = bisect_left(add_times, current_ts - 60.0)
        window_count = len(add_times) - left
        if window_count >= 2:
            span = current_ts - add_times[left]
            if span > 0:
                avg_gap = span / float(window_count - 1)
                actual_gap = current_ts - last_add_time
                if avg_gap > 0.0:
                    if actual_gap < (avg_gap * threshold):
                        close_flagged.add(target.target_id)
                    elif threshold > 0.0 and actual_gap > (avg_gap / threshold):
                        wide_flagged.add(target.target_id)
        last_add_time = current_ts
    return close_flagged, wide_flagged


class TargetOverlayController(QObject):
    # True when the current system has a conveyor calibration (a tracking
    # line); the Track button is only usable then (Chris, 2026-09-13).
    calibration_ready = Signal(bool)

    def __init__(
        self,
        viewer,
        buffer_widget,
        replay_timeline,
        settings_provider: Callable,
        calibration_system_id_provider: Callable[[], str],
        parent_widget,
    ):
        super().__init__(parent_widget)
        self._viewer = viewer
        self._buffer_widget = buffer_widget
        self._replay_timeline = replay_timeline
        self._settings_provider = settings_provider
        self._calibration_system_id_provider = calibration_system_id_provider
        self._parent_widget = parent_widget

        self.panel_visible = False
        self._buffer_slot = JobSlot(self)
        self._buffer_events: list = []
        self._buffer_clip_start: datetime | None = None
        self._buffer_clip_end: datetime | None = None
        self._conveyor_cal: ConveyorCalibration = ConveyorCalibration(system_id="")
        self._cal_dialog: ConveyorCalibrationDialog | None = None
        self._last_targets: list = []
        self._last_playhead_dt: datetime | None = None
        self._tracking_enabled: bool = True
        self._close_gap_target_ids: set[str] = set()
        self._wide_gap_target_ids: set[str] = set()

    # ---- buffer loading --------------------------------------------------

    def clear(self) -> None:
        self._buffer_events = []
        self._buffer_clip_start = None
        self._buffer_clip_end = None
        self._buffer_widget.clear()

    def load_buffer_events(self, pikpak_root: Path | None, clip_start, clip_end) -> None:
        self._buffer_events = []
        self._buffer_clip_start = clip_start
        self._buffer_clip_end = clip_end
        self._buffer_widget.clear()
        if pikpak_root is None or clip_start is None or clip_end is None:
            return

        print(f"[buffer] starting load for {pikpak_root}  {clip_start} -> {clip_end}")
        settings = self._settings_provider()
        self._buffer_slot.start(
            lambda job: fetch_buffer_events(settings, pikpak_root, clip_start, clip_end),
            on_result=self._on_buffer_events_loaded,
            on_error=self._on_buffer_events_failed,
        )

    def _on_buffer_events_failed(self, message: str) -> None:
        print(f"[buffer] load failed: {message}")
        self._on_buffer_events_loaded([])

    def _on_buffer_events_loaded(self, events: list) -> None:
        self._buffer_events = events
        self._recompute_gap_ids()
        self._buffer_widget.set_buffer_events(events)
        self._buffer_widget.set_alerted_target_ids(self._close_gap_target_ids)
        self._buffer_widget.set_wide_gap_target_ids(self._wide_gap_target_ids)
        if self._buffer_clip_start is not None and self._buffer_clip_end is not None:
            buckets = clip_target_rate_buckets_from_buffer_events(
                events,
                self._buffer_clip_start,
                self._buffer_clip_end,
            )
            self._replay_timeline.set_clip_target_rate_heat(
                self._buffer_clip_start, self._buffer_clip_end, buckets
            )
        print(f"[buffer] {len(events)} buffer state transitions loaded")
        if self._last_playhead_dt:
            if self.panel_visible:
                self._buffer_widget.update_for_time(self._last_playhead_dt)
            self._push_conveyor_overlays(self._last_playhead_dt)

    def _recompute_gap_ids(self) -> None:
        self._close_gap_target_ids, self._wide_gap_target_ids = compute_gap_target_ids(
            self._buffer_events, float(self._viewer.gap_threshold)
        )

    # ---- per-frame fan-in ------------------------------------------------

    def on_playhead(self, dt: datetime) -> None:
        self._last_playhead_dt = dt
        if self.panel_visible and self._buffer_events:
            self._buffer_widget.set_alerted_target_ids(self._close_gap_target_ids)
            self._buffer_widget.set_wide_gap_target_ids(self._wide_gap_target_ids)
            self._buffer_widget.update_for_time(dt)
        self._push_conveyor_overlays(dt)
        if self._cal_dialog is not None:
            self._cal_dialog.on_time(dt)

    def on_gap_threshold_changed(self, _value: float) -> None:
        if not self._buffer_events:
            return
        self._recompute_gap_ids()
        self._buffer_widget.set_alerted_target_ids(self._close_gap_target_ids)
        self._buffer_widget.set_wide_gap_target_ids(self._wide_gap_target_ids)
        if self.panel_visible and self._last_playhead_dt is not None:
            self._buffer_widget.update_for_time(self._last_playhead_dt)
        if self._last_playhead_dt is not None:
            self._push_conveyor_overlays(self._last_playhead_dt)

    def set_tracking_enabled(self, enabled: bool) -> None:
        self._tracking_enabled = enabled
        if enabled:
            if self._last_playhead_dt:
                self._push_conveyor_overlays(self._last_playhead_dt)
        else:
            self._viewer.video_label.set_target_overlays([])

    # ---- conveyor calibration -------------------------------------------

    def first_product_time(self):
        """When the first product was seen on the loaded clip: the earliest
        target_added event in the clip's buffer events (they are fetched
        for the clip's span and sorted), or None before any is known."""
        for event in self._buffer_events:
            if getattr(event, "event_type", "") == "target_added":
                return event.timestamp
        return self._buffer_events[0].timestamp if self._buffer_events else None

    def has_calibration(self) -> bool:
        return bool(self._conveyor_cal.has_tracking_line())

    def reload_calibration(self) -> None:
        sid = self._calibration_system_id_provider()
        self._conveyor_cal = load_calibration(sid)
        print(f"[cal] loaded calibration for '{sid}', "
              f"{'tracking line ready' if self._conveyor_cal.has_tracking_line() else 'no tracking line'}")
        self.calibration_ready.emit(self.has_calibration())

    def open_calibration_dialog(self) -> None:
        if self._cal_dialog is not None:
            self._cal_dialog.raise_()
            self._cal_dialog.activateWindow()
            return
        self.reload_calibration()
        dialog = ConveyorCalibrationDialog(self._conveyor_cal, parent=self._parent_widget)
        dialog.calibration_saved.connect(self._on_calibration_saved)
        dialog.finished.connect(self._on_cal_dialog_closed)
        dialog.transport_step.connect(self._on_cal_transport_step)
        dialog.transport_seek_fraction.connect(self._on_cal_transport_seek)
        dialog.transport_play.connect(self._viewer.play)
        dialog.transport_pause.connect(self._viewer.pause)
        dialog.transport_jump_fraction.connect(self._on_cal_transport_jump)
        dialog.set_clip_context(self._current_clip_key(), self._cal_position_info)
        self._viewer.playing_changed.connect(self._feed_cal_dialog_playing)
        dialog.on_playing_state(bool(self._viewer.playing))

        # Live frame updates. Connected unconditionally: previously this only
        # connected when a frame existed at open time, so a dialog opened
        # before the clip finished loading never received frames.
        self._viewer.current_time_changed.connect(self._feed_cal_dialog_frame)
        frame = self._viewer.video_label._frame
        if frame is not None:
            dialog.on_frame(frame)

        # Feed current targets
        if self._buffer_events and self._last_playhead_dt:
            targets, _ = buffer_state_at(self._buffer_events, self._last_playhead_dt)
            dialog.on_targets(targets)
        if self._last_playhead_dt:
            dialog.on_time(self._last_playhead_dt)

        self._cal_dialog = dialog
        self._feed_cal_dialog_position()
        dialog.show()

    def _current_clip_key(self) -> str | None:
        """Stable identity for the open clip whether the viewer holds the
        share path or the local cache copy (whose stem carries a trailing
        _<16-hex> content key)."""
        path = self._viewer.current_video_path
        if not path:
            return None
        return re.sub(r"_[0-9a-f]{16}$", "", Path(path).stem)

    def _cal_position_info(self, fraction: float) -> tuple[int, datetime | None]:
        """Frame number and playback time for a captured clip fraction —
        the same maths the go-to jump and the viewer's clock use."""
        viewer = self._viewer
        frame = int(round(max(0.0, min(1.0, float(fraction))) * max(1, viewer.frame_count - 1)))
        dt = None
        if viewer.fps > 0:
            t = frame / viewer.fps
            if viewer.video_start_dt is not None:
                dt = viewer.alignment.playback_datetime(viewer.video_start_dt, t)
            elif viewer.current_video_filename_dt is not None:
                dt = viewer.alignment.playback_datetime_from_filename(
                    viewer.current_video_filename_dt, t
                )
        return frame, dt

    def _on_cal_transport_step(self, delta_frames: int) -> None:
        self._viewer.scrub_by_frames(int(delta_frames))

    def _on_cal_transport_jump(self, fraction: float) -> None:
        """Exact inverse of the position feed (current_frame/(frame_count-1)),
        so a go-to lands on the captured frame itself — the seconds-based
        seek path can be off by one frame."""
        viewer = self._viewer
        if viewer.cap is None or viewer.frame_count <= 1:
            return
        frame = int(round(max(0.0, min(1.0, float(fraction))) * (viewer.frame_count - 1)))
        viewer.pause()
        viewer.current_frame = frame
        viewer.show_frame(frame)

    def _on_cal_transport_seek(self, fraction: float) -> None:
        viewer = self._viewer
        if viewer.cap is None or viewer.fps <= 0 or viewer.frame_count <= 0:
            return
        duration = viewer.frame_count / viewer.fps
        viewer.seek_to_seconds(max(0.0, min(1.0, float(fraction))) * duration)

    def _feed_cal_dialog_position(self) -> None:
        if self._cal_dialog is None:
            return
        viewer = self._viewer
        if viewer.cap is None or viewer.frame_count <= 1:
            return
        self._cal_dialog.on_clip_position(
            viewer.current_frame / (viewer.frame_count - 1),
            viewer.current_frame,
            viewer.frame_count,
        )

    def _feed_cal_dialog_frame(self, dt: datetime) -> None:
        if self._cal_dialog is None:
            return
        # last_qimage is set synchronously in show_frame BEFORE this signal;
        # video_label._frame only updates on a deferred (0ms-timer) repaint,
        # so reading it here handed the dialog the PREVIOUS frame — making
        # the first step after a direction change appear to move backwards.
        frame = self._viewer.last_qimage
        if frame is None:
            frame = self._viewer.video_label._frame
        if frame is not None:
            self._cal_dialog.on_frame(frame)
        self._feed_cal_dialog_position()
        if self._buffer_events:
            if dt.tzinfo is None:
                dt = dt.astimezone(timezone.utc)
            targets, _ = buffer_state_at(self._buffer_events, dt)
            self._cal_dialog.on_targets(targets)

    def _on_calibration_saved(self, cal: ConveyorCalibration) -> None:
        self._conveyor_cal = cal
        self.calibration_ready.emit(self.has_calibration())
        if self._last_playhead_dt:
            self._push_conveyor_overlays(self._last_playhead_dt)

    def _feed_cal_dialog_playing(self, playing: bool) -> None:
        if self._cal_dialog is not None:
            self._cal_dialog.on_playing_state(playing)

    def _on_cal_dialog_closed(self) -> None:
        try:
            self._viewer.current_time_changed.disconnect(self._feed_cal_dialog_frame)
        except Exception:
            pass
        try:
            self._viewer.playing_changed.disconnect(self._feed_cal_dialog_playing)
        except Exception:
            pass
        self._cal_dialog = None

    # ---- overlays --------------------------------------------------------

    def _push_conveyor_overlays(self, dt: datetime) -> None:
        """Update the target panel and line-tracked overlays."""
        if not self._tracking_enabled:
            return
        buffer_targets, _last_event = self._buffer_targets_for_time(dt)
        tracked_targets = self._visible_tracked_targets(buffer_targets, dt)
        self._viewer.video_label.set_target_overlays(
            self._tracked_target_overlays(tracked_targets, dt)
        )
        self._last_targets = tracked_targets

    def _buffer_targets_for_time(self, dt: datetime) -> tuple[list, object | None]:
        if not self._buffer_events:
            return [], None
        dt = dt.astimezone(timezone.utc)
        targets, last_event = buffer_state_at(self._buffer_events, dt)
        return targets, last_event

    def _visible_tracked_targets(self, targets: list, dt: datetime) -> list:
        if not self._conveyor_cal.has_tracking_line():
            return []
        dt = dt.astimezone(timezone.utc)
        visible = []
        for target in targets:
            age = (dt - target.added_at.astimezone(timezone.utc)).total_seconds()
            if self._conveyor_cal.tracking_position_for_age(age) is not None:
                visible.append(target)
        return visible

    def _tracked_target_overlays(self, targets: list, dt: datetime) -> list[dict]:
        if not self._conveyor_cal.has_tracking_line():
            return []
        dt = dt.astimezone(timezone.utc)
        overlays: list[dict] = []
        total = max(1, len(targets))
        for idx, target in enumerate(targets):
            age = (dt - target.added_at.astimezone(timezone.utc)).total_seconds()
            pos = self._conveyor_cal.tracking_position_for_age(age)
            if pos is None:
                continue
            pid = _display_target_id(target)
            opacity = min(1.0, 0.45 + ((idx + 1) / total) * 0.55)
            is_valid = bool(target.source_doc.get("valid", True))
            is_close_gap = target.target_id in self._close_gap_target_ids
            is_wide_gap = target.target_id in self._wide_gap_target_ids
            detail_lines = [f"#{pid}"]
            for key, value in _summary_rows(target.source_doc) + _detail_rows(target.source_doc):
                if key in {"Front corner", "Back corner"}:
                    continue
                detail_lines.append(f"{key}: {value}")
            if is_close_gap:
                detail_lines.append("Tight gap")
            elif is_wide_gap:
                detail_lines.append("Wide gap")
            overlays.append({
                "norm_x": pos[0],
                "norm_y": pos[1],
                "label": f"#{pid}",
                "info_lines": detail_lines,
                "color": "#e74c3c" if not is_valid else "#f39c12",
                "text_bg_color": "#5c2020" if not is_valid else ("#7a4800" if is_close_gap else ("#163a5a" if is_wide_gap else "#1f4d2e")),
                "opacity": opacity,
                "alert": is_close_gap,
            })
        return overlays

    def export_overlays_for(self, t_seconds: float) -> list[dict]:
        """Overlay provider for clip export: maps clip time to playback time."""
        viewer = self._viewer
        playback_dt = None
        alignment = viewer.alignment
        if viewer.video_start_dt is not None and viewer.fps > 0:
            playback_dt = alignment.playback_datetime(viewer.video_start_dt, t_seconds)
        elif viewer.current_video_filename_dt is not None:
            playback_dt = alignment.playback_datetime_from_filename(
                viewer.current_video_filename_dt, t_seconds
            )
        if playback_dt is None:
            return []
        buffer_targets, _last_event = self._buffer_targets_for_time(playback_dt)
        tracked_targets = self._visible_tracked_targets(buffer_targets, playback_dt)
        return self._tracked_target_overlays(tracked_targets, playback_dt)

    # ---- lifecycle -------------------------------------------------------

    def shutdown(self) -> None:
        self._buffer_slot.shutdown()
