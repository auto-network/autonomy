"""Request identity and trusted graph scope for the dashboard API.

This module is the common authentication boundary for ``/api`` requests.  It
classifies credentials once, records the resulting principal on the ASGI
request state, and binds the graph ops context to a scope that credential is
allowed to select:

* a dashboard cookie is the local operator and may select any org;
* an org-stamped session bearer is an agent and is forced to its token org;
* a positively identified org-less host bearer is a local operator session;
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
    COMPATIBILITY = "compatibility"


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    """Identity and authority established at the API boundary."""

    kind: ApiPrincipalKind
    subject: str | None = None
    org: str | None = None
    auth_error_status: int | None = None

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


COMPATIBILITY_PRINCIPAL = ApiPrincipal(ApiPrincipalKind.COMPATIBILITY)


def principal_from_request(request: Request) -> ApiPrincipal:
    """Return the middleware-established API principal.

    The fallback keeps direct handler unit tests and non-HTTP callers honest:
    absence means compatibility traffic, never implicit operator authority.
    """

    return getattr(request.state, "api_principal", COMPATIBILITY_PRINCIPAL)


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
    ):
        self.app = app
        self.authenticate_bearer = authenticate_bearer
        self.verify_cookie = verify_cookie
        self.cookie_name = cookie_name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from tools.graph import ops as graph_ops

        request = Request(scope, receive=receive)
        header_org = request.headers.get("x-graph-org") or None
        principal, effective_org = self._classify(request, header_org)

        scope.setdefault("state", {})["api_principal"] = principal
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
                if org is None:
                    return (
                        ApiPrincipal(ApiPrincipalKind.LOCAL_SESSION, subject=session),
                        header_org,
                    )
                return (
                    ApiPrincipal(
                        ApiPrincipalKind.ORG_SESSION,
                        subject=session,
                        org=org,
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
            return (
                ApiPrincipal(
                    ApiPrincipalKind.OPERATOR_COOKIE,
                    subject=cookie_payload.get("sid"),
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
