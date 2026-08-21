"""Organization-scoped persistence and bounded aggregates for Testing."""
from __future__ import annotations

import threading
import time
from statistics import median
from typing import Any
from uuid import uuid4

from tools.dashboard.plugins.testing.entrypoints.schemas import (
    AgentTestObservationV1,
    AgentTestRunV1,
    OBSERVATION_SET_ID,
    RUN_SET_ID,
    SCHEMA_REVISION,
    TELEMETRY_SET_ID,
)
from tools.graph import settings_ops


MAX_OBSERVATIONS_PER_TEST = 10
MAX_RUNS_PER_REPOSITORY = 2_000
MAX_OBSERVATIONS_PER_REQUEST = 20_000
MAX_SELECTORS = 200
MAX_RECENT_RUNS = 100
MAX_RANKED_TESTS = 50
_LOCK = threading.Lock()


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


def _members(set_id: str, org: str) -> list[Any]:
    return list(settings_ops.read_owned_set(set_id, org=org).members)


def record_event(org: str, session: str, event: str) -> dict[str, Any]:
    with _LOCK:
        row = settings_ops.read_set_key(
            TELEMETRY_SET_ID, session, org=org, peers=[],
        )
        counts = dict((row or {}).get("payload", {}).get("counts") or {})
        counts[event] = int(counts.get(event, 0)) + 1
        payload = {"counts": counts, "last_event": event, "last_at": time.time()}
        settings_ops.upsert_by_key(
            TELEMETRY_SET_ID,
            SCHEMA_REVISION,
            session,
            payload,
            org=org,
            state="raw",
        )
        return {"ok": True, "session": session, "counts": counts}


def telemetry_status(org: str) -> dict[str, Any]:
    members = _members(TELEMETRY_SET_ID, org)
    totals: dict[str, int] = {}
    for member in members:
        for name, amount in member.payload.get("counts", {}).items():
            totals[name] = totals.get(name, 0) + int(amount)
    return {"ok": True, "sessions": len(members), "counts": totals}


def record_run(org: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist one immutable terminal run and cap repository run history."""

    if not run_id or len(run_id) > 200:
        return {"ok": False, "error": "valid run_id is required"}
    try:
        AgentTestRunV1.validate(payload)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:1000]}

    with _LOCK:
        existing = settings_ops.read_set_key(
            RUN_SET_ID, run_id, org=org, peers=[],
        )
        if existing is not None:
            return {"ok": True, "duplicate": True, "run_id": run_id}
        settings_ops.add_setting(
            RUN_SET_ID,
            SCHEMA_REVISION,
            run_id,
            payload,
            org=org,
            state="raw",
        )
        repository = payload["repository"]
        history = [
            member for member in _members(RUN_SET_ID, org)
            if member.payload.get("repository") == repository
        ]
        history.sort(
            key=lambda member: (
                str(member.payload.get("finished_at") or ""),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )
        stale_runs = history[MAX_RUNS_PER_REPOSITORY:]
        stale_run_ids = {member.key for member in stale_runs}
        stale = [member.id for member in stale_runs]
        if stale_run_ids:
            stale.extend(
                member.id for member in _members(OBSERVATION_SET_ID, org)
                if member.payload.get("run_id") in stale_run_ids
            )
        pruned = settings_ops.remove_raw_settings(stale, org=org)
    return {
        "ok": True,
        "duplicate": False,
        "run_id": run_id,
        "pruned": pruned,
        "history_limit": MAX_RUNS_PER_REPOSITORY,
    }


def _observation_members(org: str, repository: str) -> list[Any]:
    return [
        member for member in _members(OBSERVATION_SET_ID, org)
        if member.payload.get("repository") == repository
    ]


def record_observations(
    org: str,
    repository: str,
    run_id: str,
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append observations once and retain the latest ten per test node."""

    if not repository or len(repository) > 1000:
        return {"ok": False, "error": "valid repository is required"}
    if not run_id or len(run_id) > 200:
        return {"ok": False, "error": "valid run_id is required"}
    if not isinstance(observations, list) or not 1 <= len(observations) <= MAX_OBSERVATIONS_PER_REQUEST:
        return {
            "ok": False,
            "error": (
                "observations must contain 1 to "
                f"{MAX_OBSERVATIONS_PER_REQUEST} entries"
            ),
        }

    recorded_at = time.time()
    prepared: list[dict[str, Any]] = []
    batch_seen: set[str] = set()
    duplicates = 0
    for raw in observations:
        if not isinstance(raw, dict):
            return {"ok": False, "error": "each observation must be an object"}
        nodeid = str(raw.get("nodeid") or "").strip()
        if nodeid in batch_seen:
            duplicates += 1
            continue
        batch_seen.add(nodeid)
        payload = {
            "repository": repository,
            "run_id": run_id,
            "nodeid": nodeid,
            "duration_seconds": raw.get("duration_seconds"),
            "outcome": raw.get("outcome"),
            "recorded_at": recorded_at,
        }
        try:
            AgentTestObservationV1.validate(payload)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}
        prepared.append(payload)

    with _LOCK:
        existing = _observation_members(org, repository)
        seen = {
            (str(member.payload.get("run_id")), str(member.payload.get("nodeid")))
            for member in existing
        }
        entries = []
        for payload in prepared:
            identity = (run_id, payload["nodeid"])
            if identity in seen:
                duplicates += 1
                continue
            entries.append((str(uuid4()), payload))
        try:
            appended = len(settings_ops.append_log_entries(
                OBSERVATION_SET_ID,
                SCHEMA_REVISION,
                entries,
                org=org,
            ))
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

        by_node: dict[str, list[Any]] = {}
        for member in _observation_members(org, repository):
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
            stale_ids.extend(
                member.id for member in history[MAX_OBSERVATIONS_PER_TEST:]
            )
        pruned = settings_ops.remove_raw_settings(stale_ids, org=org)
    return {
        "ok": True,
        "repository": repository,
        "appended": appended,
        "duplicates": duplicates,
        "pruned": pruned,
        "history_limit": MAX_OBSERVATIONS_PER_TEST,
    }


def _validated_selectors(
    repository: str, selectors: list[str],
) -> tuple[list[str] | None, dict[str, Any] | None]:
    if not repository or len(repository) > 1000:
        return None, {"ok": False, "error": "valid repository is required"}
    if not isinstance(selectors, list):
        return None, {"ok": False, "error": "selectors must be an array"}
    normalized = list(dict.fromkeys(
        str(value).strip() for value in selectors if str(value).strip()
    ))
    if not normalized:
        return None, {"ok": False, "error": "at least one selector is required"}
    if len(normalized) > MAX_SELECTORS:
        return None, {"ok": False, "error": f"at most {MAX_SELECTORS} selectors are accepted"}
    return normalized, None


def duration_history(
    org: str,
    repository: str,
    selectors: list[str],
    *,
    limit_tests: int = 10,
) -> dict[str, Any]:
    selectors, error = _validated_selectors(repository, selectors)
    if error is not None:
        return error
    assert selectors is not None
    try:
        limit_tests = max(1, min(int(limit_tests), MAX_RANKED_TESTS))
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit_tests must be an integer"}
    by_node: dict[str, list[Any]] = {}
    for member in _observation_members(org, repository):
        nodeid = str(member.payload.get("nodeid") or "")
        if any(_selector_matches(nodeid, selector) for selector in selectors):
            by_node.setdefault(nodeid, []).append(member)
    tests = []
    for nodeid in sorted(by_node)[:limit_tests]:
        history = sorted(
            by_node[nodeid],
            key=lambda member: (
                float(member.payload.get("recorded_at") or 0),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )[:MAX_OBSERVATIONS_PER_TEST]
        samples = [float(member.payload["duration_seconds"]) for member in history]
        tests.append({
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
        })
    return {
        "ok": True,
        "repository": repository,
        "selectors": selectors,
        "tests": tests,
        "matched_tests": len(by_node),
        "omitted_tests": max(0, len(by_node) - len(tests)),
        "history_limit": MAX_OBSERVATIONS_PER_TEST,
    }


def estimate_duration(
    org: str,
    repository: str,
    selectors: list[str],
    *,
    parallelism: int = 1,
) -> dict[str, Any]:
    selectors, error = _validated_selectors(repository, selectors)
    if error is not None:
        return error
    assert selectors is not None
    by_node: dict[str, list[tuple[float, float]]] = {}
    for member in _observation_members(org, repository):
        nodeid = str(member.payload.get("nodeid") or "")
        if any(_selector_matches(nodeid, selector) for selector in selectors):
            by_node.setdefault(nodeid, []).append((
                float(member.payload.get("recorded_at") or 0),
                float(member.payload["duration_seconds"]),
            ))
    try:
        parallelism = max(1, min(int(parallelism), 256))
    except (TypeError, ValueError):
        return {"ok": False, "error": "parallelism must be an integer"}
    recent = {
        nodeid: [duration for _at, duration in sorted(samples, reverse=True)[:MAX_OBSERVATIONS_PER_TEST]]
        for nodeid, samples in by_node.items()
    }
    total = sum(float(median(samples)) for samples in recent.values())
    known = {
        selector for selector in selectors
        if any(_selector_matches(nodeid, selector) for nodeid in by_node)
    }
    return {
        "ok": True,
        "repository": repository,
        "selectors": selectors,
        "estimated_seconds": total / parallelism if by_node else None,
        "serial_seconds": total if by_node else None,
        "parallelism": parallelism,
        "sampled_tests": len(by_node),
        "sample_count": sum(len(samples) for samples in recent.values()),
        "unknown_selectors": [selector for selector in selectors if selector not in known],
        "history_limit": MAX_OBSERVATIONS_PER_TEST,
    }


def dashboard_summary(
    org: str,
    repository: str = "",
    *,
    recent_limit: int = 20,
    ranked_limit: int = 10,
) -> dict[str, Any]:
    """Return bounded aggregate data for the Testing dashboard."""

    recent_limit = max(1, min(int(recent_limit), MAX_RECENT_RUNS))
    ranked_limit = max(1, min(int(ranked_limit), MAX_RANKED_TESTS))
    all_runs = _members(RUN_SET_ID, org)
    repositories = sorted({
        str(member.payload.get("repository") or "")
        for member in all_runs if member.payload.get("repository")
    })
    runs = [
        member for member in all_runs
        if not repository or member.payload.get("repository") == repository
    ]
    runs.sort(
        key=lambda member: (
            str(member.payload.get("finished_at") or ""),
            member.created_at,
            member.id,
        ),
        reverse=True,
    )
    status_counts: dict[str, int] = {}
    totals = {name: 0 for name in ("collected", "passed", "failed", "errors", "skipped")}
    for member in runs:
        payload = member.payload
        status = str(payload.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        for name in totals:
            totals[name] += int(payload.get(name) or 0)
    terminal = status_counts.get("passed", 0) + status_counts.get("failed", 0) + status_counts.get("error", 0)
    pass_ratio = status_counts.get("passed", 0) / terminal if terminal else None

    observations = _members(OBSERVATION_SET_ID, org)
    if repository:
        observations = [
            member for member in observations
            if member.payload.get("repository") == repository
        ]
    by_node: dict[str, list[Any]] = {}
    for member in observations:
        by_node.setdefault(str(member.payload.get("nodeid") or ""), []).append(member)
    ranked = []
    for nodeid, history in by_node.items():
        history.sort(
            key=lambda member: (
                float(member.payload.get("recorded_at") or 0),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )
        history = history[:MAX_OBSERVATIONS_PER_TEST]
        outcomes = [str(member.payload["outcome"]) for member in history]
        failures = sum(outcome in {"failed", "error"} for outcome in outcomes)
        passes = outcomes.count("passed")
        decisions = failures + passes
        durations = [float(member.payload["duration_seconds"]) for member in history]
        ranked.append({
            "nodeid": nodeid,
            "samples": len(history),
            "failure_rate": failures / decisions if decisions else 0.0,
            "flaky": failures > 0 and passes > 0,
            "latest_outcome": outcomes[0],
            "median_seconds": median(durations),
        })
    flaky = sorted(
        (item for item in ranked if item["flaky"]),
        key=lambda item: (-item["failure_rate"], -item["samples"], item["nodeid"]),
    )[:ranked_limit]
    slow = sorted(
        ranked,
        key=lambda item: (-item["median_seconds"], item["nodeid"]),
    )[:ranked_limit]

    recent_runs = []
    for member in runs[:recent_limit]:
        payload = member.payload
        recent_runs.append({
            "run_id": member.key,
            "repository": payload["repository"],
            "session": payload["session"],
            "status": payload["status"],
            "mode": payload["mode"],
            "duration_seconds": payload["duration_seconds"],
            "created_at": payload["created_at"],
            "finished_at": payload["finished_at"],
            "selector_count": len(payload.get("selectors") or []),
            "collected": payload["collected"],
            "passed": payload["passed"],
            "failed": payload["failed"],
            "errors": payload["errors"],
            "skipped": payload["skipped"],
            "parallelism": payload.get("parallelism", 1),
            "rerun_of": payload.get("rerun_of") or None,
        })
    telemetry = telemetry_status(org)
    return {
        "ok": True,
        "organization": org,
        "repository": repository or None,
        "repositories": repositories[:100],
        "repository_count": len(repositories),
        "runs": {
            "total": len(runs),
            "status_counts": status_counts,
            "pass_ratio": pass_ratio,
            "test_totals": totals,
        },
        "tests": {
            "observed": len(by_node),
            "flaky": flaky,
            "slow": slow,
            "ranked_limit": ranked_limit,
            "history_limit": MAX_OBSERVATIONS_PER_TEST,
        },
        "recent_runs": recent_runs,
        "recent_limit": recent_limit,
        "telemetry": telemetry,
    }
