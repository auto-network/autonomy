"""member-message/1: one member's sealed message to a co-member's session.

Connector-side half (graph://bace7454-c77 "The seam", bead auto-qrmlg.9).
A member-message pair is a relay-brokered directed pair of its own
capability (part 1, relay); inside it the ORG hello runs — the persona
certificate over the serving machine key plus the membership proof against
the adopted checkpoint, mutual, exactly what the organization sync
responder serves (fleet_relay_carrier) — and then the pair carries exactly
one request and its reply as JSON records, the same shape session-control/1
uses::

    request  {"v": 1, "op": "send", "body": {...}}
    reply    {"v": 1, "ok": true, "result": {...}}
           | {"v": 1, "ok": false, "refusal": "<typed reason>", "detail": "..."}

The relay and this process never read the body: what it carries (a sealed,
signed message) is the dashboard's to build and to open (part 3). What THIS
half proves is WHO: the request the dashboard receives names the org the
pair was admitted in and the persona and serving machine the org hello
proved for the peer, never a claim from the body.

The channel lives in the ORG connector process (one tunnel per org), so the
dashboard drives it over the connector's authenticated control socket, as
session control does over the personal connector's:

* OUTBOUND: ctl ``member-message-request`` — resolve the org channel, open the
  pair to the exact (persona, serving machine) slot, run the org hello as
  initiator, send the request, return the reply (:func:`request`).
* INBOUND: an accepted pair's request is parked in :data:`inbound`; the
  dashboard long-polls ctl ``member-message-next``, executes the op itself,
  and answers with ``member-message-reply``.

Every failure is a typed refusal record, never a hang: a relay or a peer
that predates the capability answers "not negotiated"; an offline peer is
the relay's destination-slot-absent within the open deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from tools.network.fleet_sync_channel import (
    FleetHandshakeRefused,
    HandshakeError,
    authenticate_fleet_transport,
    serve_fleet_transport,
)
from tools.network.relaykit.fleet_stream import (
    FleetStreamClosed,
    FleetStreamEndpoint,
    FleetStreamRefused,
)
from tools.network.session_control import (
    HANDSHAKE_TIMEOUT,
    INBOUND_REPLY_TIMEOUT_S,
    PEER_CLOSED_AT_OPEN,
    PEER_CLOSED_IN_HANDSHAKE,
    REQUEST_TOO_LARGE,
    InboundBroker,
    SessionControlError,
    _exchange,
    _NotSent,
    _PeerRefusal,
    _RefusalAwareTransport,
    _refuse_pair,
    _send_refusal,
    encode,
    parse_request,
    refusal,
)

logger = logging.getLogger(__name__)

VERSION = 1
#: A pair that opens but never completes the hello and exchange is cut here.
OPEN_DEADLINE_S = 10.0

NOT_NEGOTIATED = "member-message-not-negotiated"     # the relay offered no member-message/1 on this tunnel
UNARMED = "member-message-unarmed"                   # no fleet runtime armed in this process
NO_ORG_CHANNEL = "member-message-no-org-channel"     # this process holds no org hello for the organization
NOT_ORG_ADMITTED = "member-message-not-org-admitted" # the pair was not admitted by the org hello
NOT_A_MEMBER = "member-message-not-a-member"         # the proved persona is not a CONFIRMED current member
WRONG_MEMBER = "member-message-wrong-member"         # the target's hello proved a persona other than the one addressed
ORG_HELLO_REFUSED = "member-message-org-hello-refused" # the target's org hello check refused ours (detail names it)
FAILED = "member-message-failed"                     # an exception no path above names

#: Refusal codes a peer may report in place of its hello (see
#: session_control._send_refusal): recognised, others fold to peer-refused.
PEER_REFUSAL_CODES = frozenset({UNARMED, NO_ORG_CHANNEL, NOT_ORG_ADMITTED, NOT_A_MEMBER, ORG_HELLO_REFUSED})


class MemberMessageError(SessionControlError):
    """A member-message refusal decided here (``refusal`` names it)."""


def org_authenticator(runtime, genesis: str):
    """The org hello authenticator this ARMED process holds for the
    organization *genesis*, or a typed refusal."""
    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        raise MemberMessageError(UNARMED, "this process is not armed")
    resolver = getattr(scheduler, "_org_channel_for_genesis", None)
    channel = resolver(genesis) if resolver is not None else None
    if channel is None:
        raise MemberMessageError(
            NO_ORG_CHANNEL, f"no organization channel for {genesis[:12]} in this process")
    return channel


# ── inbound: pairs a co-member's machine opened to this one ────────────────

inbound = InboundBroker()


def member_message_offer_handler(
    runtime, broker: InboundBroker = inbound,
) -> Callable[[FleetStreamEndpoint], Awaitable[bool]]:
    """The connector's ``member_message_offer``: accept an inbound pair only
    while this process is armed, then serve the ORG hello and one request."""
    tasks: set = set()

    async def on_offer(endpoint: FleetStreamEndpoint) -> bool:
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            logger.info("member-message offer refused: %s", UNARMED)
            task = asyncio.create_task(_refuse_pair(endpoint, UNARMED, "this process is not armed"))
        else:
            task = asyncio.create_task(_serve(endpoint, scheduler, broker))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    return on_offer


async def _serve(endpoint: FleetStreamEndpoint, scheduler, broker: InboundBroker) -> None:
    await endpoint.ready.wait()
    if endpoint.closed.is_set():
        return
    hello_sent = False

    async def send(message: bytes) -> None:
        nonlocal hello_sent
        hello_sent = True
        await endpoint.send(message)

    async def handler(_token, message, client_pub, **extra):
        # Only the org hello admits a member message: a personal-roster
        # hello (the operator's own machine) has session control for that,
        # and a follow admission never reaches a pair.
        admitted_org = extra.get("admitted_org")
        if admitted_org is None:
            return encode(refusal(NOT_ORG_ADMITTED, "the pair was not admitted by the organization hello"))
        try:
            request = parse_request(message)
        except SessionControlError as exc:
            return encode(refusal(exc.refusal, exc.detail))
        channel = scheduler._org_channel_for_genesis(admitted_org)
        peer = channel.admitted(client_pub) if channel is not None else None
        # Membership must be CONFIRMED: is_member answers None when this
        # node cannot tell (no member list adopted yet), and "cannot tell"
        # never admits a message as a member's (review of 02ed8f96).
        if peer is None or channel.is_member(peer.persona_pub) is not True:
            return encode(refusal(NOT_A_MEMBER, "the peer is not a confirmed current member"))
        submitted = time.monotonic()
        # The dashboard receives WHO the org hello proved: the organization,
        # the persona and the serving machine — never anything the body says.
        reply = await broker.submit(
            request["op"], request["body"], peer_machine_pub=client_pub,
            org=admitted_org, persona_pub=peer.persona_pub,
        )
        logger.info(
            "member-message served op=%s org=%s from=%s dashboard_ms=%.0f ok=%s",
            request["op"], admitted_org[:12], peer.persona_pub[:12],
            (time.monotonic() - submitted) * 1000, reply.get("ok"),
        )
        try:
            return encode(reply)
        except SessionControlError as exc:
            return encode(refusal(exc.refusal, exc.detail))

    async def close(**_kwargs):
        await endpoint.close()

    try:
        await asyncio.wait_for(serve_fleet_transport(
            token=endpoint.session, recv=endpoint.recv, send=send,
            handler=handler, close=close,
            authenticator=scheduler.authenticator,
            org_channel_for=scheduler._org_channel_for_genesis,
        ), OPEN_DEADLINE_S + INBOUND_REPLY_TIMEOUT_S)   # hello, then one dashboard answer
    except HandshakeError as exc:
        # Our check of the requester's hello failed: a typed fleet refusal
        # keeps its code; an org-hello refusal (no channel for the org, a
        # persona not in the adopted member set, a stale proof) is named
        # by ORG_HELLO_REFUSED with the check's own words as the detail.
        code = getattr(exc, "refusal", None) or ORG_HELLO_REFUSED
        detail = getattr(exc, "detail", None) or str(exc)
        logger.info("member-message hello refused: %s (%s)", code, detail)
        if not hello_sent and not endpoint.closed.is_set():
            await _send_refusal(endpoint, code, detail)
    except (FleetStreamClosed, ConnectionError, asyncio.TimeoutError):
        pass
    except Exception:
        logger.warning("member-message pair %s ended with an error", endpoint.pair_id[:8], exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            await endpoint.close()


# ── outbound: one request to a co-member's session ─────────────────────────


async def request(connector, runtime, *, genesis: str, persona_pub: str,
                  machine: str, op: str, body: dict,
                  timeout: float = OPEN_DEADLINE_S) -> dict:
    """Send one request to the co-member session at the exact slot
    (*persona_pub*, *machine*) in organization *genesis* and return its
    reply record. Every failure is a typed refusal record, never a raise;
    ``at`` is ``local`` when this side decided it, ``peer`` when the target
    did."""
    try:
        record = encode({"v": VERSION, "op": op, "body": body}, too_large=REQUEST_TOO_LARGE)
        adapter = getattr(connector, "member_streams", None)
        if adapter is None:
            # The relay did not negotiate member-message/1 on this tunnel:
            # a relay that predates the op, seen as a typed refusal.
            raise MemberMessageError(NOT_NEGOTIATED, "the relay did not negotiate member-message/1")
        authenticator = org_authenticator(runtime, genesis)
        started = time.monotonic()
        try:
            endpoint = await adapter.open(
                persona_pub, machine, claimed_machine_pub=authenticator.machine_pub, timeout=timeout)
        except FleetStreamRefused as exc:
            # The relay's own admission reason, verbatim: destination-slot-absent
            # (the peer is offline), the caps, a peer that did not negotiate.
            raise MemberMessageError(exc.reason) from exc
        except FleetStreamClosed as exc:
            raise MemberMessageError(PEER_CLOSED_AT_OPEN, str(exc)) from exc
        try:
            try:
                channel = await asyncio.wait_for(authenticate_fleet_transport(
                    _RefusalAwareTransport(endpoint, codes=PEER_REFUSAL_CODES),
                    authenticator=authenticator, expected_machine_pub=machine,
                    session=endpoint.session,
                ), timeout)
            except asyncio.TimeoutError:
                raise MemberMessageError(HANDSHAKE_TIMEOUT, f"no handshake within {timeout}s") from None
            except FleetStreamClosed as exc:
                raise MemberMessageError(PEER_CLOSED_IN_HANDSHAKE, str(exc)) from exc
            # The hello proved the machine (expected_machine_pub); the persona
            # it proved for that machine must be the member addressed, stated
            # outright rather than implied by the relay slot.
            reached = authenticator.admitted(machine)
            if reached is None or reached.persona_pub != persona_pub:
                raise MemberMessageError(
                    WRONG_MEMBER, "the target's hello proved a persona other than the one addressed")
            try:
                reply = await _exchange(channel, record, timeout)
            except _NotSent as exc:
                raise MemberMessageError(PEER_CLOSED_IN_HANDSHAKE, "the channel closed at send") from exc
            logger.info(
                "member-message request op=%s org=%s to=%s total_ms=%.0f ok=%s", op,
                genesis[:12], persona_pub[:12], (time.monotonic() - started) * 1000, reply.get("ok"),
            )
        finally:
            with contextlib.suppress(Exception):
                await endpoint.close()
        if reply.get("ok") is False and "at" not in reply:
            reply = {**reply, "at": "peer"}
        return reply
    except _PeerRefusal as exc:
        return refusal(exc.refusal, exc.detail, at="peer")
    except SessionControlError as exc:
        return refusal(exc.refusal, exc.detail, at="local")
    except FleetHandshakeRefused as exc:
        return refusal(exc.refusal, exc.detail, at="local")
    except Exception as exc:  # nothing above names it
        return refusal(FAILED, f"{type(exc).__name__}: {exc}", at="local")


# ── the dashboard ↔ connector bridge (ctl socket ops) ───────────────────────


async def handle_ctl(connector, runtime, op: str, args: Any,
                     broker: InboundBroker = inbound) -> dict:
    """The three control ops the dashboard drives this half with."""
    if op == "member-message-request":
        try:
            genesis, persona_pub, machine = (str(args["genesis"]), str(args["persona_pub"]), str(args["machine"]))
            inner_op, body = str(args["op"]), dict(args.get("body") or {})
        except (KeyError, TypeError, ValueError):
            return {"ok": True, "reply": refusal(
                "ctl-request-malformed", "genesis, persona_pub, machine, op and body are required", at="local")}
        reply = await request(
            connector, runtime, genesis=genesis, persona_pub=persona_pub, machine=machine,
            op=inner_op, body=body, timeout=float(args.get("timeout") or OPEN_DEADLINE_S),
        )
        return {"ok": True, "reply": reply}
    if op == "member-message-next":
        item = await broker.next(float(args.get("wait") or 20.0))
        return {"ok": True, "request": item}
    if op == "member-message-reply":
        request_id, reply = args.get("id"), args.get("reply")
        if not isinstance(request_id, str) or not isinstance(reply, dict):
            return {"ok": False, "error": "id and reply are required"}
        return {"ok": True, "delivered": broker.reply(request_id, reply)}
    return {"ok": False, "error": f"unknown member-message ctl op {op!r}"}
