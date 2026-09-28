"""Browser broker caller routes (auto-czoc0; design graph://c330323d-986).

``POST /api/browser/leases``, ``GET /api/browser/leases/{lease}`` and
``DELETE /api/browser/leases/{lease}``, authorized by the session token and
the workspace's ``browser`` capability. Organization, workspace and session
come only from the authenticated caller; a lease owned by another session
answers 404, like one that does not exist.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import struct
import threading
import time
from pathlib import Path

from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import browser_containers as containers
from tools.dashboard import browser_reconciler as reconciler
from tools.dashboard.capability_gate import CapabilityRefused, require_capability
from tools.dashboard.dao import browser_leases as store

logger = logging.getLogger(__name__)

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


# ── password sign-in (auto-8q7oe.9) ────────────────────────────────────

_TARGET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
#: Credentials are decrypted here, never on a request thread.
_SECRETS = None


def _secrets_pool():
    global _SECRETS
    if _SECRETS is None:
        from concurrent.futures import ThreadPoolExecutor
        _SECRETS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="browser-secrets")
    return _SECRETS


def _stored_origin(org: str, target_key: str) -> Optional[str]:
    """The credential's stored origin, read without decrypting anything."""
    from tools.connectors.repl_login import SECURE_SETTING_SET_ID, SECURE_SETTING_V2_REVISION
    from tools.graph import ops as graph_ops

    members = graph_ops.read_set(SECURE_SETTING_SET_ID, org=org, peers=[],
                                 min_revision=SECURE_SETTING_V2_REVISION)
    match = next((m for m in members.members if m.key == target_key), None)
    return (match.payload or {}).get("origin") if match is not None else None


def _decrypt(org: str, workspace: str, target_key: str, stored_origin: str) -> dict:
    from tools.connectors.repl_login import load_credentials
    from tools.data_paths import REPO_ROOT, resolve_store

    return load_credentials(autonomy_root=REPO_ROOT, key_file=resolve_store("repl_login_key"),
                            org=org, target_key=target_key, expected_origin=stored_origin,
                            caller_workspace=workspace)


def secure_login(authorization, lease_id: str, body) -> tuple[int, dict]:
    """Sign in with a stored credential the caller never sees (the design's
    seven steps: lock, check the page, decrypt, type, wait, clean up, unlock)."""
    from tools.browser_broker.lease_agent import exact_origin
    from tools.dashboard.capability_gate import capability_enabled

    scope = _scope(authorization)
    if not capability_enabled(scope.org, scope.workspace, "repl_login"):
        raise _Reply(403, {"error": f"workspace {scope.workspace!r} does not enable repl_login"})
    lease = store.get(store.lease_hash(lease_id)) if _LEASE_RE.fullmatch(lease_id) else None
    if lease is None or lease.session != scope.session:
        raise _Reply(404, {"error": "not found"})
    if not isinstance(body, dict) or set(body) - {"target_key", "fields", "submit", "success_text"}:
        raise _Reply(400, {"error": "body is {target_key, fields, submit, success_text?}"})
    target_key = body.get("target_key")
    if not isinstance(target_key, str) or not _TARGET_KEY_RE.fullmatch(target_key):
        raise _Reply(400, {"error": "target_key is invalid"})
    epoch = _epoch()
    stored = _stored_origin(scope.org, target_key)
    origin = exact_origin(stored or "")
    if stored is None:
        return 200, {"authenticated": False, "human_required": False, "reason": "not-provisioned"}
    if origin is None or origin[0] != "https":
        return 200, {"authenticated": False, "human_required": False, "reason": "origin-not-https"}
    page_work = {"origin": f"https://{origin[1]}:{origin[2]}", "fields": body.get("fields"),
                 "submit": body.get("submit"), "success_text": body.get("success_text") or []}
    from tools.browser_broker.lease_agent import BadRequest, parse_login_request
    try:
        parse_login_request(page_work)
    except BadRequest as exc:
        raise _Reply(400, {"error": str(exc)}) from exc

    # 1. Lock against the caller (row, then the agent; fail closed).
    refusal = _command_refusal(lease)
    if refusal:
        raise _Reply(409, refusal)
    if not store.transition(lease.lease_hash, epoch=epoch, to="locked", expect=("ready",),
                            lock_holder="privileged", last_activity=time.time()):
        raise _Reply(409, _command_refusal(store.get(lease.lease_hash)) or {"error": "busy"})
    handed_over = False
    agent_locked = False
    reason = "error"
    try:
        agent_locked = containers.agent_lock(lease, True)
        if not agent_locked:
            reason = "agent-lock-failed"
            return 502, {"authenticated": False, "human_required": False, "reason": reason}
        # 2. Check the page before any secret exists.
        status, checked = containers.agent_request(lease.address, lease.secret, "POST",
                                                   "/login/check", page_work, timeout=35)
        if status != 200 or not checked.get("ok"):
            reason = checked.get("reason", "check-failed") if status == 200 else "agent-error"
            return 200, {"authenticated": False, "human_required": False, "reason": reason}
        # 3. Decrypt in the secrets pool; the plaintext never touches this frame's
        #    logs, responses or audit rows.
        try:
            credentials = _secrets_pool().submit(
                _decrypt, scope.org, scope.workspace, target_key, stored).result(timeout=30)
        except Exception as exc:
            reason = "credential-unavailable"
            logger.warning("secure-login: credential %s unavailable for %s: %s",
                           target_key, scope.workspace, type(exc).__name__)
            return 200, {"authenticated": False, "human_required": False, "reason": reason}
        # 4-6. Type, wait and clean up in the lease agent.
        try:
            status, outcome = containers.agent_request(
                lease.address, lease.secret, "POST", "/login/submit",
                {**page_work, "credentials": credentials}, timeout=95)
        finally:
            credentials.clear()
        if status != 200:
            reason = "agent-error"
            return 502, {"authenticated": False, "human_required": False, "reason": reason}
        reason = str(outcome.get("reason", "unknown"))[:40]
        handed_over = bool(outcome.get("human_required"))
        return 200, {"authenticated": bool(outcome.get("authenticated")),
                     "human_required": handed_over, "reason": reason}
    finally:
        # 7. Unlock — or, on a verification-code request, pass to the operator
        #    (still locked against the caller; cleanup runs on their return).
        if handed_over:
            store.transition(lease.lease_hash, epoch=epoch, to="locked", expect=("locked",),
                             lock_holder="human", audit_op="secure-login", result=reason)
        elif not agent_locked or containers.agent_lock(lease, False):
            # (An agent that never confirmed the lock has nothing to unlock.)
            store.transition(lease.lease_hash, epoch=epoch, to="ready", expect=("locked",),
                             lock_holder=None, audit_op="secure-login", result=reason,
                             last_activity=time.time())
        else:
            # The agent may still be locked: keep the row locked (privileged) so
            # the reconciler's reclaim retries, rather than showing a false ready.
            store.transition(lease.lease_hash, epoch=epoch, to="locked", expect=("locked",),
                             audit_op="secure-login", result=f"{reason}:unlock-unconfirmed")


async def post_secure_login(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    return await _serve(secure_login, request.headers.get("Authorization"),
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


# ── operator side (auto-8q7oe.7) ───────────────────────────────────────

from starlette.routing import WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocket, WebSocketDisconnect  # noqa: E402

from tools.dashboard import browser_viewer as viewer  # noqa: E402

_VIEWER_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _operator_refusal(request: Request):
    """Global operator authority and the dashboard's own origin, or a reply."""
    from tools.dashboard.api_auth import require_global_api_authority

    refused = require_global_api_authority(request)
    if refused is not None:
        return refused
    if request.method != "GET" and not viewer.same_origin(
            request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    return None


def operator_leases() -> list[dict]:
    from tools.dashboard.dao import dashboard_db

    out = []
    for lease in store.list_leases():
        row = dashboard_db.get_session(lease.session) or {}
        running = lease.state in ("starting", "ready", "busy", "locked", "unhealthy")
        usage = containers.stats(lease.container_name) if running else {"cpu": None, "mem_mb": None}
        out.append({
            "lease_ref": lease.lease_hash[:16], "session": lease.session,
            "session_label": row.get("label") or lease.session,
            "profile_kind": lease.profile_kind, "profile_name": lease.profile_name,
            "state": lease.state, "lock_holder": lease.lock_holder,
            "created_at": lease.created_at, "expires_at": lease.expires_at, **usage,
        })
    return out


async def get_operator_leases(request: Request) -> JSONResponse:
    refused = _operator_refusal(request)
    if refused is not None:
        return refused
    leases = await asyncio.to_thread(operator_leases)
    limits = await asyncio.to_thread(reconciler.defaults)
    return JSONResponse({"leases": leases, "limits": {"max_leases": limits["max_leases"],
                                                      "memory_mb": limits["memory_mb"]}})


async def post_control(request: Request) -> JSONResponse:
    refused = _operator_refusal(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    action, viewer_id = (body or {}).get("action"), (body or {}).get("viewer")
    if action not in ("take", "return") or not isinstance(viewer_id, str) \
            or not _VIEWER_RE.fullmatch(viewer_id):
        return JSONResponse({"error": 'body must be {"action": "take"|"return", "viewer"}'},
                            status_code=400)
    lease = await asyncio.to_thread(viewer.find_lease, request.path_params["lease"])
    if lease is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    fn = viewer.take_control if action == "take" else viewer.return_control
    try:
        return JSONResponse(await asyncio.to_thread(fn, lease, viewer_id))
    except viewer.ControlRefused as exc:
        return JSONResponse({"error": exc.error}, status_code=exc.status)


class _WsReader:
    """Byte-exact reads over WebSocket binary messages (handshake only)."""

    def __init__(self, websocket: WebSocket):
        self.ws, self.buffer = websocket, b""

    async def read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            self.buffer += await self.ws.receive_bytes()
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data


async def ws_view(websocket: WebSocket) -> None:
    """Relay VNC between the operator's noVNC and the lease's x11vnc."""
    from tools.dashboard import unlock_routes

    headers = websocket.headers
    if not viewer.same_origin(headers.get("origin"), headers.get("host")):
        await websocket.close(code=4403)
        return
    if await asyncio.to_thread(viewer.organization_bearer, headers.get("authorization")):
        await websocket.close(code=4403)
        return
    cookie = unlock_routes._cookie_from_scope(websocket.scope)
    viewer_id = websocket.query_params.get("viewer", "")
    lease = await asyncio.to_thread(viewer.find_lease, websocket.path_params["lease"])
    if lease is None or not _VIEWER_RE.fullmatch(viewer_id) or not lease.address:
        await websocket.close(code=4404)
        return
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(lease.address, viewer.VNC_PORT), timeout=5)
        await asyncio.wait_for(_server_handshake(reader, writer, lease), timeout=10)
    except Exception:
        logger.warning("browser viewer: cannot reach VNC for %s", lease.container_name, exc_info=True)
        await websocket.close(code=4502)
        return
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in offered else None)
    state = viewer._control(lease.lease_hash)
    state.viewers.add(viewer_id)
    loop = asyncio.get_running_loop()
    try:
        client = _WsReader(websocket)
        await websocket.send_bytes(viewer.RFB_VERSION)
        await client.read(12)
        await websocket.send_bytes(bytes([1, 1]))          # one security type: None
        if (await client.read(1)) != b"\x01":
            return
        await websocket.send_bytes(struct.pack(">I", 0))    # security result: OK
        await client.read(1)                                 # ClientInit
        writer.write(b"\x01")                               # always shared: many viewers watch
        await writer.drain()
        await _relay(websocket, client, reader, writer, lease, viewer_id, cookie)
    except (WebSocketDisconnect, ConnectionError, asyncio.IncompleteReadError, viewer.ProtocolError):
        pass
    finally:
        writer.close()
        viewer.viewer_left(lease.lease_hash, viewer_id, loop)


async def _server_handshake(reader, writer, lease: store.Lease) -> None:
    await reader.readexactly(12)
    writer.write(viewer.RFB_VERSION)
    types = await reader.readexactly((await reader.readexactly(1))[0])
    if 2 not in types:
        raise viewer.ProtocolError("the lease's VNC server does not offer VNC authentication")
    writer.write(b"\x02")
    challenge = await reader.readexactly(16)
    password = await asyncio.to_thread(lambda: lease.vnc_password)
    writer.write(viewer.vnc_auth_response(password, challenge))
    await writer.drain()
    if struct.unpack(">I", await reader.readexactly(4))[0] != 0:
        raise viewer.ProtocolError("the lease's VNC server refused the password")


async def _relay(websocket, client, reader, writer, lease, viewer_id, cookie) -> None:
    from tools.dashboard import unlock_routes

    filt = viewer.ClientFilter()
    last_activity = [0.0]

    async def lease_to_operator():
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await websocket.send_bytes(data)

    async def operator_to_lease():
        if client.buffer:  # bytes that arrived with the handshake
            pending, client.buffer = client.buffer, b""
            await _forward(pending)
        while True:
            await _forward(await websocket.receive_bytes())

    async def _forward(data: bytes):
        allowed = viewer.holds_control(lease.lease_hash, viewer_id)
        out, saw_input = filt.feed(data, allowed)
        if out:
            writer.write(out)
            await writer.drain()
        if saw_input and allowed and time.monotonic() - last_activity[0] >= viewer.ACTIVITY_WRITE_S:
            last_activity[0] = time.monotonic()
            epoch = reconciler.epoch()
            if epoch is not None:
                await asyncio.to_thread(store.update, lease.lease_hash, epoch=epoch,
                                        last_activity=time.time())

    async def session_still_valid():
        while True:
            await asyncio.sleep(viewer.SESSION_RECHECK_S)
            if unlock_routes.gate_enforced() and \
                    await asyncio.to_thread(unlock_routes.verify_session_token, cookie) is None:
                await websocket.close(code=4401)
                return
            fresh = await asyncio.to_thread(store.get, lease.lease_hash)
            if fresh is None or fresh.state in store.FINAL_STATES + ("releasing",):
                await websocket.close(code=4410)
                return

    tasks = [asyncio.create_task(t()) for t in (lease_to_operator, operator_to_lease, session_still_valid)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()


_PAGE = Path(__file__).resolve().parent / "static" / "browser" / "browser.html"


async def browser_page(request: Request):
    """The operator's lease list (/browser) and viewer (/browser/{lease}). The
    dashboard sign-in gate covers these paths; the page carries no data and
    reads everything from the operator-authority APIs."""
    from starlette.responses import FileResponse

    return FileResponse(_PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})


ROUTES = [
    Route("/browser", browser_page, methods=["GET"]),
    Route("/browser/{lease}", browser_page, methods=["GET"]),
    Route("/api/browser/leases", post_lease, methods=["POST"]),
    Route("/api/browser/leases/{lease}", get_lease, methods=["GET"]),
    Route("/api/browser/leases/{lease}", delete_lease, methods=["DELETE"]),
    Route("/api/browser/leases/{lease}/commands", post_command, methods=["POST"]),
    Route("/api/browser/leases/{lease}/secure-login", post_secure_login, methods=["POST"]),
    Route("/api/browser/operator/leases", get_operator_leases, methods=["GET"]),
    Route("/api/browser/leases/{lease}/control", post_control, methods=["POST"]),
    WebSocketRoute("/ws/browser/{lease}/view", ws_view),
]
