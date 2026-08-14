"""A pre-index org DB self-heals its duplicate live bases on open.

Regression for auto-55jwx (self-heal cf928f5d): before idx_settings_one_base
existed, callers could leave multiple live same-publication_state base rows
for one key. A bare CREATE UNIQUE INDEX refuses such a table, which bricked
any un-migrated DB (a backup restore, a dev copy) at open — looking like
corruption rather than policy. The migration now deprecates the losing
duplicates FIRST (created_at DESC, id DESC — the same winner upsert_by_key
updates), keeps every row for history, and only then builds the index.

Acceptance shape verified with the settings owner: three live same-state raw
bases (B < A < C by created_at) plus one canonical base for the SAME key —
the canonical row is a different publication_state group and must survive
untouched. The heal is a correlated "newer live sibling exists" update, so
the winner is never touched whatever the insertion order; a variant inserts
the duplicates in reverse recency order to pin that order-independence.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from tools.graph.db import GraphDB

SET_ID = "autonomy.test.selfheal"
KEY = "the-key"


def _insert_base(conn, *, state: str, created_at: str, marker: str) -> str:
    row_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO settings (id, set_id, schema_revision, key, payload,"
        " publication_state, supersedes, excludes, deprecated, created_at,"
        " updated_at) VALUES (?, ?, 1, ?, ?, ?, NULL, NULL, 0, ?, ?)",
        (row_id, SET_ID, KEY, '{"marker": "%s"}' % marker, state,
         created_at, created_at),
    )
    return row_id


def _make_preindex_db(tmp_path, insertion_order):
    """A DB shaped like the pre-index era: index dropped, duplicates present.

    ``insertion_order`` is a list of (marker, created_at) for the raw-state
    rows; a canonical row for the same key is always added last.
    """
    db = GraphDB.create_org_db(f"heal-{uuid.uuid4().hex[:8]}", root=tmp_path)
    db.conn.execute("DROP INDEX idx_settings_one_base")
    ids = {}
    for marker, created_at in insertion_order:
        ids[marker] = _insert_base(
            db.conn, state="raw", created_at=created_at, marker=marker)
    ids["canonical"] = _insert_base(
        db.conn, state="canonical", created_at="2026-01-01T00:00:00Z",
        marker="canonical")
    # A real pre-index DB carries an older schema stamp; reopen skips every
    # migration when user_version is current (db.py:400), so resetting the
    # stamp is what routes the reopen through _migrate_settings at all.
    db.conn.execute("PRAGMA user_version = 0")
    db.conn.commit()
    path = pathlib.Path(db.conn.execute("PRAGMA database_list").fetchone()[2])
    db.close()
    GraphDB.close_all_pooled()
    return path, ids


# B < A < C by created_at; A inserted first so the newest is neither the
# first nor the last insert.
_CHRONO = [
    ("A", "2026-01-02T00:00:00Z"),
    ("B", "2026-01-01T00:00:00Z"),
    ("C", "2026-01-03T00:00:00Z"),
]


@pytest.mark.parametrize(
    "insertion_order",
    [_CHRONO, list(reversed(_CHRONO))],
    ids=["mixed-order", "reverse-recency-order"],
)
def test_preindex_duplicates_self_heal_on_open(tmp_path, insertion_order):
    path, ids = _make_preindex_db(tmp_path, insertion_order)

    # Reopen: migrations run. Pre-fix this raised IntegrityError at
    # CREATE UNIQUE INDEX; now it must open and converge.
    db = GraphDB(path)
    try:
        rows = db.conn.execute(
            "SELECT id, publication_state, deprecated FROM settings"
            " WHERE set_id = ? AND key = ?", (SET_ID, KEY),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}

        # History intact: all four rows retained, none deleted.
        assert len(rows) == 4

        # The newest raw base (C) is the sole live raw row.
        live_raw = [r for r in rows
                    if r["publication_state"] == "raw" and not r["deprecated"]]
        assert [r["id"] for r in live_raw] == [ids["C"]]

        # The losers are deprecated, not deleted.
        assert by_id[ids["A"]]["deprecated"] == 1
        assert by_id[ids["B"]]["deprecated"] == 1

        # The canonical row is a different publication_state group:
        # untouched and live.
        assert by_id[ids["canonical"]]["deprecated"] == 0

        # The one-live-base-row index exists after the heal.
        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='idx_settings_one_base'").fetchone() is not None
    finally:
        db.close()
        GraphDB.close_all_pooled()


def test_clean_db_open_is_a_noop(tmp_path):
    """An already-clean DB opens with nothing deprecated by the heal."""
    db = GraphDB.create_org_db(f"clean-{uuid.uuid4().hex[:8]}", root=tmp_path)
    _insert_base(db.conn, state="raw", created_at="2026-01-01T00:00:00Z",
                 marker="only")
    # Route the reopen through the migrations so the heal actually RUNS
    # and no-ops — with a current stamp it would be skipped, and this
    # test would pass without exercising anything.
    db.conn.execute("PRAGMA user_version = 0")
    db.conn.commit()
    path = pathlib.Path(db.conn.execute("PRAGMA database_list").fetchone()[2])
    db.close()
    GraphDB.close_all_pooled()

    db = GraphDB(path)
    try:
        rows = db.conn.execute(
            "SELECT deprecated FROM settings WHERE set_id = ? AND key = ?",
            (SET_ID, KEY)).fetchall()
        assert [r["deprecated"] for r in rows] == [0]
    finally:
        db.close()
        GraphDB.close_all_pooled()


def test_prior_version_stamp_with_duplicates_heals_on_open(tmp_path):
    """The version bump (auto-uq1mi) re-runs heal-then-index fleet-wide:
    a DB stamped at the PRIOR schema version — the fleet's real state
    before the bump, where the index commit never bumped the stamp — with
    live same-state duplicates opens, converges to one live base, gains
    the index, and re-stamps current."""
    from tools.graph.db import _SCHEMA_USER_VERSION

    prior = _SCHEMA_USER_VERSION - 1
    db = GraphDB.create_org_db(f"bump-{uuid.uuid4().hex[:8]}", root=tmp_path)
    db.conn.execute("DROP INDEX idx_settings_one_base")
    ids = {}
    for marker, created_at in _CHRONO:
        ids[marker] = _insert_base(
            db.conn, state="raw", created_at=created_at, marker=marker)
    db.conn.execute(f"PRAGMA user_version = {prior}")
    db.conn.commit()
    path = pathlib.Path(db.conn.execute("PRAGMA database_list").fetchone()[2])
    db.close()
    GraphDB.close_all_pooled()

    db = GraphDB(path)
    try:
        rows = db.conn.execute(
            "SELECT id, deprecated FROM settings WHERE set_id = ? AND key = ?",
            (SET_ID, KEY)).fetchall()
        live = [r["id"] for r in rows if not r["deprecated"]]
        assert live == [ids["C"]], "newest wins; losers deprecated"
        assert len(rows) == 3, "history retained"
        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='idx_settings_one_base'").fetchone() is not None
        stamped = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert stamped == _SCHEMA_USER_VERSION, "re-stamped current"
    finally:
        db.close()
        GraphDB.close_all_pooled()
