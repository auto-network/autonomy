"""Tests for OperatorActivity write integration.

Covers ``_record_operator_input``, the singleton-row write that
``SessionMonitor._process_tail_entries`` schedules when any session
parses a ``user`` or ``crosstalk`` entry. The row backs
``Presence.is_idle()``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.dashboard.session_monitor import (
    _OPERATOR_INPUT_TYPES,
    _record_operator_input,
)
from tools.graph import settings_ops
from tools.graph.surface import (
    OPERATOR_ACTIVITY_SET_ID,
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


def _read_singleton() -> dict | None:
    members = settings_ops.read_set(
        OPERATOR_ACTIVITY_SET_ID, org="personal", peers=[],
    )
    if not members.members:
        return None
    return dict(members.members[0].payload)


# ── Direct helper ────────────────────────────────────────────


def test_record_writes_singleton_row(graph_db_env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _record_operator_input(_iso(now))

    payload = _read_singleton()
    assert payload is not None
    assert payload["last_input_at"] == _iso(now)


def test_subsequent_record_overwrites(graph_db_env):
    """The row is a singleton — the latest write wins."""
    earlier = datetime.now(timezone.utc).replace(microsecond=0) \
        - timedelta(minutes=10)
    later = earlier + timedelta(minutes=5)

    _record_operator_input(_iso(earlier))
    _record_operator_input(_iso(later))

    # Presence.last_user_input reads the most recent row.
    got = Presence.last_user_input()
    assert got is not None
    assert int(got.timestamp()) == int(later.timestamp())


def test_writes_from_two_sessions_share_singleton(graph_db_env):
    """Acceptance #4: any session's input updates the same row.

    Conceptually both sessions hand the timestamp to the same writer;
    no per-session keying. Post-upsert migration (auto-nqlzg) the
    singleton stays a single row — the second write updates the first
    in place rather than appending. Latest write wins.
    """
    import json as _json

    base = datetime.now(timezone.utc).replace(microsecond=0)
    sess_a_ts = base
    sess_b_ts = base + timedelta(seconds=30)

    _record_operator_input(_iso(sess_a_ts))
    _record_operator_input(_iso(sess_b_ts))

    db = settings_ops._open(None)
    try:
        rows = db.conn.execute(
            "SELECT key, payload FROM settings WHERE set_id = ?",
            (OPERATOR_ACTIVITY_SET_ID,),
        ).fetchall()
    finally:
        db.close()

    # One singleton row — never per-session, never appended.
    assert len(rows) == 1
    assert rows[0]["key"] == "operator"
    payload = _json.loads(rows[0]["payload"])
    # Latest write wins: session B's later timestamp survives.
    assert payload["last_input_at"] == _iso(sess_b_ts)


def test_record_swallows_settings_failure(graph_db_env, monkeypatch):
    """A graph-DB hiccup must not propagate up to the tailer."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(settings_ops, "upsert_by_key", boom)
    # Should not raise.
    _record_operator_input(_iso(datetime.now(timezone.utc)))
    # No row landed.
    assert _read_singleton() is None


# ── Operator-input type set ──────────────────────────────────


def test_operator_input_types_covers_user_and_crosstalk():
    """The two entry kinds the tailer treats as operator input."""
    assert "user" in _OPERATOR_INPUT_TYPES
    assert "crosstalk" in _OPERATOR_INPUT_TYPES
    # Tool steps and assistant text are NOT operator input.
    assert "tool_use" not in _OPERATOR_INPUT_TYPES
    assert "assistant_text" not in _OPERATOR_INPUT_TYPES


# ── Presence helpers reflect the writes ──────────────────────


def test_presence_is_idle_no_row_returns_true(graph_db_env):
    assert Presence.is_idle() is True


def test_presence_is_idle_recent_input_returns_false(graph_db_env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _record_operator_input(_iso(now))
    assert Presence.is_idle(threshold=timedelta(minutes=5)) is False


def test_presence_is_idle_stale_input_returns_true(graph_db_env):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    _record_operator_input(_iso(long_ago))
    assert Presence.is_idle(threshold=timedelta(minutes=30)) is True


def test_presence_last_user_input_returns_parsed_datetime(graph_db_env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _record_operator_input(_iso(now))
    got = Presence.last_user_input()
    assert got is not None
    assert got.tzinfo is not None
    assert int(got.timestamp()) == int(now.timestamp())
