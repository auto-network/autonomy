"""Per-viewer relay backpressure and teardown (auto-agc4a).

These tests exercise the registry primitive directly so the slow socket is
deterministic: one viewer's ``send_bytes`` never completes, while another
viewer on the same org tunnel must continue making progress.
"""

from __future__ import annotations

import asyncio

import tools.network.registry.relay as relay
from tools.network.registry.relay import (
    CLOSE_UNKNOWN_LINK,
    CLOSE_VIEWER_QUEUE_OVERFLOW,
    VIEWER_QUEUE_MAX_BYTES,
    Tunnel,
    viewer_endpoint,
)
from tools.network.relaykit.frames import FRAME_CLOSE, decode_frame


class _TunnelSocket:
    def __init__(self):
        self.frames: list[bytes] = []
        self.sent = asyncio.Event()

    async def send_bytes(self, payload: bytes) -> None:
        self.frames.append(payload)
        self.sent.set()


class _ViewerSocket:
    def __init__(self, *, blocked: bool = False):
        self.blocked = blocked
        self.send_started = asyncio.Event()
        self.sent = asyncio.Event()
        self.closed = asyncio.Event()
        self.payloads: list[bytes] = []
        self.close_codes: list[int] = []
        self._release = asyncio.Event()

    async def send_bytes(self, payload: bytes) -> None:
        self.send_started.set()
        if self.blocked:
            await self._release.wait()
        self.payloads.append(payload)
        self.sent.set()

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)
        self.closed.set()
        self._release.set()


def test_slow_viewer_overflow_isolated_from_other_channel():
    async def run():
        assert VIEWER_QUEUE_MAX_BYTES == 2 * 1024 * 1024
        assert CLOSE_VIEWER_QUEUE_OVERFLOW == 4413

        tunnel_socket = _TunnelSocket()
        tunnel = Tunnel(tunnel_socket, "test-org")
        slow_socket = _ViewerSocket(blocked=True)
        fast_socket = _ViewerSocket()
        slow_id = b"s" * 16
        fast_id = b"f" * 16
        slow = tunnel.add_viewer(slow_id, slow_socket)
        fast = tunnel.add_viewer(fast_id, fast_socket)

        one_mib = b"x" * (1024 * 1024)
        tunnel.enqueue_viewer(slow_id, one_mib)
        await asyncio.wait_for(slow_socket.send_started.wait(), timeout=1)

        # The slow send remains charged. A second MiB exactly fills the
        # channel's cushion, but it does not obstruct a different channel.
        tunnel.enqueue_viewer(slow_id, one_mib)
        assert slow.queued_bytes == VIEWER_QUEUE_MAX_BYTES
        tunnel.enqueue_viewer(fast_id, b"channel-b-progress")
        await asyncio.wait_for(fast_socket.sent.wait(), timeout=1)
        assert fast_socket.payloads == [b"channel-b-progress"]

        # One more byte fails closed for only the slow channel.
        tunnel.enqueue_viewer(slow_id, b"!")
        await asyncio.wait_for(slow_socket.closed.wait(), timeout=1)
        assert slow_socket.close_codes == [CLOSE_VIEWER_QUEUE_OVERFLOW]
        assert slow_id not in tunnel.channels
        assert tunnel.channels[fast_id] is fast
        assert slow.queued_bytes == 0

        fast_socket.sent.clear()
        tunnel.enqueue_viewer(fast_id, b"still-live-after-overflow")
        await asyncio.wait_for(fast_socket.sent.wait(), timeout=1)
        assert fast_socket.payloads[-1] == b"still-live-after-overflow"

        # The dashboard is told to release only the overflowed channel.
        await asyncio.wait_for(tunnel_socket.sent.wait(), timeout=1)
        close_frame = decode_frame(tunnel_socket.frames[-1])
        assert close_frame.type == FRAME_CLOSE
        assert close_frame.channel_id == slow_id

        await tunnel.close_all_viewers(1001)

    asyncio.run(run())


def test_tunnel_teardown_cancels_writer_and_releases_queued_bytes():
    async def run():
        tunnel = Tunnel(_TunnelSocket(), "test-org")
        socket = _ViewerSocket(blocked=True)
        channel_id = b"q" * 16
        channel = tunnel.add_viewer(channel_id, socket)

        payload = b"x" * (VIEWER_QUEUE_MAX_BYTES // 2)
        tunnel.enqueue_viewer(channel_id, payload)
        await asyncio.wait_for(socket.send_started.wait(), timeout=1)
        tunnel.enqueue_viewer(channel_id, payload)
        assert channel.queued_bytes == VIEWER_QUEUE_MAX_BYTES

        await asyncio.wait_for(tunnel.close_all_viewers(1001), timeout=1)
        assert tunnel.channels == {}
        assert channel.queued_bytes == 0
        assert channel._writer_task.done()
        assert socket.close_codes == [1001]

    asyncio.run(run())


# -- per-tunnel viewer channel cap (auto-lh57j abuse bound) -----------------


class _AcceptCloseSocket:
    def __init__(self):
        self.accepted = False
        self.close_codes: list[int] = []

    async def accept(self):
        self.accepted = True

    async def close(self, *, code: int):
        self.close_codes.append(code)


def test_viewer_channel_cap_rejects_when_tunnel_full(monkeypatch):
    # A tunnel already at the per-tunnel cap refuses a further viewer with the
    # same anonymous code as every other admission failure. One bearer-link
    # holder cannot open unbounded channels or learn the internal reason.
    monkeypatch.setattr(relay, "MAX_VIEWER_CHANNELS_PER_TUNNEL", 2)
    tunnel = Tunnel(_TunnelSocket(), "test-org")
    tunnel.channels = {b"a" * 16: object(), b"b" * 16: object()}  # at cap

    class _Link:
        org_uuid = "test-org"
        target_type = "note"

    class _Hub:
        def get(self, org):
            return tunnel

    monkeypatch.setattr(relay, "_resolve_live_link", lambda store, token, now: _Link())
    ws = _AcceptCloseSocket()

    asyncio.run(viewer_endpoint(ws, "0" * 32, _Hub(), None, lambda: 0))

    assert ws.accepted
    assert ws.close_codes == [CLOSE_UNKNOWN_LINK]
    assert len(tunnel.channels) == 2  # the rejected viewer was not admitted


def test_viewer_channel_cap_admits_below_capacity(monkeypatch):
    # Below the cap a viewer is admitted (a channel is created); the send of
    # its FRAME_OPEN then fails on the fake tunnel and it is cleaned up — the
    # point here is that the cap did not reject it.
    monkeypatch.setattr(relay, "MAX_VIEWER_CHANNELS_PER_TUNNEL", 8)
    tunnel = Tunnel(_TunnelSocket(), "test-org")

    class _Link:
        org_uuid = "test-org"
        target_type = "note"

    class _Hub:
        def get(self, org):
            return tunnel

    monkeypatch.setattr(relay, "_resolve_live_link", lambda store, token, now: _Link())

    class _AdmitSocket(_AcceptCloseSocket):
        async def receive(self):
            return {"type": "websocket.disconnect"}

    ws = _AdmitSocket()
    asyncio.run(viewer_endpoint(ws, "0" * 32, _Hub(), None, lambda: 0))
    # It reached FRAME_OPEN rather than being refused by the cap. The fake
    # tunnel then fails that send and uses the same uniform public close.
    assert len(tunnel.channels) == 0
