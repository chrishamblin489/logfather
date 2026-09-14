# The Logfather — code review and refactoring plan (2026-09-14)

A deep read of the code base at v0.461 after two weeks of fast feature work
(the Sync CCTV Time rewrite, the readings table, the replay chart View menu
and zoom, the Track gate, the first-product button, the Errors box, the
Overview strips). Three passes were made: a module inventory with layering
and dead-code checks, a comparison of every on-screen label with the
identifier behind it, and a search for duplicated and oversized code.

The previous review (`docs/CODE_REVIEW_2026-09.md`, 2026-09-02) is the
baseline; section 0 says which of its items are closed.

Baseline numbers: 41,072 lines of Python under `src/`, `tools/` and
`tests/`; 71 modules in the package; 312 unit tests.

| File | Lines | Note |
|---|---:|---|
| `ui/replay_view.py` (was Log_vid_gui.py) | 5,658 | one class, 257 methods, 246 attributes |
| `ui/time_ocr.py` | 2,944 | engine + Sync CCTV Time window |
| `ui/overview_widget.py` | 2,506 | |
| `ui/Main_Window.py` | 2,132 | 100 methods |
| `ui/replay_timeline.py` (was Time_Picker.py) | 2,093 | |
| `data/elastic_loader.py` | 1,717 | |

## What was done in the same pass (2026-09-14)

Renamed to match the screen (all references updated, smoke test and 312
tests green):

| On screen | Was | Now |
|---|---|---|
| PikPak Replay (the view) | `Log_vid_gui.py` / `VideoLogViewer` | `replay_view.py` / `ReplayView` |
| PikPak Replay timeline chart | `Time_Picker.py` / `TimePicker` / `self.time_picker` | `replay_timeline.py` / `ReplayTimeline` / `self.replay_timeline` |
| Data / Additional data boxes | `overview_signals.py` / `SignalBoxes` | `data_boxes.py` / `DataBoxes` |
| Sync CCTV Time window | `OcrVideoPlayer` | `SyncCctvTimeWindow` |
| Sync Time button's action | `_analyze_first_10s` | `_run_clock_checks` |
| Frame / Exact time / FPS table | `ocr_history` | `readings_list` |
| Time (frame N) preview | `roi_preview` | `time_preview` |
| Conveyor button | `calibrate_btn` | `conveyor_btn` |
| PikPak Replay / Search mode buttons | `viewer_btn` / `fleetwide_search_btn` | `replay_btn` / `search_btn` |
| Errors / Stops button | `errors_btn` | `errors_stops_btn` |
| Targets button and panel | `buffer_toggle` / `buffer_widget` | `targets_toggle` / `targets_panel` |
| Drift slider | `offset_caption/slider/display`, `_on_offset_slider_changed` | `drift_*`, `_on_drift_slider_changed` |
| Gap slider | `close_gap_caption/slider/display/threshold` | `gap_*` |
| Info text | `status_text_btn` | `info_text_btn` |
| Bird's Eye | `tray_view_*` (58 sites in two files) | `birds_eye_*` |
| Additional CCTV | `secondary_*` (324 sites) | `additional_*` |
| Sync window openers | `open_ocr_roi_tool` / `open_secondary_ocr_tool` | `open_sync_cctv_time` / `open_additional_sync_cctv_time` |
| Timeline View menu | `_bars_menu*` | `_view_menu*` |
| Saved mode in last_session | `"viewer"` / `"fleetwide"` | `"replay"` / `"search"` (old values still load) |

Deliberately **not** renamed (persisted or too wide):

- ui_state keys (`viewer_status_text`, `viewer_clip_counters`, `replay_bars_hidden`, `replay_rows`, `*_hidden_systems`, `overview_*_order`) and the `TimelineItem.kind == "video"` string shown as "CCTV". Renaming them would drop users' saved ticks.
- The `%LOCALAPPDATA%\VideoLogViewer\cache` folder name (seven string literals). Renaming moves everyone's cache.
- The condition name "Motor overcurrent" and the telemetry spec name `overcurrent`, which feed saved ui_state keys.
- `self.viewer` in Main_Window (the ReplayView instance) — "viewer" still reads correctly.
- `fleetwide_elastic_search_widget.py` / `FleetwideElasticSearchWidget` behind the Search button: the heading inside the screen still says "Fleetwide Elastic Search". Pick one word first.

Also done: the About page rebuilt (schematic, screen-to-module map, all 71
modules described, GitHub links fixed to chrishamblin489), the architecture
doc's table regenerated from the About page, and the smoke test made to
import every module under the package instead of a hand-kept list of 27.

## 0. Status of the 2026-09-02 review

Closed and verified: `closeEvent` (§1.1), the single `TRANSITION_STATES`
list (§1.5), `_is_past_day` on local dates (§1.7), the stop-report freeze
and the OCR SMB copy moved off the GUI thread (§1.11), no `.terminate()`
calls (§1.14), the five QThread loaders folded into one `Job` (§3), robot-id
extraction in one place (`elastic_schema`), asset lookup in `app_assets`,
Stage 1 and 2 extractions.

Still open:

| Item | Where it stands |
|---|---|
| `SYSTEM_ID_OVERRIDE` module global read by worker threads | `data/elastic_loader.py` lines 55, 76, 148, 744, 1001, 1253; set from `Main_Window` |
| Overview repaint is `scene.clear()` + a 397-line rebuild | `overview_widget._redraw`, 11 call sites |
| `ScrubbableLabel` defined three times | `time_ocr.py`, `viewer_widgets.py`, `tools/Vid_Frame_Differencing.py` |
| `ensure_utc` re-implemented as `_ensure_utc` | `elastic_loader.py` line 169, 21 uses |
| Main-vs-additional camera twins in the replay view | see §2 |
| `time_ocr` duplicates five of its own algorithms | see §2 |
| `hasattr`/`getattr(self, …)` self-probes | gone from Main_Window; 68 remain in `replay_view.py` |
| `elastic_loader` split | still 1,717 lines with three 230–350-line functions |

## 1. Layering

The import layering is clean: nothing in `core` imports `data`, `ui`,
PySide6 or requests; nothing in `data` imports `ui` or QtWidgets; the single
Qt import in `data` (`clip_cache.py`, QObject/Signal) is allowed.

Two borderline cases worth a decision: `core/frame_analysis.py` imports
cv2 and numpy (pure maths, fine), and `core/app_version.py` shells out to
git and does a `git ls-remote` over the network from the "no network"
layer.

**Private names used across module boundaries** are the real smell. Eight
name-sets wear a leading underscore but are de-facto public API:

- `elastic_loader._normalize_index_id`, `_search_url`, `_build_robot_filters`, `_ensure_utc`, `_get_robot_id`, `_parse_ts` — imported by seven `data` modules and by `ui/data_sources_dialog.py`.
- `core.timeline_model._annotations_path_for`, `_build_annotation_index`, `_build_cache_index`, `_cache_key_for`, `_has_annotations`, `_is_path_cached`, `_path_key` — imported by three `ui` modules.
- `ui.target_buffer_widget._detail_rows`, `_display_target_id`, `_summary_rows` — imported by the overlay controller and the scope widget.
- `core.log_events._to_local_naive`; `ui.viewer_widgets._dist`, `_distance_to_segment`.

Fix: drop the underscore (they are public). Cheap and mechanical.

Attribute reach-ins (35 sites) to turn into accessors, in order of how often
they bite: `Main_Window` reading `replay_timeline._current_date`, `_items`,
`_static_tracks`, `_fit_to_items()`; `overview_widget._known_system_names`;
`date_picker._scan_slot`; the overlay controller reading
`viewer.video_label._frame` two objects deep; `data_boxes` calling five
private methods on its owner (`_maybe_fetch_signals`, `_schedule_redraw`,
`_fit_text`, `_redraw`, `_drag_candidate`).

## 2. Duplicated logic

### 2a. `time_ocr.py`: window methods vs module twins (~230 lines)

| Window method | Module twin | Divergence |
|---|---|---|
| `_collect_ocr_samples` (61) | `_collect_ocr_samples_for_cap` (60) | UI hooks only |
| `_verify_frame_offset` (64) | `_verify_frame_offset_for_cap` (63) | **behaviour drift**: mid-frame is `frame_count//2` in one and `max(count//2, start_frame + 2·fps)` in the other |
| `_estimate_start_from_transitions` (35) | same name (34) | identical |
| `_time_text_to_seconds`, `_combine_date_and_time` | same names | identical |
| `_pick_best_start` (nested, 29) | `_estimate_start_from_samples` (21) | same algorithm |

Above them, `_run_clock_checks`'s body (`_run_sync_analysis`, 144 lines)
and `analyze_video_offset` (200 lines) are the same four-stage algorithm
written twice. Only the module copies are unit-tested; the copies the GUI
actually runs are not.

Proposal: one engine, `estimate_offset(cap, fps, frame_count, *, roi,
base_dt, start_frame, hooks)` where `hooks` carries `on_stage`, `on_frame`
and `should_abort`. The window passes hooks that drive the progress dialog
and previews; the headless path passes the job's interrupt flag. The GUI
then inherits the 36 existing tests.

### 2b. `replay_view.py`: main vs Additional CCTV OCR (~350 lines, ~150 removable)

`open_sync_cctv_time` (72 lines) vs `open_additional_sync_cctv_time` (74):
about 12 lines genuinely differ (path source, settings key, cache-key tag,
which attributes, which JobSlot, the post-apply call).
`_auto_sync_with_ocr` (95) vs `_auto_sync_additional_with_ocr` (102): same
skeleton; the additional path has a four-step filename fallback the main
path lacks, and the main path has a plausibility log line the additional
path lacks. That is the cost of the duplication: fixes land on one side.

Proposal: an `OcrChannel` value object `(offset_store, slot, cache_key_tag,
settings_key_fn, cam_label, on_applied)` and one `open_sync(channel)` /
`auto_sync(channel, force)` pair; the six `ocr_*` / `additional_ocr_*`
attribute pairs become `channel.alignment: TimeAlignment`.

### 2c. Progress and busy dialogs (nine sites, ~90 lines)

Three near-identical indeterminate busy dialogs (`replay_view._set_video_busy`,
`_set_log_busy`, `replay_timeline` line 977) and six cancellable determinate
ones (three in `time_ocr`, `Main_Window.open_stop_report`,
`replay_view.export_current_clip_with_overlays`). Proposal: `BusyDialog`
and a `job_progress(parent, title, slot)` context manager in a new
`ui/progress.py`.

### 2d. Splitter-pane animations (four copies, ~123 lines)

`Main_Window._animate_targets_panel`, `_animate_left_panel`,
`_animate_timeline_height` and `replay_view._set_right_tabs_visible` are
the same stop/start/step/finish triple; two wrap the step in a bare
`except Exception: pass` that silently freezes the slide on an error.
Proposal: `ui/pane_animator.py::PaneAnimator(splitter, index)` (~40 lines).

### 2e. Filter panel sextet and triplets (`replay_view.py`, ~141 lines)

Six copies of `select_all_/select_no_{sources,states,messages}` (the
messages pair omits `update_message_visibility_from_filters()`, which may be
a bug), three copies of `_reset_*_panel`, and the two marker-bar refreshers.
One `_set_group_checked(group, checked)`, one `_reset_filter_panel(kind)`,
one `_markers_to_ratios()`.

### 2f. Smaller ones

- `about_page` re-implemented git and asset lookup (fixed 2026-09-14: it now uses `app_assets`; the git call remains and belongs in `core/app_version`).
- `icons.py`: 11 of 23 painters still open-code the six-line preamble `_start()` already provides; `first_product_icon` redraws the punnet geometry of `punnet_icon`.
- `overview_widget._is_shutdown_message` / `_is_stop_like_event` shadow the canonical `elastic_schema` versions.
- `core/frame_analysis.py` and `tools/Vid_Frame_Differencing.py` still carry the same three functions.

## 3. Dead and dormant code (all grep-verified, one occurrence = the definition)

**Dead top-level:** `SettingsDialog` (`settings_dialog.py` 131; only the
three panels are used), `TargetScopeWidget` (whole module, 224 lines; kept
alive only by the old smoke list), `_OverviewThumbItem`
(`overview_widget.py` 252), `ocr_time_from_video` + `ocr_time_from_video_samples`
(`time_ocr.py` 681/693), `target_buffer_widget._elapsed`,
`elastic_loader._events_cache_path`, `pick_rate.bucket_minutes`,
`settings_store.format_system_button_text`, `grafana_client.search_dashboards`,
`icons.bars_icon`, `grid_icon`, `radar_icon`.

**Dead methods:** `ReplayView._clip_annotation_path_for_cache`,
`_copy_to_cache`, `_invalidate_cached_copy`, `_find_additional_cctv_clip`,
`load_pending_logs`, `add_playback_right_widget`;
`ReplayTimeline._draw_day_rate_heat_strip`, `_draw_selected_clip_rate_heat`,
`is_loading`, `set_loader`; `TelemetryPanel.set_status`;
`DatePicker._emit_settings_requested` (and its `settings_requested` signal,
which nothing connects); `SystemLayoutPanel._selected_customer_name`;
`RoiEditorLabel._roi`, `_to_frame`, `_roi_label_rect` (slider-era leftovers);
`SyncCctvTimeWindow._current_actual_time_str`, `_open_video_dialog`,
`_show_verification_dialog`.

**Dead constants:** `DISCOVER_INDEX_ID_DEFAULT`, `OVERVIEW_TEMP_STRIP_HEIGHT`,
`OVERVIEW_MAX_RANGE_DAYS`, `OVERVIEW_THUMB_SIZE`, and the six unreferenced
`_OVERVIEW_*_KEY` constants in `overview_widget.py` lines 68–73.

**Dormant subsystems (live code feeding hidden output):**

1. The pick-rate heat strip. The two drawers were unhooked ("the Picks strip shows it") but `_build_day_rate_proxy_buckets`, `_heat_color`, `set_clip_target_rate_heat` and `clear_clip_target_rate_heat` still run on every load and clip open, each forcing a full timeline redraw, and `TestGapAndBuckets` tests output nobody renders. Removing it saves two wasted redraws per clip.
2. The yellow log-marker bar (`event_marker_bar`), hidden on 2026-09-11 but still constructed, cleared, fed and padded (`_refresh_marker_bar`, `_update_marker_bar_padding`).

## 4. Oversized functions (over ~110 lines)

| Lines | Function | Split |
|---:|---|---|
| 397 | `overview_widget._redraw` | grid / system row / SKU bands / stop markers / status column / hover layer |
| 370 | `annotated_video_widget.paintEvent` | one `_paint_<tool>` per annotation kind + `_paint_hud` |
| 354 | `elastic_loader.fetch_fleetwide_search_histogram` | pure query builder + pure bucket parser + a 30-line fetch |
| 279 | `SyncCctvTimeWindow.__init__` | `_init_state / _build_video_pane / _build_readings_pane / _build_controls / _wire_signals` |
| 276 | `DataInventoryDialog.__init__` | sectioned build |
| 273 | `data_inventory.fetch_elastic_inventory` | query / parse / fetch |
| 270 | `replay_timeline._redraw_timeline` | hour ruler / clip blocks / event marks / SKU bands / playhead; delete the unconditional `print` at the top |
| 236 / 233 | `elastic_loader.fetch_events` / `fetch_sku_items` | cache-read / build / paginate / normalise / cache-write |
| 223 | `OverviewWidget.__init__` | sectioned build |
| 200 / 144 | `time_ocr.analyze_video_offset` / `_run_sync_analysis` | fold into the §2a engine |
| 178 / 177 / 169 | `ReplayTimeline.__init__`, `MainWindow._build_top_bar_and_layout`, `_build_panels` | mode buttons / system chooser / activity bar / gear |
| 168 | `ReplayView._build_analysis_controls` | move to `analysis_panel.py` |
| 168 | `target_buffer_loader.fetch_buffer_events` | query / parse / join |
| 154 | `Settings._from_dict` | per-section parsers, each testable |
| 141 | `ReplayView.export_current_clip_with_overlays` | open writer / render frames / mux audio, in `clip_export.py` |

`_draw_help_flowchart` (68 lines) is layout arithmetic with magic indices
(`centres[2]`, `[5]`, `[6]`, `[7]`) that break silently if a step is
inserted; a small declarative spec (boxes and edges by letter) would fix it.

## 5. The two god objects

### `ReplayView` (5,512 lines in one class)

| Cluster | ~Lines | Target | Value / risk |
|---|---:|---|---|
| Log filtering UI (panels, presets, custom filters, tab highlights) | 860 | `log_filter_panel.py` with a `filters_changed` signal | **highest value, low risk** |
| OCR offset handling, main + additional | 505 | `ocr_sync_controller.py` with `OcrChannel` (§2b) | high / medium |
| Analysis (diff and optical flow controls, view, popout) | 440 | `analysis_panel.py` (maths already in core) | high / low |
| Additional video (find, open, poll, per-time frame) | 345 | into the OCR controller + a `VideoSource` | medium / high (hot path) |
| Annotations | 241 | `clip_annotations.py` | medium / low |
| Alignment, overlay, status, highlight | 212 | the `TimeAlignment` value object | high / high |
| Overlay strip (ppm, SKU lines, marker padding) | 161 | `playback_overlay.py` | medium / low |
| Export | 144 | `clip_export.py` | medium / low |
| Elastic log session | 126 | `elastic_log_session.py` | medium / low |
| Cache façade | 194 | delete the forwarders; callers use `viewer.clip_cache` | low / low |
| Construction, playback, frame display, shutdown | ~1,000 | stays | — |

Order: log filter panel, analysis panel, export + log session + annotations,
OCR controller, overlay strip, then VideoSource/TimeAlignment last. The
first three tiers take the file from 5,658 to about 3,700 lines without
touching the playback hot path.

### `MainWindow` (2,029 lines)

Extract in this order: `PaneAnimator` (§2d, 175 lines to ~60), the Overview
to clip navigation state machine (197 lines, `overview_navigation.py`), the
activity bar (93 lines, owns its own state), session persistence (106 lines,
testable), version check and relaunch (58 lines, into `core/app_version`),
the top bar as a widget (~180 lines).

## 6. Hygiene

- **75 unused imports** across 14 files, verified one occurrence each. The four leftover `QThread` imports are actively misleading after the Job port. Straight delete.
- **68 `hasattr`/`getattr(self, …)` self-probes in `replay_view.py`**, every one naming an attribute `__init__` always creates. They hide typos and make the class's real interface invisible.
- **84 `except …: pass` blocks.** The ones that hide bugs rather than tolerate Qt teardown: the two splitter animation steps, `_plan_ocr_video_source` (a cache failure silently reads over SMB, the 30 s freeze the docstring warns about), `_ocr_video_source` (copy failure silently falls back to the share), the two `_auto_sync_*` cached-offset parses (a bad value resets the offset to None with no message), and four in `qt_worker`. Narrow each to the concrete exception plus one status line.
- **147 `print()` calls, 52 in the replay view.** One is unconditional at the top of the 270-line `_redraw_timeline`; nine sit in the two per-frame methods; a dozen fire on every filter click. Proposal: a 15-line `core/log.py` with `dbg(tag, msg)` gated on `LOGFATHER_DEBUG` and a `timed(tag)` context manager, applied mechanically.
- **Magic colours.** Nine status colours are repeated as literals 62 times; four of them already have a theme token the code walks past (`#2ecc71` = `SUCCESS_BRIGHT`, `#9aa0a6` = `TEXT_MUTED`, `#ecf0f4` = `TEXT_BRIGHT`, `#e74c3c` = `LEGEND_OPERATION`). Add `WARNING` (#f0ad4e), `DANGER_SOFT` (#ff7a70), `OCR_TIME` (#00ff5a), `OCR_DATE` (#c77dff) and replace; `time_ocr.py` alone holds 26 literals and imports no theme.
- **Magic numbers:** the verify candidates `[-2,-1,0,1,2]` (twice), inlier tolerances 1.0 and 2.0, the 4 MiB copy chunk, the 0.5 s poll, `frame_count - 60`.
- **String-keyed dicts that want dataclasses:** `OcrOffsetStore.get()` (parsed in four try/excepts at the call sites), `_summarize_system`'s return dict (asserted by 20 tests), the `_plan_ocr_video_source` triple, the `_pending_video_load` triple, the marker tuples with a parallel source enum.
- No mutable default arguments, no bare `except:`, no local re-imports that shadow module imports (the `sync_icon` class of bug is closed).

## 7. Tests

Cheap, uncovered pure functions: `build_events_from_rows` and the
`log_events` formatters; the `timeline_model` timezone helpers (DST and
naive/aware boundaries, where `_is_past_day` already bit once);
`inferred_live_clip_end`; the TimelineItem (de)serialisers; the four query
builders as snapshots; `_custom_filter_match` (the AND branch was flagged
unreachable and nothing proves it either way); `pick_rate` buckets;
`ClipCache._prune_cache` (deletes user data, untested); `_additional_clip_covering`;
the window-placement geometry.

Brittle: `test_overview_summary.py` calls `_summarize_system` unbound with
the class as `self` (breaks the moment the method reads any attribute);
six tests use `date.today()` / `datetime.now()` and will drift around
midnight and DST; `test_parsing.py` (902 lines) monkeypatches
`elastic_loader` module attributes and will need editing on every split;
`test_time_ocr_engine.py` tests the module twins, not the methods the GUI
runs (fixed by §2a).

## 8. Ranked plan

| # | Refactor | Effort | Risk |
|---:|---|:--:|:--:|
| 1 | Delete the 75 dead imports, the 68 self-probes, the §3 dead code and the two dormant subsystems | S | low |
| 2 | `time_ocr` engine merge: one `estimate_offset(..., hooks)`; the GUI inherits the tests | M | med |
| 3 | Theme tokens for the nine status colours; name the OCR magic numbers | S | low |
| 4 | `log_filter_panel.py` out of the replay view, collapsing the sextet and triplets on the way | L | low |
| 5 | `BusyDialog` + `job_progress()` and apply to the nine sites | S | low |
| 6 | `PaneAnimator` for the four animation triples | S | low |
| 7 | `OcrChannel` + one open/auto-sync pair for main and Additional CCTV | M | med |
| 8 | `core/log.py` and convert the 147 prints | M | low |
| 9 | `analysis_panel.py`, `clip_export.py`, `elastic_log_session.py` out of the replay view | M | low |
| 10 | `SYSTEM_ID_OVERRIDE` to an explicit `SystemRef` argument | M | high |

Then: `overview_navigation.py` out of MainWindow, the dataclasses from §6,
the activity bar, finishing `icons.py`'s `_start`/`_finish`, and the
Overview build-once-mutate redraw last.
