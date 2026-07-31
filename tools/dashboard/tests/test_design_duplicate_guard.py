"""HTTP contract for Design Studio duplicate-name protection."""

from __future__ import annotations

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
