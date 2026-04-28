"""Tests for the search-page filter-strip chrome (auto-zvu3z).

The /search page chrome was restructured from a "search input + back button +
chip rail" into a three-row filter strip:

  Row 1: Org chip + Publication-state chip
  Row 2: Horizontal scrolling type pills (kept from auto-bcxdr)
  Row 3: Summary row (kept from auto-bcxdr)

The page must NOT render its own search input or back button — the global
"Search graph…" header input is the canonical query input.
"""

from __future__ import annotations

from starlette.testclient import TestClient


# ── No back button, no page-level search input ───────────────────────


def test_search_page_has_no_back_button(test_app):
    """The page-level back button is gone — operators use the global header /
    SPA navigation instead."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert 'class="sp-back-btn"' not in html, (
        "back button still present in search chrome"
    )
    # The legacy back-button glyph (U+2039) should not survive either.
    assert "goBack()" not in html, (
        "goBack() handler still wired in search chrome"
    )


def test_search_page_has_no_page_level_search_input(test_app):
    """No ``<input class="sp-search-field">`` — the canonical query input
    is the global header's #global-search field, not a per-page one."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert "sp-search-field" not in html, (
        "page-level search-field input still present"
    )
    # And no visible <input> on the page at all (the dropdown options are
    # buttons, not inputs).
    assert "<input" not in html, (
        "search page should not render any <input> — found one in: "
        + html[: html.find("<input") + 200]
    )


# ── Filter strip row 1: org chip + state chip ─────────────────────────


def test_search_page_filter_strip_row_1_has_org_and_state_chips(test_app):
    """Row 1 of the filter strip has the org chip + the publication-state
    chip, both with their data-testid hooks."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    # Filter strip wrapper exists.
    assert 'data-testid="sp-filter-strip"' in html, (
        "filter strip container missing"
    )
    # Org chip — kept from auto-13134, just relocated inside the strip.
    assert 'data-testid="sp-org-chip"' in html, "org chip missing"
    # Publication-state chip — new for auto-zvu3z.
    assert 'data-testid="sp-state-chip"' in html, "state chip missing"
    assert 'data-testid="sp-state-dropdown"' in html, "state dropdown missing"
    # The state chip's three configurable values are wired into the JS.
    # (The values themselves render client-side from STATE_OPTIONS.)


# ── Row 2: type pills (kept) ──────────────────────────────────────────


def test_search_page_type_pills_row_2_present(test_app):
    """The horizontal-scrolling type pills still render — they live in row
    2 of the filter strip, after the chip row."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert 'data-testid="sp-chip-rail"' in html, "type-pill rail missing"
    # The All chip + the dynamic pill template both render via Alpine.
    assert "setType('all')" in html
    assert 'x-for="t in chipTypes"' in html


# ── Row 3: summary row (kept) ─────────────────────────────────────────


def test_search_page_summary_row_present(test_app):
    """The summary row ("N sources · M matches for …") still renders."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert "sp-summary-row" in html, "summary row missing"
    # The summary echoes the query — the binding still references it.
    assert "sp-summary-query" in html


# ── Result cards unchanged ────────────────────────────────────────────


def test_search_page_result_cards_still_render(test_app):
    """Card layout is unchanged from auto-bcxdr — only the chrome above
    them was restructured. The accent-rail-by-source-type binding and the
    short_description block still ship."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert 'data-testid="sp-results"' in html
    assert 'class="sp-source-card"' in html
    assert "railClass(r.source_type)" in html
    # short_description block from the prior round still renders.
    assert 'data-testid="sp-short-description"' in html


# ── Global input listener wiring ──────────────────────────────────────


def test_search_page_listens_for_global_search_events(test_app):
    """The Alpine root listens for ``global-search:input`` and
    ``global-search:enter`` window events — that's the contract by which
    the global header input drives this page's query."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert "@global-search:input.window=" in html
    assert "@global-search:enter.window=" in html
