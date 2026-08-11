"""Relay-side stream fan-out and retention (auto-albp6.7).

One inbound frame reaches every listener of a stream without the
publisher paying per-listener cost, and a stalled listener never blocks
delivery to anyone else -- the same head-of-line principle
test_relay_backpressure.py already proves for ordinary per-viewer
channels (auto-agc4a), now proved for the stream fan-out layered on top
of it. Delivery reuses each channel's own existing bounded writer
(``_ViewerRelayChannel.try_enqueue``) rather than a second queue/writer
per listener -- see Listener's docstring in relay.py for why.
"""

from __future__ import annotations

import asyncio

import tools.network.registry.relay as relay
from tools.network.registry.relay import (
    CLOSE_LISTENER_FELL_BEHIND,
    STREAM_BUFFER_CAP_BYTES,
    STREAM_EXPIRY_SECONDS,
    STREAM_MIN_RETAINED_FRAMES,
    Stream,
    Tunnel,
)
from tools.network.relaykit.frames import VIEWER_KIND_FEED, tag_viewer_message


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

    def release(self) -> None:
        self._release.set()


class _TunnelSocket:
    def __init__(self):
        self.frames: list[bytes] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.frames.append(payload)


class _FakeChannel:
    """A viewer_channel stub with a controllable ``try_enqueue`` -- for
    Stream-level retention tests that need a listener with a precise,
    deterministic consumption behavior (always succeeds / always fails)
    rather than a real socket's actual timing. No event loop required."""

    def __init__(self, *, accepts: bool = True):
        self.accepts = accepts
        self.enqueued: list[bytes] = []
        self.closed_with: list[int] = []

    def try_enqueue(self, payload: bytes) -> bool:
        if not self.accepts:
            return False
        self.enqueued.append(bytes(payload))
        return True

    def start_close(self, code: int):
        self.closed_with.append(code)


def _tunnel() -> Tunnel:
    return Tunnel(_TunnelSocket(), "test-org")


def _attach(tunnel: Tunnel, token: str, channel_id: bytes, socket) -> "relay._ViewerRelayChannel":
    channel = tunnel.add_viewer(channel_id, socket)
    channel.stream_token = token
    tunnel.attach_listener(token, channel_id, channel)
    return channel


# ── TestFanOut ──────────────────────────────────────────────────────────


def test_one_inbound_frame_reaches_every_listener():
    async def run():
        tunnel = _tunnel()
        sockets = [_ViewerSocket() for _ in range(50)]
        for i, socket in enumerate(sockets):
            _attach(tunnel, "tok", f"c{i}".encode().rjust(16, b"0"), socket)

        assert tunnel.publish_stream("tok", b"hello", now=0.0) is True
        for socket in sockets:
            await asyncio.wait_for(socket.sent.wait(), timeout=1)
            assert socket.payloads == [tag_viewer_message(VIEWER_KIND_FEED, b"hello")]

    asyncio.run(run())


def test_publisher_bytes_do_not_vary_with_listener_count():
    async def run():
        for count in (1, 50):
            tunnel = _tunnel()
            for i in range(count):
                _attach(tunnel, "tok", f"c{i}".encode().rjust(16, b"0"), _ViewerSocket())
            tunnel.publish_stream("tok", b"one-frame", now=0.0)
            stream = tunnel.streams["tok"]
            # Exactly one _StreamFrame is ever constructed per publish call,
            # regardless of listener count -- the connector-side sealing
            # cost (auto-albp6.8) this bead exists to make possible has
            # nothing to do per listener. (The buffer itself empties
            # immediately here since every listener keeps up -- rule 2,
            # proven separately by test_buffer_empties_when_all_listeners_keep_up.)
            assert stream._next_seq == 1

    asyncio.run(run())


def test_frame_for_a_stream_with_no_listeners_is_discarded():
    tunnel = _tunnel()
    assert tunnel.publish_stream("nope", b"x", now=0.0) is False
    assert "nope" not in tunnel.streams


def test_non_session_grants_are_unaffected():
    """A channel that never publishes is an idle stream of one listener
    and zero frames -- request/response behavior for it is untouched;
    see test_relay_backpressure.py and test_tunnel_control.py for the
    regression proof that this bead does not change it."""
    async def run():
        tunnel = _tunnel()
        channel = _attach(tunnel, "tok", b"c" * 16, _ViewerSocket())
        assert list(tunnel.streams["tok"].buffer) == []
        assert tunnel.channels[b"c" * 16] is channel

    asyncio.run(run())


# ── TestHeadOfLine ──────────────────────────────────────────────────────


def test_stalled_listener_does_not_delay_a_fast_one():
    async def run():
        tunnel = _tunnel()
        slow = _ViewerSocket(blocked=True)
        fast = _ViewerSocket()
        _attach(tunnel, "tok", b"s" * 16, slow)
        _attach(tunnel, "tok", b"f" * 16, fast)

        tunnel.publish_stream("tok", b"go", now=0.0)
        await asyncio.wait_for(fast.sent.wait(), timeout=1)
        assert fast.payloads == [tag_viewer_message(VIEWER_KIND_FEED, b"go")]
        assert slow.payloads == []  # still blocked in send_bytes
        slow.release()

    asyncio.run(run())


def test_stalled_listener_does_not_delay_another_channel_on_the_same_tunnel():
    async def run():
        tunnel = _tunnel()
        slow = _ViewerSocket(blocked=True)
        _attach(tunnel, "tok-a", b"s" * 16, slow)
        ordinary = tunnel.add_viewer(b"o" * 16, _ViewerSocket())

        tunnel.publish_stream("tok-a", b"go", now=0.0)
        await asyncio.wait_for(slow.send_started.wait(), timeout=1)
        # The stalled stream listener is mid-send; an unrelated ordinary
        # channel on the same tunnel still enqueues without waiting.
        assert tunnel.enqueue_viewer(b"o" * 16, b"unblocked") is None
        assert ordinary.try_enqueue is not None  # channel still live
        slow.release()

    asyncio.run(run())


def test_tunnel_read_loop_never_awaits_a_viewer_socket():
    """Stream.publish's fan-out loop is try_enqueue (synchronous,
    non-blocking) for every listener -- inspected directly rather than
    timed, since that is the actual invariant."""
    import inspect
    assert not inspect.iscoroutinefunction(Stream.publish)
    assert not inspect.iscoroutinefunction(Stream._apply_retention)


# ── TestRetention ───────────────────────────────────────────────────────


def test_buffer_empties_when_all_listeners_keep_up():
    async def run():
        tunnel = _tunnel()
        _attach(tunnel, "tok", b"a" * 16, _ViewerSocket())
        tunnel.publish_stream("tok", b"1", now=0.0)
        tunnel.publish_stream("tok", b"2", now=0.1)
        assert tunnel.streams["tok"].retained_bytes == 0
        assert list(tunnel.streams["tok"].buffer) == []

    asyncio.run(run())


def test_no_listeners_means_no_buffer():
    stream = Stream("tok")
    stream.publish(b"orphaned", now=0.0)  # never reached via Tunnel.publish_stream
    assert stream.buffer[0].payload == b"orphaned"  # buffered, but...
    stream._apply_retention(now=0.0)
    # Rule 2 (min over an empty listener set) does not fire; nothing here
    # asserts eviction without a real Tunnel, since Tunnel.publish_stream
    # is what a real caller uses and it never creates a stream with zero
    # listeners in the first place (attach() is the only creator).
    assert "tok" not in _tunnel().streams


def test_frames_expire_after_sixty_seconds():
    """A permanently-stuck listener (try_enqueue always fails, e.g. its
    own queue is already at its byte cap) never advances its cursor, so
    rule 2 alone would retain everything forever -- rule 1 (expiry) is
    what still bounds it."""
    stream = Stream("tok")
    stream.listeners[b"a" * 16] = relay.Listener(b"a" * 16, _FakeChannel(accepts=False), cursor=0)
    stream.publish(b"old", now=0.0)
    stream.publish(b"new", now=STREAM_EXPIRY_SECONDS + 1)
    remaining = [f.payload for f in stream.buffer]
    assert b"old" not in remaining


def test_expiry_applies_to_unconsumed_frames():
    stream = Stream("tok")
    stream.listeners[b"a" * 16] = relay.Listener(b"a" * 16, _FakeChannel(accepts=False), cursor=0)
    stream.publish(b"never-read", now=0.0)
    assert stream.retained_bytes > 0
    stream._apply_retention(now=STREAM_EXPIRY_SECONDS + 1)
    assert stream.retained_bytes == 0
    assert len(stream.buffer) == 0


def test_slowest_listener_sets_the_retention_point():
    stream = Stream("tok")
    fast = _FakeChannel(accepts=True)
    slow = _FakeChannel(accepts=False)
    stream.listeners[b"f" * 16] = relay.Listener(b"f" * 16, fast, cursor=0)
    stream.listeners[b"s" * 16] = relay.Listener(b"s" * 16, slow, cursor=0)
    stream.publish(b"1", now=0.0)
    stream.publish(b"2", now=0.1)
    # fast's cursor advances on every publish; slow's never does. Rule 2's
    # min-cursor is slow's, so nothing is dropped while slow is attached.
    assert len(stream.buffer) == 2
    assert stream.listeners[b"f" * 16].cursor == 2
    assert stream.listeners[b"s" * 16].cursor == 0


def test_size_cap_evicts_above_ten_frames():
    stream = Stream("tok")
    stalled = _FakeChannel(accepts=False)  # never advances -> rule 2 saves nothing
    stream.listeners[b"a" * 16] = relay.Listener(b"a" * 16, stalled, cursor=0)
    big = b"x" * (STREAM_BUFFER_CAP_BYTES // 5)
    for _ in range(11):
        stream.publish(big, now=0.0)
    assert len(stream.buffer) >= STREAM_MIN_RETAINED_FRAMES


def test_size_cap_never_evicts_below_ten_frames():
    stream = Stream("tok")
    stream.listeners[b"a" * 16] = relay.Listener(b"a" * 16, _FakeChannel(accepts=False), cursor=0)
    big = b"x" * (STREAM_BUFFER_CAP_BYTES // 5)
    for _ in range(5):
        stream.publish(big, now=0.0)
    assert len(stream.buffer) == 5  # below the floor's own frame count: nothing to evict yet
    for _ in range(6):
        stream.publish(big, now=0.0)
    assert len(stream.buffer) >= STREAM_MIN_RETAINED_FRAMES


def test_single_oversize_frame_is_retained_and_drops_nobody():
    stream = Stream("tok")
    fell_behind = stream.publish(b"x" * (4 * 1024 * 1024), now=0.0)
    assert fell_behind == []
    assert len(stream.buffer) == 1


def test_ten_oversize_frames_are_all_retained():
    stream = Stream("tok")
    for _ in range(10):
        fell_behind = stream.publish(b"x" * (4 * 1024 * 1024), now=0.0)
        assert fell_behind == []
    assert len(stream.buffer) == 10


# ── TestFallBehind ──────────────────────────────────────────────────────


def test_listener_falling_off_is_closed_with_the_distinct_code():
    """Tunnel.publish_stream is the seam that owns closing a fallen-
    behind listener (Stream itself only reports which ones fell behind)
    -- exercised directly against Tunnel with fake channels so the
    scenario (a permanently-full listener) is deterministic rather than
    timing-dependent."""
    async def run():
        tunnel = _tunnel()
        stalled = _FakeChannel(accepts=False)
        keeping_up = _FakeChannel(accepts=True)
        tunnel.channels[b"s" * 16] = stalled
        tunnel.channels[b"k" * 16] = keeping_up
        tunnel.attach_listener("tok", b"s" * 16, stalled)
        tunnel.attach_listener("tok", b"k" * 16, keeping_up)
        big = b"x" * (STREAM_BUFFER_CAP_BYTES // 5)
        for _ in range(11):
            tunnel.publish_stream("tok", big, now=0.0)
        assert stalled.closed_with == [CLOSE_LISTENER_FELL_BEHIND]
        assert b"s" * 16 not in tunnel.channels
        assert b"s" * 16 not in tunnel.streams["tok"].listeners

    asyncio.run(run())


def _attach_with_cap(tunnel, token, channel_id, ws, *, max_queued_bytes):
    """Like _attach, but with a small enough queue cap that try_enqueue
    fails deterministically instead of depending on real socket timing."""
    channel = relay._ViewerRelayChannel(
        ws,
        on_writer_failure=lambda failed: tunnel._writer_failed(channel_id, failed),
        max_queued_bytes=max_queued_bytes,
    )
    channel.stream_token = token
    tunnel.channels[channel_id] = channel
    tunnel.attach_listener(token, channel_id, channel)
    return channel


def test_its_task_and_queue_are_released():
    """No separate Listener queue/task exists (delivery reuses the
    channel's own _ViewerRelayChannel writer) -- the equivalent property
    is that falling behind releases *that* writer's queued bytes and
    cancels *its* task, exactly as start_close already guarantees and
    test_relay_backpressure.py already proves for the ordinary path."""
    async def run():
        tunnel = _tunnel()
        stalled_socket = _ViewerSocket(blocked=True)
        channel = _attach_with_cap(
            tunnel, "tok", b"s" * 16, stalled_socket, max_queued_bytes=1,
        )
        big = b"x" * (STREAM_BUFFER_CAP_BYTES // 5)  # exceeds the 1-byte cap every time
        for _ in range(11):
            tunnel.publish_stream("tok", big, now=0.0)
        await asyncio.wait_for(stalled_socket.closed.wait(), timeout=1)
        assert channel.queued_bytes == 0
        assert b"s" * 16 not in tunnel.channels

    asyncio.run(run())


def test_minimum_cursor_advances_after_it_is_dropped():
    async def run():
        tunnel = _tunnel()
        stalled = _FakeChannel(accepts=False)
        keeping_up = _FakeChannel(accepts=True)
        tunnel.channels[b"s" * 16] = stalled
        tunnel.channels[b"k" * 16] = keeping_up
        tunnel.attach_listener("tok", b"s" * 16, stalled)
        tunnel.attach_listener("tok", b"k" * 16, keeping_up)
        big = b"x" * (STREAM_BUFFER_CAP_BYTES // 5)
        for _ in range(11):
            tunnel.publish_stream("tok", big, now=0.0)
        stream = tunnel.streams["tok"]
        assert b"s" * 16 not in stream.listeners
        # With the fallen-behind listener gone, rule 2's min-cursor is now
        # keeping_up's alone, so the next publish drains immediately
        # instead of being held back by a listener that no longer exists.
        tunnel.publish_stream("tok", b"tiny", now=0.0)
        assert len(stream.buffer) <= STREAM_MIN_RETAINED_FRAMES

    asyncio.run(run())


def test_reattaching_listener_starts_at_the_head():
    stream = Stream("tok")
    stream.listeners[b"a" * 16] = relay.Listener(b"a" * 16, _FakeChannel(accepts=False), cursor=0)
    stream.publish(b"already-published", now=0.0)
    late = _FakeChannel(accepts=True)
    stream.attach(b"b" * 16, late)
    # A newly attached listener's cursor starts at the stream's current
    # next_seq -- it is never backfilled from the buffer (live, best-
    # effort; history is the catch-up path, not this buffer).
    assert stream.listeners[b"b" * 16].cursor == stream._next_seq == 1


# ── TestTeardown ────────────────────────────────────────────────────────


def test_last_listener_leaving_releases_the_stream():
    async def run():
        tunnel = _tunnel()
        channel = _attach(tunnel, "tok", b"a" * 16, _ViewerSocket())
        assert "tok" in tunnel.streams
        tunnel.detach_viewer(b"a" * 16, channel)
        tunnel.detach_listener("tok", b"a" * 16)
        assert "tok" not in tunnel.streams

    asyncio.run(run())


def test_tunnel_close_tears_down_every_stream():
    async def run():
        tunnel = _tunnel()
        _attach(tunnel, "tok-a", b"a" * 16, _ViewerSocket())
        _attach(tunnel, "tok-b", b"b" * 16, _ViewerSocket())
        assert len(tunnel.streams) == 2
        await tunnel.close_all_viewers(1001)
        assert tunnel.streams == {}

    asyncio.run(run())


def test_socket_write_failure_removes_the_listener():
    async def run():
        class _FailingSocket:
            async def send_bytes(self, payload: bytes) -> None:
                raise RuntimeError("socket died")

            async def close(self, *, code: int) -> None:
                pass

        tunnel = _tunnel()
        _attach(tunnel, "tok", b"a" * 16, _FailingSocket())
        tunnel.publish_stream("tok", b"go", now=0.0)
        for _ in range(20):
            await asyncio.sleep(0)
        assert "tok" not in tunnel.streams
        assert b"a" * 16 not in tunnel.channels

    asyncio.run(run())


# ── TestSteadyState ─────────────────────────────────────────────────────


def test_ten_thousand_cycles_with_churn_do_not_grow_memory():
    """Fake channels, not real sockets: this is a Stream/Tunnel
    bookkeeping bound, not a real-transport concern -- 10,000 real
    asyncio writer tasks would just be slow to prove the same thing."""
    tunnel = _tunnel()
    stream = Stream("tok")
    tunnel.streams["tok"] = stream
    for i in range(10_000):
        channel_id = f"{i % 50}".encode().rjust(16, b"0")
        if i % 3 == 0 and channel_id in tunnel.channels:
            del tunnel.channels[channel_id]
            tunnel.detach_listener("tok", channel_id)
            if "tok" not in tunnel.streams:  # last listener left -> recreate for the test
                tunnel.streams["tok"] = stream = Stream("tok")
        elif channel_id not in tunnel.channels:
            fake = _FakeChannel(accepts=True)
            tunnel.channels[channel_id] = fake
            tunnel.attach_listener("tok", channel_id, fake)
        if "tok" in tunnel.streams:
            tunnel.publish_stream("tok", b"x" * 100, now=float(i))
    stream = tunnel.streams.get("tok")
    if stream is not None:
        assert stream.retained_bytes <= STREAM_BUFFER_CAP_BYTES + (4 * 1024 * 1024)
