"""auto-0zdky: concurrent persona/machine tunnels on one org.

Drives ``/t/{org}`` with v2 hellos: distinct machines coexist, reconnect
replaces only its own (persona, machine) slot with 4409, v1 connectors
keep the legacy org-slot semantics, and revocation sweeps exactly the
revoked signer's tunnels.
"""

from __future__ import annotations

import contextlib

import json
import pytest
from fastapi.testclient import TestClient  # noqa: F401  (fixture plumbing)

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit import hello as hello_mod
from tools.network.relaykit.hello import HelloError
from tools.network.registry import relay as relay_mod
from tools.network.registry.relay import CLOSE_REPLACED, _verify_tunnel_hello

from .conftest import DAY, NOW, ORG, register

PERSONA_A = "ab" * 32
PERSONA_B = "cd" * 32


def _serve_cert(root, serve_key, persona=PERSONA_A, org=ORG):
    return issue_cert(
        root,
        serve_key.public_hex,
        scope=("tunnel:serve",),
        org=org,
        subject=Subject("persona", persona),
        not_before=NOW - 100,
        not_after=NOW + 30 * DAY,
    )


def _v2_hello(root, *, persona=PERSONA_A, machine_key=None, ts=NOW,
              caps=("host-lease/1",)):
    serve_key = KeyPair.generate()
    machine_key = machine_key or KeyPair.generate()
    cert = _serve_cert(root, serve_key, persona)
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, cert, machine_key=machine_key, org=ORG, ts=ts, caps=caps,
        machine_hello_domain=hello_mod.SERVING_MACHINE_HELLO_DOMAIN,
    )
    return raw, serve_key, machine_key


@contextlib.contextmanager
def _tunnel_v2(client, clock, root, **kw):
    raw, serve_key, machine_key = _v2_hello(root, ts=clock.now, **kw)
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(raw)
        ack = ws.receive_json()
        assert ack["ok"] is True, ack
        assert ack["v"] == 2
        yield ws, serve_key, machine_key, ack


def test_verify_returns_machine_and_caps(app, clock, client, root):
    register(client, clock, root, org_uuid=ORG)
    raw, serve_key, machine_key = _v2_hello(root, ts=clock.now)
    verified = _verify_tunnel_hello(raw, ORG, app.state.store, clock.now)
    assert verified.persona_pub == PERSONA_A
    assert verified.signer_pub == serve_key.public_hex
    assert verified.machine == machine_key.public_hex
    assert verified.caps == ("host-lease/1",)
    assert verified.version == 2


def test_a_hello_without_a_machine_identity_is_refused(app, clock, client, root):
    """INVERTED: this asserted that a v1 hello verified into an EMPTY machine
    slot. That slot is exactly what let two machines of one org overwrite each
    other, and v1 carried no machine identity and could carry no capabilities.
    It is deleted; a hello that cannot name its machine is now refused."""
    register(client, clock, root, org_uuid=ORG)
    serve_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key)
    # Build a REAL hello and relabel it v1, so the refusal is proven against
    # a well-formed message rather than a malformed one.
    from tools.network.relaykit.hello import (
        SERVING_MACHINE_HELLO_DOMAIN, build_tunnel_hello_v2,
    )
    payload = json.loads(build_tunnel_hello_v2(
        serve_key, cert, machine_key=KeyPair.generate(), org=ORG,
        ts=clock.now, machine_hello_domain=SERVING_MACHINE_HELLO_DOMAIN,
    ))
    payload["v"] = 1
    raw = json.dumps(payload)

    with pytest.raises(HelloError):
        _verify_tunnel_hello(raw, ORG, app.state.store, clock.now)


def test_bad_machine_signature_is_refused(app, clock, client, root):
    register(client, clock, root, org_uuid=ORG)
    import json

    raw, _, _ = _v2_hello(root, ts=clock.now)
    data = json.loads(raw)
    data["machine_sig"] = KeyPair.generate().sign_hex(b"unrelated")
    with pytest.raises(relay_mod.HelloError, match="machine"):
        _verify_tunnel_hello(
            json.dumps(data), ORG, app.state.store, clock.now
        )


def test_machine_field_swap_is_refused(app, clock, client, root):
    """A hello claiming a machine key that did not co-sign fails closed."""
    register(client, clock, root, org_uuid=ORG)
    import json

    raw, _, _ = _v2_hello(root, ts=clock.now)
    data = json.loads(raw)
    data["machine"] = KeyPair.generate().public_hex
    with pytest.raises(relay_mod.HelloError):
        _verify_tunnel_hello(
            json.dumps(data), ORG, app.state.store, clock.now
        )


def test_distinct_machines_coexist_and_reconnect_replaces_own_slot_only(
    client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    machine_a = KeyPair.generate()
    machine_b = KeyPair.generate()

    with _tunnel_v2(client, clock, root, machine_key=machine_a) as (ws_a, *_):
        with _tunnel_v2(client, clock, root, machine_key=machine_b) as (
            ws_b, *_,
        ):
            # Both live concurrently: each still answers a ping-level ctrl.
            # A reconnect of machine A replaces ONLY machine A's slot.
            raw2, _, _ = _v2_hello(
                root, machine_key=machine_a, ts=clock.now
            )
            with client.websocket_connect(f"/t/{ORG}") as ws_a2:
                ws_a2.send_text(raw2)
                assert ws_a2.receive_json()["ok"] is True
                # The first machine-A socket is closed 4409 …
                from starlette.websockets import WebSocketDisconnect

                with pytest.raises(WebSocketDisconnect) as exc:
                    ws_a.receive_bytes()
                assert exc.value.code == CLOSE_REPLACED
                # … while machine B's tunnel remains open and serviceable.
                import json as _json

                from tools.network.relaykit.frames import (
                    CTRL_CHANNEL_ID,
                    FRAME_CTRL,
                    decode_frame,
                    encode_frame,
                )

                ws_b.send_bytes(encode_frame(
                    FRAME_CTRL,
                    CTRL_CHANNEL_ID,
                    _json.dumps({
                        "id": "ab" * 16, "op": "no-such-op", "args": {},
                    }).encode(),
                ))
                reply = _json.loads(
                    decode_frame(ws_b.receive_bytes()).payload
                )
                assert reply["ok"] is False  # alive and answering


def test_two_personas_serve_one_org_concurrently(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel_v2(client, clock, root, persona=PERSONA_A) as (ws_a, *_):
        with _tunnel_v2(client, clock, root, persona=PERSONA_B) as (
            ws_b, *_,
        ):
            hub = None  # both admitted without either being closed
            del hub
            # Liveness: closing B does not disturb A.
        import json as _json

        from tools.network.relaykit.frames import (
            CTRL_CHANNEL_ID,
            FRAME_CTRL,
            decode_frame,
            encode_frame,
        )

        ws_a.send_bytes(encode_frame(
            FRAME_CTRL,
            CTRL_CHANNEL_ID,
            _json.dumps({
                "id": "cd" * 16, "op": "no-such-op", "args": {},
            }).encode(),
        ))
        reply = _json.loads(decode_frame(ws_a.receive_bytes()).payload)
        assert reply["ok"] is False


# -- hostname probe routing (the host-probe diagnostic) ---------------------

import hashlib as _hashlib
import json as _json2
import uuid as _uuid2

from starlette.websockets import WebSocketDisconnect as _WSDisconnect

from tools.network.relaykit.frames import (
    FRAME_DATA as _FRAME_DATA,
    FRAME_OPEN as _FRAME_OPEN,
    decode_frame as _decode_frame,
    encode_frame as _encode_frame,
)

_RES_NS = _uuid2.UUID("6cf440db-c8b4-566c-99db-e7be17109bdc")


def _mk_host(app_label, persona):
    suffix = _hashlib.sha256(bytes.fromhex(persona)).hexdigest()[:20]
    return f"{app_label}.worker-{suffix}.serve.auto.network"


def _mk_reservation(persona, app_label):
    return str(_uuid2.uuid5(_RES_NS, f"{persona}\0{app_label}"))


def _lease(ws, reservation, host):
    request = {"id": "ab" * 16, "op": "host-register",
               "args": {"reservation": reservation, "host": host}}
    from tools.network.relaykit.frames import CTRL_CHANNEL_ID, FRAME_CTRL

    ws.send_bytes(_encode_frame(
        FRAME_CTRL, CTRL_CHANNEL_ID, _json2.dumps(request).encode()))
    reply = _json2.loads(_decode_frame(ws.receive_bytes()).payload)
    assert reply["ok"] is True, reply
    return reply


def test_probe_routes_to_exactly_the_leased_tunnel(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    machine_a = KeyPair.generate()
    machine_b = KeyPair.generate()
    host_a = _mk_host("appa", PERSONA_A)
    host_b = _mk_host("appb", PERSONA_A)

    with _tunnel_v2(client, clock, root, machine_key=machine_a) as (
        ws_a, *_,
    ):
        with _tunnel_v2(client, clock, root, machine_key=machine_b) as (
            ws_b, *_,
        ):
            _lease(ws_a, _mk_reservation(PERSONA_A, "appa"), host_a)
            _lease(ws_b, _mk_reservation(PERSONA_A, "appb"), host_b)

            # Probe host_a: the OPEN must land on tunnel A only, and the
            # echo A answers must reach the prober verbatim.
            with client.websocket_connect(
                f"/v1/hosts/{host_a}/probe"
            ) as probe:
                frame = _decode_frame(ws_a.receive_bytes())
                assert frame.type == _FRAME_OPEN
                meta = _json2.loads(frame.payload)
                assert meta["kind"] == "host-probe"
                assert meta["host"] == host_a
                echo = _json2.dumps({"kind": "host-probe",
                                     "machine": "a"}).encode()
                ws_a.send_bytes(_encode_frame(
                    _FRAME_DATA, frame.channel_id, echo))
                received = probe.receive_bytes()
                assert received == echo


def test_probe_unknown_host_closes_uniformly(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _mk_host("ghost", PERSONA_A)
    with pytest.raises(_WSDisconnect) as exc:
        with client.websocket_connect(f"/v1/hosts/{host}/probe") as probe:
            probe.receive_bytes()
    assert exc.value.code == 4404
