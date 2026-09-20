"""The corrected write floor rules (graph://d9153c5a-76e O-K, operator's
correction of 2026-09-20; TLA/FleetSyncWriteFloors.tla):

C1 a server's reply is bounded by its own cursor per origin: rows at or
   below it, the origin's floor only when the cursor has reached it;
C2 a puller moves its cursor to a received floor only once every row of
   that origin at or below the floor is resolved here (zero rows resolve);
C3 the pull request's watermark is the cursor alone.
"""
from __future__ import annotations

from pathlib import Path

from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.catalog import CURSOR_HOLDING_REASONS, ensure_quarantine_table
from tools.network.fleet_sync.codec import encode_value
from tools.network.fleet_sync.tests.test_origin_cursor import ORIGIN_A, _pair, _served, _write
from tools.network.idkit.keys import KeyPair


def _signed_floor(origin: KeyPair, floor_ns: int) -> dict:
    return {
        "origin": origin.public_hex, "write_floor_ns": floor_ns,
        "sig": origin.sign_hex(write_floors.machine_write_floor_body(origin.public_hex, floor_ns, origin.public_hex)),
    }


def test_c3_a_held_floor_is_not_a_watermark_until_claimed(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        origin = KeyPair.generate()
        assert write_floors.adopt_machine_write_floor(target.conn, _signed_floor(origin, 5_000)) is True
        assert client.origin_watermarks().get(origin.public_hex, 0) == 0, "stored, not claimed"
        assert client.claim_write_floor(origin.public_hex, 5_000) is True
        assert client.origin_watermarks()[origin.public_hex] == 5_000
    finally:
        source.close(); target.close()


def test_c2_a_floor_is_claimed_only_once_every_row_at_or_below_it_resolved(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(3):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        served = _served(server)
        client.apply_remote_batch(served[0][2], complete=True)
        client.apply_remote_batch(served[1][2], complete=False)   # t1 unresolved
        client.apply_remote_batch(served[2][2], complete=True)
        assert client.origin_watermarks()[ORIGIN_A] == 1_000
        # A floor above the unresolved row is held back ...
        assert client.claim_write_floor(ORIGIN_A, 1_005) is False
        assert client.origin_watermarks()[ORIGIN_A] == 1_000
        # ... a floor below it is claimed: everything at or below 1_000 is resolved.
        assert client.claim_write_floor(ORIGIN_A, 1_000) is False   # not above the cursor
        # The last group lands: t1 resolves, the walk reaches t2, and the floor is claimed.
        client.apply_remote_batch(served[1][2], complete=True)
        assert client.origin_watermarks()[ORIGIN_A] == 1_002
        assert client.claim_write_floor(ORIGIN_A, 1_005) is True
        assert client.origin_watermarks()[ORIGIN_A] == 1_005
    finally:
        source.close(); target.close()


def test_c2_a_quarantined_row_holds_the_claim_until_it_drains(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(2):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        served = _served(server)
        ensure_quarantine_table(target.conn)
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
        assert client.claim_write_floor(ORIGIN_A, 1_010) is False
        target.conn.execute("DELETE FROM fleet_sync_quarantine")
        target.conn.commit()
        assert client.claim_write_floor(ORIGIN_A, 1_010) is True
        assert client.origin_watermarks()[ORIGIN_A] == 1_010
    finally:
        source.close(); target.close()


def test_c1_rows_past_the_servers_cursor_are_not_served(tmp_path: Path) -> None:
    """A relay holding t1 unresolved and t2 applied serves nothing past t0:
    a puller at 0 never receives t2 alone and never crosses the gap."""
    source, target, server, client = _pair(tmp_path)
    try:
        for i in range(3):
            _write(server, source.conn, 1_000 + i, f"t{i}", f"s{i}")
        served = _served(server)
        client.apply_remote_batch(served[0][2], complete=True)
        client.apply_remote_batch(served[1][2], complete=False)
        client.apply_remote_batch(served[2][2], complete=True)
        bound = client.origin_watermarks()[ORIGIN_A]
        assert bound == 1_000
        unbounded = client.next_transaction_heads_for_origin(ORIGIN_A, 0, None, limit=10)
        assert [row[2] for row in unbounded] == ["t0", "t1", "t2"]
        bounded = client.next_transaction_heads_for_origin(ORIGIN_A, 0, None, limit=10, through_ns=bound)
        assert [row[2] for row in bounded] == ["t0"]
    finally:
        source.close(); target.close()


def test_c1_a_floor_the_servers_cursor_has_not_reached_is_not_sent(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        origin = KeyPair.generate()
        assert write_floors.adopt_machine_write_floor(target.conn, _signed_floor(origin, 5_000)) is True
        records = write_floors.machine_write_floor_records(target.conn)
        held_back = write_floors.machine_write_floor_frames(records, 6, {}, bounds={origin.public_hex: 0})
        assert held_back == []
        assert client.claim_write_floor(origin.public_hex, 5_000) is True
        sent = write_floors.machine_write_floor_frames(
            records, 6, {}, bounds=client.origin_watermarks(),
        )
        assert len(sent) == 1 and b'"write_floor_ns":5000' in sent[0]
        # Already at or below the puller's watermark: nothing to send.
        assert write_floors.machine_write_floor_frames(
            records, 6, {origin.public_hex: 5_000}, bounds=client.origin_watermarks(),
        ) == []
    finally:
        source.close(); target.close()


def test_the_sealing_machines_own_cursor_is_its_floor(tmp_path: Path) -> None:
    source, target, server, client = _pair(tmp_path)
    try:
        machine = KeyPair.generate()
        from tools.network.fleet_sync.catalog import MutationCatalog
        own = MutationCatalog(source.conn, machine.public_hex)
        assert write_floors.seal_machine_write_floor(source.conn, machine, machine.public_hex, 7_000) == 7_000
        assert own.origin_watermarks()[machine.public_hex] == 7_000
    finally:
        source.close(); target.close()


def test_a_cursor_seed_never_takes_a_claimed_floor_back(tmp_path: Path) -> None:
    """The bootstrap seed sets cursors at the newest recorded transaction. A
    cursor already past that (a claimed write floor) must not regress."""
    source, target, server, client = _pair(tmp_path)
    try:
        _write(server, source.conn, 1_000, "t0", "s0")
        client.apply_remote_batch(_served(server)[0][2], complete=True)
        assert client.claim_write_floor(ORIGIN_A, 1_500) is True
        client.seed_cursors_from_newest()
        assert client.origin_watermarks()[ORIGIN_A] == 1_500
    finally:
        source.close(); target.close()
