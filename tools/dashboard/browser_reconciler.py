"""Browser lease reconciler (auto-czoc0; design graph://c330323d-986).

Runs in the activated dashboard worker (one per machine). On activation it
takes a new epoch, fencing off the previous worker's writes, attaches the
dashboard to the lease network and adopts every running lease container. Then
every :data:`TICK_S` seconds it:

- health-checks leases through their agents: the first healthy answer makes a
  starting lease ready, and two failures one tick apart release a lease;
- releases leases whose owning session ended, whose idle limit passed, whose
  workspace no longer enables ``browser``, or whose time limit passed (the
  in-container watchdog normally ends those first);
- every second tick, compares Docker with the records: a container with no
  active record is stopped, and a record whose container is gone is closed.

Lease containers are separate from the dashboard, so worker reloads, crashes
and container restarts leave them running; only these rules end them.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from tools.dashboard import browser_containers as containers
from tools.dashboard.dao import browser_leases as store

logger = logging.getLogger(__name__)

TICK_S = 5.0
START_TIMEOUT_S = 90.0
EXPIRY_GRACE_S = 15.0
#: The longest a command can legitimately hold a lease busy: the lease
#: agent's largest command timeout plus the route's margin, plus 15 s so a
#: live maximum-length command is never reclaimed while its route still
#: waits (last_activity is stamped just before that wait starts). A busy row
#: older than this belongs to a request that died (a worker reload or crash
#: while the command ran, or a stuck agent).
BUSY_LIMIT_S = 60.0 + 35.0 + 15.0

_epoch: Optional[int] = None
_tick = 0
_isolated = False
_isolation_checked = False
PAUSE_MESSAGE = "browser broker paused: network redesign (bead follows)"


def isolation_checked() -> bool:
    """Whether this worker has checked the isolation policy at least once;
    until then a lease request answers broker-starting, never isolation."""
    return _isolation_checked


def isolated() -> bool:
    """Whether leases may start. False while the broker is paused for the
    network redesign (auto-8c2df): lease requests answer 503 isolation."""
    return _isolated


def _ensure_isolation() -> None:
    """Paused: no firewall rules and no network attachment until the redesign.
    Logged once per activation, never retried."""
    global _isolated, _isolation_checked
    _isolated = False
    _isolation_checked = True
    logger.warning(PAUSE_MESSAGE)


def epoch() -> Optional[int]:
    """This worker's epoch, or None before activation (writes are refused)."""
    return _epoch


def defaults() -> dict:
    from tools.graph import ops as graph_ops
    from tools.graph.schemas import browser_defaults

    try:
        row = graph_ops.read_set_key(browser_defaults.SET_ID, "default", org="machine", peers=[])
    except Exception:
        row = None
    return browser_defaults.resolved((row or {}).get("payload"))


# ── lifecycle actions ──────────────────────────────────────────────────


def release(lease: store.Lease, reason: str, *, epoch_: int) -> bool:
    """Move a lease to releasing, stop its container, and close the record."""
    if lease.state != "releasing":
        if lease.state in ("ready", "busy", "locked", "starting", "unhealthy"):
            if not store.transition(lease.lease_hash, epoch=epoch_, to="releasing",
                                    audit_op="release", result=reason):
                return False
        elif lease.state == "requested":
            store.transition(lease.lease_hash, epoch=epoch_, to="failed",
                             audit_op="release", result=reason, diagnostic=reason)
            return store.transition(lease.lease_hash, epoch=epoch_, to="gone")
    containers.stop(lease.container_name, lease_hash=lease.lease_hash)
    return store.transition(lease.lease_hash, epoch=epoch_, to="gone", audit_op="gone", result=reason)


def release_async(lease: store.Lease, reason: str) -> None:
    epoch_ = _epoch
    if epoch_ is None:
        return
    threading.Thread(target=_release_logged, args=(lease, reason, epoch_), daemon=True,
                     name=f"browser-release-{lease.container_name}").start()


def _free_interrupted(lease: store.Lease, epoch_: int) -> None:
    """A lease left busy by a request that died: stop whatever the agent is
    still doing, then make the lease usable again."""
    if lease.address:
        try:
            containers.agent_request(lease.address, lease.secret, "POST", "/abort", {}, timeout=3)
        except Exception:
            logger.warning("browser broker: abort of interrupted command failed for %s",
                           lease.container_name)
    store.transition(lease.lease_hash, epoch=epoch_, to="ready", expect=("busy",),
                     audit_op="command:interrupted", result="freed", last_activity=time.time())


def _release_isolated(lease: store.Lease, reason: str, epoch_: int) -> None:
    """Release one lease inside a reconciler pass without letting it stall or
    abort the pass for every other lease; it stays releasing and is retried."""
    try:
        release(lease, reason, epoch_=epoch_)
    except Exception:
        logger.exception("browser broker: releasing %s failed; retrying next tick",
                         lease.container_name)


def _release_logged(lease: store.Lease, reason: str, epoch_: int) -> None:
    try:
        release(lease, reason, epoch_=epoch_)
    except Exception:
        logger.exception("browser lease release failed; the reconciler will retry")


def wait_ready(lease_hash: str, epoch_: int, timeout_s: float = START_TIMEOUT_S) -> None:
    """Poll a starting lease's agent until healthy (start-time path)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        lease = store.get(lease_hash)
        if lease is None or lease.state != "starting" or not lease.address:
            return
        if _healthy(lease):
            store.transition(lease_hash, epoch=epoch_, to="ready", expect=("starting",),
                             audit_op="ready", last_health_at=time.time(), health_failures=0)
            return
        time.sleep(0.5)


def _healthy(lease: store.Lease) -> bool:
    try:
        status, body = containers.agent_request(lease.address, lease.secret, "GET", "/health",
                                                timeout=3)
    except Exception:
        return False
    return status == 200 and body.get("healthy") is True


# ── activation and the loop ────────────────────────────────────────────


def activate() -> int:
    """Take a new epoch, join the lease network and adopt running leases.

    The epoch is published only when every step succeeded: a Docker error
    part-way (now raised, auto-y8o21) leaves this worker unactivated, so the
    loop retries the whole activation instead of running with an epoch whose
    leases were never adopted."""
    global _epoch
    epoch_ = store.take_epoch()
    _ensure_isolation()
    rows = {lease.lease_hash: lease for lease in store.list_leases()}
    adopted = orphans = 0
    for container in containers.list_containers():
        lease = rows.get(container.lease_hash)
        if lease is not None and container.state == "running":
            adopted += store.adopt(lease.lease_hash, epoch=epoch_)
        elif lease is None:
            containers.stop(container.name, lease_hash=container.lease_hash)
            orphans += 1
    _epoch = epoch_
    logger.info("browser broker: epoch %d, adopted %d lease(s), stopped %d orphan container(s)",
                _epoch, adopted, orphans)
    return _epoch


def reconcile_once(now: Optional[float] = None) -> None:
    global _tick
    epoch_ = _epoch
    if epoch_ is None:
        return
    if store.current_epoch() != epoch_:
        return  # a newer worker owns the leases
    now = now or time.time()
    _tick += 1
    if _tick % 2 == 1:
        _docker_pass(epoch_, now)
    leases = store.list_leases()
    if not leases:
        return
    limits = defaults()
    capability_cache: dict[tuple[str, str], bool] = {}
    for lease in leases:
        reason = _end_reason(lease, now, limits, capability_cache)
        if reason:
            _release_isolated(lease, reason, epoch_)
            continue
        if lease.state == "busy" and now - lease.last_activity >= BUSY_LIMIT_S:
            _free_interrupted(lease, epoch_)
            continue
        if lease.state in ("starting", "ready", "busy", "locked", "unhealthy") and lease.address:
            _health_pass(lease, epoch_, now)


def _docker_pass(epoch_: int, now: float) -> None:
    listed = {c.lease_hash: c for c in containers.list_containers()}
    rows = {lease.lease_hash: lease for lease in store.list_leases()}
    for lease_hash, container in listed.items():
        if lease_hash not in rows:
            logger.warning("browser broker: stopping %s, which has no lease record", container.name)
            containers.stop(container.name, lease_hash=container.lease_hash)
    for lease in rows.values():
        container = listed.get(lease.lease_hash)
        if container is not None and container.state in ("running", "created"):
            continue
        if lease.state == "requested" and now - lease.created_at < START_TIMEOUT_S:
            continue  # its container is being created right now
        _release_isolated(lease, "container-gone", epoch_)


def _end_reason(lease: store.Lease, now: float, limits: dict, capability_cache: dict) -> Optional[str]:
    if lease.state == "releasing":
        return "releasing"
    if now >= lease.expires_at + EXPIRY_GRACE_S:
        return "time-limit"
    if now - lease.last_activity >= limits["idle_s"]:
        return "idle"
    if lease.state == "starting" and now - lease.created_at >= START_TIMEOUT_S:
        return "start-timeout"
    from tools.dashboard.dao import dashboard_db

    if not dashboard_db.is_session_live(lease.session):
        return "session-ended"
    key = (lease.org, lease.workspace)
    if key not in capability_cache:
        from tools.dashboard.capability_gate import capability_enabled

        try:
            capability_cache[key] = capability_enabled(lease.org, lease.workspace, "browser")
        except Exception:
            capability_cache[key] = True  # unreadable is not revoked
    if not capability_cache[key]:
        return "capability-revoked"
    return None


def _health_pass(lease: store.Lease, epoch_: int, now: float) -> None:
    if _healthy(lease):
        if lease.state == "starting":
            store.transition(lease.lease_hash, epoch=epoch_, to="ready", expect=("starting",),
                             audit_op="ready", last_health_at=now, health_failures=0)
        elif lease.health_failures:
            store.update(lease.lease_hash, epoch=epoch_, health_failures=0, last_health_at=now)
        return
    if lease.state == "starting":
        return  # still booting; the start timeout ends it
    failures = lease.health_failures + 1
    store.update(lease.lease_hash, epoch=epoch_, health_failures=failures, last_health_at=now)
    if failures >= 2:
        if lease.state == "ready":
            store.transition(lease.lease_hash, epoch=epoch_, to="unhealthy", expect=("ready",),
                             audit_op="health", result="unhealthy")
        fresh = store.get(lease.lease_hash)
        if fresh is not None:
            _release_isolated(fresh, "unhealthy", epoch_)


async def run_forever() -> None:
    """The dashboard's background task (started at worker activation)."""
    import asyncio

    try:
        await asyncio.to_thread(activate)
    except Exception:
        logger.exception("browser broker: activation failed; retrying in the loop")
    backoff = TICK_S
    while True:
        try:
            if _epoch is None:
                await asyncio.to_thread(activate)
            await asyncio.to_thread(reconcile_once)
            backoff = TICK_S
        except asyncio.CancelledError:
            raise
        except containers.DockerUnavailable:
            backoff = min(backoff * 2, 60.0)
            logger.warning("browser broker: docker unavailable; next attempt in %.0fs", backoff)
        except Exception:
            logger.exception("browser broker: reconcile failed; retrying next tick")
        await asyncio.sleep(backoff)
