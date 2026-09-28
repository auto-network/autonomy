"""Browser broker caller routes (auto-czoc0; design graph://c330323d-986).

``POST /api/browser/leases``, ``GET /api/browser/leases/{lease}`` and
``DELETE /api/browser/leases/{lease}``, authorized by the session token and
the workspace's ``browser`` capability. Organization, workspace and session
come only from the authenticated caller; a lease owned by another session
answers 404, like one that does not exist.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import threading
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import browser_containers as containers
from tools.dashboard import browser_reconciler as reconciler
from tools.dashboard.capability_gate import CapabilityRefused, require_capability
from tools.dashboard.dao import browser_leases as store

ADAPTERS = ("chrome-headed",)
_LEASE_RE = re.compile(r"^brl_[0-9a-f]{32}$")


class _Reply(Exception):
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload


def _scope(authorization):
    try:
        return require_capability(authorization, "browser")
    except CapabilityRefused as exc:
        raise _Reply(exc.status, {"error": exc.detail}) from exc


def _epoch() -> int:
    epoch = reconciler.epoch()
    if epoch is None:
        raise _Reply(503, {"error": "unavailable", "reason": "broker-starting"})
    return epoch


def parse_request(body) -> tuple[str, str, str | None, int | None]:
    """``(adapter, profile_kind, profile_name, ttl_s)`` or a 400 reply."""
    if not isinstance(body, dict) or set(body) - {"adapter", "profile", "ttl_s"}:
        raise _Reply(400, {"error": "body must be {adapter, profile, ttl_s?}"})
    adapter = body.get("adapter")
    if adapter == "agent-headless":
        raise _Reply(400, {"error": "adapter-unavailable",
                           "detail": "agent-headless leases are not offered yet; use agent-browser"})
    if adapter not in ADAPTERS:
        raise _Reply(400, {"error": f"adapter must be one of {list(ADAPTERS)}"})
    profile = body.get("profile")
    if profile == {"kind": "ephemeral"}:
        kind, name = "ephemeral", None
    elif isinstance(profile, dict) and set(profile) == {"kind", "store"} \
            and profile["kind"] == "persistent" and isinstance(profile["store"], str):
        kind, name = "persistent", profile["store"]
    else:
        raise _Reply(400, {"error": "profile must be {kind: ephemeral} or {kind: persistent, store}"})
    ttl = body.get("ttl_s")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 60):
        raise _Reply(400, {"error": "ttl_s must be an integer of at least 60"})
    return adapter, kind, name, ttl


def _free_gib() -> float:
    from tools.data_paths import resolve_store

    path = resolve_store("browser_profiles")
    while not path.exists() and path != path.parent:
        path = path.parent
    return containers.free_gib(path)


def create_lease(authorization, body) -> tuple[int, dict]:
    scope = _scope(authorization)
    adapter, kind, name, ttl = parse_request(body)
    epoch = _epoch()
    if not reconciler.isolation_checked():
        # A worker that just activated has not checked isolation yet: a reload,
        # not an isolation failure.
        raise _Reply(503, {"error": "unavailable", "reason": "broker-starting"})
    if not reconciler.isolated():
        raise _Reply(503, {"error": "unavailable", "reason": "isolation"})
    limits = reconciler.defaults()
    if kind == "persistent":
        from tools.browser_broker.profiles import profile_container_name, profile_path
        try:
            path = profile_path(scope.org, scope.workspace, name)
        except ValueError as exc:
            raise _Reply(400, {"error": str(exc)}) from exc
        container_name = profile_container_name(scope.org, scope.workspace, name)
    else:
        path, container_name = None, "brw-e-" + secrets.token_hex(8)
    try:
        if _free_gib() < limits["min_free_gib"]:
            raise _Reply(503, {"error": "admission", "reason": "disk"})
    except OSError as exc:
        raise _Reply(503, {"error": "admission", "reason": "disk"}) from exc

    limit = limits["persistent_ttl_s" if kind == "persistent" else "ephemeral_ttl_s"]
    expires_at = time.time() + min(ttl or limit, limit)
    lease_id = store.new_lease_id()
    lease_hash = store.lease_hash(lease_id)
    secret, vnc_password = store.new_lease_secret(), store.new_vnc_password()
    try:
        store.admit(epoch=epoch, max_leases=limits["max_leases"], lease_hash_=lease_hash,
                    session=scope.session, org=scope.org, workspace=scope.workspace,
                    profile_kind=kind, profile_name=name, adapter=adapter,
                    container_name=container_name, expires_at=expires_at,
                    secret=secret, vnc_password=vnc_password)
    except store.AdmissionRefused as exc:
        raise _Reply(503, {"error": "admission", "reason": exc.reason}) from exc
    except store.StaleEpoch as exc:
        raise _Reply(503, {"error": "unavailable", "reason": "broker-starting"}) from exc

    try:
        mount_argv: list[str] = []
        if path is not None:
            containers.prepare_profile_dir(path)
            mount_argv = containers.profile_mount_argv(scope.org, scope.workspace, name)
        argv = containers.create_args(
            name=container_name,
            lease_labels=containers.labels(
                lease_hash=lease_hash, session=scope.session, org=scope.org,
                workspace=scope.workspace, profile=f"{kind}:{name or ''}", adapter=adapter,
                expires_at=expires_at),
            caps=containers.Caps(limits["memory_mb"], limits["cpus"], limits["pids"]),
            mount_argv=mount_argv, timezone=os.environ.get("TZ"))
        address = containers.create_and_start(
            argv=argv, secret=secret, vnc_password=vnc_password, expires_at=expires_at,
            name=container_name,
            before_start=(lambda: containers.profile_integrity(path)) if path else None)
    except containers.NameConflict:
        store.delete_requested(lease_hash, epoch=epoch)
        raise _Reply(409, {"error": "profile-busy"}) from None
    except containers.ProfileDamaged as exc:
        store.transition(lease_hash, epoch=epoch, to="failed", audit_op="start",
                         result="profile-damaged", diagnostic=str(exc))
        return 201, {"lease": lease_id, "state": "failed", "adapter": adapter,
                     "expires_at": expires_at, "diagnostic": str(exc)}
    except containers.DockerUnavailable as exc:
        store.transition(lease_hash, epoch=epoch, to="failed", audit_op="start",
                         result="docker-unavailable", diagnostic="docker unavailable")
        raise _Reply(503, {"error": "unavailable", "reason": "docker"}) from exc
    except Exception:
        store.transition(lease_hash, epoch=epoch, to="failed", audit_op="start",
                         result="error", diagnostic="container start failed")
        raise

    store.transition(lease_hash, epoch=epoch, to="starting", expect=("requested",),
                     audit_op="start", address=address)
    threading.Thread(target=reconciler.wait_ready, args=(lease_hash, epoch), daemon=True,
                     name=f"browser-start-{container_name}").start()
    return 201, {"lease": lease_id, "state": "starting", "adapter": adapter,
                 "expires_at": expires_at}


def _owned(authorization, lease_id: str) -> store.Lease:
    scope = _scope(authorization)
    lease = store.get(store.lease_hash(lease_id)) if _LEASE_RE.fullmatch(lease_id) else None
    if lease is None or lease.session != scope.session:
        raise _Reply(404, {"error": "not found"})
    return lease


def lease_status(authorization, lease_id: str) -> tuple[int, dict]:
    lease = _owned(authorization, lease_id)
    running = lease.state in ("starting", "ready", "busy", "locked", "unhealthy")
    usage = containers.stats(lease.container_name) if running else {"cpu": None, "mem_mb": None}
    if lease.state in ("ready", "busy", "locked"):
        health = "ok" if lease.health_failures == 0 else "failing"
    else:
        health = "unhealthy" if lease.state == "unhealthy" else "unknown"
    payload = {"state": lease.state, "health": health, "adapter": lease.adapter,
               "age_s": round(time.time() - lease.created_at), **usage,
               "expires_at": lease.expires_at}
    if lease.diagnostic:
        payload["diagnostic"] = lease.diagnostic
    return 200, payload


def release_lease(authorization, lease_id: str) -> tuple[int, dict]:
    lease = _owned(authorization, lease_id)
    if lease.state in store.FINAL_STATES:
        return 200, {"state": lease.state}
    epoch = _epoch()
    if lease.state != "releasing":
        store.transition(lease.lease_hash, epoch=epoch, to="releasing", audit_op="release",
                         result="caller")
    fresh = store.get(lease.lease_hash)
    reconciler.release_async(fresh, "caller")
    return 200, {"state": "releasing"}


def run_command(authorization, lease_id: str, body) -> tuple[int, dict]:
    """Forward one structured command from the lease's owner to its agent.

    404 for another session's lease (nothing reaches the agent), 400 for an
    operation outside the list, 409 while the lease is locked or busy. The
    busy state is taken by compare-and-set; if take-control lands while the
    command runs, its result is discarded and the caller gets 409. The audit
    trail records the operation and a result category, never page content."""
    from tools.browser_broker.lease_agent import BadRequest, validate_command

    lease = _owned(authorization, lease_id)
    try:
        op, checked = validate_command(body)
    except BadRequest as exc:
        raise _Reply(400, {"error": str(exc)}) from exc
    epoch = _epoch()
    refusal = _command_refusal(lease)
    if refusal:
        raise _Reply(409, refusal)
    if not store.transition(lease.lease_hash, epoch=epoch, to="busy", expect=("ready",),
                            last_activity=time.time()):
        fresh = store.get(lease.lease_hash)
        raise _Reply(409, _command_refusal(fresh) or {"error": "busy"})
    status, reply, category = 502, {"error": "lease agent unreachable"}, "error"
    try:
        # The raw args that passed validate_command are forwarded (its parsed
        # form is internal); the agent re-validates with the same function.
        status, reply = containers.agent_request(
            lease.address, lease.secret, "POST", "/command",
            {"op": op, "args": (body or {}).get("args", {})},
            timeout=checked["timeout_ms"] / 1000 + 35)
        if status == 200 and reply.get("ok"):
            category = "ok"
        elif reply.get("error") in ("aborted", "locked"):
            category = "aborted"
    except Exception:
        logger.warning("browser command %s to %s failed", op, lease.container_name, exc_info=True)
    finally:
        # busy -> ready unless take-control moved the lease on meanwhile.
        store.transition(lease.lease_hash, epoch=epoch, to="ready", expect=("busy",),
                         audit_op=f"command:{op}", result=category, last_activity=time.time())
    after = store.get(lease.lease_hash)
    if after is not None and after.state == "locked":
        raise _Reply(409, {"error": "locked", "holder": after.lock_holder or "privileged"})
    if category == "aborted":
        # Stopped, but nobody holds the lease: the broker freed it (a dead
        # request's lease was reclaimed); do not claim a take-control.
        raise _Reply(409, {"error": "interrupted"})
    if status == 409:
        raise _Reply(409, {"error": reply.get("error", "busy")})
    if 400 <= status < 500:
        # A secret or validation disagreement between broker and agent is a
        # broker fault, not the caller's outcome.
        raise _Reply(502, {"error": "lease agent refused"})
    if status >= 500:
        raise _Reply(502, {"error": "lease agent unreachable"})
    return 200, {"ok": bool(reply.get("ok")), **({"result": reply["result"]} if "result" in reply
                                                 else {"error": reply.get("error")})}


def _command_refusal(lease) -> dict | None:
    if lease is None or lease.state in store.FINAL_STATES or lease.state == "releasing":
        return {"error": "gone"}
    if lease.state == "locked":
        return {"error": "locked", "holder": lease.lock_holder or "privileged"}
    if lease.state == "busy":
        return {"error": "busy"}
    if lease.state != "ready":
        return {"error": "not-ready", "state": lease.state}
    return None


async def post_command(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    return await _serve(run_command, request.headers.get("Authorization"),
                        request.path_params["lease"], body)


async def _serve(fn, *args) -> JSONResponse:
    try:
        status, payload = await asyncio.to_thread(fn, *args)
    except _Reply as reply:
        status, payload = reply.status, reply.payload
    return JSONResponse(payload, status_code=status)


async def post_lease(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    return await _serve(create_lease, request.headers.get("Authorization"), body)


async def get_lease(request: Request) -> JSONResponse:
    return await _serve(lease_status, request.headers.get("Authorization"),
                        request.path_params["lease"])


async def delete_lease(request: Request) -> JSONResponse:
    return await _serve(release_lease, request.headers.get("Authorization"),
                        request.path_params["lease"])


ROUTES = [
    Route("/api/browser/leases", post_lease, methods=["POST"]),
    Route("/api/browser/leases/{lease}", get_lease, methods=["GET"]),
    Route("/api/browser/leases/{lease}", delete_lease, methods=["DELETE"]),
    Route("/api/browser/leases/{lease}/commands", post_command, methods=["POST"]),
]
