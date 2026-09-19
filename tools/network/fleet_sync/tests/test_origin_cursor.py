"""The per-origin watermark is a contiguous-prefix CURSOR, not a MAX
(graph://d9153c5a-76e O-G, bead auto-85jlk).

A cursor advances through an origin's recorded transactions in
(timestamp_ns, transaction_id) order and stops at the first that is not
resolved: a multi-group transaction whose last group has not landed, or a
transaction holding an undrained quarantine row. What the store advertises
and what the pager serves from is that cursor, so a crash between two
commits can only cause a re-serve, never a permanent hole.
"""
from __future__ import annotations

import random
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import (
    CURSOR_HOLDING_REASONS, MutationCatalog, ensure_quarantine_table,
)
from tools.network.fleet_sync.codec import encode_value

ORIGIN_A = "a" * 64
ORIGIN_B = "b" * 64


def _insert_source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"),
    )


def _pair(tmp_path: Path):
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    server = MutationCatalog(source.conn, ORIGIN_A)
    client = MutationCatalog(target.conn, ORIGIN_B)
    server.install()
    client.install()
    return source, target, server, client


def _served(server: MutationCatalog, origin: str = ORIGIN_A) -> list:
    """Every transaction of *origin* in origin order: (timestamp, id, items)."""
    out = []
    position: tuple[int, str | None] = (0, None)
    while True:
        page = server.next_transactions_for_origin(origin, position[0], position[1], limit=50)
        if not page:
            return out
        for _ref, timestamp, transaction_id, items in page:
            out.append((timestamp, transaction_id, items))
            position = (timestamp, transaction_id)


def _write(server, conn, stamp: int, txid: str, key: str) -> None:
    with server.transaction(stamp, txid):
        _insert_source(conn, key, f"title-{key}")


def test_cursor_follows_contiguous_applies(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(5):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        for _ts, _id, items in _served(server):
            client.apply_remote_batch(items)
        assert client.origin_watermarks()[ORIGIN_A] == 1_004
        assert client.origin_max_timestamps()[ORIGIN_A] == 1_004
        assert client.unresolved_transactions() == {}
    finally:
        source.close(); target.close()


def test_incomplete_group_holds_the_cursor_and_is_reserved_whole(tmp_path: Path) -> None:
    """The multi-group crash window: group 1 of a transaction lands, the
    rest never does. MAX would pass it; the cursor stays before it, and a
    pull from the cursor re-serves the whole transaction."""
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(3):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        served = _served(server)
        client.apply_remote_batch(served[0][2], complete=True)
        client.apply_remote_batch(served[1][2], complete=False)   # "crash" before its last group
        client.apply_remote_batch(served[2][2], complete=True)
        assert client.origin_watermarks()[ORIGIN_A] == 1_000
        assert client.origin_max_timestamps()[ORIGIN_A] == 1_002
        assert client.unresolved_transactions() == {ORIGIN_A: 2}
        # The next pull asks from the cursor and gets t1 and t2 again, whole.
        again = server.next_transactions_for_origin(ORIGIN_A, 1_000, "t0", limit=50)
        assert [row[2] for row in again] == ["t1", "t2"]
        client.apply_remote_batch(served[1][2], complete=True)   # last group lands
        assert client.origin_watermarks()[ORIGIN_A] == 1_002
        assert client.unresolved_transactions() == {}
    finally:
        source.close(); target.close()


def test_an_all_losers_transaction_is_recorded_and_passed(tmp_path: Path) -> None:
    """Finding 5 of the design review: a transaction every operation of
    which loses last-writer-wins used to be rolled back unrecorded, which
    a strict cursor could never pass. It is now recorded as resolved."""
    source, target, server, client = _pair(tmp_path)
    try:
        # The client already holds a NEWER version of the same address.
        with client.transaction(2_000, "local-newer"):
            _insert_source(target.conn, "shared", "newer on the client")
        _write(server, source.conn, 1_000, "older", "shared")   # loses on the client
        _write(server, source.conn, 1_001, "after", "other")
        for _ts, _id, items in _served(server):
            applied, _ignored = client.apply_remote_batch(items)
        assert client.origin_watermarks()[ORIGIN_A] == 1_001
        assert client.unresolved_transactions() == {}
        assert target.conn.execute(
            "SELECT title FROM sources WHERE id='shared'"
        ).fetchone()[0] == "newer on the client"
    finally:
        source.close(); target.close()


def test_a_holding_quarantine_row_pins_the_cursor_until_drained(tmp_path: Path) -> None:
    """D9: fk_orphan / secondary_identity_conflict rows hold the cursor at
    the transaction that produced them; an attachment awaiting bytes does
    not."""
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(3):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        served = _served(server)
        ensure_quarantine_table(target.conn)
        # A row of t1 sits in quarantine, undrained, for a holding reason.
        target.conn.execute(
            "INSERT INTO fleet_sync_quarantine(address,table_name,logical_address,reason,"
            "watermark,quarantined_at_ns,origin,transaction_id) VALUES(?,?,?,?,?,?,?,?)",
            (encode_value(["thoughts", ["x"]]), "thoughts", "x", CURSOR_HOLDING_REASONS[0],
             1_001, 1, ORIGIN_A, "t1"),
        )
        target.conn.commit()
        for _ts, _id, items in served:
            client.apply_remote_batch(items)
        assert client.origin_watermarks()[ORIGIN_A] == 1_000
        assert client.unresolved_transactions() == {ORIGIN_A: 2}
        # The drain lands: the row leaves quarantine and the next resolution
        # walks the cursor to the end.
        target.conn.execute("DELETE FROM fleet_sync_quarantine")
        target.conn.commit()
        client.record_transactions([(ORIGIN_A, "t3", 1_003)])
        assert client.origin_watermarks()[ORIGIN_A] == 1_003
        # An attachment deferral is the steady state and never holds.
        target.conn.execute(
            "INSERT INTO fleet_sync_quarantine(address,table_name,logical_address,reason,"
            "watermark,quarantined_at_ns,origin,transaction_id) VALUES(?,?,?,?,?,?,?,?)",
            (encode_value(["attachments", ["y"]]), "attachments", "y",
             "attachment_bytes_unavailable", 1_004, 1, ORIGIN_A, "t4"),
        )
        target.conn.commit()
        client.record_transactions([(ORIGIN_A, "t4", 1_004)])
        assert client.origin_watermarks()[ORIGIN_A] == 1_004
    finally:
        source.close(); target.close()


def test_an_upgraded_store_seeds_its_cursor_at_the_newest_transaction(tmp_path: Path) -> None:
    """Operational default S6: a store that predates the cursor reports MAX
    until its first write, which creates the table and seeds every origin at
    its newest transaction; enforcement is forward-only."""
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(3):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        for _ts, _id, items in _served(server):
            client.apply_remote_batch(items)
        target.conn.execute("DROP TABLE fleet_sync_origin_cursor")   # pre-upgrade shape
        target.conn.commit()
        assert client.origin_watermarks()[ORIGIN_A] == 1_002          # MAX fallback
        _write(server, source.conn, 1_003, "t3", "s3")
        client.apply_remote_batch(_served(server)[-1][2])
        assert client.origin_watermarks()[ORIGIN_A] == 1_003
        assert target.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_origin_cursor"
        ).fetchone()[0] == 1
    finally:
        source.close(); target.close()


def test_bootstrap_seed_puts_every_cursor_at_the_newest(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(4):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        served = _served(server)
        # Sweep-like arrival: out of origin order, some marked incomplete.
        client.apply_remote_batch(served[2][2], complete=False)
        client.apply_remote_batch(served[0][2])
        client.apply_remote_batch(served[3][2])
        assert client.origin_watermarks()[ORIGIN_A] == 1_000
        client.seed_cursors_from_newest()
        assert client.origin_watermarks()[ORIGIN_A] == 1_003
    finally:
        source.close(); target.close()


def _model_cursor(order: list[tuple[int, str]], resolved: dict) -> tuple[int, str]:
    """Reference rule: the last position such that every recorded
    transaction at or below it is resolved."""
    cursor = (0, "")
    for position in sorted(order):
        if not resolved.get(position, False):
            break
        cursor = position
    return cursor


def test_random_interleavings_never_pass_an_unresolved_transaction(tmp_path: Path) -> None:
    """Randomized schedules on the real catalog: transactions arrive in any
    order, some first as an incomplete group, and every step's cursor equals
    the reference rule. One server and one installed client template are
    built once; each schedule copies the template file (milliseconds) rather
    than building a database (hundreds of milliseconds)."""
    import shutil

    rng = random.Random(85_311)
    source = GraphDB(tmp_path / "src.db")
    server = MutationCatalog(source.conn, ORIGIN_A)
    server.install()
    n_max = 5
    for i in range(n_max):
        _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
    served = _served(server)
    source.close()
    template = GraphDB(tmp_path / "template.db")
    MutationCatalog(template.conn, ORIGIN_B).install()
    template.close()
    for schedule in range(40):
        path = tmp_path / f"dst{schedule}.db"
        shutil.copyfile(tmp_path / "template.db", path)
        target = GraphDB(path)
        client = MutationCatalog(target.conn, ORIGIN_B)
        try:
            # The pager serves an origin in order and the receiver commits
            # row groups before the empties that follow them, so a
            # transaction's FIRST arrival is always after every earlier
            # transaction's first arrival. What varies: whether that first
            # arrival is a partial group, and duplicate partial re-deliveries
            # of anything already seen, which must never un-complete it.
            n = rng.randint(1, n_max)
            events: list[tuple[int, bool]] = []
            for i in range(n):
                if rng.random() < 0.4:
                    events.append((i, False))
                if events and rng.random() < 0.3:
                    events.append((rng.randrange(i + 1), False))
                events.append((i, True))
            recorded: list[tuple[int, str]] = []
            resolved: dict = {}
            for i, complete in events:
                ts, txid, items = served[i]
                client.apply_remote_batch(items, complete=complete)
                position = (ts, txid)
                if position not in recorded:
                    recorded.append(position)
                resolved[position] = resolved.get(position, False) or complete
                expected = _model_cursor(recorded, resolved)[0]
                assert client.origin_watermarks().get(ORIGIN_A, 0) == expected, (
                    schedule, events, i, complete)
            assert client.origin_watermarks()[ORIGIN_A] == 1_000 + n - 1
        finally:
            target.close()
            path.unlink()


def test_pure_model_ten_thousand_schedules() -> None:
    rng = random.Random(10_000)
    for _ in range(10_000):
        n = rng.randint(1, 8)
        positions = [(1_000 + i, f"t{i}") for i in range(n)]
        events = []
        for p in positions:
            if rng.random() < 0.5:
                events.append((p, False))
            events.append((p, True))
        rng.shuffle(events)
        recorded, resolved = [], {}
        for p, complete in events:
            if p not in recorded:
                recorded.append(p)
            resolved[p] = resolved.get(p, False) or complete
            cursor = _model_cursor(recorded, resolved)
            # Invariant: nothing recorded at or below the cursor is unresolved.
            assert all(resolved[q] for q in recorded if q <= cursor)
        assert _model_cursor(recorded, resolved) == positions[-1]
