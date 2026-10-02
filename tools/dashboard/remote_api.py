"""Remote API: a decorated dashboard route, callable on another machine
through one generic ``api`` operation (design graph://b76a496e-a2d; epic
auto-0ltyz).

A route opts in with ``@remote("fleet")``, ``@remote("org")`` or both, plus
the session rules the operator named (2026-09-30):

* ``session_field`` -- the path parameter or JSON body field holding the
  session. For an ``org`` caller, the session must exist here and belong to
  the caller's organization, else ``no-such-session``.
* ``check_is_runner`` -- for an ``org`` caller, this machine must have a live
  runner offer in the caller's organization, else ``runner-not-offered``.
* ``check_is_owner`` -- for an ``org`` caller, the session's ``owner_persona``
  must be the caller's proved persona, else ``not-owner``.

A ``fleet`` caller (the operator's own machine) skips all three.

A request names its target in ``X-Autonomy-Machine`` or the ``_machine``
query parameter. Absent, or naming this machine, the route runs here. Else
the decorated endpoint forwards ``{method, path, query, headers, body}`` as
the ``api`` op; the receiver matches the route, requires its marker and the
caller's kind, and dispatches through this app with the caller -- proved by
the handshake, never a request field -- in the ASGI scope (``SCOPE_KEY``),
where the identity middleware classifies it and the decorator enforces the
rules before the handler runs.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match

logger = logging.getLogger(__name__)

#: In-process only: an ASGI scope key no HTTP request can set.
SCOPE_KEY = "autonomy.remote_caller"
TARGET_HEADER = "x-autonomy-machine"
TARGET_PARAM = "_machine"
FLEET, ORG = "fleet", "org"
#: Request headers that cross; everything else stays on the caller.
REQUEST_HEADERS = ("content-type", "accept", "range")
#: Response headers that come back.
RESPONSE_HEADERS = ("content-type", "cache-control", "content-range",
                    "content-disposition")
#: Largest body in either direction until replies stream (auto-ij6bo).
MAX_BODY_BYTES = 180 * 1024
DISPATCH_TIMEOUT_S = 30.0

ROUTE_NOT_REMOTE = "route-not-remote"
SCOPE_REFUSED = "remote-scope-refused"
NO_SUCH_SESSION = "no-such-session"
RUNNER_NOT_OFFERED = "runner-not-offered"
NOT_OWNER = "not-owner"
BODY_TOO_LARGE = "remote-body-too-large"
BAD_REQUEST = "remote-request-malformed"
DISPATCH_TIMEOUT = "remote-dispatch-timeout"


@dataclass(frozen=True)
class RemoteRule:
    kinds: tuple[str, ...]
    session_field: str | None = None
    check_is_runner: bool = False
    check_is_owner: bool = False


@dataclass(frozen=True)
class RemoteCaller:
    """Who the handshake proved: ``fleet`` (a roster machine of this
    operator) or ``org`` (a confirmed member persona of ``org``)."""

    kind: str
    machine_pub: str
    persona: str | None = None
    org: str | None = None


def rule_of(endpoint) -> RemoteRule | None:
    return getattr(endpoint, "__remote__", None) if endpoint is not None else None


def _refuse(code: str, status: int, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": detail or code, "refusal": code}, status_code=status)


# ── the caller's side ──────────────────────────────────────────────────────


def _local_pub() -> str | None:
    from tools.dashboard import session_presence

    local = session_presence.local_machine()
    return local.machine_pub if local else None


def _org_runner(named: str) -> dict | None:
    """A verified runner offer in one of this machine's organizations whose
    serving key is *named* (full, or a unique prefix of 8+ hex)."""
    from tools.dashboard import org_runners, org_sync_channels

    hits = []
    for slug, channel in org_sync_channels.provider()().items():
        for machine_pub, offer in org_runners._offers(slug, channel).items():
            if machine_pub == named or (len(named) >= 8 and machine_pub.startswith(named)):
                hits.append({"org": slug, "persona_pub": offer["persona_pub"],
                             "machine_pub": machine_pub})
    return hits[0] if len(hits) == 1 else None


def target_of(request: Request) -> tuple[str, object] | None:
    """Where the request runs: None (here), ("fleet", machine_pub), ("org",
    runner target) or ("unknown", name)."""
    from tools.dashboard import session_control_client

    named = request.headers.get(TARGET_HEADER) or request.query_params.get(TARGET_PARAM)
    if not named:
        return None
    machine = session_control_client.resolve_machine(named)
    if machine is not None:
        return None if machine == _local_pub() else (FLEET, machine)
    runner = _org_runner(named)
    if runner is not None:
        return ORG, runner
    return "unknown", named


def authorize_forward(request: Request, target: tuple[str, object]) -> Response | None:
    """Whether THIS caller may send the request to *target*, decided before
    anything is forwarded (auto-3s3gi). The receiver runs the route as this
    machine -- a fleet target with the operator's global authority, an org
    runner as this machine's member persona in that runner's organization --
    so the caller here must already hold that authority:

    * a fleet machine, or an unknown name: global operator authority;
    * an org runner: global authority, or an organization-bound principal of
      that runner's organization.

    ``None`` means authorized; otherwise the same refusal the handler would
    give (401 for compatibility traffic, 403 for an org-bound caller)."""
    from tools.dashboard import api_auth

    kind, where = target
    if kind == ORG:
        principal = api_auth.principal_from_request(request)
        if principal.org_bound and principal.org and principal.org == where.get("org"):
            return None
    return api_auth.require_global_api_authority(request)


#: Reply content types a forwarded response may keep; any other becomes
#: application/octet-stream, so a remote machine cannot serve HTML or script
#: on this dashboard's origin.
SAFE_CONTENT_TYPES = ("application/json", "text/plain", "text/event-stream",
                      "application/octet-stream")


def _reply_headers(headers) -> dict:
    """The remote reply's headers that may reach this origin: only
    RESPONSE_HEADERS, with an unsafe content type replaced, plus nosniff."""
    out = {}
    for key, value in (headers or {}).items() if isinstance(headers, dict) else ():
        name = str(key).lower()
        if name not in RESPONSE_HEADERS:
            continue
        value = str(value)
        if name == "content-type" and value.split(";")[0].strip().lower() not in SAFE_CONTENT_TYPES:
            value = "application/octet-stream"
        out[name] = value
    out["x-content-type-options"] = "nosniff"
    return out


async def forward(request: Request, target: tuple[str, object]) -> Response:
    from tools.dashboard import member_message_client, session_control_client

    kind, where = target
    if kind == "unknown":
        return _refuse("unknown-machine", 404,
                       f"{where!r} is neither a machine of this fleet nor an "
                       "organization's runner")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _refuse(BODY_TOO_LARGE, 413)
    query = [(k, v) for k, v in parse_qsl(request.url.query, keep_blank_values=True)
             if k != TARGET_PARAM]
    payload = {
        "method": request.method, "path": request.url.path, "query": urlencode(query),
        "headers": {k: v for k, v in request.headers.items() if k in REQUEST_HEADERS},
        "body": base64.b64encode(body).decode("ascii"),
    }
    if kind == FLEET:
        reply = await session_control_client.request(where, "api", payload,
                                                     timeout=DISPATCH_TIMEOUT_S + 5)
    else:
        reply = await member_message_client.request(where, "api", payload,
                                                    timeout=DISPATCH_TIMEOUT_S + 5)
    if not reply.get("ok"):
        status = 404 if reply.get("refusal") == ROUTE_NOT_REMOTE else 502
        return JSONResponse({"error": reply.get("detail") or reply.get("refusal"),
                             "refusal": reply.get("refusal"), "at": reply.get("at")},
                            status_code=status)
    result = reply.get("result") or {}
    return Response(content=base64.b64decode(result.get("body") or ""),
                    status_code=int(result.get("status") or 502),
                    headers=_reply_headers(result.get("headers")))


class RemoteTargetGuard:
    """Refuses a remote target on anything but an ``@remote`` route. It fails
    closed: a named target with no decorated endpoint matched -- an
    undecorated route, a mounted plugin route, or no route at all -- is
    refused rather than run HERE."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and SCOPE_KEY not in scope and _names_target(scope):
            if rule_of(_matched_endpoint(scope)) is None:
                await _refuse(ROUTE_NOT_REMOTE, 404,
                              "this route cannot run on another machine")(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _names_target(scope) -> bool:
    for name, _value in scope.get("headers") or []:
        if name == TARGET_HEADER.encode():
            return True
    query = (scope.get("query_string") or b"").decode("latin-1")
    return any(key == TARGET_PARAM for key, _ in parse_qsl(query, keep_blank_values=True))


def _matched_endpoint(scope, routes=None):
    """The endpoint a request would reach, through Mounts; None if none."""
    if routes is None:
        routes = getattr(getattr(scope.get("app"), "router", None), "routes", None) or []
    for route in routes:
        try:
            match, child = route.matches(scope)
        except Exception:
            continue
        if match != Match.FULL:
            continue
        inner = getattr(route, "routes", None)
        if inner is not None and not hasattr(route, "endpoint"):
            return _matched_endpoint({**scope, **child}, inner)
        return getattr(route, "endpoint", None)
    return None


# ── the decorator ──────────────────────────────────────────────────────────


def remote(*kinds: str, session_field: str | None = None,
           check_is_runner: bool = False, check_is_owner: bool = False):
    if not kinds or any(k not in (FLEET, ORG) for k in kinds):
        raise ValueError("remote() takes 'fleet' and/or 'org'")
    if check_is_owner and not session_field:
        raise ValueError("check_is_owner needs session_field")
    rule = RemoteRule(tuple(kinds), session_field, check_is_runner, check_is_owner)

    def decorate(fn):
        @functools.wraps(fn)
        async def endpoint(request: Request):
            caller = (getattr(request, "scope", None) or {}).get(SCOPE_KEY)
            if caller is None:
                if not (hasattr(request, "headers") and hasattr(request, "query_params")):
                    return await fn(request)    # a direct call, not an HTTP request
                target = await asyncio.to_thread(target_of, request)
                if target is not None:
                    refused = authorize_forward(request, target)
                    return refused if refused is not None else await forward(request, target)
                return await fn(request)
            refused = await enforce(rule, request, caller)
            return refused if refused is not None else await fn(request)

        endpoint.__remote__ = rule
        return endpoint

    return decorate


async def _session_name(request: Request, field: str) -> str | None:
    name = request.path_params.get(field)
    if name:
        return str(name)
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return None
    value = body.get(field) if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


async def enforce(rule: RemoteRule, request: Request, caller: RemoteCaller) -> Response | None:
    """The one enforcement point, before the handler runs."""
    if caller.kind not in rule.kinds:
        return _refuse(SCOPE_REFUSED, 403, "this route is not open to this caller")
    if caller.kind == FLEET:
        return None
    if rule.check_is_runner:
        from tools.dashboard import org_runners

        if not await asyncio.to_thread(org_runners.offered_here, caller.org):
            return _refuse(RUNNER_NOT_OFFERED, 403,
                           "this machine does not offer itself to that organization")
    if rule.session_field:
        from tools.dashboard import session_presence
        from tools.dashboard.dao import dashboard_db

        name = await _session_name(request, rule.session_field)
        row = await asyncio.to_thread(dashboard_db.get_session, name) if name else None
        if row is None or session_presence.session_org(row) != caller.org:
            return _refuse(NO_SUCH_SESSION, 404, name or "no session named")
        if rule.check_is_owner and row.get("owner_persona") != caller.persona:
            return _refuse(NOT_OWNER, 403, "only the member who launched this session may do that")
    return None


# ── the receiver's side: the ``api`` op ────────────────────────────────────


async def dispatch(app, payload: dict, caller: RemoteCaller) -> dict:
    """Run one forwarded request through *app* as *caller*; the reply record."""
    from tools.dashboard import session_control_client as scc

    method, path = payload.get("method"), payload.get("path")
    if not isinstance(method, str) or not isinstance(path, str) or not path.startswith("/api/"):
        return scc.refusal(BAD_REQUEST, "method and an /api/ path are required")
    try:
        body = base64.b64decode(payload.get("body") or "")
    except (ValueError, TypeError):
        return scc.refusal(BAD_REQUEST, "body is not base64")
    headers = [(k.lower().encode("latin-1"), str(v).encode("latin-1"))
               for k, v in (payload.get("headers") or {}).items()
               if isinstance(k, str) and k.lower() in REQUEST_HEADERS]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method.upper(), "scheme": "https", "path": path,
        "raw_path": path.encode("utf-8"), "root_path": "",
        "query_string": str(payload.get("query") or "").encode("latin-1"),
        "headers": headers + [(b"host", b"remote")], "client": ("remote", 0),
        "server": ("remote", 0), "app": app, SCOPE_KEY: caller,
    }
    endpoint = _matched_endpoint(scope)
    rule = rule_of(endpoint) if endpoint is not None else None
    if rule is None:
        return scc.refusal(ROUTE_NOT_REMOTE, f"{method} {path} cannot run remotely")
    if caller.kind not in rule.kinds:
        return scc.refusal(SCOPE_REFUSED, f"{method} {path} is not open to a {caller.kind} caller")

    sent = {"status": 500, "headers": {}, "body": bytearray()}
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            await asyncio.sleep(3600)
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode("latin-1"): v.decode("latin-1")
                               for k, v in message.get("headers") or []
                               if k.decode("latin-1").lower() in RESPONSE_HEADERS}
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body") or b""
            if len(sent["body"]) > MAX_BODY_BYTES:
                raise _TooLarge()

    try:
        await asyncio.wait_for(app(scope, receive, send), DISPATCH_TIMEOUT_S)
    except _TooLarge:
        return scc.refusal(BODY_TOO_LARGE, "the reply is too large until replies stream")
    except asyncio.TimeoutError:
        return scc.refusal(DISPATCH_TIMEOUT, f"no reply within {DISPATCH_TIMEOUT_S:.0f}s")
    logger.info("remote api %s %s from %s %s -> %s", method, path, caller.kind,
                (caller.persona or caller.machine_pub)[:12], sent["status"])
    return scc.ok({"status": sent["status"], "headers": sent["headers"],
                   "body": base64.b64encode(bytes(sent["body"])).decode("ascii")})


class _TooLarge(Exception):
    pass


def fleet_op(app):
    """The session-control ``api`` op: the caller is the roster machine the
    session:control handshake proved."""

    async def op(body: dict, peer: str) -> dict:
        return await dispatch(app, body, RemoteCaller(FLEET, machine_pub=peer))

    return op


def org_op(app):
    """The member-message ``api`` op: the caller is the organization member
    the org hello proved (a confirmed member; the connector refuses others)."""
    from tools.dashboard import member_message_client as mmc

    async def op(body: dict, proved: dict) -> dict:
        slug = await asyncio.to_thread(mmc.slug_of, proved["org"])
        if slug is None:
            return mmc.refusal("unknown-organization", "no local store for that organization")
        return await dispatch(app, body, RemoteCaller(
            ORG, machine_pub=proved["peer_machine_pub"],
            persona=proved["persona_pub"], org=slug))

    return op

