"""Dashboard half of member-message/1 (graph://bace7454-c77, auto-qrmlg.9).

The pair, its org hello and the one request/reply live in the ORGANIZATION
connector (tools/network/member_message.py); this module is what the
dashboard does with them over that connector's authenticated control
socket, one connector per organization:

* :func:`request` sends one op to a co-member's session, addressed by the
  organization roster row (organization, persona, serving machine), and
  returns the reply record ``{"v", "ok", "result"}`` or a typed refusal
  ``{"v", "ok": false, "refusal", "detail", "at"}``. It never raises for
  a refusal: an old relay, an offline machine, a member the receiver does
  not confirm, all read the same way.
* :class:`InboundPump` long-polls one organization connector for requests
  co-members sent here and executes each through :data:`OPS`. The handler
  receives WHAT THE ORG HELLO PROVED (organization genesis, persona,
  serving machine) beside the body, and decides on that, never on a field
  of the body.
* :func:`remote_crosstalk` is the one seam both CrossTalk senders use for a
  ``name@machine`` target: the operator's own fleet machine first
  (session-control/1), else the co-member's machine (this path).

Confidentiality is the pair's: the org hello derives fresh channel keys and
every record inside is opaque to the relay. The body is not sealed to the
member's persona key on top of that: the receiving dashboard pastes the
text into a terminal in the clear, so a persona seal that the same process
must open first protects nothing the channel does not. Recorded on
auto-qrmlg.9; decision 1 of the design note is answered "neither" by it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: ctl long-poll: how long the connector holds one ``member-message-next``.
POLL_WAIT_S = 20.0
#: Backoff while an organization connector is not reachable.
UNAVAILABLE_BACKOFF_S = 5.0
#: Largest text a member send may paste.
MAX_SEND_BYTES = 256 * 1024

CONNECTOR_CALL_FAILED = "connector-call-failed"     # this dashboard could not reach the org connector's control socket
CONNECTOR_REFUSED = "connector-refused-request"     # the connector answered but did not run the request
UNKNOWN_TARGET = "unknown-target"                   # no co-member roster row names that session on that machine
NO_SUCH_SESSION = "no-such-session"
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


def _control(org: str, op: str, args: dict, *, timeout: float) -> dict:
    from tools.dashboard import link_serving_supervisor

    return link_serving_supervisor.control(org, op, args, timeout=timeout)


# ── organizations and targets ──────────────────────────────────────────────


def genesis_of(slug: str) -> str | None:
    """The ledger genesis id of the organization *slug* holds, or None."""
    from tools.dashboard import org_sync_channels

    try:
        return org_sync_channels._genesis_id(slug)
    except Exception:
        return None


def slug_of(genesis: str) -> str | None:
    """The slug of the organization whose genesis is *genesis*, among the
    organizations this process holds a channel for."""
    from tools.dashboard import session_presence

    for sink in session_presence.org_sinks():
        if genesis_of(sink.org) == genesis:
            return sink.org
    return None


def resolve_target(name: str, machine: str, *, rows: list[dict] | None = None) -> dict | None:
    """The organization roster row for session *name* on *machine*, or None.

    *machine* is the machine label the roster shows (``name@machine``), a
    full machine_pub, or a unique machine_pub prefix (>= 8 hex). Exactly one
    row must match; a name two co-members both run on the same machine
    label cannot happen (the row key is machine_pub:tmux_name).
    """
    from tools.dashboard import session_presence

    rows = session_presence.org_status_rows() if rows is None else rows
    wanted = (name or "").strip()
    where = (machine or "").strip()
    if not wanted or not where:
        return None
    hits = []
    for row in rows:
        row_name, _, row_machine = str(row.get("tmux_name") or "").rpartition("@")
        if row_name != wanted:
            continue
        machine_pub = str(row.get("machine_pub") or "")
        if where == row_machine or where == machine_pub or (
                len(where) >= 8 and re.fullmatch(r"[0-9a-f]+", where) and machine_pub.startswith(where)):
            hits.append(row)
    if len(hits) != 1:
        return None
    row = hits[0]
    if not (_HEX64.fullmatch(str(row.get("persona_pub") or ""))
            and _HEX64.fullmatch(str(row.get("machine_pub") or ""))):
        return None
    return {"org": str(row["org"]), "persona_pub": str(row["persona_pub"]),
            "machine_pub": str(row["machine_pub"]), "tmux_name": wanted,
            "machine": str(row.get("machine") or row["machine_pub"][:12]),
            "reachable": bool(row.get("reachable"))}


# ── outbound ────────────────────────────────────────────────────────────────


async def request(target: dict, op: str, body: dict | None = None, *,
                  timeout: float = 15.0) -> dict:
    """Send *op* to the co-member session *target* (a :func:`resolve_target`
    row); the reply record."""
    genesis = await asyncio.to_thread(genesis_of, target["org"])
    if genesis is None:
        return refusal(UNKNOWN_TARGET, f"no ledger for organization {target['org']!r}", at="local")
    args = {"genesis": genesis, "persona_pub": target["persona_pub"],
            "machine": target["machine_pub"], "op": op, "body": body or {},
            "timeout": timeout}
    started = time.monotonic()
    try:
        reply = await asyncio.to_thread(
            _control, target["org"], "member-message-request", args, timeout=timeout + 10.0)
    except Exception as exc:
        return refusal(CONNECTOR_CALL_FAILED, f"{type(exc).__name__}: {exc}", at="local")
    finally:
        logger.info("member-message request op=%s org=%s to=%s@%s total_ms=%.0f",
                    op, target["org"], target["persona_pub"][:12], target["machine_pub"][:12],
                    (time.monotonic() - started) * 1000)
    if not (isinstance(reply, dict) and reply.get("ok") is True
            and isinstance(reply.get("reply"), dict)):
        detail = reply.get("error") if isinstance(reply, dict) else repr(reply)
        return refusal(CONNECTOR_REFUSED, str(detail), at="local")
    return reply["reply"]


async def remote_crosstalk(name: str, machine: str, text: str, *,
                           from_session: str, from_label: str) -> dict:
    """Deliver a CrossTalk *text* to session *name* on another machine: the
    operator's own fleet machine over session-control/1 when *machine* is
    one, else a co-member's machine over member-message/1. The reply record;
    a machine neither path knows is UNKNOWN_TARGET."""
    from tools.dashboard import session_control_client

    if await asyncio.to_thread(session_control_client.resolve_machine, machine) is not None:
        return await session_control_client.request(machine, "send", {
            "tmux_name": name, "kind": "crosstalk", "text": text,
            "from_session": from_session, "from_label": from_label,
        })
    target = await asyncio.to_thread(resolve_target, name, machine)
    if target is None:
        return refusal(UNKNOWN_TARGET,
                       f"{name}@{machine} is not a session of this fleet or of a co-member",
                       at="local")
    return await request(target, "send", {
        "tmux_name": name, "text": text,
        "from_session": from_session, "from_label": from_label,
    })


# ── inbound ─────────────────────────────────────────────────────────────────

#: op name -> async handler(body, proved) -> reply record, where *proved*
#: is ``{"org": <genesis>, "persona_pub", "peer_machine_pub"}`` from the
#: org hello.
OpHandler = Callable[[dict, dict], Awaitable[dict]]
OPS: dict[str, OpHandler] = {}


def register_op(name: str, handler: OpHandler) -> None:
    """Register an inbound op. AUTHORIZATION RULE: decide on *proved* (the
    organization, persona and machine the org hello proved), never on a
    field of the body; and record the persona in the op's audit trail."""
    OPS[name] = handler


async def dispatch(op: str, body: dict, proved: dict) -> dict:
    handler = OPS.get(op)
    if handler is None:
        return refusal(UNKNOWN_OP, f"{op!r} is not a member-message op here")
    try:
        return await handler(body, proved)
    except Exception as exc:
        logger.warning("member-message op %s failed", op, exc_info=True)
        return refusal(OP_FAILED, f"{type(exc).__name__}: {exc}")


def member_label(org: str, persona_pub: str) -> str:
    """The display name the member chose for the organization, else the
    persona's first 12 hex."""
    from tools.dashboard import member_directory

    try:
        for row in member_directory.rows(org):
            if row.get("persona_pub") == persona_pub and row.get("display_name"):
                return str(row["display_name"])[:80].replace('"', "'")
    except Exception:
        pass
    return persona_pub[:12]


def render_member_envelope(*, claimed_session: str, label: str, org: str,
                           member: str, machine: str, text: str) -> str:
    """The CrossTalk block a member's message pastes as. ``from`` is the
    claimed sending session ADDRESSED AT THE MEMBER AND MACHINE THE ORG HELLO
    PROVED; the claim itself is shaped, never trusted for anything else."""
    from tools.graph.schemas.personal_session_presence import is_tmux_name

    claimed = claimed_session if is_tmux_name(claimed_session) else "unknown"
    label = str(label or claimed)[:200].replace('"', "'")
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'<crosstalk from="{claimed}@{member}"\n'
        f'           label="{label}"\n'
        f'           org="{org}" member="{member}" machine="{machine}"\n'
        f'           timestamp="{iso_now}">\n'
        f'{text}\n'
        f'</crosstalk>'
    )


class InboundPump:
    """Long-poll one organization's connector for inbound requests and
    answer them."""

    def __init__(self, org: str, *, poll=None, reply=None) -> None:
        self.org = org
        self._poll = poll or (lambda: _control(
            org, "member-message-next", {"wait": POLL_WAIT_S}, timeout=POLL_WAIT_S + 10.0))
        self._reply = reply or (lambda request_id, record: _control(
            org, "member-message-reply", {"id": request_id, "reply": record}, timeout=10.0))
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())
            self._task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            logger.warning("member-message inbound pump for %s stopped (%r)",
                           self.org, task.exception())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def once(self) -> bool:
        """One poll; True when a request was answered."""
        reply = await asyncio.to_thread(self._poll)
        if not (isinstance(reply, dict) and reply.get("ok") is True):
            await asyncio.sleep(UNAVAILABLE_BACKOFF_S)
            return False
        item = reply.get("request")
        if not isinstance(item, dict):
            return False
        proved = {"org": str(item.get("org") or ""),
                  "persona_pub": str(item.get("persona_pub") or ""),
                  "peer_machine_pub": str(item.get("peer_machine_pub") or "")}
        record = await dispatch(str(item.get("op")), item.get("body") or {}, proved)
        await asyncio.to_thread(self._reply, item["id"], record)
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No connector for this organization yet (before sign-on, or
                # across a hot reload): wait and poll again.
                logger.debug("member-message poll for %s unavailable: %s", self.org, exc)
                await asyncio.sleep(UNAVAILABLE_BACKOFF_S)


class Pumps:
    """One pump per organization this process holds a channel for; the set
    is re-read every backoff so an organization joined after activation
    gains its pump without a restart."""

    def __init__(self, *, orgs: Callable[[], list[str]] | None = None) -> None:
        self._orgs = orgs or (lambda: sorted({
            sink.org for sink in __import__(
                "tools.dashboard.session_presence", fromlist=["org_sinks"]).org_sinks()}))
        self.pumps: dict[str, InboundPump] = {}
        self._task: asyncio.Task | None = None

    def refresh(self) -> list[str]:
        try:
            wanted = list(self._orgs())
        except Exception:
            wanted = []
        for org in wanted:
            if org not in self.pumps:
                self.pumps[org] = InboundPump(org)
                self.pumps[org].start()
        return wanted

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def run(self) -> None:
        while True:
            self.refresh()
            await asyncio.sleep(UNAVAILABLE_BACKOFF_S * 6)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for pump in self.pumps.values():
            await pump.stop()
        self.pumps.clear()


def install(ops: dict[str, OpHandler]) -> Pumps:
    """Register the server-owned *ops* and start the pumps (worker
    activation)."""
    for name, handler in ops.items():
        register_op(name, handler)
    pumps = Pumps()
    pumps.start()
    return pumps
