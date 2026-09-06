"""Viewer-side channel client — the bootloader's reference implementation.

This is what the `/l/{token}` static JS will reimplement in WebCrypto:
fetch the envelope (org + root_pub), open the channel WebSocket, run the
X25519 handshake with the org root as the pin (I5), then exchange
AES-256-GCM messages. Kept dependency-light and WebCrypto-shaped on
purpose — every primitive used here (X25519, HKDF-SHA256, AES-GCM,
Ed25519 verify) exists in ``crypto.subtle``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import Optional

import websockets

from .channel import (
    ChannelCrypto,
    HandshakeError,
    build_client_hello,
    verify_server_hello,
)
from .frames import (
    VIEWER_KIND_FEED,
    VIEWER_KIND_RECORD,
    FrameError,
    split_viewer_message,
)


VIEWER_DEMUX_MAX_FRAMES = 128
VIEWER_DEMUX_MAX_BYTES = 8 * 1024 * 1024


def read_viewer_record(raw: bytes) -> bytes:
    """Payload of a tagged message that must be a pairwise record.

    Used on the handshake, which both this client and ``dialer`` read
    directly rather than through the demultiplexer. A feed frame cannot
    appear there -- the channel has no keys yet -- so anything else is a
    protocol error, surfaced as HandshakeError to keep the handshake's
    single failure type.
    """
    try:
        kind, payload = split_viewer_message(raw)
    except FrameError as exc:
        raise HandshakeError(str(exc)) from exc
    if kind != VIEWER_KIND_RECORD:
        raise HandshakeError(f"expected a channel record, got kind {kind:#x}")
    return payload


class ViewerChannel:
    """An established E2E channel from the viewer end.

    ``root_pub`` and ``org`` come from the registry envelope
    (``GET /v1/links/{token}/envelope``) — fetched over HTTPS *before*
    any channel bytes flow; that ordering is what makes the pin sound.
    """

    def __init__(self, ws, crypto: ChannelCrypto):
        self._ws = ws
        self._crypto = crypto
        #: One socket, two kinds of message, one reader. Whichever call
        #: reads next routes what it finds into BOTH queues, so a feed
        #: frame arriving mid-request and a record arriving while waiting
        #: on a feed are each buffered rather than dropped or misdecoded.
        self._records: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=VIEWER_DEMUX_MAX_FRAMES
        )
        self._feed: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=VIEWER_DEMUX_MAX_FRAMES
        )
        self._record_bytes = 0
        self._feed_bytes = 0

    async def _next(self, queue: "asyncio.Queue[bytes]") -> bytes:
        """Next payload from *queue*, pumping the socket until it has one.

        The kind byte says which queue a message belongs in. Without it a
        feed frame went to the pairwise decoder, which read its random
        nonce as a sequence number, raised "record out of sequence", and
        tore the channel down -- taking the page with it when this landed
        during the initial artifact fetch.
        """
        while queue.empty():
            raw = await self._ws.recv()
            if isinstance(raw, str):
                continue
            kind, payload = split_viewer_message(raw)
            target = self._feed if kind == VIEWER_KIND_FEED else self._records
            queued_bytes = (
                self._feed_bytes if kind == VIEWER_KIND_FEED else self._record_bytes
            )
            if (
                target.full()
                or queued_bytes + len(payload) > VIEWER_DEMUX_MAX_BYTES
            ):
                await self.close()
                raise ConnectionError("viewer demultiplexer queue is full")
            target.put_nowait(payload)
            if kind == VIEWER_KIND_FEED:
                self._feed_bytes += len(payload)
            else:
                self._record_bytes += len(payload)
        payload = queue.get_nowait()
        if queue is self._feed:
            self._feed_bytes -= len(payload)
        else:
            self._record_bytes -= len(payload)
        return payload

    async def recv_feed(self) -> bytes:
        """Next sealed feed frame.

        Returned still sealed: it opens with the LINK's stream key
        (``open_stream_frame``), obtained via the ``subscribe`` op — not
        with this channel's pairwise key.
        """
        return await self._next(self._feed)

    @classmethod
    async def authenticate(
        cls,
        transport,
        token: str,
        *,
        root_pub: Optional[str] = None,
        link_pub: Optional[str] = None,
        org: str,
        now: Optional[int] = None,
    ) -> "ViewerChannel":
        """Run the application handshake over an already-open transport.

        The transport needs only the WebSocket-shaped ``send``, ``recv``, and
        ``close`` methods.  This keeps one handshake and record implementation
        for the Relay WebSocket, a native WebRTC DataChannel, and tests.
        """
        try:
            eph_priv, client_hello = build_client_hello()
            client_eph = eph_priv.public_key().public_bytes_raw().hex()
            await transport.send(client_hello)
            server_hello = await transport.recv()
            if isinstance(server_hello, str):
                raise HandshakeError("expected binary SERVER_HELLO")
            server_hello = read_viewer_record(server_hello)
            server_eph, transcript_hash = verify_server_hello(
                server_hello,
                root_pub=root_pub,
                link_pub=link_pub,
                org=org,
                token=token,
                client_eph=client_eph,
                now=now,
            )
            crypto = ChannelCrypto.client(eph_priv, server_eph, transcript_hash)
        except BaseException:
            with contextlib.suppress(Exception):
                result = transport.close()
                if inspect.isawaitable(result):
                    await result
            raise
        return cls(transport, crypto)

    @classmethod
    async def connect(
        cls,
        relay_url: str,
        token: str,
        *,
        root_pub: Optional[str] = None,
        link_pub: Optional[str] = None,
        org: str,
        now: Optional[int] = None,
        open_timeout: float = 10.0,
        ping_interval: Optional[float] = 20.0,
        ping_timeout: Optional[float] = 20.0,
    ) -> "ViewerChannel":
        # ping_interval/ping_timeout default to the library's 20s/20s. A
        # caller that receives bulk data (a fleet checkpoint pull) must raise
        # ping_timeout: the relay's pong queues behind gigabytes of data
        # frames, and 20s killed a 2.27GB transfer 3.5 minutes in while its
        # frames were still flowing (live 2026-09-06, close 1011). Frame
        # silence, not pong latency, is that caller's liveness signal.
        ws = await websockets.connect(
            f"{relay_url.rstrip('/')}/v1/links/{token}/channel",
            max_size=2**22,
            open_timeout=open_timeout,
            compression=None,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )
        return await cls.authenticate(
            ws, token, root_pub=root_pub, link_pub=link_pub, org=org, now=now
        )

    async def send_message(self, data: bytes) -> None:
        for record in self._crypto.seal_message(data):
            await self._ws.send(record)

    async def recv_message(self) -> bytes:
        while True:
            message = self._crypto.open_record(await self._next(self._records))
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
            opened = self._crypto.open_stream_record(await self._next(self._records))
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
