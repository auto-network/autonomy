"""A member can mint their own agent delegate, without the org root (auto-wrkaq).

Design of record graph://1e005d5c-c11 §7 (PIN 6b) and §8: "a persona
provisions and expires its own delegates" — the B7 retraction table permits
delegation from a MEMBER PERSONA and retracts it from the org root. The
fold's old gate authorized by DELEGABLE attenuation only, and role-held
scopes are non-delegable, so a member holding the storage scopes through a
role was refused with the fold having already established they HOLD the
scope. The new branch admits a CURRENT MEMBER PERSONA minting a strictly
weaker, non-redelegable, expiring instrument of its own held authority,
restricted to the exact storage scope pair — the scopes whose acceptance re-derives
authority from current membership at use time, so mint-time attenuation
does no security work for them.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import HLC, sign_delegate_proof
from tools.network.ledger.fold import (
    R_DELEGATE_NONCE_REUSED,
    R_NOT_REDELEGABLE,
    R_SCOPE_ESCALATION,
    self_delegable_exact,
)
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.storagekit import storage_delegate_scopes

from .conftest import Sim


def storage_world():
    """A founded org whose 'member' role carries the two storage scopes,
    with one admitted member — no root-present enabling act anywhere."""
    sim = Sim()
    domain = organization_content_domain_id(sim.genesis_id)
    scopes = storage_delegate_scopes(domain)
    sim.role_define(sim.root, "member", requires="self", scope_set=scopes)
    member = KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=member)
    sim.claim(invite, member, member)
    return sim, member, scopes


def test_a_role_holding_member_mints_its_own_bounded_delegate():
    """THE divergence this bead closes: the member holds the scopes through
    its role, mints a non-redelegable, expiring delegate, and the fold
    admits it. FAILS pre-wrkaq with R_NOT_REDELEGABLE — a refusal the old
    code reached only after establishing the author holds the scope."""
    sim, member, scopes = storage_world()
    agent = KeyPair.generate()
    grant = sim.delegate(member, agent, scopes, ttl=60_000)
    state = sim.fold()
    assert state.valid[grant] is True, state.reasons.get(grant)
    assert state.delegation_parents[agent.public_hex] == (member.public_hex,)


@pytest.mark.parametrize(
    "violation, expected",
    [
        ("redelegate", R_NOT_REDELEGABLE),
        ("no-ttl", R_NOT_REDELEGABLE),
        ("scope-outside-held", R_SCOPE_ESCALATION),
    ],
)
def test_each_bound_is_enforced_by_name(violation, expected):
    """The instrument must be strictly weaker, non-redelegable AND
    expiring; each bound refused by its own name, not a generic no."""
    sim, member, scopes = storage_world()
    agent = KeyPair.generate()
    if violation == "redelegate":
        grant = sim.delegate(member, agent, scopes, redelegate=True, ttl=60_000)
    elif violation == "no-ttl":
        grant = sim.delegate(member, agent, scopes)
    else:
        grant = sim.delegate(
            member, agent, sorted(set(scopes) | {"link:publish"}), ttl=60_000,
        )
    state = sim.fold()
    assert state.valid[grant] is False
    assert state.reasons[grant] == expected


def test_a_scope_outside_self_delegable_is_refused_even_when_held():
    """The genericity guard: _h_delegate governs delegation for EVERY
    scope, and the mint-time-check-does-no-security-work argument is
    established only for the storage scopes (their acceptance re-derives
    authority from current membership at use). A role-held scope outside
    the self-delegable pair is refused even though it attenuates held — this test
    FAILS if the branch is written without the scope restriction."""
    sim = Sim()
    sim.role_define(
        sim.root, "member", requires="self", scope_set=["link:publish"],
    )
    member = KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=member)
    sim.claim(invite, member, member)
    assert not self_delegable_exact(["link:publish"])  # the guard's premise
    grant = sim.delegate(member, KeyPair.generate(), ["link:publish"], ttl=60_000)
    state = sim.fold()
    assert state.valid[grant] is False
    assert state.reasons[grant] == R_NOT_REDELEGABLE


def test_the_chain_terminates_at_depth_one():
    """member -> A succeeds; A -> B is refused. Driven for BOTH ways a
    delegate could try to continue the chain:

    A member-minted A never even enters ``held`` — the authority walk
    computes effective scopes from ``deleg[author]``, and a role-holding
    author has none — so its onward mint dies as scope escalation with or
    without the persona condition.

    The staging where the persona condition ALONE does the work is a
    delegate holding the scopes via a ROOT-authored NON-redelegable grant:
    ``by_root`` feeds ``held[A]`` directly (can_redelegate gates ``deleg``
    only), so A satisfies every other clause of the self-delegation
    branch. This half FAILS if the persona condition is omitted — A would
    mint B under the very exception, and B would resolve upward through A.
    A delegate is not a persona."""
    sim, member, scopes = storage_world()
    a = KeyPair.generate()
    first = sim.delegate(member, a, scopes, ttl=600_000)
    b = KeyPair.generate()
    second = sim.delegate(a, b, scopes, ttl=60_000)

    held_a = KeyPair.generate()  # holds via root, non-redelegably
    root_grant = sim.delegate(sim.root, held_a, scopes, ttl=600_000)
    c = KeyPair.generate()
    third = sim.delegate(held_a, c, scopes, ttl=60_000)

    state = sim.fold()
    assert state.valid[first] is True
    assert state.valid[root_grant] is True
    assert state.valid[second] is False
    assert state.reasons[second] == R_SCOPE_ESCALATION
    assert state.valid[third] is False
    assert state.reasons[third] == R_NOT_REDELEGABLE, (
        "held_a HOLDS the scopes (by_root feeds held regardless of "
        "can_redelegate); only the persona condition stands between it "
        "and minting onward"
    )


def test_root_authored_mints_behave_exactly_as_before():
    """The root branch is DECIDED, not inherited: root passes on the
    ordinary-delegation condition via its UNIVERSE delegable authority, so
    its behaviour is unchanged by this bead — redelegable grants, no-ttl
    grants, and scopes far outside the self-delegable pair all still admit.
    (Whether the fold should refuse root-authored delegates at all is a
    separate, unsettled question.)"""
    sim = Sim()
    redelegable = sim.delegate(
        sim.root, KeyPair.generate(), ["link:publish"], redelegate=True,
    )
    unbounded = sim.delegate(sim.root, KeyPair.generate(), ["link:revoke"])
    state = sim.fold()
    assert state.valid[redelegable] is True
    assert state.valid[unbounded] is True


def test_a_refused_self_mint_does_not_burn_its_nonce():
    """le0kg v2's invariant survives the new branch: a mint refused on the
    redelegation bound does not consume its nonce, so the corrected
    re-issue under the same consent ceremony admits."""
    sim, member, scopes = storage_world()
    agent = KeyPair.generate()
    nonce = "aa" * 32
    refused = sim.delegate(
        member, agent, scopes, redelegate=True, ttl=60_000, nonce=nonce,
        proof=sign_delegate_proof(
            agent, sim.genesis_id, member.public_hex, scopes,
            can_redelegate=True, ttl=60_000, grant_nonce=nonce,
        ),
    )
    corrected = sim.delegate(member, agent, scopes, ttl=60_000, nonce=nonce,
        proof=sign_delegate_proof(
            agent, sim.genesis_id, member.public_hex, scopes,
            can_redelegate=False, ttl=60_000, grant_nonce=nonce,
        ),
    )
    state = sim.fold()
    assert state.reasons[refused] == R_NOT_REDELEGABLE
    assert state.valid[corrected] is True
    # And the nonce IS burned now: a further reuse is refused.
    replay = sim.delegate(member, agent, scopes, ttl=60_000, nonce=nonce,
        proof=sign_delegate_proof(
            agent, sim.genesis_id, member.public_hex, scopes,
            can_redelegate=False, ttl=60_000, grant_nonce=nonce,
        ),
    )
    assert sim.fold().reasons[replay] == R_DELEGATE_NONCE_REUSED


def test_the_root_present_enabling_act_no_longer_exists():
    """authorize_member_storage is deleted, not deprecated: leaving it
    would preserve a root-authored path to the storage scopes — the exact
    shape B7 retracted — for as long as it takes someone to notice."""
    from tools.network import storagekit
    from tools.network.storagekit import delegate as delegate_mod

    assert not hasattr(delegate_mod, "authorize_member_storage")
    assert not hasattr(storagekit, "authorize_member_storage")


def test_acceptance_follows_current_membership_not_the_mint():
    """The safety argument this bead rests on, driven end to end: the
    self-minted delegate authorizes storage because its chain resolves to
    a CURRENT member key — and stops authorizing the moment the minter's
    persona is rekeyed, with no revocation and no TTL expiry involved.
    Mint-time attenuation does no security work; use-time re-derivation
    does all of it."""
    from tools.network.storagekit.acceptance import resolve_member_key

    sim, member, scopes = storage_world()
    agent = KeyPair.generate()
    grant = sim.delegate(member, agent, scopes, ttl=600_000)
    state = sim.fold()
    assert state.valid[grant] is True
    assert resolve_member_key(state, agent.public_hex) == member.public_hex

    new_key = KeyPair.generate()
    sim.rekey(member, member, member, new_key)
    after = sim.fold()
    assert resolve_member_key(after, agent.public_hex) != member.public_hex


def test_the_scope_check_is_an_exact_same_domain_pair_not_coverage():
    """The gate reviewer's requirement, driven: pattern coverage admits a
    SINGLETON (an instrument the design does not define), a MIXED-DOMAIN
    pair (one delegate spanning two domains), and the PAIR PLUS A THIRD
    (reach beyond the defined shape). All three attenuate the member's
    held authority, so each must be refused by the exactness predicate —
    this test FAILS on a coverage implementation, which admitted all
    three (measured on 44898efc)."""
    d1, d2 = "aa" * 32, "bb" * 32
    s1, s2 = storage_delegate_scopes(d1), storage_delegate_scopes(d2)
    sim = Sim()
    sim.role_define(
        sim.root, "member", requires="self",
        scope_set=sorted(set(s1 + s2)),
    )
    member = KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=member)
    sim.claim(invite, member, member)

    singleton = sim.delegate(member, KeyPair.generate(), [s1[0]], ttl=60_000)
    mixed = sim.delegate(
        member, KeyPair.generate(), sorted([s1[0], s2[1]]), ttl=60_000,
    )
    triple = sim.delegate(
        member, KeyPair.generate(), sorted(set(s1 + [s2[0]])), ttl=60_000,
    )
    exact = sim.delegate(member, KeyPair.generate(), s1, ttl=60_000)

    state = sim.fold()
    for grant, label in ((singleton, "singleton"), (mixed, "mixed-domain"),
                         (triple, "pair-plus-third")):
        assert state.valid[grant] is False, f"{label} admitted"
        assert state.reasons[grant] == R_NOT_REDELEGABLE, label
    assert state.valid[exact] is True, state.reasons.get(exact)


def test_the_exactness_predicate_rejects_pattern_shaped_scopes():
    """A wildcard is not a domain: a member cannot self-mint
    ``storage:capability:grant:*`` even if a role somehow held it — the
    predicate requires one literal shared domain."""
    assert self_delegable_exact(storage_delegate_scopes("cc" * 32))
    assert not self_delegable_exact(
        ["storage:capability:grant:*", "storage:state:advance:*"]
    )
    assert not self_delegable_exact(
        ["storage:capability:grant:", "storage:state:advance:"]
    )
