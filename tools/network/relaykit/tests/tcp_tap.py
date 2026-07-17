"""Passive TCP tap for the ciphertext-on-the-wire assertion.

Sits between the connector and the registry relay, forwarding bytes
verbatim while appending each direction to a capture file. This is the
test's eavesdropper: everything the relay link carries — WS handshake,
mux frames, channel payloads — lands in these files exactly as it
crossed the wire (clients run ``compression=None``, and the
server→client direction is unmasked per RFC 6455, so plaintext WOULD be
literally visible there if E2E encryption were absent).

Run: ``python tcp_tap.py <listen_port> <upstream_port> <c2s_file> <s2c_file>``
"""

from __future__ import annotations

import asyncio
import contextlib
import sys


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                capture_path: str) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            with open(capture_path, "ab") as fh:
                fh.write(data)
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def main(listen_port: int, upstream_port: int, c2s_path: str, s2c_path: str) -> None:
    async def handle(reader, writer):
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", upstream_port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            _pump(reader, up_writer, c2s_path),
            _pump(up_reader, writer, s2c_path),
            return_exceptions=True,
        )

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]))
