"""auto-bhs3c: serve.dns01.present / serve.dns01.cleanup over the tunnel.

The record name is DERIVED from the tunnel persona's serving-label
binding — never body-supplied. Authority is a second, attenuated chain
(scope exactly serve:dns-01, subject == tunnel persona) verified per op.
Every negative replies the uniform {"ok": false, "error": "refused"}.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import uuid

import pytest

from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.relaykit import hello as hello_mod
from tools.network.relaykit.frames import (
    CTRL_CHANNEL_ID,
    FRAME_CTRL,
    decode_frame,
    encode_frame,
)
from tools.network.registry.relay import DNS01_DOMAIN

from .conftest import DAY, NOW, ORG, register

PERSONA = "ab" * 32
PERSONA_OTHER = "cd" * 32
RESERVATION_NS = uuid.UUID("6cf440db-c8b4-566c-99db-e7be17109bdc")
CAPS = ("dns-01/1", "host-lease/1")
SUFFIX = hashlib.sha256(bytes.fromhex(PERSONA)).hexdigest()[:20]
LABEL = f"worker-{SUFFIX}"
NAME = f"_acme-challenge.{LABEL}.serve.auto.network."


def _serve_cert(root, key, persona=PERSONA):
    return issue_cert(
        root, key.public_hex, scope=("tunnel:serve",), org=ORG,
        subject=Subject("persona", persona),
        not_before=NOW - 100, not_after=NOW + 30 * DAY,
    )


def _dns01_cert(root, key, persona=PERSONA, scope=("serve:dns-01",)):
    return issue_cert(
        root, key.public_hex, scope=scope, org=ORG,
        subject=Subject("persona", persona),
        not_before=NOW - 100, not_after=NOW + 900,
    )


@contextlib.contextmanager
def _tunnel(client, clock, root, *, persona=PERSONA, caps=CAPS):
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, _serve_cert(root, serve_key, persona),
        machine_key=machine_key, org=ORG, ts=clock.now, caps=caps,
    )
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(raw)
        assert ws.receive_json()["ok"] is True
        yield ws


_SEQ = iter(range(100_000))


def _ctrl(ws, op, args):
    correlation = format(next(_SEQ), "032x")
    ws.send_bytes(encode_frame(
        FRAME_CTRL, CTRL_CHANNEL_ID,
        json.dumps({"id": correlation, "op": op, "args": args}).encode(),
    ))
    reply = json.loads(decode_frame(ws.receive_bytes()).payload.decode())
    assert reply["id"] == correlation
    return reply


def _bind_label(ws):
    app = "docs"
    reservation = str(uuid.uuid5(RESERVATION_NS, f"{PERSONA}\0{app}"))
    host = f"{app}.{LABEL}.serve.auto.network"
    reply = _ctrl(ws, "host-register", {
        "reservation": reservation, "host": host,
    })
    assert reply["ok"] is True, reply


def _present_args(root, clock, *, op="serve.dns01.present", order="order-1",
                  value="tok-1", ttl=120, expiry_in=600, expiry=None,
                  ts=None, persona=PERSONA, scope=("serve:dns-01",),
                  key=None, sign_with=None):
    key = key or KeyPair.generate()
    cert = _dns01_cert(root, key, persona, scope)
    ts = clock.now if ts is None else ts
    expiry = ts + expiry_in if expiry is None else expiry
    core = {"op": op, "order": order, "value": value,
            "ttl": ttl, "expiry": expiry, "ts": ts}
    signer = sign_with or key
    return {
        "order": order, "value": value, "ttl": ttl, "expiry": expiry,
        "ts": ts, "cert": cert.to_json().decode("ascii"),
        "sig": signer.sign_hex(DNS01_DOMAIN + canonical_json(core)),
    }


def _cleanup_args(root, clock, *, order="order-1", value="tok-1", ts=None):
    key = KeyPair.generate()
    cert = _dns01_cert(root, key)
    ts = clock.now if ts is None else ts
    core = {"op": "serve.dns01.cleanup", "order": order, "value": value,
            "ts": ts}
    return {
        "order": order, "value": value, "ts": ts,
        "cert": cert.to_json().decode("ascii"),
        "sig": key.sign_hex(DNS01_DOMAIN + canonical_json(core)),
    }


def _live(app):
    return app.state.store.live_serve_challenges(
        now=int(app.state.now_fn()))


def test_present_derives_name_and_serves_value(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        reply = _ctrl(ws, "serve.dns01.present",
                      _present_args(root, clock))
        assert reply["ok"] is True, reply
        assert reply["name"] == NAME.rstrip(".")
        assert reply["expires_at"] == clock.now + 600
        live = _live(app)
        assert live[NAME]["values"] == ["tok-1"]
        assert live[NAME]["ttl"] == 120


def test_simultaneous_orders_coexist_and_cleanup_is_order_scoped(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        assert _ctrl(ws, "serve.dns01.present", _present_args(
            root, clock, order="apex-order", value="apex-tok"))["ok"]
        assert _ctrl(ws, "serve.dns01.present", _present_args(
            root, clock, order="wild-order", value="wild-tok"))["ok"]
        assert sorted(_live(app)[NAME]["values"]) == [
            "apex-tok", "wild-tok"]
        # Cleanup with the WRONG order does not remove the value.
        assert _ctrl(ws, "serve.dns01.cleanup", _cleanup_args(
            root, clock, order="wrong-order", value="apex-tok"))["ok"]
        assert "apex-tok" in _live(app)[NAME]["values"]
        # The right order removes exactly its own value.
        assert _ctrl(ws, "serve.dns01.cleanup", _cleanup_args(
            root, clock, order="apex-order", value="apex-tok"))["ok"]
        assert _live(app)[NAME]["values"] == ["wild-tok"]


def test_replay_is_idempotent_expiry_refresh(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        args = _present_args(root, clock, expiry_in=100)
        assert _ctrl(ws, "serve.dns01.present", args)["ok"]
        clock.advance(90)
        args2 = _present_args(root, clock, expiry_in=100)
        assert _ctrl(ws, "serve.dns01.present", args2)["ok"]
        clock.advance(90)  # past first expiry, inside refreshed
        assert _live(app)[NAME]["values"] == ["tok-1"]


def test_ttl_clamped_and_deadline_is_the_signed_absolute(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        reply = _ctrl(ws, "serve.dns01.present", _present_args(
            root, clock, ttl=5, expiry_in=900))
        assert reply["ok"] is True
        assert reply["expires_at"] == clock.now + 900   # the signed deadline
        assert _live(app)[NAME]["ttl"] == 30            # floor


def test_replay_reasserts_but_never_extends_the_signed_deadline(
    app, client, clock, root,
):
    """The checkpoint correction: a captured present replayed inside the
    skew window re-asserts its ORIGINAL deadline — the 15-minute bound
    holds from the signed act, not from the replay."""
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        args = _present_args(root, clock, expiry_in=120)
        deadline = args["expiry"]
        assert _ctrl(ws, "serve.dns01.present", args)["ok"]
        clock.advance(100)  # inside skew, near the deadline
        reply = _ctrl(ws, "serve.dns01.present", args)  # exact replay
        assert reply["ok"] is True
        assert reply["expires_at"] == deadline          # NOT extended
        clock.advance(30)   # past the signed deadline
        assert _live(app) == {}


@pytest.mark.parametrize("mutate", [
    lambda a, root, clock: a.update(value="x" * 256),
    lambda a, root, clock: a.update(order="bad order!"),
    lambda a, root, clock: a.update(ts=a["ts"] - 9999),        # skew
    lambda a, root, clock: a.update(sig="0" * 128),            # bad sig
    lambda a, root, clock: a.update(extra=1),                  # arg set
    lambda a, root, clock: a.pop("expiry"),
    lambda a, root, clock: a.update(_present_args(
        root, clock, expiry_in=30)),                    # deadline < 60s
    lambda a, root, clock: a.update(_present_args(
        root, clock, expiry_in=1200)),                  # deadline > 900s
    lambda a, root, clock: a.update(_present_args(
        root, clock, scope=("tunnel:serve",))),                # wrong scope
    lambda a, root, clock: a.update(_present_args(
        root, clock, persona=PERSONA_OTHER)),                  # wrong subject
])
def test_every_negative_is_uniformly_refused(
    app, client, clock, root, mutate,
):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        args = _present_args(root, clock)
        mutate(args, root, clock)
        reply = _ctrl(ws, "serve.dns01.present", args)
        assert reply["ok"] is False
        assert reply["error"] == "refused"
    assert _live(app) == {}


def test_no_label_binding_is_refused(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        reply = _ctrl(ws, "serve.dns01.present",
                      _present_args(root, clock))
        assert reply == {"id": reply["id"], "ok": False,
                         "error": "refused"}
    assert _live(app) == {}


def test_missing_capability_is_refused(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root, caps=("host-lease/1",)) as ws:
        _bind_label(ws)
        reply = _ctrl(ws, "serve.dns01.present",
                      _present_args(root, clock))
        assert reply["ok"] is False and reply["error"] == "refused"
    assert _live(app) == {}


def test_value_cap_refused_uniformly(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        _bind_label(ws)
        for i in range(8):
            assert _ctrl(ws, "serve.dns01.present", _present_args(
                root, clock, order=f"o{i}", value=f"v{i}"))["ok"]
        reply = _ctrl(ws, "serve.dns01.present", _present_args(
            root, clock, order="o9", value="v9"))
        assert reply["ok"] is False and reply["error"] == "refused"
