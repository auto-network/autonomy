"""Relay-side raw-stream ingress (tls-stream/1, auto-9z1xh).

A loopback TCP acceptor (fronted publicly by auto-ot0t7's pinned Caddy L4
later — this bead ships no public edge): bounded ClientHello/SNI peek,
hostname routing through the live lease table, abuse admission, then an
OPEN toward the leased connector and bounded credit-governed pumps in both
directions. Payload bytes are never inspected or logged.

Every refusal — no SNI, parse failure, unknown/unrouted host, tunnel
without the negotiated capability, admission denied, stream cap — closes
the socket having written zero bytes back: refusals are byte-identical
and carry no oracle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from tools.network.relaykit.frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    FRAME_STREAM_CTRL,
)
from tools.network.relaykit.stream_wire import (
    CAP_TLS_STREAM,
    CreditWindow,
    NeedMoreData,
    ReplenishTracker,
    RESET_BYTE_BUDGET,
    RESET_ORDERLY,
    RESET_PROTOCOL,
    RESET_TIMEOUT,
    SNI_PEEK_MAX_BYTES,
    STREAM_HANDSHAKE_TIMEOUT,
    STREAM_IDLE_TIMEOUT,
    STREAM_INITIAL_CREDIT,
    STREAM_MAX_DATA,
    STREAM_MAX_PER_TUNNEL,
    StreamProtocolError,
    build_ctrl_credit,
    build_ctrl_eof,
    build_ctrl_reset,
    build_stream_open,
    extract_sni,
    parse_ctrl,
)

logger = logging.getLogger("registry.stream")


class RelayRawStream:
    """One public TCP connection bridged to its leased tunnel."""

    def __init__(self, tunnel, channel_id: bytes, reservation: str,
                 host: str, reader, writer, *, abuse_lease=None,
                 idle_timeout: float = STREAM_IDLE_TIMEOUT,
                 charge_starvation: float | None = None):
        self.tunnel = tunnel
        self.channel_id = channel_id
        self.reservation = reservation
        self.host = host
        self.reader = reader
        self.writer = writer
        self.abuse_lease = abuse_lease
        self.idle_timeout = idle_timeout
        self.charge_starvation = charge_starvation
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.open_ok: asyncio.Future = asyncio.get_event_loop().create_future()
        #: connector's grant for browser→connector bytes; set by open-ok
        self.send_window = CreditWindow(0)
        self.send_credit_event = asyncio.Event()
        #: our grant for connector→browser bytes
        self.granted_outstanding = STREAM_INITIAL_CREDIT
        self.replenish = ReplenishTracker()
        self.last_activity = asyncio.get_event_loop().time()
        self._sent_eof = False
        self._recv_eof = False
        self._tasks: list[asyncio.Task] = []
        self._done = asyncio.Event()

    # -- entry points from the tunnel receive loop (never block) -----------

    def on_data(self, payload: bytes) -> None:
        self.inbound.put_nowait(("data", payload))

    def on_ctrl_raw(self, payload: bytes) -> None:
        try:
            msg = parse_ctrl(payload)
        except StreamProtocolError:
            msg = {"op": "reset", "code": RESET_PROTOCOL}
        if msg["op"] == "open-ok" and not self.open_ok.done():
            self.open_ok.set_result(msg["credit"])
            return
        if msg["op"] == "reset" and not self.open_ok.done():
            self.open_ok.set_exception(_Refused(msg["code"]))
            return
        self.inbound.put_nowait(("ctrl", msg))

    def on_close(self) -> None:
        if not self.open_ok.done():
            self.open_ok.set_exception(_Refused(RESET_PROTOCOL))
        self.inbound.put_nowait(("close", None))

    def signal_reset(self, code: int) -> None:
        """Route removal (release/revoke): reset from the relay side."""
        self.inbound.put_nowait(("local-reset", code))

    # -- pumps -------------------------------------------------------------

    async def run(self, first_bytes: bytes) -> None:
        """The from-tunnel pump owns the stream lifetime: it returns on
        reset, close, error, or orderly completion (both directions
        EOF'd), so half-close keeps the opposite direction flowing."""
        self._tasks = [
            asyncio.create_task(self._pump_to_tunnel(first_bytes)),
            asyncio.create_task(self._pump_from_tunnel()),
            asyncio.create_task(self._idle_watchdog()),
        ]
        with contextlib.suppress(asyncio.CancelledError):
            await self._tasks[1]
        await self.teardown()

    def _touch(self) -> None:
        self.last_activity = asyncio.get_event_loop().time()

    async def _charge(self, n: int) -> bool:
        """Charge stream bytes with SHAPING, never termination (operator
        rule 2026-09-01, from the TURN 4404 incident): a drained byte
        bucket is congestion — this stream simply waits for refill, at
        any duration. Reset 4 exists only for a separately justified
        prolonged resource emergency, opted into via *charge_starvation*;
        the default is indefinite backpressure."""
        if self.abuse_lease is None:
            return True
        loop = asyncio.get_event_loop()
        deadline = (
            loop.time() + self.charge_starvation
            if self.charge_starvation is not None else None
        )
        while not self.abuse_lease.charge_bytes(n):
            if deadline is not None and loop.time() > deadline:
                return False
            self._touch()  # shaping is progress, not idleness
            await asyncio.sleep(0.1)
        return True

    async def _send_reset(self, code: int) -> None:
        with contextlib.suppress(Exception):
            await self.tunnel.send_frame(
                FRAME_STREAM_CTRL, self.channel_id, build_ctrl_reset(code)
            )

    async def _pump_to_tunnel(self, first_bytes: bytes) -> None:
        """Public TCP → DATA, governed by the connector's grant. The
        buffered ClientHello is the first payload — sent only here, i.e.
        strictly after open-ok resolved."""
        pending = first_bytes
        try:
            while True:
                if pending:
                    chunk, pending = (
                        pending[:STREAM_MAX_DATA], pending[STREAM_MAX_DATA:]
                    )
                    while self.send_window.sendable < len(chunk):
                        self.send_credit_event.clear()
                        await self.send_credit_event.wait()
                else:
                    while self.send_window.sendable <= 0:
                        self.send_credit_event.clear()
                        await self.send_credit_event.wait()
                    chunk = await self.reader.read(
                        min(STREAM_MAX_DATA, self.send_window.sendable)
                    )
                if not chunk:
                    if not self._sent_eof:
                        self._sent_eof = True
                        with contextlib.suppress(Exception):
                            await self.tunnel.send_frame(
                                FRAME_STREAM_CTRL, self.channel_id,
                                build_ctrl_eof(),
                            )
                        # Let the lifetime-owning pump re-check orderly
                        # completion (both directions now EOF'd?).
                        self.inbound.put_nowait(("eof-sent", None))
                    return
                if not await self._charge(len(chunk)):
                    await self._send_reset(RESET_BYTE_BUDGET)
                    return
                self.send_window.consume(len(chunk))
                self._touch()
                await self.tunnel.send_frame(
                    FRAME_DATA, self.channel_id, chunk
                )
        except (ConnectionError, OSError):
            await self._send_reset(RESET_ORDERLY)
        except StreamProtocolError:
            await self._send_reset(RESET_PROTOCOL)

    async def _pump_from_tunnel(self) -> None:
        try:
            while True:
                kind, payload = await self.inbound.get()
                if kind == "data":
                    if len(payload) > STREAM_MAX_DATA or \
                            len(payload) > self.granted_outstanding:
                        await self._send_reset(RESET_PROTOCOL)
                        return
                    if not await self._charge(len(payload)):
                        await self._send_reset(RESET_BYTE_BUDGET)
                        return
                    self.granted_outstanding -= len(payload)
                    self._touch()
                    self.writer.write(payload)
                    await self.writer.drain()
                    grant = self.replenish.consumed(len(payload))
                    if grant is not None:
                        self.granted_outstanding += grant
                        await self.tunnel.send_frame(
                            FRAME_STREAM_CTRL, self.channel_id,
                            build_ctrl_credit(grant),
                        )
                elif kind == "ctrl":
                    op = payload["op"]
                    if op == "credit":
                        self.send_window.grant(payload["add"])
                        self.send_credit_event.set()
                        self._touch()
                    elif op == "eof":
                        if self._recv_eof:
                            await self._send_reset(RESET_PROTOCOL)
                            return
                        self._recv_eof = True
                        with contextlib.suppress(
                            OSError, RuntimeError, NotImplementedError
                        ):
                            self.writer.write_eof()
                        if self._sent_eof:
                            return  # orderly: both directions done
                    elif op == "reset":
                        return
                    else:
                        await self._send_reset(RESET_PROTOCOL)
                        return
                elif kind == "eof-sent":
                    if self._recv_eof:
                        return  # orderly: both directions done
                elif kind == "local-reset":
                    await self._send_reset(payload)
                    return
                else:  # close
                    return
        except (ConnectionError, OSError):
            await self._send_reset(RESET_ORDERLY)

    async def _idle_watchdog(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            idle_for = loop.time() - self.last_activity
            if idle_for >= self.idle_timeout:
                await self._send_reset(RESET_TIMEOUT)
                self.inbound.put_nowait(("close", None))
                return
            await asyncio.sleep(
                min(self.idle_timeout - idle_for, self.idle_timeout)
            )

    async def teardown(self) -> None:
        if self._done.is_set():
            return
        self._done.set()
        if self.tunnel.raw_streams.get(self.channel_id) is self:
            del self.tunnel.raw_streams[self.channel_id]
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        if self.abuse_lease is not None:
            self.abuse_lease.release()
            self.abuse_lease = None
        with contextlib.suppress(Exception):
            await self.tunnel.send_frame(FRAME_CLOSE, self.channel_id)
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


class _Refused(Exception):
    def __init__(self, code: int):
        self.code = code


async def _peek_client_hello(reader) -> tuple[str, bytes]:
    """Read just enough to extract the SNI. Returns (sni, buffered)."""
    buffered = b""
    while len(buffered) < SNI_PEEK_MAX_BYTES:
        chunk = await reader.read(4096)
        if not chunk:
            raise StreamProtocolError("connection ended before ClientHello")
        buffered += chunk
        try:
            sni = extract_sni(buffered)
        except NeedMoreData:
            continue
        if sni is None:
            raise StreamProtocolError("ClientHello carries no SNI")
        return sni, buffered
    raise StreamProtocolError("ClientHello exceeds peek bound")


async def handle_stream_connection(
    reader, writer, *, host_routes, abuse_limiter=None,
    idle_timeout: float = STREAM_IDLE_TIMEOUT,
) -> None:
    """One ingress connection, accept to teardown."""
    from tools.network.relaykit.frames import new_channel_id

    abuse_lease = None
    try:
        try:
            sni, buffered = await asyncio.wait_for(
                _peek_client_hello(reader), STREAM_HANDSHAKE_TIMEOUT
            )
        except (StreamProtocolError, asyncio.TimeoutError, OSError):
            return
        admission = None
        if abuse_limiter is not None:
            peer = writer.get_extra_info("peername") or ("unknown",)
            admission = abuse_limiter.begin(str(peer[0]))
            if admission is None:
                return
        tunnel = host_routes.route(sni)
        if tunnel is None or CAP_TLS_STREAM not in tunnel.caps:
            return
        if abuse_limiter is not None:
            resolved = abuse_limiter.resolve(admission, sni, tunnel.org)
            if resolved is None:
                return
            abuse_lease = abuse_limiter.acquire(resolved)
            if abuse_lease is None:
                return
        if len(tunnel.raw_streams) >= STREAM_MAX_PER_TUNNEL:
            return
        channel_id = new_channel_id()
        stream = RelayRawStream(
            tunnel, channel_id, host_routes.reservation_for(sni), sni,
            reader, writer, abuse_lease=abuse_lease,
            idle_timeout=idle_timeout,
        )
        abuse_lease = None  # owned by the stream now
        tunnel.raw_streams[channel_id] = stream
        try:
            await tunnel.send_frame(
                FRAME_OPEN, channel_id,
                build_stream_open(host=sni, reservation=stream.reservation,
                                  credit=STREAM_INITIAL_CREDIT),
            )
            credit = await asyncio.wait_for(
                stream.open_ok, STREAM_HANDSHAKE_TIMEOUT
            )
        except Exception:  # _Refused, timeout, or a dying tunnel send
            # Pre-open-ok refusal/timeout: the buffered ClientHello is
            # discarded and the public socket closes (seam §4.1).
            await stream.teardown()
            return
        stream.send_window.grant(credit)
        stream.send_credit_event.set()
        await stream.run(buffered)
    finally:
        if abuse_lease is not None:
            abuse_lease.release()
        with contextlib.suppress(Exception):
            writer.close()


async def start_stream_ingress(
    host: str, port: int, *, host_routes, abuse_limiter=None,
    idle_timeout: float = STREAM_IDLE_TIMEOUT,
):
    async def handle(reader, writer):
        await handle_stream_connection(
            reader, writer, host_routes=host_routes,
            abuse_limiter=abuse_limiter, idle_timeout=idle_timeout,
        )

    server = await asyncio.start_server(handle, host, port)
    logger.info("stream ingress listening on %s:%d", host, port)
    return server
