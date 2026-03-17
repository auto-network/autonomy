"""Tests for the /api/timeline and /api/timeline/stats endpoints."""

import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import agents.dispatch_db as db

# Must be importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.dashboard.server import (
    _build_timeline_where,
    _parse_range,
    _row_to_timeline_entry,
)


def _use_temp_db():
    """Point dispatch_db at a temp file, init, and return path."""
    tmp = tempfile.mktemp(suffix=".db")
    db.DB_PATH = Path(tmp)
    db.init_db()
    return tmp


def _insert_test_rows(tmp: str):
    """Insert a few test rows for timeline queries."""
    conn = sqlite3.connect(tmp)
    conn.execute(db.CREATE_TABLE)
    rows = [
        ("run-1", "auto-abc", "2026-03-17 10:00:00", "2026-03-17 10:05:00", 300,
         "DONE", "Completed successfully", None, "abc123", "Fix the bug", None, None,
         None, None, None, 10, 5, 3, 4, 5, 4, 20, 60, 15, 5, 1, 1, None),
        ("run-2", "auto-def", "2026-03-17 11:00:00", "2026-03-17 11:10:00", 600,
         "FAILED", "Tests failed", "code", "def456", "Add feature", None, None,
         None, None, None, 50, 10, 8, 2, 3, 2, None, None, None, None, 0, 0, None),
        ("run-3", "proj-xyz", "2026-03-10 08:00:00", "2026-03-10 08:30:00", 1800,
         "DONE", "All good", None, "xyz789", "Refactor module", None, None,
         None, None, None, 20, 8, 5, None, None, None, None, None, None, None, 2, 1, None),
    ]
    for r in rows:
        conn.execute(
            """INSERT OR REPLACE INTO dispatch_runs (
                id, bead_id, started_at, completed_at, duration_secs,
                status, reason, failure_category, commit_hash, commit_message,
                branch, branch_base, image, container_name, exit_code,
                lines_added, lines_removed, files_changed,
                score_tooling, score_clarity, score_confidence,
                time_research_pct, time_coding_pct, time_debugging_pct, time_tooling_pct,
                discovered_beads_count, has_experience_report, output_dir
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            r,
        )
    conn.commit()
    conn.close()


def test_parse_range_common():
    cutoff = _parse_range("1d")
    assert cutoff is not None
    assert len(cutoff) == 19  # "YYYY-MM-DD HH:MM:SS"


def test_parse_range_all():
    assert _parse_range("all") is None
    assert _parse_range("") is None
    assert _parse_range(None) is None


def test_parse_range_custom():
    cutoff = _parse_range("2h")
    assert cutoff is not None
    cutoff = _parse_range("15d")
    assert cutoff is not None


def test_parse_range_invalid():
    assert _parse_range("xyz") is None
    assert _parse_range("1x") is None


def test_build_where_no_filters():
    where, params = _build_timeline_where(None, None, None)
    assert where == "1=1"
    assert params == []


def test_build_where_range_only():
    where, params = _build_timeline_where("1d", None, None)
    assert "completed_at >= ?" in where
    assert len(params) == 1


def test_build_where_project_filter():
    where, params = _build_timeline_where(None, "auto", None)
    assert "bead_id LIKE ?" in where
    assert "image LIKE ?" in where
    assert params[0] == "auto%"
    assert params[1] == "%auto%"


def test_build_where_text_search():
    where, params = _build_timeline_where(None, None, "fix bug")
    # Two terms, each with 3 LIKE params
    assert len(params) == 6
    assert "%fix%" in params
    assert "%bug%" in params


def test_build_where_all_filters():
    where, params = _build_timeline_where("1d", "auto", "fix")
    assert "completed_at >= ?" in where
    assert "bead_id LIKE ?" in where
    assert len(params) == 1 + 2 + 3  # range + project + 1 search term


def test_row_to_timeline_entry():
    """Test conversion of a sqlite3.Row to timeline entry dict."""
    tmp = _use_temp_db()
    _insert_test_rows(tmp)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatch_runs WHERE id = 'run-1'").fetchone()
    conn.close()

    entry = _row_to_timeline_entry(row)
    assert entry["bead_id"] == "auto-abc"
    assert entry["status"] == "DONE"
    assert entry["duration_secs"] == 300
    assert entry["lines_added"] == 10
    assert entry["scores"] == {"tooling": 4, "clarity": 5, "confidence": 4}
    assert entry["time_breakdown"]["research_pct"] == 20
    assert entry["has_experience_report"] is True
    assert entry["failure_category"] is None


def test_row_to_timeline_entry_null_scores():
    """NULL scores should result in None for scores dict."""
    tmp = _use_temp_db()
    _insert_test_rows(tmp)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatch_runs WHERE id = 'run-3'").fetchone()
    conn.close()

    entry = _row_to_timeline_entry(row)
    assert entry["scores"] is None
    assert entry["time_breakdown"] is None


def test_timeline_query_with_project_filter():
    """Full SQL query with project filter returns correct rows."""
    tmp = _use_temp_db()
    _insert_test_rows(tmp)

    where, params = _build_timeline_where(None, "auto", None)
    sql = f"SELECT * FROM dispatch_runs WHERE {where} ORDER BY completed_at DESC"

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    # Should match auto-abc and auto-def, not proj-xyz
    bead_ids = [r["bead_id"] for r in rows]
    assert "auto-abc" in bead_ids
    assert "auto-def" in bead_ids
    assert "proj-xyz" not in bead_ids


def test_stats_query():
    """Stats aggregation query returns correct values."""
    tmp = _use_temp_db()
    _insert_test_rows(tmp)

    where, params = _build_timeline_where(None, None, None)
    sql = f"""
        SELECT
            COUNT(*) as total_count,
            COUNT(CASE WHEN status = 'DONE' THEN 1 END) as completed_count,
            COUNT(CASE WHEN status = 'FAILED' THEN 1 END) as failed_count,
            COUNT(CASE WHEN status = 'BLOCKED' THEN 1 END) as blocked_count,
            AVG(duration_secs) as avg_duration,
            AVG(score_tooling) as avg_tooling_score,
            AVG(score_confidence) as avg_confidence_score
        FROM dispatch_runs WHERE {where}
    """

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()

    assert row["total_count"] == 3
    assert row["completed_count"] == 2
    assert row["failed_count"] == 1
    assert row["blocked_count"] == 0
    # AVG ignores NULLs: (300+600+1800)/3 = 900
    assert row["avg_duration"] == 900.0
    # avg_tooling: (4+2)/2 = 3.0 (NULL excluded)
    assert row["avg_tooling_score"] == 3.0
