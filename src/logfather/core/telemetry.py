"""Robot telemetry as the Logfather shows it: which Prometheus metrics, how
a system is selected, and how the series are grouped into tracks. Pure
logic, no Qt, no network.

Facts learned from the Actuators issues dashboard (2026-09-07):
  sensors_cpu_temperature, sensors_rcu_temperature, sensors_gpu_temperature,
  sensors_brake_resistor_temperature: one series per system.
  actuators_motor_temperature, actuators_motor_current: one series per
  motor (motor_id 0..6; a motor that is not fitted reads a flat 0).
  Argus 1 systems carry the id in leap_robot_id, Argus 2 in system_id, and
  Argus 2 series also carry run_id / sku labels, so one motor's day arrives
  as several series that have to be stitched back together.
  Samples every 30 s.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Iterable, Optional

from logfather.core.grafana import Series


@dataclass(frozen=True)
class MetricSpec:
    key: str
    group: str
    label: str          # "{motor_id}" is filled in for per-motor metrics
    metric: str
    unit: str
    per_motor: bool = False
    # Shown in the System Replay Telemetry tab (the Overview-only extras
    # keep that tab's day load small).
    in_replay: bool = True
    # How the fleet query folds a system's series into one: max (a
    # temperature, the hottest motor) or sum (halting errors over motors).
    fleet_agg: str = "max"


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("cpu_temp", "Temperatures", "CPU", "sensors_cpu_temperature", "°C"),
    MetricSpec("rcu_temp", "Temperatures", "RCU", "sensors_rcu_temperature", "°C"),
    MetricSpec("gpu_temp", "Temperatures", "GPU", "sensors_gpu_temperature", "°C"),
    MetricSpec("brake_temp", "Temperatures", "Brake resistor", "sensors_brake_resistor_temperature", "°C"),
    MetricSpec("motor_temp", "Motor temperatures", "Motor {motor_id}", "actuators_motor_temperature", "°C", per_motor=True),
    MetricSpec("motor_current", "Motor currents", "Motor {motor_id}", "actuators_motor_current", "A", per_motor=True),
    # Supply air, in bar (readings sit around 6-8; a system with the air
    # off reads about 0 or slightly negative).
    MetricSpec("air_pressure", "Air pressure", "Air pressure", "sensors_air_pressure", " bar"),
    # Overview "Additional" box (Chris, 2026-09-08): computer health and
    # housekeeping counters. CPU and memory are percentages; the clock
    # offset is seconds; halting errors are a per-motor counter summed
    # over the motors; the log queue is lines waiting for Elastic.
    MetricSpec("ccu_cpu", "CPU load", "CCU", "diagnostics_ccu_cpu_average", "%", in_replay=False),
    MetricSpec("rcu_cpu", "CPU load", "RCU", "diagnostics_rcu_cpu_average", "%", in_replay=False),
    MetricSpec("ccu_memory", "Memory", "CCU", "diagnostics_ccu_memory", "%", in_replay=False),
    MetricSpec("rcu_memory", "Memory", "RCU", "diagnostics_rcu_memory", "%", in_replay=False),
    MetricSpec("clock_offset", "Clock offset", "RCU CCU offset (s)", "behaviour_RCU_CCU_time_offset", " s", in_replay=False),
    MetricSpec("halting_errors", "Motor halting errors", "All motors", "actuators_motor_halting_errors", "", per_motor=True, in_replay=False, fleet_agg="sum"),
    MetricSpec("log_queue", "Argus log queue", "Queued lines", "argus_logs_queue_size", "", in_replay=False),
    # CAN bus (Chris, 2026-09-08). These are NOT cumulative counters: the
    # health node reports a count per reporting window and starts again
    # (frames sit near 30k idle and 430k running, errors near a power
    # event follow the same shape), so they are drawn as reported.
    MetricSpec("canbus_errors", "CAN bus", "Errors", "behaviour_canbus_errors", "", in_replay=False),
    MetricSpec("canbus_errors_power", "CAN bus", "Errors near power event", "behaviour_canbus_errors_near_power_event", "", in_replay=False),
    MetricSpec("canbus_frames", "CAN bus", "Frames seen", "behaviour_canbus_frames_seen", "", in_replay=False),
    # Pick rate (Chris, 2026-09-08): reported by the targeting node on Argus 2
    # systems only; Argus 1 has no such metric in Grafana.
    MetricSpec("picks_per_min", "Picks", "Picks per minute", "targeting_products_picked_per_min", "/min", in_replay=False),
)
GROUP_ORDER = ("Temperatures", "Motor temperatures", "Motor currents", "Air pressure")
SAMPLE_INTERVAL_MS = 30_000


@dataclass
class Track:
    name: str
    times_ms: list[int]
    values: list[Optional[float]]
    spec_key: str = ""

    def value_at(self, t_ms: int, tolerance_ms: Optional[int] = None) -> Optional[float]:
        """The nearest sample, within three sample steps of the track's own
        spacing (a month-long track is sampled every 5 or 30 minutes, not
        every 30 s; Chris, 2026-09-11: the Picks strip was blank zoomed out)."""
        if tolerance_ms is None:
            tolerance_ms = 3 * max(SAMPLE_INTERVAL_MS, self.step_ms())
        return value_at(self.times_ms, self.values, t_ms, tolerance_ms)

    def step_ms(self) -> int:
        """The track's typical sample spacing: the median gap between
        consecutive sample times, valued or not (cached)."""
        cached = self.__dict__.get("_step")
        if cached is None:
            times = self.times_ms
            gaps = sorted(b - a for a, b in zip(times, times[1:]))
            cached = gaps[len(gaps) // 2] if gaps else SAMPLE_INTERVAL_MS
            self.__dict__["_step"] = cached
        return cached

    def gap_break_ms(self) -> int:
        """A gap this long between samples means the line breaks: five
        minutes, or two and a half sample steps for coarser tracks."""
        return max(5 * 60_000, int(2.5 * self.step_ms()))

    def dense(self) -> tuple[list[int], list[float]]:
        """(times, values) with the gaps dropped, cached: min/max over a
        slice of these runs at C speed, which the per-second Overview
        redraw needs (2026-09-08)."""
        cached = self.__dict__.get("_dense")
        if cached is None:
            times = [t for t, v in zip(self.times_ms, self.values) if v is not None]
            values = [v for v in self.values if v is not None]
            cached = (times, values)
            self.__dict__["_dense"] = cached
        return cached


@dataclass
class TrackGroup:
    name: str
    unit: str
    tracks: list[Track] = field(default_factory=list)


@dataclass
class TelemetryDay:
    robot_id: str
    day_start_ms: int
    day_end_ms: int
    groups: list[TrackGroup] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(g.tracks for g in self.groups)


def robot_query(spec: MetricSpec, robot_id: str) -> str:
    """Select one system under either label scheme."""
    m = spec.metric
    return f'{m}{{leap_robot_id="{robot_id}"}} or {m}{{system_id="{robot_id}"}}'


def value_at(times_ms: list[int], values: list[Optional[float]], t_ms: int, tolerance_ms: int) -> Optional[float]:
    """The sample nearest t, or None when the nearest is further than the
    tolerance (a gap, the robot off)."""
    if not times_ms:
        return None
    i = bisect_left(times_ms, t_ms)
    best: Optional[int] = None
    for j in (i - 1, i):
        if 0 <= j < len(times_ms) and values[j] is not None:
            if best is None or abs(times_ms[j] - t_ms) < abs(times_ms[best] - t_ms):
                best = j
    if best is None or abs(times_ms[best] - t_ms) > tolerance_ms:
        return None
    return values[best]


def merge_series(parts: Iterable[Series]) -> tuple[list[int], list[Optional[float]]]:
    """Stitch several series of the same signal (Argus 2 splits a day by
    run) into one, sorted by time, later duplicates winning."""
    points: dict[int, Optional[float]] = {}
    for s in parts:
        for t, v in zip(s.times_ms, s.values):
            if v is not None or t not in points:
                points[t] = v
    times = sorted(points)
    return times, [points[t] for t in times]


def is_absent(values: Iterable[Optional[float]]) -> bool:
    """A motor slot with nothing fitted reads a flat zero all day."""
    seen = False
    for v in values:
        if v is None:
            continue
        seen = True
        if abs(v) > 1e-9:
            return False
    return seen or True


def series_key(spec: MetricSpec, s: Series) -> str:
    if spec.per_motor:
        return s.labels.get("motor_id", "?")
    return ""


def build_groups(results: dict[str, list[Series]], specs: Iterable[MetricSpec] = METRICS) -> list[TrackGroup]:
    """results: spec.key -> the series Grafana returned for that metric."""
    groups: dict[str, TrackGroup] = {}
    for spec in specs:
        parts_by_key: dict[str, list[Series]] = {}
        for s in results.get(spec.key, []):
            parts_by_key.setdefault(series_key(spec, s), []).append(s)
        for key in sorted(parts_by_key, key=_motor_sort):
            times, values = merge_series(parts_by_key[key])
            if not times or (spec.per_motor and is_absent(values)):
                continue
            name = spec.label.format(motor_id=key) if spec.per_motor else spec.label
            group = groups.setdefault(spec.group, TrackGroup(spec.group, spec.unit))
            group.tracks.append(Track(name, times, values, spec.key))
    return [groups[g] for g in GROUP_ORDER if g in groups] + [g for n, g in groups.items() if n not in GROUP_ORDER]


def _motor_sort(key: str):
    return (0, int(key)) if key.isdigit() else (1, key)


def max_across(tracks: Iterable[Track], name: str, spec_key: str = "") -> Optional[Track]:
    """One track holding, at each sample time, the highest value across the
    given tracks (the hottest motor at that moment)."""
    best: dict[int, float] = {}
    for t in tracks:
        for time_ms, v in zip(t.times_ms, t.values):
            if v is None:
                continue
            if time_ms not in best or v > best[time_ms]:
                best[time_ms] = v
    if not best:
        return None
    times = sorted(best)
    return Track(name, times, [best[t] for t in times], spec_key)


def summary_track(data: Optional[TelemetryDay]) -> Optional[tuple[Track, str]]:
    """The one line worth a timeline row: the hottest motor through the
    day, or the CPU temperature when the system has no motor readings.
    Returns (track, unit)."""
    if data is None:
        return None
    by_name = {g.name: g for g in data.groups}
    motors = by_name.get("Motor temperatures")
    if motors and motors.tracks:
        track = max_across(motors.tracks, "Hottest motor", "motor_temp")
        if track is not None:
            return track, motors.unit
    temps = by_name.get("Temperatures")
    if temps:
        for t in temps.tracks:
            if t.spec_key == "cpu_temp":
                return Track("CPU temperature", t.times_ms, t.values, t.spec_key), temps.unit
    return None


def downsample(times_ms: list[int], values: list[Optional[float]], t0: int, t1: int, columns: int) -> list[tuple[int, float, float]]:
    """Per pixel column: (column, min, max) of the samples that fall in it.
    Painting this instead of ~2,900 points keeps a resize instant."""
    if columns <= 0 or t1 <= t0:
        return []
    span = t1 - t0
    lo: list[Optional[float]] = [None] * columns
    hi: list[Optional[float]] = [None] * columns
    for t, v in zip(times_ms, values):
        if v is None or t < t0 or t > t1:
            continue
        c = min(columns - 1, int((t - t0) * columns / span))
        if lo[c] is None or v < lo[c]:
            lo[c] = v
        if hi[c] is None or v > hi[c]:
            hi[c] = v
    return [(c, lo[c], hi[c]) for c in range(columns) if lo[c] is not None]


def value_range(tracks: Iterable[Track]) -> tuple[float, float]:
    lo, hi = None, None
    for t in tracks:
        for v in t.values:
            if v is None:
                continue
            lo = v if lo is None else min(lo, v)
            hi = v if hi is None else max(hi, v)
    if lo is None:
        return 0.0, 1.0
    if hi - lo < 1e-9:
        return lo - 1.0, hi + 1.0
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


# ---- fleet-wide temperatures for the Overview (Chris, 2026-09-07) --------
# (key, label) in menu order; "motor_temp" is the hottest fitted motor.
# Slots 0 and 4 exist in the data but read a flat zero on every system
# (checked over 30 days, 2026-09-08): not fitted, so not offered.
MOTOR_IDS = ("1", "2", "3", "5", "6")
TEMPERATURE_CHOICES: tuple[tuple[str, str], ...] = (
    ("cpu_temp", "CPU"),
    ("rcu_temp", "RCU"),
    ("gpu_temp", "GPU"),
    ("brake_temp", "Brake resistor"),
    ("motor_temp", "Hottest motor"),
) + tuple((f"motor_temp_{m}", f"Motor {m}") for m in MOTOR_IDS)
TEMPERATURE_COLOURS = {
    "cpu_temp": "#5e9bff", "rcu_temp": "#ff8a65", "gpu_temp": "#2ecc71", "brake_temp": "#f1c40f", "motor_temp": "#d46bff",
    "motor_temp_1": "#ff85c0", "motor_temp_2": "#36cfc9", "motor_temp_3": "#ffd666",
    "motor_temp_5": "#b37feb", "motor_temp_6": "#ff9c6e",
}


# Currents on the Overview (Chris, 2026-09-08): the highest motor (the
# largest magnitude at each moment) or one motor.
CURRENT_CHOICES: tuple[tuple[str, str], ...] = (
    ("motor_current", "Highest motor"),
) + tuple((f"motor_current_{m}", f"Motor {m}") for m in MOTOR_IDS)
CURRENT_COLOURS = {
    "motor_current": "#d46bff", "motor_current_1": "#ff85c0", "motor_current_2": "#36cfc9",
    "motor_current_3": "#ffd666", "motor_current_5": "#b37feb", "motor_current_6": "#ff9c6e",
}
# Air pressure on the Overview (Chris, 2026-09-08): one reading per system.
PRESSURE_CHOICES: tuple[tuple[str, str], ...] = (("air_pressure", "Air pressure"),)
PRESSURE_COLOURS = {"air_pressure": "#36cfc9"}
PICKS_CHOICES: tuple[tuple[str, str], ...] = (("picks_per_min", "Picks per minute"),)
PICKS_COLOURS = {"picks_per_min": "#95de64"}
# The Additional box: one channel (strip) per entry, each with its own
# choices, unit and colours.
ADDITIONAL_CHANNELS: tuple[dict, ...] = (
    {"name": "cpu", "title": "CPU load", "unit": "%", "axis_unit": "%", "decimals": 0, "axis_min": 0.0,
     "choices": (("ccu_cpu", "CCU"), ("rcu_cpu", "RCU")), "colours": {"ccu_cpu": "#5e9bff", "rcu_cpu": "#ff8a65"}},
    {"name": "memory", "title": "Memory", "unit": "%", "axis_unit": "%", "decimals": 0, "axis_min": 0.0, "axis_max": 100.0,
     "choices": (("ccu_memory", "CCU"), ("rcu_memory", "RCU")), "colours": {"ccu_memory": "#2ecc71", "rcu_memory": "#f1c40f"}},
    {"name": "clock", "title": "Clock offset", "unit": " s", "axis_unit": "s", "decimals": 3, "axis_min": None,
     "choices": (("clock_offset", "RCU CCU offset (s)"),), "colours": {"clock_offset": "#d46bff"}},
    {"name": "halting", "title": "Halting errors", "unit": "", "axis_unit": "", "decimals": 0, "axis_min": 0.0,
     "choices": (("halting_errors", "Motor halting errors"),), "colours": {"halting_errors": "#ff4d4f"}},
    # From Elastic, not Grafana (Chris, 2026-09-11): the controller's
    # "Current over limit" trips as a running total over the loaded span.
    {"name": "overcurrent", "title": "Motor overcurrent", "unit": "", "axis_unit": "", "decimals": 0, "axis_min": 0.0,
     "choices": (("motor_overcurrent", "Overcurrent trips (running total)"),), "colours": {"motor_overcurrent": "#ffd666"}},
    {"name": "logqueue", "title": "Log queue", "unit": "", "axis_unit": "", "decimals": 0, "axis_min": 0.0,
     "choices": (("log_queue", "Argus log queue"),), "colours": {"log_queue": "#36cfc9"}},
    {"name": "canbus", "title": "CAN bus", "unit": "", "axis_unit": "", "decimals": 0, "axis_min": 0.0,
     "choices": (("canbus_errors", "CAN bus errors"), ("canbus_errors_power", "CAN errors near power event"), ("canbus_frames", "CAN frames seen")),
     "colours": {"canbus_errors": "#ff4d4f", "canbus_errors_power": "#ff9c6e", "canbus_frames": "#95de64"}},
)
SIGNAL_LABELS: dict[str, str] = dict(TEMPERATURE_CHOICES) | dict(CURRENT_CHOICES) | dict(PRESSURE_CHOICES) | dict(PICKS_CHOICES)
for _channel in ADDITIONAL_CHANNELS:
    SIGNAL_LABELS.update(dict(_channel["choices"]))


def parse_signal_key(key: str) -> tuple[str, Optional[str]]:
    """"motor_temp_3" -> ("motor_temp", "3"), "motor_current_5" ->
    ("motor_current", "5"); anything else -> (key, None)."""
    for prefix in ("motor_temp_", "motor_current_"):
        if key.startswith(prefix):
            return prefix[:-1], key[len(prefix):]
    return key, None


parse_temperature_key = parse_signal_key
ROBOT_ID_LABELS = ("leap_robot_id", "system_id")
_ROBOT_PREFIX = "35-2300-"


def normalise_robot_id(label: str) -> str:
    """Argus 2 systems sometimes report just the three digits ("018")."""
    text = (label or "").strip()
    if text.isdigit() and len(text) == 3:
        return f"{_ROBOT_PREFIX}{text}"
    return text


def fleet_query(spec: MetricSpec, label: str, motor_id: Optional[str] = None) -> str:
    """One series per system for a metric, under one label scheme. Motors
    collapse to the hottest fitted one (an unfitted slot reads 0) unless a
    motor_id picks one motor."""
    if spec.key == "motor_current":
        # A current is legitimately 0 when idle, so no zero filter; the
        # "highest" is the largest magnitude across the fitted motors.
        if motor_id is not None:
            return f'max by({label}) ({spec.metric}{{{label}!="", motor_id="{motor_id}"}})'
        return f'max by({label}) (abs({spec.metric}{{{label}!=""}}))'
    selector = f'{spec.metric}{{{label}!=""' + (f', motor_id="{motor_id}"' if motor_id is not None else "") + "}"
    if spec.key == "motor_temp":
        selector += " != 0"  # an unfitted slot reads a flat zero
    return f'{spec.fleet_agg} by({label}) ({selector})'


def tracks_by_robot(series_list: Iterable[Series], label: str, name: str, spec_key: str = "") -> dict[str, Track]:
    """Series keyed by the robot id in `label`, merged per robot."""
    parts: dict[str, list[Series]] = {}
    for s in series_list:
        robot = normalise_robot_id(s.labels.get(label, ""))
        if robot:
            parts.setdefault(robot, []).append(s)
    out: dict[str, Track] = {}
    for robot, group in parts.items():
        times, values = merge_series(group)
        if times:
            out[robot] = Track(name, times, values, spec_key)
    return out


def window_stats(track: Track, t0_ms: int, t1_ms: int) -> Optional[tuple[float, float, float]]:
    """(min, max, latest) of the samples inside [t0, t1], or None."""
    from bisect import bisect_right

    times, values = track.dense()
    i0 = bisect_left(times, t0_ms)
    i1 = bisect_right(times, t1_ms)
    if i1 <= i0:
        return None
    window = values[i0:i1]
    return min(window), max(window), window[-1]
