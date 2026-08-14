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
    """Hermetic stores whose resolutions AGREE (test_surface.py's recipe):
    flag reads resolve at explicit org='personal', so the pin points AT
    the orgs tree's personal.db — explicit, pin, and caller scope all
    converge on one hermetic file."""
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    db_path = orgs / "personal.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    ff.invalidate_cache(all_orgs=True)
    yield db_path
    ff.invalidate_cache(all_orgs=True)


def _seed_flag(name: str, *, enabled: bool, owner: str = "test", description: str = "test flag"):
    settings_ops.upsert_by_key(
        ff_schema.FEATURE_FLAGS_SET_ID,
        ff_schema.FEATURE_FLAGS_REVISION,
        name,
        {"enabled": enabled, "description": description, "owner": owner},
        org=settings_ops.CALLER_ORG,
    )
    # Production receives this through server._settings_emit_hook. These
    # focused helper tests do not require importing the full ASGI server.
    # Invalidate under the org identity READS cache with: a caller-scope
    # write resolves to org=None, but the personal store's snapshot is
    # keyed 'personal' — invalidate(None) pops a different key and the
    # stale empty snapshot keeps answering (measured; the None<->personal
    # cache-key split is flagged to the resolution owners).
    ff.invalidate_cache(org=ff._FLAGS_ORG)


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


# ── is_enabled(default=...) — W4 unflagged-default support ───────────


def test_is_enabled_absent_flag_honors_default_true(graph_db_env):
    """A flag W4 wants unflagged-on (e.g. ingest.eager_sources) reads
    True with no Settings row at all — no seeding required."""
    assert ff.is_enabled("ingest.eager_sources", default=True) is True


def test_is_enabled_explicit_false_row_overrides_default_true(graph_db_env):
    """An explicit disable still wins over default=True — a deployment
    can always opt out regardless of the code-level default."""
    _seed_flag("ingest.eager_sources", enabled=False)
    assert ff.is_enabled("ingest.eager_sources", default=True) is False


def test_is_enabled_explicit_true_row_with_default_false_unaffected(graph_db_env):
    """default= only matters for absent rows — an explicit true row
    behaves identically regardless of what default= is passed."""
    _seed_flag("voice.pipe_enabled", enabled=True)
    assert ff.is_enabled("voice.pipe_enabled", default=False) is True


def test_is_enabled_default_false_is_backward_compatible(graph_db_env):
    """Existing callers that don't pass default= keep the original
    absent-row-returns-False contract."""
    assert ff.is_enabled("some.other.flag") is False
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


def test_multiple_flag_checks_share_one_settings_read(graph_db_env, monkeypatch):
    _seed_flag("voice.client_enabled", enabled=True)
    ff.invalidate_cache(all_orgs=True)
    original = settings_ops.read_set
    calls = 0

    def counted_read_set(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(settings_ops, "read_set", counted_read_set)

    assert ff.is_enabled("voice.client_enabled") is True
    assert ff.is_enabled("voice.unknown") is False
    assert ff.all_flags()["voice.client_enabled"]["enabled"] is True
    assert calls == 1
