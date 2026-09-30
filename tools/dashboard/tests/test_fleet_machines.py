"""Machine chooser and remote Active cards, backend (bead auto-mje3g):
resource sampling, the launch targets list, remote session rows and the
viewer's explicit unreachable signal."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import (
    fleet_machines,
    machine_resources,
    remote_view,
    server,
    session_presence,
)
from tools.dashboard import session_control_client as scc
from tools.dashboard.dao import dashboard_db

HOME = session_presence.LocalMachine("a1" * 32, "a2" * 32)
SJC = "b1" * 32
ROSTER = {HOME.machine_pub: "a2" * 32, SJC: "b2" * 32}
NAMES = {"a2" * 32: "home", "b2" * 32: "sjc-2"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    fleet_machines._cache.clear()
    fleet_machines._inflight.clear()
    yield
    fleet_machines._cache.clear()


# ── sampling ────────────────────────────────────────────────────────────────


def _meminfo(tmp_path, total_kb, avail_kb):
    p = tmp_path / "meminfo"
    p.write_text(f"MemTotal: {total_kb} kB\nMemFree: 1 kB\nMemAvailable: {avail_kb} kB\n")
    return p


def test_sample_reads_meminfo_disk_and_load(tmp_path):
    out = machine_resources.sample(
        tmp_path, meminfo=_meminfo(tmp_path, 32 * 1024 * 1024, 12 * 1024 * 1024),
        cgroup=tmp_path / "no-cgroup")
    assert out["ram_total_gb"] == 32.0 and out["ram_free_gb"] == 12.0
    assert out["ram_limited"] is False
    assert out["disk_total_gb"] > 0 and out["cpus"] >= 1
    assert isinstance(out["load_1m"], float)


def test_a_cgroup_memory_limit_is_the_total(tmp_path):
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.max").write_text(str(8 * 1024 ** 3))
    (cg / "memory.current").write_text(str(3 * 1024 ** 3))
    out = machine_resources.sample(
        tmp_path, meminfo=_meminfo(tmp_path, 64 * 1024 * 1024, 40 * 1024 * 1024), cgroup=cg)
    assert (out["ram_total_gb"], out["ram_free_gb"], out["ram_limited"]) == (8.0, 5.0, True)


def test_reclaimable_file_cache_counts_as_free_under_a_limit(tmp_path):
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.max").write_text(str(8 * 1024 ** 3))
    (cg / "memory.current").write_text(str(6 * 1024 ** 3))
    (cg / "memory.stat").write_text(f"anon 1\ninactive_file {2 * 1024 ** 3}\nactive_file 5\n")
    out = machine_resources.sample(
        tmp_path, meminfo=_meminfo(tmp_path, 64 * 1024 * 1024, 40 * 1024 * 1024), cgroup=cg)
    assert out["ram_free_gb"] == 4.0


def test_an_unlimited_cgroup_leaves_meminfo(tmp_path):
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.max").write_text("max")
    (cg / "memory.current").write_text("1")
    out = machine_resources.sample(
        tmp_path, meminfo=_meminfo(tmp_path, 16 * 1024 * 1024, 4 * 1024 * 1024), cgroup=cg)
    assert out["ram_total_gb"] == 16.0 and out["ram_limited"] is False


# ── the ops ─────────────────────────────────────────────────────────────────


def test_status_carries_live_sessions_and_resources(monkeypatch):
    monkeypatch.setattr(session_presence, "local_machine", lambda: HOME)
    monkeypatch.setattr(session_presence, "_machine_names", lambda: NAMES)
    monkeypatch.setattr(dashboard_db, "get_live_sessions", lambda: [{}, {}, {}])
    monkeypatch.setattr(machine_resources, "sample", lambda: {"ram_free_gb": 1.5})
    reply = asyncio.run(scc.status_op(lambda: {})({}, SJC))
    assert reply["result"]["live_sessions"] == 3
    assert reply["result"]["resources"] == {"ram_free_gb": 1.5}


def test_the_subscription_ships_only_allow_listed_card_fields():
    from tools.dashboard import remote_sessions

    rows = [{"session_id": "auto-1", "label": "x", "topics": ["t"],
             "harness_token": "org-uuid", "harness_token_alias": "work",
             "jsonl_path": "/data/x.jsonl", "lifecycle_detail": "boom",
             "brand_new_column": "secret"}]
    assert remote_sessions._project("session:registry", rows) == [
        {"session_id": "auto-1", "label": "x", "topics": ["t"]}]


# ── Home: launch targets and remote rows ────────────────────────────────────


@pytest.fixture
def home(monkeypatch):
    monkeypatch.setattr(fleet_machines, "_context",
                        lambda: (HOME, ROSTER, NAMES, {SJC: 1_800_000_000}))
    monkeypatch.setattr(dashboard_db, "get_live_sessions", lambda: [{}, {}])
    monkeypatch.setattr(machine_resources, "sample",
                        lambda: {"ram_free_gb": 11.6, "ram_total_gb": 31.2,
                                 "disk_free_gb": 184.0, "load_1m": 3.1, "cpus": 16})
    from types import SimpleNamespace

    state = SimpleNamespace(calls=[], replies={})

    async def fake_request(machine, op, body=None, *, timeout=15.0, stream=False):
        state.calls.append((machine, op))
        return state.replies[op]

    monkeypatch.setattr(scc, "request", fake_request)
    return state


def test_launch_targets_put_this_machine_first_and_ask_the_rest(home):
    home.replies = {"status": {"v": 1, "ok": True, "result": {
        "live_sessions": 2, "resources": {"ram_free_gb": 47.3, "disk_free_gb": 812.0,
                                          "load_1m": 1.4, "cpus": 32}}}}
    targets = asyncio.run(fleet_machines.launch_targets())
    assert [t["label"] for t in targets] == ["home", "sjc-2"]
    assert targets[0]["local"] is True and targets[0]["live_sessions"] == 2
    assert targets[0]["ram_free_gb"] == 11.6
    assert targets[1] == {"machine_pub": SJC, "label": "sjc-2", "local": False,
                          "reachable": True, "state": "reachable", "unreachable_since": None,
                          "live_sessions": 2, "ram_free_gb": 47.3,
                          "disk_free_gb": 812.0, "load_1m": 1.4, "cpus": 32}
    assert home.calls == [(SJC, "status")]


def test_an_unanswering_machine_is_unreachable_since_its_last_pull(home):
    home.replies = {"status": {"v": 1, "ok": False,
                                            "refusal": "destination-slot-absent"}}
    targets = asyncio.run(fleet_machines.launch_targets())
    assert targets[1]["reachable"] is False and targets[1]["state"] == "unreachable"
    assert targets[1]["unreachable_since"] == 1_800_000_000
    assert targets[1]["refusal"] == "destination-slot-absent"


@pytest.mark.parametrize("refusal, at", [
    ("session-control-not-negotiated", "local"),
    ("session-control-not-granted", "local"),
    ("session-control-not-granted", "peer"),
    ("peer-scope-missing", "peer"),
    ("own-delegation-expired", "local"),
])
def test_a_missing_capability_or_grant_is_not_enabled_not_unreachable(home, refusal, at):
    home.replies = {"status": {"v": 1, "ok": False, "refusal": refusal,
                               "detail": "why", "at": at}}
    targets = asyncio.run(fleet_machines.launch_targets())
    # The code, its detail and where it was decided all reach the chooser.
    assert (targets[1]["state"], targets[1]["reason"], targets[1]["detail"],
            targets[1]["at"]) == ("not_enabled", refusal, "why", at)


@pytest.mark.parametrize("refusal", [
    "destination-slot-absent", "slot-lookup-failed", "handshake-timeout",
    "reply-timeout", "peer-closed-in-handshake", "connector-call-failed",
])
def test_a_machine_that_cannot_be_reached_is_unreachable(home, refusal):
    home.replies = {"status": {"v": 1, "ok": False, "refusal": refusal}}
    targets = asyncio.run(fleet_machines.launch_targets())
    assert (targets[1]["state"], targets[1]["reason"]) == ("unreachable", refusal)


def test_concurrent_and_repeated_asks_share_one_request(home):
    home.replies = {"status": {"v": 1, "ok": True, "result": {"live_sessions": 1}}}

    async def run():
        await asyncio.gather(fleet_machines.launch_targets(), fleet_machines.launch_targets())
        await fleet_machines.launch_targets()

    asyncio.run(run())
    assert home.calls == [(SJC, "status")]


def test_no_other_machine_means_only_this_one(monkeypatch):
    monkeypatch.setattr(fleet_machines, "_context",
                        lambda: (HOME, {HOME.machine_pub: "a2" * 32}, NAMES, {}))
    monkeypatch.setattr(dashboard_db, "get_live_sessions", lambda: [])
    monkeypatch.setattr(machine_resources, "sample", lambda: {})
    targets = asyncio.run(fleet_machines.launch_targets())
    assert [t["label"] for t in targets] == ["home"]


# ── the viewer's unreachable signal ─────────────────────────────────────────


class _Req:
    def __init__(self, path_params, query=None):
        self.path_params = path_params
        self.query_params = query or {}


def test_an_unreachable_machine_yields_an_explicit_viewer_state(monkeypatch):
    from tools.dashboard import api_auth

    async def down(machine, name, project, query, *, timeout=20.0):
        return {"v": 1, "ok": False, "refusal": "destination-slot-absent"}

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    monkeypatch.setattr(remote_view, "fetch_tail", down)
    monkeypatch.setattr(remote_view, "unreachable_since", lambda m: 1_800_000_000)
    response = asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": "auto-9@sjc-2"}, {"tail_entries": "100"})))
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["entries"] == [] and data["is_live"] is False
    assert data["machine_unreachable"] == {"machine": "sjc-2", "since": 1_800_000_000}


def test_one_timed_out_request_is_a_timeout_not_an_unreachable_machine(monkeypatch):
    """A reply that ran out of time said "unreachable" with 0 entries; the
    viewer then hid everything it showed (remote-sessions item 3)."""
    from tools.dashboard import api_auth

    async def slow(machine, name, project, query, *, timeout=20.0):
        return {"v": 1, "ok": False, "refusal": "reply-timeout",
                "detail": "no reply within 20.0s"}

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    monkeypatch.setattr(remote_view, "fetch_tail", slow)
    response = asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": "auto-9@sjc-2"}, {"tail_entries": "100"})))
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["machine_timed_out"] == {"machine": "sjc-2", "reason": "reply-timeout",
                                         "detail": "no reply within 20.0s"}
    assert "machine_unreachable" not in data
    assert data["machine_reachable"] is True
    assert "is_live" not in data        # says nothing about the session


def test_which_refusals_are_timeouts():
    for code in ("reply-timeout", "handshake-timeout", "stream-chunk-timeout",
                 "transfer-deadline-exceeded"):
        assert remote_view.refusal_state(code) == "timeout"
    # this machine's own control call to its connector ran out of time
    assert remote_view.refusal_state(
        "connector-call-failed", "TimeoutError: timed out") == "timeout"
    assert remote_view.refusal_state(
        "connector-call-failed", "ConnectionRefusedError: no listener") == "unreachable"
    assert remote_view.refusal_state("destination-slot-absent") == "unreachable"
    assert remote_view.refusal_state("session-control-not-granted") == "not_enabled"


def test_a_missing_session_on_a_reachable_machine_is_still_404(monkeypatch):
    from tools.dashboard import api_auth

    async def missing(machine, name, project, query, *, timeout=20.0):
        return {"v": 1, "ok": False, "refusal": "no-such-session"}

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    monkeypatch.setattr(remote_view, "fetch_tail", missing)
    response = asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": "auto-9@sjc-2"})))
    assert response.status_code == 404



def test_a_machine_without_remote_sessions_enabled_says_so_in_the_viewer(monkeypatch):
    from tools.dashboard import api_auth

    async def not_enabled(machine, name, project, query, *, timeout=20.0):
        return {"v": 1, "ok": False, "refusal": "session-control-not-granted",
                "detail": "scope", "at": "peer"}

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda r: None)
    monkeypatch.setattr(remote_view, "fetch_tail", not_enabled)
    data = json.loads(asyncio.run(server.api_session_tail(_Req(
        {"project": "p", "session_id": "auto-9@sjc-2"}))).body)
    assert data["machine_not_enabled"] == {
        "machine": "sjc-2", "reason": "session-control-not-granted",
        "detail": "scope", "at": "peer"}
    assert "machine_unreachable" not in data
