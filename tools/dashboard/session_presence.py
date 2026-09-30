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
from tools.graph.schemas.org_session_presence import (
    ORG_SESSION_PRESENCE_REVISION,
    ORG_SESSION_PRESENCE_SET_ID,
    OrgSessionPresenceV1,
)
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


@dataclass(frozen=True)
class OrgSink:
    """One organization this machine writes its roster rows into
    (auto-qrmlg.8): the org's slug, this machine's serving machine key for
    that org (the row key's prefix, as the org channel presents it), and
    the member persona the rows name. Rows written into an organization
    store are signed by that member's delegate and verified on arrival
    (signed settings), so the persona is also the row's signer."""

    org: str
    machine_pub: str
    persona_pub: str


def org_sinks() -> list[OrgSink]:
    """The organizations this process holds a fleet:sync certificate for
    (org_sync_channels.report): the certificate names the serving machine
    key and the persona. Empty before sign-on installs them."""
    try:
        from tools.dashboard import org_sync_channels

        report = org_sync_channels.report()
    except Exception:
        logger.debug("session_presence: org channels unavailable", exc_info=True)
        return []
    out = []
    for slug, entry in sorted(report.items()):
        cert = entry.get("certificate") if isinstance(entry, dict) else None
        if not cert or not cert.get("child_pub") or not cert.get("persona"):
            continue
        out.append(OrgSink(str(slug), str(cert["child_pub"]), str(cert["persona"])))
    return out


def session_org(session: dict) -> str | None:
    """The organization slug a live session belongs to, or None when it is
    personal or unknown: only that organization's roster lists it."""
    try:
        from tools.dashboard.org_identity import UNKNOWN_SLUG, session_org_slug

        slug = session_org_slug(session)
    except Exception:
        return None
    if not slug or slug in ("personal", "machine") or slug == UNKNOWN_SLUG:
        return None
    return str(slug)


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


def desired_rows(machine_id: str, live: list[dict], *, sink: OrgSink | None = None) -> dict[str, dict]:
    """tmux_name -> payload for each live interactive session; with *sink*,
    only the sessions of that organization, each naming the member persona."""
    rows: dict[str, dict] = {}
    for session in live:
        tmux_name = session.get("tmux_name")
        if not is_tmux_name(tmux_name):
            continue
        if sink is not None and session_org(session) != sink.org:
            continue
        payload: dict[str, Any] = {
            "machine_id": machine_id,
            "state": session.get("state") or "ACTIVE",
            "since": int(float(session.get("created_at") or 0)),
        }
        if sink is not None:
            payload["persona_pub"] = sink.persona_pub
        for name in _COPIED:
            value = session.get(name)
            if isinstance(value, str) and value:
                payload[name] = value
        for name in ("launched_by", "home_machine"):
            value = session.get(name)
            if isinstance(value, str) and value:
                payload[name] = value
        try:
            (OrgSessionPresenceV1 if sink is not None else PersonalSessionPresenceV1).validate(payload)
        except Exception:
            logger.warning(
                "session_presence: skipping %s, payload invalid", tmux_name,
                exc_info=True,
            )
            continue
        rows[tmux_name] = payload
    return rows


@dataclass(frozen=True)
class _OwnRow:
    id: str
    key: str
    payload: dict


def _members(prefix: str | None = None, *, org: str = _ORG):
    """The live base rows this writer reconciles against. The personal set
    is read through resolution as before. An organization set is read RAW
    (base rows, not deprecated, under the key prefix): they are this
    machine's own statements and the writer must see them whatever the
    store's resolution says of them — resolution drops a signed row whose
    persona is not in the fold, and a store that has not folded the
    membership yet would otherwise make the writer rewrite its rows every
    pass."""
    if org == _ORG:
        return [
            member for member in settings_ops.read_owned_set(
                PERSONAL_SESSION_PRESENCE_SET_ID, org=_ORG,
                target_revision=PERSONAL_SESSION_PRESENCE_REVISION, prefix=prefix,
            ).members
            if not member.deprecated
        ]
    import json as _json

    db = settings_ops._open_read(org, ORG_SESSION_PRESENCE_SET_ID)
    try:
        rows = db.conn.execute(
            "SELECT id, key, payload FROM settings WHERE set_id=? AND schema_revision=? "
            "AND deprecated=0 AND supersedes IS NULL AND excludes IS NULL AND key LIKE ?",
            (ORG_SESSION_PRESENCE_SET_ID, ORG_SESSION_PRESENCE_REVISION, (prefix or "") + ":%"),
        ).fetchall()
    finally:
        db.close()
    out = []
    for row_id, key, payload in rows:
        try:
            value = _json.loads(payload) if isinstance(payload, str) else dict(payload)
        except (ValueError, TypeError):
            continue
        out.append(_OwnRow(str(row_id), str(key), value))
    return out


def reconcile(machine: LocalMachine, live: list[dict], *, sink: OrgSink | None = None) -> dict[str, int]:
    """Make this machine's rows equal *live*; write only differences. With
    *sink*, the rows are that organization's, under the org serving machine
    key, and hold only that organization's sessions."""
    org = sink.org if sink is not None else _ORG
    key_pub = sink.machine_pub if sink is not None else machine.machine_pub
    set_id, revision = (
        (ORG_SESSION_PRESENCE_SET_ID, ORG_SESSION_PRESENCE_REVISION) if sink is not None
        else (PERSONAL_SESSION_PRESENCE_SET_ID, PERSONAL_SESSION_PRESENCE_REVISION)
    )
    wanted = desired_rows(machine.machine_id, live, sink=sink)
    stored = {}
    # read_set appends the ":" separator itself (_prefix_like_pattern).
    for member in _members(prefix=key_pub, org=org):
        parts = split_key(member.key)
        if parts is None or parts[0] != key_pub:
            continue
        stored[parts[1]] = member
    upserted = deprecated = 0
    for tmux_name, payload in wanted.items():
        member = stored.get(tmux_name)
        if member is not None and member.payload == payload:
            continue
        settings_ops.upsert_by_key(
            set_id, revision, presence_key(key_pub, tmux_name), payload, org=org,
        )
        upserted += 1
    for tmux_name, member in stored.items():
        if tmux_name in wanted:
            continue
        settings_ops.deprecate_setting(member.id, org=org)
        deprecated += 1
    return {"upserted": upserted, "deprecated": deprecated}


def reconcile_local() -> dict[str, int] | None:
    """Reconcile from the dashboard's live roster into the personal sink
    and into every organization sink this process holds a channel for;
    None before enrollment. Counts are summed; an organization sink that
    fails (its store refused the write, its delegate is not held) is
    logged and does not stop the others."""
    from tools.dashboard.dao import dashboard_db

    machine = local_machine()
    if machine is None:
        return None
    live = dashboard_db.get_live_sessions()
    total = reconcile(machine, live)
    for sink in org_sinks():
        try:
            result = reconcile(machine, live, sink=sink)
        except Exception:
            logger.warning("session_presence: organization %r roster reconcile failed", sink.org, exc_info=True)
            continue
        total = {k: total[k] + result[k] for k in total}
    return total


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


# ── the organization roster's reader ────────────────────────────────────────


def _org_relay_slots() -> dict[str, set[str]]:
    """org slug -> serving machine pubs holding a LIVE slot at the org's
    relay right now (org_sync_channels.relay_slots_provider): the only live
    presence signal a member has about a co-member's machine."""
    try:
        from tools.dashboard import org_sync_channels

        slots = org_sync_channels.relay_slots_provider()()
    except Exception:
        logger.debug("session_presence: relay slots unavailable", exc_info=True)
        return {}
    out: dict[str, set[str]] = {}
    for slug, entries in (slots or {}).items():
        out[str(slug)] = {
            str(e.get("machine")) for e in (entries or ()) if isinstance(e, dict) and e.get("machine")
        }
    return out


def _org_peer_last_success_s(org: str) -> dict[str, int]:
    """machine_pub -> unix seconds of the newest successful pull of *org*'s
    scope from it."""
    try:
        from tools.graph.db import _org_db_path
        from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

        raw = SQLiteFleetSyncStore(_org_db_path(org)).peer_last_success()
    except Exception:
        logger.debug("session_presence: no peer state for %r", org, exc_info=True)
        return {}
    return {pub: int(ns // 1_000_000_000) for pub, ns in raw.items() if ns}


def _machine_personas(org: str) -> dict[str, str]:
    """serving machine_pub -> member persona for *org*, from what binds the
    two: this node's own org channel certificates, the org's live relay
    serving slots ({persona_pub, machine}), and the VERIFIED reachability
    rows replicated into the org store (a persona certificate over the
    machine key, verified by fleet_org_reachability.verify_row). A machine
    no binding names is unknown here and its rows do not count."""
    out: dict[str, str] = {}
    for sink in org_sinks():
        if sink.org == org:
            out[sink.machine_pub] = sink.persona_pub
    try:
        from tools.dashboard import org_sync_channels

        for entry in (org_sync_channels.relay_slots_provider()() or {}).get(org, ()) or ():
            if isinstance(entry, dict) and entry.get("machine") and entry.get("persona_pub"):
                out.setdefault(str(entry["machine"]), str(entry["persona_pub"]))
        genesis = org_sync_channels._genesis_id(org)
    except Exception:
        logger.debug("session_presence: org channel state unavailable for %r", org, exc_info=True)
        genesis = None
    if genesis:
        try:
            from tools.graph.db import _org_db_path
            from tools.network.fleet_org_reachability import read_rows, verify_row

            for machine_pub, payload in read_rows(_org_db_path(org)).items():
                verified = verify_row(machine_pub, payload, org=genesis, now=int(time.time()))
                if verified is not None:
                    out.setdefault(str(machine_pub), str(verified[0]))
        except Exception:
            logger.debug("session_presence: reachability rows unavailable for %r", org, exc_info=True)
    return out


def _org_rows(org: str) -> list[dict]:
    """Live base rows of the org roster with the persona each was SIGNED
    for: the boundary verified the signer on arrival and the store keeps
    that persona on the row (terminal_persona), so a row whose payload
    names another persona than its signer is not that member's statement
    and is dropped here."""
    from tools.graph.schemas.org_session_presence import ORG_SESSION_PRESENCE_SET_ID as SET_ID

    db = settings_ops._open_read(org, SET_ID)
    try:
        rows = db.conn.execute(
            "SELECT id, key, payload, terminal_persona FROM settings WHERE set_id=? "
            "AND schema_revision=? AND deprecated=0 AND supersedes IS NULL AND excludes IS NULL",
            (SET_ID, ORG_SESSION_PRESENCE_REVISION),
        ).fetchall()
    finally:
        db.close()
    import json as _json

    out = []
    for row in rows:
        payload = row[2]
        try:
            payload = _json.loads(payload) if isinstance(payload, str) else dict(payload)
        except (ValueError, TypeError):
            continue
        signer = row[3]
        # An unsigned row has no member behind it (a row that landed before
        # the require-signed flag, or a store where it is off) and could
        # name anyone: it does not count.
        if signer is None or str(payload.get("persona_pub")) != str(signer):
            logger.debug("session_presence: dropping %s: names %s, signed for %s",
                         row[1], str(payload.get("persona_pub"))[:12], str(signer or "")[:12])
            continue
        out.append({"id": row[0], "key": row[1], "payload": payload, "signer": signer})
    return out


def read_org_presence(
    org: str,
    *,
    local_pubs: set[str] | None = None,
    relay_slots: set[str] | None = None,
    peer_last_success: dict[str, int] | None = None,
    names: dict[str, str] | None = None,
    machine_personas: dict[str, str] | None = None,
    now: float | None = None,
) -> list[dict]:
    """Every member machine's roster rows for *org*, each with ``org``,
    ``persona_pub``, ``machine`` and ``reachable``.

    A row counts only when it is signed, its payload names its signer, and
    the machine in its key is bound to that signer (``machine_personas``:
    this node's own channels, the live relay slots, the verified
    reachability rows). A member cannot list a session under another
    member's persona, nor under another member's machine.

    A row is live when it is this machine's own, or its machine holds a
    live serving slot at the org's relay, or that machine was pulled from
    within REACHABLE_WINDOW_S; otherwise ``unreachable_since`` carries the
    last successful pull (None: never). A powered-off machine's rows are
    therefore never shown live.
    """
    if local_pubs is None:
        local_pubs = {sink.machine_pub for sink in org_sinks() if sink.org == org}
    if relay_slots is None:
        relay_slots = _org_relay_slots().get(org, set())
    last = _org_peer_last_success_s(org) if peer_last_success is None else peer_last_success
    names = _machine_names() if names is None else names
    bindings = _machine_personas(org) if machine_personas is None else machine_personas
    current = time.time() if now is None else now
    out = []
    for entry in _org_rows(org):
        parts = split_key(entry["key"])
        if parts is None:
            continue
        machine_pub, tmux_name = parts
        payload = dict(entry["payload"])
        if bindings.get(machine_pub) != entry["signer"]:
            logger.debug("session_presence: dropping %s: machine %s is not the signer's (%s)",
                         entry["key"], machine_pub[:12], str(entry["signer"])[:12])
            continue
        own = machine_pub in local_pubs
        seen = last.get(machine_pub)
        reachable = own or machine_pub in relay_slots or (
            seen is not None and current - seen <= REACHABLE_WINDOW_S
        )
        machine_id = payload.get("machine_id") or ""
        out.append({
            **payload,
            "org": org,
            "tmux_name": tmux_name,
            "machine_pub": machine_pub,
            "machine": names.get(machine_id) or machine_pub[:12],
            "local": own,
            "reachable": reachable,
            "unreachable_since": None if reachable else seen,
        })
    return out


def org_status_rows(**kwargs) -> list[dict]:
    """Co-members' sessions across every organization this machine holds a
    channel for, shaped for ``graph sessions --status``: ``tmux_name`` is
    ``<name>@<machine>``, ``org`` names the organization, an unreachable
    row's state reads ``unreach``."""
    rows = []
    for org in sorted({sink.org for sink in org_sinks()}):
        try:
            entries = read_org_presence(org, **kwargs)
        except Exception:
            logger.warning("session_presence: organization %r roster read failed", org, exc_info=True)
            continue
        for row in entries:
            if row["local"]:
                continue
            rows.append({
                "tmux_name": f"{row['tmux_name']}@{row['machine']}",
                "org": org,
                "persona_pub": row.get("persona_pub"),
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


def wake() -> None:
    """Ask the running writer for a reconcile pass (org channels installed
    at sign-on: the org sinks exist only from then)."""
    writer = _WRITER
    if writer is not None:
        writer.request()


_WRITER = None


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
        self._loop = None

    def start(self) -> None:
        global _WRITER
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())
            self._task.add_done_callback(self._on_done)
            _WRITER = self

    def request(self) -> None:
        """Schedule a reconcile pass from any thread."""
        try:
            self._loop.call_soon_threadsafe(self._wake.set)
        except Exception:
            logger.debug("session_presence: wake dropped", exc_info=True)

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
        self._loop = asyncio.get_running_loop()
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
