"""auto-8q7oe.5: the lease agent's request checks (the browser itself is proven on the node)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.browser_broker import lease_agent
from tools.browser_broker.lease_agent import (
    OPS, BadRequest, LeaseHandler, parse_target, secret_ok, validate_command, window_args,
)

SECRET = "s" * 48


def test_secret_check():
    assert secret_ok(SECRET, SECRET)
    assert not secret_ok(None, SECRET)
    assert not secret_ok("", SECRET)
    assert not secret_ok(SECRET[:-1], SECRET)
    assert not secret_ok("", "")  # an unset secret admits nobody


def test_operation_list_is_exact_and_runs_no_script():
    assert OPS == {"goto", "snapshot", "screenshot", "click", "fill", "type", "press",
                   "wait", "url", "title", "text", "download"}
    for op in ("eval", "evaluate", "script", "add_init_script", "cdp", "cookies",
               "cookies_summary", "tabs", "query", "fetch_url"):
        with pytest.raises(BadRequest):
            validate_command({"op": op, "args": {}})


@pytest.mark.parametrize("body", [
    None, [], {"op": "url", "extra": 1}, {"op": "url", "args": []},
    {"op": "goto", "args": {"url": "javascript:alert(1)"}},
    {"op": "goto", "args": {"url": "file:///etc/passwd"}},
    {"op": "goto", "args": {"url": "chrome://settings"}},
    {"op": "goto", "args": {"url": "https://a.example", "expression": "1"}},
    {"op": "click", "args": {}},
    {"op": "click", "args": {"target": {"ref": "e1", "css": "a"}}},
    {"op": "click", "args": {"target": {"ref": "x1"}}},
    {"op": "click", "args": {"target": {"js": "document.body"}}},
    {"op": "click", "args": {"target": {"role": "Button!", "name": "x"}}},
    {"op": "click", "args": {"target": {"css": "a"}, "timeout_ms": 600_000}},
    {"op": "click", "args": {"target": {"css": "a"}, "timeout_ms": True}},
    {"op": "fill", "args": {"target": {"label": "Email"}}},
    {"op": "press", "args": {"key": "Enter; rm"}},
    {"op": "wait", "args": {}},
    {"op": "wait", "args": {"text": "a", "ms": 5}},
    {"op": "wait", "args": {"ms": 0}},
    {"op": "screenshot", "args": {"full_page": "yes"}},
])
def test_malformed_commands_are_refused(body):
    with pytest.raises(BadRequest):
        validate_command(body)


def test_well_formed_commands_parse():
    assert validate_command({"op": "goto", "args": {"url": "https://example.com/"}}) == (
        "goto", {"url": "https://example.com/", "timeout_ms": 30_000})
    op, args = validate_command({"op": "fill", "args": {
        "target": {"role": "textbox", "name": "Email"}, "value": "", "timeout_ms": 5000}})
    assert (op, args["target"], args["value"], args["timeout_ms"]) == (
        "fill", ("role", "textbox", "Email"), "", 5000)
    assert validate_command({"op": "press", "args": {"key": "Control+A"}})[1]["key"] == "Control+A"
    assert validate_command({"op": "wait", "args": {"ms": 250}})[1]["ms"] == 250
    assert validate_command({"op": "text"})[1] == {"timeout_ms": 30_000}
    assert parse_target({"ref": "e12"}) == ("ref", "e12")
    assert parse_target({"css": "#main"}) == ("css", "#main")


def test_window_fills_the_screen():
    assert window_args("1920x1080x24") == ["--window-position=0,0", "--window-size=1920,1080"]
    assert window_args("") == []
    assert window_args("big") == []


class _FakeAgent:
    def __init__(self):
        self.submitted = []

    def health(self):
        return {"browser": True, "context": True, "page": False, "healthy": False}

    def submit(self, op, args, timeout_s):
        self.submitted.append(op)
        return {"ok": True, "result": op}

    def abort(self):
        return {"stopped": True, "running": False}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_agent, "EXPIRY_FILE", tmp_path / "expiry")
    agent = _FakeAgent()
    handler = type("H", (LeaseHandler,), {"agent": agent, "secret": SECRET})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", agent, tmp_path
    httpd.shutdown()


def _call(base, method, path, body=None, secret=SECRET):
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Lease-Secret"] = secret
    request = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"])
@pytest.mark.parametrize("method,path", [
    ("GET", "/health"), ("POST", "/command"), ("POST", "/abort"), ("POST", "/expiry"), ("GET", "/nope"),
])
def test_every_endpoint_requires_the_secret(server, secret, method, path):
    base, agent, _ = server
    status, body = _call(base, method, path, {"op": "url"} if method == "POST" else None, secret)
    assert (status, body) == (401, {"error": "unauthorized"})
    assert agent.submitted == []


def test_commands_health_and_expiry(server):
    base, agent, tmp_path = server
    assert _call(base, "POST", "/command", {"op": "title"}) == (200, {"ok": True, "result": "title"})
    assert _call(base, "POST", "/command", {"op": "eval", "args": {}})[0] == 400
    assert agent.submitted == ["title"]
    assert _call(base, "GET", "/health")[0] == 503
    assert _call(base, "POST", "/abort", {}) == (200, {"stopped": True, "running": False})
    assert _call(base, "POST", "/expiry", {"expires_at": 1_900_000_000}) == (
        200, {"expires_at": 1_900_000_000.0})
    assert float((tmp_path / "expiry").read_text()) == 1_900_000_000.0
    for bad in ("soon", -1, True, None):
        assert _call(base, "POST", "/expiry", {"expires_at": bad})[0] == 400


def test_seccomp_profile_adds_only_the_namespace_rule():
    profile = json.loads((Path(__file__).parents[1] / "image" / "seccomp-chrome.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    added = [r for r in profile["syscalls"] if "autonomy-browser" in r.get("comment", "")]
    assert added == [profile["syscalls"][-1]]
    assert (sorted(added[0]["names"]), added[0]["action"]) == (
        ["clone", "setns", "unshare"], "SCMP_ACT_ALLOW")
    assert "args" not in added[0] and "includes" not in added[0]
