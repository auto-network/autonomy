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
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: ctl long-poll: how long the connector holds one ``session-control-next``.
POLL_WAIT_S = 20.0
#: Backoff while the personal connector is not reachable.
UNAVAILABLE_BACKOFF_S = 5.0

#: The dashboard could not reach its personal connector's control socket.
CONNECTOR_CALL_FAILED = "connector-call-failed"
#: The personal connector answered but did not run the request.
CONNECTOR_REFUSED = "connector-refused-request"
NO_SUCH_SESSION = "no-such-session"
OP_TOO_LARGE = "op-too-large"
#: Largest text a remote send may paste (graph://7eb29bc8-31a §9.2).
MAX_SEND_BYTES = 256 * 1024
UNKNOWN_MACHINE = "unknown-machine"
UNKNOWN_OP = "unknown-op"
OP_FAILED = "op-failed"


def refusal(reason: str, detail: str = "", *, at: str | None = None) -> dict:
    out = {"v": VERSION, "ok": False, "refusal": reason}
    if detail:
        out["detail"] = detail[:300]
    if at is not None:
        out["at"] = at
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
                  timeout: float = 15.0, stream: bool = False) -> dict:
    """Send *op* to the fleet machine named *machine*; the reply record.

    ``stream`` asks for a reply that carries a file (``output``,
    a large ``tail``): the personal connector writes it under
    data/session-transfer and ``result.file`` names it; the caller owns
    deleting it."""
    machine_pub = await asyncio.to_thread(resolve_machine, machine)
    if machine_pub is None:
        return refusal(UNKNOWN_MACHINE,
                       f"{machine!r} is not an active machine of this fleet", at="local")
    args = {"machine_pub": machine_pub, "op": op, "body": body or {},
            "timeout": timeout}
    if stream:
        args["stream"] = True
    started = time.monotonic()
    try:
        reply = await asyncio.to_thread(
            _control, "session-control-request", args,
            timeout=timeout + (120.0 if stream else 10.0))
    except Exception as exc:
        return refusal(CONNECTOR_CALL_FAILED, f"{type(exc).__name__}: {exc}", at="local")
    finally:
        # With the connector's own line (lock, open, exchange), the rest of
        # this is the control socket and the thread hop.
        logger.info("session-control request op=%s to=%s total_ms=%.0f",
                    op, machine_pub[:12], (time.monotonic() - started) * 1000)
    if not (isinstance(reply, dict) and reply.get("ok") is True
            and isinstance(reply.get("reply"), dict)):
        detail = reply.get("error") if isinstance(reply, dict) else repr(reply)
        return refusal(CONNECTOR_REFUSED, str(detail), at="local")
    return reply["reply"]


# ── inbound ─────────────────────────────────────────────────────────────────

#: op name -> async handler(body, peer_machine_pub) -> reply record.
OpHandler = Callable[[dict, str], Awaitable[dict]]
OPS: dict[str, OpHandler] = {}


def register_op(name: str, handler: OpHandler) -> None:
    """Register an inbound op.

    AUTHORIZATION RULE for every op that changes anything (launch, stop,
    send input, ...): decide on ``peer_machine_pub`` -- the machine the
    session:control handshake proved, an ACTIVE roster machine of THIS
    fleet -- and never on a field of the request body; and record that
    machine in the op's audit trail (for example the launched session's
    ``launched_by`` / ``home_machine``). Read-only ops such as ``status``
    may ignore it.
    """
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
        from tools.dashboard import machine_resources

        resources = await asyncio.to_thread(machine_resources.sample)
        return ok({
            "machine_pub": machine.machine_pub if machine else None,
            "machine_id": machine.machine_id if machine else None,
            "label": names.get(machine.machine_id) if machine else None,
            "active": len(live),
            "live_sessions": len(live),
            "resources": resources,
            "dispatch_limits": limits,
        })

    return status


#: The ONLY fields of a session:registry row that leave this machine over its
#: subscription (remote_sessions): what an Active card renders. An allow-list,
#: so a column added to the row later (a host path, an error text, a
#: credential id) never crosses by default.
SESSIONS_ROW_FIELDS = (
    "session_id", "project", "type", "is_live", "started_at", "label", "role",
    "entry_count", "context_tokens", "last_activity", "last_input_at",
    "last_message", "topics", "activity_state", "harness", "model", "resolved",
    "startup_state", "state", "attention", "lifecycle_state", "phase_progress",
    "org",
)



_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
LAUNCH_REFUSED = "launch-refused"
#: The operation id already launched a session that has since ended.
LAUNCH_OP_SPENT = "launch-op-spent"
WORKSPACE_UNAVAILABLE = "workspace-unavailable"
WORKSPACE_CONFIG_ERROR = "workspace-config-error"


def launch_op(create: Callable[[dict], Awaitable[object]]) -> OpHandler:
    """``launch``: start a workspace session HERE for another fleet machine
    (graph://7eb29bc8-31a §9.2, §9.4).

    *create* is the dashboard's own session-create path for a parsed body
    (server._create_session_from_body), so a remote launch is exactly a local
    create. Authorization is the handshake: *peer* is an active roster machine
    of this fleet, and it -- never a body field -- is what gets recorded as
    ``home_machine`` / ``launched_by``. ``operation_id`` makes a retry return
    the session it already started.

    The provenance is handed to *create*, which records it right after
    registering the session and before enqueueing its launch, so the
    session's primer and env see it and a retry finds its operation id.
    """
    lock = asyncio.Lock()

    async def launch(body: dict, peer: str) -> dict:
        from tools.dashboard import session_presence
        from tools.dashboard.dao import dashboard_db

        operation_id = body.get("operation_id")
        project = body.get("project")
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            return refusal("bad-operation-id", "operation_id must be 32 hex")
        if not isinstance(project, str) or not project:
            return refusal("missing-project", "project is required")
        if not _HEX64.fullmatch(peer or ""):
            return refusal("no-authenticated-peer", "no authenticated peer")
        machine = await asyncio.to_thread(session_presence.local_machine)
        names = await asyncio.to_thread(session_presence._machine_names)
        here = {
            "machine_pub": machine.machine_pub if machine else None,
            "machine": names.get(machine.machine_id) if machine else None,
        }
        async with lock:
            existing = await asyncio.to_thread(
                dashboard_db.session_for_launch_op, operation_id)
            if existing is not None:
                state = existing.get("state")
                if state in ("FAILED", "ENDED"):
                    # The operation already ran and its session is gone. Say
                    # so, so the caller mints a new operation id; never
                    # relaunch silently under the old one.
                    return refusal(
                        LAUNCH_OP_SPENT,
                        f"operation {operation_id} already launched "
                        f"{existing['tmux_name']}, now {state}; retry with a "
                        f"new operation_id")
                return ok({"tmux_name": existing["tmux_name"], **here,
                           "repeated": True})
            request = {"type": "container", "project": project}
            for name in ("primer", "model", "harness"):
                if isinstance(body.get(name), str) and body[name]:
                    request[name] = body[name]
            response = await create(request, provenance={
                "launched_by": f"machine:{peer}", "home_machine": peer,
                "launch_op_id": operation_id,
            })
            status = getattr(response, "status_code", 500)
            try:
                import json as _json
                data = _json.loads(getattr(response, "body", b"{}") or b"{}")
            except ValueError:
                data = {}
            if status != 202 or not data.get("tmux_name"):
                error = str(data.get("error") or f"create returned {status}")
                reason = {"unknown-project": WORKSPACE_UNAVAILABLE,
                          "workspace-config-error": WORKSPACE_CONFIG_ERROR,
                          }.get(data.get("code"), LAUNCH_REFUSED)
                return refusal(reason, error)
        return ok({"tmux_name": data["tmux_name"], **here})

    return launch


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
        if not (isinstance(reply, dict) and reply.get("ok") is True):
            # A connector that answers at once without the op (the window
            # after a hot reload, before it restarts) must not be polled in
            # a tight loop.
            await asyncio.sleep(UNAVAILABLE_BACKOFF_S)
            return False
        item = reply.get("request")
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


def install(limits_provider: Callable[[], dict],
            create: Callable[[dict], Awaitable[object]] | None = None,
            ops: dict[str, OpHandler] | None = None,
            ) -> InboundPump:
    """Register the built-in ops, plus the server-owned *ops*, and start the
    pump (worker activation)."""
    register_op("status", status_op(limits_provider))
    if create is not None:
        register_op("launch", launch_op(create))
    for name, handler in (ops or {}).items():
        register_op(name, handler)
    pump = InboundPump()
    pump.start()
    return pump

