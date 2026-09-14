"""One camera's OCR clock-sync state and wiring (main / Additional CCTV).

The PikPak Replay syncs two pictures to their burnt-in clocks: the main
camera and the Additional CCTV. Until 2026-09-14 the viewer carried the
two as parallel copies (six ``ocr_*`` / ``additional_ocr_*`` attribute
pairs and two near-identical method pairs), so fixes landed on one side
only (review doc, item 7). An ``OcrChannel`` holds everything that
differs between the two - the store, the worker slot, the key tag, the
ROI settings key, the messages and the callbacks - plus the per-clip
state, and the viewer has one code path over a channel.

The pure parts (reading a cached entry, dropping an implausible one, the
filename fallback ladder) live here so they can be tested without
Tesseract or a window. ``docs/OCR_OFFSET.md`` sections 4 and 5 describe
the behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from logfather.core.log import log
from logfather.core.time_alignment import plausible_ocr_offset
from logfather.data.ocr_offset_store import OcrOffsetStore
from logfather.ui.time_ocr import parse_filename_datetime

if TYPE_CHECKING:  # the slot is a QObject; construction is the viewer's job
    from logfather.ui.qt_worker import JobSlot


@dataclass(frozen=True)
class OcrClipRef:
    """Where a channel's clip is right now, as the viewer sees it.

    ``video_path`` is what OpenCV and the OCR read (the cache copy once it
    has landed); ``key_path`` is the path the store key and the PikPak id
    are taken from (the share path when known, so the share copy and the
    cache copy share one entry); ``filename_dt`` is the start the viewer
    already recorded for the clip; ``original_path`` is the share path.
    """

    video_path: Path
    key_path: Path
    filename_dt: datetime | None = None
    original_path: Path | None = None


def resolve_filename_start(
    key_path: Path,
    filename_dt: datetime | None = None,
    original_path: Path | None = None,
) -> datetime | None:
    """The clip's filename time, tried four ways (the ladder the
    Additional CCTV grew on its own; both cameras use it since 2026-09-14):
    the 14-digit stamp in ``key_path``, then the start the viewer recorded,
    then the stamp in the share path, then the share file's mtime."""
    parsed = parse_filename_datetime(key_path)
    if parsed is not None:
        return parsed
    if filename_dt is not None:
        return filename_dt
    if original_path is not None:
        parsed = parse_filename_datetime(original_path)
        if parsed is not None:
            return parsed
        try:
            return datetime.fromtimestamp(original_path.stat().st_mtime)
        except Exception:
            return None
    return None


@dataclass
class OcrChannel:
    """One camera's OCR sync: its wiring (fixed for the life of the viewer)
    and its state (reset per clip)."""

    name: str                       # "main" / "additional"
    label: str                      # "main camera" / "additional camera", for log lines and warnings
    dialog_title: str               # "OCR" / "Additional CCTV OCR", for message boxes
    clip_noun: str                  # "a video" / "an additional CCTV clip", for "Load ... first."
    cam_label: str                  # "" / " (2nd cam)", the activity-bar key suffix
    cache_key_tag: str | None       # suffix on the store key (":additional")
    store_source: str | None        # the "source" tag written to, and required of, store entries
    settings_key: Callable[[str], str]          # PikPak id -> ROI settings key
    clip_ref: Callable[[], OcrClipRef | None]   # the clip showing on this channel, or None
    on_applied: Callable[[], None]              # what to refresh once an offset is in force
    store: OcrOffsetStore = field(default_factory=OcrOffsetStore)
    slot: "JobSlot | None" = None   # the worker slot; one per channel so the two syncs can overlap

    # ---- per-clip state -------------------------------------------------------
    offset_seconds: float | None = None
    frame_offset: int = 0
    video_start_dt: datetime | None = None
    sync_done: bool = False         # an offset is in force for this clip (the Sync button goes green)
    auto_attempted_key: str | None = None   # the auto-open of the Sync window runs once per clip

    def clear_offset(self) -> None:
        """Forget the clip's offset (not ``sync_done`` or the attempted key:
        the callers reset those at the points they always did)."""
        self.offset_seconds = None
        self.frame_offset = 0
        self.video_start_dt = None

    def set_offset(self, video_start_dt: datetime | None, offset_seconds, frame_offset) -> None:
        """An offset is in force for this clip (cached, automatic or manual)."""
        self.offset_seconds = float(offset_seconds)
        self.frame_offset = int(frame_offset)
        self.video_start_dt = video_start_dt
        self.sync_done = True

    def save_offset(self, key: str, offset_seconds, frame_offset) -> None:
        self.store.set(key, offset_seconds, frame_offset, source=self.store_source)

    def cached_entry(self, key: str) -> dict | None:
        """The store entry for ``key`` when it belongs to this channel (the
        additional store only trusts entries tagged ``source="additional"``)."""
        cached = self.store.get(key)
        if not isinstance(cached, dict):
            return None
        if self.store_source and cached.get("source") != self.store_source:
            return None
        return cached

    def load_cached(self, key: str, clip: OcrClipRef) -> bool:
        """Apply the cached offset for ``key`` to this channel's state.

        An entry that fails the plausibility check is deleted from the
        store and reported (``[ocr] ... not plausible; dropped``), both
        cameras alike since 2026-09-14. Returns True when an offset is in
        force afterwards (``set_offset`` ran, so ``sync_done`` is set).
        With an offset but no filename time to anchor it, the offset is
        kept (so no automatic run starts) but there is no start.
        """
        cached = self.cached_entry(key)
        if cached is None:
            return False
        try:
            offset_seconds: float | None = float(cached.get("offset_seconds"))
            frame_offset = int(cached.get("frame_offset", 0))
        except Exception:
            offset_seconds, frame_offset = None, 0
        if offset_seconds is not None and not plausible_ocr_offset(offset_seconds):
            log(
                "ocr",
                f"cached {self.label} offset {offset_seconds:.0f}s for {key} is not "
                "plausible; dropped, using the filename time",
            )
            try:
                self.store.remove(key)
            except Exception:
                pass
            offset_seconds, frame_offset = None, 0
        if offset_seconds is None:
            return False
        filename_dt = resolve_filename_start(clip.key_path, clip.filename_dt, clip.original_path)
        if filename_dt is None:
            self.offset_seconds = offset_seconds
            self.frame_offset = frame_offset
            return False
        self.set_offset(filename_dt + timedelta(seconds=offset_seconds), offset_seconds, frame_offset)
        return True


def channel_property(channel_attr: str, name: str) -> property:
    """A viewer attribute that reads and writes one field of a channel, so
    readers elsewhere keep the old names (``viewer.video_start_dt``,
    ``viewer.additional_ocr_frame_offset``, ...)."""
    return property(
        lambda self: getattr(getattr(self, channel_attr), name),
        lambda self, value: setattr(getattr(self, channel_attr), name, value),
    )
