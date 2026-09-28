"""session-control/1: remote session control between the operator's machines.

Connector-side half (graph://7eb29bc8-31a §9.1, bead auto-99ioi). A
session-control pair is a relay-brokered directed pair of its own capability;
inside it the fleet handshake runs with a ``session:control`` delegation
(FleetAuthenticator(scope="session:control")), and then each pair carries
exactly one request and its reply as JSON records::

    request  {"v": 1, "op": "<op>", "body": {...}}
    reply    {"v": 1, "ok": true, "result": {...}}
           | {"v": 1, "ok": false, "refusal": "<typed reason>", "detail": "..."}

The channel lives in this connector process, but the operations belong to the
dashboard (its lifecycle worker, its session store). Rather than a new
connector-to-dashboard listener or credential, the dashboard's existing
authenticated control socket is used in both directions:

* OUTBOUND: the dashboard calls ctl ``session-control-request``; this process
  resolves the peer's slot, opens the pair, authenticates, sends the request
  and returns the reply (:func:`request`).
* INBOUND: an accepted pair's request is parked in :data:`inbound`; the
  dashboard long-polls ctl ``session-control-next``, executes the op itself,
  and answers with ``session-control-reply`` (:class:`InboundBroker`).

The destination is resolved from the ROSTER by durable machine_pub, never
from a presence row or a display name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Optional

from tools.network.fleet_sync_channel import (
    SESSION_CAP_MISSING,
    FleetAuthenticator,
    authenticate_fleet_transport,
    serve_fleet_transport,
)
from tools.network.relaykit.fleet_stream import (
    FleetStreamClosed,
    FleetStreamEndpoint,
    FleetStreamRefused,
)

logger = logging.getLogger("fleet.session_control")

SESSION_CONTROL_VERSION = 1
SESSION_CONTROL_SCOPE = "session:control"
#: Largest request or reply body, JSON-encoded.
MAX_RECORD_BYTES = 256 * 1024
#: How long an inbound request waits for the dashboard to answer it.
INBOUND_REPLY_TIMEOUT_S = 30.0
#: Bound on requests parked for the dashboard; beyond it a request is refused.
INBOUND_QUEUE_LIMIT = 64

# Typed refusals (graph://7eb29bc8-31a §9.2). ``destination-slot-absent`` and
# the other relay admission reasons arrive verbatim from the relay.
NOT_NEGOTIATED = "session-control-not-negotiated"
UNARMED = "session-control-unarmed"
NOT_IN_ROSTER = "peer-not-in-roster"
DASHBOARD_UNAVAILABLE = "session-control-dashboard-unavailable"
BUSY = "session-control-busy"
PEER_REFUSED = "peer-refused-session-control"
BAD_REQUEST = "bad-request"
OP_TOO_LARGE = "op-too-large"


class SessionControlError(Exception):
    """A request that did not produce a reply, with its typed reason."""

    def __init__(self, refusal: str, detail: str = ""):
        super().__init__(f"{refusal}: {detail}" if detail else refusal)
        self.refusal = refusal
        self.detail = detail


def encode(record: dict) -> bytes:
    data = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise SessionControlError(OP_TOO_LARGE, f"{len(data)} bytes")
    return data


def refusal(reason: str, detail: str = "") -> dict:
    out = {"v": SESSION_CONTROL_VERSION, "ok": False, "refusal": reason}
    if detail:
        out["detail"] = detail[:300]
    return out


def parse_request(raw: bytes) -> dict:
    if len(raw) > MAX_RECORD_BYTES:
        raise SessionControlError(OP_TOO_LARGE, f"{len(raw)} bytes")
    try:
        request = json.loads(raw)
    except ValueError as exc:
        raise SessionControlError(BAD_REQUEST, "not JSON") from exc
    if (
        not isinstance(request, dict)
        or request.get("v") != SESSION_CONTROL_VERSION
        or not isinstance(request.get("op"), str)
        or not isinstance(request.get("body", {}), dict)
    ):
        raise SessionControlError(BAD_REQUEST, "not a session-control request")
    return {"op": request["op"], "body": request.get("body") or {}}


def session_authenticator(runtime) -> FleetAuthenticator:
    """The session:control authenticator for an ARMED connector runtime.

    Same process key, root and roster as the sync authenticator; only the
    delegation (and so the verified scope) differs.
    """
    scheduler = getattr(runtime, "scheduler", None)
    cert = getattr(runtime, "session_control_cert", None)
    if scheduler is None:
        raise SessionControlError(UNARMED, "this process is not armed")
    if cert is None:
        raise SessionControlError(
            SESSION_CAP_MISSING, "this runtime holds no session:control delegation")
    sync = scheduler.authenticator
    return FleetAuthenticator(
        sync.machine_key,
        root_pub=sync.root_pub,
        roster_entries=sync._roster_entries,
        roster_machine_pub=sync.machine_pub,
        delegation_cert=cert,
        require_delegation=True,
        scope=SESSION_CONTROL_SCOPE,
    )


# ── inbound: pairs another machine opened to this one ─────────────────────────


class InboundBroker:
    """Requests from peers, parked until the dashboard answers them."""

    def __init__(self, *, limit: int = INBOUND_QUEUE_LIMIT,
                 reply_timeout: float = INBOUND_REPLY_TIMEOUT_S):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._waiting: dict[str, asyncio.Future] = {}
        self._limit = limit
        self._reply_timeout = reply_timeout

    async def submit(self, op: str, body: dict, *, peer_machine_pub: str) -> dict:
        """Park one request and wait for the dashboard's reply."""
        if len(self._waiting) >= self._limit:
            return refusal(BUSY, "too many requests waiting for the dashboard")
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        self._queue.put_nowait({
            "id": request_id, "op": op, "body": body,
            "peer_machine_pub": peer_machine_pub, "received_at": time.time(),
        })
        try:
            return await asyncio.wait_for(future, self._reply_timeout)
        except asyncio.TimeoutError:
            return refusal(DASHBOARD_UNAVAILABLE,
                           "the dashboard did not answer in time")
        finally:
            self._waiting.pop(request_id, None)

    async def next(self, wait_s: float) -> Optional[dict]:
        """The next request whose submitter is still waiting, or None."""
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_s)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                item = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if item["id"] in self._waiting:
                return item

    def reply(self, request_id: str, reply: dict) -> bool:
        future = self._waiting.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(reply)
        return True


#: This connector process's broker; the ctl ops read and answer it.
inbound = InboundBroker()


def session_control_offer_handler(
    runtime, broker: InboundBroker = inbound,
) -> Callable[[FleetStreamEndpoint], Awaitable[bool]]:
    """The connector's ``session_control_offer``: accept an inbound pair only
    while this process holds a session:control credential, then serve the
    session:control handshake and one request on it."""
    tasks: set = set()

    async def on_offer(endpoint: FleetStreamEndpoint) -> bool:
        try:
            authenticator = session_authenticator(runtime)
        except SessionControlError as exc:
            logger.info("session-control offer refused: %s", exc.refusal)
            return False
        task = asyncio.create_task(_serve(endpoint, authenticator, broker))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    return on_offer


async def _serve(endpoint: FleetStreamEndpoint, authenticator: FleetAuthenticator,
                 broker: InboundBroker) -> None:
    await endpoint.ready.wait()
    if endpoint.closed.is_set():
        return

    async def handler(_token, message, client_pub, **_extra):
        # The sender is the machine the handshake proved, never a field of
        # the request.
        try:
            request = parse_request(message)
        except SessionControlError as exc:
            return encode(refusal(exc.refusal, exc.detail))
        reply = await broker.submit(
            request["op"], request["body"], peer_machine_pub=client_pub)
        try:
            return encode(reply)
        except SessionControlError as exc:
            return encode(refusal(exc.refusal, exc.detail))

    async def close(**_kwargs):
        await endpoint.close()

    try:
        await serve_fleet_transport(
            token=endpoint.session, recv=endpoint.recv, send=endpoint.send,
            handler=handler, close=close, authenticator=authenticator,
        )
    except (FleetStreamClosed, ConnectionError):
        pass
    except Exception:
        logger.warning("session-control pair %s ended with an error",
                       endpoint.pair_id[:8], exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            await endpoint.close()


# ── outbound: this machine asks another ───────────────────────────────────────


async def request(connector, runtime, *, machine_pub: str, op: str,
                  body: dict, timeout: float = 15.0,
                  resolve_slot=None) -> dict:
    """Send one request to the fleet machine *machine_pub* and return its
    reply record. Every failure is a typed refusal record, never a raise, so
    an old relay ("unknown control op") or an unarmed peer reads the same way
    as any other refusal."""
    try:
        record = encode({"v": SESSION_CONTROL_VERSION, "op": op, "body": body})
        adapter = getattr(connector, "session_streams", None)
        if adapter is None:
            # The relay did not negotiate session-control/1 on this tunnel,
            # which is what a relay that predates it looks like.
            raise SessionControlError(
                NOT_NEGOTIATED, "the relay did not negotiate session-control/1")
        authenticator = session_authenticator(runtime)
        if resolve_slot is None:
            from tools.network.fleet_relay_carrier import resolve_peer_slot
            resolve_slot = resolve_peer_slot
        try:
            (persona_pub, slot_machine), _source = await resolve_slot(
                connector, runtime, machine_pub, timeout=timeout)
        except ConnectionError as exc:
            reason = NOT_IN_ROSTER if "not in the active roster" in str(exc) \
                else "destination-slot-absent"
            raise SessionControlError(reason, str(exc)) from exc
        try:
            endpoint = await adapter.open(
                persona_pub, slot_machine,
                claimed_machine_pub=authenticator.machine_pub, timeout=timeout)
        except FleetStreamRefused as exc:
            # The relay's own admission reason, verbatim; a relay that
            # predates session-control/1 says "unknown control op".
            raise SessionControlError(exc.reason) from exc
        except FleetStreamClosed as exc:
            # Admitted by the relay, then refused by the peer's connector:
            # unarmed, or holding no session:control delegation.
            raise SessionControlError(PEER_REFUSED, str(exc)) from exc
        try:
            channel = await asyncio.wait_for(authenticate_fleet_transport(
                endpoint, authenticator=authenticator,
                expected_machine_pub=machine_pub, session=endpoint.session,
            ), timeout)
            await channel.send_message(record)
            raw = await asyncio.wait_for(channel.recv_message(), timeout)
        finally:
            with contextlib.suppress(Exception):
                await endpoint.close()
        reply = json.loads(raw)
        if not isinstance(reply, dict) or reply.get("v") != SESSION_CONTROL_VERSION:
            raise SessionControlError(BAD_REQUEST, "malformed reply")
        return reply
    except SessionControlError as exc:
        return refusal(exc.refusal, exc.detail)
    except asyncio.TimeoutError:
        return refusal("session-control-timeout", f"no reply within {timeout}s")
    except Exception as exc:  # handshake refusals, closed pairs, bad JSON
        detail = f"{type(exc).__name__}: {exc}"
        reason = SESSION_CAP_MISSING if SESSION_CAP_MISSING in str(exc) \
            else "session-control-failed"
        return refusal(reason, detail)


def ctl_op(op: str) -> bool:
    return op in ("session-control-request", "session-control-next",
                  "session-control-reply")


async def handle_ctl(connector, runtime, op: str, args: Any,
                     broker: InboundBroker = inbound) -> dict:
    """The three ctl ops the dashboard drives this module with."""
    args = args if isinstance(args, dict) else {}
    if op == "session-control-request":
        machine_pub, request_op = args.get("machine_pub"), args.get("op")
        if not isinstance(machine_pub, str) or not isinstance(request_op, str):
            return {"ok": True, "reply": refusal(BAD_REQUEST, "machine_pub and op")}
        reply = await request(
            connector, runtime, machine_pub=machine_pub, op=request_op,
            body=args.get("body") if isinstance(args.get("body"), dict) else {},
            timeout=float(args.get("timeout") or 15.0),
        )
        return {"ok": True, "reply": reply}
    if op == "session-control-next":
        item = await broker.next(float(args.get("wait") or 20.0))
        return {"ok": True, "request": item}
    if op == "session-control-reply":
        request_id, reply = args.get("id"), args.get("reply")
        if not isinstance(request_id, str) or not isinstance(reply, dict):
            return {"ok": False, "error": "id and reply are required"}
        return {"ok": True, "delivered": broker.reply(request_id, reply)}
    return {"ok": False, "error": f"unknown session-control ctl op {op!r}"}
