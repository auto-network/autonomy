"""Stalest-first bounded peer selection replaces full fan-out."""

import time
from pathlib import Path

from tools.network.fleet_sync.harness import HarnessFleet
from tools.network.fleet_sync_scheduler import rank_peers


def test_ranking_is_stalest_first_bounded_and_deterministic() -> None:
    weights = {"aa": 500, "bb": 100, "cc": 900}
    # Never-synced ranks ahead of every recorded success.
    assert rank_peers(["aa", "bb", "cc", "dd"], weights, limit=2) == [
        "dd", "bb",
    ]
    # Among recorded successes: oldest first; ties break on the key.
    assert rank_peers(["aa", "bb", "cc"], weights, limit=3) == [
        "bb", "aa", "cc",
    ]
    assert rank_peers(["aa", "bb"], {"aa": 5, "bb": 5}, limit=1) == ["aa"]
    # Zero limit means unbounded, order preserved.
    assert rank_peers(["cc", "aa"], weights, limit=0) == ["aa", "cc"]


def test_random_source_breaks_lockstep_but_keeps_fairness() -> None:
    """Twenty fresh machines must not all pick the same first peer, and a
    peer far staler than the rest must still be picked."""
    import collections
    import random

    from tools.network.fleet_sync_scheduler import RANK_POOL_FACTOR

    peers = [f"{i:02d}" * 32 for i in range(20)]
    firsts = collections.Counter()
    for seed in range(200):
        rng = random.Random(seed)
        firsts[rank_peers(peers, {}, limit=1, rng=rng)[0]] += 1
    assert len(firsts) >= 10, firsts          # spread, not one hot server
    assert max(firsts.values()) < 60         # no single peer dominates

    # Fairness bound: with one very stale peer, it is always in the pool
    # and is picked at least as often as 1/pool.
    weights = {pub: 1_000 for pub in peers}
    weights[peers[7]] = 1
    picks = collections.Counter(
        rank_peers(peers, weights, limit=1, rng=random.Random(seed))[0]
        for seed in range(300)
    )
    assert picks[peers[7]] >= 300 // max(RANK_POOL_FACTOR, 3) // 2


def test_bounded_rounds_converge_and_cap_pull_volume(tmp_path: Path) -> None:
    fleet = HarnessFleet(
        tmp_path / "fleet", size=4, max_concurrent_pulls=2
    ).build()
    try:
        fleet.start_all()
        started = time.monotonic()
        for machine in range(4):
            fleet.write(machine, f"m{machine}", f"from {machine}")
        fleet.wait_converged(timeout=120.0)
        elapsed = time.monotonic() - started
        pulls = fleet.total_successful_pulls()
        rounds = max(1.0, elapsed / 0.06)
        per_machine_per_round = pulls / 4 / rounds
        fleet.evidence["pulls"] = pulls
        fleet.evidence["pulls_per_machine_per_round"] = round(
            per_machine_per_round, 3
        )
        # The cap binds: full fan-out would allow up to 3 successful pulls
        # per machine per round; bounded selection allows at most 2.
        assert per_machine_per_round <= 2.2
        fleet.write_evidence(tmp_path / "weighted.json")
    finally:
        fleet.shutdown()


def test_returning_peer_is_prioritized(tmp_path: Path) -> None:
    fleet = HarnessFleet(
        tmp_path / "fleet", size=3, max_concurrent_pulls=1
    ).build()
    try:
        fleet.start_all()
        fleet.write(0, "before", "before the absence")
        fleet.wait_converged(timeout=120.0)

        fleet.stop(2, kill=True)
        fleet.write(0, "during-1", "authored while 2 was away")
        fleet.write(1, "during-2", "also while away")
        fleet.wait(
            lambda: fleet.has(1, "during-1") and fleet.has(0, "during-2"),
            timeout=120.0, label="live pair keeps syncing",
        )

        # On return, every peer's stalest slot is machine 2 — with only ONE
        # pull slot per round, it converges promptly anyway.
        fleet.start(2)
        fleet.wait(
            lambda: fleet.has(2, "during-1") and fleet.has(2, "during-2"),
            timeout=120.0, label="returning peer catch-up",
        )
        fleet.wait_converged(timeout=120.0)
    finally:
        fleet.shutdown()
