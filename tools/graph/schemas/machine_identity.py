"""This enrolled machine's durable identity (auto-b6fee).

The only machine credential input persisted at rest, in machine.db
(``@home("machine")``, never leaves the machine). It is committed only after
the operator approves the one-time enrollment ceremony; it is the same
joiner-minted ``machine_id`` frozen into that request. It is a PUBLIC identifier,
not a secret — the machine's operating key is DERIVED from
``personal_root + machine_id`` (``idkit.derive_machine_key``) on demand and is
never stored or sealed. So there is nothing here to protect at rest beyond the
store's own locality: the id is safe in the clear, and the key that derives
from it is safe because the personal root is (under the one boot factor).

The roster root-binds this public id, the derived key's public half, and the
machine's fleet standing. This row is the joining machine's approved local
copy; it remains machine-local because it is the input used
to select this installation's derived private key.
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

MACHINE_IDENTITY_SET_ID = "autonomy.machine.identity"
MACHINE_IDENTITY_REVISION = 1
MACHINE_IDENTITY_KEY = "self"

SYNOPSIS = {
    "summary": (
        "This enrolled machine's durable ID, committed after approval and stored in "
        "machine.db (never synced). A public identifier — the machine's "
        "operating key derives from personal_root + machine_id and is never "
        "stored. See tools.network.machine_boot."
    ),
    "nouns": [
        "machine identity", "machine id", "fleet enrollment",
        "derived machine key",
    ],
    "related_set_ids": ["autonomy.fleet.roster#2"],
}

_HEX = "0123456789abcdef"


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_self")
class MachineIdentityV1(SettingSchema):
    """The one row of this enrolled machine's identity: its approved id."""

    set_id = MACHINE_IDENTITY_SET_ID
    schema_revision = MACHINE_IDENTITY_REVISION

    machine_id: str = field(
        required=True,
        description="64-hex machine id committed after fleet approval. Public.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        v = payload.get("machine_id")
        if not isinstance(v, str) or len(v) != 64 or any(c not in _HEX for c in v):
            raise SchemaValidationError(
                f"{cls.__name__}: 'machine_id' must be exactly 64 lowercase hex chars"
            )
