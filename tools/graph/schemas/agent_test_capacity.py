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

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        required = {"session", "run_id", "resources", "acquired_at", "expires_at"}
        extra = set(payload) - required
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
