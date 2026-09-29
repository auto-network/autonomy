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
    mirror._labels = {SJC: "sjc-2"}
    return mirror, bus


def test_the_list_starts_from_presence_and_each_machines_rows_replace_its_part():
    mirror, bus = _mirror()
    mirror._seed([{"tmux_name": "auto-9", "machine_pub": SJC, "label": "Sweep",
                   "state": "ACTIVE", "since": 5}])
    (topic, data), = bus.events
    assert topic == remote_sessions.REMOTE_REGISTRY_TOPIC
    (row,) = data["machines"]["sjc-2"]
    assert (row["session_id"], row["label"], row["machine_reachable"]) == (
        "auto-9@sjc-2", "Sweep", False)

    async def run():
        await mirror._apply({"machine_pub": SJC, "subscribed": True}, "p")
        await mirror._apply({"machine_pub": SJC, "event": {"topic": "session:registry",
            "data": [{"session_id": "auto-7", "startup_state": "harness_starting"}]}}, "p")

    asyncio.run(run())
    rows = bus.events[-1][1]["machines"]["sjc-2"]
    assert [(r["session_id"], r["startup_state"], r["machine_reachable"]) for r in rows] == [
        ("auto-7@sjc-2", "harness_starting", True)]


def test_messages_and_endings_are_republished_under_the_address():
    mirror, bus = _mirror()

    async def run():
        await mirror._apply({"machine_pub": SJC, "event": {"topic": "session:messages",
            "data": {"session_id": "auto-9", "entries": [{"type": "assistant"}],
                     "span": {"file": "f", "from": 0, "to": 9}}}}, "p")
        await mirror._apply({"machine_pub": SJC, "event": {"topic": "session:ended",
            "data": {"id": "auto-9", "tmux_session": "auto-9", "state": "FAILED"}}}, "p")

    asyncio.run(run())
    (t1, messages), (t2, _rows), (t3, ended) = bus.events
    assert t1 == "session:messages" and messages["session_id"] == "auto-9@sjc-2"
    assert messages["span"] == {"file": "f", "from": 0, "to": 9}
    assert t3 == "session:ended"
    assert (ended["id"], ended["tmux_session"], ended["state"]) == (
        "auto-9@sjc-2", "auto-9@sjc-2", "FAILED")


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
