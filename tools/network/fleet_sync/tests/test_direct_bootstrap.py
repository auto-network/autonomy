"""Checkpoint bootstrap over the direct path, and serve-onward through it."""

import sqlite3
from pathlib import Path

from tools.network.fleet_sync.harness import HarnessFleet


def _checkpoints_received(db_path: Path) -> int:
    try:
        with sqlite3.connect(
            f"file:{db_path}?mode=ro&immutable=1", uri=True
        ) as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(checkpoints_received),0) "
                "FROM fleet_sync_peer_state"
            ).fetchone()[0])
    except sqlite3.Error:
        return 0


def test_bootstrap_chain_without_relay(tmp_path: Path) -> None:
    """A joins nothing; B bootstraps from A via a direct checkpoint; A goes
    away; empty C bootstraps FROM B — possible only because B, whose
    installed journal cannot replay retired history, serves a checkpoint it
    builds from its own live state. Deltas then flow to C normally."""
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()

    # A starts alone and authors state before anyone else exists online.
    fleet.start(0)
    for note in range(5):
        fleet.write(0, f"seed-{note}", f"pre-fleet note {note}")

    # B starts empty: its pull declares bootstrap and installs a checkpoint.
    fleet.start(1)
    try:
        fleet.wait(
            lambda: all(fleet.has(1, f"seed-{n}") for n in range(5)),
            timeout=120.0, label="B bootstrap",
        )
        assert _checkpoints_received(fleet.machines[1].db_path) >= 1

        # A leaves. C starts empty and can only reach B.
        fleet.stop(0, kill=True)
        fleet.start(2)
        fleet.wait(
            lambda: all(fleet.has(2, f"seed-{n}") for n in range(5)),
            timeout=120.0, label="C bootstrap via B",
        )
        assert _checkpoints_received(fleet.machines[2].db_path) >= 1

        # Post-bootstrap deltas flow from B to C.
        fleet.write(1, "after-chain", "authored after the chain")
        fleet.wait(
            lambda: fleet.has(2, "after-chain"),
            timeout=120.0, label="post-bootstrap delta",
        )
        assert fleet.digest(1) == fleet.digest(2)
    finally:
        fleet.shutdown()
