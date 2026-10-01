"""Production Web Push transport: identity binding, latch, and durable send."""

from __future__ import annotations

import base64
import json
import hashlib
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import web_push
from tools.dashboard import web_push_routes
from tools.dashboard import web_push_sender
from tools.dashboard.dao import web_push as web_push_dao
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
    monkeypatch.setattr(web_push, "VAPID_DIR", tmp_path / "web-push-keys")
    web_push._vapid.clear()
    monkeypatch.setattr(web_push, "_stable_owner_id", lambda: "a" * 64)
    monkeypatch.setattr(web_push_routes, "_operator_cookie_only", lambda _request: None)
    monkeypatch.setattr(approval_requests, "DB_PATH", tmp_path / "approvals.db")
    web_push.init_db()
    yield db_path


def _client() -> TestClient:
    return TestClient(Starlette(routes=web_push.ROUTES), base_url="https://dashboard.test")


def _enroll(*, owner="a" * 64, installation="install_1234567890", token="one"):
    """Enroll a device the way the live device route does: through the store."""
    subscription = web_push._validate_subscription(_subscription(token=token))
    store = web_push_dao.WebPushStore(web_push.DB_PATH)
    key = web_push_dao.VapidKeyCustody(store, key_dir=web_push.VAPID_DIR).ensure_active()
    store.enroll(
        operator_subject=owner, device_id=installation,
        endpoint=subscription["endpoint"],
        endpoint_hash=hashlib.sha256(subscription["endpoint"].encode()).hexdigest(),
        endpoint_origin="https://web.push.apple.com", vapid_subject="https://dashboard.test",
        p256dh=subscription["keys"]["p256dh"], auth_secret=subscription["keys"]["auth"],
        vapid_key_id=key.key_id, expiration_time=subscription.get("expiration_time"),
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


def test_worker_sender_uses_claimed_subscription_key_and_bounded_headers(monkeypatch):
    captured = {}
    immutable_key = object()
    monkeypatch.setattr(web_push, "_load_vapid", lambda key_id: (
        captured.setdefault("key_id", key_id), immutable_key,
    )[1])

    def send(**kwargs):
        captured.update(kwargs)
        return web_push_sender.SendResult(201, "accepted", False, False, None)

    monkeypatch.setattr(web_push_sender, "send_encrypted_web_push", send)
    monkeypatch.setattr(web_push.time, "time", lambda: 1_000.0)
    status = web_push._send_push({
        "event_id": "approval:opaque",
        "event_version": 7,
        "endpoint": "https://web.push.apple.com/Q/opaque",
        "p256dh": "receiver-key",
        "auth_secret": "auth-secret",
        "vapid_key_id": "a" * 32,
        "origin": "https://dashboard.example",
        "expires_at": 91_000.0,
        "created_at": 900.0,
        "attention_class": "approval_pending",
        "route": "/activity?focus=approval&id=opaque",
    })
    assert status == 201
    assert captured["key_id"] == "a" * 32
    assert captured["vapid_key"] is immutable_key
    assert captured["endpoint"] == "https://web.push.apple.com/Q/opaque"
    assert captured["vapid_subject"] == "https://dashboard.example"
    assert captured["ttl"] == 86400
    assert captured["urgency"] == "normal"
    assert len(captured["topic"]) == 32
    assert json.loads(captured["payload"])["class"] == "approval_pending"


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
    assert web_push._ack_attention("approval:abc123", 1, "a" * 64) is True

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
    assert "const NOTIFICATION_ROUTE = '/activity';" in script
    assert "REGISTERED_CLASSES" not in script
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
    # A push opens /activity?focus=approval&id=..., which the Central inbox on
    # every page opens in the shared dialog and acknowledges.
    central = (DASHBOARD / "static" / "js" / "components" / "central-attention.js").read_text()
    assert "focus === 'approval' && id" in central
    assert "acknowledgeApproval?.(item.id)" in central


def test_worker_sleeps_until_the_grace_deadline_not_a_fixed_poll(transport):
    assert web_push._next_due_delay() is None  # nothing queued: no timer
    _enroll()
    web_push._register_attention(
        event_id="approval:due", event_version=1,
        application="approvals", attention_class="approval_pending",
        route="/activity", coalesce_key="approval:due",
        delivery_class="normal", budget_class="operator_approval",
        grace_seconds=20,
    )
    delay = web_push._next_due_delay()
    assert delay is not None and 19 < delay <= 20
    web_push._ack_attention("approval:due", 1, "a" * 64)
    assert web_push._next_due_delay() is None  # acknowledged rows never wake it


def _run_worker(monkeypatch, delays, on_wait):
    """Drive _worker_loop with no sendable rows, recording each wait timeout."""
    import asyncio

    waits: list[float] = []
    remaining = iter(delays)

    async def scenario():
        monkeypatch.setattr(web_push, "_claim_due", lambda: None)
        monkeypatch.setattr(web_push, "_cleanup", lambda: None)
        monkeypatch.setattr(web_push, "_next_due_delay", lambda: next(remaining))
        real_wait_for = asyncio.wait_for

        async def recording_wait_for(awaitable, timeout):
            waits.append(timeout)
            on_wait(len(waits))
            return await real_wait_for(awaitable, timeout)

        monkeypatch.setattr(web_push.asyncio, "wait_for", recording_wait_for)
        web_push._worker_wake = asyncio.Event()
        web_push._worker_stop = asyncio.Event()
        web_push._worker_loop_ref = asyncio.get_running_loop()
        try:
            await real_wait_for(web_push._worker_loop(), timeout=5)
        finally:
            web_push._worker_wake = web_push._worker_stop = None
            web_push._worker_loop_ref = None

    asyncio.run(scenario())
    return waits


def _stop_after(count):
    def on_wait(seen):
        if seen == count:
            web_push._worker_stop.set()
        web_push.wake_worker()  # a producer arrives: the wait returns at once
    return on_wait


def test_worker_waits_exactly_until_the_next_due_row(transport, monkeypatch):
    waits = _run_worker(monkeypatch, [25.0, None, 7200.0], _stop_after(3))
    # Due in 25 s -> 25 s; nothing due -> only the hourly cleanup; never 5 s.
    assert waits == [25.0, web_push._IDLE_SECONDS, web_push._IDLE_SECONDS]


def test_due_but_unclaimable_waits_a_second_instead_of_spinning(transport, monkeypatch):
    waits = _run_worker(monkeypatch, [-3.0], _stop_after(1))
    assert waits == [web_push._MIN_WAIT_SECONDS]


def test_schema_is_checked_once_per_process_not_per_connection(tmp_path, monkeypatch):
    calls: list[str] = []
    real = web_push_dao.WebPushStore.initialize

    def counting(self):
        calls.append(str(self.db_path))
        return real(self)

    monkeypatch.setattr(web_push_dao.WebPushStore, "initialize", counting)
    monkeypatch.setattr(web_push, "_schema_ready", set())
    monkeypatch.setattr(web_push_dao, "_INITIALIZED", set())
    db_path = tmp_path / "once.db"
    for _ in range(5):
        web_push._conn(db_path).close()
    store = web_push_dao.WebPushStore(db_path)
    for _ in range(5):
        store.connect().close()
    assert calls == [str(db_path)]
    connection = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        connection.close()
    assert {"web_push_outbox", "web_push_subscriptions"} <= tables
    assert not {name for name in tables if name.startswith("web_push_delivery_")}
    assert "web_push_preferences" not in tables
