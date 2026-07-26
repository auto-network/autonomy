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

import json
import copy
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
from tools.network.ledger import HLC, LedgerStore, make_event, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger
from tools.network.registry.app import create_app as create_registry_app
from tools.network.registry.signing import sign_request

ORG = "netorg"  # dashboard-side org slug (Settings scope)
ORG_UUID = "11111111-1111-4111-8111-111111111111"
TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "auto-agent-1"
REGISTRY_URL = "http://registry.test"
PUBLIC_LINK_URL = "https://relay.auto.network"

SESSION_SCOPE = ("delegate:agent", "link:publish", "link:revoke",
                 "tunnel:serve", "viewer:identify")


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def session_key():
    return KeyPair.generate()


@pytest.fixture
def founded_org(tmp_path, monkeypatch, root):
    """Found the real authority ledger used by the approval executor."""
    from tools.graph.db import GraphDB

    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        return found_org_ledger(
            store,
            org_id=ORG_UUID,
            org_root=root,
            personal_root_seed=b"\x91" * 32,
            now=int(time.time() * 1000),
        )


def _persona_cert(
    root, session_key, persona_pub, scope=SESSION_SCOPE, *, kind="operator",
):
    """Session cert naming the acting persona in ``subject.id``.

    The current HTTP registry transport still accepts operator subjects only.
    D19 replaces it with an org-authenticated tunnel, at which point the
    persona remains local and no certificate subject crosses that boundary.
    """
    now = int(time.time())
    return issue_cert(
        root, session_key.public_hex, scope=scope, org=ORG_UUID,
        subject=Subject(kind, persona_pub),
        not_before=now - 3600, not_after=now + 30 * 86400,
    )


@pytest.fixture
def session_cert(root, session_key, founded_org):
    """Root-delegated session key attributed to the founder persona."""
    return _persona_cert(
        root, session_key, founded_org.founder_persona_pub,
    )


@pytest.fixture
def registry_app(root):
    """The real B1 registry with our org bound to *root*."""
    app = create_registry_app(":memory:", base_url=PUBLIC_LINK_URL,
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
def env(tmp_path, monkeypatch, registry_app, root, founded_org):
    """Approvals app + tmp Settings DB + registry routed through ASGI."""
    from tools.graph.db import GraphDB

    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    GraphDB.close_all_pooled()
    monkeypatch.delenv("GRAPH_DB", raising=False)
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


def _registry_request_with_ttl(rr, ttl):
    adjusted = copy.deepcopy(rr)
    meta = dict(adjusted["payload"].get("meta") or {})
    if ttl is None:
        meta.pop("ttl", None)
    else:
        meta["ttl"] = ttl
    if meta:
        adjusted["payload"]["meta"] = meta
    else:
        adjusted["payload"].pop("meta", None)
    return adjusted


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


def _append_role(root, persona_pub, role, scopes):
    """Grant a scoped role through the production ledger vocabulary."""
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        head = store.heads()[0]
        last_hlc = store.get(head).hlc
        defined = store.append(make_event(
            root,
            {
                "type": "role.define",
                "name": role,
                "scope_set": list(scopes),
                "claim_requires": "self",
                "version": 1,
            },
            [head],
            HLC(last_hlc.ts, last_hlc.count + 1),
        ))
        store.append(make_event(
            root,
            {"type": "role.grant", "persona": persona_pub, "role": role},
            [defined],
            HLC(last_hlc.ts, last_hlc.count + 2),
        ))


def _revoke_role(root, persona_pub, role):
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        head = store.heads()[0]
        last_hlc = store.get(head).hlc
        store.append(make_event(
            root,
            {"type": "role.revoke", "persona": persona_pub, "role": role},
            [head],
            HLC(last_hlc.ts, last_hlc.count + 1),
        ))


def test_cached_grant_uses_owning_scope_reader(monkeypatch):
    """A peer grant cannot spoof the revoke dialog's local cache lookup."""
    class _Members:
        members = []

    def fail_composed(*_args, **_kwargs):
        raise AssertionError("authority cache must not compose peer Settings")

    monkeypatch.setattr(settings_ops, "read_set", fail_composed)
    monkeypatch.setattr(settings_ops, "read_owned_set",
                        lambda *_args, **_kwargs: _Members())
    assert link_approvals._cached_grant("deadbeef", ORG) is None


def _approve_body(envelope, rr=None):
    """What the browser posts: just the verdict + the signed envelope. The
    destination is frozen server-side at render; the decision cannot carry
    or influence it."""
    return {"approved": True, "envelope": envelope}


def test_load_binding_ignores_other_orgs_published_binding(tmp_path, monkeypatch, root):
    """An unbound org must not inherit a binding from the peer-composed view."""
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    GraphDB.create_org_db("unregorg", root=orgs_dir).close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
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
        state="canonical",
    )

    # The generic Settings view demonstrates the old failure: unregorg has
    # no row of its own, yet sees netorg's public binding through peers.
    composed = settings_ops.read_set(
        NETWORK_BINDING_SET_ID, org="unregorg",
    ).members
    assert len(composed) == 1
    assert composed[0].org == ORG

    binding, error = link_approvals._load_binding("unregorg")
    assert binding is None
    assert "not registered on auto.network" in error
    GraphDB.close_all_pooled()


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
    assert execution["url"] == f"{PUBLIC_LINK_URL}/l/{token}"
    # the grant cache row the serving path (I9) will read
    grants = _cached_grants()
    assert token in grants
    assert grants[token]["target_uuid"] == TARGET
    assert grants[token]["meta"] == {"ttl": 3600, "label": "binder"}


def test_grant_records_issuing_subject_i6(env, session_key, session_cert):
    _, _, result = _publish(env, session_key, session_cert)
    token = result["execution"]["token"]
    # I6: the stored grant is attributable to the acting persona. The
    # operator kind is the current HTTP transport shape; D19 retires it.
    assert _cached_grants()[token]["subject"] == {
        "kind": "operator", "id": session_cert.subject.id}


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


def test_note_preview_comes_from_trusted_graph_target(env):
    from tools.graph import ops as graph_ops

    note = graph_ops.create_note(
        "Trusted note body\n\n- first\n- second",
        title="Release checklist",
        org=ORG,
    )
    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {
            "org": ORG,
            "target_uuid": note["id"],
            "target_type": "note",
            "preview": "requester-controlled fake copy",
        },
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    assert enriched["target_title"] == "Release checklist"
    assert enriched["target_preview"] == {
        "title": "Release checklist",
        "content": "Trusted note body\n\n- first\n- second",
    }
    assert "requester-controlled" not in str(enriched["target_preview"])


@pytest.mark.parametrize("ttl", [86400, 365 * 86400])
def test_ttl_override_is_forwarded_and_cached(env, session_key, session_cert, ttl):
    rid = _create_publish(env, meta={"ttl": 3600, "label": "binder"})
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    adjusted = _registry_request_with_ttl(rr, ttl)
    envelope = _signed_envelope(session_key, session_cert, adjusted)
    result = _decide_and_wait(env, rid, {
        "approved": True, "envelope": envelope, "ttl": ttl,
    })
    assert result["execution"]["ok"] is True
    token = result["execution"]["token"]
    assert _cached_grants()[token]["meta"] == {"ttl": ttl, "label": "binder"}


def test_no_expiration_removes_only_ttl(env, session_key, session_cert):
    rid = _create_publish(env, meta={"ttl": 3600, "label": "binder"})
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    adjusted = _registry_request_with_ttl(rr, None)
    envelope = _signed_envelope(session_key, session_cert, adjusted)
    result = _decide_and_wait(env, rid, {
        "approved": True, "envelope": envelope, "ttl": None,
    })
    assert result["execution"]["ok"] is True
    token = result["execution"]["token"]
    assert _cached_grants()[token]["meta"] == {"label": "binder"}


@pytest.mark.parametrize("ttl", [0, -1, True, "3600", 365 * 86400 + 1, {}, []])
def test_invalid_ttl_override_is_rejected(env, session_key, session_cert, ttl):
    rid = _create_publish(env, meta={"ttl": 3600})
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    result = _decide_and_wait(env, rid, {
        "approved": True, "envelope": envelope, "ttl": ttl,
    })
    assert result["execution"]["ok"] is False
    assert "between 1 and 365 days" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_ttl_override_cannot_smuggle_frozen_fields(env, session_key, session_cert):
    rid = _create_publish(env, meta={"ttl": 3600, "label": "trusted"})
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    adjusted = _registry_request_with_ttl(rr, 86400)
    envelope = _signed_envelope(session_key, session_cert, adjusted)
    result = _decide_and_wait(env, rid, {
        "approved": True,
        "envelope": envelope,
        "ttl": 86400,
        "target_uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "target_type": "file",
        "org": "evil-org",
        "registry_url": "https://evil.example",
        "method": "DELETE",
        "path": "/v1/orgs",
        "binding": {"root_pub": "00" * 32},
        "meta": {"label": "forged"},
    })
    assert result["execution"]["ok"] is True
    token = result["execution"]["token"]
    grant = _cached_grants()[token]
    assert grant["target_uuid"] == TARGET
    assert grant["target_type"] == "present"
    assert grant["meta"] == {"ttl": 86400, "label": "trusted"}


def test_signed_payload_must_match_ttl_only_reconstruction(
    env, session_key, session_cert,
):
    rid = _create_publish(env, meta={"ttl": 3600})
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    adjusted = _registry_request_with_ttl(rr, 86400)
    adjusted["payload"]["target_uuid"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    envelope = _signed_envelope(session_key, session_cert, adjusted)
    result = _decide_and_wait(env, rid, {
        "approved": True, "envelope": envelope, "ttl": 86400,
    })
    assert result["execution"]["ok"] is False
    assert "does not match the staged request" in result["execution"]["error"]
    assert _cached_grants() == {}


def test_execution_records_trusted_personal_actor(
    env, session_key, session_cert, monkeypatch,
):
    actor = {"display_name": "Alex Operator", "root_pub": "ab" * 32}
    monkeypatch.setattr(link_approvals, "_approval_identities", lambda _org: {
        "acting_identity": {"name": "Network Org"},
        "actor_identity": actor,
    })
    _, _, result = _publish(env, session_key, session_cert)
    assert result["execution"]["actor"] == actor


def test_decline_surfaces_to_requester(env):
    rid = _create_publish(env)
    result = _decide_and_wait(env, rid, {"approved": False})
    assert result == {"approved": False}          # the CLI's clean-deny state
    assert _cached_grants() == {}                 # nothing published, nothing cached
    assert ar.pending_for_session(SESSION) is None


def test_no_envelope_has_an_actionable_error(env):
    """Approving without a browser signature fails with operator wording."""
    rid = _create_publish(env)
    result = _decide_and_wait(env, rid, {"approved": True})
    execution = result["execution"]
    assert execution["ok"] is False
    assert "unlock the organization" in execution["error"]
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


def test_registry_rejection_propagates(
    env, session_key, root, founded_org,
):
    """A cert without link:publish scope: registry 403 comes back readable."""
    now = int(time.time())
    weak_cert = issue_cert(
        root, session_key.public_hex, scope=("viewer:identify",), org=ORG_UUID,
        subject=Subject("operator", founded_org.founder_persona_pub),
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


def test_publish_refused_without_link_publish_scope(
    env, root, session_key, monkeypatch,
):
    outsider = KeyPair.generate()
    cert = _persona_cert(
        root, session_key, outsider.public_hex, kind="persona",
    )
    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, cert, rr)

    forwarded = []

    async def forbidden_forward(*args):
        forwarded.append(args)
        raise AssertionError("denied publish reached the registry")

    monkeypatch.setattr(
        link_approvals, "_forward_to_registry", forbidden_forward,
    )
    upserts = []

    def forbidden_upsert(*args, **kwargs):
        upserts.append((args, kwargs))
        raise AssertionError("denied publish reached the grant cache")

    monkeypatch.setattr(settings_ops, "upsert_by_key", forbidden_upsert)
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))

    assert result["execution"] == {
        "ok": False,
        "error": (
            f"{outsider.public_hex} is not authorized to publish "
            f"share links in {ORG}"
        ),
    }
    assert forwarded == []
    assert upserts == []


def test_malformed_persona_key_fails_closed(
    env, root, session_key, monkeypatch,
):
    malformed = "not-a-valid-ed25519-public-key"
    cert = _persona_cert(
        root, session_key, malformed, kind="persona",
    )
    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, cert, rr)
    forwarded = []

    async def forbidden_forward(*args):
        forwarded.append(args)
        raise AssertionError("malformed persona reached the registry")

    monkeypatch.setattr(
        link_approvals, "_forward_to_registry", forbidden_forward,
    )
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))

    assert result["execution"]["ok"] is False
    assert malformed in result["execution"]["error"]
    assert forwarded == []
    assert _cached_grants() == {}


def test_authority_store_error_fails_closed(
    env, session_key, session_cert, monkeypatch,
):
    from tools.dashboard import org_authority

    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    forwarded = []

    def unavailable(*args, **kwargs):
        raise OSError("authority ledger is unavailable")

    async def forbidden_forward(*args):
        forwarded.append(args)
        raise AssertionError("authority exception reached the registry")

    monkeypatch.setattr(org_authority, "authorize", unavailable)
    monkeypatch.setattr(
        link_approvals, "_forward_to_registry", forbidden_forward,
    )
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))

    assert result["execution"]["ok"] is False
    assert "is not authorized to publish share links" in result["execution"]["error"]
    assert forwarded == []
    assert _cached_grants() == {}


def test_publish_allowed_with_link_publish_scope(
    env, root, session_key,
):
    publisher = KeyPair.generate()
    _append_role(root, publisher.public_hex, "publisher", ["link:publish"])
    cert = _persona_cert(root, session_key, publisher.public_hex)

    _, _, result = _publish(env, session_key, cert)

    assert result["execution"]["ok"] is True
    token = result["execution"]["token"]
    assert _cached_grants()[token]["subject"] == {
        "kind": "operator",
        "id": publisher.public_hex,
    }


def test_revoke_requires_link_revoke_scope(
    env, root, session_key, session_cert, monkeypatch,
):
    _, _, published = _publish(env, session_key, session_cert)
    token = published["execution"]["token"]
    publisher = KeyPair.generate()
    _append_role(root, publisher.public_hex, "publisher", ["link:publish"])
    publisher_cert = _persona_cert(
        root, session_key, publisher.public_hex, kind="persona",
    )

    created = env.post("/api/approvals", json={
        "kind": "link_revoke",
        "session": SESSION,
        "request": {"org": ORG, "token": token},
    })
    rid = created.json()["id"]
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, publisher_cert, rr)
    forwarded = []

    async def forbidden_forward(*args):
        forwarded.append(args)
        raise AssertionError("denied revoke reached the registry")

    monkeypatch.setattr(
        link_approvals, "_forward_to_registry", forbidden_forward,
    )
    result = _decide_and_wait(env, rid, _approve_body(envelope, rr))

    assert result["execution"]["ok"] is False
    assert "is not authorized to revoke share links" in result["execution"]["error"]
    assert forwarded == []
    assert token in _cached_grants()


def test_revocation_between_publishes_blocks_second(
    env, root, session_key, session_cert, founded_org, monkeypatch,
):
    _, _, first = _publish(env, session_key, session_cert)
    assert first["execution"]["ok"] is True
    _revoke_role(root, founded_org.founder_persona_pub, "owner")

    rid = _create_publish(env)
    rr = env.get(f"/api/approvals/{rid}").json()["registry_request"]
    envelope = _signed_envelope(session_key, session_cert, rr)
    forwarded = []

    async def forbidden_forward(*args):
        forwarded.append(args)
        raise AssertionError("revoked persona reached the registry")

    monkeypatch.setattr(
        link_approvals, "_forward_to_registry", forbidden_forward,
    )
    second = _decide_and_wait(env, rid, _approve_body(envelope, rr))

    assert second["execution"]["ok"] is False
    assert "is not authorized to publish share links" in second["execution"]["error"]
    assert forwarded == []
    assert len(_cached_grants()) == 1


# ── register-on-first-publish: enrich surfaces a graceful seam, not C1 ──

def _seed_org_key(org, root):
    """Store a keyed org WITHOUT a registry binding (the D3a autonomy state)."""
    from tools.graph.schemas.network_identity import (
        NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION)
    from tools.network.idkit.armor import encrypt_root_key
    settings_ops.add_setting(
        NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION, "default",
        {"armored_private_key": encrypt_root_key(root, "org-pw-123", iterations=10_000),
         "root_pub": root.public_hex},
        org=org,
    )


def _isolated_orgs_with_peer_binding(tmp_path, monkeypatch, root, *test_orgs):
    """Own-DB-per-org isolation + a PEER (canonical) binding published by ORG.

    The peer binding is the contaminant: owning-scope reads (P2) must not
    let *test_orgs* inherit it. Mirrors
    ``test_load_binding_ignores_other_orgs_published_binding``.
    """
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    for o in test_orgs:
        GraphDB.create_org_db(o, root=orgs_dir).close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
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
        state="canonical",
    )


def test_keyed_unregistered_org_enrich_is_registerable_not_c1(tmp_path, monkeypatch, root):
    """A keyed-but-unbound org: enrich flags registration_required, emits NO
    blocking binding_error, never freezes a request (register-before-freeze),
    and never leaks the C1 codename — even though a PEER has published a
    binding (owning-scope read, P2)."""
    _isolated_orgs_with_peer_binding(tmp_path, monkeypatch, root, "unregorg")
    _seed_org_key("unregorg", root)   # unregorg owns a key, no binding of its own
    enriched = link_approvals._enrich_link_publish({
        "id": "r-unreg",
        "request": {"org": "unregorg", "target_uuid": TARGET,
                    "target_type": "present", "meta": {"ttl": 3600}},
    })
    assert enriched["registration_required"] is True
    assert not enriched.get("binding_error")
    # No staged request is frozen until a binding exists.
    assert "registry_request" not in enriched
    blob = json.dumps(enriched)
    assert "C1" not in blob and "ceremony" not in blob
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()


def test_unkeyed_org_enrich_errors_cleanly_without_codename(tmp_path, monkeypatch, root):
    """An org with no key at all is a real error — but codename-free, and NOT
    marked registerable (that's the new-key setup path, out of scope here).
    A peer's binding must not spoof it into looking bound (owning-scope, P2)."""
    _isolated_orgs_with_peer_binding(tmp_path, monkeypatch, root, "nokeyorg")
    enriched = link_approvals._enrich_link_publish({
        "id": "r-nokey",
        "request": {"org": "nokeyorg", "target_uuid": TARGET,
                    "target_type": "present", "meta": {"ttl": 3600}},
    })
    assert enriched["registration_required"] is False
    assert enriched.get("binding_error")            # a real error remains
    blob = json.dumps(enriched)
    assert "C1" not in blob and "ceremony" not in blob
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
