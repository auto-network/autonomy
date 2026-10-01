"""Remote output files (graph://7eb29bc8-31a §9.2, bead auto-yi2pe part a):
the inbound ``output`` op resolves a session's /workspace/output file with
the same containment as the local route and hands the connector a file to
stream; the route's ``?m=`` branch serves the streamed copy once."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import api_auth, server
from tools.dashboard import session_control_client as scc
from tools.network import session_control

PEER = "a1" * 32


@pytest.fixture
def runs(tmp_path, monkeypatch):
    runs = tmp_path / "agent-runs"
    (runs / "auto-1-20260928-100000").mkdir(parents=True)
    (runs / "auto-1-20260928-100000" / "proof.txt").write_text("proof")
    monkeypatch.setattr(server, "AGENT_RUNS_DIR", runs)
    monkeypatch.setattr(server, "HOST_UPLOADS_DIR", tmp_path / "host-uploads")
    return runs


def _output(body):
    return asyncio.run(server._inbound_session_output(body, PEER))


def test_output_names_the_file_for_the_connector_to_stream(runs):
    reply = _output({"tmux_name": "auto-1", "path": "proof.txt"})
    assert reply["ok"] is True
    result = reply["result"]
    assert result["name"] == "proof.txt" and result["size"] == 5
    assert result["stream_file"] == str(
        (runs / "auto-1-20260928-100000" / "proof.txt").resolve())


@pytest.mark.parametrize("body,refusal", [
    ({"tmux_name": "auto-1", "path": "missing.txt"}, "file-not-found"),
    # Every session-control refusal names its own cause (f5864628).
    ({"tmux_name": "auto-1", "path": "../../etc/passwd"}, "invalid-path"),
    ({"tmux_name": "auto-1", "path": "/etc/passwd"}, "invalid-path"),
    ({"tmux_name": "bad name", "path": "proof.txt"}, "invalid-tmux-name"),
])
def test_output_refusals(runs, body, refusal):
    assert _output(body)["refusal"] == refusal


def test_an_oversize_file_is_refused(runs, monkeypatch):
    monkeypatch.setattr(session_control, "MAX_STREAM_BYTES", 3)
    assert _output({"tmux_name": "auto-1", "path": "proof.txt"})["refusal"] == "file-too-large"


class _Request:
    def __init__(self, name, path, machine):
        self.path_params = {"tmux_name": name, "path": path}
        self.query_params = {"m": machine}


def test_the_route_serves_the_streamed_copy_once_and_needs_authority(tmp_path, monkeypatch):
    from starlette.responses import JSONResponse

    received = tmp_path / "in-copy"
    received.write_text("remote proof")
    asked = []

    async def fake_request(machine, op, body=None, *, timeout=15.0, stream=False):
        asked.append((machine, op, body, stream))
        return {"v": 1, "ok": True, "result": {"name": "proof.txt",
                                               "file": str(received)}}

    monkeypatch.setattr(scc, "request", fake_request)
    monkeypatch.setattr(api_auth, "require_global_api_authority",
                        lambda request: JSONResponse({}, status_code=403))
    denied = asyncio.run(server.api_session_output(_Request("auto-1", "proof.txt", "sjc-2")))
    assert denied.status_code == 403 and asked == []

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda request: None)
    response = asyncio.run(server.api_session_output(_Request("auto-1", "proof.txt", "sjc-2")))
    assert response.status_code == 200
    assert str(response.path) == str(received)
    assert asked == [("sjc-2", "output", {"tmux_name": "auto-1", "path": "proof.txt"}, True)]
    asyncio.run(response.background())
    assert not received.exists()


def test_a_missing_remote_file_is_404(monkeypatch):
    async def fake_request(machine, op, body=None, *, timeout=15.0, stream=False):
        return {"v": 1, "ok": False, "refusal": "file-not-found"}

    monkeypatch.setattr(scc, "request", fake_request)
    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda request: None)
    response = asyncio.run(server.api_session_output(_Request("auto-1", "x.txt", "sjc-2")))
    assert response.status_code == 404
    assert json.loads(response.body)["refusal"] == "file-not-found"
