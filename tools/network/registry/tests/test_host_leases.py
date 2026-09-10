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


@contextlib.contextmanager
def _audit_records():
    """Collect the registry's audit logger. It sets propagate=False, so
    caplog's root handler never sees it — the handler has to go on it."""
    import logging

    collected = []

    class _Collect(logging.Handler):
        def emit(self, record):
            collected.append(record.getMessage())

    logger = logging.getLogger("autonomy.registry.audit")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)


def test_the_allowset_refusal_is_audited_not_silent(app, client, clock, root):
    """The moment this gate starts refusing must be visible ON THE REGISTRY.

    A HelloError is sent to the client and the socket closed, and nothing was
    logged here — so a stale allow-set presented as an identity fault in the
    rejected connector's log and left no trace on the side that decided it.
    Serving keys rotate (per-org derivation landing, certs re-minted), so this
    is the signal that says an allow-set needs re-registering rather than that
    a machine is an impostor.
    """
    register(client, clock, root, org_uuid=ORG)
    store = app.state.store
    registered = KeyPair.generate()
    store.register_serving_machine_key(
        ORG, registered.public_hex, now=clock.now)

    stranger = KeyPair.generate()
    serve_key = KeyPair.generate()
    with _audit_records() as records:
        with client.websocket_connect(f"/t/{ORG}") as ws:
            ws.send_text(_hello_for(root, clock, serve_key, stranger))
            assert ws.receive_json()["ok"] is False
    refusals = [line for line in records if "serving-key REFUSED" in line]
    assert len(refusals) == 1, records
    # Enough to diagnose without a second query: which org, which key was
    # presented, and how many ARE registered — so "wrong key" and "stale
    # allow-set" are distinguishable on sight.
    assert ORG[:8] in refusals[0]
    assert stranger.public_hex[:16] in refusals[0]
    assert "1 key(s) registered" in refusals[0]

    # And the accepted case emits no refusal.
    with _audit_records() as records:
        with _tunnel(client, clock, root, machine_key=registered) as (ws, _):
            assert _ctrl(ws, "host-register", {
                "reservation": _reservation(PERSONA_A, "docs"),
                "host": _host("docs", PERSONA_A),
            })["ok"] is True
    assert not [line for line in records if "REFUSED" in line]


def test_registering_under_the_genesis_id_leaves_enforcement_OFF(
    app, client, clock, root,
):
    """The allow-set is keyed by org_uuid. A backfill run against the GENESIS
    ID writes a row nothing reads, and the silent consequence is that the
    transitional accept stays open -- while the backfill tool's own success
    line reports the org now has a registered key.

    This drives the consequence rather than the identifier: an unregistered
    serving machine is still ACCEPTED after the wrong-keyed registration, and
    refused after the right-keyed one. Nothing here inspects the tool's help
    text; the guard it grew is exercised through its own entry point below.
    """
    register(client, clock, root, org_uuid=ORG)
    store = app.state.store
    genesis_id = "b1" * 32          # what derive_serving_machine_key takes
    assert genesis_id != ORG

    registered = KeyPair.generate()
    store.register_serving_machine_key(
        genesis_id, registered.public_hex, now=clock.now)
    # The row exists -- under a key the relay never looks up.
    assert store.registered_serving_keys(genesis_id) == {registered.public_hex}
    assert store.registered_serving_keys(ORG) == set()

    # So an UNREGISTERED serving machine is still admitted: enforcement never
    # turned on, and nothing said so.
    stranger = KeyPair.generate()
    with _tunnel(client, clock, root, machine_key=stranger) as (ws, _):
        assert _ctrl(ws, "host-register", {
            "reservation": _reservation(PERSONA_A, "docs"),
            "host": _host("docs", PERSONA_A),
        })["ok"] is True

    # The same registration under the org_uuid turns it on.
    store.register_serving_machine_key(ORG, registered.public_hex, now=clock.now)
    serve_key = KeyPair.generate()
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(_hello_for(root, clock, serve_key, stranger))
        assert ws.receive_json()["ok"] is False


def test_the_backfill_tool_refuses_a_genesis_id(tmp_path, monkeypatch):
    """The guard, through the tool's real entry point: a 64-hex value is a
    genesis id and must crash rather than write a row that confirms itself."""
    from tools.network.registry import backfill_serving_keys as backfill
    from tools.network.registry.store import RegistryStore

    db = tmp_path / "registry.db"
    RegistryStore(str(db))
    machine_pub = "cd" * 32

    def run(org):
        monkeypatch.setattr("sys.argv", [
            "backfill", "--db", str(db), "--org", org,
            "--machine", "m1", "--serving-pub", machine_pub,
        ])
        backfill.main()

    with pytest.raises(SystemExit) as exc:
        run("b1" * 32)
    assert "GENESIS ID" in str(exc.value)
    with pytest.raises(SystemExit):
        run("anchore")
    # Nothing was written by either refusal.
    store = RegistryStore(str(db))
    assert store.count_orgs_with_serving_keys() == 0

    # Uppercase either value and the row would be written but never matched:
    # the lookup is a literal string compare and the hello's machine field is
    # pinned lowercase. Both refused.
    with pytest.raises(SystemExit) as exc:
        run("C8E5CD04-8F19-4BC2-8951-A6B6B80B2699")
    assert "LOWERCASE" in str(exc.value)
    store = RegistryStore(str(db))
    assert store.count_orgs_with_serving_keys() == 0

    org_uuid = "c8e5cd04-8f19-4bc2-8951-a6b6b80b2699"
    monkeypatch.setattr("sys.argv", [
        "backfill", "--db", str(db), "--org", org_uuid,
        "--machine", "m1", "--serving-pub", machine_pub.upper(),
    ])
    with pytest.raises(SystemExit) as exc:
        backfill.main()
    assert "LOWERCASE" in str(exc.value)
    store = RegistryStore(str(db))
    assert store.count_orgs_with_serving_keys() == 0

    # The org_uuid form writes exactly one row, and is idempotent.
    run(org_uuid)
    run(org_uuid)
    store = RegistryStore(str(db))
    assert store.registered_serving_keys(org_uuid) == {machine_pub}
    assert store.count_orgs_with_serving_keys() == 1


def test_a_stale_manifest_is_refused(tmp_path, monkeypatch):
    """Serving keys rotate, so a manifest has a shelf life.

    76d61b5b moved home's three org connectors from its durable roster key to
    per-org keys in the middle of this work; an allow-set registered from
    values collected before it would have hard-gated all three at the hello.
    So `register` refuses a manifest older than the bound, and refuses two
    manifests whose commits disagree, rather than writing a gate from values
    that may already be wrong.
    """
    import json
    import time as _time

    from tools.network.registry import backfill_serving_keys as backfill
    from tools.network.registry.store import RegistryStore

    db = tmp_path / "registry.db"
    RegistryStore(str(db))
    entry = {"scope": "anchore",
             "org_uuid": "c8e5cd04-8f19-4bc2-8951-a6b6b80b2699",
             "serving_pub": "ab" * 32}

    def write(name, *, age_s, commit="c0c69985"):
        path = tmp_path / name
        path.write_text(json.dumps({
            "collected_at": int(_time.time()) - age_s,
            "boot_commit": commit,
            "entries": [dict(entry)],
        }))
        return str(path)

    def run(*paths):
        monkeypatch.setattr("sys.argv", [
            "backfill", "--db", str(db), *sum((["--manifest", p] for p in paths), []),
        ])
        backfill.main()

    stale = write("stale.json", age_s=backfill.MANIFEST_MAX_AGE_S + 60)
    with pytest.raises(SystemExit) as exc:
        run(stale)
    assert "older than" in str(exc.value)
    assert RegistryStore(str(db)).count_orgs_with_serving_keys() == 0

    # Two machines on different commits: one of them may predate a derivation
    # change, and we cannot tell which from here.
    fresh_a = write("a.json", age_s=5, commit="c0c69985")
    fresh_b = write("b.json", age_s=5, commit="76d61b5b")
    with pytest.raises(SystemExit) as exc:
        run(fresh_a, fresh_b)
    assert "different commits" in str(exc.value)
    assert RegistryStore(str(db)).count_orgs_with_serving_keys() == 0

    # Fresh and in agreement: registered, and read back.
    run(fresh_a)
    store = RegistryStore(str(db))
    assert store.registered_serving_keys(entry["org_uuid"]) == {entry["serving_pub"]}


def test_the_backfill_tool_reads_back_what_relay_will_look_up(
    tmp_path, monkeypatch, capsys,
):
    """It reports what it ACHIEVED, not what it attempted.

    A write followed by a lookup through the same function relay.py calls is
    what catches the mistakes no guard anticipated. Driven here by making that
    lookup answer wrongly: the tool must refuse to call it a backfill.
    """
    from tools.network.registry import backfill_serving_keys as backfill
    from tools.network.registry.store import RegistryStore

    db = tmp_path / "registry.db"
    RegistryStore(str(db))
    org_uuid = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
    machine_pub = "ab" * 32
    argv = ["backfill", "--db", str(db), "--org", org_uuid,
            "--machine", "m1", "--serving-pub", machine_pub]

    monkeypatch.setattr("sys.argv", argv)
    backfill.main()
    out = capsys.readouterr().out
    assert machine_pub in out and org_uuid in out, out

    # Now make the read-back disagree with the write, the way a wrong key or a
    # second database would. The tool must NOT report success.
    monkeypatch.setattr(
        RegistryStore, "registered_serving_keys", lambda self, org: set())
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit) as exc:
        backfill.main()
    assert "READ-BACK FAILED" in str(exc.value)


def _hello_for(root, clock, serve_key, machine_key, persona=PERSONA_A):
    return hello_mod.build_tunnel_hello_v2(
        serve_key, _serve_cert(root, serve_key, persona),
        machine_key=machine_key, org=ORG, ts=clock.now,
        caps=("host-lease/1",),
        machine_hello_domain=hello_mod.SERVING_MACHINE_HELLO_DOMAIN,
    )
