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
    for col in ("TMUX", "STATE", "LAST", "TOKENS", "SOURCE", "LABEL"):
        assert col in header, f"missing column {col!r} in header: {header!r}"


def _make_db_with_source_ids(tmp_path: Path) -> Path:
    """Like _make_db but includes a graph_source_id column populated for one row."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tmux_sessions (
            tmux_name       TEXT PRIMARY KEY,
            graph_source_id TEXT,
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
        ("auto-known", "abcdef0123456789cafe", now - 30, 1, now - 30,
         "hello", 10, 4200, "linked", "busy"),
        ("auto-unlinked", None, now - 120, 1, now - 120,
         "hi", 5, 800, "no source", "idle"),
    ]
    conn.executemany(
        "INSERT INTO tmux_sessions (tmux_name,graph_source_id,created_at,is_live,"
        "last_activity,last_message,entry_count,context_tokens,label,activity_state)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


def test_status_renders_source_id_column(tmp_path, monkeypatch, capsys):
    """SOURCE column shows the linked graph_source_id (12-char prefix)."""
    _make_db_with_source_ids(tmp_path)
    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))

    cli._print_session_status()
    out = capsys.readouterr().out
    linked = [line for line in out.splitlines() if line.startswith("auto-known")]
    unlinked = [line for line in out.splitlines() if line.startswith("auto-unlinked")]
    assert linked and "abcdef012345" in linked[0], linked
    assert unlinked and "—" in unlinked[0], unlinked


def test_resolve_tmux_name_lookup(tmp_path, monkeypatch):
    """_resolve_tmux_name_to_source_id reads dashboard.db when present."""
    _make_db_with_source_ids(tmp_path)
    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))
    monkeypatch.delenv("GRAPH_API", raising=False)

    assert cli._resolve_tmux_name_to_source_id("auto-known") == "abcdef0123456789cafe"
    assert cli._resolve_tmux_name_to_source_id("auto-unlinked") is None
    assert cli._resolve_tmux_name_to_source_id("auto-missing") is None


def test_looks_like_tmux_name_heuristic():
    """Hex-only source-id prefixes do NOT trigger tmux lookup."""
    from tools.graph import cli
    assert cli._looks_like_tmux_name("auto-0506-001257") is True
    assert cli._looks_like_tmux_name("host-0506-095207") is True
    assert cli._looks_like_tmux_name("abcdef0123456789") is False  # hex-only
    assert cli._looks_like_tmux_name("f6c6c43e-24a") is False  # short ID
    assert cli._looks_like_tmux_name("8cdc1d85") is False
    assert cli._looks_like_tmux_name("") is False


def test_lookup_tmux_for_source_by_graph_id(tmp_path, monkeypatch):
    """_lookup_tmux_for_source resolves session sources via graph_source_id."""
    _make_db_with_source_ids(tmp_path)
    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))

    src = {"id": "abcdef0123456789cafe", "type": "session", "metadata": {}}
    assert cli._lookup_tmux_for_source(src) == "auto-known"

    # Non-session sources should return None even when an ID would match.
    note = {"id": "abcdef0123456789cafe", "type": "note", "metadata": {}}
    assert cli._lookup_tmux_for_source(note) is None


def test_lookup_tmux_for_source_by_session_uuid(tmp_path, monkeypatch):
    """Falls back to session_uuid when graph_source_id link hasn't landed yet."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tmux_sessions (
            tmux_name       TEXT PRIMARY KEY,
            session_uuid    TEXT,
            graph_source_id TEXT,
            created_at      REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tmux_sessions (tmux_name,session_uuid,graph_source_id,created_at) VALUES (?,?,?,?)",
        ("auto-pending-link", "deadbeef-uuid", None, time.time()),
    )
    conn.commit()
    conn.close()

    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))

    src = {
        "id": "fresh000000000000000",
        "type": "session",
        "metadata": {"session_uuid": "deadbeef-uuid"},
    }
    assert cli._lookup_tmux_for_source(src) == "auto-pending-link"


def test_print_session_header_chip_uses_attached_field(tmp_path, monkeypatch, capsys):
    """Chip prefers ``tmux_session`` already attached by the API."""
    from tools.graph import cli
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    src = {"id": "x" * 20, "type": "session", "tmux_session": "auto-from-api"}
    cli._print_session_header_chip(src)
    out = capsys.readouterr().out
    assert "Session: auto-from-api" in out
    assert "Viewer: https://localhost:8080/session/auto-from-api" in out


def test_print_session_header_chip_silent_when_no_match(tmp_path, monkeypatch, capsys):
    """Non-session sources or missing tmux name produce no output."""
    from tools.graph import cli
    note = {"id": "f" * 20, "type": "note"}
    cli._print_session_header_chip(note)
    assert capsys.readouterr().out == ""


def _make_db_with_topics(tmp_path: Path) -> Path:
    """Status DB seeded with a topics JSON column on one row."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tmux_sessions (
            tmux_name       TEXT PRIMARY KEY,
            graph_source_id TEXT,
            topics          TEXT DEFAULT '[]',
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
        ("auto-busy", "abcdef0123456789cafe",
         '["wiring tmux name resolver", "tests passing"]',
         now - 30, 1, now - 30, "hi", 10, 4200, "drift label", "busy"),
        ("auto-empty", "1234567890abcafe1234",
         "[]",
         now - 120, 1, now - 120, "hi", 5, 800, "no topics", "idle"),
    ]
    conn.executemany(
        "INSERT INTO tmux_sessions (tmux_name,graph_source_id,topics,created_at,"
        "is_live,last_activity,last_message,entry_count,context_tokens,label,activity_state)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


def test_status_topics_flag_off_by_default(tmp_path, monkeypatch, capsys):
    """Topics never appear without --topics."""
    _make_db_with_topics(tmp_path)
    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))

    cli._print_session_status()
    out = capsys.readouterr().out
    assert "wiring tmux name resolver" not in out


def test_status_topics_flag_renders_topic_lines(tmp_path, monkeypatch, capsys):
    """--topics appends one ⤷-prefixed line per topic under the row."""
    _make_db_with_topics(tmp_path)
    from tools.graph import cli
    (tmp_path / "tools" / "graph").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools" / "graph" / "cli.py"))

    cli._print_session_status(show_topics=True)
    out = capsys.readouterr().out
    assert "⤷ wiring tmux name resolver" in out
    assert "⤷ tests passing" in out
    # The empty-topics row should still render but produce no topic lines.
    busy_lines = [l for l in out.splitlines() if "auto-busy" in l]
    empty_lines = [l for l in out.splitlines() if "auto-empty" in l]
    assert busy_lines and empty_lines


def test_format_source_header_uses_minute_for_notes():
    from tools.graph import cli
    note = {
        "id": "abcdef0123456789cafe", "type": "note",
        "title": "Some Note", "project": "autonomy", "org": "autonomy",
        "created_at": "2026-05-06T17:31:42Z",
    }
    head = cli._format_source_header(note)
    # Full minute-precision timestamp, not just YYYY-MM-DD.
    assert "2026-05-06 17:31" in head
    assert "Some Note" in head
    # No bracketed-org duplicate of project.
    assert head.count("autonomy") == 1


def test_format_source_header_marks_withdrawn():
    from tools.graph import cli
    note = {
        "id": "abcdef0123456789cafe", "type": "note", "deprecated": 1,
        "title": "Some Note", "project": "autonomy", "org": "autonomy",
        "created_at": "2026-05-06T17:31:42Z",
    }
    head = cli._format_source_header(note)
    assert "[withdrawn]" in head


def test_format_source_header_omits_withdrawn_marker_when_not_deprecated():
    from tools.graph import cli
    note = {
        "id": "abcdef0123456789cafe", "type": "note", "deprecated": 0,
        "title": "Some Note", "project": "autonomy", "org": "autonomy",
        "created_at": "2026-05-06T17:31:42Z",
    }
    head = cli._format_source_header(note)
    assert "[withdrawn]" not in head


def test_format_source_header_session_range_same_day():
    from tools.graph import cli
    sess = {
        "id": "1234567890abcafe1234", "type": "session",
        "title": "auto-0506-173131", "project": "autonomy", "org": "autonomy",
        "created_at": "2026-05-06T17:31:00Z",
        "metadata": {
            "started_at": "2026-05-06T17:31:00Z",
            "ended_at":   "2026-05-06T18:14:00Z",
        },
    }
    head = cli._format_source_header(sess)
    # Same-day collapses to start-date + start-min → end-min only.
    assert "2026-05-06 17:31 → 18:14" in head, head
    assert "auto-0506-173131" in head


def test_format_source_header_session_range_multi_day():
    from tools.graph import cli
    sess = {
        "id": "1234567890abcafe1234", "type": "session",
        "title": "long session", "project": "autonomy", "org": "autonomy",
        "created_at": "2026-05-06T17:31:00Z",
        "metadata": {
            "started_at": "2026-05-06T17:31:00Z",
            "ended_at":   "2026-05-07T03:14:00Z",
        },
    }
    head = cli._format_source_header(sess)
    # Cross-day: full timestamp on each side.
    assert "2026-05-06 17:31 → 2026-05-07 03:14" in head, head


def test_format_source_header_session_falls_back_to_created_at():
    """Sessions without started_at/ended_at metadata still get a header."""
    from tools.graph import cli
    sess = {
        "id": "1234567890abcafe1234", "type": "session",
        "title": "in-flight session", "project": "autonomy",
        "created_at": "2026-05-06T17:31:00Z",
        "metadata": {},
    }
    head = cli._format_source_header(sess)
    assert "2026-05-06 17:31" in head
    assert "→" not in head  # no range available


def test_row_topic_lines_handles_bad_input():
    """Defensive parsing — bad JSON / non-list / null all return []."""
    from tools.graph import cli
    assert cli._row_topic_lines({"topics": None}) == []
    assert cli._row_topic_lines({"topics": ""}) == []
    assert cli._row_topic_lines({"topics": "not json"}) == []
    assert cli._row_topic_lines({"topics": '"a string"'}) == []  # not a list
    assert cli._row_topic_lines({"topics": '["one", "  ", "two"]'}) == ["one", "two"]
    # Already-parsed list (mock DAO shape)
    assert cli._row_topic_lines({"topics": ["x", "y"]}) == ["x", "y"]
