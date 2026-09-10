"""The relay's directed-stream broker: real ``Tunnel`` objects in a real
``TunnelHub``, driven through the same ``raw_streams`` entry points the
tunnel receive loop uses, with only the WebSocket stood in for.

What these prove, in the broker's own terms: admission is typed and
counted before allocation; READY follows both open-oks and a re-check;
DATA is validated against the sender's window before a byte is retained
and forwarded one-to-one; credit returns to the sender only for an exact
FIFO prefix of receipts; custody per direction never exceeds the
receiver's offer even against a blocked socket; teardown releases once and
tells the survivor; the directed scheduler is fair across peers and does
not starve a legacy caller of ``send_frame``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.network.registry import directed_stream as ds
from tools.network.registry.directed_pair import (
    CAP_FLEET_DIRECTED_STREAM as CAP,
    PAIR_DESTINATION_ABSENT,
    PAIR_DESTINATION_CAPABILITY,
    PAIR_DESTINATION_REPLACED,
    PAIR_SELF,
)
from tools.network.registry.relay import Tunnel, TunnelHub
from tools.network.relaykit.fleet_stream_wire import (
    build_fleet_credit,
    build_fleet_open_ok,
    parse_fleet_ctrl,
    parse_fleet_open,
)
from tools.network.relaykit.frames import (
    CTRL_CHANNEL_ID,
    FRAME_CTRL,
    FRAME_DATA,
    FRAME_OPEN,
    FRAME_STREAM_CTRL,
    decode_frame,
)
from tools.network.relaykit.stream_wire import (
    RESET_ORDERLY,
    RESET_PROTOCOL,
    RESET_ROUTE_RELEASED,
    RESET_TIMEOUT,
    RESET_TUNNEL_LOSS,
    build_ctrl_eof,
    build_ctrl_reset,
)

ORG = "org-a"
PERSONA = "ab" * 32
MACHINE_A, MACHINE_B, MACHINE_C = "11" * 32, "22" * 32, "33" * 32
OP = "0f" * 16


class FakeWS:
    """Records every frame; ``gate`` blocks sends while cleared; ``dead``
    makes every send raise, as a dropped socket does."""

    def __init__(self):
        self.frames = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.dead = False

    async def send_bytes(self, raw: bytes) -> None:
        await self.gate.wait()
        if self.dead:
            raise ConnectionError("socket gone")
        self.frames.append(decode_frame(raw))

    def of(self, frame_type, channel_id=None):
        return [
            f for f in self.frames
            if f.type == frame_type and (channel_id is None or f.channel_id == channel_id)
        ]

    def ctrls(self, channel_id):
        return [parse_fleet_ctrl(f.payload) for f in self.of(FRAME_STREAM_CTRL, channel_id)]


def tunnel(hub, machine, *, caps=(CAP,), persona=PERSONA, org=ORG):
    t = Tunnel(FakeWS(), org, persona_pub=persona, machine=machine, caps=tuple(caps))
    hub.register(t)
    return t


def args(machine=MACHINE_B, op=OP, **extra):
    return {"dst_persona_pub": PERSONA, "dst_machine": machine, "operation_id": op, **extra}


async def settle(n: int = 3):
    for _ in range(n):
        await asyncio.sleep(0)


async def opened(broker, src, dst, *, op=OP, bytes_=1000, slots=4,
                 accept_src=True, accept_dst=True):
    """Open a pair and complete both open-oks. Returns the pair and the two
    legs. Windows are what each leg OFFERS to receive."""
    reply = await broker.open(src, args(dst.machine, op))
    pair = broker.pair(reply["pair_id"])
    for leg, accept in ((pair.destination, accept_dst), (pair.source, accept_src)):
        if accept:
            leg.on_ctrl_raw(build_fleet_open_ok(nonce=leg.nonce, bytes_=bytes_, slots=slots))
    await settle(6)
    return pair


@pytest.fixture
def hub():
    return TunnelHub()


@pytest.fixture
def broker(hub):
    return ds.DirectedStreamBroker(hub, open_deadline=0.3)


# -- admission ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_sends_open_to_both_legs_and_answers_pair_identity(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    reply = await broker.open(a, args(MACHINE_B, claimed_machine_pub="44" * 32))
    pair = broker.pair(reply["pair_id"])
    assert pair is not None and reply["channel_id"] == pair.source.channel_id.hex()
    (open_a,) = a.ws.of(FRAME_OPEN)
    (open_b,) = b.ws.of(FRAME_OPEN)
    meta_a = parse_fleet_open(json.loads(open_a.payload))
    meta_b = parse_fleet_open(json.loads(open_b.payload))
    assert meta_a["role"] == "source" and meta_b["role"] == "destination"
    assert meta_a["peer_machine"] == MACHINE_B and meta_b["peer_machine"] == MACHINE_A
    assert meta_a["pair_id"] == meta_b["pair_id"] == pair.pair_id
    assert meta_a["leg_nonce"] != meta_b["leg_nonce"]
    # The hint reaches the destination only, unverified.
    assert meta_a["claimed_machine_pub"] is None
    assert meta_b["claimed_machine_pub"] == "44" * 32
    # Both legs are dispatchable through the tunnel's raw_streams seam.
    assert a.raw_streams[pair.source.channel_id] is pair.source
    assert b.raw_streams[pair.destination.channel_id] is pair.destination
    # No READY yet: nobody has accepted.
    assert not a.ws.of(FRAME_STREAM_CTRL) and not b.ws.of(FRAME_STREAM_CTRL)
    broker._close(pair, "test", RESET_ORDERLY, by=None)


@pytest.mark.asyncio
async def test_refusals_are_typed_and_allocate_nothing(hub, broker):
    a = tunnel(hub, MACHINE_A)
    b_nocap = tunnel(hub, MACHINE_B, caps=())
    with pytest.raises(ds.DirectedStreamError, match=PAIR_DESTINATION_ABSENT):
        await broker.open(a, args(MACHINE_C))
    with pytest.raises(ds.DirectedStreamError, match=PAIR_SELF):
        await broker.open(a, args(MACHINE_A))
    with pytest.raises(ds.DirectedStreamError, match=PAIR_DESTINATION_CAPABILITY):
        await broker.open(a, args(MACHINE_B))
    with pytest.raises(ds.DirectedStreamError, match=ds.PAIR_INVALID_ARGS):
        await broker.open(a, {"dst_machine": MACHINE_B})
    assert broker.snapshot()["pairs"] == 0
    assert not a.raw_streams and not a.ws.frames


@pytest.mark.asyncio
async def test_a_duplicate_operation_is_refused_loudly_in_its_own_reply(hub, broker):
    """The identity contract's fencing rule: one pair per (source,
    destination, operation_id); the loser learns synchronously."""
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    first = await broker.open(a, args(MACHINE_B, OP))
    with pytest.raises(ds.DirectedStreamError, match=ds.PAIR_OPERATION_OPEN):
        await broker.open(a, args(MACHINE_B, OP))
    # A different operation between the same machines is independent.
    second = await broker.open(a, args(MACHINE_B, "aa" * 16))
    assert second["pair_id"] != first["pair_id"]
    # The reverse direction with the same operation id is its own pair.
    reverse = await broker.open(b, args(MACHINE_A, OP))
    assert len({first["pair_id"], second["pair_id"], reverse["pair_id"]}) == 3
    assert broker.snapshot()["pairs"] == 3


@pytest.mark.asyncio
async def test_caps_are_counted_before_allocation(hub):
    broker = ds.DirectedStreamBroker(hub, pairs_per_tunnel=2, pairs_per_process=3)
    a, b, c = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B), tunnel(hub, MACHINE_C)
    await broker.open(a, args(MACHINE_B, "01" * 16))
    await broker.open(a, args(MACHINE_C, "02" * 16))
    with pytest.raises(ds.DirectedStreamError, match=ds.PAIR_CAP_TUNNEL):
        await broker.open(b, args(MACHINE_A, "03" * 16))   # a already holds 2
    await broker.open(b, args(MACHINE_C, "04" * 16))
    with pytest.raises(ds.DirectedStreamError, match=ds.PAIR_CAP_PROCESS):
        await broker.open(c, args(MACHINE_B, "05" * 16))
    assert broker.snapshot()["pairs"] == 3
    assert len(c.raw_streams) == 2                          # no leg for the refusal


# -- readiness ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_follows_both_open_oks_destination_first(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    reply = await broker.open(a, args())
    pair = broker.pair(reply["pair_id"])
    pair.source.on_ctrl_raw(build_fleet_open_ok(nonce=pair.source.nonce, bytes_=500, slots=5))
    await settle()
    assert not a.ws.of(FRAME_STREAM_CTRL) and not b.ws.of(FRAME_STREAM_CTRL)
    pair.destination.on_ctrl_raw(build_fleet_open_ok(
        nonce=pair.destination.nonce, bytes_=700, slots=7))
    await settle()
    (ready_b,) = b.ws.ctrls(pair.destination.channel_id)
    (ready_a,) = a.ws.ctrls(pair.source.channel_id)
    assert ready_a["op"] == ready_b["op"] == "fleet-ready"
    # Each leg's send window is the PEER's offer.
    assert ready_a["window"] == {"bytes": 700, "slots": 7}
    assert ready_b["window"] == {"bytes": 500, "slots": 5}
    assert ready_a["source_nonce"] == ready_b["source_nonce"] == pair.source.nonce
    assert ready_a["destination_nonce"] == pair.destination.nonce
    assert pair.state.may_send(a) and pair.state.may_send(b)


@pytest.mark.asyncio
async def test_a_stale_or_repeated_open_ok_is_a_protocol_reset(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = broker.pair((await broker.open(a, args()))["pair_id"])
    pair.source.on_ctrl_raw(build_fleet_open_ok(nonce="00" * 16, bytes_=1, slots=1))
    await settle()
    assert pair.done and pair.closed_reason == ds.CLOSED_PROTOCOL
    assert b.ws.ctrls(pair.destination.channel_id) == [{"op": "reset", "code": RESET_PROTOCOL}]
    assert broker.snapshot()["pairs"] == 0


@pytest.mark.asyncio
async def test_open_timeout_resets_both_and_releases(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = broker.pair((await broker.open(a, args()))["pair_id"])
    pair.source.on_ctrl_raw(build_fleet_open_ok(nonce=pair.source.nonce, bytes_=1, slots=1))
    await asyncio.sleep(0.5)
    assert pair.done and pair.closed_reason == ds.CLOSED_OPEN_TIMEOUT
    assert a.ws.ctrls(pair.source.channel_id) == [{"op": "reset", "code": RESET_TIMEOUT}]
    assert b.ws.ctrls(pair.destination.channel_id) == [{"op": "reset", "code": RESET_TIMEOUT}]
    assert not a.raw_streams and not b.raw_streams
    assert broker.snapshot() == {"pairs": 0, "schedulers": 0, "queued_bytes": 0,
                                 "queued_slots": 0, "outstanding_bytes": 0}


@pytest.mark.asyncio
async def test_activation_fails_when_the_destination_was_replaced(hub, broker):
    """Resolution is instantaneous, not a lease: a reconnect between OPEN
    and the second open-ok must not be paired with the OLD object."""
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = broker.pair((await broker.open(a, args()))["pair_id"])
    b2 = tunnel(hub, MACHINE_B)                     # same slot, new tunnel
    pair.destination.on_ctrl_raw(build_fleet_open_ok(nonce=pair.destination.nonce, bytes_=1, slots=1))
    pair.source.on_ctrl_raw(build_fleet_open_ok(nonce=pair.source.nonce, bytes_=1, slots=1))
    await settle(6)
    assert pair.done and pair.closed_reason == PAIR_DESTINATION_REPLACED
    assert a.ws.ctrls(pair.source.channel_id) == [{"op": "reset", "code": RESET_ROUTE_RELEASED}]
    assert not b2.raw_streams and not b2.ws.frames  # the replacement saw nothing


# -- data and credit ------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_before_ready_is_a_protocol_reset(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = broker.pair((await broker.open(a, args()))["pair_id"])
    pair.source.on_data(b"early")
    await settle()
    assert pair.done and pair.closed_reason == ds.CLOSED_PROTOCOL
    assert not b.ws.of(FRAME_DATA)


@pytest.mark.asyncio
async def test_data_forwards_one_to_one_within_the_window(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b, bytes_=100, slots=3)
    for n in (1, 63, 36):
        pair.source.on_data(b"x" * n)
    await settle(8)
    assert [len(f.payload) for f in b.ws.of(FRAME_DATA, pair.destination.channel_id)] == [1, 63, 36]
    assert pair.source.send_window.bytes == 0 and pair.source.send_window.slots == 0
    snap = broker.snapshot()
    assert snap["queued_bytes"] == 0 and snap["outstanding_bytes"] == 100
    assert not pair.done


@pytest.mark.parametrize("frames", [
    [b"x" * 101],                           # one byte over the window
    [b"x" * 30, b"x" * 30, b"x" * 30, b"x"],  # a fourth slot
    [b""],                                  # empty DATA is never legal
])
@pytest.mark.asyncio
async def test_exceeding_the_window_resets_before_a_byte_is_retained(hub, broker, frames):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b, bytes_=100, slots=3)
    for frame in frames:
        pair.source.on_data(frame)
    await settle(8)
    assert pair.done and pair.closed_reason == ds.CLOSED_PROTOCOL
    # The violating frame is never forwarded, and custody is released with
    # the pair: frames still queued at the reset die with it (the pair is
    # dead; a receiver could not credit them anyway).
    forwarded = b.ws.of(FRAME_DATA, pair.destination.channel_id)
    assert frames[-1] not in [f.payload for f in forwarded]
    assert len(forwarded) < len(frames)
    assert broker.snapshot()["queued_bytes"] == 0
    assert a.ws.ctrls(pair.source.channel_id)[-1] == {"op": "reset", "code": RESET_PROTOCOL}


@pytest.mark.asyncio
async def test_credit_is_conserved_and_must_name_an_exact_prefix(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b, bytes_=100, slots=4)
    for n in (10, 20, 30):
        pair.source.on_data(b"x" * n)
    await settle(8)
    assert pair.source.send_window.bytes == 40 and pair.source.send_window.slots == 1
    assert pair.destination.receipts.outstanding_bytes == 60

    # The receiver consumed the first two frames: the sender gets exactly that.
    pair.destination.on_ctrl_raw(build_fleet_credit(bytes_=30, slots=2))
    await settle(8)
    assert pair.source.send_window.bytes == 70 and pair.source.send_window.slots == 3
    assert a.ws.ctrls(pair.source.channel_id)[-1] == {"op": "fleet-credit", "bytes": 30, "slots": 2}
    assert pair.destination.receipts.outstanding_bytes == 30
    # Invariant, per direction: sender outstanding == queued + receiver outstanding.
    sender_outstanding = 100 - pair.source.send_window.bytes
    assert sender_outstanding == broker.snapshot()["queued_bytes"] + 30

    # A credit that does not match the remaining prefix resets the pair.
    pair.destination.on_ctrl_raw(build_fleet_credit(bytes_=29, slots=1))
    await settle(8)
    assert pair.done and pair.closed_reason == ds.CLOSED_PROTOCOL
    assert broker.snapshot()["outstanding_bytes"] == 0


@pytest.mark.asyncio
async def test_a_blocked_destination_socket_bounds_custody_and_stalls_the_sender(hub, broker):
    """The slow-consumer property at the relay: with the destination's
    socket blocked, the sender can put at most the receiver's window into
    relay custody, then has zero credit. Nothing is discarded, nothing is
    reset, and unblocking drains it in order."""
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b, bytes_=100, slots=4)
    b.ws.gate.clear()
    for _ in range(4):
        pair.source.on_data(b"y" * 25)
    await settle(8)
    snap = broker.snapshot()
    # One frame is inside send_frame (awaiting the gate), three queued.
    assert snap["queued_bytes"] + snap["outstanding_bytes"] == 100
    assert snap["queued_bytes"] <= 100 and snap["queued_slots"] <= 4
    assert pair.source.send_window.bytes == 0
    assert not pair.done
    assert not b.ws.of(FRAME_DATA)
    b.ws.gate.set()
    await settle(12)
    assert [len(f.payload) for f in b.ws.of(FRAME_DATA, pair.destination.channel_id)] == [25] * 4
    assert broker.snapshot()["queued_bytes"] == 0
    pair.destination.on_ctrl_raw(build_fleet_credit(bytes_=100, slots=4))
    await settle(8)
    assert pair.source.send_window.bytes == 100 and pair.source.send_window.slots == 4


@pytest.mark.asyncio
async def test_eof_is_forwarded_in_order_after_data(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b)
    pair.source.on_data(b"last")
    pair.source.on_ctrl_raw(build_ctrl_eof())
    await settle(8)
    frames = [f for f in b.ws.frames if f.channel_id == pair.destination.channel_id]
    kinds = [(f.type, f.payload if f.type == FRAME_DATA else parse_fleet_ctrl(f.payload)["op"])
             for f in frames[2:]]                 # frames[0:2] are OPEN and READY
    assert kinds == [(FRAME_DATA, b"last"), (FRAME_STREAM_CTRL, "eof")]
    pair.source.on_data(b"after eof")
    await settle()
    assert pair.done and pair.closed_reason == ds.CLOSED_PROTOCOL


@pytest.mark.asyncio
async def test_a_peer_reset_reaches_the_other_leg_with_its_code_and_releases(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b)
    pair.destination.on_ctrl_raw(build_ctrl_reset(RESET_ORDERLY))
    await settle(8)
    assert pair.done and pair.closed_reason == ds.CLOSED_PEER_RESET
    assert a.ws.ctrls(pair.source.channel_id)[-1] == {"op": "reset", "code": RESET_ORDERLY}
    # The leg that reset us is not told again.
    assert all(c["op"] != "reset" for c in b.ws.ctrls(pair.destination.channel_id))
    assert broker.snapshot()["pairs"] == 0 and not a.raw_streams and not b.raw_streams


@pytest.mark.asyncio
async def test_tunnel_loss_tells_the_survivor_with_code_6_and_releases_once(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b)
    pair.source.on_data(b"in flight")
    await settle(8)
    # What close_all_viewers does for every raw stream of a dying tunnel.
    b.ws.dead = True
    await pair.destination.teardown()
    await settle(8)
    assert pair.done and pair.closed_reason == ds.CLOSED_TUNNEL_LOSS
    assert a.ws.ctrls(pair.source.channel_id)[-1] == {"op": "reset", "code": RESET_TUNNEL_LOSS}
    assert broker.snapshot() == {"pairs": 0, "schedulers": 0, "queued_bytes": 0,
                                 "queued_slots": 0, "outstanding_bytes": 0}
    # A second teardown (the tunnel's own finally) is a no-op, not a second release.
    await pair.destination.teardown()
    assert broker.snapshot()["pairs"] == 0


@pytest.mark.asyncio
async def test_a_replaced_tunnel_closes_only_its_own_pair(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    old_pair = await opened(broker, a, b, op="01" * 16)
    b2 = tunnel(hub, MACHINE_B)                     # b's reconnect replaces its slot
    new_pair = await opened(broker, a, b2, op="02" * 16)
    assert not new_pair.done
    # The superseded tunnel is torn down: its pair ends, the replacement's lives.
    await old_pair.destination.teardown()
    await settle(8)
    assert old_pair.done and old_pair.closed_reason == ds.CLOSED_TUNNEL_LOSS
    assert not new_pair.done
    new_pair.source.on_data(b"still flowing")
    await settle(8)
    assert [f.payload for f in b2.ws.of(FRAME_DATA)] == [b"still flowing"]


# -- scheduling -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_scheduler_alternates_between_peers_toward_one_tunnel(hub, broker):
    a, b, c = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B), tunnel(hub, MACHINE_C)
    from_b = await opened(broker, b, a, op="01" * 16, bytes_=1000, slots=8)
    from_c = await opened(broker, c, a, op="02" * 16, bytes_=1000, slots=8)
    a.ws.gate.clear()
    for _ in range(4):
        from_b.source.on_data(b"B")
    for _ in range(4):
        from_c.source.on_data(b"C")
    await settle(4)
    a.ws.gate.set()
    await settle(30)
    order = [f.payload for f in a.ws.of(FRAME_DATA)]
    assert sorted(order) == [b"B"] * 4 + [b"C"] * 4
    # Never two consecutive frames from one peer while the other has backlog.
    assert all(x != y for x, y in zip(order, order[1:])), order


@pytest.mark.asyncio
async def test_a_legacy_send_frame_caller_is_not_starved_by_directed_backlog(hub, broker):
    a, b = tunnel(hub, MACHINE_A), tunnel(hub, MACHINE_B)
    pair = await opened(broker, a, b, bytes_=100_000, slots=64)
    b.ws.gate.clear()
    for _ in range(40):
        pair.source.on_data(b"bulk")
    await settle(4)
    legacy = asyncio.create_task(b.send_frame(FRAME_CTRL, CTRL_CHANNEL_ID, b'{"ok": true}'))
    await settle(2)
    b.ws.gate.set()
    await legacy
    await settle(60)
    kinds = [f.type for f in b.ws.frames]
    assert kinds.count(FRAME_DATA) == 40
    # The control reply left well before the directed backlog finished.
    assert kinds.index(FRAME_CTRL) < kinds.index(FRAME_DATA) + 3
