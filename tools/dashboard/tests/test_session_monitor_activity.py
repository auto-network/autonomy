"""Tests for ParticipantActivity write integration (substrate.D).

Covers the ``_write_participant_activity`` helper that
:meth:`SessionMonitor._process_tail_entries` calls on every batch of
freshly-parsed JSONL entries. The activity row backs
``Presence.is_idle()``; with these writes wired, Presence helpers stop
returning stub defaults and start reflecting real session activity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.dashboard.session_monitor import (
    _ACTIVITY_WINDOW_SECONDS,
    _TailState,
    _parse_iso_timestamp,
    _write_participant_activity,
)
from tools.graph import settings_ops
from tools.graph.surface import (
    PARTICIPANT_ACTIVITY_SET_ID,
    Presence,
)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh per-test sqlite file."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("GRAPH_SCOPE", raising=False)
    yield db_path


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(etype: str, dt: datetime, **extra) -> dict:
    return {"type": etype, "timestamp": _iso(dt), **extra}


def _read_activity(participant_id: str) -> dict | None:
    members = settings_ops.read_set(
        PARTICIPANT_ACTIVITY_SET_ID, org="personal", peers=[],
    )
    for m in members.members:
        if m.key == participant_id:
            return dict(m.payload)
    return None


# ── Acceptance #1: every parsed user turn writes the row ─────


def test_user_turn_writes_activity_row(graph_db_env):
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    entries = [_entry("user", now, role="user", content="hi")]

    _write_participant_activity("sess-1", ts, entries)

    payload = _read_activity("sess-1")
    assert payload is not None
    assert payload["participant_id"] == "sess-1"
    assert payload["participant_kind"] == "agent"
    assert payload["last_user_input_at"] == _iso(now)
    assert payload["last_session_turn_at"] == _iso(now)
    assert payload["last_meaningful_at"] == _iso(now)
    assert payload["inputs_last_hour"] == 1
    assert payload["turns_last_hour"] == 1


def test_assistant_only_batch_does_not_clobber_user_input(graph_db_env):
    """An assistant-only batch must carry forward last_user_input_at.

    Asserts against the in-memory ``ts.last_activity_payload`` (the exact
    final write) rather than ``read_set``: same-second writes hit the v1
    substrate gap where ``read_set`` tie-breaks nondeterministically.
    """
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    user_at = now - timedelta(minutes=5)

    _write_participant_activity(
        "sess-2", ts, [_entry("user", user_at, content="hi")],
    )
    _write_participant_activity(
        "sess-2", ts,
        [_entry("assistant_text", now, role="assistant", content="hi back")],
    )

    payload = ts.last_activity_payload
    assert payload is not None
    assert payload["last_user_input_at"] == _iso(user_at)
    assert payload["last_session_turn_at"] == _iso(now)
    # inputs_last_hour stays at 1 (the prior user input is still in window),
    # turns_last_hour is now 2 (user + assistant).
    assert payload["inputs_last_hour"] == 1
    assert payload["turns_last_hour"] == 2


def test_tool_only_batch_does_not_write(graph_db_env):
    """Tool steps are substeps of an existing turn — they do NOT count."""
    ts = _TailState()
    now = datetime.now(timezone.utc)

    _write_participant_activity(
        "sess-tools",
        ts,
        [
            _entry("tool_use", now, tool_id="tu-1"),
            _entry("tool_result", now, tool_id="tu-1"),
        ],
    )
    assert _read_activity("sess-tools") is None
    assert not ts.activity_user_inputs
    assert not ts.activity_turns


def test_crosstalk_counts_as_user_input(graph_db_env):
    """CrossTalk pings to a session wake the agent — count as input."""
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _write_participant_activity(
        "sess-ct", ts,
        [_entry("crosstalk", now, sender="alice", content="hi")],
    )
    payload = _read_activity("sess-ct")
    assert payload["last_user_input_at"] == _iso(now)
    assert payload["inputs_last_hour"] == 1
    assert payload["turns_last_hour"] == 1


def test_empty_batch_is_a_noop(graph_db_env):
    ts = _TailState()
    _write_participant_activity("sess-empty", ts, [])
    assert _read_activity("sess-empty") is None


def test_unparseable_timestamp_skipped(graph_db_env):
    """A malformed timestamp shouldn't take down the helper."""
    ts = _TailState()
    _write_participant_activity(
        "sess-bad",
        ts,
        [{"type": "user", "timestamp": "not-a-date", "content": "x"}],
    )
    # Nothing parseable → no write.
    assert _read_activity("sess-bad") is None


# ── Acceptance #4: counters reflect last hour ────────────────


def test_counters_prune_to_one_hour(graph_db_env):
    """Old entries fall out of the sliding window once newer ones land."""
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(seconds=_ACTIVITY_WINDOW_SECONDS + 600)
    recent = now - timedelta(seconds=10)

    _write_participant_activity(
        "sess-prune", ts, [_entry("user", old, content="ancient")],
    )
    _write_participant_activity(
        "sess-prune", ts, [_entry("user", recent, content="recent")],
    )

    # In-memory payload has the latest counter values (avoids the v1
    # read_set same-second tie-break gap).
    payload = ts.last_activity_payload
    assert payload["inputs_last_hour"] == 1
    assert payload["turns_last_hour"] == 1
    # Both deques should have pruned the ancient entry.
    assert len(ts.activity_user_inputs) == 1
    assert len(ts.activity_turns) == 1


def test_multiple_recent_inputs_increment_counter(graph_db_env):
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _write_participant_activity(
        "sess-multi", ts,
        [
            _entry("user", now - timedelta(minutes=10), content="first"),
            _entry("user", now - timedelta(minutes=5), content="second"),
            _entry("assistant_text", now, content="reply"),
        ],
    )
    payload = _read_activity("sess-multi")
    assert payload["inputs_last_hour"] == 2
    assert payload["turns_last_hour"] == 3


# ── Acceptance #2/#3: Presence helpers see the writes ────────


def test_presence_is_idle_reflects_recent_input(graph_db_env):
    """Acceptance #2: Presence.is_idle uses the row session_monitor wrote."""
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _write_participant_activity(
        "sess-live", ts, [_entry("user", now, content="hi")],
    )
    assert Presence.is_idle("sess-live") is False


def test_presence_is_idle_true_for_stale_session(graph_db_env):
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    long_ago = now - timedelta(hours=2)
    _write_participant_activity(
        "sess-stale", ts, [_entry("user", long_ago, content="hi")],
    )
    assert Presence.is_idle(
        "sess-stale", threshold=timedelta(minutes=30),
    ) is True


def test_presence_last_user_input_returns_actual_timestamp(graph_db_env):
    """Acceptance #3: last_user_input returns the most-recent input."""
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _write_participant_activity(
        "sess-last", ts, [_entry("user", now, content="hi")],
    )
    got = Presence.last_user_input("sess-last")
    assert got is not None
    assert int(got.timestamp()) == int(now.timestamp())


def test_presence_inputs_last_hour_matches_counter(graph_db_env):
    """Acceptance #4: inputs_last_hour mirrors what the writer recorded."""
    ts = _TailState()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _write_participant_activity(
        "sess-counter", ts,
        [
            _entry("user", now - timedelta(minutes=20), content="first"),
            _entry("user", now - timedelta(minutes=2), content="second"),
        ],
    )
    assert Presence.inputs_last_hour("sess-counter") == 2


def test_write_failure_swallows_exception(graph_db_env, monkeypatch):
    """Settings hiccups must not propagate up to the tailer."""
    ts = _TailState()
    now = datetime.now(timezone.utc)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(settings_ops, "add_setting", boom)
    # Should not raise.
    _write_participant_activity(
        "sess-boom", ts, [_entry("user", now, content="hi")],
    )
    # No row landed.
    assert _read_activity("sess-boom") is None


# ── Tiny unit checks on the timestamp parser ─────────────────


def test_parse_iso_timestamp_z_suffix():
    got = _parse_iso_timestamp("2026-05-02T12:34:56Z")
    assert got is not None
    expected = datetime(
        2026, 5, 2, 12, 34, 56, tzinfo=timezone.utc,
    ).timestamp()
    assert abs(got - expected) < 1e-3


def test_parse_iso_timestamp_offset():
    got = _parse_iso_timestamp("2026-05-02T12:34:56+00:00")
    assert got is not None


def test_parse_iso_timestamp_garbage_returns_none():
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("nope") is None
