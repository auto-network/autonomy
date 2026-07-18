"""C3 share-link approval kinds: link_publish / link_revoke end-to-end.

Drives the generalized approval primitive exactly the way the pieces do in
production: the CLI's request shape on POST /api/approvals, the browser's
enrichment GET (resolved target title, staged registry request), and a
decision carrying a session-key-signed envelope. The registry is the REAL
B1 FastAPI app mounted in-process through httpx.ASGITransport — grants are
issued by actual chain verification, not a stub — and the grant cache is
real Settings rows in a tmp GRAPH_DB.

Invariant coverage: I6 (cached grant records the issuing cert subject;
root-direct refused), I2 (only CSPRNG-shaped tokens enter the cache),
staged-request integrity (tampered envelope payload refused), and the C2
seam (no envelope → clean error, nothing published).
"""

from __future__ import annotations

import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import approvals_routes, link_approvals
from tools.dashboard.dao import approval_requests as ar
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_SET_ID,
    NETWORK_BINDING_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import create_app as create_registry_app
from tools.network.registry.signing import sign_request

ORG = "netorg"  # dashboard-side org slug (Settings scope)
ORG_UUID = "11111111-1111-4111-8111-111111111111"
TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "auto-agent-1"
OPERATOR_SESSION = "op-session-1"
REGISTRY_URL = "http://registry.test"

SESSION_SCOPE = ("delegate:agent", "link:publish", "link:revoke",
                 "tunnel:serve", "viewer:identify")


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def session_key():
    return KeyPair.generate()


@pytest.fixture
def session_cert(root, session_key):
    """Operator sign-on cert (what C2 mints): root -> session key."""
    now = int(time.time())
    return issue_cert(
        root, session_key.public_hex, scope=SESSION_SCOPE, org=ORG_UUID,
        subject=Subject("operator", OPERATOR_SESSION),
        not_before=now - 3600, not_after=now + 30 * 86400,
    )


@pytest.fixture
def registry_app(root):
    """The real B1 registry with our org bound to *root*."""
    app = create_registry_app(":memory:", base_url=REGISTRY_URL,
                              secure_cookies=False)
    rc = TestClient(app)
    envelope = sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "recovery_policy": "none"},
        ts=int(time.time()),
    )
    r = rc.post("/v1/orgs", json=envelope)
    assert r.status_code == 201, r.json()
    return app


@pytest.fixture
def env(tmp_path, monkeypatch, registry_app, root):
    """Approvals app + tmp Settings DB + registry routed through ASGI."""
    from tools.graph.db import GraphDB

    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_ORG", raising=False)

    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )

    def fake_registry_client(base_url):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url)

    monkeypatch.setattr(link_approvals, "_registry_client", fake_registry_client)

    with TestClient(Starlette(routes=approvals_routes.ROUTES)) as client:
        yield client
    GraphDB.close_all_pooled()


def _create_publish(client, meta=None):
    """POST the approval exactly the way `graph link publish` does."""
    r = client.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {"org": ORG, "target_uuid": TARGET,
                    "target_type": "present", "meta": meta or {}},
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _signed_envelope(session_key, session_cert, rr):
    """What the browser's C2 signer produces for the staged request."""
    return sign_request(
        session_key, rr["method"], rr["path"], rr["payload"],
        ts=int(time.time()), cert=session_cert,
    )


def _decide_and_wait(client, rid, body):
    ok = client.post(f"/api/approvals/{rid}/decision", json=body)
    assert ok.status_code == 200, ok.text
    for _ in range(50):
        d = client.get(f"/api/approvals/{rid}?wait=2").json()
        if d["result"] is not None:
            return d["result"]
    raise AssertionError("decision result never landed")


def _cached_grants():
    return {m.key: m.payload
            for m in settings_ops.read_set(NETWORK_LINK_GRANT_SET_ID, org=ORG)}


def _approve_body(envelope, rr=None):
    """What the browser posts: just the verdict + the signed envelope. The
    destination is frozen server-side at render; the decision cannot carry
    or influence it."""
    return {"approved": True, "envelope": envelope}


def _publish(client, session_key, session_cert, meta=None):
    """Full happy path: create → enrich → sign → approve → executed result."""
    rid = _create_publish(client, meta=meta)
    enriched = client.get(f"/api/approvals/{rid}").json()
    rr = enriched["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    result = _decide_and_wait(client, rid, _approve_body(envelope, rr))
    return rid, enriched, result


def test_publish_end_to_end(env, session_key, session_cert):
    rid, enriched, result = _publish(env, session_key, session_cert,
                                     meta={"ttl": 3600, "label": "binder"})
    assert result["approved"] is True
    execution = result["execution"]
    assert execution["ok"] is True, execution
    token = execution["token"]
    assert len(token) == 32 and int(token, 16) >= 0  # opaque 128-bit hex (I2)
    assert execution["url"] == f"{REGISTRY_URL}/l/{token}"
    # the grant cache row the serving path (I9) will read
    grants = _cached_grants()
    assert token in grants
    assert grants[token]["target_uuid"] == TARGET
    assert grants[token]["meta"] == {"ttl": 3600, "label": "binder"}


def test_grant_records_issuing_subject_i6(env, session_key, session_cert):
    _, _, result = _publish(env, session_key, session_cert)
    token = result["execution"]["token"]
    # I6: the stored grant is attributable to the operator session that signed
    assert _cached_grants()[token]["subject"] == {
        "kind": "operator", "id": OPERATOR_SESSION}


def test_enrichment_renders_target_and_ttl(env, tmp_path, monkeypatch):
    """What the operator reviews: real title from Design Studio + the TTL."""
    from agents import design_db
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "designs.db")
    monkeypatch.setattr(design_db, "_initialized", False)  # re-init at tmp path
    rev_id = design_db.create_design(
        title="OSS Insights briefing binder",
        variants=[{"id": "a", "html": "<section>hi</section>"}])

    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {"org": ORG, "target_uuid": rev_id,
                    "target_type": "present", "meta": {"ttl": 7 * 86400}},
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    assert enriched["target_title"] == "OSS Insights briefing binder"
    assert enriched["type_label"] == "Present deck"
    assert enriched["ttl"] == 7 * 86400
    assert enriched["registry_request"]["payload"]["target_uuid"] == rev_id


def test_decline_surfaces_to_requester(env):
    rid = _create_publish(env)
    result = _decide_and_wait(env, rid, {"approved": False})
    assert result == {"approved": False}          # the CLI's clean-deny state
    assert _cached_grants() == {}                 # nothing published, nothing cached
    assert ar.pending_for_session(SESSION) is None


def test_no_envelope_is_a_clean_c2_error(env):
    """Approving without a signed envelope (C2 absent) fails cleanly."""
    rid = _create_publish(env)
    result = _decide_and_wait(env, rid, {"approved": True})
    execution = result["execution"]
    assert execution["ok"] is False
    assert "C2" in execution["error"]
    assert _cached_grants() == {}


def test_root_direct_envelope_refused_i6(env, root):
    """A certless (root-direct) envelope names no subject — refused."""
    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = sign_request(root, rr["method"], rr["path"], rr["payload"],
                            ts=int(time.time()))
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))
    assert result["execution"]["ok"] is False
    assert "I6" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_tampered_payload_refused(env, session_key, session_cert):
    """The signed payload must be exactly what the operator approved."""
    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    tampered = dict(rr["payload"], target_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    envelope = sign_request(session_key, rr["method"], rr["path"], tampered,
                            ts=int(time.time()), cert=session_cert)
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))
    assert result["execution"]["ok"] is False
    assert "does not match" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_binding_swap_between_render_and_approve_refused(
        env, session_key, session_cert, registry_app, monkeypatch):
    """Confused-deputy regression (Codex validator finding on C3): the org
    binding's registry_url is swapped AFTER the dialog renders (and the
    envelope is signed) but BEFORE the approval executes. The operator
    approved 'publish to registry.test'; nothing may be forwarded to the
    swapped destination — the execution must be REFUSED and no grant cached.
    """
    forwarded = []

    def recording_client(base_url):
        forwarded.append(base_url)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url)

    monkeypatch.setattr(link_approvals, "_registry_client", recording_client)

    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    assert rr["registry_url"] == REGISTRY_URL          # what the operator sees
    envelope = _signed_envelope(session_key, session_cert, rr)

    # The attacker swaps the binding under the pending approval.
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": envelope["signer"],  # any valid pub; url is the attack
            "registry_url": "http://evil-registry.test",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )

    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))
    execution = result["execution"]
    assert execution["ok"] is False
    assert "changed between review and approval" in execution["error"]
    assert forwarded == []          # the signed request never left the house
    assert _cached_grants() == {}   # and nothing entered the serving cache


def test_forged_audience_claim_ignored(env, session_key, session_cert,
                                       registry_app, monkeypatch):
    """Codex escalation repro: the attacker swaps the binding to evil AND
    forges the decision body to claim registry_url=evil so a client-trusting
    check would 'match'. The executor takes its destination from the
    server-frozen snapshot only — the forged claim changes nothing, the
    drift refuses, and nothing is ever forwarded to evil."""
    forwarded = []

    def recording_client(base_url):
        forwarded.append(base_url)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url)

    monkeypatch.setattr(link_approvals, "_registry_client", recording_client)

    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": envelope["signer"],
            "registry_url": "http://evil-registry.test",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )
    result = _decide_and_wait(env, rid, {
        "approved": True, "envelope": envelope,
        "registry_url": "http://evil-registry.test",   # the forged claim
    })
    execution = result["execution"]
    assert execution["ok"] is False
    assert "changed between review and approval" in execution["error"]
    assert "http://evil-registry.test" not in forwarded
    assert forwarded == []
    assert _cached_grants() == {}


def test_root_pub_swap_after_render_refused(env, session_key, session_cert,
                                            registry_app, root, monkeypatch):
    """Codex escalation repro: same registry_url, but the binding's root_pub
    is swapped after render (an org-identity substitution). The frozen
    binding snapshot catches it; execution refuses, nothing forwarded."""
    forwarded = []

    def recording_client(base_url):
        forwarded.append(base_url)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url)

    monkeypatch.setattr(link_approvals, "_registry_client", recording_client)

    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": KeyPair.generate().public_hex,   # swapped identity
            "registry_url": REGISTRY_URL,                # url unchanged
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )
    result = _decide_and_wait(env, rid, _approve_body(envelope))
    execution = result["execution"]
    assert execution["ok"] is False
    assert "root_pub" in execution["error"]
    assert forwarded == []
    assert _cached_grants() == {}


def test_unstaged_approval_refused(env, session_key, session_cert):
    """No render, no freeze, no execution: a decision on a request that was
    never staged server-side is refused — the client cannot substitute its
    own idea of the registry request."""
    rid = _create_publish(env)
    payload = {"org": ORG_UUID, "target_uuid": TARGET, "target_type": "present"}
    envelope = sign_request(session_key, "POST", "/v1/links", payload,
                            ts=int(time.time()), cert=session_cert)
    result = _decide_and_wait(env, rid, _approve_body(envelope))
    assert result["execution"]["ok"] is False
    assert "never staged" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_rerender_shows_frozen_destination_and_drift(env, session_key,
                                                     session_cert):
    """A re-open after a binding swap still shows the FROZEN destination —
    the operator can never see (and approve) a moved target — plus a drift
    flag the dialog turns into a warning."""
    rid = _create_publish(env)
    first = env.get(f"/api/approvals/{rid}").json()
    assert first["registry_request"]["registry_url"] == REGISTRY_URL
    assert first["binding_drift"] is False
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": KeyPair.generate().public_hex,
            "registry_url": "http://evil-registry.test",
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )
    second = env.get(f"/api/approvals/{rid}").json()
    assert second["registry_request"]["registry_url"] == REGISTRY_URL  # frozen
    assert second["binding_drift"] is True


def test_registry_rejection_propagates(env, session_key, root):
    """A cert without link:publish scope: registry 403 comes back readable."""
    now = int(time.time())
    weak_cert = issue_cert(
        root, session_key.public_hex, scope=("viewer:identify",), org=ORG_UUID,
        subject=Subject("operator", OPERATOR_SESSION),
        not_before=now - 3600, not_after=now + 86400,
    )
    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, weak_cert, rr)
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))
    assert result["execution"]["ok"] is False
    assert "registry refused" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_malformed_registry_token_not_cached_i2(env, session_key, session_cert,
                                               monkeypatch):
    """A registry answering with a non-CSPRNG-shaped token never reaches the
    cache — the I2 tripwire on the dashboard side."""
    def bogus_client(base_url):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(
                201, json={"token": "not-a-token", "url": "http://x/l/not-a-token"})),
            base_url=base_url)
    monkeypatch.setattr(link_approvals, "_registry_client", bogus_client)
    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))
    assert result["execution"]["ok"] is False
    assert "malformed grant token" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_revoke_end_to_end(env, session_key, session_cert, registry_app):
    _, _, result = _publish(env, session_key, session_cert)
    token = result["execution"]["token"]
    assert token in _cached_grants()

    r = env.post("/api/approvals", json={
        "kind": "link_revoke", "session": SESSION,
        "request": {"org": ORG, "token": token},
    })
    rid = r.json()["id"]
    enriched = env.get(f"/api/approvals/{rid}").json()
    assert enriched["cached"] is True
    rr = enriched["registry_request"]
    assert rr == {"method": "DELETE", "path": f"/v1/links/{token}",
                  "registry_url": REGISTRY_URL, "payload": {}}
    envelope = _signed_envelope(session_key, session_cert, rr)
    outcome = _decide_and_wait(env, rid, _approve_body(envelope, rr))
    execution = outcome["execution"]
    assert execution["ok"] is True and execution["cache_removed"] is True
    assert token not in _cached_grants()          # I9: serving stops immediately
    # and the registry side agrees the grant is gone
    rc = TestClient(registry_app)
    assert rc.get(f"/v1/links/{token}/envelope").status_code in (404, 410)
