"""Relay-side broker for fleet-directed-stream/1 (auto-fh2nv).

Joins two AUTHENTICATED outbound tunnels of one organization at this
relay process — the single-POP software switchboard of the operator's
scope ruling — and forwards frames between them under bounded,
transport-owned credit.

What this consumes, unchanged:

* ``directed_pair.resolve_directed_pair`` for "which live tunnel is the
  destination", with its typed refusals, and ``directed_pair.DirectedPair``
  for the offered/active/terminal lifecycle fenced on pinned tunnel objects.
* ``Tunnel.raw_streams`` as the dispatch seam: a leg registers under its
  channel id and the tunnel receive loop hands it DATA / STREAM_CTRL / CLOSE
  exactly as it does for a public ``tls-stream/1`` stream. The receive loop
  is not modified.
* ``Tunnel.send_frame`` as the ONLY writer. Every legacy caller keeps its
  path; directed DATA joins the same lock through a per-tunnel scheduler
  that sends one frame per turn.

Custody is conserved per direction. A leg's send window IS the peer's
offered receive window, forwarded verbatim in READY; the relay validates
every DATA against that window before it retains a byte, forwards each
frame one-to-one, keeps a FIFO receipt per forwarded frame, and returns
credit to the sender only when the receiver's ``fleet-credit`` names an
exact prefix of those receipts. Dequeuing is not a refund. So, per
direction::

    sender_outstanding == relay_queued + receiver_outstanding <= receiver_window

and the relay's retained payload for a pair is bounded by the two offered
windows, before counting the parser's single materialized frame.

A stalled consumer is therefore backpressure, never a discard: the sender
reaches zero credit and waits. There is no idle reset on a directed pair
by design; a dead PEER is the tunnel's own liveness problem (ping/pong),
and tunnel loss tears down every pair the tunnel is a leg of.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import deque
from typing import Dict, Optional, TYPE_CHECKING

from tools.network.relaykit.frames import (
    FRAME_DATA,
    FRAME_OPEN,
    FRAME_STREAM_CTRL,
    new_channel_id,
)
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM,
    FLEET_OPEN_DEADLINE_S,
    FLEET_PAIRS_PER_PROCESS,
    FLEET_PAIRS_PER_TUNNEL,
    FleetWindow,
    ROLE_DESTINATION,
    ROLE_SOURCE,
    ReceiptLedger,
    build_fleet_open,
    build_fleet_ready,
    build_fleet_credit,
    parse_fleet_ctrl,
    parse_fleet_open_args,
)
from tools.network.relaykit.stream_wire import (
    RESET_ORDERLY,
    RESET_PROTOCOL,
    RESET_ROUTE_RELEASED,
    RESET_TIMEOUT,
    RESET_TUNNEL_LOSS,
    STREAM_MAX_DATA,
    StreamProtocolError,
    build_ctrl_eof,
    build_ctrl_reset,
)

from .directed_pair import (
    DirectedPair,
    PAIR_OK,
    resolve_directed_pair,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .relay import Tunnel, TunnelHub

logger = logging.getLogger("registry.directed")

#: Admission refusals this broker adds to the resolver's.
PAIR_CAP_TUNNEL = "pair-cap-tunnel"
PAIR_CAP_PROCESS = "pair-cap-process"
PAIR_INVALID_ARGS = "invalid-fleet-open-args"
PAIR_OPEN_DELIVERY_FAILED = "open-delivery-failed"
#: One pair per (source tunnel, destination tunnel, operation_id): the
#: deterministic arbitration the identity contract's generation fencing
#: relies on. The first admitted open wins; a later duplicate is refused in
#: its own control reply, so the loser is told without a round trip.
PAIR_OPERATION_OPEN = "operation-already-open"

#: Terminal reasons recorded on the pair (operator readout / tests).
CLOSED_OPEN_TIMEOUT = "open-timeout"
CLOSED_PROTOCOL = "protocol"
CLOSED_ORDERLY = "orderly"
CLOSED_TUNNEL_LOSS = "tunnel-loss"
CLOSED_READY_DELIVERY_FAILED = "ready-delivery-failed"
CLOSED_PEER_RESET = "peer-reset"


class DirectedStreamError(Exception):
    """A typed refusal of a ``fleet-open`` control op."""


class _Leg:
    """One end of a pair on one pinned tunnel. Registered in that tunnel's
    ``raw_streams`` under its channel id; the receive loop calls the
    ``on_*`` entry points, which never block."""

    #: The raw_streams contract: lease removal resets streams by
    #: reservation; a directed leg has none and is never matched that way.
    reservation = None

    def __init__(self, pair: "_Pair", tunnel: "Tunnel", role: str):
        self.pair = pair
        self.tunnel = tunnel
        self.role = role
        self.channel_id = new_channel_id()
        self.nonce = secrets.token_hex(16)
        #: What THIS leg offered to receive (its open-ok); bounds the relay's
        #: custody for frames travelling toward it.
        self.offered_bytes = 0
        self.offered_slots = 0
        self.accepted = False
        self.ready_sent = False
        #: The relay's view of what this leg may still send: the PEER's
        #: offer, replenished by the peer's credits.
        self.send_window = FleetWindow()
        #: Frames forwarded TO this leg and not yet credited back by it.
        self.receipts = ReceiptLedger()
        #: Items queued toward this leg (DATA and in-order controls).
        self.outbound: deque = deque()
        self.queued_bytes = 0
        self.queued_slots = 0
        self.sent_eof = False   # this leg's peer half-closed toward it
        self.recv_eof = False   # this leg half-closed

    @property
    def peer(self) -> "_Leg":
        return self.pair.other(self)

    # -- entry points from the tunnel receive loop (never block) ------------

    def on_data(self, payload: bytes) -> None:
        pair = self.pair
        if pair.done:
            return
        if not pair.state.may_send(self.tunnel) or self.recv_eof:
            pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
            return
        if not self.send_window.can_send(len(payload)):
            pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
            return
        self.send_window.consume(len(payload))
        peer = self.peer
        # Defense in depth: the credit math already bounds this, so reaching
        # the receiver's offer here means a broker bug, not a peer fault, and
        # the honest reaction is still to stop rather than retain.
        if (
            peer.queued_bytes + len(payload) > peer.offered_bytes
            or peer.queued_slots + 1 > peer.offered_slots
        ):
            logger.error("directed custody bound exceeded pair=%s", pair.pair_id[:8])
            pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
            return
        peer.queued_bytes += len(payload)
        peer.queued_slots += 1
        peer.outbound.append(("data", payload))
        pair.broker._wake(peer.tunnel)

    def on_ctrl_raw(self, payload: bytes) -> None:
        pair = self.pair
        if pair.done:
            return
        try:
            msg = parse_fleet_ctrl(payload)
        except StreamProtocolError:
            pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
            return
        op = msg["op"]
        if op == "fleet-open-ok":
            if self.accepted or msg["nonce"] != self.nonce:
                pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
                return
            self.offered_bytes = msg["window"]["bytes"]
            self.offered_slots = msg["window"]["slots"]
            self.accepted = True
            pair.state.leg_accepted(self.tunnel)
            pair.accepted_event.set()
        elif op == "fleet-credit":
            if not self.accepted:
                pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
                return
            try:
                self.receipts.credited(msg["bytes"], msg["slots"])
            except StreamProtocolError:
                pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
                return
            peer = self.peer
            peer.send_window.grant(msg["bytes"], msg["slots"])
            peer.outbound.append(("ctrl", build_fleet_credit(
                bytes_=msg["bytes"], slots=msg["slots"])))
            pair.broker._wake(peer.tunnel)
        elif op == "eof":
            if self.recv_eof or not pair.state.may_send(self.tunnel):
                pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)
                return
            self.recv_eof = True
            peer = self.peer
            peer.outbound.append(("ctrl", build_ctrl_eof()))
            pair.broker._wake(peer.tunnel)
        elif op == "reset":
            pair.broker._close(pair, CLOSED_PEER_RESET, msg["code"], by=self)
        else:  # fleet-ready is relay → leg only
            pair.broker._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=self)

    def on_close(self) -> None:
        self.pair.broker._close(self.pair, CLOSED_ORDERLY, RESET_ORDERLY, by=self)

    def signal_reset(self, code: int) -> None:
        """Relay-initiated reset (the raw_streams contract)."""
        self.pair.broker._close(self.pair, CLOSED_ORDERLY, code, by=None)

    async def teardown(self) -> None:
        """This leg's tunnel is going away."""
        self.pair.broker._close(
            self.pair, CLOSED_TUNNEL_LOSS, RESET_TUNNEL_LOSS, by=self, dead=self,
        )


class _Pair:
    def __init__(self, broker: "DirectedStreamBroker", source: "Tunnel",
                 destination: "Tunnel", operation_id: str,
                 claimed_machine_pub: Optional[str]):
        self.broker = broker
        self.pair_id = secrets.token_hex(16)
        self.operation_id = operation_id
        self.claimed_machine_pub = claimed_machine_pub
        self.state = DirectedPair(source, destination)
        self.source = _Leg(self, source, ROLE_SOURCE)
        self.destination = _Leg(self, destination, ROLE_DESTINATION)
        self.accepted_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.done = False
        self.closed_reason: Optional[str] = None

    def other(self, leg: _Leg) -> _Leg:
        return self.destination if leg is self.source else self.source

    @property
    def legs(self) -> tuple[_Leg, _Leg]:
        return (self.source, self.destination)


class _TunnelScheduler:
    """Directed-only sender for one tunnel: round-robin over peer tunnels,
    then over pairs with that peer, one item per turn, every item through
    the tunnel's existing ``send_frame``. Legacy callers are untouched and
    keep their own ordering; this task only competes for the same lock."""

    def __init__(self, broker: "DirectedStreamBroker", tunnel: "Tunnel"):
        self.broker = broker
        self.tunnel = tunnel
        self.wake = asyncio.Event()
        self.task = asyncio.create_task(self._run())
        self._peer_cursor = 0
        self._pair_cursor: Dict[int, int] = {}

    def _pick(self) -> Optional[_Leg]:
        """The next leg on this tunnel with something queued, fair across
        peers first and pairs within a peer second."""
        by_peer: Dict[int, list] = {}
        for pair in self.broker._pairs_on(self.tunnel):
            leg = pair.source if pair.source.tunnel is self.tunnel else pair.destination
            if leg.outbound:
                by_peer.setdefault(id(leg.peer.tunnel), []).append(leg)
        if not by_peer:
            return None
        peers = sorted(by_peer)
        self._peer_cursor %= len(peers)
        peer_key = peers[self._peer_cursor]
        self._peer_cursor += 1
        legs = sorted(by_peer[peer_key], key=lambda l: l.pair.pair_id)
        cursor = self._pair_cursor.get(peer_key, 0) % len(legs)
        self._pair_cursor[peer_key] = cursor + 1
        return legs[cursor]

    async def _run(self) -> None:
        while True:
            leg = self._pick()
            if leg is None:
                self.wake.clear()
                await self.wake.wait()
                continue
            kind, payload = leg.outbound.popleft()
            if kind == "data":
                leg.queued_bytes -= len(payload)
                leg.queued_slots -= 1
                # Receipt BEFORE the send: the receiver's credit can only
                # follow its receipt of this frame, but recording after the
                # await would race it.
                leg.receipts.forwarded(len(payload))
                frame_type = FRAME_DATA
            else:
                frame_type = FRAME_STREAM_CTRL
            try:
                await self.tunnel.send_frame(frame_type, leg.channel_id, payload)
            except Exception:
                self.broker._close(
                    leg.pair, CLOSED_TUNNEL_LOSS, RESET_TUNNEL_LOSS,
                    by=leg, dead=leg,
                )


class DirectedStreamBroker:
    """All directed pairs of one relay process."""

    def __init__(self, hub: "TunnelHub", *,
                 pairs_per_tunnel: int = FLEET_PAIRS_PER_TUNNEL,
                 pairs_per_process: int = FLEET_PAIRS_PER_PROCESS,
                 open_deadline: float = FLEET_OPEN_DEADLINE_S):
        self.hub = hub
        self.pairs_per_tunnel = int(pairs_per_tunnel)
        self.pairs_per_process = int(pairs_per_process)
        self.open_deadline = float(open_deadline)
        self._pairs: Dict[str, _Pair] = {}
        self._schedulers: Dict[int, _TunnelScheduler] = {}
        self._background: set = set()

    # -- readouts -----------------------------------------------------------

    def _pairs_on(self, tunnel: "Tunnel") -> list:
        return [
            pair for pair in self._pairs.values()
            if pair.source.tunnel is tunnel or pair.destination.tunnel is tunnel
        ]

    def snapshot(self) -> dict:
        """Operator/test readout: counts and retained custody, never payload."""
        return {
            "pairs": len(self._pairs),
            "schedulers": len(self._schedulers),
            "queued_bytes": sum(
                leg.queued_bytes for pair in self._pairs.values() for leg in pair.legs
            ),
            "queued_slots": sum(
                leg.queued_slots for pair in self._pairs.values() for leg in pair.legs
            ),
            "outstanding_bytes": sum(
                leg.receipts.outstanding_bytes
                for pair in self._pairs.values() for leg in pair.legs
            ),
        }

    def pair(self, pair_id: str) -> Optional[_Pair]:
        return self._pairs.get(pair_id)

    # -- admission ----------------------------------------------------------

    async def open(self, source: "Tunnel", args: object) -> dict:
        """Handle one ``fleet-open`` control op from *source*.

        Returns the control reply body on admission; raises
        :class:`DirectedStreamError` with the typed reason otherwise. The
        reply means "both legs have been sent OPEN", not readiness: the
        source's endpoint learns readiness from its own READY.
        """
        try:
            request = parse_fleet_open_args(args)
        except StreamProtocolError as exc:
            raise DirectedStreamError(f"{PAIR_INVALID_ARGS}: {exc}") from exc
        destination, reason = resolve_directed_pair(
            self.hub, source, request["dst_persona_pub"], request["dst_machine"],
        )
        if reason != PAIR_OK:
            raise DirectedStreamError(reason)
        for existing in self._pairs.values():
            if (
                existing.source.tunnel is source
                and existing.destination.tunnel is destination
                and existing.operation_id == request["operation_id"]
            ):
                raise DirectedStreamError(PAIR_OPERATION_OPEN)
        # Admission BEFORE allocation: count what this pair would add.
        if len(self._pairs) >= self.pairs_per_process:
            raise DirectedStreamError(PAIR_CAP_PROCESS)
        for tunnel in (source, destination):
            if len(self._pairs_on(tunnel)) >= self.pairs_per_tunnel:
                raise DirectedStreamError(PAIR_CAP_TUNNEL)

        pair = _Pair(
            self, source, destination, request["operation_id"],
            request["claimed_machine_pub"],
        )
        self._pairs[pair.pair_id] = pair
        for leg in pair.legs:
            leg.tunnel.raw_streams[leg.channel_id] = leg
        try:
            for leg in pair.legs:
                peer = leg.peer
                await leg.tunnel.send_frame(FRAME_OPEN, leg.channel_id, build_fleet_open(
                    pair_id=pair.pair_id, leg_nonce=leg.nonce, role=leg.role,
                    operation_id=pair.operation_id,
                    peer_persona_pub=peer.tunnel.persona_pub,
                    peer_machine=peer.tunnel.machine,
                    # The hint travels to the DESTINATION only: it is the
                    # source's claim about itself.
                    claimed_machine_pub=(
                        pair.claimed_machine_pub
                        if leg.role == ROLE_DESTINATION else None
                    ),
                ))
        except Exception as exc:
            self._close(pair, CLOSED_TUNNEL_LOSS, RESET_TUNNEL_LOSS, by=None)
            raise DirectedStreamError(PAIR_OPEN_DELIVERY_FAILED) from exc
        pair.task = asyncio.create_task(self._run_pair(pair))
        return {
            "pair_id": pair.pair_id,
            "channel_id": pair.source.channel_id.hex(),
            "leg_nonce": pair.source.nonce,
        }

    async def _run_pair(self, pair: _Pair) -> None:
        """Wait for both open-oks, re-validate, then READY both legs."""
        try:
            try:
                deadline = asyncio.get_event_loop().time() + self.open_deadline
                while not (pair.source.accepted and pair.destination.accepted):
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    pair.accepted_event.clear()
                    await asyncio.wait_for(pair.accepted_event.wait(), remaining)
            except asyncio.TimeoutError:
                self._close(pair, CLOSED_OPEN_TIMEOUT, RESET_TIMEOUT, by=None)
                return
            if pair.done:
                return
            reason = pair.state.activate(self.hub)
            if reason != PAIR_OK:
                self._close(pair, reason, RESET_ROUTE_RELEASED, by=None)
                return
            # Each leg's send window is the PEER's offer.
            pair.source.send_window = FleetWindow(
                pair.destination.offered_bytes, pair.destination.offered_slots)
            pair.destination.send_window = FleetWindow(
                pair.source.offered_bytes, pair.source.offered_slots)
            # Destination first: the responder is ready before the initiator
            # is told it may send its hello. Either order is safe (receiving
            # is legal from open-ok), this one just avoids the common wait.
            for leg in (pair.destination, pair.source):
                if pair.done:
                    return
                peer = leg.peer
                try:
                    await leg.tunnel.send_frame(
                        FRAME_STREAM_CTRL, leg.channel_id, build_fleet_ready(
                            pair_id=pair.pair_id,
                            source_nonce=pair.source.nonce,
                            destination_nonce=pair.destination.nonce,
                            bytes_=peer.offered_bytes, slots=peer.offered_slots,
                        ),
                    )
                except Exception:
                    self._close(
                        pair, CLOSED_READY_DELIVERY_FAILED, RESET_TUNNEL_LOSS,
                        by=None, dead=leg,
                    )
                    return
                leg.ready_sent = True
                pair.state.ready_enqueued(leg.tunnel)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("directed pair setup failed pair=%s", pair.pair_id[:8])
            self._close(pair, CLOSED_PROTOCOL, RESET_PROTOCOL, by=None)

    # -- teardown -----------------------------------------------------------

    def _close(self, pair: _Pair, reason: str, code: int, *,
               by: Optional[_Leg], dead: Optional[_Leg] = None) -> None:
        """Terminate *pair* once. *by* is the leg whose event caused it
        (fenced by pinned tunnel identity through DirectedPair.close);
        *dead* is a leg whose tunnel is known gone, so no reset is sent
        there. Resources are released exactly once, whatever ended the
        pair."""
        pair.state.close(by.tunnel if by is not None else None, reason)
        if not pair.state.claim_cleanup():
            return
        pair.done = True
        pair.closed_reason = reason
        pair.accepted_event.set()
        self._pairs.pop(pair.pair_id, None)
        if pair.task is not None and pair.task is not asyncio.current_task():
            pair.task.cancel()
        for leg in pair.legs:
            if leg.tunnel.raw_streams.get(leg.channel_id) is leg:
                del leg.tunnel.raw_streams[leg.channel_id]
            leg.outbound.clear()
            leg.queued_bytes = 0
            leg.queued_slots = 0
            if leg is dead:
                continue
            # The leg that reset us already knows; everyone else is told
            # with the code we were given. Best effort: its tunnel may be
            # dying too.
            if leg is by and reason == CLOSED_PEER_RESET:
                continue
            self._spawn(self._send_reset(leg, code))
        self._reap_scheduler(pair.source.tunnel)
        self._reap_scheduler(pair.destination.tunnel)

    async def _send_reset(self, leg: _Leg, code: int) -> None:
        with contextlib.suppress(Exception):
            await leg.tunnel.send_frame(
                FRAME_STREAM_CTRL, leg.channel_id, build_ctrl_reset(code))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- scheduling ---------------------------------------------------------

    def _wake(self, tunnel: "Tunnel") -> None:
        scheduler = self._schedulers.get(id(tunnel))
        if scheduler is None:
            scheduler = _TunnelScheduler(self, tunnel)
            self._schedulers[id(tunnel)] = scheduler
        scheduler.wake.set()

    def _reap_scheduler(self, tunnel: "Tunnel") -> None:
        if self._pairs_on(tunnel):
            return
        scheduler = self._schedulers.pop(id(tunnel), None)
        if scheduler is not None and scheduler.task is not asyncio.current_task():
            scheduler.task.cancel()

    async def close_all(self) -> None:
        """Process shutdown: terminate every pair and scheduler."""
        for pair in list(self._pairs.values()):
            self._close(pair, CLOSED_TUNNEL_LOSS, RESET_TUNNEL_LOSS, by=None)
        for scheduler in list(self._schedulers.values()):
            scheduler.task.cancel()
        self._schedulers.clear()
        tasks = list(self._background)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
