"""L1 tests for the MCP relay: spec plumbing, peer lifecycle, scope gating.

Runs the real server in a subprocess against a stubbed `graph` binary, so no
graph database is touched.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

GATEWAY = Path(__file__).parent / "gateway.py"

GRAPH_STUB = """#!/usr/bin/env bash
case "$1" in
  search)    echo '[{"source_id": "abc-123", "source_title": "Stub hit", "source_type": "note", "content": "stub content body", "source_created_at": "2026-08-09T00:00:00Z"}]' ;;
  read)      echo "full stub source body" ;;
  tail)      echo "turn 1: stub tail" ;;
  sessions)  echo "TMUX  STATE  LABEL" ;;
  crosstalk)
    if [ "$2" = "send" ]; then echo "sent"; elif [ "$2" = "broadcast" ]; then echo "broadcast"; else echo "log line"; fi ;;
  note)      echo "note created: deadbeef-123" ;;
  *)         echo "unknown stub command: $1" >&2; exit 1 ;;
esac
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rpc(port, payload, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        return resp.status, json.loads(body) if body else None


def call_tool(port, name, arguments, msg_id=1):
    return rpc(port, {
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })[1]


def gateway_cli(data_dir, *argv):
    return subprocess.run(
        [sys.executable, str(GATEWAY), "--data-dir", str(data_dir), *argv],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("relay")
    stub = tmp / "graph-stub"
    stub.write_text(GRAPH_STUB)
    stub.chmod(0o755)
    data_dir = tmp / "data"
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--data-dir", str(data_dir), "run", "--port", str(port)],
        env={"PATH": "/usr/bin:/bin", "GRAPH_BIN": str(stub)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("relay did not start")
    yield {"port": port, "data_dir": data_dir}
    proc.terminate()
    proc.wait(timeout=5)


def test_health(relay):
    with urllib.request.urlopen(f"http://127.0.0.1:{relay['port']}/healthz") as resp:
        assert json.loads(resp.read())["ok"] is True


def test_server_discover(relay):
    _, resp = rpc(relay["port"], {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    result = resp["result"]
    assert "2026-07-28" in result["protocolVersions"]
    assert result["serverInfo"]["name"] == "autonomy-mcp-relay"
    assert result["resultType"] == "complete"


def test_legacy_initialize(relay):
    _, resp = rpc(relay["port"], {
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "clientInfo": {"name": "test", "version": "0"}},
    })
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_notification_gets_202(relay):
    status, body = rpc(relay["port"], {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert body is None


def test_tools_list(relay):
    _, resp = rpc(relay["port"], {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    result = resp["result"]
    names = [t["name"] for t in result["tools"]]
    assert names == ["hello", "search", "read", "tail", "sessions",
                     "crosstalk_log", "note", "crosstalk_send"]
    assert result["ttlMs"] > 0
    assert result["cacheScope"] == "private"
    for tool in result["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_header_mismatch_rejected(relay):
    _, resp = rpc(relay["port"],
                  {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                  headers={"Mcp-Method": "tools/call"})
    assert resp["error"]["code"] == -32020


def test_unauthed_call_is_tool_error(relay):
    resp = call_tool(relay["port"], "search", {"query": "anything"})
    assert resp["result"]["isError"] is True
    assert resp["result"]["structuredContent"]["error"] == "not_authorized"


def test_peer_lifecycle_and_scopes(relay):
    port, data_dir = relay["port"], relay["data_dir"]

    # 1. hello -> pending
    resp = call_tool(port, "hello", {"peer_name": "test-peer"})
    assert resp["result"]["structuredContent"]["status"] == "pending"

    # 2. operator approves read+send only
    out = gateway_cli(data_dir, "approve", "test-peer", "--scopes", "read,send")
    assert "Approved 'test-peer'" in out

    # 3. hello again -> token
    resp = call_tool(port, "hello", {"peer_name": "test-peer"})
    sc = resp["result"]["structuredContent"]
    assert sc["status"] == "approved"
    token = sc["peer_token"]
    assert len(token) == 48

    # 4. read-scope tool works
    resp = call_tool(port, "search", {"peer_token": token, "query": "tunnel"})
    assert resp["result"]["isError"] is False
    assert resp["result"]["structuredContent"]["results"][0]["source_id"] == "abc-123"

    # 5. send-scope tool works, message is attributed
    resp = call_tool(port, "crosstalk_send",
                     {"peer_token": token, "session": "auto-0808-230541", "message": "hi"})
    assert resp["result"]["isError"] is False

    # 6. write-scope tool denied
    resp = call_tool(port, "note", {"peer_token": token, "text": "should fail"})
    assert resp["result"]["isError"] is True
    assert resp["result"]["structuredContent"]["error"] == "scope_denied"

    # 7. extend scopes -> note now works
    gateway_cli(data_dir, "approve", "test-peer", "--scopes", "read,send,write")
    resp = call_tool(port, "note", {"peer_token": token, "text": "kept insight"})
    assert resp["result"]["isError"] is False

    # 8. revoke -> token dead
    gateway_cli(data_dir, "revoke", "test-peer")
    resp = call_tool(port, "search", {"peer_token": token, "query": "tunnel"})
    assert resp["result"]["isError"] is True


def test_bad_session_ident_rejected(relay):
    gateway_cli(relay["data_dir"], "approve", "ident-peer", "--scopes", "read,send")
    resp = call_tool(relay["port"], "hello", {"peer_name": "ident-peer"})
    token = resp["result"]["structuredContent"]["peer_token"]
    resp = call_tool(relay["port"], "crosstalk_send",
                     {"peer_token": token, "session": "--evil-flag", "message": "x"})
    assert resp["result"]["isError"] is True


def test_unknown_tool(relay):
    resp = call_tool(relay["port"], "does_not_exist", {})
    assert resp["error"]["code"] == -32602
