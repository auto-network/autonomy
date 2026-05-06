"""Tests for the vote → CrossTalk source-session ping handler.

The handler in :mod:`tools.dashboard.notifications_actions` is
registered against the settings_mediator's thin handler registry and
fires on every :class:`AskVoteV1` ``setting.changed`` event for
``dashboard.activity.ask_vote``.

Acceptance covered:

* CrossTalk delivery to the source session with an inline preview
  pulled from the v2 ``normal`` zoom field (or v1 ``text`` fallback).
* Sender attribution stamps ``from="dashboard:<voter_id>"`` so the
  receiving session can distinguish an operator vote from a peer
  agent's CrossTalk message.
* ``direction`` (up / down) carried through to envelope attrs.
* Skip + warn when the SessionAsk row is missing.
* Skip + warn when the vote payload has no ``ask_id``.
* Handler registered at module import time.
* Module imported during dashboard startup so the registration fires
  before :func:`settings_mediator.start_action_loop` ticks.
"""
from __future__ import annotations

import importlib
import logging
import sys
from uuid import uuid4

import pytest

from tools.dashboard import notifications_actions
from tools.dashboard import notifications_settings as ns
from tools.dashboard import settings_mediator
from tools.dashboard.notifications_actions import deliver_vote_ping
from tools.dashboard.settings_mediator import Row, Services
from tools.dashboard.settings_mediator.loop import _HANDLERS
from tools.graph import settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


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
    settings_mediator.clear_registry()
    settings_mediator.reset_health()
    yield
    settings_mediator.clear_registry()
    settings_mediator.reset_health()


@pytest.fixture
def services_capture():
    """Stub :class:`Services` recording every CrosstalkService call."""
    sent: list[tuple[str, str, dict]] = []

    class _Recorder:
        async def send(self, *, target, kind, body):
            sent.append((target, kind, dict(body)))

    async def _session_send(session, text):
        raise AssertionError(
            "deliver_vote_ping must route via services.crosstalk.send"
        )

    svc = Services(
        session_send=_session_send,
        log=logging.getLogger("settings_mediator.test_vote"),
        crosstalk=_Recorder(),
    )
    svc._sent = sent
    return svc


@pytest.fixture
def notifications_schemas_registered():
    """Register the SessionAsk v1 + v2 + AskVote schemas after the
    autouse snapshot reset the registry. v2 also wires its
    ``upconvert_from_prev`` so reads of v1 rows surface as v2 payloads.
    """
    from tools.graph.schemas.registry import register_schema

    register_schema(
        ns.SESSION_ASK_SET_ID, 1, ns.SessionAskV1,
    )
    register_schema(
        ns.SESSION_ASK_SET_ID, 2, ns.SessionAskV2,
        upconvert_from_prev=ns.SessionAskV2.upconvert_from_prev,
    )
    register_schema(
        ns.ASK_VOTE_SET_ID, ns.SCHEMA_REVISION, ns.AskVoteV1,
    )


def _write_session_ask_v2(
    *, session_id: str, normal: str, revision_seq: int,
    compact: str = "", expanded: str = "",
    created_at: str = "2026-05-06T12:00:00Z",
) -> str:
    return settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SESSION_ASK_LATEST_REVISION, session_id,
        {
            "session_id": session_id,
            "compact": compact,
            "normal": normal,
            "expanded": expanded,
            "revision_seq": revision_seq,
            "created_at": created_at,
        },
        org=settings_ops.CALLER_ORG,
    )


def _write_session_ask_v1_legacy(
    *, session_id: str, text: str, revision_seq: int,
    created_at: str = "2026-05-06T12:00:00Z",
) -> str:
    """Write a legacy v1 row directly so the upconverter path is
    exercised on read.
    """
    return settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, 1, session_id,
        {
            "session_id": session_id,
            "text": text,
            "revision_seq": revision_seq,
            "created_at": created_at,
        },
        org=settings_ops.CALLER_ORG,
    )


def _make_vote_row(payload: dict, *, key: str) -> Row:
    return Row(
        id=str(uuid4()),
        set_id=ns.ASK_VOTE_SET_ID,
        key=key,
        payload=payload,
        created_at="2026-05-06T12:00:00Z",
        updated_at="2026-05-06T12:00:00Z",
    )


# ── Direct handler unit tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_vote_ping_sends_crosstalk_to_source_session_with_inline_preview(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """Operator votes 👍 → source session receives a CrossTalk ping
    targeted at the source session id with the ask body inlined and
    the sender stamped as ``dashboard:<voter_id>``.
    """
    _write_session_ask_v2(
        session_id="auto-source-1",
        normal="Should I ship the migration?",
        revision_seq=3,
    )
    payload = {
        "ask_id": "auto-source-1",
        "voter_id": "operator-jeremy",
        "direction": "up",
        "voted_at": "2026-05-06T12:00:00Z",
    }
    row = _make_vote_row(payload, key="auto-source-1:operator-jeremy")

    await deliver_vote_ping(row, services_capture)

    assert len(services_capture._sent) == 1
    target, kind, body = services_capture._sent[0]
    assert target == "auto-source-1"
    assert kind == "ask-vote"
    assert "Should I ship the migration?" in body["text"]
    # Sender identity is dashboard-stamped, not a bare voter id.
    assert body["from"] == "dashboard:operator-jeremy"
    assert body["attrs"]["ask_id"] == "auto-source-1"
    assert body["attrs"]["direction"] == "up"
    assert body["attrs"]["voter_id"] == "operator-jeremy"


@pytest.mark.asyncio
async def test_vote_ping_thumbs_down_glyph_in_body(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """Down votes render a 👎 glyph; up votes render 👍."""
    _write_session_ask_v2(
        session_id="auto-down", normal="needs op input", revision_seq=1,
    )
    payload = {
        "ask_id": "auto-down",
        "voter_id": "op",
        "direction": "down",
        "voted_at": "2026-05-06T12:00:00Z",
    }
    row = _make_vote_row(payload, key="auto-down:op")

    await deliver_vote_ping(row, services_capture)

    body = services_capture._sent[0][2]
    assert "👎" in body["text"]
    assert body["attrs"]["direction"] == "down"


@pytest.mark.asyncio
async def test_vote_ping_reads_legacy_v1_text_via_upconverter(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """A vote on a session that wrote a legacy v1 ask still produces a
    body preview — the upconverter maps ``text → normal`` on read.
    """
    _write_session_ask_v1_legacy(
        session_id="auto-legacy",
        text="LEGACY_BODY_FROM_V1",
        revision_seq=1,
    )
    payload = {
        "ask_id": "auto-legacy",
        "voter_id": "op",
        "direction": "up",
        "voted_at": "2026-05-06T12:00:00Z",
    }
    row = _make_vote_row(payload, key="auto-legacy:op")

    await deliver_vote_ping(row, services_capture)

    body = services_capture._sent[0][2]
    assert "LEGACY_BODY_FROM_V1" in body["text"]


@pytest.mark.asyncio
async def test_vote_ping_skips_when_session_ask_missing(
    graph_db_env, notifications_schemas_registered, services_capture, caplog,
):
    """Vote row exists but the SessionAsk row is gone — log + skip."""
    payload = {
        "ask_id": "ghost-ask",
        "voter_id": "op",
        "direction": "up",
        "voted_at": "2026-05-06T12:00:00Z",
    }
    row = _make_vote_row(payload, key="ghost-ask:op")

    with caplog.at_level(logging.WARNING, logger="notifications_actions"):
        await deliver_vote_ping(row, services_capture)

    assert services_capture._sent == []
    assert any(
        "SessionAsk row missing" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_vote_ping_skips_when_payload_missing_ask_id(
    graph_db_env, notifications_schemas_registered, services_capture, caplog,
):
    """A vote row whose payload has no ``ask_id`` has nowhere to
    deliver — log + skip rather than crash.
    """
    payload = {
        "voter_id": "op",
        "direction": "up",
        "voted_at": "2026-05-06T12:00:00Z",
    }
    row = _make_vote_row(payload, key=":op")

    with caplog.at_level(logging.WARNING, logger="notifications_actions"):
        await deliver_vote_ping(row, services_capture)

    assert services_capture._sent == []
    assert any(
        "missing ask_id" in rec.getMessage()
        for rec in caplog.records
    )


# ── Registration contracts ───────────────────────────────────────────


def test_vote_ping_handler_registered_at_module_import():
    """Module import wires the handler into the registry."""
    importlib.reload(notifications_actions)

    actions = _HANDLERS.get(ns.ASK_VOTE_SET_ID, [])
    names = [a.name for a in actions]
    assert "notifications.vote_ping" in names, (
        f"expected notifications.vote_ping on {ns.ASK_VOTE_SET_ID}; "
        f"got {names}"
    )


def test_vote_ping_module_imported_at_dashboard_startup():
    """``tools.dashboard.notifications_actions`` is in ``sys.modules``
    once the dashboard server module has been imported.
    """
    import tools.dashboard.server  # noqa: F401 — eager import is the contract

    assert "tools.dashboard.notifications_actions" in sys.modules
