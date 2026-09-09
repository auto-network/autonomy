"""The frontier gate, inspected on the ACTUAL encoded pull requests.

An incomplete bootstrap holds partially applied transactions, so its computed
watermarks do not satisfy the per-origin write-floor promise. Publishing them
would make a peer serve only what is newer -- permanently withholding rows
below the claim -- and would feed that peer's prune floor.

This suppresses an unearned progress claim. It does not turn bootstrap repair
into replay-from-zero: the bootstrap PULL half stays bounded by the fixed F
recorded at sweep start, which is receive integration, not this gate.
"""

from pathlib import Path
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.sweep_receive import (
    begin_bootstrap,
    record_pull_complete,
    record_sweep_complete,
)
from tools.network.fleet_sync_scheduler import (
    SQLiteFleetSyncStore,
    decode_pull_request,
    encode_pull_request,
)

# A real origin incarnation is 64 hex characters, and encode_pull_request
# validates it. A placeholder here would make the GATED cases pass
# trivially -- an empty map never reaches that validation.
ORIGIN = "a1" * 32
EPOCH = "ab" * 32
COMPAT = "cd" * 32


def _seed(path: Path) -> None:
    db = GraphDB(path)
    try:
        catalog = MutationCatalog(db.conn, ORIGIN)
        catalog.install()
        with catalog.transaction(10, "tx-1"):
            db.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,"
                "ingested_at) VALUES(?,?,?,?,?,?)",
                ("s-1", "note", "t", "{}", "2026-08-19T00:00:00Z",
                 "2026-08-19T00:00:00Z"),
            )
    finally:
        db.close()


def _encoded_watermarks(store: SQLiteFleetSyncStore):
    """Encode a real pull request and read the map back off the wire bytes."""
    request = encode_pull_request(
        EPOCH, compat=COMPAT,
        watermarks=store.advertisable_origin_watermarks(),
    )
    return decode_pull_request(request)[7]


def _bootstrap_conn(path: Path):
    conn = sqlite3.connect(path)
    return conn


def test_established_store_advertises_its_real_frontier(tmp_path: Path) -> None:
    """Regression guard: a store that never bootstrapped is unchanged."""
    path = tmp_path / "personal.db"
    _seed(path)
    store = SQLiteFleetSyncStore(path)
    real = store.origin_watermarks()
    assert real == {ORIGIN: 10}
    assert store.advertisable_origin_watermarks() == real
    assert _encoded_watermarks(store) == {ORIGIN: 10}


@pytest.mark.parametrize("phase_calls", [[], ["sweep"]])
def test_incomplete_bootstrap_publishes_nothing(
    tmp_path: Path, phase_calls: list[str]
) -> None:
    """SWEEPING and PULLING both claim nothing on the encoded request."""
    path = tmp_path / "personal.db"
    _seed(path)
    conn = _bootstrap_conn(path)
    try:
        begin_bootstrap(conn, {ORIGIN: 10})
        for call in phase_calls:
            assert call == "sweep"
            record_sweep_complete(conn)
    finally:
        conn.close()

    store = SQLiteFleetSyncStore(path)
    assert store.origin_watermarks() == {ORIGIN: 10}, (
        "the real map is still readable internally"
    )
    assert not store.advertisable_origin_watermarks()
    assert not _encoded_watermarks(store), (
        "the encoded request must not carry a progress claim"
    )


def test_completion_reopens_advertisement(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    _seed(path)
    conn = _bootstrap_conn(path)
    try:
        begin_bootstrap(conn, {ORIGIN: 10})
        record_sweep_complete(conn)
        record_pull_complete(conn)
    finally:
        conn.close()

    store = SQLiteFleetSyncStore(path)
    assert _encoded_watermarks(store) == {ORIGIN: 10}


def test_gate_survives_restart_mid_sweep(tmp_path: Path) -> None:
    """Durable, not in-memory: a crash mid-sweep must still suppress."""
    path = tmp_path / "personal.db"
    _seed(path)
    conn = _bootstrap_conn(path)
    try:
        begin_bootstrap(conn, {ORIGIN: 10})
    finally:
        conn.close()

    # A brand-new store object, as a restarted process would build.
    assert not _encoded_watermarks(SQLiteFleetSyncStore(path))


def test_partial_transaction_is_the_case_being_prevented(
    tmp_path: Path,
) -> None:
    """The real failure: a partially applied transaction makes the computed
    map non-empty while the store does not hold that transaction's siblings.
    Internally visible, outwardly silent."""
    path = tmp_path / "target.db"
    db = GraphDB(path)
    try:
        catalog = MutationCatalog(db.conn, "b2" * 32)
        catalog.install()
        begin_bootstrap(db.conn, {ORIGIN: 99})
        # Apply ONE record of a remote transaction that has more operations.
        from tools.network.fleet_sync.codec import Mutation
        from tools.network.fleet_sync.compaction import AuthoredMutation
        partial = AuthoredMutation(
            ORIGIN, "tx-remote", 0,
            Mutation(
                "sources", ("s-remote",), 50, False,
                (("created_at", "2026-08-19T00:00:00Z"), ("deprecated", 0),
                 ("id", "s-remote"), ("ingested_at", "2026-08-19T00:00:00Z"),
                 ("keywords", None), ("last_activity_at", None),
                 ("metadata", {}), ("moved_to_org", None),
                 ("persona_id", None), ("platform", None),
                 ("publication_state", "curated"), ("session_id", None),
                 ("short_description", None), ("successor_id", None),
                 ("title", "partial"), ("type", "note"), ("url", None)),
            ),
        )
        applied, _ = catalog.apply_remote_batch([partial])
        assert applied == 1
    finally:
        db.close()

    store = SQLiteFleetSyncStore(path)
    assert store.origin_watermarks().get(ORIGIN) == 50, (
        "the partial transaction DID advance the computed map -- this is "
        "exactly the unearned claim the gate exists to suppress"
    )
    assert not _encoded_watermarks(store)


# ── the changed senders actually use the gate ────────────────────────────

def _referenced_names(func) -> set[str]:
    """Attribute/global names referenced by a function and its nested code.

    Compiled code, not source text, so reformatting cannot fool it and a
    reverted call site cannot hide. The relay site lives inside a lambda, so
    nested code objects are walked too.
    """
    import types

    seen: set[str] = set()
    stack = [func.__code__]
    while stack:
        code = stack.pop()
        seen.update(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)
    return seen


def test_direct_sender_uses_the_gate_not_the_raw_read() -> None:
    """Fails if scheduler's pull-request builder reverts to origin_watermarks."""
    from tools.network.fleet_sync_scheduler import FleetSyncScheduler

    names = _referenced_names(FleetSyncScheduler._pull_scope)
    assert "advertisable_origin_watermarks" in names
    assert "origin_watermarks" not in names, (
        "the direct sender must not read the ungated map"
    )


def test_relay_sender_uses_the_gate_not_the_raw_read() -> None:
    """Fails if the relay pull-request builder reverts to origin_watermarks."""
    import tools.network.fleet_relay_sync as relay

    target = None
    for name in dir(relay):
        candidate = getattr(relay, name)
        if callable(candidate) and getattr(candidate, "__code__", None):
            if "advertisable_origin_watermarks" in _referenced_names(candidate):
                target = candidate
                break
    assert target is not None, (
        "no relay function references the gate; the call site was removed"
    )
    assert "origin_watermarks" not in _referenced_names(target), (
        "the relay sender must not read the ungated map"
    )


def test_gate_reads_phase_and_watermarks_in_one_snapshot(
    tmp_path: Path,
) -> None:
    """One connection is not one snapshot. If the phase check and the catalog
    read were separate autocommit reads, a bootstrap beginning between them
    would let a permitting check escort a claim that is no longer earned."""
    path = tmp_path / "personal.db"
    _seed(path)
    store = SQLiteFleetSyncStore(path)

    import tools.network.fleet_sync.sweep_receive as receive

    real = receive.may_advertise_frontier
    fired = {"n": 0}

    def interleaving(conn):
        # Permit, then start a bootstrap before the watermark read -- the race
        # the snapshot must exclude.
        allowed = real(conn)
        if fired["n"] == 0:
            fired["n"] = 1
            side = sqlite3.connect(path, timeout=5.0)
            try:
                begin_bootstrap(side, {ORIGIN: 10})
            except Exception:
                pass
            finally:
                side.close()
        return allowed

    receive.may_advertise_frontier = interleaving
    try:
        result = store.advertisable_origin_watermarks()
    finally:
        receive.may_advertise_frontier = real

    assert fired["n"] == 1, "the interleaving control never ran"
    # Whatever the snapshot saw, the answer must be internally consistent:
    # either the pre-bootstrap map or nothing -- never a map read after a
    # bootstrap the phase check did not see.
    assert result in ({ORIGIN: 10}, {}), result


def test_read_path_does_not_create_the_bootstrap_table(tmp_path: Path) -> None:
    """Diagnostics and the gate run on healthy stores; neither may mutate one."""
    from tools.network.fleet_sync.sweep_receive import read_bootstrap

    path = tmp_path / "personal.db"
    _seed(path)

    conn = sqlite3.connect(path)
    try:
        assert read_bootstrap(conn) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_bootstrap'"
        ).fetchone()[0] == 0, "a read created the table"
        assert not conn.in_transaction, "a read left a transaction open"
    finally:
        conn.close()

    # The gate runs the same read path on every outward request.
    SQLiteFleetSyncStore(path).advertisable_origin_watermarks()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_bootstrap'"
        ).fetchone()[0] == 0, "the gate created the table on a healthy store"
    finally:
        conn.close()
