"""Witness v2 store: one grouped chain per org, non-decreasing t, migration.

Section 2 of auto-jqd9q, at the store layer.
"""

from __future__ import annotations

from tools.network.registry.store import RegistryStore
from tools.network.registry.witness import validate_entry_v2

ORG = "org-uuid-1"
PUB = "ab" * 32


def _h(n):
    return f"{n:02x}" * 32


def test_first_append_is_seq_one_and_carries_the_topic():
    s = RegistryStore(":memory:")
    att = s.append_witness_v2(ORG, "authority", [_h(1)], PUB, now=1000)
    e = validate_entry_v2(att["entry"])
    assert e["seq"] == 1 and e["prev"] is None and e["t"] == 1000
    assert e["heads"] == {"authority": [_h(1)]}


def test_second_topic_carries_the_first_forward():
    s = RegistryStore(":memory:")
    s.append_witness_v2(ORG, "authority", [_h(1)], PUB, now=1000)
    att = s.append_witness_v2(ORG, "storage", [_h(2)], PUB, now=1001)
    e = att["entry"]
    assert e["seq"] == 2 and e["heads"] == {"authority": [_h(1)], "storage": [_h(2)]}
    assert e["prev"] == s.witness_since_v2(ORG, 0)[0]["entry_id"]


def test_reattesting_the_same_map_is_idempotent():
    s = RegistryStore(":memory:")
    s.append_witness_v2(ORG, "authority", [_h(1)], PUB, now=1000)
    a = s.append_witness_v2(ORG, "authority", [_h(1)], PUB, now=1005)  # same heads
    assert a["entry"]["seq"] == 1  # no new entry


def test_t_is_non_decreasing_even_if_now_goes_backward():
    s = RegistryStore(":memory:")
    s.append_witness_v2(ORG, "authority", [_h(1)], PUB, now=5000)
    att = s.append_witness_v2(ORG, "authority", [_h(2)], PUB, now=4000)  # clock skew back
    assert att["entry"]["t"] == 5000  # max(now, tip.t), never backdated


def test_chain_links_and_grows():
    s = RegistryStore(":memory:")
    s.append_witness_v2(ORG, "authority", [_h(1)], PUB, now=1)
    s.append_witness_v2(ORG, "authority", [_h(2)], PUB, now=2)
    s.append_witness_v2(ORG, "storage", [_h(3)], PUB, now=3)
    chain = s.witness_since_v2(ORG, 0)
    assert [c["entry"]["seq"] for c in chain] == [1, 2, 3]
    for prev, nxt in zip(chain, chain[1:]):
        assert nxt["entry"]["prev"] == prev["entry_id"]  # hash-linked


def test_migration_seeds_v2_baseline_from_v1_tips():
    s = RegistryStore(":memory:")
    # Two v1 chains exist first.
    s.append_witness(ORG, "ledger", [_h(1)], PUB, now=100)
    s.append_witness(ORG, "storage", [_h(2)], PUB, now=101)
    # First v2 append triggers the one-time migration.
    att = s.append_witness_v2(ORG, "authority", [_h(3)], PUB, now=200)
    chain = s.witness_since_v2(ORG, 0)
    # seq 1 is the migrated baseline: ledger tip -> authority group, storage -> storage.
    baseline = chain[0]["entry"]
    assert baseline["seq"] == 1 and baseline["prev"] is None
    assert baseline["heads"] == {"authority": [_h(1)], "storage": [_h(2)]}
    # The v2 append is seq 2, chaining onto the baseline, and it advances authority.
    assert att["entry"]["seq"] == 2
    assert att["entry"]["heads"] == {"authority": [_h(3)], "storage": [_h(2)]}
    # The archived v1 chain is left intact.
    assert s.witness_tip(ORG, "ledger")["entry"]["heads"] == [_h(1)]


def test_migration_runs_once():
    s = RegistryStore(":memory:")
    s.append_witness(ORG, "ledger", [_h(1)], PUB, now=100)
    s.append_witness_v2(ORG, "authority", [_h(2)], PUB, now=200)
    s.append_witness_v2(ORG, "authority", [_h(3)], PUB, now=201)
    seqs = [c["entry"]["seq"] for c in s.witness_since_v2(ORG, 0)]
    assert seqs == [1, 2, 3]  # baseline + two appends, no second baseline
