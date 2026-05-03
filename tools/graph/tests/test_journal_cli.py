"""CLI tests for the ergonomic ``graph journal write`` (auto-xfnqh).

Covers the flag-form contract: positional ``<compact>`` plus
``--normal``/``--normal-stdin`` (and optionally ``--expanded``,
``--start``/``--end``/``--since``, ``--type``, ``--link``), relative
timestamp parsing, edge resolution from ``--link``, and the back-compat
``-c -`` JSON-stdin escape hatch.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone, timedelta

import pytest

from tools.graph import cli, ops


# ── shared fixtures ─────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin a fresh on-disk graph.db for each test (host mode, no GRAPH_API)."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _run_cli(argv: list[str], *, stdin: str | None = None) -> tuple[int, str, str]:
    """Drive ``graph`` through ``cli.main``. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    saved_stdin = sys.stdin
    sys.argv = ["graph"] + argv
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
        sys.stdin = saved_stdin
    return rc, out.getvalue(), err.getvalue()


def _only_entry(db_path) -> dict:
    """Return the single journal entry expected in the DB."""
    entries = ops.list_journal_entries()
    assert len(entries) == 1, f"expected 1 journal entry, got {len(entries)}"
    return entries[0]


def _edges_for(db_path, source_id: str) -> list[tuple[str, str]]:
    """Return ``(relation, target_id)`` rows for ``source_id`` from edges."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT relation, target_id FROM edges WHERE source_id = ? "
                "ORDER BY rowid",
                (source_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ── 1. flag-form: --since shortcut ─────────────────────────


def test_flag_form_since_writes_compact_plus_normal(graph_db_env, tmp_path):
    normal_path = tmp_path / "normal.md"
    normal_path.write_text("auth shape decided — passkeys not OAuth")
    rc, out, err = _run_cli([
        "journal", "write",
        "Auth shape decided — passkeys not OAuth",
        "--normal", str(normal_path),
        "--since", "2h",
    ])
    assert rc == 0, err
    assert "Journal entry saved" in out

    entry = _only_entry(graph_db_env)
    assert entry["compact"] == "Auth shape decided — passkeys not OAuth"
    assert entry["normal"] == "auth shape decided — passkeys not OAuth"
    assert entry["expanded"] == ""
    assert entry["entry_type"] == "attention"
    # timestamps are ISO8601 UTC with the trailing Z and roughly 2h apart.
    start = datetime.fromisoformat(entry["timestamp_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(entry["timestamp_end"].replace("Z", "+00:00"))
    delta = end - start
    assert timedelta(minutes=119) <= delta <= timedelta(minutes=121), delta


# ── 2. flag-form: all three zoom levels ─────────────────────


def test_flag_form_with_expanded_writes_three_zoom_levels(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    expanded_path = tmp_path / "e.md"
    normal_path.write_text("normal text")
    expanded_path.write_text("expanded text\nsecond line")
    rc, _, err = _run_cli([
        "journal", "write",
        "Substrate gap closed",
        "--normal", str(normal_path),
        "--expanded", str(expanded_path),
        "--since", "30m",
    ])
    assert rc == 0, err
    entry = _only_entry(graph_db_env)
    assert entry["compact"] == "Substrate gap closed"
    assert entry["normal"] == "normal text"
    assert entry["expanded"] == "expanded text\nsecond line"


# ── 3. flag-form: --link adds an edges[] entry ──────────────


def test_flag_form_link_creates_edge(graph_db_env, tmp_path):
    """``--link <target>:<relation>`` adds a row to the edges table."""
    # Create a target source first so the edge resolves.
    from tools.graph.models import Source
    from tools.graph.db import GraphDB
    target_src = Source(
        type="note", platform="local", project="autonomy",
        title="target", file_path="note:target",
    )
    db = GraphDB(str(graph_db_env), mode="rw")
    try:
        db.insert_source(target_src)
        db.commit()
    finally:
        db.close()

    normal_path = tmp_path / "n.md"
    normal_path.write_text("normal")
    rc, _, err = _run_cli([
        "journal", "write",
        "Linked entry",
        "--normal", str(normal_path),
        "--since", "1h",
        "--link", f"{target_src.id}:fixed_by",
    ])
    assert rc == 0, err

    entry = _only_entry(graph_db_env)
    edges = _edges_for(graph_db_env, entry["id"])
    assert (("fixed_by", target_src.id),) == tuple(edges), edges


def test_flag_form_link_with_turn_parses(graph_db_env, tmp_path):
    """``--link <target>:<relation>:<turn>`` includes turn metadata."""
    from tools.graph.models import Source
    from tools.graph.db import GraphDB
    target_src = Source(
        type="note", platform="local", project="autonomy",
        title="target", file_path="note:t2",
    )
    db = GraphDB(str(graph_db_env), mode="rw")
    try:
        db.insert_source(target_src)
        db.commit()
    finally:
        db.close()

    normal_path = tmp_path / "n.md"
    normal_path.write_text("normal")
    rc, _, err = _run_cli([
        "journal", "write",
        "Linked",
        "--normal", str(normal_path),
        "--since", "1h",
        "--link", f"{target_src.id}:drew_from:42",
    ])
    assert rc == 0, err

    # Confirm metadata.turn = 42.
    conn = sqlite3.connect(str(graph_db_env))
    try:
        rows = conn.execute(
            "SELECT metadata FROM edges WHERE relation = 'drew_from'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    meta = json.loads(rows[0][0])
    assert meta.get("turn") == 42


# ── 4. relative timestamp parsing ───────────────────────────


def test_parse_journal_timestamp_now():
    fixed = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
    assert cli._parse_journal_timestamp("now", now=fixed) == "2026-05-03T12:00:00Z"


@pytest.mark.parametrize(
    "value,delta",
    [("2h", timedelta(hours=2)),
     ("30m", timedelta(minutes=30)),
     ("1d", timedelta(days=1)),
     ("1w", timedelta(weeks=1))],
)
def test_parse_journal_timestamp_relative(value, delta):
    fixed = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
    expected = (fixed - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli._parse_journal_timestamp(value, now=fixed) == expected


def test_parse_journal_timestamp_iso8601_round_trip():
    """Plain ISO8601 input passes through after a tz-normalisation round-trip."""
    fixed = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
    result = cli._parse_journal_timestamp("2026-05-03T04:00:00Z", now=fixed)
    assert result == "2026-05-03T04:00:00Z"


def test_parse_journal_timestamp_invalid_raises():
    with pytest.raises(ValueError, match="invalid timestamp"):
        cli._parse_journal_timestamp("not-a-time")


# ── 5. flag-form: --start / --end explicit ──────────────────


def test_flag_form_explicit_start_and_end(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("normal")
    rc, _, err = _run_cli([
        "journal", "write",
        "Explicit window",
        "--normal", str(normal_path),
        "--start", "2026-05-03T04:00:00Z",
        "--end", "2026-05-03T05:00:00Z",
    ])
    assert rc == 0, err
    entry = _only_entry(graph_db_env)
    assert entry["timestamp_start"] == "2026-05-03T04:00:00Z"
    assert entry["timestamp_end"] == "2026-05-03T05:00:00Z"


# ── 6. flag-form: --normal-stdin ────────────────────────────


def test_flag_form_normal_stdin_reads_from_stdin(graph_db_env):
    rc, _, err = _run_cli(
        ["journal", "write", "From stdin", "--normal-stdin", "--since", "1h"],
        stdin="streamed normal body\nline two",
    )
    assert rc == 0, err
    entry = _only_entry(graph_db_env)
    assert entry["normal"] == "streamed normal body\nline two"


def test_flag_form_expanded_stdin_reads_from_stdin(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli(
        ["journal", "write", "Expanded via stdin",
         "--normal", str(normal_path),
         "--expanded-stdin", "--since", "1h"],
        stdin="long expanded text\nparagraph 2",
    )
    assert rc == 0, err
    entry = _only_entry(graph_db_env)
    assert entry["expanded"] == "long expanded text\nparagraph 2"


# ── 7. flag-form: --type override ───────────────────────────


def test_flag_form_type_override(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli([
        "journal", "write", "Decision",
        "--normal", str(normal_path),
        "--type", "decision",
        "--since", "1h",
    ])
    assert rc == 0, err
    entry = _only_entry(graph_db_env)
    assert entry["entry_type"] == "decision"


# ── 8. flag-form: defaults to --since 1h ────────────────────


def test_flag_form_no_window_defaults_to_one_hour(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli([
        "journal", "write", "Default window",
        "--normal", str(normal_path),
    ])
    assert rc == 0, err
    entry = _only_entry(graph_db_env)
    start = datetime.fromisoformat(entry["timestamp_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(entry["timestamp_end"].replace("Z", "+00:00"))
    delta = end - start
    assert timedelta(minutes=59) <= delta <= timedelta(minutes=61), delta


# ── 9. flag-form: validation errors ─────────────────────────


def test_flag_form_missing_normal_errors(graph_db_env):
    rc, _, err = _run_cli([
        "journal", "write", "No normal",
        "--since", "1h",
    ])
    assert rc != 0
    assert "normal" in err.lower()


def test_flag_form_missing_compact_errors(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli([
        "journal", "write",
        "--normal", str(normal_path),
        "--since", "1h",
    ])
    assert rc != 0
    assert "compact" in err.lower() or "headline" in err.lower()


def test_flag_form_normal_and_normal_stdin_mutually_exclusive(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli(
        ["journal", "write", "x", "--normal", str(normal_path),
         "--normal-stdin", "--since", "1h"],
        stdin="from stdin",
    )
    assert rc != 0
    assert "normal" in err.lower()


def test_flag_form_since_and_start_mutually_exclusive(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli([
        "journal", "write", "x", "--normal", str(normal_path),
        "--since", "1h",
        "--start", "2026-05-03T00:00:00Z",
    ])
    assert rc != 0
    assert "since" in err.lower() or "start" in err.lower()


def test_flag_form_invalid_link_errors(graph_db_env, tmp_path):
    normal_path = tmp_path / "n.md"
    normal_path.write_text("n")
    rc, _, err = _run_cli([
        "journal", "write", "x", "--normal", str(normal_path),
        "--since", "1h",
        "--link", "no-relation",  # no colon
    ])
    assert rc != 0
    assert "link" in err.lower()


# ── 10. back-compat: JSON-stdin path still works ────────────


def test_json_stdin_back_compat_still_writes(graph_db_env):
    payload = {
        "compact": "JSON path still works",
        "normal": "back-compat normal",
        "expanded": "back-compat expanded",
        "timestamp_start": "2026-05-03T04:00:00Z",
        "timestamp_end": "2026-05-03T05:00:00Z",
        "entry_type": "attention",
    }
    rc, out, err = _run_cli(
        ["journal", "write", "-c", "-"],
        stdin=json.dumps(payload),
    )
    assert rc == 0, err
    assert "Journal entry saved" in out

    entry = _only_entry(graph_db_env)
    assert entry["compact"] == "JSON path still works"
    assert entry["normal"] == "back-compat normal"
    assert entry["expanded"] == "back-compat expanded"
    assert entry["timestamp_start"] == "2026-05-03T04:00:00Z"
    assert entry["timestamp_end"] == "2026-05-03T05:00:00Z"


def test_json_stdin_back_compat_missing_field_errors(graph_db_env):
    payload = {"compact": "x", "normal": "y"}  # missing timestamps
    rc, _, err = _run_cli(
        ["journal", "write", "-c", "-"],
        stdin=json.dumps(payload),
    )
    assert rc != 0
    assert "missing required field" in err.lower()


def test_json_stdin_back_compat_invalid_json_errors(graph_db_env):
    rc, _, err = _run_cli(
        ["journal", "write", "-c", "-"],
        stdin="{not json",
    )
    assert rc != 0
    assert "invalid json" in err.lower()


# ── 11. --link parser unit checks ───────────────────────────


def test_parse_link_two_parts():
    assert cli._parse_journal_link("auto-xxx:fixed_by") == {
        "target": "auto-xxx", "relation": "fixed_by",
    }


def test_parse_link_three_parts_with_turn():
    assert cli._parse_journal_link("src-id:drew_from:7") == {
        "target": "src-id", "relation": "drew_from", "turn": 7,
    }


@pytest.mark.parametrize("bad", ["", ":relation", "target:", "no-colon", "a:b:not-an-int"])
def test_parse_link_invalid_raises(bad):
    with pytest.raises(ValueError):
        cli._parse_journal_link(bad)
