"""Default-deny at the API boundary.

Every ``/api`` route is authenticated **by construction**: the assembly wrap
below refuses any caller the identity middleware did not authenticate, unless
the route's ``(method, path)`` is one of the explicit, justified exceptions in
:data:`PUBLIC_EXCEPTIONS`. A route reaches "servable without a credential" only
by being written into that one table with a reason a reviewer reads — never by
omission. Forgetting to declare a route's policy yields **closed**, not open.

Most routes accept one of two identities the middleware classifies as
authenticated: a **bearer session token** (an agent using the API) or the
**dashboard session cookie** (the operator). A small number of exact routes
also accept machine-scoped service bearers; the identity middleware recognizes
each only on its registered method/path, so it gains no general API authority.
There is no implicit visitor or anonymous authority. Guest surfaces (e.g. a
mission's rendered screen) are served through the sandboxed Content Frame
document path, not through these ``/api`` routes.

Design of record: ``graph://78220bd8-fea`` (this feature's decision note),
``graph://f42db05f-7ca`` (the lockdown invariants), ``graph://a557b9ff-a5a``
(the two gates). Enforcement composes with the human gate: the guard stands
down while ``unlock_routes.gate_enforced()`` is false (an unenrolled or
recovery dashboard), so the recovery path is never broken by this wrap.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth


#: The ONLY ``(method, path)`` pairs served without a credential, each with the
#: reason it must be. Every one is a route the browser needs *before* it can
#: hold a credential — the pre-authentication bootstrap. Nothing else belongs
#: here; a plugin route never does (see :func:`assert_no_plugin_exceptions`).
#: Adding an entry is a deliberate, reviewed act — this table is the single
#: place "public" is written down, so the review is a one-file read.
PUBLIC_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("GET", "/api/health"):
        "Liveness probe. Returns no data and touches no state.",
    ("GET", "/api/ping"):
        "Liveness probe. Returns no data and touches no state.",
    ("GET", "/api/identity/personal"):
        "Serves the ENCRYPTED personal-root armor the browser must fetch "
        "before it can authenticate. Its protection is the encryption and the "
        "password never leaving the browser, not an auth gate — gating it makes "
        "sign-in impossible.",
    ("GET", "/api/identity/factor-policy"):
        "Describes the public, root-signed policy already enclosed in the "
        "pre-authentication personal armor plus operator-facing non-secret "
        "factor labels. The sign-in browser needs the dashboard-access and "
        "root-membership roles before choosing a ceremony; no password "
        "protector ciphertext or private factor material is returned.",
    ("POST", "/api/identity/personal"):
        "First-run identity enrolment, which by definition happens before any "
        "credential exists. Reachable only while the dashboard is unenrolled "
        "(the human gate's fail-open-then-enforce); once enrolled the gate "
        "governs it.",
    ("GET", "/api/identity/status"):
        "Tells the pre-sign-in browser whether an identity is enrolled and the "
        "gate enforced, so it knows which ceremony to show. No secret.",
    ("GET", "/api/identity/session"):
        "Reports current session state to a browser that may not yet hold one.",
    ("POST", "/api/identity/unlock/password/options"):
        "Issues the one-use password-unlock challenge. The caller has no "
        "session yet — obtaining one is the point of the call.",
    ("POST", "/api/identity/unlock/password"):
        "Proves the password unlock and mints the session cookie. Pre-session "
        "by construction.",
    ("POST", "/api/identity/unlock/combined"):
        "Proves the combined (MFA) unlock — password + passkey together — and "
        "mints the session cookie. Pre-session by construction, exactly like the "
        "password path it shares; an MFA identity has NO other unlock route, so "
        "gating this locks the operator out.",
    ("POST", "/api/identity/unlock/passkey/options"):
        "Issues the one-use passkey assertion challenge. Pre-session.",
    ("POST", "/api/identity/unlock/passkey"):
        "Completes the passkey unlock and mints the session cookie. "
        "Pre-session by construction.",
    ("POST", "/api/identity/ceremony-error"):
        "Diagnostic-only report of a client-side ceremony failure (no secrets). "
        "Unlock ceremonies fail BEFORE a session exists, so their failures must "
        "be reportable pre-session or the very lockouts we most need to see "
        "would be the ones we never log.",
    ("POST", "/api/fleet/enrollment/local-resume"):
        "Resumes only the Fleet request and invitation credential already "
        "stored by first run. It must remain reachable after encrypted root "
        "delivery turns on the human gate but before the joining browser can "
        "unlock and obtain its first session.",
    ("POST", "/api/dropbox/enrollments"):
        "A generic signed iPhone Shortcut begins with only this dashboard "
        "origin. This bounded route creates an operator approval but grants no "
        "authority until the operator decides it.",
    ("GET", "/api/dropbox/enrollments/{id}"):
        "The high-entropy enrollment id is the one-time reply capability used "
        "by the Shortcut while it waits for the operator-approved upload token.",
}


def _guarded(endpoint, path: str, *, plugin: bool):
    """Wrap *endpoint* so a non-exception ``(method, path)`` is refused unless
    authenticated. The check is per-request so a path with mixed
    public/authenticated methods is handled correctly.

    Two enforcement strengths, and the difference is the operator ruling of
    2026-08-21:

    * An APP route delegates to :func:`api_auth.require_authenticated_api_caller`,
      which stands down while the human gate is not enforced — a fresh install
      must reach the pre-enrolment bootstrap before any credential exists.
    * A PLUGIN route is authenticated UNCONDITIONALLY: it refuses a caller the
      middleware did not authenticate regardless of gate state, because an
      unenrolled dashboard exposes NO plugin routes. A plugin has no bootstrap
      role — its callers are an agent (bearer) or the operator (cookie), and
      neither exists before enrolment — so there is no gate-open window in
      which serving it is correct. This is also why the destructive plugin
      routes (delete a mission/pillar, mint a visitor credential) are never
      reachable by an anonymous caller on any dashboard state.
    """

    async def guarded(request):
        if (request.method, path) in PUBLIC_EXCEPTIONS:
            return await endpoint(request)
        if plugin:
            principal = api_auth.principal_from_request(request)
            if not principal.authenticated:
                return JSONResponse(
                    {"error": "authentication required"}, status_code=401,
                )
        else:
            refused = api_auth.require_authenticated_api_caller(request)
            if refused is not None:
                return refused
        return await endpoint(request)

    guarded._route_policy_wrapped = True  # idempotence marker (see below)
    return guarded


def apply_default_deny(routes: list, *, plugin: bool = False) -> list:
    """Return *routes* with every ``/api`` route authenticated by construction.

    Applied at the app route-list assembly (``plugin=False``) AND, with
    ``plugin=True``, inside the plugin mount, so an app or plugin route is
    closed on omission. A plugin route is authenticated unconditionally (no
    gate stand-down); an app route uses the fail-open-then-enforce guard so a
    fresh install can bootstrap. Non-``/api`` routes (pages, static, mounts)
    are returned untouched — the human gate governs those.
    """
    out = []
    for r in routes:
        already_wrapped = getattr(
            getattr(r, "endpoint", None), "_route_policy_wrapped", False
        )
        if (
            isinstance(r, Route)
            and r.path.startswith("/api/")
            and not already_wrapped
        ):
            out.append(
                Route(
                    r.path,
                    _guarded(r.endpoint, r.path, plugin=plugin),
                    methods=list(r.methods or ["GET"]),
                    name=r.name,
                )
            )
        else:
            out.append(r)
    return out


def gate_plugin_enabled(plugin_id: str, routes: list, enabled_fn) -> list:
    """Return *routes* with every ``/api`` route gated on plugin enablement.

    Dormant means dormant: pages, fragments, static, and the skill route
    already check the enable Setting per request, and the CLI mounts only
    while enabled — this closes the one surface that didn't, the plugin's
    own API routes. Applied INSIDE ``apply_default_deny`` (gate wraps the
    endpoint first, auth wraps the gate), so an unauthenticated caller
    still sees the uniform auth refusal and learns nothing about which
    plugins exist; an authenticated caller gets 404 while disabled.

    ``enabled_fn`` is called per request (no restart needed to flip the
    Setting), matching ``_plugin_enabled_map``'s live semantics.
    """
    out = []
    for r in routes:
        if not (isinstance(r, Route) and r.path.startswith("/api/")):
            out.append(r)
            continue
        original = r.endpoint

        def _make(original, plugin_id=plugin_id):
            async def gated(request):
                if not enabled_fn().get(plugin_id):
                    from starlette.responses import JSONResponse
                    return JSONResponse({"error": "Not Found"},
                                        status_code=404)
                return await original(request)
            gated.__name__ = getattr(original, "__name__", "route")
            return gated

        out.append(Route(r.path, _make(original),
                         methods=list(r.methods or ["GET"]), name=r.name))
    return out


def assert_no_plugin_exceptions(plugin_route_paths: set[str]) -> None:
    """Refuse a plugin route that tries to be a public exception.

    Plugins get no open option: session and operator credentials cover every
    plugin case. A plugin path appearing in
    :data:`PUBLIC_EXCEPTIONS` is a configuration error, raised loudly at
    startup rather than served open.
    """
    offenders = {
        path for (_method, path) in PUBLIC_EXCEPTIONS
        if path in plugin_route_paths
    }
    if offenders:
        raise RuntimeError(
            "plugin routes may not be public exceptions (no open option for "
            f"plugins): {sorted(offenders)}"
        )
