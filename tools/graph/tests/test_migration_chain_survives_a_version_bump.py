"""A version bump must not be able to take the graph API down.

On 2026-09-08 bumping ``_SCHEMA_USER_VERSION`` for an unrelated table removal
made every store re-run the whole schema-phase chain. One entry could not
complete on a fleet-activated store, and because the version stamps only
after the WHOLE chain, its failure un-ran the ones that had just succeeded
and the next open repeated it. Every writable open failed for twelve
minutes.

The control below REPRODUCES that, so the passing case means something. Three
things had to be right before it would reproduce at all, and each was a wrong
turn worth recording:

1. The store must be REOPENED after activation. The fail-closed capture guard
   is installed at connection-open time from the presence of triggers, so a
   connection that predates them is not guarded.
2. The write must happen INSIDE ``_init_schema``. Once the catalog attaches,
   the raising stub is replaced by the real capture functions, so calling the
   migration directly afterwards can never fail.
3. The org must be ``shared`` and rows must actually have NULL personas, or
   the migration returns early and writes nothing.

A fixture that satisfies none of those passes against broken code.
"""

from pathlib import Path
import sqlite3

import pytest

from tools.graph import db as db_module
from tools.graph import org_ops
from tools.graph.db import GraphDB


def _activated_store_with_unattributed_rows(path: Path) -> Path:
    db = GraphDB(path)
    db.conn.execute(
        "INSERT INTO orgs(id,slug,type,created_at) "
        "VALUES('o1','autonomy','shared','2026-01-01T00:00:00Z')"
    )
    for index in range(5):
        db.conn.execute(
            "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at,"
            f"persona_id) VALUES('s{index}','note','t{index}','{{}}',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',NULL)"
        )
    db.conn.commit()
    db.activate_fleet_sync_writers("ee" * 32)
    # Reopen so the next connection sees the triggers and installs the
    # fail-closed guard. Without this the fixture cannot reproduce anything.
    db.close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {db_module._SCHEMA_USER_VERSION}")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def unattributed(monkeypatch):
    monkeypatch.setattr(org_ops, "local_persona_pub", lambda: "ff" * 32)


def test_the_control_still_reproduces_the_outage(
    tmp_path: Path, monkeypatch, unattributed
) -> None:
    """Put a replicated-row rewrite back in the chain and bump: it must fail
    exactly as it did, and must leave the version UNSTAMPED -- which is what
    turned one failure into every-open-forever."""
    path = _activated_store_with_unattributed_rows(tmp_path / "control.db")
    before = db_module._SCHEMA_USER_VERSION

    monkeypatch.setattr(
        GraphDB, "_legacy_backfill", GraphDB.backfill_content_persona,
        raising=False,
    )
    monkeypatch.setattr(
        db_module, "_SCHEMA_PHASE_MIGRATIONS",
        db_module._SCHEMA_PHASE_MIGRATIONS + ((before + 1, "_legacy_backfill"),),
    )
    monkeypatch.setattr(db_module, "_SCHEMA_USER_VERSION", before + 1)

    with pytest.raises(sqlite3.IntegrityError, match="uncaptured write"):
        GraphDB(path).close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == before, (
            "the version stamped despite the failure; the replay loop that "
            "caused the outage depends on it NOT stamping"
        )
    finally:
        conn.close()


def test_a_version_bump_completes_on_an_activated_store(
    tmp_path: Path, monkeypatch, unattributed
) -> None:
    """The same fixture and the same bump against the shipped chain."""
    path = _activated_store_with_unattributed_rows(tmp_path / "fixed.db")
    target = db_module._SCHEMA_USER_VERSION + 1
    monkeypatch.setattr(db_module, "_SCHEMA_USER_VERSION", target)

    db = GraphDB(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == target
    finally:
        db.close()


def test_a_bump_runs_only_the_migrations_newer_than_the_store(
    tmp_path: Path, monkeypatch, unattributed
) -> None:
    """The whole chain re-running on every bump is what made one broken
    entry catastrophic rather than local."""
    path = _activated_store_with_unattributed_rows(tmp_path / "gated.db")
    called: list[str] = []
    for _version, name in db_module._SCHEMA_PHASE_MIGRATIONS:
        original = getattr(GraphDB, name)

        def wrapper(self, *a, _n=name, _o=original, **k):
            called.append(_n)
            return _o(self, *a, **k)

        monkeypatch.setattr(GraphDB, name, wrapper)
    monkeypatch.setattr(
        db_module, "_SCHEMA_USER_VERSION", db_module._SCHEMA_USER_VERSION + 1
    )

    GraphDB(path).close()
    assert called == [], (
        "a bump re-ran already-applied migrations on an activated store: "
        f"{called}"
    )
