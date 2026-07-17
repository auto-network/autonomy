"""L1 — deterministic fold: identical DAG ⇒ identical authority state.

Hypothesis-style property tests on plain pytest: seeded random DAG
generation (concurrent branches, unauthorized events, revocations, invite
claims, tie races) replayed into fresh replicas in shuffled orders. Every
replica must fold to a bit-identical state fingerprint, event-validity map,
and invalidity reasons.
"""

from __future__ import annotations

import random

import pytest

from tools.network.ledger import Ledger, fold

from .conftest import random_events, shuffled_ledger

SEEDS = list(range(14))


@pytest.mark.parametrize("seed", SEEDS)
def test_shuffled_replay_orders_fold_identically(seed):
    events = random_events(seed, n=40)
    rng = random.Random(seed * 7919 + 1)

    reference = Ledger()
    reference.ingest(list(events))
    ref_state = fold(reference)
    ref_fp = ref_state.fingerprint()

    for _trial in range(4):
        replica = shuffled_ledger(events, rng)
        state = fold(replica)
        assert state.fingerprint() == ref_fp
        assert state.valid == ref_state.valid
        assert state.reasons == ref_state.reasons
        assert state.root == ref_state.root
        assert state.invites == ref_state.invites
        assert state.members.keys() == ref_state.members.keys()


@pytest.mark.parametrize("seed", SEEDS[:6])
def test_partial_head_folds_are_deterministic(seed):
    """Folding at an interior head set is order-independent too."""
    events = random_events(seed, n=30)
    rng = random.Random(seed * 104_729 + 3)

    reference = Ledger()
    reference.ingest(list(events))
    ids = sorted(reference.all_ids())
    heads = rng.sample(ids, min(3, len(ids)))
    ref_fp = fold(reference, heads=heads).fingerprint()

    for _trial in range(3):
        replica = shuffled_ledger(events, rng)
        assert fold(replica, heads=heads).fingerprint() == ref_fp


@pytest.mark.parametrize("seed", SEEDS[:6])
def test_incremental_growth_matches_batch(seed):
    """A replica that grew event-by-event folds like one built at once."""
    events = random_events(seed, n=25)
    batch = Ledger()
    batch.ingest(list(events))

    incremental = Ledger()
    incremental.ingest(list(events))  # ingest resolves the order internally
    assert fold(incremental).fingerprint() == fold(batch).fingerprint()


def test_now_parameter_does_not_break_determinism():
    events = random_events(99, n=30)
    a = Ledger()
    a.ingest(list(events))
    b = shuffled_ledger(events, random.Random(1))
    now = events[-1].hlc.ts + 1
    assert fold(a, now=now).fingerprint() == fold(b, now=now).fingerprint()
