"""Schema: ``dashboard.capability.host_install_state#1``.

The canonical record of *what a capability's host-install actually did on
this host right now* — resolved state that complements the *intent*
declared in the repo (the ``host_install`` block on
``autonomy.capability.impl#2``). One row per implementation that declares
``host_install``, keyed by ``<implementation_name>`` (e.g.
``autonomy/video``).

Written by the host-install runner (``graph capability host-install`` and
the host-side substrate-maintenance triggers). Read by operators via
``graph set members dashboard.capability.host_install_state`` and by the
dashboard's "Capability installs" panel.

Contract: Capability Host-Install Runner protocol — graph://149705db-a39
§ "State Setting".
"""

from __future__ import annotations

from typing import Any

from .registry import (
    publication_band,
    home,
    home,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


SET_ID = "dashboard.capability.host_install_state"
SCHEMA_REVISION = 1

VALID_STATES = ("ready", "running", "failed", "unknown")

SYNOPSIS = {
    "summary": (
        "Host-install runner state for one capability implementation. One "
        "row per impl declaring host_install, keyed by implementation name; "
        "records fingerprint, timestamps, exit code, and truncated logs."
    ),
    "nouns": [
        "host install",
        "capability install",
        "install state",
        "fingerprint",
        "install runner",
    ],
    "related_set_ids": [
        "autonomy.capability.impl#2",
    ],
}


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="raw")
@home("organization")
@keyed_per_entity(key_strategy="implementation_name")
class HostInstallStateV1(SettingSchema):
    """Resolved host-install state for one capability implementation.

    Required: ``state`` (one of :data:`VALID_STATES`). Everything else is
    optional — a brand-new impl the runner has only just encountered may
    carry nothing but ``state=running``; a ``failed`` row deliberately
    preserves the ``last_fingerprint`` of the last *successful* install so
    the next run retries the same install rather than treating the failure
    as current.
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    state: str = field(
        required=True,
        enum=list(VALID_STATES),
        description="Lifecycle state of the most recent install attempt",
    )
    last_fingerprint: str = field(
        required=False,
        description=(
            "Content hash of fingerprint_files from the last SUCCESSFUL "
            "install (preserved across failures)"
        ),
    )
    last_attempted_at: str = field(
        required=False,
        description="ISO timestamp: start of the most recent run",
    )
    last_succeeded_at: str = field(
        required=False,
        description="ISO timestamp: end of the most recent successful run",
    )
    last_exit_code: int = field(
        required=False,
        description="Exit code of the most recent run (-1 for timeout)",
    )
    stdout_tail: str = field(
        required=False,
        description="Last 4 KB of stdout from the most recent run",
    )
    stderr_tail: str = field(
        required=False,
        description="Last 4 KB of stderr from the most recent run",
    )
    runner_version: str = field(
        required=False,
        description="Runner's own version string, for forensics",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        state = payload.get("state")
        if state not in VALID_STATES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'state' is required and must be one of "
                f"{VALID_STATES}, got {state!r}"
            )
        for key in (
            "last_fingerprint",
            "last_attempted_at",
            "last_succeeded_at",
            "stdout_tail",
            "stderr_tail",
            "runner_version",
        ):
            if key in payload and not isinstance(payload[key], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a string, got "
                    f"{type(payload[key]).__name__}"
                )
        if "last_exit_code" in payload:
            code = payload["last_exit_code"]
            if isinstance(code, bool) or not isinstance(code, int):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'last_exit_code' must be an integer, "
                    f"got {type(code).__name__}"
                )
