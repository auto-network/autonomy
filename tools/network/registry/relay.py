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
from typing import Callable, Coroutine, Dict, Optional

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
)
from tools.network.relaykit.hello import HelloError, hello_signing_input, parse_tunnel_hello

from .signing import MAX_CLOCK_SKEW
from .store import LinkGrant, RegistryStore

# WS close codes (4000-4999 = application-defined).
CLOSE_UNAUTHENTICATED = 4403
CLOSE_UNKNOWN_LINK = 4404  # unknown token or no serving tunnel
CLOSE_REPLACED = 4409
CLOSE_VIEWER_QUEUE_OVERFLOW = 4413

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


class Tunnel:
    """A live dashboard connection plus its open viewer channels."""

    def __init__(self, ws: WebSocket, org: str):
        self.ws = ws
        self.org = org
        self.channels: Dict[bytes, _ViewerRelayChannel] = {}
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

    async def close_all_viewers(self, code: int) -> None:
        channels = list(self.channels.values())
        self.channels.clear()
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


def _verify_tunnel_hello(raw, org: str, store: RegistryStore, now: int) -> None:
    """The tunnel's I4 gate: hello signature + tunnel:serve chain to the
    org's bound root. Raises HelloError on any failure."""
    data = parse_tunnel_hello(raw)
    if data["org"] != org:
        raise HelloError("hello org does not match tunnel path")
    if abs(now - data["ts"]) > MAX_CLOCK_SKEW:
        raise HelloError(f"hello ts outside ±{MAX_CLOCK_SKEW}s freshness window")

    binding = store.get_org(org)
    if binding is None or binding.expires_at < now:
        raise HelloError("no live binding for org")

    try:
        verify_signature(data["signer"], data["sig"],
                         hello_signing_input(org, data["signer"], data["ts"]))
        cert = DelegationCert.from_json(data["cert"])
        if cert.child_pub != data["signer"]:
            raise HelloError("cert does not delegate to the hello signer")
        store.purge_expired_revocations(now=now)
        verify_chain(
            cert,
            binding.root_pub,
            org=org,
            now=now,
            revocations=store.revocation_set(org),
            required_scope="tunnel:serve",
        )
    except (ChainVerifyError, MalformedError) as exc:
        raise HelloError(f"{type(exc).__name__}: {exc}") from exc


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
        _verify_tunnel_hello(raw_hello, org, store, int(now_fn()))
    except HelloError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({"ok": False, "error": str(exc)})
        await _close_quietly(websocket, CLOSE_UNAUTHENTICATED)
        return

    tunnel = Tunnel(websocket, org)
    replaced = hub.register(tunnel)
    if replaced is not None:
        await _close_quietly(replaced.ws, CLOSE_REPLACED)
    await websocket.send_json({"ok": True})

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

    channel_id = new_channel_id()
    relay_channel = tunnel.add_viewer(channel_id, websocket)
    try:
        await tunnel.send_frame(FRAME_OPEN, channel_id,
                                canonical_json({"token": token}))
    except Exception:
        tunnel.detach_viewer(channel_id, relay_channel)
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
            await relay_channel.close(1001)
            with contextlib.suppress(Exception):
                await tunnel.send_frame(FRAME_CLOSE, channel_id)
        else:
            await relay_channel.wait_closed()
