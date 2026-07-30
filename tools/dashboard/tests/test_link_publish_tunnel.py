"""D19: share-link publish/revoke over the org tunnel (auto-zudu9 §4).

Share links no longer travel to the registry over HTTP. The dashboard
authenticates the acting persona LOCALLY — the approval envelope's
signature proves possession of the session key, its certificate chains to
the org's bound root with the required scope, and the ledger fold grants
that scope — and then sends the mint/revoke as a control op on the
already-authenticated serving tunnel. The registry never sees the persona.

These tests found a real authority ledger, mock the tunnel control seam
at ``link_serving_supervisor.control``, and drive the approval flow so the
rewritten executor runs exactly as production would call it.
"""

from __future__ import annotations

import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import (
    approvals_routes,
    link_approvals,
    link_serving_supervisor,
)
from tools.dashboard.dao import approval_requests as ar
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger
from tools.network.registry.signing import sign_request

ORG = "netorg"
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
    from tools.graph.db import GraphDB

    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        return found_org_ledger(
            store, org_id=ORG_UUID, org_root=root,
            personal_root_seed=b"\x91" * 32, now=int(time.time() * 1000),
        )


def _persona_cert(root, session_key, persona_pub, *, kind="operator",
                  scope=SESSION_SCOPE, not_before=None, not_after=None):
    now = int(time.time())
    return issue_cert(
        root, session_key.public_hex, scope=scope, org=ORG_UUID,
        subject=Subject(kind, persona_pub),
        not_before=now - 3600 if not_before is None else not_before,
        not_after=now + 30 * 86400 if not_after is None else not_after,
    )


@pytest.fixture
def session_cert(root, session_key, founded_org):
    return _persona_cert(root, session_key, founded_org.founder_persona_pub)


@pytest.fixture
def env(tmp_path, monkeypatch, root, founded_org):
    from tools.graph.db import GraphDB

    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    GraphDB.close_all_pooled()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID, "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )
    with TestClient(Starlette(routes=approvals_routes.ROUTES)) as client:
        yield client
    GraphDB.close_all_pooled()


class _ControlRecorder:
    """Stands in for the serving tunnel: records control ops and replies."""

    def __init__(self, reply=None, raise_unavailable=False):
        self.calls = []
        self._reply = reply
        self._raise = raise_unavailable

    def __call__(self, org, op, args, **kwargs):
        self.calls.append((org, op, args))
        if self._raise:
            raise link_serving_supervisor.TunnelUnavailable("no tunnel")
        if self._reply is not None:
            return self._reply
        token = "c0ffee00" * 4  # 32 hex
        return {"ok": True, "token": token, "url": f"{PUBLIC_LINK_URL}/l/{token}"}


def _install_control(monkeypatch, recorder, revoked=None):
    monkeypatch.setattr(link_serving_supervisor, "control", recorder)
    ensured = []
    monkeypatch.setattr(
        link_serving_supervisor, "get_supervisor",
        lambda: type("S", (), {"ensure": lambda self, org: ensured.append(org)})(),
    )
    # The executor fetches the org's revocation denylist from the registry;
    # the tunnel-test env has no registry, so stub it. Default: nothing
    # revoked. Pass `revoked` to simulate a revoked session/cert key.
    revoked_set = set(revoked or [])

    async def _fake_fetch(binding):
        return revoked_set

    monkeypatch.setattr(link_approvals, "_fetch_org_revocations", _fake_fetch)
    return ensured


def _tunnel_envelope(session_key, cert, pop_path, payload=None):
    """What the browser signs on the tunnel path: a proof-of-possession
    signature over fixed bytes, not a destination-bound registry request."""
    return sign_request(
        session_key, "TUNNEL", pop_path,
        payload or {"target_uuid": TARGET, "target_type": "present"},
        ts=int(time.time()), cert=cert,
    )


def _create_publish(client, meta=None):
    r = client.post("/api/approvals", json={
        "kind": "link_publish", "session": SESSION,
        "request": {"org": ORG, "target_uuid": TARGET,
                    "target_type": "present", "meta": meta or {}},
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _decide_and_wait(client, rid, envelope):
    ok = client.post(f"/api/approvals/{rid}/decision",
                     json={"approved": True, "envelope": envelope})
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
    from tools.network.ledger import HLC, make_event
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        head = store.heads()[0]
        last = store.get(head).hlc
        defined = store.append(make_event(
            root,
            {"type": "role.define", "name": role, "scope_set": list(scopes),
             "claim_requires": "self", "version": 1},
            [head], HLC(last.ts, last.count + 1)))
        store.append(make_event(
            root, {"type": "role.grant", "persona": persona_pub, "role": role},
            [defined], HLC(last.ts, last.count + 2)))


# ── publish ───────────────────────────────────────────────────


def test_authorized_publish_emits_frame_and_caches_grant(
    env, root, session_key, session_cert, monkeypatch,
):
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)

    rid = _create_publish(env, meta={"ttl": 3600, "label": "binder"})
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)

    execution = result["execution"]
    assert execution["ok"] is True, execution
    assert execution["serving"] == {"live": True, "via": "tunnel-control"}
    # The registry saw one create-link, as the org, carrying only the target
    # and meta — never a persona.
    assert recorder.calls == [
        (ORG, "create-link",
         {"target_uuid": TARGET, "target_type": "present",
          "meta": {"ttl": 3600, "label": "binder"}}),
    ]
    token = execution["token"]
    grants = _cached_grants()
    assert token in grants
    assert grants[token]["subject"] == {
        "kind": "operator", "id": session_cert.subject.id}
    assert grants[token]["meta"] == {"ttl": 3600, "label": "binder"}


def test_publish_refused_without_scope_emits_no_frame(
    env, root, session_key, monkeypatch,
):
    outsider = KeyPair.generate()  # a persona holding no role in the fold
    cert = _persona_cert(root, session_key, outsider.public_hex)
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)

    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is False
    assert "not authorized to publish" in result["execution"]["error"]
    assert recorder.calls == []          # refused before any frame
    assert _cached_grants() == {}


def test_publish_refused_when_signature_forged(
    env, root, session_key, session_cert, monkeypatch,
):
    """A stolen certificate without the session key cannot publish: the
    proof-of-possession signature is checked locally."""
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)

    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    envelope["sig"] = ("0" if envelope["sig"][0] != "0" else "1") + envelope["sig"][1:]
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is False
    assert "signature does not verify" in result["execution"]["error"]
    assert recorder.calls == []
    assert _cached_grants() == {}


def test_publish_tunnel_unavailable_fails_and_triggers_ensure(
    env, root, session_key, session_cert, monkeypatch,
):
    recorder = _ControlRecorder(raise_unavailable=True)
    ensured = _install_control(monkeypatch, recorder)

    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is False
    assert "publish rides the tunnel" in result["execution"]["error"]
    assert _cached_grants() == {}        # no grant for an unminted link
    assert ensured == [ORG]              # nudged serving for next time


# ── revoke ────────────────────────────────────────────────────


def _seed_share_grant(root, session_key, session_cert, env, monkeypatch):
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    token = _decide_and_wait(env, rid, envelope)["execution"]["token"]
    return token


def test_revoke_over_tunnel_drops_local_grant(
    env, root, session_key, session_cert, monkeypatch,
):
    token = _seed_share_grant(root, session_key, session_cert, env, monkeypatch)
    assert token in _cached_grants()

    recorder = _ControlRecorder(reply={"ok": True, "token": token,
                                       "revoked_at": 1})
    _install_control(monkeypatch, recorder)
    created = env.post("/api/approvals", json={
        "kind": "link_revoke", "session": SESSION,
        "request": {"org": ORG, "token": token},
    })
    rid = created.json()["id"]
    envelope = _tunnel_envelope(session_key, session_cert,
                               "/control/revoke-link", payload={})
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is True
    assert result["execution"]["via"] == "tunnel-control"
    assert recorder.calls == [(ORG, "revoke-link", {"token": token})]
    assert token not in _cached_grants()


def test_revoke_refused_without_scope(
    env, root, session_key, session_cert, monkeypatch,
):
    token = _seed_share_grant(root, session_key, session_cert, env, monkeypatch)
    outsider = KeyPair.generate()
    cert = _persona_cert(root, session_key, outsider.public_hex)
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)

    created = env.post("/api/approvals", json={
        "kind": "link_revoke", "session": SESSION,
        "request": {"org": ORG, "token": token},
    })
    rid = created.json()["id"]
    envelope = _tunnel_envelope(session_key, cert, "/control/revoke-link",
                               payload={})
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is False
    assert "not authorized to revoke" in result["execution"]["error"]
    assert recorder.calls == []
    assert token in _cached_grants()     # nothing revoked


# ── Codex D19 review findings (2026-07-30) — regression guards ──


def test_expired_cert_is_refused(env, root, session_key, founded_org, monkeypatch):
    """Finding #1: a cert whose validity window has passed must not mint.
    (The chain check must verify at request time, not the cert midpoint.)"""
    now = int(time.time())
    cert = _persona_cert(root, session_key, founded_org.founder_persona_pub,
                        not_before=now - 7200, not_after=now - 3600)
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)
    assert result["execution"]["ok"] is False
    assert recorder.calls == []
    assert _cached_grants() == {}


def test_not_yet_valid_cert_is_refused(env, root, session_key, founded_org,
                                       monkeypatch):
    """Finding #1: a cert whose validity window is in the future must not mint."""
    now = int(time.time())
    cert = _persona_cert(root, session_key, founded_org.founder_persona_pub,
                        not_before=now + 3600, not_after=now + 7200)
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)
    assert result["execution"]["ok"] is False
    assert recorder.calls == []
    assert _cached_grants() == {}


def test_non_operator_subject_kind_is_refused(env, root, session_key,
                                              founded_org, monkeypatch):
    """Finding #4: an agent- or persona-kind cert whose subject.id names an
    authorized persona must not reach mint — the rung-1 transport pins the
    subject kind to 'operator'."""
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    for kind in ("agent", "persona"):
        cert = _persona_cert(root, session_key, founded_org.founder_persona_pub,
                            kind=kind)
        rid = _create_publish(env)
        envelope = _tunnel_envelope(session_key, cert, "/control/create-link")
        result = _decide_and_wait(env, rid, envelope)
        assert result["execution"]["ok"] is False, kind
        assert "operator subjects only" in result["execution"]["error"]
    assert recorder.calls == []
    assert _cached_grants() == {}


def test_uncached_token_revoke_never_reaches_the_tunnel(
    env, root, session_key, session_cert, monkeypatch,
):
    """Finding #5: a token not classifiable as a share link (cache miss, as
    an org:join token would be if uncached) must NOT be routed to the tunnel
    revoke. It goes to the HTTP path instead; the tunnel control seam is
    never called."""
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    forwarded = []

    async def fake_http(staged, envelope):
        forwarded.append(staged)
        # Emulate the registry accepting the revoke over HTTP.
        class _Resp:
            status_code = 200
        return _Resp(), None

    monkeypatch.setattr(link_approvals, "_forward_to_registry", fake_http)

    created = env.post("/api/approvals", json={
        "kind": "link_revoke", "session": SESSION,
        "request": {"org": ORG, "token": "deadbeef" * 4},  # not in cache
    })
    rid = created.json()["id"]
    envelope = _tunnel_envelope(session_key, session_cert,
                               "/control/revoke-link", payload={})
    _decide_and_wait(env, rid, envelope)
    # The tunnel control seam was never used for an unclassifiable token.
    assert recorder.calls == []


def test_revoked_session_key_is_refused(env, root, session_key, session_cert,
                                        monkeypatch):
    """Finding #2: a session cert whose key is on the org's revocation
    denylist must not publish, even though its subject is still an
    authorized persona. The dashboard fetches the denylist and passes it to
    verify_chain (the check the registry used to run)."""
    recorder = _ControlRecorder()
    # The signing session key is revoked on the org's denylist.
    _install_control(monkeypatch, recorder, revoked=[session_key.public_hex])
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)
    assert result["execution"]["ok"] is False
    assert recorder.calls == []
    assert _cached_grants() == {}


def test_revocation_fetch_failure_fails_closed(env, root, session_key,
                                               session_cert, monkeypatch):
    """Finding #2: if the revocation denylist cannot be fetched, publish is
    refused rather than proceeding without the check."""
    recorder = _ControlRecorder()
    monkeypatch.setattr(link_serving_supervisor, "control", recorder)
    monkeypatch.setattr(
        link_serving_supervisor, "get_supervisor",
        lambda: type("S", (), {"ensure": lambda self, org: None})())

    async def _boom(binding):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(link_approvals, "_fetch_org_revocations", _boom)
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)
    assert result["execution"]["ok"] is False
    assert "revocation list" in result["execution"]["error"]
    assert recorder.calls == []
    assert _cached_grants() == {}
