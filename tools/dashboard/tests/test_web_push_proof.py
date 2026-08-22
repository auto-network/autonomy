"""Structural Web Push proof: secure root worker and one Apple-only send."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, unlock_routes, web_push_proof


DASHBOARD = Path(__file__).resolve().parents[1]


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _subscription(host: str = "web.push.apple.com") -> dict:
    receiver = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962,
        PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://{host}/Q123456789",
        "expirationTime": None,
        "keys": {
            "p256dh": _b64url(receiver),
            "auth": _b64url(b"0123456789abcdef"),
        },
    }


def _client() -> TestClient:
    app = Starlette(routes=[
        Route("/service-worker.js", web_push_proof.service_worker),
        Route("/api/web-push/proof/config", web_push_proof.api_config),
        Route("/api/web-push/proof/send", web_push_proof.api_send,
              methods=["POST"]),
    ])
    return TestClient(app, base_url="https://testserver")


def test_root_worker_is_push_only_visible_and_never_cached():
    response = _client().get("/service-worker.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == (
        "no-cache, no-store, must-revalidate"
    )
    assert response.headers["service-worker-allowed"] == "/"
    assert response.headers["content-type"].startswith("application/javascript")
    script = response.text
    assert "addEventListener('push'" in script
    assert "showNotification" in script
    assert "addEventListener('notificationclick'" in script
    assert "addEventListener('fetch'" not in script
    assert "addEventListener('notificationclose'" not in script


def test_worker_remains_fetchable_while_human_gate_is_locked():
    assert unlock_routes._path_is_gated("/service-worker.js") is False
    assert unlock_routes._path_is_gated("/web-push-proof") is True


def test_manifests_have_stable_distinct_install_ids():
    main = json.loads((DASHBOARD / "static" / "manifest.json").read_text())
    proof = json.loads(
        (DASHBOARD / "static" / "web-push-proof-manifest.json").read_text()
    )
    assert main["id"] == "/"
    assert main["start_url"] == "/"
    assert proof["id"] == "/web-push-proof"
    assert proof["start_url"] == "/web-push-proof"
    assert proof["display"] == "standalone"


def test_proof_page_preserves_direct_gesture_and_explains_non_durability():
    page = (DASHBOARD / "templates" / "web-push-proof.html").read_text()
    handler = page[page.index("byId('send').addEventListener"):]
    assert handler.index("Notification.requestPermission()") < handler.index(
        "fetch('/api/web-push/proof/config'"
    )
    assert "stores nothing" in page
    assert "Add to Home Screen" in page


def test_config_returns_uncompressed_vapid_public_key(monkeypatch):
    class FakeVapid:
        public_key = ec.generate_private_key(ec.SECP256R1()).public_key()

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda _r: None)
    monkeypatch.setattr(web_push_proof, "_load_vapid", lambda: FakeVapid())
    response = _client().get("/api/web-push/proof/config")
    assert response.status_code == 200
    body = response.json()
    assert body["durable"] is False
    assert body["apple_only"] is True
    decoded = base64.urlsafe_b64decode(
        body["application_server_key"] + "=" *
        (-len(body["application_server_key"]) % 4)
    )
    assert len(decoded) == 65 and decoded[0] == 0x04


def test_same_origin_apple_subscription_sends_once(monkeypatch):
    seen = []
    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda _r: None)
    monkeypatch.setattr(
        web_push_proof,
        "_send_push",
        lambda subscription, *, contact: seen.append((subscription, contact)) or 201,
    )
    response = _client().post(
        "/api/web-push/proof/send",
        headers={"Origin": "https://testserver", "Content-Type": "application/json"},
        json={"subscription": _subscription()},
    )
    assert response.status_code == 200
    assert response.json()["push_service_status"] == 201
    assert len(seen) == 1
    assert seen[0][0]["endpoint"].startswith("https://web.push.apple.com/")
    assert seen[0][1] == "mailto:webpush-proof@autonomy.invalid"


def test_send_refuses_cross_origin_and_non_apple_endpoints(monkeypatch):
    called = []
    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda _r: None)
    monkeypatch.setattr(
        web_push_proof,
        "_send_push",
        lambda *_args, **_kwargs: called.append(True) or 201,
    )
    cross_origin = _client().post(
        "/api/web-push/proof/send",
        headers={"Origin": "https://attacker.example"},
        json={"subscription": _subscription()},
    )
    assert cross_origin.status_code == 403

    non_apple = _client().post(
        "/api/web-push/proof/send",
        headers={"Origin": "https://testserver"},
        json={"subscription": _subscription("push.example.com")},
    )
    assert non_apple.status_code == 422
    assert "Apple" in non_apple.json()["error"]
    assert called == []
