"""Request identity and trusted graph scope for the dashboard API.

This module is the common authentication boundary for ``/api`` requests.  It
classifies credentials once, records the resulting principal on the ASGI
request state, and binds the graph ops context to a scope that credential is
allowed to select:

* a dashboard cookie is the local operator and may select any org;
* an org-stamped session bearer is an agent and is forced to its token org;
* a positively identified org-less host bearer is a local operator session;
* route-scoped service bearers classify only on their registered API routes;
* missing or unrecognised credentials remain compatibility traffic for now.

The middleware deliberately does not reject compatibility traffic.  Route
policy enforcement can therefore be migrated without breaking the existing
API surface, while every migrated handler already consumes the final principal
and trusted-scope representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse


logger = logging.getLogger(__name__)


class ApiPrincipalKind(str, Enum):
    """The authenticated identity class established for an API request."""

    OPERATOR_COOKIE = "operator_cookie"
    LOCAL_SESSION = "local_session"
    ORG_SESSION = "org_session"
    #: A machine-scoped service credential (the ChatGPT MCP relay). Authenticated,
    #: but deliberately BELOW local: no organization and no dashboard authority.
    #: Produced only on the routes that credential is scoped to, so its entire
    #: reach is those routes — everywhere else the same token does not classify
    #: here and fails to authenticate.
    MCP_SERVICE = "mcp_service"
    #: An operator-approved external service credential. It classifies only
    #: when a stored exact method/path capability matches the current request.
    EXTERNAL_SERVICE = "external_service"
    COMPATIBILITY = "compatibility"


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    """Identity and authority established at the API boundary."""

    kind: ApiPrincipalKind
    subject: str | None = None
    org: str | None = None
    persona_id: str | None = None
    auth_error_status: int | None = None
    api_capabilities: tuple[tuple[str, str], ...] = ()
    application_scope: str | None = None
    resource_audience: str | None = None
    source_approval_id: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.kind is not ApiPrincipalKind.COMPATIBILITY

    @property
    def global_authority(self) -> bool:
        return self.kind in {
            ApiPrincipalKind.OPERATOR_COOKIE,
            ApiPrincipalKind.LOCAL_SESSION,
        }

    @property
    def org_bound(self) -> bool:
        return self.kind is ApiPrincipalKind.ORG_SESSION

    def allows_api(self, method: str, path: str) -> bool:
        """Whether a scoped external principal allows this exact API call."""
        return (method.upper(), path) in self.api_capabilities


COMPATIBILITY_PRINCIPAL = ApiPrincipal(ApiPrincipalKind.COMPATIBILITY)


def principal_from_request(request: Request) -> ApiPrincipal:
    """Return the middleware-established API principal.

    The fallback keeps direct handler unit tests and non-HTTP callers honest:
    absence means compatibility traffic, never implicit operator authority.
    """

    return getattr(request.state, "api_principal", COMPATIBILITY_PRINCIPAL)


def organization_scope_from_request(request: Request) -> str | None:
    """Return the middleware-approved organization for this API request.

    This is the trusted scope to pass into organization-homed Settings calls.
    For an organization session it always comes from the bearer, even when the
    caller supplies a conflicting ``X-Graph-Org`` header.  For a dashboard
    operator or local host session it is the explicit organization selection,
    when one was supplied.  ``None`` means that no organization was selected;
    handlers whose data requires an organization should reject that request.

    Route handlers must use this helper instead of re-parsing headers, query
    parameters, or request bodies.  Those values are inputs to the identity
    middleware, not independent sources of authority.
    """

    return getattr(request.state, "api_organization", None)


def caller_org_scope_hides(request: Request, resource_org: str | None) -> bool:
    """Standard org-scoped-resource visibility check: True when this caller must
    NOT see a resource owned by ``resource_org``.

    The canonical predicate for any ``/api`` resource that carries an owning org
    (a design, a session, …). Invariant 1: the token's org is authoritative and
    un-widenable, so an org-bound caller (an org session bearer) sees only its
    own org; a resource whose org is unresolvable (``None``) is hidden from it,
    since an org caller cannot distinguish that from cross-org. A global-authority
    caller (the operator cookie or a local host token) sees every org.

    Authentication is a separate gate (:func:`require_authenticated_api_caller`
    or the default-deny wrap); this only decides org visibility for an already
    authenticated caller, and never judges compatibility traffic (which the gate
    governs) — a compatibility principal is not org-bound, so this returns False.

    Callers return their route-native not-found (404) when this is True, so a
    cross-org resource is byte-indistinguishable from a nonexistent one — a 403
    would confirm the resource exists in another org.
    """
    principal = principal_from_request(request)
    if not principal.org_bound:
        return False
    return not (resource_org and principal.org and resource_org == principal.org)


def require_authenticated_api_caller(request: Request) -> JSONResponse | None:
    """Refuse an API caller that presents no credential at all.

    Invariant 4 of the org-scope lockdown (graph://f42db05f-7ca): "Every
    registered ``/api`` route rejects a no-credential request unless on an
    explicit public allowlist."  A CREDENTIAL, not the operator's — which is
    the distinction this function exists to make.

    Use this, not :func:`require_global_api_authority`, on a surface every
    authenticated caller legitimately uses.  An earlier guard on the generic
    Settings readers demanded global authority and 401'd every agent on the
    fleet, because agents read Settings constantly as normal operation and
    hold an ORG bearer, never the operator's.  "Only the operator reads
    arbitrary Settings" was simply false.

    What the caller may then READ is a separate question answered elsewhere:
    the token forces the org (invariant 1), and a schema's ``@home`` plus its
    ``@publication_band`` decide whether a row is reachable at all — six
    secret-bearing sets are pinned ``max=raw`` and so are structurally never
    read-through-able.  That is why this guard needs no list of secret sets:
    a new one is protected by declaring its band, not by someone remembering
    to edit a constant here.

    ``None`` means authorized.  Only compatibility traffic is refused, with
    401; every authenticated principal passes, including an org-bound one.

    Like the global guard, this stands down while the human gate is
    deliberately open — refusing a caller the gate has just admitted
    contradicts it, and the gate is what decides whether this dashboard is
    open at all.
    """
    principal = principal_from_request(request)
    if principal.authenticated:
        return None
    from tools.dashboard import unlock_routes
    if not unlock_routes.gate_enforced():
        return None
    logger.warning(
        "api_authz_refused policy=authenticated method=%s path=%s",
        request.method, request.url.path,
    )
    return JSONResponse({"error": "authentication required"}, status_code=401)


def require_global_api_authority(request: Request) -> JSONResponse | None:
    """Refuse an API caller that lacks global operator authority.

    This is the final route-level guard for operations that belong to the
    local operator rather than to any organization.  It consumes only the
    identity established by :class:`ApiIdentityMiddleware`: handlers must not
    re-parse cookies, bearers, or caller-controlled organization selectors.

    ``None`` means authorized.  Compatibility traffic receives 401;
    authenticated organization sessions receive 403.  The distinction lets a
    legitimate agent see that its credential is valid but deliberately too
    narrow, while missing or unrecognized credentials gain no authority.

    One exception, and it is not a weakening.  While
    :func:`~tools.dashboard.unlock_routes.gate_enforced` is false —
    ``DASHBOARD_AUTH`` set to a disabling value, or nothing enrolled yet —
    :class:`~tools.dashboard.unlock_routes.HumanGateMiddleware` admits the
    browser WITHOUT a session cookie.  The operator's own requests then arrive
    as compatibility traffic, and refusing them here would contradict the gate
    that just let them in.  Refusing also protects nothing: the gate is open,
    so the same browser reaches the underlying stores through every ungated
    route regardless.

    Both halves are states a real deployment sits in.  The switch is the
    recovery path for a dashboard whose unlock is broken, and a guard that
    fails there fails exactly when it is being relied on.  Unenrolled is every
    dashboard that has not set up a passkey — including one that never will.

    It applies to compatibility traffic ONLY.  An org-bound agent is positively
    identified and deliberately narrow; the state of the HUMAN gate says nothing
    about it, so its 403 stands.
    """

    principal = principal_from_request(request)
    if principal.global_authority:
        return None
    if principal.org_bound:
        logger.warning(
            "api_authz_refused policy=global_operator method=%s path=%s "
            "caller=%s caller_org=%s",
            request.method,
            request.url.path,
            principal.subject,
            principal.org,
        )
        return JSONResponse(
            {"error": "global operator authority required"},
            status_code=403,
        )
    # Imported here, not at module scope: unlock_routes pulls in the identity
    # and network route modules, which import this one back.
    from tools.dashboard import unlock_routes
    if not unlock_routes.gate_enforced():
        return None
    return JSONResponse({"error": "authentication required"}, status_code=401)


BearerAuthenticator = Callable[[Request], tuple[tuple[str, str | None] | None, object | None]]
CookieVerifier = Callable[[str | None], dict | None]
#: Resolves a machine-scoped service credential to its principal, or ``None`` if
#: the request is not a valid service call. The implementation owns the route
#: scoping: it returns a principal ONLY on the routes the credential is allowed
#: to reach, so a service token classifies as authenticated only there.
ServiceAuthenticator = Callable[[Request], "ApiPrincipal | None"]


class ApiIdentityMiddleware:
    """Classify API callers and bind their trusted graph scope.

    Valid bearer credentials take precedence over cookies so an org-bound
    agent cannot gain the cookie's wider authority when both happen to be
    present.  Invalid or non-session bearers may still belong to a delegated
    endpoint (for example MCP), so this compatibility stage records the failed
    primary-auth result and lets the endpoint-specific verifier decide.
    """

    def __init__(
        self,
        app,
        *,
        authenticate_bearer: BearerAuthenticator,
        verify_cookie: CookieVerifier,
        cookie_name: str,
        authenticate_service: ServiceAuthenticator | None = None,
    ):
        self.app = app
        self.authenticate_bearer = authenticate_bearer
        self.verify_cookie = verify_cookie
        self.cookie_name = cookie_name
        self.authenticate_service = authenticate_service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from tools.graph import ops as graph_ops

        request = Request(scope, receive=receive)
        header_org = request.headers.get("x-graph-org") or None
        principal, effective_org = self._classify(request, header_org)

        request_state = scope.setdefault("state", {})
        request_state["api_principal"] = principal
        request_state["api_organization"] = effective_org
        token = graph_ops.set_caller_org(effective_org)
        try:
            await self.app(scope, receive, send)
        finally:
            graph_ops.reset_caller_org(token)

    def _classify(
        self, request: Request, header_org: str | None,
    ) -> tuple[ApiPrincipal, str | None]:
        path = request.url.path

        # Non-API requests keep the existing selection semantics.  The human
        # gate remains their authentication boundary.
        if not path.startswith("/api/"):
            return COMPATIBILITY_PRINCIPAL, header_org

        # A machine-scoped service credential is checked first, but the verifier
        # itself decides whether this request is on a route that credential may
        # reach — so it authenticates ONLY there and carries no org (it never
        # sets an effective scope).  Off its routes it returns None and the
        # ordinary bearer/cookie path runs, where a service token is not a valid
        # session token and so does not authenticate.
        if self.authenticate_service is not None:
            try:
                service_principal = self.authenticate_service(request)
            except Exception:
                logger.warning(
                    "API service-token classification failed; continuing "
                    "through ordinary authentication",
                    exc_info=True,
                )
                service_principal = None
            if service_principal is not None:
                # A service credential carries no organization scope: return None
                # rather than the client-supplied header, so a token holder can
                # never select a scope via X-Graph-Org.
                return service_principal, None

        authorization = request.headers.get("authorization", "")
        bearer_error_status: int | None = None
        if authorization:
            try:
                identity, error = self.authenticate_bearer(request)
            except Exception:
                logger.warning(
                    "API session-bearer classification failed; continuing "
                    "through compatibility authentication",
                    exc_info=True,
                )
                identity, error = None, None
            if identity is not None and error is None:
                session, org = identity
                from tools.graph.org_ops import local_persona_pub
                persona_id = local_persona_pub()
                if org is None:
                    return (
                        ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject=session, persona_id=persona_id),
                        header_org,
                    )
                return (
                    ApiPrincipal(
                        ApiPrincipalKind.ORG_SESSION,
                        subject=session,
                        org=org,
                        persona_id=persona_id,
                    ),
                    org,
                )
            bearer_error_status = getattr(error, "status_code", None)

        try:
            cookie_payload = self.verify_cookie(
                request.cookies.get(self.cookie_name),
            )
        except Exception:
            logger.warning(
                "API dashboard-cookie classification failed; continuing "
                "as compatibility traffic",
                exc_info=True,
            )
            cookie_payload = None
        if cookie_payload is not None:
            from tools.graph.org_ops import local_persona_pub
            return (
                ApiPrincipal(
                    ApiPrincipalKind.OPERATOR_COOKIE,
                    subject=cookie_payload.get("sid"),
                    persona_id=local_persona_pub(),
                ),
                header_org,
            )

        # Compatibility is intentionally availability-preserving.  Until the
        # route policy inventory is complete, keep the legacy header scope for
        # callers that have no primary credential or use another endpoint's
        # delegated bearer.  They never receive authenticated authority.
        return (
            ApiPrincipal(
                ApiPrincipalKind.COMPATIBILITY,
                auth_error_status=bearer_error_status,
            ),
            header_org,
        )
