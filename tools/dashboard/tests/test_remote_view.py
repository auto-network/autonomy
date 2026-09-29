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


def test_forward_cursor_prefers_the_cursor_then_the_chain_end():
    assert remote_view.forward_cursor({"cursor": {"file": "b", "off": 7}}) == {"file": "b", "off": 7}
    assert remote_view.forward_cursor({"chain": ["a", "b"], "offset": 99}) == {"file": "b", "off": 99}
    assert remote_view.forward_cursor({}) is None


class Bus:
    def __init__(self):
        self.events = []

    async def broadcast(self, topic, data, dedup=True):
        self.events.append((topic, data))


def _watcher(replies, bus):
    asked = []

    async def fetch(machine, name, project, query, *, timeout=20.0):
        asked.append((machine, name, project, query))
        return replies.pop(0)

    return remote_view.RemoteWatcher(bus, fetch=fetch, interval=0.01, ttl=5), asked


def test_a_poll_republishes_new_entries_under_the_address_and_advances():
    bus = Bus()
    watcher, asked = _watcher([{"v": 1, "ok": True, "tail": {
        "session_id": "auto-9", "is_live": True,
        "entries": [{"type": "assistant", "text": "hi"}],
        "cursor": {"file": "s1", "off": 200}}}], bus)
    watch = remote_view._Watch(ADDRESS, "sjc-2", "auto-9", "p",
                               {"file": "s1", "off": 100}, until=1e18)
    assert asyncio.run(watcher.poll_once(watch)) is True
    assert asked == [("sjc-2", "auto-9", "p", {"after_file": "s1", "after": "100"})]
    ((topic, data),) = bus.events
    assert topic == "session:messages" and data["session_id"] == ADDRESS
    assert data["entries"] == [{"type": "assistant", "text": "hi"}]
    assert watch.cursor == {"file": "s1", "off": 200}


def test_an_ended_session_with_nothing_new_stops_the_watch():
    bus = Bus()
    watcher, _ = _watcher([{"v": 1, "ok": True, "tail": {
        "is_live": False, "entries": [], "cursor": {"file": "s1", "off": 5}}}], bus)
    watch = remote_view._Watch(ADDRESS, "sjc-2", "auto-9", "p",
                               {"file": "s1", "off": 5}, until=1e18)
    assert asyncio.run(watcher.poll_once(watch)) is False
    assert bus.events == []


def test_an_unreachable_machine_keeps_the_watch_and_its_cursor():
    bus = Bus()
    watcher, _ = _watcher([{"v": 1, "ok": False, "refusal": "destination-slot-absent"}], bus)
    watch = remote_view._Watch(ADDRESS, "sjc-2", "auto-9", "p",
                               {"file": "s1", "off": 5}, until=1e18)
    assert asyncio.run(watcher.poll_once(watch)) is True
    assert watch.cursor == {"file": "s1", "off": 5}


def test_a_second_tail_keeps_one_watch_alive_instead_of_starting_another():
    async def run():
        bus = Bus()
        replies = [{"v": 1, "ok": True, "tail": {"is_live": True, "entries": []}}] * 50
        watcher, _ = _watcher(list(replies), bus)
        watcher.watch(ADDRESS, "sjc-2", "auto-9", "p", {"file": "s1", "off": 0})
        watcher.watch(ADDRESS, "sjc-2", "auto-9", "p", {"file": "s1", "off": 0})
        assert watcher.watching() == [ADDRESS]
        await watcher.stop()

    asyncio.run(run())


# ── the far side: the tail op ───────────────────────────────────────────────


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


def test_the_viewers_tail_is_proxied_rewritten_and_watched(monkeypatch):
    watched = []

    async def fake_fetch(machine, name, project, query, *, timeout=20.0):
        assert (machine, name, project, query) == ("sjc-2", "auto-9", "p",
                                                   {"tail_entries": "100"})
        return {"v": 1, "ok": True, "tail": {"session_id": "auto-9", "entries": [],
                                             "chain": ["s1"], "offset": 42}}

    class W:
        def watch(self, *args):
            watched.append(args)
            return True

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    monkeypatch.setattr(remote_view, "fetch_tail", fake_fetch)
    monkeypatch.setattr(server, "_get_remote_watcher", lambda: W())
    response = asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": ADDRESS},
        {"tail_entries": "100", "ignored": "x"})))
    assert response.status_code == 200
    assert json.loads(response.body)["session_id"] == ADDRESS
    assert watched == [(ADDRESS, "sjc-2", "auto-9", "p", {"file": "s1", "off": 42})]


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
    assert response.headers["location"] == f"/session/autonomy-developer-opus/{ADDRESS}"


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



def test_watches_are_capped_and_the_tail_says_so():
    async def run():
        bus = Bus()
        replies = [{"v": 1, "ok": True, "tail": {"is_live": True, "entries": []}}] * 50
        watcher, _ = _watcher(list(replies), bus)
        watcher._max = 2
        results = [watcher.watch(f"auto-{i}@sjc-2", "sjc-2", f"auto-{i}", "p",
                                 {"file": "s", "off": 0}) for i in range(3)]
        assert results == [True, True, False]
        assert watcher.watch("auto-0@sjc-2", "sjc-2", "auto-0", "p",
                             {"file": "s", "off": 0}) is True    # keep-alive still works
        await watcher.stop()
        assert watcher.watching() == []

    asyncio.run(run())
