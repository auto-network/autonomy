"""The /terminal page loads the terminal emulator before mounting it.

aa71cc94 made xterm.js load on first use through ensureTerminalLibs() and
updated the session viewer, but renderTerminal() in app.js kept calling
mountTerminal() directly. mountTerminal sets "connecting..." and then throws
on the missing Terminal global, so the page stayed on "connecting..." for
every session (Windows test node, 2026-09-27).
"""

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "static" / "app.js"


def test_render_terminal_awaits_the_emulator_before_mounting():
    src = APP.read_text()
    body = src[src.index("async function renderTerminal("):]
    body = body[: body.index("\nfunction ", 1) if "\nfunction " in body[1:] else len(body)]
    ensure = body.index("await window.ensureTerminalLibs()")
    mount = body.index("window.mountTerminal(")
    assert ensure < mount
