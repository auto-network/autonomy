"""The operator's software-update preference (graph://89d3c8df-544 §6).

Two choices, set once at onboarding and changeable later (operator decision
2026-09-27):

- ``auto_check`` — "Automatically check for and notify me of updates": the
  dashboard fetches origin every ``interval_minutes`` and the profile menu
  shows the update when one exists. Off: nothing reaches GitHub until the
  operator clicks "Check for updates".
- ``auto_install`` — "Automatically install updates when available", offered
  only while ``auto_check`` is on: an available fast-forward is applied on the
  next check (pull latest code; the dashboard hot-reloads and sessions keep
  running). It never applies where ``software_update.perform_update`` refuses
  (a dirty tree, or an author machine ahead of origin).

One personal row, key ``__default__``. With no row the defaults below apply.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)

logger = logging.getLogger(__name__)

SOFTWARE_UPDATE_PREFERENCE_SET_ID = "software_update.preference"
SCHEMA_REVISION = 1
PREFERENCE_KEY = "__default__"
PREFERENCE_ORG = "personal"

DEFAULTS: dict[str, Any] = {"auto_check": True, "auto_install": False, "interval_minutes": 360}
MIN_INTERVAL_MINUTES = 30


@home("personal")
@publication_band(max="raw")
@singleton(key=PREFERENCE_KEY)
class SoftwareUpdatePreferenceV1(SettingSchema):
    """Whether this operator's dashboards check for, and install, updates."""

    set_id = SOFTWARE_UPDATE_PREFERENCE_SET_ID
    schema_revision = SCHEMA_REVISION

    auto_check: bool = field(default=True, description="Check origin on a schedule and notify when an update exists.")
    auto_install: bool = field(default=False, description="Apply an available fast-forward on the next check (only while auto_check).")
    interval_minutes: int = field(default=360, description=f"Minutes between checks; at least {MIN_INTERVAL_MINUTES}.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            raise SchemaValidationError(f"{cls.__name__}: payload must be a dict")
        for name in ("auto_check", "auto_install"):
            if name in payload and not isinstance(payload[name], bool):
                raise SchemaValidationError(f"{cls.__name__}: {name!r} must be a boolean")
        if "interval_minutes" in payload:
            value = payload["interval_minutes"]
            if isinstance(value, bool) or not isinstance(value, int) or value < MIN_INTERVAL_MINUTES:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'interval_minutes' must be an integer >= {MIN_INTERVAL_MINUTES}"
                )


def resolve(payload: Any) -> dict[str, Any]:
    """The effective preference: stored fields over the defaults. Auto-install
    without auto-check is not a state the operator can choose, so it reads off."""
    out = dict(DEFAULTS)
    if isinstance(payload, dict):
        out.update({k: payload[k] for k in DEFAULTS if k in payload})
    if not out["auto_check"]:
        out["auto_install"] = False
    return out


def read_preference() -> dict[str, Any]:
    """The operator's preference, or the defaults when unset or unreadable."""
    try:
        from tools.graph.settings_ops import read_set_key

        row = read_set_key(
            SOFTWARE_UPDATE_PREFERENCE_SET_ID, PREFERENCE_KEY, org=PREFERENCE_ORG, peers=[],
        )
    except Exception:
        logger.debug("software_update: preference read failed; using defaults", exc_info=True)
        return resolve(None)
    return resolve(getattr(row, "payload", None) if row is not None else None)


def write_preference(changes: dict[str, Any]) -> dict[str, Any]:
    """Merge *changes* into the stored preference and persist it."""
    from tools.graph.settings_ops import upsert_by_key

    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise SchemaValidationError(f"unknown preference field(s): {sorted(unknown)}")
    merged = resolve({**read_preference(), **changes})
    SoftwareUpdatePreferenceV1.validate(merged)
    upsert_by_key(
        SOFTWARE_UPDATE_PREFERENCE_SET_ID, SCHEMA_REVISION, PREFERENCE_KEY, merged,
        org=PREFERENCE_ORG,
    )
    return merged
