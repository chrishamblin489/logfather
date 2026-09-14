"""Unit tests for the pure-logic helpers the app depends on most.

Run with:  .venv\\Scripts\\python.exe -m pytest
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logfather.data.elastic_loader as elastic_loader
from logfather.ui.replay_timeline import parse_time_from_name


class TestExtractRobotId:
    def test_pikpak_folder_maps_to_robot_id(self):
        assert elastic_loader._extract_robot_id(Path("Z:/public/PikPak012")) == "35-2300-012"

    def test_three_trailing_digits_required(self):
        assert elastic_loader._extract_robot_id(Path("Z:/public/PikPak")) is None

    def test_unrelated_folder_with_digits_still_maps(self):
        # Any folder ending in three digits is treated as a system folder.
        assert elastic_loader._extract_robot_id(Path("Z:/public/Spare003")) == "35-2300-003"


class TestResolveRobotId:
    """The robot id a fetch queries, resolved once on the UI thread
    (review item 10): the picker's override wins, else the folder."""

    @staticmethod
    def _resolve(root, override):
        from logfather.data.elastic_schema import resolve_robot_id
        return resolve_robot_id(root, override)

    def test_override_wins_over_the_folder(self):
        assert self._resolve(Path("Z:/public/PikPak012"), "35-2300-SIM") == "35-2300-SIM"

    def test_override_without_a_folder(self):
        assert self._resolve(None, "35-2300-SIM") == "35-2300-SIM"

    def test_folder_id_when_no_override(self):
        assert self._resolve(Path("Z:/public/PikPak012"), None) == "35-2300-012"

    def test_folder_given_as_a_string(self):
        assert self._resolve("Z:/public/PikPak012", None) == "35-2300-012"

    def test_empty_override_counts_as_none(self):
        assert self._resolve(Path("Z:/public/PikPak012"), "") == "35-2300-012"

    def test_neither_is_none(self):
        assert self._resolve(None, None) is None
        assert self._resolve(None, "") is None

    def test_underivable_folder_is_none(self):
        # The timeline loads a "." root in the override case; without an
        # override nothing is derivable from it.
        assert self._resolve(Path("."), None) is None
        assert self._resolve(Path("Z:/public/PikPak"), None) is None


class TestExtractHitRobotId:
    def test_prefers_leap_robot_id(self):
        doc = {"leap_robot_id": "35-2300-010", "system_id": "35-2300-999"}
        assert elastic_loader._extract_hit_robot_id(doc) == "35-2300-010"

    def test_falls_back_to_system_id(self):
        assert elastic_loader._extract_hit_robot_id({"system_id": "35-2300-013"}) == "35-2300-013"

    def test_strips_whitespace(self):
        assert elastic_loader._extract_hit_robot_id({"leap_robot_id": " 35-2300-010 "}) == "35-2300-010"

    def test_empty_doc_returns_none(self):
        assert elastic_loader._extract_hit_robot_id({}) is None
        assert elastic_loader._extract_hit_robot_id({"leap_robot_id": "  "}) is None


class TestParseTs:
    def test_zulu_suffix(self):
        ts = elastic_loader._parse_ts("2026-09-02T15:44:47.562Z")
        assert ts == datetime(2026, 9, 2, 15, 44, 47, 562000, tzinfo=timezone.utc)

    def test_explicit_offset(self):
        ts = elastic_loader._parse_ts("2026-09-02T16:44:47+01:00")
        assert ts is not None
        assert ts.astimezone(timezone.utc).hour == 15

    def test_garbage_returns_none(self):
        assert elastic_loader._parse_ts("not a timestamp") is None


class TestEnsureUtc:
    def test_naive_becomes_utc(self):
        dt = elastic_loader._ensure_utc(datetime(2026, 9, 2, 12, 0, 0))
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 12

    def test_aware_is_converted(self):
        from datetime import timedelta, timezone as tz

        plus_two = tz(timedelta(hours=2))
        dt = elastic_loader._ensure_utc(datetime(2026, 9, 2, 12, 0, 0, tzinfo=plus_two))
        assert dt.hour == 10


class TestSearchUrl:
    def test_kibana_url_converted_to_es_endpoint(self):
        url = elastic_loader._search_url(
            "https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243", "logstash-*"
        )
        assert ".es.europe-west2" in url
        assert ".kb." not in url
        assert "/logstash-*/_search" in url

    def test_trailing_slash_stripped(self):
        url = elastic_loader._search_url("https://example.com:9243/", "idx")
        assert url.startswith("https://example.com:9243/idx/_search")


class TestParseTimeFromName:
    def test_real_cctv_filename(self):
        ts = parse_time_from_name(Path("PikPak010 -Line 1-_00_20260901052919.mp4"))
        assert ts is not None
        assert (ts.year, ts.month, ts.day) == (2026, 9, 1)
        assert (ts.hour, ts.minute, ts.second) == (5, 29, 19)

    def test_filename_without_timestamp(self):
        assert parse_time_from_name(Path("clip.mp4")) is None

    def test_invalid_date_digits(self):
        assert parse_time_from_name(Path("cam_99999999999999.mp4")) is None


class _FakeCap:
    """Stands in for cv2.VideoCapture; counts grab() calls."""

    def __init__(self, grab_results=None):
        self.grab_calls = 0
        self._grab_results = list(grab_results or [])

    def grab(self):
        self.grab_calls += 1
        if self._grab_results:
            return self._grab_results.pop(0)
        return True


class TestPositionCaptureSequential:
    def _fn(self):
        from logfather.ui.replay_view import _position_capture_sequential
        return _position_capture_sequential

    def test_next_frame_needs_no_work(self):
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 100) is True
        assert cap.grab_calls == 0

    def test_out_of_sequence_requires_seek(self):
        cap = _FakeCap()
        assert self._fn()(cap, False, 100, 100) is False
        assert cap.grab_calls == 0

    def test_backward_jump_requires_seek(self):
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 50) is False
        assert cap.grab_calls == 0

    def test_small_forward_jump_grabs(self):
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 103) is True
        assert cap.grab_calls == 3

    def test_large_forward_jump_requires_seek(self):
        from logfather.ui.replay_view import MAX_GRAB_SKIP_FRAMES
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 100 + MAX_GRAB_SKIP_FRAMES + 1) is False
        assert cap.grab_calls == 0

    def test_boundary_forward_jump_grabs(self):
        from logfather.ui.replay_view import MAX_GRAB_SKIP_FRAMES
        cap = _FakeCap()
        assert self._fn()(cap, True, 0, MAX_GRAB_SKIP_FRAMES) is True
        assert cap.grab_calls == MAX_GRAB_SKIP_FRAMES

    def test_failed_grab_falls_back_to_seek(self):
        cap = _FakeCap(grab_results=[True, False])
        assert self._fn()(cap, True, 100, 103) is False
        assert cap.grab_calls == 2


class TestEventsCacheSkuComplete:
    def _paths(self, tmp_path):
        return tmp_path / "events.json"

    def test_round_trip_preserves_flag(self, tmp_path):
        from datetime import date, timedelta
        cache = tmp_path / "events.json"
        yesterday = date.today() - timedelta(days=1)
        elastic_loader._save_events_cache(cache, [], sku_complete=True)
        result = elastic_loader._load_events_cache(cache, yesterday)
        assert result is not None
        items, sku_complete = result
        assert items == []
        assert sku_complete is True

    def test_default_flag_is_false(self, tmp_path):
        from datetime import date, timedelta
        cache = tmp_path / "events.json"
        yesterday = date.today() - timedelta(days=1)
        elastic_loader._save_events_cache(cache, [])
        _, sku_complete = elastic_loader._load_events_cache(cache, yesterday)
        assert sku_complete is False

    def test_legacy_cache_without_flag_reads_incomplete(self, tmp_path):
        import json as _json
        from datetime import date, timedelta
        cache = tmp_path / "events.json"
        yesterday = date.today() - timedelta(days=1)
        cache.write_text(_json.dumps({"generated_at": "x", "items": []}), encoding="utf-8")
        result = elastic_loader._load_events_cache(cache, yesterday)
        assert result is not None
        _, sku_complete = result
        assert sku_complete is False

    def test_is_past_day_uses_local_date(self):
        # Days are local calendar days: today (local) must never be "past",
        # even when the UTC date has already rolled over.
        from datetime import date, timedelta
        today_local = date.today()
        assert elastic_loader._is_past_day(today_local - timedelta(days=1)) is True
        assert elastic_loader._is_past_day(today_local) is False
        assert elastic_loader._is_past_day(today_local + timedelta(days=1)) is False


class TestSettingsResilience:
    def test_save_and_load_round_trip(self, tmp_path):
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.elastic_api_key = "secret-key"
        s.save(path)
        loaded = Settings.load(path)
        assert loaded.elastic_api_key == "secret-key"
        assert loaded.load_warning is None

    def test_second_save_keeps_backup(self, tmp_path):
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.elastic_api_key = "v1"
        s.save(path)
        s.elastic_api_key = "v2"
        s.save(path)
        bak = tmp_path / "settings.json.bak"
        assert bak.exists()
        assert '"v1"' in bak.read_text()

    def test_corrupt_file_restores_from_backup(self, tmp_path):
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.elastic_api_key = "good-key"
        s.save(path)
        s.save(path)  # ensure .bak holds the good content
        path.write_text("{ this is not json", encoding="utf-8")
        loaded = Settings.load(path)
        assert loaded.elastic_api_key == "good-key"
        assert loaded.load_warning is not None
        assert list(tmp_path.glob("settings.json.corrupt-*"))

    def test_corrupt_file_without_backup_warns_and_defaults(self, tmp_path):
        from logfather.data.settings_store import Settings, _default_conditions
        path = tmp_path / "settings.json"
        path.write_text("garbage", encoding="utf-8")
        loaded = Settings.load(path)
        assert loaded.elastic_api_key is None
        assert loaded.load_warning is not None
        assert len(loaded.conditions) == len(_default_conditions())
        assert list(tmp_path.glob("settings.json.corrupt-*"))

    def test_load_warning_not_persisted(self, tmp_path):
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.load_warning = "boom"
        s.save(path)
        assert "load_warning" not in path.read_text()

    def test_malformed_import_raises_and_preserves(self, tmp_path):
        import pytest
        from logfather.data.settings_store import Settings, SHAREABLE_EXPORT_FORMAT
        export = tmp_path / "export.json"
        export.write_text(
            '{"_format": "%s", "conditions": 42}' % SHAREABLE_EXPORT_FORMAT,
            encoding="utf-8",
        )
        s = Settings.load(tmp_path / "none.json")
        s.elastic_url = "https://keep-me.example"
        with pytest.raises(ValueError):
            s.import_shareable(export)
        assert s.elastic_url == "https://keep-me.example"


class TestFrameAnalysis:
    def _frames(self):
        import numpy as np
        base = np.zeros((40, 60, 3), dtype=np.uint8)
        moved = base.copy()
        moved[10:20, 20:40] = 255
        return base, moved

    def test_pixel_diff_shape_and_signal(self):
        import numpy as np
        from logfather.core.frame_analysis import compute_pixel_diff_view
        base, moved = self._frames()
        out = compute_pixel_diff_view(
            moved, base, gain=1.0, threshold=10, heatmap=False, overlay=False, alpha=0.5
        )
        assert out.shape == base.shape and out.dtype == np.uint8
        assert out.sum() > 0  # the changed region must register

    def test_pixel_diff_identical_frames_dark(self):
        from logfather.core.frame_analysis import compute_pixel_diff_view
        base, _ = self._frames()
        out = compute_pixel_diff_view(
            base, base.copy(), gain=1.0, threshold=10, heatmap=False, overlay=False, alpha=0.5
        )
        assert int(out.sum()) == 0

    def test_optical_flow_shape(self):
        import numpy as np
        from logfather.core.frame_analysis import compute_optical_flow_view
        base, moved = self._frames()
        out = compute_optical_flow_view(
            moved, base, gain=1.0, min_motion=0, heatmap=False, overlay=False,
            alpha=0.5, arrows=False, arrow_step=16, arrow_scale=1.0, compute_scale=1.0,
        )
        assert out.shape == base.shape and out.dtype == np.uint8


class TestClipCache:
    def _cache(self, tmp_path, protected=None):
        from logfather.data.clip_cache import ClipCache
        return ClipCache(
            protected_paths_provider=(lambda: protected or []),
            root=tmp_path / "cache",
        )

    def test_cache_path_is_stable_and_distinct(self, tmp_path):
        cache = self._cache(tmp_path)
        a = cache.cache_path_for(Path("Z:/public/PikPak007/2026/09/01/a.mp4"))
        b = cache.cache_path_for(Path("Z:/public/PikPak008/2026/09/01/a.mp4"))
        assert a == cache.cache_path_for(Path("Z:/public/PikPak007/2026/09/01/a.mp4"))
        assert a != b
        assert a.parent == cache.root

    def test_copy_and_validity_round_trip(self, tmp_path):
        cache = self._cache(tmp_path)
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"fake video data")
        target = cache.cache_path_for(source)
        assert cache.copy_to_cache(source, target) is True
        assert target.exists()
        assert cache.is_cached_copy_current(source, target) is True
        assert cache.get_valid_cached_path(source) == target
        source.write_bytes(b"fake video data CHANGED!")
        assert cache.is_cached_copy_current(source, target) is False

    def test_invalidate_removes_meta_too(self, tmp_path):
        cache = self._cache(tmp_path)
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"data")
        target = cache.cache_path_for(source)
        cache.copy_to_cache(source, target)
        assert cache.meta_path_for(target).exists()
        cache.invalidate(target)
        assert not target.exists()
        assert not cache.meta_path_for(target).exists()

    def test_prune_never_deletes_protected_clip(self, tmp_path):
        import os, time
        from logfather.data.clip_cache import ClipCache
        import logfather.data.clip_cache as cc
        cache = ClipCache(root=tmp_path / "cache")
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"x" * 1024)
        target = cache.cache_path_for(source)
        cache.copy_to_cache(source, target)
        cache._protected_paths_provider = lambda: [str(target)]
        # Age the entry far past the cutoff, then prune.
        old = time.time() - (cc.CACHE_MAX_AGE_DAYS + 5) * 86400
        os.utime(target, (old, old))
        os.utime(cache.meta_path_for(target), (old, old))
        cache.prune()
        assert target.exists()

    def test_prune_deletes_expired_unprotected_clip(self, tmp_path):
        import os, time
        import logfather.data.clip_cache as cc
        cache = self._cache(tmp_path)
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"x" * 1024)
        target = cache.cache_path_for(source)
        cache.copy_to_cache(source, target)
        old = time.time() - (cc.CACHE_MAX_AGE_DAYS + 5) * 86400
        os.utime(target, (old, old))
        os.utime(cache.meta_path_for(target), (old, old))
        cache.prune()
        assert not target.exists()


class TestElasticSchema:
    def test_robot_id_from_folder(self):
        from logfather.data.elastic_schema import robot_id_from_folder
        assert robot_id_from_folder("PikPak012") == "35-2300-012"
        assert robot_id_from_folder("Spare003") == "35-2300-003"
        assert robot_id_from_folder("PikPak") is None
        assert robot_id_from_folder("") is None

    def test_identity_filter_single_shape(self):
        from logfather.data.elastic_schema import identity_filter, IDENTITY_FIELDS
        f = identity_filter(["35-2300-007"])
        clauses = f["bool"]["should"]
        assert len(clauses) == 3 * len(IDENTITY_FIELDS)
        assert f["bool"]["minimum_should_match"] == 1
        assert {"term": {"leap_robot_id.keyword": "35-2300-007"}} in clauses
        assert {"match_phrase": {"system_id": "35-2300-007"}} in clauses

    def test_identity_filter_multi_nests_per_robot(self):
        from logfather.data.elastic_schema import identity_filter
        f = identity_filter(["35-2300-007", "35-2300-010"])
        outer = f["bool"]["should"]
        assert len(outer) == 2
        assert all("bool" in clause for clause in outer)

    def test_sku_argus1_nested_data_collection(self):
        from logfather.data.elastic_schema import extract_ui_selection
        doc = {"data_collection": {"user_selection": "SKU-A", "tray_selection": "T1", "tool_selection": "tool_9"}}
        sel = extract_ui_selection(doc)
        assert sel == {"sku": "SKU-A", "tray": "T1", "tool": "tool_9"}

    def test_sku_argus2_sku_name_fields(self):
        from logfather.data.elastic_schema import extract_ui_selection
        doc = {"data_collection": {"sku_name": "SKU-B", "sku_tray": "ALDI_Full_Tray", "sku_tool": "tool_4834"}}
        sel = extract_ui_selection(doc)
        assert sel == {"sku": "SKU-B", "tray": "ALDI_Full_Tray", "tool": "tool_4834"}

    def test_sku_argus2_flat_dotted_field(self):
        from logfather.data.elastic_schema import extract_ui_selection
        sel = extract_ui_selection({"data_collection.sku_name": "SKU-C"})
        assert sel is not None and sel["sku"] == "SKU-C"

    def test_sku_block(self):
        from logfather.data.elastic_schema import extract_ui_selection
        sel = extract_ui_selection({"sku": {"name": "SKU-D", "tray": "T", "tool": "X"}})
        assert sel == {"sku": "SKU-D", "tray": "T", "tool": "X"}

    def test_sku_from_ui_node_json_request(self):
        from logfather.data.elastic_schema import extract_ui_selection
        doc = {
            "source": "/leap/manip1/ui_node",
            "json_request": {"params": '{"data": {"user_selection": "SKU-E", "tray_selection": "T2", "tool_selection": "tool_1"}}'},
        }
        sel = extract_ui_selection(doc)
        assert sel == {"sku": "SKU-E", "tray": "T2", "tool": "tool_1"}

    def test_disallowed_source_without_sku_fields_rejected(self):
        from logfather.data.elastic_schema import extract_ui_selection
        assert extract_ui_selection({"source": "/leap/manip1/motion_control_node"}) is None

    def test_no_sku_returns_none(self):
        from logfather.data.elastic_schema import extract_ui_selection
        assert extract_ui_selection({}) is None

    def test_stop_like_states(self):
        from logfather.data.elastic_schema import is_stop_like_event
        assert is_stop_like_event("system_stop", "") is True
        assert is_stop_like_event("emergency_stop", "") is True
        assert is_stop_like_event("start_pnp", "") is False
        assert is_stop_like_event("", "Shutting down system now") is True
        assert is_stop_like_event("", "", "system_shutdown") is True


class TestBuildSkuBands:
    def _dt(self, minute, second=0):
        from datetime import datetime, timezone
        return datetime(2026, 9, 1, 8, minute, second, tzinfo=timezone.utc)

    def _bands(self, events, cap_minute=59):
        from logfather.core.sku_timeline import build_sku_bands
        return build_sku_bands(events, self._dt(cap_minute))

    def test_empty_events(self):
        assert self._bands([]) == []

    def test_start_then_stop_makes_one_sku_band(self):
        sel = {"sku": "SKU-A", "tray": "T", "tool": "X"}
        bands = self._bands([
            (self._dt(0), "start", sel, "start_pnp"),
            (self._dt(10), "stop", None, "stop_pnp"),
        ])
        assert len(bands) == 1
        band = bands[0]
        assert (band.start, band.end) == (self._dt(0), self._dt(10))
        assert band.label == "SKU-A"
        assert band.payload["_ui_sku"] == "SKU-A"

    def test_system_stop_terminates_band(self):
        # The state the old 8-state query never returned.
        bands = self._bands([
            (self._dt(0), "start", {"sku": "S"}, "start_pnp"),
            (self._dt(5), "stop", None, "system_stop"),
        ])
        assert len(bands) == 1
        assert bands[0].end == self._dt(5)

    def test_manual_closed_by_auto_not_by_stop(self):
        bands = self._bands([
            (self._dt(0), "manual", None, "controller_node_manual_mode"),
            (self._dt(2), "stop", None, "stop_pnp"),      # must NOT close manual
            (self._dt(8), "auto", None, "controller_node_automatic_mode"),
        ])
        assert len(bands) == 1
        band = bands[0]
        assert band.payload.get("_ui_manual") is True
        assert (band.start, band.end) == (self._dt(0), self._dt(8))

    def test_reselect_same_sku_does_not_split(self):
        sel = {"sku": "S", "tray": "T", "tool": "X"}
        bands = self._bands([
            (self._dt(0), "start", sel, "start_pnp"),
            (self._dt(3), "select", dict(sel), "select"),
            (self._dt(10), "stop", None, "stop_pnp"),
        ])
        assert len(bands) == 1

    def test_select_different_sku_splits_band(self):
        bands = self._bands([
            (self._dt(0), "start", {"sku": "A"}, "start_pnp"),
            (self._dt(4), "select", {"sku": "B"}, "select"),
            (self._dt(10), "stop", None, "stop_pnp"),
        ])
        assert len(bands) == 2
        assert bands[0].label == "A" and bands[0].end == self._dt(4)
        assert bands[1].label == "B" and bands[1].start == self._dt(4)

    def test_start_without_data_carries_last_selection(self):
        bands = self._bands([
            (self._dt(0), "start", {"sku": "A"}, "start_pnp"),
            (self._dt(5), "stop", None, "stop_pnp"),
            (self._dt(20), "start", None, "start_pnp"),
            (self._dt(25), "stop", None, "stop_pnp"),
        ])
        assert len(bands) == 2
        assert bands[1].label == "A"  # carried forward

    def test_open_band_clamped_to_cap_end(self):
        bands = self._bands([
            (self._dt(0), "start", {"sku": "A"}, "start_pnp"),
        ], cap_minute=30)
        assert len(bands) == 1
        assert bands[0].end == self._dt(30)

    def test_events_past_cap_ignored(self):
        bands = self._bands([
            (self._dt(0), "start", {"sku": "A"}, "start_pnp"),
            (self._dt(40), "stop", None, "stop_pnp"),
        ], cap_minute=30)
        assert len(bands) == 1
        assert bands[0].end == self._dt(30)


class _FakeBufferEvent:
    def __init__(self, ts, target_id, event_type="target_added"):
        from types import SimpleNamespace
        self.timestamp = ts
        self.event_type = event_type
        self.buffer_snapshot = [SimpleNamespace(target_id=target_id)]


class TestGapTargetIds:
    def _ts(self, seconds):
        from datetime import datetime, timezone, timedelta
        return datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)

    def test_steady_rate_flags_nothing(self):
        from logfather.ui.target_overlay_controller import compute_gap_target_ids
        events = [_FakeBufferEvent(self._ts(i * 5), f"t{i}") for i in range(10)]
        close, wide = compute_gap_target_ids(events, threshold=0.5)
        assert close == set() and wide == set()

    def test_tight_gap_flagged(self):
        from logfather.ui.target_overlay_controller import compute_gap_target_ids
        times = [0, 5, 10, 15, 20, 21]  # last add comes far quicker than avg
        events = [_FakeBufferEvent(self._ts(s), f"t{i}") for i, s in enumerate(times)]
        close, _wide = compute_gap_target_ids(events, threshold=0.5)
        assert "t5" in close

    def test_wide_gap_flagged(self):
        from logfather.ui.target_overlay_controller import compute_gap_target_ids
        times = [0, 5, 10, 15, 20, 45]  # last add much slower than avg
        events = [_FakeBufferEvent(self._ts(s), f"t{i}") for i, s in enumerate(times)]
        _close, wide = compute_gap_target_ids(events, threshold=0.5)
        assert "t5" in wide


class TestResolveTrackingLine:
    def _dt(self, seconds):
        from datetime import datetime, timezone, timedelta
        return datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)

    def test_forward_capture_unchanged(self):
        from logfather.ui.conveyor_calibration_dialog import resolve_tracking_line
        out = resolve_tracking_line(self._dt(0), (0.2, 0.5), self._dt(4), (0.8, 0.5))
        assert out == ((0.2, 0.5), (0.8, 0.5), 4.0)

    def test_reverse_capture_swaps_points(self):
        # End point captured at an EARLIER frame: the item was at 0.8 later
        # and 0.2 earlier, so the belt still runs 0.2 -> 0.8.
        from logfather.ui.conveyor_calibration_dialog import resolve_tracking_line
        out = resolve_tracking_line(self._dt(4), (0.8, 0.5), self._dt(0), (0.2, 0.5))
        assert out == ((0.2, 0.5), (0.8, 0.5), 4.0)

    def test_too_close_in_time_rejected(self):
        from logfather.ui.conveyor_calibration_dialog import resolve_tracking_line
        assert resolve_tracking_line(self._dt(0), (0.2, 0.5), self._dt(0.05), (0.8, 0.5)) is None


class TestSessionResumeSettings:
    def test_last_session_round_trip(self, tmp_path):
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.last_session = {"root": "Z:/public/PikPak007", "day": "2026-09-01", "playhead": "2026-09-01T08:15:00+00:00"}
        s.save(path)
        loaded = Settings.load(path)
        assert loaded.last_session == s.last_session

    def test_defaults(self, tmp_path):
        from logfather.data.settings_store import Settings
        s = Settings.load(tmp_path / "none.json")
        assert s.last_session is None

    def test_stale_resume_on_startup_key_is_ignored(self, tmp_path):
        # Settings written before 2026-09-03 may still carry the removed
        # always/never preference; it must not break loading (startup
        # always asks now).
        import json
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        Settings.load(path).save(path)
        data = json.loads(path.read_text())
        data["resume_on_startup"] = "never"
        path.write_text(json.dumps(data))
        loaded = Settings.load(path)
        assert not hasattr(loaded, "resume_on_startup")

    def test_garbage_last_session_dropped(self, tmp_path):
        import json
        from logfather.data.settings_store import Settings
        path = tmp_path / "settings.json"
        Settings.load(path).save(path)
        data = json.loads(path.read_text())
        data["last_session"] = "not a dict"
        path.write_text(json.dumps(data))
        assert Settings.load(path).last_session is None


class TestWindowGeometryClamp:
    def _clamp(self, rect, avail):
        from PySide6.QtCore import QRect
        from logfather.ui.app_main import clamp_rect_to_screen
        return clamp_rect_to_screen(QRect(*rect), QRect(*avail))

    def test_fully_on_screen_untouched(self):
        out = self._clamp((100, 50, 800, 600), (0, 0, 1920, 1080))
        assert (out.x(), out.y(), out.width(), out.height()) == (100, 50, 800, 600)

    def test_half_off_right_edge_pulled_back(self):
        out = self._clamp((1600, 50, 800, 600), (0, 0, 1920, 1080))
        assert out.x() == 1920 - 800
        assert out.y() == 50

    def test_off_top_left_pulled_in(self):
        out = self._clamp((-500, -300, 800, 600), (0, 0, 1920, 1080))
        assert (out.x(), out.y()) == (0, 0)

    def test_larger_than_screen_shrinks_to_fit(self):
        out = self._clamp((0, 0, 3000, 2000), (0, 0, 1920, 1080))
        assert (out.width(), out.height()) == (1920, 1080)
        assert (out.x(), out.y()) == (0, 0)

    def test_secondary_monitor_offsets_respected(self):
        # Screen to the left of primary: negative coordinates are valid.
        out = self._clamp((-1900, 10, 800, 600), (-1920, 0, 1920, 1080))
        assert out.x() == -1900
        assert out.y() == 10


class TestOcrOffsetStore:
    def _store(self, tmp_path):
        from logfather.data.ocr_offset_store import OcrOffsetStore
        return OcrOffsetStore(tmp_path / "offsets.json")

    def test_round_trip(self, tmp_path):
        store = self._store(tmp_path)
        store.set("PikPak012:20260901052919", 1.24, 3)
        entry = store.get("PikPak012:20260901052919")
        assert entry == {"offset_seconds": 1.24, "frame_offset": 3}

    def test_source_tag_persisted(self, tmp_path):
        store = self._store(tmp_path)
        store.set("k", 0.5, 1, source="additional")
        assert store.get("k")["source"] == "additional"

    def test_missing_key_returns_none(self, tmp_path):
        assert self._store(tmp_path).get("nope") is None

    def test_pathless_store_is_inert(self):
        from logfather.data.ocr_offset_store import OcrOffsetStore
        store = OcrOffsetStore()
        store.set("k", 1.0, 0)
        assert store.get("k") is None

    def test_corrupt_file_reads_as_empty(self, tmp_path):
        store = self._store(tmp_path)
        store.path.write_text("{not json", encoding="utf-8")
        assert store.get("k") is None
        store.set("k", 2.0, 4)  # recovers by rewriting
        assert store.get("k")["offset_seconds"] == 2.0


class TestLayoutSettingsSnapshot:
    """_layout_settings_snapshot gates the settings_saved reload reaction:
    equal snapshots skip the widget rebuild (which re-lists the Z: share)."""

    @staticmethod
    def _snapshot(settings):
        from logfather.ui.Main_Window import MainWindow
        return MainWindow._layout_settings_snapshot(settings)

    def test_volatile_fields_do_not_change_the_snapshot(self):
        from logfather.data.settings_store import Settings
        plain = Settings()
        volatile = Settings(
            last_session={"system": "PikPak010"},
            window_geometry={"x": 1, "y": 2, "w": 3, "h": 4},
            load_warning="recovered",
        )
        assert self._snapshot(plain) == self._snapshot(volatile)

    def test_layout_field_change_is_detected(self):
        from logfather.data.settings_store import Settings
        assert self._snapshot(Settings()) != self._snapshot(Settings(customers=["Acme"]))

    def test_last_parent_change_is_detected(self):
        from logfather.data.settings_store import Settings
        assert self._snapshot(Settings()) != self._snapshot(Settings(last_parent="Z:/public"))


class TestFetchSkuItemsLastVideoEnd:
    """fetch_sku_items must not re-list the share when the timeline loader
    already supplies last_video_end (the duplicate ~5s WAN scan)."""

    def _settings_without_credentials(self):
        from logfather.data.settings_store import Settings
        return Settings(elastic_api_key=None, elastic_url="https://example.invalid")

    def test_provided_value_skips_the_share_scan(self, monkeypatch):
        from datetime import date
        def _boom(*_args, **_kwargs):
            raise AssertionError("share scan ran despite provided last_video_end")
        monkeypatch.setattr(elastic_loader, "_last_video_end", _boom)
        result = elastic_loader.fetch_sku_items(
            self._settings_without_credentials(),
            Path("Z:/public/PikPak012"),
            date(2026, 9, 1),
            last_video_end=None,
        )
        assert list(result) == []

    def test_default_still_scans(self, monkeypatch):
        from datetime import date
        import pytest as _pytest
        def _boom(*_args, **_kwargs):
            raise AssertionError("scan expected")
        monkeypatch.setattr(elastic_loader, "_last_video_end", _boom)
        with _pytest.raises(AssertionError, match="scan expected"):
            elastic_loader.fetch_sku_items(
                self._settings_without_credentials(),
                Path("Z:/public/PikPak012"),
                date(2026, 9, 1),
            )


class TestTimelineLoaderConcurrency:
    """_load_timeline_items runs extra loaders concurrently with the video
    scan; each receives a resolver for the last-video-end that blocks until
    the scan publishes it."""

    class _FakeJob:
        def __init__(self):
            self.partials = []
        def interrupted(self):
            return False
        def emit_progress(self, payload):
            self.partials.append(payload)

    def test_resolver_delivers_last_video_end(self):
        from datetime import date
        from logfather.ui.replay_timeline import _load_timeline_items

        clips = [
            Path("Z:/nowhere/PikPak012 -Line 1-_00_20260901080000.mp4"),
            Path("Z:/nowhere/PikPak012 -Line 1-_00_20260901081500.mp4"),
        ]
        seen = {}

        def extra_loader(_root, _day, resolve_last_video_end):
            assert callable(resolve_last_video_end)
            seen["value"] = resolve_last_video_end()
            return []

        job = self._FakeJob()
        result = _load_timeline_items(
            job, Path("Z:/nowhere"), date(2026, 9, 1),
            lambda _root, _day: clips, [extra_loader], None,
        )
        assert result is not None
        items, _day, _root = result
        videos = [i for i in items if i.kind == "video"]
        assert len(videos) == 2
        assert seen["value"] == max(v.end for v in videos)
        # All partials append now; arrival order no longer matters.
        assert all(p[3] is True for p in job.partials if p[0] == "partial")

    def test_no_videos_resolves_none(self):
        from datetime import date
        from logfather.ui.replay_timeline import _load_timeline_items

        seen = {}

        def extra_loader(_root, _day, resolve_last_video_end):
            seen["value"] = resolve_last_video_end()
            return []

        result = _load_timeline_items(
            self._FakeJob(), Path("Z:/nowhere"), date(2026, 9, 1),
            lambda _root, _day: [], [extra_loader], None,
        )
        assert result is not None
        assert seen["value"] is None


class TestActivityEta:
    """ETA text on the activity bar (MainWindow._eta_text)."""

    @staticmethod
    def _eta(remaining, rate):
        from logfather.ui.Main_Window import MainWindow
        return MainWindow._eta_text(remaining, rate)

    def test_under_five_seconds(self):
        assert self._eta(4 * 1024, 1024) == "a few seconds left"

    def test_seconds_form(self):
        assert self._eta(45 * 1024, 1024) == "~45s left"

    def test_minutes_form(self):
        assert self._eta(130 * 1024, 1024) == "~2m 10s left"

    def test_minutes_pads_seconds(self):
        assert self._eta(125 * 1024, 1024) == "~2m 05s left"


class TestCalibrationCaptureFields:
    """Capture clip key + fractions round-trip through the calibration
    JSON and default to None for pre-existing files."""

    def test_round_trip(self):
        from logfather.data.conveyor_calibration import ConveyorCalibration
        cal = ConveyorCalibration(
            system_id="PikPak012",
            capture_clip_key="PikPak012 -Line 1-_00_20260901080000",
            capture_start_fraction=0.25,
            capture_end_fraction=0.75,
        )
        restored = ConveyorCalibration.from_dict(cal.to_dict())
        assert restored.capture_clip_key == cal.capture_clip_key
        assert restored.capture_start_fraction == 0.25
        assert restored.capture_end_fraction == 0.75

    def test_old_files_default_to_none(self):
        from logfather.data.conveyor_calibration import ConveyorCalibration
        restored = ConveyorCalibration.from_dict({"system_id": "PikPak012"})
        assert restored.capture_clip_key is None
        assert restored.capture_start_fraction is None
        assert restored.capture_end_fraction is None


class TestFmtSig:
    """Friendly 2-significant-figure formatting in the calibration results."""

    @staticmethod
    def _fmt(value, sig=2):
        from logfather.ui.conveyor_calibration_dialog import _fmt_sig
        return _fmt_sig(value, sig)

    def test_small_fraction(self):
        assert self._fmt(-0.18003) == "-0.18"

    def test_sub_hundredth(self):
        assert self._fmt(0.0012345) == "0.0012"

    def test_over_one(self):
        assert self._fmt(1.667202) == "1.7"

    def test_large_value_no_scientific(self):
        assert self._fmt(634.7) == "630"

    def test_zero(self):
        assert self._fmt(0.0) == "0"
