"""Tests for the /collab page tab strip — bead auto-yn1gt.

Verify that the rendered ``/pages/collab`` HTML fragment has:
- the new five-tab order (Recent | Curated | Thoughts | Threads | Topics)
- a Recent tab block AND a Curated tab block, both rendering note cards
- the page-level capture input above the tabs
"""

from __future__ import annotations

import re

from starlette.testclient import TestClient


def _fetch_collab_fragment(test_app) -> str:
    with TestClient(test_app) as client:
        r = client.get("/pages/collab")
    assert r.status_code == 200, r.text
    return r.text


# Match the rendered tab labels in source order. Each ``<button class="collab-tab"``
# wraps a label followed by an Alpine-templated count span — assert on the
# label only (the count is rendered client-side).
_TAB_LABEL_RE = re.compile(
    r'<button class="collab-tab"[^>]*>\s*([A-Za-z]+)<span',
    re.IGNORECASE,
)


def test_collab_tab_order(test_app):
    """Tab order matches the spec: Recent | Curated | Thoughts | Threads | Topics."""
    html = _fetch_collab_fragment(test_app)
    labels = _TAB_LABEL_RE.findall(html)
    assert labels == ["Recent", "Curated", "Thoughts", "Threads", "Topics"], (
        f"unexpected tab order: {labels}"
    )


def test_collab_renders_recent_and_curated_blocks(test_app):
    """Both Recent and Curated tab bodies exist and render note cards."""
    html = _fetch_collab_fragment(test_app)
    assert 'data-testid="collab-recent"' in html, "Recent tab body missing"
    assert 'data-testid="collab-curated"' in html, "Curated tab body missing"
    # Each block iterates its data array via x-for, producing note cards.
    assert 'x-for="item in recent"' in html
    assert 'x-for="item in curated"' in html
    # And both bind the common note-card template.
    assert html.count("note-card") >= 2
    # Check both blocks bind the same card classes/handlers.
    assert html.count('borderClass(item)') >= 2
    assert html.count('typeClass(item)') >= 2


def test_collab_capture_input_above_tabs(test_app):
    """The page-level capture input renders before the tab strip."""
    html = _fetch_collab_fragment(test_app)
    capture_idx = html.find('data-testid="page-capture-input"')
    tab_idx = html.find('class="collab-tabs"')
    assert capture_idx != -1, "capture input not present"
    assert tab_idx != -1, "tab strip not present"
    assert capture_idx < tab_idx, (
        "page-capture input should appear above the tab strip"
    )
