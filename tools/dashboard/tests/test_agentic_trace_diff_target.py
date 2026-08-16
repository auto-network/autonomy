"""Typed diff-target resolution for completed agentic Trace pages."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_active_agentic_card_joins_monitored_provider_and_stats(monkeypatch):
    from tools.dashboard import server

    run = {
        "id": "agentic-fix-card-abcd",
        "bead_id": "",
        "kind": "agentic",
        "agentic_source_id": "source-123",
        "container_name": "agentic-fix-card-abcd",
        "title": "Fix the card",
        "started_at": "2026-08-16 22:00:00Z",
        "last_activity": "2026-08-16 22:01:00Z",
        "cpu_pct": 27.4,
        "mem_mb": 612,
        "token_count": 18420,
        "tool_count": 31,
        "turn_count": 14,
    }
    monkeypatch.setattr(server.dao_beads, "get_dispatch_beads", lambda: {
        "approved_waiting": [], "approved_blocked": [],
    })
    monkeypatch.setattr(server.dao_dispatch, "get_running_with_stats", lambda: [run])
    monkeypatch.setattr(server.dao_beads, "get_bead_title_priority", lambda _ids: {})
    monkeypatch.setattr(server.dashboard_db, "get_session", lambda _run: {
        "harness": "codex", "model": "gpt-5.6-sol",
    })
    monkeypatch.setattr(server, "_resolve_agentic_identity", lambda _source: {
        "action_label": "Implement to Branch",
        "member_key": "bead.implement-to-branch",
        "target_kind": "bead",
        "target_source_id": "auto-card",
        "target_org": "autonomy",
        "dispatched_by_session": "dashboard",
        "harness": None,
        "model": None,
        "title": "Fix the card",
    })
    monkeypatch.setattr(server, "_get_pause_state", lambda: {})
    monkeypatch.setattr(server, "_get_pause_reasons", lambda: {})

    payload = await server._collect_dispatch_data()
    card = payload["active"][0]

    assert card["harness"] == "codex"
    assert card["model"] == "gpt-5.6-sol"
    assert card["cpu_pct"] == 27.4
    assert card["mem_mb"] == 612
    assert card["turn_count"] == 14


def test_trace_prefers_retained_worktree(monkeypatch, tmp_path):
    from tools.dashboard import server

    row = SimpleNamespace(
        session_name="agentic-fix-card-abcd",
        repo_name="autonomy",
        is_dirty=True,
        commits_ahead=1,
    )
    monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: [row])
    monkeypatch.setattr(server, "WORKTREES_DIR", tmp_path)

    target = server._agentic_trace_diff_target("agentic-fix-card-abcd")

    assert target == {
        "kind": "worktree",
        "session_name": "agentic-fix-card-abcd",
        "href": "/worktrees?session=agentic-fix-card-abcd",
        "repo_count": 1,
    }


def test_trace_uses_merge_commit_after_worktree_is_clean(monkeypatch, tmp_path):
    from tools.dashboard import server

    row = SimpleNamespace(
        session_name="agentic-fix-card-abcd",
        repo_name="autonomy",
        is_dirty=False,
        commits_ahead=0,
    )
    monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: [row])
    monkeypatch.setattr(server, "WORKTREES_DIR", tmp_path)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE dispatch_runs (id TEXT, kind TEXT, container_name TEXT, "
        "commit_hash TEXT, branch TEXT, branch_base TEXT, completed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO dispatch_runs VALUES (?, 'worktree-merge', ?, ?, ?, ?, ?)",
        (
            "wt-123456789abc",
            "agentic-fix-card-abcd",
            "123456789abcdef0",
            "session/agentic-fix-card-abcd",
            "master",
            "2026-08-16 22:00:00",
        ),
    )
    conn.commit()
    monkeypatch.setattr(server, "_timeline_conn", lambda: conn)

    target = server._agentic_trace_diff_target("agentic-fix-card-abcd")

    assert target == {
        "kind": "commit",
        "run_id": "wt-123456789abc",
        "commit_hash": "123456789abcdef0",
        "branch": "session/agentic-fix-card-abcd",
        "branch_base": "master",
    }


def test_trace_uses_deterministic_deeplink_during_monitor_cold_start(
    monkeypatch, tmp_path,
):
    from tools.dashboard import server

    run = "agentic-name with spaces"
    (tmp_path / run).mkdir()
    monkeypatch.setattr(server.worktree_monitor, "get_all", lambda: [])
    monkeypatch.setattr(server, "WORKTREES_DIR", tmp_path)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE dispatch_runs (id TEXT, kind TEXT, container_name TEXT, "
        "commit_hash TEXT, branch TEXT, branch_base TEXT, completed_at TEXT)"
    )
    monkeypatch.setattr(server, "_timeline_conn", lambda: conn)

    target = server._agentic_trace_diff_target(run)

    assert target["kind"] == "worktree"
    assert target["href"] == "/worktrees?session=agentic-name%20with%20spaces"


def test_trace_and_dispatch_templates_reuse_existing_overlays_and_badge():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    trace_template = (
        root / "tools/dashboard/templates/pages/trace.html"
    ).read_text()
    trace_js = (
        root / "tools/dashboard/static/js/pages/trace.js"
    ).read_text()
    dispatch_card = (
        root / "tools/dashboard/templates/partials/bead-card.html"
    ).read_text()

    assert 'data-testid="trace-view-diffs"' in trace_template
    assert "openWorktreeReviewOverlay" in trace_js
    assert "openCommitOverlay" in trace_js
    assert "session-harness-badge.html" in dispatch_card
    assert "flex flex-wrap items-center" in dispatch_card
