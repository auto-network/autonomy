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
from typing import Awaitable, Callable, Optional

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
    timeout: float = 10.0,
):
    """Open one mutually authenticated fleet channel through the relay to
    the exact serving slot ``(persona_pub, machine)``. The carrier decides
    WHERE; the handshake decides WHO."""
    adapter = getattr(connector, "fleet_streams", None)
    if adapter is None:
        raise ConnectionError(
            "no live tunnel negotiated fleet-directed-stream/1 on this connector")
    endpoint = await adapter.open(
        persona_pub, machine, claimed_machine_pub=claimed_machine_pub,
        timeout=timeout,
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
                      timeout: float = 10.0) -> dict:
    """Prove the carrier end to end from THIS machine: for every other slot
    of this org on the relay (or the given *targets*), open a pair and run
    the fleet handshake against the roster's other machines. Returns one
    record per slot with what was exercised and what was seen; never raises
    for a peer's refusal."""
    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        return {"ok": False, "error": "this process is not armed for fleet sync"}
    authenticator = scheduler.authenticator
    own_durable = authenticator.machine_pub
    active = resolve_roster(
        authenticator._roster_entries(), anchor_root_pub=authenticator.root_pub)
    candidates = [pub for pub in active if pub != own_durable]
    own_slot = getattr(connector, "serving_slot", None) or {}
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
        record = {"persona_pub": persona_pub, "machine": machine,
                  "ok": False, "attempts": []}
        for expected in candidates:
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
            "slots": len(slots), "results": results}
