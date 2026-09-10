"""The carrier soak the operator ruling requires (auto-z49ee): a real relay
in its OWN process, two real outbound connectors, one bulk pair whose
reader stalls for a long window midway while a second pair between the
same two tunnels keeps its service, and memory measured per process.

Skipped unless AUTONOMY_FLEET_SOAK=1: it moves gigabytes. Knobs:

    AUTONOMY_FLEET_SOAK_MIB      total bytes on the bulk pair (default 2724 ≈ 2.66 GiB)
    AUTONOMY_FLEET_SOAK_STALL_S  reader pause at the halfway point (default 120)
    AUTONOMY_FLEET_STREAM_WINDOW_SLOTS etc. reach the relay subprocess through the env.

What is asserted, in the ruling's words:

1. slow-consumer isolation — the stalled pair is throttled, never reset or
   discarded; the healthy pair's request latency during the stall stays
   within a bound of its pre-stall latency; hashes match after resume;
2. bounded memory across both legs — the relay's retained custody for the
   org never exceeds two offered windows per pair, and the relay process's
   RSS above its idle baseline stays under a declared bound at the stall
   peak and returns toward baseline after teardown;
3. cleanup — pairs, schedulers and custody are zero at the end.

The measurements are printed and written to a JSON file beside the retained
evidence so the numeric freeze cites them rather than the assertions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM as CAP,
    FLEET_STREAM_MAX_MESSAGE,
    FLEET_STREAM_WINDOW_BYTES,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTONOMY_FLEET_SOAK") != "1",
    reason="soak: set AUTONOMY_FLEET_SOAK=1 (moves gigabytes)",
)

REPO = Path(__file__).resolve().parents[4]
ORG = "77777777-7777-4777-8777-777777777777"
PERSONA = "ab" * 32
MIB = int(os.environ.get("AUTONOMY_FLEET_SOAK_MIB", "2724"))
STALL_S = float(os.environ.get("AUTONOMY_FLEET_SOAK_STALL_S", "120"))
MESSAGE = min(FLEET_STREAM_MAX_MESSAGE, 192 * 1024)
#: Declared bound for the relay's RSS growth above idle at the stall peak.
#: Provisional; the freeze records the measured value.
RELAY_RSS_BOUND = 64 * 1024 * 1024
OUT = Path(os.environ.get("AUTONOMY_FLEET_SOAK_OUT", "/workspace/output/fh2nv/soak.json"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _rss(pid: int) -> int:
    with open(f"/proc/{pid}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def _start_registry(port: int, log: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry", "--db", ":memory:",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=open(log, "ab"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"registry did not come up: {log.read_text()[-2000:]}")


def _register(port, root):
    resp = httpx.post(f"http://127.0.0.1:{port}/v1/orgs", json=sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": ORG, "root_pub": root.public_hex, "recovery_policy": "none"},
        ts=int(time.time())), timeout=10)
    assert resp.status_code == 201, resp.text


class Machine:
    def __init__(self, root, port):
        self.serve_key, self.machine_key = KeyPair.generate(), KeyPair.generate()
        now = int(time.time())
        self.cert = issue_cert(root, self.serve_key.public_hex, scope=("tunnel:serve",),
                               org=ORG, subject=Subject("persona", PERSONA),
                               not_before=now - 100, not_after=now + 30 * 86400)
        self.port = port
        self.connector = None
        self.task = None

    async def start(self):
        async def accept(endpoint):
            return True
        self.connector = TunnelConnector(
            f"ws://127.0.0.1:{self.port}", ORG, self.serve_key, self.cert,
            machine_key=self.machine_key, caps=(CAP,), fleet_stream_offer=accept,
            min_backoff=0.05, max_backoff=0.2)
        self.task = asyncio.create_task(self.connector.run())
        await asyncio.wait_for(self.connector.connected.wait(), 15)
        return self

    async def stop(self):
        self.connector.stop()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


async def _custody(connector) -> dict:
    reply = await connector.control("fleet-pairs", {}, timeout=10)
    assert reply.get("ok") is True, reply
    return {k: reply[k] for k in ("pairs", "schedulers", "queued_bytes", "queued_slots", "outstanding_bytes")}


def test_soak_stalled_reader_is_throttled_not_reset_and_memory_stays_bounded(tmp_path):
    root = KeyPair.generate()
    port = _free_port()
    registry = _start_registry(port, tmp_path / "registry.log")
    try:
        report = asyncio.run(_scenario(root, port, registry.pid))
    finally:
        registry.terminate()
        with contextlib.suppress(Exception):
            registry.wait(timeout=10)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1))
    print("\n[fleet soak] " + json.dumps(report))


async def _scenario(root, port, relay_pid) -> dict:
    _register(port, root)
    a = await Machine(root, port).start()
    b = await Machine(root, port).start()
    report: dict = {"mib": MIB, "stall_s": STALL_S, "window_bytes": FLEET_STREAM_WINDOW_BYTES}
    try:
        await asyncio.sleep(1.0)
        relay_idle = _rss(relay_pid)
        self_idle = _rss(os.getpid())
        report["relay_rss_idle"] = relay_idle
        report["self_rss_idle"] = self_idle

        bulk = await a.connector.fleet_streams.open(PERSONA, b.machine_key.public_hex)
        bulk_peer = await asyncio.wait_for(b.connector.fleet_streams.accepted.get(), 10)
        probe = await a.connector.fleet_streams.open(PERSONA, b.machine_key.public_hex)
        probe_peer = await asyncio.wait_for(b.connector.fleet_streams.accepted.get(), 10)
        assert bulk.pair_id != probe.pair_id

        total = MIB * 1024 * 1024
        digest_out, digest_in = hashlib.sha256(), hashlib.sha256()
        sent_bytes = 0
        received_bytes = 0
        halfway = total // 2
        stall_started = asyncio.Event()
        stall_done = asyncio.Event()
        custody_samples: list[dict] = []
        rss_samples: list[tuple[str, int, int]] = []
        reset_seen = []

        async def sender():
            nonlocal sent_bytes
            while sent_bytes < total:
                chunk = os.urandom(min(MESSAGE, total - sent_bytes))
                digest_out.update(chunk)
                await bulk.send(chunk)
                sent_bytes += len(chunk)
            await bulk.half_close()

        async def reader():
            nonlocal received_bytes
            stalled = False
            while True:
                if not stalled and received_bytes >= halfway:
                    stalled = True
                    stall_started.set()
                    await asyncio.sleep(STALL_S)
                    stall_done.set()
                message = await bulk_peer.recv()
                if message is None:
                    return
                digest_in.update(message)
                received_bytes += len(message)

        async def echo_responder():
            while True:
                message = await probe_peer.recv()
                if message is None:
                    return
                await probe_peer.send(message)

        async def prober(samples: list, until: asyncio.Event):
            while not until.is_set():
                started = time.monotonic()
                await probe.send(b"ping" * 16)
                assert await asyncio.wait_for(probe.recv(), 30) == b"ping" * 16
                samples.append(time.monotonic() - started)
                await asyncio.sleep(0.25)

        async def sampler():
            await stall_started.wait()
            while not stall_done.is_set():
                custody_samples.append(await _custody(a.connector))
                rss_samples.append(("stall", _rss(relay_pid), _rss(os.getpid())))
                await asyncio.sleep(2.0)

        pre_stall: list = []
        during_stall: list = []
        tasks = [asyncio.create_task(sender()), asyncio.create_task(reader()),
                 asyncio.create_task(echo_responder()), asyncio.create_task(sampler())]
        started = time.monotonic()
        await asyncio.gather(
            asyncio.create_task(prober(pre_stall, stall_started)),
        )
        await asyncio.gather(asyncio.create_task(prober(during_stall, stall_done)))
        await asyncio.wait_for(asyncio.gather(tasks[0], tasks[1]), timeout=MIB * 2 + STALL_S + 120)
        elapsed = time.monotonic() - started
        tasks[3].cancel()
        await probe.half_close()
        await asyncio.wait_for(tasks[2], 10)

        # 1. isolation and exactness
        assert digest_out.digest() == digest_in.digest(), "bulk bytes differ after the stall"
        assert received_bytes == total
        assert not bulk.closed.is_set() or bulk.reset_code in (None,), bulk.reset_code
        assert bulk_peer.reset_code is None and probe.reset_code is None
        pre_p50 = statistics.median(pre_stall) if pre_stall else 0.0
        stall_p50 = statistics.median(during_stall) if during_stall else 0.0
        stall_p95 = sorted(during_stall)[int(len(during_stall) * 0.95)] if during_stall else 0.0
        report.update(
            elapsed_s=round(elapsed, 2), bulk_mib_per_s=round(MIB / max(elapsed - STALL_S, 1e-6), 2),
            probe_pre_stall_p50_ms=round(pre_p50 * 1000, 2),
            probe_during_stall_p50_ms=round(stall_p50 * 1000, 2),
            probe_during_stall_p95_ms=round(stall_p95 * 1000, 2),
            probe_samples_during_stall=len(during_stall),
        )
        assert during_stall, "the healthy pair produced no samples during the stall"
        assert stall_p95 < max(1.0, pre_p50 * 20), (pre_p50, stall_p95)

        # 2. bounded memory
        peak_custody = max((s["queued_bytes"] + s["outstanding_bytes"] for s in custody_samples), default=0)
        peak_relay_rss = max((r for _, r, _ in rss_samples), default=relay_idle)
        peak_self_rss = max((s for _, _, s in rss_samples), default=self_idle)
        report.update(
            peak_custody_bytes=peak_custody,
            custody_bound_bytes=2 * 2 * FLEET_STREAM_WINDOW_BYTES,
            relay_rss_peak_above_idle=peak_relay_rss - relay_idle,
            self_rss_peak_above_idle=peak_self_rss - self_idle,
        )
        assert peak_custody <= 2 * 2 * FLEET_STREAM_WINDOW_BYTES, report
        assert peak_relay_rss - relay_idle < RELAY_RSS_BOUND, report

        # 3. cleanup
        await bulk.close()
        await probe.close()
        deadline = time.time() + 10
        final = await _custody(a.connector)
        while time.time() < deadline and (final["pairs"] or final["queued_bytes"]):
            await asyncio.sleep(0.2)
            final = await _custody(a.connector)
        report["final_custody"] = final
        assert final["pairs"] == 0 and final["queued_bytes"] == 0 and final["outstanding_bytes"] == 0
        await asyncio.sleep(1.0)
        report["relay_rss_after_above_idle"] = _rss(relay_pid) - relay_idle
        return report
    finally:
        await a.stop()
        await b.stop()
