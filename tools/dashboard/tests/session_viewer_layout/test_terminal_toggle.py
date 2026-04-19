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


def test_toggle_button_rendered_when_tmux_present(test_client):
    """Header row1 contains the terminal toggle when _tmuxSession is set."""
    resp = test_client.get("/pages/session-view")
    assert resp.status_code == 200
    html = resp.text
    assert 'class="sv-term-toggle"' in html or "class='sv-term-toggle'" in html, \
        "sv-term-toggle button missing from template"
    # Must reference showTerminal state and toggleTerminal method
    assert 'toggleTerminal' in html
    assert 'showTerminal' in html


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
    """base.html must set overflow-y:auto + touch-action:pan-y on
    .sv-terminal .xterm-viewport so iPhone finger-drag reaches scrollback
    (auto-bvob2). Without these hints xterm's viewport renders non-scrollable
    on iOS even though wheel events work on desktop.
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
