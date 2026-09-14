"""Console output for the app: an always-on informational channel and an
opt-in debug channel. Pure Python, no Qt.

``log(tag, msg)`` prints ``[tag] msg`` every time - these are the lines
Chris and the lead read in ``app_vNNN.log`` (loaded/failed/collected/...).
``dbg(tag, msg)`` prints only when ``LOGFATHER_DEBUG`` is set: ``1`` (or
``all``) enables every tag, a comma list such as ``timeline,ocr`` enables
just those. The older ``LOGFATHER_DEBUG_PLAYHEAD=1`` still enables the
``playhead`` tag.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

ENV_VAR = "LOGFATHER_DEBUG"
_ALL = {"1", "true", "yes", "on", "all", "*"}
_LEGACY_VARS = {"LOGFATHER_DEBUG_PLAYHEAD": "playhead"}


def log(tag: str, msg: str) -> None:
    """Informational line: always printed, flushed so a captured log is live."""
    print(f"[{tag}] {msg}", flush=True)


def debug_enabled(tag: str) -> bool:
    """True when LOGFATHER_DEBUG enables *tag*. Read per call so tests and a
    running session can flip it; the cost is one environment lookup."""
    value = os.environ.get(ENV_VAR, "")
    if value.strip():
        wanted = {part.strip().lower() for part in value.split(",") if part.strip()}
        if wanted & _ALL or tag.lower() in wanted:
            return True
    for var, legacy_tag in _LEGACY_VARS.items():
        if legacy_tag == tag and os.environ.get(var):
            return True
    return False


def dbg(tag: str, msg: str) -> None:
    """Debug/trace line: printed only when ``debug_enabled(tag)``."""
    if debug_enabled(tag):
        print(f"[{tag}] {msg}", flush=True)


@contextmanager
def timed(tag: str, label: str, threshold_s: float = 0.0) -> Iterator[None]:
    """Time the block; report ``[tag] label: 123ms`` through ``dbg`` when the
    elapsed time exceeds *threshold_s* (so perf guards stay opt-in)."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        if elapsed > threshold_s:
            dbg(tag, f"{label}: {elapsed * 1000:.0f}ms")
