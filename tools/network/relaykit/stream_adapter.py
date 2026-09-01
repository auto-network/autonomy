"""Connector-side raw-stream plumbing (tls-stream/1, auto-9z1xh).

The dashboard supplies ONE async callable — ``open_stream(host,
reservation) -> (StreamReader, StreamWriter) | None`` — that dials only
local Caddy (or, in tests, a TCP echo). Everything else on the wire is
owned here: open-ok + credit issuance/consumption, ≤64 KiB DATA framing,
fairness at frame granularity, eof ↔ TCP half-close mapping, reset codes,
and deterministic teardown on tunnel loss. The encrypted request/response
handler seam is never involved; payload bytes are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .frames import FRAME_CLOSE, FRAME_DATA, FRAME_STREAM_CTRL
from .stream_wire import (
    CreditWindow,
    ReplenishTracker,
    RESET_ORDERLY,
    RESET_PROTOCOL,
    RESET_ROUTE_RELEASED,
    RESET_TUNNEL_LOSS,
    STREAM_INITIAL_CREDIT,
    STREAM_MAX_DATA,
    StreamProtocolError,
    build_ctrl_credit,
    build_ctrl_eof,
    build_ctrl_open_ok,
    build_ctrl_reset,
    parse_ctrl,
)

logger = logging.getLogger("relaykit.stream")


def tcp_dial_handler(host: str, port: int):
    """The reference stream handler: dial one fixed local TCP target.
    The dashboard passes local Caddy's Compose address; harnesses pass an
    echo server."""

    async def open_stream(serving_host: str, reservation: str):
        try:
            return await asyncio.open_connection(host, port)
        except OSError:
            return None

    return open_stream


class _ConnectorStream:
    """One raw stream on the connector: local socket ⇄ tunnel frames."""

    def __init__(self, adapter: "StreamAdapter", channel_id: bytes,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 relay_credit: int):
        self.adapter = adapter
        self.channel_id = channel_id
        self.reader = reader
        self.writer = writer
        #: what the relay lets us send toward it (browser-bound bytes)
        self.send_window = CreditWindow(relay_credit)
        self.send_credit_event = asyncio.Event()
        #: what we granted the relay (Caddy-bound bytes), replenished
        #: after each local write completes
        self.granted_outstanding = STREAM_INITIAL_CREDIT
        self.replenish = ReplenishTracker()
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.tasks: list[asyncio.Task] = []
        self.closed = asyncio.Event()
        self._sent_eof = False
        self._recv_eof = False

    # -- frame entry (called from the connector serve loop; never blocks) --

    def on_data(self, payload: bytes) -> None:
        self.inbound.put_nowait(("data", payload))

    def on_ctrl(self, msg: dict) -> None:
        self.inbound.put_nowait(("ctrl", msg))

    def on_close(self) -> None:
        self.inbound.put_nowait(("close", None))

    # -- pumps -------------------------------------------------------------

    async def run(self) -> None:
        """The from-relay pump owns the stream lifetime: it returns on
        reset, close, error, or orderly completion (both directions
        EOF'd). Half-close keeps the opposite direction flowing."""
        self.tasks = [
            asyncio.create_task(self._pump_to_relay()),
            asyncio.create_task(self._pump_from_relay()),
        ]
        try:
            await self.tasks[1]
        except asyncio.CancelledError:
            pass
        finally:
            await self._teardown()

    async def _pump_to_relay(self) -> None:
        """Local socket → DATA frames, bounded by the relay's grant."""
        send = self.adapter.send_frame
        try:
            while True:
                while self.send_window.sendable <= 0:
                    self.send_credit_event.clear()
                    await self.send_credit_event.wait()
                chunk = await self.reader.read(
                    min(STREAM_MAX_DATA, self.send_window.sendable)
                )
                if not chunk:
                    if not self._sent_eof:
                        self._sent_eof = True
                        await send(FRAME_STREAM_CTRL, self.channel_id,
                                   build_ctrl_eof())
                        # Let the lifetime-owning pump re-check orderly
                        # completion (both directions now EOF'd?).
                        self.inbound.put_nowait(("eof-sent", None))
                    return
                self.send_window.consume(len(chunk))
                await send(FRAME_DATA, self.channel_id, chunk)
        except (ConnectionError, OSError):
            await self._reset(RESET_ORDERLY)
        except StreamProtocolError:
            await self._reset(RESET_PROTOCOL)

    async def _pump_from_relay(self) -> None:
        """Relay frames → local socket, replenishing our grant on drain."""
        send = self.adapter.send_frame
        try:
            while True:
                kind, payload = await self.inbound.get()
                if kind == "data":
                    if len(payload) > STREAM_MAX_DATA or \
                            len(payload) > self.granted_outstanding:
                        await self._reset(RESET_PROTOCOL)
                        return
                    self.granted_outstanding -= len(payload)
                    self.writer.write(payload)
                    await self.writer.drain()
                    grant = self.replenish.consumed(len(payload))
                    if grant is not None:
                        self.granted_outstanding += grant
                        await send(FRAME_STREAM_CTRL, self.channel_id,
                                   build_ctrl_credit(grant))
                elif kind == "ctrl":
                    op = payload["op"]
                    if op == "credit":
                        self.send_window.grant(payload["add"])
                        self.send_credit_event.set()
                    elif op == "eof":
                        if self._recv_eof:
                            await self._reset(RESET_PROTOCOL)
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
                    else:  # open-ok after open: protocol violation
                        await self._reset(RESET_PROTOCOL)
                        return
                elif kind == "eof-sent":
                    if self._recv_eof:
                        return  # orderly: both directions done
                else:  # close
                    return
        except (ConnectionError, OSError):
            await self._reset(RESET_ORDERLY)

    async def _reset(self, code: int) -> None:
        with contextlib.suppress(Exception):
            await self.adapter.send_frame(
                FRAME_STREAM_CTRL, self.channel_id, build_ctrl_reset(code)
            )

    async def _teardown(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()
        self.adapter.forget(self.channel_id)
        self.closed.set()


class StreamAdapter:
    """Per-tunnel raw-stream dispatcher living beside the serve loop."""

    def __init__(self, handler, send_frame, *, lease_lookup):
        self._handler = handler
        self.send_frame = send_frame
        #: reservation -> host map of the connector's registered leases —
        #: the seam's exact-pair admission check (r4 §4.4).
        self._lease_lookup = lease_lookup
        self._streams: dict[bytes, _ConnectorStream] = {}
        self._stream_tasks: set[asyncio.Task] = set()

    def forget(self, channel_id: bytes) -> None:
        self._streams.pop(channel_id, None)

    def dispatch_data(self, channel_id: bytes, payload: bytes) -> bool:
        stream = self._streams.get(channel_id)
        if stream is None:
            return False
        stream.on_data(payload)
        return True

    def dispatch_ctrl(self, channel_id: bytes, payload: bytes) -> None:
        stream = self._streams.get(channel_id)
        if stream is None:
            return  # stale frame for a torn-down stream: ignored
        try:
            stream.on_ctrl(parse_ctrl(payload))
        except StreamProtocolError:
            stream.on_ctrl({"op": "reset", "code": RESET_PROTOCOL})

    def dispatch_close(self, channel_id: bytes) -> bool:
        stream = self._streams.get(channel_id)
        if stream is None:
            return False
        stream.on_close()
        return True

    async def open(self, channel_id: bytes, meta: dict) -> None:
        """Handle one tls-stream OPEN end to end (admission → open-ok →
        pumps). Runs as its own task so a slow local dial cannot stall
        the tunnel serve loop."""
        from .stream_wire import parse_stream_open

        try:
            host, reservation, relay_credit = parse_stream_open(meta)
        except StreamProtocolError:
            await self._refuse(channel_id, RESET_PROTOCOL)
            return
        if self._lease_lookup(reservation) != host:
            await self._refuse(channel_id, RESET_ROUTE_RELEASED)
            return
        target = None
        try:
            target = await self._handler(host, reservation)
        except Exception:
            logger.warning("stream handler failed host=%s", host)
        if target is None:
            await self._refuse(channel_id, RESET_ROUTE_RELEASED)
            return
        reader, writer = target
        stream = _ConnectorStream(self, channel_id, reader, writer,
                                  relay_credit)
        self._streams[channel_id] = stream
        try:
            await self.send_frame(FRAME_STREAM_CTRL, channel_id,
                                  build_ctrl_open_ok(
                                      credit=STREAM_INITIAL_CREDIT))
        except Exception:
            self.forget(channel_id)
            with contextlib.suppress(Exception):
                writer.close()
            return
        task = asyncio.create_task(stream.run())
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    async def _refuse(self, channel_id: bytes, code: int) -> None:
        with contextlib.suppress(Exception):
            await self.send_frame(FRAME_STREAM_CTRL, channel_id,
                                  build_ctrl_reset(code))

    async def shutdown(self) -> None:
        """Tunnel loss: close every local socket, cancel every pump —
        zero live stream tasks/sockets survive (seam §4.3 code 6)."""
        streams = list(self._streams.values())
        self._streams.clear()
        for stream in streams:
            stream.on_ctrl({"op": "reset", "code": RESET_TUNNEL_LOSS})
        tasks = list(self._stream_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for stream in streams:
            with contextlib.suppress(Exception):
                stream.writer.close()
