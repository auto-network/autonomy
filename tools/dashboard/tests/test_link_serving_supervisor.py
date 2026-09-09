"""Unit coverage for the serving supervisor's reconciliation logic.

Drives the real ``serve_cert_state`` / grant / binding reads against tmp
stores and real minted certs + key files, but with a FAKE spawn so the
start/stop/idempotence/watchdog decisions are asserted without real
subprocesses. The genuine subprocess-brings-serving-live path is proven
separately in ``test_link_serving_tunnel.py``.

Pinned behaviors (the operator's rule — run iff a valid cert AND a live
grant):

* launches once when a cert + live grant are present, and never double-spawns;
* does not launch with no live grant, an expired cert, a missing key file, or
  a key that does not match the cert;
* stops a running connector when its last grant is revoked;
* the watchdog reconcile relaunches a connector whose process has died.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.dashboard import link_serving_supervisor as sup

_REPO_ROOT = Path(__file__).resolve().parents[3]
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_SERVE_CERT_REVISION,
    NETWORK_SERVE_CERT_SET_ID,
)
from tools.graph.schemas.namespace_reservation import (
    NAMESPACE_RESERVATION_REVISION,
    NAMESPACE_RESERVATION_SET_ID,
)
from tools.graph.schemas.service_target import (
    SERVICE_TARGET_REVISION,
    SERVICE_TARGET_SET_ID,
)

ORG = "netorg"
ORG_UUID = "2d4b90cb-0000-4000-8000-000000000000"
ISO = "%Y-%m-%dT%H:%M:%SZ"


class FakeProc:
    def __init__(self):
        self._alive = True
        self._serving = True

    def pid(self):
        return None  # not a real /proc process; never a reap target

    def alive(self):
        return self._alive

    def stop(self):
        self._alive = False

    def serving(self):
        return self._alive and self._serving

    def disconnect(self):
        self._serving = False

    def connect(self):
        self._serving = True

    def die(self):  # simulate a crash for the watchdog test
        self._alive = False


class FakeSpawn:
    def __init__(self):
        self.calls = []
        self.procs = []

    def __call__(self, argv, env, *, log_path=None, ctl_path=None):
        self.calls.append({"argv": argv, "env": env, "log_path": log_path,
                           "ctl_path": ctl_path})
        p = FakeProc()
        self.procs.append(p)
        return p


def test_default_spawn_captures_connector_warnings_in_shared_log(tmp_path):
    """The real supervisor spawn path preserves connector WARNING output."""
    log_path = tmp_path / "serve.log"
    marker = "bounded connector failure marker"
    proc = sup._default_spawn(
        [
            sys.executable,
            "-c",
            f"import logging; logging.warning({marker!r})",
        ],
        dict(os.environ),
        log_path=str(log_path),
    )
    deadline = time.time() + 5
    while proc.alive() and time.time() < deadline:
        time.sleep(0.01)
    assert proc.alive() is False
    assert marker in log_path.read_text()


def test_default_spawn_forces_unbuffered_connector_output(tmp_path):
    log_path = tmp_path / "serve.log"
    proc = sup._default_spawn(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('ready'); time.sleep(0.5)",
        ],
        dict(os.environ),
        log_path=str(log_path),
    )
    deadline = time.time() + 0.4
    while time.time() < deadline and log_path.read_bytes() != b"ready":
        time.sleep(0.01)
    try:
        assert log_path.read_bytes() == b"ready"
    finally:
        proc.stop()


def test_connector_survives_its_parent_death(tmp_path):
    """INVERTED CONTRACT (2026-09-06): a spawned connector must SURVIVE its
    dashboard's death.

    The old PR_SET_PDEATHSIG tie meant every hot reload (fired by every code
    merge) SIGTERMed the serving connector mid-stream — a fleet member pulling
    a checkpoint lost its transfer on every merge and re-pulled from scratch,
    forever. The connector now detaches (start_new_session); orphan protection
    is the successor supervisor's job: adopt a healthy incumbent
    (_adopt_incumbent), reap only the genuinely sick (_reap_strays).
    """
    parent_src = (
        "import os, sys, time\n"
        "from tools.dashboard import link_serving_supervisor as sup\n"
        "proc = sup._default_spawn(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    dict(os.environ))\n"
        "print(proc.pid(), flush=True)\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_src],
        stdout=subprocess.PIPE, cwd=_REPO_ROOT, env=dict(os.environ),
    )
    try:
        child_pid = int(parent.stdout.readline().decode().strip())
        assert _alive(child_pid)
        parent.kill()  # SIGKILL: like a hot reload, no shutdown hook runs
        parent.wait(timeout=10)
        time.sleep(1.0)
        assert _alive(child_pid), (
            "connector died with its parent — a hot reload would sever every "
            "in-flight fleet-sync stream again"
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            parent.kill()
        with contextlib.suppress(Exception):
            os.kill(child_pid, signal.SIGKILL)


def test_launch_adopts_a_healthy_incumbent_instead_of_reaping(env, monkeypatch):
    """A serving connector from a previous dashboard incarnation is adopted —
    recorded as owned, streams preserved — never killed and relaunched."""
    from tools.dashboard import link_serving_supervisor as sup

    _provision_serve_cert(env)
    state = sup.serve_cert_state(ORG)
    assert state["status"] == "ok", state
    supervisor = sup.ServingSupervisor(spawn=_refusing_spawn)
    incumbent_pid = os.getpid() + 100000  # sentinel; alive() not exercised
    from tools.network import build_version
    monkeypatch.setattr(
        sup, "_probe_ctl_status",
        lambda ctl: {"ok": True, "serving": True,
                     "boot_commit": build_version.disk_head()},
    )
    monkeypatch.setattr(
        sup, "_iter_connector_pids", lambda org_uuid: iter([incumbent_pid])
    )
    reaped = []
    monkeypatch.setattr(
        supervisor, "_reap_strays", lambda org: reaped.append(org)
    )
    result = supervisor._launch(ORG, state)
    assert result == {"running": True, "reason": "adopted"}
    assert reaped == [], "adoption must preclude the reap"
    handle = supervisor._procs[ORG]
    assert handle.pid() == incumbent_pid
    assert supervisor._credentials[ORG] == (
        state["cert"], state["viewer_cert"], state["key_path"])


def _refusing_spawn(*args, **kwargs):
    raise AssertionError("spawn must not run when an incumbent is adopted")


def test_stale_incumbent_is_replaced_not_adopted(env, monkeypatch):
    """A serving incumbent on an old code generation (boot_commit != disk head,
    or absent = pre-gate connector) must be REPLACED: observed live 2026-09-06,
    a pre-fix connector crashed every fleet stream while answering
    serving=True, and adoption immortalised it."""
    from tools.dashboard import link_serving_supervisor as sup

    _provision_serve_cert(env)
    state = sup.serve_cert_state(ORG)
    assert state["status"] == "ok", state
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    monkeypatch.setattr(
        sup, "_probe_ctl_status",
        lambda ctl: {"ok": True, "serving": True, "boot_commit": "0" * 40},
    )
    reaped = []
    monkeypatch.setattr(
        supervisor, "_reap_strays", lambda org: reaped.append(org)
    )
    result = supervisor._launch(ORG, state)
    assert result == {"running": True, "reason": "launched"}
    assert reaped == [ORG], "stale incumbent must fall through to the reap"
    assert len(spawn.calls) == 1, "a fresh connector must replace it"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: the code under test
    # resolves explicit orgs, which a pin silently swallows (73bad14e)
    # and the fail-loud resolver refuses. delenv guards ambient leaks.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(ORG).close()
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield tmp_path
    GraphDB.close_all_pooled()


def _provision_serve_cert(tmp_path, *, ttl=30 * 24 * 3600) -> dict:
    """Mint a real root-signed tunnel:serve delegate, write its key file
    (0600), and store the serve-cert row + a matching binding."""
    from tools.network.idkit import KeyPair, Subject, issue_cert

    root = KeyPair.generate()
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 300, not_after=now + ttl,
    )
    viewer_cert = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", delegate.public_hex),
        not_before=cert.not_before, not_after=cert.not_after,
    )
    dns01_cert = issue_cert(
        root, delegate.public_hex, scope=("serve:dns-01",), org=ORG_UUID,
        subject=cert.subject,
        not_before=cert.not_before, not_after=cert.not_after,
    )
    keydir = tmp_path / "network"
    keydir.mkdir(exist_ok=True)
    key_path = keydir / "serve.key"
    key_path.write_text(delegate.private_hex)
    os.chmod(key_path, 0o600)

    settings_ops.add_setting(
        NETWORK_SERVE_CERT_SET_ID, NETWORK_SERVE_CERT_REVISION, "default",
        {
            "cert": cert.to_json().decode("ascii"),
            "viewer_cert": viewer_cert.to_json().decode("ascii"),
            "dns01_cert": dns01_cert.to_json().decode("ascii"),
            "key_path": str(key_path),
            "root_pub": root.public_hex,
            "not_after": cert.not_after,
        },
        org=ORG,
    )
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "auto.network",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": "https://auto.network",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": time.strftime(ISO, time.gmtime(now + ttl)),
        },
        org=ORG,
    )
    return {"root": root, "delegate": delegate, "cert": cert,
            "viewer_cert": viewer_cert, "dns01_cert": dns01_cert,
            "key_path": key_path}


def _put_grant(token="a" * 32, *, meta=None, issued_at=None):
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": ORG_UUID,  # any uuid; supervisor only checks validity
            "target_type": "present",
            "meta": meta or {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": issued_at or time.strftime(ISO, time.gmtime()),
        },
        org=ORG,
    )


def _replace_key_path(provisioned: dict, key_path: str) -> None:
    settings_ops.upsert_by_key(
        NETWORK_SERVE_CERT_SET_ID,
        NETWORK_SERVE_CERT_REVISION,
        "default",
        {
            "cert": provisioned["cert"].to_json().decode("ascii"),
            "viewer_cert": provisioned["viewer_cert"].to_json().decode("ascii"),
            "key_path": key_path,
            "root_pub": provisioned["root"].public_hex,
            "not_after": provisioned["cert"].not_after,
        },
        org=ORG,
    )


def _drop_grant(token="a" * 32):
    for m in settings_ops.read_owned_set(NETWORK_LINK_GRANT_SET_ID, org=ORG).members:
        if m.key == token:
            settings_ops.remove_setting(m.id, org=ORG)


def _put_service_publication(*, state="active"):
    reservation_id = "11111111-1111-5111-8111-111111111111"
    now = "2026-09-02T20:00:00.000Z"
    persona_pub = "ab" * 32
    reservation = {
        "persona_pub": persona_pub,
        "persona_label": "persona-" + hashlib.sha256(
            bytes.fromhex(persona_pub)
        ).hexdigest()[:20],
        "app_label": "service",
        "state": state,
        "created_at": now,
        "updated_at": now,
    }
    if state == "released":
        reservation["released_at"] = now
    settings_ops.add_setting(
        NAMESPACE_RESERVATION_SET_ID,
        NAMESPACE_RESERVATION_REVISION,
        reservation_id,
        reservation,
        org=ORG,
    )
    settings_ops.add_setting(
        SERVICE_TARGET_SET_ID,
        SERVICE_TARGET_REVISION,
        reservation_id,
        {
            "machine_id": "cd" * 32,
            "session_id": "auto-test",
            "container_id": "ef" * 32,
            "port": 8000,
            "created_at": now,
            "updated_at": now,
        },
        org=ORG,
    )
    return reservation_id


# ── the run condition ─────────────────────────────────────────


def test_launches_once_with_cert_and_live_grant(env):
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)

    assert s.ensure(ORG) == {"running": True, "reason": "launched"}
    assert len(spawn.calls) == 1
    # idempotent: a second ensure with the proc alive does not respawn
    assert s.ensure(ORG) == {"running": True, "reason": "already-running"}
    assert len(spawn.calls) == 1

    argv = spawn.calls[0]["argv"]
    assert "--relay" in argv and "wss://auto.network" in argv
    assert "--org" in argv and ORG_UUID in argv
    assert "--graph-org" in argv and ORG in argv


def test_no_launch_without_live_grant(env):
    _provision_serve_cert(env)  # cert present, but no grant cached
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG) == {"running": False, "reason": "no-live-grants"}
    assert spawn.calls == []


@pytest.mark.parametrize("state", ["active", "paused"])
def test_launches_for_bound_service_without_artifact_grant(env, state):
    _provision_serve_cert(env)
    _put_service_publication(state=state)
    spawn = FakeSpawn()

    assert sup.ServingSupervisor(spawn=spawn).ensure(ORG) == {
        "running": True,
        "reason": "launched",
    }
    assert len(spawn.calls) == 1


def test_unbound_or_released_service_does_not_keep_connector_alive(env):
    _provision_serve_cert(env)
    reservation_id = _put_service_publication(state="released")
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)

    assert supervisor.ensure(ORG) == {
        "running": False,
        "reason": "no-live-grants",
    }
    assert spawn.calls == []


def test_no_launch_when_cert_missing(env):
    _put_grant()  # a live grant but no serve-cert provisioned
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG) == {"running": False, "reason": "missing"}
    assert spawn.calls == []


def test_no_launch_when_cert_expired(env):
    _provision_serve_cert(env, ttl=-10)  # already expired
    _put_grant()
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG) == {"running": False, "reason": "expired"}
    assert spawn.calls == []


def test_no_launch_when_key_file_missing(env):
    prov = _provision_serve_cert(env)
    _put_grant()
    os.remove(prov["key_path"])  # row points at a gone key file
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG) == {"running": False, "reason": "key-missing"}
    assert spawn.calls == []


def test_no_launch_when_key_does_not_match_cert(env):
    from tools.network.idkit import KeyPair
    prov = _provision_serve_cert(env)
    _put_grant()
    prov["key_path"].write_text(KeyPair.generate().private_hex)  # wrong key
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    out = s.ensure(ORG)
    assert out["running"] is False
    assert "does not match" in out["reason"]
    assert spawn.calls == []


def test_stops_when_last_grant_revoked(env):
    _provision_serve_cert(env)
    _put_grant()
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.ensure(ORG)["running"] is True
    proc = spawn.procs[0]

    _drop_grant()  # what link_revoke does to the cache
    clock[0] += 60  # past the fresh-tunnel grace, so the watchdog reaps it
    assert s.ensure(ORG) == {"running": False, "reason": "no-live-grants"}
    assert proc.alive() is False  # the connector was stopped


# ── reaping leaked / orphaned connectors ──────────────────────


def _spawn_fake_connector(org_uuid: str):
    """A harmless stand-in for a leaked serving connector: a sleeper whose
    /proc cmdline carries the exact tokens the reaper matches on."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)",
         sup._CONNECTOR_MODULE, "--org", org_uuid],
    )


def _await_visible(pid: int, org_uuid: str) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if pid in set(sup._iter_connector_pids(org_uuid)):
            return
        time.sleep(0.02)
    raise AssertionError(f"connector pid={pid} never appeared in /proc scan")


def test_iter_connector_pids_matches_by_org(env):
    proc = _spawn_fake_connector(ORG_UUID)
    try:
        _await_visible(proc.pid, ORG_UUID)
        assert proc.pid in set(sup._iter_connector_pids(ORG_UUID))
        # a connector for a different org is not this org's stray
        assert proc.pid not in set(sup._iter_connector_pids("99" * 16))
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reap_strays_terminates_an_unowned_connector(env):
    _provision_serve_cert(env)  # provisions the binding → org_uuid
    proc = _spawn_fake_connector(ORG_UUID)
    s = sup.ServingSupervisor(spawn=FakeSpawn())
    try:
        _await_visible(proc.pid, ORG_UUID)
        s._reap_strays(ORG)
        proc.wait(timeout=8)
        assert proc.poll() is not None  # the orphan was terminated
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def test_reap_strays_spares_a_connector_this_supervisor_owns(env):
    _provision_serve_cert(env)
    proc = _spawn_fake_connector(ORG_UUID)
    s = sup.ServingSupervisor(spawn=FakeSpawn())
    # Model this pid as the supervisor's own child: the reap must exclude it.
    s._procs[ORG] = SimpleNamespace(pid=lambda: proc.pid, alive=lambda: True)
    try:
        _await_visible(proc.pid, ORG_UUID)
        s._reap_strays(ORG)
        time.sleep(0.4)
        assert proc.poll() is None  # spared
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_launch_reaps_strays_before_spawning(env):
    """Startup with an inherited orphan: the new connector clears the leaked
    generation before it joins the crowd fighting for the relay slot."""
    _provision_serve_cert(env)
    _put_grant()
    stray = _spawn_fake_connector(ORG_UUID)
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    try:
        _await_visible(stray.pid, ORG_UUID)
        assert s.ensure(ORG)["running"] is True
        assert len(spawn.calls) == 1        # our clean connector launched
        stray.wait(timeout=8)
        assert stray.poll() is not None     # the orphan was reaped first
    finally:
        with contextlib.suppress(ProcessLookupError):
            stray.kill()


def test_losing_roster_membership_stops_a_live_connector(env, monkeypatch):
    """The watchdog enforces a changed synced roster on a live child.

    This test previously asserted that a changed SELECTION stopped the
    connector. Under all-machine serving (auto-clune.7) designation alone must
    not stop it — that inversion is asserted in
    `test_a_designation_change_alone_no_longer_stops_a_live_connector`. What
    still holds, and is enforced here, is the stronger condition: a machine
    REMOVED FROM THE ACTIVE ROSTER loses serving on the next reconcile.
    """
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    _seed_tunnel_inputs(
        monkeypatch, local="local-machine",
        roster=["local-machine", "other-machine"], selected="local-machine")
    assert supervisor.ensure(ORG)["reason"] == "launched"
    proc = spawn.procs[0]

    _seed_tunnel_inputs(
        monkeypatch, local="local-machine",
        roster=["other-machine"], selected="other-machine")

    assert supervisor.ensure(ORG)["running"] is False
    assert proc.alive() is False
    assert supervisor.running_orgs() == []
    assert len(spawn.calls) == 1


def test_first_publish_cannot_bypass_roster_membership(env, monkeypatch):
    """The no-live-grant start path is gated exactly like reconciliation.

    Designation no longer gates serving (auto-clune.7), so the assertion moves
    to the property this test was actually protecting: a machine that is NOT IN
    THE ACTIVE ROSTER cannot start a connector by taking the first-publish
    path. Real state() is driven to tunnel-server-unassigned, which returns
    before it validates local identity, so the predicate's re-validation is
    what refuses here.
    """
    _provision_serve_cert(env)
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    _seed_tunnel_inputs(
        monkeypatch, local="stranger", roster=["a-machine", "b-machine"],
        selected=None)

    assert supervisor.start(ORG)["running"] is False
    assert spawn.calls == []


def test_start_launches_without_a_live_grant(env):
    """First publish: ensure() won't start (no grant yet), but start() must —
    the grant is created BY riding this tunnel, so it cannot pre-exist."""
    _provision_serve_cert(env)  # cert present, NO grant cached
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG) == {"running": False, "reason": "no-live-grants"}
    assert s.start(ORG) == {"running": True, "reason": "launched"}
    assert len(spawn.calls) == 1
    assert s.start(ORG) == {"running": True, "reason": "already-running"}  # idempotent
    assert len(spawn.calls) == 1


def test_start_refuses_without_a_serve_cert(env):
    spawn = FakeSpawn()  # no serve-cert provisioned
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.start(ORG) == {"running": False, "reason": "missing"}
    assert spawn.calls == []


def test_fresh_tunnel_survives_the_grace_then_is_reaped(env):
    """A just-started tunnel with no grant yet is skipped for one interval, then
    reaped if the publish never cached a grant."""
    _provision_serve_cert(env)
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.start(ORG)["running"] is True
    proc = spawn.procs[0]
    clock[0] += 5  # inside the 20s grace
    assert s.ensure(ORG) == {"running": True, "reason": "fresh-grace"}
    assert proc.alive() is True
    clock[0] += 60  # past the grace, still no grant
    assert s.ensure(ORG) == {"running": False, "reason": "no-live-grants"}
    assert proc.alive() is False


def test_fresh_tunnel_kept_once_its_grant_lands(env):
    """The normal success path: the publish caches its grant within the grace,
    so the tunnel keeps running past the grace with no restart."""
    _provision_serve_cert(env)
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.start(ORG)["running"] is True
    _put_grant()  # the publish round-tripped and cached its grant
    clock[0] += 60  # well past the grace
    assert s.ensure(ORG) == {"running": True, "reason": "already-running"}
    assert spawn.procs[0].alive() is True
    assert len(spawn.calls) == 1  # never respawned


def test_watchdog_reconcile_relaunches_dead_proc(env):
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    s.ensure(ORG)
    assert len(spawn.calls) == 1

    spawn.procs[0].die()          # simulate a crash
    s.ensure_all()                 # what the watchdog thread runs
    assert len(spawn.calls) == 2   # relaunched
    assert s.running_orgs() == [ORG]


def test_watchdog_replaces_alive_child_that_never_serves(env):
    _provision_serve_cert(env)
    _put_grant()
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.ensure(ORG)["reason"] == "launched"
    first = spawn.procs[0]
    first.disconnect()

    clock[0] += sup.CONNECTOR_STARTUP_TIMEOUT_S - 1
    assert s.ensure(ORG) == {"running": True, "reason": "starting"}
    assert len(spawn.calls) == 1

    clock[0] += 2
    assert s.ensure(ORG)["reason"] == "launched"
    assert first.alive() is False
    assert len(spawn.calls) == 2


def test_watchdog_leaves_reconnect_to_child_after_it_has_served(env):
    _provision_serve_cert(env)
    _put_grant()
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.ensure(ORG)["reason"] == "launched"
    proc = spawn.procs[0]

    assert s.ensure(ORG)["reason"] == "already-running"
    proc.disconnect()
    clock[0] += sup.CONNECTOR_STARTUP_TIMEOUT_S * 2
    assert s.ensure(ORG) == {"running": True, "reason": "reconnecting"}
    assert proc.alive() is True
    assert len(spawn.calls) == 1


def test_a_connector_that_served_then_wedged_is_eventually_replaced(env):
    """The gap the reconnect grace used to leave open.

    A child that completes one handshake and then stops serving forever is
    alive, correctly credentialed, and useless. Because it had served, it was
    exempt from the startup deadline and no other path reaped it, so the
    supervisor reported it running indefinitely and a guest reached nothing.
    """
    _provision_serve_cert(env)
    _put_grant()
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.ensure(ORG)["reason"] == "launched"
    wedged = spawn.procs[0]
    assert s.ensure(ORG)["reason"] == "already-running"

    wedged.disconnect()

    # Still inside the window its own reconnect loop owns: left alone.
    clock[0] += sup.CONNECTOR_RECONNECT_TIMEOUT_S - 1
    assert s.ensure(ORG) == {"running": True, "reason": "reconnecting"}
    assert wedged.alive() is True
    assert len(spawn.calls) == 1

    # Past it, the process is wedged rather than reconnecting: replace it.
    clock[0] += 2
    assert s.ensure(ORG)["reason"] == "launched"
    assert wedged.alive() is False
    assert len(spawn.calls) == 2


def test_reconnect_deadline_is_measured_from_last_serving_not_launch(env):
    """A connector serving normally for a long time is never reaped for age.

    Measuring from launch would replace every healthy long-lived connector
    the moment it outlived the deadline, which is the opposite failure.
    """
    _provision_serve_cert(env)
    _put_grant()
    clock = [1000.0]
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn, now=lambda: clock[0])
    assert s.ensure(ORG)["reason"] == "launched"
    proc = spawn.procs[0]

    for _ in range(5):
        clock[0] += sup.CONNECTOR_RECONNECT_TIMEOUT_S / 2
        assert s.ensure(ORG) == {"running": True, "reason": "already-running"}

    assert proc.alive() is True
    assert len(spawn.calls) == 1, "a serving connector must never be churned"


def test_only_one_dashboard_process_owns_an_org_connector(env):
    """Independent dashboard supervisors share one process-wide file lock.

    A non-owner must neither spawn a competing connector nor remove the
    owner's shared control descriptor.  Once the owner stops, another
    dashboard may acquire ownership normally.
    """
    _provision_serve_cert(env)
    _put_grant()
    first_spawn = FakeSpawn()
    second_spawn = FakeSpawn()
    first = sup.ServingSupervisor(spawn=first_spawn)
    second = sup.ServingSupervisor(spawn=second_spawn)

    assert first.ensure(ORG)["reason"] == "launched"
    assert second.ensure(ORG) == {
        "running": True,
        "reason": "owned-by-other-dashboard",
    }
    assert len(first_spawn.calls) == 1
    assert second_spawn.calls == []

    first.stop_all()
    assert second.ensure(ORG)["reason"] == "launched"
    assert len(second_spawn.calls) == 1
    second.stop_all()


def test_pre_spawn_failure_releases_org_ownership(env, monkeypatch):
    """A failed owner must not prevent another dashboard from taking over."""
    _provision_serve_cert(env)
    _put_grant()
    first = sup.ServingSupervisor(spawn=FakeSpawn())
    second_spawn = FakeSpawn()
    second = sup.ServingSupervisor(spawn=second_spawn)
    original = sup._materialize_cert

    def fail_materialization(*_args, **_kwargs):
        raise OSError("simulated certificate write failure")

    monkeypatch.setattr(sup, "_materialize_cert", fail_materialization)
    with pytest.raises(OSError, match="simulated certificate write failure"):
        first.ensure(ORG)

    monkeypatch.setattr(sup, "_materialize_cert", original)
    assert second.ensure(ORG)["reason"] == "launched"
    assert len(second_spawn.calls) == 1
    second.stop_all()


def test_reprovisioned_credential_restarts_existing_connector(env):
    from tools.network.idkit import KeyPair, Subject, issue_cert

    provisioned = _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG)["reason"] == "launched"
    original = spawn.procs[0]

    replacement = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        provisioned["root"],
        replacement.public_hex,
        scope=("tunnel:serve",),
        org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 10,
        not_after=now + 30 * 24 * 3600,
    )
    viewer_cert = issue_cert(
        provisioned["root"], replacement.public_hex,
        scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", replacement.public_hex),
        not_before=cert.not_before, not_after=cert.not_after,
    )
    provisioned["key_path"].write_text(replacement.private_hex)
    settings_ops.upsert_by_key(
        NETWORK_SERVE_CERT_SET_ID,
        NETWORK_SERVE_CERT_REVISION,
        "default",
        {
            "cert": cert.to_json().decode("ascii"),
            "viewer_cert": viewer_cert.to_json().decode("ascii"),
            "key_path": str(provisioned["key_path"]),
            "root_pub": provisioned["root"].public_hex,
            "not_after": cert.not_after,
        },
        org=ORG,
    )

    assert s.ensure(ORG)["reason"] == "launched"
    assert original.alive() is False
    assert len(spawn.calls) == 2


def test_legacy_operator_serve_cert_requires_reprovision(
    env, monkeypatch,
):
    from tools.network.idkit import KeyPair, Subject, issue_cert

    root = KeyPair.generate()
    child = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root,
        child.public_hex,
        scope=("tunnel:serve",),
        org=ORG_UUID,
        subject=Subject("operator", "legacy-browser"),
        not_before=now - 10,
        not_after=now + 3600,
    )
    key_path = env / "legacy.key"
    key_path.write_text(child.private_hex)
    row = {
        "cert": cert.to_json().decode("ascii"),
        "key_path": str(key_path),
        "root_pub": root.public_hex,
        "not_after": cert.not_after,
    }
    monkeypatch.setattr(
        settings_ops,
        "read_owned_set",
        lambda *args, **kwargs: SimpleNamespace(
            members=[SimpleNamespace(payload=row)]
        ),
    )

    state = sup.serve_cert_state(ORG)
    assert state["status"] == "identity-invalid"
    assert "reprovision" in state["error"]


def test_serve_cert_ok_reflects_status(env):
    assert sup.serve_cert_ok(ORG) is False
    _provision_serve_cert(env)
    assert sup.serve_cert_ok(ORG) is True


def test_portable_basename_resolves_inside_current_serving_key_store(
    env,
    monkeypatch,
):
    provisioned = _provision_serve_cert(env)
    monkeypatch.setenv("AUTONOMY_NETWORK_KEY_DIR", str(env / "network"))
    _replace_key_path(provisioned, provisioned["key_path"].name)

    state = sup.serve_cert_state(ORG)
    assert state["status"] == "ok"
    assert state["key_path"] == str(provisioned["key_path"])


def test_legacy_absolute_key_path_remains_valid_in_place(env):
    provisioned = _provision_serve_cert(env)
    state = sup.serve_cert_state(ORG)
    assert state["status"] == "ok"
    assert state["key_path"] == str(provisioned["key_path"])


@pytest.mark.parametrize(
    "stored",
    [
        "../serve.key",
        "nested/serve.key",
        r"nested\serve.key",
        ".",
        "..",
    ],
)
def test_relative_key_path_traversal_and_nesting_fail_closed(
    env,
    monkeypatch,
    stored,
):
    provisioned = _provision_serve_cert(env)
    monkeypatch.setenv("AUTONOMY_NETWORK_KEY_DIR", str(env / "network"))
    _replace_key_path(provisioned, stored)

    state = sup.serve_cert_state(ORG)
    assert state["status"] == "key-invalid"
    assert "bare filename" in state["error"]
    _put_grant()
    spawn = FakeSpawn()
    assert sup.ServingSupervisor(spawn=spawn).ensure(ORG) == {
        "running": False,
        "reason": "key-invalid",
    }
    assert spawn.calls == []


def test_bootstrap_ensures_and_arms_watchdog(env, monkeypatch):
    """Startup entry discovers, reconciles, and watches the local org."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    # bootstrap() uses the process singleton; point it at our fake-spawn one.
    s = sup.ServingSupervisor(spawn=spawn)
    monkeypatch.setattr(sup, "_SINGLETON", s)
    monkeypatch.setattr(sup, "_discover_startup_orgs", lambda: [ORG])

    out = sup.bootstrap()
    assert out is s
    assert len(spawn.calls) == 1          # reconciled → launched
    assert s._watchdog is not None        # watchdog armed
    s.stop_all()
    assert s._watchdog is None            # can be armed again in the same process


def test_startup_org_discovery_covers_every_local_org(env, monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_ORG", ORG)  # deliberately inert
    # Phase 1 tests the PINNED branch explicitly (the fixture no longer
    # pins): with GRAPH_DB set, discovery collapses to the pinned database's
    # own scopeless slot — nothing ambient can label or widen it.
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "pin.db"))
    assert sup._discover_startup_orgs() == [None]

    monkeypatch.delenv("GRAPH_DB")
    monkeypatch.setattr(
        "tools.graph.org_ops.list_orgs",
        lambda: [SimpleNamespace(slug="autonomy"), SimpleNamespace(slug="dynbench")],
    )
    assert sup._discover_startup_orgs() == [None, "autonomy", "dynbench"]


# ── when a serving credential is due for renewal ──


def _renewal_decision(ttl_days: float) -> dict:
    """What the status route tells the browser about a cert with this much
    life left: the JSON body of GET /api/network/serve-cert."""
    import json as _json

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from tools.dashboard import network_routes

    app = Starlette(routes=network_routes.ROUTES)
    # The route refuses a cross-org read; scope the caller to this org, as a
    # signed-in browser would be.
    previous = os.environ.get("GRAPH_ORG")
    os.environ["GRAPH_ORG"] = ORG
    try:
        with TestClient(app) as client:
            resp = client.get(f"/api/network/serve-cert?org={ORG}")
    finally:
        if previous is None:
            os.environ.pop("GRAPH_ORG", None)
        else:
            os.environ["GRAPH_ORG"] = previous
    body = _json.loads(resp.content)
    assert resp.status_code == 200, body
    return body


def test_a_fresh_credential_is_not_reminted_on_every_sign_in(env, tmp_path):
    """A 30-day delegate has 30 days left the moment it is minted, so it must
    sit well clear of the threshold -- otherwise signing in daily would mint a
    new credential every day."""
    _provision_serve_cert(tmp_path, ttl=30 * 24 * 3600)
    decision = _renewal_decision(30)
    assert decision["status"] == "ok"
    assert decision["required"] is False
    assert 29 < decision["days_remaining"] <= 30


def test_a_credential_past_its_first_ten_days_is_renewed(env, tmp_path):
    """Renewing below 20 days of a 30-day life means a fresh credential is
    left alone for its first 10 days and replaced by any sign-in after that --
    at most one mint per 10 days, however often the operator signs in."""
    _provision_serve_cert(tmp_path, ttl=19 * 24 * 3600)
    decision = _renewal_decision(19)
    assert decision["required"] is True
    assert decision["status"] == "ok", (
        "still perfectly usable -- serving must not stop while it is renewed")
    assert 18 < decision["days_remaining"] <= 19


def test_a_credential_days_from_death_is_renewed(env, tmp_path):
    """The case that went unnoticed: valid, so the old check called it 'ok'
    and never re-minted, and it would have expired before anything replaced
    it."""
    _provision_serve_cert(tmp_path, ttl=4 * 24 * 3600)
    decision = _renewal_decision(4)
    assert decision["required"] is True
    assert 3 < decision["days_remaining"] <= 4


def test_serving_probe_is_deterministic_and_never_raises(monkeypatch):
    """The tunnel/serving indicators call ``bool(get_supervisor().serving())``.

    Before this method existed on ServingSupervisor, that call AttributeError'd
    on every request and both consumers swallowed it in a broad ``except`` —
    the flag tray degraded to a dim "Tunnel state is unavailable" tile that
    could never show Down, and the fleet page fell through to a green
    "Serving". (The old test_unlock_state_sync stub gave get_supervisor a fake
    ``serving`` method, so the missing-method bug slipped past every test.)
    serving() must EXIST, return a bool, and never raise: a dead connector
    reads False (Down), a live handshaking one True.
    """
    s = sup.ServingSupervisor()

    # No connector reachable (orphan .ctl descriptor / refused socket) — the
    # exact live-dead state: control raises TunnelUnavailable.
    monkeypatch.setattr(sup, "control", _raise(sup.TunnelUnavailable(
        "no control listener", kind="no-listener")))
    assert s.serving() is False

    # A connector that answers but is not serving -> False.
    monkeypatch.setattr(sup, "control", lambda *_a, **_k: {"ok": True, "serving": False})
    assert s.serving() is False
    # ok False -> False even if it claims serving.
    monkeypatch.setattr(sup, "control", lambda *_a, **_k: {"ok": False, "serving": True})
    assert s.serving() is False
    # Only a live, handshaking connector -> True.
    monkeypatch.setattr(sup, "control", lambda *_a, **_k: {"ok": True, "serving": True})
    assert s.serving() is True

    # Any unexpected probe error is still False, never propagated to callers.
    monkeypatch.setattr(sup, "control", _raise(RuntimeError("unexpected")))
    assert s.serving() is False


def _raise(exc):
    def _fn(*_a, **_k):
        raise exc
    return _fn


def _lame_duck_fixture(env, monkeypatch, *, now=None):
    """Provision + adopt a STALE incumbent that reports one active stream.

    Returns (supervisor, spawn, sleeper, probe) where probe is a mutable
    dict later reconcile passes read active_streams from."""
    from tools.dashboard import link_serving_supervisor as sup

    _provision_serve_cert(env)
    state = sup.serve_cert_state(ORG)
    assert state["status"] == "ok", state
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn, now=now)
    # A real process so _AdoptedProc.alive() holds during later reconciles.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    probe = {"ok": True, "serving": True, "boot_commit": "0" * 40,
             "active_streams": 1, "stream_activity_age_s": 1.0}
    # A killed duck's control socket goes silent — mirror that, or the
    # re-launch after replacement would re-adopt the corpse forever.
    monkeypatch.setattr(
        sup, "_probe_ctl_status",
        lambda ctl: dict(probe) if sleeper.poll() is None else None,
    )
    monkeypatch.setattr(
        sup, "_iter_connector_pids", lambda org_uuid: iter([sleeper.pid])
    )
    reaped = []
    monkeypatch.setattr(
        supervisor, "_reap_strays", lambda org: reaped.append(org)
    )
    # Reconcile gates that are out of scope here: eligible + live grant.
    monkeypatch.setattr(
        sup.ServingSupervisor, "_fleet_eligibility",
        staticmethod(lambda: SimpleNamespace(
            allowed=True, active_machine_count=0, reason="",
            selected_machine_id=None,
        )),
    )
    monkeypatch.setattr(sup, "_has_live_grant", lambda org, now: True)
    return supervisor, spawn, sleeper, probe, reaped, state


def test_stale_incumbent_mid_stream_is_adopted_as_lame_duck(env, monkeypatch):
    """A stale-code incumbent WITH an active stream drains instead of dying:
    recycling on every /app commit killed every first-contact fleet transfer
    (~66s generations vs a multi-minute 325MiB pull, live 2026-09-06)."""
    supervisor, spawn, sleeper, _probe, reaped, state = _lame_duck_fixture(
        env, monkeypatch)
    try:
        result = supervisor._launch(ORG, state)
        assert result == {"running": True, "reason": "lame-duck-draining"}
        assert reaped == [], "a draining duck must not be reaped"
        assert spawn.calls == [], "no replacement until the duck drains"
        assert ORG in supervisor._lame_duck_since
    finally:
        sleeper.kill()


def test_lame_duck_is_replaced_once_streams_drain(env, monkeypatch):
    supervisor, spawn, sleeper, probe, _reaped, state = _lame_duck_fixture(
        env, monkeypatch)
    try:
        assert supervisor._launch(ORG, state)["reason"] == "lame-duck-draining"
        assert supervisor.ensure(ORG)["reason"] == "lame-duck-draining"
        probe["active_streams"] = 0
        result = supervisor.ensure(ORG)
        assert result["reason"] == "launched"
        assert len(spawn.calls) == 1, "drained duck must be replaced"
        assert ORG not in supervisor._lame_duck_since
    finally:
        sleeper.kill()


def test_lame_duck_is_replaced_at_the_drain_deadline(env, monkeypatch):
    from tools.dashboard import link_serving_supervisor as sup

    clock = {"t": 1000.0}
    supervisor, spawn, sleeper, _probe, _reaped, state = _lame_duck_fixture(
        env, monkeypatch, now=lambda: clock["t"])
    try:
        assert supervisor._launch(ORG, state)["reason"] == "lame-duck-draining"
        clock["t"] += sup.LAME_DUCK_DRAIN_DEADLINE_S - 1
        assert supervisor.ensure(ORG)["reason"] == "lame-duck-draining"
        clock["t"] += 2
        result = supervisor.ensure(ORG)
        assert result["reason"] == "launched", (
            "streams still active, but the deadline bounds stale serving"
        )
        assert len(spawn.calls) == 1
    finally:
        sleeper.kill()


def _running_connector(env, monkeypatch, *, disk):
    """A launched FakeProc connector under a reconcile that is eligible and
    has a live grant; ``disk`` is a mutable dict the disk-head stub reads."""
    from tools.dashboard import link_serving_supervisor as sup
    from tools.network import build_version

    _provision_serve_cert(env)
    state = sup.serve_cert_state(ORG)
    assert state["status"] == "ok", state
    monkeypatch.setattr(build_version, "disk_head", lambda: disk["head"])
    monkeypatch.setattr(
        sup.ServingSupervisor, "_fleet_eligibility",
        staticmethod(lambda: SimpleNamespace(
            allowed=True, active_machine_count=0, reason="",
            selected_machine_id=None,
        )),
    )
    monkeypatch.setattr(sup, "_has_live_grant", lambda org, now: True)
    monkeypatch.setattr(sup, "_iter_connector_pids", lambda org_uuid: iter([]))
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    monkeypatch.setattr(supervisor, "_reap_strays", lambda org: None)
    assert supervisor.ensure(ORG)["reason"] == "launched"
    assert supervisor._boot_commit[ORG] == disk["head"]
    return sup, supervisor, spawn


def test_watchdog_replaces_an_idle_connector_when_the_disk_head_moves(env, monkeypatch):
    """A tools/network-only merge never hot-reloads the dashboard; the
    watchdog itself must notice the code generation moved (2026-09-06)."""
    disk = {"head": "a" * 40}
    sup, supervisor, spawn = _running_connector(env, monkeypatch, disk=disk)
    monkeypatch.setattr(sup, "_probe_ctl_status",
                        lambda ctl: {"ok": True, "serving": True, "active_streams": 0})
    assert supervisor.ensure(ORG)["reason"] == "already-running"
    assert len(spawn.calls) == 1, "unchanged disk head: leave it alone"
    disk["head"] = "b" * 40
    assert supervisor.ensure(ORG)["reason"] == "launched"
    assert len(spawn.calls) == 2, "moved disk head + idle: replaced"
    assert spawn.procs[0].alive() is False
    assert supervisor._boot_commit[ORG] == "b" * 40


def test_watchdog_lame_ducks_a_streaming_connector_when_the_disk_head_moves(env, monkeypatch):
    disk = {"head": "a" * 40}
    sup, supervisor, spawn = _running_connector(env, monkeypatch, disk=disk)
    probe = {"ok": True, "serving": True, "active_streams": 1,
             "stream_activity_age_s": 1.0}
    monkeypatch.setattr(sup, "_probe_ctl_status", lambda ctl: dict(probe))
    disk["head"] = "b" * 40
    assert supervisor.ensure(ORG)["reason"] == "lame-duck-draining"
    assert len(spawn.calls) == 1, "mid-stream: drained, not severed"
    assert spawn.procs[0].alive() is True
    probe["active_streams"] = 0
    assert supervisor.ensure(ORG)["reason"] == "launched"
    assert len(spawn.calls) == 2, "drained: replaced on the new generation"


def test_stale_incumbent_with_a_stuck_stream_counter_is_replaced(env, monkeypatch):
    """active_streams=1 with NO recent activity is a leaked counter, not a
    stream (held 5+ min live 2026-09-06): the stale connector is replaced,
    never kept as a lame duck."""
    from tools.dashboard import link_serving_supervisor as sup

    _provision_serve_cert(env)
    state = sup.serve_cert_state(ORG)
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    monkeypatch.setattr(
        sup, "_probe_ctl_status",
        lambda ctl: {"ok": True, "serving": True, "boot_commit": "0" * 40,
                     "active_streams": 1, "stream_activity_age_s": 900.0},
    )
    reaped = []
    monkeypatch.setattr(supervisor, "_reap_strays", lambda org: reaped.append(org))
    assert supervisor._launch(ORG, state)["reason"] == "launched"
    assert reaped == [ORG] and len(spawn.calls) == 1
    assert ORG not in supervisor._lame_duck_since

# -- activation: real predicate, faked inputs (auto-clune.7) -------------------
#
# These drive the REAL fleet_tunnel_server.state() and the REAL
# tunnel_serving_permitted() by faking their INPUTS — roster, local identity,
# stored selection. Patching `_serving_permitted` to return a bool would test
# supervisor branching only and would never exercise the roster re-validation
# that is this change's entire safety property.


def _seed_tunnel_inputs(monkeypatch, *, local, roster, selected, joining=False):
    """Make the real state() reach a chosen designation outcome."""
    from tools.network import fleet_tunnel_server as fts

    entries = tuple(roster)
    monkeypatch.setattr(fts.machine_boot, "machine_id", lambda **k: local)
    monkeypatch.setattr(fts.machine_boot, "is_joining", lambda **k: joining)
    monkeypatch.setattr(fts, "_personal_root_pub", lambda: "aa" * 32)
    monkeypatch.setattr(fts.fleet_roster, "load_entries", lambda **k: entries)
    monkeypatch.setattr(
        fts.fleet_roster, "resolve",
        lambda entries, anchor_root_pub: {
            m: SimpleNamespace(machine_id=m) for m in roster})
    monkeypatch.setattr(fts, "_stored_selection", lambda: (selected, None))


def test_an_active_non_selected_member_may_now_serve(env, monkeypatch):
    """Designation alone no longer withholds serving. Real state() returns
    not-designated here — this machine is rostered but another is selected —
    and the connector launches anyway."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    _seed_tunnel_inputs(
        monkeypatch, local="local-machine",
        roster=["local-machine", "other-machine"], selected="other-machine")
    from tools.network import fleet_tunnel_server as fts
    assert fts.state().reason == "not-designated"      # the real gate is exercised
    assert fts.state().allowed is False                # election unchanged

    assert sup.ServingSupervisor(spawn=spawn).ensure(ORG)["running"] is True


def test_a_machine_absent_from_the_roster_still_refuses(env, monkeypatch):
    """NEGATIVE CONTROL, and the property the old test was protecting. With no
    selection assigned, state() returns tunnel-server-unassigned BEFORE it
    validates local identity, so the predicate re-validates roster membership.
    A machine that is not in the active roster must not serve."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    _seed_tunnel_inputs(
        monkeypatch, local="stranger", roster=["a-machine", "b-machine"],
        selected=None)

    result = sup.ServingSupervisor(spawn=spawn).ensure(ORG)

    assert result["running"] is False
    # The refusal must name the ACTUAL cause. state() returns
    # tunnel-server-unassigned before it validates membership, so reporting the
    # election's reason here would send an operator looking for a missing
    # assignment when the machine simply is not in the roster.
    assert result["reason"] == "machine-not-rostered"
    assert spawn.calls == []


def test_the_stop_log_names_the_actual_cause(env, monkeypatch, caplog):
    """The RETURN value is read by an API caller; the LOG is what an operator
    reads when asking why a connector is not running. Both must carry the same
    effective reason, or the likelier diagnostic path is the misleading one: a
    machine dropped from the roster would be logged as an assignment problem and
    send the operator after a selection that is not the cause."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    _seed_tunnel_inputs(
        monkeypatch, local="local-machine",
        roster=["local-machine", "other-machine"], selected="local-machine")
    assert supervisor.ensure(ORG)["running"] is True   # a live proc to stop

    # Dropped from the roster with no selection stored: state() reports
    # tunnel-server-unassigned, the predicate reports machine-not-rostered.
    _seed_tunnel_inputs(
        monkeypatch, local="local-machine", roster=["other-machine"], selected=None)
    with caplog.at_level(logging.WARNING, logger=sup._log.name):
        result = supervisor.ensure(ORG)

    assert result["reason"] == "machine-not-rostered"
    stops = [r.getMessage() for r in caplog.records
             if "stopping serving connector" in r.getMessage()]
    assert stops, "a stop must never be silent"
    assert "machine-not-rostered" in stops[-1]
    assert "tunnel-server-unassigned" not in stops[-1]


def test_a_machine_with_no_durable_identity_still_refuses(env, monkeypatch):
    """The second condition state() had not reached at tunnel-server-unassigned."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    _seed_tunnel_inputs(
        monkeypatch, local=None, roster=["a-machine", "b-machine"], selected=None)
    result = sup.ServingSupervisor(spawn=spawn).ensure(ORG)

    assert result["running"] is False
    assert result["reason"] == "machine-identity-missing"
    assert spawn.calls == []


def test_a_machine_mid_join_still_refuses(env, monkeypatch):
    """Preserved negative control: a joining machine fails closed so it never
    serves as a second primary before its roster arrives."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    _seed_tunnel_inputs(
        monkeypatch, local="local-machine", roster=[], selected=None, joining=True)

    assert sup.ServingSupervisor(spawn=spawn).ensure(ORG)["running"] is False
    assert spawn.calls == []


def test_a_designation_change_alone_no_longer_stops_a_live_connector(env, monkeypatch):
    """The inverted premise, stated explicitly. The watchdog used to stop a
    connector when the synced selection named another machine; under
    all-machine serving it must not, provided this machine is still rostered."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    _seed_tunnel_inputs(
        monkeypatch, local="local-machine",
        roster=["local-machine", "other-machine"], selected="local-machine")
    assert supervisor.ensure(ORG)["running"] is True
    proc = spawn.procs[0]

    _seed_tunnel_inputs(
        monkeypatch, local="local-machine",
        roster=["local-machine", "other-machine"], selected="other-machine")

    assert supervisor.ensure(ORG)["running"] is True
    assert proc.alive() is True
