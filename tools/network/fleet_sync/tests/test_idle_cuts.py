"""Idle cuts (auto-mmwgu): an origin's watermark advances without writes.

Acceptance from the bead: a racing writer never commits at or below a
sealed cut; a backward clock pauses cuts and writes are refused until the
clock passes the floor; a cut sealed on A reaches C through B without A
serving C; a persona cut is the minimum machine position and is sealed only
once every roster machine has a position.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync import cuts
from tools.network.fleet_sync.catalog import MutationCatalog, WatermarkError
from tools.network.fleet_sync.tests.test_protocol_v4 import (
    _eventually, _insert, _prepare, _scheduler, _title,
)
from tools.network.fleet_sync_scheduler import (
    FLEET_SYNC_PROTOCOL_VERSION, SQLiteFleetSyncStore, _TRANSACTION_MAGIC,
    encode_pull_request, roster_epoch,
)
from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "genesis-" + "ab" * 28


def _insert_source(conn, identity: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"),
    )


# ── machine cut ─────────────────────────────────────────────────────────────

def test_a_racing_writer_never_commits_at_or_below_a_sealed_cut(tmp_path: Path) -> None:
    """10,000 interleavings of a cut and a write on two connections. The
    write's stamp is drawn around the floor the cut just raised, so about
    half the attempts land at or below it. The gate's answer is checked
    exactly every time: refused if and only if the stamp is at or below
    the floor, and what the store recorded afterwards is exactly the set
    of accepted stamps. Deterministic, so a failure reproduces."""
    import random

    machine = KeyPair.generate()
    path = tmp_path / "race.db"
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    db.conn.execute("PRAGMA synchronous=OFF")
    sealer = sqlite3.connect(path, timeout=30)
    sealer.execute("PRAGMA synchronous=OFF")
    rng = random.Random(10_000)
    clock = 1_000_000
    sealed: list[int] = []
    accepted: list[int] = []
    refused = 0
    for i in range(10_000):
        if rng.random() < 0.5:
            clock += rng.randint(1, 5)
            cut = cuts.seal_machine_cut(sealer, machine, machine.public_hex, clock)
            assert cut is not None and (not sealed or cut >= sealed[-1])
            sealed.append(cut)
        floor = db.conn.execute(
            "SELECT write_floor FROM fleet_sync_state WHERE singleton=1"
        ).fetchone()[0]
        last = accepted[-1] if accepted else 0
        # The writer shares the machine clock: a stamp is never ahead of it.
        clock += rng.randint(1, 5)
        stamp = clock - rng.randint(0, 6)
        try:
            with catalog.transaction(stamp, f"w{i}"):
                pass
        except WatermarkError:
            refused += 1
            assert stamp <= max(floor, last), (i, stamp, floor, last)
            continue
        assert stamp > max(floor, last), (i, stamp, floor, last)
        accepted.append(stamp)
    assert sealed and accepted and refused, (len(sealed), len(accepted), refused)
    recorded = [int(r[0]) for r in db.conn.execute(
        "SELECT timestamp_ns FROM fleet_sync_transactions ORDER BY id"
    )]
    assert recorded == accepted
    assert all(later > earlier for earlier, later in zip(recorded, recorded[1:]))
    final_floor = db.conn.execute(
        "SELECT write_floor FROM fleet_sync_state WHERE singleton=1"
    ).fetchone()[0]
    assert final_floor == sealed[-1]
    db.close(); sealer.close()


def test_a_backward_clock_pauses_cuts_and_writes_until_it_passes_the_floor(tmp_path: Path) -> None:
    machine = KeyPair.generate()
    db = GraphDB(tmp_path / "clock.db")
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    with catalog.transaction(10, "t0"):
        _insert_source(db.conn, "s0")
    conn = db.conn
    assert cuts.seal_machine_cut(conn, machine, machine.public_hex, 1_000) == 1_000
    # The clock steps back: no cut, and the gate refuses a write below the floor.
    assert cuts.seal_machine_cut(conn, machine, machine.public_hex, 500) is None
    with pytest.raises(WatermarkError):
        with catalog.transaction(600, "t1"):
            _insert_source(db.conn, "s1")
    assert catalog.origin_watermarks()[machine.public_hex] == 1_000
    # The clock passes the floor: cuts resume and never regress.
    assert cuts.seal_machine_cut(conn, machine, machine.public_hex, 1_100) == 1_100
    with catalog.transaction(1_200, "t2"):
        _insert_source(db.conn, "s2")
    assert cuts.seal_machine_cut(conn, machine, machine.public_hex, 1_150) == 1_200, (
        "a cut is max(last write, now), never below the last write"
    )
    db.close()


def test_an_origin_cut_is_verified_against_the_origin_key(tmp_path: Path) -> None:
    origin, other = KeyPair.generate(), KeyPair.generate()
    db = GraphDB(tmp_path / "adopt.db")
    MutationCatalog(db.conn, other.public_hex).install()
    good = {"origin": origin.public_hex, "cut_ns": 5_000,
            "sig": origin.sign_hex(cuts.origin_cut_body(origin.public_hex, 5_000, origin.public_hex))}
    assert cuts.adopt_origin_cut(db.conn, good) is True
    assert cuts.adopt_origin_cut(db.conn, good) is False          # not newer
    assert cuts.origin_cuts(db.conn)[origin.public_hex][0] == 5_000
    forged = dict(good, cut_ns=9_000)                              # signature is for 5_000
    with pytest.raises(cuts.CutError):
        cuts.adopt_origin_cut(db.conn, forged)
    signed_by_other = {"origin": origin.public_hex, "cut_ns": 9_000,
                       "sig": other.sign_hex(cuts.origin_cut_body(origin.public_hex, 9_000, origin.public_hex))}
    with pytest.raises(cuts.CutError):
        cuts.adopt_origin_cut(db.conn, signed_by_other)
    assert cuts.origin_cuts(db.conn)[origin.public_hex][0] == 5_000
    # The watermark is the greater of cursor and cut.
    assert MutationCatalog(db.conn, other.public_hex).origin_watermarks()[origin.public_hex] == 5_000
    db.close()


def test_a_cut_signed_by_the_delegated_process_key_verifies_through_its_chain(tmp_path: Path) -> None:
    """Production signs with a process key the machine key delegated to
    (fleet:sync, machine-direct), never with the machine key itself: the
    origin IS the machine key, and the cut carries the delegation so a
    receiver walks origin -> signer. Live-fleet failure 2026-09-19."""
    machine, process, stranger = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        machine, process.public_hex, scope=["fleet:sync"], org="personal:" + "ab" * 32,
        subject=Subject("machine", "m-1"), not_before=now - 60, not_after=now + 3600,
    )
    db = GraphDB(tmp_path / "delegated.db")
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    with catalog.transaction(10, "t0"):
        _insert_source(db.conn, "s0")
    # A delegate without its delegation cannot seal.
    with pytest.raises(cuts.CutError):
        cuts.seal_machine_cut(db.conn, process, machine.public_hex, 1_000)
    assert cuts.seal_machine_cut(db.conn, process, machine.public_hex, 1_000, cert=cert) == 1_000
    record = cuts.origin_cut_records(db.conn)[machine.public_hex]
    assert record["signer"] == process.public_hex and record["cert"] is not None
    frame = {"origin": machine.public_hex, **record}
    assert cuts.verify_origin_cut(frame, now=now)["cut_ns"] == 1_000
    # A receiver adopts it through the same verification.
    peer = GraphDB(tmp_path / "peer.db")
    MutationCatalog(peer.conn, stranger.public_hex).install()
    assert cuts.adopt_origin_cut(peer.conn, frame) is True
    assert MutationCatalog(peer.conn, stranger.public_hex).origin_watermarks()[machine.public_hex] == 1_000
    # A chain anchored elsewhere, a wrong scope, or a leaf that is not the
    # signer is refused.
    other_cert = issue_cert(
        stranger, process.public_hex, scope=["fleet:sync"], org="personal:" + "ab" * 32,
        subject=Subject("machine", "m-1"), not_before=now - 60, not_after=now + 3600,
    )
    with pytest.raises(cuts.CutError):
        cuts.verify_origin_cut({**frame, "cert": other_cert.to_dict()}, now=now)
    wide = issue_cert(
        machine, process.public_hex, scope=["fleet:sync", "link:*"], org="personal:" + "ab" * 32,
        subject=Subject("machine", "m-1"), not_before=now - 60, not_after=now + 3600,
    )
    with pytest.raises(cuts.CutError):
        cuts.verify_origin_cut({**frame, "cert": wide.to_dict()}, now=now)
    with pytest.raises(cuts.CutError):
        cuts.verify_origin_cut({**frame, "signer": stranger.public_hex}, now=now)
    db.close(); peer.close()


# ── propagation through the real pull ──────────────────────────────────────

def test_a_cut_sealed_on_a_reaches_c_through_b_without_a_serving_c(tmp_path: Path) -> None:
    async def run() -> None:
        root = KeyPair.generate()
        keys = [KeyPair.generate() for _ in range(3)]
        paths = [tmp_path / f"m{i}.db" for i in range(3)]
        for key, path in zip(keys, paths):
            _prepare(path, key)
        entries = [enroll(root, machine_pub=k.public_hex, seq=i) for i, k in enumerate(keys)]
        a = _scheduler(keys[0], root, entries, paths[0])
        await a.start()
        b = _scheduler(keys[1], root, entries, paths[1],
                       peers={keys[0].public_hex: [f"ws://127.0.0.1:{a.port}"]})
        await b.start()
        # C dials ONLY B. It never has an address for A.
        c = _scheduler(keys[2], root, entries, paths[2],
                       peers={keys[1].public_hex: [f"ws://127.0.0.1:{b.port}"]})
        _insert(paths[0], "from-a", "a wrote once")
        await c.start()
        try:
            await _eventually(lambda: _title(paths[2], "from-a") == "a wrote once", timeout=10)
            written_at = SQLiteFleetSyncStore(paths[0]).origin_watermarks()[keys[0].public_hex]
            # A is idle from here on. Its cut keeps moving and C learns it via B.
            def c_has_a_cut_above_the_write() -> bool:
                held = SQLiteFleetSyncStore(paths[2]).origin_cuts().get(keys[0].public_hex)
                return held is not None and held[0] > written_at
            await _eventually(c_has_a_cut_above_the_write, timeout=10)
            cut_on_a = SQLiteFleetSyncStore(paths[0]).origin_cuts()[keys[0].public_hex][0]
            cut_on_c = SQLiteFleetSyncStore(paths[2]).origin_cuts()[keys[0].public_hex][0]
            assert cut_on_c <= cut_on_a
            assert SQLiteFleetSyncStore(paths[2]).origin_watermarks()[keys[0].public_hex] == cut_on_c
            # Freshness: an idle A's cut as held on B is no older than a few rounds.
            age_s = (time.time_ns() - SQLiteFleetSyncStore(paths[1]).origin_cuts()[keys[0].public_hex][0]) / 1e9
            assert age_s < 2.0 + 0.03 * 3, f"cut held on B is {age_s:.2f}s old"
        finally:
            await c.stop(); await b.stop(); await a.stop()

    asyncio.run(run())


def test_cut_frames_follow_every_transaction_in_a_served_stream(tmp_path: Path) -> None:
    """The adoption invariant on the wire: no origin.cut frame precedes any
    transaction header in one pull reply."""
    root, machine, peer = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    path = tmp_path / "server.db"
    _prepare(path, machine)
    for i in range(3):
        _insert(path, f"row-{i}", "served before the cut")
    store = SQLiteFleetSyncStore(path)
    assert store.seal_machine_cut(machine, time.time_ns()) is not None
    entries = [enroll(root, machine_pub=machine.public_hex),
               enroll(root, machine_pub=peer.public_hex, seq=1)]
    server = _scheduler(machine, root, entries, path)
    server._roster_snapshot = tuple(entries)
    request = encode_pull_request(
        roster_epoch(server._roster_snapshot, root.public_hex),
        compat=store.compatibility_digest(),
    )

    async def frames() -> list[bytes]:
        stream = await server._handle("t" * 32, request, peer.public_hex)
        return [f async for f in stream]

    out = asyncio.run(frames())
    kinds = [
        "header" if f.startswith(_TRANSACTION_MAGIC)
        else ("cut" if f.startswith(b"{") and b'"origin.cut"' in f else "other")
        for f in out
    ]
    assert "header" in kinds and "cut" in kinds
    assert kinds.index("cut") > max(i for i, k in enumerate(kinds) if k == "header")


# ── persona cut ─────────────────────────────────────────────────────────────

def _persona_cert(persona: KeyPair, machine: KeyPair, org: str = ORG):
    now = int(time.time())
    return issue_cert(
        persona, machine.public_hex, scope=["fleet:sync"], org=org,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 60, not_after=now + 3600,
    )


def test_a_persona_cut_is_the_minimum_position_over_the_whole_roster(tmp_path: Path) -> None:
    persona, sealer = KeyPair.generate(), KeyPair.generate()
    machines = [sealer] + [KeyPair.generate() for _ in range(2)]
    db = GraphDB(tmp_path / "org.db")
    MutationCatalog(db.conn, sealer.public_hex).install()
    cert = _persona_cert(persona, sealer)
    roster = {m.public_hex for m in machines}
    positions = {machines[0].public_hex: 300, machines[1].public_hex: 100, machines[2].public_hex: 200}
    record = cuts.seal_persona_cut(
        db.conn, signer=sealer, persona_cert=cert, org=ORG,
        roster_machines=roster, positions=positions,
    )
    assert record is not None and record["cut_ns"] == 100
    assert record["machines"] == positions
    # A fourth machine enrolled but never heard from blocks the next seal.
    fourth = KeyPair.generate()
    assert cuts.seal_persona_cut(
        db.conn, signer=sealer, persona_cert=cert, org=ORG,
        roster_machines=roster | {fourth.public_hex},
        positions={**positions, machines[1].public_hex: 400},
    ) is None
    assert cuts.persona_frontiers(db.conn) == {persona.public_hex: 100}
    # Once it has a position the seal moves to the new minimum.
    moved = cuts.seal_persona_cut(
        db.conn, signer=sealer, persona_cert=cert, org=ORG,
        roster_machines=roster | {fourth.public_hex},
        positions={**positions, machines[1].public_hex: 400, fourth.public_hex: 250},
    )
    assert moved is not None and moved["cut_ns"] == 200
    # A receiver verifies the chain and the signature; a record for another
    # organization, or signed by a machine the cert does not name, is refused.
    assert cuts.verify_persona_cut(moved, org=ORG, now=int(time.time())) == (persona.public_hex, 200)
    with pytest.raises(cuts.CutError):
        cuts.verify_persona_cut(moved, org="genesis-other", now=int(time.time()))
    impostor = KeyPair.generate()
    body = cuts.persona_cut_body(persona.public_hex, ORG, 999, moved["machines"], impostor.public_hex)
    forged = dict(moved, cut_ns=999, signer=impostor.public_hex, sig=impostor.sign_hex(body))
    with pytest.raises(cuts.CutError):
        cuts.verify_persona_cut(forged, org=ORG, now=int(time.time()))
    db.close()


def test_the_wire_carries_the_known_persona_cuts(tmp_path: Path) -> None:
    from tools.network.fleet_sync_scheduler import decode_pull_request
    request = encode_pull_request("ab" * 32, compat="cd" * 32, scope="alpha",
                                  personas={"ef" * 32: 77})
    assert decode_pull_request(request)[7] == {"ef" * 32: 77}
    assert decode_pull_request(encode_pull_request("ab" * 32, compat="cd" * 32))[7] is None
    assert FLEET_SYNC_PROTOCOL_VERSION == 6, "no version bump inside the sprint"
