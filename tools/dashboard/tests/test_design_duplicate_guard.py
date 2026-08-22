"""HTTP contract for Design Studio duplicate-name protection."""

from __future__ import annotations

from unittest.mock import AsyncMock

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agents.design_db import DuplicateDesignTitleError
from tools.dashboard import server


def test_design_create_returns_conflict_and_requires_boolean_force(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if not kwargs["force"]:
            raise DuplicateDesignTitleError(
                kwargs["title"],
                [{
                    "design_id": "design-1",
                    "latest_revision_id": "revision-2",
                    "title": kwargs["title"],
                    "revision_count": 2,
                }],
            )
        return "forced-revision"

    monkeypatch.setattr(server, "create_design", create)
    monkeypatch.setattr(server, "get_design", lambda _revision_id: None)
    app = Starlette(routes=[Route("/api/design", server.api_design_create, methods=["POST"])])

    with TestClient(app) as client:
        conflict = client.post(
            "/api/design",
            json={"title": "Release review", "variants": [{"id": "main", "html": "<main/>"}]},
        )
        string_force = client.post(
            "/api/design",
            json={
                "title": "Release review",
                "variants": [{"id": "main", "html": "<main/>"}],
                "force": "true",
            },
        )
        forced = client.post(
            "/api/design",
            json={
                "title": "Release review",
                "variants": [{"id": "main", "html": "<main/>"}],
                "force": True,
            },
        )

    assert conflict.status_code == 409
    assert conflict.json()["error"] == "duplicate_design_name"
    assert conflict.json()["existing"][0]["design_id"] == "design-1"
    assert string_force.status_code == 409
    assert forced.status_code == 201
    assert forced.json() == {"id": "forced-revision"}
    assert [call["force"] for call in calls] == [False, False, True]


def test_design_create_broadcasts_reverse_link_to_creator_session(monkeypatch):
    monkeypatch.setattr(server, "create_design", lambda **_kwargs: "revision-3")
    monkeypatch.setattr(server, "get_design", lambda _revision_id: {
        "design_id": "design-1",
        "revision_seq": 3,
        "title": "Session header polish",
        "linked_session": "auto-designer",
    })
    broadcast = AsyncMock()
    monkeypatch.setattr(server.event_bus, "broadcast", broadcast)
    app = Starlette(routes=[Route("/api/design", server.api_design_create, methods=["POST"])])

    with TestClient(app) as client:
        response = client.post(
            "/api/design",
            json={
                "title": "Session header polish",
                "creator_session_id": "auto-designer",
                "variants": [{"id": "main", "html": "<main/>"}],
            },
        )

    assert response.status_code == 201
    assert [call.args[0] for call in broadcast.await_args_list] == [
        "design:design-1",
        "session-design:auto-designer",
    ]
    assert broadcast.await_args_list[1].args[1] == {
        "revision_id": "revision-3",
        "latest_revision_id": "revision-3",
        "design_id": "design-1",
        "title": "Session header polish",
    }
