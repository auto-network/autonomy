"""Desired-state lifecycle for public Service publication on this node.

The durable publication and certificate stores remain authoritative. This
module converges their two local runtime projections: hostname leases on the
serving connector and routes on the Compose-local Caddy. It starts the dormant
Caddy service, loads one complete config atomically, reports routes only after
that load succeeds, and stops the service when nothing remains authorized.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from tools.dashboard import service_certificate, service_gateway, service_publication
from tools.graph.schemas.namespace_reservation import NAMESPACE_RESERVATION_SET_ID
from tools.graph.schemas.service_target import SERVICE_TARGET_SET_ID


logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class DesiredRoute:
    """One authorized route identity and the exact authority it represents."""

    route_id: str
    fingerprint: str


@dataclass(frozen=True)
class GatewayDesiredState:
    """A complete config candidate derived from current durable/live state."""

    caddyfile: str
    routes: tuple[DesiredRoute, ...]
    ready: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        route_ids = [route.route_id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("duplicate desired route id")
        if self.routes and not self.caddyfile.strip():
            raise ValueError("non-empty desired routes require a Caddy config")


class GatewayRuntime(Protocol):
    async def ensure_started(self) -> None: ...

    async def is_healthy(self) -> bool: ...

    async def instance_marker(self) -> str | None: ...

    async def stop(self) -> None: ...


Loader = Callable[[str], Awaitable[None]]

INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 30.0
RECONCILE_INTERVAL_SECONDS = 1.0
CERT_SOURCE_PATH = "/run/autonomy-keycache/service-gateway/tls.crt"
KEY_SOURCE_PATH = "/run/autonomy-keycache/service-gateway/tls.key"


def _discover_orgs() -> list[str]:
    from tools.graph import org_ops

    return sorted(ref.slug for ref in org_ops.list_orgs() if ref.type == "shared")


async def _connector_ready(org: str) -> bool:
    from tools.dashboard.link_serving_supervisor import control

    try:
        result = await asyncio.to_thread(
            lambda: control(org, "connector-status", {}, timeout=2.0)
        )
    except Exception:
        return False
    return result.get("ok") is True and result.get("serving") is True


def _desired_hostname_leases() -> dict[str, dict[str, str]]:
    """Return the complete Settings-authorized hostname set per organization.

    A reservation becomes a publication only once it has a target.  Paused
    publications retain their hostname lease so the local gateway can return
    its deliberate 503; released and removed publications are absent.
    """
    desired: dict[str, dict[str, str]] = {}
    for org in _discover_orgs():
        target_ids = {
            row.get("reservation_id")
            for row in service_publication.list_service_targets(org)
            if isinstance(row, dict)
        }
        leases: dict[str, str] = {}
        for row in service_publication.list_reservations(org):
            if (
                isinstance(row, dict)
                and row.get("state") in {"active", "paused"}
                and row.get("reservation_id") in target_ids
            ):
                reservation_id = row["reservation_id"]
                leases[reservation_id] = service_gateway.reservation_hostname(
                    org, reservation_id
                )
        desired[org] = leases
    return desired


class HostnameLeaseReconciler:
    """Converge connector leases from Settings without owning renewal.

    Applied state is only an optimization that prevents a per-publication call
    every watchdog tick.  Settings remain authoritative.  A new connector
    instance invalidates the optimization and receives the complete desired
    set; the connector/registry own renewal after that enrollment.
    """

    def __init__(self, *, control_fn=None, desired_fn=None) -> None:
        self._control = control_fn
        self._desired = desired_fn or _desired_hostname_leases
        self._applied: dict[str, tuple[str, dict[str, str]]] = {}

    def _call(self, org: str, op: str, args: dict) -> dict:
        if self._control is None:
            from tools.dashboard.link_serving_supervisor import control

            return control(org, op, args, timeout=2.0)
        return self._control(org, op, args)

    async def reconcile(self) -> None:
        desired = self._desired()
        for org, leases in desired.items():
            try:
                status = await asyncio.to_thread(
                    self._call, org, "connector-status", {}
                )
            except Exception:
                continue
            marker = status.get("connector_instance")
            if (
                status.get("ok") is not True
                or status.get("serving") is not True
                or not isinstance(marker, str)
                or not marker
            ):
                continue

            previous_marker, previous = self._applied.get(org, ("", {}))
            if previous_marker != marker:
                previous = {}

            next_applied = dict(previous)
            for reservation_id in sorted(set(previous) - set(leases)):
                try:
                    reply = await asyncio.to_thread(
                        self._call,
                        org,
                        "release-host",
                        {"reservation": reservation_id},
                    )
                except Exception:
                    continue
                if reply.get("ok") is True:
                    next_applied.pop(reservation_id, None)

            for reservation_id, host in sorted(leases.items()):
                if previous.get(reservation_id) == host:
                    continue
                try:
                    reply = await asyncio.to_thread(
                        self._call,
                        org,
                        "serve-host",
                        {"reservation": reservation_id, "host": host},
                    )
                except Exception:
                    continue
                if reply.get("ok") is True:
                    next_applied[reservation_id] = host

            # Retain each successful idempotent operation independently so a
            # failure for one publication does not cause calls for every
            # healthy sibling to repeat on the next watchdog tick.
            self._applied[org] = (marker, next_applied)


def _route_fingerprint(value: dict) -> str:
    wire = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(wire).hexdigest()


async def build_desired_state() -> GatewayDesiredState:
    """Derive one complete config, failing closed if authority is unreadable."""
    try:
        return await _build_desired_state()
    except Exception:
        logger.warning(
            "Service gateway authority could not be resolved; stopping routes",
            exc_info=True,
        )
        return GatewayDesiredState(
            caddyfile="", routes=(), ready=False, reason="authority-unavailable"
        )


async def _build_desired_state() -> GatewayDesiredState:
    active_routes: list[service_gateway.ServiceGatewayRoute] = []
    paused_hosts: list[str] = []
    unavailable_hosts: list[str] = []
    certificates: dict[str, tuple[str, str]] = {}
    desired_routes: list[DesiredRoute] = []
    found_publication = False
    found_unready_connector = False
    found_missing_certificate = False

    for org in _discover_orgs():
        reservations = service_publication.list_reservations(org)
        target_ids = {
            row.get("reservation_id")
            for row in service_publication.list_service_targets(org)
            if isinstance(row, dict)
        }
        candidates = sorted(
            (
                row
                for row in reservations
                if isinstance(row, dict)
                and row.get("state") in {"active", "paused"}
                and row.get("reservation_id") in target_ids
            ),
            key=lambda row: row["reservation_id"],
        )
        if not candidates:
            continue
        found_publication = True
        if not await _connector_ready(org):
            found_unready_connector = True
            continue

        for reservation in candidates:
            reservation_id = reservation["reservation_id"]
            persona_label = reservation.get("persona_label")
            pair = service_certificate.active_gateway_pair(org, persona_label)
            if pair is None:
                found_missing_certificate = True
                continue
            if reservation["state"] == "paused":
                try:
                    hostname = service_gateway.reservation_hostname(
                        org, reservation_id
                    )
                except Exception:
                    continue
                paused_hosts.append(hostname)
                certificates[hostname] = pair
                desired_routes.append(
                    DesiredRoute(
                        reservation_id,
                        _route_fingerprint(
                            {"mode": "paused", "hostname": hostname}
                        ),
                    )
                )
                continue

            try:
                route = await service_gateway.resolve_gateway_route(
                    org, reservation_id
                )
            except Exception:
                try:
                    hostname = service_gateway.reservation_hostname(
                        org, reservation_id
                    )
                except Exception:
                    continue
                unavailable_hosts.append(hostname)
                desired_routes.append(
                    DesiredRoute(
                        reservation_id,
                        _route_fingerprint(
                            {"mode": "unavailable", "hostname": hostname}
                        ),
                    )
                )
                continue

            active_routes.append(route)
            certificates[route.hostname] = pair
            desired_routes.append(
                DesiredRoute(
                    reservation_id,
                    _route_fingerprint(
                        {
                            "mode": "active",
                            "hostname": route.hostname,
                            "session_id": route.session_id,
                            "container_id": route.container_id,
                            "network": route.network,
                            "port": route.port,
                            "certificate": pair,
                        }
                    ),
                )
            )

    desired_routes.sort()
    active_routes.sort(key=lambda route: route.reservation_id)
    unavailable_hosts.sort()
    if desired_routes:
        return GatewayDesiredState(
            caddyfile=service_gateway.render_caddyfile(
                active_routes,
                paused_hosts=paused_hosts,
                unavailable_hosts=unavailable_hosts,
                certificates=certificates,
            ),
            routes=tuple(desired_routes),
        )
    reason = (
        "connector-unavailable"
        if found_publication and found_unready_connector
        else (
            "certificate-unavailable"
            if found_publication and found_missing_certificate
            else "no-publications"
        )
    )
    return GatewayDesiredState(caddyfile="", routes=(), ready=False, reason=reason)


class GatewayRuntimeError(RuntimeError):
    """The exact Compose service could not reach the requested state."""


async def _default_runner(
    argv: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class ComposeGatewayRuntime:
    """Bounded control of only the profile-gated Service gateway container."""

    def __init__(
        self,
        *,
        compose_dir: str = "/app",
        project: str = "autonomy",
        runner=None,
        sleep=None,
        now=None,
    ) -> None:
        self._base = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--project-directory",
            # Compose runs inside the Dashboard container.  Host-root paths
            # are valid only for bind sources resolved by the Docker daemon;
            # using one here hides /app/.env and breaks interpolation.
            compose_dir,
            "-f",
            os.path.join(compose_dir, "docker-compose.yml"),
            "--profile",
            "service-gateway",
        ]
        self._runner = runner or _default_runner
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._last_marker: str | None = None

    async def _run(self, argv: list[str], timeout: float = 30.0):
        return await self._runner(argv, timeout)

    @staticmethod
    def _require(result: subprocess.CompletedProcess[str], action: str) -> None:
        if result.returncode != 0:
            raise GatewayRuntimeError(
                f"{action} failed ({result.returncode}): {result.stderr[-1000:]}"
            )

    async def ensure_started(self) -> None:
        result = await self._run(
            [
                *self._base,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "service-gateway",
            ],
            timeout=120.0,
        )
        self._require(result, "start Service gateway")
        deadline = self._now() + 10.0
        while self._now() < deadline:
            if await self.is_healthy():
                return
            await self._sleep(0.1)
        raise GatewayRuntimeError("Service gateway did not become healthy within 10s")

    async def is_healthy(self) -> bool:
        try:
            located = await self._run(
                [*self._base, "ps", "-q", "service-gateway"], timeout=5.0
            )
        except OSError:
            return False
        if located.returncode != 0:
            return False
        container_id = located.stdout.strip()
        if not container_id:
            return False
        inspected = await self._run(
            ["docker", "inspect", container_id], timeout=5.0
        )
        if inspected.returncode != 0:
            return False
        try:
            documents = json.loads(inspected.stdout)
            state = documents[0]["State"]
        except (ValueError, IndexError, KeyError, TypeError):
            return False
        healthy = (
            state.get("Running") is True
            and state.get("Health", {}).get("Status") == "healthy"
        )
        self._last_marker = (
            f"{container_id}:{state.get('StartedAt', '')}:"
            f"{documents[0].get('RestartCount', 0)}"
            if healthy
            else None
        )
        return healthy

    async def instance_marker(self) -> str | None:
        return self._last_marker

    async def stop(self) -> None:
        result = await self._run(
            [*self._base, "rm", "-f", "-s", "service-gateway"], timeout=60.0
        )
        self._require(result, "stop Service gateway")


class GatewayReconcileWorker:
    """Startup/event/watchdog driver around the pure reconciler."""

    def __init__(
        self,
        *,
        supervisor,
        planner=build_desired_state,
        lease_reconciler=None,
    ) -> None:
        self._supervisor = supervisor
        self._planner = planner
        self._lease_reconciler = lease_reconciler or HostnameLeaseReconciler()
        self._task: asyncio.Task | None = None

    @staticmethod
    def event_relevant(topic: str, data: object) -> bool:
        if topic in {"session:registry", "network:serving"}:
            return True
        return (
            topic == "setting.changed"
            and isinstance(data, dict)
            and data.get("set_id")
            in {NAMESPACE_RESERVATION_SET_ID, SERVICE_TARGET_SET_ID}
        )

    async def reconcile_once(self) -> dict:
        # Hostname registration must precede certificate readiness: the
        # registry's persona-label binding is what DNS-01 uses to derive the
        # challenge name on first issuance.
        try:
            await self._lease_reconciler.reconcile()
        except Exception:
            # Lease convergence and local gateway fail-closed behavior are
            # independent.  An unreadable Settings fold must still reach the
            # planner, which removes any previously loaded Caddy routes.
            logger.warning("Hostname lease reconciliation failed", exc_info=True)
        return await self._supervisor.reconcile(await self._planner())

    async def _run(self, event_bus) -> None:
        queue = event_bus.subscribe(client_id="web-gateway-supervisor")
        try:
            await self._reconcile_safely()
            loop = asyncio.get_running_loop()
            watchdog_at = loop.time() + RECONCILE_INTERVAL_SECONDS
            while True:
                try:
                    topic, data, _sequence = await asyncio.wait_for(
                        queue.get(),
                        timeout=max(0.0, watchdog_at - loop.time()),
                    )
                except asyncio.TimeoutError:
                    await self._reconcile_safely()
                    watchdog_at = loop.time() + RECONCILE_INTERVAL_SECONDS
                    continue
                if self.event_relevant(topic, data):
                    await self._reconcile_safely()
                    watchdog_at = loop.time() + RECONCILE_INTERVAL_SECONDS
        finally:
            event_bus.unsubscribe(queue)

    async def _reconcile_safely(self) -> None:
        try:
            await self.reconcile_once()
        except Exception:
            logger.warning("Service gateway reconciliation failed", exc_info=True)

    async def start(self, event_bus) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(event_bus), name="web-gateway-supervisor"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None


class WebGatewaySupervisor:
    """Serialize complete desired-state transitions for one local Caddy."""

    def __init__(
        self,
        *,
        runtime: GatewayRuntime,
        loader: Loader,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._runtime = runtime
        self._loader = loader
        self._lock = asyncio.Lock()
        self._state = "stopped"
        self._reason = "no-publications"
        self._last_error: str | None = None
        self._loaded_config: str | None = None
        self._loaded_routes: tuple[DesiredRoute, ...] = ()
        self._loaded_instance_marker: str | None = None
        self._config_revision = 0
        self._now = now or time.monotonic
        self._failed_signature: tuple | None = None
        self._failure_count = 0
        self._retry_at = 0.0
        self._runtime_observed_stopped = False

    def status(self) -> dict:
        result = {
            "state": self._state,
            "reason": self._reason,
            "advertised_routes": sorted(
                route.route_id for route in self._loaded_routes
            ),
            "config_revision": self._config_revision,
        }
        if self._last_error is not None:
            result["error"] = self._last_error
        return result

    async def reconcile(self, desired: GatewayDesiredState) -> dict:
        async with self._lock:
            if not desired.routes or not desired.ready:
                return await self._stop(
                    desired.reason
                    or ("no-publications" if not desired.routes else "not-ready")
                )

            signature = (desired.caddyfile, desired.routes, desired.ready)
            if signature != self._failed_signature:
                self._failure_count = 0
                self._retry_at = 0.0
            elif self._now() < self._retry_at:
                self._state = "backoff"
                self._reason = "backoff"
                return self.status()

            self._runtime_observed_stopped = False
            try:
                healthy = await self._runtime.is_healthy()
            except Exception as exc:
                return await self._fail(desired, exc)
            marker = await self._runtime.instance_marker() if healthy else None
            if healthy and marker != self._loaded_instance_marker:
                # Caddy can restart in the same container under
                # `restart: unless-stopped`. Its bootstrap is healthy but has
                # none of the dynamic routes loaded into the prior process.
                self._loaded_config = None
                self._loaded_routes = ()
                self._loaded_instance_marker = None
            unchanged = (
                healthy
                and marker is not None
                and self._loaded_instance_marker == marker
                and self._loaded_config == desired.caddyfile
                and self._loaded_routes == desired.routes
            )
            if unchanged:
                self._state = "healthy"
                self._reason = "ready"
                self._last_error = None
                return self.status()

            if not healthy:
                # A dead process cannot be serving the previously loaded
                # routes. Clear the advertisement before attempting recovery.
                self._loaded_config = None
                self._loaded_routes = ()
                self._loaded_instance_marker = None
                self._state = "starting"
                self._reason = "starting"
                try:
                    await self._runtime.ensure_started()
                    healthy = await self._runtime.is_healthy()
                    marker = (
                        await self._runtime.instance_marker() if healthy else None
                    )
                except Exception as exc:
                    return await self._fail(desired, exc)
                if not healthy:
                    return await self._fail(
                        desired, RuntimeError("gateway did not become healthy")
                    )

            self._state = "loading"
            self._reason = "loading"
            try:
                await self._loader(desired.caddyfile)
            except Exception as exc:
                return await self._fail(desired, exc)

            # Advertisement is an output of a verified load, never an input to
            # it and never visible during starting/loading.
            self._loaded_config = desired.caddyfile
            self._loaded_routes = desired.routes
            self._loaded_instance_marker = marker
            self._config_revision += 1
            self._state = "healthy"
            self._reason = "ready"
            self._last_error = None
            self._failed_signature = None
            self._failure_count = 0
            self._retry_at = 0.0
            return self.status()

    async def _stop(self, reason: str) -> dict:
        running = bool(self._loaded_routes)
        if not running and not self._runtime_observed_stopped:
            running = await self._runtime.is_healthy()
        if running:
            self._state = "draining"
            self._reason = reason
            await self._runtime.stop()
        self._runtime_observed_stopped = True
        self._loaded_config = None
        self._loaded_routes = ()
        self._loaded_instance_marker = None
        self._state = "stopped"
        self._reason = reason
        self._last_error = None
        self._failed_signature = None
        self._failure_count = 0
        self._retry_at = 0.0
        return self.status()

    async def _fail(self, desired: GatewayDesiredState, exc: Exception) -> dict:
        self._state = "failed"
        self._reason = "load-failed"
        self._last_error = str(exc)
        signature = (desired.caddyfile, desired.routes, desired.ready)
        if signature != self._failed_signature:
            self._failure_count = 0
        self._failed_signature = signature
        self._failure_count += 1
        delay = min(
            INITIAL_BACKOFF_SECONDS * (2 ** (self._failure_count - 1)),
            MAX_BACKOFF_SECONDS,
        )
        self._retry_at = self._now() + delay

        desired_by_id = {
            route.route_id: route.fingerprint for route in desired.routes
        }
        old_still_authorized = all(
            desired_by_id.get(route.route_id) == route.fingerprint
            for route in self._loaded_routes
        )
        if not old_still_authorized:
            await self._runtime.stop()
            self._runtime_observed_stopped = True
            self._loaded_config = None
            self._loaded_routes = ()
            self._loaded_instance_marker = None
        return self.status()


async def _load_complete_config(caddyfile: str) -> None:
    await asyncio.to_thread(service_gateway.load_caddyfile, caddyfile)


_runtime = ComposeGatewayRuntime()
_supervisor = WebGatewaySupervisor(
    runtime=_runtime,
    loader=_load_complete_config,
)
_worker = GatewayReconcileWorker(supervisor=_supervisor)


def status() -> dict:
    """Return process-local observed state; durable rows remain authoritative."""
    return _supervisor.status()


async def start_worker(event_bus) -> None:
    await _worker.start(event_bus)


async def stop_worker() -> None:
    # Deliberately leave a healthy Caddy running across a Dashboard hot reload.
    # The next process reconstructs and atomically reloads durable desired state.
    await _worker.stop()
