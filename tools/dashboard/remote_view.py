"""Read a session on another fleet machine through this machine's viewer
(graph://7eb29bc8-31a §6.2, §9.6, bead auto-fd68i).

The viewer addresses a remote session as ``<name>@<machine>`` end to end:
its page and its tail requests, which api_session_tail proxies over
session-control's ``tail`` op. New entries arrive as ``session:messages``
over the machine's standing subscription (remote_sessions), not from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Query keys the tail op forwards; every tail mode the viewer uses.
TAIL_QUERY_KEYS = ("tail_lines", "tail_entries", "before", "before_file",
                   "after_file", "after")


#: Refusals that mean the MACHINE cannot be reached right now (as opposed to
#: a session it does not have): the viewer renders them as "unreachable".
#: One entry per failure path (tools/network/session_control.py codes).
UNREACHABLE_REFUSALS = frozenset({
    "destination-slot-absent", "slot-lookup-failed",
    "handshake-timeout", "reply-timeout", "stream-chunk-timeout",
    "transfer-deadline-exceeded", "peer-closed-in-handshake",
    "personal-connector-unavailable", "connector-call-failed",
    "connector-refused-request", "session-control-failed",
})
#: Refusals that mean the machine answered but remote sessions are not
#: ENABLED between the two: a relay or connector without the capability, a
#: runtime whose delegation lacks session:control, or a handshake check that
#: refused. Every handshake code (``own-*`` / ``peer-*``, from
#: fleet_sync_channel.FleetHandshakeRefused) counts, named by
#: ``refusal_state``; this set is the rest.
NOT_ENABLED_REFUSALS = frozenset({
    "session-control-not-negotiated", "session-control-unarmed",
    "session-control-not-granted", "peer-closed-at-open",
    "peer-not-in-roster", "unknown-machine", "peer-refused",
})


def refusal_state(refusal: str | None) -> str | None:
    """``unreachable``, ``not_enabled``, or None (a refusal about the request
    or the session, not the machine)."""
    if not refusal:
        return None
    if refusal in UNREACHABLE_REFUSALS:
        return "unreachable"
    if refusal in NOT_ENABLED_REFUSALS or refusal.startswith(("own-", "peer-")):
        return "not_enabled"
    return None


def unreachable_since(machine: str) -> int | None:
    """Unix seconds of the last successful pull from *machine* (a name or
    machine_pub), or None when it was never reached."""
    from tools.dashboard import session_control_client, session_presence

    pub = session_control_client.resolve_machine(machine)
    if pub is None:
        return None
    return session_presence._peer_last_success_s().get(pub)


def rewrite_identity(data: dict, address: str) -> dict:
    """Make a far machine's tail response name the session by its Home
    address, so the viewer's store, SSE routing and attachment URLs
    (which go back through ``<name>@<machine>``) all agree."""
    for key in ("session_id", "tmux_session", "tmux_name"):
        if key in data:
            data[key] = address
    for entry in data.get("entries") or []:
        if isinstance(entry, dict) and entry.get("type") == "viewer_attachment":
            entry["session"] = address
    data["machine_address"] = address
    return data


async def fetch_tail(machine: str, name: str, project: str, query: dict,
                     *, timeout: float = 20.0) -> dict:
    """The far machine's tail response, or a refusal record."""
    from tools.dashboard import session_control_client

    reply = await session_control_client.request(
        machine, "tail", {"session_id": name, "project": project, "query": query},
        timeout=timeout, stream=True)
    if not reply.get("ok"):
        return reply
    result = reply.get("result") or {}
    if "file" in result:
        path = Path(result["file"])
        try:
            data = json.loads(await asyncio.to_thread(path.read_bytes))
        finally:
            path.unlink(missing_ok=True)
    else:
        data = result.get("tail")
    if not isinstance(data, dict):
        return {"v": 1, "ok": False, "refusal": "tail-missing", "detail": "the reply carried no tail", "at": "local"}
    return {"v": 1, "ok": True, "tail": data}
