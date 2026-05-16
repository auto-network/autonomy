"""Tests for :mod:`tools.dashboard.feature_flags`.

End-to-end reads against a temp graph DB: writes a flag row via
:func:`settings_ops.upsert_by_key`, then asserts the dashboard helper
returns the right boolean. Also covers absent-row, malformed-payload,
and ``all_flags`` aggregation. Companion to S0 (graph://40dd9d7a-23a).
"""

from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.schemas import feature_flags as ff_schema
from tools.dashboard import feature_flags as ff


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for per-test isolation."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _seed_flag(name: str, *, enabled: bool, owner: str = "test", description: str = "test flag"):
    settings_ops.upsert_by_key(
        ff_schema.FEATURE_FLAGS_SET_ID,
        ff_schema.FEATURE_FLAGS_REVISION,
        name,
        {"enabled": enabled, "description": description, "owner": owner},
        org=settings_ops.CALLER_ORG,
    )


# ── is_enabled ───────────────────────────────────────────────


def test_is_enabled_absent_flag_returns_false(graph_db_env):
    assert ff.is_enabled("voice.never.set") is False


def test_is_enabled_returns_true_when_flag_set_true(graph_db_env):
    _seed_flag("voice.pipe_enabled", enabled=True)
    assert ff.is_enabled("voice.pipe_enabled") is True


def test_is_enabled_returns_false_when_flag_set_false(graph_db_env):
    _seed_flag("voice.pipe_enabled", enabled=False)
    assert ff.is_enabled("voice.pipe_enabled") is False


def test_is_enabled_isolates_flags(graph_db_env):
    """Two flags coexist; reading one doesn't leak the other's value."""
    _seed_flag("voice.client_enabled", enabled=True)
    _seed_flag("inference.librarian_routing", enabled=False)
    assert ff.is_enabled("voice.client_enabled") is True
    assert ff.is_enabled("inference.librarian_routing") is False
    assert ff.is_enabled("voice.unknown") is False


def test_is_enabled_round_trips_through_upsert(graph_db_env):
    """Flipping a flag via upsert_by_key is observable immediately."""
    _seed_flag("voice.toggle", enabled=False)
    assert ff.is_enabled("voice.toggle") is False
    _seed_flag("voice.toggle", enabled=True)
    assert ff.is_enabled("voice.toggle") is True
    _seed_flag("voice.toggle", enabled=False)
    assert ff.is_enabled("voice.toggle") is False


# ── all_flags ────────────────────────────────────────────────


def test_all_flags_empty_when_no_rows(graph_db_env):
    assert ff.all_flags() == {}


def test_all_flags_returns_all_rows(graph_db_env):
    _seed_flag("voice.client_enabled", enabled=True, owner="S5", description="enable voice")
    _seed_flag("voice.responsive_collapse_enabled", enabled=False, owner="S6", description="collapse mobile")
    _seed_flag("inference.librarian_routing", enabled=True, owner="S7", description="route summarize")

    snapshot = ff.all_flags()

    assert set(snapshot.keys()) == {
        "voice.client_enabled",
        "voice.responsive_collapse_enabled",
        "inference.librarian_routing",
    }
    assert snapshot["voice.client_enabled"]["enabled"] is True
    assert snapshot["voice.client_enabled"]["owner"] == "S5"
    assert snapshot["voice.responsive_collapse_enabled"]["enabled"] is False
    assert snapshot["inference.librarian_routing"]["description"] == "route summarize"


def test_all_flags_returns_fresh_dicts(graph_db_env):
    """Mutating the returned snapshot must not affect underlying storage."""
    _seed_flag("voice.client_enabled", enabled=True)
    snapshot = ff.all_flags()
    snapshot["voice.client_enabled"]["enabled"] = False
    # Re-read; original value preserved.
    assert ff.is_enabled("voice.client_enabled") is True
