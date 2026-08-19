"""This machine's identity: its random machine ID (auto-b6fee).

The ONLY thing first boot persists at rest, in machine.db
(``@home("machine")``, never leaves the machine). It is a PUBLIC identifier,
not a secret — the machine's operating key is DERIVED from
``personal_root + machine_id`` (``idkit.derive_machine_key``) on demand and is
never stored or sealed. So there is nothing here to protect at rest beyond the
store's own locality: the id is safe in the clear, and the key that derives
from it is safe because the personal root is (under the one boot factor).

The id goes into the fleet roster alongside the derived key's public half at
approval (auto-5ydhe); this row is the joining machine's local copy of its own
id, minted here and never reassigned.
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
        "This machine's random machine ID, minted at first boot and stored in "
        "machine.db (never synced). A public identifier — the machine's "
        "operating key derives from personal_root + machine_id and is never "
        "stored. See tools.network.machine_boot."
    ),
    "nouns": [
        "machine identity", "machine id", "first boot", "fleet enrollment",
        "derived machine key",
    ],
    "related_set_ids": ["autonomy.fleet.roster#1"],
}

_HEX = "0123456789abcdef"


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_self")
class MachineIdentityV1(SettingSchema):
    """The one row of this machine's identity (key ``self``): its machine id."""

    set_id = MACHINE_IDENTITY_SET_ID
    schema_revision = MACHINE_IDENTITY_REVISION

    machine_id: str = field(
        required=True,
        description="64-hex random machine id (idkit.mint_machine_id). Public.",
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
