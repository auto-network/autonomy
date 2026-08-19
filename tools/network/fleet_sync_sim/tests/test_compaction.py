from pathlib import Path

import pytest

from tools.network.fleet_sync_sim.codec import Mutation
from tools.network.fleet_sync_sim.compaction import (
    DurableOrigin,
    PrefixStore,
    WatermarkError,
    run_compaction_simulation,
)


def test_active_roster_minimum_and_kick_control_compaction() -> None:
    evidence = run_compaction_simulation()
    assert evidence["status"] == "pass"
    assert evidence["guarded"]["frontier_before_branch"] == 10
    assert evidence["guarded"]["frontier_after_branch"] == 10
    assert evidence["guarded"]["frontier_after_timeout"] == 10
    assert evidence["guarded"]["compaction_refused_before_kick"] is True
    assert evidence["kick"]["active_after_all_remaining_observers"] == ["A", "B"]
    assert evidence["kick"]["stale_roster_round_refused"] is True
    assert evidence["canonical_base"]["horizon"] == 30
    assert evidence["canonical_base"]["gc_floor"] == 30
    assert evidence["canonical_base"]["included_cuts"] == {"A": 50, "B": 30}
    assert evidence["canonical_base"]["post_floor_tombstones_retained"] == 1
    assert set(evidence["canonical_base"]["acknowledgments"]) == {"A", "B"}
    assert evidence["canonical_base"]["ack_after_durable_install"] is True
    assert evidence["kick"]["old_peer_refused"] is True
    assert evidence["kick"]["fresh_identity_admitted"] is True
    assert evidence["watermark_contract"]["crash_before_advertisement_safe"] is True
    assert evidence["watermark_contract"]["sole_copy_advertisement_refused"] is True
    assert "write refused before time" in evidence["watermark_contract"][
        "backward_write_refusal"
    ]
    assert evidence["restore"]["forward_write_accepted"] is True


def _mutation(identity: str, timestamp: int) -> Mutation:
    return Mutation(
        "sources", (identity,), timestamp, False,
        (("id", identity), ("metadata", {}), ("publication_state", "raw"),
         ("title", identity), ("type", "note")),
    )


def test_watermark_is_earned_not_assigned(tmp_path: Path) -> None:
    origin = DurableOrigin(tmp_path / "A", "A", 1)
    custodian = PrefixStore(tmp_path / "B" / "prefixes")
    try:
        origin.author(_mutation("one", 10))
        cut = origin.freeze_cut()
        assert cut == 10
        assert origin.advertised_watermark == 0
        with pytest.raises(WatermarkError, match="write refused before time 11"):
            origin.author(_mutation("late", 10))
        artifact = origin.seal_cut(cut)
        with pytest.raises(WatermarkError, match="surviving durable holder"):
            origin.advertise(artifact, {"A": origin.store, "B": custodian})
        custodian.copy_from(artifact, origin.store)
        receipt = origin.advertise(
            artifact, {"A": origin.store, "B": custodian}
        )
        assert receipt.watermark == 10
        assert receipt.holders == ("A", "B")
    finally:
        origin.close()


def test_uncertain_restore_cannot_author_until_floor_recovered(tmp_path: Path) -> None:
    origin = DurableOrigin(tmp_path / "A", "A", 1, uncertain_startup=True)
    try:
        with pytest.raises(WatermarkError, match="startup watermark recovery"):
            origin.author(_mutation("blocked", 20))
        origin.recover_startup_floor(30)
        with pytest.raises(WatermarkError, match="write refused before time 31"):
            origin.author(_mutation("old", 30))
        origin.author(_mutation("new", 31))
    finally:
        origin.close()
