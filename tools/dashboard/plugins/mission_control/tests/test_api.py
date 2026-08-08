"""API tests for the Mission Control plugin."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
