"""C4 acceptance on the B2 harness: grant-gated serving over the real tunnel.

Topology: a real registry+relay subprocess, the real ``TunnelConnector``
running in-process with the C4 grant handler, and the real
``ViewerChannel`` E2E client. Three tokens, all known to the REGISTRY
(it opens a viewer channel for each), only differing in the dashboard's
LOCAL grant cache:

* cached + valid   → the binder-sized Present fixture streams through
  the E2E channel and matches byte length exactly;
* absent from the cache → REFUSED, even though the relay dutifully
  requested it — a registry compromise alone opens nothing (I9 pinned);
* cached but expired (``meta.ttl``) → REFUSED, byte-identical to the
  absent case.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from tools.dashboard.link_probe import probe_link
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
    NETWORK_SERVE_CERT_REVISION,
    NETWORK_SERVE_CERT_SET_ID,
)
from tools.network.idkit import Subject, issue_cert
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

REPO = Path(__file__).resolve().parents[3]
ORG = "netorg"
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


def start_registry(port: int, db: Path, log: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry",
         "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
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


def publish_link(port: int, root: KeyPair, target_uuid: str) -> str:
    """Root-direct link publish on the registry; returns the minted token."""
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        response = client.post("/v1/links", json=sign_request(
            root, "POST", "/v1/links",
            {"org": ORG_UUID, "target_uuid": target_uuid, "target_type": "present"},
            ts=int(time.time()),
        ))
        assert response.status_code == 201, response.text
        return response.json()["token"]


def cache_grant(token: str, target_uuid: str, *, meta: dict | None = None,
                issued_at: str | None = None) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "target_uuid": target_uuid,
            "target_type": "present",
            "meta": meta or {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": issued_at or time.strftime(ISO, time.gmtime()),
        },
        org=ORG,
    )


async def fetch_over_tunnel(port: int, token: str, root_pub: str,
                            timeout: float = 20.0) -> bytes:
    """Connect a viewer, run the E2E handshake, fetch once."""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            channel = await ViewerChannel.connect(
                f"ws://127.0.0.1:{port}", token, root_pub=root_pub, org=ORG_UUID,
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


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Registry subprocess + in-process C4-serving connector + tmp stores."""
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "designs.db")
    monkeypatch.setattr(design_db, "_initialized", False)

    root = KeyPair.generate()
    session_key = KeyPair.generate()
    now = int(time.time())
    session_cert = issue_cert(
        root, session_key.public_hex,
        scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", "op-session-1"),
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

        connector = TunnelConnector(
            f"ws://127.0.0.1:{port}", ORG_UUID, session_key, session_cert,
            handler=link_serving.make_grant_handler(ORG),
            min_backoff=0.1, max_backoff=1.0,
        )
        yield {"port": port, "root": root, "root_pub": root.public_hex,
               "connector": connector}
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
    granted = publish_link(stack["port"], stack["root"], binder_rev)
    registry_only = publish_link(stack["port"], stack["root"], binder_rev)
    expired = publish_link(stack["port"], stack["root"], binder_rev)
    cache_grant(granted, binder_rev)
    cache_grant(expired, binder_rev, meta={"ttl": 60},
                issued_at=time.strftime(ISO, time.gmtime(time.time() - 3600)))

    async def run():
        connector = stack["connector"]
        task = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(connector.connected.wait(), timeout=15)

            served = await fetch_over_tunnel(
                stack["port"], granted, stack["root_pub"])
            header, _, body = served.partition(b"\n")
            assert json.loads(header) == {
                "v": 1, "status": "ok", "kind": "present",
                "viewer": {"offset": 0, "length": len(BINDER_BYTES)},
                "branding": {
                    "name": "netorg", "color": "#118AB2", "initial": "N",
                },
            }
            assert len(body) == len(BINDER_BYTES)  # binder-sized, byte-exact
            assert body == BINDER_BYTES

            # I9 pinned: the REGISTRY vouches for this token and the relay
            # opens the channel — but with no local grant, nothing serves.
            refused = await fetch_over_tunnel(
                stack["port"], registry_only, stack["root_pub"])
            assert refused == link_serving.REFUSED

            # Expired local grant → same refusal, byte-identical.
            stale = await fetch_over_tunnel(
                stack["port"], expired, stack["root_pub"])
            assert stale == link_serving.REFUSED
            assert stale == refused
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
    granted = publish_link(stack["port"], stack["root"], binder_rev)
    registry_only = publish_link(stack["port"], stack["root"], binder_rev)
    cache_grant(granted, binder_rev)

    relay = f"ws://127.0.0.1:{stack['port']}"

    async def run():
        connector = stack["connector"]
        task = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(connector.connected.wait(), timeout=15)

            live = await probe_link(
                relay_url=relay, token=granted,
                root_pub=stack["root_pub"], org_uuid=ORG_UUID,
                total_timeout=15.0,
            )
            assert live["live"] is True, live
            assert live["status"] == 200
            assert live["content_length"] == len(BINDER_BYTES)  # headers-only

            # Tunnel up (handshake succeeds), grant absent → not live, 404.
            dead = await probe_link(
                relay_url=relay, token=registry_only,
                root_pub=stack["root_pub"], org_uuid=ORG_UUID,
                total_timeout=15.0,
            )
            assert dead["live"] is False, dead
            assert dead["status"] == 404
        finally:
            connector.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    asyncio.run(run())


def _provision_serve_cert(tmp_path, root, port):
    """Mint a real root-signed tunnel:serve delegate, write its 0600 key file,
    and store the serve-cert row + binding — what the provision endpoint will
    do once the browser dual-mint lands."""
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", "op-serve"),
        not_before=now - 300, not_after=now + 30 * 24 * 3600,
    )
    keydir = tmp_path / "network"
    keydir.mkdir(exist_ok=True)
    key_path = keydir / "serve.key"
    key_path.write_text(delegate.private_hex)
    os.chmod(key_path, 0o600)
    settings_ops.add_setting(
        NETWORK_SERVE_CERT_SET_ID, NETWORK_SERVE_CERT_REVISION, "default",
        {"cert": cert.to_json().decode("ascii"), "key_path": str(key_path),
         "root_pub": root.public_hex, "not_after": cert.not_after},
        org=ORG,
    )
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "auto.network",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "registry_url": f"http://127.0.0.1:{port}",
         "recovery_policy": {"mode": "none"},
         "binding_expires_at": time.strftime(ISO, time.gmtime(now + 30 * 86400))},
        org=ORG,
    )


def test_supervisor_brings_serving_live_end_to_end(stack, tmp_path, monkeypatch):
    """The whole point: given a provisioned serve-cert, the supervisor spawns
    the REAL connector subprocess and the freshly published link goes live —
    proven by the publisher's own probe, headers-only."""
    # The spawned connector is a SEPARATE process: it inherits GRAPH_DB (its
    # grant cache) from the env, but the designs DB is a module attr the stack
    # fixture only monkeypatched in-process — so point the subprocess at the
    # same tmp designs DB via the env var design_db reads.
    monkeypatch.setenv("EXPERIMENTS_DB", str(design_db.DB_PATH))
    binder_rev = design_db.create_design(
        title="OSS Insights binder",
        variants=[{"id": "v1", "html": BINDER_HTML}],
    )
    token = publish_link(stack["port"], stack["root"], binder_rev)
    cache_grant(token, binder_rev)
    _provision_serve_cert(tmp_path, stack["root"], stack["port"])

    supervisor = sup.ServingSupervisor()
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
                verdict = await probe_link(
                    relay_url=relay, token=token, root_pub=stack["root_pub"],
                    org_uuid=ORG_UUID, total_timeout=4.0, connect_timeout=2.0,
                    attempts=1,
                )
                if verdict["live"]:
                    break
                await asyncio.sleep(0.5)
            assert verdict and verdict["live"] is True, verdict
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
    token = publish_link(stack["port"], stack["root"], binder_rev)
    cache_grant(token, binder_rev)  # local grant is fine — the TUNNEL is down
    relay = f"ws://127.0.0.1:{stack['port']}"

    async def run():
        # connector deliberately NOT started.
        verdict = await probe_link(
            relay_url=relay, token=token,
            root_pub=stack["root_pub"], org_uuid=ORG_UUID,
            total_timeout=4.0, connect_timeout=1.5,
        )
        assert verdict["live"] is False, verdict
        assert verdict["status"] is None, verdict

    asyncio.run(run())
