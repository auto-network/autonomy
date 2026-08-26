"""State-machine proof for Central-latch Web Push transport rows."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from tools.dashboard.dao.web_push import WebPushStore
from tools.dashboard.web_push_delivery import (
    ReleaseAuthorization,
    WebPushDeliveryError,
    WebPushDeliveryStore,
)


OWNER = "a" * 64
OTHER_OWNER = "b" * 64
DELIVERY = "D" * 43
EVENT = "E" * 43


def _subscription(
    store: WebPushStore,
    *,
    owner: str = OWNER,
    subscription_id: str = "sub-one",
    device_id: str = "device_1234567890",
    created_at: float = 900.0,
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
                subscription_id, owner, device_id, None,
                f"https://web.push.apple.com/Q/{subscription_id}", subscription_id.ljust(64, "0"),
                "https://web.push.apple.com", "https://dashboard.test",
                "receiver", "auth", "f" * 32, None, "generic", "active",
                "token-hash", 1, None, None, None, created_at, created_at,
                created_at, None, None,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _preference(store: WebPushStore, owner: str = OWNER, mode: str = "generic") -> None:
    store.set_preference(owner, "worktrees", mode)


def _latch(
    *,
    state: str = "background_due",
    state_version: int = 1,
    created_at: float = 1000.0,
    expires_at: float = 7000.0,
    event_id: str = EVENT,
    source_ref: str = "approval-opaque",
) -> dict:
    payload = {
        "event_id": event_id,
        "attention_id": "attention-opaque",
        "source_version": 1,
        "application_scope": "worktrees",
        "notification_class": "commit_sign",
        "class_policy_revision": 1,
        "delivery_class": "normal",
        "budget_class": "operator_approval",
        "coalesce_key": "C" * 43,
        "urgency": "normal",
        "privacy_renderer_id": "web_push.generic.v1",
        "route_builder_id": "activity.approval.v1",
        "destination_id": "activity.approval",
        "source_guard": {"kind": "approval", "ref": source_ref, "version": 1},
        "created_at": created_at,
        "expires_at": expires_at,
        "state": state,
        "state_version": state_version,
        "updated_at": created_at,
    }
    if state == "foreground_wait":
        payload.update({
            "foreground_selected_at": created_at,
            "fallback_due_at": created_at + 20,
        })
    return payload


@pytest.fixture
def substrate(tmp_path):
    store = WebPushStore(tmp_path / "web-push.db")
    store.initialize()
    _subscription(store)
    _preference(store)
    clock = [1000.0]
    tokens = iter(f"{number:043d}" for number in range(1000))
    adapter = WebPushDeliveryStore(
        store,
        owner_subject=OWNER,
        clock=lambda: clock[0],
        token_factory=lambda: next(tokens),
        jitter=lambda _low, high: high,
    )
    return store, adapter, clock


class TestWebPushOutboxStateMachine:
    def test_schema_v2_upgrade_adds_transport_tables_without_losing_substrate(self, tmp_path):
        db = tmp_path / "web-push-v2.db"
        store = WebPushStore(db)
        store.initialize()
        _subscription(store)
        _preference(store)
        connection = store.connect()
        try:
            connection.execute("DROP TABLE web_push_delivery_attempts")
            connection.execute("DROP TABLE web_push_delivery_targets")
            connection.execute("DROP TABLE web_push_delivery_events")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        finally:
            connection.close()

        store.initialize()
        connection = store.connect()
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
            assert connection.execute(
                "SELECT count(*) FROM web_push_subscriptions"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT mode FROM web_push_preferences"
            ).fetchone()[0] == "generic"
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'web_push_delivery_%'"
                ).fetchall()
            }
            assert tables == {
                "web_push_delivery_events",
                "web_push_delivery_targets",
                "web_push_delivery_attempts",
            }
        finally:
            connection.close()

    def test_wait_promotes_to_due_and_never_backfills_new_subscription(self, substrate):
        store, adapter, clock = substrate
        assert adapter.project_latch(DELIVERY, _latch(state="foreground_wait")) == 1
        connection = store.connect()
        try:
            row = connection.execute(
                "SELECT state,available_at FROM web_push_delivery_targets"
            ).fetchone()
            assert tuple(row) == ("fallback_wait", 1020.0)
        finally:
            connection.close()

        due = _latch(state="background_due", state_version=2)
        due.update({
            "foreground_selected_at": 1000.0,
            "fallback_due_at": 1020.0,
        })
        due["updated_at"] = 1020.0
        clock[0] = 1020.0
        assert adapter.project_latch(DELIVERY, due) == 0
        _subscription(
            store, subscription_id="sub-late", device_id="device_abcdefghij",
            created_at=1021.0,
        )
        adapter.project_latch(DELIVERY, due)
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_targets"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == "pending"
        finally:
            connection.close()

    def test_terminal_projection_cancels_unsent_and_refuses_timing_drift(self, substrate):
        store, adapter, clock = substrate
        waiting = _latch(state="foreground_wait")
        adapter.project_latch(DELIVERY, waiting)
        drifted = _latch(state="background_due", state_version=2)
        drifted["updated_at"] = 1020.0
        clock[0] = 1020.0
        with pytest.raises(WebPushDeliveryError, match="state conflict"):
            adapter.project_latch(DELIVERY, drifted)

        applied = _latch(state="foreground_applied", state_version=2)
        applied.update({
            "foreground_selected_at": 1000.0,
            "fallback_due_at": 1020.0,
            "acknowledged_at": 1010.0,
            "visibility_proof_epoch": 1,
            "ack_epoch": 1,
            "updated_at": 1010.0,
        })
        assert adapter.project_latch(DELIVERY, applied) == 1
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT latch_state,latch_state_version FROM web_push_delivery_events"
            ).fetchone()) == ("foreground_applied", 2)
            assert tuple(connection.execute(
                "SELECT state,last_reason FROM web_push_delivery_targets"
            ).fetchone()) == ("canceled", "foreground_applied")
        finally:
            connection.close()

        # A delayed older snapshot cannot resurrect transport work.
        assert adapter.project_latch(DELIVERY, waiting) == 0
        assert adapter.claim_next() is None

    def test_preference_off_at_projection_is_a_terminal_transport_suppression(self, substrate):
        store, adapter, _clock = substrate
        _preference(store, mode="off")
        assert adapter.project_latch(DELIVERY, _latch()) == 1
        _preference(store, mode="generic")
        adapter.project_latch(DELIVERY, _latch())
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,last_reason FROM web_push_delivery_targets"
            ).fetchone()) == ("canceled", "preference_off_at_projection")
        finally:
            connection.close()

    def test_owner_bound_marker_is_durable_idempotent_and_wrong_lease_is_stale(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        claim = adapter.claim_next()
        assert claim is not None
        stale = adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, "W" * 43,
        )
        assert stale.status == "stale"
        crossed = adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        )
        assert crossed.status == "crossed"
        assert crossed.release_token is not None
        assert adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        ) == crossed
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,release_token FROM web_push_delivery_targets"
            ).fetchone()) == ("guard_crossed", crossed.release_token)
            assert connection.execute(
                "SELECT budget_reserved_at FROM web_push_delivery_events"
            ).fetchone()[0] == 1000.0
        finally:
            connection.close()

    def test_two_workers_cannot_share_a_lease_or_cross_owner_guard(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        peer = WebPushDeliveryStore(
            store,
            owner_subject=OWNER,
            clock=lambda: 1000.0,
            token_factory=lambda: "P" * 43,
        )
        claim = adapter.claim_next()
        assert claim is not None
        assert peer.claim_next() is None

        foreign = WebPushDeliveryStore(
            store, owner_subject=OTHER_OWNER, clock=lambda: 1000.0,
        )
        assert foreign.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        ).status == "stale"
        assert peer.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        ).status == "crossed"

    def test_preference_change_between_lease_and_marker_cancels_atomically(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        claim = adapter.claim_next()
        assert claim is not None
        _preference(store, mode="off")
        result = adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        )
        assert result.status == "stale"
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,last_reason FROM web_push_delivery_targets"
            ).fetchone()) == ("canceled", "preference_off")
            assert connection.execute(
                "SELECT budget_reserved_at FROM web_push_delivery_events"
            ).fetchone()[0] is None
        finally:
            connection.close()

    def test_cancel_is_owner_delivery_and_event_scoped(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        _subscription(
            store, owner=OTHER_OWNER, subscription_id="sub-other",
            device_id="device_other_1234",
        )
        _preference(store, owner=OTHER_OWNER)
        other = WebPushDeliveryStore(store, owner_subject=OTHER_OWNER, clock=lambda: 1000.0)
        other.project_latch(DELIVERY, _latch())
        assert adapter.cancel_unsent(DELIVERY, EVENT, "foreground_applied") == 1
        connection = store.connect()
        try:
            rows = connection.execute(
                "SELECT owner_subject,state FROM web_push_delivery_targets "
                "ORDER BY owner_subject"
            ).fetchall()
            assert [tuple(row) for row in rows] == [
                (OWNER, "canceled"), (OTHER_OWNER, "pending"),
            ]
        finally:
            connection.close()

    def test_identity_drift_and_malformed_snapshot_fail_without_repair(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        changed = _latch(state_version=2)
        changed["application_scope"] = "fleet"
        with pytest.raises(WebPushDeliveryError, match="identity conflict"):
            adapter.project_latch(DELIVERY, changed)
        with pytest.raises(WebPushDeliveryError, match="invalid request"):
            adapter.project_latch(DELIVERY, {"event_id": EVENT})
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT application_scope,latch_state_version "
                "FROM web_push_delivery_events"
            ).fetchone()) == ("worktrees", 1)
        finally:
            connection.close()

    def test_retry_reenters_guard_and_stale_authorization_cannot_finish(self, substrate):
        store, adapter, clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        claim = adapter.claim_next()
        crossed = adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        )
        authorized_claim = replace(claim, release_token=crossed.release_token)
        authorization = ReleaseAuthorization(
            claim.delivery_id, claim.event_id, claim.target_id,
            claim.lease_token, crossed.release_token,
        )
        assert adapter.finish_attempt(
            authorized_claim, authorization, status=503, outcome="retryable",
            retryable=True, reason="retryable",
        )
        clock[0] = 1005.0
        retried = adapter.claim_next()
        assert retried is not None
        assert retried.lease_token != claim.lease_token
        assert retried.release_token is None
        assert adapter.finish_attempt(
            authorized_claim, authorization, status=201, outcome="accepted",
            retryable=False,
        ) is False

    def test_budget_is_one_recipient_event_and_thirteenth_event_defers(self, substrate):
        store, adapter, _clock = substrate
        for number in range(12):
            delivery = f"{number:043d}"
            event = f"{number + 100:043d}"
            adapter.project_latch(delivery, _latch(event_id=event))
            claim = adapter.claim_next()
            assert claim is not None and claim.delivery_id == delivery
            crossed = adapter.mark_target_guard_crossed(
                delivery, event, claim.target_id, claim.lease_token,
            )
            authorized = replace(claim, release_token=crossed.release_token)
            assert adapter.finish_attempt(
                authorized,
                ReleaseAuthorization(
                    delivery, event, claim.target_id, claim.lease_token,
                    crossed.release_token,
                ),
                status=201, outcome="accepted", retryable=False,
            )

        delivery = "9" * 43
        event = "8" * 43
        adapter.project_latch(delivery, _latch(event_id=event))
        claim = adapter.claim_next()
        result = adapter.mark_target_guard_crossed(
            delivery, event, claim.target_id, claim.lease_token,
        )
        assert result.status == "deferred"
        assert result.available_at == 4600.0
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_events "
                "WHERE budget_reserved_at IS NOT NULL"
            ).fetchone()[0] == 12
            assert tuple(connection.execute(
                "SELECT state,last_reason,available_at FROM web_push_delivery_targets "
                "WHERE delivery_id=?", (delivery,),
            ).fetchone()) == ("budget_wait", "budget_wait", 4600.0)
        finally:
            connection.close()

    def test_unregistered_budget_class_never_releases(self, substrate):
        store, adapter, _clock = substrate
        latch = _latch()
        latch["budget_class"] = "caller_unlimited"
        adapter.project_latch(DELIVERY, latch)
        claim = adapter.claim_next()
        with pytest.raises(WebPushDeliveryError, match="unsupported budget class"):
            adapter.mark_target_guard_crossed(
                claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
            )
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,release_token FROM web_push_delivery_targets"
            ).fetchone()) == ("leased", None)
            assert connection.execute(
                "SELECT budget_reserved_at FROM web_push_delivery_events"
            ).fetchone()[0] is None
        finally:
            connection.close()

    def test_expired_targets_never_claim_and_expired_leases_reclaim_with_new_token(self, substrate):
        store, adapter, clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        first = adapter.claim_next()
        assert first is not None
        clock[0] = 1061.0
        second = adapter.claim_next()
        assert second is not None
        assert second.target_id == first.target_id
        assert second.lease_token != first.lease_token
        assert adapter.mark_target_guard_crossed(
            first.delivery_id, first.event_id, first.target_id, first.lease_token,
        ).status == "stale"
        clock[0] = 7001.0
        connection = store.connect()
        try:
            connection.execute(
                "UPDATE web_push_delivery_targets SET state='pending',"
                "lease_token=NULL,lease_until=NULL"
            )
            connection.commit()
        finally:
            connection.close()
        assert adapter.claim_next() is None
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT state FROM web_push_delivery_targets"
            ).fetchone()[0] == "expired"
        finally:
            connection.close()

    def test_unreleased_guard_marker_expires_and_garbage_collects(self, substrate):
        store, adapter, clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        claim = adapter.claim_next()
        crossed = adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        )
        assert crossed.status == "crossed"

        clock[0] = 7001.0
        assert adapter.claim_next() is None
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,lease_token,release_token FROM web_push_delivery_targets"
            ).fetchone()) == ("expired", None, None)
        finally:
            connection.close()

        clock[0] = 700000.0
        attempts, targets = adapter.cleanup()
        assert attempts == 0
        assert targets == 1
        connection = store.connect()
        try:
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_events"
            ).fetchone()[0] == 0
        finally:
            connection.close()

    def test_invalid_backoff_rolls_back_without_losing_guard_marker(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        claim = adapter.claim_next()
        crossed = adapter.mark_target_guard_crossed(
            claim.delivery_id, claim.event_id, claim.target_id, claim.lease_token,
        )
        authorized = replace(claim, release_token=crossed.release_token)
        authorization = ReleaseAuthorization(
            claim.delivery_id, claim.event_id, claim.target_id,
            claim.lease_token, crossed.release_token,
        )
        adapter._jitter = lambda _low, _high: float("inf")
        with pytest.raises(WebPushDeliveryError, match="invalid backoff"):
            adapter.finish_attempt(
                authorized, authorization, status=503, outcome="retryable",
                retryable=True,
            )
        connection = store.connect()
        try:
            assert tuple(connection.execute(
                "SELECT state,release_token FROM web_push_delivery_targets"
            ).fetchone()) == ("guard_crossed", crossed.release_token)
            assert connection.execute(
                "SELECT count(*) FROM web_push_delivery_attempts"
            ).fetchone()[0] == 0
        finally:
            connection.close()

    def test_diagnostics_are_aggregate_and_owner_scoped(self, substrate):
        store, adapter, _clock = substrate
        adapter.project_latch(DELIVERY, _latch())
        diagnostics = adapter.diagnostics()
        assert diagnostics == {
            "states": {"pending": 1},
            "oldest_due_age_seconds": 0.0,
            "attempts_24h": {},
            "budget_reservations_1h": 0,
        }
        wire = repr(diagnostics)
        assert "web.push.apple.com" not in wire
        assert "receiver" not in wire
        assert "auth" not in wire
