"""Tests for :func:`tools.graph.settings_ops.upsert_by_key` (auto-nqlzg).

Closes substrate gap #2 (``graph://dff97eec-c59``): an atomic
single-transaction UPDATE-or-INSERT at ``(set_id, schema_revision,
key)``. Acceptance criteria from the bead:

1. Public import works.
2. Missing row → INSERT, returns new id.
3. Existing row → UPDATE in place, same id.
4. ``@cache``-decorated schemas restamp ``expires_at`` on every upsert.
5. Exactly one ``set_emit_hook`` per upsert (insert OR update).
6. Legacy duplicates → newest base wins, no error.
7. Payload validation runs first; bad payload writes nothing.
8. Concurrent in-process writers produce one final row.
9. Surface Presence migration: heartbeat collisions stay one row.
10. ``dashboard.operator.activity`` migration: singleton stays one row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from uuid import uuid4

import pytest

from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SCHEMAS,
    SchemaValidationError,
    SettingSchema,
    cache,
    field,
    keyed_per_entity,
    register_schema,
    schema_key,
    singleton,
    unregister_schema,
)


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin ``GRAPH_DB`` to a fresh per-test SQLite file."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    yield db_path


@pytest.fixture
def upsert_schema():
    """Register a small keyed-per-entity schema with one optional field.

    The schema lives only for the test that uses this fixture so other
    tests don't see leftover registrations. Cleared on teardown via
    :func:`unregister_schema`.
    """
    set_id = f"x.upsert.{uuid4().hex[:8]}"

    @keyed_per_entity
    class V1(SettingSchema):
        set_id_local = set_id  # avoid clobbering the class attribute below
        schema_revision = 1

        name: str = field(required=True)
        note: str = field(default="")

        @classmethod
        def validate(cls, payload):
            if not isinstance(payload, dict):
                raise SchemaValidationError("payload must be a dict")
            if not isinstance(payload.get("name"), str) or not payload["name"]:
                raise SchemaValidationError("missing required 'name'")
            if "note" in payload and not isinstance(payload["note"], str):
                raise SchemaValidationError("'note' must be a string")
            extra = set(payload) - {"name", "note"}
            if extra:
                raise SchemaValidationError(
                    f"unknown field(s): {sorted(extra)}"
                )

    V1.set_id = set_id
    register_schema(set_id, 1, V1)
    try:
        yield set_id, V1
    finally:
        unregister_schema(set_id, 1)


@pytest.fixture
def cache_schema():
    """Register a ``@cache``-decorated schema with a 60s TTL.

    ``upsert_by_key`` must restamp ``expires_at`` on every write for
    cache schemas (acceptance #4). 60 seconds is enough that
    ``cache_expires_at`` produces a non-NULL stamp and the test can
    detect changes between writes scheduled microseconds apart.
    """
    set_id = f"x.cache.{uuid4().hex[:8]}"

    @cache(ttl=timedelta(minutes=1))
    class V1(SettingSchema):
        schema_revision = 1
        name: str = field(required=True)

        @classmethod
        def validate(cls, payload):
            if not isinstance(payload, dict):
                raise SchemaValidationError("payload must be a dict")
            if not isinstance(payload.get("name"), str) or not payload["name"]:
                raise SchemaValidationError("missing 'name'")

    V1.set_id = set_id
    register_schema(set_id, 1, V1)
    try:
        yield set_id, V1
    finally:
        unregister_schema(set_id, 1)


@pytest.fixture
def hook_recorder():
    """Capture every emit-hook call. Guarantees teardown clears the hook
    so test order can't leak a recording closure into the registry.
    """
    calls: list[dict] = []

    def hook(*, operation, snapshot, org):
        calls.append({"operation": operation, "snapshot": dict(snapshot), "org": org})

    settings_ops.set_emit_hook(hook)
    try:
        yield calls
    finally:
        settings_ops.set_emit_hook(None)


# ── Helpers ──────────────────────────────────────────────────


def _all_base_rows(set_id: str, key: str) -> list[dict]:
    """Return every base row (``supersedes IS NULL AND excludes IS NULL``)
    for ``(set_id, key)`` as plain dicts. Used to verify the upsert
    invariant: at most one base row per composite key after migration
    (legacy duplicates aside).
    """
    db = settings_ops._open(None)
    try:
        rows = db.conn.execute(
            "SELECT * FROM settings WHERE set_id = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL "
            "ORDER BY created_at, id",
            (set_id, key),
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


# ── Acceptance #1: import surface ────────────────────────────


def test_upsert_by_key_is_importable():
    """The bead's contract requires the canonical import path."""
    from tools.graph.settings_ops import upsert_by_key as imported
    assert imported is settings_ops.upsert_by_key


# ── Acceptance #2: insert path ───────────────────────────────


def test_upsert_inserts_when_no_row_exists(graph_db_env, upsert_schema):
    set_id, _ = upsert_schema

    sid = settings_ops.upsert_by_key(
        set_id, 1, "alpha", {"name": "first", "note": "hello"},
     org=settings_ops.CALLER_ORG)

    rows = _all_base_rows(set_id, "alpha")
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert json.loads(rows[0]["payload"]) == {"name": "first", "note": "hello"}
    assert rows[0]["publication_state"] == "raw"


def test_upsert_returns_string_setting_id(graph_db_env, upsert_schema):
    set_id, _ = upsert_schema
    sid = settings_ops.upsert_by_key(set_id, 1, "k", {"name": "x"}, org=settings_ops.CALLER_ORG)
    assert isinstance(sid, str)
    assert len(sid) >= 32  # uuid4-ish


def test_upsert_respects_state_kwarg(graph_db_env, upsert_schema):
    set_id, _ = upsert_schema
    sid = settings_ops.upsert_by_key(
        set_id, 1, "k", {"name": "x"}, state="published",
     org=settings_ops.CALLER_ORG)
    rows = _all_base_rows(set_id, "k")
    assert rows[0]["publication_state"] == "published"
    assert rows[0]["id"] == sid


def test_upsert_rejects_unknown_state(graph_db_env, upsert_schema):
    set_id, _ = upsert_schema
    with pytest.raises(ValueError):
        settings_ops.upsert_by_key(
            set_id, 1, "k", {"name": "x"}, state="bogus",
         org=settings_ops.CALLER_ORG)


# ── Acceptance #3: update-in-place path ──────────────────────


def test_upsert_updates_in_place_preserving_id(graph_db_env, upsert_schema):
    set_id, _ = upsert_schema

    sid_first = settings_ops.upsert_by_key(
        set_id, 1, "alpha", {"name": "first"},
     org=settings_ops.CALLER_ORG)
    sid_second = settings_ops.upsert_by_key(
        set_id, 1, "alpha", {"name": "second", "note": "updated"},
     org=settings_ops.CALLER_ORG)

    assert sid_second == sid_first
    rows = _all_base_rows(set_id, "alpha")
    assert len(rows) == 1
    assert rows[0]["id"] == sid_first
    assert json.loads(rows[0]["payload"]) == {
        "name": "second", "note": "updated",
    }


def test_upsert_updates_publication_state_on_existing_row(
    graph_db_env, upsert_schema,
):
    """``state=`` on the second call rewrites the row's publication state."""
    set_id, _ = upsert_schema
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "x"}, state="raw", org=settings_ops.CALLER_ORG)
    settings_ops.upsert_by_key(
        set_id, 1, "k", {"name": "x"}, state="curated",
     org=settings_ops.CALLER_ORG)
    rows = _all_base_rows(set_id, "k")
    assert len(rows) == 1
    assert rows[0]["publication_state"] == "curated"


def test_upsert_updates_updated_at_on_each_call(
    graph_db_env, upsert_schema, monkeypatch,
):
    """``updated_at`` advances on every upsert; ``created_at`` does not."""
    set_id, _ = upsert_schema

    timestamps = iter([
        "2026-05-01T10:00:00Z",
        "2026-05-01T10:00:30Z",
        "2026-05-01T10:01:00Z",
    ])
    monkeypatch.setattr(settings_ops, "_now_iso", lambda: next(timestamps))

    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v1"}, org=settings_ops.CALLER_ORG)
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v2"}, org=settings_ops.CALLER_ORG)
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v3"}, org=settings_ops.CALLER_ORG)

    rows = _all_base_rows(set_id, "k")
    assert len(rows) == 1
    assert rows[0]["created_at"] == "2026-05-01T10:00:00Z"
    assert rows[0]["updated_at"] == "2026-05-01T10:01:00Z"


def test_upsert_keys_are_independent(graph_db_env, upsert_schema):
    """Two different keys produce two different rows with distinct ids."""
    set_id, _ = upsert_schema
    a_id = settings_ops.upsert_by_key(set_id, 1, "alpha", {"name": "a"}, org=settings_ops.CALLER_ORG)
    b_id = settings_ops.upsert_by_key(set_id, 1, "beta", {"name": "b"}, org=settings_ops.CALLER_ORG)
    assert a_id != b_id
    assert len(_all_base_rows(set_id, "alpha")) == 1
    assert len(_all_base_rows(set_id, "beta")) == 1


# ── Acceptance #4: TTL restamping for @cache schemas ─────────


def test_upsert_stamps_expires_at_for_cache_schema(
    graph_db_env, cache_schema, monkeypatch,
):
    set_id, _ = cache_schema
    monkeypatch.setattr(
        settings_ops, "_now_iso", lambda: "2026-05-01T00:00:00Z",
    )
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "x"}, org=settings_ops.CALLER_ORG)
    rows = _all_base_rows(set_id, "k")
    assert rows[0]["expires_at"] == "2026-05-01T00:01:00Z"


def test_upsert_restamps_expires_at_on_update(
    graph_db_env, cache_schema, monkeypatch,
):
    """The bead's invariant #3: TTL restamps on every upsert."""
    set_id, _ = cache_schema
    times = iter([
        "2026-05-01T00:00:00Z",
        "2026-05-01T00:00:30Z",
    ])
    monkeypatch.setattr(settings_ops, "_now_iso", lambda: next(times))

    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v1"}, org=settings_ops.CALLER_ORG)
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v2"}, org=settings_ops.CALLER_ORG)

    rows = _all_base_rows(set_id, "k")
    assert len(rows) == 1
    # Sliding window: TTL is 60s from the latest updated_at.
    assert rows[0]["updated_at"] == "2026-05-01T00:00:30Z"
    assert rows[0]["expires_at"] == "2026-05-01T00:01:30Z"


def test_upsert_leaves_expires_at_null_for_non_cache_schema(
    graph_db_env, upsert_schema,
):
    set_id, _ = upsert_schema
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "x"}, org=settings_ops.CALLER_ORG)
    rows = _all_base_rows(set_id, "k")
    assert rows[0]["expires_at"] is None


# ── Acceptance #5: exactly one emit per upsert ───────────────


def test_emit_hook_fires_once_on_insert(
    graph_db_env, upsert_schema, hook_recorder,
):
    set_id, _ = upsert_schema
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "x"}, org=settings_ops.CALLER_ORG)
    assert len(hook_recorder) == 1
    call = hook_recorder[0]
    assert call["operation"] == "write"
    assert call["snapshot"]["set_id"] == set_id
    assert call["snapshot"]["key"] == "k"
    assert call["snapshot"]["publication_state"] == "raw"
    assert call["snapshot"]["deprecated"] is False


def test_emit_hook_fires_once_on_update(
    graph_db_env, upsert_schema, hook_recorder,
):
    set_id, _ = upsert_schema
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v1"}, org=settings_ops.CALLER_ORG)
    settings_ops.upsert_by_key(set_id, 1, "k", {"name": "v2"}, org=settings_ops.CALLER_ORG)
    # Two upserts → exactly two hook calls (no double-fire on update,
    # no missing-fire on insert).
    assert len(hook_recorder) == 2
    assert all(c["operation"] == "write" for c in hook_recorder)


def test_emit_hook_swallows_exceptions(graph_db_env, upsert_schema):
    """A broken subscriber must not corrupt the write."""
    def boom(**kw):
        raise RuntimeError("boom")

    settings_ops.set_emit_hook(boom)
    try:
        sid = settings_ops.upsert_by_key(
            set_id=upsert_schema[0], schema_revision=1, key="k",
            payload={"name": "x"},
         org=settings_ops.CALLER_ORG)
    finally:
        settings_ops.set_emit_hook(None)
    assert len(_all_base_rows(upsert_schema[0], "k")) == 1
    assert isinstance(sid, str)


# ── Acceptance #6: legacy duplicates — RETIRED ───────────────
#
# test_upsert_picks_newest_when_legacy_duplicates_exist asserted the
# recency tiebreak over same-publication_state duplicate base rows. The
# one-live-base-row index (idx_settings_one_base, f5c20ebd) forbids
# constructing that state, and there is no dedupe migration: a pre-index
# DB still holding such duplicates fails at index creation on open and
# never reaches the resolver. The settings owner ruled the scenario
# unreachable (2026-08-13): no openable DB exists in which the tiebreak
# runs. Precedence across DIFFERENT publication states remains live and
# keeps its tests.


def test_upsert_ignores_override_and_exclude_rows(
    graph_db_env, upsert_schema, monkeypatch,
):
    """Override (``supersedes``) and exclude (``excludes``) rows are not
    'the writable base.' An upsert must SELECT past them and update the
    base directly, never an override row.
    """
    set_id, _ = upsert_schema

    base_id = settings_ops.add_setting(set_id, 1, "k", {"name": "base"}, org=settings_ops.CALLER_ORG)
    # Override + exclude. exclude_setting raises if the target schema
    # isn't keyed-per-entity-friendly; override is simpler.
    settings_ops.override_setting(base_id, {"note": "patched"}, org=settings_ops.CALLER_ORG)

    sid = settings_ops.upsert_by_key(set_id, 1, "k", {"name": "rewritten"}, org=settings_ops.CALLER_ORG)
    assert sid == base_id
    rows = _all_base_rows(set_id, "k")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"]) == {"name": "rewritten"}


# ── Acceptance #7: validation runs first ─────────────────────


def test_upsert_validates_payload_before_writing(
    graph_db_env, upsert_schema,
):
    """A bad payload must reject the call before any row is written."""
    set_id, _ = upsert_schema
    with pytest.raises(SchemaValidationError):
        settings_ops.upsert_by_key(
            set_id, 1, "k", {"unknown": "field"},
         org=settings_ops.CALLER_ORG)
    assert _all_base_rows(set_id, "k") == []


def test_upsert_raises_for_unknown_schema(graph_db_env):
    """Schema not registered → :class:`SchemaValidationError` (mirroring
    :func:`add_setting`)."""
    with pytest.raises(SchemaValidationError):
        settings_ops.upsert_by_key(
            "x.never_registered", 1, "k", {"name": "x"},
         org=settings_ops.CALLER_ORG)


def test_upsert_validation_failure_does_not_fire_hook(
    graph_db_env, upsert_schema, hook_recorder,
):
    """Pre-write validation failure must NOT fire the post-commit hook."""
    set_id, _ = upsert_schema
    with pytest.raises(SchemaValidationError):
        settings_ops.upsert_by_key(set_id, 1, "k", {"unknown": True}, org=settings_ops.CALLER_ORG)
    assert hook_recorder == []


def test_upsert_validation_on_update_keeps_old_payload(
    graph_db_env, upsert_schema,
):
    """A second upsert with a bad payload must not corrupt the existing
    row — validation runs before SELECT/UPDATE.
    """
    set_id, _ = upsert_schema
    sid = settings_ops.upsert_by_key(set_id, 1, "k", {"name": "good"}, org=settings_ops.CALLER_ORG)
    with pytest.raises(SchemaValidationError):
        settings_ops.upsert_by_key(set_id, 1, "k", {"unknown": "field"}, org=settings_ops.CALLER_ORG)
    rows = _all_base_rows(set_id, "k")
    assert rows[0]["id"] == sid
    assert json.loads(rows[0]["payload"]) == {"name": "good"}


# ── Acceptance #8: concurrent writers in one process ─────────


def test_upsert_concurrent_writers_produce_one_final_row(
    graph_db_env, upsert_schema,
):
    """``BEGIN IMMEDIATE`` serializes concurrent writers in the same
    process. After 16 threads each hammer the same composite key, the
    DB must contain exactly one base row (no duplicate inserts) and the
    surviving id must equal the value returned by every thread.
    """
    set_id, _ = upsert_schema
    barrier = threading.Barrier(16)
    results: list[str] = []
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            sid = settings_ops.upsert_by_key(
                set_id, 1, "race-key", {"name": f"writer-{i}"},
             org=settings_ops.CALLER_ORG)
            with results_lock:
                results.append(sid)
        except BaseException as e:  # noqa: BLE001 — re-surfaced below
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"upsert-w-{i}")
        for i in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"worker threads raised: {errors!r}"

    rows = _all_base_rows(set_id, "race-key")
    assert len(rows) == 1, (
        f"expected exactly one base row after concurrent upserts, "
        f"got {len(rows)}: {rows}"
    )
    surviving_id = rows[0]["id"]
    # Every successful call returned the same surviving id — first
    # writer inserted, the rest updated that row in place.
    assert set(results) == {surviving_id}
    # The payload reflects one of the writers (whichever committed last).
    payload = json.loads(rows[0]["payload"])
    assert payload["name"].startswith("writer-")


# ── Acceptance #9: Surface Presence migration ────────────────


def test_presence_writes_route_through_upsert(graph_db_env):
    """The migrated :class:`Presence` writes a single base row per
    ``(surface, participant)`` even after multiple state changes.
    """
    from tools.graph.surface import (
        SURFACE_PRESENCE_SET_ID,
        Presence,
    )
    with Presence(
        surface_id="surf",
        participant_kind="agent",
        participant_id="agent-1",
        label="A1",
        heartbeat_interval=1000,  # disable heartbeat noise
    ) as p:
        p.set_state(
            "working", position_kind="tile",
            position_value="tile-9", intent="reviewing",
        )
        p.set_state(
            "present", position_kind="none",
            position_value="", intent="",
        )

    rows = _all_base_rows(SURFACE_PRESENCE_SET_ID, "surf:agent-1")
    assert len(rows) == 1, (
        f"expected one base row across enter+set_state+exit, got {rows}"
    )


def test_presence_concurrent_heartbeat_collisions_stay_one_row(graph_db_env):
    """Simulate heartbeat-collision substrate.A: two writes within the
    same second go through ``_write_presence`` from different threads.
    The migrated path keeps one base row.
    """
    from tools.graph.surface import (
        SURFACE_PRESENCE_SET_ID,
        Presence,
    )
    p = Presence(
        surface_id="surf",
        participant_kind="agent",
        participant_id="agent-X",
        label="AX",
        heartbeat_interval=1000,
    )
    p.__enter__()
    try:
        barrier = threading.Barrier(8)

        def beat() -> None:
            barrier.wait(timeout=5)
            p._write_presence()

        threads = [threading.Thread(target=beat) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    finally:
        p.__exit__(None, None, None)

    rows = _all_base_rows(SURFACE_PRESENCE_SET_ID, "surf:agent-X")
    assert len(rows) == 1


# ── Acceptance #10: OperatorActivity migration ───────────────


def test_operator_activity_singleton_stays_one_row(graph_db_env):
    """``_record_operator_input`` previously appended; the migration
    keeps the singleton row a single row across many writes.
    """
    from tools.dashboard.session_monitor import _record_operator_input
    from tools.graph.surface import OPERATOR_ACTIVITY_SET_ID

    for ts in (
        "2026-05-01T10:00:00Z",
        "2026-05-01T10:00:01Z",
        "2026-05-01T10:00:02Z",
        "2026-05-01T10:00:30Z",
        "2026-05-01T10:01:00Z",
    ):
        _record_operator_input(ts)

    rows = _all_base_rows(OPERATOR_ACTIVITY_SET_ID, "operator")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    # Latest write wins — ``last_input_at`` reflects the final call.
    assert payload["last_input_at"] == "2026-05-01T10:01:00Z"
