"""Tests for the search chrome polish round (auto-gsu99).

This bead refines the filter strip shipped in auto-zvu3z (commit 4d7e440)
along four axes:

  1. Tighter top padding above the org/state chip row (4px not 10px).
  2. The "All orgs" infinity glyph is muted dark, not a rainbow gradient.
  3. The State dropdown becomes a *minimum-state* ladder
     (Raw → Curated → Published → Canonical), with progressive bar
     glyphs communicating restrictiveness.
  4. The empty-state and loading messages clear the sticky filter strip
     instead of being tucked under it. ``/search`` (no q) hides the chip
     rail entirely and shows a centered hint.

The minimum-state semantic is the load-bearing change: picking a state
now means "this state OR more restrictive". Raw is the floor (sends
``include_raw=1`` so it really means "any state, anywhere", including
raw rows from other sessions); Canonical is the ceiling.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient


# ── Helpers ───────────────────────────────────────────────────────────

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "pages" / "search.html"
_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "pages" / "search.js"


def _read_template() -> str:
    return _TEMPLATE.read_text()


def _read_js() -> str:
    return _JS.read_text()


def _extract_css_block(css: str, selector: str) -> str:
    """Return the body of the first ``selector { … }`` rule in ``css``.

    Selectors may contain regex-special characters (``.``, etc.) — those
    are escaped before matching. Whitespace inside the rule body is
    preserved so the caller can grep individual property declarations.
    """
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, css)
    assert match is not None, f"CSS selector {selector!r} not found"
    return match.group(1)


def _state_options_block(js: str) -> str:
    """Return the JS literal for the STATE_OPTIONS array (raw text).

    Used by the option-order / bar-count tests to introspect the
    declared mapping without executing JS.
    """
    match = re.search(
        r"var\s+STATE_OPTIONS\s*=\s*\[(.*?)\];", js, flags=re.DOTALL
    )
    assert match is not None, "STATE_OPTIONS array not found in search.js"
    return match.group(1)


# ── 1. Filter strip top padding ───────────────────────────────────────


def test_filter_strip_padding_top_reduced(test_app):
    """The sticky filter-strip's ``padding-top`` is ≤ 6px after the polish.

    The previous chrome (auto-zvu3z) used 10px, which felt airy at
    desktop width. The design experiment (a34b7927-3ef) tightens it to
    4px. Allow 6px slack so a future tweak to 5/6px doesn't break this.
    """
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    body = _extract_css_block(html, ".sp-header")
    # Match either ``padding-top: Npx`` or the shorthand ``padding: N…``.
    pt = None
    m = re.search(r"padding-top\s*:\s*(\d+)px", body)
    if m:
        pt = int(m.group(1))
    else:
        m = re.search(r"padding\s*:\s*(\d+)px", body)
        assert m is not None, (
            f".sp-header has no padding declaration: {body!r}"
        )
        pt = int(m.group(1))
    assert pt <= 6, (
        f".sp-header padding-top should be tightened to ≤ 6px (was 10px), "
        f"got {pt}px"
    )


# ── 2. All-orgs glyph muted, not rainbow ──────────────────────────────


def test_all_orgs_glyph_not_rainbow(test_app):
    """The All-orgs chip glyph is solid muted gray, not a linear-gradient.

    Operator feedback: the "no filter applied" state shouldn't be the
    most colorful element on the page. The polish swaps the multi-colour
    gradient for ``#2a3441`` background + ``#6b7280`` foreground.
    """
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    body = _extract_css_block(html, ".sp-filter-chip-glyph.sp-filter-chip-all")
    assert "linear-gradient" not in body, (
        f".sp-filter-chip-all still uses a gradient: {body!r}"
    )
    # And it has a solid background of the muted dark colour.
    assert re.search(r"background\s*:\s*#2a3441", body), (
        f".sp-filter-chip-all should use solid #2a3441 background, got {body!r}"
    )


def test_all_orgs_dropdown_glyph_not_rainbow(test_app):
    """The matching "All orgs" entry inside the org dropdown also drops
    its rainbow treatment so the chip and dropdown stay visually
    consistent."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    body = _extract_css_block(
        html, ".sp-org-option-glyph.sp-org-option-glyph-all"
    )
    assert "linear-gradient" not in body, (
        f".sp-org-option-glyph-all still uses a gradient: {body!r}"
    )


# ── 3. State dropdown ordering ────────────────────────────────────────


def test_state_dropdown_options_ordered_raw_to_canonical():
    """STATE_OPTIONS in search.js is declared least → most restrictive
    (Raw, Curated, Published, Canonical) — NOT alphabetical.

    The order is the load-bearing thing: top of the dropdown is the
    floor (Raw = "any"), bottom is the ceiling (Canonical = strictest).
    """
    js = _read_js()
    block = _state_options_block(js)
    keys = re.findall(r"key:\s*'([^']+)'", block)
    assert keys == ["raw", "curated", "published", "canonical"], (
        f"STATE_OPTIONS keys should be ordered "
        f"raw → curated → published → canonical, got {keys!r}"
    )


# ── 4. Each option carries a progress-bar count ───────────────────────


def test_state_dropdown_each_option_has_progress_bars():
    """Each STATE_OPTIONS entry carries a ``bars`` count from 1..4 in
    increasing order, which drives the option's bar-glyph render in the
    dropdown.

    Mapping:  Raw=1  Curated=2  Published=3  Canonical=4.
    """
    js = _read_js()
    block = _state_options_block(js)
    # Pull (key, bars) pairs in declaration order.
    pairs = re.findall(
        r"key:\s*'([^']+)'.*?bars:\s*(\d+)", block, flags=re.DOTALL,
    )
    assert pairs == [
        ("raw", "1"),
        ("curated", "2"),
        ("published", "3"),
        ("canonical", "4"),
    ], (
        f"STATE_OPTIONS bar counts should be raw=1, curated=2, "
        f"published=3, canonical=4 in order, got {pairs!r}"
    )


def test_state_dropdown_options_render_bar_glyph_in_template(test_app):
    """The dropdown template renders a bar-glyph ladder element inside
    each option (the ``data-testid="sp-state-option-bars"`` hook)."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert 'data-testid="sp-state-option-bars"' in html, (
        "dropdown options no longer render a bar-glyph ladder"
    )
    # And the chip glyph itself becomes a bar ladder rather than a static icon.
    assert 'data-testid="sp-state-chip-glyph"' in html, (
        "state chip's bar-glyph wrapper missing"
    )


# ── 5/6/7. Minimum-state mapping (JS contract) ────────────────────────
#
# The JS lives in search.js. The integration tests below use the
# `_state_option_for(key)` helper to assert the in-memory mapping that
# `_refetch()` consumes.


def _state_option_for(js: str, key: str) -> dict:
    """Parse STATE_OPTIONS from search.js and return the entry for ``key``.

    The parser is structural rather than executing JS — it reads the
    object literal as text and pulls out the fields the test cares
    about (key, states list, includeRaw flag, bars, hint).
    """
    block = _state_options_block(js)
    # Each option is on its own line. Split on `},` boundaries (the
    # closing brace of each entry) to isolate per-entry text.
    raw_entries = re.split(r"\}\s*,", block)
    for raw in raw_entries:
        m = re.search(r"key:\s*'([^']+)'", raw)
        if not m or m.group(1) != key:
            continue
        states_match = re.search(r"states:\s*(\[[^\]]*\]|null)", raw)
        include_raw_match = re.search(r"includeRaw:\s*(true|false)", raw)
        bars_match = re.search(r"bars:\s*(\d+)", raw)
        states_raw = states_match.group(1) if states_match else None
        if states_raw is None or states_raw == "null":
            states = None
        else:
            states = re.findall(r"'([^']+)'", states_raw)
        return {
            "key": key,
            "states": states,
            "include_raw": include_raw_match.group(1) == "true"
                if include_raw_match else False,
            "bars": int(bars_match.group(1)) if bars_match else None,
        }
    raise AssertionError(f"STATE_OPTIONS entry {key!r} not found")


def test_state_filter_minimum_state_mapping_curated():
    """Picking ``Curated`` sends ``?states=curated,published,canonical``
    to /api/search — that's the minimum-state semantic ("Curated or
    more restrictive"). And NO ``include_raw`` is sent on this branch.
    """
    js = _read_js()
    opt = _state_option_for(js, "curated")
    assert opt["states"] == ["curated", "published", "canonical"], (
        f"Curated should map to states=[curated, published, canonical], "
        f"got {opt['states']!r}"
    )
    assert opt["include_raw"] is False, (
        "Curated must NOT set include_raw — that's only for the Raw floor"
    )
    # And _refetch joins opt.states with commas (the on-the-wire form).
    assert (
        "url += '&states=' + encodeURIComponent(opt.states.join(','))" in js
    ), "search.js _refetch() no longer joins opt.states for the &states= URL"


def test_state_filter_minimum_state_mapping_canonical():
    """Picking ``Canonical`` sends ``?states=canonical`` (only the
    ceiling state — strictest possible filter). NO ``include_raw``."""
    js = _read_js()
    opt = _state_option_for(js, "canonical")
    assert opt["states"] == ["canonical"], (
        f"Canonical should map to states=[canonical], got {opt['states']!r}"
    )
    assert opt["include_raw"] is False, (
        "Canonical must NOT set include_raw"
    )


def test_state_filter_published_includes_canonical():
    """Picking ``Published`` sends both ``published`` AND ``canonical``
    (canonical is more restrictive than published, so it qualifies)."""
    js = _read_js()
    opt = _state_option_for(js, "published")
    assert opt["states"] == ["published", "canonical"], (
        f"Published should map to states=[published, canonical], "
        f"got {opt['states']!r}"
    )
    assert opt["include_raw"] is False


def test_state_filter_raw_sends_include_raw_no_states():
    """Picking ``Raw`` (the default / floor) sends ``?include_raw=1`` and
    NO ``?states=`` segment.

    Critical: "no states param" alone is NOT equivalent to "show
    everything" — db.py's default state filter excludes raw rows from
    *other sessions*. ``include_raw=1`` clears that filter, which is
    what the user's mental model of Raw = "any state, anywhere"
    requires.
    """
    js = _read_js()
    opt = _state_option_for(js, "raw")
    assert opt["states"] is None, (
        f"Raw must NOT carry a states filter, got {opt['states']!r}"
    )
    assert opt["include_raw"] is True, (
        "Raw must set include_raw=true to clear the cross-session filter"
    )
    # And _refetch routes the includeRaw branch to ?include_raw=1.
    assert "url += '&include_raw=1'" in js, (
        "search.js _refetch() no longer appends &include_raw=1 for Raw"
    )


def test_state_filter_raw_returns_raw_rows_from_other_sessions(test_app):
    """End-to-end plumbing: ``?include_raw=1`` reaches ops.search as
    ``include_raw=True``. db.py uses that flag to drop the
    "exclude raw rows from other sessions" filter, so a Raw chip on
    session A's dashboard surfaces session B's raw notes.

    Two-session DB setup is heavy for a unit test — instead, we verify
    the plumbing from URL → server → ops.search. The db.py layer is
    independently covered by graph-level tests; the contract this test
    enforces is that the search page's Raw chip activates that path.
    """
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get("/api/search?q=worktree&include_raw=1")
            assert r.status_code == 200

    assert captured["q"] == "worktree"
    assert captured.get("include_raw") is True, (
        f"include_raw=1 did not reach ops.search, got "
        f"include_raw={captured.get('include_raw')!r}"
    )
    # And no states= clamp leaks in alongside the Raw branch.
    assert captured.get("states") in (None, []), (
        f"Raw should clear states=, got states={captured.get('states')!r}"
    )


def test_state_filter_curated_passes_three_states_through_to_ops_search(test_app):
    """End-to-end plumbing for the Curated branch: ``?states=curated,
    published,canonical`` reaches ops.search as a 3-element list."""
    from tools.dashboard import server

    captured: dict = {}

    def fake_search(q, **kwargs):
        captured["q"] = q
        captured.update(kwargs)
        return []

    with patch.object(server.graph_ops, "search", side_effect=fake_search):
        with TestClient(test_app) as client:
            r = client.get(
                "/api/search?q=x&states=curated,published,canonical"
            )
            assert r.status_code == 200

    assert captured.get("states") == ["curated", "published", "canonical"], (
        f"Curated mapping did not split into three states, "
        f"got {captured.get('states')!r}"
    )
    assert captured.get("include_raw") is False, (
        "Curated branch must NOT set include_raw"
    )


# ── 8. Empty-state padding ────────────────────────────────────────────


def test_no_results_message_padding(test_app):
    """The empty-state container has > 0 top padding so the "No results
    for X" message lands below the sticky filter strip rather than
    being clipped by it on a fast-render."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    body = _extract_css_block(html, ".sp-empty-state")
    # padding-top OR padding shorthand must be > 0.
    pad = 0
    m = re.search(r"padding-top\s*:\s*(\d+)px", body)
    if m:
        pad = int(m.group(1))
    else:
        m = re.search(r"padding\s*:\s*(\d+)px", body)
        if m:
            pad = int(m.group(1))
    margin = 0
    m = re.search(r"margin-top\s*:\s*(\d+)px", body)
    if m:
        margin = int(m.group(1))
    assert pad > 0 or margin > 0, (
        f".sp-empty-state should have padding-top or margin-top > 0 "
        f"so it clears the sticky strip, got {body!r}"
    )
    # The empty-state markup uses this class (not the bare text-gray-500
    # span which had no padding).
    assert 'data-testid="sp-empty-state"' in html, (
        "empty-state markup should be tagged with data-testid=sp-empty-state"
    )


def test_loading_state_also_clears_sticky_strip(test_app):
    """Same fix on the ``Searching…`` loading state — top padding so it
    doesn't tuck under the sticky strip on a fast load."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    # Loading uses the same .sp-empty-state class (single source of truth).
    assert 'data-testid="sp-loading-state"' in html, (
        "loading state markup not tagged"
    )
    # And it lives inside a <div class="sp-empty-state">.
    loading_segment = html[html.find('data-testid="sp-loading-state"') - 200:
                           html.find('data-testid="sp-loading-state"') + 200]
    assert "sp-empty-state" in loading_segment, (
        "loading state should reuse .sp-empty-state class for the same "
        f"top-padding fix, got context: {loading_segment!r}"
    )


# ── 9. Empty-query state hides chip rail ──────────────────────────────


def test_empty_query_state_hides_chip_rail(test_app):
    """When ``query === ''`` the entire filter strip (and chip rail
    inside it) is hidden — only the centered "type a query" hint shows.

    The strip's hidden via ``x-show="query !== ''"`` on the wrapper, so
    Alpine renders nothing visible when the page lands on /search with
    no q= param.
    """
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    # The filter-strip wrapper carries the visibility guard.
    strip_idx = html.find('data-testid="sp-filter-strip"')
    assert strip_idx >= 0, "filter strip element missing"
    # Pull the opening tag of the wrapper to inspect its directives.
    tag_start = html.rfind("<div", 0, strip_idx)
    tag_end = html.find(">", strip_idx) + 1
    open_tag = html[tag_start:tag_end]
    assert "x-show=" in open_tag and "query !== ''" in open_tag, (
        f"filter-strip wrapper missing x-show=\"query !== ''\" guard, "
        f"got: {open_tag!r}"
    )


def test_empty_query_renders_centered_hint(test_app):
    """The empty-query branch renders a single centered hint instead of
    the chip rail / "No results for ''" pair the old chrome left
    behind."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    assert 'data-testid="sp-no-query-hint"' in html, (
        "empty-query hint element missing"
    )
    # The hint template fires only when query is empty.
    hint_idx = html.find('data-testid="sp-no-query-hint"')
    template_start = html.rfind("<template", 0, hint_idx)
    template_open = html[template_start:html.find(">", template_start) + 1]
    assert "query === ''" in template_open, (
        f"sp-no-query-hint template guard should fire on query==='', "
        f"got: {template_open!r}"
    )


def test_empty_query_summary_row_hidden(test_app):
    """The summary row ("N sources · M matches for …") must not render
    when there's no query — its x-show guard should include the
    ``query !== ''`` clause so the empty-query state stays clean."""
    with TestClient(test_app) as client:
        html = client.get("/pages/search").text
    # Skip past the <style> block so we find the actual element, not the
    # CSS rule with the same class name.
    body_idx = html.find("</style>")
    assert body_idx >= 0, "<style> block missing"
    summary_idx = html.find('class="sp-summary-row"', body_idx)
    assert summary_idx >= 0, "summary row element missing in markup"
    tag_start = html.rfind("<div", 0, summary_idx)
    tag_end = html.find(">", summary_idx) + 1
    open_tag = html[tag_start:tag_end]
    assert "query !== ''" in open_tag, (
        f"summary row x-show should require query !== '' to avoid "
        f"rendering on /search (no q), got: {open_tag!r}"
    )
