"""Default-deny at the API boundary.

Every ``/api`` route is authenticated **by construction**: the assembly wrap
below refuses any caller the identity middleware did not authenticate, unless
the route's ``(method, path)`` is one of the explicit, justified exceptions in
:data:`PUBLIC_EXCEPTIONS`. A route reaches "servable without a credential" only
by being written into that one table with a reason a reviewer reads — never by
omission. Forgetting to declare a route's policy yields **closed**, not open.

The two credentials a route accepts are the two the middleware already
classifies as authenticated: a **bearer session token** (an agent using the
API) or the **dashboard session cookie** (the operator). There is no third
way in — no visitor token, no anonymous, no outside access. Guest surfaces
(e.g. a mission's rendered screen) are served through the sandboxed Content
Frame document path, not through these ``/api`` routes.

Design of record: ``graph://78220bd8-fea`` (this feature's decision note),
``graph://f42db05f-7ca`` (the lockdown invariants), ``graph://a557b9ff-a5a``
(the two gates). Enforcement composes with the human gate: the guard stands
down while ``unlock_routes.gate_enforced()`` is false (an unenrolled or
recovery dashboard), so the recovery path is never broken by this wrap.
"""

from __future__ import annotations

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
    ("POST", "/api/identity/unlock/passkey/options"):
        "Issues the one-use passkey assertion challenge. Pre-session.",
    ("POST", "/api/identity/unlock/passkey"):
        "Completes the passkey unlock and mints the session cookie. "
        "Pre-session by construction.",
}


def _guarded(endpoint, path: str):
    """Wrap *endpoint* so a non-exception ``(method, path)`` is refused unless
    authenticated. Exception methods pass straight through. The check is
    per-request so a path with mixed public/authenticated methods is handled
    correctly, and it delegates to the existing guard (which itself stands down
    while the human gate is open)."""

    async def guarded(request):
        if (request.method, path) not in PUBLIC_EXCEPTIONS:
            refused = api_auth.require_authenticated_api_caller(request)
            if refused is not None:
                return refused
        return await endpoint(request)

    return guarded


def apply_default_deny(routes: list) -> list:
    """Return *routes* with every ``/api`` route authenticated by construction.

    Applied at route-list assembly AND inside the plugin mount, so an app or
    plugin route is closed on omission. Non-``/api`` routes (pages, static,
    mounts) are returned untouched — the human gate governs those.
    """
    out = []
    for r in routes:
        if isinstance(r, Route) and r.path.startswith("/api/"):
            out.append(
                Route(
                    r.path,
                    _guarded(r.endpoint, r.path),
                    methods=list(r.methods or ["GET"]),
                    name=r.name,
                )
            )
        else:
            out.append(r)
    return out


def assert_no_plugin_exceptions(plugin_route_paths: set[str]) -> None:
    """Refuse a plugin route that tries to be a public exception.

    Plugins get no open option: the two credentials cover every plugin case
    (agent = bearer, operator = cookie). A plugin path appearing in
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
