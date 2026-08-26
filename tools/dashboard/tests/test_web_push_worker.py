"""Recovery and final-authorization proof for the Web Push worker."""

from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard.dao.web_push import WebPushStore
from tools.dashboard import web_push_sender
from tools.dashboard.web_push_delivery import (
    GuardResult,
    ReleaseAuthorization,
    WebPushDeliveryStore,
)
from tools.dashboard.web_push_worker import WebPushDeliveryWorker
from tools.dashboard.web_push_worker import _payload


OWNER = "c" * 64
DELIVERY = "L" * 43
EVENT = "V" * 43


def _subscription(
    store: WebPushStore,
    *,
    subscription_id: str,
    device_id: str,
) -> None:
    connection = store.connect()
    try:
        connection.execute(
            "INSERT INTO web_push_subscriptions("
            "id,operator_subject,device_id,serving_machine_id,endpoint,endpoint_hash,"
            "endpoint_origin,vapid_subject,p256dh,auth_secret,vapid_key_id,expiration_time,"
            "max_detail,status,device_update_token_hash,token_version,device_label,"
            "browser_family,platform_family,created_at,updated_at,last_confirmed_at,"
            "retired_at,retire_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                subscription_id, OWNER, device_id, None,
                f"https://web.push.apple.com/Q/{subscription_id}",
                subscription_id.ljust(64, "0"), "https://web.push.apple.com",
                "https://dashboard.test", "receiver", "auth", "d" * 32,
                None, "generic", "active", "token", 1, None, None, None,
                900.0, 900.0, 900.0, None, None,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _latch() -> dict:
    return {
        "event_id": EVENT,
        "attention_id": "attention-worker",
        "source_version": 1,
        "application_scope": "worktrees",
        "notification_class": "commit_sign",
        "class_policy_revision": 1,
        "delivery_class": "normal",
        "budget_class": "operator_approval",
        "coalesce_key": "K" * 43,
        "urgency": "normal",
        "privacy_renderer_id": "web_push.generic.v1",
        "route_builder_id": "activity.approval.v1",
        "destination_id": "activity.approval",
        "source_guard": {"kind": "approval", "ref": "opaque-approval", "version": 1},
        "created_at": 1000.0,
        "expires_at": 7000.0,
        "state": "background_due",
        "state_version": 1,
        "updated_at": 1000.0,
    }


@pytest.fixture
def runtime(tmp_path):
    store = WebPushStore(tmp_path / "web-push.db")
    store.initialize()
    _subscription(store, subscription_id="sub-one", device_id="device_1234567890")
    store.set_preference(OWNER, "worktrees", "generic")
    tokens = iter(f"{number:043d}" for number in range(1000))
    adapter = WebPushDeliveryStore(
        store,
        owner_subject=OWNER,
        clock=lambda: 1000.0,
        token_factory=lambda: next(tokens),
        jitter=lambda _low, high: high,
    )
    adapter.project_latch(DELIVERY, _latch())
    return store, adapter


class _Coordinator:
    def __init__(self, *, authorize: bool = True, fail_after_marker: bool = False):
        self.authorize = authorize
        self.fail_after_marker = fail_after_marker
        self.reconciles = 0

    def reconcile_web_push(self, _adapter):
        self.reconciles += 1

    def final_guard_and_release(
        self, delivery_id, event_id, target_id, lease_token, marker,
    ):
        result = marker(delivery_id, event_id, target_id, lease_token)
        assert isinstance(result, GuardResult)
        if self.fail_after_marker:
            raise RuntimeError("Settings write refused")
        if not self.authorize or result.status != "crossed":
            return None
        return ReleaseAuthorization(
            delivery_id, event_id, target_id, lease_token, result.release_token,
        )


class TestWebPushWorkerRecovery:
    def test_payload_is_generic_bounded_and_uses_only_trusted_route_evidence(self, runtime):
        _store, adapter = runtime
        claim = adapter.claim_next()
        payload = _payload(claim)
        assert len(payload.encode("utf-8")) <= 2048
        assert '"title":"Autonomy needs your attention"' in payload
        assert '"body":"Open the dashboard to review."' in payload
        assert "opaque-approval" in payload
        assert "receiver" not in payload
        assert "auth" not in payload
        assert "web.push.apple.com" not in payload

    @pytest.mark.asyncio
    async def test_marker_without_central_authorization_invokes_zero_sender(self, runtime):
        store, adapter = runtime
        sent = []
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=_Coordinator(authorize=False),
            sender=lambda claim: sent.append(claim),
        )
        claim = adapter.claim_next()
        assert claim is not None
        await worker._attempt(claim)
        assert sent == []
        connection = store.connect()
        try:
            row = connection.execute(
                "SELECT state,release_token FROM web_push_delivery_targets"
            ).fetchone()
            assert row["state"] == "guard_crossed"
            assert row["release_token"] is not None
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_attempts"
            ).fetchone()[0] == 0
        finally:
            connection.close()

    @pytest.mark.asyncio
    async def test_same_marker_repairs_restart_then_sends_once(self, runtime):
        store, adapter = runtime
        first = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=_Coordinator(fail_after_marker=True),
            sender=lambda _claim: pytest.fail("marker alone sent"),
        )
        claim = adapter.claim_next()
        await first._attempt(claim)
        adapter._clock = lambda: 1001.0
        recovered = adapter.claim_next()
        assert recovered is not None and recovered.release_token is not None
        sent = []

        def sender(target):
            sent.append(target.release_token)
            return web_push_sender.SendResult(201, "accepted", False, False, None)

        second = WebPushDeliveryWorker(
            adapter=adapter, coordinator=_Coordinator(), sender=sender,
        )
        await second._attempt(recovered)
        assert sent == [recovered.release_token]
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == "accepted"
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_attempts "
                "WHERE outcome='accepted'"
            ).fetchone()[0] == 1
        finally:
            connection.close()

    @pytest.mark.asyncio
    async def test_ten_pre_marker_failures_terminalize_without_eleventh_lease_or_send(
        self, runtime,
    ):
        store, adapter = runtime
        clock = [1000.0]
        adapter._clock = lambda: clock[0]

        class FailingCoordinator(_Coordinator):
            def final_guard_and_release(
                self, delivery_id, event_id, target_id, lease_token, marker,
            ):
                raise RuntimeError("Central unavailable before durable marker")

        sent = []
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=FailingCoordinator(),
            sender=lambda claim: sent.append(claim),
        )
        for attempt in range(10):
            claim = adapter.claim_next()
            assert claim is not None
            assert claim.attempt_count == attempt + 1
            await worker._attempt(claim)
            clock[0] += 61.0

        assert adapter.claim_next() is None
        assert sent == []
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,attempt_count,last_reason "
                "FROM web_push_delivery_targets"
            ).fetchone()) == ("failed_permanent", 10, "attempt_limit")
        finally:
            connection.close()

    @pytest.mark.asyncio
    async def test_marker_read_failure_defers_same_marker_without_sender_or_spin(
        self, runtime, monkeypatch,
    ):
        store, adapter = runtime
        clock = [1000.0]
        adapter._clock = lambda: clock[0]
        monkeypatch.setattr(
            adapter,
            "authorized_claim",
            lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError()),
        )
        sent = []
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=_Coordinator(),
            sender=lambda claim: sent.append(claim),
        )

        claim = adapter.claim_next()
        await worker._attempt(claim)
        assert sent == []
        assert adapter.claim_next() is None
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,attempt_count,available_at "
                "FROM web_push_delivery_targets"
            ).fetchone()) == ("guard_crossed", 1, 1001.0)
        finally:
            connection.close()

        clock[0] = 1001.0
        recovered = adapter.claim_next()
        assert recovered is not None
        assert recovered.lease_token == claim.lease_token
        assert recovered.release_token is not None
        assert recovered.attempt_count == 1

    @pytest.mark.asyncio
    async def test_release_a_then_cancel_leaves_only_authorized_target(self, runtime):
        store, adapter = runtime
        _subscription(store, subscription_id="sub-two", device_id="device_abcdefghij")
        adapter.project_latch(DELIVERY, _latch())
        claim = adapter.claim_next()

        def sender(_target):
            adapter.cancel_unsent(DELIVERY, EVENT, "foreground_applied")
            return web_push_sender.SendResult(201, "accepted", False, False, None)

        worker = WebPushDeliveryWorker(
            adapter=adapter, coordinator=_Coordinator(), sender=sender,
        )
        await worker._attempt(claim)
        connection = store.connect()
        try:
            states = [
                row[0] for row in connection.execute(
                    "SELECT state FROM web_push_delivery_targets ORDER BY target_id"
                ).fetchall()
            ]
            assert sorted(states) == ["accepted", "canceled"]
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_attempts"
            ).fetchone()[0] == 1
        finally:
            connection.close()

    @pytest.mark.asyncio
    async def test_push_service_retirement_cancels_subscription_siblings(self, runtime):
        store, adapter = runtime
        claim = adapter.claim_next()
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=_Coordinator(),
            sender=lambda _target: web_push_sender.SendResult(
                410, "retired", False, True, None,
            ),
        )
        await worker._attempt(claim)
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT status,retire_reason FROM web_push_subscriptions"
            ).fetchone()) == ("retired", "push_service_gone")
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == "canceled"
        finally:
            connection.close()

    @pytest.mark.asyncio
    async def test_wrong_authorization_shape_never_reaches_sender(self, runtime):
        _store, adapter = runtime

        class WrongCoordinator(_Coordinator):
            def final_guard_and_release(
                self, delivery_id, event_id, target_id, lease_token, marker,
            ):
                result = marker(delivery_id, event_id, target_id, lease_token)
                return ReleaseAuthorization(
                    delivery_id, event_id, "X" * 43, lease_token, result.release_token,
                )

        sent = []
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=WrongCoordinator(),
            sender=lambda claim: sent.append(claim),
        )
        await worker._attempt(adapter.claim_next())
        assert sent == []

    @pytest.mark.asyncio
    async def test_central_authorization_without_durable_marker_never_sends(self, runtime):
        _store, adapter = runtime

        class ForgedCoordinator(_Coordinator):
            def final_guard_and_release(
                self, delivery_id, event_id, target_id, lease_token, _marker,
            ):
                return ReleaseAuthorization(
                    delivery_id, event_id, target_id, lease_token, "R" * 43,
                )

        sent = []
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=ForgedCoordinator(),
            sender=lambda claim: sent.append(claim),
        )
        await worker._attempt(adapter.claim_next())
        assert sent == []

    @pytest.mark.asyncio
    async def test_cancellation_after_central_return_before_sender_suppresses(self, runtime):
        _store, adapter = runtime

        class CancelingCoordinator(_Coordinator):
            def final_guard_and_release(
                self, delivery_id, event_id, target_id, lease_token, marker,
            ):
                result = marker(delivery_id, event_id, target_id, lease_token)
                adapter.cancel_unsent(delivery_id, event_id, "foreground_applied")
                return ReleaseAuthorization(
                    delivery_id, event_id, target_id, lease_token, result.release_token,
                )

        sent = []
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=CancelingCoordinator(),
            sender=lambda claim: sent.append(claim),
        )
        await worker._attempt(adapter.claim_next())
        assert sent == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "target_state", "subscription_state"),
        (
            (web_push_sender.SendResult(201, "accepted", False, False, None),
             "accepted", "active"),
            (web_push_sender.SendResult(404, "retired", False, True, None),
             "canceled", "retired"),
            (web_push_sender.SendResult(429, "retryable", True, False, 30.0),
             "retry_wait", "active"),
            (web_push_sender.SendResult(503, "retryable", True, False, None),
             "retry_wait", "active"),
            (web_push_sender.SendResult(400, "permanent_failure", False, False, None),
             "failed_permanent", "active"),
            (web_push_sender.SendResult(302, "redirect_refused", False, False, None),
             "failed_permanent", "active"),
        ),
    )
    async def test_push_service_result_classes_are_durable(
        self, runtime, result, target_state, subscription_state,
    ):
        store, adapter = runtime
        worker = WebPushDeliveryWorker(
            adapter=adapter, coordinator=_Coordinator(), sender=lambda _claim: result,
        )
        await worker._attempt(adapter.claim_next())
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == target_state
            assert connection.execute(
                "SELECT status FROM web_push_subscriptions"
            ).fetchone()[0] == subscription_state
            assert connection.execute(
                "SELECT outcome FROM web_push_delivery_attempts"
            ).fetchone()[0] == result.outcome
        finally:
            connection.close()

    @pytest.mark.asyncio
    async def test_attempt_record_failure_does_not_escape_or_lose_marker(
        self, runtime, monkeypatch,
    ):
        store, adapter = runtime
        monkeypatch.setattr(
            adapter, "finish_attempt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
        )
        worker = WebPushDeliveryWorker(
            adapter=adapter,
            coordinator=_Coordinator(),
            sender=lambda _claim: web_push_sender.SendResult(
                201, "accepted", False, False, None,
            ),
        )
        await worker._attempt(adapter.claim_next())
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == "guard_crossed"
        finally:
            connection.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "expected_state"),
        (
            (RuntimeError("sender runtime missing"), "failed_permanent"),
            (OSError("temporary network failure"), "retry_wait"),
        ),
    )
    async def test_sender_configuration_is_permanent_but_network_errors_retry(
        self, runtime, error, expected_state,
    ):
        store, adapter = runtime

        def fail(_claim):
            raise error

        worker = WebPushDeliveryWorker(
            adapter=adapter, coordinator=_Coordinator(), sender=fail,
        )
        await worker._attempt(adapter.claim_next())
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == expected_state
        finally:
            connection.close()
