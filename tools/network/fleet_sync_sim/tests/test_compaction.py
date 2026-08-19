from tools.network.fleet_sync_sim.compaction import run_compaction_simulation


def test_active_roster_minimum_and_kick_control_compaction() -> None:
    evidence = run_compaction_simulation()
    assert evidence["status"] == "pass"
    assert evidence["unsafe_variant_resurrected"] is True
    assert evidence["guarded"]["frontier_before_branch"] == 10
    assert evidence["guarded"]["frontier_after_branch"] == 10
    assert evidence["guarded"]["frontier_after_timeout"] == 10
    assert evidence["guarded"]["compaction_refused_before_kick"] is True
    assert evidence["kick"]["active_after_all_remaining_observers"] == ["A", "B"]
    assert evidence["canonical_base"]["horizon"] == 30
    assert set(evidence["canonical_base"]["acknowledgments"]) == {"A", "B"}
    assert evidence["kick"]["old_peer_refused"] is True
    assert evidence["kick"]["fresh_identity_admitted"] is True
