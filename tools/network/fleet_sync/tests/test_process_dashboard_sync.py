from __future__ import annotations

import json

from tools.network.fleet_sync.process_dashboard_sync import (
    run_process_acceptance,
)


def test_real_processes_retry_transfer_reconnect_and_close(tmp_path) -> None:
    evidence = run_process_acceptance(tmp_path / "process-fleet")
    print(json.dumps(evidence, sort_keys=True))

    assert len(evidence["peer_ids"]) == 2
    assert len(set(evidence["peer_ids"])) == 2
    assert evidence["bytes_sent"] > 0
    assert evidence["bytes_received"] > 0
    assert evidence["retries"] >= 1
    assert evidence["transactions_applied"] == 2
    assert evidence["acknowledgements"] >= 2
    assert evidence["digests_match"] is True
    assert evidence["reconnected"] is True
    assert evidence["connections_closed"] is True
