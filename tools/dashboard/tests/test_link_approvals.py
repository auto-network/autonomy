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
    # Unpin GRAPH_DB BEFORE creating org DBs: while the hermetic pin is
    # set, create_org_db(root=...) writes into the pinned store, not
    # orgs_dir/<slug>.db, so the resolver later can't find the org.
    monkeypatch.delenv("GRAPH_DB", raising=False)
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
    # Unpin GRAPH_DB before creating the org DBs — otherwise create_org_db
    # writes into the hermetic pin, not orgs_dir/<slug>.db, and the org
    # read below can't find it.
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    GraphDB.create_org_db("unregorg", root=orgs_dir).close()
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


def test_mission_title_comes_from_trusted_mission_store(env, tmp_path, monkeypatch):
    from tools.dashboard.dao import mission_control_db as mdb
    monkeypatch.setattr(mdb, "DB_PATH", tmp_path / "mission_control.db")
    mission = mdb.create_mission("OSS Insights")

    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {
            "org": ORG,
            "target_uuid": mission["mission_id"],
            "target_type": "mission",
            "preview": "requester-controlled fake title",
        },
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    assert enriched["target_title"] == "OSS Insights"
    assert "requester-controlled" not in str(enriched.get("target_title"))


def test_unknown_mission_target_errors_cleanly(env, tmp_path, monkeypatch):
    from tools.dashboard.dao import mission_control_db as mdb
    monkeypatch.setattr(mdb, "DB_PATH", tmp_path / "mission_control.db")
    mdb.init_db(tmp_path / "mission_control.db")

    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {
            "org": ORG,
            "target_uuid": "nope-not-a-real-mission",
            "target_type": "mission",
        },
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    assert enriched["target_title"] is None
    assert "not found" in (enriched.get("target_error") or "")


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


def _seed_org_key_sealed(org, root):
    """Store a keyed org via the B4 Option-B SEALED scheme (sealed_root_key,
    no armored_private_key) — the state anchore was in when first-publish
    registration was wrongly disabled — again WITHOUT a registry binding."""
    import os

    from tools.graph import org_ops
    org_ops._seal_org_root_setting(org, root, os.urandom(32))


def _isolated_orgs_with_peer_binding(tmp_path, monkeypatch, root, *test_orgs):
    """Own-DB-per-org isolation + a PEER (canonical) binding published by ORG.

    The peer binding is the contaminant: owning-scope reads (P2) must not
    let *test_orgs* inherit it. Mirrors
    ``test_load_binding_ignores_other_orgs_published_binding``.
    """
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    # Unpin GRAPH_DB before creating the org DBs (see the other sites): a
    # live pin sends create_org_db into the pinned store instead of
    # orgs_dir/<slug>.db.
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    for o in test_orgs:
        GraphDB.create_org_db(o, root=orgs_dir).close()
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


def test_sealed_keyed_unregistered_org_is_registerable(tmp_path, monkeypatch, root):
    """Regression: an org keyed with the B4 Option-B SEALED scheme
    (sealed_root_key, no armored_private_key) — anchore's real state — must be
    recognised as keyed and offered inline first-publish registration, exactly
    like an armored org. Before the fix, _org_has_key checked only
    armored_private_key, so registration_required stayed False and the publish
    dialog dead-ended with 'not registered' + a disabled Approve button."""
    _isolated_orgs_with_peer_binding(tmp_path, monkeypatch, root, "sealedorg")
    _seed_org_key_sealed("sealedorg", root)
    enriched = link_approvals._enrich_link_publish({
        "id": "r-sealed",
        "request": {"org": "sealedorg", "target_uuid": TARGET,
                    "target_type": "present", "meta": {"ttl": 3600}},
    })
    assert enriched["registration_required"] is True
    assert not enriched.get("binding_error")
    assert "registry_request" not in enriched
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


def test_mission_recipient_is_its_own_field_not_the_title(env, tmp_path, monkeypatch):
    """A mission link is bound to ONE guest, and that binding decides whose
    name lands on every question and whose access dies when the link is
    revoked -- so the operator must see WHO before approving. The person is
    a FIRST-CLASS FIELD, never folded into the target's name: the target row
    says what is shared, the recipient row says who it is for, and the view
    renders the recipient as an identity (avatar + name), not as prose.""" 
    from tools.dashboard.dao import mission_control_db as mdb
    monkeypatch.setattr(mdb, "DB_PATH", tmp_path / "mission_control.db")
    mission = mdb.create_mission("OSS Insights")
    guest = mdb.create_visitor_token("Priya (data partner)")

    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {
            "org": ORG,
            "target_uuid": mission["mission_id"],
            "target_type": "mission",
            "meta": {"participant_id": guest["participant_id"]},
        },
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    # The target names the mission ALONE...
    assert enriched["target_title"] == "OSS Insights"
    # ...and the person is structured, resolvable, and separate.
    assert enriched["recipient"] == {
        "participant_id": guest["participant_id"],
        "display_name": "Priya (data partner)",
        "avatar_url": None,  # no photo -> the initial-and-color avatar
    }


def test_mission_link_bound_to_an_unknown_guest_errors(env, tmp_path, monkeypatch):
    from tools.dashboard.dao import mission_control_db as mdb
    monkeypatch.setattr(mdb, "DB_PATH", tmp_path / "mission_control.db")
    mission = mdb.create_mission("OSS Insights")

    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {
            "org": ORG,
            "target_uuid": mission["mission_id"],
            "target_type": "mission",
            "meta": {"participant_id": "guest:not-a-real-participant"},
        },
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    assert enriched["recipient"] is None
    assert "not a known participant" in (enriched.get("target_error") or "")


def test_tunnel_meta_carries_participant_id():
    """REGRESSION: the executor's meta allowlist named only ttl and label,
    so a mission publish lost its participant binding on the way to the
    grant and then failed NetworkLinkGrantV3 validation AFTER the operator
    had already approved. Found on a real publish, not by a unit test --
    the CLI-side tests mocked the approval round trip entirely."""
    from tools.dashboard import link_approvals

    meta, error = link_approvals._tunnel_link_meta(
        {"meta": {"participant_id": "guest:abc", "label": "Briefing", "ttl": 3600}},
        {},
    )
    assert error is None
    assert meta == {"participant_id": "guest:abc", "label": "Briefing", "ttl": 3600}


def test_tunnel_meta_carries_the_signed_ice_policy():
    meta, error = link_approvals._tunnel_link_meta(
        {"meta": {"ice_policy": "relay_only", "label": "Private route"}},
        {},
    )

    assert error is None
    assert meta == {"ice_policy": "relay_only", "label": "Private route"}

    # Policy is enforced by the serving dashboard from this signed local
    # grant. The Registry neither authorizes nor routes with it, so the
    # control frame must not disclose it or require a Registry wire change.
    wire = {
        k: v for k, v in meta.items()
        if k not in link_approvals._LOCAL_ONLY_META
    }
    assert wire == {"label": "Private route"}


def test_tunnel_meta_still_drops_unknown_keys():
    """The allowlist is the point: an unknown key must never reach a grant."""
    from tools.dashboard import link_approvals

    meta, error = link_approvals._tunnel_link_meta(
        {"meta": {"participant_id": "guest:abc", "require_auth": True, "junk": 1}},
        {},
    )
    assert error is None
    assert meta == {"participant_id": "guest:abc"}


def test_participant_id_is_kept_off_the_wire_to_the_registry():
    """REGRESSION + design invariant: the relay is untrusted and authorizes
    nothing with participant_id (check_grant reads the LOCAL cache, never
    the registry), so it must not learn who a link is for. The local grant
    keeps it; the control frame does not carry it."""
    from tools.dashboard import link_approvals

    meta, error = link_approvals._tunnel_link_meta(
        {"meta": {"participant_id": "guest:abc", "label": "Briefing", "ttl": 3600}}, {},
    )
    assert error is None
    # The local grant keeps the binding...
    assert meta["participant_id"] == "guest:abc"
    # ...and the wire copy drops it, keeping everything the relay does need.
    wire = {k: v for k, v in meta.items() if k not in link_approvals._LOCAL_ONLY_META}
    assert wire == {"label": "Briefing", "ttl": 3600}


def test_recipient_avatar_is_a_url_into_the_attachment_store(env, tmp_path, monkeypatch):
    """The photo's BYTES never ride this payload. They live once in the
    graph's content-addressed attachment store (hash-deduped, alt-texted,
    same-origin cacheable, and already fetchable over the relay's own
    attachment protocol); the recipient carries a reference."""
    from tools.dashboard.dao import mission_control_db as mdb
    monkeypatch.setattr(mdb, "DB_PATH", tmp_path / "mission_control.db")
    mission = mdb.create_mission("OSS Insights")
    guest = mdb.create_visitor_token("Leon Zachery", avatar_attachment_id="att-123")

    r = env.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {
            "org": ORG,
            "target_uuid": mission["mission_id"],
            "target_type": "mission",
            "meta": {"participant_id": guest["participant_id"]},
        },
    })
    enriched = env.get(f"/api/approvals/{r.json()['id']}").json()
    assert enriched["recipient"]["avatar_url"] == "/api/attachment/att-123"
    # And nothing image-shaped is inlined anywhere in the payload.
    assert "data:image" not in json.dumps(enriched)
