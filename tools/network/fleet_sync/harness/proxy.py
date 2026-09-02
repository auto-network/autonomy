"""Userspace per-link TCP proxies with scriptable network faults.

Every inter-machine connection in a harness fleet is dialed through one of
these links, so a scenario can degrade exactly one direction of one pair
while the rest of the fleet stays clean.

Loss semantics, stated honestly: a userspace TCP proxy cannot un-ACK bytes,
so packet loss cannot be emulated by discarding stream bytes (that corrupts
the WebSocket framing rather than triggering retransmission). What the
application actually observes under packet loss is retransmission latency
and, past TCP's patience, a dead connection — and those are exactly what
these faults inject: ``stall_rate`` adds per-chunk retransmit-like delay,
``reset_rate`` kills the connection mid-stream, ``latency_s``/``jitter_s``
shape every chunk, ``bandwidth_bytes_per_s`` caps throughput, and
``partitioned`` refuses new dials and aborts live ones.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import random
import threading
from typing import Callable

_CHUNK = 65536


@dataclass(frozen=True)
class LinkFaults:
    latency_s: float = 0.0
    jitter_s: float = 0.0
    bandwidth_bytes_per_s: float | None = None
    stall_rate: float = 0.0
    stall_s: float = 0.25
    reset_rate: float = 0.0
    partitioned: bool = False


class FaultyLink:
    """One direction of one machine pair: dialer-side listener → target."""

    def __init__(self, name: str, rng: random.Random) -> None:
        self.name = name
        self._rng = rng
        self.faults = LinkFaults()
        self.target: tuple[str, int] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.host = "127.0.0.1"
        self.port = 0
        self.forwarded_bytes = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve, self.host, 0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.abort_connections()

    def abort_connections(self) -> None:
        for writer in tuple(self._writers):
            writer.transport.abort()
        self._writers.clear()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        target = self.target
        if target is None or self.faults.partitioned:
            writer.transport.abort()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                *target
            )
        except OSError:
            writer.transport.abort()
            return
        self._writers.add(writer)
        self._writers.add(upstream_writer)
        try:
            await asyncio.gather(
                self._pump(reader, upstream_writer),
                self._pump(upstream_reader, writer),
            )
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            for each in (writer, upstream_writer):
                self._writers.discard(each)
                each.transport.abort()

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            chunk = await reader.read(_CHUNK)
            if not chunk:
                try:
                    writer.write_eof()
                except (OSError, RuntimeError):
                    pass
                return
            faults = self.faults
            if faults.partitioned:
                raise ConnectionResetError("link partitioned")
            if faults.reset_rate and self._rng.random() < faults.reset_rate:
                raise ConnectionResetError("link fault reset")
            delay = faults.latency_s
            if faults.jitter_s:
                delay += self._rng.uniform(0.0, faults.jitter_s)
            if faults.stall_rate and self._rng.random() < faults.stall_rate:
                delay += faults.stall_s
            if faults.bandwidth_bytes_per_s:
                delay += len(chunk) / faults.bandwidth_bytes_per_s
            if delay > 0:
                await asyncio.sleep(delay)
            writer.write(chunk)
            self.forwarded_bytes += len(chunk)
            await writer.drain()


class ProxyHub:
    """Owns the proxy event loop on a background thread.

    The harness parent is synchronous (it drives subprocesses); every link
    mutation crosses into the loop thread through this hub.
    """

    def __init__(self, seed: int = 7) -> None:
        self._seed = seed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._links: dict[str, FaultyLink] = {}
        self._started = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="harness-proxy-hub", daemon=True
        )
        self._thread.start()
        if not self._started.wait(5.0):
            raise RuntimeError("proxy hub did not start")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()
        self._loop.close()

    def _call(self, coro):
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(10.0)

    def create_link(self, name: str) -> tuple[str, int]:
        link = FaultyLink(
            name, random.Random(f"{self._seed}:{name}")
        )
        self._call(link.start())
        self._links[name] = link
        return link.host, link.port

    def set_target(self, name: str, host: str, port: int) -> None:
        async def apply() -> None:
            self._links[name].target = (host, port)
        self._call(apply())

    def set_faults(self, name: str, **changes: object) -> None:
        async def apply() -> None:
            link = self._links[name]
            link.faults = replace(link.faults, **changes)
            if link.faults.partitioned:
                link.abort_connections()
        self._call(apply())

    def link(self, name: str) -> FaultyLink:
        return self._links[name]

    def stop(self) -> None:
        if self._loop is None:
            return
        for link in self._links.values():
            self._call(link.stop())
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5.0)
