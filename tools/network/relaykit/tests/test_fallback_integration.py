"""G1 acceptance: the full fallback chain, three real processes.

Topology (all real ``__main__`` entrypoints, like the B2 suite)::

    dialer (test process)
       │
       ├─ rung 1: direct ws ──────────▶ node (subprocess: relaykit.node)
       ├─ rung 2: peer relay ws ─────▶ peer relay (subprocess: relaykit.peer)
       │                                  │ parked tunnel
       │                                  ▼
       └─ rung 3: central floor ─────▶ registry (subprocess: registry)
                                          │ B2 tunnel
                                          ▼
                                        node

One org, one root key shared by the authority ledger and the registry
binding (the real-world shape). The node announces its reachability
hints to the registry and parks at both the peer relay and the floor;
the dialer discovers everything through the registry + a real ledger
fold and walks the chain:

- direct path (control) — hints-discovered address carries the channel;
- direct blocked (simulated NAT: the candidate is a black hole) — the
  ledger∩hints peer relay carries the E2E channel intact;
- peer relay ALSO dead (SIGKILL) — the auto.network floor carries it.

The SAME payload crosses on every path — one digest, three transports.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger.projections import build_live_keys
from tools.network.ledger.testkit import OrgSim
from tools.network.relaykit.dialer import (
    PATH_DIRECT,
    PATH_FLOOR,
    PATH_PEER_RELAY,
    dial_peer,
    fetch_hints,
    relay_candidates,
)

from .test_relay_integration import free_port, register_org_and_link, start_registry

REPO = Path(__file__).resolve().parents[4]
ORG = "77777777-7777-4777-8777-777777777777"
CANARY = b"AUTONOMY_PLAINTEXT_CANARY_7f3a9c" * 2


def _write_identity(tmp, name: str, key: KeyPair, cert) -> tuple:
    key_file, cert_file = tmp / f"{name}.hex", tmp / f"{name}.cert"
    key_file.write_text(key.private_hex)
    cert_file.write_text(cert.to_json().decode("ascii"))
    return key_file, cert_file


def _blackhole_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fallback-stack")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    now = int(time.time())

    # One root anchors everything: the ledger genesis, the registry
    # binding, and every idkit chain.
    sim = OrgSim(ORG)
    root = sim.root
    node_key, floor_key, relay_key = (
        KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    )

    # The authority ledger records who may do what; relay:serve on the
    # relay node is THE grant this bead is about.
    ledger = sim.store()
    sim.delegate(ledger, child=relay_key,
                 scope=("node:announce", "relay:serve", "tunnel:serve"))
    sim.delegate(ledger, child=node_key,
                 scope=("node:announce", "node:lookup", "tunnel:serve"))
    live_keys = build_live_keys(ledger.fold())

    # idkit chains mirroring the ledger grants (the wire-presentable form).
    relay_cert = issue_cert(
        root, relay_key.public_hex,
        scope=("node:announce", "relay:serve", "tunnel:serve"),
        org=ORG, subject=Subject("agent", "relay-node"),
        not_before=now - 300, not_after=now + 7 * 86_400,
    )
    node_cert = issue_cert(
        root, node_key.public_hex,
        scope=("node:announce", "node:lookup", "tunnel:serve"),
        org=ORG, subject=Subject("agent", "target-node"),
        not_before=now - 300, not_after=now + 7 * 86_400,
    )
    floor_cert = issue_cert(
        root, floor_key.public_hex,
        scope=("tunnel:serve",),
        org=ORG, subject=Subject("persona", "ab" * 32),
        not_before=node_cert.not_before, not_after=node_cert.not_after,
    )
    floor_channel_cert = issue_cert(
        root, floor_key.public_hex,
        scope=("tunnel:serve",),
        org=ORG, subject=Subject("operator", floor_key.public_hex),
        not_before=node_cert.not_before, not_after=node_cert.not_after,
    )

    registry_port = free_port()
    registry = start_registry(registry_port, tmp / "registry.db", env,
                              tmp / "registry.log")
    floor_token = register_org_and_link(registry_port, root, ORG)

    relay_port, listen_port = free_port(), free_port()
    node_kf, node_cf = _write_identity(tmp, "node", node_key, node_cert)
    floor_kf, floor_cf = _write_identity(tmp, "node-floor", floor_key, floor_cert)
    _floor_channel_kf, floor_channel_cf = _write_identity(
        tmp, "node-floor-channel", floor_key, floor_channel_cert)
    relay_kf, relay_cf = _write_identity(tmp, "relay", relay_key, relay_cert)

    peer_relay = subprocess.Popen(
        [sys.executable, "-m", "tools.network.relaykit.peer",
         "--org", ORG, "--root-pub", root.public_hex,
         "--key-file", str(relay_kf), "--cert-file", str(relay_cf),
         "--port", str(relay_port)],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "peer-relay.log", "ab"), stderr=subprocess.STDOUT,
    )
    node = subprocess.Popen(
        [sys.executable, "-m", "tools.network.relaykit.node",
         "--org", ORG, "--root-pub", root.public_hex,
         "--key-file", str(node_kf), "--cert-file", str(node_cf),
         "--floor-key-file", str(floor_kf),
         "--floor-cert-file", str(floor_cf),
         "--floor-channel-cert-file", str(floor_channel_cf),
         "--listen-port", str(listen_port),
         "--floor", f"ws://127.0.0.1:{registry_port}",
         "--peer-relay", f"ws://127.0.0.1:{relay_port}",
         "--registry", f"http://127.0.0.1:{registry_port}",
         "--announce-addr", f"ws://127.0.0.1:{listen_port}",
         "--announce-ttl", "600",
         "--min-backoff", "0.1", "--max-backoff", "1.0"],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "node.log", "ab"), stderr=subprocess.STDOUT,
    )

    # The relay announces its dial URL under ITS key (hint row the
    # dialer will intersect with the ledger's relay:serve holders).
    import httpx

    from tools.network.registry.signing import sign_request

    path = f"/v1/orgs/{ORG}/reachability"
    envelope = sign_request(relay_key, "POST", path,
                            {"addrs": [], "relay_url": f"ws://127.0.0.1:{relay_port}",
                             "ttl": 600},
                            ts=int(time.time()), cert=relay_cert)
    httpx.post(f"http://127.0.0.1:{registry_port}{path}", json=envelope,
               timeout=10.0).raise_for_status()

    # Readiness: the node's own announcement appearing in the registry
    # means its announce loop (and thus its event loop) is running.
    deadline = time.time() + 20
    while time.time() < deadline:
        hints = fetch_hints(f"http://127.0.0.1:{registry_port}", ORG, node_key,
                            node_cert, node=node_key.public_hex)
        if hints:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(
            f"node never announced; node log: {(tmp / 'node.log').read_text()[-2000:]}"
        )

    state = {
        "tmp": tmp,
        "root_pub": root.public_hex,
        "node_pub": node_key.public_hex,
        "node_key": node_key,
        "node_cert": node_cert,
        "live_keys": live_keys,
        "registry_port": registry_port,
        "relay_port": relay_port,
        "listen_port": listen_port,
        "floor_token": floor_token,
        "procs": {"registry": registry, "peer_relay": peer_relay, "node": node},
        "digests": {},
    }
    yield state
    for proc in state["procs"].values():
        with contextlib.suppress(Exception):
            proc.terminate()
    for proc in state["procs"].values():
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _payload() -> bytes:
    # deterministic: identical bytes must cross every path
    return CANARY + hashlib.sha256(b"g1").digest() * 12_000 + CANARY


async def _dial_with_retry(deadline_s=20.0, **kwargs):
    """Retry the whole chain until the stack is warm (parks + tunnels
    are established by background subprocesses)."""
    deadline = time.time() + deadline_s
    last = None
    while time.time() < deadline:
        try:
            return await dial_peer(**kwargs)
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.3)
    raise AssertionError(f"dial did not succeed within {deadline_s}s: {last!r}")


def _run_path(state, expected_path, **dial_kwargs):
    payload = _payload()

    async def run():
        result = await _dial_with_retry(
            org=ORG, root_pub=state["root_pub"], target_pub=state["node_pub"],
            **dial_kwargs,
        )
        assert result.path == expected_path, result.attempts
        async with result.channel as channel:
            await channel.send_message(payload)
            echoed = await channel.recv_message()
        return result, echoed

    result, echoed = asyncio.run(run())
    assert echoed == payload
    state["digests"][expected_path] = hashlib.sha256(echoed).hexdigest()
    return result


class TestFallbackChain:
    """Ordered: each test knocks out the previous rung."""

    def test_discovery_through_registry_and_ledger(self, stack):
        hints = fetch_hints(f"http://127.0.0.1:{stack['registry_port']}", ORG,
                            stack["node_key"], stack["node_cert"])
        by_node = {h["node"]: h for h in hints}
        assert stack["node_pub"] in by_node
        assert by_node[stack["node_pub"]]["addrs"] == [
            f"ws://127.0.0.1:{stack['listen_port']}"
        ]
        relays = relay_candidates(stack["live_keys"], hints)
        assert relays == [{"node": relays[0]["node"],
                           "relay_url": f"ws://127.0.0.1:{stack['relay_port']}"}]
        stack["hints"] = hints
        stack["relays"] = relays

    def test_rung1_direct(self, stack):
        target_hint = next(h for h in stack["hints"]
                           if h["node"] == stack["node_pub"])
        result = _run_path(
            stack, PATH_DIRECT,
            direct_addrs=target_hint["addrs"],
            relays=stack["relays"],
            floor=(f"ws://127.0.0.1:{stack['registry_port']}", stack["floor_token"]),
        )
        assert result.attempts == []

    def test_rung2_direct_blocked_peer_relay_carries(self, stack):
        """Simulated NAT: the direct candidate is a black hole; the org's
        own relay carries the channel intact."""
        result = _run_path(
            stack, PATH_PEER_RELAY,
            direct_addrs=[f"ws://127.0.0.1:{_blackhole_port()}"],
            relays=stack["relays"],
            floor=(f"ws://127.0.0.1:{stack['registry_port']}", stack["floor_token"]),
            attempt_timeout=2.0,
        )
        assert [a[0] for a in result.attempts] == [PATH_DIRECT]

    def test_rung3_peer_relay_dead_floor_carries(self, stack):
        stack["procs"]["peer_relay"].kill()
        stack["procs"]["peer_relay"].wait(timeout=5)

        result = _run_path(
            stack, PATH_FLOOR,
            direct_addrs=[f"ws://127.0.0.1:{_blackhole_port()}"],
            relays=stack["relays"],
            floor=(f"ws://127.0.0.1:{stack['registry_port']}", stack["floor_token"]),
            attempt_timeout=2.0,
        )
        assert {a[0] for a in result.attempts} == {PATH_DIRECT, PATH_PEER_RELAY}

    def test_same_payload_hash_on_all_three_paths(self, stack):
        digests = stack["digests"]
        assert set(digests) == {PATH_DIRECT, PATH_PEER_RELAY, PATH_FLOOR}
        assert len(set(digests.values())) == 1
