from __future__ import annotations

import json
import base64
import os
import shutil
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, List
import re


@dataclass
class Condition:
    name: str = ""
    query: str = ""
    color: str = ""  # hex string, assigned from defaults


@dataclass
class CustomFilterPreset:
    name: str = ""
    filter_in: str = ""
    filter_out: str = ""
    enabled: bool = False


@dataclass
class FilterPreset:
    name: str = ""
    sources: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass
class SystemLayoutEntry:
    system_name: str = ""
    customer: str = ""
    production_line: str = ""


@dataclass
class FleetwideSearchDefinition:
    name: str = ""
    query: str = ""


DEFAULT_SETTINGS_PATH = Path.home() / ".cctv_picker_settings.json"
GRAFANA_URL_DEFAULT = "https://leapmonitoring.grafana.net"
SHAREABLE_EXPORT_FORMAT = "logfather-settings"
SHAREABLE_EXPORT_VERSION = 1
SHAREABLE_FIELDS = (
    "elastic_url",
    "elastic_index",
    "elastic_timestamp_field",
    "grafana_url",
    "auto_ocr_sync",
    "auto_ocr_open_on_missing",
    "log_panel_pinned",
    "conditions",
    "custom_filters",
    "filter_presets",
    "customers",
    "customer_start_collapsed",
    "system_layouts",
    "fleetwide_searches",
)
DEFAULT_COLORS = [
    "#52c41a",  # Start (green)
    "#fa8c16",  # Caution (orange)
    "#ff4d4f",  # EStop (red)
    "#1890ff",
    "#8c8c8c",
    "#a0a0ff",
    "#c0c0c0",
    "#8080c0",
    "#66cccc",
    "#c2f0c2",
    "#d46bff",
    "#ff85c0",
    "#36cfc9",
    "#ffd666",
    "#597ef7",
]
DEFAULT_COND_PRESETS = [
    ("Start", 'state_name:"start_pnp"'),
    ("Caution", 'state_name:"caution_led_on"'),
    ("EStop", 'state_name:"hardware_emergency_stop"'),
    ("Cond 4", ""),
    ("Cond 5", ""),
    ("Cond 6", ""),
    ("Cond 7", ""),
    ("Cond 8", ""),
    ("Cond 9", ""),
    ("Cond 10", ""),
    ("Cond 11", ""),
    ("Cond 12", ""),
    # Motor faults from the actuator controller (Chris, 2026-09-10): the
    # over-current trip at 07:25 on PikPak 007 was invisible on the timeline.
    # The over-current trip first, then the other controller trips (stop /
    # enable / queue failures, rejected PVT points) (Chris, 2026-09-11).
    ("Motor overcurrent", '"Current over limit"'),
    ("Motor other fault", '"Fault on motor" AND NOT "Current over limit"'),
    ("Cond 15", ""),
]


# Preset queries that were changed after shipping: a slot still holding the
# old text is moved to the new one on load (a slot the user edited is left
# alone).
PRESET_QUERY_UPGRADES = {
    '"Fault on motor"': '"Fault on motor" AND NOT "Current over limit"',
}
# Preset names changed after shipping: a slot holding the old name with
# the preset's own query is renamed on load.
PRESET_NAME_UPGRADES = {
    "Motor fault": "Motor other fault",
}
MOTOR_OVERCURRENT_QUERY = '"Current over limit"'
MOTOR_OTHER_FAULT_QUERY = '"Fault on motor" AND NOT "Current over limit"'


def _default_conditions() -> List[Condition]:
    conds: List[Condition] = []
    for idx, (name, query) in enumerate(DEFAULT_COND_PRESETS):
        color = DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
        conds.append(Condition(name=name, query=query, color=color))
    return conds


def _default_custom_filters() -> List[CustomFilterPreset]:
    return [CustomFilterPreset(name=f"Preset {i}") for i in range(1, 6)]


def _default_filter_presets() -> List[FilterPreset]:
    return [FilterPreset(name=f"Preset {i}") for i in range(1, 16)]


def _default_fleetwide_searches() -> List[FleetwideSearchDefinition]:
    return [
        FleetwideSearchDefinition(
            name="Error reset / no error",
            query='update_info.keyword:"Error Reset or No Error :: N/A"',
        )
    ]


@dataclass
class Settings:
    last_parent: Optional[str] = None
    elastic_api_key: Optional[str] = None
    elastic_url: Optional[str] = None
    elastic_index: Optional[str] = None
    elastic_timestamp_field: Optional[str] = None
    # Grafana Cloud stack and a service-account token (Viewer role). The
    # token is a secret like the Elastic key: stored in the home settings
    # file, never exported in the shareable settings.
    grafana_url: Optional[str] = None
    grafana_token: Optional[str] = None
    auto_ocr_sync: bool = True
    auto_ocr_open_on_missing: bool = False
    conditions: List[Condition] = field(default_factory=_default_conditions)
    custom_filters: List[CustomFilterPreset] = field(default_factory=_default_custom_filters)
    filter_presets: List[FilterPreset] = field(default_factory=_default_filter_presets)
    log_panel_pinned: bool = False
    customers: List[str] = field(default_factory=list)
    customer_logos: dict[str, str] = field(default_factory=dict)
    customer_start_collapsed: dict[str, bool] = field(default_factory=dict)
    system_layouts: List[SystemLayoutEntry] = field(default_factory=list)
    fleetwide_searches: List[FleetwideSearchDefinition] = field(default_factory=_default_fleetwide_searches)
    # Session resume: what was open at last shutdown. Startup always asks
    # before restoring (no remembered always/never — removed 2026-09-03).
    last_session: Optional[dict] = None
    # Main-window geometry at last close: {x, y, w, h, maximized}. Restored
    # on startup, clamped to a live screen (a saved rect from a now-absent
    # monitor must not strand the window off-screen).
    window_geometry: Optional[dict] = None
    # Set by load() when the settings file was unreadable; never persisted.
    # The UI shows it once at startup so a recovery is not silent.
    load_warning: Optional[str] = None

    @classmethod
    def load(cls, path: Path = DEFAULT_SETTINGS_PATH) -> "Settings":
        if not path.exists():
            return cls(
                elastic_url="https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243",
                elastic_api_key=None,
                elastic_index=None,
                elastic_timestamp_field="@timestamp_ros",
                grafana_url=GRAFANA_URL_DEFAULT,
                conditions=_default_conditions(),
            )
        try:
            data = json.loads(path.read_text())
            return cls._from_dict(data)
        except Exception as exc:
            # A corrupt settings file must NOT silently become a factory
            # reset (losing the API key, customers, and all conditions).
            # Preserve the evidence, then try the backup save() keeps.
            print(f"[settings] FAILED to load {path}: {exc}", flush=True)
            warning = f"Settings file could not be read ({exc})."
            try:
                quarantine = path.with_name(
                    path.name + f".corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                )
                shutil.copy2(path, quarantine)
                warning += f"\nThe unreadable file was kept as {quarantine.name}."
                print(f"[settings] corrupt file preserved as {quarantine}", flush=True)
            except Exception:
                pass
            backup = path.with_name(path.name + ".bak")
            if backup.exists():
                try:
                    settings = cls._from_dict(json.loads(backup.read_text()))
                    warning += "\nSettings were restored from the last backup."
                    settings.load_warning = warning
                    print(f"[settings] restored from backup {backup}", flush=True)
                    return settings
                except Exception as backup_exc:
                    print(f"[settings] backup also unreadable: {backup_exc}", flush=True)
            settings = cls(
                elastic_url="https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243",
                elastic_api_key=None,
                elastic_index=None,
                elastic_timestamp_field="@timestamp_ros",
                grafana_url=GRAFANA_URL_DEFAULT,
                conditions=_default_conditions(),
            )
            settings.load_warning = (
                warning + "\nNo usable backup was found; defaults are in use."
            )
            return settings

    @classmethod
    def _from_dict(cls, data: dict) -> "Settings":
        try:
            # Rehydrate conditions
            conds = []
            for idx, c in enumerate(data.get("conditions", [])):
                color = c.get("color") or DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
                conds.append(Condition(name=c.get("name", ""), query=c.get("query", ""), color=color))
            # Ensure we always have target slots
            target_len = len(DEFAULT_COND_PRESETS)
            while len(conds) < target_len:
                idx = len(conds)
                color = DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
                conds.append(Condition(color=color))
            # Backfill missing preset queries/names
            for idx, (name, query) in enumerate(DEFAULT_COND_PRESETS):
                if idx >= len(conds):
                    break
                if not conds[idx].query and query:
                    conds[idx].query = query
                elif conds[idx].query in PRESET_QUERY_UPGRADES:
                    conds[idx].query = PRESET_QUERY_UPGRADES[conds[idx].query]
                if name and (not conds[idx].name or conds[idx].name == f"Cond {idx + 1}"):
                    conds[idx].name = name
            # Motor overcurrent moved ahead of the other motor faults
            # (Chris, 2026-09-11): swap a saved pair still in the old order,
            # and rename the old "Motor fault" slot.
            if (len(conds) > 13 and conds[12].query == MOTOR_OTHER_FAULT_QUERY and conds[13].query == MOTOR_OVERCURRENT_QUERY):
                conds[12], conds[13] = conds[13], conds[12]
            for cond in conds:
                new_name = PRESET_NAME_UPGRADES.get(cond.name)
                if new_name and cond.query == MOTOR_OTHER_FAULT_QUERY:
                    cond.name = new_name
                lower = cond.name.lower()
                if lower == "motor overcurrent":
                    cond.color = "#ff7a45"
                elif lower == "motor other fault":
                    cond.color = "#ffd666"
                # Enforce key colors: Start green, Caution orange, EStop red.
                lower = conds[idx].name.lower()
                if lower == "start":
                    conds[idx].color = "#52c41a"
                elif lower == "caution":
                    conds[idx].color = "#fa8c16"
                elif lower == "estop":
                    conds[idx].color = "#ff4d4f"
                elif lower == "motor overcurrent":
                    conds[idx].color = "#ff7a45"
                elif lower in ("motor fault", "motor other fault"):
                    conds[idx].color = "#ffd666"
                elif not conds[idx].color:
                    conds[idx].color = DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
            # If loaded conditions are all blank, seed defaults.
            if all(not c.name and not c.query for c in conds):
                conds = _default_conditions()
            data["conditions"] = conds
            custom_filters = []
            for idx, entry in enumerate(data.get("custom_filters", [])):
                custom_filters.append(
                    CustomFilterPreset(
                        name=entry.get("name", f"Preset {idx + 1}"),
                        filter_in=entry.get("filter_in", ""),
                        filter_out=entry.get("filter_out", ""),
                        enabled=False,
                    )
                )
            while len(custom_filters) < 5:
                custom_filters.append(CustomFilterPreset(name=f"Preset {len(custom_filters) + 1}"))
            filter_presets = []
            for idx, entry in enumerate(data.get("filter_presets", [])):
                filter_presets.append(
                    FilterPreset(
                        name=entry.get("name", f"Preset {idx + 1}"),
                        sources=list(entry.get("sources", [])),
                        states=list(entry.get("states", [])),
                        messages=list(entry.get("messages", [])),
                    )
                )
            while len(filter_presets) < 15:
                filter_presets.append(FilterPreset(name=f"Preset {len(filter_presets) + 1}"))
            customers = []
            for raw in data.get("customers", []):
                value = str(raw or "").strip()
                if value and value not in customers:
                    customers.append(value)
            customer_logos = {}
            for key, value in (data.get("customer_logos", {}) or {}).items():
                name = str(key or "").strip()
                logo = str(value or "").strip()
                if name and logo:
                    customer_logos[name] = logo
            customer_start_collapsed = {}
            for key, value in (data.get("customer_start_collapsed", {}) or {}).items():
                name = str(key or "").strip()
                if name:
                    customer_start_collapsed[name] = bool(value)
            system_layouts = []
            for entry in data.get("system_layouts", []):
                if not isinstance(entry, dict):
                    continue
                system_name = str(entry.get("system_name", "")).strip()
                if not system_name:
                    continue
                system_layouts.append(
                    SystemLayoutEntry(
                        system_name=system_name,
                        customer=str(entry.get("customer", "")).strip(),
                        production_line=str(entry.get("production_line", "")).strip(),
                    )
                )
            fleetwide_searches = []
            for entry in data.get("fleetwide_searches", []):
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", "")).strip()
                query = str(entry.get("query", "")).strip()
                if name and query:
                    fleetwide_searches.append(FleetwideSearchDefinition(name=name, query=query))
            # Migrate the initial placeholder shipped with the first dashboard
            # version wherever it appears. Other searches are preserved.
            replacement = _default_fleetwide_searches()[0]
            fleetwide_searches = [
                FleetwideSearchDefinition(name=replacement.name, query=replacement.query)
                if search.name == "Enabling failed" and search.query == "enabling_failed"
                else search
                for search in fleetwide_searches
            ]
            if not fleetwide_searches:
                fleetwide_searches = _default_fleetwide_searches()
            return cls(
                last_parent=data.get("last_parent"),
                elastic_api_key=data.get("elastic_api_key") or None,
                elastic_url=data.get("elastic_url") or "https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243",
                elastic_index=data.get("elastic_index"),
                elastic_timestamp_field=data.get("elastic_timestamp_field") or "@timestamp_ros",
                grafana_url=data.get("grafana_url") or GRAFANA_URL_DEFAULT,
                grafana_token=data.get("grafana_token") or None,
                auto_ocr_sync=bool(data.get("auto_ocr_sync", True)),
                auto_ocr_open_on_missing=bool(data.get("auto_ocr_open_on_missing", False)),
                log_panel_pinned=bool(data.get("log_panel_pinned", False)),
                conditions=conds[:target_len],
                custom_filters=custom_filters[:5],
                filter_presets=filter_presets[:15],
                customers=customers,
                customer_logos=customer_logos,
                customer_start_collapsed=customer_start_collapsed,
                system_layouts=system_layouts,
                fleetwide_searches=fleetwide_searches,
                last_session=data.get("last_session") if isinstance(data.get("last_session"), dict) else None,
                window_geometry=data.get("window_geometry") if isinstance(data.get("window_geometry"), dict) else None,
            )
        except Exception:
            # Parse errors propagate: load() decides how to recover
            # (quarantine + backup), import_shareable() refuses the file.
            raise

    def save(self, path: Path = DEFAULT_SETTINGS_PATH) -> None:
        try:
            payload = asdict(self)
            payload["conditions"] = [asdict(c) for c in self.conditions][: len(DEFAULT_COND_PRESETS)]
            payload["custom_filters"] = [asdict(c) for c in self.custom_filters][:5]
            payload["filter_presets"] = [asdict(c) for c in self.filter_presets][:15]
            payload["customers"] = [str(name).strip() for name in self.customers if str(name).strip()]
            payload["customer_logos"] = {
                str(name).strip(): str(value).strip()
                for name, value in (self.customer_logos or {}).items()
                if str(name).strip() and str(value).strip()
            }
            payload["customer_start_collapsed"] = {
                str(name).strip(): bool(value)
                for name, value in (self.customer_start_collapsed or {}).items()
                if str(name).strip()
            }
            payload["system_layouts"] = [asdict(c) for c in self.system_layouts if c.system_name.strip()]
            payload["fleetwide_searches"] = [
                asdict(search)
                for search in self.fleetwide_searches
                if search.name.strip() and search.query.strip()
            ]
            payload.pop("load_warning", None)
            text = json.dumps(payload, indent=2)
            # Atomic write: a crash mid-save must not leave a truncated file
            # (which load() would then have to quarantine). Keep the previous
            # good file as .bak for load()'s recovery path.
            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_text(text)
            if path.exists():
                try:
                    shutil.copy2(path, path.with_name(path.name + ".bak"))
                except Exception:
                    pass
            os.replace(tmp_path, path)
        except Exception as exc:
            print(f"[settings] FAILED to save {path}: {exc}", flush=True)

    def export_shareable(self, path: Path) -> None:
        payload = {
            "_format": SHAREABLE_EXPORT_FORMAT,
            "_version": SHAREABLE_EXPORT_VERSION,
        }
        for name in SHAREABLE_FIELDS:
            value = getattr(self, name, None)
            if isinstance(value, list) and value and hasattr(value[0], "__dataclass_fields__"):
                payload[name] = [asdict(item) for item in value]
            else:
                payload[name] = value
        path.write_text(json.dumps(payload, indent=2))

    def import_shareable(self, path: Path) -> None:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("_format") != SHAREABLE_EXPORT_FORMAT:
            raise ValueError("File is not a Logfather settings export.")
        # Parse with all defaulting / backfilling logic, then copy only the
        # fields that are actually present in the import file so locally
        # stored secrets and per-user paths are left untouched. A malformed
        # export must raise, never fall back to defaults (which would wipe
        # the live settings with factory values).
        try:
            parsed = Settings._from_dict(data)
        except Exception as exc:
            raise ValueError(f"Settings export could not be parsed: {exc}") from exc
        for name in SHAREABLE_FIELDS:
            if name in data:
                setattr(self, name, getattr(parsed, name))


def get_system_layout(settings: Settings, system_name: str) -> SystemLayoutEntry:
    target = str(system_name or "").strip().lower()
    for entry in settings.system_layouts:
        if entry.system_name.strip().lower() == target:
            return entry
    return SystemLayoutEntry(system_name=str(system_name or "").strip())


def customer_sort_key(settings: Settings, customer: str) -> tuple[int, str]:
    value = str(customer or "").strip()
    if not value:
        return (1, "zzz")
    return (0, value.lower())


def production_line_sort_key(line: str) -> tuple[int, object, str]:
    value = str(line or "").strip()
    if not value:
        return (1, "zzz", "")
    match = re.search(r"(\d+)", value)
    if match:
        return (0, int(match.group(1)), value.lower())
    return (0, value.lower(), value.lower())


def system_group_sort_key(settings: Settings, system_name: str) -> tuple:
    layout = get_system_layout(settings, system_name)
    return (
        customer_sort_key(settings, layout.customer),
        production_line_sort_key(layout.production_line),
        str(system_name or "").lower(),
    )


def display_customer_name(settings: Settings, system_name: str) -> str:
    layout = get_system_layout(settings, system_name)
    return layout.customer.strip() or "Unassigned"


def display_line_name(settings: Settings, system_name: str) -> str:
    layout = get_system_layout(settings, system_name)
    return layout.production_line.strip()


def customer_logo_bytes(settings: Settings, customer: str) -> bytes | None:
    key = str(customer or "").strip()
    if not key:
        return None
    logo = (settings.customer_logos or {}).get(key, "")
    if not logo:
        return None
    try:
        return base64.b64decode(logo)
    except Exception:
        return None


def customer_starts_collapsed(settings: Settings, customer: str) -> bool:
    key = str(customer or "").strip()
    if not key:
        return False
    return bool((settings.customer_start_collapsed or {}).get(key, False))
