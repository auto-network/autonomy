"""The human unlock gate (unlock_routes): sessions, ceremonies, enforcement.

The browser does the visible ceremony (unlock.js); these tests pin the
server half and the gate itself:

* session tokens are signed and tamper-evident: forged payloads, forged
  signatures, expiry, and cross-secret tokens are all rejected;
* fail-open-then-enforce: with NOTHING enrolled every human path is
  open (bootstrap can't lock itself out); the moment a personal
  identity or a passkey exists, page loads redirect to /unlock,
  fragments and websockets are refused — and the AGENT ``/api``
  surface stays open throughout;
* creating the identity mints the bootstrap session (the enrolling
  browser is never locked out mid-onboarding) and gates the passkey
  register endpoints (an open register path would let anyone enroll
  their own credential and walk in);
* passkey ASSERT ceremony discipline matches the register side:
  challenge single-use + TTL + bounded concurrent options, completion
  host-locked, unknown credentials refused, user-verification required,
  sign-count regressions refused and the stored count advances;
* password unlock is the always-available floor: works with zero
  passkeys, single-use host-bound challenges, and only a signature by
  the STORED personal root verifies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from tools.dashboard import identity_routes, unlock_routes
from tools.dashboard.dao import identity_sessions
from tools.graph import settings_ops
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key
from tools.network.idkit.canonical import canonical_json

ORG = "unlockorg"
PASSWORD = "week-glacier-thirty-nine"
HOST = "localhost:8080"
TSNET_HOST = "dash.tail1234.ts.net"


# ── app under test: pages + agent api + gate, like the real server ────


async def _page(request):
    return HTMLResponse("<html>dashboard</html>")


async def _fragment(request):
    return HTMLResponse("<div>fragment</div>")


async def _agent_api(request):
    return JSONResponse({"ok": True, "who": "agent"})


async def _unlock_page(request):
    return HTMLResponse("<html>unlock</html>")


async def _ws_echo(websocket):
    await websocket.accept()
    await websocket.send_text("hello")
    await websocket.close()


def _build_app():
    routes = [
        Route("/", _page),
        Route("/beads", _page),
        Route("/pages/beads", _fragment),
        Route("/unlock", _unlock_page),
        Route("/missions/{mission_id}", _page),
        Route("/mission-control", _page),
        Route("/api/graph/search", _agent_api),
        Route("/api/worktrees", _agent_api, methods=["GET", "POST"]),
        WebSocketRoute("/ws/terminal", _ws_echo),
        *identity_routes.ROUTES,
        *unlock_routes.ROUTES,
    ]
    return Starlette(routes=routes,
                     middleware=[Middleware(unlock_routes.HumanGateMiddleware)])


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    # Agreement pin (test_feature_flags.py's recipe): the routes write at
    # org=None while the tests read/write at explicit org=ORG, so the pin
    # points AT the orgs tree's own db for ORG — explicit-org resolution
    # and the pin converge on one hermetic file instead of the pin
    # contradicting the org (OrgResolutionConflict).
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_DB", str(orgs_dir / f"{ORG}.db"))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB",
                       str(tmp_path / "identity-sessions.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    identity_sessions.reset_for_tests()
    with TestClient(_build_app(), base_url=f"https://{HOST}") as client:
        yield client
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    identity_sessions.reset_for_tests()
    GraphDB.close_all_pooled()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _armor(root: KeyPair) -> str:
    return encrypt_root_key(root, PASSWORD, iterations=10_000)


def _store_identity(client, root: KeyPair, name="Alex"):
    r = client.post("/api/identity/personal",
                    json={"display_name": name,
                          "armored_private_key": _armor(root)})
    assert r.status_code == 200, r.text
    return r


# ── authenticator emulation ───────────────────────────────────────────
#
# Same software authenticator as the register-side tests, but the P-256
# key is kept so the SAME credential can later sign assertions.


def _cose_p256(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    nums = private_key.public_key().public_numbers()
    return cbor2.dumps({
        1: 2, 3: -7, -1: 1,
        -2: nums.x.to_bytes(32, "big"),
        -3: nums.y.to_bytes(32, "big"),
    })


def _make_attestation(private_key, challenge_b64url: str, *, rp_id: str,
                      origin: str, cred_id: bytes = b"test-credential-0001",
                      sign_count: int = 0) -> dict:
    cose_key = _cose_p256(private_key)
    auth_data = (
        hashlib.sha256(rp_id.encode()).digest()
        + bytes([0x45])                     # UP | UV | AT
        + struct.pack(">I", sign_count)
        + b"\x00" * 16
        + struct.pack(">H", len(cred_id))
        + cred_id
        + cose_key
    )
    client_data = json.dumps({
        "type": "webauthn.create", "challenge": challenge_b64url,
        "origin": origin, "crossOrigin": False,
    }).encode()
    attestation_object = cbor2.dumps({
        "fmt": "none", "attStmt": {}, "authData": auth_data,
    })
    return {
        "id": _b64url(cred_id), "rawId": _b64url(cred_id),
        "type": "public-key", "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": _b64url(client_data),
            "attestationObject": _b64url(attestation_object),
            "transports": ["internal"],
        },
    }


def _make_assertion(private_key, challenge_b64url: str, *, rp_id: str,
                    origin: str, cred_id: bytes = b"test-credential-0001",
                    flags: int = 0x05,        # UP | UV
                    sign_count: int = 1,
                    cred_type: str = "webauthn.get") -> dict:
    auth_data = (
        hashlib.sha256(rp_id.encode()).digest()
        + bytes([flags])
        + struct.pack(">I", sign_count)
    )
    client_data = json.dumps({
        "type": cred_type, "challenge": challenge_b64url,
        "origin": origin, "crossOrigin": False,
    }).encode()
    signature = private_key.sign(
        auth_data + hashlib.sha256(client_data).digest(),
        ec.ECDSA(hashes.SHA256()))
    return {
        "id": _b64url(cred_id), "rawId": _b64url(cred_id),
        "type": "public-key", "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": _b64url(client_data),
            "authenticatorData": _b64url(auth_data),
            "signature": _b64url(signature),
        },
    }


def _enroll_passkey(client, *, host=HOST, cred_id=b"test-credential-0001",
                    sign_count: int = 0):
    """Register a credential; returns its P-256 key for later assertions."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    opts = client.post("/api/identity/passkey/register-options", json={},
                       headers={"host": host})
    assert opts.status_code == 200, opts.text
    body = opts.json()
    credential = _make_attestation(
        private_key, body["options"]["challenge"], rp_id=body["rp_id"],
        origin=body["origin"], cred_id=cred_id, sign_count=sign_count)
    r = client.post("/api/identity/passkey/register",
                    json={"credential": credential}, headers={"host": host})
    assert r.status_code == 200, r.text
    return private_key


def _assert_options(client, *, host=HOST):
    r = client.post("/api/identity/unlock/passkey/options", json={},
                    headers={"host": host})
    assert r.status_code == 200, r.text
    return r.json()


def _unlock_with_passkey(client, private_key, *, host=HOST,
                         cred_id=b"test-credential-0001", sign_count=1):
    minted = _assert_options(client, host=host)
    assertion = _make_assertion(
        private_key, minted["options"]["challenge"], rp_id=minted["rp_id"],
        origin=minted["origin"], cred_id=cred_id, sign_count=sign_count)
    return client.post("/api/identity/unlock/passkey",
                       json={"credential": assertion}, headers={"host": host})


def _pw_sign(root: KeyPair, challenge: str, origin: str) -> str:
    message = unlock_routes.UNLOCK_SIGNING_DOMAIN + canonical_json(
        {"v": 1, "challenge": challenge, "origin": origin})
    return root.sign_hex(message)


def _unlock_with_password(client, root: KeyPair, *, host=HOST):
    minted = client.post("/api/identity/unlock/password/options", json={},
                         headers={"host": host})
    assert minted.status_code == 200, minted.text
    body = minted.json()
    return client.post("/api/identity/unlock/password",
                       json={"challenge": body["challenge"],
                             "signature": _pw_sign(root, body["challenge"],
                                                   body["origin"])},
                       headers={"host": host})


# ── session tokens: signed, tamper-evident ────────────────────────────


def test_session_token_roundtrip(env):
    token = unlock_routes.mint_session_token("passkey")
    payload = unlock_routes.verify_session_token(token)
    assert payload is not None
    assert payload["method"] == "passkey"
    assert payload["exp"] > payload["iat"]


def test_session_token_rejects_tampered_payload(env):
    token = unlock_routes.mint_session_token("passkey")
    body, sig = token.split(".")
    payload = json.loads(_b64url_decode(body))
    payload["exp"] += 10_000_000
    forged = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    assert unlock_routes.verify_session_token(f"{forged}.{sig}") is None


def test_session_token_rejects_tampered_signature(env):
    token = unlock_routes.mint_session_token("passkey")
    body, sig = token.split(".")
    bad = _b64url(bytes(32))
    assert unlock_routes.verify_session_token(f"{body}.{bad}") is None
    assert unlock_routes.verify_session_token(body) is None
    assert unlock_routes.verify_session_token("") is None
    assert unlock_routes.verify_session_token("a.b.c") is None
    assert unlock_routes.verify_session_token("!!!.???") is None


def test_session_token_rejects_expired(env):
    secret = unlock_routes._session_secret()
    payload = {"v": 1, "sid": "x", "method": "passkey",
               "iat": 1000, "exp": 2000}   # long past
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url(hmac_mod.new(secret, body.encode(), hashlib.sha256).digest())
    identity_sessions.create_session(
        sid="x", method="passkey", created_at=1000, expires_at=2000,
        last_activity=1000,
    )
    assert unlock_routes.verify_session_token(f"{body}.{sig}") is None


def test_signed_cookie_without_server_row_is_rejected(env):
    secret = unlock_routes._session_secret()
    now = int(unlock_routes._now())
    payload = {"v": 1, "sid": "legacy-stateless", "method": "passkey",
               "iat": now, "exp": now + 3600}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url(hmac_mod.new(secret, body.encode(), hashlib.sha256).digest())
    assert unlock_routes._verified_session_payload(f"{body}.{sig}") == payload
    assert unlock_routes.verify_session_token(f"{body}.{sig}") is None


def test_session_token_rejects_foreign_secret(env, tmp_path):
    token = unlock_routes.mint_session_token("passkey")
    # Rotate to a different secret: previously minted tokens die with it.
    unlock_routes._secret_cache.update(
        {"path": None, "value": None})
    (tmp_path / "session.secret").unlink()
    assert unlock_routes.verify_session_token(token) is None


# ── fail-open-then-enforce ────────────────────────────────────────────


def test_everything_open_before_enrollment(env):
    assert env.get("/beads").status_code == 200
    assert env.get("/pages/beads").status_code == 200
    assert env.get("/api/graph/search").status_code == 200
    r = env.post("/api/identity/passkey/register-options", json={})
    assert r.status_code == 409          # needs an identity — NOT the gate
    with env.websocket_connect("/ws/terminal") as ws:
        assert ws.receive_text() == "hello"


def test_broken_session_store_does_not_brick_unenrolled_bootstrap(
        env, monkeypatch):
    def should_not_run(**_kwargs):
        raise AssertionError("session store must not decide enrollment")

    monkeypatch.setattr(identity_sessions, "check_active", should_not_run)
    assert env.get("/beads").status_code == 200


def test_identity_enrollment_turns_the_gate_on(env, root):
    _store_identity(env, root)
    env.cookies.clear()                  # a browser without the session
    r = env.get("/beads", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/unlock?next=%2Fbeads")
    assert env.get("/pages/beads").status_code == 401
    # the Unlock page itself and its data endpoints stay reachable
    assert env.get("/unlock").status_code == 200
    assert env.get("/api/identity/status").status_code == 200
    assert env.get("/api/identity/personal").status_code == 200


def test_agent_api_stays_open_under_enforcement(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    # The container/agent surface (graph CLI, dispatcher) is untouched:
    # no cookie, no redirect, plain 200s.
    assert env.get("/api/graph/search").status_code == 200
    assert env.post("/api/worktrees").status_code == 200


def test_mission_site_route_stays_open_under_enforcement(env, root):
    """/missions/<id> — operator-ratified parity with Present's high-entropy
    deck links: the id itself is the access control until P2's identity
    shim, so this route bypasses the gate like the agent /api/ surface."""
    _store_identity(env, root)
    env.cookies.clear()
    assert env.get("/missions/33d6c481-4726-4777-a298-4b2f20398e61").status_code == 200


def test_mission_control_management_page_stays_gated(env, root):
    """The /missions/ exemption must not leak into /mission-control (no
    trailing slash, hyphenated) — that's the plugin's own authenticated
    management page, not a mission-site link."""
    _store_identity(env, root)
    env.cookies.clear()
    r = env.get("/mission-control", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/unlock?next=%2Fmission-control")


def test_websocket_refused_under_enforcement(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    with pytest.raises(Exception):
        with env.websocket_connect("/ws/terminal"):
            pass


# ── DASHBOARD_AUTH kill-switch (env-only recovery hatch) ──────────────


def test_kill_switch_disables_enforcement(env, root, monkeypatch):
    _store_identity(env, root)
    env.cookies.clear()
    assert env.get("/beads", follow_redirects=False).status_code == 302
    for value in ("off", "OFF", " Off ", "0", "false", "False", "no", "NO"):
        monkeypatch.setenv("DASHBOARD_AUTH", value)
        assert env.get("/beads").status_code == 200, value
        assert env.get("/pages/beads").status_code == 200, value
    with env.websocket_connect("/ws/terminal") as ws:
        assert ws.receive_text() == "hello"
    # the gated register endpoints open too — recovery mode is total
    assert env.post("/api/identity/passkey/register-options",
                    json={}).status_code == 200


def test_kill_switch_fails_safe_on_any_other_value(env, root, monkeypatch):
    _store_identity(env, root)
    env.cookies.clear()
    for value in ("", "on", "1", "true", "yes", "enforce", "of", "OFF!",
                  "disable", "none", "null"):
        monkeypatch.setenv("DASHBOARD_AUTH", value)
        r = env.get("/beads", follow_redirects=False)
        assert r.status_code == 302, f"DASHBOARD_AUTH={value!r} must enforce"
    monkeypatch.delenv("DASHBOARD_AUTH")
    assert env.get("/beads", follow_redirects=False).status_code == 302


def test_kill_switch_is_never_request_controllable(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    attempts = [
        {"headers": {"X-Dashboard-Auth": "off"}},
        {"headers": {"DASHBOARD_AUTH": "off"}},
        {"headers": {"Cookie": "DASHBOARD_AUTH=off"}},
        {"params": {"DASHBOARD_AUTH": "off"}},
        {"params": {"dashboard_auth": "off"}},
    ]
    for kw in attempts:
        r = env.get("/beads", follow_redirects=False, **kw)
        assert r.status_code == 302, kw
    r = env.post("/api/identity/passkey/register-options", json={},
                 headers={"X-Dashboard-Auth": "off"})
    assert r.status_code == 401


def test_kill_switch_reenables_the_moment_env_changes(env, root, monkeypatch):
    _store_identity(env, root)
    env.cookies.clear()
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert env.get("/beads").status_code == 200
    monkeypatch.setenv("DASHBOARD_AUTH", "back-on")
    assert env.get("/beads", follow_redirects=False).status_code == 302


def test_kill_switch_beats_wedged_enrollment_read(env, root, monkeypatch):
    """The hatch must work even when the settings-DB read is broken —
    that is exactly the failure it exists to recover from, so it is
    checked BEFORE the enrollment lookup."""
    _store_identity(env, root)
    env.cookies.clear()

    def boom(_org):
        raise RuntimeError("settings DB wedged")

    monkeypatch.setattr(unlock_routes, "_personal_member", boom)
    monkeypatch.setattr(unlock_routes, "_passkey_rows", boom)
    unlock_routes._enforce_cache.update({"at": 0.0, "value": True})
    assert env.get("/beads", follow_redirects=False).status_code == 302
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert env.get("/beads").status_code == 200


class TestGateFailsClosedOnReadError:
    def test_enrolled_cold_cache_fails_closed(self, env, root, monkeypatch):
        _store_identity(env, root)
        unlock_routes._enforce_cache.update({"at": 0.0, "value": None})

        def unavailable():
            raise RuntimeError("settings DB unavailable")

        monkeypatch.setattr(unlock_routes, "_personal_member", unavailable)

        assert unlock_routes.human_auth_enrolled() is True
        # A failure is not cached, so recovery is observed on the next call.
        assert unlock_routes._enforce_cache == {"at": 0.0, "value": None}

    def test_fresh_missing_store_reads_empty_and_stays_open(
            self, env, tmp_path):
        personal_db = tmp_path / "orgs" / f"{ORG}.db"   # the env fixture's pin
        assert not personal_db.exists()

        assert unlock_routes.human_auth_enrolled() is False
        assert personal_db.exists()

    def test_dashboard_auth_off_bypasses_unreadable_store(
            self, env, monkeypatch):
        reads = 0

        def unavailable():
            nonlocal reads
            reads += 1
            raise RuntimeError("settings DB unavailable")

        monkeypatch.setattr(unlock_routes, "_personal_member", unavailable)
        unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
        monkeypatch.setenv("DASHBOARD_AUTH", "off")

        assert env.get("/beads").status_code == 200
        assert reads == 0

    def test_short_write_lock_recovers_without_gate_flip(
            self, env, root, tmp_path, monkeypatch):
        import sqlite3
        import threading
        from tools.graph import db as graph_db

        _store_identity(env, root)
        personal_db = tmp_path / "orgs" / f"{ORG}.db"   # the env fixture's pin
        with sqlite3.connect(personal_db) as conn:
            conn.execute("PRAGMA user_version = 0")

        holder = sqlite3.connect(personal_db, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.15, holder.rollback)
        release.start()
        monkeypatch.setattr(
            graph_db, "_SQLITE_CONNECT_TIMEOUT_S", 0.01
        )
        unlock_routes._enforce_cache.update({"at": 0.0, "value": None})

        try:
            assert unlock_routes.human_auth_enrolled() is True
            # A cached value proves the read recovered rather than taking
            # the uncached fail-closed branch.
            assert unlock_routes._enforce_cache["value"] is True
        finally:
            release.join(timeout=1)
            if holder.in_transaction:
                holder.rollback()
            holder.close()

    def test_long_write_lock_fails_closed_then_recovers(
            self, env, tmp_path, monkeypatch):
        import sqlite3
        from tools.graph import db as graph_db
        from tools.graph.db import GraphDB

        personal_db = tmp_path / "orgs" / f"{ORG}.db"   # the env fixture's pin
        GraphDB(personal_db).close()
        with sqlite3.connect(personal_db) as conn:
            conn.execute("PRAGMA user_version = 0")

        holder = sqlite3.connect(personal_db)
        holder.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(graph_db, "_SQLITE_CONNECT_TIMEOUT_S", 0.0)
        monkeypatch.setattr(
            graph_db, "_RW_OPEN_BACKOFF_S", (0.001, 0.001, 0.001)
        )
        monkeypatch.setattr(
            unlock_routes, "_ENROLLMENT_READ_RETRY_S", 0.001
        )
        unlock_routes._enforce_cache.update({"at": 0.0, "value": None})

        try:
            assert unlock_routes.human_auth_enrolled() is True
            assert unlock_routes._enforce_cache == {
                "at": 0.0,
                "value": None,
            }
        finally:
            holder.rollback()
            holder.close()

        assert unlock_routes.human_auth_enrolled() is False
        assert unlock_routes._enforce_cache["value"] is False

    @pytest.mark.parametrize("cached", [False, True])
    def test_warm_cache_is_returned_without_read(
            self, env, monkeypatch, cached):
        now = 10_000.0
        unlock_routes._enforce_cache.update({"at": now, "value": cached})
        monkeypatch.setattr(unlock_routes, "_now", lambda: now + 1.0)

        def unavailable():
            raise AssertionError("warm cache must avoid enrollment read")

        monkeypatch.setattr(unlock_routes, "_personal_member", unavailable)

        assert unlock_routes.human_auth_enrolled() is cached


def test_server_startup_warm_open_prepares_first_gate_read(
        env, tmp_path, monkeypatch):
    import sqlite3
    from tools.dashboard import server
    from tools.graph import db as graph_db
    from tools.graph.db import GraphDB

    personal_db = tmp_path / "orgs" / f"{ORG}.db"   # the env fixture's pin
    assert not personal_db.exists()

    server._warm_personal_settings_store()

    with sqlite3.connect(personal_db) as conn:
        assert conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == graph_db._SCHEMA_USER_VERSION

    def unexpected_schema_init(_self):
        raise AssertionError("first gate read must use the warmed schema")

    monkeypatch.setattr(GraphDB, "_migrate_settings", unexpected_schema_init)
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    assert unlock_routes.human_auth_enrolled() is False


def test_store_failure_is_closed_when_enrolled_but_kill_switch_escapes(
        env, root, monkeypatch):
    _store_identity(env, root)

    def unavailable(**_kwargs):
        raise identity_sessions.SessionStoreError("disk unavailable")

    monkeypatch.setattr(identity_sessions, "check_active", unavailable)
    assert env.get("/beads", follow_redirects=False).status_code == 302
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert env.get("/beads").status_code == 200


def test_status_surfaces_gate_disabled(env, root, monkeypatch):
    _store_identity(env, root)
    assert env.get("/api/identity/status").json()["gate_disabled"] is False
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert env.get("/api/identity/status").json()["gate_disabled"] is True
    monkeypatch.setenv("DASHBOARD_AUTH", "on")
    assert env.get("/api/identity/status").json()["gate_disabled"] is False


def test_session_surfaces_gate_disabled_and_zeroes_enforced(env, root,
                                                            monkeypatch):
    """The chrome's forced-open marker: while the switch is on,
    'enforced' reports what the gate actually does (nothing), not what
    enrollment alone would imply."""
    _store_identity(env, root)
    r = env.get("/api/identity/session").json()
    assert r["enforced"] is True
    assert r["gate_disabled"] is False
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    r = env.get("/api/identity/session").json()
    assert r["enforced"] is False
    assert r["gate_disabled"] is True
    monkeypatch.setenv("DASHBOARD_AUTH", "definitely-not-off")
    r = env.get("/api/identity/session").json()
    assert r["enforced"] is True
    assert r["gate_disabled"] is False


def test_bootstrap_session_minted_on_identity_creation(env, root):
    r = _store_identity(env, root)
    assert unlock_routes.SESSION_COOKIE in r.cookies
    # The enrolling browser keeps working through the now-closed gate…
    assert env.get("/beads").status_code == 200
    assert env.get("/pages/beads").status_code == 200
    # …including the passkey enrollment step it is about to run.
    assert env.post("/api/identity/passkey/register-options",
                    json={}).status_code == 200


def test_identity_creation_survives_unwritable_session_secret(env, root, monkeypatch):
    """If the session-secret store can't be written, POST /api/identity/
    personal must still succeed (identity is already persisted) — a 500
    here would strand the operator with a 409-on-retry behind a gate they
    can't pass. The bootstrap cookie is best-effort."""
    def boom(_method):
        raise OSError("read-only filesystem")

    real_mint = unlock_routes.mint_session_token
    monkeypatch.setattr(unlock_routes, "mint_session_token", boom)
    r = _store_identity(env, root)
    assert r.status_code == 200
    assert unlock_routes.SESSION_COOKIE not in r.cookies   # no cookie, but no crash
    # The gate is on; the password floor still lets the operator in.
    # Restore only mint (not env — env/test share pytest's monkeypatch).
    monkeypatch.setattr(unlock_routes, "mint_session_token", real_mint)
    env.cookies.clear()
    assert _unlock_with_password(env, root).status_code == 200


def test_session_secret_survives_concurrent_creation(env, tmp_path, monkeypatch):
    """Two workers racing to create the secret must converge on one value
    (O_EXCL winner), not cache divergent per-worker secrets."""
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE", str(tmp_path / "race.secret"))
    unlock_routes._secret_cache.update({"path": None, "value": None})
    first = unlock_routes._session_secret()
    # Simulate a second worker with a cold cache reading the same file.
    unlock_routes._secret_cache.update({"path": None, "value": None})
    second = unlock_routes._session_secret()
    assert first == second


def test_gate_401_carries_unlock_header(env, root):
    """The fragment/API 401 must carry X-Autonomy-Unlock so the SPA guard
    can turn it into a navigation to /unlock instead of injecting the body."""
    _store_identity(env, root)
    env.cookies.clear()
    r = env.get("/pages/beads")
    assert r.status_code == 401
    assert r.headers.get("x-autonomy-unlock") == "/unlock"


def test_register_endpoints_gated_once_enforced(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    # An attacker on the network can no longer enroll their own passkey.
    r = env.post("/api/identity/passkey/register-options", json={})
    assert r.status_code == 401
    r = env.post("/api/identity/passkey/register", json={"credential": {}})
    assert r.status_code == 401


def test_redirect_preserves_query(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    r = env.get("/beads?tab=ready&x=1", follow_redirects=False)
    assert r.status_code == 302
    assert "next=%2Fbeads%3Ftab%3Dready%26x%3D1" in r.headers["location"]


def test_sanitize_next():
    ok = unlock_routes.sanitize_next
    assert ok("/beads?tab=1") == "/beads?tab=1"
    assert ok(None) == "/"
    assert ok("") == "/"
    assert ok("https://evil.example") == "/"
    assert ok("//evil.example") == "/"
    assert ok("/\\evil") == "/"
    assert ok("javascript:alert(1)") == "/"


def test_gate_ignores_attacker_caller_org(env, root, monkeypatch):
    """The gate resolves enrollment through explicit personal scope,
    never the request's X-Graph-Org. If it honoured the header, an
    attacker could name an un-enrolled org and fail the lock open (and
    poison the shared cache to False for everyone). The helper takes no org
    argument, so even a bound caller context cannot redirect the lookup."""
    from tools.graph import ops
    _store_identity(env, root)
    unlock_routes.bust_enforce_cache()

    seen = {}
    real = unlock_routes._personal_member

    def spy():
        seen["ctx"] = ops._caller_org_var.get()
        return real()

    monkeypatch.setattr(unlock_routes, "_personal_member", spy)
    # Simulate request middleware having bound an attacker-chosen org.
    token = ops.set_caller_org("attacker-bogus-org")
    try:
        result = unlock_routes.human_auth_enrolled()
    finally:
        ops.reset_caller_org(token)
    assert result is True                # still sees the real enrollment
    assert seen["ctx"] == "attacker-bogus-org"  # context cannot affect helper


def test_gate_enforces_despite_bogus_org_header(env, root):
    """End-to-end: a gated request carrying a bogus X-Graph-Org is still
    redirected, and does not open the gate for a following normal request
    (no cache poisoning)."""
    _store_identity(env, root)
    env.cookies.clear()
    r = env.get("/beads", headers={"X-Graph-Org": "nothing-here"},
                follow_redirects=False)
    assert r.status_code == 302
    # The next plain request is still gated (cache wasn't flipped open).
    assert env.get("/beads", follow_redirects=False).status_code == 302


# ── generic-settings write guard (Codex bypass regressions) ───────────
#
# The unlock gate leaves the /api surface open for agents, but the passkey
# and personal-identity Settings sets ARE dashboard credentials — writing
# them through the generic settings API (add/upsert/override/exclude/
# promote/deprecate/delete/migrate) would defeat the gate. settings_ops
# refuses those set IDs unless the caller carries the identity-route
# capability. These tests drive the ops layer directly (the same functions
# POST /api/graph/setting and friends call).

from tools.graph import settings_ops as _sops
from tools.graph.schemas.personal_identity import (
    PASSKEY_SET_ID as _PASSKEY_SET,
    PERSONAL_IDENTITY_SET_ID as _PERSONAL_SET,
)


def _valid_passkey_payload(cred_id="attacker-injected-cred1"):
    return {
        "credential_id": cred_id,
        "public_key": _b64url(b"\x01" * 64),
        "sign_count": 0,
        "rp_id": "localhost",
        "origin": f"https://{HOST}",
        "label": "attacker device",
        "transports": ["internal"],
        "created_at": "2026-07-19T00:00:00Z",
    }


def test_passkey_injection_via_generic_settings_is_refused(env, root):
    """Codex bypass #1: a no-session caller POSTs a valid passkey row via
    the generic settings create path, then unlocks with their own key.
    The write must be refused at the data layer."""
    _store_identity(env, root)
    with pytest.raises(_sops.ProtectedSettingError):
        _sops.add_setting(_PASSKEY_SET, 1, "attacker-injected-cred1",
                          _valid_passkey_payload(), org=ORG)
    # upsert path too (what an attacker might reach for next).
    with pytest.raises(_sops.ProtectedSettingError):
        _sops.upsert_by_key(_PASSKEY_SET, 1, "attacker-injected-cred1",
                            _valid_passkey_payload(), org=ORG)
    assert _sops.read_set(_PASSKEY_SET, org=ORG).members == []


def test_identity_shadow_via_generic_settings_is_refused(env, root):
    """Codex bypass #2: a no-session caller writes a personal-identity row
    with a low-sorting key to SHADOW the operator's 'default', then
    password-unlocks against their own root. The write must be refused,
    and even if one existed the canonical-label pin must ignore it."""
    _store_identity(env, root)
    attacker = KeyPair.generate()
    shadow_payload = {
        "armored_private_key": _armor(attacker),
        "root_pub": attacker.public_hex,
        "display_name": "attacker",
        "created_at": "2026-07-19T00:00:00Z",
    }
    with pytest.raises(_sops.ProtectedSettingError):
        _sops.add_setting(_PERSONAL_SET, 1, "000-attacker-shadows-default",
                          shadow_payload, org=ORG)
    # The operator's identity is still the one that verifies.
    env.cookies.clear()
    assert _unlock_with_password(env, root).status_code == 200
    env.cookies.clear()
    assert _unlock_with_password(env, attacker).status_code == 403


def test_canonical_label_pin_defeats_a_shadow_row(env, root, monkeypatch):
    """Defense-in-depth: even if a shadow row is present (written WITH the
    capability, e.g. a bug elsewhere), _personal_member pins to 'default'
    so the operator's root — not the low-key attacker row — verifies."""
    _store_identity(env, root)                     # writes key 'default'
    attacker = KeyPair.generate()
    with _sops.identity_write_context():           # simulate a row slipping in
        _sops.add_setting(_PERSONAL_SET, 1, "000-shadow", {
            "armored_private_key": _armor(attacker),
            "root_pub": attacker.public_hex,
            "display_name": "attacker",
            "created_at": "2026-07-19T00:00:00Z",
        }, org=ORG)
    member = identity_routes._personal_member()
    assert member.key == "default"
    assert member.payload["root_pub"] == root.public_hex
    env.cookies.clear()
    assert _unlock_with_password(env, attacker).status_code == 403
    env.cookies.clear()
    assert _unlock_with_password(env, root).status_code == 200


def test_auth_reads_ignore_canonical_identity_and_passkey_from_peer_org(
    tmp_path, monkeypatch, root,
):
    """Auth decisions are physically personal, never peer-composed.

    ``org=None`` chooses personal.db but ordinary ``read_set`` still merges
    every subscribed peer.  A peer's canonical ``default`` identity would
    otherwise outrank the operator's raw row, while any published peer
    passkey would otherwise be added to the accepted credential list.
    """
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    # Point the env at the orgs tree BEFORE creating the DBs:
    # AUTONOMY_ORGS_DIR outranks create_org_db's root= argument
    # (resolve_orgs_root precedence), so creating first would land the
    # rows in the ambient hermetic tree where later resolution never looks.
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.create_org_db("personal", type_="personal", root=orgs_dir).close()
    GraphDB.create_org_db(ORG, root=orgs_dir).close()
    GraphDB.create_org_db("hostile", root=orgs_dir).close()
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB",
                       str(tmp_path / "identity-sessions.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    identity_sessions.reset_for_tests()

    attacker = KeyPair.generate()
    operator_payload = {
        "armored_private_key": _armor(root),
        "root_pub": root.public_hex,
        "display_name": "operator",
        "created_at": "2026-07-20T00:00:00Z",
    }
    attacker_payload = {
        "armored_private_key": _armor(attacker),
        "root_pub": attacker.public_hex,
        "display_name": "attacker",
        "created_at": "2026-07-20T00:00:00Z",
    }
    hostile_passkey = _valid_passkey_payload("hostile-credential")
    with settings_ops.identity_write_context():
        settings_ops.add_setting(
            _PERSONAL_SET, 1, "default", operator_payload, org=None,
        )
        settings_ops.add_setting(
            _PERSONAL_SET, 1, "default", attacker_payload,
            org="hostile", state="canonical",
        )
        settings_ops.add_setting(
            _PASSKEY_SET, 1, "hostile-credential", hostile_passkey,
            org="hostile", state="published",
        )

    # Prove the fixture is adversarial: the ordinary composed Settings view
    # picks the peer's higher-precedence identity and includes its passkey.
    composed_identity = settings_ops.read_set(_PERSONAL_SET, org=None).members
    assert composed_identity[0].org == "hostile"
    assert composed_identity[0].payload["root_pub"] == attacker.public_hex
    assert [m.key for m in settings_ops.read_set(_PASSKEY_SET, org=None).members] \
        == ["hostile-credential"]

    # The auth-specific readers must instead remain inside personal.db.
    assert identity_routes._personal_member().payload["root_pub"] == root.public_hex
    assert identity_routes._passkey_rows() == []

    with TestClient(_build_app(), base_url=f"https://{HOST}") as client:
        client.cookies.clear()
        assert _unlock_with_password(client, attacker).status_code == 403
        client.cookies.clear()
        assert _unlock_with_password(client, root).status_code == 200

    identity_sessions.reset_for_tests()
    GraphDB.close_all_pooled()


def test_every_mutation_path_refuses_protected_identity_sets(env, root):
    """add / upsert / override / exclude / promote / deprecate / delete /
    remove-by-prefix / migrate must ALL refuse the protected sets without
    the capability. Create the target rows WITH the capability first, so
    the id-based paths have something to aim at."""
    _store_identity(env, root)
    with _sops.identity_write_context():
        pk_id = _sops.add_setting(_PASSKEY_SET, 1, "victim-cred",
                                 _valid_passkey_payload("victim-cred"), org=ORG)
        personal = identity_routes._personal_member()
    # Resolve the personal row's setting id for the id-based paths.
    personal_rows = _sops.read_set(_PERSONAL_SET, org=ORG).members
    personal_id = next(m.id for m in personal_rows if m.key == "default")

    # set_id-addressed paths
    for call in (
        lambda: _sops.add_setting(_PASSKEY_SET, 1, "x",
                                  _valid_passkey_payload("x"), org=ORG),
        lambda: _sops.upsert_by_key(_PERSONAL_SET, 1, "default", {}, org=ORG),
        lambda: _sops.remove_settings_by_key_prefix(_PASSKEY_SET, prefix="",
                                                    org=ORG),
        lambda: _sops.migrate_setting_revisions(_PASSKEY_SET, 1, org=ORG),
    ):
        with pytest.raises(_sops.ProtectedSettingError):
            call()

    # id-addressed paths (resolve the target's set_id, then refuse)
    for call in (
        lambda: _sops.override_setting(pk_id, {"sign_count": 999}, org=ORG),
        lambda: _sops.exclude_setting(personal_id, org=ORG),
        lambda: _sops.promote_setting(pk_id, "published", org=ORG),
        lambda: _sops.deprecate_setting(personal_id, org=ORG),
        lambda: _sops.remove_setting(personal_id, org=ORG),
    ):
        with pytest.raises(_sops.ProtectedSettingError):
            call()

    # Nothing was mutated: the victim rows survive, the operator still unlocks.
    assert len(_sops.read_set(_PASSKEY_SET, org=ORG).members) == 1
    env.cookies.clear()
    assert _unlock_with_password(env, root).status_code == 200


def test_delete_of_enrollment_cannot_disable_the_gate(env, root):
    """The nastiest variant: DELETE the operator's identity to make the
    gate read 'nothing enrolled' → fail-open. Must be refused."""
    _store_identity(env, root)
    unlock_routes.bust_enforce_cache()
    assert unlock_routes.human_auth_enrolled() is True
    personal_id = next(m.id for m in _sops.read_set(_PERSONAL_SET, org=ORG).members
                       if m.key == "default")
    with pytest.raises(_sops.ProtectedSettingError):
        _sops.remove_setting(personal_id, org=ORG)
    unlock_routes.bust_enforce_cache()
    assert unlock_routes.human_auth_enrolled() is True     # gate still on


def test_non_identity_sets_are_unaffected_by_the_guard(env):
    """The guard is scoped to the identity sets — ordinary settings writes
    through the generic path keep working."""
    from tools.graph import schemas
    # A throwaway schema-less write would fail validation; use an existing
    # benign set if present, else assert the guard only trips on our sets.
    assert "some.other.set" not in _sops.PROTECTED_IDENTITY_SET_IDS
    # Directly: the guard helper is a no-op for non-protected ids.
    _sops._guard_protected_set("some.other.set")           # must not raise


def test_mock_mode_never_enforces(env, root, monkeypatch):
    _store_identity(env, root)
    env.cookies.clear()
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    unlock_routes.bust_enforce_cache()
    assert env.get("/beads").status_code == 200


# ── passkey unlock (assert ceremony) ──────────────────────────────────


def test_passkey_unlock_happy_path(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    r = _unlock_with_passkey(env, key)
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "passkey"
    assert unlock_routes.SESSION_COOKIE in r.cookies
    token = r.cookies.get(unlock_routes.SESSION_COOKIE)
    payload = unlock_routes._verified_session_payload(token)
    row = identity_sessions.get_session(payload["sid"], now=unlock_routes._now())
    assert row["credential_id"] == _b64url(b"test-credential-0001")
    assert env.get("/beads").status_code == 200        # gate passes now


def test_assert_options_carry_only_this_hosts_credentials(env, root):
    _store_identity(env, root)
    _enroll_passkey(env, cred_id=b"local-cred-000000001")
    _enroll_passkey(env, host=TSNET_HOST, cred_id=b"tsnet-cred-000000001")
    minted = _assert_options(env)
    ids = {c["id"] for c in minted["options"]["allowCredentials"]}
    assert ids == {_b64url(b"local-cred-000000001")}


def test_assert_options_without_passkey_point_at_password(env, root):
    _store_identity(env, root)
    r = env.post("/api/identity/unlock/passkey/options", json={},
                 headers={"host": HOST})
    assert r.status_code == 409
    assert r.json()["fallback"] == "password"


def test_assert_rejects_unknown_challenge(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    minted = _assert_options(env)
    assertion = _make_assertion(key, _b64url(b"not-the-minted-challenge"),
                                rp_id="localhost",
                                origin=minted["origin"])
    r = env.post("/api/identity/unlock/passkey",
                 json={"credential": assertion})
    assert r.status_code == 400
    assert "no pending unlock ceremony" in r.json()["error"]


def test_assert_challenge_is_single_use(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    minted = _assert_options(env)
    assertion = _make_assertion(key, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"])
    assert env.post("/api/identity/unlock/passkey",
                    json={"credential": assertion}).status_code == 200
    env.cookies.clear()
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 400          # consumed — replay is refused


def test_assert_rejects_expired_challenge(env, root, monkeypatch):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    minted = _assert_options(env)
    for pending in unlock_routes._assert_pending.values():
        pending["expires"] = 1.0
    assertion = _make_assertion(key, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"])
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 400


def test_two_browsers_can_complete_concurrent_assert_options(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    first = _assert_options(env)
    _assert_options(env)                 # another browser on the same host
    assertion = _make_assertion(key, first["options"]["challenge"],
                                rp_id=first["rp_id"], origin=first["origin"])
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 200


def test_two_browsers_can_complete_concurrent_password_options(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    first = env.post("/api/identity/unlock/password/options", json={}).json()
    second = env.post("/api/identity/unlock/password/options", json={}).json()
    assert first["challenge"] != second["challenge"]
    sig = _pw_sign(root, first["challenge"], first["origin"])
    r = env.post("/api/identity/unlock/password", json={
        "challenge": first["challenge"], "signature": sig,
    })
    assert r.status_code == 200


def test_pending_fifo_reserves_exact_capacity(env, monkeypatch):
    monkeypatch.setattr(unlock_routes, "_now", lambda: 1000.0)
    store = {
        f"challenge-{index}": {"expires": 2000.0}
        for index in range(unlock_routes.PENDING_MAX)
    }
    unlock_routes._prune(store, reserve=1)
    assert len(store) == unlock_routes.PENDING_MAX - 1
    assert "challenge-0" not in store
    store["newest"] = {"expires": 2000.0}
    assert len(store) == unlock_routes.PENDING_MAX


def test_assert_cannot_complete_cross_host(env, root):
    _store_identity(env, root)
    key_local = _enroll_passkey(env, cred_id=b"local-cred-000000001")
    _enroll_passkey(env, host=TSNET_HOST, cred_id=b"tsnet-cred-000000001")
    env.cookies.clear()
    minted = _assert_options(env)        # minted on localhost
    assertion = _make_assertion(key_local, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"],
                                cred_id=b"local-cred-000000001")
    r = env.post("/api/identity/unlock/passkey",
                 json={"credential": assertion},
                 headers={"host": TSNET_HOST})
    assert r.status_code == 400
    assert "completed from" in r.json()["error"]


def test_assert_rejects_unenrolled_credential(env, root):
    _store_identity(env, root)
    _enroll_passkey(env)
    env.cookies.clear()
    minted = _assert_options(env)
    stranger = ec.generate_private_key(ec.SECP256R1())
    assertion = _make_assertion(stranger, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"],
                                cred_id=b"never-enrolled-cred1")
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 403
    assert "not enrolled" in r.json()["error"]


def test_assert_rejects_wrong_key_signature(env, root):
    _store_identity(env, root)
    _enroll_passkey(env)                 # stored public key
    env.cookies.clear()
    minted = _assert_options(env)
    imposter = ec.generate_private_key(ec.SECP256R1())
    assertion = _make_assertion(imposter, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"])
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 403
    assert "did not verify" in r.json()["error"]


def test_assert_requires_user_verification(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    minted = _assert_options(env)
    assertion = _make_assertion(key, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"],
                                flags=0x01)              # UP only, no UV
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 403


def test_assert_rejects_create_type(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    minted = _assert_options(env)
    assertion = _make_assertion(key, minted["options"]["challenge"],
                                rp_id=minted["rp_id"], origin=minted["origin"],
                                cred_type="webauthn.create")
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code in (400, 403)


def test_assert_rejects_sign_count_regression(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env, sign_count=5)
    env.cookies.clear()
    r = _unlock_with_passkey(env, key, sign_count=3)     # 3 ≤ stored 5
    assert r.status_code == 403
    assert "sign count" in r.json()["error"].lower()


def test_assert_advances_stored_sign_count(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env, sign_count=5)
    env.cookies.clear()
    assert _unlock_with_passkey(env, key, sign_count=6).status_code == 200
    env.cookies.clear()
    # 6 is now the stored count — replaying it is a clone signal.
    assert _unlock_with_passkey(env, key, sign_count=6).status_code == 403
    env.cookies.clear()
    assert _unlock_with_passkey(env, key, sign_count=7).status_code == 200


# ── password unlock (the floor) ───────────────────────────────────────


def test_password_unlock_happy_path_with_zero_passkeys(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    assert env.get("/beads", follow_redirects=False).status_code == 302
    r = _unlock_with_password(env, root)
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "password"
    assert r.json()["display_name"] == "Alex"
    token = r.cookies.get(unlock_routes.SESSION_COOKIE)
    payload = unlock_routes._verified_session_payload(token)
    row = identity_sessions.get_session(payload["sid"], now=unlock_routes._now())
    assert row["credential_id"] is None
    assert env.get("/beads").status_code == 200


def test_verified_password_proof_fails_closed_if_session_cannot_persist(
        env, root, monkeypatch):
    _store_identity(env, root)
    env.cookies.clear()

    def unavailable(**_kwargs):
        raise identity_sessions.SessionStoreError("disk unavailable")

    monkeypatch.setattr(identity_sessions, "create_session", unavailable)
    r = _unlock_with_password(env, root)
    assert r.status_code == 503
    assert "could not create a revocable session" in r.json()["error"]


def test_password_options_require_an_identity(env):
    r = env.post("/api/identity/unlock/password/options", json={})
    assert r.status_code == 409


def test_password_challenge_is_single_use(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    minted = env.post("/api/identity/unlock/password/options", json={}).json()
    sig = _pw_sign(root, minted["challenge"], minted["origin"])
    body = {"challenge": minted["challenge"], "signature": sig}
    assert env.post("/api/identity/unlock/password",
                    json=body).status_code == 200
    env.cookies.clear()
    assert env.post("/api/identity/unlock/password",
                    json=body).status_code == 400


def test_password_challenge_expires(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    minted = env.post("/api/identity/unlock/password/options", json={}).json()
    for pending in unlock_routes._pw_pending.values():
        pending["expires"] = 1.0
    sig = _pw_sign(root, minted["challenge"], minted["origin"])
    r = env.post("/api/identity/unlock/password",
                 json={"challenge": minted["challenge"], "signature": sig})
    assert r.status_code == 400


def test_password_unlock_rejects_wrong_key(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    minted = env.post("/api/identity/unlock/password/options", json={}).json()
    imposter = KeyPair.generate()        # right protocol, wrong root
    sig = _pw_sign(imposter, minted["challenge"], minted["origin"])
    r = env.post("/api/identity/unlock/password",
                 json={"challenge": minted["challenge"], "signature": sig})
    assert r.status_code == 403
    assert "does not verify" in r.json()["error"]


def test_password_unlock_rejects_unknown_challenge(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    sig = _pw_sign(root, "ab" * 32, f"https://{HOST}")
    r = env.post("/api/identity/unlock/password",
                 json={"challenge": "ab" * 32, "signature": sig})
    assert r.status_code == 400


def test_password_challenge_is_host_bound(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    minted = env.post("/api/identity/unlock/password/options", json={},
                      headers={"host": HOST}).json()
    sig = _pw_sign(root, minted["challenge"], minted["origin"])
    r = env.post("/api/identity/unlock/password",
                 json={"challenge": minted["challenge"], "signature": sig},
                 headers={"host": TSNET_HOST})
    assert r.status_code == 400
    assert "completed from" in r.json()["error"]


def test_password_signature_is_domain_separated(env, root):
    """A signature over the bare challenge (no domain, no canonical
    envelope) must not verify — cross-protocol replay is structurally
    impossible."""
    _store_identity(env, root)
    env.cookies.clear()
    minted = env.post("/api/identity/unlock/password/options", json={}).json()
    bare = root.sign_hex(minted["challenge"].encode())
    r = env.post("/api/identity/unlock/password",
                 json={"challenge": minted["challenge"], "signature": bare})
    assert r.status_code == 403


# ── session chrome + lock ─────────────────────────────────────────────


def test_session_endpoint_reflects_state(env, root):
    r = env.get("/api/identity/session").json()
    assert r == {"enforced": False, "unlocked": False, "method": None,
                 "expires_at": None, "gate_disabled": False}
    _store_identity(env, root)
    r = env.get("/api/identity/session").json()
    assert r["enforced"] is True
    assert r["unlocked"] is True         # the bootstrap session
    assert r["method"] == "bootstrap"


def test_lock_drops_the_session(env, root):
    created = _store_identity(env, root)
    token = created.cookies.get(unlock_routes.SESSION_COOKIE)
    payload = unlock_routes._verified_session_payload(token)
    other = unlock_routes.mint_session_token("password")
    other_payload = unlock_routes._verified_session_payload(other)
    assert env.get("/beads").status_code == 200
    assert env.post("/api/identity/lock").json() == {"ok": True}
    locked = identity_sessions.get_session(payload["sid"], now=unlock_routes._now())
    assert locked["status"] == "ended"
    assert locked["end_reason"] == "locked"
    assert identity_sessions.get_session(
        other_payload["sid"], now=unlock_routes._now()
    )["status"] == "active"
    assert unlock_routes.verify_session_token(token) is None
    assert unlock_routes.verify_session_token(other) is not None
    r = env.get("/beads", follow_redirects=False)
    assert r.status_code == 302


def test_server_revoke_refuses_a_still_valid_cookie(env, root):
    created = _store_identity(env, root)
    token = created.cookies.get(unlock_routes.SESSION_COOKIE)
    payload = unlock_routes._verified_session_payload(token)
    assert payload["exp"] > unlock_routes._now()
    assert identity_sessions.revoke_session(
        payload["sid"], now=unlock_routes._now(),
    ) is True
    assert env.get("/beads", follow_redirects=False).status_code == 302


def test_session_cookie_is_always_secure(env, root):
    created = _store_identity(env, root)
    assert "secure" in created.headers["set-cookie"].lower()


def test_garbage_cookie_does_not_pass(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    env.cookies.set(unlock_routes.SESSION_COOKIE, "forged.token")
    r = env.get("/beads", follow_redirects=False)
    assert r.status_code == 302
