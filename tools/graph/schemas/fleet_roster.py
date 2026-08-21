"""Schema for the fleet roster's per-machine entries (auto-0vpse).

Each member is one personal-root-signed statement about one machine — an
enrol, a kick, or a tombstone-citing re-enrol. The rows live in personal.db
(``@home("personal")``) and are pinned to ``raw`` band
(``@publication_band(max="raw")``): at ``published``/``canonical`` a personal
row is seen by every organization's read on the operator's machines, which
would leak fleet topology into org surfaces. ``raw`` keeps it local, and the
personal-store sync (``auto-q9ic5``) is what carries it across the fleet.

The key is the deterministic ``roster_entry_id`` (the hash of the signed
binding), so every entry is its OWN member — enrol, kick and re-enrol about one
machine all coexist, which is what the OR-set merge in
:mod:`tools.network.fleet_roster` resolves over. The signature and merge
semantics live in that module; this schema only pins the stored shape and the
band.

Revision 2 deliberately has no revision-1 upconverter: the old signature did
not cover ``machine_id`` or standing, so manufacturing those fields during a
read would create unsigned authority. No live roster rows existed when this
revision replaced the pre-enrollment shape.
"""

from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

FLEET_ROSTER_SET_ID = "autonomy.fleet.roster"
FLEET_ROSTER_REVISION = 2

#: ``graph set find`` synopsis. Personal-scoped, not an organization set.
SYNOPSIS = {
    "summary": (
        "The fleet roster — one personal-root-signed row per machine that "
        "holds the operator's personal root (enroll / kick / re-enroll). "
        "Lives in personal.db at raw band; the OR-set merge that resolves it "
        "is tools.network.fleet_roster."
    ),
    "nouns": [
        "fleet", "roster", "fleet machine", "enrollment", "kick",
        "personal root", "machine key", "fleet register",
    ],
    "related_set_ids": [],
}

_HEX64 = "0123456789abcdef"


def _hex(payload: dict, key: str, length: int, cls_name: str) -> str:
    value = payload.get(key)
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(c not in _HEX64 for c in value)
    ):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be exactly {length} lowercase hex chars"
        )
    return value


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="roster_entry_id")
class FleetRosterEntryV2(SettingSchema):
    """One machine's roster statement, personal-root-signed.

    Key strategy ``roster_entry_id``: the key is the entry's own content id
    (``fleet_roster.RosterEntry.entry_id``, the SHA-256 of its signed body).
    The entity a key names is the ENTRY itself — content-addressed, so it
    references no other set — which is why it is not ``natural`` (a
    caller-chosen key that names nothing): every entry has exactly one id,
    and enrol/kick/re-enrol about one machine are distinct entries with
    distinct ids that must all coexist for the OR-set merge to see them.
    """

    set_id = FLEET_ROSTER_SET_ID
    schema_revision = FLEET_ROSTER_REVISION

    personal_root_pub: str = field(
        required=True,
        description="64-hex personal root public key — the fleet anchor and signer.",
    )
    machine_id: str = field(
        required=True,
        description="64-hex durable machine assignment made at fleet approval.",
    )
    machine_pub: str = field(
        required=True,
        description=(
            "64-hex machine AUTHORIZATION (Ed25519 signing) public key. The "
            "machine's X25519 distribution address is a separate record "
            "(auto-pw9bs.6), never this."
        ),
    )
    assignment: str = field(
        required=True,
        enum=["personal_root_holder"],
        description=(
            "Durable fleet standing root-bound with the machine id and public key."
        ),
    )
    kind: str = field(
        required=True, enum=["enroll", "kick"],
        description="enroll = in the fleet; kick = revoked (an absorbing tombstone).",
    )
    seq: int = field(
        required=True,
        description="Per-machine sequence. NOT a roster-wide counter — there is none.",
    )
    issued_at: int = field(
        required=True,
        description="Unix ms, informational. Never a merge input.",
    )
    supersedes: str | None = field(
        required=False, default=None,
        description=(
            "For a re-enrol only: the entry id of the kick it supersedes. A "
            "kick never cites one."
        ),
    )
    signature: str = field(
        required=True,
        description="128-hex personal-root signature over the domain-separated body.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _hex(payload, "personal_root_pub", 64, cls.__name__)
        _hex(payload, "machine_id", 64, cls.__name__)
        _hex(payload, "machine_pub", 64, cls.__name__)
        _hex(payload, "signature", 128, cls.__name__)
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise SchemaValidationError(
                f"{cls.__name__}: 'seq' must be a non-negative int"
            )
        issued_at = payload.get("issued_at")
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or issued_at < 0
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'issued_at' must be a non-negative int"
            )
        supersedes = payload.get("supersedes")
        if supersedes is not None:
            _hex({"supersedes": supersedes}, "supersedes", 64, cls.__name__)
        if payload.get("kind") == "kick" and supersedes is not None:
            raise SchemaValidationError(
                f"{cls.__name__}: a kick tombstone does not cite a supersedes"
            )
