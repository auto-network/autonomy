"""Bounded rate rings, and the peer frontier persisted from what already arrives.

The two properties that make these records safe to keep forever: a ring never
grows and never needs pruning, and a slot from an earlier turn of the ring is
recognised as stale rather than added to.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB, _org_db_path
from tools.graph.schemas.fleet_sync_traffic import HOUR_SLOTS, MINUTE_SLOTS
from tools.network import fleet_sync_peer_scope, fleet_sync_traffic
from tools.network.idkit import KeyPair

MINUTE_NS = 60 * 1_000_000_000
HOUR_NS = 3600 * 1_000_000_000
PEER = "bb" * 32


@pytest.fixture
def local_stores(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    personal = GraphDB.create_org_db("personal", type_="personal")
    personal.activate_fleet_sync_writers(KeyPair.generate().public_hex)
    personal.close()
    yield _org_db_path("personal"), _org_db_path("machine")
    GraphDB.close_all_pooled()


def _catalog_rows(path) -> int:
    with sqlite3.connect(path) as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM fleet_sync_catalog").fetchone()[0]
        )


def _row(scope="personal", transport="direct", direction="received"):
    return next(
        row for row in fleet_sync_traffic.read_traffic_rows()
        if (row["transport"], row["direction"], row["scope"])
        == (transport, direction, scope)
    )


def _window(row, key, stamp_key, slots, newest_epoch, count):
    """Sum the slots a reader would count for the last ``count`` epochs.

    This is the same rule the Fleet page applies: a slot counts only when its
    stamp equals the epoch being asked about.
    """
    total = 0
    for index in range(count):
        epoch = newest_epoch - count + 1 + index
        slot = epoch % slots
        if row[stamp_key][slot] == epoch:
            total += row[key][slot]
    return total


def test_traffic_never_authors_personal_fleet_work(local_stores):
    personal, _machine = local_stores
    before = _catalog_rows(personal)
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="received", scope="personal", amount=1024
    )
    assert _catalog_rows(personal) == before


def test_a_slot_from_the_previous_turn_of_the_ring_is_not_added_to(local_stores):
    """The hazard the stamps exist for: same slot, an hour apart.

    Minute 5 and minute 65 share slot 5. Without the stamp check the second
    write would land on top of the first and a five-minute window would report
    bytes that moved an hour ago.
    """
    base = 1_000_000 * MINUTE_NS
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="received", scope="personal",
        amount=900, at_ns=base + 5 * MINUTE_NS,
    )
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="received", scope="personal",
        amount=100, at_ns=base + 65 * MINUTE_NS,
    )
    row = _row()
    newest = (base + 65 * MINUTE_NS) // 1_000_000_000 // 60
    slot = newest % MINUTE_SLOTS
    assert row["minute_bytes"][slot] == 100, "the stale 900 was not cleared"
    assert _window(row, "minute_bytes", "minute_stamp", MINUTE_SLOTS, newest, 5) == 100


def test_the_hour_ring_keeps_what_the_minute_ring_has_rotated_past(local_stores):
    """The two rings answer different questions and must not be conflated."""
    base = 1_000_000 * HOUR_NS
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="received", scope="personal",
        amount=4096, at_ns=base,
    )
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="received", scope="personal",
        amount=2048, at_ns=base + 3 * HOUR_NS,
    )
    row = _row()
    newest_hour = (base + 3 * HOUR_NS) // 1_000_000_000 // 3600
    newest_minute = (base + 3 * HOUR_NS) // 1_000_000_000 // 60
    assert _window(row, "hour_bytes", "hour_stamp", HOUR_SLOTS, newest_hour, 24) == 6144
    assert _window(
        row, "minute_bytes", "minute_stamp", MINUTE_SLOTS, newest_minute, 60
    ) == 2048


def test_ring_size_is_fixed_across_ten_thousand_writes():
    """The bound, exercised against the ring arithmetic itself.

    Ten thousand persisted writes would prove the same property at a hundred
    times the cost: the store is not what could make a ring grow, ``_add`` is.
    Persistence is covered by the tests either side of this one.
    """
    payload = fleet_sync_traffic._zero_payload()
    for minute in range(10_000):
        fleet_sync_traffic._add(
            payload, "minute_bytes", "minute_stamp", MINUTE_SLOTS, minute, 1
        )
        fleet_sync_traffic._add(
            payload, "hour_bytes", "hour_stamp", HOUR_SLOTS, minute // 60, 1
        )
    assert len(payload["minute_bytes"]) == MINUTE_SLOTS
    assert len(payload["minute_stamp"]) == MINUTE_SLOTS
    assert len(payload["hour_bytes"]) == HOUR_SLOTS
    assert len(payload["hour_stamp"]) == HOUR_SLOTS
    # One byte a minute, so the last hour holds exactly sixty and nothing
    # older leaks in through a slot the ring has already reused.
    assert _window(
        payload, "minute_bytes", "minute_stamp", MINUTE_SLOTS, 9_999, 60
    ) == 60
    assert sum(payload["minute_bytes"]) == 60


def test_row_count_is_transports_times_directions_times_scopes(local_stores):
    for scope in ("personal", "autonomy", "anchore"):
        for transport in ("direct", "relay"):
            for direction in ("sent", "received"):
                fleet_sync_traffic.record_bytes(
                    transport=transport, direction=direction, scope=scope,
                    amount=7,
                )
    assert len(fleet_sync_traffic.read_traffic_rows()) == 2 * 2 * 3


def test_zero_bytes_do_not_restamp_a_slot(local_stores):
    base = 1_000_000 * MINUTE_NS
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="sent", scope="personal",
        amount=512, at_ns=base,
    )
    fleet_sync_traffic.record_bytes(
        transport="direct", direction="sent", scope="personal",
        amount=0, at_ns=base + 90 * MINUTE_NS,
    )
    row = _row(direction="sent")
    assert sum(row["minute_bytes"]) == 512


class TestPeerFrontier:
    def test_the_oldest_origin_decides_how_far_behind_a_peer_is(self, local_stores):
        """A peer current on four origins and behind on one is behind."""
        fleet_sync_peer_scope.record_frontier(
            PEER, scope="autonomy",
            watermarks={"aa" * 32: 5_000, "bb" * 32: 9_000, "cc" * 32: 7_000},
            at_ns=1_234,
        )
        row = fleet_sync_peer_scope.read_peer_scopes()[PEER][0]
        assert row["frontier_ns"] == 5_000
        assert row["observed_at_ns"] == 1_234

    def test_an_empty_map_leaves_the_stored_frontier_alone(self, local_stores):
        """A bootstrapping peer advertises nothing on purpose.

        Recording that as zero would render a healthy joiner as behind by the
        whole age of the universe.
        """
        fleet_sync_peer_scope.record_frontier(
            PEER, scope="personal", watermarks={"aa" * 32: 8_000}, at_ns=1
        )
        fleet_sync_peer_scope.record_frontier(PEER, scope="personal", watermarks={})
        assert (
            fleet_sync_peer_scope.read_peer_scopes()[PEER][0]["frontier_ns"] == 8_000
        )

    def test_bytes_accumulate_per_scope(self, local_stores):
        fleet_sync_peer_scope.record_bytes(
            PEER, scope="personal", bytes_in=100, bytes_out=10
        )
        fleet_sync_peer_scope.record_bytes(
            PEER, scope="personal", bytes_in=50, bytes_out=5
        )
        fleet_sync_peer_scope.record_bytes(PEER, scope="autonomy", bytes_in=7)
        scopes = {
            row["scope"]: row
            for row in fleet_sync_peer_scope.read_peer_scopes()[PEER]
        }
        assert (scopes["personal"]["bytes_in"], scopes["personal"]["bytes_out"]) == (150, 15)
        assert scopes["autonomy"]["bytes_in"] == 7

    def test_reset_zeroes_bytes_and_keeps_the_frontier(self, local_stores):
        """Resetting a counter must not make a converged peer look behind."""
        fleet_sync_peer_scope.record_frontier(
            PEER, scope="personal", watermarks={"aa" * 32: 8_000}, at_ns=99
        )
        fleet_sync_peer_scope.record_bytes(
            PEER, scope="personal", bytes_in=4096, bytes_out=64
        )

        assert fleet_sync_peer_scope.reset_byte_totals() == 1

        row = fleet_sync_peer_scope.read_peer_scopes()[PEER][0]
        assert (row["bytes_in"], row["bytes_out"]) == (0, 0)
        assert (row["frontier_ns"], row["observed_at_ns"]) == (8_000, 99)


def test_a_field_a_later_schema_dropped_does_not_block_the_next_write(local_stores):
    """The 2026-09-09 outage, as a test.

    Deleting the checkpoint subsystem removed two counters from
    FleetSyncTelemetryV1 at revision 1. Every stored row still carried them,
    record_iteration merges the row it reads into the row it writes, and
    upsert_by_key's enforce_declared_fields then refused the write -- silently,
    because both call sites suppress. Home recorded no serve telemetry for five
    hours while sync itself was healthy.

    Resolution drops undeclared fields, so a stale field cannot reach a writer.
    """
    import json
    import sqlite3

    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_sync_telemetry import (
        FLEET_SYNC_TELEMETRY_REVISION,
        FLEET_SYNC_TELEMETRY_SET_ID,
    )
    from tools.network import fleet_sync_telemetry

    _personal, machine = local_stores
    key = fleet_sync_telemetry.telemetry_key(PEER, "direct", "serve", "anchore")
    first = fleet_sync_telemetry.record_iteration(
        PEER, channel="direct", direction="serve", mode="delta",
        outcome="success", started_at_ns=1, duration_ms=1, scope="anchore",
        bytes_sent=10,
    )
    assert first["iterations"] == 1

    # Age the row into what a pre-deletion writer left behind. Written
    # directly because the store itself now refuses to accept the field.
    with sqlite3.connect(machine) as conn:
        row = conn.execute(
            "SELECT id,payload FROM settings WHERE set_id=? AND key=?",
            (FLEET_SYNC_TELEMETRY_SET_ID, key),
        ).fetchone()
        stored = json.loads(row[1])
        stored["total_checkpoint_bytes"] = 4096
        stored["last_checkpoint_bytes"] = 512
        conn.execute("UPDATE settings SET payload=? WHERE id=?",
                     (json.dumps(stored), row[0]))

    second = fleet_sync_telemetry.record_iteration(
        PEER, channel="direct", direction="serve", mode="delta",
        outcome="success", started_at_ns=2, duration_ms=1, scope="anchore",
        bytes_sent=10,
    )
    assert second["iterations"] == 2, "the dead field blocked the write"
    assert "total_checkpoint_bytes" not in second

    resolved = settings_ops.read_set_key(
        FLEET_SYNC_TELEMETRY_SET_ID, key, org="machine", peers=[],
    )["payload"]
    assert not [name for name in resolved if "checkpoint" in name]


def test_a_misspelled_field_is_still_refused(local_stores):
    """Dropping on read must not make a typo a silent no-op -- that is the
    failure enforce_declared_fields exists to catch."""
    from tools.graph import settings_ops
    from tools.graph.schemas.registry import SchemaValidationError
    from tools.graph.schemas.fleet_sync_telemetry import (
        FLEET_SYNC_TELEMETRY_REVISION,
        FLEET_SYNC_TELEMETRY_SET_ID,
    )
    from tools.network import fleet_sync_telemetry

    payload = fleet_sync_telemetry._zero_payload()
    payload.update({"last_mode": "delta", "last_outcome": "success",
                    "total_bytes_snet": 5})
    with pytest.raises(SchemaValidationError):
        settings_ops.upsert_by_key(
            FLEET_SYNC_TELEMETRY_SET_ID, FLEET_SYNC_TELEMETRY_REVISION,
            f"direct:serve:{PEER}", payload, org="machine", state="raw",
        )
