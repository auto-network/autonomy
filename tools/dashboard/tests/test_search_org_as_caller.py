"""Tests for org-as-caller wiring + publication-state pass-through (auto-zvu3z).

The org chip on /search now sets the ``X-Graph-Org`` header (caller_org)
on each /api/search call instead of sending ``?only_org=`` (peer-view).
The semantic difference matters:

  - ``X-Graph-Org: anchore`` → caller IS anchore → full anchore surface
    (raw + published + canonical + curated). What an operator means when
    they "pin to anchore".
  - ``?only_org=anchore`` (no caller) → peer-view → published + canonical
    only. Kept around for explicit peer-surface audit, but NOT what the
    org chip uses anymore.

Plus: the new publication-state chip translates its selection into a
``?states=…`` query param.

The /search Alpine page itself runs in the browser — these tests exercise:
  1. The static template+JS contract (search.js sends X-Graph-Org, not
     ?only_org=, when an org is pinned).
  2. The /api/search endpoint's existing X-Graph-Org → caller plumbing
     (already shipped via _CallerOrgMiddleware) survives the new chip.
  3. The endpoint accepts ``?states=`` and forwards it to ops.search.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient


# ── 1. JS contract: chip → header, not query param ────────────────────


def _read_search_js() -> str:
    js_path = Path(__file__).resolve().parents[1] / "static" / "js" / "pages" / "search.js"
    return js_path.read_text()


def test_org_chip_sends_x_graph_org_header():
    """The fetch() in _refetch() must build a headers object that includes
    ``X-Graph-Org`` when an org is pinned. The header name is the contract
    with _CallerOrgMiddleware on the server."""
    src = _read_search_js()
    # The header is set conditionally on selectedOrg.
    assert "'X-Graph-Org'" in src, "search.js no longer sets X-Graph-Org"
    # It must be wired into the fetch call's options object.
    assert "fetch(url, { headers: headers })" in src, (
        "fetch() in _refetch() must pass headers"
    )


def test_all_orgs_sends_no_x_graph_org_header():
    """When ``selectedOrg === ''`` the headers object stays empty and
    fetch() goes out without X-Graph-Org. The condition ``if
    (this.selectedOrg) headers['X-Graph-Org'] = …`` is the contract."""
    src = _read_search_js()
    assert "if (this.selectedOrg) headers['X-Graph-Org'] = this.selectedOrg" in src, (
        "search.js no longer guards X-Graph-Org behind selectedOrg"
    )


def test_org_chip_does_not_send_only_org_query_param():
    """The chip must NOT add ``?only_org=`` to the URL anymore — that
    semantic (peer-view inspection) belongs to explicit audit calls,
    not the chip. ``?only_org=…`` may still be parsed from the URL on
    init for backwards compatibility, but never appended on a refetch."""
    src = _read_search_js()
    # No code path encodes only_org into the fetch URL. Two ways the old
    # behaviour could leak back in: the literal ``'&only_org='`` string
    # concat, or the ``encodeURIComponent(this.selectedOrg)`` pattern that
    # auto-13134 used.
    assert "'&only_org='" not in src, (
        "search.js still concatenates &only_org= into a URL — the chip "
        "should set X-Graph-Org header instead"
    )
    assert "&only_org=" not in src, (
        "search.js still has &only_org= in a URL string"
    )
    assert "url += '&only_org=" not in src, (
        "search.js still appends only_org= to its fetch URL"
    )


# ── 2. Server contract: X-Graph-Org reaches ops.search as caller ──────


def test_pinned_org_routes_through_caller_org(test_app):
    """When the chip sends ``X-Graph-Org: autonomy`` and no
    ``?only_org=…``, the request reaches ops.search with caller-org wired
    via the contextvar but NO ``only_org=`` kwarg. That's the difference
    between "search AS autonomy" vs "search anchore PEER-VIEW"."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get(
                "/api/search?q=Worktree&group=1&limit=50",
                headers={"X-Graph-Org": "autonomy"},
            )
            assert r.status_code == 200

    assert captured["q"] == "Worktree"
    # Caller-org is forwarded as ``org=…`` (the kwarg name on ops.search).
    assert captured.get("org") == "autonomy"
    # And no peer-view override leaks through.
    assert captured.get("only_org") in (None, ""), (
        f"only_org should not be set when the chip uses caller-org wiring, "
        f"got {captured.get('only_org')!r}"
    )


def test_all_orgs_sends_no_caller(test_app):
    """When the chip is on "All orgs" (no header), ops.search receives
    org=None. This is the global-scope path — no caller, no peer-view."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    # Make sure no GRAPH_ORG env leaks in from the host process.
    env_before = os.environ.pop("GRAPH_ORG", None)
    try:
        with patch.object(server.graph_ops, "search", side_effect=fake_search):
            with TestClient(test_app) as client:
                r = client.get("/api/search?q=Worktree&group=1&limit=50")
                assert r.status_code == 200
    finally:
        if env_before is not None:
            os.environ["GRAPH_ORG"] = env_before

    assert captured.get("org") in (None, ""), (
        f"caller-org should be unset when no header is sent, got {captured.get('org')!r}"
    )
    assert captured.get("only_org") in (None, ""), (
        "only_org should not be set when the chip is on 'All orgs'"
    )


# ── 3. only_org= kept as explicit peer-view override ──────────────────


def test_only_org_query_param_still_forwarded(test_app):
    """``?only_org=`` is preserved as an *explicit* peer-view override —
    operators auditing what a peer would see still need this path. The
    chip just doesn't use it."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=x&only_org=anchore")
            assert r.status_code == 200

    assert captured.get("only_org") == "anchore", (
        "explicit ?only_org= override should still pass through to ops.search"
    )


# ── 4. Publication-state filter pass-through ──────────────────────────


def test_state_filter_param_passes_through(test_app):
    """``?states=published`` from the new state chip must reach ops.search
    as ``states=['published']`` — that's how the post-auto-lpzcc surface
    clamping works."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=x&states=published")
            assert r.status_code == 200

    assert captured.get("states") == ["published"], (
        f"states param did not reach ops.search verbatim: {captured.get('states')!r}"
    )


def test_state_filter_any_sends_no_states_param(test_app):
    """``Any`` (default) means no ``states=`` filter — ops.search receives
    ``states=None`` and applies its default surface (raw included for the
    caller's own org, public for peers)."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=x")
            assert r.status_code == 200

    assert captured.get("states") is None, (
        f"states should be None when no chip selection, got {captured.get('states')!r}"
    )


# ── 5. JS contract: states=… built from the state chip ────────────────


def test_search_js_sends_states_param_from_state_chip():
    """search.js _refetch() builds the ``&states=…`` query segment from
    selectedState. The condition ``if (this.selectedState) url +=
    '&states=' + …`` is the contract."""
    src = _read_search_js()
    assert "url += '&states=' + encodeURIComponent(this.selectedState)" in src, (
        "search.js _refetch() no longer appends &states= when state chip is set"
    )
