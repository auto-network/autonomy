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
import fcntl
import multiprocessing
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


def test_connector_dies_with_its_parent_via_pdeathsig(tmp_path):
    """A spawned connector is signalled when its dashboard dies, however it
    dies — the leak this bead exists to fix.

    A stand-in "dashboard" process spawns a long-lived child through the real
    ``_default_spawn`` (which arms ``PR_SET_PDEATHSIG``), reports the child pid,
    then is SIGKILLed — the crash path ``stop_all()`` never covers. The kernel
    must reap the orphan; without the death signal it would survive, reparented
    to init, exactly as the five leaked generations did.
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
        parent.kill()  # SIGKILL: the shutdown hook never runs
        parent.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline and _alive(child_pid):
            time.sleep(0.05)
        assert not _alive(child_pid), (
            "orphaned connector survived its parent's death — PDEATHSIG did "
            "not fire"
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            parent.kill()
        with contextlib.suppress(Exception):
            os.kill(child_pid, signal.SIGKILL)


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
            "viewer_cert": viewer_cert, "key_path": key_path}


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


def test_roster_assignment_stops_a_connector_owned_by_another_machine(
    env, monkeypatch,
):
    """The watchdog enforces a changed synced selection on a live child."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    assert supervisor.ensure(ORG)["reason"] == "launched"
    proc = spawn.procs[0]

    monkeypatch.setattr(
        supervisor,
        "_fleet_eligibility",
        lambda: SimpleNamespace(
            allowed=False,
            reason="not-designated",
            selected_machine_id="22" * 32,
        ),
    )
    assert supervisor.ensure(ORG) == {
        "running": False,
        "reason": "not-designated",
        "selected_machine_id": "22" * 32,
    }
    assert proc.alive() is False
    assert supervisor.running_orgs() == []
    assert len(spawn.calls) == 1


def test_first_publish_cannot_bypass_roster_assignment(env, monkeypatch):
    """The no-live-grant start path is gated exactly like reconciliation."""
    _provision_serve_cert(env)
    spawn = FakeSpawn()
    supervisor = sup.ServingSupervisor(spawn=spawn)
    monkeypatch.setattr(
        supervisor,
        "_fleet_eligibility",
        lambda: SimpleNamespace(
            allowed=False,
            reason="tunnel-server-unassigned",
            selected_machine_id=None,
        ),
    )
    assert supervisor.start(ORG) == {
        "running": False,
        "reason": "tunnel-server-unassigned",
    }
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


def test_fork_child_drops_inherited_ownership_descriptors_without_unlocking(monkeypatch):
    """A multiprocessing child must not keep the Dashboard's flocks alive.

    The child shares the parent's open-file descriptions after ``fork()``.
    Closing its duplicate is safe; explicitly unlocking it would also unlock
    the still-live parent's ownership.
    """
    class _InheritedLock:
        closed = False

        def close(self):
            self.closed = True

    inherited = _InheritedLock()
    supervisor = sup.ServingSupervisor(spawn=FakeSpawn())
    supervisor._locks[ORG] = inherited
    flock_calls = []
    monkeypatch.setattr(sup.fcntl, "flock", lambda *args: flock_calls.append(args))

    supervisor._drop_inherited_locks_after_fork()

    assert inherited.closed is True
    assert supervisor._locks == {}
    assert flock_calls == []


def test_multiprocessing_child_cannot_prolong_parent_ownership(env):
    """Exercise multiprocessing's own post-fork callback registry.

    The parent releases its legitimate lock only after the child exists.  A
    child that retained the duplicate would keep the flock busy; a cleaned
    child can acquire it through a new file description.
    """
    supervisor = sup.ServingSupervisor(spawn=FakeSpawn())
    key_path = str(env / "fork-proof.key")
    assert supervisor._acquire_lock(ORG, key_path) is True
    sup._register_multiprocessing_fork_cleanup(supervisor)
    parent, child = multiprocessing.get_context("fork").Pipe()

    def probe_after_parent_release(pipe, path):
        pipe.recv()
        candidate = open(path, "a+")
        try:
            fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pipe.send(False)
        else:
            pipe.send(True)
        finally:
            candidate.close()

    process = multiprocessing.get_context("fork").Process(
        target=probe_after_parent_release,
        args=(child, sup._lock_path_for(key_path)),
    )
    process.start()
    supervisor._release_lock(ORG)
    parent.send(True)
    assert parent.recv() is True
    process.join(timeout=5)
    assert process.exitcode == 0


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
