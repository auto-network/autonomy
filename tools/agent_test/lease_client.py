"""HTTP client for the dashboard's machine-wide Agent Test lease broker."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any


def lease_request(action: str, **payload: Any) -> dict[str, Any]:
    base = os.environ.get("AGENT_TEST_DASHBOARD", "https://localhost:8080").rstrip("/")
    data = json.dumps({"action": action, **payload}).encode()
    request = urllib.request.Request(
        f"{base}/api/agent-test/leases",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:1000], "unavailable": True}


def telemetry_request(action: str = "event", event: str | None = None) -> dict[str, Any]:
    session = os.environ.get("AUTONOMY_SESSION", "").strip()
    if event is not None and not session:
        return {"ok": False, "unavailable": True, "error": "no session identity"}
    base = os.environ.get("AGENT_TEST_DASHBOARD", "https://localhost:8080").rstrip("/")
    payload: dict[str, Any] = {"action": action}
    if event is not None:
        payload.update({"event": event, "session": session})
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base}/api/agent-test/telemetry",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:500], "unavailable": True}


def duration_request(action: str, **payload: Any) -> dict[str, Any]:
    """Reach the machine-local capped duration-history service."""
    if not os.environ.get("AUTONOMY_SESSION") and "AGENT_TEST_DASHBOARD" not in os.environ:
        return {"ok": False, "unavailable": True, "error": "no machine coordinator"}
    base = os.environ.get("AGENT_TEST_DASHBOARD", "https://localhost:8080").rstrip("/")
    data = json.dumps({"action": action, **payload}).encode()
    request = urllib.request.Request(
        f"{base}/api/agent-test/durations",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:500], "unavailable": True}
