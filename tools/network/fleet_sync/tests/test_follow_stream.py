"""A follower sees the organization as one author with one position (bead
auto-8cpnm; design of record graph://5f2f5a49-00d v7 §10.2; constitution
graph://6ad52a52-f75 principle 5, pieces 4 and 6).

The serve for a follow admission: every header names the organization's id
as its origin, never a machine; the reply opens with exactly one frontier
key, the org at F (this member's minimum over its covered persona write
floors); nothing above F is served; a delta serves (c, F] across origins in
timestamp order; no write-floor frames, no breadcrumb, no served-ack row; a
cursor below retention is refused too-old; a member with no covered persona
floor refuses with the typed record. A fleet peer's serve is untouched (the
rest of the suite is its byte-identity witness).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync_channel import Admission
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_sync_scheduler import (
    FOLLOW_NO_FRONTIER_KIND, PULL_BEGIN_KIND, PULL_TOO_OLD_KIND,
    SQLiteFleetSyncStore, _DONE_MAGIC, _REFUSAL_MAGIC, _TRANSACTION_MAGIC,
    decode_transaction_header, encode_pull_request, roster_epoch,
)
from tools.network.idkit import KeyPair, Subject, issue_cert

# The organization's id on the wire is its ledger genesis id: 64 hex.
ORG = "cd" * 32


def _insert_source(conn, identity: str, state: str = "published") -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at,"
        "publication_state) VALUES(?,?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-20T00:00:00Z",
         "2026-09-20T00:00:00Z", state),
    )


def _persona_cert(persona: KeyPair, machine: KeyPair):
    now = int(time.time())
    return issue_cert(
        persona, machine.public_hex, scope=["fleet:sync"], org=ORG,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 60, not_after=now + 3600,
    )


def _build(tmp_path: Path, *, seal_floor_at: int | None):
    """A store with three published sources at 100, 200, 300 written by this
    machine, and (optionally) a persona write floor covering position
    ``seal_floor_at`` so the org frontier F equals it."""
    root, machine, peer = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    personal = tmp_path / "personal.db"
    db = GraphDB(personal)
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    for ts, name in ((100, "s1"), (200, "s2"), (300, "s3")):
        with catalog.transaction(ts, f"tx-{name}"):
            _insert_source(db.conn, name)
    if seal_floor_at is not None:
        persona = KeyPair.generate()
        record = write_floors.seal_persona_write_floor(
            db.conn, signer=machine, persona_cert=_persona_cert(persona, machine),
            org=ORG, roster_machines={machine.public_hex},
            positions={machine.public_hex: seal_floor_at},
        )
        assert record is not None and record["write_floor_ns"] == seal_floor_at
        db.conn.commit()
    db.close()
    entries = (
        enroll(root, machine_pub=machine.public_hex),
        enroll(root, machine_pub=peer.public_hex, seq=1),
    )
    scheduler = fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=machine, personal_root_pub=root.public_hex,
        roster_entries=lambda: entries, peer_addresses=lambda: {},
        personal_db_path=personal, poll_interval=60.0,
    ))
    scheduler._roster_snapshot = entries
    # The follow admission is confined to the org's scope (F3); this test
    # drives the serve path alone and lets the org scope be the personal store.
    scheduler._org_scopes_for = lambda org: ["personal"]
    return scheduler, personal, machine.public_hex


def _serve(scheduler, personal: Path, *, cursor: int | None, bootstrap: bool = False):
    store = SQLiteFleetSyncStore(personal)
    request = encode_pull_request(
        roster_epoch(scheduler._roster_snapshot, scheduler.config.personal_root_pub),
        compat=store.compatibility_digest(), bootstrap=bootstrap,
        watermarks=None if cursor is None else {ORG: cursor},
    )
    admission = Admission(kind="follow", org=ORG)

    async def run():
        out = []
        frames = await scheduler._handle("t" * 32, request, "", admission=admission)
        async for frame in frames:
            out.append(bytes(frame))
        return out

    return asyncio.run(run())


def _classify(frames):
    control, headers, refusals, other = [], [], [], []
    for f in frames:
        if f.startswith(_TRANSACTION_MAGIC):
            headers.append(decode_transaction_header(f))
        elif f.startswith(_REFUSAL_MAGIC):
            refusals.append(json.loads(f[len(_REFUSAL_MAGIC):]))
        elif f.startswith(_DONE_MAGIC):
            control.append({"kind": "done", **json.loads(f[len(_DONE_MAGIC):])})
        elif f[:1] == b"{":
            control.append(json.loads(f))
        else:
            other.append(f)
    return control, headers, refusals, other


def test_a_follow_delta_is_one_origin_between_cursor_and_frontier(tmp_path):
    scheduler, personal, machine_pub = _build(tmp_path, seal_floor_at=200)
    frames = _serve(scheduler, personal, cursor=100)
    control, headers, refusals, other = _classify(frames)
    assert refusals == []
    # Opens with pull.begin: projection public, ONE frontier key, the org at F.
    assert control[0]["kind"] == PULL_BEGIN_KIND
    assert control[0]["projection"] == "public"
    assert control[0]["frontier"] == {ORG: 200}
    # Exactly the transaction at 200: above the cursor, at or below F. The
    # one at 300 is above F and never served; the one at 100 is the cursor.
    assert [h[1] for h in headers] == ["tx-s2"]
    # Every header names the organization, never the machine.
    assert all(h[0] == ORG for h in headers)
    assert all(machine_pub.encode() not in f for f in frames)
    # No write-floor frames of any kind, no origin map anywhere.
    kinds = [c.get("kind") for c in control]
    assert "machine.write_floor" not in kinds and "persona.write_floor" not in kinds
    assert kinds[-1] == "done"
    payload = b"".join(other)
    assert b"s2" in payload and b"s3" not in payload
    # Nothing per-follower is recorded on the server.
    db = GraphDB(personal)
    try:
        acks = db.conn.execute("SELECT count(*) FROM fleet_sync_served_acks").fetchone()[0]
    except Exception:
        acks = 0
    finally:
        db.close()
    assert acks == 0


def test_a_fresh_follower_gets_a_sweep_capped_at_the_frontier(tmp_path):
    scheduler, personal, machine_pub = _build(tmp_path, seal_floor_at=200)
    frames = _serve(scheduler, personal, cursor=None, bootstrap=True)
    control, headers, refusals, other = _classify(frames)
    assert refusals == []
    assert control[0]["kind"] == "sweep.begin"
    assert control[0]["projection"] == "public"
    assert control[0]["frontier"] == {ORG: 200}
    assert all(h[0] == ORG for h in headers)
    assert all(machine_pub.encode() not in f for f in frames)
    payload = b"".join(other)
    assert b"s1" in payload and b"s2" in payload and b"s3" not in payload
    # After a follow sweep there is no delta in the same reply: nothing
    # above F is served, and the delta's opening record never appears.
    kinds = [c.get("kind") for c in control]
    assert PULL_BEGIN_KIND not in kinds
    assert "sweep.end" in kinds and kinds[-1] == "done"


def test_a_cursor_below_retention_is_refused_too_old(tmp_path):
    scheduler, personal, _ = _build(tmp_path, seal_floor_at=300)
    db = GraphDB(personal)
    db.conn.execute("DELETE FROM fleet_sync_catalog WHERE timestamp_ns=100")
    db.conn.execute("DELETE FROM fleet_sync_transactions WHERE timestamp_ns=100")
    db.conn.commit()
    db.close()
    frames = _serve(scheduler, personal, cursor=150)
    control, headers, refusals, other = _classify(frames)
    assert [r["kind"] for r in refusals] == [PULL_TOO_OLD_KIND]
    assert headers == [] and other == [] and control == []


def test_a_member_without_a_covered_persona_floor_refuses(tmp_path):
    scheduler, personal, _ = _build(tmp_path, seal_floor_at=None)
    frames = _serve(scheduler, personal, cursor=100)
    control, headers, refusals, other = _classify(frames)
    assert [r["kind"] for r in refusals] == [FOLLOW_NO_FRONTIER_KIND]
    assert headers == [] and other == [] and control == []


def test_follow_heads_interleave_origins_in_timestamp_order(tmp_path):
    db = GraphDB(tmp_path / "t.db")
    catalog = MutationCatalog(db.conn, "aa" * 32)
    catalog.install()
    with catalog.transaction(100, "a-100"):
        _insert_source(db.conn, "a1")
    with catalog.transaction(300, "a-300"):
        _insert_source(db.conn, "a2")
    # A second origin's transactions, as a synced store holds them.
    db.conn.execute("INSERT INTO fleet_sync_origins(incarnation) VALUES(?)", ("bb" * 32,))
    other = db.conn.execute("SELECT id FROM fleet_sync_origins WHERE incarnation=?", ("bb" * 32,)).fetchone()[0]
    for ts, tid in ((200, "b-200"), (250, "b-250"), (400, "b-400")):
        db.conn.execute(
            "INSERT INTO fleet_sync_transactions(origin_id,transaction_id,timestamp_ns) VALUES(?,?,?)",
            (other, tid, ts),
        )
    db.conn.commit()
    heads = catalog.next_follow_transaction_heads(100, None, through_ns=300, limit=10)
    assert [(h[1], h[2], h[3][:2]) for h in heads] == [
        (200, "b-200", "bb"), (250, "b-250", "bb"), (300, "a-300", "aa"),
    ]
    # Paging resumes after a (timestamp, transaction id) position.
    heads = catalog.next_follow_transaction_heads(200, "b-200", through_ns=300, limit=10)
    assert [h[2] for h in heads] == ["b-250", "a-300"]
    db.close()
