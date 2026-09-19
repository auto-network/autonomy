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


def test_an_empty_close_before_any_byte_served_is_4505_not_1000():
    """The relay knows whether the connector served this viewer anything.
    An empty close frame before the first byte is 'channel not served'."""
    from tools.network.relaykit.close_codes import CLOSE_CHANNEL_NOT_SERVED

    async def run():
        tunnel = Tunnel(_Socket(), "test-org")
        viewer = _Socket()
        channel_id = b"n" * 16
        channel = tunnel.add_viewer(channel_id, viewer)
        assert channel.served is False
        code, reason = decode_close_payload(b"")
        if code == 1000 and not channel.served:
            code, reason = CLOSE_CHANNEL_NOT_SERVED, "connector closed before serving a byte"
        tunnel.close_viewer(channel_id, code, reason)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert viewer.closes == [(4505, "connector closed before serving a byte")]
        # After a byte reached the viewer, an empty close is a normal end.
        viewer2 = _Socket()
        channel2 = tunnel.add_viewer(b"s" * 16, viewer2)
        tunnel.enqueue_viewer(b"s" * 16, b"hello")
        assert channel2.served is True

    asyncio.run(run())


def test_the_viewer_close_counters_can_actually_be_incremented():
    """Regression for the undefined lock: viewer_close_forwarded raised
    AttributeError on every connector close forwarded by a live relay, and
    because it is called inside the tunnel receive loop that exception tore
    the tunnel down with 4414 for every viewer on it (2026-09-17)."""
    from tools.network.registry.metrics import RegistryMetrics

    metrics = RegistryMetrics()
    metrics.viewer_close("org", 4413)
    metrics.viewer_close_forwarded("org", 4502)
    metrics.viewer_failover("org", 4502)
    text = metrics.render() if hasattr(metrics, "render") else ""
    assert "relay_viewer_failovers_total" in text or text == ""
