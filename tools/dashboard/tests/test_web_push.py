"""Production Web Push transport: identity binding, latch, and durable send."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import web_push
from tools.dashboard.dao import approval_requests


DASHBOARD = Path(__file__).resolve().parents[1]


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _subscription(host: str = "web.push.apple.com", token: str = "one") -> dict:
    receiver = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://{host}/Q/{token}",
        "expirationTime": None,
        "keys": {
            "p256dh": _b64url(receiver),
            "auth": _b64url(b"0123456789abcdef"),
        },
    }


@pytest.fixture
def transport(tmp_path, monkeypatch):
    db_path = tmp_path / "web-push.db"
    monkeypatch.setattr(web_push, "DB_PATH", db_path)
    monkeypatch.setattr(web_push, "VAPID_PATH", tmp_path / "web-push-vapid.pem")
    monkeypatch.setattr(web_push, "_stable_owner_id", lambda: "owner-one")
    monkeypatch.setattr(web_push, "_operator_only", lambda _request: None)
    monkeypatch.setattr(approval_requests, "DB_PATH", tmp_path / "approvals.db")
    web_push.init_db()
    yield db_path


def _client() -> TestClient:
    return TestClient(Starlette(routes=web_push.ROUTES), base_url="https://dashboard.test")


def _enroll(*, owner="owner-one", installation="install_1234567890", token="one"):
    subscription = web_push._validate_subscription(_subscription(token=token))
    web_push._upsert_subscription(
        owner_id=owner,
        origin="https://dashboard.test",
        installation_id=installation,
        subscription=subscription,
    )


def test_endpoint_validation_accepts_browser_services_and_refuses_ssrf():
    for host in (
        "web.push.apple.com",
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
    ):
        assert web_push._validate_subscription(_subscription(host))["host"] == host
    with pytest.raises(ValueError, match="allowed browser push service"):
        web_push._validate_subscription(_subscription("127.0.0.1"))
    with pytest.raises(ValueError, match="allowed browser push service"):
        web_push._validate_subscription(_subscription("metadata.internal"))


def test_enrollment_binds_to_server_owner_and_state(transport):
    response = _client().post(
        "/api/web-push/subscriptions",
        headers={"Origin": "https://dashboard.test"},
        json={
            "installation_id": "install_1234567890",
            "subscription": _subscription(),
        },
    )
    assert response.status_code == 200
    state = _client().get(
        "/api/web-push/state?installation_id=install_1234567890"
    ).json()
    assert state["active_installations"] == 1
    assert state["this_installation"]["status"] == "active"

    connection = sqlite3.connect(transport)
    try:
        row = connection.execute(
            "SELECT owner_id,origin FROM web_push_subscriptions"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("owner-one", "https://dashboard.test")


def test_enrollment_refuses_body_identity_and_cross_owner_endpoint(transport):
    bad = _client().post(
        "/api/web-push/subscriptions",
        headers={"Origin": "https://dashboard.test"},
        json={
            "installation_id": "install_1234567890",
            "subscription": _subscription(),
            "owner_id": "attacker",
        },
    )
    assert bad.status_code == 422

    _enroll(owner="owner-one")
    with pytest.raises(PermissionError, match="another operator"):
        web_push._upsert_subscription(
            owner_id="owner-two",
            origin="https://dashboard.test",
            installation_id="install_abcdefghij",
            subscription=web_push._validate_subscription(_subscription()),
        )


def test_foreground_applied_ack_cancels_every_unsent_device(transport):
    _enroll(installation="install_1234567890", token="one")
    _enroll(installation="install_abcdefghij", token="two")
    count = web_push._register_attention(
        event_id="approval:abc123", event_version=1,
        application="approvals", attention_class="approval_pending",
        route="/activity?focus=approval&id=abc123",
        coalesce_key="approval:abc123", delivery_class="normal",
        budget_class="operator_approval", grace_seconds=20,
    )
    assert count == 2
    assert web_push._ack_attention("approval:abc123", 1, "owner-one") is True

    connection = sqlite3.connect(transport)
    try:
        states = connection.execute(
            "SELECT state,last_reason FROM web_push_outbox ORDER BY id"
        ).fetchall()
        acknowledged = connection.execute(
            "SELECT acknowledged_at FROM web_push_attention_events"
        ).fetchone()[0]
    finally:
        connection.close()
    assert states == [
        ("canceled", "foreground_applied"),
        ("canceled", "foreground_applied"),
    ]
    assert acknowledged is not None


def test_push_service_gone_retires_subscription_and_outbox(transport):
    _enroll()
    web_push._register_attention(
        event_id="approval:gone", event_version=1,
        application="approvals", attention_class="approval_pending",
        route="/activity?focus=approval&id=gone", coalesce_key="approval:gone",
        delivery_class="normal", budget_class="operator_approval", grace_seconds=0,
    )
    row = web_push._claim_due()
    assert row is not None and row["state"] == "leased"
    web_push._finish_attempt(row, status=410, reason="Unregistered")

    connection = sqlite3.connect(transport)
    try:
        subscription = connection.execute(
            "SELECT status,retire_reason FROM web_push_subscriptions"
        ).fetchone()
        outbox = connection.execute(
            "SELECT state,last_reason FROM web_push_outbox"
        ).fetchone()
    finally:
        connection.close()
    assert subscription == ("retired", "push_service_gone")
    assert outbox[0] == "canceled"


def test_final_guard_refuses_a_claim_canceled_after_lease(transport):
    _enroll()
    web_push._register_attention(
        event_id="approval:raced", event_version=1,
        application="approvals", attention_class="approval_pending",
        route="/activity?focus=approval&id=raced", coalesce_key="approval:raced",
        delivery_class="normal", budget_class="operator_approval", grace_seconds=0,
    )
    row = web_push._claim_due()
    assert row is not None
    web_push._cancel_attention("approval:raced", 1, "approval_decided")
    assert web_push._lease_still_sendable(row) is False


def test_startup_reconciliation_fills_enqueue_gap_and_cancels_decided(transport):
    _enroll()
    approval_id = approval_requests.create(
        kind="commit_sign", session="auto-test", request={"value": 1},
        created_at=web_push.time.time(),
    )
    web_push._reconcile_approval_attention_sync(lambda kind: kind == "commit_sign")
    connection = sqlite3.connect(transport)
    try:
        assert connection.execute(
            "SELECT count(*) FROM web_push_outbox WHERE event_id=?",
            (f"approval:{approval_id}",),
        ).fetchone()[0] == 1
    finally:
        connection.close()

    assert approval_requests.set_result(approval_id, {"approved": False}) is True
    web_push._reconcile_approval_attention_sync(lambda kind: kind == "commit_sign")
    connection = sqlite3.connect(transport)
    try:
        state = connection.execute(
            "SELECT state FROM web_push_outbox WHERE event_id=?",
            (f"approval:{approval_id}",),
        ).fetchone()[0]
    finally:
        connection.close()
    assert state == "canceled"


def test_diagnostic_budget_is_one_event_not_one_attempt(transport):
    _enroll()
    for number in range(4):
        web_push._register_attention(
            event_id=f"diagnostic:{number}", event_version=1,
            application="dashboard", attention_class="device_alert_test",
            route="/activity", coalesce_key="device-alert-test",
            delivery_class="normal", budget_class="operator_diagnostic",
            grace_seconds=0,
        )
    for _ in range(3):
        row = web_push._claim_due()
        assert row is not None
        web_push._finish_attempt(row, status=201, reason=None)
    assert web_push._claim_due() is None

    connection = sqlite3.connect(transport)
    try:
        waiting = connection.execute(
            "SELECT state,last_reason FROM web_push_outbox "
            "WHERE event_id='diagnostic:3'"
        ).fetchone()
    finally:
        connection.close()
    assert waiting == ("retry_wait", "budget_wait")


def test_test_api_queues_fixed_diagnostic_not_caller_content(transport):
    _enroll()
    response = _client().post(
        "/api/web-push/test",
        headers={"Origin": "https://dashboard.test", "Content-Type": "application/json"},
        content="{}",
    )
    assert response.status_code == 200
    assert response.json()["queued_installations"] == 1
    refused = _client().post(
        "/api/web-push/test",
        headers={"Origin": "https://dashboard.test", "Content-Type": "application/json"},
        content=json.dumps({"title": "caller chose this"}),
    )
    assert refused.status_code == 422


def test_worker_is_generic_visible_and_click_route_is_bounded():
    script = (DASHBOARD / "static" / "service-worker.js").read_text()
    assert "showNotification" in script
    assert "Autonomy needs your attention" in script
    assert "notificationclose" in script
    assert "addEventListener('notificationclose'" not in script
    assert "url.pathname !== '/activity'" in script
    assert "addEventListener('fetch'" not in script


def test_main_activity_exposes_direct_gesture_controls_and_render_ack():
    activity = (DASHBOARD / "templates" / "pages" / "timeline.html").read_text()
    controller = (DASHBOARD / "static" / "js" / "web-push-register.js").read_text()
    overlay = (DASHBOARD / "static" / "js" / "pages" / "worktrees.js").read_text()
    assert "device-alerts-enable" in activity
    assert "Enable and send test" in activity
    assert "Notification.requestPermission()" in controller
    assert "VapidPkHashMismatch" in controller
    assert "subscription.options" in controller
    assert controller.index("Notification.requestPermission()") < controller.index(
        "pushManager.subscribe"
    )
    assert "document.visibilityState === 'visible'" in overlay
    assert "acknowledgeApproval(r.id)" in overlay
    activity_controller = (DASHBOARD / "static" / "js" / "pages" / "activity.js").read_text()
    assert "params.get('focus') !== 'approval'" in activity_controller
    assert "window.openApprovalOverlay(id)" in activity_controller
