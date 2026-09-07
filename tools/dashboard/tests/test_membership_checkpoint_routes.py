"""The sign-on hook's literal endpoint paths must resolve against the router.

Regression for the seed-mint silent no-op (found by auto-0831-221227,
2026-09-07): network-signon.mjs `_publishMembershipCheckpoint` GETs the
decision at `/api/network/membership-checkpoint/decision`, but the GET
handler was registered ONLY at the bare `/api/network/membership-checkpoint`.
`_fetchJsonOrNull` turns the resulting 404 into null, and the hook returns a
non-fatal `{action: 'unavailable'}` — so every operator sign-on since
9c6e2848 silently minted nothing and the registry never saw a seed. This
asserts the JS's OWN literal paths (parsed from the module, so the test
tracks the client) are served by the real ROUTES table.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.routing import Route

from tools.dashboard import network_routes

SIGNON_JS = Path(__file__).resolve().parents[1] / "static/js/network-signon.mjs"


def _routed_get_paths() -> set[str]:
    return {
        r.path for r in network_routes.ROUTES
        if isinstance(r, Route) and "GET" in (r.methods or ())
    }


def _routed_post_paths() -> set[str]:
    return {
        r.path for r in network_routes.ROUTES
        if isinstance(r, Route) and "POST" in (r.methods or ())
    }


def test_signon_hook_decision_and_submit_paths_are_registered():
    js = SIGNON_JS.read_text()

    # The two literals the hook uses. The decision GET carries a query string
    # appended after the literal; the submit POST uses the bare literal.
    decision = re.search(r"'(/api/network/membership-checkpoint/decision)'", js)
    submit = re.search(
        r"_transport\.fetch\('(/api/network/membership-checkpoint)'", js)

    assert decision, "network-signon.mjs no longer GETs the decision literal"
    assert submit, "network-signon.mjs no longer POSTs the submit literal"

    get_paths = _routed_get_paths()
    post_paths = _routed_post_paths()
    assert decision.group(1) in get_paths, (
        f"{decision.group(1)} is not a registered GET route — the sign-on "
        f"hook would 404 into a silent no-op. Registered GETs: "
        f"{sorted(p for p in get_paths if 'membership' in p)}")
    assert submit.group(1) in post_paths, (
        f"{submit.group(1)} is not a registered POST route")
