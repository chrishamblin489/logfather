# The Logfather — dev notes

CCTV video + Elastic log viewer (PySide6/OpenCV), originally from a colleague.
Chris directs the work and reviews visually; Claude implements, tests, and commits.

## Run / test commands

```
.venv\Scripts\python.exe src\Main_Window.py        # run the app
.venv\Scripts\python.exe tools\smoke_test.py       # after EVERY edit: imports + offscreen window build
.venv\Scripts\python.exe -m pytest                 # unit tests
.venv\Scripts\python.exe tools\elastic_api_check.py  # live Elastic query with the app's settings
```

`LOGFATHER_DEBUG=1` (or a tag list, e.g. `LOGFATHER_DEBUG=timeline,ocr`) turns on the
`dbg()`/`timed()` debug traces from `core/log.py`; the informational `[tag]` lines
(`log()`) always print, so a captured `app_vNNN.log` stays readable by default.

## Repo layout

`src/logfather/` the app package: `core/` pure logic+models (no Qt, no network),
`data/` Elastic/caches/stores (QtCore signals OK, no GUI imports), `ui/` everything
Qt. `src/Main_Window.py` is only an entry shim (keeps the run command, PyInstaller
specs and the desktop shortcut working) — the real module is
`logfather/ui/Main_Window.py`. Imports are absolute (`from logfather.data.x import y`).
The layering rule: `ui` may import `data`/`core`; `data` may import `core`; never the
reverse. `assets/` icons/splash media/diagram · `docs/` architecture notes · `tools/`
smoke test, Elastic check, standalone scripts · `tests/` pytest · `legacy/` old
variants, don't touch. Deep architecture map: `docs/ARCHITECTURE.md` (predates the
package split; rewritten 2026-09-14 - its file table is generated from the About page by `tools/gen_architecture_table.py`). Modules are named after what the screen calls them: `replay_view.py` (PikPak Replay), `replay_timeline.py` (its chart), `data_boxes.py` (the Data boxes), `SyncCctvTimeWindow` in `time_ocr.py`. Latest review: `docs/CODE_REVIEW_2026-09-14.md`.

## Per-change loop

1. Small, single-purpose change.
2. Smoke test, then pytest. Both must pass before showing Chris.
3. Relaunch the app (background) for Chris's visual check.
4. Commit once verified and push immediately (no need to ask). When a function with pure logic is
   touched, add/extend its tests in `tests/`.

## Gotchas — learned the hard way (2026-09-02)

- **Settings live at `~/.cctv_picker_settings.json`** (home dir, leading dot).
  The `cctv_picker_settings.json` in this repo folder is the colleague's export;
  the app NEVER reads it. Back up the home file before editing it.
- **Video root**: the CCTV share is `Z:/public` on Chris's machine (IONOS HiDrive);
  the colleague maps the same share as `Y:`. Clip layout: `PikPak<NNN>/YYYY/MM/DD/*.mp4`,
  filenames carry `YYYYMMDDHHMMSS` local time.
- **Robot IDs**: folder `PikPak012` ↔ robot `35-2300-012`. Elastic docs carry the id
  in `leap_robot_id` OR `system_id` — always handle both (see `_extract_hit_robot_id`).
- **Elastic**: settings hold a Kibana URL; queries go to the ES host (`.kb.` → `.es.`),
  index pattern `logstash-*,pikpak,pikpak-*`. Never commit API keys — the standalone
  downloader reads `LOGFATHER_ELASTIC_API_KEY` from the environment.
- **No BOM in JSON**: PowerShell `Set-Content -Encoding utf8` writes a BOM that the
  app's `json.loads(path.read_text())` rejects, silently resetting all settings.
  Edit JSON with the Edit tool or `[IO.File]::WriteAllText` with `UTF8Encoding($false)`.
- **One app instance at a time**: settings autosave; a stale instance holding old
  settings can overwrite the file. Check for running instances before diagnosing
  "settings not applied" and kill stale ones hard (skips save-on-exit).
- `git push` may be blocked by the permission classifier when the session's working
  directory is elsewhere — give Chris a Run-button command with a `C:/...` path
  (works in any shell; `/c/...` only works in Git Bash).

## Repo hygiene

- `*_backup.py`, `*_old.py`, `*_no_*.py` variants are gitignored leftovers — don't
  import them, don't extend them.
- `build.ps1` builds the Windows exe/installer (only for releases, not dev).
- Tesseract OCR binary is optional; OCR features degrade gracefully without it.
- **Caches never expire for past days** and the Refresh button reads them. Any
  change to query logic or to what a cache stores (condition clauses, TimelineItem
  fields, counting rules) must bump the matching cache schema version in the same
  commit (`EVENTS_CACHE_SCHEMA_VERSION` in `elastic_loader.py`; check other
  `*_cache*` stores too), or Chris keeps seeing the old numbers (2026-09-11).
- **GitHub remote** is `https://github.com/chrishamblin489/logfather` (moved from
  chris1leap on 2026-09-11); the remote URL names the account so Git Credential
  Manager uses the chrishamblin489 login. Commits are authored as
  chris.hamblin@helloleap.ai.
