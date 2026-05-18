"""L1 tests for the expanded session header.

Verifies the three guarantees added by the expanded-header bead:
  1. Compact-row title is static — never contenteditable.
  2. Expanded block references the four required getters (topics, entryCount,
     contextTokens, lastActivity).
  3. session-stats.js is served and exposes window.SessionStats with all
     five formatters.

Uses the test_client fixture from tests/conftest.py.
"""

from pathlib import Path
import re


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
    """Expanded block must reference the data it renders in the drawer."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    for field in ('topics', 'entryCount', 'contextTokens', 'lastActivity', 'workspaceName'):
        assert field in html, f"Expanded block missing reference to {field}"


def test_expanded_block_uses_workspace_tile_and_copyable_tmux(test_client):
    """Drawer stats must expose the workspace label and tap-to-copy tmux value."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert '>WORKSPACE<' in html, "Workspace tile label missing from expanded header"
    assert 'copyTmuxSession()' in html, "TMUX value is not wired to copy on tap"


def test_session_viewer_js_reads_workspace_setting_and_copy_helper(test_client):
    """The page controller must resolve workspace names via Settings and support clipboard copy."""
    resp = test_client.get("/static/js/pages/session-viewer.js")
    assert resp.status_code == 200
    body = resp.text
    assert "window.Schema.of('autonomy.workspace')" in body, (
        "session-viewer.js is not using the JS schema runtime for workspace lookup"
    )
    assert 'Workspace.read(workspaceId)' in body, (
        "session-viewer.js is not reading workspace Settings by key"
    )
    assert 'copyTmuxSession' in body, "session-viewer.js missing TMUX copy helper"


def test_session_stats_lib_served(test_client):
    """SessionStats formatters must be exposed as a standalone script."""
    resp = test_client.get("/static/js/lib/session-stats.js")
    assert resp.status_code == 200
    body = resp.text
    assert 'window.SessionStats' in body
    for fn in ('turnsStr', 'ctxStr', 'ctxWarn', 'idleStr', 'recencyColor'):
        assert fn in body, f"session-stats.js missing formatter: {fn}"


def test_sv_input_rule_uses_closed_state_safe_area_padding():
    """The grid-shell composer should reserve the validated resting safe-area inset."""
    base_html = Path(__file__).resolve().parents[2] / "templates" / "base.html"
    css = base_html.read_text(encoding="utf-8")
    match = re.search(r"\.sv-input\s*\{(?P<body>.*?)\n\s*\}", css, re.S)
    assert match, "Could not locate .sv-input CSS rule in base.html"
    rule = match.group("body")
    assert "max(0px, calc(var(--sab) - 8px))" in rule
