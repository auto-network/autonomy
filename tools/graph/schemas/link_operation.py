"""``autonomy.machine.link-operation#1`` — THIS machine's once-only record of
each approved share-link publish or revoke it carried out (auto-fkhq0.10a).

A publish or revoke runs once, on the machine that accepted the request, over
the org's serving tunnel. Two initiators share this record:

- an approved Central request (``initiator`` ``approval:<id>``, keyed by the
  approval id), and
- the operator in their own browser (``initiator`` ``operator``, keyed by a
  server-generated operation id): the row is first ``prepared`` with the
  frozen request the review dialog shows and the operator signs.

The row is written ``claimed`` before the tunnel frame, recording the grant id
the publish writes first (O-C), and completed ``done`` with the executor's
result or ``failed`` with its error. A claim
left by a stopped process is resolved when it is next read, from that grant
row: a published grant means the publish landed; an unpublished one is
cleaned up and the publish failed; none means it failed. Nothing is resent.

Why machine-homed: the result carries the share URL with its viewer key
fragment, the same value the legacy approvals.db row held on this machine
only. Key: the Central approval id, or the operator operation id.
"""
from __future__ import annotations

from .registry import SettingSchema, field, home, keyed_per_entity, publication_band

LINK_OPERATION_SET_ID = "autonomy.machine.link-operation"
LINK_OPERATION_REVISION = 1
OPERATION_STATES = ("prepared", "claimed", "done", "failed")

SYNOPSIS = {
    "summary": (
        "This machine's once-only record of a share-link publish or revoke, "
        "approved in Central or initiated by the operator: prepared, claimed "
        "before the tunnel frame (with the grant id written first), then done "
        "with the result or failed. Machine-homed, keyed by the approval id or "
        "a server-generated operation id."
    ),
    "nouns": ["link operation", "share link publish", "share link revoke"],
    "related_set_ids": [
        "autonomy.central.approval-request#1",
        "autonomy.network.link-grant#6",
    ],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="approval_id_or_operation_id")
class LinkOperationV1(SettingSchema):
    """One approved link operation's outcome on this machine; see the module."""

    set_id = LINK_OPERATION_SET_ID
    schema_revision = LINK_OPERATION_REVISION

    state: str = field(required=True, enum=list(OPERATION_STATES),
                       description="prepared (operator-initiated, awaiting the signature) | "
                                   "claimed (before the tunnel frame) | done | failed")
    op: str = field(required=True, enum=["publish", "revoke"],
                    description="Which operation this carries out.")
    initiator: str = field(required=True,
                           description="'operator' (the operator's own browser) or 'approval:<id>'.")
    prepared_at: float = field(required=True,
                               description="Epoch seconds the frozen request was prepared or granted.")
    request: dict = field(required=True, description="The validated request the operation carries out.")
    staged: dict = field(required=True,
                         description="The frozen registry request {method, path, registry_url, payload, binding}.")
    persona_pub: str = field(required=False, description="The signing persona, once verified.")
    owner_pid: int = field(required=False, description="The process that holds the claim.")
    owner_start: str = field(required=False,
                             description="That process's kernel birth identity (boot id + start time).")
    claimed_at: float = field(required=False, description="Epoch seconds the operation was claimed.")
    grant_id: str = field(
        required=False,
        description="The grant row a publish writes before create-link (O-C), or the revoked token's grant.",
    )
    finished_at: float = field(required=False, description="Epoch seconds the outcome was recorded.")
    execution: dict = field(
        required=False,
        description="The executor's result: {ok, url, token, channel_pub, ...} or {ok: false, error}.",
    )
