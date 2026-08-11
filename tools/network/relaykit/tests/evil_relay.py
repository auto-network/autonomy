"""An actively malicious relay for the I5 MITM tests.

Speaks the real tunnel + viewer protocols (it reuses ``frames.py``), but
attacks the handshake passing through it:

- ``mode="server_eph"``: rewrites the SERVER_HELLO's ephemeral X25519
  key to its own before forwarding to the viewer — the classic MITM
  key-substitution. It cannot re-sign: the tunnel:serve signature covers
  both ephemeral keys, and the relay does not hold any org-delegated key.
- ``mode="client_eph"``: rewrites the CLIENT_HELLO's ephemeral key on
  the way to the dashboard — the dashboard then signs the attacker's
  key, and the viewer detects that the signed client_eph is not its own.

Either way the viewer's ``verify_server_hello`` must fail (I5: the org
key pins the served end; the relay cannot MITM).

It also skips hello verification entirely (an evil relay accepts anyone)
— which doubles as a demonstration that channel security does not depend
on the relay behaving.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Dict, Optional

import websockets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.idkit import canonical_json
from tools.network.relaykit.frames import (
    FrameError,
    split_viewer_message,
    tag_viewer_message,
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    decode_frame,
    encode_frame,
    new_channel_id,
)


class EvilRelay:
    def __init__(self, mode: str = "server_eph"):
        assert mode in ("server_eph", "client_eph", "passthrough")
        self.mode = mode
        self._tunnel = None
        self._tunnel_lock = asyncio.Lock()
        self._channels: Dict[bytes, object] = {}
        self._attacked: set = set()
        self._server = None
        self.port: Optional[int] = None

    def _mitm_eph(self) -> str:
        return X25519PrivateKey.generate().public_key().public_bytes_raw().hex()

    def _rewrite_eph(self, payload: bytes, *, tagged: bool = False) -> bytes:
        """Substitute our own ECDH key in a hello.

        Dashboard -> viewer messages carry a one-byte kind
        (``frames.VIEWER_KIND_*``); viewer -> dashboard messages do not. A
        real attacker in this position has to parse the framing to reach
        the hello, so this does too -- and re-emits it unchanged, because
        mangling the framing is not the attack under test.
        """
        kind = None
        if tagged:
            try:
                kind, payload = split_viewer_message(payload)
            except FrameError:
                return payload if kind is None else tag_viewer_message(kind, payload)
        try:
            hello = json.loads(payload)
            hello["eph_pub"] = self._mitm_eph()
            payload = canonical_json(hello)
        except (ValueError, TypeError):
            pass
        return payload if kind is None else tag_viewer_message(kind, payload)

    async def start(self) -> int:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0,
                                              max_size=2**22, compression=None)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws) -> None:
        path = ws.request.path
        if path.startswith("/t/"):
            await self._handle_tunnel(ws)
        elif path.endswith("/channel"):
            token = path.split("/")[-2]
            await self._handle_viewer(ws, token)
        else:
            await ws.close()

    async def _handle_tunnel(self, ws) -> None:
        await ws.recv()  # the hello — an evil relay doesn't check anything
        await ws.send(json.dumps({"ok": True}))
        self._tunnel = ws
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                frame = decode_frame(raw)
                viewer = self._channels.get(frame.channel_id)
                if viewer is None:
                    continue
                if frame.type == FRAME_DATA:
                    payload = frame.payload
                    # First dashboard->viewer message on a channel is the
                    # SERVER_HELLO: substitute our own ECDH key.
                    if self.mode == "server_eph" and frame.channel_id not in self._attacked:
                        self._attacked.add(frame.channel_id)
                        payload = self._rewrite_eph(payload, tagged=True)
                    with contextlib.suppress(Exception):
                        await viewer.send(payload)
                elif frame.type == FRAME_CLOSE:
                    self._channels.pop(frame.channel_id, None)
                    with contextlib.suppress(Exception):
                        await viewer.close()
        finally:
            if self._tunnel is ws:
                self._tunnel = None

    async def _handle_viewer(self, ws, token: str) -> None:
        tunnel = self._tunnel
        if tunnel is None:
            await ws.close(code=4404)
            return
        channel_id = new_channel_id()
        self._channels[channel_id] = ws
        first_from_viewer = True
        async with self._tunnel_lock:
            await tunnel.send(encode_frame(FRAME_OPEN, channel_id,
                                           canonical_json({"token": token})))
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                payload = raw
                # First viewer->dashboard message is the CLIENT_HELLO:
                # substitute our own ECDH key on the way in.
                if self.mode == "client_eph" and first_from_viewer:
                    payload = self._rewrite_eph(payload)
                first_from_viewer = False
                async with self._tunnel_lock:
                    await tunnel.send(encode_frame(FRAME_DATA, channel_id, payload))
        finally:
            self._channels.pop(channel_id, None)
            if self._tunnel is not None:
                with contextlib.suppress(Exception):
                    async with self._tunnel_lock:
                        await self._tunnel.send(encode_frame(FRAME_CLOSE, channel_id))
