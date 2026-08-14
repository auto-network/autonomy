"""The dashboard-origin invite resolve endpoint (auto-yw5gz).

Paste-into-your-own-dashboard: the page hands the resolve endpoint the
TRANSPORT credentials only ({relay_host, channel_token}); the endpoint
pins the org root (from the link host's public envelope, or the handoff
link's own query), opens the root-pinned E2E join channel, and returns the
org-served context + verified identity. The bearer (#t=) is NEVER accepted
here — the whole point is that it stays in the browser.

The transport is the one seam these tests double: ``_read_org_context``
(and, for the /l/ paste, ``_fetch_link_envelope``) are stubbed for the
pure-logic walks, and one integration walk binds a REAL relaykit-shaped
channel to the REAL org:join handler over a REAL founded org, so the
context read is exercised end to end minus the websocket bytes relaykit
already proves.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import network_routes

ORG_UUID = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
ROOT_PUB = "a" * 64
INVITE_REF = "e" * 64
TOKEN = "7f" * 16
RELAY = "https://relay.auto.network"

ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
IDENTITY = {
    "status": "ok",
    "org_name": "Northwind Robotics",
    "org_color": "#2f6f4f",
    "org_description": "We build warehouse automation.",
    "org_icon": ICON,
}


@pytest.fixture
def client():
    app = Starlette(routes=network_routes.ROUTES)
    return TestClient(app)


def _post(client, body):
    return client.post("/api/network/invite/resolve", json=body)


# -- input is transport credentials only; the bearer can never ride in --------


def test_an_unexpected_key_is_a_hard_refusal(client):
    # 't', 'bearer', or anything outside the vocabulary is refused, not
    # ignored — the ledger bearer can never reach a server, including ours.
    for smuggle in ({"t": "secret"}, {"bearer": "secret"}, {"passphrase": "x"}):
        resp = _post(client, {"relay_host": RELAY, "channel_token": TOKEN, **smuggle})
        assert resp.status_code == 400, smuggle
        assert "unexpected keys" in resp.json()["error"]


def test_channel_token_must_be_32_hex(client):
    for bad in ("", "zz" * 16, "7f" * 15, TOKEN + "ab"):
        resp = _post(client, {"relay_host": RELAY, "channel_token": bad})
        assert resp.status_code == 400, bad


def test_relay_host_must_be_an_http_origin(client):
    for bad in ("", "ftp://relay", "ws://relay", "//nohost"):
        resp = _post(client, {"relay_host": bad, "channel_token": TOKEN})
        assert resp.status_code == 400, bad


def test_partial_handoff_context_is_refused(client):
    # org/root_pub/invite_ref are all-or-nothing and shape-checked.
    resp = _post(client, {"relay_host": RELAY, "channel_token": TOKEN, "org": ORG_UUID})
    assert resp.status_code == 400
    resp = _post(client, {
        "relay_host": RELAY, "channel_token": TOKEN,
        "org": "not-a-uuid", "root_pub": ROOT_PUB, "invite_ref": INVITE_REF,
    })
    assert resp.status_code == 400


# -- the /l/ paste path: pin from the public envelope, then read the org ------


def test_share_link_resolves_to_the_verified_identity(client, monkeypatch):
    seen = {}

    async def fake_envelope(http_base, token):
        seen["envelope"] = (http_base, token)
        return {"org": ORG_UUID, "root_pub": ROOT_PUB, "invite_ref": INVITE_REF}

    async def fake_context(ws_base, token, *, root_pub, org):
        seen["context"] = {"ws_base": ws_base, "token": token,
                           "root_pub": root_pub, "org": org}
        return dict(IDENTITY)

    monkeypatch.setattr(network_routes, "_fetch_link_envelope", fake_envelope)
    monkeypatch.setattr(network_routes, "_read_org_context", fake_context)

    resp = _post(client, {"relay_host": RELAY, "channel_token": TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "org": ORG_UUID,
        "invite_ref": INVITE_REF,
        "org_name": "Northwind Robotics",
        "org_color": "#2f6f4f",
        "org_description": "We build warehouse automation.",
        "org_icon": ICON,
    }
    # The pin was established from the link's OWN host before any channel byte,
    # and the channel dialed that same relay over wss with the pinned root.
    assert seen["envelope"] == ("https://relay.auto.network", TOKEN)
    assert seen["context"]["root_pub"] == ROOT_PUB
    assert seen["context"]["org"] == ORG_UUID
    assert seen["context"]["ws_base"] == "wss://relay.auto.network"


def test_older_org_node_degrades_to_the_minimal_form(client, monkeypatch):
    # Envelope resolves (id + invite ref known) but the org node cannot serve
    # identity — the minimal verified-org step stands, never an error page.
    async def fake_envelope(http_base, token):
        return {"org": ORG_UUID, "root_pub": ROOT_PUB, "invite_ref": INVITE_REF}

    async def no_context(ws_base, token, *, root_pub, org):
        return None

    monkeypatch.setattr(network_routes, "_fetch_link_envelope", fake_envelope)
    monkeypatch.setattr(network_routes, "_read_org_context", no_context)

    body = _post(client, {"relay_host": RELAY, "channel_token": TOKEN}).json()
    assert body == {"ok": True, "org": ORG_UUID, "invite_ref": INVITE_REF}


def test_relay_unreachable_is_an_honest_miss_not_an_error(client, monkeypatch):
    async def no_envelope(http_base, token):
        return None

    monkeypatch.setattr(network_routes, "_fetch_link_envelope", no_envelope)
    resp = _post(client, {"relay_host": RELAY, "channel_token": TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "unreachable"}


# -- the handoff path: pin is already in the query, no second registry hop ----


def test_handoff_context_skips_the_envelope(client, monkeypatch):
    called = {"envelope": False}

    async def envelope_should_not_run(http_base, token):
        called["envelope"] = True
        return None

    async def fake_context(ws_base, token, *, root_pub, org):
        return dict(IDENTITY)

    monkeypatch.setattr(network_routes, "_fetch_link_envelope",
                        envelope_should_not_run)
    monkeypatch.setattr(network_routes, "_read_org_context", fake_context)

    body = _post(client, {
        "relay_host": RELAY, "channel_token": TOKEN,
        "org": ORG_UUID, "root_pub": ROOT_PUB, "invite_ref": INVITE_REF,
    }).json()
    assert called["envelope"] is False
    assert body["ok"] is True and body["org_name"] == "Northwind Robotics"


# -- identity guards MATCH the bridge (safeColor / safeIcon) -------------------


def test_unsafe_identity_fields_are_dropped(client, monkeypatch):
    hostile = {
        "status": "ok",
        "org_name": "x" * 400,                       # clamped
        "org_color": "red; background:url(x)",        # not bare hex → dropped
        "org_description": "d" * 400,                 # clamped
        "org_icon": "https://evil.example/pixel.png",  # remote URL → dropped
    }

    async def fake_envelope(http_base, token):
        return {"org": ORG_UUID, "root_pub": ROOT_PUB, "invite_ref": INVITE_REF}

    async def fake_context(ws_base, token, *, root_pub, org):
        return hostile

    monkeypatch.setattr(network_routes, "_fetch_link_envelope", fake_envelope)
    monkeypatch.setattr(network_routes, "_read_org_context", fake_context)

    body = _post(client, {"relay_host": RELAY, "channel_token": TOKEN}).json()
    assert "org_color" not in body        # arbitrary CSS never emitted
    assert "org_icon" not in body         # remote URL never emitted
    assert len(body["org_name"]) == 120   # clamped
    assert len(body["org_description"]) == 300


def test_safe_identity_and_relay_bases_units():
    assert network_routes._safe_identity(IDENTITY) == {
        "org_name": "Northwind Robotics",
        "org_color": "#2f6f4f",
        "org_description": "We build warehouse automation.",
        "org_icon": ICON,
    }
    assert network_routes._safe_identity({"org_icon": "x" * 400001}) == {}
    http_base, ws_base, err = network_routes._relay_bases("relay.auto.network")
    assert (http_base, ws_base, err) == (
        "https://relay.auto.network", "wss://relay.auto.network", None)
    http_base, ws_base, err = network_routes._relay_bases("http://127.0.0.1:8000")
    assert (http_base, ws_base) == ("http://127.0.0.1:8000", "ws://127.0.0.1:8000")


# -- integration: a REAL org:join handler over a relaykit-shaped channel ------


class _FakeViewerChannel:
    """The relaykit ViewerChannel seam, bound to the real grant handler.

    send/recv are the same two calls the endpoint makes on a real channel;
    the handler returns exactly the bytes a real connector would seal.
    """

    def __init__(self, handler, token):
        self._handler = handler
        self._token = token
        self._sent = None

    async def send_message(self, data):
        self._sent = data

    async def recv_message(self):
        return await self._handler(self._token, self._sent)

    async def close(self):
        pass


def test_resolve_reads_context_over_the_real_join_handler(client, tmp_path,
                                                          monkeypatch):
    from tools.graph.db import GraphDB
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
    )
    from tools.network.idkit import KeyPair, generate_token
    from tools.network.ledger import (
        HLC, LedgerStore, make_event, org_ledger_db_path,
    )
    from tools.network.ledger.found import found_org_ledger
    from tools.network.relaykit import viewer
    from tools.dashboard import link_serving

    org_slug = "resolveorg"
    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("personal", type_="personal", root=tmp_path).close()
    GraphDB.create_org_db(org_slug, root=tmp_path).close()

    root = KeyPair.generate()
    store = LedgerStore(org_ledger_db_path(org_slug))
    founded = found_org_ledger(
        store, org_id=ORG_UUID, org_root=root,
        personal_root_seed=os.urandom(32), now=1_800_000_000_000,
    )
    ts = 1_800_000_010_000

    def emit(payload):
        nonlocal ts
        ts += 1_000
        return store.append(
            make_event(root, payload, sorted(store.heads()), HLC(ts))
        )

    emit({"type": "role.define", "name": "member", "scope_set": ["link:publish"],
          "claim_requires": "admin-ack", "version": 1})
    token = generate_token()
    invite_ref = emit({
        "type": "invite", "granted_role": "member", "expiry": ts + 10 ** 9,
        "sponsor": root.public_hex,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
    })
    store.close()

    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, TOKEN,
        {
            "token": TOKEN, "target_uuid": ORG_UUID, "target_type": "org:join",
            "invite_ref": invite_ref,
            "subject": {"kind": "operator", "id": "join-issuer"},
            "url": f"https://relay.auto.network/l/{TOKEN}",
            "issued_at": "2026-08-14T00:00:00Z", "meta": {},
        },
        org=org_slug, state="canonical",
    )

    handler = link_serving.make_grant_handler(org_slug)

    async def fake_connect(ws_base, tok, *, root_pub, org, now=None,
                          open_timeout=10.0):
        assert root_pub == root.public_hex  # the endpoint pinned the real root
        return _FakeViewerChannel(handler, tok)

    monkeypatch.setattr(viewer.ViewerChannel, "connect", staticmethod(fake_connect))

    body = _post(client, {
        "relay_host": RELAY, "channel_token": TOKEN,
        "org": ORG_UUID, "root_pub": root.public_hex, "invite_ref": invite_ref,
    }).json()

    # The real handler served an org:join context over the channel: status ok,
    # so the resolve reports the verified-org step for this real founding.
    assert body["ok"] is True
    assert body["org"] == ORG_UUID
    assert body["invite_ref"] == invite_ref
    assert founded.genesis_id  # the org really was founded
    GraphDB.close_all_pooled()
