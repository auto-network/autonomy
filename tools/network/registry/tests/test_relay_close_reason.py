"""The relay forwards a connector's coded close (code + its own words) to
the viewer verbatim, and still says 1000 for an empty close frame."""
from __future__ import annotations

import asyncio

from tools.network.registry.relay import Tunnel
from tools.network.relaykit.close_codes import decode_close_payload, encode_close_payload


class _Socket:
    def __init__(self):
        self.sent = asyncio.Event()
        self.frames = []
        self.closes = []

    async def send_bytes(self, payload):
        self.frames.append(payload)
        self.sent.set()

    async def close(self, *, code, reason=""):
        self.closes.append((code, reason))


def test_close_viewer_forwards_code_and_reason():
    async def run():
        tunnel = Tunnel(_Socket(), "test-org")
        viewer = _Socket()
        channel_id = b"v" * 16
        tunnel.add_viewer(channel_id, viewer)
        code, reason = decode_close_payload(
            encode_close_payload(4502, "link key resolution refused"))
        tunnel.close_viewer(channel_id, code, reason)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert viewer.closes == [(4502, "link key resolution refused")]
        assert channel_id not in tunnel.channels

    asyncio.run(run())


def test_an_empty_close_frame_is_still_a_normal_1000():
    async def run():
        tunnel = Tunnel(_Socket(), "test-org")
        viewer = _Socket()
        channel_id = b"w" * 16
        tunnel.add_viewer(channel_id, viewer)
        tunnel.close_viewer(channel_id, *decode_close_payload(b""))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert viewer.closes == [(1000, "")]

    asyncio.run(run())
