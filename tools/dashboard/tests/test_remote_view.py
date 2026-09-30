"""Watching a remote session in Home's viewer (graph://7eb29bc8-31a §9.6,
bead auto-fd68i): tails of ``<name>@<machine>`` run on the far machine with
the viewer's query and come back addressed to the Home address; a watcher
republishes new entries on Home's bus while the session is viewed."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.responses import JSONResponse

from tools.dashboard import api_auth, remote_view, server
from tools.dashboard import session_control_client as scc
from tools.network import session_control

PEER = "a1" * 32
ADDRESS = "auto-9@sjc-2"


def test_rewrite_identity_names_the_home_address():
    data = {"session_id": "auto-9", "tmux_session": "auto-9", "tmux_name": "auto-9",
            "entries": [{"type": "viewer_attachment", "session": "auto-9"},
                        {"type": "assistant"}]}
    out = remote_view.rewrite_identity(data, ADDRESS)
    assert out["session_id"] == out["tmux_session"] == out["tmux_name"] == ADDRESS
    assert out["entries"][0]["session"] == ADDRESS
    assert "session" not in out["entries"][1]


def test_the_tail_op_runs_the_local_tail_with_the_viewers_query(monkeypatch):
    seen = {}

    async def fake_tail(request):
        seen["params"] = dict(request.path_params)
        seen["query"] = dict(request.query_params)
        return JSONResponse({"session_id": "auto-9", "entries": [{"n": 1}]})

    monkeypatch.setattr(server, "api_session_tail", fake_tail)
    reply = asyncio.run(server._inbound_session_tail(
        {"session_id": "auto-9", "project": "p",
         "query": {"after_file": "s1", "after": "10"}}, PEER))
    assert reply == {"v": 1, "ok": True,
                     "result": {"tail": {"session_id": "auto-9", "entries": [{"n": 1}]}}}
    assert seen == {"params": {"project": "p", "session_id": "auto-9"},
                    "query": {"after_file": "s1", "after": "10"}}


def test_a_large_tail_streams_and_unknown_query_keys_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)

    async def big_tail(request):
        return JSONResponse({"entries": [{"x": "y" * 300_000}]})

    monkeypatch.setattr(server, "api_session_tail", big_tail)
    reply = asyncio.run(server._inbound_session_tail(
        {"session_id": "auto-9", "project": "p", "query": {"tail_entries": "100"}}, PEER))
    staged = reply["result"]["stream_file"]
    assert reply["result"]["stream_delete"] is True
    assert json.loads(open(staged).read())["entries"][0]["x"].startswith("yyy")
    bad = asyncio.run(server._inbound_session_tail(
        {"session_id": "auto-9", "project": "p", "query": {"secret": "1"}}, PEER))
    assert bad["refusal"] == "invalid-query"


def test_a_missing_session_is_no_such_session(monkeypatch):
    async def missing(request):
        return JSONResponse({"error": "not found"}, status_code=404)

    monkeypatch.setattr(server, "api_session_tail", missing)
    reply = asyncio.run(server._inbound_session_tail(
        {"session_id": "auto-9", "project": "p", "query": {}}, PEER))
    assert reply["refusal"] == scc.NO_SUCH_SESSION


# ── Home: the viewer's routes ───────────────────────────────────────────────


class _Req:
    def __init__(self, path_params, query=None):
        self.path_params = path_params
        self.query_params = query or {}


def test_the_viewers_tail_is_proxied_and_rewritten(monkeypatch):
    async def fake_fetch(machine, name, project, query, *, timeout=20.0):
        assert (machine, name, project, query) == ("sjc-2", "auto-9", "p",
                                                   {"tail_entries": "100"})
        return {"v": 1, "ok": True, "tail": {"session_id": "auto-9", "entries": [],
                                             "chain": ["s1"], "offset": 42}}

    class Subscriptions:
        def connected(self, machine_pub):
            return machine_pub == PEER

    from tools.dashboard import fleet_machines

    monkeypatch.setattr(scc, "resolve_machine", lambda name: PEER)
    monkeypatch.setattr(fleet_machines, "label_for", lambda pub: "sjc-2")
    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    monkeypatch.setattr(remote_view, "fetch_tail", fake_fetch)
    monkeypatch.setattr(server, "_remote_subscriptions", Subscriptions())
    response = asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": ADDRESS},
        {"tail_entries": "100", "ignored": "x"})))
    assert response.status_code == 200
    body = json.loads(response.body)
    # Opened by display name, named by key -- as forwarded events name it
    # (auto-37b1t).
    assert body["session_id"] == body["machine_address"] == f"auto-9@{PEER}"
    assert (body["machine"], body["machine_pub"]) == ("sjc-2", PEER)   # display, key
    assert body["live_updates"] is True     # new entries arrive over the subscription


def test_the_viewers_tail_needs_global_authority(monkeypatch):
    monkeypatch.setattr(api_auth, "require_global_api_authority",
                        lambda r: JSONResponse({}, status_code=403))
    response = asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": ADDRESS})))
    assert response.status_code == 403


def test_the_viewer_page_redirects_a_remote_address_to_its_project(monkeypatch):
    from tools.dashboard import session_presence

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(scc, "resolve_machine", lambda name: PEER)
    monkeypatch.setattr(session_presence, "read_presence", lambda: [{
        "tmux_name": "auto-9", "machine_pub": PEER, "project": "autonomy-developer-opus",
        "machine": "sjc-2", "local": False}])
    response = asyncio.run(server.page_session_view_by_name(_Req({"session_id": ADDRESS})))
    assert response.status_code == 302
    assert response.headers["location"] == f"/session/autonomy-developer-opus/auto-9@{PEER}"


def test_the_viewer_page_turns_a_display_name_address_into_the_key(monkeypatch):
    """/session/<project>/<name>@<display name> would store the viewer under
    a key no forwarded event reaches (auto-37b1t)."""
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(server, "_load_template", lambda name: "<html></html>")
    monkeypatch.setattr(scc, "resolve_machine",
                        lambda name: PEER if name in ("sjc-2", PEER) else None)
    typed = asyncio.run(server.page_session_view(_Req({"project": "p", "session_id": ADDRESS})))
    assert typed.status_code == 302
    assert typed.headers["location"] == f"/session/p/auto-9@{PEER}"
    keyed = asyncio.run(server.page_session_view(
        _Req({"project": "p", "session_id": f"auto-9@{PEER}"})))
    assert keyed.status_code == 200
    unknown = asyncio.run(server.page_session_view(
        _Req({"project": "p", "session_id": "auto-9@nowhere"})))
    assert unknown.status_code == 200
    local = asyncio.run(server.page_session_view(_Req({"project": "p", "session_id": "auto-9"})))
    assert local.status_code == 200


def test_an_attachment_url_with_the_address_goes_to_the_remote_machine(tmp_path, monkeypatch):
    received = tmp_path / "copy"
    received.write_text("img")
    asked = []

    async def fake_request(machine, op, body=None, *, timeout=15.0, stream=False):
        asked.append((machine, op, body))
        return {"v": 1, "ok": True, "result": {"file": str(received)}}

    monkeypatch.setattr(scc, "request", fake_request)
    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    response = asyncio.run(server.api_session_output(_Req(
        {"tmux_name": ADDRESS, "path": "shot.png"})))
    assert response.status_code == 200
    assert asked == [("sjc-2", "output", {"tmux_name": "auto-9", "path": "shot.png"})]
