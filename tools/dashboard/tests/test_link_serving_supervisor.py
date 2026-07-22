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

import os
import time

import pytest

from tools.dashboard import link_serving_supervisor as sup
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

    def alive(self):
        return self._alive

    def stop(self):
        self._alive = False

    def die(self):  # simulate a crash for the watchdog test
        self._alive = False


class FakeSpawn:
    def __init__(self):
        self.calls = []
        self.procs = []

    def __call__(self, argv, env, *, log_path=None):
        self.calls.append({"argv": argv, "env": env, "log_path": log_path})
        p = FakeProc()
        self.procs.append(p)
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
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
        subject=Subject("operator", "op-serve"),
        not_before=now - 300, not_after=now + ttl,
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
    return {"root": root, "delegate": delegate, "cert": cert, "key_path": key_path}


def _put_grant(token="a" * 32, *, meta=None, issued_at=None):
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "target_uuid": ORG_UUID,  # any uuid; supervisor only checks validity
            "target_type": "present",
            "meta": meta or {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": issued_at or time.strftime(ISO, time.gmtime()),
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
    spawn = FakeSpawn()
    s = sup.ServingSupervisor(spawn=spawn)
    assert s.ensure(ORG)["running"] is True
    proc = spawn.procs[0]

    _drop_grant()  # what link_revoke does to the cache
    assert s.ensure(ORG) == {"running": False, "reason": "no-live-grants"}
    assert proc.alive() is False  # the connector was stopped


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


def test_serve_cert_ok_reflects_status(env):
    assert sup.serve_cert_ok(ORG) is False
    _provision_serve_cert(env)
    assert sup.serve_cert_ok(ORG) is True


def test_bootstrap_ensures_and_arms_watchdog(env, monkeypatch):
    """Startup entry: reconciles the given org and starts the watchdog once."""
    _provision_serve_cert(env)
    _put_grant()
    spawn = FakeSpawn()
    # bootstrap() uses the process singleton; point it at our fake-spawn one.
    s = sup.ServingSupervisor(spawn=spawn)
    monkeypatch.setattr(sup, "_SINGLETON", s)

    out = sup.bootstrap(orgs=[ORG])
    assert out is s
    assert len(spawn.calls) == 1          # reconciled → launched
    assert s._watchdog is not None        # watchdog armed
    s.stop_all()
    assert s._watchdog is not None        # (thread object persists; _stop is set)
