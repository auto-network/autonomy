"""Session presence: each machine's live sessions, visible to its whole fleet.

The writer keeps ``autonomy.personal.session-presence`` equal to this
machine's live interactive sessions (graph://7eb29bc8-31a §9.5, bead
auto-jh50w). It runs only when the dashboard says the roster changed
(``session:registry`` / ``session:ended`` on the event bus) and at startup,
and it writes only differences: a new or changed session is upserted, an ended
one is deprecated. Nothing is written on a quiet machine; a row is never a
heartbeat.

The reader returns every machine's rows with a liveness verdict: this
machine's own rows are live; another machine's rows are live only while that
machine has been pulled from within :data:`REACHABLE_WINDOW_S`, otherwise they
read as unreachable since the last successful pull. A crashed machine's rows
therefore stay in every copy, marked unreachable, until it returns and its own
startup reconcile deprecates them.

The organization-homed sink (auto-qrmlg.8) is meant to reuse this writer.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.personal_session_presence import (
    PERSONAL_SESSION_PRESENCE_REVISION,
    PERSONAL_SESSION_PRESENCE_SET_ID,
    PersonalSessionPresenceV1,
    is_tmux_name,
    presence_key,
    split_key,
)

logger = logging.getLogger(__name__)

_ORG = "personal"
#: A peer pulled from within this window counts as reachable. Sync rounds run
#: every 10 s and a two-machine fleet pulls its peer every round (measured
#: 22 s per round, graph://17466ea3-b02), so 60 s is about three rounds.
REACHABLE_WINDOW_S = 60
#: Second reconcile after startup: two liveness ticks (10 s each, two misses
#: before mark_dead) catch sessions that ended while the dashboard was down.
POST_LIVENESS_RECONCILE_S = 25
_TOPICS = ("session:registry", "session:ended")
#: Row fields copied from tmux_sessions when present.
_COPIED = ("label", "project", "type", "harness", "model", "role")


@dataclass(frozen=True)
class LocalMachine:
    machine_pub: str
    machine_id: str


def local_machine() -> LocalMachine | None:
    """This machine's durable roster identity, or None before enrollment."""
    from tools.dashboard import fleet_enrollment_routes

    try:
        context = fleet_enrollment_routes._runtime_context()
    except Exception:
        logger.debug("session_presence: no roster identity", exc_info=True)
        return None
    if context is None:
        return None
    _root_pub, entry = context
    return LocalMachine(entry.machine_pub, entry.machine_id)


def desired_rows(machine_id: str, live: list[dict]) -> dict[str, dict]:
    """tmux_name -> payload for each live interactive session."""
    rows: dict[str, dict] = {}
    for session in live:
        tmux_name = session.get("tmux_name")
        if not is_tmux_name(tmux_name):
            continue
        payload: dict[str, Any] = {
            "machine_id": machine_id,
            "state": session.get("state") or "ACTIVE",
            "since": int(float(session.get("created_at") or 0)),
        }
        for name in _COPIED:
            value = session.get(name)
            if isinstance(value, str) and value:
                payload[name] = value
        for name in ("launched_by", "home_machine"):
            value = session.get(name)
            if isinstance(value, str) and value:
                payload[name] = value
        try:
            PersonalSessionPresenceV1.validate(payload)
        except Exception:
            logger.warning(
                "session_presence: skipping %s, payload invalid", tmux_name,
                exc_info=True,
            )
            continue
        rows[tmux_name] = payload
    return rows


def _members(prefix: str | None = None):
    return [
        member for member in settings_ops.read_owned_set(
            PERSONAL_SESSION_PRESENCE_SET_ID,
            org=_ORG,
            target_revision=PERSONAL_SESSION_PRESENCE_REVISION,
            prefix=prefix,
        ).members
        if not member.deprecated
    ]


def reconcile(machine: LocalMachine, live: list[dict]) -> dict[str, int]:
    """Make this machine's rows equal *live*; write only differences."""
    wanted = desired_rows(machine.machine_id, live)
    stored = {}
    # read_set appends the ":" separator itself (_prefix_like_pattern).
    for member in _members(prefix=machine.machine_pub):
        parts = split_key(member.key)
        if parts is None or parts[0] != machine.machine_pub:
            continue
        stored[parts[1]] = member
    upserted = deprecated = 0
    for tmux_name, payload in wanted.items():
        member = stored.get(tmux_name)
        if member is not None and member.payload == payload:
            continue
        settings_ops.upsert_by_key(
            PERSONAL_SESSION_PRESENCE_SET_ID,
            PERSONAL_SESSION_PRESENCE_REVISION,
            presence_key(machine.machine_pub, tmux_name),
            payload,
            org=_ORG,
        )
        upserted += 1
    for tmux_name, member in stored.items():
        if tmux_name in wanted:
            continue
        settings_ops.deprecate_setting(member.id, org=_ORG)
        deprecated += 1
    return {"upserted": upserted, "deprecated": deprecated}


def reconcile_local() -> dict[str, int] | None:
    """Reconcile from the dashboard's live roster; None before enrollment."""
    from tools.dashboard.dao import dashboard_db

    machine = local_machine()
    if machine is None:
        return None
    return reconcile(machine, dashboard_db.get_live_sessions())


# ── reader ──────────────────────────────────────────────────────────────────


def _peer_last_success_s() -> dict[str, int]:
    """machine_pub -> unix seconds of the newest successful pull from it."""
    try:
        from tools.dashboard import fleet_enrollment_routes
        from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

        path = fleet_enrollment_routes._org_db_path(_ORG)
        raw = SQLiteFleetSyncStore(path).peer_last_success()
    except Exception:
        logger.debug("session_presence: no peer state", exc_info=True)
        return {}
    return {pub: int(ns // 1_000_000_000) for pub, ns in raw.items() if ns}


def _active_roster() -> dict[str, str] | None:
    """machine_pub -> machine_id of every ACTIVE roster machine, or None when
    this dashboard has no fleet identity to resolve the roster against."""
    try:
        from tools.network import fleet_roster, fleet_tunnel_server

        root_pub = fleet_tunnel_server._personal_root_pub()
        if root_pub is None:
            return None
        active = fleet_roster.resolve(
            fleet_roster.load_entries(org=None), anchor_root_pub=root_pub)
    except Exception:
        logger.debug("session_presence: roster unavailable", exc_info=True)
        return None
    return {pub: entry.machine_id for pub, entry in active.items()}


def _machine_names() -> dict[str, str]:
    try:
        from tools.network import fleet_machine_profile

        return fleet_machine_profile.names()
    except Exception:
        return {}


def read_presence(
    *,
    local_pub: str | None = None,
    peer_last_success: dict[str, int] | None = None,
    names: dict[str, str] | None = None,
    roster: dict[str, str] | None = None,
    now: float | None = None,
) -> list[dict]:
    """Every machine's presence rows, each with ``machine`` and ``reachable``.

    ``reachable`` is True for this machine's own rows, and for another
    machine's rows while it was pulled from within REACHABLE_WINDOW_S;
    otherwise ``unreachable_since`` carries the last successful pull (or None
    when it was never pulled).

    The set is unsigned, so a row is kept only when its key names an ACTIVE
    roster machine and its payload's ``machine_id`` is that machine's: a row
    one machine wrote under another's key, or for a machine no longer in the
    roster, is dropped. ``name@machine`` is an address later session control
    routes by, so the roster, never the row, decides which machine it names.
    """
    if local_pub is None:
        machine = local_machine()
        local_pub = machine.machine_pub if machine else None
    last = _peer_last_success_s() if peer_last_success is None else peer_last_success
    names = _machine_names() if names is None else names
    roster = _active_roster() if roster is None else roster
    if roster is None:
        return []
    current = time.time() if now is None else now
    out = []
    for member in _members():
        parts = split_key(member.key)
        if parts is None:
            continue
        machine_pub, tmux_name = parts
        payload = dict(member.payload)
        machine_id = payload.get("machine_id", "")
        if roster.get(machine_pub) != machine_id:
            logger.debug(
                "session_presence: dropping %s: key and machine_id do not "
                "name one active roster machine", member.key)
            continue
        seen = last.get(machine_pub)
        own = machine_pub == local_pub
        reachable = own or (seen is not None and current - seen <= REACHABLE_WINDOW_S)
        out.append({
            **payload,
            "tmux_name": tmux_name,
            "machine_pub": machine_pub,
            "machine": names.get(machine_id) or machine_pub[:12],
            "local": own,
            "reachable": reachable,
            "unreachable_since": None if reachable else seen,
        })
    return out


def remote_status_rows(**kwargs) -> list[dict]:
    """Presence rows of OTHER machines shaped for ``graph sessions --status``.

    ``tmux_name`` is the ``<name>@<machine>`` address; an unreachable row's
    state reads ``unreach``.
    """
    rows = []
    for row in read_presence(**kwargs):
        if row["local"]:
            continue
        rows.append({
            "tmux_name": f"{row['tmux_name']}@{row['machine']}",
            "state": row.get("state"),
            "attention": "remote" if row["reachable"] else "unreach",
            "created_at": row.get("since"),
            "last_activity": row.get("unreachable_since") or row.get("since"),
            "label": row.get("label"),
            "machine": row["machine"],
            "machine_pub": row["machine_pub"],
            "remote": True,
            "reachable": row["reachable"],
        })
    return rows


# ── the event-driven writer ─────────────────────────────────────────────────


class PresenceWriter:
    """Coalescing reconcile, triggered by roster events, run off the loop."""

    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())
            self._task.add_done_callback(self._on_done)

    @staticmethod
    def _on_done(task: asyncio.Task) -> None:
        # A dead writer leaves this machine's rows stale while its peers
        # still see it as reachable; say so loudly.
        if task.cancelled():
            return
        exc = task.exception()
        logger.warning(
            "session_presence: writer stopped%s; this machine's presence "
            "rows are no longer maintained",
            f" ({exc!r})" if exc else "",
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _reconcile(self, why: str) -> None:
        def _once():
            with self._lock:
                return reconcile_local()

        try:
            result = await asyncio.to_thread(_once)
        except Exception:
            logger.warning("session_presence: reconcile (%s) failed", why, exc_info=True)
            return
        if result and (result["upserted"] or result["deprecated"]):
            logger.info("session_presence: %s -> %s", why, result)

    async def _listen(self) -> None:
        queue = self._bus.subscribe(client_id="session-presence")
        try:
            while True:
                topic, _data, _seq = await queue.get()
                if topic in _TOPICS:
                    self._wake.set()
        finally:
            self._bus.unsubscribe(queue)

    async def _run(self) -> None:
        listener = asyncio.create_task(self._listen())
        try:
            await self._reconcile("startup")
            loop = asyncio.get_running_loop()
            loop.call_later(POST_LIVENESS_RECONCILE_S, self._wake.set)
            while True:
                await self._wake.wait()
                self._wake.clear()
                # Coalesce a burst of registry broadcasts into one pass.
                await asyncio.sleep(1.0)
                self._wake.clear()
                await self._reconcile("event")
        finally:
            listener.cancel()
