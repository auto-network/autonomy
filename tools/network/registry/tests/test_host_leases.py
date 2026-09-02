"""auto-0zdky: hostname ownership + live leases over tunnel control ops.

``host-register`` is the authenticated desired-state advertisement, keyed
by the NamespaceReservation UUID (UUIDv5 over ``<persona_pub>\\0<app>``,
design c880c5e6 §3.2). Ownership is durable and persona-bound; the lease
binds one live (machine, connection) with generation fencing. Identity is
always derived from the authenticated tunnel — never from op bodies.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry import relay as relay_mod
from tools.network.relaykit import hello as hello_mod
from tools.network.relaykit.frames import (
    CTRL_CHANNEL_ID,
    FRAME_CTRL,
    decode_frame,
    encode_frame,
)

from .conftest import DAY, NOW, ORG, register

PERSONA_A = "ab" * 32
PERSONA_B = "cd" * 32
RESERVATION_NS = uuid.UUID("6cf440db-c8b4-566c-99db-e7be17109bdc")


def _label(persona_pub: str, slug: str = "worker") -> str:
    suffix = hashlib.sha256(bytes.fromhex(persona_pub)).hexdigest()[:20]
    return f"{slug}-{suffix}"


def _host(app_label: str, persona_pub: str, slug: str = "worker") -> str:
    return f"{app_label}.{_label(persona_pub, slug)}.serve.auto.network"


def _reservation(persona_pub: str, app_label: str) -> str:
    name = f"{persona_pub}\0{app_label}"
    return str(uuid.uuid5(RESERVATION_NS, name))


def _serve_cert(root, serve_key, persona=PERSONA_A):
    return issue_cert(
        root,
        serve_key.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=Subject("persona", persona),
        not_before=NOW - 100,
        not_after=NOW + 30 * DAY,
    )


@contextlib.contextmanager
def _tunnel(client, clock, root, *, persona=PERSONA_A, machine_key=None,
            caps=("host-lease/1",)):
    serve_key = KeyPair.generate()
    machine_key = machine_key or KeyPair.generate()
    cert = _serve_cert(root, serve_key, persona)
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, cert, machine_key=machine_key, org=ORG,
        ts=clock.now, caps=caps,
        machine_hello_domain=hello_mod.SERVING_MACHINE_HELLO_DOMAIN,
    )
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(raw)
        ack = ws.receive_json()
        assert ack["ok"] is True, ack
        yield ws, machine_key


_SEQ = iter(range(10_000))


def _ctrl(ws, op, args):
    correlation = format(next(_SEQ), "032x")
    ws.send_bytes(encode_frame(
        FRAME_CTRL, CTRL_CHANNEL_ID,
        json.dumps({"id": correlation, "op": op, "args": args}).encode(),
    ))
    frame = decode_frame(ws.receive_bytes())
    assert frame.type == FRAME_CTRL
    reply = json.loads(frame.payload.decode())
    assert reply["id"] == correlation
    return reply


def test_register_returns_lease_and_persists_ownership(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root) as (ws, _):
        reply = _ctrl(ws, "host-register", {"reservation": res, "host": host})
        assert reply["ok"] is True, reply
        lease = reply["lease"]
        assert lease["generation"] >= 1
        assert lease["expires_at"] == clock.now + relay_mod.HOST_LEASE_TTL
        # Live route resolves while leased …
        assert app.state.host_routes.route(host) is not None
    # … and is gone immediately after tunnel loss (fail closed, ≤2 s bound
    # satisfied synchronously in the disconnect path).
    assert app.state.host_routes.route(host) is None


def test_reservation_uuid_must_match_persona_app_derivation(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    with _tunnel(client, clock, root) as (ws, _):
        reply = _ctrl(ws, "host-register", {
            "reservation": str(uuid.uuid4()), "host": host,
        })
        assert reply["ok"] is False
        assert reply["error"] == "label-invalid"


def test_cross_persona_claim_fails_closed(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root, persona=PERSONA_A) as (ws_a, _):
        assert _ctrl(ws_a, "host-register", {
            "reservation": res, "host": host,
        })["ok"] is True
        with _tunnel(client, clock, root, persona=PERSONA_B) as (ws_b, _):
            reply = _ctrl(ws_b, "host-register", {
                "reservation": res, "host": host,
            })
            assert reply["ok"] is False
            assert reply["error"] in (
                "label-invalid", "host-owned-elsewhere", "not-authorized",
            )


def test_second_connection_register_gets_lease_held(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root) as (ws_a, _):
        assert _ctrl(ws_a, "host-register", {
            "reservation": res, "host": host,
        })["ok"] is True
        with _tunnel(client, clock, root) as (ws_b, _):
            reply = _ctrl(ws_b, "host-register", {
                "reservation": res, "host": host,
            })
            assert reply["ok"] is False
            assert reply["error"] == "lease-held"


def test_renew_with_stale_generation_is_fenced(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root) as (ws, _):
        gen1 = _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })["lease"]["generation"]
    # Lease dropped on disconnect; a new connection re-registers → gen+1.
    with _tunnel(client, clock, root) as (ws2, _):
        gen2 = _ctrl(ws2, "host-register", {
            "reservation": res, "host": host,
        })["lease"]["generation"]
        assert gen2 == gen1 + 1
        stale = _ctrl(ws2, "host-renew", {
            "reservation": res, "generation": gen1,
        })
        assert stale["ok"] is False
        assert stale["error"] == "stale-generation"
        fresh = _ctrl(ws2, "host-renew", {
            "reservation": res, "generation": gen2,
        })
        assert fresh["ok"] is True
        assert fresh["lease"]["generation"] == gen2


def test_release_and_sibling_independence(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host_docs = _host("docs", PERSONA_A)
    host_api = _host("app2", PERSONA_A)
    res_docs = _reservation(PERSONA_A, "docs")
    res_api = _reservation(PERSONA_A, "app2")
    with _tunnel(client, clock, root) as (ws, _):
        assert _ctrl(ws, "host-register", {
            "reservation": res_docs, "host": host_docs,
        })["ok"] is True
        assert _ctrl(ws, "host-register", {
            "reservation": res_api, "host": host_api,
        })["ok"] is True
        assert _ctrl(ws, "host-release", {"reservation": res_docs})["ok"]
        assert app.state.host_routes.route(host_docs) is None
        assert app.state.host_routes.route(host_api) is not None


@pytest.mark.parametrize("app_label", [
    "www", "api", "relay", "registry", "auto", "serve", "_autonomy",
    "-bad", "bad-", "UPPER", "a" * 64,
])
def test_reserved_or_malformed_app_labels_refused(
    client, clock, root, app_label,
):
    register(client, clock, root, org_uuid=ORG)
    host = _host(app_label, PERSONA_A)
    res = _reservation(PERSONA_A, app_label)
    with _tunnel(client, clock, root) as (ws, _):
        reply = _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })
        assert reply["ok"] is False
        assert reply["error"] == "label-invalid"


def test_wrong_persona_suffix_in_host_is_refused(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    # Host whose label suffix binds to persona B, offered by persona A.
    host = _host("docs", PERSONA_B)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root, persona=PERSONA_A) as (ws, _):
        reply = _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })
        assert reply["ok"] is False
        assert reply["error"] == "label-invalid"


def test_identity_fields_in_op_body_are_refused(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root) as (ws, _):
        for spoof in ({"persona": PERSONA_B}, {"machine": "ef" * 32}):
            reply = _ctrl(ws, "host-register", {
                "reservation": res, "host": host, **spoof,
            })
            assert reply["ok"] is False


def test_ops_require_negotiated_capability(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root, caps=()) as (ws, _):
        reply = _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })
        assert reply["ok"] is False
        assert reply["error"] == "not-authorized"


def test_clean_op_failure_keeps_tunnel_serving(client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as (ws, _):
        bad = _ctrl(ws, "host-register", {"reservation": "nope", "host": "x"})
        assert bad["ok"] is False
        # The tunnel is still up and answers the next op.
        host = _host("docs", PERSONA_A)
        res = _reservation(PERSONA_A, "docs")
        good = _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })
        assert good["ok"] is True


def test_persona_serving_label_is_immutable(client, clock, root):
    """First registration binds the persona's label; a second app under a
    different slug (same valid suffix) fails closed as label-invalid."""
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as (ws, _):
        assert _ctrl(ws, "host-register", {
            "reservation": _reservation(PERSONA_A, "docs"),
            "host": _host("docs", PERSONA_A, slug="worker"),
        })["ok"] is True
        reply = _ctrl(ws, "host-register", {
            "reservation": _reservation(PERSONA_A, "blog"),
            "host": _host("blog", PERSONA_A, slug="other"),
        })
        assert reply["ok"] is False
        assert reply["error"] == "label-invalid"
        # Same slug is fine: many apps under one persona label.
        assert _ctrl(ws, "host-register", {
            "reservation": _reservation(PERSONA_A, "blog"),
            "host": _host("blog", PERSONA_A, slug="worker"),
        })["ok"] is True


def test_revocation_drops_hostname_routes_before_socket_close(
    app, client, clock, root,
):
    """Revoking the serving signer removes its hostname routes in the same
    act that removes the tunnel from admission — never left to the
    endpoint teardown or the lease TTL."""
    from tools.network.idkit import issue_revocation

    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    serve_key = KeyPair.generate()
    machine_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key)
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, cert, machine_key=machine_key, org=ORG,
        ts=clock.now, caps=("host-lease/1",),
        machine_hello_domain=hello_mod.SERVING_MACHINE_HELLO_DOMAIN,
    )
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(raw)
        assert ws.receive_json()["ok"] is True
        assert _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })["ok"] is True
        assert app.state.host_routes.route(host) is not None

        record = issue_revocation(
            root,
            serve_key.public_hex,
            org=ORG,
            revoked_at=clock.now,
            expires_at=cert.not_after,
            revoked_cert=cert,
        )
        response = client.post("/v1/revocations", json={
            "org": ORG,
            "record": record.to_json().decode("ascii"),
            "revoked_cert": cert.to_json().decode("ascii"),
        })
        assert response.status_code == 201, response.text
        assert app.state.host_routes.route(host) is None


# -- auto-ja0rf: one tunnel-wide keepalive ----------------------------------


def test_renew_all_extends_every_owned_lease_and_no_others(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    res_docs = _reservation(PERSONA_A, "docs")
    res_app2 = _reservation(PERSONA_A, "app2")
    res_b = _reservation(PERSONA_B, "docs")
    with _tunnel(client, clock, root, persona=PERSONA_A) as (ws_a, _):
        for res, app_label in ((res_docs, "docs"), (res_app2, "app2")):
            assert _ctrl(ws_a, "host-register", {
                "reservation": res, "host": _host(app_label, PERSONA_A),
            })["ok"] is True
        with _tunnel(client, clock, root, persona=PERSONA_B) as (ws_b, _):
            assert _ctrl(ws_b, "host-register", {
                "reservation": res_b, "host": _host("docs", PERSONA_B),
            })["ok"] is True
            before_b = app.state.host_routes._leases[res_b].expires_at
            clock.advance(300)
            reply = _ctrl(ws_a, "host-renew-all", {})
            assert reply["ok"] is True, reply
            assert reply["renewed"] == 2
            assert reply["expires_at"] == clock.now + relay_mod.HOST_LEASE_TTL
            leases = app.state.host_routes._leases
            assert leases[res_docs].expires_at == reply["expires_at"]
            assert leases[res_app2].expires_at == reply["expires_at"]
            # The other connection's lease is untouched by A's keepalive.
            assert leases[res_b].expires_at == before_b


def test_renew_all_takes_no_args_and_reports_zero_without_leases(
    client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as (ws, _):
        # Exact-arg-set discipline: any body field fails closed.
        bad = _ctrl(ws, "host-renew-all", {"reservation": "x"})
        assert bad["ok"] is False
        assert bad["error"] == "bad-request"
        # No leases: the keepalive succeeds and says so honestly.
        reply = _ctrl(ws, "host-renew-all", {})
        assert reply["ok"] is True
        assert reply["renewed"] == 0


def test_silent_connection_loses_all_routes_after_ttl(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    host_docs = _host("docs", PERSONA_A)
    host_app2 = _host("app2", PERSONA_A)
    with _tunnel(client, clock, root) as (ws, _):
        for res, host in (
            (_reservation(PERSONA_A, "docs"), host_docs),
            (_reservation(PERSONA_A, "app2"), host_app2),
        ):
            assert _ctrl(ws, "host-register", {
                "reservation": res, "host": host,
            })["ok"] is True
        clock.advance(relay_mod.HOST_LEASE_TTL - 1)
        assert app.state.host_routes.route(host_docs) is not None
        clock.advance(2)
        # Dead-man's switch: a connection that never renews loses every
        # route within one TTL, together.
        assert app.state.host_routes.route(host_docs) is None
        assert app.state.host_routes.route(host_app2) is None


def test_renew_all_keepalive_spans_many_old_ttls(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    host = _host("docs", PERSONA_A)
    res = _reservation(PERSONA_A, "docs")
    with _tunnel(client, clock, root) as (ws, _):
        assert _ctrl(ws, "host-register", {
            "reservation": res, "host": host,
        })["ok"] is True
        # Six half-life keepalives keep the route alive for 30 minutes —
        # far past the retired 120 s per-share TTL.
        for _ in range(6):
            clock.advance(relay_mod.HOST_LEASE_TTL // 2)
            assert _ctrl(ws, "host-renew-all", {})["renewed"] == 1
        assert app.state.host_routes.route(host) is not None


# -- auto-e2ufw: serving machine key domain + Option B allow-set -----------


def test_v2_hello_under_old_machine_domain_is_rejected(client, clock, root):
    """The domain flip is hard: a v2 hello whose machine_sig is under the
    retired fleet MACHINE_HELLO_DOMAIN no longer verifies."""
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    cert = _serve_cert(root, serve_key, PERSONA_A)
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, cert, machine_key=machine_key, org=ORG, ts=clock.now,
        caps=("host-lease/1",),
        machine_hello_domain=hello_mod.MACHINE_HELLO_DOMAIN,  # wrong domain
    )
    register(client, clock, root, org_uuid=ORG)
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(raw)
        ack = ws.receive_json()
        assert ack["ok"] is False


def test_empty_allowset_accepts_then_nonempty_enforces(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    store = app.state.store
    assert store.registered_serving_keys(ORG) == set()  # un-backfilled

    # Empty set: a valid serving-domain hello is accepted (transitional).
    mk1 = KeyPair.generate()
    with _tunnel(client, clock, root, machine_key=mk1) as (ws, _):
        assert _ctrl(ws, "host-register", {
            "reservation": _reservation(PERSONA_A, "docs"),
            "host": _host("docs", PERSONA_A),
        })["ok"] is True

    # Backfill registers a DIFFERENT machine key for the org.
    registered = KeyPair.generate()
    store.register_serving_machine_key(
        ORG, registered.public_hex, now=clock.now)
    assert store.count_orgs_with_serving_keys() == 1

    # Now the set is non-empty: an UNregistered serving machine (mk1) is
    # rejected even though its serving-domain co-signature is valid.
    unregistered = KeyPair.generate()
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(_hello_for(root, clock, unregistered, mk1))
        assert ws.receive_json()["ok"] is False

    # The registered machine key is accepted.
    with _tunnel(client, clock, root, machine_key=registered) as (ws, _):
        assert _ctrl(ws, "host-register", {
            "reservation": _reservation(PERSONA_A, "docs"),
            "host": _host("docs", PERSONA_A),
        })["ok"] is True


def _hello_for(root, clock, serve_key, machine_key, persona=PERSONA_A):
    return hello_mod.build_tunnel_hello_v2(
        serve_key, _serve_cert(root, serve_key, persona),
        machine_key=machine_key, org=ORG, ts=clock.now,
        caps=("host-lease/1",),
        machine_hello_domain=hello_mod.SERVING_MACHINE_HELLO_DOMAIN,
    )
