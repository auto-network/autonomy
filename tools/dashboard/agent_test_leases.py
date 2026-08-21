"""Machine-store admission control for Agent Test runs across containers."""

from __future__ import annotations

import threading
import time
from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.agent_test_capacity import (
    CAPACITY_SET_ID,
    LEASE_SET_ID,
    SCHEMA_REVISION,
)


DEFAULT_CAPACITY = {"tests": 16, "browsers": 4}
DEFAULT_TTL_SECONDS = 90
_LOCK = threading.Lock()


def _capacity() -> dict[str, Any]:
    row = settings_ops.read_set_key(CAPACITY_SET_ID, "global", org="machine", peers=[])
    if row is not None:
        return dict(row["payload"])
    payload = {"limits": dict(DEFAULT_CAPACITY), "lease_ttl_seconds": DEFAULT_TTL_SECONDS}
    settings_ops.upsert_by_key(CAPACITY_SET_ID, SCHEMA_REVISION, "global", payload, org="machine")
    return payload


def _active(now: float) -> list[dict[str, Any]]:
    members = settings_ops.read_set(LEASE_SET_ID, org="machine", peers=[]).members
    active: list[dict[str, Any]] = []
    for member in members:
        payload = dict(member.payload)
        if float(payload.get("expires_at") or 0) <= now:
            try:
                settings_ops.remove_setting(member.id, org="machine")
            except LookupError:
                pass
            continue
        active.append({"id": member.id, "key": member.key, **payload})
    return active


def transact(action: str, body: dict[str, Any]) -> dict[str, Any]:
    """Serialize one admission transaction against the machine store."""
    with _LOCK:
        now = time.time()
        capacity = _capacity()
        limits = dict(capacity["limits"])
        ttl = int(capacity["lease_ttl_seconds"])
        leases = _active(now)
        if action == "status":
            return _status(limits, leases)

        lease_id = str(body.get("lease_id") or "").strip()
        if not lease_id:
            return {"ok": False, "error": "lease_id is required"}
        existing = next((item for item in leases if item["key"] == lease_id), None)
        if action == "release":
            if existing is not None:
                settings_ops.remove_setting(existing["id"], org="machine")
            return {"ok": True, "state": "released", "lease_id": lease_id}
        if action == "renew":
            if existing is None:
                return {"ok": False, "state": "expired", "error": "lease no longer exists"}
            payload = {key: existing[key] for key in ("session", "run_id", "resources", "acquired_at")}
            payload["expires_at"] = now + ttl
            settings_ops.upsert_by_key(LEASE_SET_ID, SCHEMA_REVISION, lease_id, payload, org="machine")
            return {"ok": True, "state": "granted", "lease_id": lease_id, "expires_at": payload["expires_at"]}
        if action != "acquire":
            return {"ok": False, "error": f"unsupported action: {action}"}

        resources = body.get("resources")
        if not isinstance(resources, dict) or not resources:
            return {"ok": False, "error": "resources must be a non-empty object"}
        unknown = sorted(set(resources) - set(limits))
        if unknown:
            return {"ok": False, "error": f"resources have no configured capacity: {', '.join(unknown)}"}
        if existing is not None:
            return {"ok": True, "state": "granted", "lease_id": lease_id, "expires_at": existing["expires_at"]}
        used = {name: 0 for name in limits}
        for lease in leases:
            for name, amount in lease["resources"].items():
                used[name] = used.get(name, 0) + int(amount)
        unavailable = {
            name: {"requested": int(amount), "used": used.get(name, 0), "limit": limits[name]}
            for name, amount in resources.items()
            if used.get(name, 0) + int(amount) > limits[name]
        }
        if unavailable:
            return {"ok": True, "state": "queued", "unavailable": unavailable, **_status(limits, leases)}
        payload = {
            "session": str(body.get("session") or "unknown"),
            "run_id": str(body.get("run_id") or lease_id),
            "resources": {str(name): int(amount) for name, amount in resources.items()},
            "acquired_at": now,
            "expires_at": now + ttl,
        }
        settings_ops.upsert_by_key(LEASE_SET_ID, SCHEMA_REVISION, lease_id, payload, org="machine")
        return {"ok": True, "state": "granted", "lease_id": lease_id, "expires_at": payload["expires_at"]}


def _status(limits: dict[str, int], leases: list[dict[str, Any]]) -> dict[str, Any]:
    used = {name: 0 for name in limits}
    for lease in leases:
        for name, amount in lease["resources"].items():
            used[name] = used.get(name, 0) + int(amount)
    return {
        "ok": True,
        "limits": limits,
        "used": used,
        "available": {name: max(0, limit - used.get(name, 0)) for name, limit in limits.items()},
        "active_leases": len(leases),
    }
