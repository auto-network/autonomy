"""WitnessJournalV2 — the client verifier for the grouped v2 chain (jqd9q §3).

Drives real signed v2 attestations through the journal against in-memory
ancestry providers. Pins: grouped per-topic domination, monotonic-t
equivocation, split-seq / fork-prev / stale / gap, drift-observed-never-refused,
and v1 re-anchor.
"""

from __future__ import annotations

from tools.network.dag_tag import AUTHORITY, tag_dag

import pytest

from tools.network.idkit import KeyPair
from tools.network.registry import witness as ww
from tools.network.ledger.witness import (
    WitnessJournalV2,
    WitnessEquivocation,
    WitnessRetraction,
    WitnessStale,
    WitnessGap,
    WitnessError,
)

ORG = "org-1"
PUB = "cd" * 32


def _h(n: int) -> str:
    return f"{n:02x}" * 32


class FakeProvider:
    """Ancestry over a parent map: ancestry(ids) = ids + all transitive parents."""

    def __init__(self, parents: dict):
        self.parents = parents  # id -> [parent ids]

    def __contains__(self, i):
        return i in self.parents

    @tag_dag(AUTHORITY)
    def ancestry(self, ids):
        seen, stack = set(), list(ids)
        while stack:
            i = stack.pop()
            if i in seen:
                continue
            seen.add(i)
            stack.extend(self.parents.get(i, []))
        return seen


@pytest.fixture
def key():
    return KeyPair.generate()


def _att(key, seq, heads, prev, t):
    return ww.sign_attestation_v2(key, ww.build_entry_v2(ORG, seq, heads, prev, t, PUB))


def _journal(key, parents_by_topic, **kw):
    providers = {topic: FakeProvider(p) for topic, p in parents_by_topic.items()}
    return WitnessJournalV2(key.public_hex, providers, org=ORG, **kw)


def test_baseline_then_forward_advance_with_domination(key):
    # authority: h2 descends h1; storage: h4 descends h3.
    j = _journal(key, {
        "authority": {_h(1): [], _h(2): [_h(1)]},
        "storage": {_h(3): [], _h(4): [_h(3)]},
    })
    a1 = _att(key, 1, {"authority": [_h(1)], "storage": [_h(3)]}, None, 1000)
    j.admit(a1)
    assert j.seq == 1 and j.t == 1000
    e1 = a1["entry"]
    a2 = _att(key, 2, {"authority": [_h(2)], "storage": [_h(4)]}, ww.entry_id(e1), 1001)
    j.admit(a2)
    assert j.seq == 2 and j.t == 1001
    assert j.witnessed_frontiers() == {"authority": [_h(2)], "storage": [_h(4)]}


def test_decreasing_t_is_equivocation(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)]}})
    a1 = _att(key, 1, {"authority": [_h(1)]}, None, 5000)
    j.admit(a1)
    a2 = _att(key, 2, {"authority": [_h(2)]}, ww.entry_id(a1["entry"]), 4000)  # t goes back
    with pytest.raises(WitnessEquivocation):
        j.admit(a2)


def test_split_seq_is_equivocation(key):
    j = _journal(key, {"authority": {_h(1): []}})
    j.admit(_att(key, 1, {"authority": [_h(1)]}, None, 10))
    other = _att(key, 1, {"authority": [_h(2)]}, None, 10)  # same seq, different content
    with pytest.raises(WitnessEquivocation):
        j.admit(other)


def test_fork_prev_is_equivocation(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)]}})
    j.admit(_att(key, 1, {"authority": [_h(1)]}, None, 10))
    bad = _att(key, 2, {"authority": [_h(2)]}, _h(9), 11)  # prev points nowhere real
    with pytest.raises(WitnessEquivocation):
        j.admit(bad)


def test_stale_and_gap(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)], _h(3): [_h(2)]}})
    a1 = _att(key, 1, {"authority": [_h(1)]}, None, 10)
    j.admit(a1)
    a2 = _att(key, 2, {"authority": [_h(2)]}, ww.entry_id(a1["entry"]), 11)
    j.admit(a2)
    with pytest.raises(WitnessStale):
        j.admit(a1)  # seq 1 behind seq 2
    a4 = _att(key, 4, {"authority": [_h(3)]}, ww.entry_id(a2["entry"]), 12)
    with pytest.raises(WitnessGap):
        j.admit(a4)  # skips seq 3


def test_grouped_domination_retraction_names_the_topic(key):
    # authority advances fine; storage DROPS h3 for an unrelated h5 (no descent).
    j = _journal(key, {
        "authority": {_h(1): [], _h(2): [_h(1)]},
        "storage": {_h(3): [], _h(5): []},  # h5 does NOT descend h3
    })
    a1 = _att(key, 1, {"authority": [_h(1)], "storage": [_h(3)]}, None, 10)
    j.admit(a1)
    a2 = _att(key, 2, {"authority": [_h(2)], "storage": [_h(5)]}, ww.entry_id(a1["entry"]), 11)
    with pytest.raises(WitnessRetraction) as ei:
        j.admit(a2)
    assert "storage" in str(ei.value)


def test_missing_provider_for_present_topic_raises(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)]}})  # no storage provider
    a1 = _att(key, 1, {"authority": [_h(1)]}, None, 10)
    j.admit(a1)
    a2 = _att(key, 2, {"authority": [_h(2)], "storage": [_h(3)]},
              ww.entry_id(a1["entry"]), 11)
    with pytest.raises(WitnessError):
        j.admit(a2)


def test_clock_drift_is_observed_never_refused(key):
    # A wildly wrong local clock does not refuse anything.
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)]}},
                 max_skew_s=300, clock=lambda: (10 ** 9, 0.0))
    a1 = _att(key, 1, {"authority": [_h(1)]}, None, 10)
    j.admit(a1)
    a2 = _att(key, 2, {"authority": [_h(2)]}, ww.entry_id(a1["entry"]), 11)
    j.admit(a2)  # huge skew, still admitted
    assert j.seq == 2
    assert len(j.drift_samples) == 2
    assert j.drift_samples[0][0] == 10  # the entry's t is recorded


def test_absent_topic_means_unchanged(key):
    j = _journal(key, {
        "authority": {_h(1): [], _h(2): [_h(1)]},
        "storage": {_h(3): []},
    })
    a1 = _att(key, 1, {"authority": [_h(1)], "storage": [_h(3)]}, None, 10)
    j.admit(a1)
    # seq 2 carries only authority; storage is unchanged, not dropped.
    a2 = _att(key, 2, {"authority": [_h(2)]}, ww.entry_id(a1["entry"]), 11)
    j.admit(a2)
    assert j.witnessed_frontiers() == {"authority": [_h(2)], "storage": [_h(3)]}


def test_v1_last_reanchors_on_first_v2(key):
    j = _journal(key, {"authority": {_h(1): []}})
    # Seed the journal's _last with a v1 attestation (a migrating client).
    v1 = ww.sign_attestation(key, ww.build_entry(ORG, "ledger", 7, [_h(9)], _h(8), PUB))
    j._last = v1
    assert j._last_is_v1()
    a1 = _att(key, 1, {"authority": [_h(1)]}, None, 10)
    j.admit(a1)  # first v2 is a fresh baseline despite the v1 last
    assert j.seq == 1 and j.reanchored_from_v1 is True


# -- section 4: the equivocation proofs verify (EquivocationProof.verify on v2) --

from tools.network.ledger.witness import EquivocationProof


def test_decreasing_t_proof_verifies(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)]}})
    a1 = _att(key, 1, {"authority": [_h(1)]}, None, 5000)
    j.admit(a1)
    a2 = _att(key, 2, {"authority": [_h(2)]}, ww.entry_id(a1["entry"]), 4000)
    with pytest.raises(WitnessEquivocation) as ei:
        j.admit(a2)
    proof = ei.value.proof
    assert proof.kind == "decreasing-t"
    assert proof.verify(key.public_hex) is True
    assert proof.verify(KeyPair.generate().public_hex) is False  # wrong pin


def test_split_seq_proof_verifies_on_v2(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): []}})
    j.admit(_att(key, 1, {"authority": [_h(1)]}, None, 10))
    with pytest.raises(WitnessEquivocation) as ei:
        j.admit(_att(key, 1, {"authority": [_h(2)]}, None, 10))
    assert ei.value.proof.verify(key.public_hex) is True


def test_fork_prev_proof_verifies_on_v2(key):
    j = _journal(key, {"authority": {_h(1): [], _h(2): [_h(1)]}})
    j.admit(_att(key, 1, {"authority": [_h(1)]}, None, 10))
    with pytest.raises(WitnessEquivocation) as ei:
        j.admit(_att(key, 2, {"authority": [_h(2)]}, _h(9), 11))  # bad prev
    assert ei.value.proof.verify(key.public_hex) is True


def test_cross_version_pair_never_verifies(key):
    v1 = ww.sign_attestation(key, ww.build_entry(ORG, "ledger", 1, [_h(1)], None, PUB))
    v2 = _att(key, 1, {"authority": [_h(1)]}, None, 10)
    assert EquivocationProof(v1, v2, "split-seq").verify(key.public_hex) is False
