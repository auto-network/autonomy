"""API tests for ``POST /api/agent-actions/dispatch`` (auto-pqgrl).

The endpoint dispatches an agentic action against a graph asset or bead.
Routing is strictly own-org-of-asset: actions defined in ``anchore.db``
only render on anchore notes; bead actions currently live in autonomy.
Universal Send-To is special-cased — it does not spawn an agent, only
delivers a CrossTalk primer.

Tests cover:

* Non-universal action creates an agentic source row in the target org
  with full provenance metadata.
* The dispatch_runs row is written with ``kind='agentic'`` and a populated
  ``agentic_source_id`` column.
* Universal Send-To delivers a primer via the CrossTalk surface and does
  NOT create an agentic source row.
* 404 for unknown member key.
* 5-second idempotency window deduplicates double clicks.
* The agent is launched in the *target asset's* workspace, not the
  operator's.
* An explicit ``workspace`` override materializes the full workspace
  launch bundle instead of the lightweight default path.
* Bead-targeted actions persist bead identity and route/trace correctly.
"""

from __future__ import annotations

import asyncio
import importlib
import time
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from tools.graph import org_ops, schemas  # noqa: F401 — registers contracts
from tools.graph import ops as graph_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.agent_actions import (
    AGENT_ACTIONS_REVISION,
    AGENT_ACTIONS_SET_ID,
)


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def isolated_dispatch_db(tmp_path, monkeypatch):
    """Pin DISPATCH_DB to a fresh tmp file and reload the writer module."""
    monkeypatch.setenv("DISPATCH_DB", str(tmp_path / "dispatch.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    return writer_mod


@pytest.fixture
def per_org_universe(tmp_path, monkeypatch):
    """Create per-org DBs for autonomy + anchore with seeded actions and a
    workspace registered for each."""
    orgs_dir = tmp_path / "orgs"
    # test_app's hermetic-orgs fixture may have created this same path
    # already; both fixtures share tmp_path.
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    # Avoid stomping on the live single-DB graph by clearing GRAPH_DB.
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)

    for slug in ("autonomy", "anchore"):
        org_ops.create_org(
            slug,
            type_="shared",
            identity_payload={"name": slug.capitalize()},
            root=orgs_dir,
        )

    # Seed dashboard.agent-actions members directly into each org's DB.
    _seed_actions(orgs_dir / "autonomy.db")
    _seed_actions(orgs_dir / "anchore.db")

    # Register a workspace for each org so the dispatch endpoint can
    # locate one when target_org is autonomy or anchore.
    _seed_workspace(orgs_dir / "autonomy.db", workspace_id="autonomy-rig")
    _seed_workspace(
        orgs_dir / "autonomy.db",
        workspace_id="operator",
        payload_overrides={
            "working_dir": "/workspace/repo/tools/dashboard",
            "env": {"FEATURE_FLAG": "1"},
            "dind": True,
            "network_host": False,
            "tags": ["operator", "agent-actions"],
        },
    )
    _seed_workspace(orgs_dir / "anchore.db", workspace_id="anchore-rig")

    return orgs_dir


def _seed_actions(org_db: Path) -> None:
    """Write a Send-To member + one note action into *org_db*."""
    members: list[tuple[str, dict]] = [
        (
            "session.send-to",
            {
                "asset_type": "*",
                "label": "Send To…",
                "icon": "↗",
                "universal": True,
                "writes": [],
            },
        ),
        (
            "note.update-summary",
            {
                "asset_type": "note",
                "label": "Update Title & Summary",
                "icon": "✏",
                "model": "claude-haiku-4-5-20251001",
                "prompt_template": (
                    "Update the title for {asset[id]}.\n"
                    "Page: {asset[url]}\n"
                    "Sender: {dispatched_by_session}\n"
                ),
                "estimated_seconds": 10,
                "writes": ["source.title"],
            },
        ),
        (
            "note.full-workspace",
            {
                "asset_type": "note",
                "label": "Deep Workspace Review",
                "icon": "🛠",
                "model": "claude-haiku-4-5-20251001",
                "workspace": "operator",
                "prompt_template": (
                    "Review {asset[id]} in a writable workspace.\n"
                    "Page: {asset[url]}\n"
                    "Sender: {dispatched_by_session}\n"
                ),
                "estimated_seconds": 25,
                "writes": ["source.title"],
            },
        ),
        (
            "bead.dry-run-implement",
            {
                "asset_type": "bead",
                "label": "Dry-Run Implement",
                "icon": "⚙",
                "model": "claude-haiku-4-5-20251001",
                "prompt_template": (
                    "Bead: {asset[id]}\n"
                    "Title: {bead[title]}\n"
                    "Status: {bead[status]}\n"
                    "Priority: {bead[priority]}\n"
                    "Primer:\n{asset[primer]}\n"
                ),
                "estimated_seconds": 20,
                "writes": ["bead.comment", "bead.labels"],
            },
        ),
        (
            "bead.ask-question",
            {
                "asset_type": "bead",
                "label": "Ask a Question",
                "icon": "?",
                "model": "claude-haiku-4-5-20251001",
                "prompt_template": (
                    "Bead: {asset[id]}\n"
                    "Question: {custom_input}\n"
                    "Primer:\n{asset[primer]}\n"
                ),
                "estimated_seconds": 60,
                "writes": ["bead.comment"],
                "input_prompt": "What do you want to ask about this bead?",
            },
        ),
        (
            "design.refresh-preview",
            {
                "asset_type": "design",
                "label": "Refresh Preview & Summary",
                "icon": "wand",
                "model": "claude-haiku-4-5-20251001",
                "prompt_template": (
                    "Refresh Design Studio metadata.\n"
                    "Revision: {asset[id]}\n"
                    "Series: {design[design_id]}\n"
                    "Status: {design[status]}\n"
                    "Page: {asset[url]}\n"
                ),
                "estimated_seconds": 60,
                "writes": ["design.thumbnail", "design.description"],
            },
        ),
    ]
    db = GraphDB(org_db)
    try:
        for key, payload in members:
            schemas.validate_payload(
                AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION, payload,
            )
            sid = "ag-" + key.replace(".", "-")
            now = "2026-04-28T00:00:00Z"
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sid, AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION,
                 key, json.dumps(payload), "canonical", now, now),
            )
        db.conn.commit()
    finally:
        db.close()


def _seed_workspace(
    org_db: Path,
    *,
    workspace_id: str,
    payload_overrides: dict[str, Any] | None = None,
) -> None:
    """Insert an ``autonomy.workspace#1`` row keyed on *workspace_id*."""
    payload = {"name": workspace_id, "image": "autonomy-session"}
    if payload_overrides:
        payload.update(payload_overrides)
    db = GraphDB(org_db)
    try:
        sid = "ws-" + workspace_id
        now = "2026-04-28T00:00:00Z"
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, "
            "payload, publication_state, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sid, "autonomy.workspace", 1, workspace_id,
             json.dumps(payload), "canonical", now, now),
        )
        db.conn.commit()
    finally:
        db.close()


def _insert_note_source(*, org: str, source_id: str, title: str) -> None:
    """Insert a ``type='docs'`` source row in *org*'s DB to act as a note."""
    from tools.graph.models import Source
    src = Source(
        id=source_id, type="docs", platform="local",
        title=title, file_path=f"note:{source_id}",
        publication_state="canonical",
    )
    db = graph_ops._open(org)  # type: ignore[attr-defined]
    try:
        db.insert_source(src)
    finally:
        db.close()


def _patch_bead_runtime(monkeypatch, bead: dict[str, Any]) -> None:
    """Patch bead lookup + primer generation for bead-targeted tests."""
    from tools.dashboard import server as server_mod
    from tools.graph import primer as primer_mod

    monkeypatch.setattr(
        server_mod.dao_beads,
        "get_bead",
        lambda bead_id: dict(bead) if bead_id == bead["id"] else None,
    )
    monkeypatch.setattr(
        primer_mod,
        "collect_primer_data",
        lambda bead_id, **kwargs: {
            "bead_id": bead_id,
            "bead": {"title": bead["title"]},
            "provenance": [],
            "related_notes": [],
            "pitfalls": [],
            "related_beads": [],
        },
    )
    monkeypatch.setattr(
        primer_mod,
        "format_for_agent",
        lambda data: f"Primer for {data['bead_id']}",
    )


@pytest.fixture
def patch_launch_session(monkeypatch):
    """Replace ``launch_session`` so the endpoint never spawns Docker."""
    calls: list[dict[str, Any]] = []

    def fake_launch(*args, **kwargs):
        # First positional is session_type; second is name; record both
        # plus kwargs.
        calls.append({
            "args": list(args),
            "kwargs": dict(kwargs),
        })
        return "container-fake-id"

    import agents.session_launcher as launcher
    monkeypatch.setattr(launcher, "launch_session", fake_launch)
    return calls


@pytest.fixture
def reset_idempotency_cache(monkeypatch):
    """Clear the in-process idempotency cache between tests."""
    from tools.dashboard import server as server_mod
    server_mod._recent_agent_action_dispatches.clear()
    yield
    server_mod._recent_agent_action_dispatches.clear()


@pytest.fixture
def client(
    test_app,
    per_org_universe,
    isolated_dispatch_db,
    patch_launch_session,
    reset_idempotency_cache,
    monkeypatch,
):
    # Deterministic accept-then-work: the background launch task completes
    # before the 202 is written, so post-conditions are assertable inline.
    monkeypatch.setenv("AGENT_ACTIONS_SYNC_LAUNCH", "1")
    with TestClient(test_app) as c:
        from tools.dashboard import unlock_routes
        c.cookies.set(
            unlock_routes.SESSION_COOKIE,
            unlock_routes.mint_session_token(method="test"),
        )
        yield c


# ── Tests ──────────────────────────────────────────────────


def test_dispatch_unknown_member_404(client, per_org_universe):
    asset_id = "11111111-1111-1111-1111-111111111111"
    _insert_note_source(org="autonomy", source_id=asset_id, title="Test note")
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.does-not-exist",
        "asset_id": asset_id,
    })
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "agent-action member not found"
    assert body["target_org"] == "autonomy"


def test_dispatch_refuses_compatibility_before_asset_lookup(
    client, monkeypatch,
):
    from tools.dashboard import api_auth, server

    client.cookies.clear()
    lookups = []

    def get_source(asset_id):
        lookups.append(asset_id)
        return None

    monkeypatch.setattr(graph_ops, "get_source", get_source)
    monkeypatch.setattr(
        server.dao_beads,
        "get_bead",
        lambda asset_id: lookups.append(asset_id) or None,
    )

    response = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": "missing-asset",
    })

    assert response.status_code == 401
    assert api_auth.COMPATIBILITY_PRINCIPAL.authenticated is False
    assert lookups == []


def test_dispatch_refuses_wrong_org_after_location_before_action_lookup(
    client, per_org_universe, monkeypatch,
):
    from tools.dashboard import api_auth, server

    asset_id = "19191919-1919-1919-1919-191919191919"
    _insert_note_source(org="anchore", source_id=asset_id, title="Anchore note")
    action_lookups = []
    monkeypatch.setattr(
        server.api_auth,
        "principal_from_request",
        lambda request: api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.ORG_SESSION,
            subject="autonomy-session",
            org="autonomy",
        ),
    )
    monkeypatch.setattr(
        server,
        "_resolve_agent_action_member",
        lambda **kwargs: action_lookups.append(kwargs) or None,
    )

    response = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })

    assert response.status_code == 404
    assert action_lookups == []


def test_dispatch_creates_agentic_source(client, per_org_universe):
    asset_id = "22222222-2222-2222-2222-222222222222"
    _insert_note_source(
        org="autonomy", source_id=asset_id, title="Architectural Signpost",
    )
    # Minimal request body. The server resolves title / org / type / url
    # from the asset's source row; client-side scrape would be
    # redundant at best and a placeholder leak at worst.
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })
    assert r.status_code == 202
    body = r.json()
    assert body["agentic_source_id"]
    assert body["target_workspace"] == "autonomy-rig"
    assert body["target_org"] == "autonomy"

    # Confirm an agentic source row landed in autonomy's DB with full
    # provenance metadata.
    row = graph_ops.get_source(body["agentic_source_id"])
    assert row is not None
    assert row["type"] == "agentic"
    md = row["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    assert md["set_id"] == AGENT_ACTIONS_SET_ID
    assert md["member_key"] == "note.update-summary"
    assert md["target_source_id"] == asset_id
    assert md["target_org"] == "autonomy"
    # Browser-initiated dispatch → server-side sentinel sender.
    assert md["dispatched_by_session"] == "dashboard"


def test_dispatch_registers_agentic_container_with_live_monitor(
    client, per_org_universe, monkeypatch,
):
    """Agentic runs become resource-visible without waiting for dispatcher."""
    from tools.dashboard import server

    asset_id = "33333333-3333-3333-3333-333333333333"
    _insert_note_source(
        org="autonomy", source_id=asset_id, title="Monitor registration target",
    )
    registered = AsyncMock()
    monkeypatch.setattr(server.session_monitor, "register_session", registered)

    response = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })

    assert response.status_code == 202, response.text
    kwargs = registered.await_args.kwargs
    assert kwargs["tmux_name"] == response.json()["slug"]
    assert kwargs["type"] == "agentic"
    assert kwargs["run_dir"]
    assert kwargs["project"] == "autonomy-rig"
    assert kwargs["harness"] == "claude"


def test_agentic_monitor_rows_are_hidden_from_interactive_sessions(
    client, per_org_universe,
):
    """Agentic telemetry rows belong to Activity, never graph sessions."""
    from tools.dashboard.dao import dashboard_db

    dashboard_db.insert_session(
        "agentic-hidden-from-session-list", "agentic", "autonomy-rig",
    )
    interactive = {
        row["tmux_name"] for row in dashboard_db.get_live_sessions()
    }
    monitored = {
        row["tmux_name"]
        for row in dashboard_db.get_live_sessions(include_agentic=True)
    }
    assert "agentic-hidden-from-session-list" not in interactive
    assert "agentic-hidden-from-session-list" in monitored


def test_dispatch_card_uses_monitored_agentic_activity_when_poller_lags(
    monkeypatch,
):
    """The Activity payload reuses SessionMonitor data without new counters."""
    from agents import dispatcher
    from tools.dashboard import server

    run_id = "agentic-live-fallback"
    monkeypatch.setattr(server.dao_dispatch, "get_running_with_stats", lambda: [{
        "id": run_id,
        "kind": "agentic",
        "status": "RUNNING",
        "started_at": "2026-08-27T10:00:00Z",
        "container_name": run_id,
        "output_dir": "/tmp/agentic-live-fallback",
        "agentic_source_id": "source-id",
        "token_count": None,
        "tool_count": None,
        "turn_count": None,
        "last_snippet": None,
        "last_activity": None,
    }])
    monkeypatch.setattr(server.dao_beads, "get_dispatch_beads", lambda: {
        "approved_waiting": [], "approved_blocked": [],
    })
    monkeypatch.setattr(server.dao_beads, "get_bead_title_priority", lambda _ids: {})
    monkeypatch.setattr(server.resource_monitor, "snapshot", lambda: {"sessions": {}})
    monkeypatch.setattr(server.dashboard_db, "get_session", lambda _id: {
        "context_tokens": 2048,
        "entry_count": 7,
        "last_message": "Working through the change",
        "last_activity": 1_787_825_000.0,
        "harness": "codex",
        "model": "gpt-5.6-terra",
    })
    monkeypatch.setattr(server, "_resolve_agentic_identity", lambda _id: {
        "title": "Live agentic action", "action_label": "Review",
        "member_key": "bead.review", "target_kind": "bead",
        "target_source_id": "auto-test", "target_org": "autonomy",
        "dispatched_by_session": "dashboard", "harness": "claude",
        "model": "fallback",
    })
    monkeypatch.setattr(dispatcher, "_find_jsonl_file", lambda _dir: None)
    monkeypatch.setattr(
        dispatcher, "_agentic_jsonl_metrics",
        lambda _path: ("", 0, 3, None),
    )

    active = asyncio.run(server._collect_dispatch_data())["active"]

    assert len(active) == 1
    row = active[0]
    assert row["token_count"] == 2048
    assert row["turn_count"] == 7
    assert row["tool_count"] == 3
    assert row["last_snippet"] == "Working through the change"
    assert row["last_activity"] == 1_787_825_000.0


def test_dispatch_design_action_uses_design_asset_context(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    from tools.dashboard import server as server_mod

    design = {
        "id": "rev-design-2",
        "latest_revision_id": "rev-design-2",
        "design_id": "series-design",
        "title": "Design library card",
        "description": "Refresh tile preview and subtitle.",
        "status": "pending",
        "revision_count": 2,
        "variant_count": 4,
        "has_fixture": True,
        "first_created_at": "2026-07-02 10:00:00",
        "latest_created_at": "2026-07-02 11:00:00",
        "creator_session_id": "auto-designer",
        "creator_session_label": "Design agent",
    }
    monkeypatch.setattr(
        server_mod,
        "_resolve_design_action_asset",
        lambda asset_id: dict(design) if asset_id in {"series-design", "rev-design-2"} else None,
    )

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "design.refresh-preview",
        "asset_kind": "design",
        "asset_id": "series-design",
    })

    assert r.status_code == 202, r.json()
    body = r.json()
    assert body["target_org"] == "autonomy"
    assert body["target_workspace"] == "autonomy-rig"

    row = graph_ops.get_source(body["agentic_source_id"])
    assert row is not None
    md = row["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    assert md["target_kind"] == "design"
    assert md["target_source_id"] == "rev-design-2"

    prompt = patch_launch_session[-1]["kwargs"]["prompt"]
    assert "Revision: rev-design-2" in prompt
    assert "Series: series-design" in prompt
    assert "Status: pending" in prompt
    assert "/design/rev-design-2" in prompt


def test_dispatch_dispatched_by_session_is_server_sentinel(
    client, per_org_universe,
):
    """The browser cannot pick the sender id; the server stamps a
    fixed sentinel ('dashboard') for every browser-initiated dispatch.
    Any value the client sends in the request body is ignored.
    """
    asset_id = "33333333-3333-3333-3333-333333333333"
    _insert_note_source(org="autonomy", source_id=asset_id, title="x")
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
        # Even if a malicious / out-of-spec client tries to pin a
        # specific session id here, the server overrides.
        "dispatched_by_session": "auto-spoofed-sender",
    })
    assert r.status_code == 202
    src = graph_ops.get_source(r.json()["agentic_source_id"])
    md = src["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    assert md["dispatched_by_session"] == "dashboard"


def test_dispatch_writes_dispatch_runs_row(
    client, per_org_universe, isolated_dispatch_db,
):
    asset_id = "44444444-4444-4444-4444-444444444444"
    _insert_note_source(org="autonomy", source_id=asset_id, title="x")
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })
    assert r.status_code == 202
    body = r.json()

    db_path = isolated_dispatch_db.DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, kind, bead_id, agentic_source_id, image, container_name "
            "FROM dispatch_runs WHERE kind = 'agentic'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    # bead_id stays empty on agentic — readers JOIN to beads via bead_id and
    # interpret NULL/'' as "non-bead lifecycle".
    assert (row["bead_id"] or "") == ""
    # Typed pointer to the graph source.
    assert row["agentic_source_id"] == body["agentic_source_id"]
    # ``id`` is the slug from insert_agentic_session, used by ingest to
    # match JSONL → source.
    assert row["id"] == body["slug"]


def test_dispatch_uses_target_asset_workspace(
    client, per_org_universe, patch_launch_session,
):
    """An anchore note should dispatch into anchore's workspace, not
    autonomy's, regardless of which org the operator is browsing from."""
    asset_id = "55555555-5555-5555-5555-555555555555"
    _insert_note_source(org="anchore", source_id=asset_id, title="Anchore note")
    r = client.post(
        "/api/agent-actions/dispatch",
        json={
            "member_key": "note.update-summary",
            "asset_id": asset_id,
        },
        # Operator's caller-org is autonomy (header), but routing must be
        # by the asset's owning org (anchore).
        headers={"X-Graph-Org": "autonomy"},
    )
    assert r.status_code == 202, r.json()
    assert r.json()["target_org"] == "anchore"
    assert r.json()["target_workspace"] == "anchore-rig"
    assert patch_launch_session, "launch_session was not called"
    # Confirm the launch_session call carried the anchore workspace image.
    call = patch_launch_session[-1]
    args = call["args"]
    # launch_session(session_type, name, prompt, mounts, metadata, detach,
    #                image, working_dir, harness, extra_env, output_dir, model)
    image = args[6] if len(args) >= 7 else call["kwargs"].get("image")
    assert image == "autonomy-session"  # both rigs share the image; routing
    # is asserted by target_workspace=anchore-rig + target_org=anchore.


def test_dispatch_explicit_workspace_materializes_workspace_settings(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    asset_id = "56565656-5656-5656-5656-565656565656"
    _insert_note_source(
        org="autonomy", source_id=asset_id, title="Workspace-heavy note",
    )

    from tools.dashboard import server as server_mod

    prepare_calls: list[tuple[str, str, bool]] = []

    def fake_prepare_session_mounts(workspace, session_name, refresh_existing_worktree):
        prepare_calls.append(
            (workspace.id, session_name, bool(refresh_existing_worktree)),
        )
        return {"/tmp/operator-worktree": "/workspace/repo"}

    monkeypatch.setattr(
        server_mod,
        "prepare_session_mounts",
        fake_prepare_session_mounts,
    )

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.full-workspace",
        "asset_id": asset_id,
    })
    assert r.status_code == 202, r.json()
    body = r.json()
    assert body["target_workspace"] == "operator"
    assert body["target_org"] == "autonomy"
    assert prepare_calls == [("operator", body["slug"], True)]

    call = patch_launch_session[-1]["kwargs"]
    assert call["mounts"] == {"/tmp/operator-worktree": "/workspace/repo"}
    assert call["working_dir"] == "/workspace/repo/tools/dashboard"
    assert call["extra_env"] == {"FEATURE_FLAG": "1"}
    # Startup scripts come from the workspace's provision Setting row
    # (autonomy.workspace.provision#1), materialized into the run dir —
    # the legacy repo-relative ``startup`` path field is deleted. With no
    # provision row seeded, no /startup.sh mounts.
    assert call["startup_script"] is None
    assert call["needs_nested_docker"] is True
    assert call["runtime"] == "privileged"
    assert call["network_host"] is False
    # One canonical org key. The launcher stamps the session token from
    # metadata["org"] alone and refuses to mint without it; graph_project /
    # graph_org were the legacy fallback keys it ignores, and setting only
    # those is what silently broke every agent-actions dispatch.
    assert call["metadata"]["org"] == "autonomy"
    assert "graph_project" not in call["metadata"]
    assert "graph_org" not in call["metadata"]
    assert call["metadata"]["graph_tags"] == ["operator", "agent-actions"]
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["global_claude_md"].name == ".claude_md"


def _seed_cross_org_action(org_db: Path, *, key: str, workspace: str) -> None:
    """One action row naming a workspace this org's DB does not itself hold.

    Same insert shape as ``_seed_actions``, isolated so this specific
    vulnerability case does not perturb the shared per-org action set every
    other test in this file depends on.
    """
    payload = {
        "asset_type": "note",
        "label": "Cross-org workspace probe",
        "model": "claude-haiku-4-5-20251001",
        "workspace": workspace,
        "prompt_template": "Review {asset[id]}.\n",
    }
    schemas.validate_payload(AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION, payload)
    db = GraphDB(org_db)
    try:
        now = "2026-04-28T00:00:00Z"
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, "
            "payload, publication_state, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("ag-" + key.replace(".", "-"), AGENT_ACTIONS_SET_ID,
             AGENT_ACTIONS_REVISION, key, json.dumps(payload), "canonical",
             now, now),
        )
        db.conn.commit()
    finally:
        db.close()


def test_dispatch_refuses_cross_org_workspace_even_when_canonical(
    client, per_org_universe,
):
    """auto-2izkp — the vulnerability this dispatcher-side check closes.

    An action stored in autonomy's DB names ``anchore-rig``, which
    ``per_org_universe`` seeds into anchore's DB at publication_state
    ``canonical`` — read-through visible cross-org by design. Without the
    ``peers=[]`` scoping in the dispatcher's check, this exact row would be
    FOUND via read-through and the dispatch would succeed, running the
    autonomy-authored prompt against anchore's mounted workspace. That is
    the bug; this test fails if the fix regresses to peers=None.
    """
    asset_id = "77777777-7777-7777-7777-777777777777"
    _insert_note_source(org="autonomy", source_id=asset_id, title="Cross-org probe")
    _seed_cross_org_action(
        per_org_universe / "autonomy.db",
        key="note.cross-org-probe",
        workspace="anchore-rig",
    )

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.cross-org-probe",
        "asset_id": asset_id,
    })

    assert r.status_code == 409, r.json()
    assert r.json()["error"] == "unknown workspace"
    # Same shape as a workspace that does not exist anywhere — deliberately
    # indistinguishable from "not found", matching the existing
    # target_org_auth_error policy of never confirming that something
    # exists in an org the caller cannot see.
    assert r.json()["workspace"] == "anchore-rig"


def test_dispatch_accepts_same_org_workspace_unaffected(
    client, per_org_universe,
):
    """The control. Without it, a check that refused everything would pass
    the test above for the wrong reason."""
    asset_id = "88888888-8888-8888-8888-888888888888"
    _insert_note_source(org="autonomy", source_id=asset_id, title="Same-org probe")
    _seed_cross_org_action(
        per_org_universe / "autonomy.db",
        key="note.same-org-probe",
        workspace="autonomy-rig",
    )

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.same-org-probe",
        "asset_id": asset_id,
    })

    assert r.status_code == 202, r.json()


def test_dispatch_bead_action_creates_agentic_source(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    bead = {
        "id": "auto-bead-101",
        "title": "Audit the bead action path",
        "status": "open",
        "priority": 1,
        "description": "Dry-run implement the requested behavior.",
        "design": "Reuse dashboard.agent-actions with bead-aware context.",
        "acceptance_criteria": "Produce a structured bead audit.",
        "notes": "No merge. No commit required.",
    }
    _patch_bead_runtime(monkeypatch, bead)

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "bead.dry-run-implement",
        "asset_id": bead["id"],
    })
    assert r.status_code == 202, r.json()
    body = r.json()
    assert body["target_org"] == "autonomy"
    assert body["target_workspace"] == "autonomy-rig"

    row = graph_ops.get_source(body["agentic_source_id"])
    assert row is not None
    md = row["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    assert md["target_kind"] == "bead"
    assert md["target_source_id"] == bead["id"]

    prompt = patch_launch_session[-1]["kwargs"]["prompt"]
    assert f"Bead: {bead['id']}" in prompt
    assert f"Title: {bead['title']}" in prompt
    assert "Primer for auto-bead-101" in prompt


def test_dispatch_send_to_uses_crosstalk(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    """Universal Send-To delivers a primer via tmux + crosstalk_messages,
    and does NOT create an agentic source row."""
    asset_id = "66666666-6666-6666-6666-666666666666"
    _insert_note_source(org="autonomy", source_id=asset_id, title="Note")

    sent: list[tuple[str, str]] = []

    async def fake_tmux_send(target, text):
        sent.append((target, text))

    inserted: list[dict] = []

    def fake_insert_message(
        sender, sender_label, target, sender_source_id, sender_entry_count,
        message, ts,
    ):
        inserted.append({
            "sender": sender, "target": target, "message": message,
        })

    from tools.dashboard import server as server_mod
    monkeypatch.setattr(server_mod, "tmux_send", fake_tmux_send)
    monkeypatch.setattr(server_mod, "_tmux_session_exists", lambda n: True)
    monkeypatch.setattr(server_mod.auth_db, "insert_message", fake_insert_message)

    # Snapshot existing agentic sources so we can prove none were added.
    pre_sources = _list_agentic_sources()

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "session.send-to",
        "asset_id": asset_id,
        "target_session_name": "auto-target-1234",
    })
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["ok"] is True
    assert body["sent_to"] == "auto-target-1234"
    # The mock and real paths both surface the primer body so callers can
    # log / display the field shape without reconstructing it.
    primer_body = body.get("primer_body") or ""
    assert primer_body, "primer_body missing from response"

    assert len(sent) == 1
    target, envelope = sent[0]
    assert target == "auto-target-1234"
    # The envelope's label must be the dashboard subsystem name, not the
    # bare 'dashboard' sentinel.
    assert 'label="Dashboard Send-To"' in envelope, envelope
    # Primer fields: full asset id, action key, no asset_url, no
    # sender_session line. The crosstalk envelope's ``from`` attribute
    # is the server-side sentinel ``dashboard`` for browser-initiated
    # dispatches.
    assert f"asset_id: {asset_id}" in envelope
    assert "action: session.send-to" in envelope
    assert "asset_url:" not in envelope, "asset_url line must be removed"
    assert "sender_session:" not in envelope, "sender_session line must be removed"
    assert 'from="dashboard"' in envelope

    # No agentic source row should have been created.
    assert _list_agentic_sources() == pre_sources
    # The launch_session helper should NOT have been called for a
    # universal Send-To.
    assert not patch_launch_session


def test_dispatch_send_to_dead_session_404(
    client, per_org_universe, monkeypatch,
):
    asset_id = "77777777-7777-7777-7777-777777777777"
    _insert_note_source(org="autonomy", source_id=asset_id, title="Note")
    from tools.dashboard import server as server_mod
    monkeypatch.setattr(server_mod, "_tmux_session_exists", lambda n: False)

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "session.send-to",
        "asset_id": asset_id,
        "target_session_name": "auto-dead-session",
    })
    assert r.status_code == 404
    body = r.json()
    assert body["target_session"] == "auto-dead-session"


def test_dispatch_idempotency_window(
    client, per_org_universe, patch_launch_session,
):
    """Two POSTs within 5s with the same key return the same source id and
    leave only one agentic row in the graph."""
    asset_id = "88888888-8888-8888-8888-888888888888"
    _insert_note_source(org="autonomy", source_id=asset_id, title="x")

    first = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })
    assert first.status_code == 202, first.json()
    second = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })
    assert second.status_code in (200, 201)

    assert (
        first.json()["agentic_source_id"]
        == second.json()["agentic_source_id"]
    )

    # Only ONE agentic source row exists for this asset.
    rows = [
        r for r in _list_agentic_sources()
        if r["metadata"].get("target_source_id") == asset_id
    ]
    assert len(rows) == 1


def test_dispatch_unknown_asset_404(client, per_org_universe):
    """Unknown asset_id returns 404 with a clear error."""
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": "deadbeef-dead-beef-dead-beefdeadbeef",
    })
    assert r.status_code == 404


def test_dispatch_request_body_is_minimal(client, per_org_universe):
    """The dispatch request body carries only ``asset_id`` + ``member_key``
    (plus action-specific params like ``target_session_name`` for
    Send-To). Everything else — title, short_description, type, org,
    url, sender — is server-derived from the resolved source row.

    This test pins the wire contract: a dispatch with the bare-minimum
    body must succeed, and the agentic source's metadata must reflect
    server-side values (not anything the client could spoof).
    """
    asset_id = "99999999-9999-9999-9999-999999999999"
    _insert_note_source(
        org="autonomy", source_id=asset_id, title="Real DB Title",
    )

    r = client.post(
        "/api/agent-actions/dispatch",
        json={"member_key": "note.update-summary", "asset_id": asset_id},
    )
    assert r.status_code == 202, r.json()
    src = graph_ops.get_source(r.json()["agentic_source_id"])
    md = src["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    # Server-derived: target_org from the source row, target_source_id is
    # the canonical UUID, dispatched_by_session is the sentinel.
    assert md["target_org"] == "autonomy"
    assert md["target_source_id"] == asset_id
    assert md["dispatched_by_session"] == "dashboard"


def test_api_dispatch_runs_surfaces_agentic_identity(
    client, per_org_universe, isolated_dispatch_db,
):
    """``/api/dispatch/runs`` must surface the agentic identity fields
    that drive timeline-card display and routing:

    - ``bead_id`` is empty for ``kind='agentic'`` (the run-id used to
      get stuffed in there, which made ``routeForRun`` compose
      ``/bead/<run-id>`` → 404).
    - ``agentic_source_id`` is present (typed pointer to the agent's
      session source row).
    - ``target_source_id`` is present (the asset the action operates
      on; this is what ``routeForRun`` should send the operator to).
    - ``target_org`` is present.
    - ``member_key`` is present (the action key, for badges).
    - ``action_label`` is the agentic source's display label
      ("Update Title & Summary").
    - ``title`` is the target asset's title for card display.
    """
    asset_title = "Architectural Signpost — Worktrees Spec"
    asset_id = "abcdef00-0000-0000-0000-000000000abc"
    _insert_note_source(
        org="autonomy", source_id=asset_id, title=asset_title,
    )
    r = client.post(
        "/api/agent-actions/dispatch",
        json={"member_key": "note.update-summary", "asset_id": asset_id},
    )
    assert r.status_code == 202, r.json()
    agentic_source_id = r.json()["agentic_source_id"]

    runs_resp = client.get("/api/dispatch/runs")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    matches = [
        run for run in runs
        if run.get("kind") == "agentic"
        and run.get("agentic_source_id") == agentic_source_id
    ]
    assert len(matches) == 1, (
        f"agentic run not surfaced; got: {[r for r in runs if r.get('kind') == 'agentic']}"
    )
    row = matches[0]
    assert (row.get("bead_id") or "") == "", (
        "bead_id must stay empty for kind='agentic' so routing falls "
        "through to the agentic_source_id / target_source_id branch"
    )
    assert row["agentic_source_id"] == agentic_source_id
    assert row.get("target_source_id") == asset_id
    assert row.get("target_org") == "autonomy"
    assert row.get("member_key") == "note.update-summary"
    # Action label comes from the action's Setting payload `label` field.
    # The fixture's agent-action member is registered with label
    # "Update Title & Summary"; if that fixture changes, this assertion
    # follows.
    assert row.get("action_label"), "action_label must populate"
    # Title shows the asset, not the action — the card reads "<asset>".
    assert row.get("title") == asset_title
    # Sender provenance: browser-initiated dispatches carry the
    # "dashboard" sentinel; the front-end uses this to decide whether
    # to render a clickable link back to the originating session
    # (real tmux name) vs plain text (sentinel).
    assert row.get("dispatched_by_session") == "dashboard"


def test_api_dispatch_runs_surfaces_agentic_bead_identity(
    client,
    per_org_universe,
    isolated_dispatch_db,
    patch_launch_session,
    monkeypatch,
):
    bead = {
        "id": "auto-bead-runs",
        "title": "Dispatch row should point back to bead",
        "status": "open",
        "priority": 2,
        "description": "Verify bead-targeted agentic identity.",
        "design": "",
        "acceptance_criteria": "",
        "notes": "",
    }
    _patch_bead_runtime(monkeypatch, bead)

    r = client.post(
        "/api/agent-actions/dispatch",
        json={"member_key": "bead.dry-run-implement", "asset_id": bead["id"]},
    )
    assert r.status_code == 202, r.json()
    agentic_source_id = r.json()["agentic_source_id"]

    runs_resp = client.get("/api/dispatch/runs")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    matches = [
        run for run in runs
        if run.get("kind") == "agentic"
        and run.get("agentic_source_id") == agentic_source_id
    ]
    assert len(matches) == 1
    row = matches[0]
    assert (row.get("bead_id") or "") == ""
    assert row.get("target_kind") == "bead"
    assert row.get("target_source_id") == bead["id"]
    assert row.get("target_org") == "autonomy"
    assert row.get("member_key") == "bead.dry-run-implement"
    assert row.get("action_label") == "Dry-Run Implement"
    assert row.get("title") == bead["title"]


def test_dispatch_canonicalises_prefix_asset_id(client, per_org_universe):
    """Browser sends a 12-char prefix (the URL-derived form);
    downstream uses must see the canonical UUID — the agent's
    ``graph read`` and the dispatch_runs row both depend on it."""
    asset_id = "abcdef12-3456-7890-abcd-ef1234567890"
    _insert_note_source(org="autonomy", source_id=asset_id, title="t")

    prefix = asset_id[:12]
    r = client.post(
        "/api/agent-actions/dispatch",
        json={"member_key": "note.update-summary", "asset_id": prefix},
    )
    assert r.status_code == 202, r.json()
    src = graph_ops.get_source(r.json()["agentic_source_id"])
    md = src["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    # target_source_id is the full canonical UUID, not the prefix.
    assert md["target_source_id"] == asset_id


def test_dispatch_trace_agentic_shape(
    test_app, per_org_universe, isolated_dispatch_db,
    patch_launch_session, reset_idempotency_cache,
):
    """``/api/dispatch/trace/<run>`` for kind='agentic' must return a
    response shaped for the agentic UI: no bead lookup (so the
    ``bd show`` ambiguous-id error doesn't crowd the response), no
    diff/experience, and the agentic identity fields surfaced
    (action_label, target_source_id, target_org, member_key, sender,
    target_title).

    Regression: before this, the trace page rendered almost nothing
    for agentic dispatches because the endpoint returned a bead-shaped
    response with empty fields and a ``bd show ""`` error.
    """
    importlib.reload(__import__("tools.dashboard.dao.dispatch", fromlist=["x"]))

    asset_title = "Anchor note for trace test"
    asset_id = "deadbeef-0000-0000-0000-deadbeef0099"
    _insert_note_source(org="autonomy", source_id=asset_id, title=asset_title)

    with TestClient(test_app) as client:
        r = client.post(
            "/api/agent-actions/dispatch",
            json={"member_key": "note.update-summary", "asset_id": asset_id},
        )
        assert r.status_code == 202, r.json()
        run_id = r.json()["slug"]

        trace = client.get(f"/api/dispatch/trace/{run_id}")
        assert trace.status_code == 200, trace.text
        body = trace.json()

    # Kind is surfaced so the front-end knows which template to render.
    assert body["kind"] == "agentic"
    # No bead lookup — empty bead_id, null bead. The previous bug
    # called ``bd show ""`` and stuffed a multi-thousand-char
    # "ambiguous ID" error string into ``bead``.
    assert (body.get("bead_id") or "") == ""
    assert body.get("bead") is None, (
        f"agentic trace must not invoke bd show; bead={body.get('bead')!r}"
    )
    # No commit/diff/experience for agentic.
    assert body.get("commit_hash") == ""
    assert body.get("diff") == ""
    assert body.get("experience_report") == ""
    # Agentic identity fields drive the new template branch.
    assert body.get("agentic_source_id") == r.json()["agentic_source_id"]
    assert body.get("action_label") == "Update Title & Summary"
    assert body.get("target_source_id") == asset_id
    assert body.get("target_org") == "autonomy"
    assert body.get("member_key") == "note.update-summary"
    assert body.get("target_title") == asset_title
    # Browser-initiated → "dashboard" sentinel + no project link.
    assert body.get("dispatched_by_session") == "dashboard"


def test_dispatch_trace_agentic_bead_shape(
    test_app,
    per_org_universe,
    isolated_dispatch_db,
    patch_launch_session,
    reset_idempotency_cache,
    monkeypatch,
):
    importlib.reload(__import__("tools.dashboard.dao.dispatch", fromlist=["x"]))

    bead = {
        "id": "auto-bead-trace",
        "title": "Trace should surface bead target",
        "status": "open",
        "priority": 2,
        "description": "Ensure bead-targeted trace metadata is populated.",
        "design": "",
        "acceptance_criteria": "",
        "notes": "",
    }
    _patch_bead_runtime(monkeypatch, bead)

    with TestClient(test_app) as client:
        r = client.post(
            "/api/agent-actions/dispatch",
            json={
                "member_key": "bead.dry-run-implement",
                "asset_id": bead["id"],
            },
        )
        assert r.status_code == 202, r.json()
        run_id = r.json()["slug"]

        trace = client.get(f"/api/dispatch/trace/{run_id}")
        assert trace.status_code == 200, trace.text
        body = trace.json()

    assert body["kind"] == "agentic"
    assert (body.get("bead_id") or "") == ""
    assert body.get("bead") is None
    assert body.get("agentic_source_id") == r.json()["agentic_source_id"]
    assert body.get("action_label") == "Dry-Run Implement"
    assert body.get("target_kind") == "bead"
    assert body.get("target_source_id") == bead["id"]
    assert body.get("target_org") == "autonomy"
    assert body.get("member_key") == "bead.dry-run-implement"
    assert body.get("target_title") == bead["title"]
    assert body.get("dispatched_by_session") == "dashboard"


@pytest.mark.asyncio
async def test_live_active_resolves_agentic_target_title(
    test_app, per_org_universe, isolated_dispatch_db,
    patch_launch_session, reset_idempotency_cache,
):
    """The SSE-fed live-active list (built by ``_collect_dispatch_data``)
    must resolve the *target asset* title for ``kind='agentic'`` rows —
    not the agentic source's own title (which is the action label
    "Update Title & Summary").

    Regression: production /dispatch page was rendering
    "Update Title & Summary" as the bold title for a RUNNING agentic
    because the live-active builder only read the agentic source's
    own title and never followed ``metadata.target_source_id`` to
    the asset. ``/api/dispatch/runs`` did the lookup correctly via
    ``_enrich_dispatch_runs``; the live builder didn't. Now both
    paths share ``_resolve_agentic_identity``.
    """
    # Reload the dao reader so its module-level DB_PATH picks up
    # ``isolated_dispatch_db``'s tmp path. Without this, the writer
    # (agents.dispatch_db) writes to the per-test DB while the reader
    # (tools.dashboard.dao.dispatch) reads from conftest's pid-tmp DB,
    # so the live-active builder finds zero rows.
    importlib.reload(__import__("tools.dashboard.dao.dispatch", fromlist=["x"]))

    asset_title = "Worktrees: per-SHA terminal-commits cache for squash-merge closure"
    asset_id = "deadbeef-0000-0000-0000-000000000123"
    _insert_note_source(org="autonomy", source_id=asset_id, title=asset_title)

    # Dispatch via the API so we get a real agentic source row + a
    # real dispatch_runs row (kind='agentic', no title column).
    with TestClient(test_app) as client:
        r = client.post(
            "/api/agent-actions/dispatch",
            json={"member_key": "note.update-summary", "asset_id": asset_id},
        )
    assert r.status_code == 202, r.json()
    agentic_source_id = r.json()["agentic_source_id"]

    # Sanity: the agentic source's own title is the action label, not
    # the asset title. This is the trap the live builder fell into.
    src = graph_ops.get_source(agentic_source_id)
    assert src["title"] == "Update Title & Summary", (
        f"agentic source's own title should be the action label; got {src['title']!r}"
    )

    # Drive the live-active builder directly. No HTTP — SSE consumers
    # call this function and read its output.
    from tools.dashboard import server as srvmod
    payload = await srvmod._collect_dispatch_data()
    active = payload.get("active") or []
    matches = [
        a for a in active
        if a.get("kind") == "agentic"
        and a.get("agentic_source_id") == agentic_source_id
    ]
    assert len(matches) == 1, (
        f"agentic row missing from live-active payload; got: {active!r}"
    )
    row = matches[0]
    # Title resolves to the *target asset*, not the action label.
    assert row["title"] == asset_title, (
        f"live-active title must be target asset title; got {row['title']!r}. "
        "If this regresses, _collect_dispatch_data is reading the agentic "
        "source's own title (the action_label) and not following "
        "metadata.target_source_id to the asset."
    )
    # Agentic identity fields surfaced for downstream rendering/routing.
    assert row.get("action_label") == "Update Title & Summary"
    assert row.get("target_source_id") == asset_id
    assert row.get("target_org") == "autonomy"
    assert row.get("member_key") == "note.update-summary"
    assert row.get("dispatched_by_session") == "dashboard"


# ── ``custom_input`` / ``input_prompt`` (auto-0tkwj) ───────


def test_dispatch_custom_input_renders_into_template(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    """An action whose template uses ``{custom_input}`` must receive the
    operator-typed value verbatim in the rendered prompt.
    """
    bead = {
        "id": "auto-bead-ask-1",
        "title": "Bead with question",
        "status": "open",
        "priority": 1,
        "description": "Something to ask about.",
        "design": "",
        "acceptance_criteria": "",
        "notes": "",
    }
    _patch_bead_runtime(monkeypatch, bead)

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "bead.ask-question",
        "asset_id": bead["id"],
        "custom_input": "Why does this dispatch use Haiku?",
    })
    assert r.status_code == 202, r.json()

    prompt = patch_launch_session[-1]["kwargs"]["prompt"]
    assert "Question: Why does this dispatch use Haiku?" in prompt
    assert f"Bead: {bead['id']}" in prompt


def test_dispatch_input_prompt_rejects_missing_custom_input(
    client, per_org_universe, monkeypatch,
):
    """An action with ``input_prompt`` set must 400 when the request omits
    ``custom_input`` (or sends an empty/whitespace string).
    """
    bead = {
        "id": "auto-bead-ask-2",
        "title": "x",
        "status": "open",
        "priority": 1,
        "description": "",
        "design": "",
        "acceptance_criteria": "",
        "notes": "",
    }
    _patch_bead_runtime(monkeypatch, bead)

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "bead.ask-question",
        "asset_id": bead["id"],
        # custom_input omitted entirely
    })
    assert r.status_code == 400, r.json()
    assert "input" in r.json().get("error", "").lower()

    r2 = client.post("/api/agent-actions/dispatch", json={
        "member_key": "bead.ask-question",
        "asset_id": bead["id"],
        "custom_input": "   \n  \t",  # whitespace-only is also rejected
    })
    assert r2.status_code == 400, r2.json()


def test_dispatch_custom_input_ignored_for_non_input_prompt_actions(
    client, per_org_universe, patch_launch_session,
):
    """Actions without ``input_prompt`` (dry-run, send-to, etc.) must
    accept and silently ignore a stray ``custom_input`` field — the
    feature is back-compat-safe.
    """
    asset_id = "abcdef34-3434-3434-3434-343434343434"
    _insert_note_source(org="autonomy", source_id=asset_id, title="t")
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
        "custom_input": "this should be ignored — template doesn't reference it",
    })
    assert r.status_code == 202, r.json()


def test_dispatch_template_validates_custom_input_placeholder(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    """A template that references ``{custom_input}`` must validate against
    the placeholder allowlist (i.e. must not be rejected as an unknown
    root) — the bead.ask-question prompt depends on this.
    """
    bead = {
        "id": "auto-bead-ask-validate",
        "title": "x",
        "status": "open",
        "priority": 1,
        "description": "",
        "design": "",
        "acceptance_criteria": "",
        "notes": "",
    }
    _patch_bead_runtime(monkeypatch, bead)

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "bead.ask-question",
        "asset_id": bead["id"],
        "custom_input": "What is this bead's design rationale?",
    })
    # 201 implies the strict template validator accepted ``{custom_input}``.
    # Pre-fix this would have been a 500 with "undefined placeholder".
    assert r.status_code == 202, r.json()


def test_render_rejects_unknown_template_root():
    """Direct exercise of the placeholder allowlist: a template that
    references a non-allowed root (e.g. ``{wrong_root}``) must still
    fail render. Guarantees the allowlist gain didn't open a hole.
    """
    from tools.dashboard.server import _render_agent_action_prompt

    with pytest.raises(ValueError, match="undefined placeholder"):
        _render_agent_action_prompt(
            "Hello {wrong_root}",
            page_context={"asset": {"id": "x"}},
            dispatched_by_session="dashboard",
            member_key="x.y",
            custom_input="",
        )


# ── Helpers ────────────────────────────────────────────────


def _list_agentic_sources() -> list[dict]:
    """Return every ``type='agentic'`` source row across known org DBs."""
    out: list[dict] = []
    for ref in org_ops.list_orgs():
        db = GraphDB(Path(ref.db_path))
        try:
            rows = db.conn.execute(
                "SELECT id, type, metadata FROM sources "
                "WHERE type = 'agentic'"
            ).fetchall()
        finally:
            db.close()
        for r in rows:
            md = r["metadata"]
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except json.JSONDecodeError:
                    md = {}
            out.append({"id": r["id"], "metadata": md, "project": ref.slug})
    return out


# ── Agentic launch cap (2026-08-28 incident, handoff item 2) ─────────

def test_agentic_running_count_ignores_stale_and_foreign_rows(monkeypatch):
    """Only recent RUNNING agentic rows count toward the launch cap.

    Wedged rows (killed/--rm'd containers stuck RUNNING forever) age out
    of the window instead of starving dispatching; bead rows and rows
    with unparsable start times never count.
    """
    from datetime import datetime, timedelta, timezone
    from tools.dashboard import server as server_mod

    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    # Shapes as get_active_agentic_runs returns them: agentic-only (the
    # SQL prefilters kind), across QUEUED/PREPARING/RUNNING.
    rows = [
        {"status": "RUNNING", "started_at": (now - timedelta(minutes=5)).strftime(fmt)},
        {"status": "QUEUED", "started_at": (now - timedelta(minutes=59)).strftime(fmt)},
        {"status": "RUNNING", "started_at": (now - timedelta(hours=26)).strftime(fmt)},  # wedged
        {"status": "PREPARING", "started_at": None},                                     # wedged
    ]
    import agents.dispatch_db as dispatch_db_mod
    monkeypatch.setattr(dispatch_db_mod, "get_active_agentic_runs", lambda: rows)
    assert server_mod._agentic_running_count_recent() == 2


def test_dispatch_rejects_at_agentic_cap(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    """At the cap the endpoint 429s BEFORE creating the agentic source
    row and before any container spawn."""
    from tools.dashboard import server as server_mod

    monkeypatch.setattr(
        server_mod, "_agentic_running_count_recent", lambda: 10,
    )
    monkeypatch.setattr(
        server_mod, "_resolved_dispatch_limits",
        lambda: {"bead_max_concurrent": 2, "agentic_max_concurrent": 10},
    )
    asset_id = "44444444-4444-4444-4444-444444444444"
    _insert_note_source(org="autonomy", source_id=asset_id, title="capped")
    pre_sources = _list_agentic_sources()

    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })
    assert r.status_code == 429, r.json()
    body = r.json()
    assert body["cap"] == 10
    assert body["running"] == 10
    assert _list_agentic_sources() == pre_sources, (
        "no agentic source row may be created for a rejected dispatch")
    assert patch_launch_session == [], "no container spawn at the cap"


def test_dispatch_under_cap_proceeds(
    client, per_org_universe, patch_launch_session, monkeypatch,
):
    """One below the cap, the dispatch flows normally end to end."""
    from tools.dashboard import server as server_mod

    monkeypatch.setattr(
        server_mod, "_agentic_running_count_recent", lambda: 9,
    )
    monkeypatch.setattr(
        server_mod, "_resolved_dispatch_limits",
        lambda: {"bead_max_concurrent": 2, "agentic_max_concurrent": 10},
    )
    asset_id = "55555555-5555-5555-5555-555555555555"
    _insert_note_source(org="autonomy", source_id=asset_id, title="undercap")
    r = client.post("/api/agent-actions/dispatch", json={
        "member_key": "note.update-summary",
        "asset_id": asset_id,
    })
    assert r.status_code == 202, r.json()
    assert len(patch_launch_session) == 1


def test_dispatch_limits_settings_round_trip(client, per_org_universe):
    """GET serves schema defaults untouched; POST persists to the machine
    store and GET reflects it; out-of-range values 400 without persisting."""
    r = client.get("/api/dispatch/limits")
    assert r.status_code == 200
    assert r.json() == {"bead_max_concurrent": 2, "agentic_max_concurrent": 10}

    r = client.post("/api/dispatch/limits", json={"agentic_max_concurrent": 3})
    assert r.status_code == 200, r.json()
    assert r.json()["agentic_max_concurrent"] == 3
    assert r.json()["bead_max_concurrent"] == 2

    r = client.get("/api/dispatch/limits")
    assert r.json()["agentic_max_concurrent"] == 3

    r = client.post("/api/dispatch/limits", json={"bead_max_concurrent": 999})
    assert r.status_code == 400
    assert client.get("/api/dispatch/limits").json()["bead_max_concurrent"] == 2


def test_prelaunch_row_lifecycle_and_orphan_sweep(isolated_dispatch_db):
    """QUEUED rows promote forward-only and restart-sweep to FAILED."""
    from agents.dispatch_db import (
        fail_stale_prelaunch_runs,
        get_active_agentic_runs,
        init_db,
        insert_launch_run,
        update_run_status,
    )
    init_db()
    insert_launch_run(
        run_id="agent-q-1", bead_id="", started_at=time.time(),
        branch="", branch_base="", image="img", container_name="agent-q-1",
        output_dir="/tmp/run-q1", kind="agentic",
        agentic_source_id="src-1", status="QUEUED",
    )
    rows = get_active_agentic_runs()
    assert [r["status"] for r in rows] == ["QUEUED"]

    update_run_status("agent-q-1", "PREPARING")
    assert get_active_agentic_runs()[0]["status"] == "PREPARING"
    update_run_status("agent-q-1", "RUNNING")
    assert get_active_agentic_runs()[0]["status"] == "RUNNING"
    # terminal states never come through this updater
    update_run_status("agent-q-1", "DONE")
    assert get_active_agentic_runs()[0]["status"] == "RUNNING"

    insert_launch_run(
        run_id="agent-q-2", bead_id="", started_at=time.time(),
        branch="", branch_base="", image="img", container_name="agent-q-2",
        output_dir="/tmp/run-q2", kind="agentic",
        agentic_source_id="src-2", status="QUEUED",
    )
    assert fail_stale_prelaunch_runs() == 1     # q-2 only; RUNNING q-1 kept
    statuses = {r["id"]: r["status"] for r in get_active_agentic_runs()}
    assert statuses == {"agent-q-1": "RUNNING"}


def test_orphan_sweep_finalizes_dead_container_runs(isolated_dispatch_db):
    """A RUNNING agentic row whose container vanished finalizes as
    orphaned-no-exit after the age threshold; young or alive rows stay."""
    from agents.dispatch_db import (
        fail_orphaned_running_agentic,
        get_active_agentic_runs,
        init_db,
        insert_launch_run,
    )
    init_db()
    old = time.time() - 3600
    insert_launch_run(
        run_id="agent-dead", bead_id="", started_at=old, branch="",
        branch_base="", image="img", container_name="agent-dead",
        output_dir="/tmp/rd", kind="agentic", agentic_source_id="s1",
    )
    insert_launch_run(
        run_id="agent-alive", bead_id="", started_at=old, branch="",
        branch_base="", image="img", container_name="agent-alive",
        output_dir="/tmp/ra", kind="agentic", agentic_source_id="s2",
    )
    insert_launch_run(
        run_id="agent-young", bead_id="", started_at=time.time(), branch="",
        branch_base="", image="img", container_name="agent-young",
        output_dir="/tmp/ry", kind="agentic", agentic_source_id="s3",
    )
    failed = fail_orphaned_running_agentic(
        container_exists=lambda name: name == "agent-alive",
    )
    assert failed == ["agent-dead"]
    remaining = {r["id"] for r in get_active_agentic_runs()}
    assert remaining == {"agent-alive", "agent-young"}


def test_queued_agentic_rows_render_in_waiting_section(
    client, per_org_universe, monkeypatch,
):
    """A QUEUED/PREPARING agentic run appears in the dispatch payload's
    waiting section (the approved-waiting-for-dispatch surface) with its
    status and routing fields — visible backpressure, not an open HTTP
    request."""
    from tools.dashboard import server as server_mod
    from agents.dispatch_db import init_db, insert_launch_run

    init_db()
    insert_launch_run(
        run_id="agent-wait-1", bead_id="", started_at=time.time(),
        branch="", branch_base="", image="img",
        container_name="agent-wait-1", output_dir="/tmp/rw", kind="agentic",
        agentic_source_id="src-wait", status="QUEUED",
    )
    monkeypatch.setattr(
        server_mod, "_resolve_agentic_identity",
        lambda sid: {
            "action_label": "note.analyze", "member_key": "note.analyze",
            "target_kind": "source", "target_source_id": "tgt-1",
            "target_org": "autonomy", "dispatched_by_session": "",
            "harness": "claude", "model": None, "title": "Analyze the note",
        },
    )
    data = asyncio.run(server_mod._collect_dispatch_data())
    queued = [w for w in data["waiting"] if w.get("kind") == "agentic"]
    assert len(queued) == 1
    entry = queued[0]
    assert entry["id"] == "agent-wait-1"
    assert entry["status"] == "queued"
    assert entry["title"] == "Analyze the note"
    assert entry["agentic_source_id"] == "src-wait"
    assert entry["target_source_id"] == "tgt-1"
    # and it is NOT double-listed as active
    assert all(a.get("id") != "agent-wait-1" for a in data["active"])
