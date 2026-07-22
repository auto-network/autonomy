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
from typing import Dict, Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

from tools.network.idkit import (
    ChainVerifyError,
    DelegationCert,
    MalformedError,
    canonical_json,
    verify_chain,
    verify_signature,
)
from tools.network.relaykit.frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    FrameError,
    decode_frame,
    encode_frame,
    new_channel_id,
)
from tools.network.relaykit.hello import HelloError, hello_signing_input, parse_tunnel_hello

from .signing import MAX_CLOCK_SKEW
from .store import RegistryStore

# WS close codes (4000-4999 = application-defined).
CLOSE_UNAUTHENTICATED = 4403
CLOSE_UNKNOWN_LINK = 4404  # unknown token or no serving tunnel
CLOSE_REPLACED = 4409


class Tunnel:
    """A live dashboard connection plus its open viewer channels."""

    def __init__(self, ws: WebSocket, org: str):
        self.ws = ws
        self.org = org
        self.channels: Dict[bytes, WebSocket] = {}
        self._send_lock = asyncio.Lock()

    async def send_frame(self, frame_type: int, channel_id: bytes, payload: bytes = b"") -> None:
        async with self._send_lock:
            await self.ws.send_bytes(encode_frame(frame_type, channel_id, payload))


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


async def tunnel_endpoint(websocket: WebSocket, org: str, hub: TunnelHub,
                          store: RegistryStore, now_fn) -> None:
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
            viewer = tunnel.channels.get(frame.channel_id)
            if viewer is None:
                continue  # viewer already gone; stale frame
            if frame.type == FRAME_DATA:
                try:
                    await viewer.send_bytes(frame.payload)
                except Exception:
                    tunnel.channels.pop(frame.channel_id, None)
            elif frame.type == FRAME_CLOSE:
                tunnel.channels.pop(frame.channel_id, None)
                await _close_quietly(viewer, 1000)
    finally:
        hub.unregister(tunnel)
        for viewer in list(tunnel.channels.values()):
            await _close_quietly(viewer, 1001)
        tunnel.channels.clear()


def _resolve_live_link(store: RegistryStore, token: str, now: int):
    """Same liveness rules as the envelope endpoint (§4.6)."""
    link = store.get_link(token)
    if (
        link is None
        or link.revoked_at is not None
        or (link.expires_at is not None and link.expires_at < now)
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
    tunnel.channels[channel_id] = websocket
    try:
        await tunnel.send_frame(FRAME_OPEN, channel_id,
                                canonical_json({"token": token}))
    except Exception:
        tunnel.channels.pop(channel_id, None)
        await _close_quietly(websocket, CLOSE_UNKNOWN_LINK)
        return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if raw is None:
                continue
            if tunnel.channels.get(channel_id) is not websocket:
                break  # channel torn down from the dashboard side
            try:
                await tunnel.send_frame(FRAME_DATA, channel_id, raw)
            except Exception:
                break  # tunnel died mid-channel
    finally:
        if tunnel.channels.pop(channel_id, None) is not None:
            with contextlib.suppress(Exception):
                await tunnel.send_frame(FRAME_CLOSE, channel_id)
