"""v3 cannot express a grouped transaction, so the serve must not group for it.

Every v3 frame declares the transaction's operation count, and the v3 receiver
rejects a second frame of the same transaction declaring a different one. The
serve pages transactions at SERVE_GROUP_OPERATIONS, which for v3 declares the
size of each PAGE -- so a transaction with more than one page of surviving
operations broke every v3 pull, permanently and deterministically.

Two shapes matter and they fail differently:

* an UNEVEN spill (2001) declares 2000 then 1 -> "operation count changed";
* an EVEN multiple (4000) declares 2000 twice, so the count never changes and
  the raise never fires -- but the receiver accumulates 4000 operations under a
  declared count of 2000 and fails its end-of-transaction length check instead.

Testing only the first would have left the second live.
"""

from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import (
    MAX_TRANSACTION_OPERATIONS, MutationCatalog,
)
from tools.network.fleet_sync_scheduler import (
    SERVE_GROUP_OPERATIONS, decode_authored, encode_authored,
    _transaction_identity,
)

ORIGIN = "a1" * 32


def _store_with_transaction(path: Path, rows: int):
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, ORIGIN)
    catalog.install()
    with catalog.transaction(10, "tx-big"):
        for index in range(rows):
            db.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,"
                "ingested_at) VALUES(?,?,?,?,?,?)",
                (f"s-{index:06d}", "note", "t", "{}",
                 "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
            )
    ref = db.conn.execute(
        "SELECT MAX(id) FROM fleet_sync_transactions"
    ).fetchone()[0]
    return db, catalog, ref


def _serve(catalog, ref, *, protocol_version: int):
    """Mirror the serve loop's grouping and per-frame count declaration."""
    limit = (
        SERVE_GROUP_OPERATIONS if protocol_version >= 4
        else MAX_TRANSACTION_OPERATIONS
    )
    offset, more, declared, frames = 0, True, [], []
    while more:
        items, more = catalog.transaction_group(
            ref, ORIGIN, "tx-big", offset=offset, limit=limit
        )
        offset += limit
        if not items:
            continue
        operation_count = len(items)
        declared.append(operation_count)
        for item in items:
            frames.append(
                encode_authored(item, transaction_operations=operation_count)
            )
    return declared, frames


def _receive_v3(frames):
    """The v3 receiver's accounting: per-frame declared count, then length."""
    pending, pending_count, identity = [], None, None
    for frame in frames:
        item, operation_count = decode_authored(frame)
        current = _transaction_identity(item)
        if identity is not None and current != identity:
            raise AssertionError("unexpected identity change in this fixture")
        identity = current
        if pending and pending_count != operation_count:
            raise ValueError(
                f"operation count changed: {pending_count} -> {operation_count}"
            )
        pending_count = operation_count
        pending.append(item)
    if pending_count != len(pending):
        raise ValueError(
            f"incomplete transaction: declared {pending_count}, "
            f"received {len(pending)}"
        )
    return len(pending)


@pytest.mark.parametrize("rows", [SERVE_GROUP_OPERATIONS + 1,
                                  SERVE_GROUP_OPERATIONS * 2])
def test_v3_receives_every_operation_of_a_multi_page_transaction(
    tmp_path: Path, rows: int
) -> None:
    """The fix: v3 is served the whole transaction, so both shapes apply."""
    db, catalog, ref = _store_with_transaction(tmp_path / f"s{rows}.db", rows)
    try:
        declared, frames = _serve(catalog, ref, protocol_version=3)
        assert len(set(declared)) == 1, (
            f"v3 was served in {len(declared)} groups declaring {declared}; "
            "it has no grouping concept"
        )
        assert _receive_v3(frames) == rows
    finally:
        db.close()


@pytest.mark.parametrize("rows,failure", [
    (SERVE_GROUP_OPERATIONS + 1, "operation count changed"),
    (SERVE_GROUP_OPERATIONS * 2, "incomplete transaction"),
])
def test_the_old_paging_broke_v3_in_two_different_ways(
    tmp_path: Path, rows: int, failure: str
) -> None:
    """The defect, reproduced against the OLD behaviour.

    Reinstating page-sized groups for v3 must fail -- and the two shapes fail
    with different messages, which is why both are covered.
    """
    db, catalog, ref = _store_with_transaction(tmp_path / f"o{rows}.db", rows)
    try:
        # Old behaviour: v3 paged like v4.
        declared, frames = _serve(catalog, ref, protocol_version=4)
        assert len(declared) > 1, "fixture did not span a page boundary"
        with pytest.raises(ValueError, match=failure):
            _receive_v3(frames)
    finally:
        db.close()


def test_locally_authored_transactions_are_capped(tmp_path: Path) -> None:
    """Serving v3 in one group relies on a source-side cap, so prove it.

    The remote-ingestion bound in apply_remote_batch does NOT establish this:
    it governs what arrives, not what this machine can author.
    """
    import sqlite3

    db = GraphDB(tmp_path / "cap.db")
    catalog = MutationCatalog(db.conn, ORIGIN)
    catalog.install()
    try:
        # The bound is enforced inside a capture trigger, so SQLite surfaces
        # it as OperationalError("user-defined function raised exception")
        # rather than the IntegrityError the raise names. The cap is real
        # either way -- what matters here is that authoring cannot exceed the
        # limit the v3 serve now relies on.
        with pytest.raises(sqlite3.OperationalError):
            with catalog.transaction(10, "tx-over"):
                for index in range(MAX_TRANSACTION_OPERATIONS + 1):
                    db.conn.execute(
                        "INSERT INTO sources(id,type,title,metadata,"
                        "created_at,ingested_at) VALUES(?,?,?,?,?,?)",
                        (f"c-{index:06d}", "note", "t", "{}",
                         "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
                    )
    finally:
        db.close()
