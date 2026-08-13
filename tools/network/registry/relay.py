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
import contextlib
import re
from collections import deque
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
    FrameError,
    decode_frame,
    encode_frame,
    new_channel_id,
    VIEWER_KIND_FEED,
    tag_viewer_message,
)
from tools.network.relaykit.hello import (
    HELLO_VERSION,
    HelloError,
    hello_signing_input,
    parse_tunnel_hello,
)

from .signing import MAX_CLOCK_SKEW
from .store import LinkGrant, RegistryStore

# WS close codes (4000-4999 = application-defined).
CLOSE_UNAUTHENTICATED = 4403
CLOSE_UNKNOWN_LINK = 4404  # unknown token or no serving tunnel
CLOSE_PROTOCOL_MISMATCH = 4406
CLOSE_REPLACED = 4409
CLOSE_VIEWER_QUEUE_OVERFLOW = 4413
CLOSE_TUNNEL_CHANNELS_EXCEEDED = 4429  # per-tunnel concurrent viewer cap hit
# Resumable, not an error: this listener fell behind a stream's retention
# window. The viewer reconnects and requests its own offset through history
# (auto-albp6.7) -- distinct from every other close code above, none of
# which are true for it.
CLOSE_LISTENER_FELL_BEHIND = 4416

# Stream (auto-albp6.7) retention, in precedence order -- the order is
# load-bearing, see Stream._apply_retention.
STREAM_EXPIRY_SECONDS = 60
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
    ):
        self.ws = ws
        self.max_queued_bytes = max_queued_bytes
        #: token of the Stream (auto-albp6.7) this channel is a listener
        #: of, if any -- set by viewer_endpoint after attaching, read by
        #: Tunnel to detach the listener wherever this channel is torn
        #: down. None for every ordinary (non-session) channel.
        self.stream_token: Optional[str] = None
        self._on_writer_failure = on_writer_failure
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
        self._queued_bytes += len(payload)
        self._queue.put_nowait(payload)
        return True

    def start_close(self, code: int) -> asyncio.Task:
        """Cancel the writer, release queued bytes, and close asynchronously."""
        if self._close_task is not None:
            return self._close_task
        self._closing = True
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
    ):
        self.ws = ws
        self.org = org
        # Connection-memory routing facts only. Neither value is written to
        # link_sessions, node_hints, logs, metrics, or a history table.
        self.persona_pub = persona_pub
        self.signer_pub = signer_pub
        self.channels: Dict[bytes, _ViewerRelayChannel] = {}
        self.streams: Dict[str, Stream] = {}
        self._send_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()

    async def send_frame(self, frame_type: int, channel_id: bytes, payload: bytes = b"") -> None:
        async with self._send_lock:
            await self.ws.send_bytes(encode_frame(frame_type, channel_id, payload))

    def add_viewer(self, channel_id: bytes, ws: WebSocket) -> _ViewerRelayChannel:
        channel = _ViewerRelayChannel(
            ws,
            on_writer_failure=lambda failed: self._writer_failed(
                channel_id, failed
            ),
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

    async def close_all_viewers(self, code: int) -> None:
        channels = list(self.channels.values())
        self.channels.clear()
        self.streams.clear()
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
    """org → live tunnel. All state is in-memory: tunnels are ephemeral
    by nature and re-dialed by connectors after any restart."""

    def __init__(self):
        self._tunnels: Dict[str, Tunnel] = {}

    def get(self, org: str) -> Optional[Tunnel]:
        return self._tunnels.get(org)

    def register(self, tunnel: Tunnel) -> Optional[Tunnel]:
        """Install *tunnel*; returns the tunnel it replaced, if any."""
        previous = self._tunnels.get(tunnel.org)
        self._tunnels[tunnel.org] = tunnel
        return previous

    def unregister(self, tunnel: Tunnel) -> None:
        if self._tunnels.get(tunnel.org) is tunnel:
            del self._tunnels[tunnel.org]

    async def close_revoked(self, org: str, signer_pub: str) -> bool:
        """Close the live tunnel authenticated by a newly revoked signer.

        Removing it from admission first prevents a viewer racing the socket
        close from opening a new channel on an already-revoked tunnel.
        """
        tunnel = self._tunnels.get(org)
        if tunnel is None or tunnel.signer_pub != signer_pub:
            return False
        self.unregister(tunnel)
        await _close_quietly(tunnel.ws, CLOSE_UNAUTHENTICATED)
        await tunnel.close_all_viewers(CLOSE_UNAUTHENTICATED)
        return True


class _ProtocolVersionMismatch(HelloError):
    def __init__(self, connector_version: int):
        self.connector_version = connector_version
        super().__init__(
            "tunnel protocol version mismatch: "
            f"connector={connector_version} registry={HELLO_VERSION}"
        )


def _verify_tunnel_hello(
    raw, org: str, store: RegistryStore, now: int
) -> tuple[str, str]:
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

    try:
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
    if data["v"] != HELLO_VERSION:
        raise _ProtocolVersionMismatch(data["v"])
    return verified.subject_id, data["signer"]


async def _close_quietly(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code)


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


class _CtrlError(Exception):
    """A control op that fails cleanly — replied as {ok: false}, tunnel
    stays up. (Distinct from a malformed FRAME payload, which drops it.)"""


def _ctrl_create_link(tunnel: "Tunnel", args: dict, store: RegistryStore,
                      base_url: str, now: int) -> dict:
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
                      now: int) -> dict:
    if not isinstance(args, dict):
        raise _CtrlError("args must be a JSON object")
    token = args.get("token")
    if not isinstance(token, str) or not token:
        raise _CtrlError("token must be a non-empty string")
    link = store.get_link(token)
    if link is None:
        raise _CtrlError("unknown link")
    # The one org-ownership check the registry retains: a tunnel may only
    # revoke its own org's grants — no cross-org revoke, no enumeration.
    if link.org_uuid != tunnel.org:
        raise _CtrlError("link belongs to another org")
    store.revoke_link(token, now=now)
    return {"token": token, "revoked_at": now}


async def _handle_ctrl_frame(tunnel: "Tunnel", payload: bytes,
                             store: RegistryStore, base_url: str,
                             now: int) -> None:
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
            result = _ctrl_create_link(tunnel, args, store, base_url, now)
        elif op == "revoke-link":
            result = _ctrl_revoke_link(tunnel, args, store, now)
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
    await tunnel.send_frame(FRAME_CTRL, CTRL_CHANNEL_ID, canonical_json(reply))


async def tunnel_endpoint(websocket: WebSocket, org: str, hub: TunnelHub,
                          store: RegistryStore, now_fn,
                          base_url: str = "") -> None:
    """Handle one dashboard tunnel connection for its whole lifetime."""
    await websocket.accept()
    try:
        raw_hello = await websocket.receive_text()
    except (WebSocketDisconnect, KeyError, RuntimeError):
        return
    try:
        persona_pub, signer_pub = _verify_tunnel_hello(
            raw_hello, org, store, int(now_fn())
        )
    except _ProtocolVersionMismatch as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({
                "ok": False,
                "error": {
                    "code": "protocol_version_mismatch",
                    "connector_version": exc.connector_version,
                    "registry_version": HELLO_VERSION,
                },
            })
        await _close_quietly(websocket, CLOSE_PROTOCOL_MISMATCH)
        return
    except HelloError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({"ok": False, "error": str(exc)})
        await _close_quietly(websocket, CLOSE_UNAUTHENTICATED)
        return

    tunnel = Tunnel(
        websocket,
        org,
        persona_pub=persona_pub,
        signer_pub=signer_pub,
    )
    replaced = hub.register(tunnel)
    if replaced is not None:
        await _close_quietly(replaced.ws, CLOSE_REPLACED)
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
                        tunnel, frame.payload, store, base_url, int(now_fn()))
                except FrameError:
                    break
                continue
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


async def viewer_endpoint(websocket: WebSocket, token: str, hub: TunnelHub,
                          store: RegistryStore, now_fn) -> None:
    """Handle one viewer (bootloader) connection for its whole lifetime."""
    await websocket.accept()
    link = _resolve_live_link(store, token, int(now_fn()))
    tunnel = hub.get(link.org_uuid) if link is not None else None
    if link is None or tunnel is None:
        # The WebSocket uses one close code; the bootloader has already
        # resolved the envelope, so it can distinguish an invalid token from
        # a valid link whose sharing dashboard is disconnected.
        await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
        return

    # Bound concurrent viewer channels per org tunnel: one bearer-link holder
    # cannot open unbounded attachment-streaming channels to exhaust relay
    # memory on the shared tunnel. Accounting is per this tunnel, not global.
    if len(tunnel.channels) >= MAX_VIEWER_CHANNELS_PER_TUNNEL:
        await _close_quietly(websocket, CLOSE_TUNNEL_CHANNELS_EXCEEDED)
        return

    channel_id = new_channel_id()
    relay_channel = tunnel.add_viewer(channel_id, websocket)
    # Every channel is a candidate stream listener (auto-albp6.7) -- the
    # relay cannot see target_type (it never parses grants, I5), so it
    # cannot know here whether this token names a session/mission. That
    # is fine: an un-published-to stream costs one empty buffer and one
    # idle listener entry, and only auto-albp6.8's connector-side
    # publisher ever decides which tokens actually receive frames.
    relay_channel.stream_token = token
    tunnel.attach_listener(token, channel_id, relay_channel)
    try:
        await tunnel.send_frame(FRAME_OPEN, channel_id,
                                canonical_json({"token": token}))
    except Exception:
        tunnel.detach_viewer(channel_id, relay_channel)
        tunnel.detach_listener(token, channel_id)
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
