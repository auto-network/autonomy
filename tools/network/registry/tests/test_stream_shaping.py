"""Shape-never-reset (operator rule 2026-09-01, from the TURN incident):
a drained byte bucket throttles a raw stream and resumes on refill; it
never disconnects for a normal-rate deficit. Reset 4 exists only behind
the explicit *charge_starvation* emergency opt-in.
"""

from __future__ import annotations

import asyncio

from tools.network.registry.stream_ingress import RelayRawStream


class _FakeTunnel:
    def __init__(self):
        self.raw_streams = {}
        self.frames = []

    async def send_frame(self, frame_type, channel_id, payload=b""):
        self.frames.append((frame_type, payload))


class _RefillingLease:
    """Empty for the first *refusals* charges, then refills forever."""

    def __init__(self, refusals: int):
        self.refusals = refusals
        self.released = False

    def charge_bytes(self, n: int) -> bool:
        if self.refusals > 0:
            self.refusals -= 1
            return False
        return True

    def release(self) -> None:
        self.released = True


def _stream(lease, **kw) -> RelayRawStream:
    return RelayRawStream(
        _FakeTunnel(), b"c" * 16, "res", "h.example",
        reader=None, writer=None, abuse_lease=lease, **kw,
    )


def test_drained_bucket_waits_for_refill_and_never_fails():
    async def scenario():
        stream = _stream(_RefillingLease(refusals=5))
        assert await stream._charge(1024) is True  # waited ~0.5s, no reset

    asyncio.run(scenario())


def test_default_is_indefinite_backpressure():
    async def scenario():
        stream = _stream(_RefillingLease(refusals=25))
        # 25 refusals ≈ 2.5s of waiting — far beyond any per-call bound,
        # still resolved by refill rather than failure.
        assert await stream._charge(1024) is True

    asyncio.run(scenario())


def test_emergency_starvation_bound_is_explicit_opt_in():
    async def scenario():
        stream = _stream(
            _RefillingLease(refusals=10 ** 9), charge_starvation=0.3
        )
        assert await stream._charge(1024) is False

    asyncio.run(scenario())
