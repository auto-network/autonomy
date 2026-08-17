"""Tests for queue-operation stub exclusion — Claude-side latch discriminator.

Reproduces the wrong-latch bug observed live on session auto-0730-021228
(2026-08-17): the viewer was pinned to a 12 KB, 9-line stub file whose first
record is a ``queue-operation`` (a queued-message flush), while the real
primary — 7,159 lines, ~4,500 turns, still being written — was ignored.

Root cause: ``_classify_codex_rollout`` returns ``"main"`` for ANY file whose
name does not start with ``rollout-`` (i.e. every Claude ``<uuid>.jsonl``), so
``_is_primary_jsonl`` cannot tell a real Claude session from a queue-operation
stub. Both are offered as primary candidates, and the latch can drift onto a
stub. There is no Claude analog to the Codex subagent exclusion.

These tests assert the property the fix must satisfy — a queue-operation-only
stub is NOT a primary session file. They FAIL today (no discriminator exists)
and are the executable spec for the fix.

Uses tmp_path with real filesystem. No real sessions.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tools.dashboard.session_monitor import (
    SessionMonitor,
    _find_primary_jsonls,
    _is_primary_jsonl,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _write(directory: Path, name: str, entries: list[dict],
           mtime_offset: float = 0) -> Path:
    p = directory / f"{name}.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    t = time.time() + mtime_offset
    os.utime(p, (t, t))
    return p


def _real_claude_primary(directory: Path, uuid: str,
                         turns: int = 200, mtime_offset: float = 0) -> Path:
    """A genuine Claude session file: starts with a `mode` record and carries
    real conversation structure (many user/assistant turns)."""
    entries: list[dict] = [
        {"type": "mode", "mode": "normal", "sessionId": uuid},
        {"type": "file-history-snapshot", "sessionId": uuid},
    ]
    for i in range(turns):
        entries.append({"type": "user", "sessionId": uuid,
                        "message": {"content": f"msg {i}"}, "uuid": f"u{i}"})
        entries.append({"type": "assistant", "sessionId": uuid,
                        "message": {"content": [{"type": "text", "text": "ok"}]},
                        "uuid": f"a{i}"})
    return _write(directory, uuid, entries, mtime_offset)


def _queue_stub(directory: Path, uuid: str, mtime_offset: float = 0) -> Path:
    """A queued-message flush stub, shaped like the real e4585cc5 stub:
    first record is a queue-operation, followed by an attachment/last-prompt
    and a single stray user/assistant pair. No session-structural record."""
    entries = [
        {"type": "queue-operation", "operation": "enqueue",
         "sessionId": uuid, "content": "ok"},
        {"type": "user", "sessionId": uuid, "message": {"content": "queued"}},
        {"type": "attachment", "sessionId": uuid},
        {"type": "assistant", "sessionId": uuid,
         "message": {"content": [{"type": "text", "text": "ack"}]}},
        {"type": "last-prompt", "sessionId": uuid},
        {"type": "queue-operation", "operation": "flush", "sessionId": uuid},
    ]
    return _write(directory, uuid, entries, mtime_offset)


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def session_with_queue_stubs(tmp_path):
    """A real primary plus two queue-operation stubs, as seen live on
    auto-0730-021228. The stubs are created AFTER the primary's start, which
    is exactly how the live latch drifted onto the newest stub."""
    rd = tmp_path / "sessions" / "-workspace-repo"
    rd.mkdir(parents=True)
    primary = _real_claude_primary(rd, "a7826859-primary",
                                   turns=200, mtime_offset=30)
    stub1 = _queue_stub(rd, "4059b153-stub", mtime_offset=10)
    stub2 = _queue_stub(rd, "e4585cc5-stub", mtime_offset=20)
    return {"rd": rd, "primary": primary, "stubs": [stub1, stub2]}


# ── Tests ─────────────────────────────────────────────────────────────────

class TestQueueStubExclusion:
    """A queue-operation-only stub must never be selected as primary."""

    @pytest.mark.xfail(strict=True, reason=(
        "BUG (auto-0730-021228 live repro): no Claude-side primary "
        "discriminator — _classify_codex_rollout returns 'main' for every "
        "non-rollout- file, so a queue-operation stub classifies as primary. "
        "Remove this marker when the discriminator lands."
    ))
    def test_is_primary_jsonl_rejects_queue_stub(self, tmp_path):
        """The discriminator itself: a queue-operation stub is not primary."""
        stub = _queue_stub(tmp_path, "e4585cc5-stub")
        assert _is_primary_jsonl(stub) is False, (
            "A queue-operation flush stub must not classify as a primary "
            "session file"
        )

    def test_is_primary_jsonl_accepts_real_session(self, tmp_path):
        """Guard the discriminator against over-reach: a real Claude session
        (mode record + real turns) MUST still be primary."""
        primary = _real_claude_primary(tmp_path, "a7826859-primary", turns=50)
        assert _is_primary_jsonl(primary) is True

    @pytest.mark.xfail(strict=True, reason=(
        "BUG (auto-0730-021228 live repro): queue-operation stubs leak into "
        "_find_primary_jsonls candidates. Remove this marker when the "
        "discriminator lands."
    ))
    def test_find_primary_excludes_queue_stubs(self, session_with_queue_stubs):
        """_find_primary_jsonls returns only the real session, not the stubs."""
        rd = session_with_queue_stubs["rd"]
        primary = session_with_queue_stubs["primary"]
        stubs = session_with_queue_stubs["stubs"]

        # Precondition: rglob sees all three files.
        assert len(list(rd.rglob("*.jsonl"))) == 3

        primaries = _find_primary_jsonls(rd)
        assert primaries == [primary], (
            f"Expected only the real primary {primary.name}; got "
            f"{[p.name for p in primaries]} — queue stubs "
            f"{[s.name for s in stubs]} leaked in"
        )

    @pytest.mark.xfail(strict=True, reason=(
        "BUG (auto-0730-021228 live repro): resolve_session_file returns a "
        "queue-operation stub instead of the real primary. Remove this marker "
        "when the discriminator lands."
    ))
    def test_run_directory_fallback_returns_real_primary(
        self, tmp_path, monkeypatch,
    ):
        """resolve_session_file for the tmux name returns the real session,
        never a queue stub — the exact live failure on auto-0730-021228."""
        agent_runs = tmp_path / "agent-runs"
        rd = agent_runs / "auto-0730-021228-x" / "sessions" / "-workspace-repo"
        rd.mkdir(parents=True)
        _queue_stub(rd, "e4585cc5-stub", mtime_offset=20)
        primary = _real_claude_primary(rd, "a7826859-primary",
                                       turns=200, mtime_offset=30)
        monkeypatch.setenv("DASHBOARD_AGENT_RUNS_DIR", str(agent_runs))

        resolved = SessionMonitor().resolve_session_file("auto-0730-021228-x")
        assert resolved == primary, (
            f"resolve_session_file returned {resolved.name if resolved else None}, "
            f"expected the real primary {primary.name}"
        )
