from __future__ import annotations

import json

from tools.network.fleet_sync.process_factor_sync import (
    run_factor_process_acceptance,
)


def test_two_dashboards_sync_a_factor_change_after_bootstrap(tmp_path) -> None:
    evidence = run_factor_process_acceptance(tmp_path / "factor-fleet")
    print(json.dumps(evidence, sort_keys=True))

    assert len(set(evidence["peer_ids"])) == 2
    # The initial factor and the subsequent LWW change both crossed real
    # scheduler processes over the roster-authenticated channel.
    assert evidence["initial_factor_synced"] is True
    assert evidence["factor_change_synced"] is True
    assert evidence["receiver_final_armor"] == evidence["expected_final_armor"]
    # Converged byte-identically — an ordinary authored-catalog crossing.
    assert evidence["factor_tables_identical"] is True
    assert evidence["transactions_applied"] >= 2
    assert evidence["acknowledgements"] >= 2
    assert evidence["reconnected"] is True
    assert evidence["connections_closed"] is True
