"""The OCR engine's pure logic (2026-09-12): filename stamps, clock text
validation, the midnight rule, the ROI maths, the vote over samples, and
the crop preprocessing. Tesseract itself is not needed."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from logfather.ui.time_ocr import (
    OcrConfig,
    Roi,
    RoiSettings,
    _combine_date_and_time,
    _estimate_start_from_samples,
    _estimate_start_from_transitions,
    _is_valid_time_text,
    _normalize_ocr_text,
    _preprocess_for_ocr,
    _time_text_to_seconds,
    load_roi_settings,
    locate_date_change,
    parse_cctv_date,
    parse_filename_datetime,
    roi_to_ratios,
    save_roi_settings,
)

BASE = datetime(2026, 9, 12, 6, 54, 16)


# ---- filename stamps ------------------------------------------------------

def test_parse_filename_datetime_reads_the_14_digit_stamp():
    assert parse_filename_datetime("PikPak007 -Line 6-_00_20260912065416.mp4") == BASE


def test_parse_filename_datetime_survives_the_cache_copy_hash_suffix():
    assert parse_filename_datetime(r"C:\cache\PikPak007 -Line 6-_00_20260912065416_38f68ae6c52be30c.mp4") == BASE


def test_parse_filename_datetime_without_a_stamp_is_none():
    assert parse_filename_datetime("clip.mp4") is None
    assert parse_filename_datetime("PikPak007_2026091206.mp4") is None  # too short


# ---- clock text -----------------------------------------------------------

def test_normalize_collapses_whitespace():
    assert _normalize_ocr_text("  06:54 :16 \n") == "06:54 :16"


@pytest.mark.parametrize("text", ["06:54:16", "00:00:00", "23:59:59"])
def test_valid_time_text(text):
    assert _is_valid_time_text(text)


@pytest.mark.parametrize("text", ["24:00:00", "06:60:16", "06:54:60", "6:54:16", "06-54-16", "06:54:16 PM", "", "065416"])
def test_invalid_time_text(text):
    assert not _is_valid_time_text(text)


def test_time_text_to_seconds():
    assert _time_text_to_seconds("00:00:01") == 1
    assert _time_text_to_seconds("23:59:59") == 86399
    assert _time_text_to_seconds("24:00:00") is None
    assert _time_text_to_seconds("nonsense") is None


# ---- the midnight rule ----------------------------------------------------

def test_combine_keeps_the_filename_date_for_a_nearby_clock():
    assert _combine_date_and_time(BASE, "06:54:23") == BASE.replace(second=23)
    # a clock a few seconds behind the filename stays on the same day
    assert _combine_date_and_time(BASE, "06:54:09") == BASE.replace(second=9)


def test_combine_rolls_to_the_next_day_across_midnight():
    late = datetime(2026, 9, 12, 23, 59, 58)
    assert _combine_date_and_time(late, "00:00:01") == datetime(2026, 9, 13, 0, 0, 1)


def test_combine_does_not_roll_back_a_clock_behind_the_filename_across_midnight():
    # Known one-way behaviour (docs/OCR_OFFSET.md): the clock reads
    # 23:59:50 just after a filename of 00:00:05 and lands 24 h ahead.
    early = datetime(2026, 9, 13, 0, 0, 5)
    assert _combine_date_and_time(early, "23:59:50") == datetime(2026, 9, 13, 23, 59, 50)


# ---- the ROI --------------------------------------------------------------

def test_roi_top_center_time_defaults_and_clamps():
    roi = Roi.top_center_time(1920, 1080)
    assert (roi.w, roi.h) == (round(1920 * 0.22), round(1080 * 0.06))
    assert roi.x == round((1920 - roi.w) / 2) and roi.y == round(1080 * 0.013)
    wide = Roi.top_center_time(1920, 1080, width_ratio=5.0, height_ratio=0.0, y_offset_ratio=2.0)
    assert wide.w == 1920 and wide.h == round(1080 * 0.01) and wide.y == round(1080 * 0.9)


def test_roi_x_offset_shifts_the_box():
    base = Roi.top_center_time(1000, 500)
    shifted = Roi.top_center_time(1000, 500, x_offset_ratio=0.1)
    assert shifted.x == base.x + 100 and (shifted.y, shifted.w, shifted.h) == (base.y, base.w, base.h)


def test_roi_crop_stays_inside_the_frame():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = Roi(x=190, y=95, w=50, h=50).crop(frame)
    assert crop.shape[:2] == (5, 10)
    assert Roi(x=-5, y=-5, w=10, h=10).crop(frame).shape[:2] == (10, 10)  # origin clamps to 0, size kept


# ---- the vote over samples ------------------------------------------------

def _sample(frame, t, text, base=BASE):
    return (frame, t, _combine_date_and_time(base, text), text)


def test_transitions_take_the_median_second_boundary():
    # 25 fps; the clock ticks to :17 at video t=0.60 and to :18 at t=1.60
    samples = [
        _sample(0, 0.00, "06:54:16"), _sample(15, 0.60, "06:54:17"),
        _sample(30, 1.20, "06:54:17"), _sample(40, 1.60, "06:54:18"),
    ]
    best, median, outliers = _estimate_start_from_transitions(samples)
    assert best == BASE + timedelta(seconds=0.40)
    assert median == best and outliers == []


def test_transitions_drop_an_outlier_boundary():
    samples = [
        _sample(0, 0.0, "06:54:16"), _sample(10, 0.4, "06:54:17"),
        _sample(20, 0.8, "06:54:17"), _sample(35, 1.4, "06:54:18"),
        _sample(60, 2.4, "06:54:18"), _sample(90, 3.6, "06:54:19"),   # a late read: start 3 s off
    ]
    best, _median, outliers = _estimate_start_from_transitions(samples)
    assert best == BASE + timedelta(seconds=0.6)
    assert [text for _dt, text in outliers] == ["06:54:19"]


def test_transitions_ignore_jumps_and_repeats_and_wrap_at_midnight():
    assert _estimate_start_from_transitions([_sample(0, 0.0, "06:54:16")]) is None
    assert _estimate_start_from_transitions([_sample(0, 0.0, "06:54:16"), _sample(5, 0.2, "06:54:19")]) is None
    late = datetime(2026, 9, 12, 23, 59, 59)
    samples = [_sample(0, 0.0, "23:59:59", late), _sample(10, 0.4, "00:00:00", late)]
    best, _m, _o = _estimate_start_from_transitions(samples)
    assert best == datetime(2026, 9, 13, 0, 0, 0) - timedelta(seconds=0.4)


def test_start_from_samples_prefers_transitions_then_falls_back_to_the_median():
    with_boundary = [_sample(0, 0.0, "06:54:16"), _sample(10, 0.4, "06:54:17")]
    assert _estimate_start_from_samples(with_boundary, BASE) == BASE + timedelta(seconds=0.6)
    flat = [_sample(0, 0.0, "06:54:16"), _sample(5, 0.2, "06:54:16"), _sample(10, 0.4, "06:54:16")]
    assert _estimate_start_from_samples(flat, BASE) == BASE - timedelta(seconds=0.2)


def test_start_from_samples_with_nothing_read_is_none():
    assert _estimate_start_from_samples([], BASE) is None


# ---- the crop preprocessing -----------------------------------------------

def test_preprocess_scales_and_binarises():
    crop = np.full((10, 40, 3), 40, dtype=np.uint8)
    crop[3:7, 10:30] = 230  # light digits on a dark clock
    out = _preprocess_for_ocr(crop, OcrConfig())
    assert out.shape == (40, 160)
    assert set(np.unique(out).tolist()) <= {0, 255}
    # invert=True turns the light digits black on white for Tesseract
    assert out[20, 80] == 0 and out[2, 2] == 255


# ---- the dragged box back to ratios -----------------------------------------

def test_roi_to_ratios_round_trips_through_top_center_time():
    frame_w, frame_h = 1920, 1080
    roi = Roi(x=700, y=30, w=500, h=70)
    ratios = roi_to_ratios(roi, frame_w, frame_h)
    back = Roi.top_center_time(frame_w, frame_h, width_ratio=ratios.width_ratio, height_ratio=ratios.height_ratio,
                               y_offset_ratio=ratios.y_offset_ratio, x_offset_ratio=ratios.x_offset_ratio)
    assert (back.x, back.y, back.w, back.h) == (roi.x, roi.y, roi.w, roi.h)


def test_roi_to_ratios_clamps_to_the_roi_limits():
    ratios = roi_to_ratios(Roi(x=0, y=1075, w=2, h=1), 1920, 1080)
    assert ratios.width_ratio == 0.01 and ratios.height_ratio == 0.01 and ratios.y_offset_ratio == 0.9


# ---- the date box --------------------------------------------------------------

def test_parse_cctv_date_reads_dd_slash_mm_slash_yyyy():
    from datetime import date
    assert parse_cctv_date("10/09/2026") == date(2026, 9, 10)
    assert parse_cctv_date(" 12 / 09 / 2026 ") == date(2026, 9, 12)   # OCR spacing
    assert parse_cctv_date("10/09/2026 10:31:01 THU") == date(2026, 9, 10)


def test_parse_cctv_date_survives_slashes_read_as_sevens_or_dropped():
    from datetime import date
    assert parse_cctv_date("0170171970") == date(1970, 1, 1)      # both slashes read as 7
    assert parse_cctv_date("10709/2026") == date(2026, 9, 10)     # one slash read as 7
    assert parse_cctv_date("10092026") == date(2026, 9, 10)       # slashes dropped
    assert parse_cctv_date("01/01/1970") == date(1970, 1, 1)


def test_parse_cctv_date_rejects_what_cannot_be_the_camera_format():
    assert parse_cctv_date("") is None
    assert parse_cctv_date("31/02/2026") is None       # not a real date
    assert parse_cctv_date("2026/09/12") is None       # year first: positions give month 26
    assert parse_cctv_date("1/9/2026") is None         # too short for the fixed layout
    assert parse_cctv_date("103109") is None


def test_roi_settings_sections_are_independent(tmp_path):
    path = tmp_path / "ocr_settings.json"
    save_roi_settings(path, "PikPak007", RoiSettings(0.22, 0.06, 0.013, 0.0))
    save_roi_settings(path, "PikPak007", RoiSettings(0.2, 0.05, 0.01, -0.3), section="date_roi_by_key")
    assert load_roi_settings(path, "PikPak007").x_offset_ratio == 0.0
    assert load_roi_settings(path, "PikPak007", section="date_roi_by_key").x_offset_ratio == -0.3


# ---- the camera date sync search ------------------------------------------------

def test_locate_date_change_finds_the_first_new_frame():
    from datetime import date
    old, new = date(1970, 1, 1), date(2026, 9, 12)
    reads = []

    def read_date(idx):
        reads.append(idx)
        return old if idx < 312 else new

    assert locate_date_change(read_date, 15000, 25) == (312, old, new)
    assert max(reads) < 400 and len(reads) < 40   # coarse steps, then a bisection


def test_locate_date_change_when_the_date_never_changes():
    from datetime import date
    assert locate_date_change(lambda idx: date(1970, 1, 1), 500, 25) is None
    assert locate_date_change(lambda idx: None, 500, 25) is None


def test_locate_date_change_skips_unreadable_frames():
    from datetime import date
    old, new = date(1970, 1, 1), date(2026, 9, 12)

    def read_date(idx):
        if idx % 7 == 3:
            return None       # a frame the OCR cannot read
        return old if idx < 100 else new

    change = locate_date_change(read_date, 1000, 25)
    assert change is not None and change[2] == new and 100 <= change[0] <= 101


def test_frame_or_first_never_uses_numpy_truthiness():
    """`first or fallback` on arrays raises ValueError; the helper must not."""
    from logfather.ui.time_ocr import SyncCctvTimeWindow

    class Stub:
        def __init__(self, first):
            self._first = first

        def _first_frame(self):
            return self._first

    fallback = np.zeros((4, 4, 3), dtype=np.uint8)
    first = np.ones((4, 4, 3), dtype=np.uint8)
    assert SyncCctvTimeWindow._frame_or_first(Stub(first), fallback) is first
    assert SyncCctvTimeWindow._frame_or_first(Stub(None), fallback) is fallback


def test_additional_camera_has_its_own_roi_entry(tmp_path):
    """The additional camera's boxes live under "<id>/additional"; with no
    entry yet it starts from the main camera's, and saving never touches
    the main camera's entry."""
    from logfather.ui.time_ocr import additional_camera_roi_key

    path = tmp_path / "ocr_settings.json"
    key = additional_camera_roi_key("PikPak007")
    assert key == "PikPak007/additional"
    save_roi_settings(path, "PikPak007", RoiSettings(0.22, 0.06, 0.013, 0.0))
    assert load_roi_settings(path, key).width_ratio == 0.22  # fallback to the main camera
    save_roi_settings(path, key, RoiSettings(0.3, 0.07, 0.02, 0.1))
    assert load_roi_settings(path, key).width_ratio == 0.3
    assert load_roi_settings(path, "PikPak007").width_ratio == 0.22
    assert load_roi_settings(path, key, section="date_roi_by_key") is None


# ---- the readings table's second-boundary search --------------------------------

def _clock_at_25fps(frame: int) -> str:
    return f"00:00:{frame // 25:02d}"


def test_find_second_boundaries_finds_every_tick_exactly():
    from logfather.ui.time_ocr import find_second_boundaries
    reads: list[int] = []

    def read_text(frame):
        reads.append(frame)
        return _clock_at_25fps(frame)

    found = find_second_boundaries(read_text, 0, 100, 5)
    assert found == [(25, "00:00:01"), (50, "00:00:02"), (75, "00:00:03"), (100, "00:00:04")]
    assert len(reads) < 101  # coarse reads plus bisection, not every frame


def test_find_second_boundaries_survives_unreadable_frames():
    from logfather.ui.time_ocr import find_second_boundaries

    def read_text(frame):
        if frame in (20, 24, 26, 27):
            return None
        return _clock_at_25fps(frame)

    assert find_second_boundaries(read_text, 0, 49, 5)[0] == (25, "00:00:01")


def test_find_second_boundaries_ignores_skips_and_misreads():
    from logfather.ui.time_ocr import find_second_boundaries

    def read_text(frame):
        if frame == 30:
            return "00:00:07"  # a misread inside the second
        if frame < 25:
            return "00:00:00"
        if frame < 50:
            return "00:00:01"
        return "00:00:05"  # the clock jumped, not a tick

    assert find_second_boundaries(read_text, 0, 74, 5) == [(25, "00:00:01")]
