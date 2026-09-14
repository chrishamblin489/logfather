"""OcrChannel: the pure parts of one camera's OCR clock sync (2026-09-14).

A fake store stands in for the JSON file; no Tesseract, no window.
"""
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from logfather.ui.ocr_channel import OcrChannel, OcrClipRef, resolve_filename_start


class FakeStore:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.removed = []

    def get(self, key):
        item = self.entries.get(key)
        return item if isinstance(item, dict) else None

    def remove(self, key):
        self.entries.pop(key, None)
        self.removed.append(key)

    def set(self, key, offset_seconds, frame_offset, *, source=None):
        entry = {"offset_seconds": float(offset_seconds), "frame_offset": int(frame_offset)}
        if source:
            entry["source"] = source
        self.entries[key] = entry


CLIP = Path("Z:/public/PikPak007/2026/09/12/PikPak007 -Line 6-_00_20260912065416.mp4")
CLIP_START = datetime(2026, 9, 12, 6, 54, 16)
KEY = "PikPak007:20260912065416"


def make_channel(store, source=None):
    return OcrChannel(
        name="additional" if source else "main",
        label=f"{'additional' if source else 'main'} camera",
        dialog_title="OCR",
        clip_noun="a video",
        cam_label="",
        cache_key_tag=source,
        store_source=source,
        settings_key=lambda pikpak_id: pikpak_id,
        clip_ref=lambda: None,
        on_applied=lambda: None,
        store=store,
    )


def clip(key_path=CLIP, filename_dt=None, original_path=None):
    return OcrClipRef(video_path=key_path, key_path=key_path, filename_dt=filename_dt, original_path=original_path)


# ---- cached offsets ----------------------------------------------------------

def test_cached_offset_is_applied_from_the_filename_time():
    ch = make_channel(FakeStore({KEY: {"offset_seconds": -7.9, "frame_offset": 1}}))
    assert ch.load_cached(KEY, clip()) is True
    assert ch.offset_seconds == -7.9
    assert ch.frame_offset == 1
    assert ch.video_start_dt == CLIP_START + timedelta(seconds=-7.9)
    assert ch.sync_done is True


def test_implausible_cached_offset_is_dropped_from_the_store(capsys):
    store = FakeStore({KEY: {"offset_seconds": -24774.2, "frame_offset": 0}})
    ch = make_channel(store)
    assert ch.load_cached(KEY, clip()) is False
    assert store.removed == [KEY] and KEY not in store.entries
    assert ch.offset_seconds is None and ch.frame_offset == 0 and ch.video_start_dt is None
    assert ch.sync_done is False
    out = capsys.readouterr().out
    assert "[ocr] cached main camera offset -24774s" in out and "not plausible; dropped" in out


def test_unparseable_entry_leaves_the_channel_empty():
    ch = make_channel(FakeStore({KEY: {"offset_seconds": "abc"}}))
    assert ch.load_cached(KEY, clip()) is False
    assert ch.offset_seconds is None and ch.video_start_dt is None


def test_missing_entry():
    ch = make_channel(FakeStore())
    assert ch.load_cached(KEY, clip()) is False
    assert ch.sync_done is False


def test_additional_store_only_trusts_its_own_tag():
    untagged = FakeStore({KEY + ":additional": {"offset_seconds": -7.7, "frame_offset": 0}})
    ch = make_channel(untagged, source="additional")
    assert ch.cached_entry(KEY + ":additional") is None
    assert ch.load_cached(KEY + ":additional", clip()) is False

    tagged = FakeStore({KEY + ":additional": {"offset_seconds": -7.7, "frame_offset": 0, "source": "additional"}})
    ch = make_channel(tagged, source="additional")
    assert ch.load_cached(KEY + ":additional", clip()) is True
    assert ch.video_start_dt == CLIP_START + timedelta(seconds=-7.7)


def test_main_store_takes_untagged_entries():
    ch = make_channel(FakeStore({KEY: {"offset_seconds": 2.0, "frame_offset": -1}}))
    assert ch.cached_entry(KEY) == {"offset_seconds": 2.0, "frame_offset": -1}


def test_offset_without_a_filename_time_is_kept_but_has_no_start():
    ch = make_channel(FakeStore({"PikPak007:clip": {"offset_seconds": 1.5, "frame_offset": 0}}))
    no_stamp = Path("Z:/public/PikPak007/clip.mp4")
    assert ch.load_cached("PikPak007:clip", clip(key_path=no_stamp)) is False
    assert ch.offset_seconds == 1.5       # so no automatic run starts for this clip
    assert ch.video_start_dt is None
    assert ch.sync_done is False


# ---- applying and saving -----------------------------------------------------

def test_save_offset_carries_the_channel_source_tag():
    main_store, add_store = FakeStore(), FakeStore()
    make_channel(main_store).save_offset(KEY, -7.9, 1)
    make_channel(add_store, source="additional").save_offset(KEY + ":additional", -7.7, 0)
    assert main_store.entries[KEY] == {"offset_seconds": -7.9, "frame_offset": 1}
    assert add_store.entries[KEY + ":additional"] == {"offset_seconds": -7.7, "frame_offset": 0, "source": "additional"}


def test_set_offset_coerces_and_marks_sync_done():
    ch = make_channel(FakeStore())
    ch.set_offset(CLIP_START, "-3.25", 2.0)
    assert ch.offset_seconds == -3.25 and isinstance(ch.offset_seconds, float)
    assert ch.frame_offset == 2 and isinstance(ch.frame_offset, int)
    assert ch.video_start_dt == CLIP_START and ch.sync_done is True


def test_clear_offset_keeps_sync_done_and_the_attempted_key():
    ch = make_channel(FakeStore())
    ch.set_offset(CLIP_START, 1.0, 0)
    ch.auto_attempted_key = KEY
    ch.clear_offset()
    assert ch.offset_seconds is None and ch.frame_offset == 0 and ch.video_start_dt is None
    assert ch.sync_done is True and ch.auto_attempted_key == KEY   # the callers reset these themselves


# ---- the filename fallback ladder -------------------------------------------

def test_ladder_takes_the_stamp_in_the_key_path_first():
    recorded = datetime(2000, 1, 1)
    assert resolve_filename_start(CLIP, recorded, Path("Z:/other_20260101000000.mp4")) == CLIP_START


def test_ladder_falls_back_to_the_recorded_start():
    recorded = datetime(2026, 9, 12, 7, 0, 0)
    assert resolve_filename_start(Path("C:/cache/clip.mp4"), recorded, None) == recorded


def test_ladder_falls_back_to_the_share_path_stamp():
    assert resolve_filename_start(Path("C:/cache/clip.mp4"), None, CLIP) == CLIP_START


def test_ladder_falls_back_to_the_share_file_mtime(tmp_path):
    original = tmp_path / "clip.mp4"
    original.write_bytes(b"")
    stamp = datetime(2026, 9, 10, 10, 38, 41)
    os.utime(original, (stamp.timestamp(), stamp.timestamp()))
    assert resolve_filename_start(Path("C:/cache/clip.mp4"), None, original) == stamp


def test_ladder_gives_up_quietly(tmp_path):
    missing = tmp_path / "gone.mp4"
    assert resolve_filename_start(Path("C:/cache/clip.mp4"), None, missing) is None
    assert resolve_filename_start(Path("C:/cache/clip.mp4"), None, None) is None


def test_load_cached_uses_the_ladder_for_a_cache_copy_without_a_stamp():
    ch = make_channel(FakeStore({KEY: {"offset_seconds": -7.9, "frame_offset": 0}}))
    cache_copy = Path("C:/cache/abcdef.mp4")
    assert ch.load_cached(KEY, clip(key_path=cache_copy, original_path=CLIP)) is True
    assert ch.video_start_dt == CLIP_START + timedelta(seconds=-7.9)
