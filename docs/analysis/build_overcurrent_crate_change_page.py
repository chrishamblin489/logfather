"""Build the self-contained "Overcurrent Near Crate Change" page.

How often a motor overcurrent fault and a crate change error landed within
N seconds of each other on the same PikPak, per week over the last year
(Chris, 2026-09-18, for the Stop the Stops taskforce).

    .venv\\Scripts\\python.exe docs\\analysis\\build_overcurrent_crate_change_page.py 300
    .venv\\Scripts\\python.exe docs\\analysis\\build_overcurrent_crate_change_page.py 60 --reuse

The window is in seconds (default 300). The Elastic connection comes from
the app's own settings (~/.cctv_picker_settings.json); nothing secret is
written to the page. ``--reuse`` skips the fetch and pairs the documents
saved by the last run (docs/analysis/.overcurrent_crate_change_raw.json,
which is listed in .gitignore).

Method: overcurrent is the Logfather's "Motor overcurrent" condition
("Current over limit"); crate change error is the state
crate_change_package_error. Documents from one PikPak within 2 s are one
event (the Errors / Stops rule). An overcurrent event counts as "both"
when its nearest crate change error on the same PikPak is within the
window; crate change errors are counted the same way from their side.
"""
from __future__ import annotations

import bisect
import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))
RAW = HERE / ".overcurrent_crate_change_raw.json"
TEMPLATE = HERE / "overcurrent-near-crate-change.template.html"
CLUSTER_S = 2.0


def fetch_raw() -> dict:
    from logfather.data.elastic_client import api_headers, get_thread_session, paginate, search_url
    from logfather.data.elastic_loader import _normalize_index_id, condition_clause
    from logfather.data.elastic_schema import extract_hit_robot_id
    from logfather.data.settings_store import Settings

    s = Settings.load()
    keys = {k: getattr(s, k) for k in vars(s) if "elastic" in k}
    url = keys.get("elastic_url") or keys.get("kibana_url")
    key = keys.get("elastic_api_key")
    index = _normalize_index_id(keys.get("elastic_index") or "logstash-*,pikpak,pikpak-*")
    session, endpoint, headers = get_thread_session(), search_url(url, index), api_headers(key)
    time_range = {"range": {"@timestamp": {"gte": "now-1y/d", "lte": "now"}}}

    def robot_of(hit: dict) -> str:
        try:
            rid = extract_hit_robot_id(hit)
        except Exception:
            rid = None
        if not rid:
            src = hit.get("_source", {})
            rid = src.get("system_id") or src.get("leap_robot_id")
        return str(rid or "?")

    def fetch(label: str, query: dict) -> list:
        def body(size, after):
            b = {
                "size": size,
                "sort": [{"@timestamp": {"order": "asc", "format": "strict_date_optional_time"}}],
                "_source": ["@timestamp", "state_name", "system_id", "leap_robot_id", "source", "message", "servo_id"],
                "query": {"bool": {"filter": [time_range], "must": [query]}},
            }
            if after:
                b["search_after"] = after
            return b

        res = paginate(body, session=session, endpoint=endpoint, headers=headers, page_size=2000,
                       max_pages=200, timeout_sec=90, label=label, max_hits=400000)
        print(f"{label}: {len(res.hits)} documents, truncated={res.truncated}")
        return [[h["_source"].get("@timestamp"), robot_of(h), h["_source"].get("message"), h["_source"].get("servo_id")]
                for h in res.hits]

    raw = {
        "overcurrent": fetch("overcurrent", condition_clause('"Current over limit"')),
        "crate_change": fetch("crate change error", {"terms": {"state_name.keyword": ["crate_change_package_error"]}}),
    }
    RAW.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def parse_ts(text: str) -> datetime:
    text = text.replace("Z", "+00:00")
    m = re.match(r"(.*\.\d{6})\d*(\+.*)", text)  # nanoseconds -> microseconds
    if m:
        text = m.group(1) + m.group(2)
    return datetime.fromisoformat(text)


def label_of(robot_id: str) -> str:
    m = re.match(r"^35-2300-(\d{3})$", robot_id)
    return f"PikPak{m.group(1)}" if m else "Workshop / dev / test"


def events(rows: list) -> dict:
    """Per robot, the first document of each 2-second burst."""
    per = defaultdict(list)
    for row in rows:
        per[row[1]].append((parse_ts(row[0]), row))
    out = defaultdict(list)
    for robot, lst in per.items():
        lst.sort(key=lambda x: x[0])
        last = None
        for t, row in lst:
            if last is None or (t - last).total_seconds() > CLUSTER_S:
                out[robot].append((t, row))
            last = t
    return out


def nearest(times: list, t: datetime):
    i = bisect.bisect_left(times, t)
    best = None
    for k in (i - 1, i):
        if 0 <= k < len(times):
            d = (times[k] - t).total_seconds()
            if best is None or abs(d) < abs(best[0]):
                best = (d, times[k])
    return best


def build_dataset(raw: dict, window: float) -> dict:
    oc, cc = events(raw["overcurrent"]), events(raw["crate_change"])
    now = datetime.now(timezone.utc)
    first = min(t for lst in list(oc.values()) + list(cc.values()) for t, _ in lst)
    start = (first - timedelta(days=first.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = []
    w = start
    while w <= now:
        weeks.append(w.strftime("%Y-%m-%d"))
        w += timedelta(days=7)

    def week_of(t):
        return weeks[max(0, min(len(weeks) - 1, (t - start).days // 7))]

    weekly = defaultdict(lambda: defaultdict(lambda: {"both": 0, "cc_only": 0, "oc_only": 0, "cc_near": 0}))
    details = []
    for robot, ocs in oc.items():
        cc_times = [t for t, _ in cc.get(robot, [])]
        for t, row in ocs:
            b = nearest(cc_times, t) if cc_times else None
            if b and abs(b[0]) <= window:
                motor = re.search(r"Fault on motor (\d+)", row[2] or "")
                details.append({
                    "robot": label_of(robot),
                    "overcurrent_at": t.isoformat(timespec="seconds"),
                    "crate_change_at": b[1].isoformat(timespec="seconds"),
                    "delta_s": round(b[0], 1),
                    "motor": motor.group(1) if motor else (str(row[3]) if row[3] else "?"),
                })
                weekly[label_of(robot)][week_of(t)]["both"] += 1
            else:
                weekly[label_of(robot)][week_of(t)]["oc_only"] += 1
    for robot, ccs in cc.items():
        oc_times = [t for t, _ in oc.get(robot, [])]
        for t, _row in ccs:
            b = nearest(oc_times, t) if oc_times else None
            weekly[label_of(robot)][week_of(t)]["cc_near" if b and abs(b[0]) <= window else "cc_only"] += 1
    details.sort(key=lambda d: d["overcurrent_at"])
    kinds = ("both", "cc_only", "oc_only", "cc_near")
    totals = {s: {k: sum(weekly[s][x][k] for x in weeks) for k in kinds} for s in weekly}
    systems = sorted(totals, key=lambda s: (s.startswith("Workshop"), -totals[s]["both"], s))

    def count(win: float) -> int:
        n = 0
        for robot, ocs in oc.items():
            cc_times = [t for t, _ in cc.get(robot, [])]
            for t, _ in ocs:
                b = nearest(cc_times, t) if cc_times else None
                if b and abs(b[0]) <= win:
                    n += 1
        return n

    return {
        "generated": now.isoformat(timespec="seconds"), "window_s": window, "weeks": weeks, "systems": systems,
        "weekly": {s: {x: weekly[s][x] for x in weeks} for s in systems}, "totals": totals,
        "oc_events": sum(len(v) for v in oc.values()), "cc_events": sum(len(v) for v in cc.values()),
        "details": details, "sensitivity": {str(x): count(x) for x in (10, 30, 60, 300, 900, 3600)},
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    window = float(args[0]) if args else 300.0
    raw = json.loads(RAW.read_text(encoding="utf-8")) if "--reuse" in sys.argv and RAW.exists() else fetch_raw()
    dataset = build_dataset(raw, window)
    page = io.open(TEMPLATE, encoding="utf-8").read()
    assert page.count("__DATASET__") == 1
    page = page.replace("__DATASET__", json.dumps(dataset, separators=(",", ":")))
    name = "1min" if window == 60 else ("5min" if window == 300 else f"{int(window)}s")
    out = HERE / f"overcurrent-near-crate-change-{name}.html"
    io.open(out, "w", encoding="utf-8", newline="\n").write(page)
    print(f"{out.name}: {len(dataset['details'])} overcurrent faults with a crate change error within {int(window)} s "
          f"({dataset['oc_events']} overcurrent events, {dataset['cc_events']} crate change errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
