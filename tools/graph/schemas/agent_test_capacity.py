"""Machine-local capacity and live-lease schemas for Agent Test."""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


CAPACITY_SET_ID = "dashboard.agent-test.capacity"
LEASE_SET_ID = "dashboard.agent-test.lease"
QUEUE_SET_ID = "dashboard.agent-test.queue"
SCHEMA_REVISION = 1
_RESOURCE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _validate_resources(owner: str, value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise SchemaValidationError(f"{owner}: resources must be a non-empty object")
    for name, amount in value.items():
        if not isinstance(name, str) or not _RESOURCE_RE.fullmatch(name):
            raise SchemaValidationError(f"{owner}: invalid resource name {name!r}")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > 4096:
            raise SchemaValidationError(
                f"{owner}: resource {name!r} must be an integer from 1 to 4096"
            )


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="capacity_scope")
class AgentTestCapacityV1(SettingSchema):
    set_id = CAPACITY_SET_ID
    schema_revision = SCHEMA_REVISION

    limits: dict = field(required=True, description="Maximum simultaneous resource slots on this machine.")
    lease_ttl_seconds: int = field(required=True, description="Seconds before a lease without renewal is reclaimed.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        extra = set(payload) - {"limits", "lease_ttl_seconds"}
        if extra:
            raise SchemaValidationError(f"{cls.__name__}: unknown fields {sorted(extra)!r}")
        _validate_resources(cls.__name__, payload.get("limits"))
        ttl = payload.get("lease_ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 30 <= ttl <= 3600:
            raise SchemaValidationError(
                f"{cls.__name__}: lease_ttl_seconds must be from 30 to 3600"
            )


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="lease_id")
class AgentTestLeaseV1(SettingSchema):
    set_id = LEASE_SET_ID
    schema_revision = SCHEMA_REVISION

    session: str = field(required=True, description="Session that owns this machine-local lease.")
    run_id: str = field(required=True, description="Agent Test run that owns this lease.")
    resources: dict = field(required=True, description="Resource slots held by this run.")
    acquired_at: float = field(required=True, description="Unix timestamp when the lease was first granted.")
    expires_at: float = field(required=True, description="Unix timestamp after which a missing renewal is reclaimed.")
    organization: str = field(required=False, description="Organization that owns the run metadata.")
    repository: str = field(required=False, description="Repository identity reported by Agent Test.")
    selectors: list = field(required=False, description="Bounded selector preview for the live activity surface.")
    selector_count: int = field(required=False, description="Total selectors requested by the run.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        required = {"session", "run_id", "resources", "acquired_at", "expires_at"}
        optional = {"organization", "repository", "selectors", "selector_count"}
        extra = set(payload) - required - optional
        missing = required - set(payload)
        if extra or missing:
            raise SchemaValidationError(
                f"{cls.__name__}: missing={sorted(missing)!r} unknown={sorted(extra)!r}"
            )
        if not isinstance(payload["session"], str) or not payload["session"]:
            raise SchemaValidationError(f"{cls.__name__}: session must be non-empty")
        if not isinstance(payload["run_id"], str) or not payload["run_id"]:
            raise SchemaValidationError(f"{cls.__name__}: run_id must be non-empty")
        _validate_resources(cls.__name__, payload["resources"])
        for name in ("acquired_at", "expires_at"):
            if isinstance(payload[name], bool) or not isinstance(payload[name], (int, float)):
                raise SchemaValidationError(f"{cls.__name__}: {name} must be numeric")
        _validate_activity_context(cls.__name__, payload)


def _validate_activity_context(owner: str, payload: dict[str, Any]) -> None:
    for name, limit in (("organization", 100), ("repository", 1000)):
        value = payload.get(name)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > limit
        ):
            raise SchemaValidationError(
                f"{owner}: {name} must be a non-empty string up to {limit} characters"
            )
    selectors = payload.get("selectors")
    if selectors is not None and (
        not isinstance(selectors, list)
        or len(selectors) > 5
        or any(not isinstance(value, str) or not value or len(value) > 1000 for value in selectors)
    ):
        raise SchemaValidationError(
            f"{owner}: selectors must contain at most five non-empty strings"
        )
    selector_count = payload.get("selector_count")
    if selector_count is not None and (
        isinstance(selector_count, bool)
        or not isinstance(selector_count, int)
        or selector_count < len(selectors or [])
        or selector_count > 100_000
    ):
        raise SchemaValidationError(
            f"{owner}: selector_count must cover the preview and be at most 100000"
        )


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="lease_id")
class AgentTestQueueV1(SettingSchema):
    """One renewable pending capacity request, removed on grant or release."""

    set_id = QUEUE_SET_ID
    schema_revision = SCHEMA_REVISION

    organization: str = field(required=True, description="Organization that owns the pending run.")
    session: str = field(required=True, description="Session waiting for machine capacity.")
    run_id: str = field(required=True, description="Agent Test run waiting for capacity.")
    repository: str = field(required=True, description="Repository identity reported by Agent Test.")
    selectors: list = field(required=True, description="Bounded selector preview for the activity surface.")
    selector_count: int = field(required=True, description="Total selectors requested by the run.")
    resources: dict = field(required=True, description="Resource slots requested by this run.")
    requested_at: float = field(required=True, description="Unix timestamp of the first capacity request.")
    updated_at: float = field(required=True, description="Unix timestamp of the latest capacity request.")
    expires_at: float = field(required=True, description="Unix timestamp after which the pending request is stale.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        required = {
            "organization", "session", "run_id", "repository", "selectors",
            "selector_count", "resources", "requested_at", "updated_at", "expires_at",
        }
        extra = set(payload) - required
        missing = required - set(payload)
        if extra or missing:
            raise SchemaValidationError(
                f"{cls.__name__}: missing={sorted(missing)!r} unknown={sorted(extra)!r}"
            )
        for name in ("session", "run_id"):
            if not isinstance(payload[name], str) or not payload[name] or len(payload[name]) > 200:
                raise SchemaValidationError(
                    f"{cls.__name__}: {name} must be a non-empty string up to 200 characters"
                )
        _validate_resources(cls.__name__, payload["resources"])
        _validate_activity_context(cls.__name__, payload)
        for name in ("requested_at", "updated_at", "expires_at"):
            if isinstance(payload[name], bool) or not isinstance(payload[name], (int, float)):
                raise SchemaValidationError(f"{cls.__name__}: {name} must be numeric")
