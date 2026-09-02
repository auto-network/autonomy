#!/usr/bin/env python3
"""serve.auto.network :443 edge — a dumb TCP forward to the raw-stream ingress.

serve.auto.network has its own public IP (a Hetzner floating IP on the relay
host). This forwards raw :443 TCP — the browser's TLS bytes, byte-unmodified —
to the registry's raw-stream ingress on 127.0.0.1:8479, which peeks the
ClientHello SNI ITSELF and routes to the leased persona/machine tunnel
(auto-9z1xh). No TLS termination and no SNI logic live here: the ingress owns
routing, so serve needs no caddy-l4 SNI demux — one IP, one dumb pipe, and the
primary IP's Caddy (auto/registry/relay) is untouched.

Deliberately minimal: stdlib only, no config beyond bind/target. Each
upstream connection is prefixed with a PROXY protocol v2 header carrying
the real client address (auto-p20eb), so the loopback→ingress hop no
longer erases the source: the ingress reads the header and attributes the
stream to the true client for per-source accounting. The header is sent
before any client bytes and byte-transparently precedes the forwarded
ClientHello.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging

logger = logging.getLogger("serve-forward")

#: PROXY v2 fixed 12-byte signature (RFC "PROXY protocol" v2).
_PROXY_V2_SIG = b"\r\n\r\n\x00\r\nQUIT\n"


def _proxy_v2_header(peer) -> bytes:
    """A PROXY v2 PROXY-command header for *peer* (a (host, port[, ...])
    tuple), or a LOCAL header when the address is unavailable/unusable so
    the ingress falls back to the socket peer rather than dropping the
    connection."""
    try:
        addr = ipaddress.ip_address(peer[0])
        src_port = int(peer[1])
    except (TypeError, ValueError, IndexError):
        # LOCAL command, AF_UNSPEC: a health check or an odd peer — the
        # ingress treats this as "no client, use the socket peer".
        return _PROXY_V2_SIG + bytes([0x20, 0x00]) + (0).to_bytes(2, "big")
    if addr.version == 4:
        fam_proto, body = 0x11, (
            addr.packed + b"\x00\x00\x00\x00"
            + src_port.to_bytes(2, "big") + (0).to_bytes(2, "big")
        )
    else:
        fam_proto, body = 0x21, (
            addr.packed + (b"\x00" * 16)
            + src_port.to_bytes(2, "big") + (0).to_bytes(2, "big")
        )
    return (
        _PROXY_V2_SIG + bytes([0x21, fam_proto])
        + len(body).to_bytes(2, "big") + body
    )


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError, NotImplementedError):
            writer.write_eof()


async def _handle(cr, cw, target_host: str, target_port: int):
    peer = cw.get_extra_info("peername")
    try:
        ur, uw = await asyncio.open_connection(target_host, target_port)
    except OSError:
        with contextlib.suppress(Exception):
            cw.close()
        return
    # The real client address goes first, as a PROXY v2 header, before any
    # forwarded byte — the ingress consumes it and attributes the stream.
    try:
        uw.write(_proxy_v2_header(peer))
        await uw.drain()
    except (ConnectionError, OSError):
        with contextlib.suppress(Exception):
            cw.close()
            uw.close()
        return
    up = asyncio.create_task(_pump(cr, uw))
    down = asyncio.create_task(_pump(ur, cw))
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (up, down):
            t.cancel()
        for w in (cw, uw):
            with contextlib.suppress(Exception):
                w.close()
    logger.info("closed forward from %s", peer)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bind", required=True)
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--target-host", default="127.0.0.1")
    p.add_argument("--target-port", type=int, default=8479)
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, a.target_host, a.target_port),
        a.bind, a.port,
    )
    logger.warning("serve forward listening on %s:%d -> %s:%d",
                   a.bind, a.port, a.target_host, a.target_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
