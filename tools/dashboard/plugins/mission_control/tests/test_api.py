"""API tests for the Mission Control plugin."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard.dao import mission_control_db as db
from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Route every DAO call in this test module at a fresh per-test DB.

    The API layer calls ``mission_control_db`` functions without a
    ``db_path`` override, so isolation happens by monkeypatching the
    module-level default.
    """
    path = tmp_path / "mission_control.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)


@pytest.fixture(autouse=True)
def _no_real_presence_writes():
    """push_site_revision/answer_question fire a best-effort coordinator
    presence heartbeat (tools.graph.surface.Presence) that isn't scoped by
    _isolated_db above -- it writes through the real graph Settings
    substrate. Autoused so no test in this module (present or future) can
    silently leak a presence row into shared state; tests that care about
    the heartbeat itself override with their own @patch."""
    with patch("tools.graph.surface.Presence", MagicMock()):
        yield


class _OperatorPrincipalMiddleware:
    """Give non-auth-focused plugin tests the production operator context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope.setdefault("state", {})["api_principal"] = api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
            subject="test-operator",
        )
        await self.app(scope, receive, send)


def _client() -> TestClient:
    app = Starlette(
        routes=mc_api.routes,
        middleware=[Middleware(_OperatorPrincipalMiddleware)],
    )
    return TestClient(app)


def _https_client() -> TestClient:
    """Cookie-round-trip tests need an https:// base — the visitor
    cookie is set with secure=True (matches the unlock system's own
    session cookie convention), and httpx's cookie jar won't carry a
    Secure cookie back over a plain http:// scheme."""
    app = Starlette(
        routes=mc_api.routes,
        middleware=[Middleware(_OperatorPrincipalMiddleware)],
    )
    return TestClient(app, base_url="https://testserver")


def test_manifest_declares_skill_doc():
    plugin_dir = mc_api.__file__
    from pathlib import Path
    plugin_dir = Path(plugin_dir).resolve().parents[1]
    manifest = PluginManifest.model_validate(
        yaml.safe_load((plugin_dir / "plugin.yaml").read_text())
    )
    assert manifest.skill == "SKILL.md"
    assert (plugin_dir / "SKILL.md").is_file()


def test_create_mission():
    client = _client()
    resp = client.post("/api/missions", json={"name": "OSS Insights", "coordinator_session": "auto-x"})
    assert resp.status_code == 201
    body = resp.json()["mission"]
    assert body["name"] == "OSS Insights"
    assert body["coordinator_session"] == "auto-x"
    assert body["current_revision_id"] is None
    assert body["status"] == "active"


def test_create_mission_requires_name():
    client = _client()
    resp = client.post("/api/missions", json={})
    assert resp.status_code == 400


def test_list_missions_against_never_written_db_does_not_500(tmp_path, monkeypatch):
    """Regression: production's very first Mission Control request was a
    GET /api/missions against a DB file that had never been written to,
    and it 500'd — only the write paths ensured the schema existed."""
    fresh_path = tmp_path / "never_touched.db"
    monkeypatch.setattr(db, "DB_PATH", fresh_path)
    assert not fresh_path.exists()

    resp = _client().get("/api/missions")
    assert resp.status_code == 200
    assert resp.json() == {"missions": []}


def test_list_missions():
    client = _client()
    client.post("/api/missions", json={"name": "A"})
    client.post("/api/missions", json={"name": "B"})
    resp = client.get("/api/missions")
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()["missions"]}
    assert names == {"A", "B"}


def test_get_mission_not_found():
    client = _client()
    resp = client.get("/api/missions/nope")
    assert resp.status_code == 404


def test_get_mission_includes_current_revision_after_push():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})

    resp = client.get(f"/api/missions/{mission_id}")
    assert resp.status_code == 200
    body = resp.json()["mission"]
    assert body["current_revision"]["revision_seq"] == 1
    assert "html" not in body["current_revision"]


def test_get_mission_includes_open_question_count():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)

    assert client.get(f"/api/missions/{mission_id}").json()["mission"]["open_question_count"] == 0

    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        )
    assert client.get(f"/api/missions/{mission_id}").json()["mission"]["open_question_count"] == 1


def test_delete_mission():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.delete(f"/api/missions/{mission_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/missions/{mission_id}").status_code == 404


def test_delete_mission_not_found():
    client = _client()
    resp = client.delete("/api/missions/nope")
    assert resp.status_code == 404


# ── Mission status (lifecycle) ─────────────────────────────────────


def test_set_mission_status():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/status", json={"status": "paused"})
    assert resp.status_code == 200
    assert resp.json()["mission"]["status"] == "paused"
    assert client.get(f"/api/missions/{mission_id}").json()["mission"]["status"] == "paused"


def test_set_mission_status_rejects_invalid_value():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/status", json={"status": "nope"})
    assert resp.status_code == 400


def test_set_mission_status_missing_mission():
    client = _client()
    resp = client.post("/api/missions/nope/status", json={"status": "paused"})
    assert resp.status_code == 404


# ── Coordinator presence heartbeat ─────────────────────────────────


def test_push_site_revision_stores_and_publishes_in_one_call():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]

    resp = client.post(
        f"/api/missions/{mission_id}/site",
        json={"html": "<html>v1</html>", "note": "first push"},
    )
    assert resp.status_code == 201
    revision = resp.json()["revision"]
    assert revision["revision_seq"] == 1
    assert revision["note"] == "first push"
    # byte_size must be populated on push too, symmetric with the
    # revisions-list route — not left null until the next history read.
    assert revision["byte_size"] == len("<html>v1</html>")

    site = client.get(f"/api/missions/{mission_id}/site")
    assert site.status_code == 200
    assert site.json()["revision"]["html"] == "<html>v1</html>"


@patch("tools.graph.surface.Presence")
def test_push_site_revision_touches_coordinator_presence(mock_presence):
    mock_presence.return_value = MagicMock()
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]

    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})

    mock_presence.assert_called_once_with(
        surface_id=f"mission:{mission_id}",
        participant_kind="agent",
        participant_id="auto-coordinator",
        label="auto-coordinator",
        org="autonomy",
    )


@patch("tools.graph.surface.Presence")
def test_push_site_revision_skips_presence_when_no_coordinator(mock_presence):
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    mock_presence.assert_not_called()


@patch("tools.graph.surface.Presence")
def test_push_site_revision_succeeds_even_if_presence_heartbeat_raises(mock_presence):
    mock_presence.side_effect = RuntimeError("graph substrate unavailable")
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]

    resp = client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    assert resp.status_code == 201


def test_push_site_revision_requires_html():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/site", json={})
    assert resp.status_code == 400


def test_push_site_revision_missing_mission():
    client = _client()
    resp = client.post("/api/missions/nope/site", json={"html": "<html></html>"})
    assert resp.status_code == 404


def test_get_current_site_before_any_push_is_404():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.get(f"/api/missions/{mission_id}/site")
    assert resp.status_code == 404


def test_list_and_get_site_revisions():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>"})

    listed = client.get(f"/api/missions/{mission_id}/site/revisions")
    assert listed.status_code == 200
    revisions = listed.json()["revisions"]
    assert [r["revision_seq"] for r in revisions] == [2, 1]
    assert "html" not in revisions[0]

    rev1_id = revisions[1]["revision_id"]
    single = client.get(f"/api/missions/{mission_id}/site/revisions/{rev1_id}")
    assert single.status_code == 200
    assert single.json()["revision"]["html"] == "<html>v1</html>"


def test_activate_revision_rolls_back_current_pointer():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    rev1 = client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>good</html>"}).json()["revision"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>bad</html>"})

    resp = client.post(f"/api/missions/{mission_id}/site/revisions/{rev1['revision_id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["revision"]["revision_id"] == rev1["revision_id"]

    current = client.get(f"/api/missions/{mission_id}/site").json()["revision"]
    assert current["html"] == "<html>good</html>"
    # No new revision was fabricated by rolling back.
    revisions = client.get(f"/api/missions/{mission_id}/site/revisions").json()["revisions"]
    assert len(revisions) == 2


def test_activate_revision_not_found():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/site/revisions/nope/activate")
    assert resp.status_code == 404


# ── Public serving: one composed screen ──────────────────────────


def test_serve_mission_site_carries_the_authors_document_unmodified():
    """The author's HTML goes into the composed screen byte for byte.

    That substring assertion IS the contract: the composer concatenates,
    it never parses, rewrites or reserialises what the coordinator wrote.
    """
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html><body>hello</body></html>"})

    resp = client.get(f"/missions/{mission_id}")
    assert resp.status_code == 200
    assert "<html><body>hello</body></html>" in resp.text
    assert resp.text.startswith("<!doctype html>\n<meta name=\"viewport\"")
    # Served at a real URL, so NO srcdoc base: with one, every relative link
    # in the author's content resolves against about:srcdoc and is blocked.
    assert "<base" not in resp.text.split("<script")[0]
    assert resp.headers["content-type"].startswith("text/html")


def test_serve_mission_site_no_stale_serve_window():
    """A push must be visible on the very next request — response is
    marked uncacheable so no browser or proxy can serve a stale copy."""
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})

    first = client.get(f"/missions/{mission_id}")
    assert "<html>v1</html>" in first.text
    assert "no-store" in first.headers["cache-control"]

    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>"})
    second = client.get(f"/missions/{mission_id}")
    assert "<html>v2</html>" in second.text
    assert "<html>v1</html>" not in second.text


def test_serve_mission_site_missing_mission_is_404():
    client = _client()
    resp = client.get("/missions/nope")
    assert resp.status_code == 404


def test_serve_mission_site_before_any_push_is_404():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.get(f"/missions/{mission_id}")
    assert resp.status_code == 404


# ── Visitor identity shim (P2) ────────────────────────────────────


def test_create_visitor_token():
    client = _client()
    resp = client.post("/api/visitor-tokens", json={"display_name": "Alex"})
    assert resp.status_code == 201
    visitor = resp.json()["visitor"]
    assert visitor["token"]
    assert visitor["participant_id"].startswith("guest:")
    assert visitor["display_name"] == "Alex"


def test_create_visitor_token_requires_display_name():
    client = _client()
    resp = client.post("/api/visitor-tokens", json={})
    assert resp.status_code == 400


def test_get_visitor_by_participant_id():
    client = _client()
    visitor = client.post(
        "/api/visitor-tokens", json={"display_name": "Priya (data partner)"},
    ).json()["visitor"]
    resp = client.get(f"/api/visitor-tokens/{visitor['participant_id']}")
    assert resp.status_code == 200
    found = resp.json()["visitor"]
    assert found["participant_id"] == visitor["participant_id"]
    assert found["display_name"] == "Priya (data partner)"
    assert "token" not in found


def test_get_visitor_by_participant_id_missing_returns_404():
    client = _client()
    resp = client.get("/api/visitor-tokens/guest:nope")
    assert resp.status_code == 404


# ── Mission conversation (P2 Q&A) ─────────────────────────────────


def _mission_with_site(client) -> str:
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    return mission_id


def _visitor(client) -> dict:
    return client.post("/api/visitor-tokens", json={"display_name": "Alex"}).json()["visitor"]

def _close(client, scope: str, entry_id: str) -> None:
    """Reopen means un-closing a CLOSED entry. Answering no longer closes
    anything -- a reply is not presumed to be the end of the exchange -- so a
    flow that wants a reopenable entry has to close it first."""
    client.post(f"/api/{scope}/questions/{entry_id}/close")




@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_ask_question_via_as_query_param(mock_send):
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)

    resp = client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "What's the timeline?"},
    )
    assert resp.status_code == 201
    entry = resp.json()["question"]
    assert entry["question"] == "What's the timeline?"
    assert entry["asked_by_participant_id"] == visitor["participant_id"]
    assert entry["asked_by_label"] == "Alex"
    assert entry["answer"] is None


def test_ask_question_requires_visitor_identity():
    client = _client()
    mission_id = _mission_with_site(client)
    resp = client.post(f"/api/missions/{mission_id}/questions", json={"question": "hi"})
    assert resp.status_code == 401


def test_ask_question_rejects_unresolved_token():
    client = _client()
    mission_id = _mission_with_site(client)
    resp = client.post(
        f"/api/missions/{mission_id}/questions?as=not-a-real-token",
        json={"question": "hi"},
    )
    assert resp.status_code == 401


def test_ask_question_requires_question_text():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    resp = client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}", json={},
    )
    assert resp.status_code == 400


def test_ask_question_missing_mission():
    client = _client()
    visitor = _visitor(client)
    resp = client.post(
        f"/api/missions/nope/questions?as={visitor['token']}", json={"question": "hi"},
    )
    assert resp.status_code == 404


def test_ask_question_sets_cookie_from_as_param_for_next_request():
    client = _https_client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)

    first = client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "q1"},
    )
    assert first.status_code == 201
    assert mc_api.VISITOR_COOKIE in client.cookies

    # Second ask relies on the cookie alone -- no ?as= this time.
    second = client.post(f"/api/missions/{mission_id}/questions", json={"question": "q2"})
    assert second.status_code == 201
    assert second.json()["question"]["asked_by_label"] == "Alex"


def test_view_site_with_as_param_sets_cookie_then_ask_question_uses_it():
    """The documented flow: visit the site once with ?as=,
    then the site's own JS can POST a question relying on the cookie
    alone, no token in the request."""
    client = _https_client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)

    view = client.get(f"/missions/{mission_id}?as={visitor['token']}")
    assert view.status_code == 200
    assert mc_api.VISITOR_COOKIE in client.cookies

    resp = client.post(f"/api/missions/{mission_id}/questions", json={"question": "hi"})
    assert resp.status_code == 201
    assert resp.json()["question"]["asked_by_label"] == "Alex"


def test_cookie_authenticates_by_token_not_bare_participant_id():
    """The impersonation fix: forging a cookie with someone's displayed
    participant_id (visible in GET .../questions) must NOT let an
    attacker post as them. Only the real token resolves."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)

    # Legitimately ask once so the participant_id is visible in the list.
    client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "q1"},
    )
    listed = client.get(f"/api/missions/{mission_id}/questions").json()["questions"]
    leaked_participant_id = listed[0]["asked_by_participant_id"]
    assert leaked_participant_id == visitor["participant_id"]

    # Attacker sets a cookie to the bare participant_id they just read.
    client.cookies.set(mc_api.VISITOR_COOKIE, leaked_participant_id)
    forged = client.post(f"/api/missions/{mission_id}/questions", json={"question": "q2"})
    assert forged.status_code == 401


def test_list_conversation_missing_mission():
    client = _client()
    resp = client.get("/api/missions/nope/questions")
    assert resp.status_code == 404


@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_ask_question_relays_via_crosstalk_in_background(mock_send):
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)

    resp = client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "What's the timeline?"},
    )
    entry_id = resp.json()["question"]["entry_id"]

    mock_send.assert_called_once()
    call_target, call_envelope = mock_send.call_args[0]
    assert call_target == "auto-coordinator"
    assert "What's the timeline?" in call_envelope
    assert "Alex" in call_envelope

    listed = client.get(f"/api/missions/{mission_id}/questions").json()["questions"]
    assert listed[0]["relay_status"] == "sent"
    assert listed[0]["entry_id"] == entry_id


def test_ask_question_relay_status_failed_when_no_coordinator_session():
    client = _client()
    mission_id = _mission_with_site(client)  # no coordinator_session set
    visitor = _visitor(client)

    client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "hi"},
    )
    listed = client.get(f"/api/missions/{mission_id}/questions").json()["questions"]
    assert listed[0]["relay_status"] == "failed"


@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_ask_question_relay_status_failed_on_send_exception(mock_send):
    mock_send.side_effect = RuntimeError("boom")
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)

    client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "hi"},
    )
    listed = client.get(f"/api/missions/{mission_id}/questions").json()["questions"]
    assert listed[0]["relay_status"] == "failed"


def test_answer_question():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)

    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "What's the timeline?"},
        ).json()["question"]

    resp = client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
        json={"answer": "Q3 2026"},
    )
    assert resp.status_code == 200
    answered = resp.json()["question"]
    assert answered["answer"] == "Q3 2026"
    assert answered["answered_by_session"] == "auto-coordinator"
    assert answered["answered_at"] is not None


def test_answer_question_snapshots_current_coordinator_session_at_answer_time():
    """coordinator_session is mutable by design (P1 agreement) -- the
    answer must record who actually answered right now, not whoever
    created the mission."""
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-v1"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)

    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    # Mission changes hands between ask and answer.
    from tools.dashboard.dao import mission_control_db as _db
    conn = _db._get_conn(db.DB_PATH)
    conn.execute(
        "UPDATE missions SET coordinator_session = ? WHERE mission_id = ?",
        ("auto-v2", mission_id),
    )
    conn.commit()
    conn.close()

    answered = client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
        json={"answer": "answer"},
    ).json()["question"]
    assert answered["answered_by_session"] == "auto-v2"


def test_answer_question_requires_answer_text():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    resp = client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer", json={},
    )
    assert resp.status_code == 400


def test_answer_question_missing_mission():
    client = _client()
    resp = client.post(
        "/api/missions/nope/questions/nope/answer", json={"answer": "x"},
    )
    assert resp.status_code == 404


def test_answer_question_missing_entry():
    client = _client()
    mission_id = _mission_with_site(client)
    resp = client.post(
        f"/api/missions/{mission_id}/questions/nope/answer", json={"answer": "x"},
    )
    assert resp.status_code == 404


# ── "Since last visit" watermark (P3) ─────────────────────────────


def test_get_mission_since_last_visit_empty_before_any_seen_call():
    """Never-seen defaults to empty, not the whole history -- a mission's
    first-ever view showing its entire past as 'new' would be noisy and
    misleading (see get_mission's docstring comment)."""
    client = _client()
    mission_id = _mission_with_site(client)
    body = client.get(f"/api/missions/{mission_id}").json()["mission"]
    assert body["since_last_visit"] == {"last_seen_at": None, "revisions": [], "questions": []}


def test_mark_mission_seen_advances_watermark():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/seen")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["seen_at"], float)


def test_mark_mission_seen_missing_mission():
    client = _client()
    resp = client.post("/api/missions/nope/seen")
    assert resp.status_code == 404


def test_since_last_visit_reflects_activity_after_seen_and_clears_on_next_seen():
    client = _client()
    mission_id = _mission_with_site(client)  # revision #1, pre-watermark
    visitor = _visitor(client)

    client.post(f"/api/missions/{mission_id}/seen")

    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>", "note": "new rev"})
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "what changed?"},
        )

    body = client.get(f"/api/missions/{mission_id}").json()["mission"]
    delta = body["since_last_visit"]
    assert delta["last_seen_at"] is not None
    assert [r["note"] for r in delta["revisions"]] == ["new rev"]
    assert [q["question"] for q in delta["questions"]] == ["what changed?"]

    # Marking seen again consumes the delta -- a subsequent view is empty.
    client.post(f"/api/missions/{mission_id}/seen")
    cleared = client.get(f"/api/missions/{mission_id}").json()["mission"]["since_last_visit"]
    assert cleared["revisions"] == []
    assert cleared["questions"] == []


def test_incidental_get_does_not_advance_the_watermark():
    """The list page's refreshMissions() GETs every mission on every load
    just to hydrate summary fields -- that must never silently erase the
    delta before a deliberate POST .../seen (mirrors toggleExpand's
    fire-only-on-expand contract in page.js)."""
    client = _client()
    mission_id = _mission_with_site(client)
    client.post(f"/api/missions/{mission_id}/seen")
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>", "note": "new rev"})

    # Several incidental reads, no POST .../seen in between.
    for _ in range(3):
        client.get(f"/api/missions/{mission_id}")

    delta = client.get(f"/api/missions/{mission_id}").json()["mission"]["since_last_visit"]
    assert len(delta["revisions"]) == 1


# ── Pillars (P4) ───────────────────────────────────────────────────


def _pillar(client, mission_id, name="P", coordinator_session="auto-pillar", color="#34d399") -> dict:
    return client.post(
        f"/api/missions/{mission_id}/pillars",
        json={"name": name, "coordinator_session": coordinator_session, "color": color},
    ).json()["pillar"]


def _pillar_with_site(client, mission_id, **kwargs) -> dict:
    pillar = _pillar(client, mission_id, **kwargs)
    client.post(f"/api/pillars/{pillar['pillar_id']}/site", json={"html": "<html>v1</html>"})
    return pillar


def test_create_pillar():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(
        f"/api/missions/{mission_id}/pillars",
        json={"name": "Dataset & Schema", "coordinator_session": "auto-schema", "color": "#34d399"},
    )
    assert resp.status_code == 201
    pillar = resp.json()["pillar"]
    assert pillar["mission_id"] == mission_id
    assert pillar["name"] == "Dataset & Schema"
    assert pillar["coordinator_session"] == "auto-schema"
    assert pillar["color"] == "#34d399"
    assert pillar["status"] == "active"


def test_create_pillar_requires_name():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/pillars", json={})
    assert resp.status_code == 400


def test_create_pillar_missing_mission():
    client = _client()
    resp = client.post("/api/missions/nope/pillars", json={"name": "P"})
    assert resp.status_code == 404


def test_list_pillars_includes_open_question_count():
    """list_pillars (unlike list_missions) enriches server-side -- a
    pillar grid shows several pillars per mission, so N+1-fetching each
    one's detail from the client would multiply across missions x pillars."""
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)
    visitor = _visitor(client)
    client.post(
        f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
        json={"question": "hi"},
    )

    listed = client.get(f"/api/missions/{mission_id}/pillars").json()["pillars"]
    assert listed[0]["open_question_count"] == 1


def test_list_pillars_missing_mission():
    client = _client()
    resp = client.get("/api/missions/nope/pillars")
    assert resp.status_code == 404


def test_get_pillar_includes_current_revision_and_since_last_visit():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)

    body = client.get(f"/api/pillars/{pillar['pillar_id']}").json()["pillar"]
    assert body["current_revision"]["revision_seq"] == 1
    assert body["since_last_visit"] == {"last_seen_at": None, "revisions": [], "questions": []}


def test_get_pillar_not_found():
    client = _client()
    resp = client.get("/api/pillars/nope")
    assert resp.status_code == 404


def test_set_pillar_status():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    resp = client.post(f"/api/pillars/{pillar['pillar_id']}/status", json={"status": "paused"})
    assert resp.status_code == 200
    assert resp.json()["pillar"]["status"] == "paused"


def test_set_pillar_status_rejects_invalid_value():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    resp = client.post(f"/api/pillars/{pillar['pillar_id']}/status", json={"status": "nope"})
    assert resp.status_code == 400


def test_delete_pillar():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    resp = client.delete(f"/api/pillars/{pillar['pillar_id']}")
    assert resp.status_code == 200
    assert client.get(f"/api/pillars/{pillar['pillar_id']}").status_code == 404


def test_delete_pillar_not_found():
    client = _client()
    resp = client.delete("/api/pillars/nope")
    assert resp.status_code == 404


def test_push_pillar_site_revision():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)

    resp = client.post(
        f"/api/pillars/{pillar['pillar_id']}/site",
        json={"html": "<html>v1</html>", "note": "first push"},
    )
    assert resp.status_code == 201
    revision = resp.json()["revision"]
    assert revision["revision_seq"] == 1
    assert revision["pillar_id"] == pillar["pillar_id"]
    assert "mission_id" not in revision

    site = client.get(f"/api/pillars/{pillar['pillar_id']}/site")
    assert site.json()["revision"]["html"] == "<html>v1</html>"


def test_push_pillar_site_revision_missing_pillar():
    client = _client()
    resp = client.post("/api/pillars/nope/site", json={"html": "<html></html>"})
    assert resp.status_code == 404


def test_pillar_site_revisions_list_and_activate():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    rev1 = client.post(f"/api/pillars/{pillar['pillar_id']}/site", json={"html": "<html>good</html>"}).json()["revision"]
    client.post(f"/api/pillars/{pillar['pillar_id']}/site", json={"html": "<html>bad</html>"})

    listed = client.get(f"/api/pillars/{pillar['pillar_id']}/site/revisions").json()["revisions"]
    assert [r["revision_seq"] for r in listed] == [2, 1]

    activated = client.post(f"/api/pillars/{pillar['pillar_id']}/site/revisions/{rev1['revision_id']}/activate")
    assert activated.status_code == 200
    current = client.get(f"/api/pillars/{pillar['pillar_id']}/site").json()["revision"]
    assert current["html"] == "<html>good</html>"


def test_serve_pillar_site_carries_the_authors_document():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    client.post(f"/api/pillars/{pillar['pillar_id']}/site", json={"html": "<html><body>pillar content</body></html>"})

    resp = client.get(f"/missions/{mission_id}/pillars/{pillar['pillar_id']}")
    assert resp.status_code == 200
    assert "<html><body>pillar content</body></html>" in resp.text
    # The screen knows which pillar it is, so the chrome opens on it.
    assert f'"screen":"{pillar["pillar_id"]}"' in resp.text


def test_serve_pillar_site_wrong_mission_is_404():
    """A pillar served under a mission_id that isn't its actual parent
    must 404, not silently serve it -- the URL's mission_id is part of
    the address, not decoration."""
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    other_mission_id = client.post("/api/missions", json={"name": "B"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)

    resp = client.get(f"/missions/{other_mission_id}/pillars/{pillar['pillar_id']}")
    assert resp.status_code == 404


def test_serve_pillar_site_before_any_push_is_404():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    resp = client.get(f"/missions/{mission_id}/pillars/{pillar['pillar_id']}")
    assert resp.status_code == 404


def test_delete_mission_cascades_to_pillars_via_api():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)
    client.delete(f"/api/missions/{mission_id}")
    assert client.get(f"/api/pillars/{pillar['pillar_id']}").status_code == 404


# ── Anchored conversation: mission-level vs pillar-level, delivery routing ──


@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_pillar_question_relays_to_both_pillar_and_mission_coordinator(mock_send):
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-top"},
    ).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id, coordinator_session="auto-pillar")
    visitor = _visitor(client)

    client.post(
        f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
        json={"question": "why 3 triggers?", "anchor": "table:x"},
    )

    assert mock_send.call_count == 2
    targets = {call.args[0] for call in mock_send.call_args_list}
    assert targets == {"auto-pillar", "auto-top"}

    envelopes = {call.args[0]: call.args[1] for call in mock_send.call_args_list}
    # Primary (pillar) is told a reply is expected; the mission is told
    # it's copied for tracking only.
    # The primary is the one handed the routes to reply with.
    assert "/answer" in envelopes["auto-pillar"]
    assert "Copied for tracking" in envelopes["auto-top"]
    assert "no reply expected from you" in envelopes["auto-top"]
    # Anchor surfaces in the relay body.
    assert "table:x" in envelopes["auto-pillar"]


@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_pillar_question_skips_cc_when_pillar_and_mission_share_a_session(mock_send):
    """A coordinator running both the mission and one of its pillars must
    not get the same question relayed to itself twice."""
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-same"},
    ).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id, coordinator_session="auto-same")
    visitor = _visitor(client)

    client.post(
        f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
        json={"question": "hi"},
    )
    assert mock_send.call_count == 1


@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_mission_level_question_relay_unchanged_by_pillars_existing(mock_send):
    """A plain mission-level question (no pillar_id) still relays to the
    mission's own coordinator only, exactly as before pillars existed --
    regression coverage for the _relay_question rewrite."""
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-top"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)

    client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "hi"},
    )
    assert mock_send.call_count == 1
    assert mock_send.call_args.args[0] == "auto-top"


def test_ask_pillar_question_requires_visitor_identity():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)
    resp = client.post(f"/api/pillars/{pillar['pillar_id']}/questions", json={"question": "hi"})
    assert resp.status_code == 401


def test_ask_pillar_question_missing_pillar():
    client = _client()
    visitor = _visitor(client)
    resp = client.post(
        f"/api/pillars/nope/questions?as={visitor['token']}", json={"question": "hi"},
    )
    assert resp.status_code == 404


def test_list_pillar_conversation():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
            json={"question": "pillar q"},
        )

    listed = client.get(f"/api/pillars/{pillar['pillar_id']}/questions").json()["questions"]
    assert len(listed) == 1
    assert listed[0]["question"] == "pillar q"
    assert listed[0]["pillar_id"] == pillar["pillar_id"]


def test_answer_pillar_question():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id, coordinator_session="auto-pillar")
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    resp = client.post(
        f"/api/pillars/{pillar['pillar_id']}/questions/{asked['entry_id']}/answer",
        json={"answer": "because reasons"},
    )
    assert resp.status_code == 200
    answered = resp.json()["question"]
    assert answered["answer"] == "because reasons"
    assert answered["answered_by_session"] == "auto-pillar"


def test_answer_pillar_question_missing_pillar():
    client = _client()
    resp = client.post("/api/pillars/nope/questions/nope/answer", json={"answer": "x"})
    assert resp.status_code == 404


# ── Progress updates ─────────────────────────────────────────────


def test_add_question_update_mission_level():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    resp = client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/update",
        json={"text": "still working"},
    )
    assert resp.status_code == 201
    assert resp.json()["update"]["text"] == "still working"

    listed = client.get(f"/api/missions/{mission_id}/questions").json()["questions"]
    assert listed[0]["updates"][0]["text"] == "still working"
    assert listed[0]["answer"] is None


def test_add_question_update_pillar_level():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    resp = client.post(
        f"/api/pillars/{pillar['pillar_id']}/questions/{asked['entry_id']}/update",
        json={"text": "capturing new screenshot"},
    )
    assert resp.status_code == 201


def test_add_question_update_requires_text():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    resp = client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/update", json={},
    )
    assert resp.status_code == 400


def test_add_question_update_missing_entry():
    client = _client()
    mission_id = _mission_with_site(client)
    resp = client.post(
        f"/api/missions/{mission_id}/questions/nope/update", json={"text": "x"},
    )
    assert resp.status_code == 404


def test_add_pillar_question_update_missing_pillar():
    client = _client()
    resp = client.post("/api/pillars/nope/questions/nope/update", json={"text": "x"})
    assert resp.status_code == 404


def test_a_conversation_keeps_every_round_after_it_is_answered():
    """A conversation is not a question and an answer with the middle thrown
    away. The first reply is very often a question of its own, and what was
    said in between is how anyone reads the ending. The clean pair is what a
    pillar WRITES when it resolves -- the output, not the storage."""
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
    client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/update",
        json={"text": "still working"},
    )
    still_open = client.get(f"/api/missions/{mission_id}/questions").json()["questions"][0]
    assert still_open["updates"]  # visible while open

    client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
        json={"answer": "final"},
    )
    answered = client.get(f"/api/missions/{mission_id}/questions").json()["questions"][0]
    trail = [u["text"] for u in answered["updates"]]
    # Delivery, then working, in the order it happened -- the sequence is the
    # thing a reader cannot reconstruct afterwards.
    assert any(t.startswith("Delivered to") for t in trail), trail
    assert "still working" in trail, trail
    assert trail.index("still working") > 0, "the receipt did not precede the work"
    assert answered["answer"] == "final"


# ── Reopening ─────────────────────────────────────────────────────


def test_reopen_question_mission_level():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "first answer"},
        )
        _close(client, f"missions/{mission_id}", asked["entry_id"])
        resp = client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
            json={"followup": "not quite -- what about X?"},
        )

    assert resp.status_code == 200
    reopened = resp.json()["question"]
    assert reopened["answer"] is None
    assert reopened["entry_id"] == asked["entry_id"]
    assert reopened["question"] == "hi"  # the original ask is untouched


@patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock)
def test_reopen_question_relays_again_with_prior_context(mock_send):
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    asked = client.post(
        f"/api/missions/{mission_id}/questions?as={visitor['token']}",
        json={"question": "hi"},
    ).json()["question"]
    client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
        json={"answer": "first answer"},
    )
    mock_send.reset_mock()

    _close(client, f"missions/{mission_id}", asked["entry_id"])
    client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
        json={"followup": "not quite -- what about X?"},
    )

    mock_send.assert_called_once()
    envelope = mock_send.call_args.args[1]
    assert "reopened" in envelope
    assert "first answer" in envelope
    assert "not quite -- what about X?" in envelope
    assert "ONE new answer" in envelope


def test_reopen_question_requires_followup_text():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "first answer"},
        )
        _close(client, f"missions/{mission_id}", asked["entry_id"])
        resp = client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
            json={},
        )
    assert resp.status_code == 400


def test_reopen_question_requires_visitor_identity():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "first answer"},
        )
        _close(client, f"missions/{mission_id}", asked["entry_id"])
        resp = client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen",
            json={"followup": "not quite"},
        )
    assert resp.status_code == 401


def test_reopening_a_live_conversation_is_refused():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        # Nothing to reopen: it never closed. More to say goes in the
        # conversation, which is what an open conversation is for.
        resp = client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
            json={"followup": "still waiting"},
        )
    assert resp.status_code == 404


def test_reopen_question_missing_mission():
    client = _client()
    visitor = _visitor(client)
    resp = client.post(
        f"/api/missions/nope/questions/nope/reopen?as={visitor['token']}",
        json={"followup": "x"},
    )
    assert resp.status_code == 404


def test_reopen_pillar_question():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id, coordinator_session="auto-pillar")
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions/{asked['entry_id']}/answer",
            json={"answer": "first answer"},
        )
        _close(client, f"pillars/{pillar['pillar_id']}", asked["entry_id"])
        resp = client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
            json={"followup": "still unclear"},
        )
    assert resp.status_code == 200
    assert resp.json()["question"]["answer"] is None


def test_reopen_pillar_question_missing_pillar():
    client = _client()
    visitor = _visitor(client)
    resp = client.post(
        f"/api/pillars/nope/questions/nope/reopen?as={visitor['token']}", json={"followup": "x"},
    )
    assert resp.status_code == 404


def test_reopen_then_reanswer_carries_one_answer_and_the_whole_exchange():
    """One question and one current answer, with every round that produced it
    still there. The answer is what stands now; the rounds are how a reader
    works out why it stands."""
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "first answer"},
        )
        _close(client, f"missions/{mission_id}", asked["entry_id"])
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
            json={"followup": "not quite -- what about X?"},
        )
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "integrated final answer"},
        )

    final = client.get(f"/api/missions/{mission_id}/questions").json()["questions"][0]
    assert final["answer"] == "integrated final answer"
    trail = [u["text"] for u in final["updates"]]
    assert any("Previous answer" in t for t in trail), (
        f"the exchange that produced this answer is gone: {trail}"
    )


# ── Live-update events (auto-ljkpn) ────────────────────────────────
#
# Pattern mirrors tools/dashboard/tests/test_approval_requests.py's
# test_sse_events_on_create_and_decision: subscribe, act via the ordinary
# test client, drain the queue, assert the expected (topic, payload-shape)
# is present. event_bus.broadcast() is a thin sync wrapper (broadcast_sync
# under the hood), so the event is on the queue by the time client.post()
# returns -- no async test harness needed.


def test_ask_question_publishes_conversation_event():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    queue = mc_api.event_bus.subscribe()
    try:
        with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
            asked = client.post(
                f"/api/missions/{mission_id}/questions?as={visitor['token']}",
                json={"question": "hi"},
            ).json()["question"]
        events = []
        while not queue.empty():
            topic, data, seq = queue.get_nowait()
            if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                events.append(data)
        assert len(events) == 1
        assert events[0]["event"] == "asked"
        assert events[0]["mission_id"] == mission_id
        assert events[0]["pillar_id"] is None
        assert events[0]["entry_id"] == asked["entry_id"]
        assert events[0]["question"]["question"] == "hi"
    finally:
        mc_api.event_bus.unsubscribe(queue)


def test_answer_question_publishes_conversation_event():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    queue = mc_api.event_bus.subscribe()
    try:
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "Q3 2026"},
        )
        events = []
        while not queue.empty():
            topic, data, seq = queue.get_nowait()
            if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                events.append(data)
        assert len(events) == 1
        assert events[0]["event"] == "answered"
        assert events[0]["question"]["answer"] == "Q3 2026"
    finally:
        mc_api.event_bus.unsubscribe(queue)


def test_add_question_update_publishes_conversation_event():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]

    queue = mc_api.event_bus.subscribe()
    try:
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/update",
            json={"text": "still working"},
        )
        events = []
        while not queue.empty():
            topic, data, seq = queue.get_nowait()
            if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                events.append(data)
        assert len(events) == 1
        assert events[0]["event"] == "update"
        assert events[0]["update"]["text"] == "still working"
    finally:
        mc_api.event_bus.unsubscribe(queue)


def test_reopen_question_publishes_conversation_event():
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
        client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
            json={"answer": "first answer"},
        )

        queue = mc_api.event_bus.subscribe()
        try:
            _close(client, f"missions/{mission_id}", asked["entry_id"])
            client.post(
                f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
                json={"followup": "not quite"},
            )
            events = []
            while not queue.empty():
                topic, data, seq = queue.get_nowait()
                if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                    events.append(data)
            # Delivery publishes its own receipt, so identify the event
            # under test rather than counting everything on the topic.
            reopened = [e for e in events if e["event"] == "reopened"]
            assert len(reopened) == 1
            assert reopened[0]["question"]["answer"] is None
        finally:
            mc_api.event_bus.unsubscribe(queue)


def test_pillar_question_event_carries_pillar_id():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar_with_site(client, mission_id)
    visitor = _visitor(client)
    queue = mc_api.event_bus.subscribe()
    try:
        with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
            client.post(
                f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
                json={"question": "pillar q"},
            )
        events = []
        while not queue.empty():
            topic, data, seq = queue.get_nowait()
            if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                events.append(data)
        asked_events = [e for e in events if e["event"] == "asked"]
        assert len(asked_events) == 1
        assert asked_events[0]["pillar_id"] == pillar["pillar_id"]
        assert asked_events[0]["mission_id"] == mission_id
    finally:
        mc_api.event_bus.unsubscribe(queue)


# ── handle_relay_write (auto-u0kxw) ──────────────────────────────
#
# The relay (tools/dashboard/link_serving.py) never calls into this
# module's HTTP handlers -- it calls handle_relay_write directly with an
# identity already resolved from the grant and a raw body it never
# interprets. These tests call handle_relay_write the same way, with no
# HTTP request/response involved.


def _run(coro):
    return asyncio.run(coro)


def test_handle_relay_write_asks_a_question():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)

    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        result = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id, {"kind": "question", "question": "hi"},
        ))

    assert result["question"]["question"] == "hi"
    assert result["question"]["asked_by_participant_id"] == visitor["participant_id"]
    assert result["question"]["asked_by_label"] == "Alex"
    entries = db.list_conversation(mission_id)
    assert len(entries) == 1 and entries[0]["question"] == "hi"


def test_handle_relay_write_reopens_a_question():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "hi"},
        ).json()["question"]
    client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/answer",
        json={"answer": "done"},
    )
    _close(client, f"missions/{mission_id}", asked["entry_id"])

    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        result = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "reopen", "entry_id": asked["entry_id"], "followup": "not quite"},
        ))

    assert result["question"]["entry_id"] == asked["entry_id"]
    assert result["question"]["answer"] is None  # reopened -- cleared back to open


def test_handle_relay_write_unknown_participant_rejected():
    client = _client()
    mission_id = _mission_with_site(client)
    result = _run(mc_api.handle_relay_write(
        "guest:nonexistent", mission_id, {"kind": "question", "question": "hi"},
    ))
    assert result is None
    assert db.list_conversation(mission_id) == []


def test_handle_relay_write_unknown_kind_rejected():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    result = _run(mc_api.handle_relay_write(
        visitor["participant_id"], mission_id, {"kind": "delete_everything"},
    ))
    assert result is None


def test_handle_relay_write_pillar_from_a_different_mission_rejected():
    """The bug found in review: a guest's channel is bound to ONE mission
    -- a pillar_id belonging to a DIFFERENT mission must be refused, not
    passed through to a coordinator who never granted this guest access."""
    client = _client()
    mission_a = _mission_with_site(client)
    mission_b = client.post("/api/missions", json={"name": "B"}).json()["mission"]["mission_id"]
    pillar_b = _pillar_with_site(client, mission_b)
    visitor = _visitor(client)

    result = _run(mc_api.handle_relay_write(
        visitor["participant_id"], mission_a,
        {"kind": "question", "question": "cross-mission smuggle", "pillar_id": pillar_b["pillar_id"]},
    ))
    assert result is None
    assert db.list_conversation(mission_a) == []
    assert db.list_pillar_conversation(pillar_b["pillar_id"]) == []


def test_handle_relay_write_pillar_scoped_question_same_mission_succeeds():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = _pillar_with_site(client, mission_id)
    visitor = _visitor(client)

    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        result = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "pillar q", "pillar_id": pillar["pillar_id"]},
        ))

    assert result["question"]["pillar_id"] == pillar["pillar_id"]
    assert len(db.list_pillar_conversation(pillar["pillar_id"])) == 1


def test_handle_relay_write_publishes_conversation_event_and_schedules_relay():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    queue = mc_api.event_bus.subscribe()
    try:
        with patch.object(mc_api, "_relay_question", new_callable=AsyncMock) as mock_relay:
            result = _run(mc_api.handle_relay_write(
                visitor["participant_id"], mission_id, {"kind": "question", "question": "hi"},
            ))
        events = []
        while not queue.empty():
            topic, data, seq = queue.get_nowait()
            if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                events.append(data)
        assert len(events) == 1
        assert events[0]["event"] == "asked"
        assert events[0]["entry_id"] == result["question"]["entry_id"]
        mock_relay.assert_called_once_with(
            mission_id=mission_id, entry_id=result["question"]["entry_id"],
        )
    finally:
        mc_api.event_bus.unsubscribe(queue)


# ── handle_relay_read (auto-t2lz1) ───────────────────────────────
#
# What the mission site's own fetch('/api/...') calls become over the
# relay, where the dashboard's origin does not exist. Called the same way
# the relay calls it: identity already resolved, body never interpreted
# by the transport.


def test_handle_relay_read_lists_pillars():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = _pillar_with_site(client, mission_id, name="Dataset")
    result = _run(mc_api.handle_relay_read("guest:1", mission_id, {"kind": "pillars"}))
    assert [p["pillar_id"] for p in result["pillars"]] == [pillar["pillar_id"]]
    assert result["pillars"][0]["name"] == "Dataset"


def test_handle_relay_read_lists_mission_questions():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "over the relay?"},
        )
    result = _run(mc_api.handle_relay_read("guest:1", mission_id, {"kind": "questions"}))
    assert [q["question"] for q in result["questions"]] == ["over the relay?"]


def test_handle_relay_read_lists_pillar_questions():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = _pillar_with_site(client, mission_id)
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        client.post(
            f"/api/pillars/{pillar['pillar_id']}/questions?as={visitor['token']}",
            json={"question": "pillar q"},
        )
    result = _run(mc_api.handle_relay_read(
        "guest:1", mission_id, {"kind": "questions", "pillar_id": pillar["pillar_id"]},
    ))
    assert [q["question"] for q in result["questions"]] == ["pillar q"]


def test_handle_relay_read_serves_a_composed_pillar_screen():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = _pillar(client, mission_id)
    client.post(f"/api/pillars/{pillar['pillar_id']}/site",
                json={"html": "<html>pillar page</html>"})
    result = _run(mc_api.handle_relay_read(
        "guest:1", mission_id, {"kind": "pillar_site", "pillar_id": pillar["pillar_id"]},
    ))
    # A composed screen, not raw HTML: the viewer document.writes what it
    # receives, so a bare fragment would land without its runtime.
    assert "<html>pillar page</html>" in result["document"]
    assert result["document"].startswith("<!doctype html>")
    assert result["pillar_id"] == pillar["pillar_id"]


def test_handle_relay_read_pillar_site_before_any_push_is_refused():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = _pillar(client, mission_id)  # no site pushed
    assert _run(mc_api.handle_relay_read(
        "guest:1", mission_id, {"kind": "pillar_site", "pillar_id": pillar["pillar_id"]},
    )) is None


def test_handle_relay_read_refuses_a_pillar_of_another_mission():
    """Same cross-mission check the write path makes: a guest's channel is
    bound to ONE mission and must never reach another's content through a
    supplied pillar_id."""
    client = _client()
    mission_a = _mission_with_site(client)
    mission_b = client.post("/api/missions", json={"name": "B"}).json()["mission"]["mission_id"]
    pillar_b = _pillar_with_site(client, mission_b)
    client.post(f"/api/pillars/{pillar_b['pillar_id']}/site", json={"html": "<html>B</html>"})

    for body in (
        {"kind": "questions", "pillar_id": pillar_b["pillar_id"]},
        {"kind": "pillar_site", "pillar_id": pillar_b["pillar_id"]},
    ):
        assert _run(mc_api.handle_relay_read("guest:1", mission_a, body)) is None


def test_handle_relay_read_unknown_mission_is_refused():
    assert _run(mc_api.handle_relay_read("guest:1", "nope", {"kind": "pillars"})) is None


@pytest.mark.parametrize("body", [
    {}, {"kind": "delete_everything"}, {"kind": None},
    {"kind": "pillar_site"},  # no pillar_id
    {"kind": "pillar_site", "pillar_id": 123},
])
def test_handle_relay_read_malformed_bodies_are_refused(body):
    client = _client()
    mission_id = _mission_with_site(client)
    assert _run(mc_api.handle_relay_read("guest:1", mission_id, body)) is None


def test_handle_relay_read_presence_returns_a_list():
    client = _client()
    mission_id = _mission_with_site(client)
    result = _run(mc_api.handle_relay_read("guest:1", mission_id, {"kind": "presence"}))
    assert isinstance(result["presence"], list)


# ── Cross-pillar decision log ────────────────────────────────────


def test_decision_log_endpoint():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id, name="Dataset & Schema")
    client.post(f"/api/pillars/{pillar['pillar_id']}/site", json={"html": "<html>v1</html>", "note": "pillar rev"})
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>", "note": "mission rev"})

    log = client.get(f"/api/missions/{mission_id}/decision-log").json()["decision_log"]
    by_text = {e["text"]: e for e in log}
    assert by_text["mission rev"]["pillar_id"] is None
    assert by_text["mission rev"]["pillar_name"] is None
    assert by_text["pillar rev"]["pillar_id"] == pillar["pillar_id"]
    assert by_text["pillar rev"]["pillar_name"] == "Dataset & Schema"


def test_decision_log_missing_mission():
    client = _client()
    resp = client.get("/api/missions/nope/decision-log")
    assert resp.status_code == 404


# ── guest avatars go to the attachment store, not into a column ──


def test_creating_a_visitor_with_a_photo_stores_it_as_an_attachment(monkeypatch):
    """The bytes belong in the graph's content-addressed store: hash
    dedup, one same-origin serving route, alt-text, and a relay fetch
    protocol the bootloader already speaks. visitor_tokens keeps the id."""
    seen = {}

    def fake_attach(path, **kw):
        from pathlib import Path
        seen["bytes"] = Path(path).read_bytes()
        seen["alt_text"] = kw.get("alt_text")
        return {"id": "att-abc"}

    monkeypatch.setattr("tools.graph.ops.attach_file", fake_attach)
    client = _client()
    png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    resp = client.post("/api/visitor-tokens",
                       json={"display_name": "Leon Zachery", "avatar": png})
    assert resp.status_code == 201
    visitor = resp.json()["visitor"]
    assert visitor["avatar_attachment_id"] == "att-abc"
    assert visitor["avatar_url"] == "/api/attachment/att-abc"
    # Real decoded bytes reached the store, not the base64 text.
    assert seen["bytes"].startswith(b"\x89PNG")
    # Alt text is not decoration -- this face renders in the operator's
    # approval dialog and in every viewer.
    assert seen["alt_text"] == "Profile photo of Leon Zachery"


def test_a_visitor_without_a_photo_has_no_avatar():
    client = _client()
    visitor = client.post("/api/visitor-tokens",
                          json={"display_name": "No Photo"}).json()["visitor"]
    assert visitor["avatar_attachment_id"] is None
    assert visitor["avatar_url"] is None


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "/etc/passwd",
    "http://insecure.example.com/x.png",
    12345,
])
def test_a_non_image_avatar_is_refused(bad):
    """This value becomes an <img src> in the operator's own dialog."""
    client = _client()
    resp = client.post("/api/visitor-tokens",
                       json={"display_name": "Sneaky", "avatar": bad})
    assert resp.status_code == 400


def test_setting_an_avatar_after_the_fact(monkeypatch):
    monkeypatch.setattr("tools.graph.ops.attach_file", lambda p, **kw: {"id": "att-later"})
    client = _client()
    visitor = client.post("/api/visitor-tokens",
                          json={"display_name": "Later Photo"}).json()["visitor"]
    png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    resp = client.post(
        f"/api/visitor-tokens/{visitor['participant_id']}/avatar",
        json={"avatar": png},
    )
    assert resp.status_code == 200
    assert resp.json()["visitor"]["avatar_url"] == "/api/attachment/att-later"


def test_setting_an_avatar_on_an_unknown_participant_is_404():
    client = _client()
    resp = client.post("/api/visitor-tokens/guest:nope/avatar",
                       json={"avatar": None})
    assert resp.status_code == 404


def test_set_pillar_last_done_route():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    text = ("Got the interface running and logged into it inside a test container. "
            "Confirmed we can demo it without the real backend.")

    resp = client.post(f"/api/pillars/{pillar['pillar_id']}/last-done",
                       json={"last_done": text})
    assert resp.status_code == 200
    assert resp.json()["pillar"]["last_done"] == text
    assert isinstance(resp.json()["pillar"]["last_done_at"], float)


def test_last_done_rejects_a_pasted_status_report():
    """Two sentences, not a report. The cap is generous enough that a real
    one never reaches it and tight enough that a dumped log does."""
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)

    resp = client.post(f"/api/pillars/{pillar['pillar_id']}/last-done",
                       json={"last_done": "x" * (mc_api.LAST_DONE_MAX + 1)})
    assert resp.status_code == 400
    assert "two sentences" in resp.json()["error"]


def test_last_done_requires_a_string_and_a_real_pillar():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    assert client.post(f"/api/pillars/{pillar['pillar_id']}/last-done",
                       json={"last_done": 5}).status_code == 400
    assert client.post("/api/pillars/nope/last-done",
                       json={"last_done": "x"}).status_code == 404


def test_a_reader_announcing_itself_appears_in_presence():
    """Presence recorded only sessions that PUSH, so the people a mission is
    written for never appeared on it. A reader now says it is here and gets
    back who else is."""
    client = _client()
    mission_id = _mission_with_site(client)
    with patch("tools.graph.surface.Presence", MagicMock()), \
         patch.object(mc_api, "_surface_presence",
                      lambda s: [{"participant_id": "guest:with-the-link"}]):
        out = _run(mc_api.handle_relay_read("", mission_id, {"kind": "here"}))
    assert out["presence"][0]["participant_id"] == "guest:with-the-link"


def test_an_unbound_reader_is_named_honestly():
    """Everyone holding one anonymous link is the same participant as far as
    anything here can tell, and it says so rather than inventing visitors."""
    client = _client()
    mission_id = _mission_with_site(client)
    seen = {}
    with patch.object(mc_api, "_heartbeat_presence",
                      lambda s, who, label, kind="agent": seen.update(
                          surface=s, who=who, label=label, kind=kind)), \
         patch.object(mc_api, "_surface_presence", lambda s: []):
        _run(mc_api.handle_relay_read("", mission_id, {"kind": "here"}))
    assert seen["who"] == "guest:with-the-link"
    assert seen["label"] == "Someone with the link"
    assert seen["kind"] == "guest"
    assert seen["surface"] == f"mission:{mission_id}"


def test_a_reader_cannot_announce_onto_another_missions_pillar():
    client = _client()
    mission_a = _mission_with_site(client)
    mission_b = client.post("/api/missions", json={"name": "B"}).json()["mission"]["mission_id"]
    pillar_b = _pillar_with_site(client, mission_b)
    assert _run(mc_api.handle_relay_read(
        "", mission_a, {"kind": "here", "pillar_id": pillar_b["pillar_id"]})) is None


def test_a_guest_heartbeat_uses_a_kind_presence_actually_accepts():
    """The REAL Presence, not a mock.

    Every other presence test patches surface.Presence out, so the one thing
    that validates participant_kind was never asked. The `here` op passed
    kind="person", which is not one of ('operator', 'agent', 'guest'):
    Presence raised, _heartbeat_presence swallowed it and logged, and guest
    presence was never recorded at all while the code looked healthy.
    """
    from tools.graph.surface import VALID_PARTICIPANT_KINDS

    seen = {}

    class _Recorder:
        def __init__(self, **kw):
            # Exactly the check the real Presence makes.
            assert kw["participant_kind"] in VALID_PARTICIPANT_KINDS, (
                f"invalid participant_kind {kw['participant_kind']!r}"
            )
            seen.update(kw)

        def __enter__(self): return self
        def __exit__(self, *a): return False

    client = _client()
    mission_id = _mission_with_site(client)
    with patch("tools.graph.surface.Presence", _Recorder), \
         patch.object(mc_api, "_surface_presence", lambda s: []):
        _run(mc_api.handle_relay_read("", mission_id, {"kind": "here"}))

    assert seen["participant_kind"] == "guest"


# ── the operator asking from their own SPA (auto-q3jb6) ──────────


def _mission() -> str:
    return db.create_mission("Mission", "auto-test")["mission_id"]


def test_the_operator_can_ask_from_the_dashboard_with_no_visitor_token():
    """A visitor token is a share-link artifact. The dashboard never has one.

    Every ask from the operator's own SPA was refused 401 "visitor identity
    required" while the surface still rendered a composer — an interface that
    invites an action the server will always reject.
    """
    mission_id = _mission()
    with patch.object(
        mc_api, "_operator_identity",
        return_value={"participant_id": "operator", "participant_label": "Jeremy"},
    ):
        r = _client().post(
            f"/api/missions/{mission_id}/questions", json={"question": "Does this work?"}
        )
    assert r.status_code == 201, r.text
    entry = r.json()["question"]
    assert entry["asked_by_label"] == "Jeremy"


def test_an_unauthenticated_caller_is_still_refused():
    """The control. If the fallback resolved an identity for everyone, the
    test above would pass while the ask route became open to the world.
    """
    mission_id = _mission()
    with patch.object(mc_api, "_operator_identity", return_value=None):
        r = _client().post(
            f"/api/missions/{mission_id}/questions", json={"question": "Anyone?"}
        )
    assert r.status_code == 401


def test_operator_identity_comes_from_the_session_not_a_request_field():
    """Identity is presented as a credential, never named in the payload.

    A caller claiming to be the operator by body, header or query gets
    nothing; only a session the store still considers active resolves.
    """
    from starlette.requests import Request

    def _req(**cookies):
        scope = {
            "type": "http", "method": "POST", "path": "/", "query_string": b"as=operator",
            "headers": [(b"x-participant-id", b"operator")]
            + ([(b"cookie", b"autonomy_dashboard_session=forged")] if cookies else []),
        }
        return Request(scope)

    with patch("tools.dashboard.unlock_routes.session_from_request", return_value=None):
        assert mc_api._operator_identity(_req(session=True)) is None

    with patch("tools.dashboard.unlock_routes.session_from_request",
               return_value={"sid": "s1", "method": "passkey"}):
        who = mc_api._operator_identity(_req(session=True))
    assert who["participant_id"] == "operator"


def test_the_operator_id_is_never_the_personal_root_key():
    """participant_id is display-safe and appears in GET .../questions to
    every reader. The personal root public key is the cross-org master;
    publishing it beside a question would correlate the operator across every
    org they belong to — the exact linkage per-org personas exist to prevent.
    """
    assert mc_api.OPERATOR_PARTICIPANT_ID == "operator"
    assert len(mc_api.OPERATOR_PARTICIPANT_ID) != 64


# ── activity events: work landing, not conversation (auto-xjajk) ──


def _pillar_for_activity():
    mid = db.create_mission("M", "auto-test")["mission_id"]
    pid = db.create_pillar(mid, "Infra", coordinator_session="s",
                           color="#34d399")["pillar_id"]
    return mid, pid


def test_a_status_line_is_published_as_an_event():
    """A pillar finishing something is the main thing a reader wants to see.
    Only the conversation was published, so a live screen showed people
    talking and never showed work landing.
    """
    mid, pid = _pillar_for_activity()
    with patch.object(mc_api, "_publish_activity_event", new=AsyncMock()) as pub:
        r = _client().post(f"/api/pillars/{pid}/last-done",
                           json={"last_done": "Finished the thing."})
    assert r.status_code == 200, r.text
    pub.assert_awaited_once()
    kwargs = pub.await_args.kwargs
    assert pub.await_args.args[0] == "status"
    assert pub.await_args.args[1] == mid
    assert kwargs["text"] == "Finished the thing."


def test_clearing_the_status_line_publishes_nothing():
    """Clearing resets the card to 'never written'. That is not a thing that
    happened, so it is not announced as one.
    """
    _mid, pid = _pillar_for_activity()
    _client().post(f"/api/pillars/{pid}/last-done", json={"last_done": "Done."})
    with patch.object(mc_api, "_publish_activity_event", new=AsyncMock()) as pub:
        r = _client().post(f"/api/pillars/{pid}/last-done", json={"last_done": ""})
    assert r.status_code == 200
    pub.assert_not_awaited()


def test_pushing_a_screen_is_published_as_an_event():
    _mid, pid = _pillar_for_activity()
    with patch.object(mc_api, "_publish_activity_event", new=AsyncMock()) as pub:
        r = _client().post(f"/api/pillars/{pid}/site",
                           json={"html": "<p>hi</p>", "note": "rev1"})
    assert r.status_code == 201, r.text
    pub.assert_awaited_once()
    assert pub.await_args.args[0] == "revision"


def test_a_publish_failure_never_fails_the_write():
    """The record matters more than the notification. A missed event costs a
    reader one stale line; a failed status write costs the record itself.
    """
    _mid, pid = _pillar_for_activity()
    with patch.object(mc_api.event_bus, "broadcast",
                      new=AsyncMock(side_effect=RuntimeError("bus down"))):
        r = _client().post(f"/api/pillars/{pid}/last-done",
                           json={"last_done": "Still recorded."})
    assert r.status_code == 200, r.text
    assert db.get_pillar(pid)["last_done"] == "Still recorded."


# ── rephrasing a question (auto-srgag) ───────────────────────────


def test_a_question_can_be_reworded_without_losing_its_answer():
    """The same entry, the same answer, the same anchor — only the words
    change. Starting a fresh entry would strand the answer that already
    belongs to this one, which is the whole reason this is an edit and not a
    re-ask.
    """
    mid = db.create_mission("M", "coord-session")["mission_id"]
    entry = db.ask_question(mid, "wat r teh implickatons", "guest:1", "Jamie")
    db.answer_question(mid, entry["entry_id"], "Q3 2026.", "coord-session")

    r = _client().post(
        f"/api/missions/{mid}/questions/{entry['entry_id']}/rephrase",
        json={"question": "What are the implications?"},
    )
    assert r.status_code == 200, r.text
    q = r.json()["question"]
    assert q["question"] == "What are the implications?"
    assert q["entry_id"] == entry["entry_id"], "same entry, not a new one"
    assert q["answer"] == "Q3 2026.", "the answer must survive a rewording"


def test_rephrasing_records_who_changed_it():
    """A reader finding wording that does not match the answer beneath it
    needs to know whether the asker tightened their own question or somebody
    else rewrote it for them.
    """
    mid = db.create_mission("M", "coord-session")["mission_id"]
    entry = db.ask_question(mid, "original", "guest:1", "Jamie")
    _client().post(
        f"/api/missions/{mid}/questions/{entry['entry_id']}/rephrase",
        json={"question": "clearer"},
    )
    row = db.get_conversation_entry(entry["entry_id"])
    assert row["question_edited_by_session"] == "coord-session"
    assert row["question_edited_at"] is not None


def test_an_empty_rephrase_is_refused():
    """Blank is not a rewording. Accepting it would erase the question and
    leave an answer with nothing above it.
    """
    mid = db.create_mission("M", "coord-session")["mission_id"]
    entry = db.ask_question(mid, "original", "guest:1", "Jamie")
    r = _client().post(
        f"/api/missions/{mid}/questions/{entry['entry_id']}/rephrase",
        json={"question": "   "},
    )
    assert r.status_code == 400
    assert db.get_conversation_entry(entry["entry_id"])["question"] == "original"


# ── the client/server seam ────────────────────────────────────────
#
# handle_relay_write's tests above call it with the SERVER's vocabulary,
# and the viewer's own tests never leave the browser. Between the two sat
# a client posting {"kind": "question", "body": ...} to a handler reading
# `question`: every ask over a share link was refused, while the identical
# call at a real URL worked, because the HTTP path happened to translate
# the key on the way past. Both ends were covered and the seam was not.
# These read the body the client actually builds and put it through the
# handler that actually receives it.

_BOOTSTRAP = (
    Path(__file__).resolve().parents[1] / "viewer" / "bootstrap.js"
).read_text()


def test_the_viewer_speaks_the_relay_handlers_field_names():
    """A write body is forwarded to handle_relay_write completely
    uninterpreted, so the client has no translation layer to hide behind --
    the names it writes are the names that must arrive."""
    assert '{kind: "question", question: text}' in _BOOTSTRAP, (
        "the viewer no longer sends the `question` field handle_relay_write reads"
    )
    assert 'body[kind === "answer" ? "answer" : "followup"] = text;' in _BOOTSTRAP, (
        "the viewer no longer sends the `followup`/`answer` fields the server reads"
    )
    assert "body: text" not in _BOOTSTRAP, (
        "a generic `body` key is exactly the mismatch this seam already shipped once"
    )


def test_the_body_the_viewer_builds_is_accepted_by_the_relay_handler():
    """The client's literal shape, not a shape written for the server."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)

    # Exactly what ask() posts for an anchored question on a pillar-less view.
    body = {"kind": "question", "question": "does the seam hold?", "anchor": "table:one"}

    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        result = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id, body,
        ))

    assert result is not None, "the handler refused the body the client sends"
    assert result["question"]["question"] == "does the seam hold?"
    assert result["question"]["anchor"] == "table:one"


def test_asking_lands_on_the_new_entry_rather_than_closing_everything():
    """On success the viewer used to call show(null), tearing down the whole
    chrome -- so a sent question vanished along with the surface that would
    have shown it being worked on. The entry_id comes back in the response;
    the view it opens is where progress updates render."""
    assert "show({entry: q.entry_id, from: from})" in _BOOTSTRAP, (
        "asking no longer lands on the entry it just created"
    )


# ── open/closed as a fact, not an inference ───────────────────────
#
# Closed used to be read off the answer field. That cannot tell a question
# you asked from one asked of you, so the app offered its only composer --
# the one that files an answer -- on the operator's own open question, and
# using it closed his question with his own words and recorded him as having
# answered himself.


def test_answering_does_not_end_the_conversation():
    """A reply is not presumed to be the end of it. The person who asked may
    have more to say, and very often the first thing back is a question of
    their own. A screen's question stops being asked when the screen stops
    asking it; a person's, when they close it."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        entry = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "why?"},
        ))["question"]
    r = client.post(
        f"/api/missions/{mission_id}/questions/{entry['entry_id']}/answer",
        json={"answer": "because"})
    assert r.status_code == 200
    q = r.json()["question"]
    assert q["answer"] == "because"
    assert q["closed_at"] is None, "answering ended the conversation"


def test_the_asker_adding_to_their_own_question_does_not_close_it():
    """The bug, in one assertion: more from the asker is more of the ask."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        entry = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "why?"},
        ))["question"]
        after = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "followup", "entry_id": entry["entry_id"],
             "followup": "and also when?"},
        ))["question"]

    assert after["closed_at"] is None, "the asker closed their own question"
    assert after["answer"] is None, "the follow-up was recorded as an answer"
    assert "why?" in after["question"] and "and also when?" in after["question"]
    # The coordinator was sent a question that is no longer the one on record.
    assert after["relay_status"] == "pending"


def test_only_the_asker_may_add_to_or_close_a_question():
    """A channel is bound to one identity. Without this check any guest could
    amend or close anybody else's question on a mission they can merely see."""
    client = _client()
    mission_id = _mission_with_site(client)
    asker = _visitor(client)
    other = client.post(
        "/api/visitor-tokens", json={"display_name": "Someone Else"},
    ).json()["visitor"]
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        entry = _run(mc_api.handle_relay_write(
            asker["participant_id"], mission_id,
            {"kind": "question", "question": "mine"},
        ))["question"]
        for body in (
            {"kind": "followup", "entry_id": entry["entry_id"], "followup": "sneak"},
            {"kind": "close", "entry_id": entry["entry_id"]},
        ):
            assert _run(mc_api.handle_relay_write(
                other["participant_id"], mission_id, body)) is None

    still = db.get_question(mission_id, entry["entry_id"])
    assert still["question"] == "mine" and still["closed_at"] is None


def test_a_question_can_be_closed_without_inventing_an_answer():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        entry = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "never mind"},
        ))["question"]
        closed = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "close", "entry_id": entry["entry_id"]},
        ))["question"]

    assert closed["closed_at"] is not None
    assert closed["answer"] is None, "closing fabricated an answer"


def test_closing_does_not_ask_the_coordinator_again():
    """Every other kind leaves someone holding a question that changed under
    them. A closed entry wants nothing from anybody."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock) as relay:
        entry = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "q"},
        ))["question"]
        relay.reset_mock()
        _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "close", "entry_id": entry["entry_id"]},
        ))
        relay.assert_not_called()


def test_the_screen_is_told_which_reader_it_is_for():
    """Without this the chrome cannot tell your question from one asked of
    you, which is the whole reason it offered the wrong control."""
    from tools.dashboard.plugins.mission_control import compose

    mission = db.create_mission("Viewer")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>x</html>", "first")
    doc = compose.compose_screen(mission_id, viewer="guest:abc").decode()
    assert '"me":"guest:abc"' in doc.replace(" ", "")
    anon = compose.compose_screen(mission_id).decode()
    assert '"me":null' in anon.replace(" ", "")


def test_the_dashboard_can_say_it_is_here():
    """The relay recorded a reader on a screen from the start; the dashboard
    had no route to. So in the app the list of who is here was empty on every
    mission, always -- five pillars can be working and it reads as abandoned,
    which on its own sends a reader back to opening sessions one at a time."""
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "P", "sess", "#4ade80")

    with patch.object(mc_api, "_heartbeat_presence") as beat:
        assert client.post(f"/api/missions/{mission_id}/here").status_code == 200
        assert client.post(f"/api/pillars/{pillar['pillar_id']}/here").status_code == 200

    surfaces = [c.args[0] for c in beat.call_args_list]
    assert surfaces == [f"mission:{mission_id}", f"pillar:{pillar['pillar_id']}"], (
        "presence was recorded against the wrong surface"
    )
    # The convention the whole plugin shares: one surface per mission, one per
    # pillar -- not one for the page.
    assert client.post("/api/missions/nope/here").status_code == 404
    assert client.post("/api/pillars/nope/here").status_code == 404


def test_an_unidentified_reader_is_one_presence_not_a_new_person_each_time():
    """Nothing here can tell two signed-out readers apart, so claiming
    otherwise would inflate the count with strangers."""
    client = _client()
    mission_id = _mission_with_site(client)
    with patch.object(mc_api, "_heartbeat_presence") as beat:
        client.post(f"/api/missions/{mission_id}/here")
        client.post(f"/api/missions/{mission_id}/here")
    who = {c.args[1] for c in beat.call_args_list}
    assert len(who) == 1, f"each visit invented a different participant: {who}"


def test_the_operator_is_one_person_on_the_presence_list():
    """Attribution and presence are different namespaces for the same human.
    Writing the attribution id onto a presence surface put the operator on the
    list twice, under two ids with the same name -- on the one surface whose
    whole job is showing who is here."""
    client = _client()
    mission_id = _mission_with_site(client)
    with patch.object(mc_api, "_operator_identity") as ident, \
         patch.object(mc_api, "_heartbeat_presence") as beat:
        ident.return_value = {
            "participant_id": mc_api.OPERATOR_PARTICIPANT_ID,
            "participant_label": "Jeremy Spilman",
        }
        client.post(f"/api/missions/{mission_id}/here")
    who, kind = beat.call_args.args[1], beat.call_args.kwargs["kind"]
    assert who == mc_api.OPERATOR_PRESENCE_ID
    assert kind == "operator"


def test_the_operator_presence_id_matches_the_one_every_page_uses():
    """No shared source for the two, so drift is only visible as the operator
    appearing twice on a live screen. Pin them instead."""
    js = (
        Path(__file__).resolve().parents[3]
        / "static" / "js" / "identity-indicator.js"
    ).read_text()
    assert f"Autonomy.operatorId = '{mc_api.OPERATOR_PRESENCE_ID}'" in js, (
        "the dashboard's operator presence id changed; mission screens will "
        "now record the operator as a second, separate person"
    )


# ── a screen asking YOU ───────────────────────────────────────────
#
# An anchor could only ever be asked ABOUT. When a screen needed something
# decided there was nowhere to put it, so the operator typed his answer into
# the only composer there was -- the one that asks -- and it was recorded as
# HIS question, still waiting on the pillar. The roles were exactly inverted.


def _op(monkeypatched=None):
    return {"participant_id": mc_api.OPERATOR_PARTICIPANT_ID,
            "participant_label": "Jeremy Spilman"}


def test_a_screens_question_lands_asked_by_the_screen_and_answered_by_you():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "sess-infra", "#4ade80")

    with patch.object(mc_api, "_operator_identity", return_value=_op()), \
         patch.object(mc_api, "_relay_answer", new_callable=AsyncMock):
        r = client.post(
            f"/api/pillars/{pillar['pillar_id']}/asked",
            json={"anchor": "decision:dispatch", "question": "Dispatcher on or off?",
                  "answer": "On -- the guidance changed."})

    assert r.status_code == 201
    q = r.json()["question"]
    assert q["asked_by_label"] == "Infra", "the screen is not recorded as the asker"
    assert q["asked_by_participant_id"] == f"pillar:{pillar['pillar_id']}"
    assert q["answered_by_session"] == "Jeremy Spilman", "the operator is not the answerer"
    assert q["answer"] == "On -- the guidance changed."
    # A screen's question is cleared by the screen, not by being answered.
    assert q["closed_at"] is None
    assert q["anchor"] == "decision:dispatch"


def test_a_screens_question_is_never_relayed_back_as_a_question():
    """The coordinator wrote it. Sending it to them would ask them to answer
    their own screen."""
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "sess-infra", "#4ade80")
    with patch.object(mc_api, "_operator_identity", return_value=_op()), \
         patch.object(mc_api, "_relay_question", new_callable=AsyncMock) as asked, \
         patch.object(mc_api, "_relay_answer", new_callable=AsyncMock):
        client.post(f"/api/pillars/{pillar['pillar_id']}/asked",
                    json={"question": "q?", "answer": "a."})
    asked.assert_not_called()
    # A pillar-scoped entry: list_conversation is mission-level only.
    entry = db.list_whole_mission_conversation(mission_id)[0]
    assert entry["relay_status"] == "sent", "it looks undelivered forever"


def test_answering_a_screens_question_needs_both_halves():
    client = _client()
    mission_id = _mission_with_site(client)
    with patch.object(mc_api, "_operator_identity", return_value=_op()):
        assert client.post(f"/api/missions/{mission_id}/asked",
                           json={"question": "q?"}).status_code == 400
        assert client.post(f"/api/missions/{mission_id}/asked",
                           json={"answer": "a."}).status_code == 400


def test_an_unidentified_reader_cannot_answer_for_the_operator():
    client = _client()
    mission_id = _mission_with_site(client)
    with patch.object(mc_api, "_operator_identity", return_value=None):
        r = client.post(f"/api/missions/{mission_id}/asked",
                        json={"question": "q?", "answer": "a."})
    assert r.status_code == 403
    assert db.list_conversation(mission_id) == []


def test_a_question_closed_without_an_answer_can_still_be_reopened():
    """Reopening was gated on there being an answer, which was the same as
    closed right up until closing became its own act. After that, a question
    closed because it stopped mattering was the one kind of entry that could
    never be reopened -- and it is the kind most likely to come back."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        entry = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "shelved for now"},
        ))["question"]
        _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "close", "entry_id": entry["entry_id"]},
        ))
        again = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "reopen", "entry_id": entry["entry_id"],
             "followup": "it matters again"},
        ))

    assert again is not None, "a question closed without an answer cannot come back"
    assert again["question"]["closed_at"] is None
    trail = [u["text"] for u in again["question"]["updates"]]
    assert not any(t.startswith("Previous answer") for t in trail), (
        "the responder was handed a previous answer that never existed"
    )


# ── what a question carries with it ───────────────────────────────
#
# "Do it" reached a coordinator as two words and "(re:
# decision:settings-invariant-bump)" -- a slug the screen's author keyed it
# by. The reminder a minute later dropped even that, and told the reader to
# go find the earlier message. The page knew the heading the anchor sat
# under the whole time; it had nowhere to put it.


def test_a_question_carries_what_it_is_about_in_words():
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        q = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "Do it",
             "anchor": "decision:settings-invariant-bump",
             "anchor_title": "Bump the settings schema version"},
        ))["question"]
    assert q["anchor_title"] == "Bump the settings schema version"
    assert q["anchor"] == "decision:settings-invariant-bump"


def test_the_relay_leads_with_the_heading_not_the_slug():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "auto-infra", "#4ade80")
    entry = db.ask_question(
        mission_id, "Do it", "guest:1", "Jeremy Spilman",
        pillar_id=pillar["pillar_id"], anchor="decision:settings-invariant-bump",
        anchor_title="Bump the settings schema version",
    )
    sent = {}

    async def fake(target, body, **kw):
        sent[target] = body
        return True

    with patch("tools.dashboard.tmux_send.tmux_send", new=fake):
        _run(mc_api._relay_question(
            mission_id=mission_id, entry_id=entry["entry_id"]))

    envelope = sent["auto-infra"]
    assert "Bump the settings schema version" in envelope
    assert "decision:settings-invariant-bump" not in envelope, (
        "the slug is still what the reader is shown"
    )
    # The protocol lives in the skill; what varies belongs in the message.
    assert "checking the acquisition log" not in envelope
    assert "If the guest pushes back later" not in envelope
    assert "/answer" in envelope, "no way to reply was carried"


def test_a_closed_or_retired_question_stops_nagging():
    """The reminder query asked whether an answer existed, which stopped being
    the same as open the moment closing became its own act -- and it never
    excluded retired entries at all. Both would nag a coordinator forever
    about something nobody is waiting on."""
    client = _client()
    mission_id = _mission_with_site(client)
    visitor = _visitor(client)
    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        closed = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "never mind"},
        ))["question"]
        _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "close", "entry_id": closed["entry_id"]},
        ))
    retired = db.ask_question(mission_id, "gone", "guest:1", "J")
    db.retire_question(retired["entry_id"], "the subject was replaced")

    open_by_session = db.list_coordinators_with_open_questions()
    still = [e["entry_id"] for entries in open_by_session.values() for e in entries]
    assert closed["entry_id"] not in still, "a closed question still nags"
    assert retired["entry_id"] not in still, "a retired question still nags"


def test_you_are_never_nagged_about_a_question_your_own_screen_asked():
    """Every open entry used to be one a guest asked and a coordinator owed,
    so mapping an entry to its coordinator was the same as mapping it to
    whoever owed the answer. A screen asking the operator something breaks
    that -- the asker IS the coordinator. Unanswered it cannot bite (no row
    exists until it is answered), but reopening your own answered ask puts one
    back in scope, and you would be chased for a decision only the operator
    can make."""
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "auto-infra", "#4ade80")

    with patch.object(mc_api, "_operator_identity", return_value={
            "participant_id": mc_api.OPERATOR_PARTICIPANT_ID,
            "participant_label": "Jeremy Spilman"}), \
         patch.object(mc_api, "_relay_answer", new_callable=AsyncMock):
        entry = client.post(
            f"/api/pillars/{pillar['pillar_id']}/asked",
            json={"question": "Run or ecosystem?", "answer": "Run."},
        ).json()["question"]

    # Answered, so closed, so out of scope either way.
    assert entry["entry_id"] not in _open_entry_ids()
    # The coordinator pushes back on the operator's answer.
    db.reopen_question(mission_id, entry["entry_id"], "that breaks re-ingest", "Infra")
    assert entry["entry_id"] not in _open_entry_ids(), (
        "the coordinator is being chased for an answer only the operator can give"
    )


def _open_entry_ids():
    return [e["entry_id"]
            for entries in db.list_coordinators_with_open_questions().values()
            for e in entries]


def test_delivery_leaves_a_receipt_in_the_conversation():
    """Whether a message reached anyone was a field on the entry that only the
    asker's screen read: the sender saw a state, the person it was sent to saw
    nothing. The receipt sits in the same column as everything else that
    happened, in the order it happened."""
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "auto-infra", "#4ade80")
    visitor = _visitor(client)

    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock) as send:
        send.return_value = True
        _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "did this arrive?",
             "pillar_id": pillar["pillar_id"]},
        ))
        await_all = [e for e in db.list_whole_mission_conversation(mission_id)]

    entry = await_all[0]
    trail = [u["text"] for u in db.list_conversation_updates(entry["entry_id"])]
    assert any(t.startswith("Delivered to") for t in trail), (
        f"no receipt in the conversation: {trail}"
    )


def test_a_failed_delivery_says_so_in_the_conversation():
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "auto-infra", "#4ade80")
    visitor = _visitor(client)

    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock) as send:
        send.side_effect = RuntimeError("no such session")
        _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "does this arrive?",
             "pillar_id": pillar["pillar_id"]},
        ))

    entry = db.list_whole_mission_conversation(mission_id)[0]
    trail = [u["text"] for u in db.list_conversation_updates(entry["entry_id"])]
    assert any("Not delivered" in t for t in trail), (
        f"a failed delivery is silent in the conversation: {trail}"
    )


# ── a coordinator acting on its own surface ───────────────────────


def test_a_coordinator_session_is_a_participant_on_its_own_surface():
    """A pillar screen can ask the operator a question, so a coordinator is an
    ASKER on its own surface -- and could not act as one, because identity
    resolution knew only a browser operator and a link guest. The session it
    acts as is the same pillar identity its screen's questions are already
    attributed to."""
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Infra", "auto-infra", "#4ade80")

    req = MagicMock()
    req.headers = {"authorization": "Bearer whatever"}
    req.cookies = {}
    req.query_params = {}

    with patch("tools.dashboard.server.authenticate_session_request",
               return_value=(("auto-infra", "autonomy"), None)):
        identity = mc_api._resolve_visitor_identity(req)

    assert identity["participant_id"] == f"pillar:{pillar['pillar_id']}"
    assert identity["participant_label"] == "Infra"


def test_a_session_that_coordinates_nothing_here_is_nobody_here():
    """Authenticating proves which session is calling, not that it has any
    standing on this mission."""
    client = _client()
    _mission_with_site(client)

    req = MagicMock()
    req.headers = {"authorization": "Bearer whatever"}
    req.cookies = {}
    req.query_params = {}

    with patch("tools.dashboard.server.authenticate_session_request",
               return_value=(("auto-a-stranger", "autonomy"), None)), \
         patch.object(mc_api, "_operator_identity", return_value=None):
        assert mc_api._resolve_visitor_identity(req) is None


def test_a_visitor_token_still_wins_over_a_bearer():
    """A guest on a share link presents a token; an agent presents a bearer.
    The two must not be confusable for one another."""
    client = _client()
    _mission_with_site(client)
    visitor = _visitor(client)

    req = MagicMock()
    req.headers = {"authorization": "Bearer whatever"}
    req.cookies = {VISITOR_COOKIE_NAME: visitor["token"]}
    req.query_params = {}

    identity = mc_api._resolve_visitor_identity(req)
    assert identity["participant_id"] == visitor["participant_id"]


VISITOR_COOKIE_NAME = mc_api.VISITOR_COOKIE


def test_an_edited_question_says_it_was_edited_and_by_whom():
    """A pillar keeps its record clear by adjusting the text, including the
    operator's. A reader finding wording that does not match what someone
    remembers saying has to be able to see that it was edited. It was recorded
    from the start and never sent to anybody, which is the same as not
    recording it."""
    client = _client()
    mission_id = client.post(
        "/api/missions", json={"name": "A", "coordinator_session": "auto-coordinator"},
    ).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    visitor = _visitor(client)
    with patch("tools.dashboard.tmux_send.tmux_send", new_callable=AsyncMock):
        asked = client.post(
            f"/api/missions/{mission_id}/questions?as={visitor['token']}",
            json={"question": "rambling original, mid-thought"},
        ).json()["question"]
    assert asked["question_edited_at"] is None
    assert asked["question_edited_by_session"] is None

    edited = client.post(
        f"/api/missions/{mission_id}/questions/{asked['entry_id']}/rephrase",
        json={"question": "the question that was actually answered"},
    ).json()["question"]
    assert edited["question"] == "the question that was actually answered"
    assert edited["question_edited_at"] is not None
    # No caller identity here, so it falls back to the session named on the
    # mission. An authenticated caller is attributed to itself instead --
    # see test_a_coordinator_session_is_a_participant_on_its_own_surface.
    assert edited["question_edited_by_session"] == "auto-coordinator"


def test_a_question_carries_the_text_it_replies_to():
    """A heading names the thing being replied to and does not say what it
    said. "(re: Now)" reached a coordinator with no indication of WHICH status
    report was being answered. Sending the slug was the first version of this
    fault and sending the heading was the second."""
    client = _client()
    mission_id = _mission_with_site(client)
    pillar = db.create_pillar(mission_id, "Crypto", "auto-crypto", "#818cf8")
    visitor = _visitor(client)
    said = "Mapped out the secrets vault and found the storage lockbox unbuilt."

    with patch.object(mc_api, "_relay_question", new_callable=AsyncMock):
        q = _run(mc_api.handle_relay_write(
            visitor["participant_id"], mission_id,
            {"kind": "question", "question": "Which lockbox?",
             "pillar_id": pillar["pillar_id"],
             "anchor": "scoping-vault", "anchor_title": "Now",
             "anchor_excerpt": said},
        ))["question"]
    assert q["anchor_excerpt"] == said

    sent = {}

    async def fake(target, body, **kw):
        sent[target] = body
        return True

    with patch("tools.dashboard.tmux_send.tmux_send", new=fake):
        _run(mc_api._relay_question(mission_id=mission_id, entry_id=q["entry_id"]))

    envelope = sent["auto-crypto"]
    assert "In reply to:" in envelope, "the coordinator cannot tell what is being answered"
    assert said in envelope


# ── Pointing a pillar at the session that runs it now ──────────────


def test_a_pillar_can_be_pointed_at_the_session_running_it_now(tmp_path):
    """A pillar outlives the session running it. Until this route existed the
    only way to correct the name was to delete the pillar and make a new one,
    which discards its screen history and every question asked on it."""
    db_path = tmp_path / "mc.db"
    mission = db.create_mission("Multi-User", "auto-controller", db_path=db_path)
    pillar = db.create_pillar(
        mission["mission_id"], "Platform", "auto-retired", "#fff", db_path=db_path)

    assert db.set_pillar_coordinator(
        pillar["pillar_id"], "auto-incoming", db_path=db_path) is True
    assert db.get_pillar(
        pillar["pillar_id"], db_path=db_path)["coordinator_session"] == "auto-incoming"


def test_the_pillar_keeps_its_screens_and_questions_when_it_changes_hands(tmp_path):
    """The reason the route exists rather than delete-and-recreate."""
    db_path = tmp_path / "mc.db"
    mission = db.create_mission("Multi-User", "auto-controller", db_path=db_path)
    pillar = db.create_pillar(
        mission["mission_id"], "Platform", "auto-retired", "#fff", db_path=db_path)
    db.push_pillar_site_revision(
        pillar["pillar_id"], "<p>the screen</p>", "rev1", db_path=db_path)
    entry = db.ask_question(
        mission["mission_id"], "why three paths?", "guest:jeremy", "Jeremy",
        pillar_id=pillar["pillar_id"], db_path=db_path)

    db.set_pillar_coordinator(pillar["pillar_id"], "auto-incoming", db_path=db_path)

    site = db.get_current_pillar_site(pillar["pillar_id"], db_path=db_path)
    assert site["html"] == "<p>the screen</p>"
    kept = db.list_pillar_conversation(pillar["pillar_id"], db_path=db_path)
    assert [q["entry_id"] for q in kept] == [entry["entry_id"]]


def test_a_mission_can_be_pointed_at_the_session_running_it_now(tmp_path):
    db_path = tmp_path / "mc.db"
    mission = db.create_mission("Multi-User", "auto-retired", db_path=db_path)

    assert db.set_mission_coordinator(
        mission["mission_id"], "auto-incoming", db_path=db_path) is True
    assert db.get_mission(
        mission["mission_id"], db_path=db_path)["coordinator_session"] == "auto-incoming"


def test_pointing_a_pillar_that_does_not_exist_reports_that(tmp_path):
    db_path = tmp_path / "mc.db"
    assert db.set_pillar_coordinator("no-such-pillar", "auto-x", db_path=db_path) is False
    assert db.set_mission_coordinator("no-such-mission", "auto-x", db_path=db_path) is False


# ── The organization a mission belongs to ──────────────────────────


def test_a_mission_records_the_organization_it_belongs_to(tmp_path):
    """A mission's organization is the only boundary it has. Sessions in it
    reach the mission; sessions in another organization do not. There is
    nothing finer -- a mission has no members and no per-session rights."""
    db_path = tmp_path / "mc.db"
    mission = db.create_mission(
        "Multi-User", "auto-controller", org="autonomy", db_path=db_path)

    assert mission["org"] == "autonomy"
    assert db.get_mission(mission["mission_id"], db_path=db_path)["org"] == "autonomy"


def test_a_mission_is_never_stored_without_an_organization(tmp_path):
    """An empty organization would read as a mission nothing is bounded by,
    which is the one value that must never reach a row. A caller that does
    not say gets its own."""
    db_path = tmp_path / "mc.db"
    for supplied in (None, "", "   "):
        mission = db.create_mission(
            "M", "auto-x", org=supplied, db_path=db_path)
        assert mission["org"]
        assert db.get_mission(mission["mission_id"], db_path=db_path)["org"]


def test_missions_created_before_the_column_existed_are_given_one(tmp_path):
    """The live database predates this column. Its missions were all made by
    the one organization running the dashboard, so the migration names it
    rather than leaving rows unbounded."""
    import sqlite3

    db_path = tmp_path / "old.db"
    db.init_db(db_path)
    raw = sqlite3.connect(str(db_path))
    raw.execute("ALTER TABLE missions DROP COLUMN org")
    raw.execute(
        "INSERT INTO missions (mission_id, name, coordinator_session,"
        " created_at, current_revision_id, status)"
        " VALUES ('m-old', 'OSS Insights', 'auto-0709', 1.0, NULL, 'active')")
    raw.commit()
    raw.close()

    migrated = db.get_mission("m-old", db_path=db_path)
    assert migrated["org"], "a pre-existing mission was left with no organization"
    assert migrated["name"] == "OSS Insights"


def test_the_api_reports_which_organization_a_mission_is_in(tmp_path):
    """A reader that cannot see the organization cannot tell a mission it is
    bounded by from one it is merely looking at."""
    db_path = tmp_path / "mc.db"
    mission = db.create_mission("M", "auto-x", org="autonomy", db_path=db_path)
    assert mc_api._mission_payload(
        db.get_mission(mission["mission_id"], db_path=db_path))["org"] == "autonomy"
