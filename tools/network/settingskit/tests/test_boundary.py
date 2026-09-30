"""The signed-settings boundary, steps 2 to 5 (auto-qrmlg.6 S1), against a
real ledger fold built by the ledger test simulator.

Operator ruling 2026-09-27 23:41Z: every member role carries
``settings:sign:*`` unless the organization narrows it; restriction is the
opt-in of a role that names a ``settings:`` scope.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger.scopes import (
    SETTINGS_SIGN_ANY,
    names_settings_scope,
    pattern_covers,
    settings_sign_scope,
)
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import storage_delegate_scopes
from tools.network.settingskit.boundary import (
    check_signer,
    persona_holds_settings_sign,
    resolve_signer_persona,
)

SET_A = "autonomy.org.member-profile"
SET_B = "autonomy.dashboard.agent-actions"


def _storage_member(sim: Sim):
    """A member whose role holds the storage delegate scopes, and a process
    key it delegated to — the only grant a member persona may mint for its
    own process key (scopes.self_delegable_exact; non-redelegable, expiring,
    within what the role holds). Returns (member, delegate, scopes)."""
    scopes = storage_delegate_scopes(organization_content_domain_id(sim.genesis_id))
    member = _member(sim, "member", scope_set=scopes)
    delegate = KeyPair.generate()
    grant = sim.delegate(member, delegate, scopes, ttl=60_000)
    assert sim.fold().valid[grant] is True, sim.fold().reasons.get(grant)
    return member, delegate


def _member(sim: Sim, role: str, scope_set=(), requires="self"):
    """Define *role* (if new), invite for it, claim a persona, return the
    (persona keypair, member key) pair."""
    if role not in {d for d in sim.fold().role_defs}:
        sim.role_define(sim.root, role, scope_set=scope_set, requires=requires)
    persona = KeyPair.generate()
    # As the ledger tests do: the invite names a key, and that key signs the
    # claim of the persona (one key for both here).
    sim.claim(sim.invite(sim.root, role, invite_key=persona), persona, persona)
    return persona


def test_the_scope_family_and_its_coverage():
    assert settings_sign_scope(SET_A) == "settings:sign:" + SET_A
    assert pattern_covers(SETTINGS_SIGN_ANY, settings_sign_scope(SET_A))
    assert pattern_covers("*", settings_sign_scope(SET_A))
    assert not pattern_covers(settings_sign_scope(SET_A), settings_sign_scope(SET_B))
    assert names_settings_scope(["role:define", "settings:sign:x.y"])
    assert not names_settings_scope(["role:define", "invite:member"])


def test_root_and_a_plain_member_role_hold_every_set_by_default():
    sim = Sim()
    member = _member(sim, "member", scope_set=())
    fold = sim.fold()
    assert persona_holds_settings_sign(fold, member.public_hex, SET_A)
    assert persona_holds_settings_sign(fold, member.public_hex, SET_B)
    verdict = check_signer(fold, signing_key=member.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x")
    assert verdict.ok and verdict.persona == member.public_hex
    # The org root holds * and is not a claimed member persona; it is refused
    # as a settings signer under the delegate strategy by membership.
    root_verdict = check_signer(fold, signing_key=sim.root.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x")
    assert not root_verdict.ok and root_verdict.reason in ("signer_unknown", "signer_not_member")


def test_a_role_that_names_settings_scopes_is_narrowed_to_them():
    sim = Sim()
    narrowed = _member(sim, "editor", scope_set=[settings_sign_scope(SET_A)])
    fold = sim.fold()
    assert persona_holds_settings_sign(fold, narrowed.public_hex, SET_A)
    assert not persona_holds_settings_sign(fold, narrowed.public_hex, SET_B)
    refused = check_signer(fold, signing_key=narrowed.public_hex, set_id=SET_B, key_strategy="delegate", row_key="x")
    assert not refused.ok and refused.reason == "signer_lacks_settings_sign" and refused.persona == narrowed.public_hex
    # A role naming the whole family is not narrowed.
    wide = _member(sim, "writer", scope_set=[SETTINGS_SIGN_ANY])
    fold = sim.fold()
    assert persona_holds_settings_sign(fold, wide.public_hex, SET_B)


def test_a_delegate_key_resolves_to_its_member_persona():
    sim = Sim()
    member, delegate = _storage_member(sim)
    fold = sim.fold()
    assert resolve_signer_persona(fold, delegate.public_hex) == member.public_hex
    verdict = check_signer(fold, signing_key=delegate.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x")
    assert verdict.ok and verdict.persona == member.public_hex
    stranger = KeyPair.generate()
    assert resolve_signer_persona(fold, stranger.public_hex) is None
    assert check_signer(fold, signing_key=stranger.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x").reason == "signer_unknown"


def test_a_revoked_key_still_resolves_but_is_refused():
    sim = Sim()
    member, delegate = _storage_member(sim)
    sim.revoke_key(sim.root, delegate)
    fold = sim.fold()
    verdict = check_signer(fold, signing_key=delegate.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x")
    assert not verdict.ok and verdict.reason in ("signer_key_revoked", "signer_unknown")


def test_a_superseded_member_key_proves_the_past_and_cannot_sign_the_present():
    sim = Sim()
    member = _member(sim, "member", scope_set=())
    new_key = KeyPair.generate()
    sim.rekey(member, member, member, new_key)
    fold = sim.fold()
    assert resolve_signer_persona(fold, member.public_hex) == member.public_hex   # continuity: attributable
    old = check_signer(fold, signing_key=member.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x")
    assert not old.ok and old.reason == "signer_key_superseded"
    current = check_signer(fold, signing_key=new_key.public_hex, set_id=SET_A, key_strategy="delegate", row_key="x")
    assert current.ok and current.persona == member.public_hex


def test_the_persona_strategy_requires_the_row_persona_and_makes_no_scope_check():
    sim = Sim()
    narrowed = _member(sim, "editor", scope_set=[settings_sign_scope(SET_A)])
    fold = sim.fold()
    own = check_signer(fold, signing_key=narrowed.public_hex, set_id=SET_B, key_strategy="persona", row_key=narrowed.public_hex)
    assert own.ok, own   # no settings:sign check on the persona branch
    other = check_signer(fold, signing_key=narrowed.public_hex, set_id=SET_B, key_strategy="persona", row_key="someone-else")
    assert not other.ok and other.reason == "signer_is_not_row_persona"
    assert check_signer(fold, signing_key=narrowed.public_hex, set_id=SET_A, key_strategy="bogus", row_key="x").reason == "unknown_key_strategy"


def test_the_persona_prefix_strategy_lets_a_persona_hold_many_keys_and_only_its_own():
    """``<persona>:<rest>``: the signer must be the key's first segment, and
    the rest of the key is free, so one persona holds any number of rows."""
    sim = Sim()
    member = _member(sim, "editor")
    other = _member(sim, "editor")
    fold = sim.fold()
    for rest in ("machine-a", "machine-b", "a:b:c"):
        own = check_signer(fold, signing_key=member.public_hex, set_id=SET_A,
                           key_strategy="persona_prefix", row_key=f"{member.public_hex}:{rest}")
        assert own.ok and own.persona == member.public_hex
    theirs = check_signer(fold, signing_key=member.public_hex, set_id=SET_A,
                          key_strategy="persona_prefix", row_key=f"{other.public_hex}:machine-a")
    assert (theirs.ok, theirs.reason) == (False, "signer_is_not_row_persona")
    bare = check_signer(fold, signing_key=member.public_hex, set_id=SET_A,
                        key_strategy="persona_prefix", row_key="machine-a")
    assert bare.ok is False


def test_a_persona_prefixed_set_declares_the_prefix_strategy():
    from tools.graph import schemas  # noqa: F401 -- registers the sets
    from tools.graph.schemas.org_session_runner import (
        ORG_SESSION_RUNNER_REVISION, ORG_SESSION_RUNNER_SET_ID)
    from tools.network.settingskit.authority import signing_key_strategy

    assert signing_key_strategy(ORG_SESSION_RUNNER_SET_ID, ORG_SESSION_RUNNER_REVISION) == "persona_prefix"
