"""Atomic org-ledger founding: genesis, owner role, invite, founder claim.

One call mints the four constitutional events (D-01, ruling D20): the
self-signed genesis, the built-in ``owner`` role (scope ``*``,
``claim_requires "self"``), a root-signed founding invitation key-bound
to the founder persona, and the founder's ``member.claim`` — so a
founded ledger has the founder in ``fold.members`` from genesis,
operating as a persona holding ``owner``. The org root signs the
constitutional events and then goes cold; day-to-day authority is
exercised by the persona, derived from the stable ``genesis_id`` after
the genesis append (the order is therefore fixed: genesis first).

The founding invitation expires at its own mint instant (``expiry ==
now``), so it is redeemable only by the founding claim in this
sequence; there is no bare ``role.grant`` — membership and ``owner``
both flow from the claim, and every predicate keys off the one
membership projection (pin 6).

``kem_seed`` is optional (RESOLUTION 2): the default founding omits the
KEM credential — the founder is the domain's initial provisioner and
publishes a credential with first storage use (contract §5). When
supplied, the claim carries the persona-signed credential so the
founding admission is immediately provisionable.

``org_id`` is the organization's stable local ``orgs.id`` UUID (D21) —
a label, never the slug and never a registry handle. The org root
plaintext is required only in the calling process; the client-signed
production founding path is auto-kz3vu / auto-5dh9a.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tools.network.idkit import KeyPair, derive_persona

from .events import make_event
from .hlc import HLC
from .store import LedgerStore


@dataclass(frozen=True)
class FoundedLedger:
    genesis_id: str
    founder_persona_pub: str
    role_define_id: str
    founding_invite_id: str
    founder_claim_id: str
    #: The PersonaKemCredential dict, present iff kem_seed was supplied.
    kem_credential: Optional[dict]
    #: The founder's encapsulation private key (64-hex) for the caller's
    #: device store; None on the default (credential-free) founding.
    kem_private_key: Optional[str]


def found_org_ledger(
    store: LedgerStore,
    *,
    org_id: str,
    org_root: KeyPair,
    personal_root_seed: bytes,
    now: int,
    kem_seed: Optional[bytes] = None,
) -> FoundedLedger:
    """Found the ledger in one atomic sequence of four appends."""
    genesis_id = store.append(
        make_event(
            org_root,
            {"type": "genesis", "org": org_id, "root_pub": org_root.public_hex},
            [],
            HLC(now, 0),
        )
    )
    founder = derive_persona(personal_root_seed, genesis_id)

    credential = kem_private = None
    if kem_seed is not None:
        # Lazy import: storagekit imports ledger; the reverse edge exists
        # only inside this optional branch.
        from tools.network.storagekit import credentials as _credentials

        record, kem_private = _credentials.build(
            founder, genesis_id, kem_seed, [genesis_id], (now, 0)
        )
        credential = record.to_dict()

    role_define_id = store.append(
        make_event(
            org_root,
            {
                "type": "role.define",
                "name": "owner",
                "scope_set": ["*"],
                "claim_requires": "self",
                "version": 1,
            },
            [genesis_id],
            HLC(now, 1),
        )
    )
    founding_invite_id = store.append(
        make_event(
            org_root,
            {
                "type": "invite",
                "granted_role": "owner",
                "expiry": now,  # spent at its own mint instant
                "sponsor": org_root.public_hex,
                "invite_pub": founder.public_hex,  # key-bound: self-completes
            },
            [role_define_id],
            HLC(now, 2),
        )
    )
    claim_payload = {
        "type": "member.claim",
        "invite_ref": founding_invite_id,
        "persona_pub": founder.public_hex,
        "profile": {},
        "approvals": [],
    }
    if credential is not None:
        claim_payload["kem_credential"] = credential
    founder_claim_id = store.append(
        make_event(founder, claim_payload, [founding_invite_id], HLC(now, 3))
    )
    return FoundedLedger(
        genesis_id=genesis_id,
        founder_persona_pub=founder.public_hex,
        role_define_id=role_define_id,
        founding_invite_id=founding_invite_id,
        founder_claim_id=founder_claim_id,
        kem_credential=credential,
        kem_private_key=kem_private,
    )
