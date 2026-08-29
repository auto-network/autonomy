"""``autonomy.dispatch.limits#1`` — operator-tunable dispatch concurrency.

Two independent limits, both enforced where their launches actually
happen: ``bead_max_concurrent`` gates the dispatcher's own bead-agent
launch phase (the ``--max-concurrent`` CLI value remains the fallback
when no row exists), and ``agentic_max_concurrent`` gates
``POST /api/agent-actions/dispatch`` (the entry point where agentic
containers spawn — nothing else caps them; 2026-08-28 incident,
handoff 148ead24 item 2).

Machine-homed: how many concurrent containers this host can absorb is a
fact about THIS computer's capacity — a second machine of the same
operator wants its own value. Written by the dashboard's dispatch page,
read by the dashboard process and the dispatcher each cycle.
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    publication_band,
    singleton,
)


SET_ID = "autonomy.dispatch.limits"
SCHEMA_REVISION = 1

DEFAULT_BEAD_MAX_CONCURRENT = 2
DEFAULT_AGENTIC_MAX_CONCURRENT = 10
_LIMIT_CEILING = 64


SYNOPSIS = {
    "summary": (
        "Operator-tunable dispatch concurrency: bead-agent launches and "
        "agentic (agent-action) launches, per machine"
    ),
    "nouns": [
        "dispatch limit", "max concurrent", "agentic cap",
        "concurrency", "throttle",
    ],
    "related_set_ids": [],
}


@publication_band(min="raw", max="curated")
@home("machine")
@singleton(key="default")
class DispatchLimitsV1(SettingSchema):
    """Shape of an ``autonomy.dispatch.limits#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "bead_max_concurrent": {
            "type": "integer",
            "description": (
                "Concurrent bead-agent launches the dispatcher may run. "
                "0 pauses bead launches without touching the queue-pause "
                "toggles."
            ),
            "default": DEFAULT_BEAD_MAX_CONCURRENT,
        },
        "agentic_max_concurrent": {
            "type": "integer",
            "description": (
                "Concurrent agentic (agent-action) runs. Excess dispatches "
                "are never rejected: they queue as QUEUED rows (visible in "
                "the dispatch page's approved-waiting section) and launch "
                "oldest-first as running slots free. 0 pauses agentic "
                "launching entirely (everything queues)."
            ),
            "default": DEFAULT_AGENTIC_MAX_CONCURRENT,
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        for name in ("bead_max_concurrent", "agentic_max_concurrent"):
            value = payload.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not (0 <= value <= _LIMIT_CEILING):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name!r} must be an integer in "
                    f"0..{_LIMIT_CEILING}, got {value!r}"
                )
