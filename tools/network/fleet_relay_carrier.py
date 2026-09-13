"""The dashboard's use of fleet-directed-stream/1 (auto-fh2nv wiring).

Two halves, both consuming the connector's :class:`FleetStreamAdapter`
unchanged and the fleet runtime's existing responder unchanged:

* RESPONDER — :func:`fleet_stream_offer_handler` accepts an inbound pair
  offer only while this process is armed (``runtime.scheduler`` exists) and
  serves it with exactly what the direct listener serves: the scheduler's
  ``_handle``, its ``FleetAuthenticator`` and its org-channel resolver,
  through ``serve_fleet_transport``. Telemetry records the channel as
  ``relay`` so the fleet card can tell the two paths apart.

* INITIATOR — :func:`fleet_relay_connect` is the carrier analogue of
  ``fleet_direct_connect``: open a pair to an exact serving slot, run the
  fleet handshake pinned to the expected DURABLE peer key, return the same
  ``ViewerChannel`` the pull path already consumes. :func:`relay_probe`
  drives it against every other slot of this org the relay reports, which
  is the operator's end-to-end proof that two dial-out-only dashboards
  reach each other through the relay with real identities.

The slot → durable-key mapping is the descriptor's job (auto-ekwbp /
auto-ieh3l). The probe does not have it yet, so it tries each OTHER active
roster machine as the expected peer; a wrong guess is a refused handshake,
never a false success, because ``verify_server`` pins the key.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable, Mapping, Optional

from tools.network.fleet_roster import resolve as resolve_roster
from tools.network.fleet_sync_channel import (
    authenticate_fleet_transport,
    serve_fleet_transport,
)
from tools.network.relaykit.fleet_stream import (
    FleetStreamClosed,
    FleetStreamEndpoint,
    FleetStreamRefused,
)

logger = logging.getLogger("fleet.relay_carrier")

#: Telemetry channel name for pulls carried by the relay pair.
RELAY_CHANNEL = "relay"


def fleet_stream_offer_handler(runtime) -> Callable[[FleetStreamEndpoint], Awaitable[bool]]:
    """The connector's ``fleet_stream_offer`` for a process whose fleet
    runtime is *runtime* (``fleet_relay_sync.connector_runtime``)."""
    tasks: set = set()

    async def on_offer(endpoint: FleetStreamEndpoint) -> bool:
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            # Unarmed: refusing here is the same posture as the direct
            # listener refusing a pull while locked, and it is counted the
            # same way so the profile flag can say so.
            with contextlib.suppress(Exception):
                runtime.locked_refusals += 1
                if runtime.first_locked_refusal_at is None:
                    runtime.first_locked_refusal_at = time.time()
            return False
        task = asyncio.create_task(_serve_offer(endpoint, scheduler))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    return on_offer


async def _serve_offer(endpoint: FleetStreamEndpoint, scheduler) -> None:
    await endpoint.ready.wait()
    if endpoint.closed.is_set():
        return

    async def handler(channel_token, message, client_pub, **extra):
        return await scheduler._handle(
            channel_token, message, client_pub,
            telemetry_channel=RELAY_CHANNEL, **extra,
        )

    async def close(**kwargs):
        await endpoint.close()

    try:
        await serve_fleet_transport(
            token=endpoint.session, recv=endpoint.recv, send=endpoint.send,
            handler=handler, close=close,
            authenticator=scheduler.authenticator,
            org_channel_for=getattr(scheduler, "_org_channel_for_genesis", None),
        )
    except (FleetStreamClosed, ConnectionError):
        pass
    except Exception:
        logger.warning(
            "relay pair %s: responder ended with an error",
            endpoint.pair_id[:8], exc_info=True,
        )
    finally:
        with contextlib.suppress(Exception):
            await endpoint.close()


async def fleet_relay_connect(
    connector, persona_pub: str, machine: str, *,
    authenticator, expected_machine_pub: str,
    claimed_machine_pub: Optional[str] = None,
    operation_id: Optional[str] = None,
    timeout: float = 10.0,
):
    """Open one mutually authenticated fleet channel through the relay to
    the exact serving slot ``(persona_pub, machine)``. The carrier decides
    WHERE; the handshake decides WHO.

    ``operation_id`` is the CALLER'S. The relay arbitrates one pair per
    (source tunnel, destination tunnel, operation_id) and refuses a duplicate
    with `operation-already-open`; if the id is minted here instead, that
    arbitration is keyed on a value the caller's controller never saw, and the
    relay-level guarantee stops guarding the caller's actual operation. Found
    by auto-0909-161758 reviewing auto-ew9wf.
    """
    adapter = getattr(connector, "fleet_streams", None)
    if adapter is None:
        raise ConnectionError(
            "no live tunnel negotiated fleet-directed-stream/1 on this connector")
    endpoint = await adapter.open(
        persona_pub, machine, claimed_machine_pub=claimed_machine_pub,
        operation_id=operation_id, timeout=timeout,
    )
    return await asyncio.wait_for(authenticate_fleet_transport(
        endpoint, authenticator=authenticator,
        expected_machine_pub=expected_machine_pub, session=endpoint.session,
    ), timeout)


async def list_org_slots(connector, *, timeout: float = 10.0) -> list:
    """The relay's live serving slots for this tunnel's org (the
    ``fleet-slots`` control op): ``[{persona_pub, machine, caps, version}]``."""
    reply = await connector.control("fleet-slots", {}, timeout=timeout)
    if not (isinstance(reply, dict) and reply.get("ok") is True):
        raise ConnectionError(
            f"fleet-slots refused: {reply.get('error') if isinstance(reply, dict) else reply!r}")
    return list(reply.get("slots") or [])


async def relay_probe(connector, runtime, *, targets: Optional[list] = None,
                      locators: Optional[Mapping] = None,
                      timeout: float = 10.0) -> dict:
    """Prove the carrier end to end from THIS machine: for every other slot
    of this org on the relay (or the given *targets*), open a pair and run
    the fleet handshake against the roster's other machines. Returns one
    record per slot with what was exercised and what was seen; never raises
    for a peer's refusal.

    ``locators`` is ``{durable machine_pub: relay locator}`` from peers'
    VERIFIED reachability descriptors (auto-e38g4). A slot named by one is
    proven against that peer's durable key and nothing else, and the record
    says ``source="descriptor"``. Without one the probe falls back to trying
    each roster machine in turn until the handshake proves one --
    ``source="roster"`` -- which is a guess that costs a connect and a
    handshake round trip per wrong answer.

    The locator decides nothing about membership. It selects WHICH durable key
    to prove; ``FleetAuthenticator`` still proves it, so a locator naming the
    wrong peer makes the probe fail and can never make it admit.
    """
    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        return {"ok": False, "error": "this process is not armed for fleet sync"}
    authenticator = scheduler.authenticator
    own_durable = authenticator.machine_pub
    active = resolve_roster(
        authenticator._roster_entries(), anchor_root_pub=authenticator.root_pub)
    candidates = [pub for pub in active if pub != own_durable]
    own_slot = getattr(connector, "serving_slot", None) or {}
    # Invert the locators: a slot a peer PUBLISHED maps to the durable key of
    # the machine that signed it. Only roster candidates are admitted, so a
    # locator for a machine that has left the roster resolves to nothing and
    # the slot falls back to the roster scan rather than to a stale identity.
    slot_owner = {}
    for durable, locator in (locators or {}).items():
        if durable not in candidates or not isinstance(locator, Mapping):
            continue
        persona, machine = locator.get("persona_pub"), locator.get("serving_machine_pub")
        if persona and machine:
            slot_owner[(persona, machine)] = durable
    try:
        slots = targets if targets is not None else await list_org_slots(
            connector, timeout=timeout)
    except ConnectionError as exc:
        return {"ok": False, "error": str(exc)}
    results = []
    for slot in slots:
        persona_pub, machine = slot.get("persona_pub"), slot.get("machine")
        if not persona_pub or not machine:
            continue
        if (persona_pub, machine) == (own_slot.get("persona_pub"), own_slot.get("machine")):
            continue
        owner = slot_owner.get((persona_pub, machine))
        expected_keys = [owner] if owner is not None else candidates
        record = {"persona_pub": persona_pub, "machine": machine,
                  "ok": False, "attempts": [],
                  "source": "descriptor" if owner is not None else "roster"}
        for expected in expected_keys:
            started = time.monotonic()
            try:
                channel = await fleet_relay_connect(
                    connector, persona_pub, machine,
                    authenticator=authenticator, expected_machine_pub=expected,
                    claimed_machine_pub=own_durable, timeout=timeout,
                )
            except FleetStreamRefused as exc:
                record["attempts"].append({"expected": expected, "refused": exc.reason})
                break                          # the relay refused the slot itself
            except Exception as exc:
                record["attempts"].append(
                    {"expected": expected, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            rtt_ms = int((time.monotonic() - started) * 1000)
            with contextlib.suppress(Exception):
                await channel.close()
            record.update(ok=True, durable_peer=expected, handshake_ms=rtt_ms)
            break
        results.append(record)
    return {"ok": all(r["ok"] for r in results) and bool(results),
            "own_slot": own_slot, "own_durable": own_durable,
            "slots": len(slots), "locators": len(slot_owner),
            "results": results}

#: Delegated relay pulls, by operation id (auto-ew9wf). In memory and
#: process-local by design: the DASHBOARD owns the controller and mints the
#: id, this process only executes what it was told. Nothing here decides which
#: peer to pull, so this map is never a second peer selector.
_RELAY_PULLS: dict = {}

#: Finished jobs are kept this long so a poller that arrives late still learns
#: the outcome rather than "unknown operation", which is indistinguishable
#: from "never started".
RELAY_PULL_RETENTION_S = 300.0


def _reap_relay_pulls(now: float) -> None:
    for operation_id, job in list(_RELAY_PULLS.items()):
        done_at = job.get("done_at")
        if done_at is not None and (now - done_at) > RELAY_PULL_RETENTION_S:
            _RELAY_PULLS.pop(operation_id, None)


async def resolve_peer_slot(connector, runtime, peer_machine_pub: str, *,
                            locator: Optional[Mapping] = None,
                            timeout: float = 10.0) -> tuple:
    """The peer's exact serving slot, and where the answer came from.

    Returns ``((persona_pub, machine), source)``. Slot resolution belongs on
    THIS side of the socket: the relay connection is here, and the dashboard
    knows a peer by its durable roster key, not by which slot that peer
    happens to be serving under right now.

    Two sources, and the source is RETURNED rather than assumed so the
    telemetry row records which one answered:

    ``descriptor`` -- the peer's own signed relay locator (auto-e38g4), which
    is the only source that can name an ORG-scope slot, where the serving key
    is a distinct per-org key and the durable-key equality below is false.

    ``org-slots`` -- the relay's live slot list matched by durable key.
    Correct for personal scope, where the serving key IS the durable key.

    A locator is a HINT and is checked against the live list rather than
    trusted in place of it. The list is being fetched on this path anyway, so
    the check costs nothing, and it means a locator left over from a connector
    that has since moved slots falls back to the equality instead of sending a
    pull at a slot the relay no longer has.

    Neither source is authority and neither pretends to be: they only narrow
    which slot to dial. The fleet handshake still proves WHO, exactly as on
    the direct path.
    """
    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        raise ConnectionError("this process is not armed for fleet sync")
    authenticator = scheduler.authenticator
    active = resolve_roster(
        authenticator._roster_entries(), anchor_root_pub=authenticator.root_pub)
    if peer_machine_pub not in active:
        # The roster says who is a peer (contract §1). A slot for a machine
        # the roster does not carry is not a peer's slot.
        raise ConnectionError(
            f"peer {peer_machine_pub[:12]} is not in the active roster")
    slots = await list_org_slots(connector, timeout=timeout)
    own = (getattr(connector, "serving_slot", None) or {}).get("machine")
    wanted = None
    if isinstance(locator, Mapping):
        persona, named = (
            locator.get("persona_pub"), locator.get("serving_machine_pub"))
        if persona and named and named != own:
            wanted = (persona, named)
    for slot in slots:
        machine = slot.get("machine")
        if not machine or machine == own:
            continue
        if wanted is not None and (slot.get("persona_pub"), machine) == wanted:
            return wanted, "descriptor"
    for slot in slots:
        machine = slot.get("machine")
        if not machine or machine == own:
            continue
        if machine == peer_machine_pub:
            # PERSONAL-SCOPE IDENTITY: the serving key equals the durable
            # roster key here, so a slot can be matched by durable key. That
            # stops being true for org scopes, which carry distinct per-org
            # serving keys since 76d61b5 — there the slot must come from the
            # peer's descriptor locator (auto-e38g4), not from this equality.
            # Correct today, invisible tomorrow, so it is named.
            return (slot.get("persona_pub"), machine), "org-slots"
    # A peer whose slot the relay does not report is not reachable by relay
    # right now. Say which peer, rather than returning an empty slot that
    # would fail later as a confusing handshake mismatch.
    raise ConnectionError(
        f"the relay reports no serving slot for peer {peer_machine_pub[:12]}")


async def start_relay_pull(connector, runtime, *, peer_machine_pub: str,
                           scope: str, operation_id: str,
                           persona_pub: str | None = None,
                           machine: str | None = None,
                           locator: Optional[Mapping] = None,
                           org_scope: str | None = None,
                           timeout: float = 10.0) -> dict:
    """Begin one delegated scope pull over the relay; return immediately.

    A pull runs for minutes and the control protocol is one request/reply, so
    holding the loopback connection open for the duration would tie the
    dashboard's control socket to the transfer. The op returns a job id and
    the caller polls :func:`relay_pull_status`.

    The operation id is the DASHBOARD's, minted by the peer path controller
    that decided to fall back. Re-submitting a live id returns the existing
    job rather than starting a second one: that is the one-open-per-operation
    promise auto-ieh3l made auto-fh2nv, enforced here as well as at the relay,
    so a retry storm cannot be created by a caller that lost its reply.
    """
    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        return {"ok": False, "error_kind": "not-armed",
                "error": "this process is not armed for fleet sync"}
    now = time.monotonic()
    _reap_relay_pulls(now)
    existing = _RELAY_PULLS.get(operation_id)
    if existing is not None:
        return {"ok": True, "operation_id": operation_id,
                "state": existing["state"], "duplicate": True}

    org_channel = None
    if org_scope:
        # An org co-member: the slot is the caller's (the relay listed it)
        # and the hello is the org's. The personal roster has no say here.
        org_channel = scheduler._org_channels().get(org_scope)
        if org_channel is None:
            return {"ok": False, "error_kind": "no-org-channel",
                    "error": f"this process holds no org sync channel for {org_scope}"}
        if persona_pub is None or machine is None:
            return {"ok": False, "error_kind": "no-slot",
                    "error": "an org pull names the slot the relay listed"}
    slot_source = "caller"
    if persona_pub is None or machine is None:
        try:
            (persona_pub, machine), slot_source = await resolve_peer_slot(
                connector, runtime, peer_machine_pub,
                locator=locator, timeout=timeout,
            )
        except Exception as exc:
            return {"ok": False, "error_kind": "no-slot",
                    "error": f"{type(exc).__name__}: {exc}"}

    job: dict = {"state": "running", "started_at": now, "done_at": None,
                 "scope": scope, "peer": peer_machine_pub,
                 "slot_source": slot_source}
    _RELAY_PULLS[operation_id] = job

    async def _run() -> None:
        try:
            await scheduler._pull_scope(
                peer_machine_pub, (), scope,
                org_channel=org_channel,
                relay_slot=(persona_pub, machine),
                relay_connector=connector,
                relay_operation_id=operation_id,
            )
        except Exception as exc:
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"[:400]
            # Carry the carrier's own taxonomy through untouched when it is
            # there: the dashboard's controller decides stand-down vs retry vs
            # stop from these, and flattening them here would make that
            # decision impossible on the far side of the socket.
            for field in ("reason", "pair_id", "code"):
                value = getattr(exc, field, None)
                if value is not None:
                    job[field] = value
        else:
            job["state"] = "done"
        finally:
            job["done_at"] = time.monotonic()

    job["task"] = asyncio.create_task(_run())
    return {"ok": True, "operation_id": operation_id, "state": "running"}


def relay_pull_status(operation_id: str) -> dict:
    """What became of a delegated pull. Unknown ids are reported as unknown
    rather than as failure: they are different facts and the caller responds
    to them differently."""
    _reap_relay_pulls(time.monotonic())
    job = _RELAY_PULLS.get(operation_id)
    if job is None:
        return {"ok": False, "error_kind": "unknown-operation",
                "error": "no such relay pull on this connector"}
    reply = {"ok": True, "operation_id": operation_id, "state": job["state"],
             "scope": job["scope"], "slot_source": job.get("slot_source")}
    for field in ("error", "reason", "pair_id", "code"):
        if field in job:
            reply[field] = job[field]
    return reply
