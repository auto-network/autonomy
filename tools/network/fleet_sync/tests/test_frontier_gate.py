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
