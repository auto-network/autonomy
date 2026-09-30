"""Who may have signed a settings row: the fold's answer (auto-qrmlg.6 S1).

The design of record (graph://21a0da9e-1c2, "Verification, at the
boundaries") lists seven steps a statement passes before it enters a store.
This module is steps 2 to 5, the ones that are functions of the fold alone:

2. resolve the signing key to its stable persona — a delegate key walks the
   fold's delegation edges upward to the member key it acts as, a member key
   maps through the rekey continuity chain;
3. the signing key itself is not revoked, independently of membership;
4. that persona is a current member and, where the set's key strategy is
   ``delegate``, holds ``settings:sign:<set_id>``; where it is ``persona``
   the resolved persona must equal the row key, with no scope check;
5. a member key must be the persona's CURRENT key: continuity verifies the
   past, currency authorizes the present.

Step 1 (the signature over the addressed record) is
:func:`tools.network.settingskit.envelope.verify_record`; steps 6 and 7 (the
per-signer freshness floor and the witness bound) need the store and the
witness key and belong to the boundary's callers (S3).

The entitlement rule for step 4 is the operator's (2026-09-27 23:41Z):
every member role carries ``settings:sign:*`` unless the organization
narrows it. A persona whose roles name NO scope of the ``settings:`` family
holds ``settings:sign:*`` implicitly (today's behaviour, now attributed);
a role that names any ``settings:`` scope is the opt-in to restriction and
entitles exactly what it names. The org root, and any key holding ``*``,
hold every set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tools.network.ledger.scopes import (
    SETTINGS_SIGN_ANY,
    names_settings_scope,
    set_covers,
    settings_sign_scope,
)

#: ``persona_prefix``: the row key's first ``:``-separated segment is the
#: signer's persona and the rest is the set's own (a persona holds many rows).
KEY_STRATEGIES = ("delegate", "persona", "persona_prefix")


@dataclass(frozen=True)
class SignerVerdict:
    """The boundary's answer for one signing key on one row."""

    ok: bool
    #: The stable persona the key resolved to (also on refusal, when known).
    persona: Optional[str]
    #: A short machine-readable reason on refusal; ``""`` when ok.
    reason: str = ""


def resolve_signer_persona(fold, signing_key: str) -> Optional[str]:
    """Step 2: the stable persona *signing_key* acts for, or None.

    A member key (current or superseded) resolves through
    ``fold.persona_for_key``. Any other key walks ``fold.delegation_parents``
    upward, breadth-first and cycle-guarded, to the first key that resolves
    to a persona — the same walk storage acceptance makes
    (storagekit.acceptance.resolve_member_key), ending at a persona rather
    than at a member key so a delegate of a since-rekeyed member still
    resolves to that member.
    """
    persona = fold.persona_for_key(signing_key)
    if persona is not None:
        return persona
    seen = {signing_key}
    frontier = [signing_key]
    parents = getattr(fold, "delegation_parents", {}) or {}
    while frontier:
        current = frontier.pop(0)
        for parent in parents.get(current, ()):
            persona = fold.persona_for_key(parent)
            if persona is not None:
                return persona
            if parent not in seen:
                seen.add(parent)
                frontier.append(parent)
    return None


def persona_holds_settings_sign(fold, persona: str, set_id: str) -> bool:
    """Step 4's entitlement under the ``delegate`` strategy.

    True when the persona's current key holds ``settings:sign:<set_id>``
    (through ``*``, ``settings:sign:*`` or the exact scope), or when none
    of the persona's roles names a ``settings:`` scope at all — the
    operator's default, every member role carries ``settings:sign:*``
    unless the organization narrows it.
    """
    member = fold.members.get(persona)
    if member is None:
        return False
    scope = settings_sign_scope(set_id)
    if fold.holds(member.current_key, scope) or fold.holds(member.current_key, SETTINGS_SIGN_ANY):
        return True
    named: set[str] = set()
    for role in member.roles:
        definition = fold.role_defs.get(role)
        if definition is not None:
            named.update(definition.scope_set)
    if not names_settings_scope(named):
        return True   # nothing narrowed: the default entitlement stands
    return set_covers(named, scope)


def check_signer(
    fold, *, signing_key: str, set_id: str, key_strategy: str, row_key: str,
) -> SignerVerdict:
    """Steps 2 to 5 for one row. Never raises on a refusal: the verdict
    names it, and nothing is written."""
    if key_strategy not in KEY_STRATEGIES:
        return SignerVerdict(False, None, "unknown_key_strategy")
    persona = resolve_signer_persona(fold, signing_key)
    if persona is None:
        return SignerVerdict(False, None, "signer_unknown")
    # Step 3: revocation of the key itself, independent of membership — a
    # revoked key still RESOLVES (what it signed stays attributable).
    if fold.key_revoked(signing_key):
        return SignerVerdict(False, persona, "signer_key_revoked")
    member = fold.members.get(persona)
    if member is None:
        return SignerVerdict(False, persona, "signer_not_member")
    # Step 5: a member key must be the persona's CURRENT key. A delegate key
    # is not a member key and is governed by its grant's usability, which
    # the delegation-parents view already enforced in step 2.
    if fold.persona_for_key(signing_key) is not None and signing_key != member.current_key:
        return SignerVerdict(False, persona, "signer_key_superseded")
    # Step 4.
    if key_strategy == "persona_prefix":
        if row_key.split(":", 1)[0] != persona:
            return SignerVerdict(False, persona, "signer_is_not_row_persona")
        return SignerVerdict(True, persona)
    if key_strategy == "persona":
        if persona != row_key:
            return SignerVerdict(False, persona, "signer_is_not_row_persona")
        return SignerVerdict(True, persona)
    if not persona_holds_settings_sign(fold, persona, set_id):
        return SignerVerdict(False, persona, "signer_lacks_settings_sign")
    return SignerVerdict(True, persona)
