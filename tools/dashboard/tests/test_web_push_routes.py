"""Authenticated Web Push device API and secret-redaction contract."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from tools.dashboard import api_auth, web_push, web_push_routes
from tools.dashboard import identity_routes
from tools.dashboard.identity_routes import StablePersonalIdentityUnavailable


COOKIE = "dashboard_session"
ROOT = "1" * 64


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _subscription(key_id: str, token: str = "one") -> dict:
    receiver = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://web.push.apple.com/Q/{token}",
        "expiration_time": None,
        "keys": {
            "p256dh": _b64url(receiver),
            "auth": _b64url(b"0123456789abcdef"),
        },
        "vapid_key_id": key_id,
    }


def _authenticate_bearer(request):
    identities = {
        "Bearer org": ("agent-session", "autonomy"),
        "Bearer local": ("host-session", None),
    }
    identity = identities.get(request.headers.get("authorization", ""))
    return (identity, None) if identity is not None else (None, object())


def _verify_cookie(value):
    if value == "valid":
        return {"sid": "transient-cookie-sid", "method": "passkey"}
    return None


def _app() -> Starlette:
    return Starlette(
        routes=[*web_push_routes.ROUTES, *web_push.ROUTES],
        middleware=[Middleware(
            api_auth.ApiIdentityMiddleware,
            authenticate_bearer=_authenticate_bearer,
            verify_cookie=_verify_cookie,
            cookie_name=COOKIE,
        )],
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "web-push.db"
    keys = tmp_path / "web-push-keys"
    monkeypatch.setattr(web_push_routes, "DB_PATH", db)
    monkeypatch.setattr(web_push_routes, "KEY_DIR", keys)
    monkeypatch.setattr(web_push_routes, "resolve_stable_personal_root_public_key", lambda: ROOT)
    monkeypatch.setattr(web_push, "DB_PATH", db)
    monkeypatch.setattr(web_push, "VAPID_DIR", keys)
    web_push._vapid.clear()
    with TestClient(_app(), base_url="https://dashboard.test") as test_client:
        test_client.cookies.set(COOKIE, "valid")
        yield test_client, db


def _config(client: TestClient) -> dict:
    response = client.get("/api/web-push/config")
    assert response.status_code == 200, response.text
    return response.json()


def _enroll(client: TestClient, *, token: str = "one", extra: dict | None = None):
    config = _config(client)
    body = {
        "subscription": _subscription(config["vapid"]["key_id"], token),
        "device_label": "Jeremy's iPhone",
        "platform_family": "ios",
        "browser_family": "safari",
        "max_detail": "generic",
    }
    if extra:
        body.update(extra)
    return client.put(
        "/api/web-push/devices/device_1234567890",
        headers={"Origin": "https://dashboard.test"},
        json=body,
    )


class TestWebPushSubscriptionRoutes:
    def test_real_auth_middleware_allows_only_operator_cookie(self, client):
        operator, _db = client
        assert _config(operator)["ready"] is True
        operator.cookies.clear()
        assert operator.get("/api/web-push/config").status_code == 401
        assert operator.get(
            "/api/web-push/config", headers={"Authorization": "Bearer org"},
        ).status_code == 403
        assert operator.get(
            "/api/web-push/config", headers={"Authorization": "Bearer local"},
        ).status_code == 403

    def test_config_carries_keys_and_devices_but_no_per_app_preferences(self, client):
        browser, _db = client
        config = _config(browser)
        assert config["application_server_key"] == config["vapid"]["public_key"]
        assert config["vapid_key_id"] == config["vapid"]["key_id"]
        assert "applications" not in config
        assert "preferences" not in config
        assert "operator_subject" not in json.dumps(config)

    def test_retired_v2_routes_are_gone(self, client):
        browser, _db = client
        assert browser.get("/api/web-push/diagnostics").status_code == 404
        assert browser.patch(
            "/api/web-push/preferences/fleet",
            headers={"Origin": "https://dashboard.test"},
            json={"mode": "generic"},
        ).status_code in (404, 405)

    def test_enrollment_derives_owner_and_never_persists_cookie_or_body_identity(self, client):
        browser, db = client
        response = _enroll(browser)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "active"
        assert result["token_version"] == 1
        assert "device_update_token" in result
        connection = sqlite3.connect(db)
        try:
            row = connection.execute(
                "SELECT operator_subject,device_id,serving_machine_id FROM web_push_subscriptions"
            ).fetchone()
        finally:
            connection.close()
        expected = hashlib.sha256(
            b"autonomy:web-push-owner:v1\0" + bytes.fromhex(ROOT)
        ).hexdigest()
        assert row == (expected, "device_1234567890", None)
        assert "transient-cookie-sid" not in db.read_bytes().decode("latin1")

        forged = _enroll(browser, extra={"owner": "attacker"})
        assert forged.status_code == 422

    def test_cookie_mutations_require_exact_json_same_origin(self, client):
        browser, _db = client
        config = _config(browser)
        body = {"subscription": _subscription(config["vapid"]["key_id"])}
        assert browser.put(
            "/api/web-push/devices/device_1234567890", json=body,
        ).status_code == 422
        assert browser.put(
            "/api/web-push/devices/device_1234567890",
            headers={"Origin": "https://evil.test"}, json=body,
        ).status_code == 422
        assert browser.put(
            "/api/web-push/devices/device_1234567890",
            headers={
                "Origin": "https://dashboard.test",
                "Content-Type": "text/plain",
            },
            content=json.dumps(body),
        ).status_code == 415

    def test_subscription_crypto_and_expiration_are_bounded(self, client):
        browser, _db = client
        key_id = _config(browser)["vapid"]["key_id"]
        invalid_point = _subscription(key_id)
        invalid_point["keys"]["p256dh"] = _b64url(b"\x04" + b"\x00" * 64)
        assert browser.put(
            "/api/web-push/devices/device_1234567890",
            headers={"Origin": "https://dashboard.test"},
            json={"subscription": invalid_point},
        ).status_code == 422

        invalid_expiry = _subscription(key_id)
        invalid_expiry["expiration_time"] = False
        assert browser.put(
            "/api/web-push/devices/device_1234567890",
            headers={"Origin": "https://dashboard.test"},
            json={"subscription": invalid_expiry},
        ).status_code == 422

        overflow_expiry = _subscription(key_id)
        overflow_expiry["expiration_time"] = 10**1000
        assert browser.put(
            "/api/web-push/devices/device_1234567890",
            headers={"Origin": "https://dashboard.test"},
            json={"subscription": overflow_expiry},
        ).status_code == 422

    def test_device_list_and_config_redact_every_transport_secret(self, client):
        browser, _db = client
        enrolled = _enroll(browser)
        token = enrolled.json()["device_update_token"]
        response = browser.get("/api/web-push/devices")
        assert response.status_code == 200
        wire = response.text
        for forbidden in (
            "web.push.apple.com", "p256dh", "auth_secret", token,
            "device_update_token_hash", ROOT,
        ):
            assert forbidden not in wire
        assert response.json()["devices"][0]["device_label"] == "Jeremy's iPhone"

    def test_background_refresh_rotates_token_and_replay_loses(self, client):
        browser, _db = client
        enrolled = _enroll(browser).json()
        old_endpoint = _subscription(_config(browser)["vapid"]["key_id"], "one")["endpoint"]
        key_id = _config(browser)["vapid"]["key_id"]
        body = {
            "old_endpoint_hash": hashlib.sha256(old_endpoint.encode()).hexdigest(),
            "token_version": enrolled["token_version"],
            "subscription": _subscription(key_id, "two"),
        }
        browser.cookies.clear()
        first = browser.post(
            "/api/web-push/devices/device_1234567890/refresh",
            headers={"Authorization": f"WebPushDevice {enrolled['device_update_token']}"},
            json=body,
        )
        assert first.status_code == 200, first.text
        assert first.json()["token_version"] == 2
        assert "access-control-allow-origin" not in first.headers
        replay = browser.post(
            "/api/web-push/devices/device_1234567890/refresh",
            headers={"Authorization": f"WebPushDevice {enrolled['device_update_token']}"},
            json=body,
        )
        assert replay.status_code == 404
        unknown = browser.post(
            "/api/web-push/devices/device_abcdefghij/refresh",
            headers={"Authorization": "WebPushDevice " + "x" * 43},
            json=body,
        )
        assert unknown.status_code == 404
        assert replay.json() == unknown.json()

    def test_explicit_forget_is_scoped_and_unknown_is_404(self, client):
        browser, _db = client
        assert _enroll(browser).status_code == 200
        retired = browser.delete(
            "/api/web-push/devices/device_1234567890",
            headers={"Origin": "https://dashboard.test"},
        )
        assert retired.status_code == 200
        assert browser.delete(
            "/api/web-push/devices/device_1234567890",
            headers={"Origin": "https://dashboard.test"},
        ).status_code == 404

    def test_test_alert_route_has_the_device_routes_auth(self, client):
        browser, _db = client
        assert _enroll(browser).status_code == 200
        origin = {"Origin": "https://dashboard.test"}
        queued = browser.post("/api/web-push/test", headers=origin)
        assert queued.status_code == 200, queued.text
        assert queued.json()["queued_installations"] == 1
        # Same-origin is proved by the Origin header, not assumed without one.
        assert browser.post("/api/web-push/test").status_code == 422
        assert browser.post(
            "/api/web-push/test", headers={"Origin": "https://evil.test"},
        ).status_code == 422
        browser.cookies.clear()
        assert browser.post("/api/web-push/test", headers=origin).status_code == 401
        for bearer in ("Bearer org", "Bearer local"):
            assert browser.post(
                "/api/web-push/test", headers={**origin, "Authorization": bearer},
            ).status_code == 403

    def test_ack_route_has_the_device_routes_auth_and_owner(self, client):
        browser, db = client
        assert _enroll(browser).status_code == 200
        # Acks and sends bind to the same subject the device routes enroll.
        assert web_push._stable_owner_id() == web_push_routes.stable_operator_subject()
        assert web_push.register_approval_pending_sync("appr_1", "commit_sign") == 1
        url = "/api/web-push/attention/appr_1/ack"
        body = {"event_version": 1, "applied": True}
        origin = {"Origin": "https://dashboard.test"}
        assert browser.post(url, json=body).status_code == 422
        browser.cookies.clear()
        assert browser.post(url, json=body, headers=origin).status_code == 401
        for bearer in ("Bearer org", "Bearer local"):
            assert browser.post(
                url, json=body, headers={**origin, "Authorization": bearer},
            ).status_code == 403

        def acknowledged():
            connection = sqlite3.connect(db)
            try:
                return connection.execute(
                    "SELECT acknowledged_at FROM web_push_attention_events"
                ).fetchone()[0]
            finally:
                connection.close()

        assert acknowledged() is None
        browser.cookies.set(COOKIE, "valid")
        response = browser.post(url, json=body, headers=origin)
        assert response.status_code == 200, response.text
        assert response.json()["acknowledged"] is True
        assert acknowledged() is not None

    def test_missing_personal_identity_is_bounded_409(self, client, monkeypatch):
        browser, _db = client
        monkeypatch.setattr(
            web_push_routes,
            "resolve_stable_personal_root_public_key",
            lambda: (_ for _ in ()).throw(StablePersonalIdentityUnavailable()),
        )
        response = browser.get("/api/web-push/config")
        assert response.status_code == 409
        assert response.json() == {
            "ok": False, "error": "personal_identity_unavailable",
        }


def test_stable_personal_root_resolution_refuses_ambiguity_and_anchor_mismatch(monkeypatch):
    class Result:
        dropped = SimpleNamespace(values=lambda: (0,))

        def __init__(self, rows):
            self.rows = rows

        def to_dict(self):
            return self.rows

    default = SimpleNamespace(payload={
        "root_pub": "a" * 64,
        "armored_private_key": "armor",
    })
    monkeypatch.setattr(
        identity_routes.settings_ops, "read_set", lambda *args, **kwargs: Result({"default": default}),
    )
    import tools.network.idkit.armor as armor_module
    monkeypatch.setattr(armor_module, "armor_root_pub", lambda _armor: "a" * 64)
    assert identity_routes.resolve_stable_personal_root_public_key() == "a" * 64

    monkeypatch.setattr(
        identity_routes.settings_ops,
        "read_set",
        lambda *args, **kwargs: Result({"default": default, "shadow": default}),
    )
    with pytest.raises(StablePersonalIdentityUnavailable, match="exactly one"):
        identity_routes.resolve_stable_personal_root_public_key()

    monkeypatch.setattr(
        identity_routes.settings_ops, "read_set", lambda *args, **kwargs: Result({"default": default}),
    )
    monkeypatch.setattr(armor_module, "armor_root_pub", lambda _armor: "b" * 64)
    with pytest.raises(StablePersonalIdentityUnavailable, match="disagree"):
        identity_routes.resolve_stable_personal_root_public_key()
