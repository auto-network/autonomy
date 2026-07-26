"""Lifecycle: safety predicate, selection, coalesced advance, union."""

from __future__ import annotations

import hashlib
import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.storagekit import bridge as bridge_mod
from tools.network.storagekit import state
from tools.network.storagekit.errors import StorageError
from tools.network.storagekit.lifecycle import (
    StateAdvanceRequired,
    advance_state,
    select_safe_state,
    state_covers,
    union_state,
)

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
OTHER_DOMAIN = "3c" * 32
HEADS = ["6f" * 32]
DIGEST = "8b" * 32

# Hand-built event DAG: a <- b <- d, a <- c <- d (diamond), e isolated.
_DAG = {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c"), "e": ()}
EID = {name: hashlib.sha256(name.encode()).hexdigest() for name in _DAG}
_PARENTS = {EID[k]: tuple(EID[p] for p in v) for k, v in _DAG.items()}


def ancestry(ids) -> frozenset:
    """Inclusive ancestor closure over the hand-built DAG."""
    seen: set = set()
    stack = list(ids)
    while stack:
        eid = stack.pop()
        if eid in seen:
            continue
        seen.add(eid)
        stack.extend(_PARENTS.get(eid, ()))
    return frozenset(seen)


@pytest.fixture(scope="module")
def creator() -> KeyPair:
    return KeyPair.generate()


def mint(creator, covered, domain_id=DOMAIN_ID, parents=()):
    return state.generate(
        creator, GENESIS, domain_id, parents, HEADS, sorted(covered), DIGEST
    )


# -- safety predicate ------------------------------------------------------------------


def test_state_covers(creator):
    descriptor, _ = mint(creator, [EID["d"]])
    # Equal and causal-ancestor contractions are covered (inclusive closure).
    assert state_covers(descriptor, [EID["d"]], ancestry)
    assert state_covers(descriptor, [EID["a"], EID["b"], EID["c"]], ancestry)
    # An empty required set is covered, even by an empty covered set.
    empty, _ = mint(creator, [])
    assert state_covers(empty, [], ancestry)
    # Outside the closure: not covered.
    assert not state_covers(descriptor, [EID["e"]], ancestry)
    assert not state_covers(empty, [EID["a"]], ancestry)


# -- selection ---------------------------------------------------------------------------


def test_select_smallest_id_order_independent(creator):
    safe = [mint(creator, [EID["d"]])[0] for _ in range(4)]
    unsafe, _ = mint(creator, [EID["b"]])  # does not cover c
    foreign, _ = mint(creator, [EID["d"]], domain_id=OTHER_DOMAIN)
    expected = min(safe, key=lambda d: d.state_id)
    required = [EID["b"], EID["c"]]
    pool = safe + [unsafe, foreign]
    rng = random.Random(20260726)
    for _ in range(5):
        rng.shuffle(pool)
        assert select_safe_state(DOMAIN_ID, required, pool, ancestry) == expected


def test_select_ignores_other_domains_and_signals_advance(creator):
    foreign, _ = mint(creator, [EID["d"]], domain_id=OTHER_DOMAIN)
    stale, _ = mint(creator, [EID["b"]])
    with pytest.raises(StateAdvanceRequired):
        select_safe_state(DOMAIN_ID, [EID["c"]], [foreign, stale], ancestry)
    with pytest.raises(StateAdvanceRequired):
        select_safe_state(DOMAIN_ID, [EID["a"]], [], ancestry)


# -- advancement -------------------------------------------------------------------------


def test_advance_coalesces_and_bridges(creator):
    parent, parent_secret = mint(creator, [])
    required = [EID["c"], EID["b"]]
    descriptor, secret, bridges = advance_state(
        creator,
        DOMAIN_ID,
        GENESIS,
        required,
        DIGEST,
        [parent],
        {parent.state_id: parent_secret},
        HEADS,
    )
    assert descriptor.covered_loss_heads == tuple(sorted(required))
    assert descriptor.parent_state_ids == (parent.state_id,)
    assert isinstance(secret, bytes) and len(secret) == 32
    assert state_covers(descriptor, required, ancestry)
    assert state.verify_structure(descriptor) is None
    assert len(bridges) == 1
    assert bridge_mod.open(bridges[0], secret) == parent_secret


def test_advance_fails_closed(creator):
    parent, parent_secret = mint(creator, [])
    with pytest.raises(StorageError):  # empty required set
        advance_state(
            creator, DOMAIN_ID, GENESIS, [], DIGEST,
            [parent], {parent.state_id: parent_secret}, HEADS,
        )
    with pytest.raises(StorageError):  # missing parent secret
        advance_state(
            creator, DOMAIN_ID, GENESIS, [EID["b"]], DIGEST, [parent], {}, HEADS
        )
    with pytest.raises(StorageError):  # duplicate parents
        advance_state(
            creator, DOMAIN_ID, GENESIS, [EID["b"]], DIGEST,
            [parent, parent], {parent.state_id: parent_secret}, HEADS,
        )


# -- union -------------------------------------------------------------------------------


def test_union_reaches_every_branch(creator):
    base, base_secret = mint(creator, [])
    required = [EID["b"], EID["c"]]
    secrets = {base.state_id: base_secret}
    branches = []
    for _ in range(2):  # two independent advances over the SAME required set
        d, s, bs = advance_state(
            creator, DOMAIN_ID, GENESIS, required, DIGEST, [base], secrets, HEADS
        )
        branches.append((d, s, bs))
    (da, sa, ba), (db, sb, bb) = branches
    # Acceptance 5: both concurrent advances are safe — no forced order.
    for d in (da, db):
        assert state_covers(d, required, ancestry)
    assert select_safe_state(DOMAIN_ID, required, [da, db], ancestry) == min(
        (da, db), key=lambda d: d.state_id
    )

    union, union_secret, union_bridges = union_state(
        creator,
        DOMAIN_ID,
        GENESIS,
        [da, db],
        {da.state_id: sa, db.state_id: sb},
        [EID["d"]],
        DIGEST,
        HEADS,
    )
    assert union.parent_state_ids == tuple(sorted([da.state_id, db.state_id]))
    assert set(union.covered_loss_heads) == set(required) | {EID["d"]}  # the union
    assert len(union_bridges) == 2

    descriptors = {x.state_id: x for x in (base, da, db, union)}
    recovered = bridge_mod.recover_ancestors(
        union.state_id,
        union_secret,
        list(union_bridges) + list(ba) + list(bb),
        descriptors,
    )
    # Every branch history stays reachable, down to the shared base.
    assert recovered[da.state_id] == sa
    assert recovered[db.state_id] == sb
    assert recovered[base.state_id] == base_secret


def test_union_fails_closed(creator):
    a, sa = mint(creator, [])
    b, sb = mint(creator, [])
    with pytest.raises(StorageError):  # fewer than two parents
        union_state(
            creator, DOMAIN_ID, GENESIS, [a], {a.state_id: sa}, [], DIGEST, HEADS
        )
    with pytest.raises(StorageError):  # missing parent secret
        union_state(
            creator, DOMAIN_ID, GENESIS, [a, b], {a.state_id: sa}, [], DIGEST, HEADS
        )
