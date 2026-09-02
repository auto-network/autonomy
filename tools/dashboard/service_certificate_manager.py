"""Unattended lifecycle for persona Service TLS certificates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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

    async def reconcile_once(self) -> bool:
        async with self._lock:
            desired = sorted(self._desired())
            for org, persona in desired:
                identity = (org, persona)
                try:
                    metadata = service_certificate.certificate_metadata(org, persona)
                    if metadata is None:
                        metadata = await asyncio.to_thread(
                            _import_legacy_pair, org, persona
                        )
                    if metadata is None:
                        # Exercise the complete DNS/CA path without ever
                        # activating the untrusted staging certificate.
                        _staging, staging_cert, staging_key = (
                            await service_certificate.obtain(
                                org, persona, staging=True
                            )
                        )
                        del staging_cert, staging_key, _staging
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
                except Exception as exc:
                    self.errors[identity] = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Service certificate reconciliation failed for %s/%s",
                        org,
                        persona,
                        exc_info=True,
                    )
            return not any(identity in self.errors for identity in desired)


class ServiceCertificateWorker:
    def __init__(self, manager=None) -> None:
        self._manager = manager or ServiceCertificateManager()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def request_reconcile(self) -> None:
        """Wake the single worker; concurrent requests coalesce."""
        self._wake.set()

    async def _run(self, event_bus) -> None:
        queue = event_bus.subscribe(client_id="service-certificate-manager")
        try:
            healthy = await self._manager.reconcile_once()
            while True:
                timeout = CHECK_INTERVAL_SECONDS if healthy else RETRY_INTERVAL_SECONDS
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
                    if wake_task in done:
                        self._wake.clear()
                        healthy = await self._manager.reconcile_once()
                    elif event_task in done:
                        topic, data, _sequence = event_task.result()
                        if topic == "network:serving" or (
                            topic == "setting.changed"
                            and isinstance(data, dict)
                            and data.get("set_id")
                            in {NAMESPACE_RESERVATION_SET_ID, SERVICE_TARGET_SET_ID}
                        ):
                            healthy = await self._manager.reconcile_once()
                    else:
                        healthy = await self._manager.reconcile_once()
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
