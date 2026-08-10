"""API tests for the Mission Control plugin."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

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


def _client() -> TestClient:
    app = Starlette(routes=mc_api.routes)
    return TestClient(app)


def _https_client() -> TestClient:
    """Cookie-round-trip tests need an https:// base — the visitor
    cookie is set with secure=True (matches the unlock system's own
    session cookie convention), and httpx's cookie jar won't carry a
    Secure cookie back over a plain http:// scheme."""
    app = Starlette(routes=mc_api.routes)
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


# ── Chromeless public serving ────────────────────────────────────


def test_serve_mission_site_renders_current_revision_raw():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html><body>hello</body></html>"})

    resp = client.get(f"/missions/{mission_id}")
    assert resp.status_code == 200
    assert resp.text == "<html><body>hello</body></html>"
    assert resp.headers["content-type"].startswith("text/html")


def test_serve_mission_site_no_stale_serve_window():
    """A push must be visible on the very next request — response is
    marked uncacheable so no browser or proxy can serve a stale copy."""
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})

    first = client.get(f"/missions/{mission_id}")
    assert first.text == "<html>v1</html>"
    assert "no-store" in first.headers["cache-control"]

    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>"})
    second = client.get(f"/missions/{mission_id}")
    assert second.text == "<html>v2</html>"


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
    """The documented flow: visit the chromeless site once with ?as=,
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


def test_serve_pillar_site_chromeless():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    pillar = _pillar(client, mission_id)
    client.post(f"/api/pillars/{pillar['pillar_id']}/site", json={"html": "<html><body>pillar content</body></html>"})

    resp = client.get(f"/missions/{mission_id}/pillars/{pillar['pillar_id']}")
    assert resp.status_code == 200
    assert resp.text == "<html><body>pillar content</body></html>"


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
    assert "reply is expected" in envelopes["auto-pillar"]
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


def test_question_payload_hides_updates_once_answered():
    """Updates are working-in-progress noise, not part of the record --
    once an entry is answered, GET .../questions must stop returning its
    update trail. See _question_payload's docstring."""
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
    assert answered["updates"] == []
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
        resp = client.post(
            f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen",
            json={"followup": "not quite"},
        )
    assert resp.status_code == 401


def test_reopen_question_not_yet_answered_returns_404():
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


def test_reopen_then_reanswer_leaves_only_the_new_answer_visible():
    """The end-to-end point of reopening: after a full reopen -> re-answer
    cycle, the record shows one question and one (new) answer -- no trace
    of the old answer or the follow-up that prompted it."""
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
    assert final["updates"] == []


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
            client.post(
                f"/api/missions/{mission_id}/questions/{asked['entry_id']}/reopen?as={visitor['token']}",
                json={"followup": "not quite"},
            )
            events = []
            while not queue.empty():
                topic, data, seq = queue.get_nowait()
                if topic == mc_api.MISSION_CONVERSATION_TOPIC and seq != 0:
                    events.append(data)
            assert len(events) == 1
            assert events[0]["event"] == "reopened"
            assert events[0]["question"]["answer"] is None
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
        assert len(events) == 1
        assert events[0]["pillar_id"] == pillar["pillar_id"]
        assert events[0]["mission_id"] == mission_id
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
