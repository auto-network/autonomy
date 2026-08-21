"""Machine-store admission control for Agent Test runs across containers."""

from __future__ import annotations

import threading
import time
from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.agent_test_capacity import (
    CAPACITY_SET_ID,
    LEASE_SET_ID,
    QUEUE_SET_ID,
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


def _pending(now: float) -> list[dict[str, Any]]:
    members = settings_ops.read_set(QUEUE_SET_ID, org="machine", peers=[]).members
    pending: list[dict[str, Any]] = []
    for member in members:
        payload = dict(member.payload)
        if float(payload.get("expires_at") or 0) <= now:
            try:
                settings_ops.remove_setting(member.id, org="machine")
            except LookupError:
                pass
            continue
        pending.append({"id": member.id, "key": member.key, **payload})
    return pending


def _remove_pending(item: dict[str, Any] | None) -> None:
    if item is None:
        return
    try:
        settings_ops.remove_setting(item["id"], org="machine")
    except LookupError:
        pass


def _activity_context(body: dict[str, Any]) -> dict[str, Any]:
    selectors = [
        str(value).strip()[:1000]
        for value in body.get("selectors") or []
        if str(value).strip()
    ]
    try:
        requested_count = int(body.get("selector_count") or len(selectors))
    except (TypeError, ValueError):
        requested_count = len(selectors)
    selector_count = max(len(selectors), requested_count)
    context = {
        "organization": str(body.get("organization") or "unknown")[:100],
        "repository": str(body.get("repository") or "unknown")[:1000],
        "selectors": selectors[:5],
        "selector_count": min(selector_count, 100_000),
    }
    for name in ("estimated_seconds", "estimated_low_seconds", "estimated_high_seconds"):
        try:
            estimate = float(body.get(name) or 0)
        except (TypeError, ValueError):
            estimate = 0
        if 0 < estimate <= 7 * 24 * 3600:
            context[name] = estimate
    try:
        unknown_count = int(body.get("unknown_selector_count") or 0)
    except (TypeError, ValueError):
        unknown_count = 0
    context["unknown_selector_count"] = max(0, min(unknown_count, 100_000))
    try:
        uncertain_count = int(body.get("uncertain_selector_count") or unknown_count)
    except (TypeError, ValueError):
        uncertain_count = unknown_count
    context["uncertain_selector_count"] = max(0, min(uncertain_count, 100_000))
    return context


def transact(action: str, body: dict[str, Any]) -> dict[str, Any]:
    """Serialize one admission transaction against the machine store."""
    with _LOCK:
        now = time.time()
        capacity = _capacity()
        limits = dict(capacity["limits"])
        ttl = int(capacity["lease_ttl_seconds"])
        leases = _active(now)
        pending = _pending(now)
        if action == "status":
            return _status(limits, leases, pending)

        lease_id = str(body.get("lease_id") or "").strip()
        if not lease_id:
            return {"ok": False, "error": "lease_id is required"}
        existing = next((item for item in leases if item["key"] == lease_id), None)
        waiting = next((item for item in pending if item["key"] == lease_id), None)
        if action == "release":
            if existing is not None:
                settings_ops.remove_setting(existing["id"], org="machine")
            _remove_pending(waiting)
            return {"ok": True, "state": "released", "lease_id": lease_id}
        if action == "renew":
            if existing is None:
                return {"ok": False, "state": "expired", "error": "lease no longer exists"}
            payload = {
                key: existing[key]
                for key in (
                    "session", "run_id", "resources", "acquired_at",
                    "organization", "repository", "selectors", "selector_count",
                    "estimated_seconds",
                    "estimated_low_seconds", "estimated_high_seconds", "unknown_selector_count",
                    "uncertain_selector_count",
                )
                if key in existing
            }
            if body.get("organization"):
                # A legacy lease can be upgraded in place on its next renewal
                # after the authenticated route begins stamping org scope.
                payload["organization"] = str(body["organization"])[:100]
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
            _remove_pending(waiting)
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
            context = _activity_context(body)
            queued_payload = {
                **context,
                "session": str(body.get("session") or "unknown")[:200],
                "run_id": str(body.get("run_id") or lease_id)[:200],
                "resources": {str(name): int(amount) for name, amount in resources.items()},
                "requested_at": float(waiting.get("requested_at") if waiting else now),
                "updated_at": now,
                "expires_at": now + ttl,
            }
            settings_ops.upsert_by_key(
                QUEUE_SET_ID, SCHEMA_REVISION, lease_id, queued_payload, org="machine",
            )
            current_pending = [item for item in pending if item["key"] != lease_id]
            current_pending.append({"id": "", "key": lease_id, **queued_payload})
            queue_position, wait_low, wait_high = _queue_estimate(
                current_pending, leases, lease_id, now,
            )
            return {
                "ok": True,
                "state": "queued",
                "unavailable": unavailable,
                "queue_position": queue_position,
                "queue_depth": len(current_pending),
                "estimated_wait_seconds": wait_high,
                "estimated_wait_low_seconds": wait_low,
                "estimated_wait_high_seconds": wait_high,
                **_status(limits, leases, current_pending),
            }
        _remove_pending(waiting)
        context = _activity_context(body)
        payload = {
            "session": str(body.get("session") or "unknown"),
            "run_id": str(body.get("run_id") or lease_id),
            "resources": {str(name): int(amount) for name, amount in resources.items()},
            "acquired_at": now,
            "expires_at": now + ttl,
            **context,
        }
        settings_ops.upsert_by_key(LEASE_SET_ID, SCHEMA_REVISION, lease_id, payload, org="machine")
        return {"ok": True, "state": "granted", "lease_id": lease_id, "expires_at": payload["expires_at"]}


def _status(
    limits: dict[str, int],
    leases: list[dict[str, Any]],
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pending = pending or []
    used = {name: 0 for name in limits}
    queued = {name: 0 for name in limits}
    for lease in leases:
        for name, amount in lease["resources"].items():
            used[name] = used.get(name, 0) + int(amount)
    for item in pending:
        for name, amount in item["resources"].items():
            queued[name] = queued.get(name, 0) + int(amount)
    return {
        "ok": True,
        "limits": limits,
        "used": used,
        "available": {name: max(0, limit - used.get(name, 0)) for name, limit in limits.items()},
        "active_leases": len(leases),
        "queued_requests": len(pending),
        "queued": queued,
    }


def _queue_estimate(
    pending: list[dict[str, Any]],
    leases: list[dict[str, Any]],
    lease_id: str,
    now: float,
) -> tuple[int, int, int]:
    """Return FIFO position and bounded wait range for one queued request.

    Unknown runtimes deliberately fall back to the lease expiry window.  This
    keeps the estimate honest while still surfacing a number that can reveal a
    dead coordinator instead of leaving an agent in an apparently infinite wait.
    """
    ordered = sorted(pending, key=lambda item: (float(item.get("requested_at") or 0), item.get("run_id", "")))
    try:
        position = next(index for index, item in enumerate(ordered, 1) if item.get("key") == lease_id)
    except StopIteration:
        return 0, 0, 0
    ahead = ordered[: position - 1]
    low = high = 0.0
    for item in leases + ahead:
        elapsed = max(0.0, now - float(item.get("acquired_at") or now))
        estimate = float(item.get("estimated_seconds") or 0)
        low_estimate = float(item.get("estimated_low_seconds") or estimate or 0)
        high_estimate = float(item.get("estimated_high_seconds") or estimate or 0)
        if item in leases:
            low_estimate = max(0.0, low_estimate - elapsed) if low_estimate else 0.0
            high_estimate = max(0.0, high_estimate - elapsed) if high_estimate else 0.0
            if not high_estimate:
                high_estimate = max(0.0, float(item.get("expires_at") or now) - now)
        low += low_estimate
        high += high_estimate
    return position, int(round(low)), int(round(high))


def activity_snapshot(organization: str) -> dict[str, Any]:
    """Return bounded machine activity owned by one organization."""
    with _LOCK:
        now = time.time()
        capacity = _capacity()
        limits = dict(capacity["limits"])
        all_leases = _active(now)
        all_pending = _pending(now)
        leases = [item for item in all_leases if item.get("organization") == organization]
        pending = [item for item in all_pending if item.get("organization") == organization]

        def public(item: dict[str, Any], timestamp: str) -> dict[str, Any]:
            return {
                "session": item["session"],
                "run_id": item["run_id"],
                "repository": item.get("repository") or "unknown",
                "selectors": list(item.get("selectors") or [])[:5],
                "selector_count": int(item.get("selector_count") or 0),
                "estimated_seconds": item.get("estimated_seconds"),
                "estimated_low_seconds": item.get("estimated_low_seconds"),
                "estimated_high_seconds": item.get("estimated_high_seconds"),
                "unknown_selector_count": int(item.get("unknown_selector_count") or 0),
                "uncertain_selector_count": int(item.get("uncertain_selector_count") or 0),
                "resources": dict(item["resources"]),
                timestamp: float(item[timestamp]),
            }

        leases.sort(key=lambda item: (float(item["acquired_at"]), item["run_id"]))
        pending.sort(key=lambda item: (float(item["requested_at"]), item["run_id"]))
        return {
            **_status(limits, leases, pending),
            "running": [public(item, "acquired_at") for item in leases[:100]],
            "queue": [public(item, "requested_at") for item in pending[:100]],
            "global_active_leases": len(all_leases),
            "global_queued_requests": len(all_pending),
            "as_of": now,
        }
