"""The connector-side fleet-stream endpoint, with a loopback stand-in for
the relay: two adapters whose frames are delivered to each other's channel
exactly as the relay forwards them (one-to-one, in order, credits passed
through) so the ENDPOINT semantics are what is under test — open flow and
session binding, message framing across frames, credit only after
delivery, stall at zero credit, half-close, reset codes, tunnel loss."""

from __future__ import annotations

import asyncio
import json
import secrets

import pytest

from tools.network.relaykit import fleet_stream as fs
from tools.network.relaykit.fleet_stream_wire import (
    FLEET_STREAM_MAX_MESSAGE,
    build_fleet_open,
    build_fleet_ready,
    parse_fleet_ctrl,
)
from tools.network.relaykit.frames import FRAME_DATA, FRAME_STREAM_CTRL
from tools.network.relaykit.stream_wire import (
    RESET_ORDERLY,
    RESET_PROTOCOL,
    RESET_ROUTE_RELEASED,
    RESET_TUNNEL_LOSS,
    STREAM_MAX_DATA,
    build_ctrl_reset,
)

PERSONA = "ab" * 32
MACHINE_A, MACHINE_B = "11" * 32, "22" * 32


class LoopbackRelay:
    """Two adapters joined the way the broker joins two legs. Frames an
    adapter sends on a paired channel are delivered to the peer adapter's
    channel; open-oks trigger READY to both with the peer's window."""

    def __init__(self):
        self.adapters: dict[str, fs.FleetStreamAdapter] = {}
        self.pairs: dict[bytes, dict] = {}     # channel_id -> leg record
        self.sent: list = []                    # (side, type, channel, payload)
        self.refuse_open = None                 # typed reason to refuse with

    def attach(self, side: str, **kwargs) -> fs.FleetStreamAdapter:
        async def send_frame(frame_type, channel_id, payload=b""):
            self.sent.append((side, frame_type, channel_id, payload))
            leg = self.pairs.get(channel_id)
            if leg is None:
                return
            peer = leg["peer"]
            if frame_type == FRAME_STREAM_CTRL:
                msg = parse_fleet_ctrl(payload)
                if msg["op"] == "fleet-open-ok":
                    leg["window"] = msg["window"]
                    leg["accepted"] = True
                    if peer["accepted"]:
                        for l in (peer, leg):
                            self.adapters[l["side"]].dispatch_ctrl(
                                l["channel"], build_fleet_ready(
                                    pair_id=l["pair_id"],
                                    source_nonce=l["source_nonce"],
                                    destination_nonce=l["destination_nonce"],
                                    bytes_=l["peer"]["window"]["bytes"],
                                    slots=l["peer"]["window"]["slots"],
                                ))
                    return
                self.adapters[peer["side"]].dispatch_ctrl(peer["channel"], payload)
            elif frame_type == FRAME_DATA:
                self.adapters[peer["side"]].dispatch_data(peer["channel"], payload)

        async def control(op, args, timeout=10.0):
            assert op == "fleet-open"
            if self.refuse_open:
                return {"ok": False, "error": self.refuse_open}
            other = "B" if side == "A" else "A"
            return {"ok": True, **self.pair(side, other, args["operation_id"],
                                            args.get("claimed_machine_pub"))}

        adapter = fs.FleetStreamAdapter(send_frame, control, **kwargs)
        self.adapters[side] = adapter
        return adapter

    def pair(self, source_side, dest_side, operation_id, claimed=None):
        pair_id = secrets.token_hex(16)
        src = {"side": source_side, "channel": secrets.token_bytes(16),
               "nonce": secrets.token_hex(16), "accepted": False, "window": None}
        dst = {"side": dest_side, "channel": secrets.token_bytes(16),
               "nonce": secrets.token_hex(16), "accepted": False, "window": None}
        for leg, peer, role in ((src, dst, "source"), (dst, src, "destination")):
            leg.update(peer=peer, pair_id=pair_id, source_nonce=src["nonce"],
                       destination_nonce=dst["nonce"])
            self.pairs[leg["channel"]] = leg
            meta = json.loads(build_fleet_open(
                pair_id=pair_id, leg_nonce=leg["nonce"], role=role,
                operation_id=operation_id, peer_persona_pub=PERSONA,
                peer_machine=MACHINE_B if role == "source" else MACHINE_A,
                claimed_machine_pub=claimed if role == "destination" else None,
            ))
            assert self.adapters[leg["side"]].dispatch_open(leg["channel"], meta)
        return {"pair_id": pair_id, "channel_id": src["channel"].hex(),
                "leg_nonce": src["nonce"]}


async def accept_all(endpoint):
    return True


async def connected(relay, **kwargs):
    a = relay.attach("A", window_bytes=kwargs.pop("a_bytes", 1000),
                     window_slots=kwargs.pop("a_slots", 8))
    b = relay.attach("B", on_offer=accept_all,
                     window_bytes=kwargs.pop("b_bytes", 1000),
                     window_slots=kwargs.pop("b_slots", 8))
    src = await a.open(PERSONA, MACHINE_B, claimed_machine_pub="33" * 32, **kwargs)
    dst = await asyncio.wait_for(b.accepted.get(), 1)
    return a, b, src, dst


@pytest.mark.asyncio
async def test_open_returns_at_ready_with_one_session_bound_on_both_ends():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay)
    assert src.role == "source" and dst.role == "destination"
    assert src.pair_id == dst.pair_id
    assert src.session == dst.session
    assert src.session == fs.fleet_session(src.pair_id, src.nonce, dst.nonce)
    assert dst.claimed_machine_pub == "33" * 32 and src.claimed_machine_pub is None
    assert dst.peer_machine == MACHINE_A and src.peer_machine == MACHINE_B
    # Each end's send window is the PEER's offer.
    assert (src.send_window.bytes, src.send_window.slots) == (1000, 8)


@pytest.mark.asyncio
async def test_messages_larger_than_a_frame_are_split_and_reassembled():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay, a_bytes=512 * 1024, a_slots=64,
                                     b_bytes=512 * 1024, b_slots=64)
    payload = bytes(range(256)) * 600          # 150 KiB, > 2 frames
    await src.send(payload)
    await src.send(b"second")
    assert await dst.recv() == payload
    assert await dst.recv() == b"second"
    data_frames = [p for s, t, c, p in relay.sent if s == "A" and t == FRAME_DATA]
    assert len(data_frames) == 4 and all(len(p) <= STREAM_MAX_DATA for p in data_frames)
    await dst.send(b"reply")
    assert await src.recv() == b"reply"


@pytest.mark.asyncio
async def test_credit_is_issued_only_after_delivery_and_a_stalled_reader_stops_the_sender():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay, b_bytes=200, b_slots=2)
    await src.send(b"x" * 90)                       # frame 1: 94 bytes
    await src.send(b"y" * 90)                       # frame 2: 94 bytes
    assert (src.send_window.bytes, src.send_window.slots) == (12, 0)
    third = asyncio.create_task(src.send(b"z"))
    await asyncio.sleep(0.05)
    assert not third.done()                         # zero slots: waits, no reset
    credits = [p for s, t, c, p in relay.sent if s == "B" and t == FRAME_STREAM_CTRL
               and parse_fleet_ctrl(p)["op"] == "fleet-credit"]
    assert credits == []                            # nothing delivered yet
    assert await dst.recv() == b"x" * 90
    credits = [parse_fleet_ctrl(p) for s, t, c, p in relay.sent if s == "B"
               and t == FRAME_STREAM_CTRL and parse_fleet_ctrl(p)["op"] == "fleet-credit"]
    assert credits == [{"op": "fleet-credit", "bytes": 94, "slots": 1}]
    await asyncio.wait_for(third, 1)                # the credit released it
    assert await dst.recv() == b"y" * 90
    assert await dst.recv() == b"z"
    assert not src.closed.is_set() and not dst.closed.is_set()


@pytest.mark.asyncio
async def test_half_close_ends_the_peer_recv_with_none():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay)
    await src.send(b"last")
    await src.half_close()
    assert await dst.recv() == b"last"
    assert await dst.recv() is None
    await dst.send(b"still open the other way")
    assert await src.recv() == b"still open the other way"
    with pytest.raises(fs.FleetStreamClosed):
        await src.send(b"after eof")


@pytest.mark.asyncio
async def test_a_reset_surfaces_its_code_and_pair_id_to_the_other_end():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay)
    waiting = asyncio.create_task(dst.recv())
    await src.close()
    with pytest.raises(fs.FleetStreamClosed) as excinfo:
        await asyncio.wait_for(waiting, 1)
    assert excinfo.value.code == RESET_ORDERLY and excinfo.value.pair_id == src.pair_id
    assert dst.closed.is_set() and dst.reset_code == RESET_ORDERLY
    with pytest.raises(fs.FleetStreamClosed):
        await dst.send(b"x")


@pytest.mark.asyncio
async def test_a_refused_open_reports_the_relay_reason():
    relay = LoopbackRelay()
    relay.refuse_open = "destination-slot-absent"
    a = relay.attach("A")
    with pytest.raises(fs.FleetStreamRefused) as excinfo:
        await a.open(PERSONA, MACHINE_B)
    assert excinfo.value.reason == "destination-slot-absent"
    assert not isinstance(excinfo.value, fs.FleetStreamClosed)   # stand down, not retry
    assert not a._pending_opens


@pytest.mark.asyncio
async def test_a_declined_offer_is_reset_route_released_and_never_accepted():
    relay = LoopbackRelay()

    async def decline(endpoint):
        return False

    a = relay.attach("A")
    b = relay.attach("B", on_offer=decline)
    opening = asyncio.create_task(a.open(PERSONA, MACHINE_B, timeout=0.3))
    await asyncio.sleep(0.05)
    resets = [parse_fleet_ctrl(p) for s, t, c, p in relay.sent if s == "B" and t == FRAME_STREAM_CTRL]
    assert resets == [{"op": "reset", "code": RESET_ROUTE_RELEASED}]
    assert b.accepted.empty()
    with pytest.raises(ConnectionError):
        await opening


@pytest.mark.asyncio
async def test_an_unsolicited_source_open_is_refused():
    """Only an open THIS connector asked for may become a source leg."""
    relay = LoopbackRelay()
    a = relay.attach("A")
    channel = secrets.token_bytes(16)
    meta = json.loads(build_fleet_open(
        pair_id="00" * 16, leg_nonce="01" * 16, role="source", operation_id="02" * 16,
        peer_persona_pub=PERSONA, peer_machine=MACHINE_B, claimed_machine_pub=None))
    assert a.dispatch_open(channel, meta)
    await asyncio.sleep(0.02)
    assert relay.sent[-1][1:3] == (FRAME_STREAM_CTRL, channel)
    assert parse_fleet_ctrl(relay.sent[-1][3]) == {"op": "reset", "code": RESET_ROUTE_RELEASED}


@pytest.mark.asyncio
async def test_data_beyond_the_offered_window_is_a_protocol_reset():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay, b_bytes=100, b_slots=1)
    # Bypass the sender's own window: a relay that forwards more than the
    # receiver offered is misbehaving, and the receiver must not retain it.
    b.dispatch_data(dst.channel_id, b"q" * 60)
    b.dispatch_data(dst.channel_id, b"q" * 60)
    await asyncio.sleep(0.02)
    assert dst.closed.is_set() and dst.reset_code == RESET_PROTOCOL
    with pytest.raises(fs.FleetStreamClosed):
        await dst.recv()


@pytest.mark.asyncio
async def test_an_oversized_message_is_refused_by_the_receiver():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay, b_bytes=1 << 20, b_slots=256)
    b.dispatch_data(dst.channel_id, (FLEET_STREAM_MAX_MESSAGE + 1).to_bytes(4, "big"))
    with pytest.raises(fs.FleetStreamClosed) as excinfo:
        await dst.recv()
    assert excinfo.value.code == RESET_PROTOCOL


@pytest.mark.asyncio
async def test_tunnel_loss_ends_every_endpoint_with_code_6_and_fails_pending_opens():
    relay = LoopbackRelay()
    a, b, src, dst = await connected(relay)
    waiting = asyncio.create_task(src.recv())
    relay.refuse_open = None
    # A pending open that will never see READY.
    relay.adapters["B"]._on_offer = None            # the peer stops accepting
    pending = asyncio.create_task(a.open(PERSONA, MACHINE_B, timeout=5))
    await asyncio.sleep(0.02)
    await a.shutdown()
    with pytest.raises(fs.FleetStreamClosed) as excinfo:
        await waiting
    assert excinfo.value.code == RESET_TUNNEL_LOSS
    with pytest.raises(ConnectionError):
        await pending
    assert a.closed and not a._pending_opens and not a._endpoints


# -- READY skew, pinned rather than reasoned -----------------------------------


class ManualRelay:
    """A relay driven by hand: the test decides when READY (or a reset)
    reaches the source leg, so the window between open-ok and READY can be
    observed directly."""

    def __init__(self):
        self.sent = []
        self.channel = secrets.token_bytes(16)
        self.pair_id = secrets.token_hex(16)
        self.nonce = secrets.token_hex(16)
        self.adapter = None

    async def send_frame(self, frame_type, channel_id, payload=b""):
        self.sent.append((frame_type, channel_id, payload))

    async def control(self, op, args, timeout=10.0):
        meta = json.loads(build_fleet_open(
            pair_id=self.pair_id, leg_nonce=self.nonce, role="source",
            operation_id=args["operation_id"], peer_persona_pub=PERSONA,
            peer_machine=MACHINE_B, claimed_machine_pub=None))
        assert self.adapter.dispatch_open(self.channel, meta)
        return {"ok": True, "pair_id": self.pair_id}

    def ready(self):
        self.adapter.dispatch_ctrl(self.channel, build_fleet_ready(
            pair_id=self.pair_id, source_nonce=self.nonce,
            destination_nonce="00" * 16, bytes_=1000, slots=8))

    def controls(self):
        return [parse_fleet_ctrl(p) for t, c, p in self.sent if t == FRAME_STREAM_CTRL]


@pytest.mark.asyncio
async def test_data_before_ready_is_buffered_and_delivered_after_ready():
    relay = ManualRelay()
    relay.adapter = fs.FleetStreamAdapter(relay.send_frame, relay.control)
    opening = asyncio.create_task(relay.adapter.open(PERSONA, MACHINE_B, timeout=2))
    await asyncio.sleep(0.02)
    assert [c["op"] for c in relay.controls()] == ["fleet-open-ok"]
    # The destination was told READY first and sent: the source has only
    # its open-ok out, and must accept within the window it offered.
    early = fs.encode_message(b"first, before ready")
    assert relay.adapter.dispatch_data(relay.channel, early)
    await asyncio.sleep(0.02)
    assert not opening.done()                       # nothing surfaced yet
    assert all(c["op"] != "reset" for c in relay.controls())
    assert all(c["op"] != "fleet-credit" for c in relay.controls())  # not consumed
    relay.ready()
    endpoint = await asyncio.wait_for(opening, 1)
    assert await endpoint.recv() == b"first, before ready"
    assert relay.controls()[-1] == {"op": "fleet-credit", "bytes": len(early), "slots": 1}


@pytest.mark.asyncio
async def test_data_before_a_failed_ready_dies_with_the_pair_and_is_never_surfaced():
    relay = ManualRelay()
    relay.adapter = fs.FleetStreamAdapter(relay.send_frame, relay.control)
    opening = asyncio.create_task(relay.adapter.open(PERSONA, MACHINE_B, timeout=2))
    await asyncio.sleep(0.02)
    assert relay.adapter.dispatch_data(relay.channel, fs.encode_message(b"orphan"))
    relay.adapter.dispatch_ctrl(relay.channel, build_ctrl_reset(RESET_TUNNEL_LOSS))
    with pytest.raises(fs.FleetStreamClosed) as excinfo:
        await asyncio.wait_for(opening, 1)
    assert excinfo.value.code == RESET_TUNNEL_LOSS
    assert excinfo.value.pair_id == relay.pair_id
    assert not relay.adapter._endpoints and not relay.adapter._pending_opens
