# The OCR offset (camera clock sync)

How Logfather works out the real wall-clock time of every video frame, so
the logs, the timeline playhead, the green clock, the second camera and the
product overlays all line up with the footage. Written 2026-09-12 from a
read of the code; file and function names are given so it can be checked
against the source.

## 1. The problem it solves

Each CCTV clip's filename carries a start time to the second
(`PikPak007 -Line 6-_00_20260912065416.mp4` starts 06:54:16). That stamp
comes from the recorder, not the camera, and is typically a few seconds
away from the clock burnt into the picture, which is the clock the logs
and the picks are compared against. The OCR offset is that difference:

    ocr_offset_seconds = (time read from the burnt-in clock at video position t) - t - filename time
    video_start_dt     = filename time + ocr_offset_seconds

`video_start_dt` is the clip's true start on the camera clock. A second,
whole-frame correction (`ocr_frame_offset`, -2..+2 frames) absorbs the
fact that a new second appears mid-frame.

## 2. The time maths (`src/logfather/core/time_alignment.py`)

`TimeAlignment` is a frozen dataclass rebuilt on every use from the
viewer's current state (`replay_view.alignment`). Three timelines are
involved: video seconds (position in the clip), event seconds (a log line
relative to the first line loaded) and the wall clock.

    effective_offset = sync_offset + time_offset        # log <-> video, plus the user's drift slider
    ocr_correction   = ocr_frame_offset / fps           # 0 when fps <= 0 or no frame offset

    clock_datetime(start, t)               = start + t + ocr_correction         # the burnt-in clock at position t
    playback_datetime(start, t)            = clock_datetime(start, t) - time_offset
    playback_datetime_from_filename(fn, t) = fn + t - time_offset               # no OCR offset known
    video_seconds_for_clock(start, clock)  = (clock - start) - ocr_correction   # used to slave the second camera

    event_to_video(e) = max(0, e + effective_offset - ocr_correction)
    video_to_event(t) = t + ocr_correction - effective_offset

`sync_offset` is set once an offset is known, by
`_apply_auto_sync_if_possible` (`replay_view.py`): the first loaded log
line's time minus `video_start_dt`. `time_offset` is the Drift slider in
the sync strip. Sign conventions are pinned by `tests/test_time_alignment.py`.

Added 2026-09-12: `plausible_ocr_offset(seconds)` returns False for
anything over `MAX_PLAUSIBLE_OCR_OFFSET_S = 900` (15 minutes) either way,
or for NaN and non-numbers. The clock and the filename differ by seconds;
an offset of hours is a misread.

## 3. Reading the clock (`src/logfather/ui/time_ocr.py`)

### Where it looks

A region of interest (ROI) near the top centre of the frame, expressed as
ratios of the frame size: `Roi.top_center_time(width_ratio=0.22,
height_ratio=0.06, y_offset_ratio=0.013, x_offset_ratio=0.0)`. The ratios
are saved per PikPak system in `%USERPROFILE%\ocr_settings.json`:

    { "roi_by_key": { "PikPak007": { "width_ratio": 0.22, "height_ratio": 0.06,
                                     "y_offset_ratio": 0.013, "x_offset_ratio": 0.0 } } }

The key is the `PikPakNNN` found in the clip's path
(`_extract_pikpak_id`). Both cameras of a system share the one entry.

### Preparing the crop

`_preprocess_for_ocr` with the hard-coded `OcrConfig`: grey, scale up 4x
(cubic), Otsu threshold, invert, then a 2x2 morphological close. None of
these are user-adjustable; only the four ROI ratios are.

### Tesseract

`ocr_time_from_frame` calls
`pytesseract.image_to_string(crop, config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789:", lang="eng")`.
The binary is found by `_ensure_tesseract`: a bundled
`tesseract\tesseract.exe` beside the app wins, then an already-configured
path, then `C:\Program Files\Tesseract-OCR`, `C:\Program Files (x86)`, then
`PATH`. If none work it raises with the probe's message; the ROI tool shows
"Tesseract: not available".

### Accepting a reading

The text must match `^\d{2}:\d{2}:\d{2}$` with hour 0-23, minute and
second 0-59 (`_is_valid_time_text`). `_combine_date_and_time` puts that
time on the filename's date, adding a day if it lands more than 12 hours
before the filename time (a clip that starts just before midnight).

### Sampling and voting (`analyze_video_offset`)

Four stages, stopping at the first that yields a start:

1. Coarse scan of the first second, one sample every 0.2 s. When two
   consecutive samples read one second apart, it bisects frame by frame to
   the first frame showing the new second.
2. Dense scan, every frame of the first second.
3. Coarse scan of the first three seconds.
4. Dense scan of the first three seconds.

`_estimate_start_from_samples` prefers second boundaries: each boundary
gives `inferred start = clock at boundary - video time at boundary`; the
median is taken, readings more than 1 s from it are dropped, and the
median of the rest is the answer. Without boundaries it does the same with
every sample and a 2 s window.

`_verify_frame_offset_for_cap` then picks `ocr_frame_offset`: around a
whole second in the middle of the clip it tries offsets -2..+2, reading
the frames either side of the predicted boundary and scoring how many
match the predicted text. The best score wins.

The result is `OcrOffsetResult(video_start_dt, offset_seconds,
frame_offset, report)`. Progress is printed as `[ocr] coarse scan 1s ...`,
`[ocr] verify ...`, `[ocr] analyze total ...`.

## 4. The three ways it runs

### Automatically when a clip opens

`load_video_from_path` clears the previous offset, records the filename
time, and looks up the offset store by key (section 5). A cached offset
that passes the plausibility check is applied at once and the Sync Time
button turns green. Otherwise:

- with `auto_ocr_open_on_missing` on, `_auto_sync_with_ocr` opens the ROI
  tool hidden, runs it, and closes it on success (once per clip);
- else, if logs are already loaded and `auto_ocr_sync` allows it,
  `_confirm_ocr_sync` asks "Run OCR time sync for this video?" (with a
  remember-for-this-session box) and runs the analysis on a worker thread
  (`_ocr_sync_slot`) against a local copy of the clip, because opening the
  share copy directly can stall for 30 s or more.

A missing Tesseract is only printed (`[ocr] auto-sync failed: ...`); the
clip then runs on the filename time.

### The Sync Time button (manual ROI tool)

`open_ocr_roi_tool` opens `SyncCctvTimeWindow`: a scrubbable frame zoomed to
the band around the clock (a tick shows the whole frame), the green OCR
box dragged on the picture by its corners, edges or middle
(`RoiEditorLabel`; `roi_to_ratios` turns the box back into the saved
ratios), a live "OCR: 12:34:56 (valid)" readout, the Tesseract status,
and a colour-coded history of every reading (green valid, red invalid,
amber outlier). A second, purple box (`date_roi_by_key`, default just
left of the time box) carries the date. The window follows a fixed
procedure (Chris, 2026-09-12; the "?" button at the top draws it as a
flowchart, `_draw_help_flowchart`):

- **A.** use the Date and Time boxes as placed;
- **B.** read frame 1's date with `ocr_date_from_frame` and
  `parse_cctv_date` (the camera always writes `DD/MM/YYYY`; slashes the
  OCR reads as 7 are tolerated by reading the ten characters by position);
- **C.** compare it with the filename date; a match means the camera was
  synced from the start, so skip to F;
- **D.** otherwise `find_date_change_frame` / `locate_date_change` scan
  the clip for the frame where the date changes from frame 1's (one read a
  second, then every frame between the last old and first new reading);
- **E.** that frame's date is shown in the "Date (frame x)" panel and
  judged against the filename date: green when they agree, red when not,
  and red "none" when the date never changes;
- **F.** the clock checks read from that frame on the synced date;
- **G.** dragging the date box waits two seconds (`_date_recheck_timer`)
  and reruns from A;
- **H.** the Date and Time box ratios are then stored in
  `ocr_settings.json` for the camera (`_store_box_locations`).

The automatic sync when a clip opens does the same date scan when a date
box is saved for the system.
"Sync Time" in the dialog runs the same four-stage analysis with the
sliders' ROI and applies the result through `_on_offset_approved`, which
stores it and re-syncs the logs. The approval dialog that exists in the
code (`show_verification_dialog`, "Approve offset / Reject") is never
shown: offsets are applied unattended.

### The additional camera

Both pictures run the same code since 2026-09-14
(`src/logfather/ui/ocr_channel.py`). The viewer holds an `OcrChannel` per
camera, `ocr_main` and `ocr_additional`, carrying what differs between
them: the offset store (section 5), the worker `JobSlot` (one each, so the
two syncs can overlap), the store key tag (`:additional`) and the
`source="additional"` tag its entries must carry, the ROI settings key
(`PikPak007` against `PikPak007/additional`), the message wording, a
`clip_ref()` that says which clip the channel is showing, and the refresh
to run once an offset is in force (`_apply_auto_sync_if_possible` for the
main camera, `_refresh_additional_after_sync` for the second). The
per-clip state sits on the channel too: `offset_seconds`, `frame_offset`,
`video_start_dt`, `sync_done` (the Sync button goes green) and the
auto-open guard. `ReplayView.open_sync_cctv_time_for(channel)` and
`_auto_sync_for(channel)` are the one implementation of the Sync Time
window and the automatic run; `open_sync_cctv_time`,
`open_additional_sync_cctv_time`, `_auto_sync_with_ocr` and
`_auto_sync_additional_with_ocr` are one-line wrappers, and
`viewer.video_start_dt`, `additional_video_start_dt`,
`ocr_offset_seconds`, `additional_ocr_frame_offset` and the rest are
properties over the channel state, so the readers elsewhere (the main
window, the overlay controller, `alignment` / `additional_alignment`) did
not change. Where the two copies had drifted apart they now share the
union: the plausibility drop with its log line and the filename fallback
ladder (section 5) run for both, and a failed automatic run opens the Sync
window for either camera with "Please adjust the ROI and try again."

Once both cameras have a start, the second camera is slaved to the first:

    clock = clock_datetime(video_start_dt, t)
    t2    = video_seconds_for_clock(additional_video_start_dt, clock)
    frame = round(t2 * additional_fps) + additional_manual_offset_frames

With either start unknown it falls back to the same clip position.

## 5. Where offsets are kept (`src/logfather/data/ocr_offset_store.py`)

Two JSON files under `%LOCALAPPDATA%\ReplayView\cache`, exempt from the
clip cache's pruning: `ocr_offsets.json` (main camera) and
`ocr_offsets_additional.json` (second camera).

    { "offsets": { "PikPak007:20260912065416": { "offset_seconds": -7.9, "frame_offset": 1 },
                   "PikPak007:20260910103841:additional": { "offset_seconds": -7.7, "frame_offset": 0,
                                                            "source": "additional" } } }

The key is `PikPakNNN:YYYYMMDDHHMMSS` from the clip path
(`_offset_cache_key`), so the share copy and the cached copy of a clip
share one entry. Entries are written when an offset is applied
(`OcrChannel.save_offset`, which adds the channel's `source` tag) and
removed only by the plausibility check.

Reading an entry back is `OcrChannel.load_cached(key, clip)`, the same
for both cameras: the entry must carry the channel's `source` tag (the
additional store's `"additional"`; the main store has none), an
implausible value is deleted and printed, and the start is the clip's
filename time plus the offset. That filename time is tried four ways
(`resolve_filename_start`): the 14-digit stamp in the key path, then the
start the viewer recorded when the clip was chosen, then the stamp in the
share path, then the share file's mtime. Before 2026-09-14 only the
additional camera had this ladder; the main camera stopped at the first
step. An entry with an offset but no filename time to anchor it keeps the
offset (so no automatic run starts) but gives no start.
`tests/test_ocr_channel.py` covers the read, the drop and the ladder with
a fake store.

Since 2026-09-12 the plausibility check runs on both cameras: a cached
offset at clip open and in the automatic path (a failing entry is deleted
from the store and printed as `[ocr] cached offset ... is not plausible;
dropped`), and a fresh automatic result (not saved). The manual tools
refuse an implausible result with a warning. The picture's
View menu shows the offset in use ("OCR offset: +7.9 s, +1 frames" or
"none (clip start taken from the filename)"); clicking it opens the sync
tools.

## 6. What depends on the offset

`update_time_and_overlay` runs on every frame and is the hub:

- **The green clock** (`calc_label`) shows `clock_datetime(video_start_dt, t)`.
  With no offset it shows 00:00:00.000.
- **The timeline playhead** and its HH:MM label, the telemetry panel and
  the product overlays all take `current_time_changed`, which carries
  `playback_datetime(video_start_dt, t)`, or the filename fallback when no
  offset is known.
- **The clip span labels** beside the seek slider use `video_start_dt` or
  the filename time.
- **Log highlighting** (`update_log_highlight`) maps the frame to event
  seconds with `video_to_event` and marks the current line red and the
  next amber.
- **Log ticks on the timeline bar** use `effective_offset` only, without
  the frame correction, so they can sit a frame's width from where a click
  on the log row seeks (noted in the code).
- **Exports with overlays** burn in SKU and target text taken from the
  clock at each frame, so a wrong offset is baked into the exported file.
- **Sync Time button colour**: green means an OCR offset is in force for
  this clip; grey means none, or a cached one was rejected.

## 7. Known weaknesses seen in the code

1. **A systematic misread passes the vote.** If Tesseract reads the same
   wrong hour digit on every frame (7 for 1, 8 for 0), every sample agrees
   and the median accepts it. That produced the -24,774 s offsets found on
   2026-09-12 for two PikPak 007 clips. The 15-minute limit now catches
   hour-scale errors; a misread minute digit (up to 9 minutes) still passes.
2. (Fixed 2026-09-12.) The second camera now runs the same plausibility
   check at its four sites: cached offsets on open and in the automatic
   path are dropped from its store, a fresh automatic result is ignored,
   and the manual tool refuses it with a warning.
3. **Midnight is handled one way only.** A camera clock behind the filename
   across midnight yields an offset of about +86,385 s, which is now
   rejected rather than corrected.
4. **Only `HH:MM:SS` in 24-hour form is accepted.** A 12-hour clock, a date
   in the same region, or a different separator fails every frame.
5. (Fixed 2026-09-12.) After a successful automatic run the Sync Time
   button now goes green like the cached and manual paths; the button
   itself moved to the top bar, left of Conveyor.
6. (Fixed 2026-09-12.) `_estimate_start_from_samples` had no guard for an
   empty sample list and raised on it, which stopped the four-stage scan
   at the first stage that read nothing; it now returns None so the next
   stage runs, matching the ROI tool's copy of the logic.
7. ~~**The ROI is shared by both cameras of a system**, so tuning it for the
   additional camera overwrites the main camera's entry.~~ Fixed
   2026-09-12: the additional camera's boxes are keyed
   `<PikPakNNN>/additional` (`additional_camera_roi_key`), falling back to
   the main camera's entry only until it has one of its own.
8. **The settings dialog couples the two flags**: every apply sets
   `auto_ocr_open_on_missing` equal to `auto_ocr_sync`, and there is no
   separate checkbox, so visiting the settings with auto-sync on turns on
   the pop-up ROI tool for every clip without an offset.
9. ~~**A timeline click seeks by the filename time**
   (`_on_clip_opened_for_navigation`), so it lands `ocr_offset_seconds`
   away from the moment the green clock then reports.~~ Fixed 2026-09-12:
   the click asks the viewer for `video_seconds_for_wall_time`, which
   inverts `clock_datetime` from the OCR-corrected start; the filename
   time is only the fallback when no offset is known.
10. ~~**The store is written non-atomically** and any read error resets it to
    empty, losing every cached offset for that camera.~~ Fixed 2026-09-12:
    `_save` writes `<name>.tmp-<pid>` and `os.replace`s it over the store;
    `_load` moves an unreadable file to `<name>.corrupt-<stamp>` (kept for
    recovery) and starts fresh, so no write ever clobbers the old content.
    Covered by `tests/test_ocr_offset_store.py`.
11. (Partly fixed 2026-09-12.) `tests/test_time_ocr_engine.py` now covers
    the filename parser, the time validator, the midnight rule, the ROI
    maths, the vote over samples and the crop preprocessing. The
    frame-offset check and the Tesseract call remain untested.

## 8. Where to look

| Topic | File |
|---|---|
| Formulas | `src/logfather/core/time_alignment.py` |
| OCR engine, ROI tool, filename parser | `src/logfather/ui/time_ocr.py` |
| Offset store | `src/logfather/data/ocr_offset_store.py` |
| Clip open, automatic sync, Sync Time buttons, second camera | `src/logfather/ui/replay_view.py` |
| One camera's sync state and wiring, cached-offset read, filename ladder | `src/logfather/ui/ocr_channel.py` |
| Settings flags `auto_ocr_sync`, `auto_ocr_open_on_missing` | `src/logfather/data/settings_store.py`, `src/logfather/ui/settings_dialog.py` |
| Tests | `tests/test_time_alignment.py`, `tests/test_ocr_offset_plausibility.py`, `tests/test_ocr_channel.py`, `tests/test_parsing.py` (store) |
