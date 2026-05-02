"""Tests for the Surface Presence + ParticipantActivity substrate (v1).

Bead: ``auto-i3tki``. Covers:

* schema registration is eager (importing the module is enough),
* validation accepts the canonical payloads and rejects bad ones,
* :class:`Presence` context-manager lifecycle: writes initial row,
  starts heartbeat thread, joins on exit, writes final row,
* heartbeat thread cleanup on exit (no leaked thread),
* :meth:`Presence.participant_color` determinism + format,
* stub static helpers return safe defaults without raising.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SCHEMAS, SchemaValidationError, schema_key,
)
from tools.graph.surface import (
    PARTICIPANT_ACTIVITY_SET_ID,
    SCHEMA_REVISION,
    SURFACE_PING_SET_ID,
    SURFACE_PRESENCE_SET_ID,
    ParticipantActivityV1,
    Presence,
    SurfacePingV1,
    SurfacePresenceV1,
)


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("GRAPH_SCOPE", raising=False)
    yield db_path


# ── Schema registration ──────────────────────────────────────


def test_schemas_registered_on_import():
    """Acceptance #6/#7/#8: importing :mod:`tools.graph.surface`
    registers the three schemas. The module-level ``register_schema``
    calls run as side effects of importing — server.py's eager import
    keeps them in the registry before ``flush_schema_meta`` is called.
    """
    assert SCHEMAS.get(schema_key(SURFACE_PRESENCE_SET_ID, 1)) \
        is SurfacePresenceV1
    assert SCHEMAS.get(schema_key(SURFACE_PING_SET_ID, 1)) \
        is SurfacePingV1
    assert SCHEMAS.get(schema_key(PARTICIPANT_ACTIVITY_SET_ID, 1)) \
        is ParticipantActivityV1


def test_presence_decorated_keyed_per_entity():
    assert SurfacePresenceV1._access_pattern == "keyed_per_entity"


def test_ping_decorated_append_only_log():
    assert SurfacePingV1._access_pattern == "append_only_log"
    assert SurfacePingV1._key_strategy == "uuid_v4"


def test_activity_decorated_with_cache_ttl():
    """``@cache`` is its own access pattern (caller-supplied keys with
    TTL-driven GC). Stacking ``@keyed_per_entity`` on top would
    overwrite ``_access_pattern`` to ``"keyed_per_entity"`` and lose
    the cache semantics — so the schema uses ``@cache`` alone, the
    same shape as ``autonomy.source_control.review_state#1``.
    """
    assert ParticipantActivityV1._access_pattern == "cache"
    # 7 days = 604_800 seconds
    assert ParticipantActivityV1._cache_ttl_seconds == 7 * 24 * 60 * 60


def test_presence_field_metadata_present():
    """``graph set schema`` introspection requires a populated
    ``_field_metadata`` map.
    """
    fields = SurfacePresenceV1._field_metadata
    for required in (
        "surface_id", "participant_kind", "participant_id",
        "participant_label", "state", "heartbeat_at",
    ):
        assert required in fields, f"missing {required!r}"
        assert fields[required].get("required") is True


def test_synopsis_present_on_module():
    """``flush_schema_meta`` reads module-level SYNOPSIS at flush time;
    placement above ``register_schema`` is the documented pitfall.
    """
    from tools.graph import surface
    assert isinstance(surface.SYNOPSIS, dict)
    assert "summary" in surface.SYNOPSIS
    assert "nouns" in surface.SYNOPSIS


# ── Schema validation ────────────────────────────────────────


def _valid_presence_payload() -> dict:
    return {
        "surface_id": "test-surface",
        "participant_kind": "agent",
        "participant_id": "test-pid",
        "participant_label": "Test Agent",
        "accepts_pings": True,
        "state": "present",
        "position_kind": "none",
        "position_value": "",
        "intent": "",
        "heartbeat_at": "2026-05-02T12:00:00Z",
        "last_ping_id": "",
    }


def test_presence_validate_accepts_canonical_payload():
    SurfacePresenceV1.validate(_valid_presence_payload())


def test_presence_validate_rejects_bad_state():
    payload = _valid_presence_payload()
    payload["state"] = "away"  # not in the enum
    with pytest.raises(SchemaValidationError):
        SurfacePresenceV1.validate(payload)


def test_presence_validate_rejects_bad_participant_kind():
    payload = _valid_presence_payload()
    payload["participant_kind"] = "robot"
    with pytest.raises(SchemaValidationError):
        SurfacePresenceV1.validate(payload)


def test_presence_validate_rejects_bad_position_kind():
    payload = _valid_presence_payload()
    payload["position_kind"] = "elsewhere"
    with pytest.raises(SchemaValidationError):
        SurfacePresenceV1.validate(payload)


def test_presence_validate_rejects_missing_required():
    payload = _valid_presence_payload()
    del payload["surface_id"]
    with pytest.raises(SchemaValidationError):
        SurfacePresenceV1.validate(payload)


def test_presence_validate_rejects_unknown_field():
    payload = _valid_presence_payload()
    payload["color"] = "indigo"  # presentation belongs view-side
    with pytest.raises(SchemaValidationError):
        SurfacePresenceV1.validate(payload)


def _valid_ping_payload() -> dict:
    return {
        "surface_id": "test-surface",
        "from_participant_id": "alice",
        "to_participant_id": "bob",
        "position_kind": "tile",
        "position_value": "tile-7",
        "message": "look here",
        "sent_at": "2026-05-02T12:00:00Z",
    }


def test_ping_validate_accepts_canonical_payload():
    SurfacePingV1.validate(_valid_ping_payload())


def test_ping_validate_rejects_position_kind_none():
    """Pings cannot target ``position_kind='none'`` — pings are
    inherently directed at a specific place.
    """
    payload = _valid_ping_payload()
    payload["position_kind"] = "none"
    with pytest.raises(SchemaValidationError):
        SurfacePingV1.validate(payload)


def test_ping_validate_rejects_missing_to_participant_id():
    payload = _valid_ping_payload()
    del payload["to_participant_id"]
    with pytest.raises(SchemaValidationError):
        SurfacePingV1.validate(payload)


def _valid_activity_payload() -> dict:
    return {
        "participant_id": "alice",
        "participant_kind": "operator",
        "participant_label": "Alice",
        "last_user_input_at": "2026-05-02T12:00:00Z",
        "last_session_turn_at": "2026-05-02T11:55:00Z",
        "last_meaningful_at": "2026-05-02T12:00:00Z",
        "inputs_last_hour": 5,
        "turns_last_hour": 12,
    }


def test_activity_validate_accepts_canonical_payload():
    ParticipantActivityV1.validate(_valid_activity_payload())


def test_activity_validate_rejects_non_int_count():
    payload = _valid_activity_payload()
    payload["inputs_last_hour"] = "many"
    with pytest.raises(SchemaValidationError):
        ParticipantActivityV1.validate(payload)


# ── Presence context manager lifecycle ───────────────────────


def test_presence_writes_initial_row_on_enter(graph_db_env):
    """Acceptance #9: entering the context writes a row visible via
    :func:`read_set`.
    """
    with Presence(
        surface_id="test-surface",
        participant_kind="agent",
        participant_id="test-pid",
        label="Test Agent",
        heartbeat_interval=1000,  # disable heartbeat noise
    ):
        members = settings_ops.read_set(SURFACE_PRESENCE_SET_ID)
        keys = {m.key for m in members.members}
        assert "test-surface:test-pid" in keys


def test_presence_context_writes_final_row_on_exit(graph_db_env):
    """Acceptance #9: on exit, a row with state='present' is written.

    Inspect raw rows because read_set's tie-break collapses
    same-second writes nondeterministically (v1 substrate gap).
    """
    with Presence(
        surface_id="test-surface",
        participant_kind="agent",
        participant_id="test-pid",
        label="Test Agent",
        heartbeat_interval=1000,
    ) as p:
        p.set_state(
            "working", position_kind="tile",
            position_value="some-tile", intent="testing",
        )

    payloads = _read_payloads_for_key(
        SURFACE_PRESENCE_SET_ID, "test-surface:test-pid",
    )
    # Find the final write (state="present" with the position cleared
    # back to "none" — the unique signature of __exit__).
    finals = [
        p for p in payloads
        if p["state"] == "present" and p["position_kind"] == "none"
        and p["position_value"] == "" and p["intent"] == ""
    ]
    assert len(finals) >= 1, (
        f"expected at least one final-shape row, got {payloads}"
    )


def _read_payloads_for_key(set_id, key):
    """Return all payload dicts written to *set_id*/key, in insertion order.

    The substrate's v1 limitation is that ``read_set`` tie-breaks
    by ``created_at`` (second resolution), which is nondeterministic
    when two writes land in the same second. Tests that need to
    inspect what a specific call wrote query the raw rows directly.
    """
    import json
    db = settings_ops._open(None)
    try:
        rows = db.conn.execute(
            "SELECT payload FROM settings WHERE set_id = ? AND key = ? "
            "ORDER BY created_at, id",
            (set_id, key),
        ).fetchall()
    finally:
        db.close()
    return [json.loads(r["payload"]) for r in rows]


def test_presence_set_state_updates_payload(graph_db_env):
    with Presence(
        surface_id="surf",
        participant_kind="operator",
        participant_id="op-1",
        label="Op One",
        heartbeat_interval=1000,
    ) as p:
        p.set_state(
            "working", position_kind="tile",
            position_value="tile-99", intent="reviewing",
        )
        # Two rows: initial enter() + the set_state. Verify the
        # set_state payload directly. (read_set tie-break can pick
        # either when timestamps collide at second resolution — the
        # accepted v1 limitation per ``graph://dff97eec-c59`` gap #2.)
        payloads = _read_payloads_for_key(
            SURFACE_PRESENCE_SET_ID, "surf:op-1",
        )
        working_rows = [p for p in payloads if p["state"] == "working"]
        assert len(working_rows) == 1
        working = working_rows[0]
        assert working["position_kind"] == "tile"
        assert working["position_value"] == "tile-99"
        assert working["intent"] == "reviewing"


def test_presence_set_state_rejects_bad_state(graph_db_env):
    with Presence(
        surface_id="surf", participant_kind="agent",
        participant_id="a", label="A", heartbeat_interval=1000,
    ) as p:
        with pytest.raises(ValueError):
            p.set_state("bogus")


def test_presence_init_rejects_bad_kind():
    with pytest.raises(ValueError):
        Presence(
            surface_id="surf", participant_kind="robot",
            participant_id="a", label="A",
        )


def test_presence_acknowledge_ping_records_id(graph_db_env):
    with Presence(
        surface_id="surf", participant_kind="agent",
        participant_id="a", label="A", heartbeat_interval=1000,
    ) as p:
        p.acknowledge_ping("ping-uuid-123")
        # See note in _read_payloads_for_key for why we query raw rows.
        payloads = _read_payloads_for_key(
            SURFACE_PRESENCE_SET_ID, "surf:a",
        )
        ping_rows = [
            p for p in payloads if p["last_ping_id"] == "ping-uuid-123"
        ]
        assert len(ping_rows) == 1


def test_presence_summon_writes_ping_row(graph_db_env):
    with Presence(
        surface_id="surf", participant_kind="agent",
        participant_id="alice", label="Alice", heartbeat_interval=1000,
    ) as p:
        ping_id = p.summon(
            "bob", "tile", "tile-7", message="look here",
        )
        assert ping_id

    pings = settings_ops.read_set(SURFACE_PING_SET_ID)
    assert len(pings.members) == 1
    payload = pings.members[0].payload
    assert payload["from_participant_id"] == "alice"
    assert payload["to_participant_id"] == "bob"
    assert payload["position_kind"] == "tile"
    assert payload["position_value"] == "tile-7"
    assert payload["message"] == "look here"


def test_presence_summon_rejects_bad_position_kind(graph_db_env):
    with Presence(
        surface_id="surf", participant_kind="agent",
        participant_id="alice", label="Alice", heartbeat_interval=1000,
    ) as p:
        with pytest.raises(ValueError):
            p.summon("bob", "elsewhere", "x")


# ── Heartbeat thread cleanup ─────────────────────────────────


def test_heartbeat_thread_starts_and_stops_on_exit(graph_db_env):
    """The heartbeat thread must signal-stop and ``.join()`` cleanly on
    context exit so test isolation isn't broken by leaked threads.
    """
    threads_before = {t.ident for t in threading.enumerate()}

    p = Presence(
        surface_id="surf", participant_kind="agent",
        participant_id="a", label="A", heartbeat_interval=1000,
    )
    with p:
        assert p._heartbeat_thread is not None
        assert p._heartbeat_thread.is_alive()

    # After exit: thread is no longer alive.
    assert not p._heartbeat_thread.is_alive()

    # And no spurious threads were left behind.
    threads_after = {t.ident for t in threading.enumerate()}
    leaked = threads_after - threads_before
    assert not leaked, f"leaked threads: {leaked}"


def test_heartbeat_writes_row_after_interval(graph_db_env):
    """A short heartbeat interval triggers at least one extra write
    while inside the context — a smoke check that the loop actually
    runs.
    """
    with Presence(
        surface_id="surf", participant_kind="agent",
        participant_id="hb", label="HB", heartbeat_interval=0.05,
    ):
        # Wait long enough for at least a couple heartbeats to fire.
        threading.Event().wait(0.25)

    # We expect more than one row (the initial + one or more heartbeats
    # + the final). read_set collapses by key, so query the raw count
    # directly.
    db = settings_ops._open(None)
    try:
        rows = db.conn.execute(
            "SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
            "AND key = ?",
            (SURFACE_PRESENCE_SET_ID, "surf:hb"),
        ).fetchone()
    finally:
        db.close()
    # Initial + final = 2; heartbeat adds at least one more.
    assert rows["n"] >= 3


# ── Static helpers — read live ParticipantActivity rows ──────


def _write_activity(
    participant_id: str,
    *,
    last_user_input_at: str = "",
    last_session_turn_at: str = "",
    last_meaningful_at: str = "",
    inputs_last_hour: int = 0,
    turns_last_hour: int = 0,
    label: str = "Tester",
    kind: str = "agent",
) -> None:
    """Helper: write a ParticipantActivity row at *participant_id*."""
    settings_ops.add_setting(
        PARTICIPANT_ACTIVITY_SET_ID,
        SCHEMA_REVISION,
        participant_id,
        {
            "participant_id": participant_id,
            "participant_kind": kind,
            "participant_label": label,
            "last_user_input_at": last_user_input_at,
            "last_session_turn_at": last_session_turn_at,
            "last_meaningful_at": last_meaningful_at,
            "inputs_last_hour": inputs_last_hour,
            "turns_last_hour": turns_last_hour,
        },
        org="personal",
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_is_idle_no_row_returns_true(graph_db_env):
    """Acceptance #2: 'never seen' participants default to idle."""
    assert Presence.is_idle("never-written") is True


def test_is_idle_recent_input_returns_false(graph_db_env):
    """A user input within the threshold = not idle."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    _write_activity(
        "active-pid",
        last_user_input_at=_iso(now - timedelta(seconds=30)),
        last_meaningful_at=_iso(now - timedelta(seconds=30)),
        inputs_last_hour=1,
        turns_last_hour=1,
    )
    assert Presence.is_idle(
        "active-pid", threshold=timedelta(minutes=5),
    ) is False


def test_is_idle_stale_input_returns_true(graph_db_env):
    """Last input older than threshold = idle."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    _write_activity(
        "stale-pid",
        last_user_input_at=_iso(now - timedelta(hours=2)),
        last_meaningful_at=_iso(now - timedelta(hours=2)),
        inputs_last_hour=0,
        turns_last_hour=0,
    )
    assert Presence.is_idle(
        "stale-pid", threshold=timedelta(minutes=30),
    ) is True


def test_is_idle_empty_last_user_input_returns_true(graph_db_env):
    """A row with an empty ``last_user_input_at`` reads as idle —
    we've never seen the operator type into this session."""
    _write_activity("never-typed", last_user_input_at="")
    assert Presence.is_idle("never-typed") is True


def test_last_user_input_no_row_returns_none(graph_db_env):
    """No activity row → no last input."""
    assert Presence.last_user_input("never-written") is None


def test_last_user_input_returns_parsed_datetime(graph_db_env):
    """The ISO timestamp comes back as a tz-aware datetime."""
    from datetime import timezone
    when = datetime.now(timezone.utc).replace(microsecond=0)
    _write_activity("hit", last_user_input_at=_iso(when))
    got = Presence.last_user_input("hit")
    assert got is not None
    assert got.tzinfo is not None
    # Compare seconds (ISO precision strips microseconds).
    assert int(got.timestamp()) == int(when.timestamp())


def test_active_within_no_row_returns_false(graph_db_env):
    """Acceptance #4: an unknown participant is not 'active'."""
    assert Presence.active_within("unknown", timedelta(hours=1)) is False


def test_active_within_recent_meaningful_returns_true(graph_db_env):
    from datetime import timezone
    now = datetime.now(timezone.utc)
    _write_activity(
        "recent",
        last_meaningful_at=_iso(now - timedelta(seconds=10)),
    )
    assert Presence.active_within("recent", timedelta(minutes=1)) is True


def test_active_within_stale_returns_false(graph_db_env):
    from datetime import timezone
    now = datetime.now(timezone.utc)
    _write_activity(
        "stale",
        last_meaningful_at=_iso(now - timedelta(hours=3)),
    )
    assert Presence.active_within("stale", timedelta(minutes=30)) is False


def test_inputs_last_hour_no_row_returns_zero(graph_db_env):
    assert Presence.inputs_last_hour("unknown") == 0


def test_inputs_last_hour_returns_counter(graph_db_env):
    _write_activity("counter", inputs_last_hour=7)
    assert Presence.inputs_last_hour("counter") == 7


def test_inputs_last_hour_handles_non_int_payload_safely(graph_db_env):
    """Defensive: a malformed counter (e.g. legacy migration) reads as 0."""
    # Insert a row with the expected schema, then patch the inner field
    # by writing a follow-up row with the canonical type. The schema
    # validator rejects non-ints on write, so this path is reached only
    # by legacy / hand-edited rows; force one by going through the DB.
    _write_activity("legacy", inputs_last_hour=4)
    db = settings_ops._open(None)
    try:
        db.conn.execute(
            "UPDATE settings SET payload = ? WHERE set_id = ? AND key = ?",
            (
                '{"participant_id": "legacy", "participant_kind": "agent",'
                '"participant_label": "L", "last_user_input_at": "",'
                '"last_session_turn_at": "", "last_meaningful_at": "",'
                '"inputs_last_hour": "many", "turns_last_hour": 0}',
                PARTICIPANT_ACTIVITY_SET_ID, "legacy",
            ),
        )
        db.conn.commit()
    finally:
        db.close()
    assert Presence.inputs_last_hour("legacy") == 0


# ── Color helper ─────────────────────────────────────────────


def test_participant_color_format():
    """Acceptance #5: returns an HSL string."""
    color = Presence.participant_color("ea2cef72-ed4")
    assert color.startswith("hsl(") and color.endswith(")")


def test_participant_color_deterministic():
    """Acceptance #5: same input always returns same output."""
    a = Presence.participant_color("ea2cef72-ed4")
    b = Presence.participant_color("ea2cef72-ed4")
    assert a == b


def test_participant_color_different_inputs_different_outputs():
    """Sanity: distinct ids generally produce distinct hues. (Not a
    strict guarantee — hash collisions exist — but a reasonable smoke
    check across a small sample.)
    """
    colors = {
        Presence.participant_color(f"id-{i:03d}") for i in range(20)
    }
    # Twenty random ids should easily produce more than one distinct
    # color; collapsing to a single value would indicate a broken hash.
    assert len(colors) > 1


def test_participant_color_hue_in_range():
    """Hue must land in [0, 360)."""
    for sample in ("alice", "bob", "carol", "ea2cef72-ed4", ""):
        color = Presence.participant_color(sample)
        # Format: "hsl(<int> 70% 60%)"
        hue_str = color.removeprefix("hsl(").split(" ", 1)[0]
        hue = int(hue_str)
        assert 0 <= hue < 360
