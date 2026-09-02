"""Registry-side relay — spec §5.1: viewer channels muxed down org tunnels.

Two WebSocket surfaces:

- ``/t/{org}`` — ONE persistent outbound connection per org, dialed by
  the org's dashboard. Authenticated by a ``tunnel:serve``-scoped idkit
  hello verified against the org binding's root key (the same I4
  discipline as every registry mutation). A newly authenticated tunnel
  REPLACES a previous one — that is what makes reconnect after a
  half-dead TCP session work.
- ``/v1/links/{token}/channel`` — where the bootloader connects. The
  token resolves exactly like the envelope endpoint; unknown, expired,
  revoked, dead-binding, and dashboard-offline all close with the same
  code (4404). The bootloader already learns token liveness from the envelope
  HTTP status, so after a valid envelope this close honestly means no serving
  tunnel is available; the UI reports the dashboard as disconnected.

The relay routes opaque frames (``relaykit.frames``) between the two.
It never parses channel payloads, holds no channel keys, and cannot
read or forge channel plaintext (I5) — what it can observe is exactly
the accepted metadata set: token, org, timing, volume.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import re
import uuid as _uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable, Coroutine, Dict, List, NamedTuple, Optional, Tuple

from starlette.websockets import WebSocket, WebSocketDisconnect

from tools.network.idkit import (
    ChainVerifyError,
    DelegationCert,
    MalformedError,
    canonical_json,
    generate_token,
    verify_chain,
    verify_signature,
)
from tools.network.relaykit.frames import (
    CTRL_CHANNEL_ID,
    FRAME_CLOSE,
    FRAME_CTRL,
    FRAME_DATA,
    FRAME_OPEN,
    FRAME_STREAM_CTRL,
    FrameError,
    decode_frame,
    encode_frame,
    new_channel_id,
    VIEWER_KIND_FEED,
    tag_viewer_message,
)
from tools.network.relaykit.stream_wire import (
    CAP_TLS_STREAM,
    RESET_ROUTE_RELEASED,
)
from tools.network.relaykit.hello import (
    HELLO_FIELDS_V2,
    HELLO_VERSION,
    HELLO_VERSION_2,
    SERVING_MACHINE_HELLO_DOMAIN,
    TUNNEL_HELLO_DOMAIN_V2,
    HelloError,
    hello_core,
    hello_signing_input,
    parse_tunnel_hello,
)

from .abuse import ChannelLease, RelayAbuseLimiter
from .signing import MAX_CLOCK_SKEW, link_operation_receipt_input
from .store import LinkGrant, RegistryStore

# WS close codes (4000-4999 = application-defined).
CLOSE_UNAUTHENTICATED = 4403
CLOSE_UNKNOWN_LINK = 4404  # unknown token or no serving tunnel
CLOSE_PROTOCOL_MISMATCH = 4406
CLOSE_REPLACED = 4409
CLOSE_VIEWER_QUEUE_OVERFLOW = 4413
# Resumable, not an error: this listener fell behind a stream's retention
# window. The viewer reconnects and requests its own offset through history
# (auto-albp6.7) -- distinct from every other close code above, none of
# which are true for it.
CLOSE_LISTENER_FELL_BEHIND = 4416

# Stream (auto-albp6.7) retention, in precedence order -- the order is
# load-bearing, see Stream._apply_retention.
# Owned by tools.network.clock (an expiry sweep); re-exported here.
from tools.network.clock import STREAM_EXPIRY_SECONDS

logger = logging.getLogger(__name__)

STREAM_BUFFER_CAP_BYTES = 1024 * 1024
STREAM_MIN_RETAINED_FRAMES = 10

# A single org tunnel may carry at most this many concurrent viewer channels.
# Each channel is independently bounded to VIEWER_QUEUE_MAX_BYTES of relay
# buffer (per-channel writer queue), so this caps total relay memory per org
# at MAX * VIEWER_QUEUE_MAX_BYTES and stops any bearer-link holder from
# opening unbounded attachment-streaming channels on the shared tunnel.
# Accounting is per authenticated tunnel/org, never global. A one-line
# operator policy knob.
MAX_VIEWER_CHANNELS_PER_TUNNEL = 128

# A viewer never gets to make the org tunnel retain an attachment-sized
# window.  The channel record layer currently emits 128 KiB records, so this
# is a small cushion for ordinary scheduler/network jitter while remaining
# far below the attachment protocol's 8 MiB pull window.
VIEWER_QUEUE_MAX_BYTES = 2 * 1024 * 1024


class _ViewerRelayChannel:
    """One viewer's bounded dashboard→viewer writer.

    ``try_enqueue`` is deliberately synchronous: the shared tunnel receive
    loop must never wait for a viewer socket.  Bytes remain charged while a
    send is in flight, not merely while they sit in ``asyncio.Queue``, so a
    wedged socket cannot hide one unbounded payload outside the accounting.
    """

    def __init__(
        self,
        ws: WebSocket,
        *,
        on_writer_failure: Callable[["_ViewerRelayChannel"], None],
        max_queued_bytes: int = VIEWER_QUEUE_MAX_BYTES,
        abuse_lease: ChannelLease | None = None,
        exempt_bytes: bool = False,
    ):
        self.ws = ws
        self.max_queued_bytes = max_queued_bytes
        #: token of the Stream (auto-albp6.7) this channel is a listener
        #: of, if any -- set by viewer_endpoint after attaching, read by
        #: Tunnel to detach the listener wherever this channel is torn
        #: down. None for every ordinary (non-session) channel.
        self.stream_token: Optional[str] = None
        self._on_writer_failure = on_writer_failure
        self._abuse_lease = abuse_lease
        # A fleet:join channel is a roster-authenticated peer doing bulk
        # replication (bounded by the checkpoint codec's own MAX_CHECKPOINT_*
        # limits, not this org's public-viewer byte buckets). It is
        # categorically not the anonymous bootloader/attachment traffic the
        # abuse limiter's byte buckets exist to bound -- active-connection
        # and admission-rate accounting still apply via abuse_lease, only
        # the byte-rate charge is skipped.
        self._exempt_bytes = exempt_bytes
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._queued_bytes = 0
        self._closing = False
        self._close_task: Optional[asyncio.Task] = None
        self._writer_task = asyncio.create_task(self._writer())
        self._writer_task.add_done_callback(self._writer_done)

    @property
    def queued_bytes(self) -> int:
        """Bytes queued or currently blocked in ``send_bytes``."""
        return self._queued_bytes

    def try_enqueue(self, payload: bytes) -> bool:
        """Queue *payload* without waiting; false means the byte cap hit."""
        if self._closing:
            return False
        payload = bytes(payload)
        if len(payload) > self.max_queued_bytes - self._queued_bytes:
            return False
        if not self._exempt_bytes and self._abuse_lease is not None \
                and not self._abuse_lease.charge_bytes(len(payload)):
            # The public close remains deliberately uniform. Starting the
            # close here also makes stream fan-out drop the listener through
            # its existing failed-enqueue cleanup path.
            logger.warning(
                "relay dial closed mid-stream (4404): byte-rate lease exhausted, "
                "token=%s payload_size=%d queued_bytes=%d",
                self.stream_token, len(payload), self._queued_bytes,
            )
            self.start_close(CLOSE_UNKNOWN_LINK)
            return False
        self._queued_bytes += len(payload)
        self._queue.put_nowait(payload)
        return True

    def start_close(self, code: int) -> asyncio.Task:
        """Cancel the writer, release queued bytes, and close asynchronously."""
        if self._close_task is not None:
            return self._close_task
        self._closing = True
        self._release_abuse_lease()
        self._release_pending()
        self._writer_task.cancel()
        self._close_task = asyncio.create_task(self._finish_close(code))
        return self._close_task

    async def close(self, code: int) -> None:
        await self.start_close(code)

    async def wait_closed(self) -> None:
        if self._close_task is not None:
            await self._close_task
        else:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer_task

    async def _writer(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self.ws.send_bytes(payload)
            finally:
                self._queued_bytes -= len(payload)
                self._queue.task_done()

    def _writer_done(self, task: asyncio.Task) -> None:
        # Always retrieve the exception so a failed socket send does not
        # become an unhandled-task warning.
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()
        if self._closing:
            return
        self._closing = True
        self._release_abuse_lease()
        self._release_pending()
        self._on_writer_failure(self)
        self._close_task = asyncio.create_task(
            _close_quietly(self.ws, 1001)
        )

    def _release_pending(self) -> None:
        while True:
            try:
                payload = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queued_bytes -= len(payload)
            self._queue.task_done()

    def _release_abuse_lease(self) -> None:
        if self._abuse_lease is not None:
            self._abuse_lease.release()

    async def _finish_close(self, code: int) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._writer_task
        await _close_quietly(self.ws, code)


class _StreamFrame(NamedTuple):
    """One buffered fan-out frame (auto-albp6.7). ``seq`` is a buffer-
    position index internal to the relay -- never shown to a viewer, not
    part of any wire protocol, and carries no ordering meaning to the
    viewer, whose ordering comes from the byte ranges the connector puts
    in the content."""
    seq: int
    written_at: float
    payload: bytes


class Listener:
    """One channel's membership in a Stream's audience (auto-albp6.7).

    Delivery reuses the channel's own already-existing bounded writer
    (``_ViewerRelayChannel.try_enqueue``) instead of a second queue and
    writer task on the same socket: two independent writers racing to
    call ``send_bytes`` on one WebSocket could interleave and corrupt
    frames, and there is already exactly one writer per socket, which is
    the invariant worth keeping. ``cursor`` is the next buffer seq this
    listener still needs; it only advances when a hand-off actually
    succeeds, so a listener whose own queue is currently full falls
    behind rather than silently losing frames.
    """

    def __init__(self, channel_id: bytes, viewer_channel: _ViewerRelayChannel, cursor: int):
        self.channel_id = channel_id
        self.viewer_channel = viewer_channel
        self.cursor = cursor


class Stream:
    """One link's shared live buffer, fanned out to every attached
    listener so publisher cost does not scale with audience size
    (auto-albp6.7, drivers D1 and D6). Created when its first listener
    attaches and destroyed when its last detaches -- publishing to a
    token with no stream discards the frame rather than creating one;
    streams are demand-driven by viewers, never by publishers.
    """

    def __init__(self, token: str):
        self.token = token
        self.buffer: "deque[_StreamFrame]" = deque()
        self.listeners: Dict[bytes, Listener] = {}
        self.retained_bytes = 0
        self._next_seq = 0

    def attach(self, channel_id: bytes, viewer_channel: _ViewerRelayChannel) -> None:
        self.listeners[channel_id] = Listener(channel_id, viewer_channel, cursor=self._next_seq)

    def detach(self, channel_id: bytes) -> None:
        self.listeners.pop(channel_id, None)

    def publish(self, payload: bytes, now: float) -> List[Tuple[bytes, _ViewerRelayChannel]]:
        """Fan *payload* out to every attached listener. Returns the
        ``(channel_id, viewer_channel)`` pairs evicted for falling
        behind retention, for the caller (Tunnel) to close and account
        for the same way any other channel closure is handled -- this
        class only manages its own buffer/listener state, never touches
        Tunnel.channels directly."""
        frame = _StreamFrame(self._next_seq, now, bytes(payload))
        self._next_seq += 1
        self.buffer.append(frame)
        self.retained_bytes += len(frame.payload)
        for listener in list(self.listeners.values()):
            if listener.viewer_channel.try_enqueue(frame.payload):
                listener.cursor = frame.seq + 1
        return self._apply_retention(now)

    def _apply_retention(self, now: float) -> List[Tuple[bytes, _ViewerRelayChannel]]:
        # Rule 1 (outermost, applied first): nothing older than the expiry
        # window survives, regardless of consumption -- without this a
        # listener that attaches and never advances would pin retention
        # indefinitely at no ongoing cost.
        while self.buffer and now - self.buffer[0].written_at > STREAM_EXPIRY_SECONDS:
            dropped = self.buffer.popleft()
            self.retained_bytes -= len(dropped.payload)
        # Rule 2: once every current listener's cursor is past a frame, it
        # is fully delivered -- discard it immediately, not just eventually.
        if self.listeners:
            min_cursor = min(listener.cursor for listener in self.listeners.values())
            while self.buffer and self.buffer[0].seq < min_cursor:
                dropped = self.buffer.popleft()
                self.retained_bytes -= len(dropped.payload)
        # Rule 3: size cap, but never below the floor -- one oversized turn
        # must not evict the whole audience. This is the only rule that can
        # drop a frame a listener still needs, so it is the only one that
        # produces fallen-behind evictions.
        fallen_behind: List[Tuple[bytes, _ViewerRelayChannel]] = []
        while (
            self.retained_bytes > STREAM_BUFFER_CAP_BYTES
            and len(self.buffer) > STREAM_MIN_RETAINED_FRAMES
        ):
            dropped = self.buffer.popleft()
            self.retained_bytes -= len(dropped.payload)
            for channel_id, listener in list(self.listeners.items()):
                if listener.cursor <= dropped.seq:
                    del self.listeners[channel_id]
                    fallen_behind.append((channel_id, listener.viewer_channel))
        return fallen_behind


_PERSONA_PUB_RE = re.compile(r"^[0-9a-f]{64}$")


class Tunnel:
    """A live dashboard connection plus its open viewer channels."""

    def __init__(
        self,
        ws: WebSocket,
        org: str,
        *,
        persona_pub: str | None = None,
        signer_pub: str | None = None,
        machine: str = "",
        caps: tuple = (),
        version: int = HELLO_VERSION_2,
    ):
        self.ws = ws
        self.org = org
        #: Hello version this connection authenticated with (operator readout).
        self.version = version
        #: Last control-op outcome for the operator readout (auto-7df7o);
        #: {op, result, reason} or None. Operational, never a token/payload.
        self.last_control: Optional[dict] = None
        # Connection-memory routing facts only. None of these values is
        # written to link_sessions, node_hints, logs, metrics, or a
        # history table.
        self.persona_pub = persona_pub
        self.signer_pub = signer_pub
        #: Enrolled machine pub for v2 hellos; "" is the legacy v1 slot.
        self.machine = machine
        #: Accepted capability intersection for this connection.
        self.caps = caps
        #: Relay-minted per-connection identity: lease generations and
        #: channel ids are fenced on it and never survive a reconnect.
        self.connection_id = new_channel_id().hex()
        self.channels: Dict[bytes, _ViewerRelayChannel] = {}
        #: tls-stream/1 raw streams (auto-9z1xh), channel_id-keyed.
        #: Registered by the ingress, dispatched by the receive loop,
        #: reset by lease removal, torn down with the tunnel.
        self.raw_streams: Dict[bytes, object] = {}
        self.streams: Dict[str, Stream] = {}
        self._send_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()

    async def send_frame(self, frame_type: int, channel_id: bytes, payload: bytes = b"") -> None:
        async with self._send_lock:
            await self.ws.send_bytes(encode_frame(frame_type, channel_id, payload))

    def add_viewer(
        self,
        channel_id: bytes,
        ws: WebSocket,
        *,
        abuse_lease: ChannelLease | None = None,
        exempt_bytes: bool = False,
    ) -> _ViewerRelayChannel:
        channel = _ViewerRelayChannel(
            ws,
            on_writer_failure=lambda failed: self._writer_failed(
                channel_id, failed
            ),
            abuse_lease=abuse_lease,
            exempt_bytes=exempt_bytes,
        )
        self.channels[channel_id] = channel
        return channel

    def enqueue_viewer(self, channel_id: bytes, payload: bytes) -> None:
        """Enqueue only; a slow viewer never blocks the tunnel read loop."""
        channel = self.channels.get(channel_id)
        if channel is None:
            return
        if channel.try_enqueue(payload):
            return
        if self.channels.get(channel_id) is channel:
            del self.channels[channel_id]
        channel.start_close(CLOSE_VIEWER_QUEUE_OVERFLOW)
        self._notify_dashboard_closed(channel_id)

    def close_viewer(self, channel_id: bytes, code: int) -> None:
        channel = self.channels.pop(channel_id, None)
        if channel is not None:
            channel.start_close(code)

    def detach_viewer(
        self, channel_id: bytes, channel: _ViewerRelayChannel
    ) -> bool:
        if self.channels.get(channel_id) is not channel:
            return False
        del self.channels[channel_id]
        return True

    def attach_listener(
        self, token: str, channel_id: bytes, channel: _ViewerRelayChannel
    ) -> None:
        """*channel* becomes a listener of *token*'s stream, creating it
        if this is the first listener (auto-albp6.7)."""
        self.streams.setdefault(token, Stream(token)).attach(channel_id, channel)

    def detach_listener(self, token: str, channel_id: bytes) -> None:
        """Remove *channel_id* from *token*'s stream, destroying the
        stream once its last listener is gone."""
        stream = self.streams.get(token)
        if stream is None:
            return
        stream.detach(channel_id)
        if not stream.listeners:
            del self.streams[token]

    def publish_stream(self, token: str, payload: bytes, now: float) -> bool:
        """Fan *payload* out to every listener of *token*'s stream.
        Returns False (frame discarded) if no stream exists -- publishing
        never creates one; only a listener attaching does."""
        stream = self.streams.get(token)
        if stream is None:
            return False
        # Marked once, here, so the retained buffer holds exactly the bytes
        # a replaying listener will be sent.
        fallen_behind = stream.publish(
            tag_viewer_message(VIEWER_KIND_FEED, payload), now
        )
        for channel_id, channel in fallen_behind:
            if self.channels.get(channel_id) is channel:
                del self.channels[channel_id]
            channel.start_close(CLOSE_LISTENER_FELL_BEHIND)
            self._notify_dashboard_closed(channel_id)
        if not stream.listeners:
            del self.streams[token]
        return True

    def reset_raw_streams(self, reservation: str | None, code: int) -> None:
        """Signal a relay-side reset to raw streams (all, or one
        reservation's) — used by lease removal (code 5). Synchronous:
        each stream's own pump performs the teardown."""
        for stream in list(self.raw_streams.values()):
            if reservation is None or stream.reservation == reservation:
                stream.signal_reset(code)

    async def close_all_viewers(self, code: int) -> None:
        channels = list(self.channels.values())
        self.channels.clear()
        self.streams.clear()
        raw_streams = list(self.raw_streams.values())
        self.raw_streams.clear()
        for stream in raw_streams:
            with contextlib.suppress(Exception):
                await stream.teardown()
        if channels:
            await asyncio.gather(
                *(channel.close(code) for channel in channels),
                return_exceptions=True,
            )
        tasks = list(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _writer_failed(
        self, channel_id: bytes, failed: _ViewerRelayChannel
    ) -> None:
        if self.channels.get(channel_id) is not failed:
            return
        del self.channels[channel_id]
        if failed.stream_token is not None:
            self.detach_listener(failed.stream_token, channel_id)
        self._notify_dashboard_closed(channel_id)

    def _notify_dashboard_closed(self, channel_id: bytes) -> None:
        self._spawn_background(self.send_frame(FRAME_CLOSE, channel_id))

    def _spawn_background(self, coro: Coroutine) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(done)


class TunnelHub:
    """org → (persona, machine) → live tunnel. All state is in-memory:
    tunnels are ephemeral by nature and re-dialed by connectors after any
    restart.

    Reconnect replaces only the same (persona, machine) slot — distinct
    machines and personas of one org coexist (auto-0zdky). v1 connectors
    occupy the empty-machine slot, preserving legacy replacement
    semantics among themselves. Org-level viewer selection is the
    TLA-verified pool rule: least-loaded live tunnel, pinned by the
    caller for the connection's lifetime.
    """

    def __init__(self):
        self._tunnels: Dict[str, Dict[tuple, Tunnel]] = {}

    @staticmethod
    def _slot(tunnel: Tunnel) -> tuple:
        return (tunnel.persona_pub, tunnel.machine)

    def get(self, org: str) -> Optional[Tunnel]:
        """Org compatibility selector: the least-loaded live tunnel.
        Callers pin the returned tunnel for the connection lifetime."""
        slots = self._tunnels.get(org)
        if not slots:
            return None
        return min(slots.values(), key=lambda t: len(t.channels))

    def get_slot(
        self, org: str, persona_pub: str, machine: str
    ) -> Optional[Tunnel]:
        return self._tunnels.get(org, {}).get((persona_pub, machine))

    def tunnels_for(self, org: str) -> List[Tunnel]:
        return list(self._tunnels.get(org, {}).values())

    def register(self, tunnel: Tunnel) -> Optional[Tunnel]:
        """Install *tunnel*; returns the same-slot tunnel it replaced, if
        any. Never touches a different persona/machine's slot."""
        slots = self._tunnels.setdefault(tunnel.org, {})
        previous = slots.get(self._slot(tunnel))
        slots[self._slot(tunnel)] = tunnel
        _ops("tunnel.register", org=tunnel.org[:8],
             persona=(tunnel.persona_pub or "")[:16],
             machine=(tunnel.machine or "")[:16],
             version=tunnel.version, pool=len(slots),
             replaced=1 if previous is not None else 0)
        return previous

    def unregister(self, tunnel: Tunnel) -> None:
        slots = self._tunnels.get(tunnel.org)
        if slots is None:
            return
        if slots.get(self._slot(tunnel)) is tunnel:
            del slots[self._slot(tunnel)]
        _ops("tunnel.unregister", org=tunnel.org[:8],
             persona=(tunnel.persona_pub or "")[:16],
             machine=(tunnel.machine or "")[:16],
             pool=len(slots))
        if not slots:
            del self._tunnels[tunnel.org]

    async def close_revoked(
        self, org: str, signer_pub: str,
        host_routes: "HostRoutes | None" = None,
    ) -> bool:
        """Close every live tunnel authenticated by a newly revoked signer.

        Removing each from admission — hub slot AND hostname leases —
        before the socket close prevents a viewer or routed open racing
        onto an already-revoked tunnel.
        """
        matches = [
            tunnel
            for tunnel in self._tunnels.get(org, {}).values()
            if tunnel.signer_pub == signer_pub
        ]
        for tunnel in matches:
            self.unregister(tunnel)
            if host_routes is not None:
                host_routes.drop_connection(tunnel)
            await _close_quietly(tunnel.ws, CLOSE_UNAUTHENTICATED)
            await tunnel.close_all_viewers(CLOSE_UNAUTHENTICATED)
        return bool(matches)


class _ProtocolVersionMismatch(HelloError):
    def __init__(self, connector_version: int):
        self.connector_version = connector_version
        super().__init__(
            "tunnel protocol version mismatch: "
            f"connector={connector_version} registry={HELLO_VERSION_2}"
        )


@dataclass(frozen=True)
class VerifiedTunnelHello:
    """The authenticated routing identity a hello establishes."""

    persona_pub: str
    signer_pub: str
    #: Enrolled machine pub (64 hex) for v2 hellos; "" for v1 — the
    #: legacy org-slot identity.
    machine: str
    #: Capabilities the connector offered (v2), before intersection with
    #: what this registry supports.
    caps: tuple
    version: int


def _verify_tunnel_hello(
    raw, org: str, store: RegistryStore, now: int
) -> VerifiedTunnelHello:
    """The tunnel's I4 gate: hello signature + tunnel:serve chain to the
    org's bound root. Raises HelloError on any failure."""
    # Parse an integer version without accepting it yet.  We authenticate the
    # signed hello first, then return a typed mismatch naming both strict
    # versions.  A bit-flipped version therefore fails signature verification
    # rather than eliciting a trusted-looking compatibility response.
    data = parse_tunnel_hello(raw, allow_version_mismatch=True)
    if data["org"] != org:
        raise HelloError("hello org does not match tunnel path")
    if abs(now - data["ts"]) > MAX_CLOCK_SKEW:
        raise HelloError(f"hello ts outside ±{MAX_CLOCK_SKEW}s freshness window")

    binding = store.get_org(org)
    if binding is None or binding.expires_at < now:
        raise HelloError("no live binding for org")

    is_v2 = data["v"] == HELLO_VERSION_2 and set(data) == HELLO_FIELDS_V2
    try:
        if is_v2:
            core = hello_core(
                org=org,
                signer=data["signer"],
                machine=data["machine"],
                caps=data["caps"],
                ts=data["ts"],
                version=data["v"],
            )
            verify_signature(
                data["signer"], data["sig"], TUNNEL_HELLO_DOMAIN_V2 + core
            )
            try:
                verify_signature(
                    data["machine"],
                    data["machine_sig"],
                    SERVING_MACHINE_HELLO_DOMAIN + core,
                )
            except Exception as exc:
                raise HelloError(
                    "machine co-signature does not verify against the "
                    "claimed serving machine key"
                ) from exc
        else:
            verify_signature(
                data["signer"],
                data["sig"],
                hello_signing_input(
                    org, data["signer"], data["ts"], version=data["v"]
                ),
            )
        cert = DelegationCert.from_json(data["cert"])
        if cert.child_pub != data["signer"]:
            raise HelloError("cert does not delegate to the hello signer")
        store.purge_expired_revocations(now=now)
        verified = verify_chain(
            cert,
            binding.root_pub,
            org=org,
            now=now,
            revocations=store.revocation_set(org),
            required_scope="tunnel:serve",
        )
        if tuple(verified.scope) != ("tunnel:serve",):
            raise HelloError("serve cert scope must be exactly tunnel:serve")
        if verified.depth != 1:
            raise HelloError("serve cert must be issued directly by the org root")
        if (
            verified.subject_kind != "persona"
            or _PERSONA_PUB_RE.fullmatch(verified.subject_id) is None
        ):
            raise HelloError(
                "serve cert subject must be a canonical organization persona"
            )
    except (ChainVerifyError, MalformedError) as exc:
        raise HelloError(f"{type(exc).__name__}: {exc}") from exc
    if is_v2:
        # auto-e2ufw Option B (crypto ruling graph://a374b260-e4a): the
        # serving-domain machine_sig above is verified UNCONDITIONALLY. The
        # allow-set is the secondary registration/unlinkability binding —
        # hard-enforced once the org has registered any serving key, and a
        # bounded transitional ACCEPT (logged, counted) for a not-yet-
        # backfilled org, whose persona is already authenticated by the
        # root-issued tunnel:serve cert above.
        allowed = store.registered_serving_keys(org)
        if allowed:
            if data["machine"] not in allowed:
                raise HelloError(
                    "serving machine key not registered for this org"
                )
        else:
            _AUDIT_LOGGER.warning(
                "serving-key transitional-accept org=%s machine=%s "
                "(no registered serving-key set yet; backfill pending)",
                org[:8], data["machine"][:16],
            )
    if data["v"] not in (HELLO_VERSION, HELLO_VERSION_2):
        raise _ProtocolVersionMismatch(data["v"])
    return VerifiedTunnelHello(
        persona_pub=verified.subject_id,
        signer_pub=data["signer"],
        machine=data["machine"] if is_v2 else "",
        caps=tuple(data["caps"]) if is_v2 else (),
        version=data["v"],
    )


async def _close_quietly(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code)


# -- auto-0zdky: serving hostname ownership + live leases ------------------

#: The delegated serving zone every registered hostname must live under.
SERVE_BASE_DOMAIN = "serve.auto.network"
#: NamespaceReservation UUIDv5 namespace (design c880c5e6 §3.2).
RESERVATION_NAMESPACE = _uuid.UUID("6cf440db-c8b4-566c-99db-e7be17109bdc")
#: Live lease lifetime; one tunnel-wide ``host-renew-all`` keepalive at
#: roughly half-life keeps every lease alive (auto-ja0rf). The TTL is a
#: dead-man's switch for a wedged-but-connected tunnel only — teardown on
#: disconnect/release is immediate, never TTL-bound.
HOST_LEASE_TTL = 600
CAP_HOST_LEASE = "host-lease/1"
CAP_DNS01 = "dns-01/1"
#: What this registry supports; the hello ack advertises the
#: intersection with what the connector offered.
REGISTRY_CAPS = frozenset({CAP_HOST_LEASE, CAP_TLS_STREAM, CAP_DNS01})

#: serve:dns-01 op signature domain (auto-bhs3c).
DNS01_DOMAIN = b"autonomy.network.serve.dns01.v1\n"

_APP_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_RESERVED_APP_LABELS = frozenset(
    {"_autonomy", "www", "api", "relay", "registry", "auto", "serve"}
)
_PERSONA_LABEL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?-[0-9a-f]{20}\Z"
)


class HostValidationError(Exception):
    """A hostname registration that fails closed as label-invalid."""


def persona_label_suffix(persona_pub: str) -> str:
    """The cryptographic binding between a serving label and its persona:
    the first 20 lowercase hex characters of SHA-256(persona key bytes)."""
    return hashlib.sha256(bytes.fromhex(persona_pub)).hexdigest()[:20]


def validate_host_registration(
    host: str, reservation: str, persona_pub: str
) -> tuple[str, str]:
    """Validate a host-register request against the authenticated persona.

    Returns ``(app_label, persona_label)``. The reservation UUID must be
    the deterministic UUIDv5 over ``<persona_pub>\\0<app_label>`` — a
    mismatched id can never claim a host, and the label suffix must be
    the persona's own key digest, so a host under another persona's
    label fails closed here regardless of ownership state.
    """
    if not isinstance(host, str) or not isinstance(reservation, str):
        raise HostValidationError("host and reservation must be strings")
    if len(host) > 253 or host != host.strip("."):
        raise HostValidationError("host is not a valid FQDN")
    suffix = "." + SERVE_BASE_DOMAIN
    if not host.endswith(suffix):
        raise HostValidationError(f"host must end with {suffix}")
    labels = host[: -len(suffix)].split(".")
    if len(labels) != 2:
        raise HostValidationError(
            "host must be <app>.<persona-label>" + suffix
        )
    app_label, persona_label = labels
    if (
        _APP_LABEL_RE.match(app_label) is None
        or app_label in _RESERVED_APP_LABELS
    ):
        raise HostValidationError("app label is reserved or malformed")
    if _PERSONA_LABEL_RE.match(persona_label) is None:
        raise HostValidationError("persona label is malformed")
    if not persona_label.endswith("-" + persona_label_suffix(persona_pub)):
        raise HostValidationError(
            "persona label does not bind to the authenticated persona"
        )
    expected = str(
        _uuid.uuid5(RESERVATION_NAMESPACE, f"{persona_pub}\0{app_label}")
    )
    if reservation != expected:
        raise HostValidationError(
            "reservation id does not derive from persona and app label"
        )
    return app_label, persona_label


class _HostLease(NamedTuple):
    tunnel: "Tunnel"
    generation: int
    expires_at: int
    host: str


class HostRoutes:
    """Live hostname → tunnel routing state.

    Durable *ownership* (persona-bound reservation rows, monotonic
    generation counters) lives in the store; the *lease* binding a
    reservation to one authenticated connection is memory-only and dies
    with the connection — reconnects re-register under a fresh
    generation, so stale routes can never survive a tunnel loss.
    """

    def __init__(self, store: RegistryStore, now_fn, metrics=None):
        self._store = store
        self._now_fn = now_fn
        self._metrics = metrics
        self._leases: Dict[str, _HostLease] = {}
        self._by_host: Dict[str, str] = {}

    def _live(self, reservation: str) -> Optional[_HostLease]:
        lease = self._leases.get(reservation)
        if lease is None:
            return None
        if lease.expires_at < int(self._now_fn()):
            if self._metrics is not None:
                self._metrics.lease_event(
                    getattr(lease.tunnel, "org", "other"), "expire")
            self._drop(reservation)
            return None
        return lease

    def _drop(self, reservation: str) -> None:
        lease = self._leases.pop(reservation, None)
        if lease is not None and self._by_host.get(lease.host) == reservation:
            del self._by_host[lease.host]
        if lease is not None:
            # Route removal resets any live raw streams on this
            # reservation (seam §4.3 code 5) in the same act — never left
            # to the lease TTL or endpoint teardown.
            lease.tunnel.reset_raw_streams(reservation, RESET_ROUTE_RELEASED)

    def route(self, host: str) -> Optional["Tunnel"]:
        """Resolve a serving hostname to its leased live tunnel, or None.
        Every miss (unknown, unleased, expired) is indistinguishable."""
        reservation = self._by_host.get(host)
        if reservation is None:
            return None
        lease = self._live(reservation)
        return lease.tunnel if lease is not None else None

    def reservation_for(self, host: str) -> str:
        """The reservation id behind a live-routed host ("" when unrouted)."""
        reservation = self._by_host.get(host)
        if reservation is None or self._live(reservation) is None:
            return ""
        return reservation

    def register(self, tunnel: "Tunnel", reservation: str, host: str) -> dict:
        """host-register: the authenticated desired-state advertisement."""
        try:
            app_label, persona_label = validate_host_registration(
                host, reservation, tunnel.persona_pub
            )
        except HostValidationError as exc:
            raise _CtrlError("label-invalid") from exc
        owner = self._store.get_host_ownership(reservation)
        if owner is not None and owner.persona_pub != tunnel.persona_pub:
            raise _CtrlError("host-owned-elsewhere")
        if owner is not None and owner.host != host:
            # One immutable serving label per persona: a different slug
            # (or app spelling) for an existing reservation fails closed.
            raise _CtrlError("label-invalid")
        other = self._store.get_host_ownership_by_host(host)
        if other is not None and other.reservation_id != reservation:
            raise _CtrlError("host-owned-elsewhere")
        # One immutable serving label per persona: the first registration
        # binds it; a different slug for the same persona fails closed, and
        # a label already bound to a different persona key is refused
        # rather than silently renamed (design §3.2).
        bound = self._store.get_persona_label(tunnel.persona_pub)
        if bound is None:
            if not self._store.bind_persona_label(
                tunnel.persona_pub, persona_label,
                now=int(self._now_fn()),
            ):
                raise _CtrlError("host-owned-elsewhere")
        elif bound != persona_label:
            raise _CtrlError("label-invalid")
        current = self._live(reservation)
        if current is not None and current.tunnel is not tunnel:
            raise _CtrlError("lease-held")
        generation = self._store.upsert_host_ownership(
            reservation_id=reservation,
            org=tunnel.org,
            persona_pub=tunnel.persona_pub,
            host=host,
            now=int(self._now_fn()),
        )
        expires_at = int(self._now_fn()) + HOST_LEASE_TTL
        self._leases[reservation] = _HostLease(
            tunnel, generation, expires_at, host
        )
        self._by_host[host] = reservation
        if self._metrics is not None:
            self._metrics.lease_event(tunnel.org, "register")
        return {"lease": {"generation": generation, "expires_at": expires_at}}

    def renew(self, tunnel: "Tunnel", reservation: str, generation) -> dict:
        lease = self._live(reservation)
        if (
            lease is None
            or lease.tunnel is not tunnel
            or type(generation) is not int
            or generation != lease.generation
        ):
            raise _CtrlError("stale-generation")
        expires_at = int(self._now_fn()) + HOST_LEASE_TTL
        self._leases[reservation] = lease._replace(expires_at=expires_at)
        if self._metrics is not None:
            self._metrics.lease_event(tunnel.org, "renew")
        return {"lease": {"generation": lease.generation,
                          "expires_at": expires_at}}

    def renew_all(self, tunnel: "Tunnel") -> dict:
        """host-renew-all: one tunnel-wide keepalive extending every live
        lease this exact connection holds (auto-ja0rf). Liveness is a
        property of the connection, so renewal carries no reservation or
        generation — the authenticated tunnel identity selects exactly
        the leases that ``drop_connection`` would tear down."""
        expires_at = int(self._now_fn()) + HOST_LEASE_TTL
        renewed = 0
        for reservation in [
            r for r, lease in self._leases.items() if lease.tunnel is tunnel
        ]:
            lease = self._live(reservation)
            if lease is None or lease.tunnel is not tunnel:
                continue
            self._leases[reservation] = lease._replace(expires_at=expires_at)
            renewed += 1
        if self._metrics is not None and renewed:
            self._metrics.lease_event(tunnel.org, "renew_all")
        return {"renewed": renewed, "expires_at": expires_at}

    def release(self, tunnel: "Tunnel", reservation: str) -> dict:
        owner = self._store.get_host_ownership(reservation)
        if owner is None or owner.persona_pub != tunnel.persona_pub:
            raise _CtrlError("not-authorized")
        lease = self._live(reservation)
        if lease is not None and lease.tunnel is not tunnel:
            raise _CtrlError("lease-held")
        if self._metrics is not None:
            self._metrics.lease_event(tunnel.org, "release")
        self._drop(reservation)
        return {}

    def drop_connection(self, tunnel: "Tunnel") -> None:
        """Immediate fail-closed teardown of every lease this exact
        connection holds — called on disconnect and 4409 replacement."""
        for reservation in [
            r for r, lease in self._leases.items() if lease.tunnel is tunnel
        ]:
            self._drop(reservation)


# -- §D19 tunnel control frames --------------------------------------------

import json as _json  # noqa: E402  (local to the control-frame handling)
import re as _re  # noqa: E402

_CORRELATION_RE = _re.compile(r"^[0-9a-f]{32}\Z")
_UUID_RE = _re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
#: The meta fields a tunnel create-link accepts — the org:join fields
#: (invite_ref / expires_at) never ride this path; org:join keeps the
#: envelope endpoint until its own transport lands.
_CTRL_LINK_META_FIELDS = frozenset({"ttl", "label", "require_auth"})
_CENTRAL_LINK_FIELDS = frozenset(
    {"operation_id", "receipt", "signature", "origin_proof"}
)


class _CtrlError(Exception):
    """A control op that fails cleanly — replied as {ok: false}, tunnel
    stays up. (Distinct from a malformed FRAME payload, which drops it.)"""


def _central_operation(
    tunnel: "Tunnel", args: dict, store: RegistryStore, witness_key
):
    present = _CENTRAL_LINK_FIELDS & set(args)
    if not present:
        return None
    if present != _CENTRAL_LINK_FIELDS or witness_key is None:
        raise _CtrlError("Central Link execution requires the complete receipt and origin proof")
    operation_id = args.get("operation_id")
    if not isinstance(operation_id, str) or not _re.fullmatch(r"[0-9a-f]{64}", operation_id):
        raise _CtrlError("operation_id must be 64 lowercase hex characters")
    receipt = args.get("receipt")
    signature = args.get("signature")
    if not isinstance(receipt, dict) or not isinstance(signature, str):
        raise _CtrlError("Central receipt is malformed")
    operation = store.get_link_operation(tunnel.org, operation_id)
    if operation is None:
        raise _CtrlError("unknown Link operation")
    try:
        receipt_json = canonical_json(receipt).decode("utf-8")
        verify_signature(
            witness_key.public_hex,
            signature,
            link_operation_receipt_input(receipt),
        )
    except Exception as exc:
        raise _CtrlError("Central receipt signature is invalid") from exc
    if not hmac.compare_digest(receipt_json, operation.receipt_json) or not hmac.compare_digest(
        signature, operation.receipt_signature
    ):
        raise _CtrlError("Central receipt does not match accepted operation")
    proof_wire = args.get("origin_proof")
    if not isinstance(proof_wire, str) or len(proof_wire) != 43 or "=" in proof_wire:
        raise _CtrlError("origin proof is malformed")
    try:
        proof = base64.urlsafe_b64decode(proof_wire + "=")
    except Exception as exc:
        raise _CtrlError("origin proof is malformed") from exc
    commitment = hashlib.sha256(
        b"autonomy.link.origin-proof-commitment.v1\n" + proof
    ).hexdigest()
    if (
        len(proof) != 32
        or base64.urlsafe_b64encode(proof).decode("ascii").rstrip("=") != proof_wire
        or not hmac.compare_digest(
            commitment, operation.origin_proof_commitment
        )
    ):
        raise _CtrlError("origin proof does not match accepted operation")
    return operation


def _ctrl_create_link(tunnel: "Tunnel", args: dict, store: RegistryStore,
                      base_url: str, now: int, witness_key=None) -> dict:
    if not isinstance(args, dict):
        raise _CtrlError("args must be a JSON object")
    target_uuid = args.get("target_uuid")
    if not isinstance(target_uuid, str) or not _UUID_RE.match(target_uuid):
        raise _CtrlError("target_uuid must be a UUID")
    target_type = args.get("target_type")
    if not isinstance(target_type, str) or not target_type:
        raise _CtrlError("target_type must be a non-empty string")
    if target_type == "org:join":
        raise _CtrlError("org:join is not carried on the control channel")
    meta = args.get("meta", {})
    if not isinstance(meta, dict):
        raise _CtrlError("meta must be a JSON object")
    if not set(meta).issubset(_CTRL_LINK_META_FIELDS):
        raise _CtrlError("meta carries unsupported fields")
    if meta.get("require_auth"):
        raise _CtrlError("require_auth grants need viewer authn (Track E + ledger)")
    link_ttl = meta.get("ttl")
    if link_ttl is not None and (type(link_ttl) is not int or link_ttl <= 0):
        raise _CtrlError("meta.ttl must be a positive integer of seconds")

    central = _central_operation(tunnel, args, store, witness_key)
    if central is not None:
        allowed = {"target_uuid", "target_type", "meta"} | _CENTRAL_LINK_FIELDS
        if set(args) - allowed:
            raise _CtrlError("Central create-link carries unknown or local-only fields")
        if central.operation != "publish":
            raise _CtrlError("Central receipt is not a publish operation")
        if set(meta) - {"ttl", "label"}:
            raise _CtrlError("Central Link meta permits only ttl and label")
        if link_ttl is not None and link_ttl > 365 * 24 * 60 * 60:
            raise _CtrlError("Central Link ttl must not exceed 365 days")
        label = meta.get("label")
        if label is not None:
            try:
                valid_label = isinstance(label, str) and len(label.encode("utf-8")) <= 256
            except UnicodeError:
                valid_label = False
            if not valid_label:
                raise _CtrlError("Central Link label must be at most 256 UTF-8 bytes")
        registry_input = {
            "operation_id": args["operation_id"],
            "target_uuid": target_uuid,
            "target_type": target_type,
            "meta": meta,
        }
        if not hmac.compare_digest(
            hashlib.sha256(canonical_json(registry_input)).hexdigest(),
            central.registry_input_digest,
        ):
            raise _CtrlError("create-link input differs from Central receipt")
        token = generate_token()
        grant = LinkGrant(
            token=token,
            org_uuid=tunnel.org,
            target_uuid=target_uuid,
            target_type=target_type,
            meta=meta,
            created_at=now,
            expires_at=now + link_ttl if link_ttl is not None else None,
            revoked_at=None,
            signer_pub=central.signer_pub,
            subject_kind=central.subject_kind,
            subject_id=central.subject_id,
            operation_id=args["operation_id"],
        )
        status, completed = store.execute_publish_operation(
            tunnel.org, args["operation_id"], grant, now=now
        )
        if status in ("binding_mismatch", "conflict", "inconsistent") or completed is None:
            raise _CtrlError("Central Link operation cannot execute")
        return {
            "token": completed.result_token,
            "url": f"{base_url}/l/{completed.result_token}",
        }

    token = generate_token()
    store.create_link(
        LinkGrant(
            token=token,
            org_uuid=tunnel.org,
            target_uuid=target_uuid,
            target_type=target_type,
            meta=meta,
            created_at=now,
            expires_at=now + link_ttl if link_ttl is not None else None,
            revoked_at=None,
            # D19: no persona ever crosses the tunnel — the grant is an act
            # of the org, attributed to the tunnel and nothing finer.
            signer_pub=None,
            subject_kind="org-tunnel",
            subject_id=None,
        )
    )
    return {"token": token, "url": f"{base_url}/l/{token}"}


def _ctrl_revoke_link(tunnel: "Tunnel", args: dict, store: RegistryStore,
                      now: int, witness_key=None) -> dict:
    if not isinstance(args, dict):
        raise _CtrlError("args must be a JSON object")
    token = args.get("token")
    if not isinstance(token, str) or not token:
        raise _CtrlError("token must be a non-empty string")
    central = _central_operation(tunnel, args, store, witness_key)
    if central is not None:
        allowed = {"token"} | _CENTRAL_LINK_FIELDS
        if set(args) - allowed:
            raise _CtrlError("Central revoke-link carries unknown or local-only fields")
        if central.operation != "revoke":
            raise _CtrlError("Central receipt is not a revoke operation")
        if _re.fullmatch(r"[0-9a-f]{32}", token) is None:
            raise _CtrlError("Central revoke token must be 32 lowercase hex characters")
        registry_input = {"operation_id": args["operation_id"]}
        if not hmac.compare_digest(
            hashlib.sha256(canonical_json(registry_input)).hexdigest(),
            central.registry_input_digest,
        ):
            raise _CtrlError("revoke-link input differs from Central receipt")
        operand_digest = hashlib.sha256(
            canonical_json(["autonomy.link.operand", 1, "revoke", token])
        ).hexdigest()
        if central.operand_digest is None or not hmac.compare_digest(
            operand_digest, central.operand_digest
        ):
            raise _CtrlError("revoke operand differs from Central receipt")
        status, completed = store.execute_revoke_operation(
            tunnel.org, args["operation_id"], token, now=now
        )
        if status in ("binding_mismatch", "conflict") or completed is None:
            raise _CtrlError("Central Link operation cannot execute")
        return {
            "state": completed.state,
            "revoked_at": completed.completed_at if completed.state == "succeeded" else None,
        }
    link = store.get_link(token)
    if link is None:
        raise _CtrlError("unknown link")
    # The one org-ownership check the registry retains: a tunnel may only
    # revoke its own org's grants — no cross-org revoke, no enumeration.
    if link.org_uuid != tunnel.org:
        raise _CtrlError("link belongs to another org")
    revoked_at = store.revoke_link(token, now=now)
    return {"token": token, "revoked_at": revoked_at}


def _ctrl_issue_turn(tunnel: "Tunnel", args: dict, turn_issuer) -> dict:
    """Issue one opaque coupon to an already-authenticated org tunnel."""
    if args != {}:
        raise _CtrlError("issue-turn takes no arguments")
    if turn_issuer is None:
        raise _CtrlError("TURN credential issuance is unavailable")
    configuration = turn_issuer.issue(tunnel.org)
    return {
        "ice_servers": list(configuration.ice_servers),
        "expires_at": configuration.expires_at,
    }


_HOST_OP_ARGS = {
    "host-register": frozenset({"reservation", "host"}),
    "host-renew": frozenset({"reservation", "generation"}),
    "host-renew-all": frozenset(),
    "host-release": frozenset({"reservation"}),
}

# -- auto-bhs3c: serve.dns01.* ops -----------------------------------------

_DNS01_ORDER_RE = _re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
_DNS01_ARGS = {
    "serve.dns01.present": frozenset(
        {"order", "value", "ttl", "expiry", "ts", "cert", "sig"}),
    "serve.dns01.cleanup": frozenset(
        {"order", "value", "ts", "cert", "sig"}),
}
DNS01_TTL_FLOOR, DNS01_TTL_CEILING = 30, 300
#: expiry is an ABSOLUTE signed deadline: 60..900 s after the signed ts.
#: A replayed request can only re-assert its own deadline — never extend
#: it — so the 15-minute bound holds against the full skew window.
DNS01_DEADLINE_MIN, DNS01_DEADLINE_MAX = 60, 900


#: Security-audit sink (auto-dn6bo). The production service runs uvicorn at
#: log_level="warning", which leaves the root logger with no INFO-passing
#: handler — DNS-01 audit events logged through the module logger were
#: silently dropped in production (pitfall cac2fc7a). Audits must not
#: depend on the service's noise threshold, so this logger owns its own
#: stderr handler (journald picks it up) and never propagates: it emits at
#: default configuration no matter what the root is set to.
_AUDIT_LOGGER = logging.getLogger("autonomy.registry.audit")
_AUDIT_LOGGER.setLevel(logging.INFO)
_AUDIT_LOGGER.propagate = False
if not _AUDIT_LOGGER.handlers:
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(
        logging.Formatter("%(asctime)s audit %(message)s")
    )
    _AUDIT_LOGGER.addHandler(_audit_handler)


#: Structured operational-events sink (auto-7df7o). Same reasoning as the
#: audit sink: operational lifecycle/control events must be visible in
#: production regardless of the service's WARNING threshold, so this owns its
#: own handler and never propagates. It carries ONLY non-secret fields — org,
#: public persona/machine ids, op names, results, counts — never a link
#: token, source address, payload, or credential.
_OPS_LOGGER = logging.getLogger("autonomy.registry.ops")
_OPS_LOGGER.setLevel(logging.INFO)
_OPS_LOGGER.propagate = False
if not _OPS_LOGGER.handlers:
    _ops_handler = logging.StreamHandler()
    _ops_handler.setFormatter(logging.Formatter("%(asctime)s ops %(message)s"))
    _OPS_LOGGER.addHandler(_ops_handler)


def _ops(event: str, **fields) -> None:
    """One structured operational line: ``event k=v k=v``. Callers pass only
    non-secret fields (the sink carries no token/address/payload)."""
    rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    _OPS_LOGGER.info("%s %s", event, rendered)


def _dns01_audit(op: str, persona: str, args: dict, result: str) -> None:
    """One audit line per op: hashed order/value, never raw values, never
    key material. The uniform wire error keeps detail server-side."""
    def _h(field):
        raw = args.get(field)
        return hashlib.sha256(
            raw.encode() if isinstance(raw, str) else b"?").hexdigest()[:16]
    _AUDIT_LOGGER.info(
        "dns01 op=%s persona=%s order=%s value=%s ttl=%s expiry=%s "
        "result=%s",
        op, (persona or "")[:8], _h("order"), _h("value"),
        args.get("ttl"), args.get("expiry"), result,
    )


def _dns01_verify(tunnel: "Tunnel", op: str, args: dict,
                  store: RegistryStore, now: int) -> None:
    """Everything short of the store write; raises on ANY defect. The
    caller collapses every failure to the uniform refusal."""
    if CAP_DNS01 not in tunnel.caps:
        raise _CtrlError("capability not negotiated")
    if not isinstance(args, dict) or set(args) != _DNS01_ARGS[op]:
        raise _CtrlError("bad arg set")
    if not isinstance(args["order"], str) or \
            _DNS01_ORDER_RE.match(args["order"]) is None:
        raise _CtrlError("bad order")
    from tools.network.registry.dns_challenges import validate_value

    validate_value(args["value"])
    if type(args["ts"]) is not int or abs(now - args["ts"]) > MAX_CLOCK_SKEW:
        raise _CtrlError("ts outside skew")
    for field in ("ttl", "expiry"):
        if field in args and type(args[field]) is not int:
            raise _CtrlError("bad numeric field")
    if op == "serve.dns01.present":
        deadline_in = args["expiry"] - args["ts"]
        if not DNS01_DEADLINE_MIN <= deadline_in <= DNS01_DEADLINE_MAX:
            raise _CtrlError("expiry deadline outside 60..900s of ts")
    core_fields = {"op": op, "order": args["order"], "value": args["value"],
                   "ts": args["ts"]}
    if op == "serve.dns01.present":
        core_fields["ttl"] = args["ttl"]
        core_fields["expiry"] = args["expiry"]
    binding = store.get_org(tunnel.org)
    if binding is None or binding.expires_at < now:
        raise _CtrlError("no live binding")
    cert = DelegationCert.from_json(args["cert"])
    store.purge_expired_revocations(now=now)
    verified = verify_chain(
        cert, binding.root_pub, org=tunnel.org, now=now,
        revocations=store.revocation_set(tunnel.org),
        required_scope="serve:dns-01",
    )
    if tuple(verified.scope) != ("serve:dns-01",):
        raise _CtrlError("scope must be exactly serve:dns-01")
    if verified.depth != 1:
        raise _CtrlError("dns01 cert must be root-issued")
    if verified.subject_kind != "persona" \
            or verified.subject_id != tunnel.persona_pub:
        raise _CtrlError("dns01 cert subject must be the tunnel persona")
    verify_signature(
        cert.child_pub, args["sig"],
        DNS01_DOMAIN + canonical_json(core_fields),
    )


def _ctrl_dns01(tunnel: "Tunnel", op: str, args: dict,
                store: RegistryStore, now: int, metrics=None) -> dict:
    """serve.dns01.present / .cleanup — the record name is DERIVED from
    the tunnel persona's serving-label binding, never body-supplied.
    Every negative collapses to the uniform {"error": "refused"}."""
    try:
        _dns01_verify(tunnel, op, args, store, now)
        label = store.get_persona_label(tunnel.persona_pub)
        if label is None:
            raise _CtrlError("no serving-label binding")
        name = f"_acme-challenge.{label}.{SERVE_BASE_DOMAIN}"
        if op == "serve.dns01.present":
            ttl = max(DNS01_TTL_FLOOR,
                      min(DNS01_TTL_CEILING, args["ttl"]))
            # The SIGNED absolute deadline, verbatim: a replay re-asserts
            # it; only a freshly signed request can move it.
            expires_at = args["expiry"]
            store.upsert_serve_challenge(
                name + ".", args["value"], expires_at=expires_at,
                now=now, ttl=ttl, order_ref=args["order"],
            )
            result = {"name": name, "expires_at": expires_at}
        else:
            store.delete_serve_challenge(
                name + ".", args["value"], order_ref=args["order"])
            result = {}
    except Exception as exc:
        _dns01_audit(op, tunnel.persona_pub or "", args
                     if isinstance(args, dict) else {},
                     f"refused:{type(exc).__name__}")
        if metrics is not None:
            metrics.dns01_op("refused")
        raise _CtrlError("refused") from exc
    _dns01_audit(op, tunnel.persona_pub or "", args, "ok")
    if metrics is not None:
        metrics.dns01_op("ok")
    return result


def _ctrl_host_op(tunnel: "Tunnel", op: str, args: dict,
                  host_routes: "HostRoutes | None") -> dict:
    """Dispatch one hostname-lease op. Identity is always the tunnel's —
    any identity-shaped field in the body is an exact-arg-set violation
    and fails closed before dispatch."""
    if host_routes is None:
        raise _CtrlError("not-authorized")
    if CAP_HOST_LEASE not in tunnel.caps:
        raise _CtrlError("not-authorized")
    if not isinstance(args, dict) or set(args) != _HOST_OP_ARGS[op]:
        raise _CtrlError("bad-request")
    if op == "host-register":
        return host_routes.register(
            tunnel, args["reservation"], args["host"]
        )
    if op == "host-renew":
        return host_routes.renew(
            tunnel, args["reservation"], args["generation"]
        )
    if op == "host-renew-all":
        return host_routes.renew_all(tunnel)
    return host_routes.release(tunnel, args["reservation"])


async def _handle_ctrl_frame(tunnel: "Tunnel", payload: bytes,
                             store: RegistryStore, base_url: str,
                             now: int, turn_issuer=None, witness_key=None,
                             host_routes: "HostRoutes | None" = None) -> None:
    """Parse one control request and reply on the control channel. A
    malformed payload raises FrameError (drops the tunnel); a clean op
    failure replies {ok: false} and leaves the tunnel up."""
    try:
        msg = _json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError(f"control payload is not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise FrameError("control payload must be a JSON object")
    correlation = msg.get("id")
    if not isinstance(correlation, str) or not _CORRELATION_RE.match(correlation):
        raise FrameError("control id must be a 32-hex correlation id")
    op = msg.get("op")
    args = msg.get("args", {})
    try:
        if op == "create-link":
            result = _ctrl_create_link(tunnel, args, store, base_url, now, witness_key)
        elif op == "revoke-link":
            result = _ctrl_revoke_link(tunnel, args, store, now, witness_key)
        elif op == "issue-turn":
            result = _ctrl_issue_turn(tunnel, args, turn_issuer)
        elif op in _HOST_OP_ARGS:
            result = _ctrl_host_op(tunnel, op, args, host_routes)
        elif op in _DNS01_ARGS:
            result = _ctrl_dns01(
                tunnel, op, args, store, now,
                metrics=getattr(host_routes, "_metrics", None),
            )
        else:
            raise _CtrlError(f"unknown control op: {op!r}")
        reply = {"id": correlation, "ok": True, **result}
    except _CtrlError as exc:
        reply = {"id": correlation, "ok": False, "error": str(exc)}
    except Exception:
        # An unexpected fault on ONE control op must not tear down the
        # tunnel (and every live viewer on it). Reply a generic error —
        # no internals leak — and keep serving.
        reply = {"id": correlation, "ok": False,
                 "error": "control op failed unexpectedly"}
    # Structured control-result line + last-outcome for the operator readout
    # (auto-7df7o). The op name and its result/reason are operational, not
    # secret; the correlation id is the connector's own request id, never a
    # link token. An unknown op is typed here as its literal op string.
    outcome = "ok" if reply.get("ok") else "error"
    reason = None if reply.get("ok") else reply.get("error")
    _ops("control", org=tunnel.org[:8],
         op=(op if isinstance(op, str) else "?"),
         id=correlation, result=outcome, reason=reason)
    tunnel.last_control = {"op": op if isinstance(op, str) else "?",
                           "result": outcome, "reason": reason}
    await tunnel.send_frame(FRAME_CTRL, CTRL_CHANNEL_ID, canonical_json(reply))


async def tunnel_endpoint(websocket: WebSocket, org: str, hub: TunnelHub,
                          store: RegistryStore, now_fn,
                          base_url: str = "", turn_issuer=None, witness_key=None,
                          host_routes: "HostRoutes | None" = None) -> None:
    """Handle one dashboard tunnel connection for its whole lifetime."""
    await websocket.accept()
    try:
        raw_hello = await websocket.receive_text()
    except (WebSocketDisconnect, KeyError, RuntimeError):
        return
    try:
        verified = _verify_tunnel_hello(
            raw_hello, org, store, int(now_fn())
        )
    except _ProtocolVersionMismatch as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({
                "ok": False,
                "error": {
                    "code": "protocol_version_mismatch",
                    "connector_version": exc.connector_version,
                    "registry_version": HELLO_VERSION_2,
                },
            })
        await _close_quietly(websocket, CLOSE_PROTOCOL_MISMATCH)
        return
    except HelloError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({"ok": False, "error": str(exc)})
        await _close_quietly(websocket, CLOSE_UNAUTHENTICATED)
        return

    accepted_caps = tuple(
        cap for cap in verified.caps if cap in REGISTRY_CAPS
    )
    tunnel = Tunnel(
        websocket,
        org,
        persona_pub=verified.persona_pub,
        signer_pub=verified.signer_pub,
        machine=verified.machine,
        caps=accepted_caps,
        version=verified.version,
    )
    replaced = hub.register(tunnel)
    if replaced is not None:
        # Same (persona, machine) reconnect: the replaced connection's
        # leases fail closed immediately — never silently migrated.
        if host_routes is not None:
            host_routes.drop_connection(replaced)
        await _close_quietly(replaced.ws, CLOSE_REPLACED)
    if verified.version == HELLO_VERSION_2:
        await websocket.send_json({
            "ok": True, "v": HELLO_VERSION_2, "caps": list(accepted_caps),
        })
    else:
        await websocket.send_json({"ok": True, "v": HELLO_VERSION})

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if raw is None:
                continue  # unexpected text frame mid-mux: ignore
            try:
                frame = decode_frame(raw)
            except FrameError:
                break  # protocol violation: drop the tunnel
            if frame.type == FRAME_CTRL:
                # Control frames are org-level acts of the (already
                # hello-authenticated) tunnel, not viewer traffic. A
                # malformed payload is a protocol violation → drop.
                try:
                    await _handle_ctrl_frame(
                        tunnel, frame.payload, store, base_url, int(now_fn()),
                        turn_issuer=turn_issuer, witness_key=witness_key,
                        host_routes=host_routes,
                    )
                except FrameError:
                    break
                continue
            raw_stream = tunnel.raw_streams.get(frame.channel_id)
            if raw_stream is not None:
                # Raw-stream frames (auto-9z1xh): enqueue only — a stream's
                # own pumps do the blocking work, never this receive loop.
                if frame.type == FRAME_STREAM_CTRL:
                    raw_stream.on_ctrl_raw(frame.payload)
                elif frame.type == FRAME_DATA:
                    raw_stream.on_data(frame.payload)
                elif frame.type == FRAME_CLOSE:
                    raw_stream.on_close()
                continue
            if frame.type == FRAME_STREAM_CTRL:
                continue  # stale frame for a torn-down stream
            if frame.channel_id not in tunnel.channels:
                if frame.type == FRAME_DATA:
                    # Not a live viewer channel -- try the same 16 bytes
                    # as a stream token instead (auto-albp6.7): a relay
                    # link token is a 128-bit CSPRNG value (idkit I2),
                    # raw-byte-identical in length to a channel id, so a
                    # publish reuses FRAME_DATA's existing shape rather
                    # than adding a second frame type. A token naming no
                    # stream is discarded, matching the existing
                    # stale-frame behavior below.
                    tunnel.publish_stream(
                        frame.channel_id.hex(), frame.payload, now_fn()
                    )
                continue  # viewer already gone; stale frame
            if frame.type == FRAME_DATA:
                tunnel.enqueue_viewer(frame.channel_id, frame.payload)
            elif frame.type == FRAME_CLOSE:
                tunnel.close_viewer(frame.channel_id, 1000)
    finally:
        # Lease removal precedes viewer teardown — the same admission-first
        # ordering close_revoked uses, so a routed open can never race onto
        # a dying connection (route disappearance is synchronous here).
        if host_routes is not None:
            host_routes.drop_connection(tunnel)
        hub.unregister(tunnel)
        await tunnel.close_all_viewers(1001)


def _resolve_live_link(store: RegistryStore, token: str, now: int):
    """Same liveness rules as the envelope endpoint (§4.6)."""
    link = store.get_link(token)
    if (
        link is None
        or link.revoked_at is not None
        or link.is_expired_at(now)
    ):
        return None
    binding = store.get_org(link.org_uuid)
    if binding is None or binding.expires_at < now:
        return None
    return link


async def viewer_endpoint(
    websocket: WebSocket,
    token: str,
    hub: TunnelHub,
    store: RegistryStore,
    now_fn,
    *,
    abuse_limiter: RelayAbuseLimiter | None = None,
) -> None:
    """Handle one viewer (bootloader) connection for its whole lifetime."""
    await websocket.accept()
    admission = None
    if abuse_limiter is not None:
        peer = getattr(websocket, "client", None)
        admission = abuse_limiter.begin(
            peer.host if peer is not None else "unknown"
        )
        if admission is None:
            logger.warning(
                "relay dial refused (4404): admission limiter denied source, "
                "token=%s peer=%s", token, peer.host if peer is not None else "unknown",
            )
            await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
            return
    link = _resolve_live_link(store, token, int(now_fn()))
    resolved = None
    if link is not None and abuse_limiter is not None:
        resolved = abuse_limiter.resolve(admission, token, link.org_uuid)
        if resolved is None:
            logger.warning(
                "relay dial refused (4404): admission limiter denied link/org, "
                "token=%s org=%s", token, link.org_uuid,
            )
            await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
            return
    tunnel = hub.get(link.org_uuid) if link is not None else None
    if link is None or tunnel is None:
        # The WebSocket uses one close code; the bootloader has already
        # resolved the envelope, so it can distinguish an invalid token from
        # a valid link whose sharing dashboard is disconnected.
        if link is None:
            logger.warning(
                "relay dial refused (4404): link not live (unknown/expired/"
                "revoked/dead org binding), token=%s", token,
            )
        else:
            logger.warning(
                "relay dial refused (4404): no tunnel parked for org, "
                "token=%s org=%s", token, link.org_uuid,
            )
        await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
        return

    # Bound concurrent viewer channels per org tunnel: one bearer-link holder
    # cannot open unbounded attachment-streaming channels to exhaust relay
    # memory on the shared tunnel. Accounting is per this tunnel, not global.
    if len(tunnel.channels) >= MAX_VIEWER_CHANNELS_PER_TUNNEL:
        logger.warning(
            "relay dial refused (4404): tunnel at max viewer channels (%d), "
            "token=%s org=%s", MAX_VIEWER_CHANNELS_PER_TUNNEL, token, link.org_uuid,
        )
        await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
        return

    abuse_lease = None
    if abuse_limiter is not None:
        abuse_lease = abuse_limiter.acquire(resolved)
        if abuse_lease is None:
            logger.warning(
                "relay dial refused (4404): active-connection lease denied, "
                "token=%s org=%s", token, link.org_uuid,
            )
            await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
            return
    channel_id = new_channel_id()
    try:
        relay_channel = tunnel.add_viewer(
            channel_id, websocket, abuse_lease=abuse_lease,
            exempt_bytes=link.target_type == "fleet:join",
        )
    except Exception:
        if abuse_lease is not None:
            abuse_lease.release()
        raise
    # Every channel is a candidate stream listener (auto-albp6.7) -- the
    # relay cannot see target_type (it never parses grants, I5), so it
    # cannot know here whether this token names a session/mission. That
    # is fine: an un-published-to stream costs one empty buffer and one
    # idle listener entry, and only auto-albp6.8's connector-side
    # publisher ever decides which tokens actually receive frames.
    try:
        relay_channel.stream_token = token
        tunnel.attach_listener(token, channel_id, relay_channel)
        await tunnel.send_frame(FRAME_OPEN, channel_id,
                                canonical_json({"token": token}))
    except Exception:
        tunnel.detach_viewer(channel_id, relay_channel)
        tunnel.detach_listener(token, channel_id)
        logger.warning(
            "relay dial refused (4404): failed to open channel on tunnel "
            "(send/attach error), token=%s org=%s", token, link.org_uuid,
            exc_info=True,
        )
        await relay_channel.close(CLOSE_UNKNOWN_LINK)
        return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if raw is None:
                continue
            if tunnel.channels.get(channel_id) is not relay_channel:
                break  # channel torn down from the dashboard side
            if abuse_lease is not None and not abuse_lease.charge_bytes(len(raw)):
                break
            try:
                await tunnel.send_frame(FRAME_DATA, channel_id, raw)
            except Exception:
                break  # tunnel died mid-channel
    finally:
        if tunnel.detach_viewer(channel_id, relay_channel):
            tunnel.detach_listener(token, channel_id)
            await relay_channel.close(1001)
            with contextlib.suppress(Exception):
                await tunnel.send_frame(FRAME_CLOSE, channel_id)
        else:
            await relay_channel.wait_closed()


async def host_probe_endpoint(
    websocket: WebSocket,
    host: str,
    host_routes: HostRoutes,
    now_fn,
    *,
    abuse_limiter: RelayAbuseLimiter | None = None,
) -> None:
    """One-shot hostname routing diagnostic (auto-0zdky).

    Resolves *host* through the live lease table exactly the way the
    tls-stream OPEN (auto-9z1xh) will, opens a probe channel on the leased
    tunnel, relays the connector's single echo record to the prober, and
    closes. Every miss — unknown host, no lease, expired lease, offline
    tunnel — is the same uniform 4404; nothing distinguishes "reserved but
    idle" from "never reserved".
    """
    await websocket.accept()
    admission = None
    if abuse_limiter is not None:
        peer = getattr(websocket, "client", None)
        admission = abuse_limiter.begin(
            peer.host if peer is not None else "unknown"
        )
        if admission is None:
            await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
            return
    tunnel = host_routes.route(host)
    if tunnel is None:
        await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
        return
    resolved = None
    if abuse_limiter is not None:
        resolved = abuse_limiter.resolve(admission, host, tunnel.org)
        if resolved is None:
            await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
            return
    if len(tunnel.channels) >= MAX_VIEWER_CHANNELS_PER_TUNNEL:
        await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
        return
    abuse_lease = None
    if abuse_limiter is not None:
        abuse_lease = abuse_limiter.acquire(resolved)
        if abuse_lease is None:
            await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
            return
    channel_id = new_channel_id()
    try:
        relay_channel = tunnel.add_viewer(
            channel_id, websocket, abuse_lease=abuse_lease
        )
    except Exception:
        if abuse_lease is not None:
            abuse_lease.release()
        raise
    reservation = host_routes.reservation_for(host)
    try:
        await tunnel.send_frame(
            FRAME_OPEN,
            channel_id,
            canonical_json({
                "kind": "host-probe", "host": host,
                "reservation": reservation,
            }),
        )
    except Exception:
        tunnel.detach_viewer(channel_id, relay_channel)
        await relay_channel.close(CLOSE_UNKNOWN_LINK)
        return
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if tunnel.channels.get(channel_id) is not relay_channel:
                break  # echoed and closed from the connector side
    finally:
        if tunnel.detach_viewer(channel_id, relay_channel):
            await relay_channel.close(1001)
            with contextlib.suppress(Exception):
                await tunnel.send_frame(FRAME_CLOSE, channel_id)
        else:
            await relay_channel.wait_closed()
