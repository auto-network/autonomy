"""Live session events between the operator's fleet machines.

Each dashboard holds ONE subscription channel to every other active roster
machine, opened at startup with the operator's persona and kept open
(session-control ``subscribe``, tools/network/session_control.py). Over it:

* HOST side (:func:`subscribe_op`): the machine running sessions forwards each
  event of its own bus about the persona's sessions as it happens --
  ``session:registry`` (rows cut to SESSIONS_ROW_FIELDS), ``session:messages``
  and ``session:ended``. Nothing else crosses. There is no snapshot.
* SUBSCRIBER side (:class:`Subscriptions`): rewrites each session to
  ``name@machine_pub`` -- the machine's key, never its display name -- and
  republishes on this machine's bus: registry rows as
  ``session:remote-rows``, messages and endings as themselves. Each updates
  only the sessions it names. Which sessions exist is the synced presence
  Settings' to say (``GET /api/sessions/presence``); nothing here keeps a
  list of them.

A personal fleet has one persona -- the operator's root -- so a host serves
every session it runs. Org personas wait for org admission on the channel.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

TOPICS = ("session:registry", "session:messages", "session:ended")
REMOTE_ROWS_TOPIC = "session:remote-rows"
#: The event proxy's bound: a bus queue this far behind drops events.
MAX_PENDING = 256
#: ctl long-poll for received events.
COLLECT_WAIT_S = 20.0
RESUBSCRIBE_INITIAL_S = 2.0
RESUBSCRIBE_MAX_S = 60.0

SUBSCRIBE_PERSONA_REFUSED = "subscribe-persona-refused"
SUBSCRIBE_MALFORMED = "subscribe-malformed"


def _control(op: str, args: dict, *, timeout: float = 10.0) -> dict:
    from tools.dashboard import session_control_client

    return session_control_client._control(op, args, timeout=timeout)


def _personal_persona() -> str | None:
    from tools.network import fleet_tunnel_server

    return fleet_tunnel_server._personal_root_pub()


# ── host side ────────────────────────────────────────────────────────────────


def _project(topic: str, data):
    """What crosses for one event, or None to drop it."""
    from tools.dashboard.session_control_client import SESSIONS_ROW_FIELDS

    if topic == "session:registry":
        if not isinstance(data, list):
            return None
        return [{k: row[k] for k in SESSIONS_ROW_FIELDS if k in row}
                for row in data if isinstance(row, dict)]
    return data if isinstance(data, dict) else None


async def forward(bus, sub_id: str, *, control=None) -> None:
    """Publish this machine's session events on subscription *sub_id* until
    the connector says it has ended."""
    control = control or _control
    queue = bus.subscribe()
    try:
        while True:
            topic, data, seq = await queue.get()
            if topic not in TOPICS or seq == 0:
                continue    # seq 0 is the bus's cached replay, not an event
            if queue.qsize() > MAX_PENDING:
                # Never drop an event: end the subscription, and the
                # subscriber subscribes again.
                await asyncio.to_thread(control, "session-control-end",
                                        {"sub_id": sub_id})
                return
            payload = _project(topic, data)
            if payload is None:
                continue
            reply = await asyncio.to_thread(
                control, "session-control-publish",
                {"sub_id": sub_id, "record": {"topic": topic, "data": payload}})
            if not reply.get("ok") and reply.get("error_kind") in (
                    "subscription-not-found", "subscription-behind"):
                return
    except Exception:
        logger.warning("remote sessions: forwarding on %s stopped", sub_id[:8],
                       exc_info=True)
    finally:
        bus.unsubscribe(queue)


#: sub_id -> the task forwarding this machine's events on that subscription.
_forwarders: dict = {}


def end_forwarder(sub_id: str) -> None:
    """The connector says subscription *sub_id* ended: stop forwarding now."""
    task = _forwarders.pop(sub_id, None)
    if task is not None:
        task.cancel()


def subscribe_op(bus):
    """The inbound ``subscribe`` op: the persona must be this machine's own
    (a personal fleet; the peer is already a roster machine of it, proved by
    the session:control handshake)."""
    from tools.dashboard import session_control_client as scc

    async def op(body: dict, peer: str) -> dict:
        sub_id, persona = body.get("sub_id"), body.get("persona")
        if not isinstance(sub_id, str) or not isinstance(persona, str):
            return scc.refusal(SUBSCRIBE_MALFORMED, "sub_id and persona are required")
        own = await asyncio.to_thread(_personal_persona)
        if own is None or persona != own:
            return scc.refusal(SUBSCRIBE_PERSONA_REFUSED,
                               "this machine serves only its own persona's sessions")
        task = asyncio.get_running_loop().create_task(forward(bus, sub_id))
        _forwarders[sub_id] = task
        task.add_done_callback(lambda _t: _forwarders.pop(sub_id, None))
        logger.info("remote sessions: %s subscribed (%s)", peer[:12], sub_id[:8])
        return scc.ok({"subscribed": True})

    return op


# ── subscriber side ──────────────────────────────────────────────────────────


def _address(name: str, machine_pub: str) -> str:
    return f"{name}@{machine_pub}"


class Subscriptions:
    """Holds this machine's subscriptions and republishes what they carry."""

    def __init__(self, bus, *, control=None):
        self._bus = bus
        self._control = control or _control
        self._names: dict[str, str] = {}      # machine_pub -> display name
        self._live: set[str] = set()          # machine_pubs with an open subscription
        self._retry: dict[str, float] = {}    # machine_pub -> next backoff
        self._task: asyncio.Task | None = None

    def connected(self, machine_pub: str | None) -> bool:
        return machine_pub in self._live

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await asyncio.to_thread(self._control, "session-control-close-subscriptions", {})

    async def _run(self) -> None:
        from tools.dashboard import fleet_machines

        local, roster, names, _last = await asyncio.to_thread(fleet_machines._context)
        persona = await asyncio.to_thread(_personal_persona)
        if local is None or persona is None:
            logger.info("remote sessions: no fleet identity; not subscribing")
            return
        self._names = {pub: fleet_machines._label(pub, roster, names)
                       for pub in roster if pub != local.machine_pub}
        # The connector holds the channels. Until it answers -- at startup,
        # and after it restarted, which ended every channel without a word --
        # nothing is subscribed; on its first answer, subscribe to everyone.
        stale = True
        while True:
            try:
                if stale:
                    # A connector outlives dashboard reloads: drop the channels
                    # an earlier dashboard left, both ways, before opening ours.
                    await asyncio.to_thread(
                        self._control, "session-control-close-subscriptions", {})
                    for pub in self._names:
                        await self._subscribe(pub, persona)
                    stale = False
                reply = await asyncio.to_thread(
                    self._control, "session-control-events",
                    {"wait": COLLECT_WAIT_S}, timeout=COLLECT_WAIT_S + 10.0)
            except Exception:
                if not stale:
                    logger.info("remote sessions: the connector is not answering; "
                                "resubscribing when it does")
                    self._live.clear()
                stale = True
                await asyncio.sleep(RESUBSCRIBE_INITIAL_S)
                continue
            if not reply.get("ok"):
                stale = True
                await asyncio.sleep(RESUBSCRIBE_INITIAL_S)
                continue
            for item in reply.get("items") or []:
                await self._apply(item, persona)

    async def _subscribe(self, pub: str, persona: str) -> None:
        await asyncio.to_thread(self._control, "session-control-subscribe",
                                {"machine_pub": pub, "persona": persona})

    async def _resubscribe_later(self, pub: str, persona: str) -> None:
        delay = self._retry.get(pub, RESUBSCRIBE_INITIAL_S)
        self._retry[pub] = min(delay * 2, RESUBSCRIBE_MAX_S)
        await asyncio.sleep(delay)
        await self._subscribe(pub, persona)

    async def _apply(self, item: dict, persona: str) -> None:
        if "ended_sub_id" in item:      # a subscription this machine serves
            end_forwarder(item["ended_sub_id"])
            return
        pub = item.get("machine_pub")
        if pub not in self._names:
            return
        if item.get("subscribed"):
            self._live.add(pub)
            self._retry.pop(pub, None)
            logger.info("remote sessions: subscribed to %s", self._names[pub])
            return
        if "end" in item:
            self._live.discard(pub)
            logger.info("remote sessions: subscription to %s ended: %s %s",
                        self._names[pub], item.get("end"), item.get("detail", ""))
            if not item.get("refused"):
                asyncio.get_running_loop().create_task(
                    self._resubscribe_later(pub, persona))
            return
        event = item.get("event") or {}
        topic, data = event.get("topic"), event.get("data")
        machine = {"machine": self._names[pub], "machine_pub": pub,
                   "machine_reachable": True}
        if topic == "session:registry" and isinstance(data, list):
            await self._bus.broadcast(REMOTE_ROWS_TOPIC, {"rows": [
                {**row, **machine, "session_id": _address(row["session_id"], pub)}
                for row in data if isinstance(row, dict) and row.get("session_id")]},
                dedup=False)
        elif topic == "session:messages" and isinstance(data, dict) and data.get("session_id"):
            from tools.dashboard import remote_view

            await self._bus.broadcast(
                "session:messages",
                remote_view.rewrite_identity(data, _address(data["session_id"], pub)),
                dedup=False)
        elif topic == "session:ended" and isinstance(data, dict) and data.get("id"):
            address = _address(data["id"], pub)
            await self._bus.broadcast("session:ended", {
                **data, "id": address, "tmux_session": address, **machine})
