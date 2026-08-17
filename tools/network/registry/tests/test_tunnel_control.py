"""§D19: link create/revoke as control frames on the org tunnel.

These drive the registry's ``/t/{org}`` websocket directly with a valid
``tunnel:serve`` hello, then exchange ``FRAME_CTRL`` frames — proving the
registry-side dispatch (auto-zudu9 §2) end to end without the dashboard
connector. The connector's ``control()`` correlation half is exercised
in the relaykit connector tests.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.frames import (
    CTRL_CHANNEL_ID,
    FRAME_CTRL,
    decode_frame,
    encode_frame,
)
from tools.network.relaykit.hello import (
    HELLO_VERSION,
    build_tunnel_hello,
    hello_signing_input,
)
from tools.network.registry.relay import (
    CLOSE_PROTOCOL_MISMATCH,
    CLOSE_UNAUTHENTICATED,
)
from tools.network.registry.app import create_app
from tools.network.registry.turn_credentials import TurnCredentialIssuer
from starlette.websockets import WebSocketDisconnect

from .conftest import DAY, NOW, ORG, ORG_NONE, TARGET, register


def _serve_cert(root: KeyPair, serve_key: KeyPair, org: str = ORG):
    return issue_cert(
        root,
        serve_key.public_hex,
        scope=("tunnel:serve",),
        org=org,
        subject=Subject("persona", "ab" * 32),
        not_before=NOW - 100,
        not_after=NOW + 30 * DAY,
    )


@contextlib.contextmanager
def _open_tunnel(client, clock, root, org=ORG):
    """Register the org, connect its tunnel, complete the hello, and yield
    the open websocket (already past the {ok: true} ack)."""
    register(client, clock, root, org_uuid=org)
    serve_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key, org)
    hello = build_tunnel_hello(serve_key, cert, org=org, ts=clock.now)
    with client.websocket_connect(f"/t/{org}") as ws:
        ws.send_text(hello)
        ack = ws.receive_json()
        assert ack == {"ok": True, "v": HELLO_VERSION}, ack
        yield ws


def _ctrl(ws, correlation, op, args):
    request = {"id": correlation, "op": op, "args": args}
    ws.send_bytes(encode_frame(
        FRAME_CTRL, CTRL_CHANNEL_ID, json.dumps(request).encode("utf-8")))
    frame = decode_frame(ws.receive_bytes())
    assert frame.type == FRAME_CTRL
    assert frame.channel_id == CTRL_CHANNEL_ID
    return json.loads(frame.payload.decode("utf-8"))


def _hello_with_version(serve_key, cert, *, org, ts, version):
    payload = json.loads(build_tunnel_hello(
        serve_key, cert, org=org, ts=ts))
    payload["v"] = version
    payload["sig"] = serve_key.sign_hex(hello_signing_input(
        org, serve_key.public_hex, ts, version=version))
    return json.dumps(payload)


def test_authenticated_old_connector_gets_typed_version_mismatch(
    client, clock, root, app,
):
    register(client, clock, root, org_uuid=ORG)
    serve_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key)
    hello = _hello_with_version(
        serve_key, cert, org=ORG, ts=clock.now, version=HELLO_VERSION - 1)

    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(hello)
        assert ws.receive_json() == {
            "ok": False,
            "error": {
                "code": "protocol_version_mismatch",
                "connector_version": HELLO_VERSION - 1,
                "registry_version": HELLO_VERSION,
            },
        }
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == CLOSE_PROTOCOL_MISMATCH
    assert app.state.tunnel_hub.get(ORG) is None


def test_tampered_connector_version_fails_signature_not_version_negotiation(
    client, clock, root, app,
):
    register(client, clock, root, org_uuid=ORG)
    serve_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key)
    payload = json.loads(build_tunnel_hello(
        serve_key, cert, org=ORG, ts=clock.now))
    payload["v"] = HELLO_VERSION - 1  # signature still covers HELLO_VERSION

    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(json.dumps(payload))
        reply = ws.receive_json()
        assert reply["ok"] is False
        assert isinstance(reply["error"], str)
        assert "signature" in reply["error"].lower()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == CLOSE_UNAUTHENTICATED
    assert app.state.tunnel_hub.get(ORG) is None


def test_create_link_mints_org_tunnel_grant(client, clock, root):
    """Acceptance 1: create-link over the tunnel returns {ok, token, url}
    and the token resolves an envelope."""
    with _open_tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "a" * 32, "create-link",
                      {"target_uuid": TARGET, "target_type": "present"})
    assert reply["ok"] is True
    assert reply["id"] == "a" * 32
    token = reply["token"]
    assert len(token) == 32 and int(token, 16) >= 0
    assert reply["url"].endswith(f"/l/{token}")
    assert client.get(f"/v1/links/{token}/envelope").status_code == 200


def test_created_link_serves_and_records_no_persona(client, clock, root, app):
    """Acceptance 1 (storage): the grant carries org_uuid = the tunnel's
    org, signer_pub null, subject_kind 'org-tunnel'."""
    with _open_tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "b" * 32, "create-link",
                      {"target_uuid": TARGET, "target_type": "present"})
    token = reply["token"]
    grant = app.state.store.get_link(token)
    assert grant is not None
    assert grant.org_uuid == ORG
    assert grant.signer_pub is None
    assert grant.subject_id is None
    assert grant.subject_kind == "org-tunnel"


def test_revoke_link_over_tunnel(client, clock, root, app):
    """Acceptance 2: revoke-link revokes the token; the envelope stops
    resolving."""
    with _open_tunnel(client, clock, root) as ws:
        created = _ctrl(ws, "c" * 32, "create-link",
                        {"target_uuid": TARGET, "target_type": "present"})
        token = created["token"]
        assert client.get(f"/v1/links/{token}/envelope").status_code == 200
        revoked = _ctrl(ws, "d" * 32, "revoke-link", {"token": token})
    assert revoked["ok"] is True
    assert revoked["token"] == token
    assert revoked["revoked_at"] == clock.now
    assert client.get(f"/v1/links/{token}/envelope").status_code == 404


def test_authenticated_org_tunnel_issues_one_opaque_turn_coupon(clock, root):
    issuer = TurnCredentialIssuer(
        ("a" * 64,), clock=clock, token_hex=lambda _size: "b" * 32
    )
    app = create_app(
        ":memory:", now_fn=clock, secure_cookies=False, turn_issuer=issuer
    )
    with TestClient(app) as client:
        with _open_tunnel(client, clock, root) as ws:
            reply = _ctrl(ws, "9" * 32, "issue-turn", {})

    assert reply["ok"] is True
    assert reply["id"] == "9" * 32
    assert set(reply) == {"id", "ok", "ice_servers", "expires_at"}
    assert reply["expires_at"] == clock.now + 15 * 60
    assert reply["ice_servers"][1]["username"].endswith(":" + "b" * 32)
    assert ORG not in json.dumps(reply)


def test_turn_coupon_is_unavailable_without_the_host_credential(client, clock, root):
    with _open_tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "8" * 32, "issue-turn", {})
    assert reply == {
        "id": "8" * 32,
        "ok": False,
        "error": "TURN credential issuance is unavailable",
    }


def test_second_org_cannot_revoke_or_enumerate(client, clock, root, app):
    """Acceptance 2 (cross-org): a second org's tunnel cannot revoke the
    first org's token — it is refused and nothing is revoked."""
    with _open_tunnel(client, clock, root) as ws:
        created = _ctrl(ws, "e" * 32, "create-link",
                        {"target_uuid": TARGET, "target_type": "present"})
        token = created["token"]

    other_root = KeyPair.generate()
    with _open_tunnel(client, clock, other_root, org=ORG_NONE) as ws2:
        reply = _ctrl(ws2, "f" * 32, "revoke-link", {"token": token})
    assert reply["ok"] is False
    assert "another org" in reply["error"]
    # Nothing was revoked: the first org's link still resolves.
    assert client.get(f"/v1/links/{token}/envelope").status_code == 200


def test_unknown_token_revoke_is_clean_failure(client, clock, root):
    with _open_tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "1" * 32, "revoke-link", {"token": "0" * 32})
    assert reply["ok"] is False
    assert "unknown link" in reply["error"]


def test_require_auth_is_rung2_refused(client, clock, root):
    """Acceptance 6: a create-link asking for require_auth is refused (a
    clean {ok: false}, not a dropped tunnel — the tunnel keeps serving)."""
    with _open_tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "2" * 32, "create-link", {
            "target_uuid": TARGET, "target_type": "present",
            "meta": {"require_auth": True},
        })
        assert reply["ok"] is False
        assert "viewer authn" in reply["error"]
        # Tunnel still live: a following valid op succeeds.
        ok = _ctrl(ws, "3" * 32, "create-link",
                   {"target_uuid": TARGET, "target_type": "present"})
        assert ok["ok"] is True


def test_org_join_refused_on_control_channel(client, clock, root):
    """org:join keeps the envelope endpoint (its own transport) — it is
    not carried as a share-link control op."""
    with _open_tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "4" * 32, "create-link",
                      {"target_uuid": ORG, "target_type": "org:join"})
    assert reply["ok"] is False
    assert "org:join" in reply["error"]


def test_malformed_control_payload_is_a_protocol_violation():
    """Acceptance 6: a malformed control payload raises FrameError — the
    receive loop treats that exactly like a bad frame and drops the
    tunnel (contrast with a clean op failure, which replies {ok: false}).
    Asserted at the handler so it does not depend on the in-process test
    transport's websocket-close timing."""
    import asyncio

    from tools.network.registry import relay
    from tools.network.relaykit.frames import FrameError

    class _StubTunnel:
        org = ORG

        async def send_frame(self, *a, **k):  # never reached on a drop
            raise AssertionError("malformed payload must not send a reply")

    store = _FreshStore()
    for payload in (b"not json", b"[]", b'{"op":"create-link"}',
                    b'{"id":"short","op":"create-link","args":{}}'):
        raised = False
        try:
            asyncio.run(relay._handle_ctrl_frame(
                _StubTunnel(), payload, store, "https://r", NOW))
        except FrameError:
            raised = True
        assert raised, f"expected FrameError for {payload!r}"


def _FreshStore():
    from tools.network.registry.store import RegistryStore
    return RegistryStore(":memory:")
