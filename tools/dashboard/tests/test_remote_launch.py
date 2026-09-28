"""Remote launch (graph://7eb29bc8-31a §9.2, §9.4, bead auto-8xlgo): the
``launch`` op starts a workspace session through the dashboard's own create
path, records the handshake-proved peer as its provenance, returns the same
session on a retried operation id, and refuses with typed reasons."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import session_control_client as scc
from tools.dashboard import session_presence
from tools.dashboard.dao import dashboard_db

PEER = "a1" * 32
HERE = session_presence.LocalMachine("b1" * 32, "b2" * 32)
OP_ID = "0f" * 16


class Response:
    def __init__(self, status, payload):
        self.status_code = status
        self.body = json.dumps(payload).encode()


@pytest.fixture
def db(tmp_path, monkeypatch):
    dashboard_db.init_db(tmp_path / "dashboard.db")
    monkeypatch.setattr(session_presence, "local_machine", lambda: HERE)
    monkeypatch.setattr(session_presence, "_machine_names",
                        lambda: {HERE.machine_id: "sjc-2"})
    return tmp_path


def _creator(calls, *, status=202, error=None):
    async def create(body):
        calls.append(body)
        name = f"auto-remote-{len(calls)}"
        if status == 202:
            dashboard_db.upsert_session(name, "container", body["project"])
            return Response(202, {"tmux_name": name, "pending": True})
        return Response(status, {"error": error})

    return create


def _launch(create, body=None, peer=PEER):
    body = {"operation_id": OP_ID, "project": "autonomy-developer-opus",
            "model": "claude-opus-5-5", **(body or {})}
    return asyncio.run(scc.launch_op(create)(body, peer))


def test_a_launch_runs_the_local_create_and_records_the_proved_peer(db):
    calls = []
    reply = _launch(_creator(calls), {"launched_by": "somebody-else",
                                      "home_machine": "cc" * 32})
    assert reply["ok"] is True, reply
    assert reply["result"] == {"tmux_name": "auto-remote-1",
                               "machine_pub": HERE.machine_pub, "machine": "sjc-2"}
    assert calls == [{"type": "container", "project": "autonomy-developer-opus",
                      "model": "claude-opus-5-5"}]
    row = dashboard_db.get_session("auto-remote-1")
    # the peer the handshake proved, never the body's claims
    assert row["home_machine"] == PEER
    assert row["launched_by"] == f"machine:{PEER}"
    assert row["launch_op_id"] == OP_ID


def test_a_retried_operation_returns_the_session_it_already_started(db):
    calls = []
    create = _creator(calls)
    launch = scc.launch_op(create)
    body = {"operation_id": OP_ID, "project": "p"}

    async def twice():
        return await launch(body, PEER), await launch(body, PEER)

    first, second = asyncio.run(twice())
    assert len(calls) == 1
    assert second["result"]["tmux_name"] == first["result"]["tmux_name"]
    assert second["result"]["repeated"] is True


def test_an_unknown_workspace_is_refused_by_name(db):
    reply = _launch(_creator([], status=400, error="Unknown project 'nope'"),
                    {"project": "nope"})
    assert reply["refusal"] == scc.WORKSPACE_UNAVAILABLE
    assert "nope" in reply["detail"]


def test_a_create_failure_is_a_typed_refusal_with_its_reason(db):
    reply = _launch(_creator([], status=503, error="session lifecycle queue is full"))
    assert reply["refusal"] == scc.LAUNCH_REFUSED
    assert "queue is full" in reply["detail"]


@pytest.mark.parametrize("body,peer", [
    ({"operation_id": "short"}, PEER),
    ({"project": ""}, PEER),
    ({}, ""),
])
def test_malformed_launches_are_refused_before_any_create(db, body, peer):
    calls = []
    reply = _launch(_creator(calls), body, peer)
    assert reply["refusal"] == "bad-request"
    assert calls == []


def test_the_presence_row_carries_the_launch_provenance(db):
    calls = []
    _launch(_creator(calls))
    live = [r for r in dashboard_db.get_live_sessions()
            if r["tmux_name"] == "auto-remote-1"]
    rows = session_presence.desired_rows(HERE.machine_id, live)
    assert rows["auto-remote-1"]["home_machine"] == PEER
    assert rows["auto-remote-1"]["launched_by"] == f"machine:{PEER}"


# ── the HTTP branch: api_session_create with ``machine`` ─────────────────────


@pytest.fixture
def remote(monkeypatch):
    from tools.dashboard import api_auth
    from tools.dashboard import server

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda request: None)
    monkeypatch.setattr(scc, "resolve_machine", lambda name: PEER if name == "sjc-2" else None)
    monkeypatch.setattr(session_presence, "local_machine", lambda: HERE)
    sent = []

    async def fake_request(machine, op, body=None, *, timeout=15.0):
        sent.append((machine, op, body))
        return fake_request.reply

    fake_request.reply = {"v": 1, "ok": True, "result": {
        "tmux_name": "auto-9", "machine": "sjc-2", "machine_pub": PEER}}
    monkeypatch.setattr(scc, "request", fake_request)
    return server, sent, fake_request


def _post(server, body):
    response = asyncio.run(server._create_remote_session(object(), body))
    return response.status_code, json.loads(response.body)


def test_a_remote_create_sends_launch_and_answers_like_a_local_one(remote):
    server, sent, _ = remote
    status, data = _post(server, {"machine": "sjc-2", "project": "p", "model": "m"})
    assert status == 202
    ((machine, op, body),) = sent
    assert data == {"tmux_name": "auto-9", "label": "", "type": "container",
                    "pending": True, "machine": "sjc-2", "machine_pub": PEER,
                    "operation_id": body["operation_id"]}
    assert (machine, op) == ("sjc-2", "launch")
    assert body["project"] == "p" and body["model"] == "m"
    assert len(body["operation_id"]) == 32


def test_a_remote_create_needs_a_workspace_project(remote):
    server, sent, _ = remote
    assert _post(server, {"machine": "sjc-2", "type": "host"})[0] == 400
    assert _post(server, {"machine": "sjc-2"})[0] == 400
    assert sent == []


def test_a_refused_launch_is_a_409_carrying_the_refusal(remote):
    server, _, fake = remote
    fake.reply = {"v": 1, "ok": False, "refusal": "destination-slot-absent"}
    status, data = _post(server, {"machine": "sjc-2", "project": "p"})
    assert status == 409 and data["refusal"] == "destination-slot-absent"
    fake.reply = {"v": 1, "ok": False, "refusal": "session-control-timeout"}
    assert _post(server, {"machine": "sjc-2", "project": "p"})[0] == 502


def test_naming_this_machine_creates_locally(remote, monkeypatch):
    server, sent, _ = remote
    monkeypatch.setattr(scc, "resolve_machine", lambda name: HERE.machine_pub)
    seen = []

    async def local(body, request=None):
        seen.append(body)
        return Response(202, {"tmux_name": "auto-local"})

    monkeypatch.setattr(server, "_create_session_from_body", local)
    assert _post(server, {"machine": "here", "project": "p"})[0] == 202
    assert seen == [{"project": "p"}] and sent == []


def test_a_remote_create_requires_global_authority(remote, monkeypatch):
    from starlette.responses import JSONResponse
    from tools.dashboard import api_auth

    server, sent, _ = remote
    monkeypatch.setattr(api_auth, "require_global_api_authority",
                        lambda request: JSONResponse({"error": "no"}, status_code=403))
    assert _post(server, {"machine": "sjc-2", "project": "p"})[0] == 403
    assert sent == []


def test_a_caller_operation_id_is_used_and_a_bad_one_refused(remote):
    server, sent, _ = remote
    op_id = "ab" * 16
    status, data = _post(server, {"machine": "sjc-2", "project": "p",
                                  "operation_id": op_id})
    assert status == 202 and data["operation_id"] == op_id
    assert sent[-1][2]["operation_id"] == op_id
    assert _post(server, {"machine": "sjc-2", "project": "p",
                          "operation_id": "nope"})[0] == 400


def test_a_lost_reply_is_retried_with_the_same_id_and_starts_one_session(
        db, remote, monkeypatch):
    """The far machine launches, the reply is lost (timeout), the retry with
    the SAME operation id returns that session, repeated, and there is one."""
    server, _sent, _ = remote
    calls = []
    far = scc.launch_op(_creator(calls))
    ids = []

    async def lossy(machine, op, body=None, *, timeout=15.0):
        ids.append(body["operation_id"])
        reply = await far(body, PEER)
        if len(ids) == 1:
            return {"v": 1, "ok": False, "refusal": "session-control-timeout"}
        return reply

    monkeypatch.setattr(scc, "request", lossy)
    status, data = _post(server, {"machine": "sjc-2", "project": "p"})
    assert status == 202, data
    assert ids[0] == ids[1]
    assert data["tmux_name"] == "auto-remote-1" and data["repeated"] is True
    assert len(calls) == 1
    assert [r["tmux_name"] for r in dashboard_db.get_live_sessions()
            if r.get("launch_op_id") == ids[0]] == ["auto-remote-1"]
