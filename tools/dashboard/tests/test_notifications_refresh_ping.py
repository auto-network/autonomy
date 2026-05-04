"""Tests for the refresh-request → CrossTalk source-session ping handler.

Bead: auto-tdlhq (migrated from auto-r92kc's hand-rolled bus
subscriber). The handler in
:mod:`tools.dashboard.notifications_actions` is registered against the
settings_mediator's thin handler registry and fires on every
:class:`AskRefreshRequestV1` ``setting.changed`` event for
``dashboard.activity.ask_refresh``.

Acceptance covered (matching the bead spec):

* CrossTalk delivery to the source session with an inline preview.
* Skip + warn when the SessionAsk row is gone.
* Preview truncation at 80 characters.
* ``target_revision`` carried into the envelope body.
* Repeat events → repeat sends (regression for the
  ``upsert_by_key`` UPDATE gap closure in auto-rc27t).
* Handler registered at module import time.
* Module imported during dashboard startup so the registration
  fires before :func:`settings_mediator.start_action_loop` ticks.
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
from tools.dashboard.notifications_actions import (
    _TEXT_PREVIEW_CHARS,
    deliver_refresh_ping,
)
from tools.dashboard.settings_mediator import Row, Services
from tools.dashboard.settings_mediator.loop import _HANDLERS, _dispatch_event
from tools.graph import settings_ops
from tools.graph import db as graph_db_mod
from tools.graph.db import GraphDB
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
    """Reset the in-process action + heartbeat registry around each test."""
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
            "deliver_refresh_ping must route via services.crosstalk.send"
        )

    svc = Services(
        session_send=_session_send,
        log=logging.getLogger("settings_mediator.test_refresh"),
        crosstalk=_Recorder(),
    )
    svc._sent = sent
    return svc


@pytest.fixture
def notifications_schemas_registered():
    """Re-register the notifications schemas after the autouse snapshot."""
    from tools.graph.schemas.registry import register_schema
    register_schema(
        ns.SESSION_ASK_SET_ID, ns.SCHEMA_REVISION, ns.SessionAskV1,
    )
    register_schema(
        ns.ASK_REFRESH_SET_ID, ns.SCHEMA_REVISION, ns.AskRefreshRequestV1,
    )


def _write_session_ask(
    *, session_id: str, text: str, revision_seq: int,
    created_at: str = "2026-05-04T12:00:00Z",
) -> str:
    return settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SCHEMA_REVISION, session_id,
        {
            "session_id": session_id,
            "text": text,
            "revision_seq": revision_seq,
            "created_at": created_at,
        },
        org=settings_ops.CALLER_ORG,
    )


def _write_refresh_request(
    *, ask_id: str, requested_by: str, target_revision: int,
    requested_at: str = "2026-05-04T12:05:00Z",
) -> str:
    return settings_ops.upsert_by_key(
        ns.ASK_REFRESH_SET_ID, ns.SCHEMA_REVISION, ask_id,
        {
            "ask_id": ask_id,
            "requested_at": requested_at,
            "requested_by": requested_by,
            "target_revision": target_revision,
        },
        org=settings_ops.CALLER_ORG,
    )


def _refresh_event(key: str) -> dict:
    return {
        "set_id": ns.ASK_REFRESH_SET_ID,
        "schema_revision": ns.SCHEMA_REVISION,
        "key": key,
        "org": None,
        "publication_state": "raw",
        "deprecated": False,
        "operation": "write",
    }


def _make_row(payload: dict, *, key: str) -> Row:
    return Row(
        id=str(uuid4()),
        set_id=ns.ASK_REFRESH_SET_ID,
        key=key,
        payload=payload,
        created_at="2026-05-04T12:00:00Z",
        updated_at="2026-05-04T12:00:00Z",
    )


# ── Direct handler unit tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_ping_sends_crosstalk_to_source_session_with_inline_preview(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """Operator clicks ↻ → source session receives a CrossTalk ping
    targeted at the source session id with the ask text inlined.
    """
    _write_session_ask(
        session_id="auto-source-1",
        text="Should I ship the migration?",
        revision_seq=3,
    )
    payload = {
        "ask_id": "auto-source-1",
        "requested_at": "2026-05-04T12:00:00Z",
        "requested_by": "operator-jeremy",
        "target_revision": 3,
    }
    row = _make_row(payload, key="auto-source-1")

    await deliver_refresh_ping(row, services_capture)

    assert len(services_capture._sent) == 1
    target, kind, body = services_capture._sent[0]
    assert target == "auto-source-1"
    assert kind == "ask-refresh"
    # Inline preview present in body text.
    assert "Should I ship the migration?" in body["text"]
    # Sender attribution carries through to envelope `from`.
    assert body["from"] == "operator-jeremy"
    assert body["attrs"]["ask_id"] == "auto-source-1"
    assert body["attrs"]["requested_by"] == "operator-jeremy"


@pytest.mark.asyncio
async def test_refresh_ping_skips_when_session_ask_missing(
    graph_db_env, notifications_schemas_registered, services_capture, caplog,
):
    """Refresh row exists but SessionAsk row is gone — log + skip."""
    payload = {
        "ask_id": "ghost-ask",
        "requested_at": "2026-05-04T12:00:00Z",
        "requested_by": "op",
        "target_revision": 1,
    }
    row = _make_row(payload, key="ghost-ask")

    with caplog.at_level(logging.WARNING, logger="notifications_actions"):
        await deliver_refresh_ping(row, services_capture)

    assert services_capture._sent == []
    assert any(
        "SessionAsk row missing" in rec.getMessage()
        for rec in caplog.records
    ), f"expected missing-row warning; got {[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_refresh_ping_inline_preview_truncated_at_80_chars(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """The inline preview is capped at ``_TEXT_PREVIEW_CHARS`` plus an
    ellipsis marker so a long ask body doesn't flood the recipient
    terminal.
    """
    long_text = "x" * 200
    _write_session_ask(
        session_id="auto-long", text=long_text, revision_seq=1,
    )
    payload = {
        "ask_id": "auto-long",
        "requested_at": "2026-05-04T12:00:00Z",
        "requested_by": "op",
        "target_revision": 1,
    }
    row = _make_row(payload, key="auto-long")

    await deliver_refresh_ping(row, services_capture)

    assert len(services_capture._sent) == 1
    text = services_capture._sent[0][2]["text"]
    truncated = "x" * _TEXT_PREVIEW_CHARS
    assert truncated in text
    assert ("x" * (_TEXT_PREVIEW_CHARS + 1)) not in text


@pytest.mark.asyncio
async def test_refresh_ping_includes_target_revision_in_body(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """The body inlines ``target_revision`` so receivers can disambiguate
    repeated refresh requests against the same ask.
    """
    _write_session_ask(
        session_id="auto-rev", text="r", revision_seq=7,
    )
    payload = {
        "ask_id": "auto-rev",
        "requested_at": "2026-05-04T12:00:00Z",
        "requested_by": "op",
        "target_revision": 7,
    }
    row = _make_row(payload, key="auto-rev")

    await deliver_refresh_ping(row, services_capture)

    body = services_capture._sent[0][2]
    assert "7" in body["text"]
    assert body["attrs"]["target_revision"] == "7"


# ── Regression for the upsert_by_key UPDATE gap (auto-rc27t) ─────────


@pytest.mark.asyncio
async def test_refresh_ping_invoked_on_every_setting_changed_event(
    graph_db_env, notifications_schemas_registered, services_capture,
):
    """Two ``setting.changed`` events for the same row → two sends.

    Regression: the pre-collapse mediator advanced cursors by
    ``created_at`` and so was blind to ``upsert_by_key`` UPDATEs
    bumping ``target_revision`` on the same row id. The new substrate
    delivers every event to every registered handler — repeat clicks
    fire repeat pings.

    Importing :mod:`tools.dashboard.notifications_actions` re-registers
    ``notifications.refresh_ping`` after the autouse fixture cleared
    the registry.
    """
    importlib.reload(notifications_actions)

    _write_session_ask(
        session_id="auto-pump", text="needs op input", revision_seq=2,
    )
    _write_refresh_request(
        ask_id="auto-pump", requested_by="op", target_revision=2,
    )
    # First event for the freshly-written row.
    await _dispatch_event(_refresh_event("auto-pump"), services_capture)
    # Bump target_revision via upsert (UPDATE on the same row id) and
    # fire a second event — the pre-collapse mediator missed this; the
    # collapsed substrate must not.
    _write_refresh_request(
        ask_id="auto-pump", requested_by="op", target_revision=3,
    )
    await _dispatch_event(_refresh_event("auto-pump"), services_capture)

    assert len(services_capture._sent) == 2
    revs = [body["attrs"]["target_revision"]
            for (_t, _k, body) in services_capture._sent]
    assert revs == ["2", "3"]


# ── Cross-org SessionAsk lookup isolation (auto-dcegc) ───────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin per-org DB layout to a tmp orgs dir; clear ``GRAPH_DB``.

    Required for cross-org tests — ``GRAPH_DB`` collapses every read
    onto one DB regardless of the ``org=`` argument, defeating the
    isolation we're trying to verify.
    """
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    GraphDB.close_all_pooled()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


@pytest.mark.asyncio
async def test_refresh_ping_in_one_org_does_not_read_session_ask_from_another(
    orgs_root, notifications_schemas_registered, services_capture,
):
    """A refresh row originating from org A must resolve its SessionAsk
    against org A's DB — not the scopeless DB and not a peer org's DB.

    Pre-fix the handler hardcoded ``org=None`` in
    :func:`notifications_actions._resolve_session_ask`, so a refresh
    fired in org A would read the SessionAsk row from the personal /
    scopeless DB instead. That is benign in the current single-org
    deployment, but silently mis-routes any future cross-org
    refresh-ping flow. This test pins the contract: the lookup follows
    the row's org.

    The two SessionAsk rows below share an ``ask_id`` but live in
    distinct org DBs — the handler MUST surface the org-A copy when
    invoked with a Row carrying ``org="alpha"``, not the scopeless one.
    """
    GraphDB.create_org_db("alpha").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    # Same ask_id in two different DBs — only the org-A read should win.
    settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SCHEMA_REVISION, "auto-shared",
        {
            "session_id": "auto-shared",
            "text": "ALPHA_ORG_TEXT",
            "revision_seq": 1,
            "created_at": "2026-05-04T12:00:00Z",
        },
        org="alpha",
    )
    settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SCHEMA_REVISION, "auto-shared",
        {
            "session_id": "auto-shared",
            "text": "SCOPELESS_LEAK_TEXT",
            "revision_seq": 1,
            "created_at": "2026-05-04T12:00:00Z",
        },
        org=None,
    )

    payload = {
        "ask_id": "auto-shared",
        "requested_at": "2026-05-04T12:05:00Z",
        "requested_by": "operator-alpha",
        "target_revision": 1,
    }
    row = Row(
        id=str(uuid4()),
        set_id=ns.ASK_REFRESH_SET_ID,
        key="auto-shared",
        payload=payload,
        created_at="2026-05-04T12:05:00Z",
        updated_at="2026-05-04T12:05:00Z",
        org="alpha",
    )

    await deliver_refresh_ping(row, services_capture)

    assert len(services_capture._sent) == 1
    body = services_capture._sent[0][2]
    assert "ALPHA_ORG_TEXT" in body["text"]
    assert "SCOPELESS_LEAK_TEXT" not in body["text"], (
        "refresh ping leaked SessionAsk text from the scopeless DB into "
        "an org-scoped envelope — the lookup must follow row.org"
    )


@pytest.mark.asyncio
async def test_refresh_ping_in_one_org_does_not_read_peer_org_session_ask(
    orgs_root, notifications_schemas_registered, services_capture,
):
    """An org-A refresh must not fall through to a peer org's SessionAsk.

    Cross-org peer reads land via the explicit ``peers=`` plumbing on
    :func:`settings_ops.read_set` (peer-public-surface only). The
    refresh-ping handler passes ``peers=[]`` to keep follow-on lookups
    scoped to the row's org — verify that here by writing two
    same-key SessionAsk rows in disjoint org DBs and asserting the
    org-A read does not surface the org-B copy.
    """
    GraphDB.create_org_db("alpha").close()
    GraphDB.create_org_db("beta").close()
    # personal.db is consulted by peer-subscription discovery; create it
    # so the cross-org subscription path is fully wired even though
    # peers=[] short-circuits it.
    GraphDB.create_org_db("personal", type_="personal").close()

    settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SCHEMA_REVISION, "auto-shared",
        {
            "session_id": "auto-shared",
            "text": "ALPHA_ORG_TEXT",
            "revision_seq": 1,
            "created_at": "2026-05-04T12:00:00Z",
        },
        org="alpha",
    )
    settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SCHEMA_REVISION, "auto-shared",
        {
            "session_id": "auto-shared",
            "text": "BETA_ORG_TEXT",
            "revision_seq": 1,
            "created_at": "2026-05-04T12:00:00Z",
        },
        org="beta",
    )

    payload = {
        "ask_id": "auto-shared",
        "requested_at": "2026-05-04T12:05:00Z",
        "requested_by": "operator-alpha",
        "target_revision": 1,
    }
    row = Row(
        id=str(uuid4()),
        set_id=ns.ASK_REFRESH_SET_ID,
        key="auto-shared",
        payload=payload,
        created_at="2026-05-04T12:05:00Z",
        updated_at="2026-05-04T12:05:00Z",
        org="alpha",
    )

    await deliver_refresh_ping(row, services_capture)

    assert len(services_capture._sent) == 1
    body = services_capture._sent[0][2]
    assert "ALPHA_ORG_TEXT" in body["text"]
    assert "BETA_ORG_TEXT" not in body["text"], (
        "org-A refresh must not pick up org-B's SessionAsk text"
    )


# ── Registration contracts ───────────────────────────────────────────


def test_refresh_ping_handler_registered_at_module_import():
    """Module import wires the handler into the registry.

    Reloading the module re-runs the ``@register_action_decorator``
    against the cleared registry (the autouse fixture clears
    ``_HANDLERS`` between tests).
    """
    importlib.reload(notifications_actions)

    actions = _HANDLERS.get(ns.ASK_REFRESH_SET_ID, [])
    assert len(actions) == 1, (
        f"expected exactly one handler on {ns.ASK_REFRESH_SET_ID}; "
        f"got {[a.name for a in actions]}"
    )
    assert actions[0].name == "notifications.refresh_ping"


def test_refresh_ping_module_imported_at_dashboard_startup():
    """``tools.dashboard.notifications_actions`` is in ``sys.modules``
    once the dashboard server module has been imported.

    The handler registration only fires at module import time, so the
    dashboard is broken if ``server.py`` lazy-imports this module —
    the registration would never run and refresh clicks would silently
    drop. This test guards against that regression.
    """
    import tools.dashboard.server  # noqa: F401 — eager import is the contract

    assert "tools.dashboard.notifications_actions" in sys.modules
