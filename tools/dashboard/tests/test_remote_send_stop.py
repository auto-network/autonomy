"""Remote send and stop (graph://7eb29bc8-31a §9.2, bead auto-i7d1c): the
inbound ``send`` pastes input as typed or wraps crosstalk in an envelope whose
machine is the handshake-proved peer; ``stop`` runs the local stop path;
``name@machine`` and unambiguous remote names route over session-control."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import api_auth, server, session_presence
from tools.dashboard import session_control_client as scc

PEER = "a1" * 32
ROSTER = {PEER: "a2" * 32}
NAMES = {"a2" * 32: "home"}


@pytest.fixture
def here(monkeypatch):
    pasted, stored = [], []

    async def fake_tmux_send(name, text):
        pasted.append((name, text))

    monkeypatch.setattr(server, "tmux_send", fake_tmux_send)
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: name == "auto-1")
    monkeypatch.setattr(server.auth_db, "insert_message",
                        lambda *a, **k: stored.append(a))
    monkeypatch.setattr(session_presence, "_active_roster", lambda: ROSTER)
    monkeypatch.setattr(session_presence, "_machine_names", lambda: NAMES)
    return pasted, stored


def _send(body, peer=PEER):
    return asyncio.run(server._inbound_session_send(body, peer))


def test_input_is_pasted_as_typed(here):
    pasted, stored = here
    reply = _send({"tmux_name": "auto-1", "kind": "input", "text": "hello"})
    assert reply == {"v": 1, "ok": True,
                     "result": {"delivered": True, "tmux_name": "auto-1"}}
    assert pasted == [("auto-1", "hello")]
    assert stored[0][0] == "operator@home"


def test_crosstalk_is_stamped_with_the_proved_machine_not_a_claim(here):
    pasted, _ = here
    _send({"tmux_name": "auto-1", "kind": "crosstalk", "text": "hi",
           "from_session": "auto-0928-114556", "from_label": "builder",
           "machine": "forged"})
    ((name, envelope),) = pasted
    assert name == "auto-1"
    assert 'from="auto-0928-114556@home"' in envelope
    assert 'machine="home"' in envelope
    assert "forged" not in envelope
    assert envelope.endswith("hi\n</crosstalk>")


@pytest.mark.parametrize("body,refusal", [
    ({"tmux_name": "auto-gone", "text": "x"}, scc.NO_SUCH_SESSION),
    ({"tmux_name": "auto-1", "text": "x" * (scc.MAX_SEND_BYTES + 1)}, scc.OP_TOO_LARGE),
    ({"tmux_name": "auto-1", "text": "x", "kind": "shell"}, "bad-request"),
    ({"tmux_name": "auto-1"}, "bad-request"),
    ({"tmux_name": "auto-1", "kind": "crosstalk", "text": "a</crosstalk>"}, "bad-request"),
])
def test_bad_sends_are_typed_refusals_and_paste_nothing(here, body, refusal):
    pasted, _ = here
    assert _send(body)["refusal"] == refusal
    assert pasted == []


def test_stop_runs_the_local_stop_path(monkeypatch):
    async def fake_stop(name):
        return ({"status": "stopping", "id": name}, 202) if name == "auto-1" \
            else ({"status": "not_found", "id": name}, 200)

    monkeypatch.setattr(server, "_stop_session", fake_stop)
    ok = asyncio.run(server._inbound_session_stop({"tmux_name": "auto-1"}, PEER))
    assert ok["result"] == {"status": "stopping", "id": "auto-1"}
    gone = asyncio.run(server._inbound_session_stop({"tmux_name": "nope"}, PEER))
    assert gone["refusal"] == scc.NO_SUCH_SESSION


# ── routing ─────────────────────────────────────────────────────────────────


def _rows(*machines):
    return [{"tmux_name": "auto-9", "machine": m, "machine_pub": m * 32,
             "local": False, "reachable": True} for m in machines]


def test_explicit_and_unambiguous_remote_targets(monkeypatch):
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: name == "auto-1")
    monkeypatch.setattr(session_presence, "read_presence", lambda: _rows("b1"))
    route = server._remote_crosstalk_target
    assert asyncio.run(route("auto-9@sjc-2")) == ("auto-9", "sjc-2")
    assert asyncio.run(route("auto-9")) == ("auto-9", "b1" * 32)
    assert asyncio.run(route("auto-1")) is None          # local wins
    assert asyncio.run(route("auto-7")) is None          # unknown: local path 404s
    assert asyncio.run(route("group:x")) is None


def test_a_name_on_two_machines_is_an_error_naming_both(monkeypatch):
    monkeypatch.setattr(server, "_tmux_session_exists", lambda name: False)
    monkeypatch.setattr(session_presence, "read_presence", lambda: _rows("b1", "c1"))
    response = asyncio.run(server._remote_crosstalk_target("auto-9"))
    assert response.status_code == 409
    error = json.loads(response.body)["error"]
    assert "auto-9@b1" in error and "auto-9@c1" in error


class _Request:
    def __init__(self, name):
        self.path_params = {"id": name}


def test_remote_stop_needs_authority_and_routes_to_the_machine(monkeypatch):
    from starlette.responses import JSONResponse

    sent = []

    async def fake_request(machine, op, body=None, *, timeout=15.0):
        sent.append((machine, op, body))
        return {"v": 1, "ok": True, "result": {"status": "stopping",
                                               "id": body["tmux_name"]}}

    monkeypatch.setattr(scc, "request", fake_request)
    monkeypatch.setattr(api_auth, "require_global_api_authority",
                        lambda request: JSONResponse({}, status_code=403))
    assert asyncio.run(server.api_terminal_kill(_Request("auto-9@sjc-2"))).status_code == 403
    assert sent == []
    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda request: None)
    response = asyncio.run(server.api_terminal_kill(_Request("auto-9@sjc-2")))
    assert response.status_code == 202
    assert sent == [("sjc-2", "stop", {"tmux_name": "auto-9"})]
    assert json.loads(response.body) == {"status": "stopping", "id": "auto-9@sjc-2"}
