"""API tests for ``POST /api/agent-actions/dispatch`` (auto-pqgrl).

The endpoint dispatches an agentic action against a graph asset. Routing
is strictly own-org-of-asset: actions defined in ``anchore.db`` only
render on anchore notes; the agent runs in the workspace registered to
the asset's owning org. Universal Send-To is special-cased — it does not
spawn an agent, only delivers a CrossTalk primer.

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
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

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
    orgs_dir.mkdir()
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
                    "Update the title for {asset_id}.\n"
                    "Page: {asset_url}\n"
                    "Sender: {dispatched_by_session}\n"
                ),
                "estimated_seconds": 10,
                "writes": ["source.title"],
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


def _seed_workspace(org_db: Path, *, workspace_id: str) -> None:
    """Insert an ``autonomy.workspace#1`` row keyed on *workspace_id*."""
    payload = {"name": workspace_id, "image": "autonomy-agent"}
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
        id=source_id, type="docs", platform="local", project=org,
        title=title, file_path=f"note:{source_id}",
        publication_state="canonical",
    )
    db = graph_ops._open(org)  # type: ignore[attr-defined]
    try:
        db.insert_source(src)
    finally:
        db.close()


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
):
    with TestClient(test_app) as c:
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
    assert r.status_code == 201
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
    assert r.status_code == 201
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
    assert r.status_code == 201
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
    assert r.status_code == 201, r.json()
    assert r.json()["target_org"] == "anchore"
    assert r.json()["target_workspace"] == "anchore-rig"
    assert patch_launch_session, "launch_session was not called"
    # Confirm the launch_session call carried the anchore workspace image.
    call = patch_launch_session[-1]
    args = call["args"]
    # launch_session(session_type, name, prompt, mounts, metadata, detach,
    #                image, working_dir, harness, extra_env, output_dir, model)
    image = args[6] if len(args) >= 7 else call["kwargs"].get("image")
    assert image == "autonomy-agent"  # both rigs share the image; routing
    # is asserted by target_workspace=anchore-rig + target_org=anchore.


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
    assert first.status_code == 201, first.json()
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
    assert r.status_code == 201, r.json()
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
    assert r.status_code == 201, r.json()
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
    assert r.status_code == 201, r.json()
    src = graph_ops.get_source(r.json()["agentic_source_id"])
    md = src["metadata"]
    if isinstance(md, str):
        md = json.loads(md)
    # target_source_id is the full canonical UUID, not the prefix.
    assert md["target_source_id"] == asset_id


# ── Helpers ────────────────────────────────────────────────


def _list_agentic_sources() -> list[dict]:
    """Return every ``type='agentic'`` source row across known org DBs."""
    out: list[dict] = []
    for ref in org_ops.list_orgs():
        db = GraphDB(Path(ref.db_path))
        try:
            rows = db.conn.execute(
                "SELECT id, type, metadata, project FROM sources "
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
            out.append({"id": r["id"], "metadata": md, "project": r["project"]})
    return out
