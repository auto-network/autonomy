"""The operator's other machines as the Sessions page sees them (bead
auto-mje3g, design 64906530 revision f984e30b).

* :func:`launch_targets` answers the chooser's first question -- which
  machines can this dashboard launch on -- and, for each, what it has free:
  live sessions, RAM, disk, load. This machine is always first and is
  sampled locally; every other ACTIVE roster machine is asked through
  session-control's ``status`` op, all at once, so the answers are ready by
  the time a workspace is picked. A machine that does not answer in time is
  listed unreachable, with the last time it was pulled from.
* :func:`remote_sessions` gives the Active list its remote cards: the live
  rows each reachable machine builds for its own session:registry, addressed
  ``<name>@<machine>``; for a machine that cannot be reached, its presence
  rows, marked unreachable, so the operator still sees what runs there.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: How long the chooser waits for one machine before calling it unreachable.
STATUS_TIMEOUT_S = 4.0
#: One answer per machine and op is shared by every caller for this long,
#: and concurrent callers share one in-flight request, so N open pages cost
#: one relay pair per machine per window, not N.
CACHE_TTL_S = 10.0

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_inflight: dict[tuple[str, str], asyncio.Future] = {}


async def _ask(pub: str, op: str, *, now=None) -> dict:
    """session-control *op* to *pub*, cached for CACHE_TTL_S, single-flight."""
    import time

    from tools.dashboard import session_control_client

    key = (pub, op)
    current = time.monotonic() if now is None else now
    hit = _cache.get(key)
    if hit is not None and current - hit[0] < CACHE_TTL_S:
        return hit[1]
    pending = _inflight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    future = asyncio.get_running_loop().create_future()
    _inflight[key] = future
    try:
        reply = await session_control_client.request(
            pub, op, {}, timeout=STATUS_TIMEOUT_S)
        _cache[key] = (time.monotonic(), reply)
        future.set_result(reply)
        return reply
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        _inflight.pop(key, None)
        if future.done() and not future.cancelled():
            future.exception()  # mark retrieved


def machine_state(reply: dict) -> dict:
    """How a machine that did not answer reads: unreachable, or reachable but
    without remote sessions enabled (a missing capability or grant)."""
    from tools.dashboard import remote_view

    refusal = reply.get("refusal")
    if refusal in remote_view.NOT_ENABLED_REFUSALS:
        return {"reachable": False, "state": "not_enabled", "reason": refusal}
    return {"reachable": False, "state": "unreachable", "reason": refusal}


def _context():
    from tools.dashboard import session_presence

    local = session_presence.local_machine()
    roster = session_presence._active_roster() or {}
    names = session_presence._machine_names()
    last = session_presence._peer_last_success_s()
    return local, roster, names, last


def _label(pub: str, roster: dict, names: dict) -> str:
    return names.get(roster.get(pub, "")) or pub[:12]


async def launch_targets() -> list[dict]:
    """This machine, then every other active roster machine, with figures."""
    from tools.dashboard import machine_resources
    from tools.dashboard.dao import dashboard_db

    local, roster, names, last = await asyncio.to_thread(_context)
    if local is None:
        return []
    live = await asyncio.to_thread(dashboard_db.get_live_sessions)
    here = await asyncio.to_thread(machine_resources.sample)
    targets = [{
        "machine_pub": local.machine_pub,
        "label": _label(local.machine_pub, roster, names),
        "local": True, "reachable": True, "state": "reachable",
        "live_sessions": len(live), **here, "unreachable_since": None,
    }]
    others = sorted(pub for pub in roster if pub != local.machine_pub)

    async def ask(pub: str) -> dict:
        label = _label(pub, roster, names)
        reply = await _ask(pub, "status")
        base = {"machine_pub": pub, "label": label, "local": False}
        if not reply.get("ok"):
            return {**base, **machine_state(reply), "refusal": reply.get("refusal"),
                    "unreachable_since": last.get(pub)}
        result = reply.get("result") or {}
        return {**base, "reachable": True, "state": "reachable",
                "unreachable_since": None,
                "live_sessions": result.get("live_sessions", result.get("active")),
                **(result.get("resources") or {})}

    targets.extend(await asyncio.gather(*(ask(pub) for pub in others)))
    return targets


def _address(row_name: str, label: str) -> str:
    return f"{row_name}@{label}"


async def remote_sessions() -> list[dict]:
    """Active-list rows for sessions on the operator's other machines."""
    from tools.dashboard import session_presence

    local, roster, names, last = await asyncio.to_thread(_context)
    if local is None:
        return []
    others = sorted(pub for pub in roster if pub != local.machine_pub)
    if not others:
        return []
    presence = await asyncio.to_thread(session_presence.read_presence)

    async def rows_for(pub: str) -> list[dict]:
        label = _label(pub, roster, names)
        machine = {"machine": label, "machine_pub": pub}
        reply = await _ask(pub, "sessions")
        if reply.get("ok"):
            out = []
            for row in (reply.get("result") or {}).get("sessions") or []:
                # get_registry rows name the session by tmux name.
                name = row.get("session_id") or row.get("tmux_session")
                if not name:
                    continue
                address = _address(name, label)
                out.append({**row, "session_id": address, "tmux_session": address,
                            "remote_tmux_name": name, **machine,
                            "machine_reachable": True, "machine_state": "reachable",
                            "machine_unreachable_since": None})
            return out
        since = last.get(pub)
        return [{
            "session_id": _address(r["tmux_name"], label),
            "tmux_session": _address(r["tmux_name"], label),
            "remote_tmux_name": r["tmux_name"],
            "project": r.get("project") or "",
            "type": r.get("type") or "container",
            "label": r.get("label") or "",
            "role": r.get("role") or "",
            "harness": r.get("harness"),
            "model": r.get("model"),
            "is_live": True,
            "created_at": r.get("since"),
            **machine,
            "machine_reachable": False,
            "machine_state": machine_state(reply)["state"],
            "machine_not_enabled_reason": machine_state(reply)["reason"],
            "machine_unreachable_since": since,
        } for r in presence if r["machine_pub"] == pub]

    results = await asyncio.gather(*(rows_for(pub) for pub in others))
    return [row for rows in results for row in rows]
