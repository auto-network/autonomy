"""Dashboard half of session-control/1 (graph://7eb29bc8-31a §9.1-9.2).

The pair and its handshake live in the personal connector
(tools/network/session_control.py); this module is what the dashboard does
with them, over the connector's existing authenticated control socket:

* :func:`request` sends one op to another fleet machine and returns the
  reply record ``{"v", "ok", "result"}`` or a typed refusal
  ``{"v", "ok": false, "refusal", "detail"}``. It never raises for a
  refusal, so an old relay, an offline machine or an unarmed peer all read
  the same way.
* :class:`InboundPump` long-polls the connector for requests other machines
  sent here, executes each through :data:`OPS`, and returns the reply.

Destinations are resolved from the ACTIVE ROSTER by durable machine_pub
(a label is looked up through the machine-profile names, then checked against
the roster), never from a presence row.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: ctl long-poll: how long the connector holds one ``session-control-next``.
POLL_WAIT_S = 20.0
#: Backoff while the personal connector is not reachable.
UNAVAILABLE_BACKOFF_S = 5.0

CONNECTOR_UNAVAILABLE = "personal-connector-unavailable"
UNKNOWN_MACHINE = "unknown-machine"
UNKNOWN_OP = "unknown-op"
OP_FAILED = "op-failed"


def refusal(reason: str, detail: str = "") -> dict:
    out = {"v": VERSION, "ok": False, "refusal": reason}
    if detail:
        out["detail"] = detail[:300]
    return out


def ok(result: dict) -> dict:
    return {"v": VERSION, "ok": True, "result": result}


def _control(op: str, args: dict, *, timeout: float) -> dict:
    from tools.dashboard import link_serving_supervisor

    return link_serving_supervisor.control("personal", op, args, timeout=timeout)


# ── machine resolution ──────────────────────────────────────────────────────


def resolve_machine(name: str, *, roster: dict[str, str] | None = None,
                    names: dict[str, str] | None = None) -> str | None:
    """The durable machine_pub an operator-typed *name* denotes, or None.

    *name* may be a full machine_pub, a unique machine_pub prefix (>= 8 hex),
    or a machine-profile display name (case-insensitive). The answer is
    always an ACTIVE roster machine.
    """
    from tools.dashboard import session_presence

    roster = session_presence._active_roster() if roster is None else roster
    if not roster:
        return None
    names = session_presence._machine_names() if names is None else names
    wanted = (name or "").strip()
    if _HEX64.fullmatch(wanted):
        return wanted if wanted in roster else None
    if len(wanted) >= 8 and re.fullmatch(r"[0-9a-f]+", wanted):
        hits = [pub for pub in roster if pub.startswith(wanted)]
        return hits[0] if len(hits) == 1 else None
    by_id = {machine_id: pub for pub, machine_id in roster.items()}
    hits = [
        by_id[machine_id] for machine_id, label in names.items()
        if label.lower() == wanted.lower() and machine_id in by_id
    ]
    return hits[0] if len(hits) == 1 else None


# ── outbound ────────────────────────────────────────────────────────────────


async def request(machine: str, op: str, body: dict | None = None, *,
                  timeout: float = 15.0) -> dict:
    """Send *op* to the fleet machine named *machine*; the reply record."""
    machine_pub = await asyncio.to_thread(resolve_machine, machine)
    if machine_pub is None:
        return refusal(UNKNOWN_MACHINE,
                       f"{machine!r} is not an active machine of this fleet")
    args = {"machine_pub": machine_pub, "op": op, "body": body or {},
            "timeout": timeout}
    try:
        reply = await asyncio.to_thread(
            _control, "session-control-request", args, timeout=timeout + 10.0)
    except Exception as exc:
        return refusal(CONNECTOR_UNAVAILABLE, f"{type(exc).__name__}: {exc}")
    if not (isinstance(reply, dict) and reply.get("ok") is True
            and isinstance(reply.get("reply"), dict)):
        detail = reply.get("error") if isinstance(reply, dict) else repr(reply)
        return refusal(CONNECTOR_UNAVAILABLE, str(detail))
    return reply["reply"]


# ── inbound ─────────────────────────────────────────────────────────────────

#: op name -> async handler(body, peer_machine_pub) -> reply record.
OpHandler = Callable[[dict, str], Awaitable[dict]]
OPS: dict[str, OpHandler] = {}


def register_op(name: str, handler: OpHandler) -> None:
    OPS[name] = handler


async def dispatch(op: str, body: dict, peer_machine_pub: str) -> dict:
    handler = OPS.get(op)
    if handler is None:
        return refusal(UNKNOWN_OP, f"{op!r} is not a session-control op here")
    try:
        return await handler(body, peer_machine_pub)
    except Exception as exc:
        logger.warning("session-control op %s failed", op, exc_info=True)
        return refusal(OP_FAILED, f"{type(exc).__name__}: {exc}")


def status_op(limits_provider: Callable[[], dict]) -> OpHandler:
    """``status``: who this machine is and what it is running."""

    async def status(_body: dict, _peer: str) -> dict:
        from tools.dashboard import session_presence
        from tools.dashboard.dao import dashboard_db

        machine = await asyncio.to_thread(session_presence.local_machine)
        live = await asyncio.to_thread(dashboard_db.get_live_sessions)
        names = await asyncio.to_thread(session_presence._machine_names)
        try:
            limits = await asyncio.to_thread(limits_provider)
        except Exception:
            limits = {}
        return ok({
            "machine_pub": machine.machine_pub if machine else None,
            "machine_id": machine.machine_id if machine else None,
            "label": names.get(machine.machine_id) if machine else None,
            "active": len(live),
            "dispatch_limits": limits,
        })

    return status


class InboundPump:
    """Long-poll the personal connector for inbound requests and answer them."""

    def __init__(self, *, poll=None, reply=None) -> None:
        self._poll = poll or (lambda: _control(
            "session-control-next", {"wait": POLL_WAIT_S},
            timeout=POLL_WAIT_S + 10.0))
        self._reply = reply or (lambda request_id, record: _control(
            "session-control-reply", {"id": request_id, "reply": record},
            timeout=10.0))
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())
            self._task.add_done_callback(self._on_done)

    @staticmethod
    def _on_done(task: asyncio.Task) -> None:
        if not task.cancelled():
            logger.warning(
                "session-control inbound pump stopped (%r); this machine no "
                "longer answers remote session control", task.exception())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def once(self) -> bool:
        """One poll; True when a request was answered."""
        reply = await asyncio.to_thread(self._poll)
        item = reply.get("request") if isinstance(reply, dict) else None
        if not isinstance(item, dict):
            return False
        record = await dispatch(
            str(item.get("op")), item.get("body") or {},
            str(item.get("peer_machine_pub") or ""))
        await asyncio.to_thread(self._reply, item["id"], record)
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The connector restarts on every hot reload and is absent
                # until the runtime is armed; wait and poll again.
                logger.debug("session-control poll unavailable: %s", exc)
                await asyncio.sleep(UNAVAILABLE_BACKOFF_S)


def install(limits_provider: Callable[[], dict]) -> InboundPump:
    """Register the built-in ops and start the pump (worker activation)."""
    register_op("status", status_op(limits_provider))
    pump = InboundPump()
    pump.start()
    return pump

