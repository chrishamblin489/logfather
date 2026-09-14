"""Local clip cache: copies CCTV clips from the share, prefetches, prunes.

Extracted from replay_view.ReplayView. One instance lives on the viewer;
Main_Window reaches it through thin forwarders the viewer keeps.

Threading model:
- `executor` (1 worker) runs click-triggered downloads and cache stats.
- `prefetch_executor` (2 workers) runs background prefetch copies.
- Completion is delivered by emitting signals from the worker thread;
  Qt queues them to the main thread (this object lives there).
- prune() is serialized by a lock: it used to run concurrently on up to
  three executor threads, racing its own directory scan and unlink calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import QObject, Signal

from logfather.core.log import log

CACHE_META_SUFFIX = ".meta.json"
CACHE_MAX_BYTES = 30 * 1024 * 1024 * 1024
CACHE_MAX_AGE_DAYS = 30
# Cache-root files that are not clip copies and must never be pruned.
_NON_CLIP_FILENAMES = {"ocr_offsets.json", "ocr_offsets_additional.json"}


def default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache"
    return Path.home() / ".videolog_cache"


class ClipCache(QObject):
    # Path on the share whose local copy just became available.
    clip_ready = Signal(object)
    # (source path as str, ok) for every finished download/prefetch job,
    # including failures — the viewer resolves pending clip loads on this.
    transfer_finished = Signal(str, bool)
    # (source path as str, bytes copied, total bytes) per copied chunk —
    # emitted from worker threads; delivery to the UI thread is queued
    # because this QObject lives there.
    transfer_progress = Signal(str, object, object)

    def __init__(
        self,
        protected_paths_provider: Callable[[], Iterable[str | None]] | None = None,
        parent: QObject | None = None,
        root: Path | None = None,
    ):
        super().__init__(parent)
        self.root = root if root is not None else default_cache_root()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.root = Path.home() / ".videolog_cache"
            self.root.mkdir(parents=True, exist_ok=True)
        self._protected_paths_provider = protected_paths_provider
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.prefetch_executor = ThreadPoolExecutor(max_workers=2)
        self._prefetch_pending: set[str] = set()
        self._prefetch_futures: dict[str, Future] = {}
        self._prune_lock = threading.Lock()
        # Set at shutdown so an in-flight SMB copy aborts at the next chunk.
        # Python joins executor threads at interpreter exit, so without this
        # the window closes but the process lives until the copy finishes.
        self._shutdown_event = threading.Event()

    # ---- paths & metadata -------------------------------------------------

    def cache_path_for(self, original_path: Path) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(str(original_path).encode("utf-8")).hexdigest()[:16]
        filename = f"{original_path.stem}_{key}{original_path.suffix}"
        return self.root / filename

    def meta_path_for(self, cache_path: Path) -> Path:
        return cache_path.with_name(cache_path.name + CACHE_META_SUFFIX)

    def annotations_dir(self) -> Path:
        path = self.root / "annotations"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return path

    def annotation_path_for(self, cache_path: Path) -> Path:
        return self.annotations_dir() / f"{cache_path.stem}.json"

    def read_meta(self, cache_path: Path) -> dict | None:
        try:
            meta_path = self.meta_path_for(cache_path)
            if not meta_path.exists():
                return None
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _write_meta(self, source_path: Path, cache_path: Path) -> None:
        try:
            source_stat = source_path.stat()
            payload = {
                "source_path": str(source_path),
                "source_size": int(source_stat.st_size),
                "source_mtime_ns": int(source_stat.st_mtime_ns),
                "cached_at": datetime.now().isoformat(),
            }
            self.meta_path_for(cache_path).write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    def _entry_paths(self, cache_path: Path) -> list[Path]:
        return [cache_path, self.meta_path_for(cache_path), self.annotation_path_for(cache_path)]

    def invalidate(self, cache_path: Path) -> None:
        for target in self._entry_paths(cache_path):
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass

    def touch_entry(self, cache_path: Path) -> None:
        now_ts = time.time()
        for target in self._entry_paths(cache_path):
            try:
                if target.exists():
                    os.utime(target, (now_ts, now_ts))
            except Exception:
                pass

    # ---- validity & copying ----------------------------------------------

    def is_cached_copy_current(self, source_path: Path, cache_path: Path) -> bool:
        try:
            if not cache_path.exists():
                return False
        except Exception:
            return False
        try:
            source_stat = source_path.stat()
        except Exception:
            return True
        meta = self.read_meta(cache_path)
        if meta:
            try:
                return (
                    int(meta.get("source_size")) == int(source_stat.st_size)
                    and int(meta.get("source_mtime_ns")) == int(source_stat.st_mtime_ns)
                )
            except Exception:
                pass
        try:
            cache_stat = cache_path.stat()
        except Exception:
            return False
        return int(cache_stat.st_size) == int(source_stat.st_size)

    def ensure_cached_copy(self, source_path: Path, cache_path: Path) -> bool:
        if self.is_cached_copy_current(source_path, cache_path):
            self.touch_entry(cache_path)
            return True
        self.invalidate(cache_path)
        return self.copy_to_cache(source_path, cache_path)

    def get_valid_cached_path(self, original_path: Path) -> Path | None:
        try:
            cache_path = self.cache_path_for(original_path)
        except Exception:
            return None
        if self.is_cached_copy_current(original_path, cache_path):
            self.touch_entry(cache_path)
            return cache_path
        return None

    _COPY_CHUNK_BYTES = 4 * 1024 * 1024

    def copy_to_cache(self, source_path: Path, cache_path: Path) -> bool:
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".part")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if tmp_path.exists():
                tmp_path.unlink()
            # Chunked instead of shutil.copy2 so shutdown can abort a copy
            # mid-file; a whole-file SMB copy is uninterruptible and kept the
            # process alive for minutes after the window closed.
            with open(source_path, "rb") as src, open(tmp_path, "wb") as dst:
                try:
                    # fstat on the open handle: no extra WAN round trip.
                    total_bytes = int(os.fstat(src.fileno()).st_size)
                except Exception:
                    total_bytes = 0
                done_bytes = 0
                self.transfer_progress.emit(str(source_path), 0, total_bytes)
                while True:
                    if self._shutdown_event.is_set():
                        raise InterruptedError("clip cache shutting down")
                    chunk = src.read(self._COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done_bytes += len(chunk)
                    self.transfer_progress.emit(str(source_path), done_bytes, total_bytes)
            shutil.copystat(source_path, tmp_path)
            tmp_path.replace(cache_path)
            self._write_meta(source_path, cache_path)
            self.touch_entry(cache_path)
            self.prune()
            return True
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False

    def _check_or_copy(self, source_path: Path, cache_path: Path) -> bool:
        if self.is_cached_copy_current(source_path, cache_path):
            self.touch_entry(cache_path)
            return True
        return self.copy_to_cache(source_path, cache_path)

    # ---- async transfers --------------------------------------------------

    def prefetch(self, paths: list[Path]) -> None:
        for path in paths or []:
            try:
                path_obj = Path(path)
                cache_path = self.cache_path_for(path_obj)
            except Exception:
                continue
            key = str(cache_path)
            if key in self._prefetch_pending:
                continue
            # No filesystem checks here: this runs on the UI thread, and even
            # a stat() on the share can block for seconds while copies
            # saturate the link. The worker decides cached-vs-copy.
            self._prefetch_pending.add(key)
            future = self.prefetch_executor.submit(self._check_or_copy, path_obj, cache_path)
            self._prefetch_futures[key] = future
            future.add_done_callback(
                lambda fut, p=path_obj, k=key: self._job_done(fut, p, k)
            )

    def download_with_priority(self, source_path: Path, cache_path: Path) -> None:
        """Download one clip on the click executor, jumping any prefetch queue.

        If a background prefetch of the same clip is still queued it is
        cancelled and resubmitted here; one that is actively copying is
        simply reused (its completion signal serves both purposes)."""
        key = str(cache_path)
        if key in self._prefetch_pending:
            prefetch_future = self._prefetch_futures.get(key)
            if prefetch_future is None or not prefetch_future.cancel():
                return  # actively copying — reuse it
            self._prefetch_futures.pop(key, None)
        else:
            self._prefetch_pending.add(key)
        future = self.executor.submit(self.copy_to_cache, source_path, cache_path)
        future.add_done_callback(
            lambda fut, p=source_path, k=key: self._job_done(fut, p, k)
        )

    def cancel_queued_prefetches(self, protected_key: str | None = None) -> None:
        """Drop prefetch jobs that haven't started copying (e.g. on a
        day/system switch). Active copies finish. `protected_key` shields the
        copy a click-triggered clip load is waiting on — a cancelled future
        never delivers the completion that opens the clip."""
        for key, future in list(self._prefetch_futures.items()):
            if key == protected_key:
                continue
            if future.cancel():
                self._prefetch_futures.pop(key, None)
                self._prefetch_pending.discard(key)

    def _job_done(self, future: Future, source_path: Path, key: str) -> None:
        # Runs on the worker thread; signal emission is queued to the main
        # thread because this QObject lives there.
        if future.cancelled():
            return  # superseded by a click-triggered download of the same clip
        try:
            ok = bool(future.result())
        except Exception:
            ok = False
        self._prefetch_pending.discard(key)
        self._prefetch_futures.pop(key, None)
        if ok:
            self.clip_ready.emit(Path(source_path))
        self.transfer_finished.emit(str(source_path), ok)

    # ---- stats & pruning --------------------------------------------------

    def calculate_stats(self) -> tuple[int, int]:
        if not self.root.exists():
            return 0, 0
        total = 0
        count = 0
        for entry in self.root.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
        return count, total

    def _group_size(self, paths: list[Path]) -> int:
        total = 0
        for path in paths:
            try:
                if path.exists():
                    total += int(path.stat().st_size)
            except Exception:
                continue
        return total

    def _group_last_used(self, paths: list[Path]) -> float:
        latest = 0.0
        for path in paths:
            try:
                if path.exists():
                    latest = max(latest, float(path.stat().st_mtime))
            except Exception:
                continue
        return latest

    def _iter_groups(self) -> list[dict]:
        if not self.root.exists():
            return []
        try:
            entries = list(self.root.iterdir())
        except Exception:
            return []
        groups: list[dict] = []
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(".part") or name.endswith(CACHE_META_SUFFIX):
                continue
            if name in _NON_CLIP_FILENAMES:
                continue
            paths = self._entry_paths(entry)
            groups.append(
                {
                    "cache_path": entry,
                    "paths": paths,
                    "size": self._group_size(paths),
                    "last_used": self._group_last_used(paths),
                }
            )
        return groups

    def _protected_paths(self) -> set[str]:
        protected: set[str] = set()
        if self._protected_paths_provider is None:
            return protected
        try:
            actives = list(self._protected_paths_provider())
        except Exception:
            return protected
        for active in actives:
            if not active:
                continue
            try:
                protected.add(str(Path(active).resolve()))
            except Exception:
                protected.add(str(active))
        return protected

    def prune(self) -> None:
        # Copies finish on three different worker threads; only one prune at
        # a time may scan and delete, the others just skip.
        if not self._prune_lock.acquire(blocking=False):
            return
        try:
            self._prune_locked()
        except Exception as exc:
            log("cache", f"prune failed: {exc}")
        finally:
            self._prune_lock.release()

    def _prune_locked(self) -> None:
        groups = self._iter_groups()
        if not groups:
            return
        cutoff_ts = time.time() - (CACHE_MAX_AGE_DAYS * 24 * 60 * 60)
        protected_paths = self._protected_paths()

        def _delete_group(group: dict) -> None:
            cache_path = group.get("cache_path")
            if isinstance(cache_path, Path):
                try:
                    resolved = str(cache_path.resolve())
                except Exception:
                    resolved = str(cache_path)
                if resolved in protected_paths:
                    return
            for path in group.get("paths", []):
                try:
                    if isinstance(path, Path) and path.exists():
                        path.unlink()
                except Exception:
                    continue

        for group in groups:
            if group.get("last_used", 0.0) < cutoff_ts:
                _delete_group(group)

        groups = [g for g in self._iter_groups() if g.get("size", 0) > 0]
        total_bytes = sum(int(g.get("size", 0)) for g in groups)
        if total_bytes <= CACHE_MAX_BYTES:
            return
        groups.sort(key=lambda g: (float(g.get("last_used", 0.0)), str(g.get("cache_path"))))
        for group in groups:
            if total_bytes <= CACHE_MAX_BYTES:
                break
            cache_path = group.get("cache_path")
            if isinstance(cache_path, Path):
                try:
                    resolved = str(cache_path.resolve())
                except Exception:
                    resolved = str(cache_path)
                if resolved in protected_paths:
                    continue
            size = int(group.get("size", 0))
            _delete_group(group)
            total_bytes -= size

    # ---- lifecycle --------------------------------------------------------

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self.cancel_queued_prefetches()
        for executor in (self.executor, self.prefetch_executor):
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
