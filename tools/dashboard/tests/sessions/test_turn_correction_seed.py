"""Tests for the lazy-seed step that rehydrates ``_TailState.recent_user_turns``
from the JSONL tail when the deque is cold (e.g., post uvicorn ``--reload``).

The matcher's lookback deque is in-memory; a process restart wipes it and
turn-corrections issued in the cold-start window get silently dropped despite
emitting valid JSON. The seed reads the JSONL tail once per state and maps
the raw entry shape to the matcher's expected shape (``message_id`` /
``content`` / ``timestamp``).

Bead auto-edec1.4. See ``_seed_recent_user_turns_from_jsonl``.
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.dashboard.session_monitor import (
    SessionMonitor,
    _TailState,
    _seed_recent_user_turns_from_jsonl,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries))


def test_seed_populates_from_string_content(tmp_path: Path) -> None:
    """Raw JSONL user entries with string ``message.content`` get mapped to
    the matcher's expected ``message_id`` / ``content`` shape."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": "hello"}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T00:01:00Z",
         "message": {"content": "world"}},
        {"type": "assistant", "uuid": "a1", "message": {"content": "hi back"}},
    ])
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    seeded = list(ts.recent_user_turns)
    assert [e["message_id"] for e in seeded] == ["u1", "u2"]
    assert seeded[0]["content"] == "hello"
    assert seeded[0]["timestamp"] == "2026-01-01T00:00:00Z"
    assert seeded[1]["content"] == "world"


def test_seed_flattens_block_list_content(tmp_path: Path) -> None:
    """Multi-block user content (Claude wire format) flattens to joined text."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "u-multi", "timestamp": "t",
         "message": {"content": [
             {"type": "text", "text": "part one "},
             {"type": "image", "source": {"type": "base64", "data": "..."}},
             {"type": "text", "text": "part two"},
         ]}},
    ])
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    seeded = list(ts.recent_user_turns)
    assert len(seeded) == 1
    assert seeded[0]["content"] == "part one part two"  # image block dropped


def test_seed_skips_non_user_entries(tmp_path: Path) -> None:
    """Assistant / tool entries don't contribute to the lookback."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "uuid": "a1", "message": {"content": "hi"}},
        {"type": "user", "uuid": "u1", "message": {"content": "ok"},
         "timestamp": "t"},
        {"type": "tool_result", "uuid": "tr1"},
    ])
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    assert [e["message_id"] for e in ts.recent_user_turns] == ["u1"]


def test_seed_skips_user_entry_with_no_uuid(tmp_path: Path) -> None:
    """Defensive: a malformed user entry with no uuid is skipped, not crashed."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "", "message": {"content": "no id"},
         "timestamp": "t"},
        {"type": "user", "uuid": "u-good", "message": {"content": "ok"},
         "timestamp": "t"},
    ])
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    assert [e["message_id"] for e in ts.recent_user_turns] == ["u-good"]


def test_seed_skips_user_entry_with_empty_content(tmp_path: Path) -> None:
    """User entries that flatten to empty content are skipped."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "u-empty", "message": {"content": ""},
         "timestamp": "t"},
        {"type": "user", "uuid": "u-list-empty",
         "message": {"content": [{"type": "image"}]}, "timestamp": "t"},
        {"type": "user", "uuid": "u-good", "message": {"content": "ok"},
         "timestamp": "t"},
    ])
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    assert [e["message_id"] for e in ts.recent_user_turns] == ["u-good"]


def test_seed_handles_malformed_jsonl_lines(tmp_path: Path) -> None:
    """One bad line never poisons the seed of the others."""
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "uuid": "u1",
                    "message": {"content": "before"}, "timestamp": "t"})
        + "\nNOT JSON\n"
        + json.dumps({"type": "user", "uuid": "u2",
                      "message": {"content": "after"}, "timestamp": "t"})
    )
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    assert [e["message_id"] for e in ts.recent_user_turns] == ["u1", "u2"]


def test_seed_no_op_for_missing_jsonl(tmp_path: Path) -> None:
    """Nonexistent JSONL path leaves the deque empty without raising."""
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(tmp_path / "nope.jsonl"))
    assert not ts.recent_user_turns


def test_seed_no_op_for_none_path() -> None:
    """``None`` jsonl_path is a defensive no-op."""
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, None)
    assert not ts.recent_user_turns


def test_persist_triggers_seed_and_sets_flag(tmp_path: Path) -> None:
    """The first turn_correction batch on a cold _TailState seeds and sets
    the flag; subsequent batches (including ones with no user activity)
    skip the seed."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"},
         "timestamp": "2026-01-01T00:00:00Z"},
    ])
    ts = _TailState()
    assert not ts.recent_user_turns
    assert not ts.recent_user_turns_seeded

    SessionMonitor._persist_turn_corrections(
        row={"jsonl_path": str(jsonl), "session_uuid": "s"},
        ts=ts,
        entries=[{"type": "turn_correction",
                  "corrected_text": "hello (cleaned)",
                  "timestamp": "2026-01-01T00:00:30Z"}],
    )
    assert ts.recent_user_turns_seeded
    assert [e["message_id"] for e in ts.recent_user_turns] == ["u1"]


def test_persist_seed_idempotent_after_flag_set(tmp_path: Path) -> None:
    """When the flag is already set (a previous batch seeded), subsequent
    batches do not re-scan the JSONL — even if the deque has been
    drained for some reason."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"},
         "timestamp": "t"},
    ])
    ts = _TailState()
    ts.recent_user_turns_seeded = True  # pretend a prior batch seeded

    SessionMonitor._persist_turn_corrections(
        row={"jsonl_path": str(jsonl), "session_uuid": "s"},
        ts=ts,
        entries=[{"type": "turn_correction",
                  "corrected_text": "x",
                  "timestamp": "t"}],
    )
    # Flag stays set, deque untouched (no entries seeded since flag gated).
    assert ts.recent_user_turns_seeded
    assert not ts.recent_user_turns


def test_persist_no_seed_without_turn_correction(tmp_path: Path) -> None:
    """A batch with no turn_correction entries does not trigger the seed —
    we only scan when there's matching work to do."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"},
         "timestamp": "t"},
    ])
    ts = _TailState()
    SessionMonitor._persist_turn_corrections(
        row={"jsonl_path": str(jsonl), "session_uuid": "s"},
        ts=ts,
        entries=[{"type": "user", "message_id": "u-other",
                  "content": "live", "timestamp": "t"}],
    )
    assert not ts.recent_user_turns_seeded
    # The user entry does still get remembered through the live path.
    assert [e["message_id"] for e in ts.recent_user_turns] == ["u-other"]


def test_seed_respects_deque_maxlen(tmp_path: Path) -> None:
    """Large JSONLs don't overflow the deque — maxlen handles the trim."""
    jsonl = tmp_path / "session.jsonl"
    entries = [
        {"type": "user", "uuid": f"u{i}",
         "message": {"content": f"msg-{i}"}, "timestamp": "t"}
        for i in range(50)
    ]
    _write_jsonl(jsonl, entries)
    ts = _TailState()
    _seed_recent_user_turns_from_jsonl(ts, str(jsonl))
    # Deque maxlen <= 50, so we should have at most maxlen entries; whatever
    # the cap is, the kept entries are the most-recent (chronological tail).
    seeded = list(ts.recent_user_turns)
    assert len(seeded) <= ts.recent_user_turns.maxlen
    # Last entry must be the chronologically-latest one regardless of cap.
    assert seeded[-1]["message_id"] == "u49"
