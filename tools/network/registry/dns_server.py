"""The serve.auto.network DNS process (auto-g1jxw).

Runs BESIDE the registry on the relay host — one system, one codebase,
one store, two crash domains: a UDP/53 flood or a codec fault here can
never stall a parked tunnel or crash the relay. Read-only by
construction: challenge state arrives over a loopback registry endpoint
(micro-cached ~1 s); when the registry is briefly down, static answers
(A/NS/SOA) keep serving and only challenge TXT degrades to NODATA — the
correct failure mode, since resolution must not fail and issuance has
slack.

    python -m tools.network.registry.dns_server \
        --bind 5.161.219.195 --port 53 \
        --registry-url http://127.0.0.1:8477 \
        --node-id registry-ash-1 [--relay-ip 5.161.219.195]

Per-source token buckets bound query rate (spoofed-flood damage control;
answers are minimal and ANY is refused, so amplification ≈ 1).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import struct
import time
import urllib.request

from .dns_responder import ZoneState, handle_query

logger = logging.getLogger("registry.dns")

#: Per-source token bucket: burst, then sustained per-second refill.
SOURCE_BURST = 50
SOURCE_RATE = 25.0
MAX_SOURCES = 65536
STATE_CACHE_SECONDS = 1.0
STATE_FETCH_TIMEOUT = 0.5
TCP_MAX_QUERY = 4096


class _Buckets:
    def __init__(self, now_fn=time.monotonic):
        self._now = now_fn
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, source: str) -> bool:
        now = self._now()
        tokens, seen = self._buckets.get(source, (float(SOURCE_BURST), now))
        tokens = min(float(SOURCE_BURST), tokens + (now - seen) * SOURCE_RATE)
        if tokens < 1.0:
            self._buckets[source] = (tokens, now)
            return False
        if len(self._buckets) > MAX_SOURCES:
            self._buckets.clear()  # bounded memory beats perfect fairness
        self._buckets[source] = (tokens - 1.0, now)
        return True


class _ChallengeCache:
    """Loopback zone-state fetch with a short cache and fail-soft reads."""

    def __init__(self, registry_url: str):
        self._url = f"{registry_url.rstrip('/')}/v1/dns/zone-state"
        self._challenges: dict[str, tuple[list[str], int]] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    def _fetch(self) -> dict[str, list[str]]:
        with urllib.request.urlopen(
            self._url, timeout=STATE_FETCH_TIMEOUT
        ) as resp:
            data = json.loads(resp.read())
        challenges = data.get("challenges", {})
        if not isinstance(challenges, dict):
            return {}
        parsed: dict[str, tuple[list[str], int]] = {}
        for name, entry in challenges.items():
            if isinstance(entry, dict):
                values = [str(v) for v in entry.get("values", [])]
                ttl = int(entry.get("ttl", 60))
            else:  # v1 compatibility: a bare value list
                values, ttl = [str(v) for v in entry], 60
            parsed[str(name)] = (values, ttl)
        return parsed

    async def refresh_if_stale(self) -> None:
        if time.monotonic() - self._fetched_at < STATE_CACHE_SECONDS:
            return
        async with self._lock:
            if time.monotonic() - self._fetched_at < STATE_CACHE_SECONDS:
                return
            try:
                self._challenges = await asyncio.get_event_loop() \
                    .run_in_executor(None, self._fetch)
            except Exception:
                self._challenges = {}  # fail soft: NODATA, statics live on
            self._fetched_at = time.monotonic()

    def lookup(self, name: str) -> list[str]:
        return self._challenges.get(name, ([], 60))[0]

    def lookup_ttl(self, name: str) -> int:
        return self._challenges.get(name, ([], 60))[1]


class DnsService:
    def __init__(self, *, relay_ip: str, node_id: str, registry_url: str,
                 metrics=None):
        self._cache = _ChallengeCache(registry_url)
        self._buckets = _Buckets()
        self._metrics = metrics
        if metrics is not None:
            metrics.bind_challenge_count(
                lambda: len(self._cache._challenges)
            )
        self.state = ZoneState(
            relay_ip=relay_ip, node_id=node_id,
            txt_lookup=self._cache.lookup,
            txt_ttl=self._cache.lookup_ttl,
        )

    async def answer(self, raw: bytes, source: str, *,
                     tcp: bool) -> bytes | None:
        if not self._buckets.allow(source):
            if self._metrics is not None:
                self._metrics.dropped()
            return None  # dropped, never an error a flood can amplify
        await self._cache.refresh_if_stale()
        reply = handle_query(raw, self.state, tcp=tcp)
        if self._metrics is not None and reply is not None and len(reply) >= 4:
            # rcode is the low 4 bits of the header's second flags byte.
            self._metrics.query(reply[3] & 0x0F)
        return reply


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, service: DnsService):
        self._service = service
        self._transport = None
        self._tasks: set[asyncio.Task] = set()

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data, addr):
        task = asyncio.create_task(self._respond(data, addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _respond(self, data, addr):
        with contextlib.suppress(Exception):
            reply = await self._service.answer(
                data, str(addr[0]), tcp=False)
            if reply is not None and self._transport is not None:
                self._transport.sendto(reply, addr)


async def _serve_tcp(reader, writer, service: DnsService):
    source = "unknown"
    peer = writer.get_extra_info("peername")
    if peer:
        source = str(peer[0])
    try:
        while True:
            header = await asyncio.wait_for(reader.readexactly(2), 10)
            (length,) = struct.unpack(">H", header)
            if length == 0 or length > TCP_MAX_QUERY:
                return
            raw = await asyncio.wait_for(reader.readexactly(length), 10)
            reply = await service.answer(raw, source, tcp=True)
            if reply is None:
                return
            writer.write(struct.pack(">H", len(reply)) + reply)
            await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError,
            ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def run_server(*, bind: str, port: int, relay_ip: str,
                     node_id: str, registry_url: str,
                     metrics_host: str = "127.0.0.1",
                     metrics_port: int | None = None) -> None:
    metrics = None
    if metrics_port is not None:
        from .metrics import DnsMetrics, start_metrics_listener
        metrics = DnsMetrics()
    service = DnsService(relay_ip=relay_ip, node_id=node_id,
                         registry_url=registry_url, metrics=metrics)
    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpProtocol(service), local_addr=(bind, port))
    tcp_server = await asyncio.start_server(
        lambda r, w: _serve_tcp(r, w, service), bind, port)
    metrics_server = None
    if metrics is not None:
        # Private, loopback-only — a SEPARATE scrape target from the
        # registry, matching the DNS process's own crash domain.
        metrics_server = await start_metrics_listener(
            metrics_host, metrics_port, metrics)
    logger.warning("serve.auto.network DNS answering on %s:%d (udp+tcp)",
                   bind, port)
    try:
        await asyncio.Event().wait()
    finally:
        transport.close()
        tcp_server.close()
        if metrics_server is not None:
            metrics_server.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="serve.auto.network authoritative DNS")
    parser.add_argument("--bind", required=True,
                        help="explicit address to answer on (relay IP)")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--relay-ip",
                        help="A-answer target (default: --bind)")
    parser.add_argument("--node-id", default="",
                        help="CHAOS id.server identity (per-PoP)")
    parser.add_argument("--registry-url",
                        default="http://127.0.0.1:8477")
    parser.add_argument("--metrics-port", type=int,
                        help="enable the PRIVATE DNS metrics exposition on "
                             "this loopback port (own scrape target)")
    parser.add_argument("--metrics-host", default="127.0.0.1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_server(
        bind=args.bind, port=args.port,
        relay_ip=args.relay_ip or args.bind,
        node_id=args.node_id, registry_url=args.registry_url,
        metrics_host=args.metrics_host, metrics_port=args.metrics_port,
    ))


if __name__ == "__main__":
    main()
