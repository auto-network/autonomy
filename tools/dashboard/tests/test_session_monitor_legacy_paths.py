"""auto-nsu0e: sessions whose rows carry a pre-cutover agent-runs path.

Rows written before the Compose cutover store the HOST prefix
(/home/…/data/agent-runs/<session>/sessions). Inside the container that path
does not exist, so the monitor could never watch those sessions and logged an
ENOENT on every startup and rescan. The monitor now re-roots such paths by the
``/agent-runs/`` anchor onto the local agent-runs directory, persists the
rewrite, and skips (once, throttled) directories that are absent here.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

import pytest

from tools.dashboard.tests.test_session_monitor_discovery import (  # noqa: F401
    _db_row,
    _make_monitor,
    setup_env,
)

LEGACY_ROOT = "/home/someone/workspace/autonomy/data/agent-runs"


def _insert_live_row(db_path, tmux_name, *, jsonl_path, resolution_dir, type_="container"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions (tmux_name, type, project, jsonl_path, created_at, "
        "is_live, resolution_dir) VALUES (?, ?, 'autonomy-developer', ?, ?, 1, ?)",
        (tmux_name, type_, jsonl_path, time.time(), resolution_dir),
    )
    conn.commit()
    conn.close()


def test_local_agent_runs_path_rehomes_only_when_needed(setup_env):
    tmp_path, _db, agent_runs = setup_env
    from tools.dashboard import session_monitor as sm
    sess = agent_runs / "auto-0812-1-20260813-011340" / "sessions"
    sess.mkdir(parents=True)
    (sess / "a.jsonl").write_text("{}\n")

    legacy = f"{LEGACY_ROOT}/auto-0812-1-20260813-011340/sessions/a.jsonl"
    assert sm._local_agent_runs_path(legacy) == str(sess / "a.jsonl")
    assert sm._local_agent_runs_path(f"{LEGACY_ROOT}/auto-0812-1-20260813-011340/sessions") == str(sess)
    # An existing stored path is kept even if it also carries the anchor.
    assert sm._local_agent_runs_path(str(sess / "a.jsonl")) == str(sess / "a.jsonl")
    # No anchor: host .claude transcripts pass through.
    assert sm._local_agent_runs_path("/home/x/.claude/projects/p/s.jsonl") == "/home/x/.claude/projects/p/s.jsonl"
    # Anchor but nothing here either: unchanged (still absent, still honest).
    gone = f"{LEGACY_ROOT}/auto-never/sessions/z.jsonl"
    assert sm._local_agent_runs_path(gone) == gone
    assert sm._local_agent_runs_path(None) is None


def test_absent_directory_is_skipped_once_not_enoent_every_time(setup_env, caplog):
    tmp_path, _db, agent_runs = setup_env
    from tools.dashboard import session_monitor as sm
    sm._watch_gaps.forget(("dir", str(agent_runs / "auto-missing" / "sessions")))
    mon = _make_monitor(setup_env)
    absent = str(agent_runs / "auto-missing" / "sessions")
    with caplog.at_level(logging.WARNING, logger=sm.logger.name):
        for _ in range(5):
            mon._add_dir_watch("auto-missing", absent)      # startup + rescans
    lines = [r.getMessage() for r in caplog.records if "auto-missing" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "session directory absent, watch skipped" in lines[0]
    assert not any("add_watch CREATE failed" in r.getMessage() for r in caplog.records)
    assert absent not in mon._dir_path_to_wd
    # The directory appears: the next attempt arms the watch.
    (agent_runs / "auto-missing" / "sessions").mkdir(parents=True)
    mon._add_dir_watch("auto-missing", absent)
    assert absent in mon._dir_path_to_wd


def test_startup_recovery_rehomes_legacy_rows_and_watches_them(setup_env, caplog):
    tmp_path, db_path, agent_runs = setup_env
    from tools.dashboard import session_monitor as sm
    name = "auto-0820-141006"
    run_dir = agent_runs / f"{name}-20260820-181008"
    sess = run_dir / "sessions"
    sess.mkdir(parents=True)
    jsonl = sess / "u1.jsonl"
    jsonl.write_text('{"type":"human","message":{"role":"user","content":[{"type":"text","text":"hi"}]},"timestamp":"2026-08-20T18:10:08Z"}\n')
    _insert_live_row(
        db_path, name,
        jsonl_path=f"{LEGACY_ROOT}/{name}-20260820-181008/sessions/u1.jsonl",
        resolution_dir=f"{LEGACY_ROOT}/{name}-20260820-181008/sessions",
    )
    # The re-homing happens at the FIRST sight of the row: inotify init
    # (get_tailable_sessions) already rewrites it before startup recovery.
    with caplog.at_level(logging.INFO, logger=sm.logger.name):
        mon = _make_monitor(setup_env)
        asyncio.run(mon._recover_unresolved_sessions())

    row = _db_row(db_path, name)
    assert row["jsonl_path"] == str(jsonl)
    assert row["resolution_dir"] == str(sess)
    assert str(sess) in mon._dir_path_to_wd, "the re-homed directory must be watched"
    assert any("re-homed pre-cutover paths" in r.getMessage() for r in caplog.records)
    assert not any("add_watch CREATE failed" in r.getMessage() for r in caplog.records)


def test_rows_already_local_are_left_alone(setup_env):
    tmp_path, db_path, agent_runs = setup_env
    name = "auto-0907-000001"
    sess = agent_runs / f"{name}-20260907-000001" / "sessions"
    sess.mkdir(parents=True)
    jsonl = sess / "u2.jsonl"
    jsonl.write_text("{}\n")
    _insert_live_row(db_path, name, jsonl_path=str(jsonl), resolution_dir=str(sess))
    mon = _make_monitor(setup_env)
    row = dict(_db_row(db_path, name))
    assert mon._rehome_row_paths(row) is False
    assert dict(_db_row(db_path, name))["jsonl_path"] == str(jsonl)
