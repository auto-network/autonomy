"""The process that delegates a pull does not report carrying it (auto-ew9wf).

Both processes write the same machine-homed telemetry store, and the key is
(peer, channel, direction, scope) — an upsert. So a row from the DELEGATING
side can only be wrong in one of two ways: under channel "relay" it would
overwrite the connector's real measurements with zero bytes, and under
"direct" it credits that key with a success and refreshes its
``last_outcome`` and ``last_success_at_ns`` for a pull direct had just failed to
carry, so the direct channel's readout claims successes that never happened.
``fleet_sync_telemetry.direct_pull_fresh`` is built on exactly those two fields
to let a relay loop defer to a working direct path; it has no production caller
today, so this corrupted the readout rather than misrouting anything live.

Found by auto-0909-161758 reviewing auto-ew9wf.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync_scheduler import (
    FleetSyncPeerUnreachable,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
)
from tools.network.idkit import KeyPair

#: Port 1 on loopback: refused in microseconds, so "direct is exhausted" is
#: reached without waiting for a real timeout.
DEAD_ADDRESS = "ws://127.0.0.1:1"


def _scheduler(tmp_path: Path, rows: list, delegate):
    key = KeyPair.generate()
    path = tmp_path / "left.db"
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(key.public_hex)
    finally:
        db.close()
    return FleetSyncScheduler(FleetSyncRuntimeConfig(
        machine_key=key,
        personal_root_pub=KeyPair.generate().public_hex,
        roster_entries=lambda: (),
        peer_addresses=lambda: {},
        personal_db_path=path,
        poll_interval=0.03,
        min_backoff=0.01,
        max_backoff=0.05,
        connect_timeout=1.0,
        telemetry_recorder=lambda peer, **values: rows.append(values),
        relay_pull=delegate,
    ))


def test_a_successful_delegation_writes_no_row_on_this_side(tmp_path):
    rows: list = []

    async def delegate(**kwargs):
        return {"outcome": "ok", "channel": "relay"}

    scheduler = _scheduler(tmp_path, rows, delegate)

    async def run():
        return await scheduler._pull_scope(
            "ab" * 32, (DEAD_ADDRESS,), "personal",
        )

    assert asyncio.run(run()) == {"outcome": "ok", "channel": "relay"}
    assert rows == [], (
        "the delegating process recorded a pull it did not carry: "
        f"{rows!r}"
    )


def test_a_refused_delegation_still_reports_the_direct_failure(tmp_path):
    """The row that SHOULD exist is not suppressed with it. Direct genuinely
    failed here, and that failure is this process's own observation."""
    rows: list = []

    async def delegate(**kwargs):
        raise ConnectionError("no tunnel negotiated the capability")

    scheduler = _scheduler(tmp_path, rows, delegate)

    async def run():
        with pytest.raises(FleetSyncPeerUnreachable):
            await scheduler._pull_scope("ab" * 32, (DEAD_ADDRESS,), "personal")

    asyncio.run(run())
    assert len(rows) == 1, rows
    assert rows[0]["channel"] == "direct"
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["bytes_received"] == 0
