"""The follower client: a node mirrors another org's public surface read-only
over the credential-free follow loop (design of record graph://5f2f5a49-00d
§10.4, bead auto-3534i).

These tests drive the REAL server serve path (``_handle`` with a
``kind="follow"`` admission, the production public projection and sweep code)
through an in-memory channel, and the REAL follower apply/prune path
(``_sync_follow_scope`` and ``_follow_attempt``). The channel stands in for the
relay + link server; the fragment-key authenticity of the channel itself is the
link layer's and is covered by the membership_sim follow tests.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync_channel import Admission
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_sync_scheduler import (
    SQLiteFleetSyncStore, canonical_json,
)
from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "cd" * 32  # the served org's ledger genesis id
SLUG = "acme"    # the org slug the follower mirrors it under


def _insert_source(conn, identity, ts, state="published"):
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at,"
        "publication_state) VALUES(?,?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-20T00:00:00Z",
         "2026-09-20T00:00:00Z", state),
    )


def _insert_thought(conn, tid, source_id):
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,role,created_at,"
        "publication_state) VALUES(?,?,?,?,?,?)",
        (tid, source_id, "a thought", "user", "2026-09-20T00:00:00Z", "raw"),
    )


def _insert_derivation(conn, did, source_id):
    conn.execute(
        "INSERT INTO derivations(id,source_id,content,created_at) "
        "VALUES(?,?,?,?)",
        (did, source_id, "a derivation", "2026-09-20T00:00:00Z"),
    )


def _persona_cert(persona, machine):
    now = int(time.time())
    return issue_cert(
        persona, machine.public_hex, scope=["fleet:sync"], org=ORG,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 60, not_after=now + 3600,
    )


def _build_server(tmp_path, *, floor_at, rows):
    """A server whose org scope ``SLUG`` holds ``rows`` = list of
    (source_id, ts, state[, kind]) plus a persona floor so F = floor_at."""
    root, machine = KeyPair.generate(), KeyPair.generate()
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    org_db = server_dir / f"{SLUG}.db"
    db = GraphDB(org_db)
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    for spec in rows:
        sid, ts, state = spec[0], spec[1], spec[2]
        with catalog.transaction(ts, f"tx-{sid}"):
            _insert_source(db.conn, sid, ts, state)
            for extra in spec[3:]:
                kind, xid = extra
                if kind == "thought":
                    _insert_thought(db.conn, xid, sid)
                elif kind == "derivation":
                    _insert_derivation(db.conn, xid, sid)
    persona = KeyPair.generate()
    record = write_floors.seal_persona_write_floor(
        db.conn, signer=machine, persona_cert=_persona_cert(persona, machine),
        org=ORG, roster_machines={machine.public_hex},
        positions={machine.public_hex: floor_at},
    )
    assert record is not None
    db.conn.commit()
    db.close()
    entries = (enroll(root, machine_pub=machine.public_hex),)
    scheduler = fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=machine, personal_root_pub=root.public_hex,
        roster_entries=lambda: entries, peer_addresses=lambda: {},
        personal_db_path=server_dir / "personal.db", poll_interval=60.0,
        sync_scopes=lambda: {SLUG: org_db},
    ))
    scheduler._roster_snapshot = entries
    scheduler._org_scopes_for = lambda org: [SLUG]
    return scheduler


class _ServerChannel:
    """An in-memory channel that pipes one follow op to the server's _handle
    and streams its reply back, standing in for the relay + link server."""

    def __init__(self, server, org=ORG):
        self._server = server
        self._org = org
        self._frames: list[bytes] = []

    async def send_message(self, data: bytes) -> None:
        env = json.loads(data)
        assert env["op"] == "follow"
        pull = canonical_json(env["request"])
        admission = Admission(kind="follow", org=self._org)
        res = await self._server._handle("t" * 32, pull, "", admission=admission)
        if hasattr(res, "__aiter__"):
            self._frames = [bytes(f) async for f in res]
        else:
            self._frames = [bytes(res)]

    async def recv_message_stream(self):
        for i, frame in enumerate(self._frames):
            yield frame, i == len(self._frames) - 1

    async def close(self):
        pass


class _ReplayChannel:
    """Replays a fixed frame list (for the interrupted-sweep and too-old cases)."""

    def __init__(self, frames):
        self._frames = frames

    async def send_message(self, data):
        pass

    async def recv_message_stream(self):
        for i, frame in enumerate(self._frames):
            yield frame, i == len(self._frames) - 1

    async def close(self):
        pass


def _follower(tmp_path, connect):
    """A follower scheduler with a followed mirror at SLUG and an injected
    follow dialer."""
    orgs_dir = tmp_path / "follower" / "orgs"
    orgs_dir.mkdir(parents=True)
    mirror = orgs_dir / f"{SLUG}.db"
    GraphDB.create_org_db(SLUG, type_="followed", org_id=ORG, path=mirror).close()
    machine = KeyPair.generate()
    root = KeyPair.generate()
    scheduler = fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=machine, personal_root_pub=root.public_hex,
        roster_entries=lambda: (), peer_addresses=lambda: {},
        personal_db_path=tmp_path / "follower" / "personal.db",
        poll_interval=60.0,
        sync_scopes=lambda: {SLUG: mirror},
        follow_connect=connect,
    ))
    return scheduler, mirror


def _mirror_sources(mirror: Path):
    db = GraphDB(mirror, mode="ro")
    try:
        return {
            r[0]: r[1] for r in db.conn.execute(
                "SELECT id, publication_state FROM sources"
            ).fetchall()
        }
    finally:
        db.close()


def _mirror_count(mirror: Path, table: str) -> int:
    db = GraphDB(mirror, mode="ro")
    try:
        return db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


def test_a_fresh_sweep_mirrors_only_the_public_surface_below_F(tmp_path):
    server = _build_server(tmp_path, floor_at=200, rows=[
        ("s1", 100, "published"),
        ("r1", 150, "raw"),         # not public -> never served
        ("s2", 200, "published"),
        ("s3", 300, "published"),   # above F -> not served
    ])
    row = {"org_uuid": ORG, "rendezvous": "https://relay/l/tok",
           "link_pub": "ab" * 32}
    follower, mirror = _follower(
        tmp_path, connect=lambda r: _server_channel_coro(server),
    )
    outcome = asyncio.run(follower._sync_follow_scope(SLUG, row))
    assert outcome == "ok"
    sources = _mirror_sources(mirror)
    assert set(sources) == {"s1", "s2"}
    # The cursor is F.
    store = SQLiteFleetSyncStore(mirror)
    assert store.follow_cursor() == (ORG, 200)


async def _server_channel_coro(server):
    return _ServerChannel(server)


def test_a_completed_resweep_prunes_a_demoted_row_and_its_satellites(tmp_path):
    server = _build_server(tmp_path, floor_at=300, rows=[
        ("s1", 100, "published"),
        ("s2", 200, "published", ("thought", "t2"), ("derivation", "d2")),
        ("s3", 300, "published"),
    ])
    row = {"org_uuid": ORG, "rendezvous": "https://relay/l/tok",
           "link_pub": "ab" * 32}
    follower, mirror = _follower(
        tmp_path, connect=lambda r: _server_channel_coro(server),
    )
    asyncio.run(follower._sync_follow_scope(SLUG, row))
    assert set(_mirror_sources(mirror)) == {"s1", "s2", "s3"}
    assert _mirror_count(mirror, "thoughts") == 1
    assert _mirror_count(mirror, "derivations") == 1

    # Demote s2 at the server: no longer public.
    sdb = GraphDB(_server_org_db(server))
    sdb.conn.execute(
        "UPDATE sources SET publication_state='curated' WHERE id='s2'")
    sdb.conn.commit()
    sdb.close()

    # A completed fresh sweep (the too-old answer) reconciles by generation:
    # s2 was not carried, so it and its satellites are pruned.
    store = SQLiteFleetSyncStore(mirror)
    store.reset_follow_bootstrap()
    outcome = asyncio.run(
        follower._follow_attempt(SLUG, row, store, force_sweep=True)
    )
    assert outcome == "ok"
    assert set(_mirror_sources(mirror)) == {"s1", "s3"}
    assert _mirror_count(mirror, "thoughts") == 0
    assert _mirror_count(mirror, "derivations") == 0


def _server_org_db(server):
    return server.config.sync_scopes()[SLUG]


def test_an_interrupted_sweep_prunes_nothing(tmp_path):
    server = _build_server(tmp_path, floor_at=300, rows=[
        ("s1", 100, "published"),
        ("s2", 200, "published"),
        ("s3", 300, "published"),
    ])
    row = {"org_uuid": ORG, "rendezvous": "https://relay/l/tok",
           "link_pub": "ab" * 32}
    follower, mirror = _follower(
        tmp_path, connect=lambda r: _server_channel_coro(server),
    )
    asyncio.run(follower._sync_follow_scope(SLUG, row))
    assert set(_mirror_sources(mirror)) == {"s1", "s2", "s3"}

    # Demote s2, then feed a TRUNCATED sweep (no done summary): the receive
    # raises and nothing is pruned — the mirror keeps every row.
    sdb = GraphDB(_server_org_db(server))
    sdb.conn.execute(
        "UPDATE sources SET publication_state='curated' WHERE id='s2'")
    sdb.conn.commit()
    sdb.close()

    # Capture a real sweep's frames, drop the final (done) frame.
    channel = _ServerChannel(server)
    asyncio.run(channel.send_message(json.dumps({
        "op": "follow",
        "request": json.loads(fss.encode_pull_request(
            "00" * 32,
            compat=SQLiteFleetSyncStore(mirror).compatibility_digest(),
            scope=SLUG, bootstrap=True, watermarks=None,
        )),
    }).encode()))
    truncated = channel._frames[:-1]  # drop the done summary

    store = SQLiteFleetSyncStore(mirror)
    store.reset_follow_bootstrap()
    with pytest.raises(Exception):
        asyncio.run(
            follower._follow_receive(SLUG, store, _ReplayChannel(truncated))
        )
    # s2 is still present: an interrupted sweep prunes nothing.
    assert "s2" in _mirror_sources(mirror)


def test_a_too_old_refusal_triggers_a_reset_and_full_sweep(tmp_path):
    server = _build_server(tmp_path, floor_at=300, rows=[
        ("s1", 100, "published"),
        ("s2", 200, "published"),
        ("s3", 300, "published"),
    ])
    row = {"org_uuid": ORG, "rendezvous": "https://relay/l/tok",
           "link_pub": "ab" * 32}
    too_old = fss.encode_follow_too_old_refusal(scope=SLUG)
    state = {"too_old_pending": False}

    async def connect(_row):
        # Inject one too-old refusal on demand; otherwise the real server.
        if state["too_old_pending"]:
            state["too_old_pending"] = False
            return _ReplayChannel([too_old])
        return _ServerChannel(server)

    follower, mirror = _follower(tmp_path, connect=connect)
    # A normal sweep so the follower has a cursor and a COMPLETE bootstrap.
    asyncio.run(follower._sync_follow_scope(SLUG, row))
    assert set(_mirror_sources(mirror)) == {"s1", "s2", "s3"}

    # Demote s2; the next pull's delta attempt hits a too-old refusal, so the
    # follower resets and full-sweeps, which prunes the demoted row.
    sdb = GraphDB(_server_org_db(server))
    sdb.conn.execute(
        "UPDATE sources SET publication_state='curated' WHERE id='s2'")
    sdb.conn.commit()
    sdb.close()
    state["too_old_pending"] = True

    outcome = asyncio.run(follower._sync_follow_scope(SLUG, row))
    assert outcome == "ok"
    assert set(_mirror_sources(mirror)) == {"s1", "s3"}


def test_an_unreachable_rendezvous_applies_zero_rows_and_records_status(tmp_path):
    # A dial failure (rendezvous down, or a server hello signed by a key other
    # than link_pub — verify_link_server_hello raises inside connect) applies no
    # row and records an unreachable status with a timestamp for `graph follow
    # status` (QA-8, QA-6).
    async def connect(_row):
        raise ConnectionError("rendezvous unreachable / bad server hello")

    follower, mirror = _follower(tmp_path, connect=connect)
    row = {"org_uuid": ORG, "rendezvous": "https://relay/l/tok",
           "link_pub": "ab" * 32}
    outcome = asyncio.run(follower._sync_follow_scope(SLUG, row))
    assert outcome == "unreachable"
    assert _mirror_sources(mirror) == {}
    from tools.network.fleet_sync import follow_mirror
    db = GraphDB(mirror, mode="ro")
    try:
        status = follow_mirror.read_follow_status(db.conn)
    finally:
        db.close()
    assert status["outcome"] == "unreachable"
    assert status["at"] is not None


def test_a_reply_without_the_projection_marker_applies_zero_rows(tmp_path):
    # A done summary with no sweep.begin/pull.begin ahead of it is refused.
    follower, mirror = _follower(tmp_path, connect=None)
    store = SQLiteFleetSyncStore(mirror)
    done = fss.encode_done(epoch="00" * 32, count=0, digest="0" * 64)
    with pytest.raises(Exception):
        asyncio.run(
            follower._follow_receive(SLUG, store, _ReplayChannel([done]))
        )
    assert _mirror_sources(mirror) == {}
