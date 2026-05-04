"""Tests for the SurfacePing CrossTalk delivery action (substrate.C, auto-9gxo8).

Covers:

* :func:`format_ping` shape — sender, attrs, body text.
* :func:`build_envelope` — escaping and layout match the existing
  ``api_crosstalk_send`` envelope so the receiving parser handles
  substrate-issued messages identically.
* :class:`CrosstalkService.send` — inner ``send_fn`` is invoked once
  with the right (target, envelope) pair.
* :func:`deliver_ping` direct invocation — routes by explicit
  ``to_participant_id`` and never consults session-role lookup.
* End-to-end through the registry's dispatch path: a synthesized
  ``setting.changed`` event resolves the row and fires the handler
  exactly once with the expected envelope.
* Registration contract: importing the module wires the handler into
  the registry keyed on ``dashboard.surface.ping``.
"""
from __future__ import annotations

import importlib
import logging
from uuid import uuid4

import pytest

from tools.dashboard import settings_mediator
from tools.dashboard.settings_mediator import Row, Services
from tools.dashboard.settings_mediator.loop import _HANDLERS, _dispatch_event
from tools.dashboard import surface_actions
from tools.dashboard.surface_actions import (
    CrosstalkService,
    build_envelope,
    deliver_ping,
    format_ping,
)
from tools.graph import settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS
from tools.graph.surface import SURFACE_PING_SET_ID, SCHEMA_REVISION


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
    """Reset the in-process action + heartbeat registry around each test."""
    settings_mediator.clear_registry()
    settings_mediator.reset_health()
    yield
    settings_mediator.clear_registry()
    settings_mediator.reset_health()


@pytest.fixture
def services_capture():
    """Stub :class:`Services` recording every CrosstalkService call.

    The whole point of substrate.C is that pings route by explicit
    ``to_participant_id``, not by role lookup. Acceptance #1 + the
    role-filter pitfall
    (``graph://1ba4d2e0-c5f``) ride on this guarantee.
    """
    sent: list[tuple[str, str, dict]] = []

    class _Recorder:
        async def send(self, *, target, kind, body):
            sent.append((target, kind, dict(body)))

    async def _session_send(session, text):
        raise AssertionError(
            "deliver_ping must route via services.crosstalk.send, "
            "not session_send"
        )

    svc = Services(
        session_send=_session_send,
        log=logging.getLogger("settings_mediator.test_surface"),
        crosstalk=_Recorder(),
    )
    svc._sent = sent
    return svc


def _make_row(payload: dict, *, row_id: str | None = None) -> Row:
    """Construct a SurfacePing Row for direct handler unit tests."""
    return Row(
        id=row_id or str(uuid4()),
        set_id=SURFACE_PING_SET_ID,
        key=str(uuid4()),
        payload=payload,
        created_at="2026-05-02T12:00:00Z",
        updated_at="2026-05-02T12:00:00Z",
    )


def _ping_payload(**overrides) -> dict:
    """Build a SurfacePing payload with sensible defaults."""
    base = {
        "surface_id": "settings-nexus",
        "from_participant_id": "operator:jeremy",
        "to_participant_id": "auto-0501-185349",
        "position_kind": "tile",
        "position_value": "redesign-thinking",
        "message": "look at this",
        "sent_at": "2026-05-02T12:00:00Z",
    }
    base.update(overrides)
    return base


# ── format_ping ──────────────────────────────────────────────────────


def test_format_ping_shape():
    """Carries sender, surface, position, message + a prose body."""
    out = format_ping(_ping_payload())

    assert out["from"] == "operator:jeremy"
    assert out["text"] == "You were summoned to settings-nexus."
    assert out["attrs"] == {
        "surface": "settings-nexus",
        "position": "tile:redesign-thinking",
        "message": "look at this",
    }


def test_format_ping_handles_missing_message_field():
    """``message`` is optional on SurfacePingV1; absence is empty string."""
    payload = _ping_payload()
    del payload["message"]

    out = format_ping(payload)
    assert out["attrs"]["message"] == ""


def test_format_ping_position_combines_kind_and_value():
    """``position`` attribute is rendered as ``<kind>:<value>``."""
    out = format_ping(_ping_payload(
        position_kind="zone", position_value="overview-strip",
    ))
    assert out["attrs"]["position"] == "zone:overview-strip"


# ── build_envelope ───────────────────────────────────────────────────


def test_build_envelope_layout():
    """Envelope mirrors the existing ``api_crosstalk_send`` shape."""
    env = build_envelope(
        from_id="operator:jeremy",
        kind="surface-ping",
        extra={
            "surface": "settings-nexus",
            "position": "tile:redesign-thinking",
            "message": "look at this",
        },
        body="You were summoned to settings-nexus.",
    )
    assert env == (
        '<crosstalk from="operator:jeremy"\n'
        '           kind="surface-ping"\n'
        '           surface="settings-nexus"\n'
        '           position="tile:redesign-thinking"\n'
        '           message="look at this">\n'
        'You were summoned to settings-nexus.\n'
        '</crosstalk>'
    )


def test_build_envelope_escapes_attribute_values():
    """Quote / angle-bracket / ampersand are escaped in attribute values."""
    env = build_envelope(
        from_id='operator "j"',
        kind="surface-ping",
        extra={"message": "x & y > 1 < 2 \"go\""},
        body="hi",
    )
    assert 'from="operator &quot;j&quot;"' in env
    assert (
        'message="x &amp; y &gt; 1 &lt; 2 &quot;go&quot;"'
        in env
    )


# ── CrosstalkService.send ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crosstalk_service_invokes_send_fn_once():
    """``send`` calls the wrapped fn exactly once with (target, envelope)."""
    captured: list[tuple[str, str]] = []

    async def _send(target, text):
        captured.append((target, text))

    svc = CrosstalkService(send_fn=_send)
    await svc.send(
        target="auto-0501-185349",
        kind="surface-ping",
        body=format_ping(_ping_payload()),
    )

    assert len(captured) == 1
    target, envelope = captured[0]
    assert target == "auto-0501-185349"
    assert envelope.startswith('<crosstalk from="operator:jeremy"\n')
    assert '           kind="surface-ping"' in envelope
    assert envelope.endswith("</crosstalk>")


# ── deliver_ping (direct unit test) ──────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_ping_routes_by_explicit_target(services_capture):
    """Acceptance #1 — handler delivers via ``services.crosstalk.send``.

    Reaching the recorder proves the handler ignored role-lookup
    entirely (per pitfall ``graph://1ba4d2e0-c5f``).
    """
    row = _make_row(_ping_payload())
    await deliver_ping(row, services_capture)

    assert len(services_capture._sent) == 1
    target, kind, body = services_capture._sent[0]
    assert target == "auto-0501-185349"
    assert kind == "surface-ping"
    assert body == format_ping(row.payload)


@pytest.mark.asyncio
async def test_deliver_ping_independent_targets(services_capture):
    """Acceptance #2 — two pings to different targets deliver independently."""
    row1 = _make_row(_ping_payload(
        to_participant_id="auto-A",
        position_value="tile-a",
    ))
    row2 = _make_row(_ping_payload(
        to_participant_id="auto-B",
        position_value="tile-b",
    ))

    await deliver_ping(row1, services_capture)
    await deliver_ping(row2, services_capture)

    targets = [t for (t, _k, _b) in services_capture._sent]
    assert targets == ["auto-A", "auto-B"]
    assert services_capture._sent[0][2]["attrs"]["position"] == "tile:tile-a"
    assert services_capture._sent[1][2]["attrs"]["position"] == "tile:tile-b"


# ── End-to-end through the registry's dispatch path ──────────────────


@pytest.fixture
def surface_schema_registered():
    """Re-register the SurfacePingV1 schema after the autouse snapshot.

    The autouse ``_isolate_schema_registry`` snapshots SCHEMAS and
    restores it on teardown. To exercise add_setting against the real
    SurfacePing validator inside the snapshot window, we re-register
    the schema explicitly.
    """
    from tools.graph.schemas.registry import register_schema
    from tools.graph.surface import SurfacePingV1
    register_schema(
        SURFACE_PING_SET_ID, SCHEMA_REVISION, SurfacePingV1,
    )
    return SurfacePingV1


def _add_ping_row(payload: dict, *, key: str | None = None) -> str:
    key = key or str(uuid4())
    sid = settings_ops.add_setting(
        SURFACE_PING_SET_ID, SCHEMA_REVISION, key, payload,
        org=settings_ops.CALLER_ORG,
    )
    return key


def _ping_event(key: str) -> dict:
    return {
        "set_id": SURFACE_PING_SET_ID,
        "schema_revision": SCHEMA_REVISION,
        "key": key,
        "org": None,
        "publication_state": "raw",
        "deprecated": False,
        "operation": "write",
    }


@pytest.mark.asyncio
async def test_e2e_surface_ping_row_dispatches_handler(
    graph_db_env, surface_schema_registered, services_capture,
):
    """Acceptance #5 — write SurfacePing + setting.changed → CrossTalk send."""
    # Importing the module registers ``surface.ping.deliver``. The
    # autouse fixture cleared the registry, so re-execute the module
    # to re-register the handler under the cleared slate.
    importlib.reload(surface_actions)

    payload = _ping_payload(to_participant_id="auto-recipient")
    key = _add_ping_row(payload)
    await _dispatch_event(_ping_event(key), services_capture)

    assert len(services_capture._sent) == 1
    target, kind, body = services_capture._sent[0]
    assert target == "auto-recipient"
    assert kind == "surface-ping"
    assert body == format_ping(payload)


@pytest.mark.asyncio
async def test_e2e_multiple_pings_dispatch_independently(
    graph_db_env, surface_schema_registered, services_capture,
):
    """Acceptance #2 — two pings in flight each deliver to their own target."""
    importlib.reload(surface_actions)

    p1 = _ping_payload(to_participant_id="auto-A", position_value="t-a")
    p2 = _ping_payload(to_participant_id="auto-B", position_value="t-b")
    p3 = _ping_payload(to_participant_id="auto-A", position_value="t-c")

    k1 = _add_ping_row(p1)
    k2 = _add_ping_row(p2)
    k3 = _add_ping_row(p3)

    for k in (k1, k2, k3):
        await _dispatch_event(_ping_event(k), services_capture)

    targets = [t for (t, _k, _b) in services_capture._sent]
    positions = [
        b["attrs"]["position"] for (_t, _k, b) in services_capture._sent
    ]
    assert sorted(targets) == ["auto-A", "auto-A", "auto-B"]
    assert sorted(positions) == [
        "tile:t-a", "tile:t-b", "tile:t-c",
    ]


@pytest.mark.asyncio
async def test_e2e_unmatched_target_does_not_raise(
    graph_db_env, surface_schema_registered,
):
    """Acceptance #3 — target session not currently live → no error.

    The substrate's CrosstalkService wraps ``tmux_send`` directly;
    ``tmux_send`` is fire-and-forget and its underlying ``subprocess.run``
    calls don't ``check=True`` on the tmux exit code. Simulating the
    same contract here: the inner ``send_fn`` returns normally even when
    no session exists. The handler must not raise either way.
    """
    importlib.reload(surface_actions)

    delivered: list[str] = []

    async def _send_fn(target, text):
        # Mimic ``tmux_send`` — no session validation, no raise.
        delivered.append(target)

    svc = Services(
        session_send=lambda *a, **kw: None,  # not used
        log=logging.getLogger("settings_mediator.test_surface"),
        crosstalk=CrosstalkService(send_fn=_send_fn),
    )

    key = _add_ping_row(_ping_payload(to_participant_id="not-a-real-session"))
    await _dispatch_event(_ping_event(key), svc)

    assert delivered == ["not-a-real-session"]
    health = settings_mediator.HEALTH
    assert "surface.ping.deliver" in health.last_handler_succeeded_at
    assert "surface.ping.deliver" not in health.last_handler_error


# ── Registration contract ────────────────────────────────────────────


def test_importing_module_registers_handler():
    """Acceptance #4 — module import wires the handler into the registry."""
    importlib.reload(surface_actions)

    actions = _HANDLERS.get(SURFACE_PING_SET_ID, [])
    assert len(actions) == 1, (
        f"expected exactly one handler on {SURFACE_PING_SET_ID}; "
        f"got {[a.name for a in actions]}"
    )
    entry = actions[0]
    assert entry.name == "surface.ping.deliver"
