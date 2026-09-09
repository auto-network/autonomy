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


def test_the_serve_path_gates_its_emit_on_the_sweep_version() -> None:
    """An old client must never RECEIVE a sweep.begin.

    STRUCTURAL guard: it inspects the compiled serve generator, proving the
    emit is conditioned on SWEEP_PROTOCOL_VERSION and not unconditional. It
    does NOT prove a running server withheld the record from a v4 peer -- that
    needs the harness, whose machines are subprocesses. Stated so this is not
    mistaken for wire evidence.

    The previous version of this control asserted `not (3 >= 5)`, which is
    arithmetic and would have passed against an unconditional emit. Its
    replacement then inspected `_pull_scope`, the CLIENT side, and failed --
    correctly, because the emit lives in the server's `_handle` response
    generator. Both mistakes are the same one: proving something about code
    that does not do the thing.
    """
    import types

    from tools.network.fleet_sync_scheduler import FleetSyncScheduler

    seen: set[str] = set()
    stack = [FleetSyncScheduler._handle.__code__]
    while stack:
        code = stack.pop()
        seen.update(code.co_names)
        seen.update(c for c in code.co_consts if isinstance(c, str))
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)

    assert "SWEEP_PROTOCOL_VERSION" in seen, (
        "the serve path does not consult the sweep version at all, so its "
        "emit cannot be gated on it"
    )
    assert "SWEEP_BEGIN_KIND" in seen


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
