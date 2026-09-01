#!/usr/bin/env python3
"""serve.auto.network :443 edge — a dumb TCP forward to the raw-stream ingress.

serve.auto.network has its own public IP (a Hetzner floating IP on the relay
host). This forwards raw :443 TCP — the browser's TLS bytes, byte-unmodified —
to the registry's raw-stream ingress on 127.0.0.1:8479, which peeks the
ClientHello SNI ITSELF and routes to the leased persona/machine tunnel
(auto-9z1xh). No TLS termination and no SNI logic live here: the ingress owns
routing, so serve needs no caddy-l4 SNI demux — one IP, one dumb pipe, and the
primary IP's Caddy (auto/registry/relay) is untouched.

Deliberately minimal: stdlib only, no config beyond bind/target. The
loopback→ingress hop means the ingress currently attributes the source as
127.0.0.1 (per-source abuse limiting on serving streams is thus deferred until
the ingress consumes PROXY v2 — the documented POC limitation).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging

logger = logging.getLogger("serve-forward")


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
