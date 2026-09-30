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


def test_the_host_never_forwards_a_session_another_machine_runs():
    """A subscriber republishes a far machine's events on its own bus as
    name@machine. Forwarding those back bounced them between Home and SJC-2
    without end, each pass adding another @machine (auto-8b1bm)."""
    async def run():
        bus = EventBus()
        sent = []

        def control(op, args, **_kw):
            sent.append(args["record"])
            if args["record"]["data"] == {"id": "auto-9"}:
                return {"ok": False, "error_kind": "subscription-not-found"}
            return {"ok": True}

        task = asyncio.create_task(remote_sessions.forward(bus, "sub", control=control))
        await asyncio.sleep(0)
        await bus.broadcast("session:messages", {"session_id": f"auto-7@{SJC}",
                                                 "entries": [1]}, dedup=False)
        await bus.broadcast("session:ended", {"id": f"auto-7@{SJC}"})
        await bus.broadcast("session:registry", [{"session_id": f"auto-7@{SJC}"}])
        await bus.broadcast("session:registry", [{"session_id": f"auto-7@{SJC}"},
                                                 {"session_id": "auto-9"}])
        await bus.broadcast("session:messages", {"session_id": "auto-9"}, dedup=False)
        await bus.broadcast("session:ended", {"id": "auto-9"})
        await asyncio.wait_for(task, 5)
        assert sent == [
            {"topic": "session:registry", "data": [{"session_id": "auto-9"}]},
            {"topic": "session:messages", "data": {"session_id": "auto-9"}},
            {"topic": "session:ended", "data": {"id": "auto-9"}}]

    asyncio.run(run())


def test_a_republished_event_is_not_forwarded_back():
    """End to end on one bus: what the subscriber republishes from SJC-2 is
    not what the host forwards to SJC-2."""
    subs, _ = _subscriptions()

    async def run():
        bus = EventBus()
        subs._bus = bus
        sent = []

        def control(op, args, **_kw):
            sent.append(args["record"])
            return {"ok": False, "error_kind": "subscription-not-found"}

        task = asyncio.create_task(remote_sessions.forward(bus, "sub", control=control))
        await asyncio.sleep(0)
        await subs._apply({"machine_pub": SJC, "event": {"topic": "session:messages",
            "data": {"session_id": "auto-9", "entries": []}}}, "p")
        await bus.broadcast("session:messages", {"session_id": "auto-1"}, dedup=False)
        await asyncio.wait_for(task, 5)
        assert sent == [{"topic": "session:messages", "data": {"session_id": "auto-1"}}]

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


def _subscriptions():
    bus = Bus()
    subs = remote_sessions.Subscriptions(bus, control=lambda *a, **k: {"ok": True})
    subs._names = {SJC: "sjc-2"}
    return subs, bus


def test_forwarded_rows_are_keyed_by_machine_pub_and_named_for_display():
    subs, bus = _subscriptions()

    async def run():
        await subs._apply({"machine_pub": SJC, "subscribed": True}, "p")
        await subs._apply({"machine_pub": SJC, "event": {"topic": "session:registry",
            "data": [{"session_id": "auto-7", "startup_state": "harness_starting"}]}}, "p")

    asyncio.run(run())
    (topic, data), = [e for e in bus.events if e[0] == remote_sessions.REMOTE_ROWS_TOPIC]
    assert topic == remote_sessions.REMOTE_ROWS_TOPIC
    (row,) = data["rows"]
    assert (row["session_id"], row["machine"], row["machine_pub"], row["startup_state"]) == (
        f"auto-7@{SJC}", "sjc-2", SJC, "harness_starting")
    assert subs.connected(SJC)


def test_messages_and_endings_are_republished_under_the_key():
    subs, bus = _subscriptions()

    async def run():
        await subs._apply({"machine_pub": SJC, "event": {"topic": "session:messages",
            "data": {"session_id": "auto-9", "entries": [{"type": "assistant"}],
                     "span": {"file": "f", "from": 0, "to": 9}}}}, "p")
        await subs._apply({"machine_pub": SJC, "event": {"topic": "session:ended",
            "data": {"id": "auto-9", "tmux_session": "auto-9", "state": "FAILED"}}}, "p")

    asyncio.run(run())
    (t1, messages), (t2, ended) = bus.events
    assert t1 == "session:messages" and messages["session_id"] == f"auto-9@{SJC}"
    assert messages["span"] == {"file": "f", "from": 0, "to": 9}
    assert t2 == "session:ended"
    assert (ended["id"], ended["tmux_session"], ended["state"], ended["machine"]) == (
        f"auto-9@{SJC}", f"auto-9@{SJC}", "FAILED", "sjc-2")


def test_an_ended_subscription_this_machine_serves_stops_its_forwarder():
    subs, _bus = _subscriptions()

    async def run():
        task = asyncio.create_task(asyncio.sleep(60))
        remote_sessions._forwarders["s1"] = task
        await subs._apply({"ended_sub_id": "s1"}, "p")
        await asyncio.sleep(0)
        assert task.cancelled() and "s1" not in remote_sessions._forwarders

    asyncio.run(run())


def test_a_lost_subscription_resubscribes_and_a_refused_one_does_not(monkeypatch):
    subs, _bus = _subscriptions()
    again = []

    async def later(pub, persona):
        again.append(pub)

    monkeypatch.setattr(subs, "_resubscribe_later", later)

    async def run():
        await subs._apply({"machine_pub": SJC, "end": "subscription-lost",
                             "refused": False}, "p")
        await subs._apply({"machine_pub": SJC, "end": "peer-not-in-roster",
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



def test_a_forwarding_failure_ends_the_subscription_instead_of_leaving_it_open():
    """Live 2026-09-30 04:03:44Z on SJC-2: one control-socket error stopped the
    forwarder and left Home's subscription open with nothing feeding it."""
    async def run():
        bus = EventBus()
        calls = []

        def control(op, args, **_kw):
            calls.append(op)
            if op == "session-control-publish":
                raise RuntimeError("serving connector closed the control connection")
            return {"ok": True}

        task = asyncio.create_task(remote_sessions.forward(bus, "sub", control=control))
        await asyncio.sleep(0)
        await bus.broadcast("session:ended", {"id": "auto-9"})
        await asyncio.wait_for(task, 5)
        assert calls == ["session-control-publish", "session-control-end"]

    asyncio.run(run())


def test_a_refused_event_ends_the_subscription_rather_than_being_dropped():
    async def run():
        bus = EventBus()
        calls = []

        def control(op, args, **_kw):
            calls.append(op)
            return {"ok": False, "error_kind": "event-too-large"} \
                if op == "session-control-publish" else {"ok": True}

        task = asyncio.create_task(remote_sessions.forward(bus, "sub", control=control))
        await asyncio.sleep(0)
        await bus.broadcast("session:ended", {"id": "auto-9"})
        await asyncio.wait_for(task, 5)
        assert calls == ["session-control-publish", "session-control-end"]

    asyncio.run(run())


def test_each_machines_subscription_state_is_published_as_it_changes():
    subs, bus = _subscriptions()

    async def run():
        await subs._apply({"machine_pub": SJC, "subscribed": True}, "p")
        await subs._apply({"machine_pub": SJC, "end": "subscription-lost", "refused": True}, "p")

    asyncio.run(run())
    states = [d for t, d in bus.events if t == remote_sessions.REMOTE_MACHINES_TOPIC]
    assert states == [{SJC: True}, {SJC: False}]
