"""In-process dispatch-session reconciliation (handoff 148ead24 item 4).

The dispatcher's HTTP monitor calls 401 (no bearer); the dashboard's
watcher now registers sessions when run rows appear and demotes them when
rows turn terminal — so a card can never be stranded ACTIVE by a missed
HTTP call, and a restart sweeps up anything stranded while it was down.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tools.dashboard import server as server_mod


class FakeMonitor:
    def __init__(self, existing: dict[str, dict] | None = None):
        self.rows = dict(existing or {})
        self.registered: list[dict] = []
        self.demoted: list[str] = []

    def get_one(self, name):
        return self.rows.get(name)

    async def register_session(self, **kw):
        self.registered.append(kw)
        self.rows[kw["tmux_name"]] = {"state": "ACTIVE"}

    async def deregister_session(self, name):
        self.demoted.append(name)
        self.rows[name] = {"state": "ENDED"}


@pytest.fixture
def wired(monkeypatch):
    import agents.dispatch_db as dispatch_db_mod
    state = SimpleNamespace(running=[], recent=[], monitor=FakeMonitor())
    monkeypatch.setattr(dispatch_db_mod, "get_currently_running",
                        lambda: state.running)
    monkeypatch.setattr(dispatch_db_mod, "list_runs",
                        lambda limit=200, **kw: state.recent)
    monkeypatch.setattr(server_mod, "session_monitor", state.monitor)
    server_mod._dispatch_sessions_registered.clear()
    server_mod._dispatch_sessions_demoted.clear()
    return state


def test_running_agentic_row_registers_its_session(wired):
    wired.running = [{"id": "agent-x-1", "kind": "agentic",
                      "output_dir": "/tmp/runs/agent-x-1-20260828", "bead_id": ""}]
    asyncio.run(server_mod._reconcile_dispatch_sessions())
    assert [r["tmux_name"] for r in wired.monitor.registered] == ["agent-x-1"]
    assert wired.monitor.registered[0]["type"] == "agentic"
    # second pass: memoized, no duplicate registration
    asyncio.run(server_mod._reconcile_dispatch_sessions())
    assert len(wired.monitor.registered) == 1


def test_terminal_row_demotes_stranded_active_session(wired):
    wired.monitor.rows["agent-x-2"] = {"state": "ACTIVE"}
    wired.recent = [{"id": "agent-x-2", "kind": "agentic", "status": "DONE",
                     "output_dir": "/tmp/runs/agent-x-2-20260828"}]
    asyncio.run(server_mod._reconcile_dispatch_sessions())
    assert wired.monitor.demoted == ["agent-x-2"]
    # already-ended rows are left alone on the next pass
    asyncio.run(server_mod._reconcile_dispatch_sessions())
    assert wired.monitor.demoted == ["agent-x-2"]


def test_bead_run_uses_output_dir_name_and_running_rows_stay(wired):
    wired.monitor.rows["agent-auto-77-123"] = {"state": "ACTIVE"}
    wired.recent = [
        {"id": "r1", "kind": "bead", "status": "RUNNING",
         "output_dir": "/tmp/runs/agent-auto-77-123", "bead_id": "auto-77"},
        {"id": "r2", "kind": "bead", "status": "FAILED",
         "output_dir": "/tmp/runs/agent-auto-88-456", "bead_id": "auto-88"},
    ]
    wired.monitor.rows["agent-auto-88-456"] = {"state": "ACTIVE"}
    asyncio.run(server_mod._reconcile_dispatch_sessions())
    assert wired.monitor.demoted == ["agent-auto-88-456"]


def test_unknown_or_ended_sessions_are_not_touched(wired):
    wired.recent = [
        {"id": "gone", "kind": "agentic", "status": "DONE",
         "output_dir": "/tmp/runs/gone-1"},           # never registered
    ]
    asyncio.run(server_mod._reconcile_dispatch_sessions())
    assert wired.monitor.demoted == []
