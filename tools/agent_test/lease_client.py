"""HTTP client for Agent Test coordination and organization history."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any


#: The dashboard this harness coordinates through, in precedence order.
#: ``AGENT_TEST_DASHBOARD`` is the explicit override. ``GRAPH_API`` is what
#: the session launcher exports for every session type, and it is the only
#: name that is right on all of them: a compose session reaches the dashboard
#: by service name, and nothing listens on its localhost:8080 -- three runs
#: in a row recorded "machine capacity coordinator unavailable" there on
#: 2026-09-10 before anyone exported the override. The localhost default is
#: for a host terminal, where GRAPH_API is not set.
DASHBOARD_ENV = ("AGENT_TEST_DASHBOARD", "GRAPH_API")
DEFAULT_DASHBOARD = "https://localhost:8080"


def dashboard_base() -> str:
    """The dashboard base URL, without a trailing slash."""
    for name in DASHBOARD_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_DASHBOARD


def coordinator_configured() -> bool:
    """Whether this process has any way to reach a coordinator: a session
    identity, or a dashboard address from either variable. A bare host
    process with none of these skips the organization-history calls."""
    return bool(
        os.environ.get("AUTONOMY_SESSION")
        or any(os.environ.get(name, "").strip() for name in DASHBOARD_ENV)
    )


def _headers() -> dict[str, str]:
    """JSON content type plus the session bearer, when this process holds one.

    The dashboard's Agent Test routes require an authenticated caller
    (invariant 4). An agent session carries its token in ``CROSSTALK_TOKEN``
    and authenticates by ``Authorization: Bearer <token>`` — the same header
    ``tools/graph/client.py`` sends. A host process without the token simply
    omits the bearer and authenticates as a local caller by other means.
    """
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("CROSSTALK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def lease_request(action: str, **payload: Any) -> dict[str, Any]:
    base = dashboard_base()
    data = json.dumps({"action": action, **payload}).encode()
    request = urllib.request.Request(
        f"{base}/api/agent-test/leases",
        data=data,
        headers=_headers(),
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:1000], "unavailable": True}


def telemetry_request(
    action: str = "event",
    event: str | None = None,
    **payload_fields: Any,
) -> dict[str, Any]:
    session = os.environ.get("AUTONOMY_SESSION", "").strip()
    if action in {"event", "error"} and not session:
        return {"ok": False, "unavailable": True, "error": "no session identity"}
    base = dashboard_base()
    try:
        from . import __version__
    except ImportError:
        __version__ = "unknown"
    payload: dict[str, Any] = {
        "action": action,
        "agent_test_version": __version__,
        **payload_fields,
    }
    if event is not None:
        payload.update({"event": event, "session": session})
    elif action in {"error", "progress"}:
        payload["session"] = session
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base}/api/plugins/testing/telemetry",
        data=data,
        headers=_headers(),
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:500], "unavailable": True}


def error_request(
    phase: str,
    category: str,
    message: str,
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """Record one bounded operational failure without sending a traceback."""
    return telemetry_request(
        action="error",
        phase=phase,
        category=category,
        message=" ".join(str(message).split())[:1000] or "unknown error",
        run_id=str(run_id or "")[:200],
    )


def progress_request(
    run_id: str,
    *,
    completed: int,
    total: int,
    percent: int,
    status: str = "running",
) -> dict[str, Any]:
    """Publish ephemeral per-session progress to the live dashboard stream."""
    session = os.environ.get("AUTONOMY_SESSION", "").strip()
    if not session:
        return {"ok": False, "unavailable": True, "error": "no session identity"}
    return telemetry_request(
        action="progress",
        run_id=str(run_id)[:200],
        completed=max(0, int(completed)),
        total=max(0, int(total)),
        percent=max(0, min(100, int(percent))),
        status=str(status)[:32],
    )


def duration_request(action: str, **payload: Any) -> dict[str, Any]:
    """Reach the caller's organization-local capped duration history."""
    if not coordinator_configured():
        return {"ok": False, "unavailable": True, "error": "no machine coordinator"}
    base = dashboard_base()
    data = json.dumps({"action": action, **payload}).encode()
    request = urllib.request.Request(
        f"{base}/api/plugins/testing/durations",
        data=data,
        headers=_headers(),
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:500], "unavailable": True}


def run_result_request(run_id: str, run: dict[str, Any]) -> dict[str, Any]:
    """Record one terminal run in the authenticated caller's organization."""
    if not coordinator_configured():
        return {"ok": False, "unavailable": True, "error": "no organization coordinator"}
    base = dashboard_base()
    data = json.dumps({"run_id": run_id, "run": run}).encode()
    request = urllib.request.Request(
        f"{base}/api/plugins/testing/runs",
        data=data,
        headers=_headers(),
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:500], "unavailable": True}
