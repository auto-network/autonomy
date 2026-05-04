"""Targeted launch nag for dashboard-approved beads + snappier launch broadcast.

Bead: auto-rh2r5.

Two coupled pieces of behaviour:

* ``api_bead_approve`` — when a bead is approved through the dashboard
  endpoint, ping the authoring session (and only the authoring session)
  via CrossTalk if it's still alive. The CLI approval path
  (``cmd_dispatch_approve``) is intentionally exempt — the dashboard-only
  rule is the proxy for "a human did this."

* ``insert_launch_run`` — when the dispatcher writes a RUNNING row, fire
  ``event_bus.broadcast("dispatch", ...)`` immediately so the dashboard's
  running indicator reacts in <1s instead of waiting on the 5s safety-net
  poll cadence.

Acceptance criteria, paraphrased from the bead:
  1. live ``terminal:<session>`` author → exactly one CrossTalk send
  2. dead ``terminal:<session>`` author → no send
  3. non-terminal author (e.g. e-mail) → no send
  4. CLI approval path → no send (proves dashboard-only attribution)
  5. ``insert_launch_run`` fires ``event_bus.broadcast("dispatch", ...)``
     within 100ms of the insert
"""

from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


# ── Shared scaffolding ────────────────────────────────────────────────


_TMUX_SESSIONS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tmux_sessions (
    tmux_name TEXT PRIMARY KEY,
    session_uuid TEXT,
    graph_source_id TEXT,
    type TEXT NOT NULL,
    project TEXT NOT NULL,
    jsonl_path TEXT,
    bead_id TEXT,
    created_at REAL NOT NULL,
    is_live INTEGER DEFAULT 1,
    file_offset INTEGER DEFAULT 0,
    last_activity REAL,
    last_message TEXT DEFAULT '',
    entry_count INTEGER DEFAULT 0,
    context_tokens INTEGER DEFAULT 0,
    label TEXT DEFAULT '',
    topics TEXT DEFAULT '[]',
    role TEXT DEFAULT '',
    nag_enabled INTEGER DEFAULT 0,
    nag_interval INTEGER DEFAULT 15,
    nag_message TEXT DEFAULT '',
    nag_last_sent REAL DEFAULT 0,
    dispatch_nag INTEGER DEFAULT 0,
    resolution_dir TEXT,
    session_uuids TEXT DEFAULT '[]',
    curr_jsonl_file TEXT,
    activity_state TEXT DEFAULT 'idle'
)
"""


def _init_dashboard_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(_TMUX_SESSIONS_SCHEMA)
    conn.commit()
    conn.close()


def _insert_session(
    db_path: Path, tmux_name: str, *, is_live: bool = True,
    session_type: str = "dispatch",
) -> None:
    """Insert a tmux_sessions row.

    Defaults to ``type='dispatch'`` because session_monitor's death sweep
    skips dispatch/librarian/agentic types — those are owned by the
    dispatcher's poll loop, not by tmux. A ``container`` row would be
    flipped to ``is_live=0`` shortly after the lifespan starts, since
    the test environment has no live tmux server.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, created_at, is_live)"
        " VALUES (?, ?, 'autonomy', ?, ?)",
        (tmux_name, session_type, time.time(), 1 if is_live else 0),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def dashboard_env(tmp_path, monkeypatch):
    """Fresh DASHBOARD_DB + reloaded server module for each test.

    Reloading server.py picks up the env-redirected DAO connection.
    The TestClient lifespan runs against this isolated DB so writes
    never touch ``data/dashboard.db``.
    """
    db_path = tmp_path / "dashboard.db"
    _init_dashboard_db(db_path)
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    from tools.dashboard.dao import dashboard_db as ddb_mod
    importlib.reload(ddb_mod)
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    return tmp_path, db_path, server_mod


def _patch_bd_set_state_ok(monkeypatch, server_mod) -> None:
    """Make ``bd set-state`` succeed without spawning a subprocess."""
    async def fake_run_cli(cmd, timeout=30, stdin_data=None):
        if cmd and cmd[0] == "bd" and len(cmd) > 1 and cmd[1] == "set-state":
            return ("ok\n", "", 0)
        raise AssertionError(
            f"unexpected run_cli during approval test: {cmd!r}"
        )

    monkeypatch.setattr(server_mod, "run_cli", fake_run_cli)


def _patch_bead_lookup(
    monkeypatch, server_mod, *, bead_id: str, title: str, created_by: str | None,
) -> None:
    bead = {
        "id": bead_id,
        "title": title,
        "created_by": created_by,
    }

    def fake_get_bead(bid: str):
        assert bid == bead_id, f"unexpected bead id: {bid!r}"
        return bead

    monkeypatch.setattr(server_mod.dao_beads, "get_bead", fake_get_bead)


def _capture_tmux_send(monkeypatch, server_mod) -> list[tuple[str, str]]:
    """Replace ``server.tmux_send`` with a recorder; return the buffer."""
    sent: list[tuple[str, str]] = []

    async def fake_tmux_send(target, payload):
        sent.append((target, payload))

    monkeypatch.setattr(server_mod, "tmux_send", fake_tmux_send)
    return sent


# ══════════════════════════════════════════════════════════════════════
# 1. Positive case — live terminal author gets exactly one CrossTalk
# ══════════════════════════════════════════════════════════════════════


def test_dashboard_approve_pings_live_authoring_session(dashboard_env, monkeypatch):
    """A bead authored by ``terminal:<id>`` whose session is live receives
    exactly one CrossTalk envelope mentioning the bead's id and title."""
    _, db_path, server_mod = dashboard_env
    _insert_session(db_path, "test-session-id", is_live=True)

    _patch_bd_set_state_ok(monkeypatch, server_mod)
    _patch_bead_lookup(
        monkeypatch, server_mod,
        bead_id="auto-test-1",
        title="Targeted nag",
        created_by="terminal:test-session-id",
    )
    sent = _capture_tmux_send(monkeypatch, server_mod)

    with TestClient(server_mod.app) as client:
        r = client.post("/api/bead/auto-test-1/approve")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "bead_id": "auto-test-1"}

    assert len(sent) == 1, f"expected exactly one CrossTalk send, got {sent!r}"
    target, envelope = sent[0]
    assert target == "test-session-id"
    assert "auto-test-1" in envelope
    assert "Targeted nag" in envelope
    assert "approved for dispatch via dashboard" in envelope
    # The ``from`` attribute is the synthetic dashboard system identity —
    # there is no authenticated session on this code path.
    assert 'from="dashboard"' in envelope


# ══════════════════════════════════════════════════════════════════════
# 2. Author session not live — no send
# ══════════════════════════════════════════════════════════════════════


def test_dashboard_approve_skips_dead_authoring_session(dashboard_env, monkeypatch):
    _, db_path, server_mod = dashboard_env
    _insert_session(db_path, "test-session-id", is_live=False)

    _patch_bd_set_state_ok(monkeypatch, server_mod)
    _patch_bead_lookup(
        monkeypatch, server_mod,
        bead_id="auto-test-2",
        title="Dead author",
        created_by="terminal:test-session-id",
    )
    sent = _capture_tmux_send(monkeypatch, server_mod)

    with TestClient(server_mod.app) as client:
        r = client.post("/api/bead/auto-test-2/approve")
        assert r.status_code == 200, r.text

    assert sent == [], (
        f"no CrossTalk should fire when the author session is dead; got {sent!r}"
    )


def test_dashboard_approve_skips_when_session_row_missing(dashboard_env, monkeypatch):
    """A row that simply doesn't exist behaves the same as is_live=0 —
    nothing to ping, no send.
    """
    _, _, server_mod = dashboard_env
    # No session row inserted — author session is unknown to the dashboard.

    _patch_bd_set_state_ok(monkeypatch, server_mod)
    _patch_bead_lookup(
        monkeypatch, server_mod,
        bead_id="auto-test-2b",
        title="Unknown author session",
        created_by="terminal:never-registered",
    )
    sent = _capture_tmux_send(monkeypatch, server_mod)

    with TestClient(server_mod.app) as client:
        r = client.post("/api/bead/auto-test-2b/approve")
        assert r.status_code == 200, r.text

    assert sent == []


# ══════════════════════════════════════════════════════════════════════
# 3. Author is not a terminal session — no send
# ══════════════════════════════════════════════════════════════════════


def test_dashboard_approve_skips_non_terminal_author(dashboard_env, monkeypatch):
    """``created_by`` that does not start with ``terminal:`` is opaque to
    the targeting rule — no session id to ping, no send."""
    _, db_path, server_mod = dashboard_env
    # Even if a session named "alice@example.com" somehow existed, the
    # rule would refuse to extract a session id from a non-prefixed
    # creator field.
    _insert_session(db_path, "alice@example.com", is_live=True)

    _patch_bd_set_state_ok(monkeypatch, server_mod)
    _patch_bead_lookup(
        monkeypatch, server_mod,
        bead_id="auto-test-3",
        title="Email author",
        created_by="alice@example.com",
    )
    sent = _capture_tmux_send(monkeypatch, server_mod)

    with TestClient(server_mod.app) as client:
        r = client.post("/api/bead/auto-test-3/approve")
        assert r.status_code == 200, r.text

    assert sent == []


# ══════════════════════════════════════════════════════════════════════
# 4. CLI approval path — never fires the targeted nag
# ══════════════════════════════════════════════════════════════════════


def test_cli_approve_does_not_fire_targeted_nag(dashboard_env, monkeypatch):
    """``cmd_dispatch_approve`` is the CLI path; even when the authoring
    session is live, the dashboard-only attribution rule must hold —
    no CrossTalk. Proves the targeting is keyed on entry point, not
    on the existence of a live author."""
    _, db_path, server_mod = dashboard_env
    _insert_session(db_path, "test-session-id", is_live=True)

    # Wire the same author / live-session conditions that pass test #1.
    _patch_bead_lookup(
        monkeypatch, server_mod,
        bead_id="auto-cli-1",
        title="CLI approval",
        created_by="terminal:test-session-id",
    )
    sent = _capture_tmux_send(monkeypatch, server_mod)

    # Stub out ``bd`` so the CLI command runs without the real binary.
    import subprocess

    class _CompletedProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""

    def fake_subprocess_run(cmd, *args, **kwargs):
        assert cmd[:2] == ["bd", "set-state"], f"unexpected: {cmd!r}"
        return _CompletedProc()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    from tools.graph import dispatch_cmd
    args = type("A", (), {"bead_ids": ["auto-cli-1"]})()
    dispatch_cmd.cmd_dispatch_approve(args)

    assert sent == [], (
        f"CLI approval must NOT trigger the dashboard-only nag; got {sent!r}"
    )


# ══════════════════════════════════════════════════════════════════════
# 5. Snappier launch broadcast — insert_launch_run fires <100ms after insert
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def isolated_dispatch_db(tmp_path, monkeypatch):
    """Fresh DISPATCH_DB and a reloaded writer module.

    The dispatcher's launch path opens a new connection per call, so a
    plain env override + module reload is enough for isolation.
    """
    monkeypatch.setenv("DISPATCH_DB", str(tmp_path / "dispatch.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    writer_mod.init_db()
    return writer_mod


class _SpyEventBus:
    """Drop-in replacement for ``event_bus`` that records broadcasts.

    Implements only ``broadcast_sync`` because that is all the launch
    path uses; calls are stored with a wall-clock timestamp so the test
    can assert the broadcast happened within the 100ms budget.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def broadcast_sync(self, topic, data, dedup=True):
        self.calls.append({
            "topic": topic,
            "data": data,
            "dedup": dedup,
            "ts": time.monotonic(),
        })
        return 1


def test_insert_launch_run_broadcasts_dispatch_immediately(
    isolated_dispatch_db, monkeypatch,
):
    """The launch path emits ``dispatch`` on the EventBus within 100ms of
    the row insert — so the dashboard's running indicator reacts in <1s
    instead of waiting on the 5s safety-net poll."""
    spy = _SpyEventBus()

    # Patch the singleton in tools.dashboard.event_bus — the writer's
    # broadcast helper imports it lazily, so a runtime patch is enough.
    import tools.dashboard.event_bus as bus_mod
    monkeypatch.setattr(bus_mod, "event_bus", spy)

    started_at = time.time()
    pre_call = time.monotonic()

    isolated_dispatch_db.insert_launch_run(
        run_id="run-rh2r5-fast-1",
        bead_id="auto-rh2r5-test",
        started_at=started_at,
        branch="agent/auto-rh2r5-test",
        branch_base="master",
        image="autonomy-agent-claude",
        container_name="agent-auto-rh2r5-test",
        output_dir="/workspace/output/run-rh2r5-fast-1",
    )

    # Verify the row landed.
    rows = isolated_dispatch_db.get_currently_running()
    assert any(r["id"] == "run-rh2r5-fast-1" for r in rows)

    assert len(spy.calls) == 1, f"expected one broadcast, got {spy.calls!r}"
    call = spy.calls[0]
    assert call["topic"] == "dispatch"
    elapsed_ms = (call["ts"] - pre_call) * 1000.0
    assert elapsed_ms < 100.0, (
        f"broadcast must fire within 100ms of the insert; "
        f"observed {elapsed_ms:.1f}ms"
    )
    # Dedup is disabled — two back-to-back launches with distinct run_ids
    # must both be observable, not silently coalesced.
    assert call["dedup"] is False
    # Payload identifies which run launched.
    data = call["data"]
    assert data["event"] == "launch"
    assert data["run_id"] == "run-rh2r5-fast-1"
    assert data["bead_id"] == "auto-rh2r5-test"
