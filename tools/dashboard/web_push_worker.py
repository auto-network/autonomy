"""Background worker for Central-authorized Web Push delivery targets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
from typing import Callable, Protocol
from urllib.parse import urlencode

from tools.dashboard.dao import web_push as web_push_dao
from tools.dashboard import web_push_sender
from tools.dashboard.web_push_delivery import (
    ClaimedTarget,
    GuardResult,
    ReleaseAuthorization,
    WebPushDeliveryStore,
)


logger = logging.getLogger(__name__)
_RECONCILE_SECONDS = 5.0


class DeliveryCoordinator(Protocol):
    """Transport-facing half implemented by the Central `.23` mediator."""

    def reconcile_web_push(self, adapter: WebPushDeliveryStore) -> None: ...

    def final_guard_and_release(
        self,
        delivery_id: str,
        event_id: str,
        target_id: str,
        lease_token: str,
        mark_target_guard_crossed: Callable[[str, str, str, str], GuardResult],
    ) -> ReleaseAuthorization | None: ...


def _payload(claim: ClaimedTarget) -> str:
    if claim.privacy_renderer_id != "web_push.generic.v1":
        raise ValueError("unregistered Web Push privacy renderer")
    if (
        claim.route_builder_id != "activity.approval.v1"
        or claim.destination_id != "activity.approval"
    ):
        raise ValueError("unregistered Web Push route builder")
    route = "/activity?" + urlencode({
        "focus": "approval",
        "id": claim.source_guard_ref,
    })
    payload = {
        "v": 1,
        "event_id": claim.event_id,
        "class": claim.notification_class,
        "title": "Autonomy needs your attention",
        "body": "Open the dashboard to review.",
        "route": route,
        "tag": f"attention:{claim.event_id}",
        "issued_at": int(claim.latch_created_at),
        "expires_at": int(claim.expires_at),
    }
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if not 1 <= len(wire.encode("utf-8")) <= 2048:
        raise ValueError("Web Push payload exceeds policy")
    return wire


def _topic(event_id: str) -> str:
    import base64
    return base64.urlsafe_b64encode(
        hashlib.sha256(event_id.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")[:32]


def send_claim(claim: ClaimedTarget) -> web_push_sender.SendResult:
    """Publish one already-authorized target through the pinned sender."""

    from tools.dashboard import web_push

    remaining = claim.expires_at - time.time()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ValueError("Web Push target expired")
    return web_push_sender.send_encrypted_web_push(
        endpoint=claim.endpoint,
        p256dh=claim.p256dh,
        auth_secret=claim.auth_secret,
        payload=_payload(claim),
        vapid_key=web_push._load_vapid(claim.vapid_key_id),
        vapid_subject=claim.vapid_subject,
        ttl=max(0, min(int(remaining), web_push_sender.MAX_TTL_SECONDS)),
        urgency=claim.urgency,
        topic=_topic(claim.event_id),
    )


class WebPushDeliveryWorker:
    def __init__(
        self,
        *,
        adapter: WebPushDeliveryStore,
        coordinator: DeliveryCoordinator,
        sender: Callable[[ClaimedTarget], web_push_sender.SendResult] = send_claim,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(adapter, WebPushDeliveryStore):
            raise ValueError("invalid Web Push delivery adapter")
        if not callable(sender) or not callable(clock):
            raise ValueError("worker dependencies must be callable")
        for name in ("reconcile_web_push", "final_guard_and_release"):
            if not callable(getattr(coordinator, name, None)):
                raise ValueError("incomplete Central delivery coordinator")
        self.adapter = adapter
        self.coordinator = coordinator
        self.sender = sender
        self.clock = clock
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_reconcile = 0.0
        self._last_cleanup = 0.0

    def wake(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is loop:
                self._wake.set()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(self._wake.set)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @staticmethod
    def _valid_authorization(
        claim: ClaimedTarget, value: object,
    ) -> ReleaseAuthorization | None:
        if not isinstance(value, ReleaseAuthorization):
            return None
        if (
            value.delivery_id != claim.delivery_id
            or value.event_id != claim.event_id
            or value.target_id != claim.target_id
            or value.lease_token != claim.lease_token
            or not isinstance(value.release_token, str)
        ):
            return None
        return value

    async def _reconcile(self) -> None:
        await asyncio.to_thread(self.coordinator.reconcile_web_push, self.adapter)

    async def _defer(self, claim: ClaimedTarget) -> None:
        try:
            await asyncio.to_thread(self.adapter.defer_marker, claim)
        except Exception:
            logger.error(
                "web_push_marker_defer_unavailable delivery=%s target=%s",
                claim.delivery_id[:12], claim.target_id[:12], exc_info=True,
            )

    async def _finish(
        self,
        claim: ClaimedTarget,
        authorization: ReleaseAuthorization,
        **result,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.adapter.finish_attempt, claim, authorization, **result,
            )
        except Exception:
            # The durable marker remains recoverable.  In particular, a response
            # accepted by a push service followed by local commit failure is an
            # unavoidable at-least-once ambiguity; the stable Topic/tag bounds
            # duplicate presentation while reconciliation retries the marker.
            logger.error(
                "web_push_attempt_record_unavailable delivery=%s target=%s",
                claim.delivery_id[:12], claim.target_id[:12], exc_info=True,
            )
            await self._defer(claim)

    async def _attempt(self, claim: ClaimedTarget) -> None:
        try:
            candidate = await asyncio.to_thread(
                self.coordinator.final_guard_and_release,
                claim.delivery_id,
                claim.event_id,
                claim.target_id,
                claim.lease_token,
                self.adapter.mark_target_guard_crossed,
            )
        except Exception:
            logger.warning(
                "web_push_final_guard_unavailable delivery=%s target=%s",
                claim.delivery_id[:12], claim.target_id[:12], exc_info=True,
            )
            await self._defer(claim)
            return
        authorization = self._valid_authorization(claim, candidate)
        if authorization is None:
            await self._defer(claim)
            return
        try:
            authorized_claim = await asyncio.to_thread(
                self.adapter.authorized_claim, claim, authorization,
            )
        except Exception:
            logger.error(
                "web_push_marker_read_unavailable delivery=%s target=%s",
                claim.delivery_id[:12], claim.target_id[:12], exc_info=True,
            )
            await self._defer(claim)
            return
        if authorized_claim is None:
            return
        try:
            result = await asyncio.to_thread(self.sender, authorized_claim)
        except Exception as exc:
            permanent = isinstance(
                exc,
                (web_push_sender.WebPushInputError,
                 web_push_sender.WebPushEgressPolicyError,
                 web_push_dao.WebPushStoreError,
                 ValueError, PermissionError, RuntimeError),
            )
            reason = type(exc).__name__
            logger.warning(
                "web_push_send_failed delivery=%s target=%s error_type=%s",
                claim.delivery_id[:12], claim.target_id[:12], reason,
            )
            await self._finish(
                authorized_claim,
                authorization,
                status=None,
                outcome="permanent_failure" if permanent else "transport_error",
                retryable=not permanent,
                reason=reason,
            )
            return
        if not isinstance(result, web_push_sender.SendResult):
            await self._finish(
                authorized_claim,
                authorization,
                status=None,
                outcome="permanent_failure",
                retryable=False,
                reason="InvalidSenderResult",
            )
            return
        await self._finish(
            authorized_claim,
            authorization,
            status=result.status,
            outcome=result.outcome,
            retryable=result.retryable,
            retire_subscription=result.retire_subscription,
            retry_after=result.retry_after,
            reason=result.outcome,
        )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                try:
                    raw_instant = self.clock()
                    if isinstance(raw_instant, bool) or not isinstance(
                        raw_instant, (int, float),
                    ):
                        raise ValueError("invalid worker clock")
                    instant = float(raw_instant)
                    if not math.isfinite(instant) or instant < 0:
                        raise ValueError("invalid worker clock")
                except Exception:
                    logger.error("web_push_worker_clock_unavailable", exc_info=True)
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                if instant - self._last_reconcile >= _RECONCILE_SECONDS:
                    try:
                        await self._reconcile()
                    except Exception:
                        logger.warning("web_push_reconciliation_unavailable", exc_info=True)
                    self._last_reconcile = instant
                try:
                    claim = await asyncio.to_thread(self.adapter.claim_next)
                except Exception:
                    logger.error("web_push_claim_unavailable", exc_info=True)
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                if claim is not None:
                    await self._attempt(claim)
                    continue
                if instant - self._last_cleanup >= 3600.0:
                    try:
                        await asyncio.to_thread(self.adapter.cleanup)
                    except Exception:
                        logger.warning("web_push_cleanup_unavailable", exc_info=True)
                    self._last_cleanup = instant
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
        finally:
            self._loop = None


_coordinator: DeliveryCoordinator | None = None
_worker: WebPushDeliveryWorker | None = None
_task: asyncio.Task | None = None
_configuration_lock = threading.Lock()


def install_coordinator(coordinator: DeliveryCoordinator) -> DeliveryCoordinator | None:
    """Install Central's process-owned coordinator before lifespan startup."""

    global _coordinator
    for name in ("reconcile_web_push", "final_guard_and_release"):
        if not callable(getattr(coordinator, name, None)):
            raise ValueError("incomplete Central delivery coordinator")
    with _configuration_lock:
        previous = _coordinator
        _coordinator = coordinator
    wake_worker()
    return previous


async def start_worker() -> None:
    global _worker, _task
    if _task is not None and not _task.done():
        return
    with _configuration_lock:
        coordinator = _coordinator
    if coordinator is None:
        logger.info("Central Web Push delivery worker awaits coordinator composition")
        return
    from tools.dashboard import web_push

    try:
        owner = await asyncio.to_thread(web_push._stable_owner_id)
        store = web_push_dao.WebPushStore(web_push_dao.DB_PATH)
        await asyncio.to_thread(store.initialize)
    except Exception:
        logger.error("Central Web Push delivery worker unavailable", exc_info=True)
        return
    adapter = WebPushDeliveryStore(store, owner_subject=owner)
    _worker = WebPushDeliveryWorker(adapter=adapter, coordinator=coordinator)
    _task = asyncio.create_task(_worker.run(), name="central-web-push-delivery")


async def stop_worker() -> None:
    global _worker, _task
    if _task is None:
        return
    assert _worker is not None
    _worker.stop()
    try:
        await _task
    finally:
        _worker = None
        _task = None


def wake_worker() -> None:
    if _worker is not None:
        _worker.wake()


__all__ = [
    "DeliveryCoordinator",
    "WebPushDeliveryWorker",
    "install_coordinator",
    "send_claim",
    "start_worker",
    "stop_worker",
    "wake_worker",
]
