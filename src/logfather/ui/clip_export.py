"""Export a clip range with the replay's overlays burnt in.

Split out of replay_view.py on 2026-09-14 (code review item 9). The
export is three stages - open the writer, render the frames through an
offscreen AnnotatedVideoWidget, mux the source audio back with ffmpeg -
under one try/finally, so the temp files and the writer are released the
same way on success, cancel and every early return (the old method had
four hand-written cleanup paths and none for a failed save).

``export_clip_with_overlays`` is the whole thing; the replay's
``export_current_clip_with_overlays`` gathers its state (annotations,
overlay lines, the target overlays from target_overlay_controller) and
calls it. ``frame_range``, ``export_fps`` and ``mux_audio_command`` are
pure and tested on their own.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import cv2
import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPainter

from logfather.ui.annotated_video_widget import AnnotatedVideoWidget
from logfather.ui.progress import StageProgress

# The messages Main_Window shows; the wording is unchanged from the replay.
NO_RANGE = "Select a non-zero clip range first."
NO_CLIP = "No loaded clip is available for export."
NO_FFMPEG = "ffmpeg was not found on PATH."
NO_DIMENSIONS = "Unable to determine clip dimensions for export."
NO_WRITER = "Unable to create temporary export video."
CANCELLED = "Export canceled."
AUDIO_NOT_MUXED = "Clip exported with baked overlays, but audio could not be muxed back in."

PROGRESS_TITLE = "Export Clip"
PROGRESS_LABEL = "Exporting clip with overlays..."

StatusLinesFor = Callable[[float], Sequence[str]]
OverlaysFor = Callable[[float], Sequence[dict]]


@dataclass(frozen=True)
class ExportResult:
    ok: bool
    message: str = ""

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.message


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def export_fps(cap_fps: float | None, fallback_fps: float | None) -> float:
    """The frame rate the export runs at: the file's, else the replay's,
    else 25."""
    return float(cap_fps or fallback_fps or 25.0)


def frame_range(start_seconds: float, end_seconds: float, fps: float) -> tuple[int, int]:
    """The [start, end) frame indices for a seconds range: start clamped
    at 0, end at least one frame past start."""
    start_frame = max(0, int(round(start_seconds * fps)))
    end_frame = max(start_frame + 1, int(round(end_seconds * fps)))
    return start_frame, end_frame


def mux_audio_command(
    ffmpeg_path: str,
    source_path: Path,
    start_seconds: float,
    video_path: Path,
    out_path: Path,
) -> list[str]:
    """ffmpeg: the rendered video stream, the source's audio (if any) from
    ``start_seconds``, AAC, faststart."""
    return [
        ffmpeg_path,
        "-y",
        "-ss", f"{start_seconds:.3f}",
        "-i", str(source_path),
        "-i", str(video_path),
        "-map", "1:v:0",
        "-map", "0:a?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(out_path),
    ]


class OverlayRenderer:
    """Paints frames through an offscreen AnnotatedVideoWidget, so the
    export shows exactly what the replay's canvas shows."""

    def __init__(self, width: int, height: int, fps: float, annotations: Sequence[dict]):
        self.width = width
        self.height = height
        self._widget = AnnotatedVideoWidget()
        self._widget.resize(width, height)
        self._widget.set_editable(False)
        self._widget.set_fps(fps)
        self._widget.set_annotations(list(annotations))

    def render(
        self,
        frame_bgr: np.ndarray,
        frame_index: int,
        status_lines: Sequence[str],
        target_overlays: Sequence[dict],
    ) -> np.ndarray:
        """The frame with the annotations, info lines and target overlays
        painted on, as BGR for the writer."""
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        if not frame_rgb.flags["C_CONTIGUOUS"]:
            frame_rgb = frame_rgb.copy()
        h, w = frame_rgb.shape[:2]
        qimg = QImage(frame_rgb.data, w, h, frame_rgb.strides[0], QImage.Format_RGB888).copy()
        widget = self._widget
        widget.set_frame(qimg)
        widget.set_current_frame_index(frame_index)
        widget.set_status_lines(list(status_lines))
        widget.set_target_overlays(list(target_overlays))
        rendered = QImage(self.width, self.height, QImage.Format_ARGB32)
        rendered.fill(Qt.black)
        painter = QPainter(rendered)
        widget.render(painter, QPoint(0, 0))
        painter.end()
        rendered = rendered.convertToFormat(QImage.Format_RGB888)
        # QImage rows are 4-byte aligned: go through bytesPerLine rather
        # than assuming width * 3.
        rows = np.frombuffer(rendered.bits().tobytes(), dtype=np.uint8).reshape(
            (self.height, rendered.bytesPerLine())
        )
        out_rgb = rows[:, : self.width * 3].reshape((self.height, self.width, 3))
        return cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)


def open_writer(path: Path, fps: float, width: int, height: int) -> cv2.VideoWriter:
    return cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))


def render_frames(
    cap: cv2.VideoCapture,
    writer: cv2.VideoWriter,
    renderer: OverlayRenderer,
    start_frame: int,
    end_frame: int,
    fps: float,
    *,
    status_lines_for: StatusLinesFor | None = None,
    target_overlays_for: OverlaysFor | None = None,
    progress: StageProgress | None = None,
) -> bool:
    """Write frames [start_frame, end_frame) of ``cap`` through the
    renderer. Stops early at the end of the stream. Returns False when
    the progress dialog was cancelled."""
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    for frame_idx in range(start_frame, end_frame):
        if progress is not None and progress.was_cancelled():
            return False
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        t_seconds = frame_idx / fps if fps > 0 else 0.0
        status_lines = list(status_lines_for(t_seconds)) if status_lines_for is not None else []
        overlays: list[dict] = []
        if callable(target_overlays_for):
            try:
                overlays = list(target_overlays_for(t_seconds) or [])
            except Exception:
                overlays = []
        writer.write(renderer.render(frame, frame_idx, status_lines, overlays))
        if progress is not None:
            progress.set(frame_idx - start_frame + 1)
    return True


def mux_audio(
    ffmpeg_path: str,
    source_path: Path,
    start_seconds: float,
    video_path: Path,
    out_path: Path,
) -> Path | None:
    """Run ffmpeg to add the source audio to ``video_path``; the muxed
    file, or None when ffmpeg failed or could not be run."""
    cmd = mux_audio_command(ffmpeg_path, source_path, start_seconds, video_path, out_path)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode == 0 and out_path.exists():
        return out_path
    return None


def export_clip_with_overlays(
    source_path: Path,
    start_seconds: float,
    end_seconds: float,
    target_path: Path,
    *,
    fallback_fps: float,
    annotations: Sequence[dict] = (),
    status_lines_for: StatusLinesFor | None = None,
    target_overlays_for: OverlaysFor | None = None,
    progress: StageProgress | None = None,
    ffmpeg_path: str | None = None,
) -> ExportResult:
    """Bake the overlays into ``source_path``'s ``start_seconds`` to
    ``end_seconds`` and save it as ``target_path``.

    ``status_lines_for(t)`` gives the info lines and ``target_overlays_for(t)``
    the product overlays for clip time ``t`` (both optional); ``progress``
    is the "Export Clip" dialog (None runs headless, uncancellable).
    ``ffmpeg_path`` None refuses with NO_FFMPEG, as the replay always did;
    an ffmpeg that fails or cannot run gives the video without audio and
    says so.
    """
    if end_seconds <= start_seconds:
        return ExportResult(False, NO_RANGE)
    if fallback_fps <= 0:
        return ExportResult(False, NO_CLIP)
    if ffmpeg_path is None:
        return ExportResult(False, NO_FFMPEG)
    cap = cv2.VideoCapture(str(source_path))
    if not cap.isOpened():
        return ExportResult(False, f"Failed to open source clip:\n{source_path}")
    temp_dir: Path | None = None
    writer: cv2.VideoWriter | None = None
    try:
        fps = export_fps(cap.get(cv2.CAP_PROP_FPS), fallback_fps)
        start_frame, end_frame = frame_range(start_seconds, end_seconds, fps)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            return ExportResult(False, NO_DIMENSIONS)

        # 1. the writer
        temp_dir = Path(tempfile.mkdtemp(prefix="logfather_export_"))
        temp_video = temp_dir / "video_no_audio.mp4"
        writer = open_writer(temp_video, fps, width, height)
        if not writer.isOpened():
            return ExportResult(False, NO_WRITER)

        # 2. the frames
        renderer = OverlayRenderer(width, height, fps, annotations)
        if progress is not None:
            progress.begin(PROGRESS_LABEL, end_frame - start_frame)
        completed = render_frames(
            cap,
            writer,
            renderer,
            start_frame,
            end_frame,
            fps,
            status_lines_for=status_lines_for,
            target_overlays_for=target_overlays_for,
            progress=progress,
        )
        writer.release()  # ffmpeg reads the file next
        writer = None
        if progress is not None:
            progress.close()
        if not completed:
            return ExportResult(False, CANCELLED)

        # 3. the audio
        muxed = mux_audio(ffmpeg_path, source_path, start_seconds, temp_video, temp_dir / "video_with_audio.mp4")
        final_source = muxed if muxed is not None else temp_video
        try:
            if target_path.exists():
                target_path.unlink()
            shutil.move(str(final_source), str(target_path))
        except Exception as exc:
            return ExportResult(False, f"Export completed but saving failed:\n{exc}")
        return ExportResult(True, "" if muxed is not None else AUDIO_NOT_MUXED)
    finally:
        if writer is not None:
            writer.release()
        cap.release()
        if progress is not None:
            progress.close()
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
