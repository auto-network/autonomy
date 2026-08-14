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

from .errors import LedgerError
from .events import make_event
from .hlc import HLC
from .store import LedgerStore


class FoundingMismatchError(LedgerError):
    """An existing partial founding does not match the supplied identity.

    Completion is refused rather than appended onto an unknown-provenance
    genesis — genesis is the identity anchor (personas derive from
    ``genesis_id``), so completing is only legitimate when the committed
    events are exactly what this identity's founding would have minted.
    """


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
    recovery_pub: Optional[str] = None,
) -> FoundedLedger:
    """Found the ledger in one atomic sequence of four appends.

    ``recovery_pub`` (the public half of the recovery-code ceremony's cold key,
    the only secretless artifact that crosses) declares the org's recovery
    policy at genesis: present -> policy ``recovery-key``; absent -> ``none``.
    make_event validates the genesis through ``_v_genesis``, so an
    ``recovery_pub == root_pub`` self-defeat is rejected here just as it is on
    the registration path -- the recovery factor must be a key the root does
    not control.
    """
    genesis_payload = {
        "type": "genesis", "org": org_id, "root_pub": org_root.public_hex,
    }
    if recovery_pub is not None:
        genesis_payload["recovery"] = {
            "policy": "recovery-key", "recovery_pub": recovery_pub,
        }
    genesis_id = store.append(
        make_event(org_root, genesis_payload, [], HLC(now, 0))
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


def resume_org_founding(
    store: LedgerStore,
    *,
    org_id: str,
    org_root: KeyPair,
    personal_root_seed: bytes,
) -> FoundedLedger:
    """Complete an interrupted founding onto its existing genesis.

    A committed genesis cannot be re-minted without changing the org
    identity (personas derive from ``genesis_id``), so completion is the
    only identity-preserving recovery — and it is GUARDED: every already-
    committed founding event must be exactly what this identity's
    founding would have minted (org label, org root, founder persona,
    prefix order); anything else raises :class:`FoundingMismatchError`
    and appends nothing. Idempotent on a complete founding.

    Resumed events keep the LOGICAL clock at the founding instant
    (``HLC(prev.ts, prev.count + 1)``): the founding invitation is spent
    at its own mint instant (``expiry == its ts``), so a wall-clock-timed
    completion would fold ``R_INVITE_EXPIRED`` — causality is carried by
    parent hashes, and the HLC is an ordering hint (see ``hlc.py``).
    """
    genesis = store.ledger.genesis  # GenesisError when nothing to resume
    if genesis.payload["org"] != org_id:
        raise FoundingMismatchError(
            "existing genesis carries a different org label"
        )
    if genesis.payload["root_pub"] != org_root.public_hex:
        raise FoundingMismatchError(
            "existing genesis was minted under a different org root"
        )
    genesis_id = genesis.event_id
    founder = derive_persona(personal_root_seed, genesis_id)

    events = store.events()
    define = next(
        (
            e
            for e in events
            if e.type == "role.define" and e.payload.get("name") == "owner"
        ),
        None,
    )
    invite = next(
        (
            e
            for e in events
            if e.type == "invite" and e.payload.get("granted_role") == "owner"
        ),
        None,
    )
    claim = next((e for e in events if e.type == "member.claim"), None)

    if define is not None and (
        define.author_key != org_root.public_hex
        or define.payload["scope_set"] != ["*"]
        or define.payload["claim_requires"] != "self"
    ):
        raise FoundingMismatchError("existing owner role has unknown provenance")
    if invite is not None:
        if define is None:
            raise FoundingMismatchError("founding events are not a prefix")
        if (
            invite.author_key != org_root.public_hex
            or invite.payload["sponsor"] != org_root.public_hex
            or invite.payload.get("invite_pub") != founder.public_hex
        ):
            raise FoundingMismatchError(
                "existing founding invitation has unknown provenance"
            )
    if claim is not None:
        if invite is None:
            raise FoundingMismatchError("founding events are not a prefix")
        if (
            claim.author_key != founder.public_hex
            or claim.payload["persona_pub"] != founder.public_hex
            or claim.payload["invite_ref"] != invite.event_id
        ):
            raise FoundingMismatchError(
                "existing founding claim has unknown provenance"
            )

    prev = claim or invite or define or genesis
    hlc = prev.hlc

    def next_hlc() -> HLC:
        nonlocal hlc
        hlc = HLC(hlc.ts, hlc.count + 1)
        return hlc

    if define is None:
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
                next_hlc(),
            )
        )
    else:
        role_define_id = define.event_id
    if invite is None:
        founding_invite_id = store.append(
            make_event(
                org_root,
                {
                    "type": "invite",
                    "granted_role": "owner",
                    "expiry": hlc.ts,  # spent at the founding instant
                    "sponsor": org_root.public_hex,
                    "invite_pub": founder.public_hex,
                },
                [role_define_id],
                next_hlc(),
            )
        )
    else:
        founding_invite_id = invite.event_id
    if claim is None:
        founder_claim_id = store.append(
            make_event(
                founder,
                {
                    "type": "member.claim",
                    "invite_ref": founding_invite_id,
                    "persona_pub": founder.public_hex,
                    "profile": {},
                    "approvals": [],
                },
                [founding_invite_id],
                next_hlc(),
            )
        )
    else:
        founder_claim_id = claim.event_id
    return FoundedLedger(
        genesis_id=genesis_id,
        founder_persona_pub=founder.public_hex,
        role_define_id=role_define_id,
        founding_invite_id=founding_invite_id,
        founder_claim_id=founder_claim_id,
        kem_credential=None,
        kem_private_key=None,
    )
