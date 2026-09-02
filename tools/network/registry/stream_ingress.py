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
                 charge_starvation: float | None = None,
                 source: str = "unknown"):
        self.tunnel = tunnel
        self.channel_id = channel_id
        self.reservation = reservation
        self.host = host
        #: real client address (PROXY v2 when the edge supplies it, else the
        #: socket peer) — the accounting/logging key, never paired with the
        #: link token or payload (auto-p20eb).
        self.source = source
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


# -- PROXY protocol v2 (auto-p20eb) ----------------------------------------
#
# The serve edge is a dumb :443→ingress TCP forward, so without this the
# ingress attributes every public stream to the forward's own address and
# source-aware accounting is impossible. The forward prefixes each upstream
# connection with a PROXY v2 header carrying the real client address; the
# ingress consumes it BEFORE the ClientHello peek — but only from peers
# named in *proxy_sources* (the trust boundary: anyone else writing a
# header is just handing the SNI parser garbage and gets the byte-identical
# silent close). The 12-byte signature can never open a valid TLS record,
# so a trusted peer that sends no header (an un-upgraded forward during
# rollout) is still parsed as TLS — deployment is order-free.

PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
_PROXY_V2_MAX_LEN = 512


async def _read_proxy_v2(first16: bytes, reader) -> str | None:
    """Parse one PROXY v2 header whose first 16 bytes are *first16*.
    Returns the source address string, or None when the header carries no
    client (LOCAL command — a health check). Raises StreamProtocolError
    on any malformation; the caller closes silently."""
    import ipaddress

    ver_cmd, fam_proto = first16[12], first16[13]
    if ver_cmd >> 4 != 0x2:
        raise StreamProtocolError("unsupported PROXY version")
    length = int.from_bytes(first16[14:16], "big")
    if length > _PROXY_V2_MAX_LEN:
        raise StreamProtocolError("oversized PROXY header")
    try:
        payload = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, OSError) as exc:
        raise StreamProtocolError("truncated PROXY header") from exc
    command = ver_cmd & 0x0F
    if command == 0x0:
        return None  # LOCAL: use the socket's own peer address
    if command != 0x1:
        raise StreamProtocolError("unknown PROXY command")
    if fam_proto == 0x11 and length >= 12:  # TCP over IPv4
        return str(ipaddress.IPv4Address(payload[0:4]))
    if fam_proto == 0x21 and length >= 36:  # TCP over IPv6
        return str(ipaddress.IPv6Address(payload[0:16]))
    raise StreamProtocolError("unsupported PROXY family")


async def _peek_client_hello(reader, initial: bytes = b"") -> tuple[str, bytes]:
    """Read just enough to extract the SNI. Returns (sni, buffered)."""
    buffered = initial
    while len(buffered) < SNI_PEEK_MAX_BYTES:
        if buffered:
            try:
                sni = extract_sni(buffered)
            except NeedMoreData:
                pass
            else:
                if sni is None:
                    raise StreamProtocolError("ClientHello carries no SNI")
                return sni, buffered
        chunk = await reader.read(4096)
        if not chunk:
            raise StreamProtocolError("connection ended before ClientHello")
        buffered += chunk
    raise StreamProtocolError("ClientHello exceeds peek bound")


async def handle_stream_connection(
    reader, writer, *, host_routes, abuse_limiter=None,
    idle_timeout: float = STREAM_IDLE_TIMEOUT,
    proxy_sources: frozenset = frozenset(),
) -> None:
    """One ingress connection, accept to teardown."""
    from tools.network.relaykit.frames import new_channel_id

    abuse_lease = None
    peer = writer.get_extra_info("peername") or ("unknown",)
    source = str(peer[0])
    try:
        initial = b""
        if proxy_sources and source in proxy_sources:
            try:
                first16 = await asyncio.wait_for(
                    reader.readexactly(16), STREAM_HANDSHAKE_TIMEOUT
                )
            except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                    OSError):
                return
            if first16[:12] == PROXY_V2_SIGNATURE:
                try:
                    real = await asyncio.wait_for(
                        _read_proxy_v2(first16, reader),
                        STREAM_HANDSHAKE_TIMEOUT,
                    )
                except (StreamProtocolError, asyncio.TimeoutError, OSError):
                    return
                if real is not None:
                    source = real
            else:
                initial = first16  # un-upgraded forward: this is TLS
        try:
            sni, buffered = await asyncio.wait_for(
                _peek_client_hello(reader, initial), STREAM_HANDSHAKE_TIMEOUT
            )
        except (StreamProtocolError, asyncio.TimeoutError, OSError):
            return
        admission = None
        if abuse_limiter is not None:
            admission = abuse_limiter.begin(source)
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
            idle_timeout=idle_timeout, source=source,
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
    proxy_sources: frozenset = frozenset(),
):
    async def handle(reader, writer):
        await handle_stream_connection(
            reader, writer, host_routes=host_routes,
            abuse_limiter=abuse_limiter, idle_timeout=idle_timeout,
            proxy_sources=proxy_sources,
        )

    server = await asyncio.start_server(handle, host, port)
    logger.info("stream ingress listening on %s:%d", host, port)
    return server
