"""C4 acceptance on the B2 harness: grant-gated serving over the real tunnel.

Topology: a real registry+relay subprocess, the real ``TunnelConnector``
running in-process with the C4 grant handler, and the real
``ViewerChannel`` E2E client. Three tokens, all known to the REGISTRY
(it opens a viewer channel for each), only differing in the dashboard's
LOCAL grant cache:

* cached + valid   → the binder-sized Present fixture streams through
  the E2E channel and matches byte length exactly;
* absent from the cache → channel closes before authentication, even though
  the relay requested it — a registry compromise alone opens nothing;
* cached but expired (``meta.ttl``) → the same pre-authentication closure.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import agents.design_db as design_db
from tools.dashboard import link_serving
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.registry.signing import sign_request
from tools.dashboard import link_serving_supervisor as sup
from tools.dashboard.tests import _serving_vault_kit as vault_kit
from tools.dashboard.link_probe import probe_link
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
)
from tools.graph.schemas.personal_identity import (
    PERSONAL_IDENTITY_REVISION,
    PERSONAL_IDENTITY_SET_ID,
)
from tools.network.idkit import Subject, issue_cert
from tools.network.idkit.root_factor_policy import mint_password_armor
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

REPO = Path(__file__).resolve().parents[3]
# The PERSONAL scope, deliberately. What this suite proves is the genuine
# subprocess path — supervisor launches a real connector, it dials a real
# relay, and serving goes live — and that path is identical for every scope.
# The cert generation is not what is under test here, and personal is the one
# scope whose root-signed revision-2 credential is still correct: it has no
# adopted membership checkpoint at the registry, so it presents a v2 hello
# that anchors at the org root. An org scope now requires a persona-signed
# cert AND a membership rider built from a founded ledger, which is its own
# fixture and its own bead; the org path is covered by the rider wiring tests
# and was verified live on sjc-2 (all four scopes serving, 2026-09-10).
ORG = "personal"
ORG_UUID = "77777777-7777-4777-8777-777777777777"
ISO = "%Y-%m-%dT%H:%M:%SZ"

# Binder-sized, like the B2 soak: ~1.55 MB of deck HTML.
BINDER_HTML = (
    "<html><body class=\"deck\">"
    + "".join(f"<section class=\"slide\">slide {i} — {'insight ' * 60}</section>"
              for i in range(3000))
    + "</body></html>"
)
BINDER_BYTES = BINDER_HTML.encode("utf-8")
assert len(BINDER_BYTES) > 1_500_000


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_registry(port: int, db: Path, log: Path,
                   extra_args: tuple[str, ...] = ()) -> subprocess.Popen:
    """*extra_args* are appended to the registry command line, for example
    ``("--log-level", "info")`` so relay routing lines reach *log*."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry",
         "--db", str(db), "--host", "127.0.0.1", "--port", str(port), *extra_args],
        cwd=str(REPO), env=env,
        stdout=open(log, "ab"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"registry did not come up; log: {log.read_text()[-2000:]}")


def publish_link(db, target_uuid: str) -> str:
    """Seed one grant at the registry store; returns the token. Publish
    rides the org tunnel in production — this stack's subject is serving."""
    from tools.network.registry.testkit import mint_link_at
    return mint_link_at(db, ORG_UUID, target_uuid)


def _link_key(token: str) -> KeyPair:
    """Stable per-token keypair for this hermetic serving stack."""
    return KeyPair.from_private_hex(hashlib.sha256(token.encode()).hexdigest())


def cache_grant(token: str, target_uuid: str, *, meta: dict | None = None,
                issued_at: str | None = None, channel_pub: str | None = None) -> None:
    channel_pub = channel_pub or _link_key(token).public_hex
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": target_uuid,
            "target_type": "present",
            "meta": meta or {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": issued_at or time.strftime(ISO, time.gmtime()),
            **({"channel_pub": channel_pub} if channel_pub else {}),
        },
        org=ORG,
    )


async def fetch_over_tunnel(port: int, token: str, link_pub: str,
                            timeout: float = 20.0) -> bytes:
    """Connect a viewer, run the E2E handshake, fetch once."""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            channel = await ViewerChannel.connect(
                f"ws://127.0.0.1:{port}", token, link_pub=link_pub, org=ORG_UUID,
            )
            break
        except Exception as exc:  # tunnel may still be dialing; retry
            last = exc
            await asyncio.sleep(0.25)
    else:
        raise AssertionError(f"viewer could not connect within {timeout}s: {last!r}")
    async with channel:
        await channel.send_message(canonical_json({"op": "fetch", "v": 1}))
        return await channel.recv_message()


async def assert_authentication_refused(port: int, token: str, link_pub: str) -> None:
    """A missing usable grant/key emits no hello or application response."""
    with pytest.raises(Exception):
        await asyncio.wait_for(
            ViewerChannel.connect(
                f"ws://127.0.0.1:{port}", token,
                link_pub=link_pub, org=ORG_UUID,
            ),
            timeout=5.0,
        )


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Registry subprocess + in-process C4-serving connector + tmp stores."""
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: the code under test
    # resolves explicit orgs, which a pin silently swallows (73bad14e)
    # and the fail-loud resolver refuses. delenv guards ambient leaks.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(ORG).close()
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "designs.db")
    monkeypatch.setattr(design_db, "_initialized", False)

    root = KeyPair.generate()
    session_key = KeyPair.generate()
    now = int(time.time())
    session_cert = issue_cert(
        root, session_key.public_hex,
        scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 300, not_after=now + 86_400,
    )
    # Identity-neutral channel certificate, as the supervisor supplies in
    # production: a persona-bearing certificate must never reach a viewer,
    # and the connector refuses legacy serving without a neutral one.
    channel_cert = issue_cert(
        root, session_key.public_hex,
        scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", session_key.public_hex),
        not_before=now - 300, not_after=now + 86_400,
    )

    port = free_port()
    registry = start_registry(port, tmp_path / "registry.db", tmp_path / "registry.log")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            response = client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
                 "recovery_policy": "none"},
                ts=now,
            ))
            assert response.status_code == 201, response.text

        # A machine identity is not optional: since bc79dc9a a connector
        # constructed without one refuses to open a tunnel at all (the
        # anonymous v1 hello is gone), and the relay files v2 tunnels by
        # (persona, machine). This org has registered no serving keys, so
        # the relay's transitional accept admits any well-signed machine key.
        connector = TunnelConnector(
            f"ws://127.0.0.1:{port}", ORG_UUID, session_key, session_cert,
            handler=link_serving.make_grant_handler(ORG),
            channel_cert=channel_cert,
            channel_authorization_for=lambda token: {
                "protocol": "public-link",
                "key": _link_key(token),
            } if link_serving.check_grant(token, org=ORG) else (_ for _ in ()).throw(
                PermissionError("link unavailable")),
            machine_key=KeyPair.generate(),
            min_backoff=0.1, max_backoff=1.0,
        )
        yield {"port": port, "root": root, "root_pub": root.public_hex,
               "db": tmp_path / "registry.db", "connector": connector}
    finally:
        registry.terminate()
        registry.wait(timeout=5)
        GraphDB.close_all_pooled()


def test_tunnel_serves_only_against_local_grants(stack):
    binder_rev = design_db.create_design(
        title="OSS Insights binder",
        variants=[{"id": "v1", "html": BINDER_HTML}],
    )

    # All three tokens are REAL registry grants — the relay will open a
    # viewer channel for each. The local cache is the only difference.
    granted = publish_link(stack["db"], binder_rev)
    registry_only = publish_link(stack["db"], binder_rev)
    expired = publish_link(stack["db"], binder_rev)
    cache_grant(granted, binder_rev)
    cache_grant(expired, binder_rev, meta={"ttl": 60},
                issued_at=time.strftime(ISO, time.gmtime(time.time() - 3600)))

    async def run():
        connector = stack["connector"]
        task = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(connector.connected.wait(), timeout=15)

            served = await fetch_over_tunnel(
                stack["port"], granted, _link_key(granted).public_hex)
            header, _, body = served.partition(b"\n")
            assert json.loads(header) == {
                "v": 1, "status": "ok", "kind": "present",
                "viewer": {"offset": 0, "length": len(BINDER_BYTES)},
                # The generated identity of the scope this suite serves as.
                "branding": {
                    "name": "personal", "color": "#F4A261", "initial": "P",
                },
            }
            assert len(body) == len(BINDER_BYTES)  # binder-sized, byte-exact
            assert body == BINDER_BYTES

            # The registry knows these tokens, but neither has a usable local
            # grant/key. Both close before authentication or application data.
            await assert_authentication_refused(
                stack["port"], registry_only,
                _link_key(registry_only).public_hex,
            )
            await assert_authentication_refused(
                stack["port"], expired, _link_key(expired).public_hex,
            )
        finally:
            connector.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    asyncio.run(run())


def test_probe_confirms_live_link_and_flags_dead_grant(stack):
    """The publisher's own end-to-end probe over the real tunnel.

    With the connector serving: a cached-and-valid token probes LIVE (200,
    content_length matches the artifact) — and it never transfers the body.
    A token the registry vouches for but the dashboard hasn't cached probes
    NOT live with a 404 status: the tunnel is up, the grant is dead — a
    distinct verdict from an unreachable tunnel.
    """
    binder_rev = design_db.create_design(
        title="OSS Insights binder",
        variants=[{"id": "v1", "html": BINDER_HTML}],
    )
    granted = publish_link(stack["db"], binder_rev)
    registry_only = publish_link(stack["db"], binder_rev)
    cache_grant(granted, binder_rev)

    relay = f"ws://127.0.0.1:{stack['port']}"

    async def run():
        connector = stack["connector"]
        task = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(connector.connected.wait(), timeout=15)

            live = await probe_link(
                relay_url=relay, token=granted,
                link_pub=_link_key(granted).public_hex, org_uuid=ORG_UUID,
                operation="head",
                total_timeout=15.0,
            )
            assert live["live"] is True, live
            assert live["status"] == 200
            assert live["content_length"] == len(BINDER_BYTES)  # headers-only

            # Tunnel up, grant absent: authentication never completes, so
            # there is deliberately no application status.
            dead = await probe_link(
                relay_url=relay, token=registry_only,
                link_pub=_link_key(registry_only).public_hex, org_uuid=ORG_UUID,
                operation="head",
                total_timeout=15.0,
            )
            assert dead["live"] is False, dead
            assert dead["status"] is None
        finally:
            connector.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    asyncio.run(run())


def _provision_serve_cert(tmp_path, root, port):
    """Mint the personal scope's root-signed tunnel:serve delegate, seal its
    key into the machine vault, and store this machine's serve-cert row + binding.

    Revision 2 is correct HERE and only here: an org scope's root-signed row
    now reports legacy-root-signed and will not launch a connector."""
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 300, not_after=now + 30 * 24 * 3600,
    )
    viewer_cert = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", delegate.public_hex),
        not_before=cert.not_before, not_after=cert.not_after,
    )
    # The key is a machine-vault row released into ramfs at launch; the
    # certificates are this machine's serve-cert row (graph://67d0aa5f-885).
    vault_kit.store_key(ORG_UUID, delegate.private_hex)
    vault_kit.store_row(
        ORG_UUID, cert=cert.to_json().decode("ascii"),
        viewer_cert=viewer_cert.to_json().decode("ascii"),
        child_pub=delegate.public_hex, not_after=cert.not_after,
        root_pub=root.public_hex,
    )
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "auto.network",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "registry_url": f"http://127.0.0.1:{port}",
         "recovery_policy": {"mode": "none"},
         "binding_expires_at": time.strftime(ISO, time.gmtime(now + 30 * 86400))},
        org=ORG,
    )


def _enroll_fleet_machine(
    tmp_path, monkeypatch, root, *, org_uuid=ORG_UUID,
) -> Path:
    """Make this installation an enrolled, serving-permitted fleet machine
    and warm the connector's runtime cache for ORG_UUID.

    The spawned connector has ONE source of machine identity: the warm
    runtime cache, keyed by its ``--org``, verified against the personal
    root and the roster (bc79dc9a deleted the anonymous fallback). So the
    subprocess needs, in the stores it inherits through AUTONOMY_ORGS_DIR:
    the personal root, one active roster entry, this machine's own identity
    row (the serving permit re-validates roster membership), and a cached
    credential whose reachability half carries the machine key the v2 hello
    co-signs with. Returns the connector's log path for diagnostics."""
    with settings_ops.identity_write_context():
        settings_ops.add_setting(
            PERSONAL_IDENTITY_SET_ID, PERSONAL_IDENTITY_REVISION, "default",
            {
                "armored_private_key": mint_password_armor(
                    root, "test-passphrase", iterations=10_000),
                "root_pub": root.public_hex,
                "display_name": "Tunnel Suite",
                "created_at": "2026-09-10T00:00:00Z",
            },
            org=None, state="raw",
        )
    # A temp dir stands in for the ramfs key cache, as the warm-cache suite
    # does; the guard itself is memory_cache's proof. The subprocess reads
    # the same directory through the inherited env (see _ramfs_free_spawn).
    keycache = tmp_path / "keycache"
    keycache.mkdir()
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(keycache))
    monkeypatch.setattr(
        "tools.network.storagekit.memory_cache.assert_memory_backed",
        lambda *a, **k: None,
    )
    _provision_fleet_runtime(personal_root=root, org_uuid=org_uuid)
    return tmp_path / "network" / "serve.log"



def _provision_fleet_runtime(*, personal_root, org_uuid: str) -> None:
    """Enroll one synthetic machine and arm its organization connector.

    Moved here from the retired deploy.harness fixture, which this was the
    only remaining caller of. It writes the roster entry, the machine
    identity and the registry binding, then arms the connector's warm cache
    so the real connector subprocess below starts armed."""
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_identity import (
        MACHINE_IDENTITY_KEY,
        MACHINE_IDENTITY_REVISION,
        MACHINE_IDENTITY_SET_ID,
    )
    from tools.graph.schemas.network_identity import (
        NETWORK_BINDING_REVISION,
        NETWORK_BINDING_SET_ID,
    )
    from tools.network import fleet_roster
    from tools.network.fleet_relay_sync import FleetRuntimeWarmCache
    from tools.network.idkit import KeyPair, Subject, issue_cert

    machine = KeyPair.generate()
    process = KeyPair.generate()
    machine_id = machine.public_hex
    now = int(time.time())
    fleet_roster.store_entry(
        fleet_roster.enroll(
            personal_root,
            machine_id=machine_id,
            machine_pub=machine.public_hex,
        ),
        org=None,
    )
    settings_ops.upsert_by_key(
        MACHINE_IDENTITY_SET_ID,
        MACHINE_IDENTITY_REVISION,
        MACHINE_IDENTITY_KEY,
        {"machine_id": machine_id},
        org="machine",
        state="raw",
    )
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "fixture-runtime",
        {
            "org_uuid": org_uuid,
            "root_pub": personal_root.public_hex,
            "registry_url": "http://relay:8477",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 24 * 60 * 60)
            ),
        },
        org="personal",
        state="raw",
    )
    delegation = issue_cert(
        machine,
        process.public_hex,
        scope=("fleet:sync",),
        org=f"personal:{personal_root.public_hex}",
        subject=Subject("machine", machine_id),
        not_before=now - 30,
        not_after=now + 3600,
    )
    reachability = issue_cert(
        personal_root,
        machine.public_hex,
        scope=("node:announce", "node:lookup"),
        org=org_uuid,
        subject=Subject("machine", machine_id),
        not_before=now - 30,
        not_after=now + 3600,
    )
    FleetRuntimeWarmCache(org_uuid).store({
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": delegation.to_dict(),
        "machine_private_seed": machine.private_hex,
        "reachability_cert": reachability.to_dict(),
    })


# The real connector, in a real subprocess, with exactly one thing
# neutered: the ramfs guard on the warm cache it re-arms from, since the
# cache above lives in a temp dir. `-c` cannot be `-m`, so the argv the
# supervisor built is re-entered through runpy with the same arguments.
_CONNECTOR_BOOTSTRAP = (
    "import runpy; "
    "from tools.network.storagekit import memory_cache; "
    "memory_cache.assert_memory_backed = lambda *a, **k: None; "
    "runpy.run_module('tools.dashboard.link_serving', "
    "run_name='__main__', alter_sys=True)"
)


def _ramfs_free_spawn(argv, env, **kwargs):
    assert argv[1:3] == ["-m", "tools.dashboard.link_serving"], argv
    return sup._default_spawn(
        [argv[0], "-c", _CONNECTOR_BOOTSTRAP, *argv[3:]], env, **kwargs)


def test_supervisor_brings_serving_live_end_to_end(stack, tmp_path, monkeypatch):
    """The whole point: given a provisioned serve-cert, the supervisor spawns
    the REAL connector subprocess and the freshly published link goes live —
    proven by the publisher's own probe, headers-only."""
    # The spawned connector is a SEPARATE process: it inherits the orgs tree
    # (AUTONOMY_ORGS_DIR — its grant cache) from the env, but the designs DB
    # is a module attr the stack fixture only monkeypatched in-process — so
    # point the subprocess at the same tmp designs DB via the env var
    # design_db reads.
    monkeypatch.setenv("EXPERIMENTS_DB", str(design_db.DB_PATH))
    binder_rev = design_db.create_design(
        title="OSS Insights binder",
        variants=[{"id": "v1", "html": BINDER_HTML}],
    )
    token = publish_link(stack["db"], binder_rev)
    link_key = KeyPair.generate()
    cache_grant(token, binder_rev, channel_pub=link_key.public_hex)
    # Only the dashboard holds the key; the REAL child must ask its resolver.
    from tools.dashboard import link_channel_key
    monkeypatch.setattr(link_channel_key, "channel_key_for", lambda *args: link_key)
    # The serving key is a machine-vault row: publish the audited recipient
    # and warm the delegate as a sign-on would, so the supervisor can release
    # the key into the (stand-in) ramfs at launch.
    vault_kit.warm(vault_kit.publish_recipient(monkeypatch))
    _provision_serve_cert(tmp_path, stack["root"], stack["port"])
    connector_log = _enroll_fleet_machine(tmp_path, monkeypatch, stack["root"])

    supervisor = sup.ServingSupervisor(spawn=_ramfs_free_spawn)
    relay = f"ws://127.0.0.1:{stack['port']}"
    try:
        # The post-publish trigger: reconcile → launch the connector subprocess.
        result = supervisor.ensure(ORG)
        assert result == {"running": True, "reason": "launched"}, result

        async def run():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            verdict = None
            while loop.time() < deadline:
                try:
                    channel = await ViewerChannel.connect(
                        relay, token, link_pub=link_key.public_hex,
                        org=ORG_UUID,
                    )
                    try:
                        await channel.send_message(json.dumps({"v": 1, "op": "fetch"}).encode())
                        response = await channel.recv_message()
                        header, _, body = response.partition(b"\n")
                        assert json.loads(header)["status"] == "ok"
                        assert body == BINDER_BYTES
                    finally:
                        await channel.close()
                    verdict = {"live": True, "content_length": len(body)}
                    break
                except Exception as exc:
                    verdict = {"live": False, "error": str(exc)}
                await asyncio.sleep(0.5)
            # The connector's own log says why it never came live; the probe
            # can only report that the relay had no tunnel to hand it.
            log_tail = (connector_log.read_text()[-3000:]
                        if connector_log.exists() else "<no connector log>")
            assert verdict and verdict["live"] is True, (verdict, log_tail)
            assert verdict["content_length"] == len(BINDER_BYTES)  # headers-only

        asyncio.run(run())
    finally:
        supervisor.stop_all()


def test_probe_reports_unreachable_when_no_connector(stack):
    """No serving tunnel dialed in (connector never started) → the probe
    reports NOT live with a None status: the honest 'dashboard offline'
    verdict, distinct from a dead-grant 404. Bounded fast by the budget."""
    binder_rev = design_db.create_design(
        title="OSS Insights binder",
        variants=[{"id": "v1", "html": BINDER_HTML}],
    )
    token = publish_link(stack["db"], binder_rev)
    cache_grant(token, binder_rev)  # local grant is fine — the TUNNEL is down
    relay = f"ws://127.0.0.1:{stack['port']}"

    async def run():
        # connector deliberately NOT started.
        verdict = await probe_link(
            relay_url=relay, token=token,
            link_pub=_link_key(token).public_hex, org_uuid=ORG_UUID,
            operation="head",
            total_timeout=4.0, connect_timeout=1.5,
        )
        assert verdict["live"] is False, verdict
        assert verdict["status"] is None, verdict

    asyncio.run(run())


def test_per_link_key_serves_end_to_end(stack, monkeypatch):
    """The keystone loop for graph://807b4e11-3e9, on the REAL stack: a link
    whose grant carries a channel key serves over the per-link handshake (the
    viewer verifies the fragment key, no root pin), and revoking either the
    grant or private key prevents a channel from authenticating."""
    from tools.dashboard import link_channel_key as lck
    from tools.network.idkit import KeyPair as _KP

    binder_rev = design_db.create_design(
        title="OSS Insights binder",
        variants=[{"id": "v1", "html": BINDER_HTML}],
    )
    keyed = publish_link(stack["db"], binder_rev)

    # The keyed link: grant row carries channel_pub; the private seed lives
    # behind the link_channel_key settings seam (in-memory here — the vault
    # sealing itself is the vault suite's proof; this test proves the loop).
    link_key = _KP.generate()
    cache_grant(keyed, binder_rev, channel_pub=link_key.public_hex)
    seeds = {keyed: link_key.private_hex}
    monkeypatch.setattr(
        lck.settings_ops, "read_set_key",
        lambda set_id, key, *, org=None, peers=None:
            ({"payload": {"seed": seeds[key]}} if key in seeds else None))

    # Wire the resolver exactly as link_serving wires it in production.
    stack["connector"]._channel_authorization_for = lambda token: {
        "protocol": "public-link", "key": lck.channel_key_for(token, ORG),
    }

    async def run():
        connector = stack["connector"]
        task = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(connector.connected.wait(), timeout=15)

            # 1. The keyed link serves against the FRAGMENT key — no root pin.
            channel = await ViewerChannel.connect(
                f"ws://127.0.0.1:{stack['port']}", keyed,
                link_pub=link_key.public_hex, org=ORG_UUID,
            )
            await channel.send_message(json.dumps(
                {"v": 1, "op": "fetch"}).encode())
            served = await channel.recv_message()
            header, _, body = served.partition(b"\n")
            assert json.loads(header)["status"] == "ok"
            assert body == BINDER_BYTES
            await channel.close()

            # Revoke: grant row and seed die; the keyed link stops serving
            #    even for a viewer still holding the fragment.
            for member in settings_ops.read_owned_set(
                    NETWORK_LINK_GRANT_SET_ID, org=ORG,
                    target_revision=NETWORK_LINK_GRANT_REVISION).members:
                if member.key == keyed:
                    settings_ops.remove_setting(member.id, org=ORG)
            del seeds[keyed]
            with pytest.raises(AssertionError, match="viewer could not connect"):
                await fetch_over_tunnel(
                    stack["port"], keyed, link_key.public_hex, timeout=2.0)
        finally:
            connector.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    asyncio.run(run())
