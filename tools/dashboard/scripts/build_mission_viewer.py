#!/usr/bin/env python3
"""Build the Mission viewer bootstrap: compile its CSS and inline it.

The bootstrap travels inside artifact bytes under a CSP that permits no
external script or stylesheet, and its chrome lives in a CLOSED shadow root,
so the CSS has to be a string inside that root. Neither a <link> nor a
document-level <style> would work.

Output is gitignored and built on demand. It is never committed -- see commit
628572bd for what a committed build output cost when three commits edited the
artifact instead of its source.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

VIEWER = Path(__file__).resolve().parents[1] / "plugins" / "mission_control" / "viewer"
SOURCE = VIEWER / "bootstrap.js"
CSS_IN = VIEWER / "chrome.css"
OUTPUT = VIEWER / ".build" / "bootstrap.js"
MARKER = "/*__CHROME_CSS__*/"


def sources() -> list[Path]:
    return [SOURCE, CSS_IN]


def build() -> str:
    binary = shutil.which("tailwindcss")
    if not binary:
        raise RuntimeError(
            "tailwindcss not on PATH; the dashboard image bakes it in and "
            "mock_server._ensure_tailwind_css downloads it otherwise"
        )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    css_out = OUTPUT.parent / "chrome.css"
    subprocess.run(
        [binary, "--cwd", str(VIEWER), "-i", CSS_IN.name, "-o", str(css_out)],
        check=True, timeout=180,
    )
    css = css_out.read_text()
    text = SOURCE.read_text()
    if text.count(MARKER) != 1:
        raise RuntimeError(f"expected exactly one {MARKER} in bootstrap.js")
    # The CSS becomes a JS string literal, so anything that would end the
    # literal or the enclosing <script> has to be neutralised.
    escaped = (css.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("</script", "<\\/script"))
    return text.replace(MARKER, escaped)


def bootstrap_source() -> str:
    """Built text, rebuilt whenever a source is newer than the output."""
    newest = max(p.stat().st_mtime for p in sources())
    if not OUTPUT.exists() or OUTPUT.stat().st_mtime < newest:
        OUTPUT.write_text(build())
    return OUTPUT.read_text()


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build())
    print(f"built {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
