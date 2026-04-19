"""L1 tests for the expanded session header.

Verifies the three guarantees added by the expanded-header bead:
  1. Compact-row title is static — never contenteditable.
  2. Expanded block references the four required getters (topics, entryCount,
     contextTokens, lastActivity).
  3. session-stats.js is served and exposes window.SessionStats with all
     five formatters.

Uses the test_client fixture from tests/conftest.py.
"""


def test_title_not_contenteditable(test_client):
    """Compact-row title must be static text — no contenteditable, no blur handler."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    # The compact-row title spans must not be contenteditable or have a blur
    # handler. The only contenteditable elements in this template are the
    # composer (.sv-editable) and the expanded-panel label (.sv-exp-label-edit).
    title_spans = [line for line in html.split('<span') if 'session-title' in line]
    assert title_spans, "No session-title spans found — template changed?"
    for span in title_spans:
        assert 'contenteditable' not in span, (
            f"Title span still contenteditable: {span[:200]}"
        )
        assert '@blur' not in span, (
            f"Title span still has blur handler: {span[:200]}"
        )


def test_expanded_block_references_required_getters(test_client):
    """Expanded block must reference topics, entryCount, contextTokens, lastActivity."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    for field in ('topics', 'entryCount', 'contextTokens', 'lastActivity'):
        assert field in html, f"Expanded block missing reference to {field}"


def test_session_stats_lib_served(test_client):
    """SessionStats formatters must be exposed as a standalone script."""
    resp = test_client.get("/static/js/lib/session-stats.js")
    assert resp.status_code == 200
    body = resp.text
    assert 'window.SessionStats' in body
    for fn in ('turnsStr', 'ctxStr', 'ctxWarn', 'idleStr', 'recencyColor'):
        assert fn in body, f"session-stats.js missing formatter: {fn}"
