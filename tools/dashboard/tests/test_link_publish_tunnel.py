"""D19: share-link publish/revoke over the org tunnel (auto-zudu9 §4).

Share links no longer travel to the registry over HTTP. The dashboard
authenticates the acting persona LOCALLY — the approval envelope's
signature proves possession of the session key, its certificate chains to
THE ACTING PERSONA with the required scope, and the ledger fold grants that
scope — and then sends the mint/revoke as a control op on the
already-authenticated serving tunnel. The registry never sees the persona.

The chain anchors at the persona, never at the org root: §7 rules that the
org root is not a domain principal and that a chain terminating outside the
roster is void. Authentication and authorization are separate — the chain
proves WHO signed, the authority ledger decides WHAT they may do.

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


PERSONAL_ROOT_SEED = b"\x91" * 32


@pytest.fixture
def founder_persona(founded_org):
    """The founder's persona KEYPAIR, derived exactly as the browser derives
    it — one personal unlock, HKDF over this org's genesis id."""
    from tools.network.idkit.persona import derive_persona

    return derive_persona(PERSONAL_ROOT_SEED, founded_org.genesis_id)


def _persona_cert(signer, session_key, persona_pub, *, kind="operator",
                  scope=SESSION_SCOPE, not_before=None, not_after=None):
    """Mint a session certificate the way sign-on does: signed by the ACTING
    PERSONA (not the org root), naming that persona as the subject.

    The org root does not appear. A session certificate has never chained to
    it since sign-on became a personal act, and §7 is explicit that the org
    root is not a domain principal — authorization is the ledger's job.
    """
    now = int(time.time())
    return issue_cert(
        signer, session_key.public_hex, scope=scope, org=ORG_UUID,
        subject=Subject(kind, persona_pub),
        not_before=now - 3600 if not_before is None else not_before,
        not_after=now + 30 * 86400 if not_after is None else not_after,
    )


@pytest.fixture
def session_cert(session_key, founder_persona):
    return _persona_cert(
        founder_persona, session_key, founder_persona.public_hex)


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
            # kind=None -> non-retryable, so the publish fails immediately
            # rather than retrying create-link for the full startup window.
            raise link_serving_supervisor.TunnelUnavailable("no tunnel")
        if self._reply is not None:
            return self._reply
        token = "c0ffee00" * 4  # 32 hex
        return {"ok": True, "token": token, "url": f"{PUBLIC_LINK_URL}/l/{token}"}


def _install_control(monkeypatch, recorder):
    """Patch the tunnel control seam and the supervisor. Returns the list of
    orgs the publish asked to start (the first-publish tunnel launch)."""
    monkeypatch.setattr(link_serving_supervisor, "control", recorder)
    started = []

    class _S:
        def start(self, org):
            started.append(org)
            return {"running": True, "reason": "launched"}

    monkeypatch.setattr(link_serving_supervisor, "get_supervisor", lambda: _S())
    return started


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
    cert = _persona_cert(outsider, session_key, outsider.public_hex)
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


def test_publish_starts_tunnel_and_fails_if_it_never_comes_up(
    env, root, session_key, session_cert, monkeypatch,
):
    recorder = _ControlRecorder(raise_unavailable=True)
    started = _install_control(monkeypatch, recorder)

    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is False
    assert "serving tunnel did not come up" in result["execution"]["error"]
    assert _cached_grants() == {}        # no grant for an unminted link
    assert started == [ORG]              # the publish DID start the tunnel first


def test_create_link_over_tunnel_retries_until_the_tunnel_dials(monkeypatch):
    """A first publish tolerates the connector/tunnel still coming up: it retries
    the pre-write 'no-tunnel' failures, then sends create-link once it dials."""
    calls = {"n": 0}
    token = "d0d0d0d0" * 4

    def flaky(org, op, args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise link_serving_supervisor.TunnelUnavailable("no tunnel", kind="no-tunnel")
        return {"ok": True, "token": token, "url": "x"}

    monkeypatch.setattr(link_serving_supervisor, "control", flaky)
    reply = link_approvals._create_link_over_tunnel(ORG, {}, timeout=2.0, poll=0.01)
    assert reply["ok"] and reply["token"] == token
    assert calls["n"] == 3               # two no-tunnel retries, then success


def test_create_link_over_tunnel_gives_up_after_timeout(monkeypatch):
    def always_no_tunnel(org, op, args, **kwargs):
        raise link_serving_supervisor.TunnelUnavailable("no tunnel", kind="no-tunnel")

    monkeypatch.setattr(link_serving_supervisor, "control", always_no_tunnel)
    with pytest.raises(link_serving_supervisor.TunnelUnavailable):
        link_approvals._create_link_over_tunnel(ORG, {}, timeout=0.2, poll=0.02)


def test_create_link_over_tunnel_never_retries_an_ambiguous_failure(monkeypatch):
    """A 'closed' failure may have sent the frame already, so retrying could
    double-create — it must raise on the first attempt."""
    calls = {"n": 0}

    def closed(org, op, args, **kwargs):
        calls["n"] += 1
        raise link_serving_supervisor.TunnelUnavailable("closed", kind="closed")

    monkeypatch.setattr(link_serving_supervisor, "control", closed)
    with pytest.raises(link_serving_supervisor.TunnelUnavailable):
        link_approvals._create_link_over_tunnel(ORG, {}, timeout=2.0, poll=0.01)
    assert calls["n"] == 1               # no retry on a possibly-post-send failure


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
    cert = _persona_cert(outsider, session_key, outsider.public_hex)
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


def test_expired_cert_is_refused(env, root, session_key, founded_org, founder_persona, monkeypatch):
    """Finding #1: a cert whose validity window has passed must not mint.
    (The chain check must verify at request time, not the cert midpoint.)"""
    now = int(time.time())
    cert = _persona_cert(founder_persona, session_key, founder_persona.public_hex,
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
                                       founder_persona, monkeypatch):
    """Finding #1: a cert whose validity window is in the future must not mint."""
    now = int(time.time())
    cert = _persona_cert(founder_persona, session_key, founder_persona.public_hex,
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
                                              founded_org, founder_persona, monkeypatch):
    """Finding #4: an agent- or persona-kind cert whose subject.id names an
    authorized persona must not reach mint — the rung-1 transport pins the
    subject kind to 'operator'."""
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    for kind in ("agent", "persona"):
        cert = _persona_cert(founder_persona, session_key, founder_persona.public_hex,
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


# ── TTL override from the approval sheet (re-expressed on the tunnel) ──


def test_ttl_override_from_decision_is_applied(env, root, session_key,
                                               session_cert, monkeypatch):
    """The operator's duration choice rides the decision as ttl and must
    reach the grant meta (the tunnel path honors it, as the HTTP path did)."""
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    rid = _create_publish(env, meta={"ttl": 3600, "label": "binder"})
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    ok = env.post(f"/api/approvals/{rid}/decision",
                  json={"approved": True, "envelope": envelope, "ttl": 86400})
    assert ok.status_code == 200
    for _ in range(50):
        d = env.get(f"/api/approvals/{rid}?wait=2").json()
        if d["result"] is not None:
            break
    token = d["result"]["execution"]["token"]
    assert recorder.calls[0][2]["meta"] == {"ttl": 86400, "label": "binder"}
    assert _cached_grants()[token]["meta"] == {"ttl": 86400, "label": "binder"}


def test_ttl_override_none_removes_expiry(env, root, session_key, session_cert,
                                          monkeypatch):
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    rid = _create_publish(env, meta={"ttl": 3600, "label": "binder"})
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    env.post(f"/api/approvals/{rid}/decision",
             json={"approved": True, "envelope": envelope, "ttl": None})
    for _ in range(50):
        d = env.get(f"/api/approvals/{rid}?wait=2").json()
        if d["result"] is not None:
            break
    assert recorder.calls[0][2]["meta"] == {"label": "binder"}


def test_invalid_ttl_override_is_refused(env, root, session_key, session_cert,
                                         monkeypatch):
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)
    rid = _create_publish(env, meta={"ttl": 3600})
    envelope = _tunnel_envelope(session_key, session_cert, "/control/create-link")
    env.post(f"/api/approvals/{rid}/decision",
             json={"approved": True, "envelope": envelope, "ttl": -1})
    for _ in range(50):
        d = env.get(f"/api/approvals/{rid}?wait=2").json()
        if d["result"] is not None:
            break
    assert d["result"]["execution"]["ok"] is False
    assert "between 1 and 365 days" in d["result"]["execution"]["error"]
    assert recorder.calls == []
    assert _cached_grants() == {}


# ── the anchor: persona, never the org root ──


def test_persona_signed_session_certificate_publishes(
    env, session_key, founder_persona, founded_org, monkeypatch,
):
    """REGRESSION. Sign-on is a personal act, so the session certificate is
    signed by the org's persona and does not chain to the org root at all.

    Anchoring the check at the org root rejected every one of them at the
    first hop -- "hop 1: signature does not verify against its parent key" --
    which reads as a forged certificate and is actually a wrong anchor. It
    blocked every share-link publish, so no mission could be shared with a
    real person.
    """
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)

    cert = _persona_cert(
        founder_persona, session_key, founder_persona.public_hex)
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is True, result["execution"].get("error")
    assert len(recorder.calls) == 1


def test_root_signed_certificate_is_refused(
    env, root, session_key, founder_persona, founded_org, monkeypatch,
):
    """The org root is NOT a domain principal (§7). A certificate signed by
    it, naming an authorized persona as subject, must not publish -- otherwise
    holding the root would silently confer every persona's authority."""
    recorder = _ControlRecorder()
    _install_control(monkeypatch, recorder)

    cert = _persona_cert(root, session_key, founder_persona.public_hex)
    rid = _create_publish(env)
    envelope = _tunnel_envelope(session_key, cert, "/control/create-link")
    result = _decide_and_wait(env, rid, envelope)

    assert result["execution"]["ok"] is False
    assert "does not chain to its acting persona" in result["execution"]["error"]
    assert recorder.calls == []
    assert _cached_grants() == {}
