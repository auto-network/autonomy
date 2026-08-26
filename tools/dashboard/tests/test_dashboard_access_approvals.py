"""Headless dashboard access through a signed, one-time approval grant."""

from __future__ import annotations

import asyncio
import concurrent.futures
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import approvals_routes, identity_routes, unlock_routes
from tools.dashboard.dao import approval_requests as ar
from tools.dashboard.dao import identity_sessions
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key
from tools.network.idkit.canonical import canonical_json


PASSWORD = "week-glacier-thirty-nine"


def _app() -> Starlette:
    return Starlette(routes=[*approvals_routes.ROUTES, *identity_routes.ROUTES,
                             *unlock_routes.ROUTES])


@pytest.fixture
def grant_env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    GraphDB(orgs_dir / "personal.db").close()
    GraphDB(orgs_dir / "hostile-org-default.db").close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", "hostile-org-default")
    monkeypatch.setenv("APPROVAL_REQUESTS_DB",
                       str(tmp_path / "approval-requests.db"))
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approval-requests.db")
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB",
                       str(tmp_path / "identity-sessions.db"))
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    identity_sessions.reset_for_tests()
    unlock_routes._secret_cache.update({"path": None, "value": None})

    personal = KeyPair.generate()
    client = TestClient(_app(), base_url="https://localhost:8080")
    stored = client.post("/api/identity/personal", json={
        "display_name": "Alex Operator",
        "armored_private_key": encrypt_root_key(
            personal, PASSWORD, iterations=10_000),
    })
    assert stored.status_code == 200, stored.text
    yield client, personal

    identity_sessions.reset_for_tests()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    GraphDB.close_all_pooled()


def _queue(client: TestClient, ephemeral: KeyPair,
           session: str = "host-0715-122549") -> tuple[str, dict]:
    # Existing pre-migration IDs remain readable/decidable/redeemable through
    # the legacy store, but production POST creation is now claimed by the
    # Settings-backed Central bridge and intentionally rejects ``session``.
    rid = asyncio.run(approvals_routes.open_approval(
        kind="dashboard_access",
        session=session,
        request_payload={"ephemeral_pub": ephemeral.public_hex},
    ))
    rendered = client.get(f"/api/approvals/{rid}")
    assert rendered.status_code == 200, rendered.text
    return rid, rendered.json()["staged"]


def _approve(client: TestClient, rid: str, grant: dict,
             personal: KeyPair, **extra) -> dict:
    signature = personal.sign_hex(
        unlock_routes.APPROVAL_GRANT_SIGNING_DOMAIN + canonical_json(grant))
    response = client.post(f"/api/approvals/{rid}/decision", json={
        "approved": True,
        "grant": grant,
        "signature": signature,
        **extra,
    })
    assert response.status_code == 200, response.text
    result = client.get(f"/api/approvals/{rid}?wait=10").json()["result"]
    return result["execution"]


def _proof(ephemeral: KeyPair, nonce: str) -> str:
    return ephemeral.sign_hex(
        unlock_routes.APPROVAL_REDEEM_SIGNING_DOMAIN
        + canonical_json({"v": 1, "nonce": nonce}))


def test_valid_signed_grant_redeems_to_normal_revocable_session(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)

    assert set(grant) == {
        "v", "nonce", "grantee", "ephemeral_pub", "scope",
        "issued_at", "expires_at",
    }
    assert grant["grantee"] == "host-0715-122549"
    assert grant["ephemeral_pub"] == ephemeral.public_hex
    assert grant["scope"] == ["dashboard:ui"]
    assert grant["expires_at"] - grant["issued_at"] == 2 * 60 * 60
    assert ar.set_staged(rid, {**grant, "scope": ["dashboard:admin"]}) is False
    assert _approve(client, rid, grant, personal) == {"ok": True}

    redeemed = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"],
        "proof": _proof(ephemeral, grant["nonce"]),
    })
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json() == {
        "ok": True,
        "method": "approval",
        "grantee": "host-0715-122549",
        "scope": ["dashboard:ui"],
        "expires_at": grant["expires_at"],
    }
    assert "Secure" in redeemed.headers["set-cookie"]

    token = redeemed.cookies.get(unlock_routes.SESSION_COOKIE)
    payload = unlock_routes.verify_session_token(token)
    assert payload is not None and payload["method"] == "approval"
    row = identity_sessions.get_session(payload["sid"], now=time.time())
    assert row is not None
    assert row["method"] == "approval"
    assert row["grantee"] == "host-0715-122549"
    assert row["scope"] == ["dashboard:ui"]
    assert row["expires_at"] == grant["expires_at"]


@pytest.mark.parametrize("request_body", [
    {},
    {"ephemeral_pub": ""},
    {"ephemeral_pub": "A" * 64},
    {"ephemeral_pub": "00" * 32, "scope": ["admin"]},
    {"ephemeral_pub": "00" * 32, "ttl": 99_999_999},
])
def test_create_rejects_malformed_or_policy_bearing_request(grant_env,
                                                            request_body):
    client, _personal = grant_env
    response = client.post("/api/approvals", json={
        "kind": "dashboard_access", "session": "host-1",
        "request": request_body,
    })
    assert response.status_code == 400


def test_decision_refuses_forgery_smuggling_and_wrong_root(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()

    rid, grant = _queue(client, ephemeral, "host-frozen")
    smuggled = {**grant, "scope": ["dashboard:admin"]}
    assert _approve(client, rid, smuggled, personal) == {
        "ok": False,
        "error": "the signed grant does not match the server-frozen request",
    }

    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, KeyPair.generate()) == {
        "ok": False,
        "error": "the approval signature does not verify",
    }

    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, personal, scope=["dashboard:admin"]) == {
        "ok": False,
        "error": (
            "dashboard access approval must carry only approved, grant, and signature"
        ),
    }


def test_decline_creates_no_redeemable_grant(grant_env):
    client, _personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    assert client.post(f"/api/approvals/{rid}/decision",
                       json={"approved": False}).json() == {"ok": True}
    response = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"], "proof": _proof(ephemeral, grant["nonce"]),
    })
    assert response.status_code == 404


def test_unauthenticated_caller_cannot_poison_or_decline_request(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    signature = personal.sign_hex(
        unlock_routes.APPROVAL_GRANT_SIGNING_DOMAIN + canonical_json(grant))
    with TestClient(_app(), base_url="https://localhost:8080") as headless:
        declined = headless.post(f"/api/approvals/{rid}/decision",
                                 json={"approved": False})
        approved = headless.post(f"/api/approvals/{rid}/decision", json={
            "approved": True, "grant": grant, "signature": signature,
        })
    assert declined.status_code == 401
    assert approved.status_code == 401
    assert client.get(f"/api/approvals/{rid}").json()["result"] is None
    assert _approve(client, rid, grant, personal) == {"ok": True}


def test_explicit_auth_kill_switch_allows_signed_recovery_decision(
        grant_env, monkeypatch):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    client.cookies.clear()
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert _approve(client, rid, grant, personal) == {"ok": True}


def test_approval_granted_session_cannot_approve_another_grant(grant_env):
    client, personal = grant_env
    first_ephemeral = KeyPair.generate()
    first_id, first_grant = _queue(client, first_ephemeral, "host-first")
    assert _approve(client, first_id, first_grant, personal) == {"ok": True}
    redeemed = client.post("/api/identity/unlock/approval", json={
        "nonce": first_grant["nonce"],
        "proof": _proof(first_ephemeral, first_grant["nonce"]),
    })
    assert redeemed.status_code == 200

    second_ephemeral = KeyPair.generate()
    second_id, second_grant = _queue(client, second_ephemeral, "host-second")
    signature = personal.sign_hex(
        unlock_routes.APPROVAL_GRANT_SIGNING_DOMAIN + canonical_json(second_grant))
    refused = client.post(f"/api/approvals/{second_id}/decision", json={
        "approved": True, "grant": second_grant, "signature": signature,
    })
    assert refused.status_code == 401
    assert client.get(f"/api/approvals/{second_id}").json()["result"] is None


def test_wrong_proof_does_not_consume_then_valid_proof_is_single_use(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, personal) == {"ok": True}

    wrong = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"],
        "proof": _proof(KeyPair.generate(), grant["nonce"]),
    })
    assert wrong.status_code == 403
    valid = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"], "proof": _proof(ephemeral, grant["nonce"]),
    })
    assert valid.status_code == 200
    replay = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"], "proof": _proof(ephemeral, grant["nonce"]),
    })
    assert replay.status_code == 409


@pytest.mark.parametrize("body", [
    {},
    {"nonce": "not-hex", "proof": "00" * 64},
    {"nonce": "00" * 32, "proof": "00" * 64, "scope": ["admin"]},
])
def test_redemption_rejects_malformed_or_extra_fields(grant_env, body):
    client, _personal = grant_env
    assert client.post("/api/identity/unlock/approval", json=body).status_code == 400


def test_expired_grant_fails_closed(grant_env, monkeypatch):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, personal) == {"ok": True}
    monkeypatch.setattr(unlock_routes, "_now",
                        lambda: grant["expires_at"] + 1)
    response = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"], "proof": _proof(ephemeral, grant["nonce"]),
    })
    assert response.status_code == 410


def test_concurrent_redeem_creates_exactly_one_session(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, personal) == {"ok": True}
    body = {"nonce": grant["nonce"],
            "proof": _proof(ephemeral, grant["nonce"])}

    def redeem():
        with TestClient(_app(), base_url="https://localhost:8080") as other:
            return other.post("/api/identity/unlock/approval", json=body).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _n: redeem(), range(2)))
    assert statuses == [200, 409]
    assert len(identity_sessions.sessions_for_grantee(
        "host-0715-122549", now=time.time())["active"]) == 1


def test_hostile_org_header_cannot_select_approval_verification_root(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, _grant = _queue(client, ephemeral, "host-1")
    grant = client.get(f"/api/approvals/{rid}",
                       headers={"X-Graph-Org": "evil"}).json()["staged"]
    signature = personal.sign_hex(
        unlock_routes.APPROVAL_GRANT_SIGNING_DOMAIN + canonical_json(grant))
    decided = client.post(f"/api/approvals/{rid}/decision",
                          headers={"X-Graph-Org": "evil"}, json={
        "approved": True, "grant": grant, "signature": signature,
    })
    assert decided.status_code == 200
    result = client.get(f"/api/approvals/{rid}?wait=10").json()["result"]
    assert result["execution"] == {"ok": True}


def test_failed_session_insert_rolls_back_consumption(grant_env):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, personal) == {"ok": True}

    conn, lock = identity_sessions._open_pooled(identity_sessions.db_path())
    with lock:
        conn.execute("""
            CREATE TRIGGER reject_approval_session
            BEFORE INSERT ON identity_sessions
            WHEN NEW.method = 'approval'
            BEGIN SELECT RAISE(ABORT, 'forced insert failure'); END
        """)
        conn.commit()
    body = {"nonce": grant["nonce"],
            "proof": _proof(ephemeral, grant["nonce"])}
    failed = client.post("/api/identity/unlock/approval", json=body)
    assert failed.status_code == 503
    assert identity_sessions.get_access_grant(grant["nonce"])["consumed_at"] is None

    with lock:
        conn.execute("DROP TRIGGER reject_approval_session")
        conn.commit()
    assert client.post("/api/identity/unlock/approval", json=body).status_code == 200


def test_store_failure_returns_503_without_cookie(grant_env, monkeypatch):
    client, personal = grant_env
    ephemeral = KeyPair.generate()
    rid, grant = _queue(client, ephemeral)
    assert _approve(client, rid, grant, personal) == {"ok": True}

    def unavailable(**_kwargs):
        raise identity_sessions.SessionStoreError("disk unavailable")

    monkeypatch.setattr(identity_sessions, "redeem_access_grant", unavailable)
    response = client.post("/api/identity/unlock/approval", json={
        "nonce": grant["nonce"], "proof": _proof(ephemeral, grant["nonce"]),
    })
    assert response.status_code == 503
    assert unlock_routes.SESSION_COOKIE not in response.cookies
