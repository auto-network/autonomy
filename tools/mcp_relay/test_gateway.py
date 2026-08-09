"""L1 tests for the MCP relay: spec plumbing, peer lifecycle, scope gating.

Runs the real server in a subprocess against a stubbed `graph` binary, so no
graph database is touched.
"""

import importlib.util
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

GATEWAY = Path(__file__).parent / "gateway.py"

# Load gateway.py as a module for in-process unit tests of pure helpers.
_spec = importlib.util.spec_from_file_location("gateway_mod", GATEWAY)
gateway_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gateway_mod)

_TUNNEL_XFCC = ("By=spiffe://unified.prod.svc-global-kube.applied.mia.openai.org"
                "/ns/tunnel-service/sa/tunnel-service;Hash=abc")


def test_extract_identity_from_meta():
    headers = {"X-Forwarded-Client-Cert": _TUNNEL_XFCC}
    params = {"_meta": {"openai/session": "v1/sess", "openai/subject": "v1/subj",
                        "openai/organization": "v1/org"}}
    idn = gateway_mod.extract_identity(headers, params)
    assert idn["openai_session"] == "v1/sess"
    assert idn["openai_subject"] == "v1/subj"
    assert idn["openai_org"] == "v1/org"
    assert idn["tunnel_verified"] is True
    assert gateway_mod.identity_trusted(idn) is True


def test_extract_identity_header_fallback():
    # no _meta -> fall back to X-Openai-* headers
    headers = {"X-Openai-Session": "v1/hs", "X-Openai-Subject": "v1/hu",
               "X-Forwarded-Client-Cert": _TUNNEL_XFCC}
    idn = gateway_mod.extract_identity(headers, {})
    assert idn["openai_session"] == "v1/hs"
    assert idn["openai_subject"] == "v1/hu"
    assert idn["tunnel_verified"] is True


def test_identity_untrusted_without_tunnel_cert(monkeypatch):
    monkeypatch.setattr(gateway_mod, "REQUIRE_TUNNEL_CERT", True)
    # a real session id but NO tunnel cert -> not trustable
    idn = gateway_mod.extract_identity({}, {"_meta": {"openai/session": "v1/x"}})
    assert idn["tunnel_verified"] is False
    assert gateway_mod.identity_trusted(idn) is False
    # unless the cert requirement is disabled for local testing
    monkeypatch.setattr(gateway_mod, "REQUIRE_TUNNEL_CERT", False)
    assert gateway_mod.identity_trusted(idn) is True


def test_identity_trusted_needs_a_session():
    monkeypatch_free = gateway_mod.extract_identity({"X-Forwarded-Client-Cert": _TUNNEL_XFCC}, {})
    assert monkeypatch_free["openai_session"] is None
    assert gateway_mod.identity_trusted(monkeypatch_free) is False


# ---- dashboard-mode authorization (stubbed dashboard) ----

_TRUSTED = {"openai_session": "v1/s", "openai_subject": "v1/u",
            "openai_org": "v1/o", "tunnel_verified": True}


def _poster(session_resp, crosstalk_resp=None):
    def poster(path, body):
        if "/session/" in path:       # resolve (hello) and status (per-request)
            return session_resp
        if "/crosstalk/" in path:
            return crosstalk_resp
        return None
    return poster


def test_authorize_untrusted_identity_denied():
    a = gateway_mod.authorize({"openai_session": None}, "search", {},
                              poster=_poster({"status": "approved"}))
    assert a["allowed"] is False


def test_authorize_pending_session_denied():
    a = gateway_mod.authorize(_TRUSTED, "search", {},
                              poster=_poster({"status": "pending"}))
    assert a["allowed"] is False and a["status"] == "pending"


def test_authorize_read_tool_allowed_and_scoped():
    a = gateway_mod.authorize(_TRUSTED, "search", {},
        poster=_poster({"status": "approved", "autonomy_org": "autonomy", "level": "read"}))
    assert a["allowed"] is True and a["org"] == "autonomy"


def test_authorize_write_requires_readwrite():
    read_only = _poster({"status": "approved", "autonomy_org": "autonomy", "level": "read"})
    assert gateway_mod.authorize(_TRUSTED, "note", {}, poster=read_only)["allowed"] is False
    rw = _poster({"status": "approved", "autonomy_org": "autonomy", "level": "readwrite"})
    assert gateway_mod.authorize(_TRUSTED, "note", {}, poster=rw)["allowed"] is True


def test_authorize_crosstalk_needs_target_grant():
    linked = {"status": "approved", "autonomy_org": "autonomy", "level": "readwrite"}
    # session linked but crosstalk to target not yet granted -> denied
    a = gateway_mod.authorize(_TRUSTED, "crosstalk_send", {"session": "auto-x"},
                              poster=_poster(linked, {"status": "pending"}))
    assert a["allowed"] is False and a["status"] == "pending"
    # target granted -> allowed
    a = gateway_mod.authorize(_TRUSTED, "crosstalk_send", {"session": "auto-x"},
                              poster=_poster(linked, {"status": "approved"}))
    assert a["allowed"] is True


def test_hello_dashboard_pending_is_not_error():
    res = gateway_mod.hello_dashboard(_TRUSTED, {"intent": "help"},
                                      poster=_poster({"status": "pending"}))
    assert res["is_error"] is False
    assert "approve" in res["text"].lower()


def test_hello_dashboard_untrusted_is_error():
    res = gateway_mod.hello_dashboard({"openai_session": None}, {},
                                      poster=_poster({"status": "pending"}))
    assert res["is_error"] is True


def test_hello_dashboard_requires_a_nonblank_intent():
    # the operator cannot approve without a reason -> reject before opening a popup
    res = gateway_mod.hello_dashboard(_TRUSTED, {"intent": "   "},
                                      poster=_poster({"status": "pending"}))
    assert res["is_error"] is True and "intent" in res["text"].lower()


def _xtalk_poster(session_status, resolve_status, status_seq):
    seq = list(status_seq)

    def poster(path, body):
        if path.endswith("/session/status"):
            return {"status": session_status}
        if path.endswith("/crosstalk/resolve"):
            return {"status": resolve_status, "approval_id": "x"}
        if path.endswith("/crosstalk/status"):
            return {"status": seq.pop(0)} if seq else {"status": "pending"}
        return None
    return poster


def test_crosstalk_send_holds_then_delivers_on_approve(monkeypatch):
    monkeypatch.setattr(gateway_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(gateway_mod, "_deliver_crosstalk",
                        lambda *a, **k: {"structured": {"delivered": True}, "text": "sent",
                                         "is_error": False})
    # linked; resolve returns pending; the operator then approves on the poll
    poster = _xtalk_poster("approved", "pending", ["pending", "approved"])
    res = gateway_mod.crosstalk_send_dashboard(
        _TRUSTED, {"session": "auto-x", "message": "hi"}, None, poster=poster)
    assert res["is_error"] is False and res["structured"]["delivered"] is True


def test_crosstalk_send_declined_never_delivers(monkeypatch):
    monkeypatch.setattr(gateway_mod.time, "sleep", lambda *_: None)
    delivered = {"n": 0}
    monkeypatch.setattr(gateway_mod, "_deliver_crosstalk",
                        lambda *a, **k: (delivered.__setitem__("n", delivered["n"] + 1),
                                         {"structured": {}, "text": "", "is_error": False})[1])
    poster = _xtalk_poster("approved", "pending", ["denied"])
    res = gateway_mod.crosstalk_send_dashboard(
        _TRUSTED, {"session": "auto-x", "message": "hi"}, None, poster=poster)
    assert res["is_error"] is True and "declined" in res["text"].lower()
    assert delivered["n"] == 0  # message never sent on decline


def test_crosstalk_send_requires_a_linked_session(monkeypatch):
    poster = _xtalk_poster("pending", "pending", [])  # session not linked
    res = gateway_mod.crosstalk_send_dashboard(
        _TRUSTED, {"session": "auto-x", "message": "hi"}, None, poster=poster)
    assert res["is_error"] is True and "hello" in res["text"].lower()


def test_stdio_transport_is_its_own_trust_boundary(monkeypatch):
    # Over stdio there is no TCP surface and no header to forge — the process
    # boundary is the trust boundary, so a session with no cert is still trusted.
    monkeypatch.setattr(gateway_mod, "REQUIRE_TUNNEL_CERT", True)
    idn = gateway_mod.extract_identity({}, {"_meta": {"openai/session": "v1/x"}})
    assert idn["tunnel_verified"] is False
    monkeypatch.setattr(gateway_mod, "STDIO_TRANSPORT", True)
    assert gateway_mod.identity_trusted(idn) is True
    monkeypatch.setattr(gateway_mod, "STDIO_TRANSPORT", False)
    assert gateway_mod.identity_trusted(idn) is False  # HTTP path, no cert -> untrusted


def test_sess_tag_never_leaks_the_raw_session():
    tag = gateway_mod.sess_tag({"openai_session": "v1/supersecretsession"})
    assert "supersecret" not in tag and len(tag) == 12


def test_stdio_loop_serves_tools_list(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--data-dir", str(tmp_path / "d"), "stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env={"PATH": "/usr/bin:/bin", "MCP_RELAY_REQUIRE_TUNNEL_CERT": "0"})
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        names = [t["name"] for t in resp["result"]["tools"]]
        assert "hello" in names and "note" in names
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5)

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
    # DiscoverResult required fields (2026-07-28): supportedVersions (NOT
    # protocolVersions), capabilities, cacheScope, resultType, ttlMs.
    assert "2026-07-28" in result["supportedVersions"]
    assert "protocolVersions" not in result
    assert result["cacheScope"] == "private"
    assert isinstance(result["ttlMs"], int)
    assert "tools" in result["capabilities"]
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "autonomy-mcp-relay"


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
