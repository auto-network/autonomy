"""Question entries notify the asker as well as the pillar coordinator.

A question asked by a session other than the pillar's coordinator (a
reviewer, a builder) used to hear nothing when the operator replied: the
entry relay went to the coordinator only (found live on the platform
pillar, question q-move-into-synced-org).
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import crosstalk_delivery
from tools.dashboard.dao import dashboard_db
from tools.dashboard.plugins.mission.entrypoints import api

_PATH = "/api/mission/item/{mission_id}/{pillar_id}/{item_id}/reply"
_SESSIONS = {"auto-coord", "auto-asker"}


def _client(monkeypatch, item: dict, by: str = "persona-key"):
    sent: list[tuple[str, str, str]] = []

    async def deliver(handle, to, message):
        sent.append((handle, to, message))
        return {"delivered": True}

    monkeypatch.setattr(api, "_owning_org", lambda request, mid: "autonomy")
    monkeypatch.setattr(api, "_identity", lambda request, org=None: by)
    monkeypatch.setattr(api.compose, "load_pillars", lambda org, mid: [
        {"pillar_id": "platform", "coordinator_session": "auto-coord"}])
    monkeypatch.setattr(dashboard_db, "session_exists", lambda n: n in _SESSIONS)
    monkeypatch.setattr(crosstalk_delivery, "deliver_from_chat", deliver)

    def write(org, mid, pid, iid, *, text, by):
        return item

    app = Starlette(routes=[Route(_PATH, api._entry_route(
        write, what="question reply"), methods=["POST"])])
    return TestClient(app), sent


def _post(client):
    r = client.post("/api/mission/item/m1/platform/q1/reply", json={"text": "use B"})
    assert r.status_code == 200, r.text
    return r.json()


def test_reply_reaches_coordinator_and_asking_session(monkeypatch):
    client, sent = _client(monkeypatch, {"kind": "question", "asked_by": "auto-asker"})
    body = _post(client)
    assert body["relayed"] is True and body["asker_notified"] is True
    assert [to for _, to, _ in sent] == ["auto-coord", "auto-asker"]
    assert "on your question q1: use B" in sent[1][2]
    assert "graph mission reply m1 platform q1" in sent[1][2]
    assert body["url"].endswith("/mission/m1#item=platform:q1")
    assert body["url"] in sent[0][2] and body["url"] in sent[1][2]


@pytest.mark.parametrize("asked_by, by", [
    ("auto-coord", "persona-key"),   # the coordinator already got it
    ("auto-asker", "auto-asker"),    # the asker wrote the entry itself
    ("persona-key-2", "persona-key"),  # a member persona is not an address
    ("", "persona-key"),
])
def test_asker_not_messaged_twice_or_at_non_sessions(monkeypatch, asked_by, by):
    client, sent = _client(monkeypatch, {"kind": "question", "asked_by": asked_by}, by=by)
    body = _post(client)
    assert body["asker_notified"] is False
    assert all(to != asked_by or to == "auto-coord" for _, to, _ in sent)
    assert len([1 for _, to, _ in sent if to == "auto-coord"]) <= 1


def test_non_question_entries_do_not_notify_asker(monkeypatch):
    client, sent = _client(monkeypatch, {"kind": "checkpoint", "asked_by": "auto-asker"})
    body = _post(client)
    assert body["asker_notified"] is False
    assert [to for _, to, _ in sent] == ["auto-coord"]
