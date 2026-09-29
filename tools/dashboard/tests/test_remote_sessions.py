"""remote_sessions: the host forwards its session events on a subscription,
and the subscriber republishes them addressed name@machine."""

from __future__ import annotations

import asyncio

from tools.dashboard import remote_sessions
from tools.dashboard.event_bus import EventBus

SJC = "b1" * 32


def test_the_host_forwards_only_session_topics_cut_to_card_fields():
    async def run():
        bus = EventBus()
        sent = []

        def control(op, args, **_kw):
            sent.append(args["record"])
            if len(sent) == 3:
                return {"ok": False, "error_kind": "subscription-not-found"}
            return {"ok": True}

        task = asyncio.create_task(remote_sessions.forward(bus, "sub", control=control))
        await asyncio.sleep(0)
        await bus.broadcast("setting.changed", {"set_id": "x"})
        await bus.broadcast("session:registry", [{"session_id": "auto-9", "label": "L",
                                                  "jsonl_path": "/secret"}])
        await bus.broadcast("worktrees", [{"repo": "r"}])
        await bus.broadcast("session:messages", {"session_id": "auto-9", "entries": [1]},
                            dedup=False)
        await bus.broadcast("session:ended", {"id": "auto-9"})
        await asyncio.wait_for(task, 5)     # ends when the subscription is gone
        assert [r["topic"] for r in sent] == [
            "session:registry", "session:messages", "session:ended"]
        assert sent[0]["data"] == [{"session_id": "auto-9", "label": "L"}]

    asyncio.run(run())


def test_the_host_serves_only_its_own_persona(monkeypatch):
    monkeypatch.setattr(remote_sessions, "_personal_persona", lambda: "p1")
    monkeypatch.setattr(remote_sessions, "forward", lambda *a, **k: asyncio.sleep(0))
    op = remote_sessions.subscribe_op(EventBus())

    async def run():
        assert (await op({"sub_id": "s", "persona": "p2"}, SJC))["refusal"] == \
            remote_sessions.SUBSCRIBE_PERSONA_REFUSED
        assert (await op({"persona": "p1"}, SJC))["refusal"] == \
            remote_sessions.SUBSCRIBE_MALFORMED
        assert (await op({"sub_id": "s", "persona": "p1"}, SJC))["ok"] is True

    asyncio.run(run())


class Bus:
    def __init__(self):
        self.events = []

    async def broadcast(self, topic, data, dedup=True):
        self.events.append((topic, data))

    def broadcast_sync(self, topic, data, dedup=True):
        self.events.append((topic, data))


def _mirror():
    bus = Bus()
    mirror = remote_sessions.Mirror(bus, control=lambda *a, **k: {"ok": True})
    mirror._names = {SJC: "sjc-2"}
    return mirror, bus


def test_forwarded_rows_are_keyed_by_machine_pub_and_named_for_display():
    mirror, bus = _mirror()

    async def run():
        await mirror._apply({"machine_pub": SJC, "subscribed": True}, "p")
        await mirror._apply({"machine_pub": SJC, "event": {"topic": "session:registry",
            "data": [{"session_id": "auto-7", "startup_state": "harness_starting"}]}}, "p")

    asyncio.run(run())
    (topic, data), = bus.events
    assert topic == remote_sessions.REMOTE_ROWS_TOPIC
    (row,) = data["rows"]
    assert (row["session_id"], row["machine"], row["machine_pub"], row["startup_state"]) == (
        f"auto-7@{SJC}", "sjc-2", SJC, "harness_starting")
    assert mirror.connected(SJC)


def test_messages_and_endings_are_republished_under_the_key():
    mirror, bus = _mirror()

    async def run():
        await mirror._apply({"machine_pub": SJC, "event": {"topic": "session:messages",
            "data": {"session_id": "auto-9", "entries": [{"type": "assistant"}],
                     "span": {"file": "f", "from": 0, "to": 9}}}}, "p")
        await mirror._apply({"machine_pub": SJC, "event": {"topic": "session:ended",
            "data": {"id": "auto-9", "tmux_session": "auto-9", "state": "FAILED"}}}, "p")

    asyncio.run(run())
    (t1, messages), (t2, ended) = bus.events
    assert t1 == "session:messages" and messages["session_id"] == f"auto-9@{SJC}"
    assert messages["span"] == {"file": "f", "from": 0, "to": 9}
    assert t2 == "session:ended"
    assert (ended["id"], ended["tmux_session"], ended["state"], ended["machine"]) == (
        f"auto-9@{SJC}", f"auto-9@{SJC}", "FAILED", "sjc-2")


def test_an_ended_subscription_this_machine_serves_stops_its_forwarder():
    mirror, _bus = _mirror()

    async def run():
        task = asyncio.create_task(asyncio.sleep(60))
        remote_sessions._forwarders["s1"] = task
        await mirror._apply({"ended_sub_id": "s1"}, "p")
        await asyncio.sleep(0)
        assert task.cancelled() and "s1" not in remote_sessions._forwarders

    asyncio.run(run())


def test_a_lost_subscription_resubscribes_and_a_refused_one_does_not(monkeypatch):
    mirror, _bus = _mirror()
    again = []

    async def later(pub, persona):
        again.append(pub)

    monkeypatch.setattr(mirror, "_resubscribe_later", later)

    async def run():
        await mirror._apply({"machine_pub": SJC, "end": "subscription-lost",
                             "refused": False}, "p")
        await mirror._apply({"machine_pub": SJC, "end": "peer-not-in-roster",
                             "refused": True}, "p")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert again == [SJC]


def test_presence_rows_are_keyed_by_machine_pub(monkeypatch):
    from tools.dashboard import fleet_machines, session_presence

    monkeypatch.setattr(session_presence, "read_presence", lambda: [
        {"tmux_name": "auto-9", "machine_pub": SJC, "machine": "sjc-2", "local": False,
         "reachable": True, "label": "Sweep", "state": "ACTIVE", "since": 5},
        {"tmux_name": "auto-1", "machine_pub": "a1" * 32, "machine": "home", "local": True}])
    (row,) = fleet_machines.presence_rows()
    assert (row["session_id"], row["machine"], row["machine_pub"], row["label"]) == (
        f"auto-9@{SJC}", "sjc-2", SJC, "Sweep")
