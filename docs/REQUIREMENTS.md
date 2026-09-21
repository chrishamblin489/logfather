# The Logfather — feature requirements

Living record of agreed functionality: what is open, and what has shipped
(with the date and who asked). Kept separate from the code-quality plan in
`CODE_REVIEW_2026-09.md`. Updated with every shipped feature (Chris,
2026-09-05: keep this regularly updated).

## Open

- Elastic real on-disk sizes in the Data window (2026-09-05): needs the
  app's API key granted the `view_index_metadata` (or `monitor`) index
  privilege on `logstash-*,pikpak,pikpak-*`; the code already prefers
  `_cat/indices` and falls back to sampled estimates until then.
- Elastic log-volume reduction at source (2026-09-05, from the PikPak010
  audit): enumerate templated messages/node names/states, drop constant
  fields, one numeric timestamp, store planner payloads once, pair
  start/end events; index-side `best_compression` + keyword mappings.
  Company decision; see the audit report.
- CCTV retention beyond ~30 days (2026-09-05): company decision on the
  share; the app assumes nothing about 30 days except the 14-day
  day-listing cache TTL and the overview's 14-day clip-scan cutoff.
- PikPak customer simulator (Chris, 2026-09-21): interactive 3D page for
  sales meetings, one self-contained HTML in `simulator/`. The customer sets
  product size/shape, a photo for the top of the punnet, crate and pack
  pattern, infeed rate and spacing. The model is a simplification of the
  real cell: infeed conveyor, the gate the products line up against, a
  simplified AUBO i10 arm with its vacuum-cup array, and the tray change
  system (Chris to describe). Engine and arm geometry shipped (below); the
  3D scene, with the SKU upload button and product list, is next. Still
  needed: inside sizes of the standard trays (600 x 400 is the nominal
  outside), a better-picture search for weak SKU images, and real figures for cycle time, tray change
  time and belt speed (the engine's defaults are placeholders, not claims).

## Shipped

### 2026-09-21

- Simulator engine (Chris): `simulator/src/engine.js`, pure logic with no
  DOM. `packPattern` (rows x columns x layers, auto 90 degree turn, gap,
  layer cap, slot positions). `Simulation` follows the real flow (Chris,
  same day): the belt runs products into a stop gate where they bunch up
  nose to tail; once `productsPerPick` are pressed up in a line the arm
  lifts them all at once with one vacuum cup each and releases them into
  consecutive slots; a full crate is changed while the arm returns. Reports
  packed / picks / crates / rolling ppm / utilisation / time the line is
  backed up to the belt start; seeded so a run repeats.
  `estimateCapacityPpm` is the ceiling: the slower of the arm's cycle and
  the line re-forming at the gate, plus the crate change.
- AUBO i10 geometry (Chris): `simulator/src/aubo_i10.js` holds the link
  lengths, side offsets, flanges, joint ranges and speeds from the AUBO
  datasheet, `solveToolDown` (elbow-up pose for a flange facing straight
  down, with the points the links are drawn between) and `minMoveTime`
  (joint-speed floor for a move). Housing diameters are scaled off the
  drawing, marked approximate.
- Simulator SKU files (Chris): `simulator/src/sku.js` reads what the sales
  team uploads, CSV saved from Excel (comma, semicolon with decimal commas,
  tab, BOM, loose header spellings) or JSON. One line = one product in one
  tray: product length/width/height mm, weight, top-down picture, tray
  inside size (blank length and width = 600 x 400, most trays), rows x
  columns per layer, layers, optional products per pick, orientation and
  the customer's line rate (`infeed_ppm`).
  Fit check (Chris): `layoutPattern` in the engine works out the spare room
  in x (tray length), y (tray width) and z (depth to the rim). Each product
  may squeeze by 5 mm per direction (Chris, same day, after 5 of a customer's
  11 real layouts ran 4 to 14 mm over on paper): inside that
  allowance the line loads flagged as a tight fit and is drawn squeezed;
  beyond it the line is refused with the overrun in mm, so it never reaches
  the simulation. Missing weight or picture only warns. Pictures are paired
  by file name (`matchImages`). Template and column guide in
  `simulator/skus/`.
- Simulator tray station (Chris): half-size trays (about 400 x 300) go
  through side by side in pairs, turned 90 degrees, covering the same
  footprint as one 600 x 400 tray and changed together. `stationPattern`
  in the engine gives the slots in station coordinates in fill order (layer
  by layer, in lines the way the vacuum head sets them down; a line carries
  on from the first half tray into the second; 3 x 2 lifted three at a time
  is set down across). SKU files default `trays_side_by_side` to 2 for a
  tray 400 x 300 or less inside; `perStation` is what is packed between two
  tray changes. three.js r160 is vendored in `simulator/vendor/` for the
  single-file page.
- Simulator page, first version (Chris): `python simulator/build.py` inlines
  three.js, the engine, the arm, the SKU reader and `src/scene.js` into
  `simulator/pikpak-simulator.html`, one file that works offline. 3D cell
  in simple blocks: infeed belt, the gate across it, the AUBO i10 on a
  pedestal posed by `solveToolDown` every frame, a vacuum head with one cup
  per product, the tray station (one tray or two half trays) on a
  placeholder track that rolls full trays out. Moves swing around the
  robot's base. Panel: product list, upload of SKU file plus pictures (the
  picture goes on the product's top face), refused and tight-fit lines,
  sliders for arrivals, spacing, belt speed, robot move, tray change and
  playback speed, and the result (packed per minute, the most it can pack,
  arm busy, belt backed up, a plain keeps-up verdict). Orbit, zoom, pan.
- Simulator cell layout from the PikPak 2 general assembly (Chris, redone
  2026-09-22 after his review: "not like the STEP file"): the STEP file's
  part descriptions name every sub-assembly, so each block now sits where
  the CAD has it (names in the `CELL` comments in `scene.js`): 4.2 m x
  405 mm infeed conveyor at 730 mm running right through the machine (belt
  speed default now its 48 m/min); robot on its pedestal at the head of the
  tray lane, 468 mm from the belt centre, camera above the pick; internal
  tray belt with the ejector arm's end stop beside the robot, where the
  tray is filled 600 across the lane; powered rollers beside it; outside,
  the sloping gravity infeed rollers and the gravity outfeed rollers with
  their end stop, side by side; control box under the belt, HMI tablet,
  open weldment frame. Trays: empties queue on the infeed rollers, one runs
  in to the end stop, the ejector pushes the full tray across, it runs out
  to the outfeed end stop and is lifted off after a while. The 3D view is
  centred beside the panel. Not in the CAD, so still an estimate: the
  product gate (placed so the line sits level with the robot and camera).
- Simulator axes and handedness (Chris, 2026-09-22): the STEP file's own X,
  Y, Z axes are drawn at its origin (checkbox in the panel), so a position
  read off the CAD maps straight into the scene: scene x = CAD Z, y = CAD Y,
  z = -(CAD X + 2755). The first CAD layout had the CAD X sign wrong, which
  made the scene a mirror image of the machine (robot and tray lanes on the
  wrong side of the belt, arm offsets mirrored); now a true turn. Trays
  travel 600 across the lane, 400 along it (Chris: they were 90 degrees
  out). Default view is from the HMI side: products run left to right.
- Simulator gate position (Chris, 2026-09-22): about 1 m further from the
  robot along Z, then back by about 200 mm: now CAD Z 700, 200 mm inside
  the outfeed side of the frame;
  the line backs up inside the machine from there. Arm reach to the line
  checked for 2 to 6 products per pick (tightest 1254 of 1350 mm).
- Simulator playback bar (Chris, 2026-09-22): Pause / Play, Restart, speed and
  run time sit in a bar under the 3D view, always in sight (they were at the
  bottom of the panel, below the fold); space bar pauses and plays; a paused
  view is outlined and the camera still turns, zooms and pans.
- Simulator tests live in `simulator/tests/*.test.js`, run under node and
  from pytest (`tests/test_simulator_engine.py`).

### 2026-09-15

- Replay step buttons (Chris, 2026-09-15/16): -1s and +1s either side of
  the play button under the CCTV picture, stepping the clip one second
  (the clip's own frame rate in frames) back or forward, pausing first;
  hold to keep stepping. First shipped as -10 / +10 frames, but a backward
  step seeks and lands on the nearest keyframe, which these cameras write
  once a second, so it showed a whole second anyway; the label now matches.

### 2026-09-14

- Fixes after the refactor (Chris, 2026-09-14): the Overview PikPaks
  filter applies even when a load is still running - closing the popup
  now retires the running load before reloading (before, the refresh was
  skipped and the old load kept filling in every system); the shared
  StageProgress helper treats a dialog destroyed underneath it (the Sync
  CCTV Time window auto-closing mid readings table) as a cancel instead
  of raising, and the readings table stops when the window is closing.
- Review item 10 done (Chris, 2026-09-14), the last known cross-thread
  data path: the module global SYSTEM_ID_OVERRIDE in elastic_loader (set
  by the date picker's SIM Logs mode on the UI thread, read by the fetch
  workers, so a fetch for one PikPak could silently query another robot
  if the picker changed mid-flight) is gone. The robot id is resolved
  once on the UI thread by elastic_schema.resolve_robot_id(root,
  override) - override wins, else the PikPak folder - and passed to
  every per-system fetch as a required robot_id keyword (fetch_events,
  fetch_sku_items, fetch_logs_for_range, fetch_buffer_events) through
  the job's arguments: the timeline's extra loaders, the clip log
  session, the Targets buffer load and the Stop report. MainWindow's
  system_id_override is the single source of truth. Same id value as
  before, so the events cache keys are unchanged (no schema bump). The
  Overview and the Fleetwide search never used the override. 14 new
  tests.
- Review item 9 done (Chris, 2026-09-14): four clusters out of the
  replay view (4,361 -> 3,485 lines): ui/analysis_panel.py (the Analysis
  tab's diff / optical-flow controls, view and popout), ui/clip_export.py
  (export with overlays burnt in: open the writer, render the frames, mux
  the audio, one try/finally for the temp files), ui/elastic_log_session.py
  (the per-clip Elastic log fetch lifecycle with ready/failed signals) and
  ui/clip_annotations.py (the annotation files, undo stack, tools, colours
  and context menu; the canvas stays in annotated_video_widget.py). Fixed
  on the way: the clip export had been broken since the progress-dialog
  change earlier the same day (it called show() on the new helper), an
  ffmpeg that cannot run now reports "audio could not be muxed" instead of
  crashing, and a failed save no longer leaves temp files. 58 new tests.
- Review items 6-8 done (Chris, 2026-09-14): (6) one PaneAnimator
  (ui/pane_animator.py) behind the Targets panel, date picker, timeline
  height and right-tabs slides - same durations and sizes, the two bare
  except-pass wrappers gone; (7) one OcrChannel (ui/ocr_channel.py) for the
  main camera and the Additional CCTV: one open-Sync-window and one
  automatic-sync path, both cameras now get the plausibility drop with its
  log line and the filename fallback ladder, store files and keys
  unchanged; (8) core/log.py: `log(tag, msg)` always prints, `dbg` and
  `timed` only with LOGFATHER_DEBUG set (1 or a tag list) - the 137
  prints are converted, the informational lines (version, calibration,
  Elastic counts, OCR plausibility, clip load times) still print by
  default and the redraw, per-frame and filter-click chatter is opt-in.
- Review items 4 and 5 done (Chris, 2026-09-14): (4) the log filtering
  (Filters and Custom tabs, the 15 presets, the custom filters and their
  persistence, the tab highlights, the row matching) moved out of the
  replay view into ui/log_filter_panel.py (LogFilterPanel, with the
  matching logic as pure functions under test) - the replay view is 898
  lines shorter; the six select-all/none and three reset copies are one
  method each; behaviour unchanged, including two quirks now pinned by
  tests (the custom filter's AND mode is unreachable from the UI, and a
  message-column filter applied while the Custom tab is on screen is
  dropped because it keys off checkbox visibility - follow-up). (5) one
  ui/progress.py: BusyDialog (indeterminate, no cancel), StageProgress
  (determinate with Cancel, nothing shown headless) and job_progress (a
  dialog bound to a JobSlot); the nine hand-rolled QProgressDialog sites
  in the replay view, the timeline, Sync CCTV Time and the Stop report
  use them, with the same titles, labels and cancel behaviour.
- Review items 1-3 done (Chris, 2026-09-14): (1) 74 unused imports, 54
  of the 57 self-probes in the replay view, the grep-verified dead code
  (SettingsDialog, the radar TargetScopeWidget module, dead methods and
  constants) and the two dormant subsystems (the pick-rate heat strip and
  the hidden yellow log-marker bar with the marker chain that only fed it)
  removed - about 1,000 lines, no behaviour change except two wasted
  timeline redraws per clip open; (2) one OCR clock-sync engine
  (`estimate_offset` with `SyncHooks`) shared by the Sync CCTV Time window
  and the automatic sync, replacing the window's duplicated copies - the
  window now uses the better mid-clip verify rule (at least two seconds
  after the sync frame) and its progress bars count from the sync frame;
  11 new engine tests; (3) the status colours (amber warning, soft red,
  the OCR box green and purple, bright and muted text) are theme tokens
  instead of literals across the ui; the per-track telemetry palettes and
  the icons' illustration colours stay literal on purpose.
- Code review and renames (Chris, 2026-09-14): modules, classes and
  controls renamed to what the screen calls them (replay_view / ReplayView,
  replay_timeline / ReplayTimeline, data_boxes / DataBoxes,
  SyncCctvTimeWindow, conveyor_btn, targets_toggle, drift_*, gap_*,
  info_text_btn, birds_eye_*, additional_* for the Additional CCTV, and
  so on - the full table is in docs/CODE_REVIEW_2026-09-14.md); saved
  mode names are now "replay" / "search" with the old values still read.
  The About page was rebuilt: a new schematic of the layers and screens,
  a "Where each screen lives" tab, every module described, links to the
  chrishamblin489 repo; docs/ARCHITECTURE.md's file table is generated
  from it (tools/gen_architecture_table.py); the smoke test imports every
  module by walking the package. The review itself, with the ranked
  refactoring plan, is docs/CODE_REVIEW_2026-09-14.md.
- First-product button (Chris, 2026-09-14): an arrow-to-punnet icon
  badged "1", just left of the drift tool in the top bar, seeks the
  viewer to when the first product was seen on the loaded clip (the
  earliest target_added buffer event in the clip's span) - the best
  moment to set the drift. Seeks through the OCR-corrected start, or the
  filename time without one; a message explains when no product is
  known yet. Shown with the other viewer tools.

### 2026-09-13

- Track needs a conveyor calibration (Chris, 2026-09-13): the Track
  button in the top bar stays unticked (tooltip "Calibrate the conveyor
  first") while the current system has no tracking line. Ticking it
  then unticks it again and shows a warning ending "Do you want to
  calibrate the conveyor now?" - Yes opens the Conveyor dialog. Once a
  calibration is saved, or a calibrated system is selected, Track ticks
  itself and draws.
- PikPak replay chart zoom buttons (Chris, 2026-09-13): round - and +
  icons to the left of the View button stretch or contract the timeline
  about the middle of the view (text sizes and bar heights are
  unchanged). Hold to repeat. 24 px circles with the plain 24 px glyph
  (Chris, 2026-09-13). The plain
  mouse wheel over the chart now zooms
  about the cursor the same way; Shift+wheel still scrolls along the
  day and Ctrl+wheel scrolls up and down (plain wheel scrolled up and
  down before).
- Every stage can be cancelled (Chris, 2026-09-13): each progress dialog
  in the Sync CCTV Time window has a Cancel button - the date scan
  ("Comparing displayed date to filename date..."), the coarse and
  frame-by-frame clock scans, and the readings table. Cancel stops the
  run at that stage and the later stages are skipped (an amber
  "cancelled" note on the sync line, "OCR: clock checks cancelled", or a
  "(cancelled ...)" row in the table); step H still stores the boxes, and
  Sync Time starts a fresh run. The flowchart carries this rule under the
  steps.

### 2026-09-12

- Sync CCTV Time tidy-up (Chris, 2026-09-12): the Tesseract path, Offset,
  Time and Frame lines under "Enable OCR" are gone, and the picture is
  exactly as tall as the zoomed band at the window's width, so no black
  band sits between it and the slider (spare height goes to the bottom of
  the column).
- Zoom tick box always reachable (Chris, 2026-09-12): "Zoom to the clock
  area" sits in the top row beside the help button, and the un-zoomed
  whole frame is capped to about 45% of the screen height (side bars
  rather than a picture that pushes the controls off screen).
- Sync CCTV Time readings table (Chris, 2026-09-12): the Frame / Exact
  time / FPS list is built once by the clock checks (step F): every
  second change in the first 10 s after the sync frame, then one drift
  check every 60 s through the rest of the clip. A drift check finds the
  next two second changes within 2.5 s of its checkpoint (coarse reads
  every 0.2 s, then a bisection to the exact frame,
  `find_second_boundaries`) and shows one row: the frame the second
  began on and how many frames it lasted. Checks that read no change are
  counted on a final line. Scrolling never adds rows; the row closest at
  or before the current frame is green and the rest plain. The
  analysis's disregarded/invalid notes go to the console instead of the
  list. Header and rows share the same regular-weight monospace font and
  left inset so the columns line up (2026-09-12).
- Sync CCTV Time frame-step buttons (Chris, 2026-09-12): -10, -1, +1 and
  +10 under the slider, sized like the conveyor calibration window's.
- Sync CCTV Time slider labels (Chris, 2026-09-12): "Frame 1" to the left
  of the scrub slider, "Frame x" (the clip's last frame) to the right, and
  "Current frame (y)" above the slider, following the handle.
- The Sync CCTV Time help button is the pixel-art question block used by
  the other windows (Chris, 2026-09-12), at 32 px; the Data Sources
  window's two question blocks were shrunk from 44 px to match.
- OCR offset store never loses offsets (Chris, 2026-09-12): writes go to a
  temporary file beside the store and are renamed into place (atomic), and
  a file that fails to parse is moved aside as `<name>.corrupt-<stamp>`
  with a console note instead of being silently replaced by an empty store.
- Timeline click lands on the clicked moment (Chris, 2026-09-12): the
  seek goes through the clip's OCR-corrected start
  (`video_seconds_for_wall_time`), so the green clock reads the time that
  was clicked; clips with no OCR offset still seek by the filename time.
- Additional camera keeps its own OCR boxes (Chris, 2026-09-12): its Date
  and Time box positions are stored under "<PikPakNNN>/additional" in
  ocr_settings.json, separate from the main camera's, both in the Sync
  CCTV Time window and in the automatic sync. Until it has boxes of its
  own it starts from the main camera's. The "Date (frame x)" caption in
  the window is purple like the date box; the green/red verdict stays on
  the "Camera date sync" line.
- Date procedure (Chris, 2026-09-12), in the Sync CCTV Time window: A. use
  the date and time boxes as placed; B. read the date on frame 1; C. if
  it matches the filename date, skip to F; D. otherwise scan the clip for
  the frame where the date changes from frame 1's; E. show that frame's
  date in the "Date (frame x)" panel and mark it green when it matches
  the filename date, red when not (or red "none" when it never changes);
  F. the clock checks carry on from that frame; G. dragging either box
  waits two seconds and then reruns from A (including F); H. the date and
  time box locations are then stored (ocr_settings.json, per camera). A
  "?" button at the top of the window opens a flowchart of these steps.
  Fixed 2026-09-12: the procedure had crashed on every window open
  (numpy array used with `or`), so nothing ran until v0.439.
- Date box re-read after dragging stops (Chris, 2026-09-12): the purple
  box's date is read again two seconds after the last drag, not on every
  move, so another corner can be pulled first.
  The initial date check and the "Date (frame 1)" preview always use
  frame 1 of the clip, wherever the scrubber is.
- Camera date sync (Chris, 2026-09-12): when the burnt-in date on the
  first frame is 01/01/1970 or differs from the filename, the Sync CCTV
  Time window scans the clip (a date read every second, then frame by
  frame) for the frame where the date changes. A line under the CCTV date reads "Camera date sync: 01/01/1970 -> 12/09/2026 (frame 34)" (hover for the frames and seconds it took), or in red that no sync was found. The clip
  is then dated by the new date and the clock is read from that frame,
  both in the window and in the automatic sync (which uses the saved date
  box).
  On the right the previews run Date (frame 1), then Date (frame x) from
  the sync frame (shown only when the first frame's date differed from
  the filename and a sync was found), then Time (frame y), all the same
  size.
- Date and Time previews (Chris, 2026-09-12): the right-hand side of the
  Sync CCTV Time window shows a large view of the purple date box above
  the green time box, labelled "Date (frame 1)" (the first frame's date,
  refreshed when the date box is dragged) and "Time (frame x)" for the
  frame on screen, each in its colour.
- CCTV date box (Chris, 2026-09-12): a second, purple box in the Sync
  CCTV Time window, by default to the left of the green time box and
  dragged the same way, saved per system under date_roi_by_key. The date
  in it is read once per clip (and again when the box is moved) and shown
  as "CCTV date: 10-09-2026 - matches the filename" in green, or in red
  when it differs from the filename date. The burnt-in date is always
  DD/MM/YYYY, and only that form is accepted.
  A burnt-in date of 01/01/1970 is the camera's unset default and is
  reported as such in amber rather than as a mismatch.
  Tesseract reads the camera font's slashes as 7 (or drops them), so
  the ten characters are also read by position and eight bare digits as
  DDMMYYYY.
  "Date" and "Time" captions sit above the boxes; numbered instructions
  under the picture: 1. ensure the Date and Time boxes are in the correct
  place on the CCTV image; 2. press "Sync Time" to find the exact frames
  when the second changes. The readings list on the right has a fixed
  "Frame  Exact time  FPS" header in a monospace font; when the first
  and last frames of a second are both known exactly (the clock changed
  between consecutive frames at both ends), the frames in that second are
  shown as FPS on the row where it began.
- OCR window title (Chris, 2026-09-12): "Sync CCTV Time" instead of
  "CCTV Time OCR".
- Filename date and time in the OCR window (Chris, 2026-09-12): top left
  reads "Filename date: 12-09-2026" over "Filename time: 06h 54m 16s" from
  the clip's name; hovering either shows the full filename.
- OCR box dragged on the picture (Chris, 2026-09-12): the OCR window's
  four ROI sliders are gone; the green box is dragged on the frame by its
  corners, edges or middle, saved as the same ratios as before (to full
  precision now). The picture opens zoomed to the band around the clock
  (a "Zoom to the clock area" tick shows the whole frame instead); the
  zoomed band stays put while the box is dragged, and is chosen afresh
  when the tick changes or a clip opens. The live OCR readout follows the
  box as it moves. Dragging a corner moves only that corner and its two
  neighbours: the box is kept exactly as dragged while the window is
  open, and the ratio maths rounds rather than truncates.
- Drift tool in the top bar (Chris, 2026-09-12): the Drift caption,
  slider and readout sit in the top bar to the left of Sync, always
  visible with the other viewer tools, instead of inside the sync strip.
- Sync button (Chris, 2026-09-12): a clock icon; after a successful sync
  (automatic, cached or manual, both cameras) it turns green and reads the
  offset to one decimal, e.g. "Sync: +1.3s"; with a clip open but no sync
  it reads "Sync: ?" and breathes gently, and a press opens the OCR
  window. The main camera's button sits in the top bar to the left of
  Conveyor, shown with the other viewer tools.
- Second camera plausibility check (Chris, 2026-09-12): the additional
  camera's OCR offset gets the same 15-minute plausibility check as the
  main camera at all four sites (cached on open, cached in the automatic
  path, fresh automatic result, manual approve).
- OCR engine unit tests (Chris, 2026-09-12): tests/test_time_ocr_engine.py
  covers the filename stamp parser, clock-text validation, the midnight
  rule, the ROI maths, the vote over samples and the crop preprocessing;
  an empty sample list no longer raises inside the four-stage scan.
- Conveyor and Track buttons (Chris, 2026-09-12): "Calibrate" reads
  "Conveyor" with a belt-and-rollers icon; "Track" has a punnet-of-tomatoes
  icon. Both painted in icons.py.
- OCR offset write-up (Chris, 2026-09-12): docs/OCR_OFFSET.md explains the
  offset, the time maths, how the clock is read and voted, the three entry
  points, the stores, what depends on it, and the known weaknesses.
- Implausible OCR offsets rejected (Chris, 2026-09-12): the green line
  and clock were missing for today's clip because an automatic OCR read
  had stored an offset of -24,774 s, putting the clip start at 00:01. An
  offset over 15 minutes either way is now dropped (cached ones removed
  from the store, new ones not saved) and the clip start comes from the
  filename instead. The picture's View menu shows the OCR offset in use;
  clicking it opens the sync tools.
- Green line survives Refresh (Chris, 2026-09-12): loading or refreshing
  a day on PikPak Replay no longer forgets the viewer's time, so the green
  playhead is drawn again as soon as the timeline appears instead of
  waiting for the video to move.
- Click anywhere to set the time (Chris, 2026-09-12): a click on the
  PikPak Replay chart that is not on a clip, SKU box or event tick moves
  the green playhead to that moment at once and opens the clip covering
  it there, if there is one.

### 2026-09-11

- Playhead through every bar (Chris, 2026-09-11): the green line on the
  PikPak Replay timeline runs from the time scale to the bottom of the
  last row (or the last readings strip, whichever is lower), instead of
  stopping under the CCTV bar when no strips are drawn.
- Replay calendar highlights one day (Chris, 2026-09-11): the PikPak
  Replay day popup highlights only the selected day; days with footage
  are bold rather than shaded, and the hint names the selected day.
- Slimmer clip bars, no pick-rate heat (Chris, 2026-09-11): the CCTV and
  Additional CCTV bars on the PikPak Replay timeline are 14 px tall (SKU
  boxes keep 24 px for their text), the event ticks on the condition rows
  (gate operation and the rest) are 14 px to match, and the red pick-rate heat strip and
  the heat overlay on the selected clip are no longer drawn, since the
  Picks strip shows the rate.
- View menu on the timeline (Chris, 2026-09-11): a "View" dropdown at the
  top right of the PikPak Replay chart shows or hides each bar: CCTV,
  Additional CCTV, SKU, Start / Stop / E-stop, Telemetry, and each
  condition row (the same ticks as the Errors box, kept in step). Hidden
  static rows are remembered in ui_state (replay_bars_hidden).
- Telemetry row optional (Chris, 2026-09-11): the Telemetry row is off
  the PikPak Replay timeline by default; "Telemetry row on the timeline"
  at the top of the Additional data menu turns it on, remembered in
  ui_state (replay_telemetry_row).
- Stop report and Fit in the gear menu, Refresh top right (Chris,
  2026-09-11): "Stop report..." and "Fit timeline to the day" are gear
  menu entries (Stop report greys out while a report builds); Refresh is
  an icon button (circular arrow) beside the gear at the top right. The
  playback row keeps only the clock and the play button.
- Label gutter (Chris, 2026-09-11): on the PikPak Replay timeline a solid
  black bar runs the full height of the scene
  behind the row and strip labels (CCTV, Additional CCTV, SKU, Start /
  Stop / E-stop, the conditions, Picks and the readings), as wide as the
  widest label, and follows the horizontal scroll, so no chart data
  shows under the labels.
- Play rolls into the next clip (Chris, 2026-09-11): pressing Play at the
  end of a CCTV clip opens the next clip on the day's timeline and starts
  playing it; with no later clip, Play behaves as before.
- Play button centred on the bottom row (Chris, 2026-09-11): the play /
  pause button sits level with the green log-time clock, centred on the
  seek slider (so it never shifts when the additional camera appears),
  with the clock on the left and the report buttons on the right; the
  right column ends level with that row.
- Green line moves on click (Chris, 2026-09-11): clicking the CCTV row or
  an event tick on the PikPak Replay timeline moves the green playhead to
  that moment at once, before the clip has loaded; playback then takes
  over from there.
- PikPak Replay is one day (Chris, 2026-09-11): its calendar picks a
  single day, and when the Overview holds a span of days that selection is
  ignored on the way in: opening a system from the Overview keeps the
  replay's own day (or today) unless a moment on the row was clicked, in
  which case that moment's day is used.
- Product tracking overlays back (Chris, 2026-09-11): the pick-buffer
  load printed log lines with an arrow and dash character, which raise
  when the app's output goes to a cp1252 file, so the load failed with
  0 events and nothing was tracked on the picture. Those log lines are
  ASCII now (the last one was fixed the same way earlier).
- Playhead time label (Chris, 2026-09-11): the current time, hours and
  minutes, in green at the top of the green playhead line on the PikPak
  Replay timeline, riding with the pinned time scale.
- Green playhead on first load (Chris, 2026-09-11): the PikPak Replay
  timeline keeps the viewer's last reported time across a redraw and draws
  the green current-time line from it, so the line is there as soon as the
  day's timeline appears instead of after the video next moves.
- Play button (Chris, 2026-09-11): the PikPak Replay Play button is the
  same light-ink play / pause glyph button as the conveyor calibration
  window's transport row (54 x 44, 28 px icon).
- Repository moved (Chris, 2026-09-11): the GitHub repo now lives under
  the chrishamblin489 account (https://github.com/chrishamblin489/logfather);
  the old chris1leap address redirects. Commits are authored as
  chris.hamblin@helloleap.ai from this date.
- Additional CCTV in the View menu (Chris, 2026-09-11): a tick item that
  shows the additional camera beside the main picture when a clip covers
  the current time (loading it from the day's AdditionalCCTV folder if
  need be) and hides it again when unticked; greyed as "none for this
  time" when no additional clip covers the moment.
- Mode button icons (Chris, 2026-09-11): Overview shows three rows with a dot each, PikPak
  Replay a play triangle, Search a magnifying glass; painted in icons.py
  (tiles, bars and radar alternatives for the Overview are there too).
- Clip span beside the scroll bar (Chris, 2026-09-11): the clip's start
  time at the left and end time at the right, to the minute (06:35), on
  the same line as the CCTV seek slider, which is shorter by their width;
  the start comes from the filename until the OCR offset refines it.
- Data, Errors / Stops and Software buttons only on the Overview (Chris,
  2026-09-11): hidden on PikPak Replay and Search.
- Less around the CCTV image (Chris, 2026-09-11): the yellow log-marker
  bar under the picture is gone; the clip-time and frame counters above it
  are hidden by default; the green log-time clock sits next to Play; and
  Sync, Overlays and Info text moved into a "View" dropdown at the top
  right of the picture, which also has a switch for the counters
  (remembered as viewer_clip_counters).
- Collapsible Errors and Data boxes (Chris, 2026-09-11): on PikPak Replay
  the Errors and Data boxes have a ^ arrow after the title that folds the
  contents away (the arrow becomes v to show them again); the choice is
  remembered in ui_state (replay_boxes_collapsed). Additional data has no
  arrow and folds and unfolds with the Data box.
  The column of boxes ends level with the bottom of the Play button, and
  the log tabs above take whatever the folded boxes free up. The timeline
  status line under the boxes shows only when it has something to say.
- No "Load logs" button (Chris, 2026-09-11): removed from the Logs tab on
  PikPak Replay; a clip's logs load on their own when the clip opens.
- Motor conditions reordered (Chris, 2026-09-11): "Motor overcurrent" is
  slot 13 and the umbrella for the other controller trips is slot 14,
  renamed "Motor other fault". A saved pair in the old order is swapped
  and renamed on load; an old "Motor fault" slot is renamed too.
  Colours: Motor overcurrent orange (#ff7a45), Motor other fault yellow
  (#ffd666), enforced on load; the Settings dialog now keeps each slot's
  colour instead of resetting it to the slot default on every apply.
- Row headers always readable (Chris, 2026-09-11): the row labels and
  counts on PikPak Replay, and the strip titles and axis values on both
  PikPak Replay and the Overview, sit on a backdrop in the table's own
  background colour that moves with them, so they never read directly
  over the chart when scrolled and are invisible otherwise.
- No "Rate" label on PikPak Replay (Chris, 2026-09-11): the day rate heat
  strip under the time scale no longer carries a "Rate" caption, which sat
  by the CCTV row label.
- Elastic key test (Chris, 2026-09-11): the Data sources test probed the
  cluster root, which needs a monitor privilege the team's keys lack, so a
  working key reported "Rejected (HTTP 403)". It now asks who the key
  belongs to and runs a search over the log indices, reporting the owner
  and the log lines in the last 24 hours.
- Naming (Chris, 2026-09-11): the "System Replay" button reads "PikPak
  Replay", and the systems filter button on the Overview, Errors & Stops
  and Data windows reads "PikPaks" (with "(N hidden)"), its popup titled
  "Show PikPaks". Internal names and saved keys are unchanged.
- Picks visible zoomed out (Chris, 2026-09-11): a month-long Overview span
  samples the strips every 5 minutes (Grafana) or 30 minutes (Argus 1 pick
  rate from Elastic), and the line broke at every gap over five minutes,
  so Argus 1 systems showed nothing. The break now scales with the track's
  own sample spacing (2.5 steps, at least five minutes) and the hover
  readout looks within three steps.
- Motor overcurrent strip (Chris, 2026-09-11): the Additional data menu on
  the Overview (and System Replay) offers "Motor overcurrent: Overcurrent
  trips (running total)", a staircase of the "Current over limit" lines
  per system from Elastic, stepping up at each trip over the loaded span,
  so trips can be read against temperatures and currents.
- Motor overcurrent condition (Chris, 2026-09-11): a preset timeline
  condition "Motor overcurrent" (search phrase "Current over limit") fills
  slot 14 so the controller's over-current trip has its own row and total.
  "Motor fault" now excludes those lines ("Fault on motor" AND NOT
  "Current over limit") and covers the other controller trips: stop /
  enable / queue failures and rejected PVT points. A settings slot still
  holding the old "Fault on motor" query is upgraded on load.
- CCTV row labelled (Chris, 2026-09-11): the clips row at the bottom of
  System Replay is labelled "CCTV" with its clip count, like the
  "Additional CCTV" row under it; it had no label before.
- Data and Errors boxes never squashed (Chris, 2026-09-11): on System
  Replay the panel under the log tabs (Errors, Data, Additional data,
  status line) pins its minimum height to what its contents need and
  re-pins when the rows change, so the text stays readable on a short
  screen; the log tabs above shrink instead, to nothing if need be.
- Compact Errors and Data boxes (Chris, 2026-09-11): 11 px text, tight
  rows and box padding, smaller icons, and the Errors box in three columns
  of tick + total, so the log tabs above keep their room.
  The key labels beside the Data buttons use the same 11 px text, with a
  shorter colour block, and 3 px between rows.
- Errors & Stops counts events, not documents (Chris, 2026-09-11): one
  failure is logged as a cascade of state changes a few hundred
  milliseconds apart (controller node error, crate_change_package_error,
  package_error; or already_stopped_error with planner_error), which the
  per-document aggregation counted two or three times. The state-change
  documents are now fetched in time order and every document from the same
  system within two seconds of an event's first document is folded into
  it, named by the first state; stops and errors are clustered separately.
  Node-state and system-state documents are both kept, because most error
  states (high_current_error, planner_error, the sensor reading errors) only
  ever appear as node states. The window says at the top how the numbers
  are calculated. PikPak 010 on 22 August now shows 6 crate change errors
  instead of 12 plus 6 under System.
- Condition counts no longer doubled (Chris, 2026-09-11): a free-text
  timeline condition such as "crate_change_package_error" also matched the
  state-change document that followed, which names the error in
  previous_state_name. State-change documents now count only when their own
  state_name matches. PikPak 010 on 22 August showed 12 crate change errors
  for 6 events; Operator stop was doubled the same way.
  The events cache schema version was bumped to 3 so cached past days are
  refetched with the corrected counts.

### 2026-09-10

- Time scale always in view (Chris, 2026-09-10): on System Replay the hour
  ticks and labels stay at the top of the timeline view, over a dark band,
  while the reading strips scroll underneath; the cursor time marker moves
  with them.
- Errors & Stops per-system view (Chris, 2026-09-10): over 14 days the day
  heading above each cluster is just the day number, and the row under the
  chart shows each day's total instead of repeating the date.
- Eject crate is normal operation (Chris, 2026-09-10): it is ticked in the
  Data box, under the readings, with the day's total, not in the Errors box;
  its row shows by default whenever the day has any.
- Errors box (Chris, 2026-09-10): on System Replay, a box in the right
  column lists every timeline condition with the day's total and a tick to
  show or hide its row. A row is shown by default only when the day has more
  than one of that error; a tick the user changes is remembered in ui_state
  (replay_rows). Conditions with no events today are listed with 0.
- Shorter timeline (Chris, 2026-09-10): the Start, Operator stop and EStop
  condition tracks share one row labelled "Start / Op stop / E-stop" with a
  combined count, each keeping its own tick colour; track rows are 20 px
  apart instead of 24.
- No OCR on timeline clicks (Chris, 2026-09-10): opening a clip at a
  moment from the timeline or an event tick no longer forces an OCR sync;
  it seeks with the cached OCR offset if there is one, else by the clip's
  filename time. The Settings option "open OCR tool when offset missing"
  was switched off in Chris's settings at the same time.
- Faults stand out (Chris, 2026-09-10): a default timeline condition
  "Motor fault" (query "Fault on motor", orange-red) fills the first unused
  condition slot, and log lines carrying a motor fault, an error state, a
  drive warning or a stop input are drawn in orange-red in the log list.
- Actuator detail in the log list (Chris, 2026-09-10): "Fault on motor"
  and "Warning update" lines now carry the servo number and the fault or
  warning text, e.g. "act_controller | Fault on motor | servo 5: Current
  limit exceeded :: Current over limit", so they can be seen and searched.
- Click a clip at a moment (Chris, 2026-09-10): clicking a video segment
  on the System Replay timeline opens the clip and seeks to the moment
  under the pointer, instead of the start of the segment; the logs follow.
- Timeline default view (Chris, 2026-09-10): Fit, and the automatic fit
  on load and resize, shows every item of the day from half an hour before
  the first to the end of the last, scrolled to the start; the scale is
  fractional so a long day fits the view exactly.
- Timeline scrolling and zoom (Chris, 2026-09-10): the mouse wheel over the
  System Replay timeline scrolls up and down, Shift+wheel scrolls left and
  right along the day, Ctrl+wheel zooms about the cursor (between the whole day
  and one second per pixel). The view's minimum height dropped from 260 to
  110 px so the horizontal scrollbar is no longer clipped when the timeline
  is collapsed.
- Info text toggle (Chris, 2026-09-10): an "Info text" button on the
  playback bar, next to Overlays, hides or shows the green pick-rate / SKU /
  tray / tool text drawn over the CCTV image (main view and pop-out). On by
  default; the choice is remembered in ui_state as viewer_status_text.

### 2026-09-08

- Log-time clock back on the top row (Chris, 2026-09-08): the green
  computed log time sits between the clip position and the frame
  counter again; it had moved into the Sync strip on 2026-09-04.
- Fits a laptop screen (Chris, on site, 2026-09-08): the right column
  (log tabs plus the Data boxes) no longer imposes its ~780 px minimum
  height, so the timeline and activity bar stay on screen at 1463x866.
- Clip download progress (Chris, 2026-09-08): the activity bar and the
  'Loading clip' dialog show downloaded / total MB, percentage, MB/s
  and time remaining.
- Secondary windows fit the screen (Chris, 2026-09-08): the Data,
  Errors & Stops and Software windows open sized to the screen the main
  window is on, centred over it with the title bar kept visible, and are
  nudged back on screen after showing (the Data window had opened with
  its title bar above the top and could not be moved or closed).
- System Replay event ticks (Chris, 2026-09-08): each condition-track
  mark has a 10 px hit area with a hand cursor; hover shows the exact
  time to the millisecond, the full message, node / state / severity and
  any SKU details; a click opens the clip covering that moment, seeks to
  it and the logs follow.
- System Replay readings (Chris, 2026-09-08): the same Data and Additional
  data boxes sit above the day timeline, and every ticked family is drawn
  as a strip under the tracks for the chosen system and day, with the
  cursor dropping dots and a box of values, and drag-to-stretch. The
  boxes and channels are one shared component (`SignalBoxes`); the
  Overview and the Replay each remember their own selection.
- Data box: Picks (Chris, 2026-09-08), first row above Temps: picks per
  minute, scaled from 0. Argus 2 from Grafana's
  targeting_products_picked_per_min; Argus 1 (no such metric) from the
  pick messages in Elastic (`data/pick_rate.py`: one date_histogram,
  20 s buckets up to two days, then 5 / 30 minutes; each point is the
  trailing minute's rate; Chris, 2026-09-08).
- Additional data: CAN bus family (Chris, 2026-09-08): CAN bus errors, CAN
  errors near power event, CAN frames seen. Not cumulative counters: the
  health node reports a count per window and restarts, so they are drawn
  as reported rather than rate()d (rate() on them explodes).
- Overview Additional box (Chris, 2026-09-08), right of Data: one menu
  ticks CPU load (CCU / RCU, %), Memory (CCU / RCU, %), RCU to CCU clock
  offset (s), Motor halting errors (summed over motors) and Argus log
  queue (lines waiting); each ticked family is its own strip with the
  same hover box and drag-to-stretch, and a combined colour key sits
  under the button. These metrics are Overview-only (`in_replay=False`)
  so the System Replay Telemetry tab's day load stays quick.
- Overview air pressure (Chris, 2026-09-08): a Pressure row in the Data
  box (gauge icon) with the one reading, sensors_air_pressure in bar,
  drawn as a third strip under currents with the same hover box and
  drag-to-stretch. The System Replay Telemetry tab gains an Air pressure
  chart too.
- Overview currents (Chris, 2026-09-08): a Currents row in the Data box
  (lightning icon) works exactly like Temps: tick Highest motor (largest
  magnitude across fitted motors) or Motor 1/2/3/5/6, a colour key
  appears, a second strip under the temperature strip draws the traces
  in amps, hover drops a dot per trace and shows a box with the values,
  and the strip stretches by dragging its bottom line. Both channels
  share one class (`ui/overview_signals.py`) and one fleet fetch.
- Overview temperatures (Chris, 2026-09-08): Temps and its colour key sit on
  their own row under Systems in a box labelled Data. Hovering a strip drops
  a dot on each trace at the hover line and the hover label shows just those
  temperatures; the min/max/latest tooltip is gone.
- Temps menu lists each fitted motor (1, 2, 3, 5, 6) below the sensors and
  Hottest motor (Chris, 2026-09-08); slots 0 and 4 read a flat zero on every
  system over 30 days, so they are not offered. Each is its own colour and
  one fleet query filtered on motor_id. The hover box over a strip carries
  the system and time, a rule, then the readings in the key's colours with
  °C, in smaller type.

### 2026-09-07

- Overview temperatures (Chris, 2026-09-07): a Temperatures menu next to
  Systems ticks which readings to show (CPU, RCU, GPU, brake resistor,
  hottest motor); any ticked adds a strip under every system's lane with
  those lines over the visible window, the range in °C at the left, the
  latest values in the right column and min/max/latest on hover. One
  Grafana query per reading for the whole fleet; live mode refreshes
  every five minutes, a chosen span loads once. The choice is remembered.
  The button is "Temps" with a thermometer icon, a colour key appears
  beside it while any reading is on, and dragging a strip's bottom line
  stretches every strip (16 to 240 px, remembered).
- Data window: choose a date (Chris, 2026-09-07). "Last 14 days" and a
  calendar "Choose days…" button (a day or a span, newest 90 days at
  most) next to Systems; Elastic, Grafana and CCTV are all fetched for
  that span and the summary title names it. The date selection is shared:
  choosing days in the Overview, Errors / Stops or Data sets the other two
  the same (`ui/day_selection.py`); Live in any of them returns all to
  live.
- Overview opens on All Day instead of 1h (Chris, 2026-09-07); 1h and 5h
  remain a click away.
- Settings window (Settings / Systems / Readme tabs) is a real window
  with a title-bar close, a Close button, a size grip, and opens sized to
  the screen and centred over the main window (Chris, 2026-09-07: it ran
  off the page with no way to close).
- Data window, Grafana (Chris, 2026-09-07): a third tile between Elastic
  and CCTV with the telemetry retained in Grafana Cloud (13-month
  retention, from the stack's own samples-per-second history), active
  series, metric count and a last-14-days foot line; "Grafana samples"
  and "Grafana size" chart views per system per day (a 30 s subquery over
  count_over_time), click a bar to open the Actuators issues dashboard on
  that system and day. Sizes are samples × 1.5 bytes and say "estimated".
  A ? on the tile opens "What is stored in Grafana": every metric with a
  system label, its family and meaning, series per generation; click a
  metric for its series over the last day (min, average, max, latest).
  `data/grafana_inventory.py`, `data/grafana_catalog.py`,
  `ui/grafana_catalog_dialog.py`.
- Telemetry tab in System Replay (Chris, 2026-09-07): choosing a system
  and day fetches that robot's day from Grafana Cloud's Prometheus (CPU,
  RCU, GPU and brake-resistor temperatures; per-motor temperature and
  current; 30 s samples) through Grafana's query API, and draws one chart
  per group with the playhead across it and a hover readout. Unfitted
  motors (flat zero) are dropped; Argus 2 runs are stitched into one
  track. `core/telemetry.py` (pure), `data/telemetry_loader.py`,
  `ui/telemetry_strip.py`. The Data sources Grafana Test also probes the
  telemetry source and names the permission to grant when it is denied.
- Telemetry row on the day timeline (Chris, 2026-09-07): a "Telemetry"
  track under SKU draws the hottest motor's temperature through the day
  (CPU temperature when a system has no motor readings), breaking at gaps
  over five minutes; hover it for the day's range, the tab for values.
- Gear menu on every window (Chris, 2026-09-07): the top-right button on
  the main window, Errors / Stops, Data and Software is the same gear
  (`ui/gear_menu.py`) with Data sources, Settings, Systems, Readme, the
  zoom row and About; Errors / Stops keeps "Show PikPak key" above them.
  The old "⋯" overflow and the gear in the System Replay tab corner are
  gone. **Data sources** (`ui/data_sources_dialog.py`) holds the CCTV
  share, Elastic URL + API key and Grafana URL + token, each with a Test
  button that runs off the UI thread; those fields left the Settings tab.
- Grafana groundwork (Chris, 2026-09-07): Settings gains Grafana URL and
  Grafana token (a service-account token, stored like the Elastic key and
  never exported); `core/grafana.py` parses dashboard JSON (panels in
  rows, data source refs, query text per source type, $variables) and
  flattens DataFrame-JSON query results; `data/grafana_client.py` talks
  to Grafana's API and proxies queries through `/api/ds/query` so the
  app never needs to know the backend; `tools/grafana_check.py` prints
  the data sources and every query on a dashboard, answering "where does
  Grafana read from today".
- The mode switcher reads Overview | System Replay | Search (was Viewer
  and Fleetwide; Chris, 2026-09-07); the screens are unchanged.
- System Replay left panel retired (Chris): the old calendar and system
  list on the left, and its hover-reveal, are gone; Choose system and
  Choose date in the top bar do that job. The panel's logic (share
  scan, day highlighting) still drives those buttons behind the scenes.
- System Replay prompts (Chris): with no system chosen the Choose system
  button pulses; once a system is chosen the Choose date button pulses
  until a day is chosen; nothing pulses on the other screens.
- Pulses breathe (Chris): the Choose system / Choose date prompts and
  the Data window's stale Refresh fade between the raised ground and
  the accent fill over about three seconds (shared `ui/pulse.py`)
  instead of blinking on and off.
- Date before system (Chris): Choose date works with no system chosen;
  the day is held, shown on the button, and applied when a system is
  picked (the popup notes footage days appear once a system is chosen).
  The pulse then moves to whichever of the two is still unchosen.
- Choose system opens instantly (Chris): the menu uses the Overview's
  cached share listing instead of listing the WAN share on every click.
- Choose system opens the same grouped box as the Overview's Systems
  filter (shared `ui/system_filter.py`: SystemPickerPopup, one choice,
  current system highlighted; SystemFilterPopup, many ticks); the top
  buttons are unchanged (Chris, 2026-09-07).
- Viewer retention warning (Chris): choosing a day more than 30 days old
  shows "CCTV footage is deleted after 30 days" with a crossed-out camera
  icon where the footage would play (`core/retention.py`, tested
  boundary); the notice clears when a clip loads.
- Viewer Choose system button (Chris): funnel icon at the start; reads
  "Customer / PikPakNNN" with no line name, and the menu lists systems by
  name only.
- The system filter button reads "Systems" (with the funnel icon) on the
  Overview, and for consistency in Errors / Stops and the Data window
  (Chris, 2026-09-07); it is never elided, so "(N hidden)" stays whole.
- Data window scrolling (Chris): the same left / right arrows, zoom + / -
  and scrollbar as Errors / Stops (shared `ui/chart_scroll.py`); the
  chart opens on 14 days and scrolling past the oldest day loads seven
  more (hatched until they arrive), up to 90; the newest day is always
  today. The Elastic cache serves already-counted days.
- Data window tiles (Chris): the ? floats in the Elastic tile's corner so
  both tiles share the same spacing and the totals line up; the long
  summary lines under the tiles are gone, replaced by one short "Last 14
  days: ..." line at the foot of each tile.
- Overview controls (Chris): Live and Choose days sit right after the
  Systems button and never move; the Zoom 1h / 5h / All day trio follows
  them. Choose days carries a calendar icon and is highlighted while a
  chosen day or span is shown, as Live is while live (Errors / Stops
  too).
- Chart day axis (Chris): two lines instead of dd/mm per bar - the day
  number under each bar (thinned when bars are narrow) and the month
  name once per run of days, so 30+ days stay readable; both the Data
  chart and the Errors / Stops charts (which keep their month line on
  top and show day numbers only underneath).
- Newer-version notice (Chris): a running instance checks 90 s after
  start and every ten minutes whether the checkout's HEAD or origin/main
  (after a quiet fetch) is a newer version; if so an amber "v0.NNN
  available · Restart" pill appears in the top bar and the activity bar
  says so. Restart pulls (fast-forward) when the new code is only on
  GitHub, closes cleanly, then starts the new instance. Frozen builds
  without git skip the check (`core/app_version.py`, tested).
- Data chart Total / Per pick / Per hour running (Chris): per pick
  divides each system's daily figure (documents, Elastic size, clips,
  CCTV size) by its pick movements that day; per hour on divides by the
  hours the system was switched on that day, idle included (five-minute
  slots containing any document from it, times five; Chris, 2026-09-07); hover shows picks, running time and both per-unit figures. Picks come from "Picking products"
  (Argus 2) and "Successfully planned pick" (Argus 1) and are cached
  with the counts.
- Data window controls on two rows (Chris): Systems, Show metrics and
  Total / Per pick / Per hour on on the first; hint, status, Last
  updated, Refresh and Zoom on the second. Every button is fixed to its
  text width so nothing is cut off, "(1 hidden)" included.
- Data chart labels (Chris): right-click a bar segment for "Add label:
  PikPakNNN" / "Remove label"; a labelled segment gets an accent tag
  with the system name above its bar and a leader line down to it (tags
  stack when a day has several); the hover text says which applies. A
  tag can be dragged anywhere on the chart and its leader line follows;
  labels and their positions are remembered per user.
  Right-clicking the tag itself offers "Delete label" (Chris).
- Data chart order (Chris): Argus 1 systems first, then Argus 2, each in
  the usual customer order; generation read from which id field carries
  the bulk of a system's documents and kept in the local cache.

### 2026-09-05

- Viewer top bar (Chris): a Choose system button (menu of every system on
  the share, grouped by customer, current one ticked) and a Choose date
  button (one-month calendar popup; days with footage highlighted, no
  future days). Once chosen the buttons read the selection, replacing the
  Customer / Line / System label. The hover-reveal left panel stays.
- Data window, local Elastic cache (Chris): per-day document counts are
  kept under LOCALAPPDATA; a refresh counts only the days not yet cached
  (today always), reuses the oldest-record date and the sampled document
  sizes for a week. The window opens on the saved figures with "Last
  updated: <date>" beside Refresh; when they are not from today the
  Refresh button pulses until clicked.
- Data window Elastic tile reads "18 Mar 2022 – <last day with data>"
  rather than "since 18 Mar 2022" (Chris, 2026-09-06).
- Data window ? box (Chris, 2026-09-06): a pixel-art question block
  (gold, in the style of the classic platform game) in the Elastic
  tile's top-right corner opens "What is stored in Elastic" - one row per
  source node with a plain description of what it logs, its share of the
  last week's documents, example messages and the fields its documents
  carry, the fields in two side-by-side columns in the widest column
  (`data/elastic_catalog.py`, curated node descriptions + one live
  aggregation). Each field is a link: clicking it opens what the field is
  (curated note, or one derived from its mapping type), how often the
  node's documents carry it, and every value it has held over the last
  year with counts and shares (top 300; numbers also get min / avg /
  max; objects and bare text fall back to the latest 200 documents)
  (Chris, 2026-09-06). Enumerated fields get a Meaning column and a
  fuller note: syslog severity 0 emergency ... 4 warning, 6
  informational, 7 debug; facility always 3 = daemon; host a
  placeholder (Chris, 2026-09-06).
- Data window, click an Elastic bar (Chris): opens Kibana Discover in the
  browser on that system and local day (either robot-id field), so the
  actual documents can be read.
- Errors / Stops window (Chris): top-bar button; the Overview's machine
  filter and Live / Choose days picker (defaults to the last 7 days).
  Two daily charts with one bar per system inside each day (same colour
  and slot every day), so a system with far more errors, or a sudden
  rise, stands out (Chris: was stacked by category); hover a bar for
  that system's breakdown by kind (emergency, protective, operator,
  caution) or category (planner, targeting, motion, sensors,
  drives/power, crate change, system). Totals above, a per-system table
  below (stoppages, errors, most common error). Long ranges scroll
  sideways (one scrollbar for both charts, wheel over a chart too)
  rather than squeezing onto one screen; scrolling past either end
  loads seven more days, drawn as hatched "loading" columns until they
  arrive (Chris). A freshly chosen range is fitted to the screen and the
  width per day then locked, so loading more days scrolls rather than
  shrinking the bars; the wheel always scrolls, never zooms; Zoom + / -
  circles at the top right are the only way to change the width per day
  (never the text size); arrow buttons either side of each chart step
  back / forward by a fifth of the view, always landing on a whole day
  (wheel too), and load more days at the ends (Chris, 2026-09-06). The per-system colour key is hidden by default;
  "Show PikPak key" in the window's top-right ⋯ menu shows it
  (remembered per user; hover still names the system) (Chris,
  2026-09-06). The Live button carries the current date, "Live (Sun 6
  Sep)", here and on the Overview (Chris, 2026-09-06). A month / year
  line ("September 2026") sits above the day headings, one label per
  run of days in a month, staying in view while scrolling (Chris,
  2026-09-06). Clicking a bar opens that system and day in the viewer
  (Chris, 2026-09-06). Exact
  Elastic aggregations over both robot-id fields
  (`data/errors_stops.py`, classification tested). The stacked bar chart
  and day-range dialog moved to shared modules (`ui/charts.py`,
  `ui/day_range_dialog.py`).
- Software window (Chris): top-bar Software button opens a timeline -
  one block per PikPak system, one lane per package (argus, planner,
  targeting, actuators, sensors, infeed, crate_change, behaviour), a bar
  per dated span labelled "version (commit)", hover for branch, dates
  and node-start count; 30d/90d/6mo/1yr ranges. Built from
  `sw_version.*` and the health node's "Node git details" documents
  (`data/software_history.py`, tested span logic). Argus 1 systems show
  as rows with no data, since they log no version or commit fields.
  A commit that no other system runs gets a red outline. The raw facts
  are cached under LOCALAPPDATA; a refresh queries only the days since
  the cache was written, and a range the cache already covers is
  re-clipped locally without a query.
- Viewer log filters survive a reload (Chris): the source / state /
  message tick boxes the user unticked are remembered when the panels
  are rebuilt, and if filters were loaded before a reload (Refresh, or
  opening another clip) they load and apply again automatically with
  the same ticks.
- Overview drag-and-drop ordering (Chris): press-and-drag a company bar
  or a machine's name to reorder; an accent line shows where it will
  land; machines stay within their company. Order remembered per user
  and used both for display and for the load sequence, so the table
  fills top to bottom. Only the ▲/▼ arrow toggles a company's collapse
  now - the rest of the bar is the drag handle. Clicking a machine name
  opens it in the viewer.
- Overview machine filter + progressive loading (Chris): funnel button
  opens a per-customer tick list (shared `ui/system_filter.py`);
  unticked systems are neither scanned nor fetched; selection remembered
  per user. Full loads emit the rows immediately and fill each system in
  as its clips and events land ("Loading..." status until then);
  in-session incremental refreshes keep the quiet fleet-wide tail.
- App-wide zoom (Chris): ⋯ menu Zoom in / out / reset, Ctrl+= / Ctrl+- /
  Ctrl+0, 60–200% in 10% steps on top of the 30% base scale; scales the
  application font (text + buttons) live; remembered per user; overview
  row geometry follows.
- Data window (Chris): top-bar Data button. Intro text, two headline
  tiles (Elastic total since oldest record; CCTV total on the share,
  estimated from the retained day folders), Elastic documents / Elastic
  size / CCTV clips / CCTV size metrics, stacked per-day bars for the
  last 14 days in pastel colours (filter, toggle, key and chart framed
  in a "14 day summary" box), hover details for the shown source
  only, funnel filter grouped by customer (remembered), click a CCTV bar
  to open that day's folder in Explorer, resizable/maximisable window.
  Elastic sizes: real index store size when the key may read it, else
  per-system sampled document sizes.
- Overview day/range filter (Chris): Live vs "Choose days…" dialog with
  From/To calendars, quick presets (Last 7 days / month / 3 months /
  year), span highlighting, selected-date readouts, day total, no future
  dates. Historic mode loads once (immutable), summaries cut at range
  end, no now/updated markers; ranges over 14 days skip clip listings;
  event chunks ≤ 1 day; day/week/4-week ticks with dd/mm labels.
- Overview presentation (Chris): "Zoom" label for 1h/5h/All Day; All Day
  spans from the first data minus 30 min; blue last-update line with a
  sticky "updated HH:MM:SS" label; sticky column headers; now-clock on
  an opaque patch; customer bars in accent blue, name-then-arrow
  centred, no logos; machines indented; ▲/▼ collapse arrows with hover
  highlight; collapse state remembered per user (shared with the date
  picker); name column sized to the widest label; loading narrated in
  stages with ETAs in the bottom activity bar.
- Overview is the default screen (Chris); switcher order Overview |
  Viewer | Fleetwide; session resume returns to the screen the session
  was saved on.
- Overview event cache (Chris): today's raw events persisted per robot
  under LOCALAPPDATA; a fresh session fetches only the tail; switching
  into Overview no longer refetches the whole day.
- Type ~30% larger app-wide (Chris); calibration transport 50% larger
  with white play/pause icons, frame-count summary removed.
- Maximised window state restored reliably (Chris): geometry captured on
  the periodic session save too, `showMaximized()` at startup.
- Desktop shortcut renames itself to "Logfather (v0.NNN)" at each launch.

### 2026-09-04

- UI redesign Stage A (Chris): global dark theme (Fusion + palette +
  base stylesheet), darker background ramp, all legacy colours merged
  onto canonical tokens, checked styles on the accent family.
- UI redesign Stage B (Chris): mode-contextual top bar (Viewer/Overview/
  Fleetwide switcher; Calibrate/Track/Targets only in viewer mode with a
  clip loaded; About behind ⋯); Settings/Systems/Readme behind a gear
  dialog; playback bar reduced to Play/Sync/Overlays/Stop Report/Fit/
  Refresh with Sync and Overlays strips; calc LCD in the Sync strip;
  system label hidden outside viewer mode.

### 2026-09-03

- Calibration window transport controls (Chris): −10/−1/+1/+10 frame
  steps, scrub slider driving and following the viewer, live playhead
  timestamp; live frame feed connects even when the dialog opens before
  the clip loads.
- Session resume (Chris): remembers system/day/playhead and always asks
  at startup; the always/never option was removed the same day.
- Reverse-scrub handling in calibration: end point earlier than start is
  swapped into time-forward order (`resolve_tracking_line`).
