"""Organization-owned Settings contracts for Agent Test history."""
from __future__ import annotations

import math
import re
from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


RUN_SET_ID = "dashboard.testing.run"
OBSERVATION_SET_ID = "dashboard.testing.test-observation"
TELEMETRY_SET_ID = "dashboard.testing.telemetry"
SCHEMA_REVISION = 1
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RUN_STATUSES = {"passed", "failed", "error", "stopped", "collected"}
_OUTCOMES = {"passed", "failed", "error", "skipped"}

SYNOPSIS = {
    "summary": (
        "Organization-local Agent Test run summaries, bounded per-test "
        "outcome and duration observations, and session usage counters."
    ),
    "nouns": [
        "testing", "agent test", "test run", "test duration",
        "flaky test", "test telemetry",
    ],
    "related_set_ids": [],
}


def _required_text(payload: dict, name: str, maximum: int) -> None:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SchemaValidationError(
            f"{name} must be a non-empty string no longer than {maximum}"
        )


def _non_negative_number(payload: dict, name: str, maximum: float) -> None:
    value = payload.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > maximum
    ):
        raise SchemaValidationError(f"{name} must be from 0 to {maximum:g}")


@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="run_id")
class AgentTestRunV1(SettingSchema):
    """One immutable completed Agent Test run, keyed by run id."""

    set_id = RUN_SET_ID
    schema_revision = SCHEMA_REVISION

    repository: str = field(required=True, description="Stable credential-free repository identity.")
    session: str = field(required=True, description="Agent session that requested the run.")
    status: str = field(required=True, description="Terminal run status.")
    mode: str = field(required=True, description="Execution mode: run or collect.")
    duration_seconds: float = field(required=True, description="Wall time spent executing the run.")
    created_at: str = field(required=True, description="ISO-8601 run creation time.")
    finished_at: str = field(required=True, description="ISO-8601 terminal time.")
    selectors: list = field(
        required=True,
        element=str,
        description="Explicit selectors requested for this run.",
    )
    collected: int = field(required=True, description="Number of test nodes collected.")
    passed: int = field(required=True, description="Number of test nodes that passed.")
    failed: int = field(required=True, description="Number of call-phase failures.")
    errors: int = field(required=True, description="Number of setup or teardown errors.")
    skipped: int = field(required=True, description="Number of skipped test nodes.")
    new_failures: int = field(default=0, description="Failures not present in retained baselines.")
    known_failures: int = field(default=0, description="Failures seen in prior retained runs.")
    quarantined_failures: int = field(default=0, description="Failures matching the repository quarantine.")
    parallelism: int = field(default=1, description="Configured pytest worker parallelism.")
    agent_test_version: str = field(default="", description="Agent Test version that produced the run.")
    fingerprint: str = field(default="", description="Code and selector fingerprint used for repeat refusal.")
    rerun_of: str = field(default="", description="Prior run whose retained failures selected this run.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _required_text(payload, "repository", 1000)
        _required_text(payload, "session", 200)
        _required_text(payload, "status", 32)
        _required_text(payload, "mode", 32)
        _required_text(payload, "created_at", 100)
        _required_text(payload, "finished_at", 100)
        if payload["status"] not in _RUN_STATUSES:
            raise SchemaValidationError("status is not a terminal Agent Test status")
        if payload["mode"] not in {"run", "collect"}:
            raise SchemaValidationError("mode must be run or collect")
        _non_negative_number(payload, "duration_seconds", 7 * 24 * 3600)
        selectors = payload.get("selectors")
        if (
            not isinstance(selectors, list)
            or len(selectors) > 200
            or not all(isinstance(item, str) and item and len(item) <= 4000 for item in selectors)
        ):
            raise SchemaValidationError("selectors must contain at most 200 bounded strings")
        for name in (
            "collected", "passed", "failed", "errors", "skipped",
            "new_failures", "known_failures", "quarantined_failures",
        ):
            value = payload.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SchemaValidationError(f"{name} must be a non-negative integer")
        parallelism = payload.get("parallelism", 1)
        if isinstance(parallelism, bool) or not isinstance(parallelism, int) or not 1 <= parallelism <= 256:
            raise SchemaValidationError("parallelism must be from 1 to 256")


@home("organization")
@publication_band(max="raw")
@append_only_log
class AgentTestObservationV1(SettingSchema):
    """One immutable test-node outcome and timing observation."""

    set_id = OBSERVATION_SET_ID
    schema_revision = SCHEMA_REVISION

    repository: str = field(required=True, description="Stable credential-free repository identity.")
    run_id: str = field(
        required=True,
        references=RUN_SET_ID,
        description="Completed Agent Test run that produced this observation.",
    )
    nodeid: str = field(required=True, description="Fully-qualified pytest node id.")
    duration_seconds: float = field(required=True, description="Setup, call, and teardown time combined.")
    outcome: str = field(required=True, description="Terminal outcome for this test node.")
    recorded_at: float = field(required=True, description="Unix timestamp when the observation was recorded.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _required_text(payload, "repository", 1000)
        _required_text(payload, "run_id", 200)
        _required_text(payload, "nodeid", 4000)
        _non_negative_number(payload, "duration_seconds", 7 * 24 * 3600)
        if payload.get("outcome") not in _OUTCOMES:
            raise SchemaValidationError("outcome is invalid")
        _non_negative_number(payload, "recorded_at", 10**11)


@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="session_id")
class AgentTestTelemetryV1(SettingSchema):
    """Cumulative bounded Agent Test event counters for one session."""

    set_id = TELEMETRY_SET_ID
    schema_revision = SCHEMA_REVISION

    counts: dict = field(required=True, description="Cumulative event-name to count mapping.")
    last_event: str = field(required=True, description="Most recent event name.")
    last_at: float = field(required=True, description="Unix time of the most recent event.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        counts = payload.get("counts")
        if not isinstance(counts, dict) or len(counts) > 64:
            raise SchemaValidationError("counts must be an object with at most 64 event names")
        for name, count in counts.items():
            if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
                raise SchemaValidationError(f"invalid event name {name!r}")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise SchemaValidationError("event counts must be non-negative integers")
        if not isinstance(payload.get("last_event"), str) or not _NAME_RE.fullmatch(payload["last_event"]):
            raise SchemaValidationError("last_event is invalid")
        _non_negative_number(payload, "last_at", 10**11)
