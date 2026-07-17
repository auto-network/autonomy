"""Direct path — rung one of the connectivity fallback chain (G1, §8).

A publicly reachable node listens on a plain WebSocket; dialers race
the candidate addresses from the registry's reachability hints and take
the first that completes. Security does not depend on the transport at
all: the same E2E channel handshake runs here as through any relay, so
a direct connection is simply the zero-middleman case.

Wire protocol::

    dialer → node   TEXT  {"v": 1, "session": <32 hex>}
    dialer → node   CLIENT_HELLO          (binary, channel.py)
    node   → dialer SERVER_HELLO          (binary, signed tunnel:serve)
    ...             AES-256-GCM records, token = session ...

The ``session`` id plays the role the link token plays on the central
relay: it binds both ends' handshake transcripts to this one dial.

v1 boundary ("ICE-style", honestly): candidates are dialed as ordinary
outbound TCP/WS connections with a short per-attempt timeout — multiple
interface/address candidates, first-success-wins. Real STUN discovery
and UDP simultaneous-open hole-punching are a later additive transport
(iroh/WebRTC territory, spec §8 implementation candidates); the chain's
*shape* — try direct, degrade to peer relay, floor at auto.network —
is what this module pins.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from typing import Optional

import websockets

from tools.network.idkit import DelegationCert, KeyPair

from .channel import (
    ChannelCrypto,
    HandshakeError,
    build_client_hello,
    verify_server_hello,
)
from .connector import echo_handler, serve_channel
from .peer import SESSION_HEX_LEN
from .viewer import ViewerChannel

DIRECT_VERSION = 1


def new_session_id() -> str:
    """A fresh dial session id (the direct/peer analogue of a token)."""
    return secrets.token_hex(SESSION_HEX_LEN // 2)


def _valid_session(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SESSION_HEX_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


class DirectChannelServer:
    """A node's direct-dial listener: one WebSocket per channel.

    Serves the same ``handler(token, message) -> response`` seam as the
    tunnel connector — the application cannot tell which rung of the
    chain a request arrived over.
    """

    def __init__(self, org: str, key: KeyPair, cert: DelegationCert,
                 handler=echo_handler, *, host: str = "127.0.0.1", port: int = 0):
        self._org = org
        self._key = key
        self._cert = cert
        self._handler = handler
        self._host = host
        self._port = port
        self._server = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> int:
        self._server = await websockets.serve(
            self._handle, self._host, self._port, max_size=2**22, compression=None
        )
        self._port = self._server.sockets[0].getsockname()[1]
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws) -> None:
        try:
            preamble = await ws.recv()
            if isinstance(preamble, (bytes, bytearray)):
                return
            data = json.loads(preamble)
            if (
                not isinstance(data, dict)
                or set(data) != {"v", "session"}
                or data["v"] != DIRECT_VERSION
                or not _valid_session(data["session"])
            ):
                return
        except (websockets.exceptions.ConnectionClosed, ValueError):
            return

        async def recv() -> Optional[bytes]:
            while True:
                try:
                    message = await ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    return None
                if isinstance(message, (bytes, bytearray)):
                    return bytes(message)

        try:
            await serve_channel(
                self._key, self._cert, org=self._org, token=data["session"],
                recv=recv, send=ws.send, handler=self._handler,
            )
        except Exception:  # HandshakeError, RecordError, transport failures
            pass
        finally:
            with contextlib.suppress(Exception):
                await ws.close()


async def direct_connect(
    addr: str,
    *,
    org: str,
    root_pub: str,
    session: str,
    now: Optional[int] = None,
    timeout: float = 3.0,
) -> ViewerChannel:
    """Dial one candidate address; returns an established E2E channel.

    The whole attempt — TCP+WS open, preamble, handshake — is bounded by
    *timeout* so one blackholed candidate never stalls the chain.
    """
    async def attempt() -> ViewerChannel:
        ws = await websockets.connect(
            addr, max_size=2**22, compression=None, open_timeout=timeout
        )
        try:
            await ws.send(json.dumps({"v": DIRECT_VERSION, "session": session}))
            eph_priv, client_hello = build_client_hello()
            client_eph = eph_priv.public_key().public_bytes_raw().hex()
            await ws.send(client_hello)
            server_hello = await ws.recv()
            if isinstance(server_hello, str):
                raise HandshakeError("expected binary SERVER_HELLO")
            server_eph, transcript_hash = verify_server_hello(
                server_hello, root_pub=root_pub, org=org, token=session,
                client_eph=client_eph, now=now,
            )
            return ViewerChannel(ws, ChannelCrypto.client(eph_priv, server_eph,
                                                          transcript_hash))
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise

    return await asyncio.wait_for(attempt(), timeout=timeout)
