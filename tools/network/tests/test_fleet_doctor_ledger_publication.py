"""auto-azzvp, second deliverable: the correspondence between a founded
ledger's events and their replicated rows is asserted, not assumed.

Nothing checked this before, and the consequence was a transport that had
never once carried an organization event while every surface reported the
system healthy — an empty set looks exactly like a set with nothing to say.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network import fleet_doctor
from tools.network.ledger.settings_bridge import SET_ID


def _store(path: Path, *, events: int, published: int, catalog: bool = True,
           newest_ts: float | None = None) -> None:
    """A store with *events* ledger rows and *published* replicated rows."""
    if catalog:
        # Real capture triggers, as any synchronized store has: without them
        # there is no fleet_sync_catalog and publication cannot be judged.
        writers = GraphDB(path)
        try:
            writers.activate_fleet_sync_writers("bb" * 32)
        finally:
            writers.close()
    db = GraphDB(path)
    try:
        conn = db.conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger_events (event_id TEXT PRIMARY KEY,"
            " event_type TEXT, author_key TEXT, hlc_ts INTEGER, hlc_count INTEGER,"
            " wire BLOB)"
        )
        stamp = int(time.time() - 86_400 if newest_ts is None else newest_ts)
        for i in range(events):
            conn.execute(
                "INSERT OR IGNORE INTO ledger_events VALUES(?,?,?,?,?,?)",
                (f"{i:064x}", "genesis" if i == 0 else "invite", "aa" * 32, stamp, i, b"x"),
            )
        for i in range(published):
            conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state) VALUES(?,?,?,?,?,'published')",
                (f"row-{i}", SET_ID, 1, f"{i:064x}", '{"wire":"x"}'),
            )
        conn.commit()
    finally:
        db.close()


def _run(tmp_path: Path, monkeypatch, capsys) -> str:
    orgs = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    report: dict = {}
    fleet_doctor.check_ledger_publication(report)
    return capsys.readouterr().out


def test_a_founded_ledger_whose_events_are_not_replicated_fails(tmp_path, monkeypatch, capsys):
    orgs = tmp_path / "orgs"; orgs.mkdir(parents=True)
    _store(orgs / "acme.db", events=16, published=0)
    out = _run(tmp_path, monkeypatch, capsys)
    assert "acme" in out and "0 published < 16 event(s)" in out
    assert "NOT replicated" in out


def test_a_fully_published_ledger_passes(tmp_path, monkeypatch, capsys):
    orgs = tmp_path / "orgs"; orgs.mkdir(parents=True)
    _store(orgs / "acme.db", events=16, published=16)
    out = _run(tmp_path, monkeypatch, capsys)
    assert "16 published >= 16 event(s)" in out
    assert "NOT replicated" not in out


def test_more_published_than_events_is_not_a_finding(tmp_path, monkeypatch, capsys):
    """The set is append-only and content-keyed, so it can hold more than the
    ledger currently does — a locally pruned store, or a co-member's events
    received but not yet absorbed. Greater-or-equal, never equality."""
    orgs = tmp_path / "orgs"; orgs.mkdir(parents=True)
    _store(orgs / "acme.db", events=107, published=108)
    out = _run(tmp_path, monkeypatch, capsys)
    assert "108 published >= 107 event(s)" in out
    assert "NOT replicated" not in out


def test_a_recent_append_is_inside_the_grace_window(tmp_path, monkeypatch, capsys):
    """Publishing is asynchronous, so a freshly founded org legitimately
    shows a shortfall for a few minutes. A check that cries wolf gets
    ignored, and then the next silent hole is invisible again."""
    orgs = tmp_path / "orgs"; orgs.mkdir(parents=True)
    _store(orgs / "acme.db", events=4, published=0, newest_ts=time.time())
    out = _run(tmp_path, monkeypatch, capsys)
    assert "grace" in out
    assert "NOT replicated" not in out


def test_an_unfounded_store_is_not_a_finding(tmp_path, monkeypatch, capsys):
    orgs = tmp_path / "orgs"; orgs.mkdir(parents=True)
    _store(orgs / "acme.db", events=0, published=0)
    out = _run(tmp_path, monkeypatch, capsys)
    assert "NOT replicated" not in out
    assert "none on this machine" in out


def test_a_store_without_fleet_writers_is_a_warning_not_a_failure(tmp_path, monkeypatch, capsys):
    """No capture triggers means no catalog at all, which says nothing about
    publishing — reporting it as a failure would be a different bug."""
    orgs = tmp_path / "orgs"; orgs.mkdir(parents=True)
    _store(orgs / "acme.db", events=16, published=0, catalog=False)
    out = _run(tmp_path, monkeypatch, capsys)
    assert "fleet writers not active" in out
    assert "NOT replicated" not in out
