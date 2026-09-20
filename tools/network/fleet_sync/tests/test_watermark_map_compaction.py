"""The compact pull-request map (auto-0my5i): a floor plus exceptions.

D6: a converged fleet lists nothing; a quiet origin stays listed until
retired; a retired origin held exactly through its final leaves the map.
D7: whatever the receiver is missing is re-served, 20 of 20 seeds.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_roster import enroll, kick
from tools.network.fleet_sync import cuts
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.watermark_map import (
    ONLINE_CUT_AGE_NS, compact_watermark_map, expand_watermark_map,
)
from tools.network.fleet_sync_scheduler import (
    SQLiteFleetSyncStore, decode_pull_request, encode_pull_request,
)
from tools.network.idkit import KeyPair

NOW = 10_000 * 1_000_000_000
A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def test_a_converged_fleet_lists_nothing_and_a_laggard_is_listed() -> None:
    fresh = {A: NOW - 1, B: NOW - 2, C: NOW - 3}
    floor, listed = compact_watermark_map(
        {A: 900, B: 910, C: 905}, fresh, NOW, known={A, B, C}, retired={},
    )
    assert (floor, listed) == (900, {})
    # C's cut is stale (offline) and its cursor is below the online floor.
    stale = {A: NOW - 1, B: NOW - 2, C: NOW - 2 * ONLINE_CUT_AGE_NS}
    floor, listed = compact_watermark_map(
        {A: 900, B: 910, C: 500}, stale, NOW, known={A, B, C}, retired={},
    )
    assert (floor, listed) == (900, {C: 500})
    # An origin no roster or persona cut names cannot be credited by the
    # server: it stays listed with its cursor whatever the floor.
    floor, listed = compact_watermark_map(
        {A: 900, B: 910, D: 950}, {A: NOW, B: NOW, D: NOW}, NOW, known={A, B}, retired={},
    )
    assert (floor, listed) == (900, {D: 950})
    # A known origin never seen is listed at 0 so it is served from the start.
    floor, listed = compact_watermark_map(
        {A: 900, B: 910}, {A: NOW, B: NOW}, NOW, known={A, B, D}, retired={},
    )
    assert listed == {D: 0} and floor == 900


def test_a_retired_origin_leaves_the_map_only_when_held_exactly_through_its_final() -> None:
    fresh = {A: NOW, B: NOW}
    # C retired with final 500: held exactly -> unlisted; held past it -> listed.
    floor, listed = compact_watermark_map(
        {A: 900, B: 910, C: 500}, {**fresh, C: NOW - 3 * ONLINE_CUT_AGE_NS}, NOW,
        known={A, B, C}, retired={C: 500},
    )
    assert listed == {} and floor == 900
    _floor, listed = compact_watermark_map(
        {A: 900, B: 910, C: 520}, {**fresh, C: NOW - 3 * ONLINE_CUT_AGE_NS}, NOW,
        known={A, B, C}, retired={C: 500},
    )
    assert listed == {C: 520}, "a peer holding rows past the final keeps listing"
    # Short of the final it is listed like any laggard.
    _floor, listed = compact_watermark_map(
        {A: 900, B: 910, C: 480}, fresh, NOW, known={A, B, C}, retired={C: 500},
    )
    assert listed == {C: 480}


def test_the_server_expands_a_compact_map_by_what_the_puller_knows() -> None:
    served_from = expand_watermark_map({C: 500}, 900, known={A, B, C}, origins=[A, B, C, D])
    assert served_from == {A: 900, B: 900, C: 500, D: 0}


def test_the_request_carries_the_floor_and_refuses_a_bad_one() -> None:
    request = encode_pull_request("ab" * 32, compat="cd" * 32, watermarks={A: 5}, floor=900)
    decoded = decode_pull_request(request)
    assert decoded[6] == {A: 5} and decoded[8] == 900
    assert decode_pull_request(encode_pull_request("ab" * 32, compat="cd" * 32))[8] is None
    from tools.network.fleet_sync_scheduler import FleetSyncProtocolError
    with pytest.raises(FleetSyncProtocolError):
        encode_pull_request("ab" * 32, compat="cd" * 32, floor=-1)


def _insert_source(conn, identity: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) VALUES(?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"),
    )


def test_twenty_seeds_of_withheld_transactions_are_all_re_served(tmp_path: Path) -> None:
    """D7 at the level that decides it: for random holes in a receiver's
    history, the served-from positions the server derives from the
    compact map are at or below every transaction the receiver lacks."""
    machines = [KeyPair.generate() for _ in range(3)]
    origins = [m.public_hex for m in machines]
    server_db = GraphDB(tmp_path / "server.db")
    server = MutationCatalog(server_db.conn, origins[0]); server.install()
    # Three origins' histories, applied into one server store.
    histories: dict[str, list] = {}
    for i, key in enumerate(machines):
        db = GraphDB(tmp_path / f"m{i}.db")
        cat = MutationCatalog(db.conn, key.public_hex); cat.install()
        for n in range(8):
            with cat.transaction(1_000 + n * 10 + i, f"t{i}-{n}"):
                _insert_source(db.conn, f"s{i}-{n}")
        page, position = [], (0, None)
        while True:
            chunk = cat.next_transactions_for_origin(key.public_hex, position[0], position[1], limit=50)
            if not chunk:
                break
            for _ref, ts, txid, items in chunk:
                page.append((ts, txid, items)); position = (ts, txid)
        histories[key.public_hex] = page
        db.close()
        for _ts, _txid, items in page:
            if key.public_hex != origins[0]:
                server.apply_remote_batch(items)
    rng = random.Random(20)
    for seed in range(20):
        client_db = GraphDB(tmp_path / f"client{seed}.db")
        client = MutationCatalog(client_db.conn, "e" * 64); client.install()
        missing: dict[str, int] = {}          # origin -> oldest missing timestamp
        for origin, page in histories.items():
            hole = rng.randrange(len(page)) if rng.random() < 0.7 else None
            for index, (ts, _txid, items) in enumerate(page):
                if hole is not None and index >= hole:
                    missing[origin] = min(missing.get(origin, ts), ts)
                    continue
                client.apply_remote_batch(items)
        watermarks = client.origin_watermarks()
        fresh = {origin: NOW for origin in origins}
        floor, listed = compact_watermark_map(
            watermarks, fresh, NOW, known=set(origins), retired={},
        )
        served_from = expand_watermark_map(listed, floor, known=set(origins), origins=origins)
        for origin, oldest_missing in missing.items():
            assert served_from[origin] < oldest_missing, (seed, origin, served_from[origin], oldest_missing)
        client_db.close()
    server_db.close()


def test_a_kicked_machine_leaves_the_personal_map_once_held_through_its_cut(tmp_path: Path) -> None:
    root, home, old = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    path = tmp_path / "personal.db"
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, home.public_hex); catalog.install()
    # The old machine's history and its final cut, adopted here.
    other = GraphDB(tmp_path / "old.db")
    theirs = MutationCatalog(other.conn, old.public_hex); theirs.install()
    with theirs.transaction(1_000, "t-old"):
        _insert_source(other.conn, "s-old")
    assert cuts.seal_machine_cut(other.conn, old, old.public_hex, 1_500) == 1_500
    page = theirs.next_transactions_for_origin(old.public_hex, 0, None, limit=50)
    for _ref, _ts, _txid, items in page:
        catalog.apply_remote_batch(items)
    record = cuts.origin_cut_records(other.conn)[old.public_hex]
    cuts.adopt_origin_cut(db.conn, {"origin": old.public_hex, **record})
    # Home's own fresh cut keeps the floor high.
    with catalog.transaction(2_000, "t-home"):
        _insert_source(db.conn, "s-home")
    import time
    assert cuts.seal_machine_cut(db.conn, home, home.public_hex, time.time_ns()) is not None
    db.close(); other.close()
    store = SQLiteFleetSyncStore(path)
    entries = [enroll(root, machine_pub=home.public_hex), enroll(root, machine_pub=old.public_hex, seq=1)]
    floor, listed = store.advertisable_watermark_map(roster_entries=entries, root_pub=root.public_hex)
    assert listed == {old.public_hex: 1_500}, "still enrolled: a quiet origin stays listed"
    kicked = entries + [kick(root, machine_pub=old.public_hex, seq=2)]
    floor, listed = store.advertisable_watermark_map(roster_entries=kicked, root_pub=root.public_hex)
    assert listed == {}, "kicked and held exactly through its final cut: folded"
    assert floor >= 2_000


def test_every_cut_bookkeeping_table_passes_the_schema_audit(tmp_path: Path) -> None:
    """The schema audit refuses to serve a store with an unclassified table;
    twice today a new bookkeeping table broke every bootstrap serve. Every
    table the cuts create must be classified."""
    from tools.network.fleet_sync.policies import audit_schema
    import time
    persona, signer, machine = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    from tools.network.idkit import Subject, issue_cert
    now = int(time.time())
    cert = issue_cert(persona, signer.public_hex, scope=["fleet:sync"], org="g",
                      subject=Subject("persona", persona.public_hex),
                      not_before=now - 60, not_after=now + 3600)
    db = GraphDB(tmp_path / "audit.db")
    catalog = MutationCatalog(db.conn, machine.public_hex); catalog.install()
    cuts.seal_machine_cut(db.conn, machine, machine.public_hex, 1_000)
    cuts.seal_persona_cut(
        db.conn, signer=signer, persona_cert=cert, org="g",
        roster_machines={machine.public_hex}, positions={machine.public_hex: 1_000},
    )
    audit_schema(db.conn)
    db.close()


def test_the_server_credits_a_puller_with_every_machine_first_listed_at_or_before_its_declared_cut(tmp_path: Path) -> None:
    """Persona cuts move every round, so a puller's declared cut is usually
    a round behind the server's. Exact equality credited nothing and the
    server re-served whole scopes from the beginning on every pull (live
    fleet 2026-09-20). The credit is by first-listed cut."""
    import time
    from tools.network.idkit import Subject, issue_cert
    persona, signer = KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(persona, signer.public_hex, scope=["fleet:sync"], org="g",
                      subject=Subject("persona", persona.public_hex),
                      not_before=now - 60, not_after=now + 3600)
    db = GraphDB(tmp_path / "server.db")
    MutationCatalog(db.conn, "e" * 64).install()
    P = persona.public_hex
    first = cuts.seal_persona_cut(db.conn, signer=signer, persona_cert=cert, org="g",
                                  roster_machines={A}, positions={A: 100})
    second = cuts.seal_persona_cut(db.conn, signer=signer, persona_cert=cert, org="g",
                                   roster_machines={A, B}, positions={A: 200, B: 150})
    assert first["cut_ns"] == 100 and second["cut_ns"] == 150
    assert cuts.machines_known_by(db.conn, {P: 100}) == {A}, "declared the first cut: knows only A"
    assert cuts.machines_known_by(db.conn, {P: 150}) == {A, B}
    assert cuts.machines_known_by(db.conn, {P: 120}) == {A}, "between the two: still only A"
    assert cuts.machines_known_by(db.conn, {}) == set()
    # A machine dropped later stays known to a puller that held the cut listing it.
    third = cuts.seal_persona_cut(db.conn, signer=signer, persona_cert=cert, org="g",
                                  roster_machines={B}, positions={B: 300})
    assert third["cut_ns"] == 300
    assert cuts.machines_known_by(db.conn, {P: 300}) == {A, B}
    assert cuts.persona_machines(db.conn) == ({B}, {A})
    db.close()
