"""Connector-side endpoint for fleet-directed-stream/1 (auto-fh2nv).

Beside the tunnel serve loop, exactly as ``StreamAdapter`` sits for
``tls-stream/1``: the loop hands OPEN / DATA / STREAM_CTRL / CLOSE frames to
this adapter and never blocks on them. What the adapter yields to the
application is a :class:`FleetStreamEndpoint` — a MESSAGE transport with
``send`` / ``recv`` / ``close``, the same shape ``authenticate_fleet_transport``
and ``serve_fleet_transport`` already consume over a direct WebSocket. The
fleet handshake, roster authorization and ``ChannelCrypto`` therefore run
unchanged over a pair; the relay only ever sees ciphertext.

Two roles, one object:

* the SOURCE calls :meth:`FleetStreamAdapter.open`; it sends the
  ``fleet-open`` control op, answers its own OPEN with an open-ok, and
  returns the endpoint once READY arrives. Relay OPEN acceptance is not
  authentication: the caller runs the fleet handshake next.
* the DESTINATION is asked through ``on_offer(offer)``; an accepted offer
  becomes an endpoint on :attr:`FleetStreamAdapter.accepted` once READY
  arrives, for the fleet responder to serve.

Credit is issued for CONSUMED frames only: after ``recv`` hands a message to
the application, the frames that message fully consumed are credited back,
so a stalled reader stops the sender at zero credit without the endpoint
retaining more than one maximum message plus one frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import Awaitable, Callable, Dict, Optional

from .frames import FRAME_DATA, FRAME_STREAM_CTRL
from .fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM,
    FLEET_OPEN_DEADLINE_S,
    FLEET_STREAM_KIND,
    FLEET_STREAM_WINDOW_BYTES,
    FLEET_STREAM_WINDOW_SLOTS,
    FleetWindow,
    MessageAssembler,
    ROLE_DESTINATION,
    ROLE_SOURCE,
    build_fleet_credit,
    build_fleet_open_ok,
    encode_message,
    parse_fleet_ctrl,
    parse_fleet_open,
)
from .stream_wire import (
    RESET_ORDERLY,
    RESET_PROTOCOL,
    RESET_ROUTE_RELEASED,
    RESET_TUNNEL_LOSS,
    STREAM_MAX_DATA,
    StreamProtocolError,
    build_ctrl_eof,
    build_ctrl_reset,
)

logger = logging.getLogger("relaykit.fleet_stream")


def fleet_session(pair_id: str, source_nonce: str, destination_nonce: str) -> str:
    """The fleet handshake ``session`` both endpoints bind to: the
    capability, the never-reused pair id, and both fresh leg nonces, in a
    fixed order so source = initiator = client on both sides."""
    return f"{CAP_FLEET_DIRECTED_STREAM}:{pair_id}:{source_nonce}:{destination_nonce}"


class FleetStreamRefused(ConnectionError):
    """The relay refused admission in its reply: no pair was minted. The
    controller's STAND_DOWN input — a typed reason, never a code, so it
    cannot be mistaken for a post-admission teardown."""

    def __init__(self, reason: str):
        super().__init__(f"fleet-open refused: {reason}")
        self.reason = reason


class FleetStreamClosed(ConnectionError):
    """The pair ended AFTER admission; ``code`` is the reset code (None for
    a local close). The controller's RETRY-or-STOP input, by code."""

    def __init__(self, pair_id: str, code: Optional[int]):
        super().__init__(f"fleet stream {pair_id[:8]} closed (code {code})")
        self.pair_id = pair_id
        self.code = code


class FleetStreamEndpoint:
    """One directed stream on this connector, as a message transport."""

    def __init__(self, adapter: "FleetStreamAdapter", channel_id: bytes,
                 offer: dict, *, window_bytes: int, window_slots: int):
        self._adapter = adapter
        self.channel_id = channel_id
        self.pair_id: str = offer["pair_id"]
        self.nonce: str = offer["leg_nonce"]
        self.role: str = offer["role"]
        self.operation_id: str = offer["operation_id"]
        self.peer_persona_pub: str = offer["peer_persona_pub"]
        self.peer_machine: str = offer["peer_machine"]
        #: UNVERIFIED hint from the source about its durable fleet key; the
        #: destination may use it as the expected value of its own fleet
        #: handshake. It grants nothing.
        self.claimed_machine_pub: Optional[str] = offer["claimed_machine_pub"]
        #: Set at READY: the fleet handshake session string.
        self.session: Optional[str] = None
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.reset_code: Optional[int] = None
        #: What we offered to receive; the relay may not exceed it.
        self._offered_bytes = int(window_bytes)
        self._offered_slots = int(window_slots)
        self._inflight_bytes = 0
        self._inflight_slots = 0
        #: What the peer offered, learned at READY; replenished by credit.
        self.send_window = FleetWindow()
        self._send_credit = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._assembler = MessageAssembler()
        self._recv_eof = False
        self._sent_eof = False

    # -- frame entry (from the serve loop; never blocks) ------------------

    def _on_data(self, payload: bytes) -> None:
        if self.closed.is_set():
            return
        if (
            not payload or len(payload) > STREAM_MAX_DATA
            or self._inflight_bytes + len(payload) > self._offered_bytes
            or self._inflight_slots + 1 > self._offered_slots
        ):
            self._fail(RESET_PROTOCOL)
            return
        self._inflight_bytes += len(payload)
        self._inflight_slots += 1
        self._inbound.put_nowait(("data", payload))

    def _on_ctrl(self, msg: dict) -> None:
        if self.closed.is_set():
            return
        op = msg["op"]
        if op == "fleet-ready":
            if self.ready.is_set() or msg["pair_id"] != self.pair_id:
                self._fail(RESET_PROTOCOL)
                return
            own = msg["source_nonce"] if self.role == ROLE_SOURCE else msg["destination_nonce"]
            if own != self.nonce:
                self._fail(RESET_PROTOCOL)
                return
            self.session = fleet_session(
                self.pair_id, msg["source_nonce"], msg["destination_nonce"])
            self.send_window = FleetWindow(msg["window"]["bytes"], msg["window"]["slots"])
            self._send_credit.set()
            self.ready.set()
            self._adapter._ready(self)
        elif op == "fleet-credit":
            self.send_window.grant(msg["bytes"], msg["slots"])
            self._send_credit.set()
        elif op == "eof":
            self._inbound.put_nowait(("eof", None))
        elif op == "reset":
            self._terminate(msg["code"], notify=False)
        else:  # open-ok is leg → relay only
            self._fail(RESET_PROTOCOL)

    def _on_close(self) -> None:
        self._terminate(RESET_ORDERLY, notify=False)

    def _fail(self, code: int) -> None:
        self._terminate(code, notify=True)

    def _terminate(self, code: Optional[int], *, notify: bool) -> None:
        if self.closed.is_set():
            return
        self.reset_code = code
        self.closed.set()
        self.ready.set()
        self._send_credit.set()
        self._inbound.put_nowait(("closed", code))
        self._adapter._forget(self)
        if notify and code is not None:
            self._adapter._spawn(self._adapter._send_ctrl(
                self.channel_id, build_ctrl_reset(code)))

    # -- the transport --------------------------------------------------------

    async def recv(self) -> Optional[bytes]:
        """Next whole message, or None at the peer's half-close."""
        while True:
            try:
                complete = self._assembler.next_message()
            except StreamProtocolError:
                self._fail(RESET_PROTOCOL)
                raise FleetStreamClosed(self.pair_id, RESET_PROTOCOL)
            if complete is not None:
                message, frames, bytes_ = complete
                if frames:
                    self._inflight_bytes -= bytes_
                    self._inflight_slots -= frames
                    await self._adapter._send_ctrl(
                        self.channel_id,
                        build_fleet_credit(bytes_=bytes_, slots=frames),
                    )
                return message
            if self._recv_eof:
                return None
            kind, payload = await self._inbound.get()
            if kind == "data":
                self._assembler.feed(payload)
            elif kind == "eof":
                if self._assembler.retained_bytes:
                    self._fail(RESET_PROTOCOL)
                    raise FleetStreamClosed(self.pair_id, RESET_PROTOCOL)
                self._recv_eof = True
            else:
                raise FleetStreamClosed(self.pair_id, payload)

    async def send(self, message: bytes) -> None:
        """Send one whole message, waiting for credit frame by frame."""
        data = encode_message(bytes(message))
        async with self._send_lock:
            if self.closed.is_set():
                raise FleetStreamClosed(self.pair_id, self.reset_code)
            if not self.ready.is_set() or self._sent_eof:
                raise FleetStreamClosed(self.pair_id, None)
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + STREAM_MAX_DATA]
                while not self.send_window.can_send(len(chunk)):
                    if self.closed.is_set():
                        raise FleetStreamClosed(self.pair_id, self.reset_code)
                    self._send_credit.clear()
                    await self._send_credit.wait()
                self.send_window.consume(len(chunk))
                await self._adapter._send_frame(FRAME_DATA, self.channel_id, chunk)
                offset += len(chunk)

    async def half_close(self) -> None:
        """No more messages from this side; the peer's ``recv`` returns None."""
        async with self._send_lock:
            if self.closed.is_set() or self._sent_eof:
                return
            self._sent_eof = True
            await self._adapter._send_ctrl(self.channel_id, build_ctrl_eof())

    async def close(self) -> None:
        """End the pair from this side (orderly reset)."""
        if self.closed.is_set():
            return
        self._terminate(RESET_ORDERLY, notify=False)
        with contextlib.suppress(Exception):
            await self._adapter._send_ctrl(
                self.channel_id, build_ctrl_reset(RESET_ORDERLY))


class FleetStreamAdapter:
    """Per-tunnel fleet-stream dispatcher living beside the serve loop."""

    def __init__(self, send_frame, control, *,
                 on_offer: Optional[Callable[[FleetStreamEndpoint], Awaitable[bool]]] = None,
                 window_bytes: int = FLEET_STREAM_WINDOW_BYTES,
                 window_slots: int = FLEET_STREAM_WINDOW_SLOTS):
        self._send_frame = send_frame
        self._control = control
        self._on_offer = on_offer
        self._window_bytes = int(window_bytes)
        self._window_slots = int(window_slots)
        self._endpoints: Dict[bytes, FleetStreamEndpoint] = {}
        #: operation_id -> future resolved with the READY endpoint (source).
        self._pending_opens: Dict[str, asyncio.Future] = {}
        #: Accepted DESTINATION endpoints, delivered at READY.
        self.accepted: asyncio.Queue = asyncio.Queue()
        self._tasks: set = set()
        self.closed = False

    # -- plumbing -----------------------------------------------------------

    async def _send_ctrl(self, channel_id: bytes, payload: bytes) -> None:
        await self._send_frame(FRAME_STREAM_CTRL, channel_id, payload)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _forget(self, endpoint: FleetStreamEndpoint) -> None:
        if self._endpoints.get(endpoint.channel_id) is endpoint:
            del self._endpoints[endpoint.channel_id]
        pending = self._pending_opens.get(endpoint.operation_id)
        if pending is not None and not pending.done():
            pending.set_exception(
                FleetStreamClosed(endpoint.pair_id, endpoint.reset_code))

    def _ready(self, endpoint: FleetStreamEndpoint) -> None:
        if endpoint.role == ROLE_SOURCE:
            pending = self._pending_opens.pop(endpoint.operation_id, None)
            if pending is not None and not pending.done():
                pending.set_result(endpoint)
        else:
            self.accepted.put_nowait(endpoint)

    # -- serve-loop dispatch ------------------------------------------------

    def dispatch_open(self, channel_id: bytes, meta: dict) -> bool:
        """A FRAME_OPEN of kind fleet-stream. Handled on its own task so an
        application's acceptance decision never stalls the serve loop."""
        if not isinstance(meta, dict) or meta.get("kind") != FLEET_STREAM_KIND:
            return False
        self._spawn(self._open(channel_id, meta))
        return True

    def dispatch_data(self, channel_id: bytes, payload: bytes) -> bool:
        endpoint = self._endpoints.get(channel_id)
        if endpoint is None:
            return False
        endpoint._on_data(payload)
        return True

    def dispatch_ctrl(self, channel_id: bytes, payload: bytes) -> bool:
        endpoint = self._endpoints.get(channel_id)
        if endpoint is None:
            return False
        try:
            endpoint._on_ctrl(parse_fleet_ctrl(payload))
        except StreamProtocolError:
            endpoint._fail(RESET_PROTOCOL)
        return True

    def dispatch_close(self, channel_id: bytes) -> bool:
        endpoint = self._endpoints.get(channel_id)
        if endpoint is None:
            return False
        endpoint._on_close()
        return True

    async def _open(self, channel_id: bytes, meta: dict) -> None:
        try:
            offer = parse_fleet_open(meta)
        except StreamProtocolError:
            await self._refuse(channel_id, RESET_PROTOCOL)
            return
        if self.closed or channel_id in self._endpoints:
            await self._refuse(channel_id, RESET_PROTOCOL)
            return
        endpoint = FleetStreamEndpoint(
            self, channel_id, offer,
            window_bytes=self._window_bytes, window_slots=self._window_slots,
        )
        if offer["role"] == ROLE_SOURCE:
            # Only an open THIS connector asked for is a source leg here.
            if offer["operation_id"] not in self._pending_opens:
                await self._refuse(channel_id, RESET_ROUTE_RELEASED)
                return
        else:
            accept = False
            if self._on_offer is not None:
                try:
                    accept = bool(await self._on_offer(endpoint))
                except Exception:
                    logger.warning("fleet stream offer handler failed",
                                   exc_info=True)
            if not accept:
                await self._refuse(channel_id, RESET_ROUTE_RELEASED)
                return
        self._endpoints[channel_id] = endpoint
        try:
            await self._send_ctrl(channel_id, build_fleet_open_ok(
                nonce=endpoint.nonce, bytes_=self._window_bytes,
                slots=self._window_slots,
            ))
        except Exception:
            endpoint._terminate(RESET_TUNNEL_LOSS, notify=False)

    async def _refuse(self, channel_id: bytes, code: int) -> None:
        with contextlib.suppress(Exception):
            await self._send_ctrl(channel_id, build_ctrl_reset(code))

    # -- the source's call --------------------------------------------------

    async def open(self, dst_persona_pub: str, dst_machine: str, *,
                   operation_id: Optional[str] = None,
                   claimed_machine_pub: Optional[str] = None,
                   timeout: float = FLEET_OPEN_DEADLINE_S) -> FleetStreamEndpoint:
        """Open a directed stream to the exact destination slot and return
        its endpoint once the relay's READY has arrived.

        Three distinct failures, for three distinct controller responses:
        :class:`FleetStreamRefused` (admission refused, typed reason: stand
        down), :class:`FleetStreamClosed` (the pair was minted and then
        ended, reset code: retry or stop by code), and a bare
        ``ConnectionError`` (this tunnel could not carry the request or no
        READY arrived within *timeout*: a transport fault, retry)."""
        operation_id = operation_id or secrets.token_hex(16)
        if operation_id in self._pending_opens:
            raise ConnectionError("operation-already-pending")
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_opens[operation_id] = future
        args = {
            "dst_persona_pub": dst_persona_pub, "dst_machine": dst_machine,
            "operation_id": operation_id,
        }
        if claimed_machine_pub is not None:
            args["claimed_machine_pub"] = claimed_machine_pub
        try:
            reply = await self._control("fleet-open", args, timeout=timeout)
            if not (isinstance(reply, dict) and reply.get("ok") is True):
                reason = reply.get("error") if isinstance(reply, dict) else "refused"
                raise FleetStreamRefused(str(reason))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionError("fleet-open: no READY before the deadline") from exc
        finally:
            self._pending_opens.pop(operation_id, None)
            if not future.done():
                future.cancel()

    # -- tunnel loss --------------------------------------------------------

    async def shutdown(self) -> None:
        """The tunnel is gone: every endpoint ends with code 6, every
        pending open fails, no task survives."""
        self.closed = True
        for endpoint in list(self._endpoints.values()):
            endpoint._terminate(RESET_TUNNEL_LOSS, notify=False)
        for operation_id, future in list(self._pending_opens.items()):
            if not future.done():
                future.set_exception(ConnectionError("tunnel lost during fleet-open"))
        self._pending_opens.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
