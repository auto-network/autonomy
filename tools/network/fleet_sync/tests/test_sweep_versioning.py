"""Mixed-version controls for the v5 sweep opt-in.

The sweep is an explicit versioned opt-in, not an additive field. A v3/v4
decoder rejects unknown kinds and versions outright, so "old peers ignore it"
is not available -- and an old peer that ignored a sweep.begin would be exactly
the failure this exists to prevent: rows applied with no recorded frontier.
"""

from pathlib import Path
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.sweep_receive import (
    SWEEP_BEGIN_KIND,
    SWEEP_PROTOCOL_VERSION,
    BootstrapNotRecorded,
    SweepBeginInvalid,
    apply_live_page,
    handle_sweep_begin,
    read_bootstrap,
)
from tools.network.fleet_sync_scheduler import (
    SUPPORTED_PROTOCOL_VERSIONS,
    FleetSyncProtocolError,
    decode_pull_request,
    encode_pull_request,
)

ORIGIN = "a1" * 32
EPOCH = "ab" * 32
COMPAT = "cd" * 32


def _store(path: Path):
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, "b2" * 32)
    catalog.install()
    return db, catalog


def test_v5_is_requestable_and_round_trips() -> None:
    """A v5 request is the peer ASKING for a sweep. Nothing infers it."""
    assert SWEEP_PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
    request = encode_pull_request(
        EPOCH, compat=COMPAT, version=SWEEP_PROTOCOL_VERSION
    )
    assert decode_pull_request(request)[5] == SWEEP_PROTOCOL_VERSION


def test_legacy_versions_still_round_trip_unchanged() -> None:
    """v3/v4 behaviour is untouched: adding v5 must not alter either."""
    for version in (3, 4):
        request = encode_pull_request(EPOCH, compat=COMPAT, version=version)
        assert decode_pull_request(request)[5] == version


def test_an_unsupported_version_is_still_refused() -> None:
    with pytest.raises(FleetSyncProtocolError):
        encode_pull_request(EPOCH, compat=COMPAT, version=2)
    with pytest.raises(FleetSyncProtocolError):
        encode_pull_request(EPOCH, compat=COMPAT, version=99)


def test_a_v4_begin_record_cannot_anchor_a_store(tmp_path: Path) -> None:
    """If an old or downgraded server sent a begin at v4, the receiver refuses
    it rather than anchoring a sweep the sender cannot actually serve."""
    db, _ = _store(tmp_path / "t.db")
    try:
        record = {
            "v": 4, "kind": SWEEP_BEGIN_KIND, "scope": "personal",
            "source_machine_pub": ORIGIN, "frontier": {ORIGIN: 10},
        }
        with pytest.raises(SweepBeginInvalid):
            handle_sweep_begin(
                db.conn, record,
                expected_scope="personal", expected_source_pub=ORIGIN,
            )
        assert read_bootstrap(db.conn) is None
    finally:
        db.close()


def test_a_reordered_stream_applies_nothing(tmp_path: Path) -> None:
    """Pages arriving before their begin record must not land. This is the
    concrete failure the guard exists for: rows in a store with no record of
    the frontier they were taken against."""
    from tools.network.fleet_sync.authored_sweep import read_live_authored_page

    src = GraphDB(tmp_path / "s.db")
    sc = MutationCatalog(src.conn, ORIGIN); sc.install()
    with sc.transaction(10, "tx-1"):
        src.conn.execute(
            "INSERT INTO sources(id,type,title,metadata,created_at,"
            "ingested_at) VALUES(?,?,?,?,?,?)",
            ("s-1", "note", "t", "{}", "2026-08-19T00:00:00Z",
             "2026-08-19T00:00:00Z"))
    tgt, tc = _store(tmp_path / "t.db")
    try:
        page = read_live_authored_page(
            src.conn, frontier={ORIGIN: 1 << 40},
            max_records=10, max_bytes=1 << 20,
        )
        assert page.records
        with pytest.raises(BootstrapNotRecorded):
            apply_live_page(tc, page.records)
        assert tgt.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0] == 0

        # And once the begin arrives, the same page applies.
        handle_sweep_begin(
            tgt.conn,
            {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
             "scope": "personal", "source_machine_pub": ORIGIN,
             "frontier": {ORIGIN: 1 << 40}},
            expected_scope="personal", expected_source_pub=ORIGIN,
        )
        assert apply_live_page(tc, page.records).applied == 1
    finally:
        src.close()
        tgt.close()


def test_an_incomplete_bootstrap_still_suppresses_its_frontier(
    tmp_path: Path,
) -> None:
    """No downgrade: a store part-way through a sweep must not start claiming
    progress, whatever version it later negotiates."""
    from tools.network.fleet_sync.sweep_receive import may_advertise_frontier

    db, _ = _store(tmp_path / "t.db")
    try:
        handle_sweep_begin(
            db.conn,
            {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
             "scope": "personal", "source_machine_pub": ORIGIN,
             "frontier": {ORIGIN: 10}},
            expected_scope="personal", expected_source_pub=ORIGIN,
        )
        assert may_advertise_frontier(db.conn) is False
    finally:
        db.close()


# ── runtime receive path, not name inspection ────────────────────────────

def test_record_sweep_begin_runs_on_the_real_store_object(
    tmp_path: Path,
) -> None:
    """Exercise the store method the receiver actually calls.

    The previous controls proved the validator worked and that the call site
    NAMED it. Neither would have caught the call passing an undefined
    variable: `peer_pub` does not exist in `_pull_scope`, whose authenticated
    peer is `machine_pub`. That was a NameError on the first sweep.begin and
    no test touched it.
    """
    from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

    path = tmp_path / "t.db"
    db, _ = _store(path)
    db.close()

    store = SQLiteFleetSyncStore(path)
    store.record_sweep_begin(
        {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
         "scope": "personal", "source_machine_pub": ORIGIN,
         "frontier": {ORIGIN: 10}},
        "personal", ORIGIN,
    )
    conn = sqlite3.connect(path)
    try:
        state = read_bootstrap(conn)
        assert state is not None and state.frontier == {ORIGIN: 10}
    finally:
        conn.close()


def test_record_sweep_begin_rejects_a_record_from_another_peer(
    tmp_path: Path,
) -> None:
    """The authenticated peer is what the receiver passes; a record naming
    someone else must not anchor this store."""
    from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

    path = tmp_path / "t.db"
    db, _ = _store(path)
    db.close()

    store = SQLiteFleetSyncStore(path)
    with pytest.raises(SweepBeginInvalid):
        store.record_sweep_begin(
            {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
             "scope": "personal", "source_machine_pub": "c3" * 32,
             "frontier": {ORIGIN: 10}},
            "personal", ORIGIN,
        )
    conn = sqlite3.connect(path)
    try:
        assert read_bootstrap(conn) is None
    finally:
        conn.close()


def test_an_incomplete_bootstrap_refuses_to_downgrade(tmp_path: Path) -> None:
    """Implemented, not merely asserted.

    A store part-way through a sweep pins its negotiation: falling back to v4
    would strand a keyspace it only partially swept
    against F -- discarding F while
    keeping the rows anchored to it.
    """
    from tools.network.fleet_sync.sweep_receive import (
        Phase, record_pull_complete, record_sweep_complete,
    )
    from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

    path = tmp_path / "t.db"
    db, _ = _store(path)
    db.close()
    store = SQLiteFleetSyncStore(path)
    assert store.bootstrap_in_progress() is False, "no bootstrap, no pin"

    store.record_sweep_begin(
        {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
         "scope": "personal", "source_machine_pub": ORIGIN,
         "frontier": {ORIGIN: 10}},
        "personal", ORIGIN,
    )
    assert store.bootstrap_in_progress() is True

    conn = sqlite3.connect(path)
    try:
        record_sweep_complete(conn)
        assert store.bootstrap_in_progress() is True, (
            "sweep-complete is not bootstrap-complete; the PULL half still owes"
        )
        record_pull_complete(conn)
    finally:
        conn.close()
    assert store.bootstrap_in_progress() is False, "finished, pin released"


def test_scope_is_validated_as_the_wire_means_it(tmp_path: Path) -> None:
    """The personal scope is OMITTED on the wire and reconstructed by the
    decoder's default, so one side may hold the string and the other None.
    Comparing raw values would reject a valid record; comparing normalized
    values must still reject a genuinely different scope.

    The scope here is taken from a real encoded/decoded request rather than a
    hardcoded fixture, so the test uses whatever representation the wire
    actually produces.
    """
    from tools.network.fleet_sync.sweep_receive import handle_sweep_begin

    wire_scope = decode_pull_request(
        encode_pull_request(EPOCH, compat=COMPAT)
    )[3]

    for record_scope in (wire_scope, None, "personal"):
        db, _ = _store(tmp_path / f"t-{record_scope}.db")
        try:
            state = handle_sweep_begin(
                db.conn,
                {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
                 "scope": record_scope, "source_machine_pub": ORIGIN,
                 "frontier": {ORIGIN: 10}},
                expected_scope=None if record_scope is None else wire_scope,
                expected_source_pub=ORIGIN,
            )
            assert state.frontier == {ORIGIN: 10}
        finally:
            db.close()

    # A genuinely different scope is still refused.
    db, _ = _store(tmp_path / "t-org.db")
    try:
        with pytest.raises(SweepBeginInvalid):
            handle_sweep_begin(
                db.conn,
                {"v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
                 "scope": "some-org", "source_machine_pub": ORIGIN,
                 "frontier": {ORIGIN: 10}},
                expected_scope=wire_scope, expected_source_pub=ORIGIN,
            )
        assert read_bootstrap(db.conn) is None
    finally:
        db.close()




def _serving_scheduler(tmp_path: Path):
    """A scheduler whose store holds one row, ready to serve a bootstrap."""
    import asyncio  # noqa: F401 - documents that the caller drives a coroutine

    from tools.network import fleet_sync_scheduler as fss
    from tools.network.fleet_roster import enroll
    from tools.network.idkit import KeyPair

    root = KeyPair.generate()
    machine = KeyPair.generate()
    peer = KeyPair.generate()

    personal = tmp_path / "personal.db"
    db, catalog = _store(personal)
    with catalog.transaction(10, "tx-served"):
        db.conn.execute(
            "INSERT INTO sources(id,type,title,metadata,created_at,"
            "ingested_at) VALUES(?,?,?,?,?,?)",
            ("s-served", "note", "t", "{}", "2026-08-19T00:00:00Z",
             "2026-08-19T00:00:00Z"),
        )
    db.close()

    entries = (
        enroll(root, machine_pub=machine.public_hex),
        enroll(root, machine_pub=peer.public_hex, seq=1),
    )
    scheduler = fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=machine,
        personal_root_pub=root.public_hex,
        roster_entries=lambda: entries,
        peer_addresses=lambda: {},
        personal_db_path=personal,
        poll_interval=60.0,
    ))
    scheduler._roster_snapshot = entries
    return scheduler, personal, peer.public_hex


def _served_kinds(scheduler, personal: Path, peer_pub: str, version: int):
    """Serve one bootstrap pull and return the control-record kinds emitted."""
    import asyncio
    import json

    from tools.network.fleet_sync_scheduler import (
        SQLiteFleetSyncStore, roster_epoch,
    )

    store = SQLiteFleetSyncStore(personal)
    request = encode_pull_request(
        roster_epoch(scheduler._roster_snapshot,
                     scheduler.config.personal_root_pub),
        compat=store.compatibility_digest(),
        bootstrap=True,
        version=version,
    )

    async def run():
        kinds = []
        frames = await scheduler._handle("t" * 32, request, peer_pub)
        async for frame in frames:
            if frame[:1] == b"{":
                try:
                    kinds.append(json.loads(frame).get("kind"))
                except (ValueError, UnicodeDecodeError):
                    pass
        return kinds

    return asyncio.run(run())


def test_a_v4_bootstrap_pull_is_served_without_a_sweep_begin(
    tmp_path: Path,
) -> None:
    """The serve path must not emit a sweep to a peer that cannot read one.

    A v4 decoder rejects the unknown kind outright, so emitting it would fail
    the pull; worse, a peer that ignored it would apply swept rows with no
    recorded frontier -- the exact failure the version gate prevents. Asking
    for a bootstrap is not enough: the peer must also speak v5.
    """
    scheduler, personal, peer_pub = _serving_scheduler(tmp_path)
    assert SWEEP_BEGIN_KIND not in _served_kinds(
        scheduler, personal, peer_pub, 4
    )


def test_a_v5_bootstrap_pull_is_served_a_sweep_begin(tmp_path: Path) -> None:
    """The same request one version up gets the frontier, exactly once."""
    scheduler, personal, peer_pub = _serving_scheduler(tmp_path)
    kinds = _served_kinds(
        scheduler, personal, peer_pub, SWEEP_PROTOCOL_VERSION
    )
    assert kinds.count(SWEEP_BEGIN_KIND) == 1
