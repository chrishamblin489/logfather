"""Log-event structures and parsing/formatting shared by the viewer.

Extracted from replay_view: the LogEvent model, timestamp formatting, and
the row->event builder that turns fetched Elastic rows into relative-time
events for playback alignment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# -------- CONFIG FOR CSV→LOG EVENTS --------

SOURCE_COLUMN = "source"
STATE_COLUMN = "state_name"
MESSAGE_COLUMN = "message"

# Example: "16 Nov, 2025 @ 13:17:37.529"

# How long each log entry is considered "active" (seconds)
CSV_EVENT_DURATION_SECONDS = 1.0

if ZoneInfo is not None:
    try:
        LOCAL_TIMEZONE = ZoneInfo("Europe/London")
    except Exception:
        LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
else:
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc


# -------- LOG EVENT STRUCTURES --------

class LogEvent:
    def __init__(self, index, start, end, text):
        self.index = index
        self.start = start  # timedelta (relative)
        self.end = end      # timedelta (relative)
        self.text = text


def format_timecode(td: timedelta) -> str:
    """Format timedelta as HH:MM:SS,mmm (SRT-style timecode)."""
    total_ms = int(td.total_seconds() * 1000)
    if total_ms < 0:
        total_ms = 0
    hours = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    minutes = rem // 60_000
    rem = rem % 60_000
    seconds = rem // 1000
    ms = rem % 1000
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def _to_local_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)


def _format_display_timestamp(dt: datetime) -> str:
    local_dt = _to_local_naive(dt) or dt
    return local_dt.strftime("%H:%M:%S.%f")[:-3]


def build_events_from_rows(rows: list[tuple]):
    print(f"[viewer] build_events_from_rows start ({len(rows)} rows)", flush=True)
    if not rows:
        print("[viewer] build_events_from_rows early exit (no rows)", flush=True)
        return [], [], [], [], [], None
    rows.sort(key=lambda x: x[0])
    t0 = rows[0][0]

    events = []
    display_rows = []
    source_keys = []
    state_keys = []
    message_keys = []

    for i, row in enumerate(rows, start=1):
        if len(row) == 5:
            dt, text, source_key, state_key, message_key = row
        elif len(row) == 4:
            dt, text, source_key, message_key = row
            state_key = ""
        else:
            continue
        start = dt - t0
        end = start + timedelta(seconds=CSV_EVENT_DURATION_SECONDS)
        events.append(LogEvent(i, start, end, text))
        display_rows.append(f"{_format_display_timestamp(dt)}  |  {text}")
        source_keys.append(source_key)
        state_keys.append(state_key)
        message_keys.append(message_key)
    # Tile each event's end to its neighbour's start so there are no gaps.
    for i in range(len(events) - 1):
        events[i].end = events[i + 1].start
    print("[viewer] build_events_from_rows done", flush=True)
    return events, display_rows, source_keys, state_keys, message_keys, t0
