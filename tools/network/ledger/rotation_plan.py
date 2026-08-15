"""Carrying a personal-root rotation through to every organization.

Declaring a new personal root is one signed record, and it is not the job.
Your member key in every organization is DERIVED from the personal root, so a
new root produces a member nobody has heard of, while the member each
organization still recognises is the one derived from the OLD root -- which is
exactly what a thief holding your old identity has. Until every organization
is told, rotating changes who you are at the top and leaves your standing
everywhere else in their hands.

So rotation is a CEREMONY over N organizations, not an operation. The half-way
state is the dangerous one -- some organizations moved, others still honouring
a key you have disowned -- and the thing that makes it safe is being able to
see exactly which. This module plans the steps and classifies what is done, so
an interrupted rotation is resumable and, more importantly, VISIBLE.

You need the OLD root seed to do any of it: the rekey in each organization is
authorised by the old member key. Keep it until the ceremony completes -- that
is not a weakness, since the only alternative is asking organizations to trust
an unheard-of key on nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.network.idkit import KeyPair
from tools.network.idkit.errors import MalformedError
from tools.network.idkit.persona import derive_persona

from .events import sign_rekey_continuity

#: A step is PENDING until the organization's ledger shows the new member key,
#: DONE once it does, and FOREIGN when the organization recognises neither --
#: which means this plan does not describe that organization and acting on it
#: would be guesswork.
PENDING = "pending"
DONE = "done"
FOREIGN = "foreign"


@dataclass(frozen=True)
class OrgRekeyStep:
    """One organization's part of the ceremony."""

    slug: str
    genesis_id: str
    persona_id: str          # the STABLE member id; never changes
    old_pub: str             # derived from the old root -- what they recognise
    new_pub: str             # derived from the new root -- where you are going

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "genesis_id": self.genesis_id,
            "persona_id": self.persona_id,
            "old_pub": self.old_pub,
            "new_pub": self.new_pub,
        }


def _seed(value: bytes, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
        raise MalformedError(f"{name} must be a 32-byte root seed")
    return bytes(value)


def plan_persona_rotation(
    old_root_seed: bytes, new_root_seed: bytes, orgs: list
) -> list:
    """One step per organization: where its member key must move.

    *orgs* is a list of ``(slug, genesis_id)``. The persona id is the member
    identity the organization has always known, which is the key first derived
    at joining -- it is stable across every rekey, which is what lets roles and
    standing survive.
    """
    old = _seed(old_root_seed, "old_root_seed")
    new = _seed(new_root_seed, "new_root_seed")
    if old == new:
        raise MalformedError("a rotation must move to a different root")

    steps = []
    seen = set()
    for entry in orgs:
        slug, genesis_id = entry
        if slug in seen:
            raise MalformedError(f"organization {slug!r} appears twice in the plan")
        seen.add(slug)
        old_persona = derive_persona(old, genesis_id)
        new_persona = derive_persona(new, genesis_id)
        steps.append(
            OrgRekeyStep(
                slug=slug,
                genesis_id=genesis_id,
                # The member id is the ORIGINAL derived key. For a member who
                # has never rekeyed that is the old-root persona; callers
                # holding an older id pass it through unchanged.
                persona_id=old_persona.public_hex,
                old_pub=old_persona.public_hex,
                new_pub=new_persona.public_hex,
            )
        )
    return steps


def build_rekey_payload(
    step: OrgRekeyStep, old_root_seed: bytes, new_root_seed: bytes
) -> dict:
    """The ``member.rekey`` payload for one step, signed both ways.

    The OLD member key authorises the move (the event is emitted under it) and
    the NEW one proves it exists, over a persona-bound domain so the proof
    cannot be replayed as any other kind of rotation.
    """
    old_persona = derive_persona(_seed(old_root_seed, "old_root_seed"), step.genesis_id)
    new_persona = derive_persona(_seed(new_root_seed, "new_root_seed"), step.genesis_id)
    if old_persona.public_hex != step.old_pub or new_persona.public_hex != step.new_pub:
        raise MalformedError(
            "the seeds given do not derive this step's keys; wrong identity or "
            "wrong organization"
        )
    return {
        "type": "member.rekey",
        "persona": step.persona_id,
        "old_pub": step.old_pub,
        "new_pub": step.new_pub,
        "continuity": sign_rekey_continuity(
            new_persona, step.persona_id, step.old_pub
        ),
        "approvals": [],
    }


def signing_key_for(step: OrgRekeyStep, old_root_seed: bytes) -> KeyPair:
    """The key that emits the event: the member the organization recognises."""
    return derive_persona(_seed(old_root_seed, "old_root_seed"), step.genesis_id)


def classify(steps: list, current_by_slug: dict) -> dict:
    """Where the ceremony has got to, per organization.

    *current_by_slug* maps slug -> the member key that organization currently
    recognises (None if it does not know this member at all). Anything that is
    neither the old nor the new key is FOREIGN: this plan does not describe
    that organization, and guessing would be worse than stopping.
    """
    out = {}
    for step in steps:
        current = current_by_slug.get(step.slug)
        if current == step.new_pub:
            out[step.slug] = DONE
        elif current == step.old_pub:
            out[step.slug] = PENDING
        else:
            out[step.slug] = FOREIGN
    return out


def is_complete(status: dict) -> bool:
    """True only when every organization has moved. FOREIGN is never complete."""
    return bool(status) and all(state == DONE for state in status.values())


def unfinished(status: dict) -> list:
    """The organizations still to do, so a resumed ceremony repeats nothing."""
    return sorted(slug for slug, state in status.items() if state != DONE)
