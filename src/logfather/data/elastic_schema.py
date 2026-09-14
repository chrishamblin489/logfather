"""The ONE place that knows the Argus 1.x / 2.x Elastic log schemas.

The fleet is mid-migration between two logging stacks and their documents
differ (see docs/CODE_REVIEW_2026-09.md §2):

- identity: Argus 1.x docs carry ``leap_robot_id``; Argus 2.x docs carry
  only ``system_id`` (older mappings also expose ``system_id.raw``);
- SKU/run metadata: 1.x nests ``data_collection.user_selection`` /
  ``tray_selection`` / ``tool_selection``; 2.x uses
  ``data_collection.sku_name`` / ``sku_tray`` / ``sku_tool`` or an
  inline ``sku`` block, and some mappings flatten the nested keys.

Every query filter, field fallback, and state-name convention that copes
with that split belongs here. Nothing outside this module should spell an
Elastic field name that differs between the two schemas.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROBOT_ID_PREFIX = "35-2300-"

# Fields that can carry the robot identity, in preference order.
IDENTITY_FIELDS = ("leap_robot_id", "system_id", "system_id.raw")

# The operation-state transitions every timeline/overview/fleetwide query
# filters on. One list — three drifted copies of it once made the timeline
# and the overview board disagree about when SKU runs ended.
TRANSITION_STATES = [
    "start_pnp",
    "stop_pnp",
    "operator_stop",
    "caution_led_on",
    "hardware_emergency_stop",
    "protective_stop",
    "controller_node_manual_mode",
    "controller_node_automatic_mode",
    "system_stop",
    "emergency_stop",
]

_FOLDER_DIGITS_RE = re.compile(r"(\d{3})$")


def robot_id_from_folder(folder_name: str) -> str | None:
    """Map a system folder name to its robot id: PikPak012 -> 35-2300-012.

    The canonical rule (three trailing digits). Previously three modules
    each had their own copy of this parsing.
    """
    m = _FOLDER_DIGITS_RE.search(str(folder_name or ""))
    if not m:
        return None
    return f"{ROBOT_ID_PREFIX}{m.group(1)}"


def resolve_robot_id(pikpak_root: Path | str | None, override: str | None) -> str | None:
    """The robot id a fetch should query: the date picker's system-id
    override when one is set (the SIM Logs mode's ``35-2300-SIM``), else
    the id derived from the PikPak folder name, else None.

    Resolved ONCE, on the UI thread, at the moment a fetch is requested,
    and passed down to the fetch as an explicit ``robot_id`` argument. It
    used to be a module global in elastic_loader read by the worker
    threads, so a fetch for PikPak A could silently query robot B when the
    picker changed mid-flight (review item 10, 2026-09-14).
    """
    if override:
        return str(override)
    if pikpak_root is None:
        return None
    return robot_id_from_folder(Path(pikpak_root).name)


def identity_filter(robot_ids: list[str]) -> dict:
    """Bool filter matching any of `robot_ids` under either schema.

    Emits term/.keyword/match_phrase triples per identity field — some
    clauses are redundant on modern mappings, but unmapped-field term
    queries are harmless and this keeps every deployment matching.
    """

    def _single(robot_id: str) -> dict:
        should_terms = []
        for field in IDENTITY_FIELDS:
            should_terms.extend(
                [
                    {"term": {f"{field}.keyword": robot_id}},
                    {"term": {field: robot_id}},
                    {"match_phrase": {field: robot_id}},
                ]
            )
        return {"bool": {"should": should_terms, "minimum_should_match": 1}}

    if len(robot_ids) == 1:
        return _single(robot_ids[0])
    return {
        "bool": {
            "should": [_single(robot_id) for robot_id in robot_ids],
            "minimum_should_match": 1,
        }
    }


def extract_hit_robot_id(source_doc: dict) -> str | None:
    """Robot id of a returned document, whichever schema wrote it."""
    for key in IDENTITY_FIELDS:
        value = source_doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_ui_selection(source_doc: dict) -> dict | None:
    """SKU/tray/tool selection from a document, across every known shape:
    the ui_node json_request payload, Argus 1.x data_collection fields,
    Argus 2.x sku_name/sku block fields, and flattened-key mappings."""
    source_val = str(source_doc.get("source") or "").strip()
    allowed_sources = {"/leap/manip1/ui_node", "/leap/manip1/behaviour_node"}
    has_sku_fields = any(
        key in source_doc
        for key in (
            "data_collection",
            "data_collection.sku_name",
            "sku",
            "sku.name",
        )
    )
    if source_val and source_val not in allowed_sources and not has_sku_fields:
        return None
    params = None
    json_req = source_doc.get("json_request")
    if isinstance(json_req, dict):
        params = json_req.get("params")
    if params is None:
        params = source_doc.get("json_request.params")
    payload = {}
    if isinstance(params, str) and params:
        try:
            payload = json.loads(params)
        except Exception:
            payload = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        sku = data.get("user_selection")
        tray = data.get("tray_selection")
        tool = data.get("tool_selection")
    else:
        sku = None
        tray = None
        tool = None

    # Argus 2.0: SKU may be stored directly on the document.
    if not sku:
        sku = source_doc.get("data_collection.sku_name") or source_doc.get("sku.name")
    if not sku:
        data_collection = source_doc.get("data_collection")
        if isinstance(data_collection, dict):
            # Argus 1.x schema
            sku = sku or data_collection.get("user_selection")
            tray = tray or data_collection.get("tray_selection")
            tool = tool or data_collection.get("tool_selection")
            # Argus 2.x schema
            sku = sku or data_collection.get("sku_name")
            tray = tray or data_collection.get("sku_tray")
            tool = tool or data_collection.get("sku_tool")
        sku_block = source_doc.get("sku")
        if isinstance(sku_block, dict):
            sku = sku or sku_block.get("name")
            tray = tray or sku_block.get("tray")
            tool = tool or sku_block.get("tool")
        # Flat field fallback if mapping flattens nested keys.
        sku = sku or source_doc.get("data_collection.user_selection")
        tray = tray or source_doc.get("data_collection.tray_selection")
        tool = tool or source_doc.get("data_collection.tool_selection")

    if not sku:
        return None
    return {
        "sku": str(sku),
        "tray": str(tray) if tray else "",
        "tool": str(tool) if tool else "",
    }


def extract_service_name(source_doc: dict) -> str:
    direct = source_doc.get("json_request.service_name")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    json_request = source_doc.get("json_request")
    if isinstance(json_request, dict):
        nested = json_request.get("service_name")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def is_manual_state(state_name: str) -> bool:
    s = (state_name or "").strip().lower()
    if not s:
        return False
    return s == "controller_node_manual_mode" or ("manual" in s and "mode" in s)


def is_automatic_state(state_name: str) -> bool:
    s = (state_name or "").strip().lower()
    if not s:
        return False
    return s == "controller_node_automatic_mode" or ("automatic" in s and "mode" in s)


def is_shutdown_message(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return "shutting down system" in msg


def is_stop_like_event(state_name: str, message: str, service_name: str = "") -> bool:
    lower_state = (state_name or "").strip().lower()
    if (
        "stop" in lower_state
        or "estop" in lower_state
        or "caution" in lower_state
        or lower_state in {
            "hardware_emergency_stop",
            "protective_stop",
            "emergency_stop",
            "system_stop",
            "stop_pnp",
            "caution_led_on",
        }
    ):
        return True
    if (service_name or "").strip().lower() == "system_shutdown":
        return True
    return is_shutdown_message(message)
