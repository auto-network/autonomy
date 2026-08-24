"""Organization-scoped persistence and bounded aggregates for Testing."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from statistics import median
from typing import Any
from uuid import uuid4

from tools.dashboard.plugins.testing.entrypoints.schemas import (
    AgentTestObservationV1,
    AgentTestRunV1,
    ERROR_SET_ID,
    OBSERVATION_SET_ID,
    RUN_SET_ID,
    SCHEMA_REVISION,
    TELEMETRY_SET_ID,
    USAGE_SET_ID,
)
from tools.graph import settings_ops
from tools.dashboard import agent_test_leases
from tools.dashboard.dao import dashboard_db


MAX_OBSERVATIONS_PER_TEST = 10
MAX_RUNS_PER_REPOSITORY = 2_000
MAX_OBSERVATIONS_PER_REQUEST = 20_000
MAX_SELECTORS = 200
MAX_RECENT_RUNS = 100
MAX_RANKED_TESTS = 50
MAX_USAGE_EVENTS = 5_000
MAX_ERROR_EVENTS = 2_000
_LOCK = threading.Lock()


def _session_title(session: str) -> str:
    try:
        row = dashboard_db.get_session(session) or {}
    except Exception:
        row = {}
    return str(row.get("label") or session)


def _iso_timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


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


def record_event(
    org: str,
    session: str,
    event: str,
    agent_test_version: str = "",
) -> dict[str, Any]:
    with _LOCK:
        now = time.time()
        settings_ops.append_log_entries(
            USAGE_SET_ID,
            SCHEMA_REVISION,
            [(str(uuid4()), {
                "session": session,
                "event": event,
                "recorded_at": now,
                "agent_test_version": str(agent_test_version or "")[:40],
            })],
            org=org,
        )
        usage = _members(USAGE_SET_ID, org)
        usage.sort(
            key=lambda member: (
                float(member.payload.get("recorded_at") or 0),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )
        settings_ops.remove_raw_settings(
            [member.id for member in usage[MAX_USAGE_EVENTS:]], org=org,
        )
        row = settings_ops.read_set_key(
            TELEMETRY_SET_ID, session, org=org, peers=[],
        )
        counts = dict((row or {}).get("payload", {}).get("counts") or {})
        counts[event] = int(counts.get(event, 0)) + 1
        payload = {"counts": counts, "last_event": event, "last_at": now}
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
    usage = _members(USAGE_SET_ID, org)
    usage.sort(
        key=lambda member: (
            float(member.payload.get("recorded_at") or 0),
            member.created_at,
            member.id,
        ),
        reverse=True,
    )
    latest_version: dict[str, str] = {}
    for member in usage:
        session = str(member.payload.get("session") or "")
        if session and session not in latest_version:
            latest_version[session] = str(member.payload.get("agent_test_version") or "unknown")
    versions: dict[str, int] = {}
    for version in latest_version.values():
        versions[version] = versions.get(version, 0) + 1
    feature_counts = {
        name: amount for name, amount in sorted(totals.items())
        if name.startswith("command_")
        or name in {"raw_pytest_refused", "repeat_refused", "capacity_queued"}
    }
    behavior = _behavior_patterns(usage)
    return {
        "ok": True,
        "sessions": len(members),
        "counts": totals,
        "feature_counts": feature_counts,
        "versions": versions,
        "behavior": behavior,
        "recent_events": [
            {
                "session": member.payload["session"],
                "session_title": _session_title(str(member.payload["session"])),
                "event": member.payload["event"],
                "recorded_at": float(member.payload["recorded_at"]),
                "agent_test_version": member.payload.get("agent_test_version") or "unknown",
            }
            for member in usage[:20]
        ],
        "usage_history_limit": MAX_USAGE_EVENTS,
    }


def _behavior_patterns(usage: list[Any]) -> dict[str, Any]:
    """Derive argument-free workflow patterns from ordered usage events."""
    by_session: dict[str, list[Any]] = {}
    for member in reversed(usage):
        by_session.setdefault(str(member.payload.get("session") or "unknown"), []).append(member)

    rows: list[dict[str, Any]] = []
    total_names = (
        "completed_runs", "background_runs", "polled_runs", "status_polls",
        "status_poll_bursts", "repeated_commands", "failed_runs_inspected",
        "focused_recoveries", "broad_recovery_attempts", "repeat_refusals",
        "raw_pytest_refusals", "queue_waits", "output_reads", "retained_runs",
    )
    totals = {name: 0 for name in total_names}
    terminal_events = {
        "run_passed", "run_failed", "run_error", "run_stopped", "run_collected",
    }
    evidence_commands = {"command_failures", "command_trace", "command_output"}
    run_commands = {"command_run_explicit", "command_run_changed", "command_run_profile"}

    for session, events in by_session.items():
        counts = {name: 0 for name in total_names}
        live: dict[str, Any] | None = None
        failed_run_open = False
        failed_run_inspected = False
        last_command = ""
        last_command_at = 0.0
        last_status_at = 0.0
        latest_version = "unknown"
        sequence: list[dict[str, Any]] = []
        for member in events:
            payload = member.payload
            event = str(payload.get("event") or "unknown")
            at = float(payload.get("recorded_at") or 0)
            version = str(payload.get("agent_test_version") or "")
            if version:
                latest_version = version
            sequence.append({"event": event, "recorded_at": at})

            if event.startswith("command_"):
                if event == last_command and at - last_command_at <= 60:
                    counts["repeated_commands"] += 1
                last_command, last_command_at = event, at
            if event == "run_started":
                live = {"status_polls": 0}
                last_status_at = 0.0
            elif event == "command_status" and live is not None:
                live["status_polls"] += 1
                counts["status_polls"] += 1
                if last_status_at and at - last_status_at <= 60:
                    counts["status_poll_bursts"] += 1
                last_status_at = at
            elif event in terminal_events:
                if live is not None:
                    counts["completed_runs"] += 1
                    if live["status_polls"]:
                        counts["polled_runs"] += 1
                    else:
                        counts["background_runs"] += 1
                live = None
                failed_run_open = event == "run_failed"
                failed_run_inspected = False
            elif event in evidence_commands:
                if event == "command_output":
                    counts["output_reads"] += 1
                if failed_run_open and not failed_run_inspected:
                    counts["failed_runs_inspected"] += 1
                    failed_run_inspected = True
            elif event == "command_rerun_failures":
                counts["focused_recoveries"] += 1
                failed_run_open = False
            elif event in run_commands and failed_run_open:
                counts["broad_recovery_attempts"] += 1
            elif event == "repeat_refused":
                counts["repeat_refusals"] += 1
            elif event == "raw_pytest_refused":
                counts["raw_pytest_refusals"] += 1
            elif event == "capacity_queued":
                counts["queue_waits"] += 1
            elif event == "command_retain":
                counts["retained_runs"] += 1

        for name in total_names:
            totals[name] += counts[name]
        rows.append({
            "session": session,
            "session_title": _session_title(session),
            "agent_test_version": latest_version,
            "event_count": len(events),
            "last_seen_at": float(events[-1].payload.get("recorded_at") or 0),
            "counts": counts,
            "recent_sequence": sequence[-10:],
        })
    rows.sort(key=lambda item: (-item["last_seen_at"], item["session"]))
    return {
        "totals": totals,
        "sessions": rows[:50],
        "session_count": len(rows),
        "sequence_limit": 10,
        "derivation": "argument-free ordered usage events",
    }


def record_error(
    org: str,
    session: str,
    *,
    run_id: str,
    phase: str,
    category: str,
    message: str,
    agent_test_version: str = "",
) -> dict[str, Any]:
    """Append one classified operational error and cap organization history."""
    payload = {
        "session": session,
        "run_id": str(run_id or "")[:200],
        "phase": phase,
        "category": category,
        "message": " ".join(str(message).split())[:1000] or "unknown error",
        "recorded_at": time.time(),
        "agent_test_version": str(agent_test_version or "")[:40],
    }
    with _LOCK:
        settings_ops.append_log_entries(
            ERROR_SET_ID, SCHEMA_REVISION, [(str(uuid4()), payload)], org=org,
        )
        events = _members(ERROR_SET_ID, org)
        events.sort(
            key=lambda member: (
                float(member.payload.get("recorded_at") or 0),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )
        pruned = settings_ops.remove_raw_settings(
            [member.id for member in events[MAX_ERROR_EVENTS:]], org=org,
        )
    return {"ok": True, "recorded": True, "pruned": pruned}


def error_status(org: str, *, limit: int = 20) -> dict[str, Any]:
    events = _members(ERROR_SET_ID, org)
    events.sort(
        key=lambda member: (
            float(member.payload.get("recorded_at") or 0),
            member.created_at,
            member.id,
        ),
        reverse=True,
    )
    counts: dict[str, int] = {}
    for member in events:
        category = str(member.payload.get("category") or "unknown")
        counts[category] = counts.get(category, 0) + 1
    return {
        "total": len(events),
        "counts": counts,
        "recent": [dict(member.payload) for member in events[:max(1, min(limit, 50))]],
        "history_limit": MAX_ERROR_EVENTS,
    }


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
            "minimum_seconds": min(samples),
            "maximum_seconds": max(samples),
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


def node_estimates(
    org: str,
    repository: str,
    nodeids: list[str],
    *,
    limit_samples: int = 10,
) -> dict[str, Any]:
    """Return compact per-node timing estimates for an active run watchdog."""
    if not repository or len(repository) > 1000:
        return {"ok": False, "error": "valid repository is required"}
    if not isinstance(nodeids, list) or not 1 <= len(nodeids) <= 2000:
        return {"ok": False, "error": "nodeids must contain 1 to 2000 entries"}
    try:
        limit_samples = max(1, min(int(limit_samples), MAX_OBSERVATIONS_PER_TEST))
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit_samples must be an integer"}
    wanted = list(dict.fromkeys(str(value).strip() for value in nodeids if str(value).strip()))
    if not wanted:
        return {"ok": False, "error": "nodeids must contain non-empty strings"}
    wanted_set = set(wanted)
    histories: dict[str, list[Any]] = {}
    for member in _observation_members(org, repository):
        nodeid = str(member.payload.get("nodeid") or "")
        if nodeid in wanted_set:
            histories.setdefault(nodeid, []).append(member)
    estimates = []
    for nodeid, members in histories.items():
        members.sort(
            key=lambda member: (
                float(member.payload.get("recorded_at") or 0),
                member.created_at,
                member.id,
            ),
            reverse=True,
        )
        durations = [float(member.payload["duration_seconds"]) for member in members[:limit_samples]]
        estimates.append({
            "nodeid": nodeid,
            "samples": len(durations),
            "median_seconds": median(durations),
            "maximum_seconds": max(durations),
        })
    estimates.sort(key=lambda item: item["nodeid"])
    return {
        "ok": True,
        "repository": repository,
        "estimates": estimates,
        "missing": len(wanted_set - set(histories)),
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
    medians = [float(median(samples)) for samples in recent.values()]
    minima = [min(samples) for samples in recent.values()]
    maxima = [max(samples) for samples in recent.values()]
    total = sum(medians)
    effective_parallelism = min(parallelism, len(by_node)) if by_node else 1

    def modeled_wall_time(values: list[float]) -> float | None:
        if not values:
            return None
        return max(max(values), sum(values) / effective_parallelism)
    known = {
        selector for selector in selectors
        if any(_selector_matches(nodeid, selector) for nodeid in by_node)
    }
    unknown = [selector for selector in selectors if selector not in known]
    open_ended = [
        selector for selector in selectors
        if selector in known and not any(nodeid == selector for nodeid in by_node)
    ]
    return {
        "ok": True,
        "repository": repository,
        "selectors": selectors,
        "estimated_seconds": modeled_wall_time(medians),
        "estimated_low_seconds": modeled_wall_time(minima),
        "estimated_high_seconds": modeled_wall_time(maxima),
        "serial_seconds": total if by_node else None,
        "parallelism": parallelism,
        "effective_parallelism": effective_parallelism,
        "sampled_tests": len(by_node),
        "sample_count": sum(len(samples) for samples in recent.values()),
        "known_selector_count": len(known),
        "requested_selector_count": len(selectors),
        "selector_history_coverage": len(known) / len(selectors),
        "unknown_selectors": unknown,
        "open_ended_selectors": open_ended,
        "estimate_complete": not unknown and not open_ended and bool(by_node),
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
    estimate_ratios = [
        float(member.payload["duration_seconds"]) / float(member.payload["estimated_seconds"])
        for member in runs
        if member.payload.get("estimate_complete") is True
        and float(member.payload.get("estimated_seconds") or 0) > 0
    ]
    estimate_quality = {
        "samples": len(estimate_ratios),
        "median_actual_to_estimate": median(estimate_ratios) if estimate_ratios else None,
        "within_factor_2": (
            sum(0.5 <= ratio <= 2 for ratio in estimate_ratios) / len(estimate_ratios)
            if estimate_ratios else None
        ),
        "underestimated": sum(ratio > 1 for ratio in estimate_ratios),
        "overestimated": sum(ratio < 1 for ratio in estimate_ratios),
    }

    observations = _members(OBSERVATION_SET_ID, org)
    if repository:
        observations = [
            member for member in observations
            if member.payload.get("repository") == repository
        ]
    by_node: dict[tuple[str, str], list[Any]] = {}
    for member in observations:
        identity = (
            str(member.payload.get("repository") or ""),
            str(member.payload.get("nodeid") or ""),
        )
        by_node.setdefault(identity, []).append(member)
    ranked = []
    for (node_repository, nodeid), history in by_node.items():
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
            "repository": node_repository,
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
            "session_title": _session_title(payload["session"]),
            "status": payload["status"],
            "mode": payload["mode"],
            "duration_seconds": payload["duration_seconds"],
            "created_at": payload["created_at"],
            "finished_at": payload["finished_at"],
            "selector_count": len(payload.get("selectors") or []),
            "selector_preview": [
                str(value) for value in (payload.get("selectors") or [])[:3]
            ],
            "collected": payload["collected"],
            "passed": payload["passed"],
            "failed": payload["failed"],
            "errors": payload["errors"],
            "skipped": payload["skipped"],
            "parallelism": payload.get("parallelism", 1),
            "rerun_of": payload.get("rerun_of") or None,
            "estimated_seconds": float(payload.get("estimated_seconds") or 0) or None,
            "estimated_low_seconds": float(payload.get("estimated_low_seconds") or 0) or None,
            "estimated_high_seconds": float(payload.get("estimated_high_seconds") or 0) or None,
            "estimate_complete": bool(payload.get("estimate_complete", False)),
            "estimate_sampled_tests": int(payload.get("estimate_sampled_tests") or 0),
            "hang_detected": bool(payload.get("hang_detected", False)),
            "hung_nodeid": str(payload.get("hung_nodeid") or ""),
            "hang_reason": str(payload.get("hang_reason") or ""),
            "hang_elapsed_seconds": float(payload.get("hang_elapsed_seconds") or 0),
            "hang_threshold_seconds": float(payload.get("hang_threshold_seconds") or 0),
        })
    telemetry = telemetry_status(org)
    operational_errors = error_status(org)
    for item in operational_errors["recent"]:
        item["session_title"] = _session_title(str(item["session"]))
    activity = agent_test_leases.activity_snapshot(org)
    telemetry_counts = telemetry.get("counts") or {}
    terminal_events = sum(
        int(amount)
        for name, amount in telemetry_counts.items()
        if name.startswith("run_") and name != "run_started"
    )
    activity["unresolved_starts"] = max(
        0, int(telemetry_counts.get("run_started", 0)) - terminal_events,
    )
    if repository:
        activity["running"] = [
            item for item in activity["running"]
            if item.get("repository") == repository
        ]
        activity["queue"] = [
            item for item in activity["queue"]
            if item.get("repository") == repository
        ]
    for state in ("running", "queue"):
        for item in activity[state]:
            item["session_title"] = _session_title(str(item["session"]))

    activity["active_runs"] = len(activity["running"])
    activity["queued_runs"] = len(activity["queue"])
    activity["active_sessions"] = len({item["session"] for item in activity["running"]})
    activity["queued_sessions"] = len({item["session"] for item in activity["queue"]})
    activity["used"] = {
        name: sum(int(item["resources"].get(name, 0)) for item in activity["running"])
        for name in activity["limits"]
    }
    activity["queued"] = {
        name: sum(int(item["resources"].get(name, 0)) for item in activity["queue"])
        for name in activity["limits"]
    }
    activity["available"] = {
        name: max(0, limit - int(activity["used"].get(name, 0)))
        for name, limit in activity["limits"].items()
    }
    by_session: dict[str, dict[str, Any]] = {}
    for state in ("running", "queue"):
        for item in activity[state]:
            group = by_session.setdefault(
                str(item["session"]),
                {
                    "session": item["session"],
                    "session_title": item["session_title"],
                    "running": [],
                    "queue": [],
                },
            )
            group[state].append(item)
    activity["sessions"] = sorted(
        by_session.values(),
        key=lambda item: (
            not bool(item["running"]),
            min(
                [row.get("acquired_at", float("inf")) for row in item["running"]]
                + [row.get("requested_at", float("inf")) for row in item["queue"]]
            ),
            item["session"],
        ),
    )

    recent_minutes = 10
    recent_cutoff = time.time() - recent_minutes * 60
    recent_members = [
        member for member in runs
        if _iso_timestamp(member.payload.get("finished_at")) >= recent_cutoff
    ]
    just_finished = [
        item for item in recent_runs
        if _iso_timestamp(item.get("finished_at")) >= recent_cutoff
    ]
    activity["recent_window_minutes"] = recent_minutes
    activity["recent_runs"] = len(recent_members)
    activity["recent_tests"] = sum(
        int(member.payload.get("collected") or 0) for member in recent_members
    )
    activity["recent_sessions"] = len({
        str(member.payload.get("session") or "") for member in recent_members
    })
    activity["just_finished"] = just_finished
    history_by_session: dict[str, dict[str, Any]] = {}
    for item in recent_runs:
        session = str(item["session"])
        group = history_by_session.setdefault(
            session,
            {
                "session": session,
                "session_title": item["session_title"],
                "runs": [],
                "tests": 0,
                "passed_runs": 0,
                "failed_runs": 0,
            },
        )
        group["runs"].append(item)
        group["tests"] += int(item.get("collected") or 0)
        group["passed_runs"] += int(item.get("status") == "passed")
        group["failed_runs"] += int(item.get("status") in {"failed", "error"})
    activity["session_history"] = sorted(
        history_by_session.values(),
        key=lambda item: (
            str(item["runs"][0].get("finished_at") or ""), item["session"]
        ),
        reverse=True,
    )
    return {
        "ok": True,
        "organization": org,
        "repository": repository or None,
        "repositories": repositories[:100],
        "repository_count": len(repositories),
        "runs": {
            "total": len(runs),
            "status_counts": status_counts,
            "hang_count": sum(bool(member.payload.get("hang_detected")) for member in runs),
            "pass_ratio": pass_ratio,
            "estimate_quality": estimate_quality,
            "test_totals": totals,
        },
        "tests": {
            "observed": len(by_node),
            "observation_rows": len(observations),
            "maximum_rows_for_observed_tests": len(by_node) * MAX_OBSERVATIONS_PER_TEST,
            "average_samples_per_test": len(observations) / len(by_node) if by_node else 0.0,
            "sample_depths": {
                str(depth): sum(len(history) == depth for history in by_node.values())
                for depth in range(1, MAX_OBSERVATIONS_PER_TEST + 1)
                if any(len(history) == depth for history in by_node.values())
            },
            "flaky": flaky,
            "slow": slow,
            "ranked_limit": ranked_limit,
            "history_limit": MAX_OBSERVATIONS_PER_TEST,
        },
        "retention": {
            "timing_observations_per_test": MAX_OBSERVATIONS_PER_TEST,
            "terminal_runs_per_repository": MAX_RUNS_PER_REPOSITORY,
            "usage_events_per_organization": MAX_USAGE_EVENTS,
            "operational_errors_per_organization": MAX_ERROR_EVENTS,
        },
        "recent_runs": recent_runs,
        "activity": activity,
        "recent_limit": recent_limit,
        "telemetry": telemetry,
        "operational_errors": operational_errors,
    }
