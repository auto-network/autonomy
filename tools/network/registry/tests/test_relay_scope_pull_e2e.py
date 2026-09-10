"""A scope pull completes over the relay with NO direct address (auto-ew9wf).

Everything real: a live registry, two outbound connectors that can only dial
OUT, a directed pair between their exact slots, and two real fleet-sync
schedulers with real stores. The puller is configured with an empty peer
address map, so the only way a row can cross is the relay.

This is the acceptance the bead names. The unit tests around it prove the
decision, the delegation and the telemetry; only this proves that a row
actually arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_relay_carrier import fleet_stream_offer_handler
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_scheduler import (
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
)
from tools.network.idkit import KeyPair

from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.stream_wire import STREAM_MAX_DATA
from tools.network.registry.tests.test_directed_stream_e2e import (  # noqa: E402
    CAP,
    ORG,
    PERSONA,
    Machine,
    _free_port,
    _live_registry,
    _register,
)


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _insert(path: Path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _titles(path: Path) -> dict:
    db = GraphDB(path)
    try:
        return {
            row["id"]: row["title"]
            for row in db.conn.execute("SELECT id, title FROM sources")
        }
    finally:
        db.close()


def _scheduler(key, root, entries, path, telemetry=None):
    # peer_addresses EMPTY on purpose: the puller has no direct address for
    # anyone, so a row can only cross via the relay.
    return FleetSyncScheduler(FleetSyncRuntimeConfig(
        machine_key=key,
        personal_root_pub=root.public_hex,
        roster_entries=lambda: entries,
        peer_addresses=lambda: {},
        personal_db_path=path,
        poll_interval=0.03,
        min_backoff=0.01,
        max_backoff=0.05,
        connect_timeout=10.0,
        telemetry_recorder=telemetry,
    ))


@pytest.mark.timeout(120)
def test_a_scope_pull_crosses_the_relay_with_no_direct_address(tmp_path):
    async def scenario():
        port = _free_port()
        root = KeyPair.generate()
        left_key, right_key = KeyPair.generate(), KeyPair.generate()
        left_path, right_path = tmp_path / "left.db", tmp_path / "right.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]

        rows = []
        left = _scheduler(
            left_key, root, entries, left_path,
            telemetry=lambda peer, **values: rows.append(values),
        )
        right = _scheduler(right_key, root, entries, right_path)
        # The roster snapshot is normally filled by start(). Seed it and do
        # NOT start either scheduler: start() binds a direct listener, and
        # this test's whole claim is that no direct path exists. An unstarted
        # scheduler cannot authorise even itself, which is what an empty
        # snapshot means.
        left._roster_snapshot = tuple(entries)
        right._roster_snapshot = tuple(entries)

        # Only the SERVER has something to send.
        _insert(right_path, "over-the-relay", "arrived without a direct address")
        assert "over-the-relay" not in _titles(left_path)

        with _live_registry(port):
            _register(port, root)
            puller = Machine(root, port)
            server = Machine(root, port)
            # The serving side answers offers with the REAL fleet-sync serve
            # path, exactly as the connector process does in production.
            runtime = type("_R", (), {"scheduler": right,
                                      "locked_refusals": 0,
                                      "first_locked_refusal_at": None})()
            server_offer = fleet_stream_offer_handler(runtime)

            await puller.start()
            # Build the server's connector directly so the real offer handler
            # is present from construction rather than attached afterwards.
            server.connector = TunnelConnector(
                f"ws://127.0.0.1:{port}", ORG, server.serve_key, server.cert,
                machine_key=server.serving_machine, caps=(CAP,),
                fleet_stream_offer=server_offer,
                min_backoff=0.05, max_backoff=0.2,
            )
            server.task = asyncio.create_task(server.connector.run())
            await asyncio.wait_for(server.connector.connected.wait(), timeout=10)
            assert CAP in server.connector.accepted_caps

            try:
                await left._pull_scope(
                    right_key.public_hex, (), "personal",
                    relay_slot=(PERSONA, server.slot),
                    relay_connector=puller.connector,
                )
            finally:
                await puller.stop()
                await server.stop()

        assert _titles(left_path).get("over-the-relay") == (
            "arrived without a direct address"
        ), "the row did not cross the relay"

        # AND PROVE IT WAS THE RELAY THAT CARRIED IT. A row arriving is not
        # by itself evidence of the path: this test would pass just as well
        # if some other transport had delivered it, which is precisely the
        # kind of green that has been wrong all night.
        pulls = [r for r in rows if r.get("direction") == "pull"]
        assert pulls, "no pull telemetry was recorded at all"
        assert pulls[-1]["channel"] == "relay", (
            f"pull was carried by {pulls[-1]['channel']!r}, not the relay"
        )
        assert pulls[-1]["path_class"] == "relay"
        assert pulls[-1]["address"].startswith("relay:")
        assert pulls[-1]["mutation_frames"] >= 1, "nothing was carried"
        assert pulls[-1]["outcome"] == "success"
        # NOT asserted, and deliberately: `transactions` reads 0 on this pull
        # while the row demonstrably arrived, mutation_frames is 1, and
        # acknowledged_transaction_ref is 1 with a breadcrumb naming a real
        # transaction. The counter increments per applied group inside the
        # batch-flush loop (fleet_sync_scheduler ~3412), so either that loop
        # did not run for a single-frame delta or the apply happened by
        # another route.
        #
        # Which of those is true decides whether this is a counting
        # definition or an undercount, and an undercount would matter: a pull
        # that applies rows while telemetry reports zero transactions is the
        # same blindness that had home's counters frozen for five hours
        # tonight while sync was healthy. Asserting either way here would
        # bake in a guess, so the question is recorded instead — see the bead.
        observed_transactions = pulls[-1]["transactions"]
        assert observed_transactions >= 0  # placeholder for the recorded question

    asyncio.run(scenario())


@pytest.mark.timeout(120)
def test_the_same_single_row_over_direct_reports_the_same_count(tmp_path):
    """THE DECIDING EXPERIMENT for the transactions=0 question above.

    Identical store shape, identical single row, identical assertions — the
    only variable is the carrier. If direct also reports transactions=0 then
    the counter simply does not count a single-frame delta and the relay path
    is not undercounting; if direct reports 1, the relay path loses a count
    that the direct path keeps, and that is a defect in what I built.

    One variable, because the whole question is which side of it the zero
    lives on.
    """
    async def scenario():
        root = KeyPair.generate()
        left_key, right_key = KeyPair.generate(), KeyPair.generate()
        left_path, right_path = tmp_path / "dl.db", tmp_path / "dr.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]

        rows = []
        left = _scheduler(
            left_key, root, entries, left_path,
            telemetry=lambda peer, **values: rows.append(values),
        )
        right = _scheduler(right_key, root, entries, right_path)
        left._roster_snapshot = tuple(entries)
        right._roster_snapshot = tuple(entries)

        _insert(right_path, "over-direct", "arrived without a relay")

        # The server needs a real direct listener for this half.
        await right.start()
        try:
            await left._pull_scope(
                right_key.public_hex,
                (f"ws://127.0.0.1:{right.port}",),
                "personal",
            )
        finally:
            await right.stop()

        assert _titles(left_path).get("over-direct") == "arrived without a relay"
        pulls = [r for r in rows if r.get("direction") == "pull"]
        assert pulls and pulls[-1]["channel"] == "direct"
        print("DIRECT TELEMETRY:", pulls[-1])
        # Recorded, not asserted, for the same reason as the relay case: this
        # test exists to COMPARE, and the comparison is the finding.
        assert pulls[-1]["mutation_frames"] >= 1


#: A receive window of EXACTLY ONE FRAME, narrower than the messages this pull
#: carries. The sender chunks at ``STREAM_MAX_DATA`` (64 KiB) and
#: ``FleetWindow.can_send`` requires the whole chunk to fit in the remaining
#: credit, so one slot of exactly one frame means every frame after the first
#: waits for the receiver to absorb its predecessor. Crediting on message
#: DELIVERY instead of frame absorption deadlocks here; that is the shape
#: auto-z49ee's soak found (fixed in 9fa08a19), and module-level constants
#: cannot express it because they bind at import.
#:
#: Not lower: an offer below one frame is UNSATISFIABLE rather than narrow —
#: the first chunk can never fit and the transfer stalls silently until the
#: stream-silence bound fires. Reported to auto-0909-161758; the wire accepts
#: any window above zero today.
_NARROW_WINDOW = {"window_bytes": STREAM_MAX_DATA, "window_slots": 1}


@pytest.mark.timeout(180)
def test_a_scope_pull_completes_when_the_window_is_narrower_than_a_message(
    tmp_path,
):
    """The same relay pull, with the puller offering a window no message can
    fit in. Nothing about sync changes; only the credit arithmetic is put
    under pressure, and a regression there is a hang rather than an error —
    so the timeout IS the assertion for the deadlock, and the row crossing is
    the assertion for correctness."""
    payload = "x" * (200 * 1024)

    async def scenario():
        port = _free_port()
        root = KeyPair.generate()
        left_key, right_key = KeyPair.generate(), KeyPair.generate()
        left_path, right_path = tmp_path / "nl.db", tmp_path / "nr.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]

        rows = []
        left = _scheduler(
            left_key, root, entries, left_path,
            telemetry=lambda peer, **values: rows.append(values),
        )
        right = _scheduler(right_key, root, entries, right_path)
        left._roster_snapshot = tuple(entries)
        right._roster_snapshot = tuple(entries)

        _insert(right_path, "narrow-window", payload)
        assert "narrow-window" not in _titles(left_path)

        with _live_registry(port):
            _register(port, root)
            puller = Machine(root, port)
            server = Machine(root, port)
            runtime = type("_R", (), {"scheduler": right,
                                      "locked_refusals": 0,
                                      "first_locked_refusal_at": None})()
            offer = fleet_stream_offer_handler(runtime)

            def _connector(machine, on_offer):
                return TunnelConnector(
                    f"ws://127.0.0.1:{port}", ORG, machine.serve_key,
                    machine.cert, machine_key=machine.serving_machine,
                    caps=(CAP,), fleet_stream_offer=on_offer,
                    fleet_stream_window=_NARROW_WINDOW,
                    min_backoff=0.05, max_backoff=0.2,
                )

            # BOTH sides narrow: the reply carries the bulk, but the request
            # crosses the same credit machinery in the other direction and a
            # one-sided test would leave half of it unexercised.
            puller.connector = _connector(puller, None)
            server.connector = _connector(server, offer)
            puller.task = asyncio.create_task(puller.connector.run())
            server.task = asyncio.create_task(server.connector.run())
            for machine in (puller, server):
                await asyncio.wait_for(
                    machine.connector.connected.wait(), timeout=10)
                assert CAP in machine.connector.accepted_caps

            try:
                await left._pull_scope(
                    right_key.public_hex, (), "personal",
                    relay_slot=(PERSONA, server.slot),
                    relay_connector=puller.connector,
                )
            finally:
                await puller.stop()
                await server.stop()

        assert _titles(left_path).get("narrow-window") == payload, (
            "the row did not cross a window narrower than its own message"
        )
        pulls = [r for r in rows if r.get("direction") == "pull"]
        assert pulls and pulls[-1]["outcome"] == "success"
        assert pulls[-1]["channel"] == "relay"
        assert pulls[-1]["bytes_received"] > len(payload), (
            "the bulk did not cross this pull"
        )

    asyncio.run(scenario())
