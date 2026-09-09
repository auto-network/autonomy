"""Sweep bootstrap over the direct path, and serve-onward through it."""

import sqlite3
from pathlib import Path

from tools.network.fleet_sync.harness import HarnessFleet


def test_bootstrap_chain_without_relay(tmp_path: Path) -> None:
    """A joins nothing; B bootstraps from A over the direct path; A goes
    away; empty C bootstraps FROM B — possible only because B, whose journal
    cannot replay retired history, sweeps its own live state. Deltas then
    flow to C normally."""
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
    try:
        # A starts alone and authors state before anyone else exists online.
        fleet.start(0)
        for note in range(5):
            fleet.write(0, f"seed-{note}", f"pre-fleet note {note}")

        # B starts empty: its pull declares bootstrap and sweeps.
        fleet.start(1)
        fleet.wait(
            lambda: all(fleet.has(1, f"seed-{n}") for n in range(5)),
            timeout=120.0, label="B bootstrap",
        )

        # A leaves. C starts empty and can only reach B.
        fleet.stop(0, kill=True)
        fleet.start(2)
        fleet.wait(
            lambda: all(fleet.has(2, f"seed-{n}") for n in range(5)),
            timeout=120.0, label="C bootstrap via B",
        )

        # Post-bootstrap deltas flow from B to C.
        fleet.write(1, "after-chain", "authored after the chain")
        fleet.wait(
            lambda: fleet.has(2, "after-chain"),
            timeout=120.0, label="post-bootstrap delta",
        )
        assert fleet.digest(1) == fleet.digest(2)
    finally:
        fleet.shutdown()
