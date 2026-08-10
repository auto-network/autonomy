"""Primary regression test for bead auto-suvcp — the CalStartupStall timeline.

Live repro ``auto-0807-225218``: commit ``80126ec1`` made ``watch_scan``
classify the normal zero-byte Codex startup rollout ``unknown`` and return
with no object retaining responsibility.  Codex wrote its complete startup
burst and went quiet.  Reconciliation later linked the now-classifiable
file, but attachment armed IN_MODIFY without reading the bytes already
present — so with no further write the row sat at offset/count zero with a
stale ``last_message`` while the transcript endpoint served the answer.

Timeline reproduced here (empty-create → burst → quiet):

  1. a Codex container session registers over an EMPTY sessions dir
  2. Codex creates an EMPTY rollout; the IN_CREATE handler classifies it
     ``unknown`` and abandons after its bounded header retries
  3. Codex writes its full startup burst (session_meta + first assistant
     message) and goes quiet — NO further write ever happens
  4. the reconciliation tick runs and links the now-classifiable file

Assertions are on PERSISTED, OPERATOR-VISIBLE state — file_offset,
entry_count, last_message, and the session:messages broadcast that makes
the turns visible.  Never "was attach called" (the original tests mocked
attachment and asserted method calls; that is exactly how this bug
shipped).

TDD discipline: this test must FAIL against the pre-fix code and PASS
after the FileTrack/SessionGate fix.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid as uuid_mod
from pathlib import Path

import pytest

pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")


ROLLOUT_STEM = f"rollout-2026-08-10T12-00-00-{uuid_mod.uuid4()}"
BURST_ANSWER = (
    "The answer is 42 — startup burst response that must become "
    "operator-visible with no further write."
)


def _session_meta_line(rollout_uuid: str) -> str:
    return json.dumps({
        "timestamp": "2026-08-10T12:00:00.100Z",
        "type": "session_meta",
        "payload": {
            "id": rollout_uuid,
            "timestamp": "2026-08-10T12:00:00Z",
            "cwd": "/workspace/repo",
            "originator": "codex_cli_rs",
            "cli_version": "0.21.0",
            "source": "exec",
        },
    })


def _agent_message_line(text: str) -> str:
    return json.dumps({
        "timestamp": "2026-08-10T12:00:01.000Z",
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    })


class _CapturingBus:
    """Minimal event-bus double capturing broadcasts by topic."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, dict]] = []
        self.cache: dict[str, object] = {}

    async def broadcast(self, topic: str, payload, **_kw) -> None:
        self.broadcasts.append((topic, payload))

    def update_cache(self, topic: str, payload) -> None:
        self.cache[topic] = payload

    def message_entries(self, session: str) -> list:
        out = []
        for topic, payload in self.broadcasts:
            if topic == "session:messages" and payload.get("session_id") == session:
                out.extend(payload.get("entries") or [])
        return out


def _db_row(db_path: Path, tmux_name: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """Fresh dashboard.db (full schema via init_db) + reloaded modules."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    agent_runs = tmp_path / "agent-runs"
    agent_runs.mkdir()
    monkeypatch.setenv("DASHBOARD_AGENT_RUNS_DIR", str(agent_runs))

    # Isolate the graph stack: the in-process eager-source / tail-appender
    # paths would otherwise write real org DBs. Both are orthogonal to the
    # tail-state regression this file pins.
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    from tools.dashboard import feature_flags as ff_mod
    monkeypatch.setattr(ff_mod, "is_enabled", lambda _name, default=False: False)

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)

    # Pre-fix code retried the empty header on IN_CREATE before abandoning;
    # keep that window short when running against it (the constant no
    # longer exists post-fix — raising=False tolerates both).
    monkeypatch.setattr(
        sm_mod, "_CODEX_HEADER_RETRY_DELAYS_SECONDS", (0.01, 0.02),
        raising=False,
    )

    yield tmp_path, db_path, sm_mod


@pytest.mark.asyncio
async def test_quiet_startup_burst_becomes_visible_without_further_write(setup_env):
    tmp_path, db_path, sm_mod = setup_env

    tmux_name = "auto-stall-001"
    sessions_dir = tmp_path / "agent-runs" / f"{tmux_name}-20260810-120000" / "sessions"
    sessions_dir.mkdir(parents=True)

    mon = sm_mod.SessionMonitor()
    mon._init_inotify()
    assert mon._use_inotify, "test requires real inotify_simple"
    bus = _CapturingBus()
    mon._event_bus = bus

    # 1. Register over an EMPTY directory (the normal container launch).
    await mon.register(
        tmux_name=tmux_name,
        session_type="container",
        project="autonomy",
        resolution_dir=sessions_dir,
        harness="codex",
        seed_message="Starting...",
    )

    # 2. Codex creates the rollout EMPTY; IN_CREATE fires while it still
    #    has no header.  Drive the exact handler the tailer loop invokes.
    rollout = sessions_dir / f"{ROLLOUT_STEM}.jsonl"
    rollout.touch()
    from tools.dashboard.dao.dashboard_db import get_session
    row = get_session(tmux_name)
    assert row is not None
    await mon._handle_container_create(tmux_name, row, rollout)

    # 3. The startup burst lands AFTER the handler gave up, then quiet.
    rollout_uuid = ROLLOUT_STEM.split("rollout-2026-08-10T12-00-00-")[1]
    rollout.write_text(
        _session_meta_line(rollout_uuid) + "\n"
        + _agent_message_line(BURST_ANSWER) + "\n"
    )
    burst_size = rollout.stat().st_size

    # 4. Reconciliation is the only remaining actor.  NO further write
    #    will ever happen; whatever state exists after this tick (plus a
    #    beat for scheduled tasks) is what the operator sees forever.
    await mon.reconciliation_tick()
    await asyncio.sleep(0.1)

    row = _db_row(db_path, tmux_name)
    assert row is not None
    assert row["jsonl_path"] == str(rollout), (
        f"the burst file must be linked; got {row['jsonl_path']!r}"
    )

    # ── The regression: persisted tail state must reflect the burst ──
    assert row["file_offset"] == burst_size, (
        "persisted file_offset must reach the burst EOF with no further "
        f"write; got {row['file_offset']} (EOF={burst_size})"
    )
    assert row["entry_count"] == 2, (
        "entry_count must count the burst's parsed raw JSONL lines; "
        f"got {row['entry_count']}"
    )
    assert BURST_ANSWER[:40] in (row["last_message"] or ""), (
        "last_message must show the burst's assistant response, not the "
        f"stale seed; got {row['last_message']!r}"
    )

    # ── Operator-visible turns: the burst must have been broadcast ──
    entries = bus.message_entries(tmux_name)
    assert any(
        e.get("type") == "assistant_text" and BURST_ANSWER[:40] in (e.get("content") or "")
        for e in entries
    ), (
        "the startup burst's assistant turn must reach session:messages "
        f"subscribers; got entries={entries!r}"
    )
