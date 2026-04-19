"""Tests for `graph sessions --status [--since ...]` CLI output.

Covers auto-0r86:
  1. No --since: live-only rows in output, dead rows absent.
  2. --since window: recent dead + live rows appear; older rows excluded.
  3. `last` column includes the date (MM-DD prefix), not just HH:MM:SS.
  4. Malformed --since exits non-zero via the shared duration parser.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal dashboard.db with the columns the status table reads."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tmux_sessions (
            tmux_name       TEXT PRIMARY KEY,
            created_at      REAL NOT NULL,
            is_live         INTEGER DEFAULT 1,
            last_activity   REAL,
            last_message    TEXT DEFAULT '',
            entry_count     INTEGER DEFAULT 0,
            context_tokens  INTEGER DEFAULT 0,
            label           TEXT DEFAULT '',
            activity_state  TEXT DEFAULT 'idle'
        )
        """
    )
    now = time.time()
    rows = [
        ("live-busy", now - 30, 1, now - 30, "hello", 10, 4200, "busy-label", "busy"),
        ("live-idle", now - 120, 1, now - 120, "hi", 5, 800, "idle-label", "idle"),
        ("dead-recent", now - 3600 * 5, 0, now - 3600 * 5, "", 99, 42000, "post-mortem", "dead"),
        ("dead-old", now - 86400 * 3, 0, now - 86400 * 3, "", 50, 12000, "ancient", "dead"),
    ]
    conn.executemany(
        "INSERT INTO tmux_sessions (tmux_name,created_at,is_live,last_activity,last_message,"
        "entry_count,context_tokens,label,activity_state) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def _fake_root(tmp_path, monkeypatch):
    """Point `_print_session_status` at tmp_path/data/dashboard.db.

    It resolves the db path via `Path(__file__).parents[2] / 'data' / 'dashboard.db'`,
    so we rewrite the module's `__file__` to live under tmp_path/tools/graph/cli.py.
    """
    _make_db(tmp_path)
    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))
    return cli


def test_status_no_since_lists_live_only(_fake_root, capsys):
    _fake_root._print_session_status()
    out = capsys.readouterr().out
    assert "live-busy" in out
    assert "live-idle" in out
    assert "dead-recent" not in out
    assert "dead-old" not in out


def test_status_since_includes_dead_within_window(_fake_root, capsys):
    _fake_root._print_session_status(since="24h")
    out = capsys.readouterr().out
    assert "live-busy" in out
    assert "live-idle" in out
    assert "dead-recent" in out, "dead session within window must appear with --since"
    assert "dead-old" not in out, "dead session outside window must be filtered out"


def test_status_state_column_marks_dead(_fake_root, capsys):
    _fake_root._print_session_status(since="24h")
    out = capsys.readouterr().out
    for line in out.splitlines():
        if line.startswith("dead-recent"):
            assert "dead" in line, f"state column must show 'dead': {line!r}"
            return
    pytest.fail("dead-recent row not printed")


def test_status_last_column_has_date_prefix(_fake_root, capsys):
    _fake_root._print_session_status()
    out = capsys.readouterr().out
    pat = re.compile(r"\b\d{2}-\d{2} \d{2}:\d{2}:\d{2}\b")
    body = [l for l in out.splitlines() if l.startswith("live-")]
    assert body, "no live rows in output"
    for line in body:
        assert pat.search(line), f"row missing MM-DD HH:MM:SS timestamp: {line!r}"


def test_status_invalid_since_exits_nonzero(_fake_root, capsys):
    with pytest.raises(SystemExit) as exc:
        _fake_root._print_session_status(since="bogus")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Invalid duration" in err


def test_status_column_headers_match_spec(_fake_root, capsys):
    _fake_root._print_session_status()
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    for col in ("TMUX", "STATE", "LAST", "TOKENS", "LABEL"):
        assert col in header, f"missing column {col!r} in header: {header!r}"
