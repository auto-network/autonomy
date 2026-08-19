"""The wrong ancestry fails loudly instead of answering the wrong question.

Two graphs, disjoint identifier spaces, identical shapes: both are
one-argument callables named ``ancestry`` returning a frozenset. Passing the
storage one where the authority one is meant does not raise on its own — it
reaches ``state_covers``, which asks a graph of storage ``state_id``s whether
it contains a set of ledger loss heads. The answer is "never" or "vacuously
always" depending on how the closure treats identifiers it has not seen, and
either way a write silently always advances or never does.

These tests exist because that mistake was made twice in one afternoon with
the correct answer already written in two places, and survived three review
rounds because the test double that would have caught it was untagged.
"""

from __future__ import annotations

import pytest

from tools.network.dag_tag import AUTHORITY, STORAGE, dag_of, require_dag, tag_dag


@tag_dag(AUTHORITY)
def _authority(ids):
    return frozenset(ids)


@tag_dag(STORAGE)
def _storage(ids):
    return frozenset(ids)


def _untagged(ids):
    return frozenset(ids)


# ── the tag survives the ways a callable is actually passed ──


def test_a_bound_method_keeps_its_tag():
    """THE ONE THAT MATTERS for real use: both ancestries are reached as bound
    methods (``ledger.ancestry``, ``store.ancestry``), and an attribute set on
    the function must still be visible through the binding or the check is
    dead in production while passing here."""

    class Thing:
        @tag_dag(AUTHORITY)
        def ancestry(self, ids):
            return frozenset(ids)

    assert dag_of(Thing().ancestry) == AUTHORITY


def test_the_real_ancestries_declare_opposite_dags():
    """Pinned against the live objects, not doubles — the tags are only worth
    anything if the two sources actually carry them."""
    from tools.network.ledger.ledger import Ledger
    from tools.network.storagekit.keycontrol import KeyControlStore

    assert dag_of(Ledger.ancestry) == AUTHORITY
    assert dag_of(KeyControlStore.ancestry) == STORAGE


# ── what require_dag accepts and refuses ──


def test_the_expected_dag_passes():
    require_dag(_authority, AUTHORITY, "seam")


def test_the_other_dag_is_refused_by_name():
    with pytest.raises(TypeError) as excinfo:
        require_dag(_storage, AUTHORITY, "state_covers")

    message = str(excinfo.value)
    assert "state_covers" in message, "the seam must be named — a caller is "\
        "holding two plausible callables and needs to know which this wants"
    assert AUTHORITY in message and STORAGE in message


def test_an_UNTAGGED_callable_is_refused_TOO():
    """The case the whole mechanism is for.

    A wrong-DAG callable is the mistake; an UNTAGGED one is how the mistake
    hides. The storagekit test double was untagged and wired to the ledger's
    ancestry, so it was right by accident and taught the contract wrong to
    everyone who read it. Refusing untagged forces every stand-in to answer
    "which DAG is this?" — the question whose absence let a wrong ancestry
    stay green through three reviews.
    """
    with pytest.raises(TypeError, match="declares no DAG"):
        require_dag(_untagged, AUTHORITY, "state_covers")


def test_a_lambda_is_refused():
    """The most convenient thing to reach for in a test is the one with no
    tag, so it must not be the one that slips through."""
    with pytest.raises(TypeError):
        require_dag(lambda ids: frozenset(ids), AUTHORITY, "seam")


# ── the guard is live on the real path ──


def test_the_storage_ancestry_is_refused_at_the_safety_predicate():
    """Not a unit test of require_dag — proof it is WIRED where it matters.

    ``state_covers`` is the leaf both ``seal_revision`` and ``create_object``
    funnel through, so a refusal here is a refusal on every write at every
    tier. Before the check, this call returned a wrong answer quietly.
    """
    from tools.network.storagekit.lifecycle import state_covers

    descriptor = type("D", (), {"covered_loss_heads": ("e1",)})()

    with pytest.raises(TypeError, match="state_covers"):
        state_covers(descriptor, ("e1",), _storage)


def test_the_authority_ancestry_still_works_there():
    """The guard must not have made the correct path harder."""
    from tools.network.storagekit.lifecycle import state_covers

    descriptor = type("D", (), {"covered_loss_heads": ("e1",)})()

    assert state_covers(descriptor, ("e1",), _authority) is True
