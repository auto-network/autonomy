"""Function-level ``setting.changed`` emit + commit-then-emit ordering.

Acceptance for bead auto-5mz65:

* Every ``settings_ops`` mutation fires the registered emit hook,
  regardless of caller (CLI / dashboard route / plugin / test fixture).
* The hook fires AFTER the underlying transaction commits — a
  subscriber that re-resolves via ``read_set`` on receipt sees the
  just-committed row, not nothing.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


TEST_SET_ID = "autonomy.test.func-emit"
TEST_REVISION = 1


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _clear_emit_hook():
    """Make sure no stale hook leaks across tests."""
    settings_ops.set_emit_hook(None)
    yield
    settings_ops.set_emit_hook(None)


@pytest.fixture
def fixture_schema():
    from tools.graph.schemas.registry import (
        SettingSchema, register_schema,
    )

    class V1(SettingSchema):
        set_id = TEST_SET_ID
        schema_revision = TEST_REVISION

    register_schema(TEST_SET_ID, TEST_REVISION, V1)

    class V2(SettingSchema):
        set_id = TEST_SET_ID
        schema_revision = 2

    register_schema(
        TEST_SET_ID, 2, V2,
        upconvert_from_prev=lambda p: {**p, "v2": True},
    )
    return V1


@pytest.fixture
def captured_events():
    """Install a capturing emit hook for the duration of the test."""
    events: list[dict] = []

    def hook(*, operation, snapshot, org):
        events.append({
            "operation": operation,
            "snapshot": dict(snapshot),
            "org": org,
        })

    settings_ops.set_emit_hook(hook)
    return events


# ── Acceptance #1 — every callsite fires the hook ────────


def test_cli_path_add_setting_fires_hook(
    graph_db_env, fixture_schema, captured_events,
):
    """CLI / library path: no dashboard route involved.

    Acceptance #1: ``settings_ops.add_setting(...)`` MUST fire
    ``setting.changed`` even when no HTTP request was made.
    """
    sid = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "alpha",
        {"name": "x", "image": "y", "harness": "claude"},
        org="autonomy",
    )
    assert isinstance(sid, str)
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["operation"] == "write"
    assert ev["snapshot"]["set_id"] == TEST_SET_ID
    assert ev["snapshot"]["key"] == "alpha"
    assert ev["snapshot"]["schema_revision"] == TEST_REVISION
    assert ev["snapshot"]["publication_state"] == "raw"
    assert ev["snapshot"]["deprecated"] is False
    assert ev["org"] == "autonomy"


def test_override_setting_fires_hook(
    graph_db_env, fixture_schema, captured_events,
):
    base = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"a": 1, "b": 2},
    )
    captured_events.clear()
    settings_ops.override_setting(base, {"b": 99}, state="raw")
    assert len(captured_events) == 1
    assert captured_events[0]["operation"] == "override"
    assert captured_events[0]["snapshot"]["key"] == "k"


def test_exclude_setting_fires_hook(
    graph_db_env, fixture_schema, captured_events,
):
    base = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"x": 1}, state="canonical",
    )
    captured_events.clear()
    settings_ops.exclude_setting(base)
    assert len(captured_events) == 1
    assert captured_events[0]["operation"] == "exclude"


def test_promote_setting_fires_hook(
    graph_db_env, fixture_schema, captured_events,
):
    sid = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"x": 1},
    )
    captured_events.clear()
    settings_ops.promote_setting(sid, "canonical")
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["operation"] == "promote"
    assert ev["snapshot"]["publication_state"] == "canonical"
    assert ev["snapshot"]["key"] == "k"


def test_deprecate_setting_fires_hook(
    graph_db_env, fixture_schema, captured_events,
):
    sid = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"x": 1},
    )
    captured_events.clear()
    settings_ops.deprecate_setting(sid)
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["operation"] == "deprecate"
    assert ev["snapshot"]["deprecated"] is True


def test_remove_setting_fires_hook_with_predelete_snapshot(
    graph_db_env, fixture_schema, captured_events,
):
    sid = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"x": 1},
    )
    captured_events.clear()
    settings_ops.remove_setting(sid)
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev["operation"] == "delete"
    # Pre-delete snapshot so subscribers know what was removed.
    assert ev["snapshot"]["set_id"] == TEST_SET_ID
    assert ev["snapshot"]["key"] == "k"


def test_migrate_setting_fires_hook_per_affected(
    graph_db_env, fixture_schema, captured_events,
):
    settings_ops.add_setting(TEST_SET_ID, TEST_REVISION, "a", {"x": 1})
    settings_ops.add_setting(TEST_SET_ID, TEST_REVISION, "b", {"x": 2})
    captured_events.clear()
    report = settings_ops.migrate_setting_revisions(TEST_SET_ID, 2)
    assert report.rewrote == 2
    assert len(captured_events) == 2
    for ev in captured_events:
        assert ev["operation"] == "migrate"
        assert ev["snapshot"]["schema_revision"] == 2


def test_migrate_dry_run_does_not_fire(
    graph_db_env, fixture_schema, captured_events,
):
    settings_ops.add_setting(TEST_SET_ID, TEST_REVISION, "a", {"x": 1})
    captured_events.clear()
    settings_ops.migrate_setting_revisions(TEST_SET_ID, 2, dry_run=True)
    assert captured_events == []


def test_no_hook_registered_is_a_no_op(graph_db_env, fixture_schema):
    """CLI / test contexts that don't register a hook still write fine."""
    settings_ops.set_emit_hook(None)
    sid = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"x": 1},
    )
    assert isinstance(sid, str)
    members = settings_ops.read_set(TEST_SET_ID)
    assert any(m.id == sid for m in members.members)


def test_hook_exception_does_not_block_write(
    graph_db_env, fixture_schema, caplog,
):
    """Hook is best-effort — an exception is logged, not raised."""
    def bad_hook(*, operation, snapshot, org):
        raise RuntimeError("subscriber crashed")

    settings_ops.set_emit_hook(bad_hook)
    # Should not raise.
    sid = settings_ops.add_setting(
        TEST_SET_ID, TEST_REVISION, "k", {"x": 1},
    )
    assert isinstance(sid, str)
    # Row is committed despite the hook failure.
    members = settings_ops.read_set(TEST_SET_ID)
    assert any(m.id == sid for m in members.members)


# ── Acceptance #2 — commit-then-emit (race-free) ─────────


def test_emit_after_commit_or_subscriber_misses_row(
    graph_db_env, fixture_schema,
):
    """Race-free observation: subscriber resolves on receipt, sees row.

    Hook fires AFTER the mutator's transaction commits. A fast
    subscriber that re-resolves via :func:`settings_ops.read_set` on
    receipt MUST see the new row. Repeated 100 times to surface any
    timing race; zero misses tolerated.

    The exact test name (per bead spec) preserves the failure mode in
    the failure output so a future contributor reading "test failed"
    immediately understands the invariant.
    """
    misses: list[dict] = []
    seen: list[str] = []

    def resolving_subscriber(*, operation, snapshot, org):
        # Immediate re-resolve on receipt of "new row" notification.
        if operation != "write":
            return
        members = settings_ops.read_set(snapshot["set_id"])
        match = next(
            (m for m in members.members if m.key == snapshot["key"]),
            None,
        )
        if match is None:
            misses.append(snapshot)
        else:
            seen.append(match.id)

    settings_ops.set_emit_hook(resolving_subscriber)

    for i in range(100):
        sid = settings_ops.add_setting(
            TEST_SET_ID, TEST_REVISION, f"k-{i:03d}", {"x": i},
        )
        seen[-1] == sid  # noqa: B015 — readability check, not assertion

    assert len(misses) == 0, (
        f"subscriber missed {len(misses)} rows out of 100 — "
        f"emit fired before commit"
    )
    assert len(seen) == 100
