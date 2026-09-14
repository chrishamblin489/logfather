"""Grafana HTTP client: dashboards, data sources, and queries proxied through
Grafana's own /api/ds/query, so the app never needs to know what sits behind
a dashboard (Prometheus, InfluxDB, SQL...).

Auth is a service-account token (Viewer role is enough). It comes from the
app settings, or from the LOGFATHER_GRAFANA_TOKEN environment variable for
scripts. Never commit a token.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests

from logfather.core.grafana import (
    DatasourceRef,
    Series,
    frames_to_series,
    grafana_base,
    query_body,
    unwrap_dashboard,
)
from logfather.data.elastic_client import get_thread_session
from logfather.data.settings_store import GRAFANA_URL_DEFAULT, Settings

TOKEN_ENV = "LOGFATHER_GRAFANA_TOKEN"
# Where the robot telemetry lives (found from the Actuators issues dashboard,
# 2026-09-07): Grafana Cloud's Prometheus. Metrics: sensors_cpu_temperature,
# sensors_rcu_temperature, sensors_gpu_temperature,
# sensors_brake_resistor_temperature (label leap_robot_id on Argus 1,
# system_id on Argus 2), actuators_motor_temperature and
# actuators_motor_current (labels system_id, motor_id).
TELEMETRY_DATASOURCE = DatasourceRef(uid="grafanacloud-prom", type="prometheus")
TELEMETRY_DATASOURCE_NAME = "grafanacloud-leapmonitoring-prom"


class GrafanaError(Exception):
    """Transport or HTTP failure talking to Grafana; message is user-readable."""


def base_url(settings: Settings) -> str:
    return grafana_base(settings.grafana_url or "") or GRAFANA_URL_DEFAULT


def token(settings: Settings) -> str:
    return (settings.grafana_token or os.environ.get(TOKEN_ENV) or "").strip()


def is_configured(settings: Settings) -> bool:
    return bool(base_url(settings)) and bool(token(settings))


def _headers(settings: Settings) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    tok = token(settings)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _request(settings: Settings, method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None, timeout: float = 20.0) -> Any:
    url = base_url(settings).rstrip("/") + "/" + path.lstrip("/")
    session = get_thread_session()
    try:
        resp = session.request(method, url, params=params, json=body, headers=_headers(settings), timeout=timeout)
    except requests.RequestException as exc:
        raise GrafanaError(f"Grafana request failed: {exc}") from exc
    if resp.status_code == 401:
        raise GrafanaError("Grafana rejected the token (401). Check the Grafana token in Settings.")
    if resp.status_code == 403:
        raise GrafanaError("Grafana refused access (403). The service account needs the Viewer role.")
    if resp.status_code >= 400:
        raise GrafanaError(f"Grafana HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GrafanaError("Grafana returned something that is not JSON") from exc


def health(settings: Settings) -> dict:
    """No auth needed; proves the URL is a Grafana."""
    return _request(settings, "GET", "/api/health")


def org(settings: Settings) -> dict:
    """Needs auth; proves the token works. Returns {id, name}."""
    return _request(settings, "GET", "/api/org")


def list_datasources(settings: Settings) -> list[dict]:
    data = _request(settings, "GET", "/api/datasources")
    return data if isinstance(data, list) else []


def get_dashboard(settings: Settings, uid: str) -> dict:
    """The dashboard model itself (panels, templating...), unwrapped."""
    return unwrap_dashboard(_request(settings, "GET", f"/api/dashboards/uid/{uid}"))


def query(
    settings: Settings,
    datasource: DatasourceRef,
    text: str,
    from_ms: int,
    to_ms: int,
    *,
    interval_ms: Optional[int] = None,
    max_points: int = 2000,
    extra: Optional[dict] = None,
    timeout: float = 60.0,
) -> dict:
    """Run one query through Grafana's data source proxy. Returns the raw
    DataFrame-JSON response; see query_series() for the flattened form."""
    q = query_body(datasource, text, "A", extra)
    q["maxDataPoints"] = max_points
    if interval_ms:
        q["intervalMs"] = interval_ms
    body = {"from": str(int(from_ms)), "to": str(int(to_ms)), "queries": [q]}
    return _request(settings, "POST", "/api/ds/query", body=body, timeout=timeout)


def query_series(settings: Settings, datasource: DatasourceRef, text: str, from_ms: int, to_ms: int, **kwargs) -> list[Series]:
    return frames_to_series(query(settings, datasource, text, from_ms, to_ms, **kwargs))


def telemetry_probe(settings: Settings, *, minutes: int = 30) -> str:
    """One sentence on whether the telemetry data source answers: runs a
    tiny query against the Prometheus source the dashboards use. Raises
    GrafanaError only for transport failures; access problems come back as
    text so a Test button can show them."""
    import time

    now = int(time.time() * 1000)
    try:
        series = query_series(settings, TELEMETRY_DATASOURCE, "sensors_cpu_temperature", now - minutes * 60_000, now, max_points=10)
    except GrafanaError as exc:
        text = str(exc)
        if "403" in text:
            return (
                f"telemetry source denied (403): give the service account Query permission on "
                f"{TELEMETRY_DATASOURCE_NAME} (Connections, Data sources, Permissions)."
            )
        if "404" in text or "400" in text:
            return f"telemetry source not found or query rejected: {text}"
        raise
    robots = sorted({s.labels.get("leap_robot_id") or s.labels.get("system_id") or "?" for s in series})
    if not series:
        return f"telemetry source OK but no CPU temperature samples in the last {minutes} minutes."
    return f"telemetry source OK: {len(series)} systems reporting ({', '.join(robots[:6])}{'…' if len(robots) > 6 else ''})."
