"""Tests for the dashboard settings-mediator substrate (bead auto-f93wj).

Covers:

* Registration via ``register_action`` and the decorator sugar.
* Dispatch on new rows + payload contents reach the handler.
* Predicate filtering (``filtered`` markers, no fn invocation).
* Idempotency on re-poll (marker prevents duplicate dispatch).
* Restart-resume from cursor (rows written during downtime processed
  exactly once each, in order).
* Per-action failure semantics (``failed`` marker, loop continues).
* Multi-handler-per-set fan-out with independent markers.
* Schemas registered + introspectable via the registry surface.
* Graceful drain on stop — in-flight handler completes.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tools.graph import ops, settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS

from tools.dashboard import settings_mediator
from tools.dashboard.settings_mediator import (
    CURSOR_SET_ID,
    HEALTH,
    STATE_SET_ID,
    MediatorHealth,
    Row,
    Services,
    iterate_once,
    register_action,
    register_action_decorator,
    start_action_loop,
    stop_action_loop,
)
from tools.dashboard.settings_mediator.loop import (
    REGISTRY,
    _marker_key,
    _marker_exists,
)


TEST_SET_ID = "dashboard.test.action-fixture"
TEST_SET_REVISION = 1


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    """Snapshot + restore the global schema registry around each test."""
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _isolate_action_registry():
    """Reset the in-process action registry around each test.

    The registry is a module-level list that survives import cycles —
    test isolation requires a fresh slate per test so a handler from
    one test never fires on another's rows. The same applies to
    ``HEALTH`` — its dict counters survive across tests by default
    and would smear handler-name keys between tests.
    """
    settings_mediator.clear_registry()
    settings_mediator.reset_health()
    yield
    settings_mediator.clear_registry()
    settings_mediator.reset_health()


@pytest.fixture
def fixture_schema():
    """Permissive schema for the test fixture set so add_setting works."""
    from tools.graph.schemas.registry import (
        SettingSchema, register_schema, schema_key,
    )

    class TestActionV1(SettingSchema):
        set_id = TEST_SET_ID
        schema_revision = TEST_SET_REVISION

    register_schema(TEST_SET_ID, TEST_SET_REVISION, TestActionV1)
    return TestActionV1


@pytest.fixture
def services():
    """Stub :class:`Services` — none of the unit tests touch tmux."""
    sent: list[tuple[str, str]] = []

    async def _send(session: str, text: str) -> None:
        sent.append((session, text))

    async def _find(role: str) -> str | None:
        return None

    svc = Services(session_send=_send, find_session_by_role=_find)
    svc._sent = sent  # test introspection
    return svc


# ── Helpers ──────────────────────────────────────────────────────────


def _add_row(payload: dict, *, key: str | None = None) -> str:
    """Insert a row into the test fixture set; returns the new Setting id."""
    return ops.add_setting(
        TEST_SET_ID,
        TEST_SET_REVISION,
        key or f"k-{int(time.time_ns())}",
        payload,
    )


def _markers_for(set_id: str, row_id: str) -> list[dict]:
    """Return all marker payloads for ``(set_id, row_id)`` regardless of action."""
    members = settings_ops.read_set(STATE_SET_ID)
    prefix = f"{set_id}:{row_id}:"
    out = []
    for m in members.members:
        if m.key.startswith(prefix):
            out.append({"key": m.key, "payload": m.payload})
    return out


def _read_cursor_payload(set_id: str) -> dict | None:
    members = settings_ops.read_set(CURSOR_SET_ID)
    for m in members.members:
        if m.key == set_id:
            return dict(m.payload) if isinstance(m.payload, dict) else None
    return None


# ── Tests ────────────────────────────────────────────────────────────


def test_schemas_registered_at_import():
    """Cursor + state schemas are wired into the registry at module import."""
    from tools.graph.schemas.registry import SCHEMAS, schema_key

    assert schema_key(CURSOR_SET_ID, 1) in SCHEMAS
    assert schema_key(STATE_SET_ID, 1) in SCHEMAS

    cursor_cls = SCHEMAS[schema_key(CURSOR_SET_ID, 1)]
    js = cursor_cls.export_json_schema()
    assert "lastRowId" in js["properties"]
    assert "lastSeenAt" in js["properties"]
    assert set(js["required"]) == {"lastRowId", "lastSeenAt"}

    state_cls = SCHEMAS[schema_key(STATE_SET_ID, 1)]
    js = state_cls.export_json_schema()
    assert "processedAt" in js["properties"]
    assert "status" in js["properties"]
    # error is optional — not in required
    assert set(js["required"]) == {"processedAt", "status"}


def test_register_action_appends_to_registry():
    """``register_action`` plus the decorator both populate REGISTRY."""

    async def h1(row, svc):
        pass

    register_action("dashboard.foo", h1, name="h1")

    @register_action_decorator("dashboard.bar", name="h2")
    async def h2(row, svc):
        pass

    names = {a.name for a in REGISTRY}
    assert names == {"h1", "h2"}
    set_ids = {a.set_id for a in REGISTRY}
    assert set_ids == {"dashboard.foo", "dashboard.bar"}


@pytest.mark.asyncio
async def test_dispatch_on_new_row(graph_db_env, fixture_schema, services):
    """Acceptance #1 — new row dispatches handler with Row + Services."""
    seen: list[Row] = []
    services_seen: list[Services] = []

    async def handler(row, svc):
        seen.append(row)
        services_seen.append(svc)

    register_action(TEST_SET_ID, handler, name="dispatch-1")

    sid = _add_row({"hello": "world"}, key="alpha")

    await iterate_once(services)

    assert len(seen) == 1
    row = seen[0]
    assert isinstance(row, Row)
    assert row.id == sid
    assert row.set_id == TEST_SET_ID
    assert row.key == "alpha"
    assert row.payload == {"hello": "world"}
    assert row["hello"] == "world"  # mapping access
    assert "hello" in row
    assert row.get("missing", "default") == "default"
    assert services_seen[0] is services


@pytest.mark.asyncio
async def test_idempotency_on_repoll(graph_db_env, fixture_schema, services):
    """Acceptance #2 — re-running the same row does not re-invoke."""
    calls: list[str] = []

    async def handler(row, svc):
        calls.append(row.id)

    register_action(TEST_SET_ID, handler, name="dispatch-2")

    sid = _add_row({"x": 1}, key="alpha")
    await iterate_once(services)
    assert calls == [sid]

    # Manually roll the cursor back so the row appears "new" again. The
    # marker should still prevent re-dispatch.
    settings_mediator.loop._write_cursor(TEST_SET_ID, "", "")
    await iterate_once(services)
    assert calls == [sid], "marker should have prevented re-dispatch"

    # And a clean re-poll without rewinding is also a no-op.
    await iterate_once(services)
    assert calls == [sid]


@pytest.mark.asyncio
async def test_predicate_filters_rows(graph_db_env, fixture_schema, services):
    """Acceptance #3 — predicate-rejected rows do not invoke fn."""
    invoked: list[dict] = []

    async def handler(row, svc):
        invoked.append(dict(row.payload))

    register_action(
        TEST_SET_ID, handler,
        predicate=lambda r: r["kind"] == "foo",
        name="dispatch-3",
    )

    foo_id = _add_row({"kind": "foo", "x": 1}, key="k1")
    bar_id = _add_row({"kind": "bar", "x": 2}, key="k2")
    foo2_id = _add_row({"kind": "foo", "x": 3}, key="k3")

    await iterate_once(services)

    # Both foo rows ran; bar didn't.
    xs = sorted(p["x"] for p in invoked)
    assert xs == [1, 3]
    assert all(p["kind"] == "foo" for p in invoked)

    # Filtered row gets a 'filtered' marker so the predicate isn't
    # re-evaluated next tick — and no further dispatch happens.
    bar_markers = _markers_for(TEST_SET_ID, bar_id)
    assert len(bar_markers) == 1
    assert bar_markers[0]["payload"]["status"] == "filtered"

    # Cursor advanced past every row: ``lastRowId`` is whichever row
    # sorts last by (created_at, id). Second-resolution ``created_at``
    # means insertion order does not necessarily match dispatch order
    # — but every inserted row should be at-or-before the cursor.
    cursor = _read_cursor_payload(TEST_SET_ID)
    assert cursor is not None
    all_ids = {foo_id, bar_id, foo2_id}
    assert cursor["lastRowId"] in all_ids

    # Re-running is a no-op — markers prevent any handler invocation.
    await iterate_once(services)
    assert sorted(p["x"] for p in invoked) == [1, 3]


@pytest.mark.asyncio
async def test_multi_handler_fanout(graph_db_env, fixture_schema, services):
    """Acceptance #4 — two handlers on the same set both run, markers independent."""
    h1_calls: list[str] = []
    h2_calls: list[str] = []

    async def h1(row, svc):
        h1_calls.append(row.id)

    async def h2(row, svc):
        h2_calls.append(row.id)

    register_action(TEST_SET_ID, h1, name="handler-A")
    register_action(TEST_SET_ID, h2, name="handler-B")

    sid = _add_row({"k": 1}, key="k")
    await iterate_once(services)

    assert h1_calls == [sid]
    assert h2_calls == [sid]

    # Both markers exist with distinct keys.
    assert _marker_exists(_marker_key(TEST_SET_ID, sid, "handler-A"))
    assert _marker_exists(_marker_key(TEST_SET_ID, sid, "handler-B"))

    # And re-running doesn't trigger either handler again.
    await iterate_once(services)
    assert h1_calls == [sid]
    assert h2_calls == [sid]


@pytest.mark.asyncio
async def test_restart_resume_from_cursor(
    graph_db_env, fixture_schema, services,
):
    """Acceptance #5 — rows written during 'downtime' are processed exactly once.

    ``created_at`` is second-resolution so insertion order does not
    necessarily match dispatch order; we sleep between batches to push
    the second batch into a later second and verify "downtime" rows
    process strictly after the pre-existing ones.
    """
    seen: list[str] = []

    async def handler(row, svc):
        seen.append(row.key)

    register_action(TEST_SET_ID, handler, name="resume")

    # First batch — process them.
    _add_row({}, key="row-1")
    _add_row({}, key="row-2")
    await iterate_once(services)
    assert sorted(seen) == ["row-1", "row-2"]
    seen_after_first = list(seen)

    # Simulate "downtime" — clear the in-process registry as if the
    # process exited, then re-register the same handler. (Cursor +
    # markers persist in the DB.) The sleep guarantees the next batch
    # has a strictly larger ``created_at`` so dispatch order is
    # well-defined across batches.
    settings_mediator.clear_registry()
    register_action(TEST_SET_ID, handler, name="resume")
    time.sleep(1.1)

    # Add new rows during "downtime".
    _add_row({}, key="row-3")
    _add_row({}, key="row-4")

    await iterate_once(services)
    assert sorted(seen) == ["row-1", "row-2", "row-3", "row-4"]
    # The downtime rows were appended to the original sequence — they
    # weren't reprocessed.
    assert seen[: len(seen_after_first)] == seen_after_first

    # Re-run is still a no-op.
    await iterate_once(services)
    assert sorted(seen) == ["row-1", "row-2", "row-3", "row-4"]


@pytest.mark.asyncio
async def test_failure_records_marker_and_continues(
    graph_db_env, fixture_schema, services,
):
    """Acceptance #6 — handler exception → 'failed' marker; subsequent rows run."""
    seen_ok: list[str] = []

    async def handler(row, svc):
        if row["fail"]:
            raise RuntimeError("boom: handler-side")
        seen_ok.append(row.key)

    register_action(TEST_SET_ID, handler, name="fail-action")

    bad_id = _add_row({"fail": True}, key="bad")
    good_id = _add_row({"fail": False}, key="good")

    await iterate_once(services)

    # Subsequent row still ran.
    assert seen_ok == ["good"]

    bad_markers = _markers_for(TEST_SET_ID, bad_id)
    assert len(bad_markers) == 1
    assert bad_markers[0]["payload"]["status"] == "failed"
    assert "boom" in bad_markers[0]["payload"]["error"]

    good_markers = _markers_for(TEST_SET_ID, good_id)
    assert len(good_markers) == 1
    assert good_markers[0]["payload"]["status"] == "ok"

    # Cursor advanced past every row. No auto-retry on the failed
    # row. (Ordering is by (created_at, id) — the trailing row id
    # depends on UUID tiebreaks when timestamps tie.)
    cursor = _read_cursor_payload(TEST_SET_ID)
    assert cursor is not None
    assert cursor["lastRowId"] in {bad_id, good_id}

    # Re-running does not re-invoke (failure marker still suppresses).
    await iterate_once(services)
    assert seen_ok == ["good"]


@pytest.mark.asyncio
async def test_predicate_exception_records_failure(
    graph_db_env, fixture_schema, services,
):
    """A predicate that raises is treated as a per-row failure, not a halt."""

    async def handler(row, svc):
        pass

    def boom_predicate(row):
        raise ValueError("predicate explosion")

    register_action(
        TEST_SET_ID, handler,
        predicate=boom_predicate, name="bad-pred",
    )

    sid = _add_row({"x": 1}, key="k")
    await iterate_once(services)

    markers = _markers_for(TEST_SET_ID, sid)
    assert len(markers) == 1
    assert markers[0]["payload"]["status"] == "failed"
    assert "predicate" in markers[0]["payload"]["error"]


@pytest.mark.asyncio
async def test_loop_starts_and_stops_gracefully(
    graph_db_env, fixture_schema, services,
):
    """Acceptance #7 — in-flight handler completes before stop returns."""
    handler_started = asyncio.Event()
    handler_done = asyncio.Event()

    async def handler(row, svc):
        handler_started.set()
        # Simulate work that crosses the stop boundary.
        await asyncio.sleep(0.2)
        handler_done.set()

    register_action(TEST_SET_ID, handler, name="graceful")

    _add_row({"x": 1}, key="k1")

    # Start the loop with a fast poll so it picks up the row promptly.
    start_action_loop(services, poll_seconds=0.05)
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=3.0)
        # Initiate shutdown while handler is mid-flight.
        stop_task = asyncio.create_task(stop_action_loop())
        # Give stop a moment so the stop_event is set, but before the
        # handler completes; verify we wait for the handler.
        await asyncio.sleep(0.05)
        assert not handler_done.is_set(), (
            "test setup: handler completed before we could observe drain"
        )
        await asyncio.wait_for(stop_task, timeout=3.0)
        # By the time stop_action_loop() returns, the handler has run
        # to completion.
        assert handler_done.is_set()
    finally:
        # Belt + suspenders: if the loop is still around (e.g., test
        # bailed early) make sure we tear it down before the next test.
        await stop_action_loop()


@pytest.mark.asyncio
async def test_loop_processes_rows_added_after_start(
    graph_db_env, fixture_schema, services,
):
    """End-to-end: loop running → write row → handler observes within poll window."""
    seen: list[str] = []
    seen_event = asyncio.Event()

    async def handler(row, svc):
        seen.append(row.key)
        seen_event.set()

    register_action(TEST_SET_ID, handler, name="post-start")

    start_action_loop(services, poll_seconds=0.05)
    try:
        # Add the row while the loop is running. The first tick may have
        # already happened with an empty set — that's fine, we want the
        # next tick to see the new row.
        await asyncio.sleep(0.1)
        _add_row({"k": 1}, key="post-start-key")
        await asyncio.wait_for(seen_event.wait(), timeout=3.0)
        assert "post-start-key" in seen
    finally:
        await stop_action_loop()


@pytest.mark.asyncio
async def test_no_registry_no_dispatch(graph_db_env, fixture_schema, services):
    """An empty registry is a no-op — no cursor written, no markers."""
    await iterate_once(services)
    assert _read_cursor_payload(TEST_SET_ID) is None


@pytest.mark.asyncio
async def test_default_action_name_uses_qualname(
    graph_db_env, fixture_schema, services,
):
    """Without ``name=``, the marker key includes ``fn.__qualname__``."""
    seen: list[str] = []

    async def my_named_handler(row, svc):
        seen.append(row.id)

    register_action(TEST_SET_ID, my_named_handler)

    sid = _add_row({"x": 1}, key="k")
    await iterate_once(services)

    assert seen == [sid]
    # The marker key encodes the qualname (or at least the function name).
    markers = _markers_for(TEST_SET_ID, sid)
    assert len(markers) == 1
    assert "my_named_handler" in markers[0]["key"]


# ── Heartbeat / diag (bead auto-3osk9) ───────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_last_tick_updates_each_iteration(
    graph_db_env, fixture_schema, services,
):
    """Acceptance: every loop iteration stamps ``last_tick_at``.

    Stamp happens at the top of :func:`iterate_once` — *before* any
    handler dispatch — so even an empty-registry tick advances the
    heartbeat. That's the whole point: a wedged loop is one whose
    timestamp stops moving regardless of registry contents.
    """
    assert HEALTH.last_tick_at == 0.0

    before = time.time()
    await iterate_once(services)
    after_first = HEALTH.last_tick_at
    assert after_first >= before

    # Second iteration produces a strictly-later tick.
    time.sleep(0.01)
    await iterate_once(services)
    assert HEALTH.last_tick_at > after_first


@pytest.mark.asyncio
async def test_heartbeat_event_arm_stamps(
    graph_db_env, fixture_schema, services,
):
    """``_bus_demuxer`` increments ``events_received_count`` and stamps timestamps."""
    from tools.dashboard.settings_mediator.loop import _bus_demuxer

    async def handler(row, svc):
        pass

    register_action(TEST_SET_ID, handler, name="bus-event-target")

    bus_queue: asyncio.Queue = asyncio.Queue()
    wakeup_event = asyncio.Event()
    stop_event = asyncio.Event()

    # Real event for our registered set_id (seq != 0).
    await bus_queue.put(
        ("setting.changed", {"set_id": TEST_SET_ID, "key": "k"}, 7)
    )
    # Replay event (seq == 0) — must not increment counter.
    await bus_queue.put(
        ("setting.changed", {"set_id": TEST_SET_ID, "key": "k"}, 0)
    )
    # Unrelated set_id — must not increment counter.
    await bus_queue.put(
        ("setting.changed", {"set_id": "dashboard.unregistered", "key": "k"}, 11)
    )
    # Wrong topic — must not increment counter.
    await bus_queue.put(
        ("other.topic", {"set_id": TEST_SET_ID, "key": "k"}, 12)
    )

    demuxer = asyncio.create_task(
        _bus_demuxer(bus_queue, wakeup_event, stop_event)
    )
    try:
        # Give the demuxer a tick to drain everything.
        await asyncio.wait_for(wakeup_event.wait(), timeout=1.0)
        # Spin once more so the last queued items are consumed.
        for _ in range(10):
            if bus_queue.empty():
                break
            await asyncio.sleep(0.01)
        assert HEALTH.events_received_count == 1
        assert HEALTH.last_event_received_at is not None
        assert HEALTH.last_event_received_set_id == TEST_SET_ID
    finally:
        stop_event.set()
        demuxer.cancel()
        try:
            await demuxer
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_heartbeat_handler_fired_and_succeeded(
    graph_db_env, fixture_schema, services,
):
    """Successful handler stamps both ``last_handler_fired_at`` + ``...succeeded_at``."""

    async def handler(row, svc):
        pass

    register_action(TEST_SET_ID, handler, name="heartbeat-ok")

    _add_row({"x": 1}, key="k")
    before = time.time()
    await iterate_once(services)

    assert "heartbeat-ok" in HEALTH.last_handler_fired_at
    assert HEALTH.last_handler_fired_at["heartbeat-ok"] >= before
    assert "heartbeat-ok" in HEALTH.last_handler_succeeded_at
    assert (
        HEALTH.last_handler_succeeded_at["heartbeat-ok"]
        >= HEALTH.last_handler_fired_at["heartbeat-ok"]
    )
    assert HEALTH.handlers_fired_count["heartbeat-ok"] == 1
    assert "heartbeat-ok" not in HEALTH.last_handler_error


@pytest.mark.asyncio
async def test_heartbeat_handler_error_recorded_loop_continues(
    graph_db_env, fixture_schema, services,
):
    """Handler exception stamps ``last_handler_error[name]`` without halting iteration."""

    async def boom(row, svc):
        raise RuntimeError("kaboom: deliberate test failure")

    async def good(row, svc):
        pass

    register_action(TEST_SET_ID, boom, name="heartbeat-bad")
    register_action(TEST_SET_ID, good, name="heartbeat-good")

    _add_row({"x": 1}, key="k1")
    _add_row({"x": 2}, key="k2")
    await iterate_once(services)

    # Failing handler: error stamped, fired_count still incremented.
    assert "heartbeat-bad" in HEALTH.last_handler_error
    assert "kaboom" in HEALTH.last_handler_error["heartbeat-bad"]
    assert HEALTH.handlers_fired_count["heartbeat-bad"] >= 1
    # Sibling handler still ran successfully — failure didn't block it.
    assert HEALTH.handlers_fired_count["heartbeat-good"] >= 1
    assert "heartbeat-good" in HEALTH.last_handler_succeeded_at


@pytest.mark.asyncio
async def test_heartbeat_cursor_positions_track_set_id(
    graph_db_env, fixture_schema, services,
):
    """Successful cursor write updates ``HEALTH.cursor_positions[set_id]``."""

    async def handler(row, svc):
        pass

    register_action(TEST_SET_ID, handler, name="cursor-track")

    sid = _add_row({"x": 1}, key="k")
    await iterate_once(services)

    assert TEST_SET_ID in HEALTH.cursor_positions
    # The stored value encodes the row id we just processed.
    assert sid in HEALTH.cursor_positions[TEST_SET_ID]


def test_health_to_dict_includes_age_and_registry():
    """``MediatorHealth.to_dict()`` exposes derived staleness + live registry."""

    async def handler(row, svc):
        pass

    register_action("dashboard.diag-test", handler, name="diag-h1")

    # Stamp a tick "60+ seconds ago" — the JSON view must surface
    # an age that crosses the staleness threshold per the spec's
    # L2.B assertion.
    HEALTH.last_tick_at = time.time() - 75.0
    HEALTH.events_received_count = 5
    HEALTH.last_event_received_set_id = "dashboard.diag-test"
    HEALTH.last_event_received_at = time.time() - 30.0
    HEALTH.cursor_positions["dashboard.diag-test"] = "2026-05-01T00:00:00Z:abc"

    snap = HEALTH.to_dict()

    assert snap["last_tick_age_s"] is not None
    assert snap["last_tick_age_s"] > 60.0, (
        "stale heartbeat must surface as a large last_tick_age_s"
    )
    assert snap["events_received_count"] == 5
    assert snap["last_event_received_set_id"] == "dashboard.diag-test"
    assert snap["cursor_positions"]["dashboard.diag-test"].endswith(":abc")
    # Registered actions are included so callers see the live registry.
    names = {a["name"] for a in snap["registered_actions"]}
    assert "diag-h1" in names


def test_health_to_dict_initial_state_is_jsonable():
    """Empty heartbeat round-trips through json.dumps (frontend-safe)."""
    import json as _json
    snap = HEALTH.to_dict()
    assert snap["last_tick_at"] is None
    assert snap["last_tick_age_s"] is None
    assert snap["events_received_count"] == 0
    assert snap["loop_started_at"] is None
    # Round-trip — the diag endpoint serialises this through JSONResponse.
    _json.loads(_json.dumps(snap))


def test_diag_settings_mediator_endpoint_returns_health(
    graph_db_env, fixture_schema, monkeypatch, tmp_path,
):
    """``GET /api/diag/settings_mediator`` returns the live HEALTH dict."""
    from starlette.testclient import TestClient
    import sqlite3

    db_path = tmp_path / "dashboard.db"
    # Mirror the schema the dashboard's init_db expects — bead_id +
    # resolution_dir backfills run on startup and otherwise blow up.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tmux_sessions (
            tmux_name TEXT PRIMARY KEY, session_uuid TEXT,
            graph_source_id TEXT, type TEXT NOT NULL, project TEXT NOT NULL,
            jsonl_path TEXT, bead_id TEXT, created_at REAL NOT NULL,
            is_live INTEGER DEFAULT 1, file_offset INTEGER DEFAULT 0,
            last_activity REAL, last_message TEXT DEFAULT '',
            entry_count INTEGER DEFAULT 0, context_tokens INTEGER DEFAULT 0,
            label TEXT DEFAULT '', topics TEXT DEFAULT '[]',
            role TEXT DEFAULT '', nag_enabled INTEGER DEFAULT 0,
            nag_interval INTEGER DEFAULT 15, nag_message TEXT DEFAULT '',
            nag_last_sent REAL DEFAULT 0, dispatch_nag INTEGER DEFAULT 0,
            resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
            curr_jsonl_file TEXT
        )"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv(
        "DASHBOARD_EVENT_BUS_STATE", str(tmp_path / "event_bus.state"),
    )

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import event_bus as event_bus_mod
    importlib.reload(event_bus_mod)
    from tools.dashboard import session_monitor as monitor_mod
    importlib.reload(monitor_mod)
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    with TestClient(server_mod.app) as client:
        # Seed HEALTH *after* the lifespan has started — otherwise the
        # mediator loop's startup tick clobbers our stale fixture and
        # `last_tick_age_s` reads ~0. Mutating inside the with-block
        # has the loop's first tick already in the past.
        HEALTH.last_tick_at = time.time() - 90.0
        HEALTH.events_received_count = 3
        HEALTH.last_handler_fired_at["coordinator_board.thumb_yes"] = (
            time.time() - 5.0
        )
        HEALTH.handlers_fired_count["coordinator_board.thumb_yes"] = 2
        resp = client.get("/api/diag/settings_mediator")

    assert resp.status_code == 200
    body = resp.json()
    # Core heartbeat fields present.
    for key in (
        "last_tick_at", "last_tick_age_s", "events_received_count",
        "last_event_received_at", "last_event_received_set_id",
        "last_watchdog_tick_at", "last_handler_fired_at",
        "last_handler_succeeded_at", "last_handler_error",
        "handlers_fired_count", "cursor_positions",
        "registered_actions", "loop_started_at",
    ):
        assert key in body, f"missing {key} in diag response"
    # L2.B: stale tick (older than 60s) is observable in the JSON
    # response — operators can spot a wedged loop without computing
    # the delta themselves.
    assert body["last_tick_age_s"] > 60.0, (
        "stale heartbeat (>60s) must surface in JSON response"
    )
    assert body["events_received_count"] == 3
    assert body["handlers_fired_count"]["coordinator_board.thumb_yes"] == 2


def test_reset_health_clears_all_fields():
    """``reset_health`` zeroes every field without rebinding the singleton."""
    HEALTH.last_tick_at = 12345.0
    HEALTH.events_received_count = 99
    HEALTH.last_handler_fired_at["x"] = 1.0
    HEALTH.last_handler_error["y"] = "boom"
    HEALTH.cursor_positions["dashboard.foo"] = "ts:row"
    HEALTH.loop_started_at = 100.0

    sentinel = HEALTH  # capture identity
    settings_mediator.reset_health()

    assert sentinel is HEALTH, "reset_health must mutate the singleton, not rebind"
    assert HEALTH.last_tick_at == 0.0
    assert HEALTH.events_received_count == 0
    assert HEALTH.last_handler_fired_at == {}
    assert HEALTH.last_handler_error == {}
    assert HEALTH.cursor_positions == {}
    assert HEALTH.loop_started_at is None
