"""Tests for the Surface Presence + OperatorActivity substrate (v1).

Bead: ``auto-i3tki``. Covers:

* schema registration is eager (importing the module is enough),
* validation accepts the canonical payloads and rejects bad ones,
* :class:`Presence` context-manager lifecycle: writes initial row,
  starts heartbeat thread, joins on exit, writes final row,
* heartbeat thread cleanup on exit (no leaked thread),
* :meth:`Presence.participant_color` determinism + format,
* :meth:`Presence.is_idle` / :meth:`Presence.last_user_input` read
  the singleton OperatorActivity row.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SCHEMAS, SchemaValidationError, schema_key,
)
from tools.graph.surface import (
    OPERATOR_ACTIVITY_SET_ID,
    SCHEMA_REVISION,
    SURFACE_PING_SET_ID,
    SURFACE_PRESENCE_SET_ID,
    OperatorActivityV1,
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
    assert SCHEMAS.get(schema_key(OPERATOR_ACTIVITY_SET_ID, 1)) \
        is OperatorActivityV1


def test_presence_decorated_keyed_per_entity():
    assert SurfacePresenceV1._access_pattern == "keyed_per_entity"


def test_ping_decorated_append_only_log():
    assert SurfacePingV1._access_pattern == "append_only_log"
    assert SurfacePingV1._key_strategy == "uuid_v4"


def test_operator_activity_decorated_singleton():
    """OperatorActivity is a single, fixed-key row — latest write wins."""
    assert OperatorActivityV1._access_pattern == "singleton"
    assert OperatorActivityV1._key_strategy == "fixed:operator"


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
    return {"last_input_at": "2026-05-02T12:00:00Z"}


def test_activity_validate_accepts_canonical_payload():
    OperatorActivityV1.validate(_valid_activity_payload())


def test_activity_validate_accepts_empty_payload():
    """All fields default — an empty dict is a valid singleton row."""
    OperatorActivityV1.validate({})


def test_activity_validate_rejects_non_string_timestamp():
    with pytest.raises(SchemaValidationError):
        OperatorActivityV1.validate({"last_input_at": 12345})


def test_activity_validate_rejects_unknown_field():
    with pytest.raises(SchemaValidationError):
        OperatorActivityV1.validate({
            "last_input_at": "2026-05-02T12:00:00Z",
            "participant_id": "leftover",
        })


def test_activity_validate_rejects_non_dict_payload():
    with pytest.raises(SchemaValidationError):
        OperatorActivityV1.validate(["not", "a", "dict"])


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

    Writes route through ``upsert_by_key`` so the row count stays at
    one; we count emit-hook invocations instead, which fire once per
    successful write (initial + heartbeats + final).
    """
    write_calls = []

    def hook(*, operation, snapshot, org):
        if snapshot.get("key") == "surf:hb":
            write_calls.append(operation)

    settings_ops.set_emit_hook(hook)
    try:
        with Presence(
            surface_id="surf", participant_kind="agent",
            participant_id="hb", label="HB", heartbeat_interval=0.05,
        ):
            # Wait long enough for at least a couple heartbeats to fire.
            threading.Event().wait(0.25)
    finally:
        settings_ops.set_emit_hook(None)

    # Initial enter + final exit = 2 writes; the heartbeat loop adds
    # at least one more during the 0.25s wait at a 0.05s interval.
    assert len(write_calls) >= 3, (
        f"expected initial + heartbeat + final writes, got {write_calls}"
    )

    # Upsert keeps the row count at one regardless of how many heartbeats
    # fired — that's the whole point of the migration.
    db = settings_ops._open(None)
    try:
        rows = db.conn.execute(
            "SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
            "AND key = ?",
            (SURFACE_PRESENCE_SET_ID, "surf:hb"),
        ).fetchone()
    finally:
        db.close()
    assert rows["n"] == 1


# ── Static helpers — read live OperatorActivity row ──────────


def _write_operator_activity(last_input_at: str) -> None:
    """Helper: write the singleton OperatorActivity row."""
    settings_ops.add_setting(
        OPERATOR_ACTIVITY_SET_ID,
        SCHEMA_REVISION,
        "operator",
        {"last_input_at": last_input_at},
        org="personal",
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_is_idle_no_row_returns_true(graph_db_env):
    """Acceptance #2: missing row → idle."""
    assert Presence.is_idle() is True


def test_is_idle_recent_input_returns_false(graph_db_env):
    """A user input within the threshold = not idle."""
    now = datetime.now(timezone.utc)
    _write_operator_activity(_iso(now - timedelta(seconds=30)))
    assert Presence.is_idle(threshold=timedelta(minutes=5)) is False


def test_is_idle_stale_input_returns_true(graph_db_env):
    """Last input older than threshold = idle."""
    now = datetime.now(timezone.utc)
    _write_operator_activity(_iso(now - timedelta(hours=2)))
    assert Presence.is_idle(threshold=timedelta(minutes=30)) is True


def test_is_idle_empty_last_input_returns_true(graph_db_env):
    """A row with empty ``last_input_at`` reads as idle."""
    _write_operator_activity("")
    assert Presence.is_idle() is True


def test_last_user_input_no_row_returns_none(graph_db_env):
    """No activity row → no last input."""
    assert Presence.last_user_input() is None


def test_last_user_input_returns_parsed_datetime(graph_db_env):
    """The ISO timestamp comes back as a tz-aware datetime."""
    when = datetime.now(timezone.utc).replace(microsecond=0)
    _write_operator_activity(_iso(when))
    got = Presence.last_user_input()
    assert got is not None
    assert got.tzinfo is not None
    assert int(got.timestamp()) == int(when.timestamp())


def test_last_user_input_unparseable_returns_none(graph_db_env):
    """A garbled timestamp on the row should not crash callers."""
    _write_operator_activity("not-an-iso-string")
    assert Presence.last_user_input() is None


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
