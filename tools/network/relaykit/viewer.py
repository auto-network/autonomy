"""Viewer-side channel client — the bootloader's reference implementation.

This is what the `/l/{token}` static JS will reimplement in WebCrypto:
fetch the envelope (org + root_pub), open the channel WebSocket, run the
X25519 handshake with the org root as the pin (I5), then exchange
AES-256-GCM messages. Kept dependency-light and WebCrypto-shaped on
purpose — every primitive used here (X25519, HKDF-SHA256, AES-GCM,
Ed25519 verify) exists in ``crypto.subtle``.
"""

from __future__ import annotations

import contextlib
from typing import Optional

import websockets

from .channel import (
    ChannelCrypto,
    HandshakeError,
    build_client_hello,
    verify_server_hello,
)


class ViewerChannel:
    """An established E2E channel from the viewer end.

    ``root_pub`` and ``org`` come from the registry envelope
    (``GET /v1/links/{token}/envelope``) — fetched over HTTPS *before*
    any channel bytes flow; that ordering is what makes the pin sound.
    """

    def __init__(self, ws, crypto: ChannelCrypto):
        self._ws = ws
        self._crypto = crypto

    @classmethod
    async def connect(
        cls,
        relay_url: str,
        token: str,
        *,
        root_pub: str,
        org: str,
        now: Optional[int] = None,
        open_timeout: float = 10.0,
    ) -> "ViewerChannel":
        ws = await websockets.connect(
            f"{relay_url.rstrip('/')}/v1/links/{token}/channel",
            max_size=2**22,
            open_timeout=open_timeout,
            compression=None,
        )
        try:
            eph_priv, client_hello = build_client_hello()
            client_eph = eph_priv.public_key().public_bytes_raw().hex()
            await ws.send(client_hello)
            server_hello = await ws.recv()
            if isinstance(server_hello, str):
                raise HandshakeError("expected binary SERVER_HELLO")
            server_eph, transcript_hash = verify_server_hello(
                server_hello,
                root_pub=root_pub,
                org=org,
                token=token,
                client_eph=client_eph,
                now=now,
            )
            crypto = ChannelCrypto.client(eph_priv, server_eph, transcript_hash)
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        return cls(ws, crypto)

    async def send_message(self, data: bytes) -> None:
        for record in self._crypto.seal_message(data):
            await self._ws.send(record)

    async def recv_message(self) -> bytes:
        while True:
            record = await self._ws.recv()
            if isinstance(record, str):
                continue
            message = self._crypto.open_record(record)
            if message is not None:
                return message

    async def recv_message_stream(self):
        """Yield ``(message, stream_final)`` for a streamed response exchange.

        The client counterpart to the connector's streaming send (c31xb):
        each record is authenticated and reassembled through
        ``open_stream_record`` without buffering the whole exchange; the
        generator ends after the record flagged ``STREAM_FINAL``. A record
        anomaly raises ``RecordError`` and stops delivery, per the record
        layer's tear-down contract.
        """
        parts: list[bytes] = []
        while True:
            record = await self._ws.recv()
            if isinstance(record, str):
                continue
            opened = self._crypto.open_stream_record(record)
            parts.append(opened.chunk)
            if opened.message_end:
                message = b"".join(parts)
                parts = []
                yield message, opened.stream_final
                if opened.stream_final:
                    return

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def __aenter__(self) -> "ViewerChannel":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
