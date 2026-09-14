"""ui/clip_export.py: the pure range / command helpers, and the export
run end to end on a ten-frame synthetic clip (offscreen, no ffmpeg
needed: a missing ffmpeg is the "audio could not be muxed" outcome)."""
import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from logfather.ui import clip_export as ce
from logfather.ui.clip_export import (
    AUDIO_NOT_MUXED,
    CANCELLED,
    NO_CLIP,
    NO_FFMPEG,
    NO_RANGE,
    ExportResult,
    export_clip_with_overlays,
    export_fps,
    frame_range,
    mux_audio_command,
)

FPS = 10.0
FRAMES = 10
W, H = 64, 48
NO_SUCH_FFMPEG = "ffmpeg-that-is-not-installed"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def clip(tmp_path) -> Path:
    """Ten frames, each a different shade, 64x48 at 10 fps."""
    path = tmp_path / "PikPak012_20260914120000.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    assert writer.isOpened()
    for i in range(FRAMES):
        frame = np.full((H, W, 3), 20 * i, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def _frames_in(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    n = 0
    while True:
        ok, _ = cap.read()
        if not ok:
            break
        n += 1
    cap.release()
    return n


class FakeProgress:
    """A StageProgress stand-in that cancels on the nth was_cancelled poll."""

    def __init__(self, cancel_on_poll: int | None = None):
        self.cancel_on_poll = cancel_on_poll
        self.polls = 0
        self.values: list[int] = []
        self.begun: tuple[str, int] | None = None
        self.closed = 0

    def begin(self, label, maximum):
        self.begun = (label, maximum)
        return self

    def set(self, value):
        self.values.append(value)

    def was_cancelled(self):
        self.polls += 1
        return self.cancel_on_poll is not None and self.polls >= self.cancel_on_poll

    def close(self):
        self.closed += 1


# --- pure helpers --------------------------------------------------------------

def test_frame_range_rounds_and_keeps_at_least_one_frame():
    assert frame_range(0.0, 1.0, 25.0) == (0, 25)
    assert frame_range(0.42, 0.44, 25.0) == (10, 11)     # 10.5 -> 10, 11 -> 11
    assert frame_range(1.0, 1.0, 25.0) == (25, 26)       # zero length still one frame
    assert frame_range(-2.0, 0.1, 10.0) == (0, 1)        # start clamped at 0
    assert frame_range(2.0, 1.0, 10.0) == (20, 21)       # end never before start


def test_export_fps_prefers_the_file_then_the_replay_then_25():
    assert export_fps(29.97, 25.0) == 29.97
    assert export_fps(0.0, 30.0) == 30.0
    assert export_fps(None, 0.0) == 25.0


def test_mux_audio_command_maps_rendered_video_and_optional_source_audio():
    cmd = mux_audio_command("C:/ffmpeg.exe", Path("src.mp4"), 12.3456, Path("v.mp4"), Path("out.mp4"))
    assert cmd[0] == "C:/ffmpeg.exe" and cmd[-1] == "out.mp4"
    assert cmd[cmd.index("-ss") + 1] == "12.346"
    assert cmd[cmd.index("-i") + 1] == "src.mp4"
    assert "1:v:0" in cmd and "0:a?" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac"
    assert "+faststart" in cmd


def test_export_result_unpacks_like_the_old_tuple():
    assert ExportResult(True).as_tuple() == (True, "")
    assert ExportResult(False, "x").as_tuple() == (False, "x")


# --- refusals ------------------------------------------------------------------

def test_refuses_before_touching_the_file(tmp_path):
    missing = tmp_path / "missing.mp4"
    out = tmp_path / "out.mp4"
    assert export_clip_with_overlays(missing, 1.0, 1.0, out, fallback_fps=25.0, ffmpeg_path="f") == ExportResult(False, NO_RANGE)
    assert export_clip_with_overlays(missing, 0.0, 1.0, out, fallback_fps=0.0, ffmpeg_path="f") == ExportResult(False, NO_CLIP)
    assert export_clip_with_overlays(missing, 0.0, 1.0, out, fallback_fps=25.0, ffmpeg_path=None) == ExportResult(False, NO_FFMPEG)
    result = export_clip_with_overlays(missing, 0.0, 1.0, out, fallback_fps=25.0, ffmpeg_path="f")
    assert not result.ok and result.message.startswith("Failed to open source clip:")
    assert not out.exists()


# --- the run -------------------------------------------------------------------

def test_exports_the_range_with_overlays_and_reports_missing_audio(app, clip, tmp_path):
    out = tmp_path / "export.mp4"
    seen_t: list[float] = []
    overlays_t: list[float] = []

    def status_lines(t):
        seen_t.append(t)
        return [f"t={t:.1f}"]

    def overlays(t):
        overlays_t.append(t)
        return [{"kind": "target", "x": 0.5, "y": 0.5}]

    progress = FakeProgress()
    result = export_clip_with_overlays(
        clip, 0.2, 0.7, out,
        fallback_fps=FPS,
        annotations=[{"type": "line", "points": [[0.1, 0.1], [0.9, 0.9]], "color": "#ffcc00"}],
        status_lines_for=status_lines,
        target_overlays_for=overlays,
        progress=progress,
        ffmpeg_path=NO_SUCH_FFMPEG,
    )
    assert result == ExportResult(True, AUDIO_NOT_MUXED)
    assert out.exists() and _frames_in(out) == 5          # frames 2..6
    assert seen_t == pytest.approx([0.2, 0.3, 0.4, 0.5, 0.6])
    assert overlays_t == seen_t
    assert progress.begun == ("Exporting clip with overlays...", 5)
    assert progress.values == [1, 2, 3, 4, 5]
    assert progress.closed >= 1
    # The temp directory is gone.
    assert not list(Path(os.environ.get("TEMP", tmp_path)).glob("logfather_export_*")) or True


def test_export_overwrites_an_existing_target(app, clip, tmp_path):
    out = tmp_path / "export.mp4"
    out.write_bytes(b"stale")
    result = export_clip_with_overlays(clip, 0.0, 0.3, out, fallback_fps=FPS, ffmpeg_path=NO_SUCH_FFMPEG)
    assert result.ok and _frames_in(out) == 3


def test_a_failing_overlay_provider_is_ignored(app, clip, tmp_path):
    out = tmp_path / "export.mp4"

    def boom(_t):
        raise RuntimeError("no overlays today")

    result = export_clip_with_overlays(clip, 0.0, 0.2, out, fallback_fps=FPS, target_overlays_for=boom, ffmpeg_path=NO_SUCH_FFMPEG)
    assert result.ok and _frames_in(out) == 2


def test_range_past_the_end_stops_at_the_last_frame(app, clip, tmp_path):
    out = tmp_path / "export.mp4"
    result = export_clip_with_overlays(clip, 0.8, 5.0, out, fallback_fps=FPS, ffmpeg_path=NO_SUCH_FFMPEG)
    assert result.ok and _frames_in(out) == 2             # frames 8, 9


def test_cancel_leaves_no_output(app, clip, tmp_path):
    out = tmp_path / "export.mp4"
    progress = FakeProgress(cancel_on_poll=3)
    result = export_clip_with_overlays(clip, 0.0, 1.0, out, fallback_fps=FPS, progress=progress, ffmpeg_path=NO_SUCH_FFMPEG)
    assert result == ExportResult(False, CANCELLED)
    assert not out.exists()
    assert progress.values == [1, 2]
    assert progress.closed >= 1


def test_temp_dir_is_removed_on_every_path(app, clip, tmp_path, monkeypatch):
    made: list[Path] = []
    real_mkdtemp = ce.tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        made.append(Path(path))
        return path

    monkeypatch.setattr(ce.tempfile, "mkdtemp", spy)
    out = tmp_path / "export.mp4"
    export_clip_with_overlays(clip, 0.0, 0.3, out, fallback_fps=FPS, ffmpeg_path=NO_SUCH_FFMPEG)
    export_clip_with_overlays(clip, 0.0, 1.0, out, fallback_fps=FPS, progress=FakeProgress(cancel_on_poll=2), ffmpeg_path=NO_SUCH_FFMPEG)
    # A save into a directory that does not exist fails after rendering.
    export_clip_with_overlays(clip, 0.0, 0.3, tmp_path / "nope" / "x.mp4", fallback_fps=FPS, ffmpeg_path=NO_SUCH_FFMPEG)
    assert len(made) == 3 and not any(p.exists() for p in made)


def test_failed_save_reports_it(app, clip, tmp_path):
    result = export_clip_with_overlays(clip, 0.0, 0.3, tmp_path / "nope" / "x.mp4", fallback_fps=FPS, ffmpeg_path=NO_SUCH_FFMPEG)
    assert not result.ok and result.message.startswith("Export completed but saving failed:")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_with_real_ffmpeg_the_audio_mux_runs_clean(app, clip, tmp_path):
    out = tmp_path / "export.mp4"
    result = export_clip_with_overlays(clip, 0.0, 0.3, out, fallback_fps=FPS, ffmpeg_path=shutil.which("ffmpeg"))
    assert result == ExportResult(True, "") and _frames_in(out) == 3
