"""Persistence for OCR-derived clip time offsets (Stage 3, review doc).

One JSON file per camera family ({"offsets": {key: {offset_seconds,
frame_offset[, source]}}}). Extracted from ReplayView, which kept this
as five methods threading an optional cache_path through every call. Key
strings are composed by the caller (they need UI-side filename helpers).

Robustness (Chris, 2026-09-12): writes go to a temporary file in the same
folder and are renamed into place, so a crash or a second instance mid-write
cannot leave a half-written file; and a file that fails to parse is set
aside as ``<name>.corrupt-<stamp>`` rather than silently replaced by an
empty store, so the cached offsets can still be recovered by hand.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


class OcrOffsetStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    # ---- file access ----------------------------------------------------------

    def _load(self) -> dict:
        """The stored data, or {} when there is no file. An unreadable file
        is renamed aside (once) and reported, never overwritten in place."""
        if not self.path or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._set_aside(f"unreadable ({exc})")
            return {}
        if not isinstance(data, dict):
            self._set_aside("not a JSON object")
            return {}
        return data

    def _set_aside(self, why: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, aside)
            print(f"[ocr-store] {self.path} is {why}; moved to {aside.name} and starting a fresh store", flush=True)
        except Exception as exc:
            print(f"[ocr-store] {self.path} is {why} and could not be moved aside ({exc}); leaving it untouched", flush=True)

    def _save(self, data: dict) -> None:
        """Atomic: write next to the file, then rename over it."""
        if not self.path:
            return
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[ocr-store] could not write {self.path}: {exc}", flush=True)
            try:
                tmp.unlink()
            except Exception:
                pass

    # ---- API ------------------------------------------------------------------

    def remove(self, key: str) -> None:
        data = self._load()
        offsets = data.get("offsets")
        if isinstance(offsets, dict) and key in offsets:
            del offsets[key]
            self._save(data)

    def get(self, key: str) -> dict | None:
        offsets = self._load().get("offsets", {})
        if not isinstance(offsets, dict):
            return None
        item = offsets.get(key)
        return item if isinstance(item, dict) else None

    def set(
        self,
        key: str,
        offset_seconds: float,
        frame_offset: int,
        *,
        source: str | None = None,
    ) -> None:
        data = self._load()
        offsets = data.get("offsets")
        if not isinstance(offsets, dict):
            offsets = {}
            data["offsets"] = offsets
        entry = {
            "offset_seconds": float(offset_seconds),
            "frame_offset": int(frame_offset),
        }
        if source:
            entry["source"] = source
        offsets[key] = entry
        self._save(data)
