"""Dashboard half of session-control/1 (bead auto-99ioi): machine names
resolve only to ACTIVE roster machines, the inbound pump answers each
request through the op table, and an unknown op or a failing op is a typed
refusal."""

from __future__ import annotations

import asyncio

from tools.dashboard import session_control_client as scc

A, B = "a1" * 32, "b1" * 32
ROSTER = {A: "a2" * 32, B: "b2" * 32}
NAMES = {"a2" * 32: "home", "b2" * 32: "sjc-2"}


def test_a_name_resolves_to_its_roster_machine():
    assert scc.resolve_machine("sjc-2", roster=ROSTER, names=NAMES) == B
    assert scc.resolve_machine("SJC-2", roster=ROSTER, names=NAMES) == B
    assert scc.resolve_machine(B, roster=ROSTER, names=NAMES) == B
    assert scc.resolve_machine(B[:12], roster=ROSTER, names=NAMES) == B


def test_an_address_is_canonical_by_key_however_its_machine_was_typed():
    """name@machine_pub is the one stored form; a display name only
    renders (auto-37b1t)."""
    for typed in ("sjc-2", "SJC-2", B, B[:12]):
        assert scc.canonical_address(
            f"auto-9@{typed}", roster=ROSTER, names=NAMES) == f"auto-9@{B}"
    assert scc.canonical_address("auto-9@nowhere", roster=ROSTER, names=NAMES) is None
    assert scc.canonical_address("auto-9", roster=ROSTER, names=NAMES) is None
    assert scc.canonical_address("@sjc-2", roster=ROSTER, names=NAMES) is None


def test_unknown_or_ambiguous_names_resolve_to_nothing():
    assert scc.resolve_machine("nowhere", roster=ROSTER, names=NAMES) is None
    assert scc.resolve_machine("cc" * 32, roster=ROSTER, names=NAMES) is None
    # a profile name for a machine that is no longer in the roster
    assert scc.resolve_machine(
        "old", roster=ROSTER, names={**NAMES, "dd" * 32: "old"}) is None
    # two machines with one name
    assert scc.resolve_machine(
        "home", roster=ROSTER, names={"a2" * 32: "home", "b2" * 32: "home"}) is None
    assert scc.resolve_machine("x", roster={}, names=NAMES) is None


def test_request_to_an_unknown_machine_is_refused_without_the_connector(monkeypatch):
    monkeypatch.setattr(scc, "resolve_machine", lambda name: None)
    reply = asyncio.run(scc.request("nowhere", "status"))
    assert reply["refusal"] == scc.UNKNOWN_MACHINE


def test_a_connector_that_cannot_be_reached_is_a_typed_refusal(monkeypatch):
    monkeypatch.setattr(scc, "resolve_machine", lambda name: B)

    def unreachable(*_a, **_k):
        raise ConnectionError("no control listener")

    monkeypatch.setattr(scc, "_control", unreachable)
    reply = asyncio.run(scc.request("sjc-2", "status"))
    assert (reply["refusal"], reply["at"]) == (scc.CONNECTOR_CALL_FAILED, "local")


def test_a_connector_that_answers_without_running_the_request_is_its_own_code(monkeypatch):
    monkeypatch.setattr(scc, "resolve_machine", lambda name: B)
    monkeypatch.setattr(scc, "_control", lambda *_a, **_k: {"ok": False, "error": "busy"})
    reply = asyncio.run(scc.request("sjc-2", "status"))
    assert (reply["refusal"], reply["detail"]) == (scc.CONNECTOR_REFUSED, "busy")


def test_the_connector_reply_record_is_returned_as_is(monkeypatch):
    monkeypatch.setattr(scc, "resolve_machine", lambda name: B)
    seen = {}

    def control(op, args, *, timeout):
        seen.update(op=op, args=args)
        return {"ok": True, "reply": {"v": 1, "ok": False,
                                      "refusal": "destination-slot-absent"}}

    monkeypatch.setattr(scc, "_control", control)
    reply = asyncio.run(scc.request("sjc-2", "status", {"x": 1}))
    assert reply == {"v": 1, "ok": False, "refusal": "destination-slot-absent"}
    assert seen["op"] == "session-control-request"
    assert seen["args"]["machine_pub"] == B and seen["args"]["body"] == {"x": 1}


def test_the_pump_answers_a_request_through_the_op_table(monkeypatch):
    replies = []

    async def echo(body, peer):
        return scc.ok({"body": body, "peer": peer})

    monkeypatch.setitem(scc.OPS, "echo", echo)
    pump = scc.InboundPump(
        poll=lambda: {"ok": True, "request": {
            "id": "r1", "op": "echo", "body": {"n": 2}, "peer_machine_pub": A}},
        reply=lambda request_id, record: replies.append((request_id, record)),
    )
    assert asyncio.run(pump.once()) is True
    assert replies == [("r1", {"v": 1, "ok": True,
                               "result": {"body": {"n": 2}, "peer": A}})]


def test_an_empty_poll_answers_nothing():
    replies = []
    pump = scc.InboundPump(poll=lambda: {"ok": True, "request": None},
                           reply=lambda *a: replies.append(a))
    assert asyncio.run(pump.once()) is False
    assert replies == []


def test_unknown_and_failing_ops_are_typed_refusals(monkeypatch):
    async def boom(body, peer):
        raise RuntimeError("broken")

    monkeypatch.setitem(scc.OPS, "boom", boom)
    assert asyncio.run(scc.dispatch("nope", {}, A))["refusal"] == scc.UNKNOWN_OP
    assert asyncio.run(scc.dispatch("boom", {}, A))["refusal"] == scc.OP_FAILED


def test_status_reports_identity_live_count_and_dispatch_limits(monkeypatch):
    from tools.dashboard import session_presence
    from tools.dashboard.dao import dashboard_db

    monkeypatch.setattr(session_presence, "local_machine",
                        lambda: session_presence.LocalMachine(B, "b2" * 32))
    monkeypatch.setattr(session_presence, "_machine_names", lambda: NAMES)
    monkeypatch.setattr(dashboard_db, "get_live_sessions",
                        lambda: [{"tmux_name": "auto-1"}, {"tmux_name": "auto-2"}])
    from tools.dashboard import machine_resources

    monkeypatch.setattr(machine_resources, "sample", lambda: {"ram_free_gb": 4.0})
    status = scc.status_op(lambda: {"bead_max_concurrent": 2})
    reply = asyncio.run(status({}, A))
    assert reply == {"v": 1, "ok": True, "result": {
        "machine_pub": B, "machine_id": "b2" * 32, "label": "sjc-2",
        "active": 2, "live_sessions": 2, "resources": {"ram_free_gb": 4.0},
        "dispatch_limits": {"bead_max_concurrent": 2}}}


def test_a_connector_that_refuses_at_once_is_not_polled_in_a_tight_loop(monkeypatch):
    monkeypatch.setattr(scc, "UNAVAILABLE_BACKOFF_S", 0.5)
    calls = []

    def poll():
        calls.append(1)
        return {"ok": False, "error": "unknown op"}

    async def run():
        pump = scc.InboundPump(poll=poll, reply=lambda *a: None)
        task = asyncio.create_task(pump.run())
        await asyncio.sleep(1.0)
        task.cancel()

    asyncio.run(run())
    assert len(calls) <= 2
