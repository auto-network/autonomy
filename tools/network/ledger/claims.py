"""Member-claim minting — the invitee-side Python client (auto-g6q9d).

Given the resolved invite context, derive the per-organization persona
and mint the signed ``member.claim`` event. The same function re-mints
at finalization with the merged approvals (the invitee's persona is the
sole valid author, and the author signature covers the full payload
including ``approvals``).

Lives in the ledger, not idkit: minting needs ``make_event``/``HLC``
and ledger sits above idkit — the primitive layer supplies only
``derive_persona``. The optional ``kem_credential`` arrives as a
caller-supplied dict (minted by ``storagekit.credentials.build``), so
the ledger stays storage-package-free.
"""

from __future__ import annotations

from typing import Optional

from tools.network.idkit import KeyPair, derive_persona

from .events import Event, make_event
from .hlc import HLC


def build_member_claim_payload(
    persona_pub: str,
    invite_ref: str,
    *,
    profile: Optional[dict] = None,
    token: Optional[str] = None,
    kem_credential: Optional[dict] = None,
    approvals=(),
) -> dict:
    """The claim payload — also what countersigners sign approvals over."""
    payload = {
        "type": "member.claim",
        "invite_ref": invite_ref,
        "persona_pub": persona_pub,
        "profile": dict(profile) if profile else {},
        "approvals": sorted(
            ({"key": e["key"], "sig": e["sig"]} for e in approvals),
            key=lambda e: e["key"],
        ),
    }
    if token is not None:
        payload["token"] = token
    if kem_credential is not None:
        payload["kem_credential"] = dict(kem_credential)
    return payload


def mint_member_claim(
    personal_root_seed: bytes,
    genesis_id: str,
    *,
    invite_ref: str,
    heads,
    hlc: HLC,
    profile: Optional[dict] = None,
    token: Optional[str] = None,
    kem_credential: Optional[dict] = None,
    approvals=(),
) -> tuple:
    """Derive the persona and sign the claim; returns ``(event, persona)``.

    The persona :class:`KeyPair` is returned so the caller can mint its
    KEM credential, sign follow-ups, and store the identity — the seed
    itself need not outlive this call.
    """
    persona = derive_persona(personal_root_seed, genesis_id)
    payload = build_member_claim_payload(
        persona.public_hex,
        invite_ref,
        profile=profile,
        token=token,
        kem_credential=kem_credential,
        approvals=approvals,
    )
    event = make_event(persona, payload, sorted(heads), hlc)
    return event, persona
