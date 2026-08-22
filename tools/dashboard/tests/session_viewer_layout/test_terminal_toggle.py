"""L1 tests for the session viewer terminal toggle.

Verifies that the session-view template and refactored mount helper deliver
the pieces required to swap the chat body for a full-screen xterm view
(bead auto-dkhyx):

  1. Compact header exposes the `sv-term-toggle` button and wires it to
     the Alpine `toggleTerminal()` method / `showTerminal` state.
  2. The terminal mount region (`x-ref="termContainer"`, `.sv-terminal`)
     is in the template.
  3. Entries/input are gated on `!showTerminal` so the toggle actually
     swaps surfaces.
  4. `/static/js/lib/terminal-mount.js` is served and exposes
     `window.mountTerminal` that opens a /ws/terminal WebSocket.
  5. The existing /terminal page still renders unchanged after the
     refactor extracted the xterm bridge into the shared helper.

Uses the test_client fixture from tests/conftest.py.
"""

from pathlib import Path


def test_toggle_button_rendered_when_tmux_present(test_client):
    """Header row1 contains the ESC action and terminal toggle when tmux is set."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert 'class="sv-term-toggle"' in html or "class='sv-term-toggle'" in html, \
        "sv-term-toggle button missing from template"
    assert 'sv-term-escape' in html, "Header ESC button missing from template"
    assert 'interrupt()' in html, "Header ESC button is not wired to interrupt()"
    # Must reference showTerminal state and toggleTerminal method
    assert 'toggleTerminal' in html
    assert 'showTerminal' in html


def test_header_background_button_rendered_for_claude_harness(test_client):
    """Claude harness sessions get a Ctrl-B header button next to ESC.

    Sends C-b via tmux to background the running Claude tool instead of
    cancelling it (which is what ESC does).
    """
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert 'sv-term-background' in html, \
        "Header Ctrl-B button missing from template"
    assert 'background()' in html, \
        "Header Ctrl-B button is not wired to background()"
    assert 'isClaudeHarness' in html, \
        "Header Ctrl-B button must be gated on isClaudeHarness"


def test_header_renders_plugin_contributions_before_escape(test_client):
    """Plugin icon actions occupy the compact return-control slot before ESC."""
    page = test_client.get("/pages/session-view")
    assert page.status_code == 200
    html = page.text
    assert 'data-testid="session-viewer-contribution-action"' in html
    assert 'sessionContributionActions' in html
    assert 'openSessionContribution(item)' in html
    assert html.index('session-viewer-contribution-action') < html.index('sv-term-escape')

    script = test_client.get("/static/js/pages/session-viewer.js")
    assert script.status_code == 200
    body = script.text
    assert "service.forSession(this.sessionKey, 'action')" in body
    assert "service.forSession(this.sessionKey, 'badge')" in body
    assert "service.load([key])" in body


def test_shared_session_card_renders_plugin_badges_and_actions(test_client):
    templates = Path(__file__).parents[2] / "templates"
    card = (templates / "partials/session-card.html").read_text()
    assert "partials/session-contributions.html" in card
    html = (templates / "partials/session-contributions.html").read_text()
    assert 'data-testid="session-card-contribution"' in html
    assert "forSession(s.session_id || s.tmux_session || s.id)" in html


def test_tile_background_button_rendered_for_claude_harness(test_client):
    """Running tool tiles in Claude harness sessions get a Ctrl-B button next
    to the existing Esc interrupt button."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert 'sc-background-btn' in html, \
        "Per-tile Ctrl-B button missing from session-entries template"
    assert 'Ctrl-B' in html, \
        "Per-tile Ctrl-B button label missing"


def test_terminal_container_div_present(test_client):
    """Template includes x-ref='termContainer' mounting div."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert 'sv-terminal' in html
    assert 'termContainer' in html


def test_entries_and_input_gated_on_show_terminal(test_client):
    """Entries div and input template must be gated by !showTerminal."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    # At least one gating of entries or input by !showTerminal
    assert '!showTerminal' in html, \
        "entries/input must hide when showTerminal is true"


def test_mount_terminal_lib_served(test_client):
    resp = test_client.get("/static/js/lib/terminal-mount.js")
    assert resp.status_code == 200
    assert 'mountTerminal' in resp.text
    assert '/ws/terminal' in resp.text


def test_existing_terminal_page_still_works(test_client):
    """Refactor must not break the standalone terminal page."""
    resp = test_client.get("/pages/terminal")
    assert resp.status_code == 200
    assert 'terminalPage' in resp.text
    assert 'terminal-container' in resp.text


def test_xterm_viewport_touch_scroll_css_present(test_client):
    """The native-scroll CSS remains as a supplement to the JS touch bridge.

    Native scrolling alone is insufficient while an interactive TUI enables
    xterm mouse reporting, but these hints still cover ordinary scrollback.
    """
    resp = test_client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert '.sv-terminal .xterm .xterm-viewport' in html, \
        "selector targeting .xterm-viewport missing from base.html"
    # Grep the declared properties — keep the assertion loose so reordering
    # inside the rule doesn't break it.
    assert 'overflow-y: auto' in html
    assert 'touch-action: pan-y' in html


def test_terminal_mount_bridges_touch_drag_to_wheel(test_client):
    """Touch drag must use xterm's wheel path, which works with mouse-reporting
    TUIs as well as ordinary xterm scrollback.
    """
    resp = test_client.get("/static/js/lib/terminal-mount.js")
    assert resp.status_code == 200
    js = resp.text
    assert "installTouchScrollBridge" in js
    assert "new WheelEvent('wheel'" in js
    assert "passive: false" in js
    assert "removeTouchScrollBridge()" in js
