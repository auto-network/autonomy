"""Structural removal assertions for transcript/monitor correction delivery.

Bead auto-hmow2 deleted every stdout/transcript correction-delivery path. These
narrow checks fail loudly if any of it is reintroduced: the parser must not
upconvert a ``turn_correction`` transcript entry, ``SessionMonitor`` must not
persist corrections, and the DAO must not recreate the resolution-attempt cache.
"""

from __future__ import annotations

import json
import sqlite3

from tools.dashboard import session_harness, session_monitor
from tools.dashboard.dao import dashboard_db


def test_harness_has_no_turn_correction_upconverters():
    for name in (
        "_upconvert_turn_correction",
        "_upconvert_turn_correction_command",
        "_build_turn_correction_entry",
    ):
        assert not hasattr(session_harness, name), (
            f"session_harness.{name} was removed by auto-hmow2 and must not return"
        )


def test_parser_does_not_emit_turn_correction_entry():
    """Feeding the old ``graph turn-correction suggest`` JSON through the Claude
    parser must NOT produce a synthetic ``turn_correction`` entry any more."""
    payload = json.dumps({"type": "turn_correction", "version": 2,
                          "corrected_text": "JSON encoded message"})
    line = json.dumps({
        "type": "user",
        "uuid": "u-1",
        "message": {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": payload,
        }]},
        "timestamp": "2026-08-10T12:00:00Z",
    })
    parsed = session_harness.parse_claude_log_line(line)
    entries = parsed if isinstance(parsed, list) else [parsed] if parsed else []
    assert all((e or {}).get("type") != "turn_correction" for e in entries), (
        "parser upconverted a turn_correction transcript entry — delivery path "
        "must be API-only"
    )


def test_session_monitor_has_no_persist_turn_corrections():
    assert not hasattr(session_monitor.SessionMonitor, "_persist_turn_corrections")
    for name in (
        "_persist_turn_corrections",
        "_seed_recent_user_turns_from_jsonl",
        "_user_turns_from_jsonl_tail",
        "_turn_correction_metrics",
    ):
        assert not hasattr(session_monitor, name), (
            f"session_monitor.{name} was correction-delivery machinery and is gone"
        )


def test_dao_has_no_correction_attempt_helpers():
    for name in ("correction_attempt_seen", "mark_correction_attempt"):
        assert not hasattr(dashboard_db, name), (
            f"dashboard_db.{name} backed the monitor resolution cache and is gone"
        )


def test_fresh_db_does_not_create_turn_correction_attempts_table(tmp_path, monkeypatch):
    """A brand-new dashboard.db must NOT contain the inert attempts table."""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    try:
        db_mod.get_conn()  # triggers schema creation
        conn = sqlite3.connect(str(db_path))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "turn_corrections" in names  # still created — authoritative store
        assert "turn_correction_attempts" not in names, (
            "fresh databases must no longer create the attempts cache table"
        )
    finally:
        importlib.reload(db_mod)
