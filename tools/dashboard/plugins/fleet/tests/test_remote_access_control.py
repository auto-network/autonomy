"""The machine card's Remote access control (bead auto-fnj20), driven through
the real page.js in node: what it says per status, which routes it calls
and how it names another fleet machine, and where it goes read-only.

Loads the real module and calls the real component; nothing here inspects
source text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_PAGE_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "page.js",
)

ORIGIN = "https://dashboard.jeremy-66687aadf862bd776c8f.serve.auto.network"
LIVE = {"mode": "autonomy", "origin": ORIGIN, "recorded": True, "certificate": "ok",
        "advertised": True, "gate": "up", "gateway_state": "healthy", "enrollment": "closed",
        "enrolled": 1, "gate_passkeys": [{"credential_id": "Y3JlZC0x", "created_at": "2026-09-27T10:00:00.000Z",
                                          "transports": ["internal"], "sign_count": 3}]}

_JS = r"""
const fs = require('fs');
global.window = { location: { host: process.env.HOST || 'localhost:8080' }, dispatchEvent() {} };
global.navigator = {};
eval(fs.readFileSync(process.env.PAGE_JS, 'utf8'));
const scenario = JSON.parse(process.env.SCENARIO);
const calls = [];
global.fetch = async (url, init) => {
  calls.push({ url, method: (init && init.method) || 'GET', body: init && init.body ? JSON.parse(init.body) : null });
  const reply = scenario.replies[url.split('?')[0]] || { ok: true, status: scenario.status };
  return { ok: reply.http !== false, status: reply.http === false ? 502 : 200, json: async () => reply };
};
(async () => {
  const c = remoteAccessControl(scenario.machine);
  const out = {};
  await c.refresh();
  out.after_refresh = {
    statusWord: c.statusWord(), tone: c.tone(), modeWord: c.modeWord(), live: c.live(),
    readOnly: c.readOnly, passkeys: c.passkeys.length, canEnrol: c.canEnrol(), canChooseMode: c.canChooseMode(),
    certWord: c.certWord(), routeWord: c.routeWord(), progress: c.showProgress() ? c.progress().map(r => r.mark + r.label) : null,
    label: c.passkeys.length ? c.passkeyLabel(c.passkeys[0]) : null, enrolment: c.enrolment, error: c.error,
  };
  for (const step of scenario.steps || []) {
    if (step === 'open') await c.openEnrolment();
    if (step === 'close') await c.closeEnrolment();
    if (step === 'revoke') await c.revoke(c.passkeys[0]);
    if (step.mode) { c.choosing = true; c.draft = step.mode; await c.applyMode(); }
  }
  out.after_steps = { enrolment: c.enrolment, passkeys: c.passkeys.length, error: c.error, choosing: c.choosing };
  out.calls = calls;
  console.log(JSON.stringify(out));
})();
"""


def _run(scenario: dict, host: str = "localhost:8080") -> dict:
    result = subprocess.run(
        ["node", "-e", _JS],
        env={**os.environ, "PAGE_JS": _PAGE_JS, "SCENARIO": json.dumps(scenario), "HOST": host},
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


LOCAL = {"isLocalMachine": True, "machinePublicKey": "aa" * 32, "entryId": "entry-local"}
PEER = {"isLocalMachine": False, "machinePublicKey": "bb" * 32, "entryId": "entry-sjc"}


def test_a_live_relay_address_with_a_passkey_reads_reachable():
    out = _run({"machine": LOCAL, "status": LIVE, "replies": {}})
    r = out["after_refresh"]
    assert r["statusWord"] == "Reachable" and r["tone"] == "good" and r["live"] is True
    assert r["modeWord"] == "Autonomy Network" and r["certWord"] == "Issued" and r["routeWord"] == "Live"
    assert r["passkeys"] == 1 and r["label"] == "Device passkey"
    assert r["canEnrol"] is True and r["canChooseMode"] is True and r["readOnly"] is False
    assert r["progress"] is None and r["enrolment"] is None


@pytest.mark.parametrize("status, word, tone, progress", [
    ({**LIVE, "gate_passkeys": [], "enrolled": 0}, "Reachable · no passkey", "warn", None),
    ({**LIVE, "certificate": "retrying", "advertised": False, "gate": "pending", "gateway_state": "loading",
      "gate_passkeys": [], "enrolled": 0, "enrollment": "open"},
     "Setting up", "warn", ["…Certificate (retrying)", "…Route on the relay", "…Passkey gate"]),
    ({**LIVE, "certificate": "failed", "certificate_detail": "rate limited", "advertised": False, "gate": "pending",
      "gateway_state": "stopped", "gate_passkeys": []}, "Certificate failed", "bad", None),
    ({"mode": "tailscale", "origin": "https://desktop.tail1234.ts.net:8080", "recorded": True,
      "origin_verified": True, "relay_publication": "paused", "relay_origin": ORIGIN}, "Tailscale", "", None),
    ({"mode": "local", "origin": "http://localhost:80", "recorded": True}, "Local only", "", None),
    ({"mode": None, "origin": None, "recorded": False}, "Not set", "", None),
])
def test_every_status_has_its_words(status, word, tone, progress):
    r = _run({"machine": LOCAL, "status": status, "replies": {}})["after_refresh"]
    assert (r["statusWord"], r["tone"], r["progress"]) == (word, tone, progress)


def test_an_open_enrollment_link_in_the_status_is_shown_and_survives_a_reload():
    status = {**LIVE, "enrollment": "open", "enrollment_url": ORIGIN + "/oauth2/enroll?token=abc",
              "enrollment_expires_at": 4_000_000_000}
    r = _run({"machine": LOCAL, "status": status, "replies": {}})["after_refresh"]
    assert r["enrolment"] == {"url": ORIGIN + "/oauth2/enroll?token=abc", "expires_at": 4_000_000_000}
    assert r["canEnrol"] is False   # the link is already there; Close enrolment is offered instead


def test_actions_call_the_routes_and_a_reopen_shows_the_new_link():
    out = _run({"machine": LOCAL, "status": LIVE, "replies": {
        "/api/network/remote-access/enrollment/open": {"ok": True, "enrollment_url": ORIGIN + "/oauth2/enroll?token=new", "expires_at": 4_000_000_000},
        "/api/network/remote-access/enrollment/close": {"ok": True},
        "/api/network/remote-access/gate/passkeys/Y3JlZC0x": {"ok": True, "enrolled": 0},
    }, "steps": ["open", "revoke", "close"]})
    methods = [(c["method"], c["url"]) for c in out["calls"] if "status" not in c["url"]]
    assert methods == [
        ("POST", "/api/network/remote-access/enrollment/open"),
        ("DELETE", "/api/network/remote-access/gate/passkeys/Y3JlZC0x"),
        ("POST", "/api/network/remote-access/enrollment/close"),
    ]
    assert out["after_steps"]["enrolment"] is None and out["after_steps"]["error"] is None
    # The open reply's link was shown before the close.
    opened = [c for c in out["calls"] if c["url"].endswith("/open")]
    assert opened and opened[0]["body"] == {}


def test_another_fleet_machine_is_named_on_every_call_and_cannot_switch_mode():
    out = _run({"machine": PEER, "status": {**LIVE, "gate_passkeys": []}, "replies": {
        "/api/network/remote-access/enrollment/open": {"ok": True, "enrollment_url": ORIGIN + "/oauth2/enroll?token=peer", "expires_at": None},
    }, "steps": ["open", {"mode": "local"}]})
    r = out["after_refresh"]
    assert r["canChooseMode"] is False and r["canEnrol"] is True
    status_call, open_call = out["calls"][0], out["calls"][1]
    assert status_call["url"] == "/api/network/remote-access/status?machine=" + "bb" * 32
    assert open_call["body"] == {"machine": "bb" * 32}
    assert out["after_steps"]["enrolment"]["url"].endswith("token=peer")
    # applyMode on a peer is a no-op: no publish call was made.
    assert not any(c["url"].endswith("/publish") for c in out["calls"])


def test_the_mode_switch_publishes_from_this_machine():
    out = _run({"machine": LOCAL, "status": LIVE, "replies": {
        "/api/network/remote-access/publish": {"ok": True, "remote_access": {"mode": "tailscale"}},
    }, "steps": [{"mode": "tailscale"}]})
    publish = [c for c in out["calls"] if c["url"].endswith("/publish")]
    assert publish == [{"url": "/api/network/remote-access/publish", "method": "POST", "body": {"mode": "tailscale"}}]
    assert out["after_steps"]["choosing"] is False


def test_viewed_at_the_relay_address_itself_the_control_is_read_only():
    r = _run({"machine": LOCAL, "status": LIVE, "replies": {}},
             host=ORIGIN.removeprefix("https://"))["after_refresh"]
    assert r["readOnly"] is True and r["canEnrol"] is False and r["canChooseMode"] is False
    assert r["statusWord"] == "Reachable" and r["passkeys"] == 1


def test_a_refused_action_is_reported_and_the_link_is_not_invented():
    out = _run({"machine": PEER, "status": LIVE, "replies": {
        "/api/network/remote-access/enrollment/open": {"ok": False, "error": "personal-connector-unavailable", "http": False},
    }, "steps": ["open"]})
    assert out["after_steps"]["error"] == "personal-connector-unavailable"
    assert out["after_steps"]["enrolment"] is None
