"""Existing tls-stream receive admission, exercised at BOTH real entrypoints.

This proves Python DATA custody against the issued byte grant, not aggregate
WebSocket/socket memory, control fairness, or bounded transport shutdown.
"""

import asyncio
from types import SimpleNamespace

import pytest

from tools.network.registry.stream_ingress import RelayRawStream
from tools.network.relaykit.stream_adapter import _ConnectorStream
from tools.network.relaykit.stream_wire import (
    RESET_PROTOCOL, STREAM_INITIAL_CREDIT, STREAM_MAX_DATA,
    build_ctrl_reset, parse_ctrl,
)
from tools.network.relaykit.frames import FRAME_STREAM_CTRL


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def make_stream(kind):
    frames = []
    writer = Writer()

    async def send(*frame):
        frames.append(frame)

    peer = SimpleNamespace(send_frame=send, raw_streams={}, org="org",
                           forget=lambda channel: None)
    reader = asyncio.StreamReader()  # read blocks until teardown cancels it
    if kind == "relay":
        stream = RelayRawStream(peer, b"channel", "reservation", "host",
                                reader, writer)
        stream.open_ok.set_result(STREAM_INITIAL_CREDIT)
        peer.raw_streams[stream.channel_id] = stream
        run = lambda: stream.run(b"")
    else:
        stream = _ConnectorStream(peer, b"channel", reader, writer,
                                  STREAM_INITIAL_CREDIT)
        run = stream.run
    return stream, writer, frames, run


def ctrl(stream, kind, message):
    if kind == "relay":
        stream.on_ctrl_raw(build_ctrl_reset(RESET_PROTOCOL))
    else:
        stream.on_ctrl(message)


def controls(frames):
    return [parse_ctrl(f[2]) for f in frames if f[0] == FRAME_STREAM_CTRL]


@pytest.mark.parametrize("kind", ["relay", "connector"])
@pytest.mark.parametrize("bad", ["overcredit", "oversize"])
def test_admission_debits_before_enqueue_and_terminal_flood_is_ignored(kind, bad):
    async def scenario():
        stream, writer, frames, run = make_stream(kind)
        # Tiny frames are legal; no arbitrary frame-count limit substitutes
        # for the existing byte-credit contract.
        for _ in range(8):
            stream.on_data(b"x")
        remaining = STREAM_INITIAL_CREDIT - 8
        while remaining:
            n = min(remaining, STREAM_MAX_DATA)
            stream.on_data(b"x" * n)
            remaining -= n
        assert stream.inbound.credit == 0
        assert sum(len(p) for k, p in stream.inbound._queue) == STREAM_INITIAL_CREDIT
        task = asyncio.create_task(run())
        await asyncio.wait_for(writer.entered.wait(), 1)
        assert stream.inbound.credit == 0  # dequeuing is NOT a refund
        stream.on_data(b"!" if bad == "overcredit" else b"!" * (STREAM_MAX_DATA + 1))
        assert stream.inbound.stopped
        assert stream.inbound.empty()
        owned_tasks = set(asyncio.all_tasks())
        for _ in range(1000):
            stream.on_data(b"again")
            stream.on_close()
            ctrl(stream, kind, {"op": "reset", "code": RESET_PROTOCOL})
        assert set(asyncio.all_tasks()) == owned_tasks
        assert stream.inbound.empty()
        await asyncio.wait_for(task, 1)
        assert writer.cancelled.is_set()
        assert writer.closed
        assert controls(frames) == [{"op": "reset", "code": RESET_PROTOCOL}]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["relay", "connector"])
def test_empty_frames_allocate_nothing_and_oversize_fails_before_start(kind):
    async def scenario():
        stream, writer, frames, run = make_stream(kind)
        for _ in range(10000):
            stream.on_data(b"")
        assert stream.inbound.empty()
        assert stream.inbound.credit == STREAM_INITIAL_CREDIT
        stream.on_data(b"!" * (STREAM_MAX_DATA + 1))
        assert stream.inbound.empty()
        assert stream.inbound.credit == STREAM_INITIAL_CREDIT
        await asyncio.wait_for(run(), 1)
        assert not writer.data
        assert controls(frames) == [{"op": "reset", "code": RESET_PROTOCOL}]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["relay", "connector"])
@pytest.mark.parametrize("terminal", ["close", "reset"])
def test_terminal_control_interrupts_blocked_drain_without_refund(kind, terminal):
    async def scenario():
        stream, writer, frames, run = make_stream(kind)
        stream.on_data(b"payload")
        task = asyncio.create_task(run())
        await asyncio.wait_for(writer.entered.wait(), 1)
        if terminal == "close":
            stream.on_close()
        else:
            ctrl(stream, kind, {"op": "reset", "code": RESET_PROTOCOL})
        await asyncio.wait_for(task, 1)
        assert writer.cancelled.is_set()
        assert stream.inbound.credit == STREAM_INITIAL_CREDIT - 7
        assert stream.inbound.empty()
        assert controls(frames) == []
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["relay", "connector"])
def test_compliant_data_refunds_only_after_drain_and_preserves_exact_bytes(kind):
    async def scenario():
        stream, writer, frames, run = make_stream(kind)
        payload = b"x" * (64 * 1024)
        stream.on_data(payload)
        task = asyncio.create_task(run())
        await asyncio.wait_for(writer.entered.wait(), 1)
        assert stream.inbound.credit == STREAM_INITIAL_CREDIT - len(payload)
        assert controls(frames) == []
        writer.release.set()
        # Yield until the pump finishes the immediately-ready drain/send.
        await asyncio.sleep(0)
        assert stream.inbound.credit == STREAM_INITIAL_CREDIT
        assert controls(frames) == [{"op": "credit", "add": len(payload)}]
        assert bytes(writer.data) == payload
        stream.on_close()
        await asyncio.wait_for(task, 1)
    asyncio.run(scenario())
