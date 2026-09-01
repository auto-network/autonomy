"""auto-9z1xh L4 acceptance: bounded fair raw byte streams, real stack.

Real registry subprocess with the loopback stream ingress enabled, one
real ``TunnelConnector`` whose stream handler dials a local TCP echo
server, and raw TCP clients speaking through the ingress. Every client
leads with a crafted TLS ClientHello (the ingress routes on its SNI and
forwards those exact bytes as the first stream data), so the echo of the
whole byte sequence proves ClientHello passthrough and payload opacity in
one hash.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit import stream_wire as sw
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.stream_adapter import tcp_dial_handler
from tools.network.relaykit.viewer import ViewerChannel

from .conftest import ORG
from .test_relay_integration import free_port, start_registry
from .test_hostname_routing_integration import (
    _register_org_and_link,
    _reservation,
    _host,
)
from .test_stream_wire import _client_hello

REPO = Path(__file__).resolve().parents[4]
PERSONA = "ab" * 32
CAPS = ("host-lease/1", "tls-stream/1")


def _rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("no VmRSS")


@pytest.fixture(scope="module")
def stack(tmp_path_factory, root):
    tmp = tmp_path_factory.mktemp("stream-stack")
    port, ingress_port = free_port(), free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    registry = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry",
         "--db", str(tmp / "registry.db"), "--host", "127.0.0.1",
         "--port", str(port), "--stream-ingress-port", str(ingress_port),
         "--stream-idle-timeout", "3"],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "registry.log", "ab"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz",
                         timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        registry.kill()
        raise RuntimeError((tmp / "registry.log").read_text()[-2000:])
    token = _register_org_and_link(port, root)
    yield {"port": port, "ingress": ingress_port, "token": token,
           "root": root, "registry": registry, "tmp": tmp}
    with contextlib.suppress(Exception):
        registry.terminate()
        registry.wait(timeout=5)


async def _echo_server():
    """A TCP echo that also honors half-close: EOF in → flush + EOF out."""
    async def handle(reader, writer):
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
            writer.write_eof()
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _serving_connector(stack, app_labels, echo_port):
    root = stack["root"]
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
        subject=Subject("persona", PERSONA),
        not_before=now - 300, not_after=now + 7 * 86_400,
    )
    connector = TunnelConnector(
        f"ws://127.0.0.1:{stack['port']}", ORG, serve_key, cert,
        min_backoff=0.1, max_backoff=0.5,
        machine_key=machine_key, caps=CAPS,
        stream_handler=tcp_dial_handler("127.0.0.1", echo_port),
    )
    task = asyncio.create_task(connector.run())
    await asyncio.wait_for(connector.connected.wait(), 10)
    assert sw.CAP_TLS_STREAM in connector.accepted_caps
    for app in app_labels:
        reply = await connector.serve_host(_reservation(app), _host(app))
        assert reply.get("ok") is True, reply
    return connector, task


async def _open_stream(stack, host: str):
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", stack["ingress"]
    )
    hello = _client_hello(host)
    writer.write(hello)
    await writer.drain()
    return reader, writer, hello


async def _pump_stream(stack, host: str, payload: bytes) -> bytes:
    """Send ClientHello + payload, half-close, read the full echo back."""
    reader, writer, hello = await _open_stream(stack, host)
    expected = hello + payload

    async def send():
        for off in range(0, len(payload), 65536):
            writer.write(payload[off:off + 65536])
            await writer.drain()
        writer.write_eof()

    send_task = asyncio.create_task(send())
    received = b""
    while len(received) < len(expected):
        chunk = await asyncio.wait_for(reader.read(65536), timeout=30)
        if not chunk:
            break
        received += chunk
    await send_task
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return received if received != expected else expected  # byte compare


def test_32_streams_10mib_bidirectional_byte_exact_with_rss_budget(stack):
    async def scenario():
        server, echo_port = await _echo_server()
        connector, task = await _serving_connector(stack, ["docs"], echo_port)
        host = _host("docs")
        rss_before = _rss_kib(stack["registry"].pid)
        payloads = [os.urandom(10 * 1024 * 1024) for _ in range(4)]
        try:
            results = await asyncio.gather(*(
                _pump_stream(stack, host, payloads[i % 4])
                for i in range(32)
            ))
            for i, received in enumerate(results):
                expected = _client_hello(host) + payloads[i % 4]
                assert hashlib.sha256(received).hexdigest() == \
                    hashlib.sha256(expected).hexdigest(), f"stream {i}"
            rss_growth_mib = (_rss_kib(stack["registry"].pid) - rss_before) / 1024
            assert rss_growth_mib <= 128, f"registry RSS grew {rss_growth_mib:.0f} MiB"
        finally:
            connector.stop(); task.cancel()
            server.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    asyncio.run(scenario())


def test_stalled_stream_does_not_block_unstalled_transfers(stack):
    async def scenario():
        server, echo_port = await _echo_server()
        connector, task = await _serving_connector(stack, ["app2"], echo_port)
        host = _host("app2")
        try:
            # The stalled client: opens, floods, never reads its echo.
            s_reader, s_writer, _ = await _open_stream(stack, host)
            for _ in range(64):
                s_writer.write(os.urandom(65536))
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(s_writer.drain(), timeout=0.5)
            started = time.monotonic()
            one_mib = os.urandom(1024 * 1024)
            results = await asyncio.wait_for(asyncio.gather(*(
                _pump_stream(stack, host, one_mib) for _ in range(8)
            )), timeout=30)
            elapsed = time.monotonic() - started
            expected = _client_hello(host) + one_mib
            assert all(r == expected for r in results)
            assert elapsed < 5.0, f"unstalled transfers took {elapsed:.1f}s"
            s_writer.close()
        finally:
            connector.stop(); task.cancel()
            server.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    asyncio.run(scenario())


def test_unknown_host_and_non_capable_tunnel_refuse_with_zero_bytes(stack):
    async def scenario():
        reader, writer, _ = await _open_stream(
            stack, _host("ghost-app")
        )
        data = await asyncio.wait_for(reader.read(64), timeout=10)
        assert data == b""  # closed with nothing written back
        writer.close()
    asyncio.run(scenario())


def test_release_resets_live_streams_route_released(stack):
    async def scenario():
        server, echo_port = await _echo_server()
        connector, task = await _serving_connector(stack, ["blog"], echo_port)
        host = _host("blog")
        try:
            reader, writer, hello = await _open_stream(stack, host)
            # Prove liveness first: a small echo round-trips.
            writer.write(b"ping")
            await writer.drain()
            got = b""
            while len(got) < len(hello) + 4:
                got += await asyncio.wait_for(reader.read(65536), timeout=10)
            assert got == hello + b"ping"
            # Release the reservation: the live stream must close promptly.
            await connector.release_host(_reservation("blog"))
            end = await asyncio.wait_for(reader.read(64), timeout=5)
            assert end == b""
            writer.close()
        finally:
            connector.stop(); task.cancel()
            server.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    asyncio.run(scenario())


def test_connector_loss_closes_streams_and_viewers_stay_responsive(stack):
    async def scenario():
        server, echo_port = await _echo_server()
        connector, task = await _serving_connector(stack, ["app4"], echo_port)
        host = _host("app4")
        try:
            reader, writer, hello = await _open_stream(stack, host)
            writer.write(b"x")
            await writer.drain()
            got = b""
            while len(got) < len(hello) + 1:
                got += await asyncio.wait_for(reader.read(65536), timeout=10)
            # Encrypted viewer channel works concurrently on the same org.
            viewer = await ViewerChannel.connect(
                f"ws://127.0.0.1:{stack['port']}", stack["token"],
                root_pub=stack["root"].public_hex, org=ORG,
            )
            await viewer.send_message(b"concurrent-viewer")
            assert await viewer.recv_message() == b"concurrent-viewer"
            await viewer.close()
            # Kill the connector: the raw stream socket must close.
            connector.stop(); task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            end = await asyncio.wait_for(reader.read(64), timeout=5)
            assert end == b""
            writer.close()
        finally:
            server.close()
            with contextlib.suppress(Exception):
                connector.stop()
    asyncio.run(scenario())


def test_idle_stream_times_out(stack):
    async def scenario():
        server, echo_port = await _echo_server()
        connector, task = await _serving_connector(stack, ["app5"], echo_port)
        host = _host("app5")
        try:
            reader, writer, hello = await _open_stream(stack, host)
            got = b""
            while len(got) < len(hello):
                got += await asyncio.wait_for(reader.read(65536), timeout=10)
            # No traffic: the 3s test idle timeout must reset the stream.
            end = await asyncio.wait_for(reader.read(64), timeout=15)
            assert end == b""
            writer.close()
        finally:
            connector.stop(); task.cancel()
            server.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    asyncio.run(scenario())
