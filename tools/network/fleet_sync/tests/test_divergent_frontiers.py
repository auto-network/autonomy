import asyncio

from tools.network.fleet_sync.divergent_frontiers import (
    run_divergent_frontier_simulation,
)


def test_divergent_frontiers_converge_over_real_relaykit_channels() -> None:
    evidence = asyncio.run(run_divergent_frontier_simulation())
    assert evidence["status"] == "pass"
    assert evidence["no_source_held_complete_union"] is True
    assert evidence["partial_peer"]["served_while_incomplete"] > 0
    assert evidence["partial_peer"]["later_completed"] is True
    assert evidence["partial_peer"]["served_after_promotion"] > 0
    assert evidence["receiver"]["latest_merged_frontier"] == 30
    assert len(evidence["receiver"]["materialized_rows"]) == 3
    required_event_fields = {
        "source", "receiver", "artifact", "sbn", "esi", "packet_bytes",
        "source_complete", "receiver_prior_ranges",
    }
    assert evidence["symbol_trace"]
    assert all(set(event) == required_event_fields for event in evidence["symbol_trace"])
    assert evidence["globally_distinct_artifact_packet_ids"] is True
