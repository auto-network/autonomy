"""``fleet-slots``: an authenticated tunnel may list its OWN org's live
serving slots — the tuples a directed pair can name — and nothing else."""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.relaykit.fleet_stream_wire import CAP_FLEET_DIRECTED_STREAM
from tools.network.relaykit.frames import CTRL_CHANNEL_ID, FRAME_CTRL, decode_frame, encode_frame
from tools.network.relaykit.hello import SERVING_MACHINE_HELLO_DOMAIN, build_tunnel_hello_v2

from .conftest import ORG, ORG_NONE, ctrl, register, serve_cert

import json


def _open(client, clock, root, org, machine_key, caps=()):
    serve_key = KeyPair.generate()
    cert = serve_cert(root, serve_key, org)
    hello = build_tunnel_hello_v2(
        serve_key, cert, machine_key=machine_key, org=org, ts=clock.now,
        caps=caps, machine_hello_domain=SERVING_MACHINE_HELLO_DOMAIN,
    )
    ws = client.websocket_connect(f"/t/{org}")
    ws.__enter__()
    ws.send_text(hello)
    ack = ws.receive_json()
    assert ack["ok"] is True, ack
    return ws, ack


def test_fleet_slots_lists_only_this_orgs_live_tunnels(client, clock, root, bound_org, bound_org_none):
    a_key, b_key, other_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    a, ack_a = _open(client, clock, root, ORG, a_key, caps=(CAP_FLEET_DIRECTED_STREAM,))
    b, _ = _open(client, clock, root, ORG, b_key)
    other, _ = _open(client, clock, bound_org_none, ORG_NONE, other_key,
                     caps=(CAP_FLEET_DIRECTED_STREAM,))
    try:
        assert CAP_FLEET_DIRECTED_STREAM in ack_a["caps"]
        reply = ctrl(a, "0" * 32, "fleet-slots", {})
        assert reply["ok"] is True, reply
        machines = {slot["machine"]: slot for slot in reply["slots"]}
        assert set(machines) == {a_key.public_hex, b_key.public_hex}
        assert machines[a_key.public_hex]["caps"] == [CAP_FLEET_DIRECTED_STREAM]
        assert machines[b_key.public_hex]["caps"] == []
        assert all(slot["persona_pub"] == "ab" * 32 for slot in reply["slots"])
        # The other org's tunnel is invisible from here, and vice versa.
        assert other_key.public_hex not in machines
        theirs = ctrl(other, "1" * 32, "fleet-slots", {})
        assert [s["machine"] for s in theirs["slots"]] == [other_key.public_hex]
        # Arguments are refused, the tunnel stays up.
        refused = ctrl(a, "2" * 32, "fleet-slots", {"org": ORG_NONE})
        assert refused["ok"] is False
    finally:
        for ws in (a, b, other):
            ws.__exit__(None, None, None)


def test_fleet_open_requires_the_negotiated_capability(client, clock, root, bound_org):
    a, _ = _open(client, clock, root, ORG, KeyPair.generate())      # no caps
    b, _ = _open(client, clock, root, ORG, KeyPair.generate(), caps=(CAP_FLEET_DIRECTED_STREAM,))
    try:
        reply = ctrl(a, "3" * 32, "fleet-open", {
            "dst_persona_pub": "ab" * 32, "dst_machine": "22" * 32,
            "operation_id": "0f" * 16,
        })
        assert reply["ok"] is False and "not negotiated" in reply["error"]
    finally:
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)
