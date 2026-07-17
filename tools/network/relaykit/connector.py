"""Dashboard-side tunnel connector — spec §5.1: the dashboard dials OUT.

Maintains one outbound WebSocket to the registry relay (``/t/{org}``),
authenticates with a ``tunnel:serve`` hello, then serves E2E channels
muxed down it. Zero inbound ports on the dashboard.

Reconnect: exponential backoff with jitter (``min_backoff`` doubling to
``max_backoff``), reset after a successful hello. A rejected hello is
retried at max backoff rather than treated as fatal — certs renew and
bindings heal without operator involvement.

Each viewer channel runs its own task: OPEN spawns it, DATA frames feed
its queue, and the E2E handshake + record layer (``channel.py``) happen
entirely inside it — one slow channel never stalls the tunnel read loop
or its siblings.

The *handler* is the application seam (C4 wires the real target
resolver into it): ``async def handler(token, message) -> response``.
Request/response per E2E message; ``EchoHandler`` is the reference
implementation used by the soak tests.

Runnable directly for tests / manual bring-up::

    python -m tools.network.relaykit.connector \
        --relay ws://127.0.0.1:8477 --org <uuid> \
        --key-file session.hex --cert-file session.cert --mode echo
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import time

import websockets

from tools.network.idkit import DelegationCert, KeyPair

from .channel import ChannelCrypto, build_server_hello, parse_client_hello
from .frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    FrameError,
    decode_frame,
    encode_frame,
)
from .hello import build_tunnel_hello


async def echo_handler(token: str, message: bytes) -> bytes:
    """Reference handler: byte-exact echo (what the soak test asserts)."""
    return message


async def serve_channel(key: KeyPair, cert: DelegationCert, *, org: str, token: str,
                        recv, send, handler) -> None:
    """Serve one E2E channel from the org-key end, transport-agnostic.

    *recv* returns the next incoming channel message (``None`` ends the
    channel), *send* transmits one outgoing message. The tunnel path
    feeds these from mux frames; the direct path (G1) feeds them from a
    dedicated WebSocket. Handshake first, then request/response messages
    through *handler*.
    """
    first = await recv()
    if first is None:
        return
    client_eph = parse_client_hello(first)
    eph_priv, server_hello, transcript_hash = build_server_hello(
        key, cert, org=org, token=token, client_eph=client_eph
    )
    await send(server_hello)
    crypto = ChannelCrypto.server(eph_priv, client_eph, transcript_hash)

    while True:
        record = await recv()
        if record is None:
            return
        message = crypto.open_record(record)
        if message is None:
            continue
        response = await handler(token, message)
        if response is None:
            continue
        for out in crypto.seal_message(response):
            await send(out)


def file_handler(path: str, content_type: str):
    """Serve one file over channel fetch protocol v1 — the C4 seam.

    Protocol: request is canonical JSON ``{"op": "fetch", "v": 1}``;
    response is a JSON header line (``{v, status, content_type}``), a
    newline, then the body bytes. C4's real target resolver replaces this
    with grant-checked (I9) per-target lookup behind the same protocol.
    """
    from tools.network.idkit import canonical_json

    body = open(path, "rb").read()
    ok = canonical_json({"v": 1, "status": 200, "content_type": content_type}) + b"\n" + body
    bad = canonical_json({"v": 1, "status": 400, "content_type": "text/plain"}) + b"\nbad request"

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except ValueError:
            return bad
        if not isinstance(request, dict) or request.get("op") != "fetch":
            return bad
        return ok

    return handler


class TunnelConnector:
    def __init__(
        self,
        relay_url: str,
        org: str,
        key: KeyPair,
        cert: DelegationCert,
        handler=echo_handler,
        *,
        min_backoff: float = 0.2,
        max_backoff: float = 5.0,
    ):
        self._url = f"{relay_url.rstrip('/')}/t/{org}"
        self._org = org
        self._key = key
        self._cert = cert
        self._handler = handler
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._stop = asyncio.Event()
        #: set while a tunnel is authenticated and serving (tests await it)
        self.connected = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Dial, serve, and re-dial until :meth:`stop`."""
        backoff = self._min_backoff
        while not self._stop.is_set():
            try:
                # compression=None: mux frames are E2E ciphertext (I5) —
                # incompressible anyway, and literal bytes keep the
                # ciphertext-on-the-wire property directly observable.
                async with websockets.connect(self._url, max_size=2**22,
                                              compression=None) as ws:
                    await self._handshake(ws)
                    backoff = self._min_backoff
                    self.connected.set()
                    try:
                        await self._serve(ws)
                    finally:
                        self.connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # transient by assumption; backoff decides the pace
            if self._stop.is_set():
                return
            await asyncio.sleep(backoff * (1 + random.random() * 0.25))
            backoff = min(backoff * 2, self._max_backoff)

    async def _handshake(self, ws) -> None:
        """Authenticate a fresh tunnel. The peer-relay park connector
        (``peer.PeerParkConnector``) overrides this to first demand the
        relay's own ``relay:serve`` proof before presenting a hello."""
        await ws.send(build_tunnel_hello(
            self._key, self._cert, org=self._org, ts=int(time.time())
        ))
        reply = json.loads(await ws.recv())
        if not (isinstance(reply, dict) and reply.get("ok")):
            raise ConnectionError(f"hello rejected: {reply!r}")

    async def _serve(self, ws) -> None:
        send_lock = asyncio.Lock()
        channels: dict = {}  # channel_id -> asyncio.Queue
        tasks: dict = {}

        async def send_frame(frame_type: int, channel_id: bytes, payload: bytes = b"") -> None:
            async with send_lock:
                await ws.send(encode_frame(frame_type, channel_id, payload))

        def drop(channel_id: bytes) -> None:
            queue = channels.pop(channel_id, None)
            if queue is not None:
                queue.put_nowait(None)  # poison pill for the channel task

        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                try:
                    frame = decode_frame(raw)
                except FrameError:
                    break
                if frame.type == FRAME_OPEN:
                    try:
                        token = json.loads(frame.payload)["token"]
                    except (ValueError, KeyError, TypeError):
                        await send_frame(FRAME_CLOSE, frame.channel_id)
                        continue
                    queue: asyncio.Queue = asyncio.Queue()
                    channels[frame.channel_id] = queue
                    tasks[frame.channel_id] = asyncio.create_task(
                        self._serve_channel(frame.channel_id, token, queue, send_frame, drop)
                    )
                elif frame.type == FRAME_DATA:
                    queue = channels.get(frame.channel_id)
                    if queue is not None:
                        queue.put_nowait(frame.payload)
                elif frame.type == FRAME_CLOSE:
                    drop(frame.channel_id)
        finally:
            for channel_id in list(channels):
                drop(channel_id)
            for task in tasks.values():
                task.cancel()
            for task in tasks.values():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _serve_channel(self, channel_id: bytes, token: str,
                             queue: asyncio.Queue, send_frame, drop) -> None:
        """One viewer channel: handshake, then request/response messages."""
        try:
            await serve_channel(
                self._key, self._cert, org=self._org, token=token,
                recv=queue.get,
                send=lambda data: send_frame(FRAME_DATA, channel_id, data),
                handler=self._handler,
            )
        except Exception:  # HandshakeError, RecordError, transport failures
            with contextlib.suppress(Exception):
                await send_frame(FRAME_CLOSE, channel_id)
        finally:
            drop(channel_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="auto.network dashboard tunnel connector")
    parser.add_argument("--relay", required=True, help="relay base URL, e.g. ws://127.0.0.1:8477")
    parser.add_argument("--org", required=True)
    parser.add_argument("--key-file", required=True, help="file holding the private key hex")
    parser.add_argument("--cert-file", required=True, help="file holding the cert wire JSON")
    parser.add_argument("--mode", choices=["echo", "serve-file"], default="echo")
    parser.add_argument("--file", help="file to serve (serve-file mode)")
    parser.add_argument("--content-type", default="text/html")
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())

    if args.mode == "serve-file":
        if not args.file:
            parser.error("--mode serve-file requires --file")
        handler = file_handler(args.file, args.content_type)
    else:
        handler = echo_handler

    connector = TunnelConnector(
        args.relay, args.org, key, cert, handler,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
    )
    asyncio.run(connector.run())


if __name__ == "__main__":
    main()
