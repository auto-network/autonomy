"""``autonomy.machine.mailbox-send#1`` — THIS machine's record of each email it
sent for an approved ``email_send`` request.

The Central approval (``autonomy.central.approval-request`` / ``-resolution``)
is personal Settings truth and replicates to every personal machine. Only the
machine that accepted the request sends the email (the request's frozen
``result_destination_id``), and this row makes that send happen at most once:
it is written as ``claimed`` before the SMTP call and completed as ``sent`` or
``failed`` after it. A ``claimed`` row found later means the process stopped
mid-send; it becomes ``unknown`` and is never retried, because resending an
email that may already have left is worse than reporting the uncertainty.

Why machine-homed: the row records a side effect of this machine's broker;
another machine never sends for this request and must not see a claim it did
not make. Key: the Central approval id.
"""
from __future__ import annotations

from .registry import SettingSchema, field, home, keyed_per_entity, publication_band

MAILBOX_SEND_SET_ID = "autonomy.machine.mailbox-send"
MAILBOX_SEND_REVISION = 1
SEND_STATES = ("claimed", "sent", "failed", "unknown")

SYNOPSIS = {
    "summary": (
        "This machine's at-most-once record of an email sent for an approved "
        "email_send request: claimed before SMTP, then sent, failed or unknown. "
        "Machine-homed, keyed by the Central approval id."
    ),
    "nouns": ["mailbox send", "email send", "sent email", "send journal"],
    "related_set_ids": [
        "autonomy.central.approval-request#1",
        "autonomy.org.capability.install#1",
    ],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="approval_id")
class MailboxSendV1(SettingSchema):
    """One approved email's send outcome on this machine; see the module."""

    set_id = MAILBOX_SEND_SET_ID
    schema_revision = MAILBOX_SEND_REVISION

    state: str = field(required=True, enum=list(SEND_STATES),
                       description="claimed (before SMTP) | sent | failed | unknown")
    claimed_at: float = field(required=True, description="Epoch seconds the send was claimed.")
    finished_at: float = field(required=False, description="Epoch seconds the outcome was recorded.")
    message_id: str = field(required=False, description="The sent message's Message-ID.")
    error: str = field(required=False, description="Why sending failed, or why the outcome is unknown.")
