"""Direct-path checkpoint installs: personal scope hands off to the runtime
installer; a pull that received a base and still failed backs off long.

Live 2026-09-06: home's direct pull of SJC's personal checkpoint (343 MB)
installed inline in the dashboard process, whose live handles made the
quiescence gate refuse; the 5s backoff then re-pulled the whole base every
round. This file pins both halves of the fix.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytestmark = pytest.mark.asyncio

from tools.graph.db import GraphDB
from tools.network import fleet_roster, fleet_sync_scheduler as fss
from tools.network.fleet_sync_connection import FleetSyncQuiescenceError
from tools.network.idkit import KeyPair, Subject, issue_cert, canonical_json


def _fleet():
    root = KeyPair.from_private_hex("10" * 32)
    server = KeyPair.from_private_hex("20" * 32)
    client = KeyPair.from_private_hex("30" * 32)
    entries = (
        fleet_roster.enroll(root, machine_id="40" * 32, machine_pub=server.public_hex),
        fleet_roster.enroll(root, machine_id="50" * 32, machine_pub=client.public_hex),
    )
    process = KeyPair.from_private_hex("70" * 32)
    now = int(time.time())
    cert = issue_cert(
        client, process.public_hex, scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id="50" * 32),
        not_before=now - 30, not_after=now + 300,
    )
    return root, server, client, entries, process, cert


def _scheduler(tmp_path):
    root, server, client, entries, process, cert = _fleet()
    personal = tmp_path / "personal.db"
    db = GraphDB(personal)
    try:
        db.activate_fleet_sync_writers(client.public_hex)
    finally:
        db.close()
    config = fss.FleetSyncRuntimeConfig(
        machine_key=process,
        personal_root_pub=root.public_hex,
        roster_entries=lambda: entries,
        peer_addresses=lambda: {},
        personal_db_path=personal,
        roster_machine_pub=client.public_hex,
        delegation_cert=cert,
        require_delegation=True,
    )
    sched = fss.FleetSyncScheduler(config)
    sched._roster_snapshot = entries
    sched.authenticator.authorize(client.public_hex)
    return sched, server.public_hex


class _Channel:
    """A peer that serves exactly one checkpoint (begin, one file, end)."""

    def __init__(self, server_pub: str, epoch: str):
        body = b"base-bytes"
        self.frames = [
            canonical_json({
                "v": fss.FLEET_SYNC_PROTOCOL_VERSION, "kind": "checkpoint.begin",
                "file_count": 1, "total_bytes": len(body),
                "source_machine_pub": server_pub, "roster_epoch": epoch,
            }),
            fss.encode_checkpoint_file("checkpoint/base-000001.chunk", body),
            canonical_json({
                "v": fss.FLEET_SYNC_PROTOCOL_VERSION, "kind": "checkpoint.end",
                "file_count": 1, "total_bytes": len(body),
            }),
        ]
        self.sent = []

    async def send_message(self, raw):
        self.sent.append(json.loads(raw))

    async def recv_message_stream(self):
        for frame in self.frames:
            yield frame, False

    async def close(self):
        pass


@pytest.fixture
def direct(monkeypatch, tmp_path):
    sched, server_pub = _scheduler(tmp_path)
    channel = _Channel(server_pub, sched._current_epoch())

    async def connect(*_a, **_k):
        return channel

    monkeypatch.setattr(fss, "fleet_direct_connect", connect)
    return sched, server_pub, channel


async def test_personal_checkpoint_hands_off_to_the_runtime_installer(direct):
    sched, server_pub, channel = direct
    seen = {}

    async def installer(stage, *, source_machine_pub):
        seen["source"] = source_machine_pub
        seen["file"] = (stage / "checkpoint" / "base-000001.chunk").read_bytes()
        seen["stage"] = stage

    sched.personal_checkpoint_installer = installer
    inline = []
    sched._install_direct_checkpoint = lambda *a: inline.append(a)  # must not run

    await sched._pull_scope(server_pub, ["ws://peer:9410"], "personal")

    handoff = [t for t in asyncio.all_tasks()
               if t.get_name() == "fleet-direct-personal-install"]
    assert len(handoff) == 1
    await handoff[0]
    assert seen["source"] == server_pub
    assert seen["file"] == b"base-bytes"
    assert not seen["stage"].exists(), "handoff owns and cleans the stage"
    assert inline == []
    assert server_pub not in sched._next_attempt, "a handoff is not a failure"


async def test_failed_pull_after_a_received_checkpoint_backs_off_long(direct, caplog):
    sched, server_pub, channel = direct
    sched.personal_checkpoint_installer = None

    async def refuse(*_a):
        raise FleetSyncQuiescenceError("database still has 3 live production connection(s)")

    sched._install_direct_checkpoint = refuse
    with pytest.raises(FleetSyncQuiescenceError):
        await sched._pull_scope(server_pub, ["ws://peer:9410"], "personal")
    wait = sched._next_attempt[server_pub] - asyncio.get_running_loop().time()
    assert wait >= fss.CHECKPOINT_FAILURE_BACKOFF_S - 1
    assert any("checkpoint received" in r.getMessage() and "not asking again" in r.getMessage()
               for r in caplog.records)


async def test_ordinary_failure_keeps_the_short_backoff(direct):
    sched, server_pub, channel = direct
    channel.frames = []   # peer closes without a frame: no checkpoint involved
    with pytest.raises(Exception):
        await sched._pull_scope(server_pub, ["ws://peer:9410"], "personal")
    wait = sched._next_attempt[server_pub] - asyncio.get_running_loop().time()
    assert wait <= sched.config.max_backoff


async def test_founded_ledger_refuses_the_checkpoint_at_the_offer(direct, caplog):
    """An origin store never accepts a peer's checkpoint: the refusal lands
    on checkpoint.begin, before any chunk is staged, and backs off long so
    the peer is not asked to rebuild the base every round."""
    import sqlite3
    from tools.network.ledger import store as ledger_store

    sched, server_pub, channel = direct
    conn = sqlite3.connect(sched.config.personal_db_path)
    try:
        conn.executescript(ledger_store._SCHEMA)
        conn.execute(
            "INSERT INTO ledger_events(event_id,event_type,author_key,hlc_ts,hlc_count,wire)"
            " VALUES('ev-1',?, 'ab', 1, 0, x'00')", (sorted(ledger_store.EVENT_TYPES)[0],),
        )
        conn.execute("INSERT INTO ledger_heads(event_id) VALUES('ev-1')")
        conn.commit()
    finally:
        conn.close()
    staged = []
    sched._install_direct_checkpoint = lambda *a: staged.append(a)

    with pytest.raises(fss.FleetSyncFoundedLedgerRefusal):
        await sched._pull_scope(server_pub, ["ws://peer:9410"], "personal")
    assert staged == []
    assert not list(sched.config.personal_db_path.parent.glob("fleet-direct-received-*"))
    wait = sched._next_attempt[server_pub] - asyncio.get_running_loop().time()
    assert wait >= fss.CHECKPOINT_FAILURE_BACKOFF_S - 1


async def test_direct_pull_records_the_connected_address_and_path(direct):
    """The telemetry row for a pull names the candidate that connected."""
    from dataclasses import replace

    sched, server_pub, channel = direct
    seen = []
    sched.config = replace(sched.config, telemetry_recorder=lambda peer, **v: seen.append((peer, v)))
    channel.frames = []   # connect succeeds, stream ends without a summary
    with pytest.raises(Exception):
        await sched._pull_scope(server_pub, ["ws://100.122.70.30:9410"], "personal")
    (peer, values), = seen
    assert peer == server_pub
    assert values["address"] == "ws://100.122.70.30:9410"
    assert values["path_class"] == "tailnet"
    assert values["outcome"] == "failed"
