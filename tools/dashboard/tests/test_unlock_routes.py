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
  challenge single-use + TTL + superseded-by-new-options, completion
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
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
    with TestClient(_build_app(), base_url=f"https://{HOST}") as client:
        yield client
    identity_routes._pending.clear()
    unlock_routes._assert_pending.clear()
    unlock_routes._pw_pending.clear()
    unlock_routes._secret_cache.update({"path": None, "value": None})
    unlock_routes._enforce_cache.update({"at": 0.0, "value": None})
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
    """The gate must resolve enrollment against the dashboard's OWN org,
    never the request's X-Graph-Org. If it honoured the header, an
    attacker could name an un-enrolled org and fail the lock open (and
    poison the shared cache to False for everyone). Verify the caller-org
    contextvar is cleared before the enrollment lookup runs."""
    from tools.graph import ops
    _store_identity(env, root)
    unlock_routes.bust_enforce_cache()

    seen = {}
    real = unlock_routes._personal_member

    def spy(org):
        seen["ctx"] = ops._caller_org_var.get()
        return real(org)

    monkeypatch.setattr(unlock_routes, "_personal_member", spy)
    # Simulate _CallerOrgMiddleware having bound an attacker-chosen org.
    token = ops.set_caller_org("attacker-bogus-org")
    try:
        result = unlock_routes.human_auth_enrolled()
    finally:
        ops.reset_caller_org(token)
    assert result is True                # still sees the real enrollment
    assert seen["ctx"] is None           # lookup ran with the header cleared


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
    member = identity_routes._personal_member(settings_ops.CALLER_ORG)
    assert member.key == "default"
    assert member.payload["root_pub"] == root.public_hex
    env.cookies.clear()
    assert _unlock_with_password(env, attacker).status_code == 403
    env.cookies.clear()
    assert _unlock_with_password(env, root).status_code == 200


def test_every_mutation_path_refuses_protected_identity_sets(env, root):
    """add / upsert / override / exclude / promote / deprecate / delete /
    remove-by-prefix / migrate must ALL refuse the protected sets without
    the capability. Create the target rows WITH the capability first, so
    the id-based paths have something to aim at."""
    _store_identity(env, root)
    with _sops.identity_write_context():
        pk_id = _sops.add_setting(_PASSKEY_SET, 1, "victim-cred",
                                 _valid_passkey_payload("victim-cred"), org=ORG)
        personal = identity_routes._personal_member(settings_ops.CALLER_ORG)
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


def test_new_assert_options_supersede_prior(env, root):
    _store_identity(env, root)
    key = _enroll_passkey(env)
    env.cookies.clear()
    first = _assert_options(env)
    _assert_options(env)                 # supersedes the first ceremony
    assertion = _make_assertion(key, first["options"]["challenge"],
                                rp_id=first["rp_id"], origin=first["origin"])
    r = env.post("/api/identity/unlock/passkey", json={"credential": assertion})
    assert r.status_code == 400


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
    assert env.get("/beads").status_code == 200


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
    _store_identity(env, root)
    assert env.get("/beads").status_code == 200
    assert env.post("/api/identity/lock").json() == {"ok": True}
    r = env.get("/beads", follow_redirects=False)
    assert r.status_code == 302


def test_garbage_cookie_does_not_pass(env, root):
    _store_identity(env, root)
    env.cookies.clear()
    env.cookies.set(unlock_routes.SESSION_COOKIE, "forged.token")
    r = env.get("/beads", follow_redirects=False)
    assert r.status_code == 302
