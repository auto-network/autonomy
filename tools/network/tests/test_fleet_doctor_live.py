"""fleet_doctor asks the RUNNING worker and probes serving end to end.

The vault is warm only inside the dashboard process; a diagnostic that reads
it from its own process says "cold" and is wrong. These sections never do:
they ask the worker over HTTP and say UNKNOWN when it cannot be asked.
"""
from __future__ import annotations

import time

from tools.network import fleet_doctor


def test_live_worker_unreachable_is_unknown_not_cold(monkeypatch, capsys):
    monkeypatch.setattr(fleet_doctor, "_api_get",
                        lambda base, path, token, timeout=8.0: (None, "URLError: refused"))
    report: dict = {}
    fleet_doctor.check_live_worker(report, "https://localhost:8080", None)
    out = capsys.readouterr().out
    assert "UNKNOWN" in out and "could not ask it" in out
    assert "cold" not in out.lower().replace("nothing below", "")
    assert report["live_worker"] == {"error": "URLError: refused"}


def test_live_worker_report_prints_what_the_worker_holds(monkeypatch, capsys):
    replies = {
        "/api/vault/status": {"pid": 4242, "audited_delegate_warm": True},
        "/api/vault/organizations": {
            "pid": 4242, "audited_delegate_warm": True,
            "personal_generation_keys_open": 1,
            "organizations": [
                {"org": "personal", "serve_cert": "ok",
                 "connector": {"reachable": True, "serving": True,
                               "tunnel": {"connected_since": time.time() - 30},
                               "fleet_runtime_configured": True, "boot_commit": "abc123def456",
                               "active_streams": 0}},
                {"org": "dynbench", "genesis_id": "g",
                 "generation_keys": {"recorded": 2, "open_in_worker": 0},
                 "organization_kem_key_held": False,
                 "delegate": {"status": "ready"},
                 "membership": {"capable": True, "members": 3, "in_member_set": True},
                 "org_sync": None,
                 "serve_cert": "ok",
                 "connector": {"reachable": True, "serving": False,
                               "tunnel": {"connected_since": None,
                                          "last_served_at": time.time() - 3600,
                                          "reconnect_attempts": 7,
                                          "last_disconnect": {"lived_s": None, "close_code": 1006,
                                                              "reason": "", "error": "ConnectionClosedError: 1006"},
                                          "next_retry_at": time.time() + 20},
                               "fleet_runtime_configured": True, "boot_commit": "abc123def456",
                               "active_streams": 0}},
            ],
        },
    }
    monkeypatch.setattr(fleet_doctor, "_api_get",
                        lambda base, path, token, timeout=8.0: (replies[path], None))
    report: dict = {}
    fleet_doctor.check_live_worker(report, "https://localhost:8080", "tok")
    out = capsys.readouterr().out
    assert "running worker pid: 4242" in out
    assert "WARM" in out
    assert "dynbench: organization generation keys: 0/2 open in the worker" in out
    assert "[WARN] dynbench: organization KEM key held in the worker: False" in out
    assert "[WARN] dynbench: org sync channel: NOT held: no fleet:sync certificate installed in the worker" in out
    assert "[FAIL] dynbench: connector: NOT serving; DOWN; last served 1.0h ago; " \
           "7 failed reconnect(s) since; last disconnect never served, close_code=1006" in out
    assert "[ok  ] personal: connector: SERVING; connected since 30s ago" in out
    assert report["live_worker"]["organizations"][1]["org"] == "dynbench"


def test_serving_liveness_names_the_failing_scope_and_reason(monkeypatch, capsys):
    from tools.dashboard import link_serving_supervisor as sup

    monkeypatch.setattr(sup, "_discover_startup_orgs", lambda: ["personal", "dynbench", "quiet"])
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, now=None: {"status": "missing" if org == "quiet" else "ok"})
    grants = {"personal": [{"token": "aa" * 16, "channel_pub": "pp", "target_type": "note"}],
              "dynbench": [{"token": "bb" * 16, "channel_pub": "qq", "target_type": "note"},
                           {"token": "cc" * 16, "channel_pub": "rr", "target_type": "note"}]}
    monkeypatch.setattr(fleet_doctor, "_probe_candidates", lambda scope, now: grants.get(scope, []))
    monkeypatch.setattr("tools.dashboard.link_approvals._load_binding",
                        lambda org: ({"registry_url": "https://registry.example", "org_uuid": "u-" + org}, None))

    async def probe_link(*, relay_url, token, link_pub, org_uuid, operation):
        if token.startswith("bb"):
            return {"live": False, "status": None,
                    "detail": "the serving tunnel did not complete the probe in time"}
        return {"live": True, "status": 200, "detail": "the link serves"}
    monkeypatch.setattr("tools.dashboard.link_probe.probe_link", probe_link)
    monkeypatch.setattr(sup, "control", lambda org, op, args, timeout=12.0: {
        "ok": True, "serving": False,
        "tunnel": {"connected_since": None, "last_served_at": time.time() - 7200,
                   "reconnect_attempts": 12,
                   "last_disconnect": {"lived_s": None, "close_code": 1006, "reason": "",
                                       "error": "ConnectionClosedError: 1006"},
                   "next_retry_at": None}})
    report: dict = {}
    fleet_doctor.check_serving_liveness(report)
    out = capsys.readouterr().out
    assert "[ok  ] personal: serving liveness: LIVE (link aaaaaaaa..., 1 live grant(s), status=200)" in out
    assert "[FAIL] dynbench: serving liveness: NOT LIVE (link bbbbbbbb..., 2 live grant(s), status=None): " \
           "the serving tunnel did not complete the probe in time" in out
    assert "[WARN] dynbench: connector tunnel: DOWN; last served 2.0h ago; 12 failed reconnect(s) since; " \
           "last disconnect never served, close_code=1006, ConnectionClosedError: 1006" in out
    assert "quiet" not in out                      # never provisioned: silent
    assert report["serving_liveness"]["dynbench"]["live"] is False
    assert report["serving_liveness"]["personal"]["token_prefix"] == "aaaaaaaa"


def test_tunnel_summary_wording():
    assert fleet_doctor._tunnel_summary(None) == "tunnel state not reported (connector predates it)"
    assert fleet_doctor._tunnel_summary({"connected_since": time.time() - 5}).startswith("connected since")
    text = fleet_doctor._tunnel_summary({"connected_since": None, "last_served_at": None,
                                         "reconnect_attempts": 0, "last_disconnect": None,
                                         "next_retry_at": None})
    assert text == "DOWN; last served never"


def test_tunnel_summary_names_a_failed_viewer_channel():
    text = fleet_doctor._tunnel_summary({
        "connected_since": time.time() - 120, "channel_failures": 3,
        "last_channel_failure": {"at": time.time() - 10, "token_prefix": "abababab",
                                 "close_code": 4502,
                                 "error": "PermissionError: link key resolution refused"},
    })
    assert text.startswith("connected since 2m ago; 3 viewer channel(s) failed, last 10s ago "
                           "on link abababab... (close 4502): PermissionError: link key resolution refused")
