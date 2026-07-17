"""B2 acceptance: two-process relay stack, driven end to end.

Topology::

    viewer (test process)
       │ ws
       ▼
    registry+relay (subprocess, real `python -m tools.network.registry`)
       ▲ ws
       │
    tcp tap (subprocess — records every tunnel-wire byte)
       ▲ ws
       │
    connector (subprocess, real `python -m tools.network.relaykit.connector`)

- 1.55 MB soak: binder-sized payload echoes byte-exact through the
  E2E channel (Q3).
- Ciphertext wire: a plaintext canary present in channel messages never
  appears in the tapped tunnel bytes; the link TOKEN (accepted §5.2
  metadata) does — proving the tap actually sees the traffic.
- Reconnect: SIGKILL the registry, restart it, and the connector
  re-establishes the tunnel; the next viewer connects fine.
- MITM (I5): an actively hostile relay substituting either side's ECDH
  key breaks the handshake; the same hostile relay in passthrough mode
  works — the failure is the attack being caught, not test plumbing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets

from tools.network.idkit import KeyPair
from tools.network.registry.signing import sign_request
from tools.network.relaykit.channel import HandshakeError
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

from .conftest import ORG, TOKEN
from .evil_relay import EvilRelay

REPO = Path(__file__).resolve().parents[4]
TESTS = Path(__file__).resolve().parent
TARGET = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CANARY = b"AUTONOMY_PLAINTEXT_CANARY_7f3a9c" * 2


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_registry(port: int, db: Path, env: dict, log: Path) -> subprocess.Popen:
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


def register_org_and_link(port: int, root: KeyPair, org: str) -> str:
    """Register *org* (root-direct) and publish one link; returns the token."""
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        ts = int(time.time())
        response = client.post("/v1/orgs", json=sign_request(
            root, "POST", "/v1/orgs",
            {"org_uuid": org, "root_pub": root.public_hex, "recovery_policy": "none"},
            ts=ts,
        ))
        assert response.status_code == 201, response.text
        response = client.post("/v1/links", json=sign_request(
            root, "POST", "/v1/links",
            {"org": org, "target_uuid": TARGET, "target_type": "present"},
            ts=ts,
        ))
        assert response.status_code == 201, response.text
        return response.json()["token"]


@pytest.fixture(scope="module")
def stack(tmp_path_factory, root, session_key, session_cert):
    tmp = tmp_path_factory.mktemp("relay-stack")
    registry_port, tap_port = free_port(), free_port()
    db = tmp / "registry.db"
    env = {**os.environ, "PYTHONPATH": str(REPO)}

    registry = start_registry(registry_port, db, env, tmp / "registry.log")
    token = register_org_and_link(registry_port, root, ORG)

    c2s, s2c = tmp / "c2s.bin", tmp / "s2c.bin"
    tap = subprocess.Popen(
        [sys.executable, str(TESTS / "tcp_tap.py"),
         str(tap_port), str(registry_port), str(c2s), str(s2c)],
        cwd=str(REPO), env=env,
    )

    key_file, cert_file = tmp / "session.hex", tmp / "session.cert"
    key_file.write_text(session_key.private_hex)
    cert_file.write_text(session_cert.to_json().decode("ascii"))
    connector = subprocess.Popen(
        [sys.executable, "-m", "tools.network.relaykit.connector",
         "--relay", f"ws://127.0.0.1:{tap_port}", "--org", ORG,
         "--key-file", str(key_file), "--cert-file", str(cert_file),
         "--min-backoff", "0.1", "--max-backoff", "1.0"],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "connector.log", "ab"), stderr=subprocess.STDOUT,
    )

    state = {
        "tmp": tmp, "env": env, "db": db, "token": token,
        "registry_port": registry_port, "tap_port": tap_port,
        "root_pub": root.public_hex, "c2s": c2s, "s2c": s2c,
        "procs": {"registry": registry, "tap": tap, "connector": connector},
    }
    yield state
    for proc in state["procs"].values():
        with contextlib.suppress(Exception):
            proc.terminate()
    for proc in state["procs"].values():
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


async def connect_viewer(state, token=None, timeout=20.0) -> ViewerChannel:
    """Retry until the connector's tunnel is up and the handshake lands."""
    deadline = time.time() + timeout
    last: Exception = None
    while time.time() < deadline:
        try:
            return await ViewerChannel.connect(
                f"ws://127.0.0.1:{state['registry_port']}",
                token or state["token"],
                root_pub=state["root_pub"], org=ORG,
            )
        except HandshakeError:
            raise  # never retry a failed pin — that's the whole point
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.25)
    raise AssertionError(f"viewer could not connect within {timeout}s: {last!r}")


async def close_code_for(state, token) -> int:
    try:
        channel = await ViewerChannel.connect(
            f"ws://127.0.0.1:{state['registry_port']}", token,
            root_pub=state["root_pub"], org=ORG,
        )
    except websockets.exceptions.ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else -1
    await channel.close()
    raise AssertionError("connection unexpectedly succeeded")


class TestRelayStack:
    def test_small_echo_roundtrip(self, stack):
        async def run():
            async with await connect_viewer(stack) as channel:
                await channel.send_message(b"ping through the fabric")
                assert await channel.recv_message() == b"ping through the fabric"
        asyncio.run(run())

    def test_soak_1_55mb_and_ciphertext_wire(self, stack):
        """Q3 soak + the I5 wire assertion in one flow."""
        payload = CANARY + os.urandom(1_550_000) + CANARY

        async def run():
            async with await connect_viewer(stack) as channel:
                await channel.send_message(payload)
                echoed = await channel.recv_message()
            assert echoed == payload
        asyncio.run(run())

        time.sleep(0.5)  # let the tap flush its last chunks
        s2c = stack["s2c"].read_bytes()  # registry -> connector (unmasked)
        c2s = stack["c2s"].read_bytes()  # connector -> registry (masked)
        assert len(s2c) > 1_550_000  # the payload really crossed this wire

        # Positive control: the tap sees real traffic — the token rides the
        # OPEN frame in the clear (accepted §5.2 metadata: token/org/timing).
        assert stack["token"].encode() in s2c
        # The canary crossed the tapped wire inside every viewer message and
        # every echo, and appears in neither direction: ciphertext only (I5).
        assert CANARY not in s2c
        assert CANARY not in c2s

    def test_anti_enumeration_uniform_close(self, stack, root):
        """Unknown token and valid-token-but-dashboard-offline close with
        the SAME code — a prober can't tell them apart (§5.3)."""
        offline_org = "66666666-6666-4666-8666-666666666666"
        offline_root = KeyPair.generate()
        offline_token = register_org_and_link(stack["registry_port"], offline_root, offline_org)

        async def run():
            unknown = await close_code_for(stack, "0" * 32)
            offline = await close_code_for(stack, offline_token)
            assert unknown == offline == 4404
        asyncio.run(run())

    def test_reconnect_after_relay_death(self, stack):
        """Kill the registry (the tunnel's far end), restart it, and the
        connector must re-establish — next viewer connects and echoes."""
        registry = stack["procs"]["registry"]
        registry.kill()
        registry.wait(timeout=5)

        stack["procs"]["registry"] = start_registry(
            stack["registry_port"], stack["db"], stack["env"],
            stack["tmp"] / "registry.log",
        )

        async def run():
            async with await connect_viewer(stack, timeout=30.0) as channel:
                await channel.send_message(b"back from the dead")
                assert await channel.recv_message() == b"back from the dead"
        asyncio.run(run())


class TestMitmI5:
    """The relay is the adversary. Same connector, same viewer — only the
    relay's behavior changes between control and attack runs."""

    def _run_against_evil_relay(self, mode, root, session_key, session_cert):
        async def run():
            relay = EvilRelay(mode=mode)
            port = await relay.start()
            connector = TunnelConnector(
                f"ws://127.0.0.1:{port}", ORG, session_key, session_cert,
                min_backoff=0.1, max_backoff=0.5,
            )
            task = asyncio.create_task(connector.run())
            try:
                await asyncio.wait_for(connector.connected.wait(), timeout=10)
                channel = await ViewerChannel.connect(
                    f"ws://127.0.0.1:{port}", TOKEN,
                    root_pub=root.public_hex, org=ORG,
                )
                try:
                    await channel.send_message(b"attack run probe")
                    return await channel.recv_message()
                finally:
                    await channel.close()
            finally:
                connector.stop()
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                await relay.stop()
        return asyncio.run(run())

    def test_passthrough_control(self, root, session_key, session_cert):
        """The evil relay faithfully forwarding works — so the failures
        below are the attacks being CAUGHT, not broken plumbing."""
        result = self._run_against_evil_relay("passthrough", root, session_key, session_cert)
        assert result == b"attack run probe"

    def test_relay_substituting_server_ecdh_key_fails(self, root, session_key, session_cert):
        with pytest.raises(HandshakeError):
            self._run_against_evil_relay("server_eph", root, session_key, session_cert)

    def test_relay_substituting_client_ecdh_key_fails(self, root, session_key, session_cert):
        with pytest.raises(HandshakeError):
            self._run_against_evil_relay("client_eph", root, session_key, session_cert)
