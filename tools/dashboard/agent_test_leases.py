"""Machine-store admission control for Agent Test runs across containers."""

from __future__ import annotations

import threading
import time
from statistics import median
from typing import Any
from uuid import uuid4

from tools.graph import settings_ops
from tools.graph.schemas.agent_test_capacity import (
    AgentTestDurationObservationV1,
    CAPACITY_SET_ID,
    DURATION_SET_ID,
    LEASE_SET_ID,
    SCHEMA_REVISION,
    TELEMETRY_SET_ID,
)


DEFAULT_CAPACITY = {"tests": 16, "browsers": 4}
DEFAULT_TTL_SECONDS = 90
_LOCK = threading.Lock()
_DURATION_LOCK = threading.Lock()
MAX_DURATION_HISTORY = 10
MAX_DURATION_BATCH = 20_000
MAX_HISTORY_TESTS = 50
MAX_DURATION_SELECTORS = 200


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


def record_event(session: str, event: str) -> dict[str, Any]:
    with _LOCK:
        row = settings_ops.read_set_key(TELEMETRY_SET_ID, session, org="machine", peers=[])
        counts = dict((row or {}).get("payload", {}).get("counts") or {})
        counts[event] = int(counts.get(event, 0)) + 1
        payload = {"counts": counts, "last_event": event, "last_at": time.time()}
        settings_ops.upsert_by_key(
            TELEMETRY_SET_ID, SCHEMA_REVISION, session, payload, org="machine"
        )
        return {"ok": True, "session": session, "counts": counts}


def telemetry_status() -> dict[str, Any]:
    with _LOCK:
        members = settings_ops.read_set(TELEMETRY_SET_ID, org="machine", peers=[]).members
        totals: dict[str, int] = {}
        for member in members:
            for name, amount in member.payload.get("counts", {}).items():
                totals[name] = totals.get(name, 0) + int(amount)
        return {"ok": True, "sessions": len(members), "counts": totals}


def _duration_members(repository: str) -> list[Any]:
    return [
        member
        for member in settings_ops.read_set(DURATION_SET_ID, org="machine", peers=[]).members
        if member.payload.get("repository") == repository
    ]


def _selector_matches(nodeid: str, selector: str) -> bool:
    normalized = selector.removeprefix("./").rstrip("/")
    return bool(
        normalized
        and (
            nodeid == normalized
            or nodeid.startswith(normalized + "::")
            or nodeid.startswith(normalized + "[")
            or nodeid.startswith(normalized + "/")
        )
    )


def record_durations(repository: str, run_id: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Append one immutable row per node and retain only its latest ten.

    All Agent Test containers reach this dashboard process, so the dedicated
    duration lock makes retry deduplication, appends, and pruning one
    serialized operation without delaying capacity-lease renewals. Surviving
    Settings rows are never rewritten.
    """
    if not repository or len(repository) > 1000:
        return {"ok": False, "error": "valid repository is required"}
    if not run_id or len(run_id) > 200:
        return {"ok": False, "error": "valid run_id is required"}
    if not isinstance(observations, list) or not 1 <= len(observations) <= MAX_DURATION_BATCH:
        return {
            "ok": False,
            "error": f"observations must contain 1 to {MAX_DURATION_BATCH} entries",
        }

    now = time.time()
    prepared: list[dict[str, Any]] = []
    batch_seen: set[str] = set()
    duplicates = 0
    for observation in observations:
        if not isinstance(observation, dict):
            return {"ok": False, "error": "each observation must be an object"}
        nodeid = str(observation.get("nodeid") or "").strip()
        if not nodeid:
            return {"ok": False, "error": "each observation requires a nodeid"}
        if nodeid in batch_seen:
            duplicates += 1
            continue
        batch_seen.add(nodeid)
        payload = {
            "repository": repository,
            "run_id": run_id,
            "nodeid": nodeid,
            "duration_seconds": observation.get("duration_seconds"),
            "outcome": observation.get("outcome"),
            "recorded_at": now,
        }
        try:
            AgentTestDurationObservationV1.validate(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)[:1000]}
        prepared.append(payload)

    with _DURATION_LOCK:
        existing = _duration_members(repository)
        seen = {
            (str(member.payload.get("run_id")), str(member.payload.get("nodeid")))
            for member in existing
        }
        entries: list[tuple[str, dict[str, Any]]] = []
        for payload in prepared:
            nodeid = payload["nodeid"]
            if (run_id, nodeid) in seen:
                duplicates += 1
                continue
            entries.append((str(uuid4()), payload))
        try:
            appended = len(
                settings_ops.append_log_entries(
                    DURATION_SET_ID,
                    SCHEMA_REVISION,
                    entries,
                    org="machine",
                )
            )
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

        members = _duration_members(repository)
        by_node: dict[str, list[Any]] = {}
        for member in members:
            by_node.setdefault(str(member.payload["nodeid"]), []).append(member)
        stale_ids: list[str] = []
        for history in by_node.values():
            history.sort(
                key=lambda member: (
                    float(member.payload.get("recorded_at") or 0),
                    member.created_at,
                    member.id,
                ),
                reverse=True,
            )
            for stale in history[MAX_DURATION_HISTORY:]:
                stale_ids.append(stale.id)
        pruned = settings_ops.remove_raw_settings(stale_ids, org="machine")
        return {
            "ok": True,
            "repository": repository,
            "appended": appended,
            "duplicates": duplicates,
            "pruned": pruned,
            "history_limit": MAX_DURATION_HISTORY,
        }


def _validated_duration_selectors(
    repository: str, selectors: list[str],
) -> tuple[list[str] | None, dict[str, Any] | None]:
    if not repository or len(repository) > 1000:
        return None, {"ok": False, "error": "valid repository is required"}
    if not isinstance(selectors, list):
        return None, {"ok": False, "error": "selectors must be an array"}
    normalized = list(
        dict.fromkeys(str(value).strip() for value in selectors if str(value).strip())
    )
    if not normalized:
        return None, {"ok": False, "error": "at least one selector is required"}
    if len(normalized) > MAX_DURATION_SELECTORS:
        return None, {
            "ok": False,
            "error": f"at most {MAX_DURATION_SELECTORS} selectors are accepted",
        }
    return normalized, None


def duration_history(repository: str, selectors: list[str], *, limit_tests: int = 10) -> dict[str, Any]:
    selectors, error = _validated_duration_selectors(repository, selectors)
    if error is not None:
        return error
    assert selectors is not None
    try:
        limit_tests = max(1, min(int(limit_tests), MAX_HISTORY_TESTS))
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit_tests must be an integer"}
    with _DURATION_LOCK:
        members = _duration_members(repository)
    by_node: dict[str, list[Any]] = {}
    for member in members:
        nodeid = str(member.payload.get("nodeid") or "")
        if any(_selector_matches(nodeid, selector) for selector in selectors):
            by_node.setdefault(nodeid, []).append(member)

    tests: list[dict[str, Any]] = []
    for nodeid in sorted(by_node)[:limit_tests]:
        history = sorted(
            by_node[nodeid],
            key=lambda member: (
                float(member.payload.get("recorded_at") or 0),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )[:MAX_DURATION_HISTORY]
        samples = [float(member.payload["duration_seconds"]) for member in history]
        tests.append(
            {
                "nodeid": nodeid,
                "median_seconds": median(samples),
                "observations": [
                    {
                        "run_id": member.payload["run_id"],
                        "duration_seconds": float(member.payload["duration_seconds"]),
                        "outcome": member.payload["outcome"],
                        "recorded_at": float(member.payload["recorded_at"]),
                    }
                    for member in history
                ],
            }
        )
    return {
        "ok": True,
        "repository": repository,
        "selectors": selectors,
        "tests": tests,
        "matched_tests": len(by_node),
        "omitted_tests": max(0, len(by_node) - len(tests)),
        "history_limit": MAX_DURATION_HISTORY,
    }


def estimate_duration(repository: str, selectors: list[str], *, parallelism: int = 1) -> dict[str, Any]:
    selectors, error = _validated_duration_selectors(repository, selectors)
    if error is not None:
        return error
    assert selectors is not None
    # Estimate over every matching node, not just the bounded presentation
    # returned by duration_history.
    with _DURATION_LOCK:
        members = _duration_members(repository)
    by_node: dict[str, list[tuple[float, float]]] = {}
    for member in members:
        nodeid = str(member.payload.get("nodeid") or "")
        if any(_selector_matches(nodeid, selector) for selector in selectors):
            by_node.setdefault(nodeid, []).append(
                (
                    float(member.payload.get("recorded_at") or 0),
                    float(member.payload["duration_seconds"]),
                )
            )
    known_selectors = {
        selector
        for selector in selectors
        if any(_selector_matches(nodeid, selector) for nodeid in by_node)
    }
    try:
        parallelism = max(1, min(int(parallelism), 256))
    except (TypeError, ValueError):
        return {"ok": False, "error": "parallelism must be an integer"}
    recent = {
        nodeid: [duration for _at, duration in sorted(samples, reverse=True)[:MAX_DURATION_HISTORY]]
        for nodeid, samples in by_node.items()
    }
    total = sum(float(median(samples)) for samples in recent.values())
    return {
        "ok": True,
        "repository": repository,
        "selectors": selectors,
        "estimated_seconds": total / parallelism if by_node else None,
        "serial_seconds": total if by_node else None,
        "parallelism": parallelism,
        "sampled_tests": len(by_node),
        "sample_count": sum(len(samples) for samples in recent.values()),
        "unknown_selectors": [selector for selector in selectors if selector not in known_selectors],
        "history_limit": MAX_DURATION_HISTORY,
    }
