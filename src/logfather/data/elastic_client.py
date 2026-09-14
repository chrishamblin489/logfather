"""Shared Elastic HTTP plumbing: per-thread session, endpoint URL, headers,
and the one search_after pagination loop with its retry ladder.

Consolidation from docs/CODE_REVIEW_2026-09.md §1: every Elastic caller
paginates through `paginate()`; none hand-roll search_after/retry/truncation.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from logfather.core.log import log
from logfather.data.elastic_errors import ElasticFetchError

_thread_local = threading.local()


def get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is not None:
        return session
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    _thread_local.session = session
    return session


def search_url(base: str, index_id: str) -> str:
    base = (base or "").rstrip("/")
    # If given a Kibana URL, convert to the ES endpoint instead of the proxy
    # (the proxy is often disabled).
    if "kb." in base:
        base = base.replace(".kb.", ".es.")
    return (
        f"{base}/{index_id}/_search"
        "?ignore_unavailable=true&allow_no_indices=true&request_cache=true"
    )


def msearch_url(base: str, index_id: str) -> str:
    base = (base or "").rstrip("/")
    if "kb." in base:
        base = base.replace(".kb.", ".es.")
    return f"{base}/{index_id}/_msearch"


def msearch_first_pages(
    bodies: list[dict],
    *,
    session: requests.Session,
    endpoint: str,
    headers: dict,
    timeout_sec: float,
    label: str = "msearch",
) -> list[list[dict] | None] | None:
    """POST one _msearch carrying several queries (their first page each).

    Returns one entry per body: that query's hit dicts, or None when the
    item errored server-side. Returns None outright on a transport/shape
    failure. Never raises — this is a fast path; the caller falls back to
    per-query paginate() for anything unanswered.
    """
    if not bodies:
        return []
    lines: list[str] = []
    for body in bodies:
        # Per-item header mirrors the _search URL params paginate() uses.
        lines.append(
            json.dumps(
                {
                    "ignore_unavailable": True,
                    "allow_no_indices": True,
                    "request_cache": True,
                }
            )
        )
        lines.append(json.dumps(body))
    payload = "\n".join(lines) + "\n"
    msearch_headers = dict(headers)
    msearch_headers["Content-Type"] = "application/x-ndjson"
    try:
        resp = session.post(
            endpoint,
            data=payload.encode("utf-8"),
            headers=msearch_headers,
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        responses = resp.json().get("responses")
    except Exception as exc:
        log("elastic", f"{label} failed; falling back to per-query requests: {exc}")
        return None
    if not isinstance(responses, list) or len(responses) != len(bodies):
        log("elastic", f"{label} returned unexpected shape; falling back")
        return None
    results: list[list[dict] | None] = []
    for item in responses:
        if not isinstance(item, dict) or item.get("error") or "hits" not in item:
            results.append(None)
            continue
        hits = item.get("hits", {}).get("hits", [])
        results.append([hit for hit in hits if isinstance(hit, dict)])
    return results


def api_headers(api_key: str) -> dict:
    """The auth/content headers every Elastic request sends. Previously
    spelled out inline at seven call sites."""
    return {
        "Content-Type": "application/json",
        "kbn-xsrf": "true",
        "Authorization": f"ApiKey {api_key}",
    }


@dataclass
class PageOutcome:
    """What paginate() collected and how the loop ended."""

    hits: list[dict] = field(default_factory=list)
    pages: int = 0
    requests_made: int = 0
    # True when the loop stopped at max_pages/max_hits while pages were still
    # coming back full — the result set may be incomplete.
    truncated: bool = False
    # Set instead of raising when on_error="warn" and a request ultimately
    # failed; hits then hold whatever was collected before the failure.
    warning: str | None = None


def paginate(
    build_body: Callable[[int, list | None], dict],
    *,
    session: requests.Session,
    endpoint: str,
    headers: dict,
    page_size: int,
    max_pages: int,
    timeout_sec: float,
    min_page_size: int | None = None,
    max_hits: int | None = None,
    label: str = "query",
    on_error: str = "raise",
) -> PageOutcome:
    """Run one search_after loop; the caller supplies only the query.

    `build_body(size, search_after)` must return a request body with that
    page size and cursor (search_after=None for the first page).

    One retry ladder for every caller: on timeout, halve the page size down
    to `min_page_size` while bumping the timeout, then up to two flat
    retries, then give up. Other request errors give up immediately with
    the response text attached. Giving up either raises ElasticFetchError
    with the partial hits attached (on_error="raise") or records
    `warning` on the outcome and returns the partial hits (on_error="warn").
    """
    if on_error not in ("raise", "warn"):
        raise ValueError(f"on_error must be 'raise' or 'warn', not {on_error!r}")
    if min_page_size is None:
        min_page_size = page_size
    min_page_size = max(1, min(min_page_size, page_size))

    outcome = PageOutcome()
    search_after: list | None = None
    timeout_retries = 0

    def _give_up(message: str, cause: Exception | None) -> PageOutcome:
        log("elastic", message)
        message = f"[elastic] {message}"  # the exception/warning text keeps the tag
        if on_error == "raise":
            raise ElasticFetchError(message, outcome.hits) from cause
        outcome.warning = message
        return outcome

    while outcome.pages < max_pages:
        size = page_size
        if max_hits is not None:
            size = min(size, max_hits - len(outcome.hits))
            if size <= 0:
                outcome.truncated = True
                return outcome
        body = build_body(size, search_after)
        try:
            outcome.requests_made += 1
            resp = session.post(endpoint, json=body, headers=headers, timeout=timeout_sec)
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            timeout_retries = 0
        except requests.exceptions.Timeout as exc:
            if page_size > min_page_size:
                page_size = max(min_page_size, page_size // 2)
                timeout_sec = min(30, timeout_sec + 3)
                log(
                    "elastic",
                    f"{label} timed out on page {outcome.pages}; "
                    f"reducing page size to {page_size} (timeout {timeout_sec}s) and retrying...",
                )
                continue
            if timeout_retries < 2:
                timeout_retries += 1
                timeout_sec = min(30, timeout_sec + 3)
                log(
                    "elastic",
                    f"{label} timed out on page {outcome.pages} "
                    f"(page size {page_size}); retry {timeout_retries}/2...",
                )
                continue
            return _give_up(
                f"{label} giving up after repeated timeouts "
                f"(page {outcome.pages}, size {page_size})",
                exc,
            )
        except Exception as exc:
            err_text = ""
            if isinstance(exc, requests.RequestException) and exc.response is not None:
                try:
                    err_text = exc.response.text
                except Exception:
                    err_text = ""
            return _give_up(
                f"{label} failed on page {outcome.pages}: {exc} {err_text}".rstrip(),
                exc,
            )

        if not hits:
            return outcome
        outcome.hits.extend(hit for hit in hits if isinstance(hit, dict))
        outcome.pages += 1
        if len(hits) < size:
            return outcome
        last_sort = hits[-1].get("sort")
        if not last_sort:
            return outcome
        search_after = last_sort

    # Ran out of pages with a full last page: results may be truncated.
    outcome.truncated = True
    return outcome
