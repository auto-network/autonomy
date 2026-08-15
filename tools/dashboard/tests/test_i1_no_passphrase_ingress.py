"""I1: no dashboard route may accept a passphrase in a request body.

Invariant I1 (``tools/graph/schemas/network_identity.py``): the server only
ever stores and serves the ENCRYPTED armor; plaintext exists solely in the
operator's browser during ceremonies. A route that takes a personal password
over HTTP breaks that outright -- the passphrase is the armor, so handing it
to the server hands over the personal root.

This is asserted by ENUMERATION over the live route table rather than by
review, so a future route cannot reintroduce the ingress silently. It is the
same live-router enumeration the org-scope lockdown audit needs.

The rule is about the WIRE, not about the function argument. Taking a password
as a parameter is correct on the CLI paths (``org_cmd.py``, ``join.py``):
nothing crosses a network there. Only handlers reachable over HTTP are
enumerated here.
"""

from __future__ import annotations

import inspect
import re


# A body-read of a field whose name mentions a password or a passphrase:
#   body.get("personal_password") / payload["passphrase"] / data.get("password")
# The container names are the ones the dashboard handlers actually bind the
# parsed request body to.
_BODY_PASSPHRASE_READ = re.compile(
    r"""(?:body|payload|data|params|fields|form)\s*(?:\.get\(|\[)\s*"""
    r"""["']([A-Za-z0-9_]*(?:password|passphrase|passwd)[A-Za-z0-9_]*)["']""",
    re.IGNORECASE,
)

# Handlers that legitimately mention a password-shaped field WITHOUT it being
# personal-identity plaintext. Each entry must say why it is not an I1 breach.
_ALLOWED: dict[str, str] = {}


def _http_handlers():
    """Every endpoint callable mounted on the live app, with its route path."""
    from tools.dashboard import server

    seen: set[int] = set()
    for route in server.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or id(endpoint) in seen:
            continue
        seen.add(id(endpoint))
        yield getattr(route, "path", "<unknown>"), endpoint


def test_no_dashboard_route_reads_a_passphrase_from_a_request_body():
    offenders: list[str] = []

    for path, endpoint in _http_handlers():
        try:
            source = inspect.getsource(inspect.unwrap(endpoint))
        except (OSError, TypeError):
            continue
        name = getattr(endpoint, "__qualname__", repr(endpoint))
        if name in _ALLOWED:
            continue
        for match in _BODY_PASSPHRASE_READ.finditer(source):
            offenders.append(f"{path} -> {name} reads body field {match.group(1)!r}")

    assert not offenders, (
        "I1 violation: a dashboard route accepts a passphrase in its request "
        "body. The passphrase IS the armor, so this hands the server the "
        "personal root. Prove possession instead (see post_unlock_password's "
        "challenge/signature), or have the browser sign the event and send "
        "only the signed result (see /api/network/ledger/found).\n  "
        + "\n  ".join(offenders)
    )


def test_the_enumeration_actually_walks_a_populated_route_table():
    """A guard that silently enumerates nothing would assert nothing."""
    handlers = list(_http_handlers())
    assert len(handlers) >= 250, (
        f"expected the live dashboard route table, got {len(handlers)} handlers"
    )
    sources = 0
    for _path, endpoint in handlers:
        try:
            inspect.getsource(inspect.unwrap(endpoint))
        except (OSError, TypeError):
            continue
        sources += 1
    assert sources >= 250, (
        f"only {sources} handlers yielded source; the passphrase scan would "
        "be vacuous over the rest"
    )


def test_the_detector_catches_a_passphrase_ingress_it_is_shown():
    """The scan must fail on a real ingress, or it proves nothing."""
    sample = '''
async def api_orgs_create(request):
    body = await request.json()
    personal_password = body.get("personal_password")
'''
    found = [m.group(1) for m in _BODY_PASSPHRASE_READ.finditer(sample)]
    assert found == ["personal_password"]

    clean = '''
async def post_unlock_password(request):
    body = await request.json()
    challenge = body.get("challenge")
    signature = body.get("signature")
'''
    assert not _BODY_PASSPHRASE_READ.findall(clean)
