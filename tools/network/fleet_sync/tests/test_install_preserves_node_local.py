"""A checkpoint install must never cost the receiver its node-local state.

Repro of the 2026-09-06 class: the receiver holds a founded ledger (rows in
ledger_events/ledger_heads) and personal-local tables that the sync policy
declares never-replicated; a member's checkpoint carries none of them.
Before this fix ``install_checkpoint`` rebuilt the database file from the
checkpoint plus a hard-coded copy list (orgs + keycontrol_*), so the ledger
was gone after the swap. Now:

* an install over a FOUNDED ledger is refused outright (the store holds
  more authority than the incoming copy) unless an explicit rebase flag is
  passed -- no production caller passes it;
* with the flag, and for every non-founded store, every node-local table
  (schema + rows) is carried across the swap intact.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.sync import (
    AlphaError,
    FleetSyncAlpha,
    founded_ledger_rows,
    install_checkpoint,
)
from tools.network.ledger import store as ledger_store

EPOCH = "a" * 64
ROSTER = ("machine-a", "machine-b")


def _source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-09-06T00:00:00Z", "2026-09-06T00:00:00Z"),
    )


def _member_checkpoint(tmp_path: Path) -> Path:
    checkpoint = tmp_path / "checkpoint"
    with FleetSyncAlpha(tmp_path / "member.db", "machine-b") as member:
        with member.author(100, "member-seed"):
            for index in range(5):
                _source(member.graph.conn, f"remote-{index}", f"remote {index}")
        member.checkpoint(checkpoint, roster_epoch=EPOCH, active_roster=ROSTER)
    return checkpoint


def _found_ledger(path: Path, events: int) -> None:
    """Give *path* the ledger schema and *events* founded events + one head."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ledger_store._SCHEMA)
        event_type = sorted(ledger_store.EVENT_TYPES)[0]
        for index in range(events):
            conn.execute(
                "INSERT INTO ledger_events(event_id,event_type,author_key,hlc_ts,"
                "hlc_count,wire) VALUES(?,?,?,?,?,?)",
                (f"ev-{index:03d}", event_type, "ab" * 32, 1000 + index, 0, b"wire"),
            )
        if events:
            conn.execute("INSERT INTO ledger_heads(event_id) VALUES(?)", (f"ev-{events-1:03d}",))
        conn.execute(
            "INSERT INTO ledger_meta(key,value) VALUES('genesis','g-1')"
        )
        conn.commit()
    finally:
        conn.close()


def _ledger_snapshot(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for table in ("ledger_meta", "ledger_events", "ledger_heads"):
            out[table] = conn.execute(
                f'SELECT * FROM "{table}" ORDER BY 1'
            ).fetchall()
        return out
    finally:
        conn.close()


def _origin_with_state(tmp_path: Path) -> Path:
    target = tmp_path / "personal.db"
    with FleetSyncAlpha(target, "machine-a") as origin:
        with origin.author(50, "local"):
            _source(origin.graph.conn, "local-only", "mine")
    return target


def test_install_over_a_founded_ledger_is_refused_and_leaves_the_file_alone(tmp_path):
    checkpoint = _member_checkpoint(tmp_path)
    target = _origin_with_state(tmp_path)
    _found_ledger(target, events=95)
    assert founded_ledger_rows(target) == 96
    before_stat = os.stat(target)
    before_ledger = _ledger_snapshot(target)

    with pytest.raises(AlphaError, match="founded ledger"):
        install_checkpoint(
            checkpoint, target,
            target_origin_incarnation="machine-a",
            expected_roster_epoch=EPOCH, expected_active_roster=ROSTER,
            merge_existing=True, checkpoint_source_machine="machine-b",
        )
    after_stat = os.stat(target)
    assert (after_stat.st_ino, after_stat.st_size) == (before_stat.st_ino, before_stat.st_size)
    assert _ledger_snapshot(target) == before_ledger
    assert not (tmp_path / ".personal.db.pre-fleet-sync").exists()
    assert not list(tmp_path.glob(".fleet-sync-install-*"))


def test_explicit_rebase_carries_the_ledger_and_local_tables_across_the_swap(tmp_path):
    checkpoint = _member_checkpoint(tmp_path)
    target = _origin_with_state(tmp_path)
    _found_ledger(target, events=3)
    before_ledger = _ledger_snapshot(target)

    installed = install_checkpoint(
        checkpoint, target,
        target_origin_incarnation="machine-a",
        expected_roster_epoch=EPOCH, expected_active_roster=ROSTER,
        merge_existing=True, checkpoint_source_machine="machine-b",
        allow_founded_ledger_rebase=True,
    )
    assert installed is not None
    # the file was replaced (new inode) -- and the ledger came with it
    assert _ledger_snapshot(target) == before_ledger
    assert founded_ledger_rows(target) == 4
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        ids = {row[0] for row in conn.execute("SELECT id FROM sources")}
        assert {"remote-0", "remote-4", "local-only"} <= ids   # merged, not replaced
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_member_with_empty_ledger_schema_installs_and_keeps_the_schema(tmp_path):
    """SJC's shape: ledger tables exist with zero rows. Not founded, so the
    bootstrap install proceeds, and the empty schema survives the swap."""
    checkpoint = _member_checkpoint(tmp_path)
    target = tmp_path / "personal.db"
    GraphDB(target).close()
    _found_ledger(target, events=0)
    assert founded_ledger_rows(target) == 0

    install_checkpoint(
        checkpoint, target,
        target_origin_incarnation="machine-c",
        expected_roster_epoch=EPOCH, expected_active_roster=ROSTER + ("machine-c",),
        merge_existing=True, checkpoint_source_machine="machine-b",
    )
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"ledger_events", "ledger_heads", "ledger_meta"} <= tables
        assert conn.execute("SELECT value FROM ledger_meta WHERE key='genesis'").fetchone()[0] == "g-1"
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 5
    finally:
        conn.close()
