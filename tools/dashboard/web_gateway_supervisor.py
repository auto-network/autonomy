"""Desired-state lifecycle for the Compose-local Service gateway.

The durable publication and certificate stores remain authoritative.  This
module owns only runtime convergence: start the dormant Caddy service, load one
complete config atomically, report routes only after that load succeeds, and
stop the service when nothing remains authorized.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


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

    async def stop(self) -> None: ...


Loader = Callable[[str], Awaitable[None]]

INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 30.0


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
        self._now = now or time.monotonic
        self._failed_signature: tuple | None = None
        self._failure_count = 0
        self._retry_at = 0.0

    def status(self) -> dict:
        result = {
            "state": self._state,
            "reason": self._reason,
            "advertised_routes": sorted(
                route.route_id for route in self._loaded_routes
            ),
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
                self._state = "failed"
                self._reason = "backoff"
                return self.status()

            healthy = await self._runtime.is_healthy()
            unchanged = (
                healthy
                and self._loaded_config == desired.caddyfile
                and self._loaded_routes == desired.routes
            )
            if unchanged:
                self._state = "healthy"
                self._reason = "ready"
                self._last_error = None
                return self.status()

            if not healthy:
                self._state = "starting"
                self._reason = "starting"
                await self._runtime.ensure_started()
                healthy = await self._runtime.is_healthy()
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
            self._state = "healthy"
            self._reason = "ready"
            self._last_error = None
            self._failed_signature = None
            self._failure_count = 0
            self._retry_at = 0.0
            return self.status()

    async def _stop(self, reason: str) -> dict:
        running = bool(self._loaded_routes) or await self._runtime.is_healthy()
        if running:
            self._state = "draining"
            self._reason = reason
            await self._runtime.stop()
        self._loaded_config = None
        self._loaded_routes = ()
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
            self._loaded_config = None
            self._loaded_routes = ()
        return self.status()
