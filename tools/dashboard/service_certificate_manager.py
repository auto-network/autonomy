"""Unattended lifecycle for persona Service TLS certificates."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import logging
import re
import time

from tools.dashboard import service_certificate, service_publication
from tools.graph.schemas.namespace_reservation import NAMESPACE_RESERVATION_SET_ID
from tools.graph.schemas.service_target import SERVICE_TARGET_SET_ID


logger = logging.getLogger(__name__)
RETRY_INTERVAL_SECONDS = 60.0
CHECK_INTERVAL_SECONDS = 6 * 60 * 60.0


def desired_personas() -> set[tuple[str, str]]:
    """Return unique org/persona pairs with an active or paused publication."""
    from tools.graph import org_ops

    desired: set[tuple[str, str]] = set()
    for ref in org_ops.list_orgs():
        if ref.type != "shared":
            continue
        targets = {
            row.get("reservation_id")
            for row in service_publication.list_service_targets(ref.slug)
            if isinstance(row, dict)
        }
        for row in service_publication.list_reservations(ref.slug):
            persona = row.get("persona_label") if isinstance(row, dict) else None
            if (
                isinstance(persona, str)
                and row.get("state") in {"active", "paused"}
                and row.get("reservation_id") in targets
            ):
                desired.add((ref.slug, persona))
    return desired


_RETRY_AFTER_RE = re.compile(r"retry after (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")
MAX_BACKOFF_SECONDS = 15 * 60.0
NO_BACKOFF_MARKERS = ("vault is locked", "vault cold", "key holder")


def failure_hold_seconds(error: str, consecutive: int, now: float) -> float:
    """How long to leave an identity alone after a failed issuance.

    An ACME rate limit names its own retry time; honour it (plus a margin)
    instead of hammering a 1-hour limit every 60 s. Other failures back off
    exponentially from one minute to fifteen. A locked vault is the operator's
    next action, not ours: no hold, so the unlock is picked up within a minute.
    """
    match = _RETRY_AFTER_RE.search(error or "")
    if match:
        when = _dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_dt.timezone.utc
        ).timestamp()
        return max(0.0, when - now) + 30.0
    lowered = (error or "").lower()
    if any(marker in lowered for marker in NO_BACKOFF_MARKERS):
        return 0.0
    return min(MAX_BACKOFF_SECONDS, 60.0 * (2 ** max(0, consecutive - 1)))


def _import_legacy_pair(org: str, persona_label: str) -> dict | None:
    """Persist the live-launch pair once, without contacting ACME again."""
    try:
        metadata = json.loads(service_certificate.STATUS_PATH.read_text())
    except Exception:
        return None
    apex = f"{persona_label}.serve.auto.network"
    if metadata.get("org") != org or metadata.get("apex") != apex:
        return None
    if not all(
        path.is_file()
        for path in (service_certificate.GATEWAY_CERT, service_certificate.GATEWAY_KEY)
    ):
        return None
    if not service_certificate.settings_ops.personal_delegate_audited_is_warm():
        # A personal audited write is intentionally possible while cold, but
        # this migration immediately reads it back before publishing metadata.
        # Refuse before the write so periodic retries cannot accumulate sealed
        # sibling rows while waiting for an operator unlock.
        raise service_certificate.ServiceCertificateError(
            "certificate vault is locked; unlock before importing the legacy pair"
        )
    verified = service_certificate._verify_pair(
        service_certificate.GATEWAY_CERT,
        service_certificate.GATEWAY_KEY,
        apex,
    )
    verified.update(
        {
            "org": org,
            "staging": bool(metadata.get("staging")),
            "activated_at": int(metadata.get("activated_at") or time.time()),
        }
    )
    return service_certificate.activate_pair(
        org,
        persona_label,
        service_certificate.GATEWAY_CERT,
        service_certificate.GATEWAY_KEY,
        verified,
    )


class ServiceCertificateManager:
    """Serialize issuance and converge every desired persona independently."""

    def __init__(self, *, now=None, desired_fn=None) -> None:
        self._now = now or time.time
        self._desired = desired_fn or desired_personas
        self._lock = asyncio.Lock()
        self._materialized: dict[tuple[str, str], str] = {}
        self.errors: dict[tuple[str, str], str] = {}
        self.in_progress: set[tuple[str, str]] = set()
        self.hold_until: dict[tuple[str, str], float] = {}
        self.failures: dict[tuple[str, str], int] = {}

    async def reconcile_once(self) -> bool:
        async with self._lock:
            desired = sorted(self._desired())
            for org, persona in desired:
                identity = (org, persona)
                if self.hold_until.get(identity, 0.0) > float(self._now()):
                    continue  # rate-limited or backing off; the state says until when
                self.in_progress.add(identity)
                try:
                    metadata = service_certificate.certificate_metadata(org, persona)
                    if metadata is None:
                        metadata = await asyncio.to_thread(
                            _import_legacy_pair, org, persona
                        )
                    if metadata is None:
                        # Staging is an explicit acceptance operation, not a
                        # prerequisite for every first production issuance.
                        # Running it here would create disposable staging
                        # accounts at production cardinality.
                        metadata = await service_certificate.issue(
                            org, persona, staging=False
                        )
                    elif int(metadata["not_after"]) - int(self._now()) <= (
                        service_certificate.RENEWAL_WINDOW_SECONDS
                    ):
                        metadata = await service_certificate.issue(
                            org, persona, staging=False
                        )
                    elif self._materialized.get(identity) != metadata["serial"]:
                        bundle = service_certificate._read_bundle(metadata["vault_key"])
                        await asyncio.to_thread(
                            service_certificate._materialize_bundle,
                            metadata,
                            bundle,
                        )
                        await asyncio.to_thread(
                            service_certificate._retire_old_ramfs, metadata
                        )
                    self._materialized[identity] = metadata["serial"]
                    self.errors.pop(identity, None)
                    self.hold_until.pop(identity, None)
                    self.failures.pop(identity, None)
                except service_certificate.ServiceCertificateError as exc:
                    self._note_failure(identity, f"{type(exc).__name__}: {exc}")
                    # Expected, self-describing refusals (vault still locked or
                    # its bundle not yet restored on a fresh worker, no gateway
                    # pair to import): one line, no traceback. The retry
                    # interval handles them; the traceback added nothing but
                    # a page of noise per worker startup.
                    self.errors[identity] = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Service certificate reconciliation deferred for %s/%s: %s",
                        org, persona, exc,
                    )
                except Exception as exc:
                    self._note_failure(identity, f"{type(exc).__name__}: {exc}")
                    logger.warning(
                        "Service certificate reconciliation failed for %s/%s",
                        org,
                        persona,
                        exc_info=True,
                    )
                finally:
                    self.in_progress.discard(identity)
            return not any(identity in self.errors for identity in desired)

    def _note_failure(self, identity: tuple[str, str], error: str) -> None:
        self.errors[identity] = error
        self.failures[identity] = self.failures.get(identity, 0) + 1
        hold = failure_hold_seconds(error, self.failures[identity], float(self._now()))
        if hold > 0:
            self.hold_until[identity] = float(self._now()) + hold
            logger.warning(
                "Service certificate: holding %s/%s for %.0f s after failure %d",
                identity[0], identity[1], hold, self.failures[identity],
            )
        else:
            self.hold_until.pop(identity, None)

    def certificate_states(self) -> list[dict]:
        """Return the operator-facing state of every desired persona pair."""
        now = int(self._now())
        states = []
        for org, persona in sorted(self._desired()):
            identity = (org, persona)
            metadata = service_certificate.certificate_metadata(org, persona)
            error = self.errors.get(identity)
            if error:
                state = "issuance_failed"
                # Certbot states the CAUSE last (the hook's stderr, the ACME
                # error, the 'attempt kept at' pointer); the first line is its
                # 'Requesting a certificate for …' preamble. Show the tail.
                lines = [line.strip() for line in error.splitlines() if line.strip()]
                summary = " · ".join(lines[-3:])[-600:] if lines else error[:600]
                reason = f"Certificate issuance failed: {summary}"
                held = self.hold_until.get(identity, 0.0)
                if held > now:
                    at = _dt.datetime.fromtimestamp(held, _dt.timezone.utc).strftime("%H:%M:%S UTC")
                    reason += f" · next attempt at {at}"
            elif identity in self.in_progress:
                state = "issuing"
                reason = "Certificate issuance or renewal is in progress."
            elif metadata is None:
                state = "missing"
                reason = "No persona Service TLS certificate has been issued yet."
            else:
                remaining = int(metadata.get("not_after") or 0) - now
                if remaining <= 0:
                    state = "expired"
                    reason = "The persona Service TLS certificate has expired."
                elif remaining <= service_certificate.RENEWAL_WINDOW_SECONDS:
                    state = "renewal_due"
                    reason = "The persona Service TLS certificate is inside its renewal window."
                else:
                    state = "current"
                    reason = "The persona Service TLS certificate is current."
            states.append(
                {
                    "detail": error if error else None,
                    "org": org,
                    "persona_label": persona,
                    "state": state,
                    "reason": reason,
                }
            )
        return states


class ServiceCertificateWorker:
    def __init__(self, manager=None, *, retry_interval=None,
                 check_interval=None) -> None:
        self._manager = manager or ServiceCertificateManager()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._retry_interval = (
            RETRY_INTERVAL_SECONDS if retry_interval is None else retry_interval
        )
        self._check_interval = (
            CHECK_INTERVAL_SECONDS if check_interval is None else check_interval
        )

    def request_reconcile(self) -> None:
        """Wake the single worker; concurrent requests coalesce."""
        self._wake.set()

    async def _run(self, event_bus) -> None:
        queue = event_bus.subscribe(client_id="service-certificate-manager")
        try:
            healthy = await self._manager.reconcile_once()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + (
                self._check_interval if healthy else self._retry_interval
            )
            while True:
                # Unrelated EventBus traffic must not restart this deadline.
                # A busy Dashboard otherwise starves a failed certificate
                # forever even though RETRY_INTERVAL_SECONDS says one minute.
                timeout = max(0.0, deadline - loop.time())
                event_task = asyncio.create_task(queue.get())
                wake_task = asyncio.create_task(self._wake.wait())
                try:
                    done, pending = await asyncio.wait(
                        {event_task, wake_task},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    reconcile = False
                    if wake_task in done:
                        self._wake.clear()
                        reconcile = True
                    elif event_task in done:
                        topic, data, _sequence = event_task.result()
                        if topic == "network:serving" or (
                            topic == "setting.changed"
                            and isinstance(data, dict)
                            and data.get("set_id")
                            in {NAMESPACE_RESERVATION_SET_ID, SERVICE_TARGET_SET_ID}
                        ):
                            reconcile = True
                    else:
                        reconcile = True
                    if reconcile:
                        healthy = await self._manager.reconcile_once()
                        deadline = loop.time() + (
                            self._check_interval if healthy
                            else self._retry_interval
                        )
                finally:
                    for task in (event_task, wake_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(event_task, wake_task, return_exceptions=True)
        finally:
            event_bus.unsubscribe(queue)

    async def start(self, event_bus) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(event_bus), name="service-certificate-manager"
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None


_worker: ServiceCertificateWorker | None = None


def certificate_states() -> list[dict]:
    """Snapshot the live manager state without starting another manager."""
    if _worker is None:
        manager = ServiceCertificateManager()
    else:
        manager = _worker._manager
    return manager.certificate_states()


async def start_worker(event_bus) -> None:
    global _worker
    if _worker is None:
        _worker = ServiceCertificateWorker()
    await _worker.start(event_bus)


async def stop_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()
    _worker = None


def request_reconcile() -> None:
    """Wake certificate convergence after a successful root-unlock ceremony."""
    if _worker is None:
        return
    _worker.request_reconcile()
