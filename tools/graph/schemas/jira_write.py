"""``autonomy.machine.jira-write#1`` — THIS machine's record of each Jira write
it performed for an approved ``jira_write`` request (auto-fkhq0.9).

The Central approval replicates to every personal machine; only the machine
that accepted the request writes to Jira (its frozen ``result_destination_id``),
and this row makes the write happen once: it is written ``claimed`` before the
Jira call and completed ``done`` or ``failed`` after it. A ``claimed`` row found
later, whose owning process (``owner_pid`` + ``owner_start``) is gone, means
that process stopped mid-call; what happens then depends on the op
(tools/dashboard/jira_central.py): a value-setting op is re-applied once
(``reapplied_after_restart``), a transition and a tagged comment/create are
reconciled against Jira, and an attachment becomes ``unknown`` and is never
retried.

Machine-homed: the row records a side effect of this machine's broker. Key:
the Central approval id.
"""
from __future__ import annotations

from .registry import SettingSchema, field, home, keyed_per_entity, publication_band

JIRA_WRITE_SET_ID = "autonomy.machine.jira-write"
JIRA_WRITE_REVISION = 1
WRITE_STATES = ("claimed", "done", "failed", "unknown")

SYNOPSIS = {
    "summary": (
        "This machine's once-only record of a Jira write performed for an "
        "approved jira_write request: claimed before the Jira call, then done, "
        "failed or unknown. Machine-homed, keyed by the Central approval id."
    ),
    "nouns": ["jira write", "jira journal", "jira execution"],
    "related_set_ids": ["autonomy.central.approval-request#1"],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="approval_id")
class JiraWriteV1(SettingSchema):
    """One approved Jira write's outcome on this machine; see the module."""

    set_id = JIRA_WRITE_SET_ID
    schema_revision = JIRA_WRITE_REVISION

    state: str = field(required=True, enum=list(WRITE_STATES),
                       description="claimed (before the Jira call) | done | failed | unknown")
    claimed_at: float = field(required=True, description="Epoch seconds the write was claimed.")
    finished_at: float = field(required=False, description="Epoch seconds the outcome was recorded.")
    result: str = field(required=False, description="The Jira result, as JSON text.")
    error: str = field(required=False, description="Why the write failed, or why it is unknown.")
    reapplied_after_restart: bool = field(
        required=False, description="A value-setting op re-applied after an interrupted claim.")
    owner_pid: int = field(required=False, description="The claiming process's pid.")
    owner_start: str = field(
        required=False,
        description="The claiming process's boot id and start time: with owner_pid, "
                    "what makes a claim interrupted only once that process is gone.")
    target_status: str = field(
        required=False, description="A transition's destination status, resolved before the call.")
