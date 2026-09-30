"""member-message/1 end to end on a real relay with two real ORGANIZATION
connectors (graph://bace7454-c77, bead auto-qrmlg.9 part 2): the pair is
brokered under its own capability, the ORG hello inside it proves which
member on which machine is asking, the request the destination's broker
receives names the organization, persona and machine the hello proved,
and every failure is a typed refusal, never a hang."""

from __future__ import annotations

import asyncio
import time

import pytest

from tools.network import fleet_roster, member_message, session_control
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM,
    CAP_MEMBER_MESSAGE,
)
from tools.network.tests.test_fleet_org_channel import ORG as GENESIS, Node
from tools.network.tests.test_fleet_relay_carrier import (
    ORG,
    _free_port,
    _live_registry,
    _register,
    _stop,
)


class Runtime:
    """What connector_runtime exposes on an organization connector: an armed
    scheduler with the personal fleet authenticator (what serve_fleet_transport
    admits a PERSONAL hello with) and the org hello lookup by genesis."""

    def __init__(self, node: Node, root, roster, *, genesis: str = GENESIS):
        self.node = node
        self.scheduler = type("S", (), {})()
        self.scheduler.authenticator = FleetAuthenticator(
            node.machine, root_pub=root.public_hex, roster_entries=lambda: roster)
        self.scheduler._org_channel_for_genesis = (
            lambda org: node.auth if org == genesis else None)


async def _connector(port, root, node: Node, runtime, broker, *, caps):
    serve_key = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
                      subject=Subject("persona", node.persona.public_hex),
                      not_before=now - 100, not_after=now + 30 * 86400)
    connector = TunnelConnector(
        f"ws://127.0.0.1:{port}", ORG, serve_key, cert, machine_key=node.machine,
        caps=caps,
        member_message_offer=member_message.member_message_offer_handler(runtime, broker),
        min_backoff=0.05, max_backoff=0.2,
    )
    task = asyncio.create_task(connector.run())
    await asyncio.wait_for(connector.connected.wait(), 10)
    return connector, task


async def _answer(broker):
    """The destination dashboard's pump, reduced to: echo who the pair proved."""
    item = await broker.next(10)
    assert item is not None
    broker.reply(item["id"], {"v": 1, "ok": True, "result": {
        "op": item["op"], "body": item["body"], "org": item["org"],
        "persona": item["persona_pub"], "machine": item["peer_machine_pub"]}})


def test_member_message_request_reply_over_the_relay():
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_scenario(root, port, app.state.directed_streams))


async def _scenario(root, port, relay_broker):
    _register(port, root)
    alice, bob = KeyPair.generate(), KeyPair.generate()
    members = [alice.public_hex, bob.public_hex]
    node_a, node_b = Node(alice, members), Node(bob, members)
    roster = (
        fleet_roster.enroll(root, machine_pub=node_a.machine.public_hex, seq=0),
        fleet_roster.enroll(root, machine_pub=node_b.machine.public_hex, seq=0),
    )
    caps = (CAP_FLEET_DIRECTED_STREAM, CAP_MEMBER_MESSAGE)
    runtime_a, runtime_b = Runtime(node_a, root, roster), Runtime(node_b, root, roster)
    broker_a, broker_b = session_control.InboundBroker(), session_control.InboundBroker()
    a, task_a = await _connector(port, root, node_a, runtime_a, broker_a, caps=caps)
    b, task_b = await _connector(port, root, node_b, runtime_b, broker_b, caps=caps)
    to_b = dict(genesis=GENESIS, persona_pub=bob.public_hex, machine=node_b.machine.public_hex)
    try:
        assert a.member_streams is not None and b.member_streams is not None

        # One request, one reply. The reply names what the ORG HELLO proved:
        # the organization, Alice's persona and her serving machine, never a
        # claim from the body. The relay counted a member-message pair.
        answering = asyncio.create_task(_answer(broker_b))
        started = time.monotonic()
        reply = await member_message.request(
            a, runtime_a, op="send", body={"sealed": "..."}, timeout=5, **to_b)
        elapsed = time.monotonic() - started
        await answering
        assert reply["ok"] is True, reply
        assert reply["result"] == {
            "op": "send", "body": {"sealed": "..."}, "org": GENESIS,
            "persona": alice.public_hex, "machine": node_a.machine.public_hex}
        assert elapsed < 3.0
        # One request, one pair: it closes after the reply (the reset reaches
        # the relay a moment after the requester has its record).
        for _ in range(50):
            if relay_broker.snapshot()["pairs"] == 0:
                break
            await asyncio.sleep(0.02)
        assert relay_broker.snapshot()["pairs"] == 0

        # The same ctl op the dashboard drives, end to end through the broker.
        answering = asyncio.create_task(_answer(broker_b))
        ctl = await member_message.handle_ctl(a, runtime_a, "member-message-request", {
            **to_b, "op": "send", "body": {"n": 2}, "timeout": 5})
        await answering
        assert ctl["ok"] is True and ctl["reply"]["result"]["body"] == {"n": 2}

        # A member whose machine is not serving is the relay's own refusal.
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5,
            genesis=GENESIS, persona_pub=bob.public_hex, machine="cc" * 32)
        assert reply["ok"] is False and reply["at"] == "local"
        assert "absent" in reply["refusal"], reply

        # An organization this process holds no org hello for: decided here.
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5,
            genesis="genesis-" + "ff" * 28, persona_pub=bob.public_hex,
            machine=node_b.machine.public_hex)
        assert (reply["refusal"], reply["at"]) == (member_message.NO_ORG_CHANNEL, "local")

        # Bob's node adopts a checkpoint that removes Alice: her org hello is
        # refused THERE, by the membership check, in place of Bob's hello.
        node_b.adopt(1, [bob.public_hex])
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5, **to_b)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, member_message.ORG_HELLO_REFUSED, "peer"), reply
        node_b.adopt(2, members)

        # The hello admits Alice, but Bob's node cannot CONFIRM her membership
        # afterwards (is_member None: no member list behind the newest
        # adoption). Cannot-tell is refused as not-a-member; the broker
        # receives nothing.
        node_b.auth.is_member = lambda persona: None
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5, **to_b)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, member_message.NOT_A_MEMBER, "peer"), reply
        assert await broker_b.next(0.1) is None
        del node_b.auth.is_member

        # The requester states, not implies, that it reached the member it
        # addressed: Bob's machine proving Bob's persona under Carol's name
        # is refused here before any request is sent.
        carol = KeyPair.generate().public_hex
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5,
            genesis=GENESIS, persona_pub=carol, machine=node_b.machine.public_hex)
        assert reply["ok"] is False and reply["at"] == "local"
        assert reply["refusal"] in (member_message.WRONG_MEMBER, "destination-slot-absent"), reply

        # A destination holding no org channel for the organization refuses
        # by name in place of its hello.
        runtime_b.scheduler._org_channel_for_genesis = lambda org: None
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5, **to_b)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, member_message.ORG_HELLO_REFUSED, "peer"), reply
        assert "no organization scope" in reply["detail"]
        runtime_b.scheduler._org_channel_for_genesis = (
            lambda org: node_b.auth if org == GENESIS else None)

        # An unarmed destination refuses before any hello.
        armed = runtime_b.scheduler
        runtime_b.scheduler = None
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5, **to_b)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, member_message.UNARMED, "peer"), reply
        runtime_b.scheduler = armed

        # A request the dashboard never answers is a typed timeout at the
        # peer, and the requester learns it rather than hanging.
        broker_b._reply_timeout = 0.3
        reply = await member_message.request(
            a, runtime_a, op="send", body={}, timeout=5, **to_b)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, session_control.DASHBOARD_UNAVAILABLE, "peer"), reply
        while await broker_b.next(0.05) is not None:
            pass
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)


def test_a_connector_without_the_capability_is_refused_by_name():
    """A tunnel whose relay did not negotiate member-message/1 (a relay that
    predates it) has no adapter: a typed local refusal, no pair."""
    alice = KeyPair.generate()
    node = Node(alice, [alice.public_hex])
    runtime = Runtime(node, KeyPair.generate(), ())
    connector = type("C", (), {"member_streams": None})()
    reply = asyncio.run(member_message.request(
        connector, runtime, genesis=GENESIS, persona_pub=alice.public_hex,
        machine="aa" * 32, op="send", body={}))
    assert (reply["refusal"], reply["at"]) == (member_message.NOT_NEGOTIATED, "local")


def test_ctl_ops_are_the_broker_seen_from_the_dashboard():
    async def scenario():
        broker = session_control.InboundBroker()
        connector, runtime = object(), object()
        assert (await member_message.handle_ctl(connector, runtime, "member-message-next",
                                                {"wait": 0.05}, broker)) == {"ok": True, "request": None}
        pending = asyncio.create_task(broker.submit(
            "send", {"k": 1}, peer_machine_pub="aa" * 32, org=GENESIS, persona_pub="bb" * 32))
        item = (await member_message.handle_ctl(connector, runtime, "member-message-next",
                                                {"wait": 1}, broker))["request"]
        assert (item["op"], item["org"], item["persona_pub"], item["peer_machine_pub"]) == (
            "send", GENESIS, "bb" * 32, "aa" * 32)
        answered = await member_message.handle_ctl(
            connector, runtime, "member-message-reply",
            {"id": item["id"], "reply": {"v": 1, "ok": True, "result": {}}}, broker)
        assert answered == {"ok": True, "delivered": True}
        assert (await pending)["ok"] is True
        malformed = await member_message.handle_ctl(
            connector, runtime, "member-message-request", {"genesis": GENESIS}, broker)
        assert malformed["reply"]["refusal"] == "ctl-request-malformed"
        assert (await member_message.handle_ctl(connector, runtime, "member-message-reply",
                                                {"id": 1}, broker))["ok"] is False
        assert (await member_message.handle_ctl(connector, runtime, "member-message-bogus",
                                                {}, broker))["ok"] is False
    asyncio.run(scenario())
