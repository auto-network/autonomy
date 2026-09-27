"""The software-update tile and the welcome preference, in JSDOM (auto-n25fc).

The coverage lives in ``software_update_menu.test.js``: the real
``identity-indicator.js`` panel and the real ``welcomeApp`` preference methods,
driven through their handlers against a routed, logged fetch. This wrapper puts
that suite in the ordinary Python run, and pins the structural rule that the
menu reads the cached status only (graph://89d3c8df-544 §6: no fetch on open).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
INDICATOR_JS = (_HERE.parent / "static" / "js" / "identity-indicator.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_software_update_menu_jsdom():
    result = subprocess.run(
        ["node", "--test", str(_HERE / "software_update_menu.test.js")],
        capture_output=True, text=True, timeout=120, cwd=str(_HERE),
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_panel_open_reads_only_the_cached_status():
    """loadUpdateStatus (run on panel open) requests ?fetch=0 and nothing else;
    the fetching form is issued only by the manual check (runCheck)."""
    load = INDICATOR_JS[INDICATOR_JS.index("function loadUpdateStatus"):
                        INDICATOR_JS.index("function onSoftwareUpdateEvent")]
    requests = re.findall(r"fetch\('([^']+)'", load)
    assert requests == ["/api/software/update-status?fetch=0"]
    check = INDICATOR_JS[INDICATOR_JS.index("async function runCheck"):
                         INDICATOR_JS.index("async function runUpdate")]
    assert "'/api/software/update-status'" in check


def test_no_other_partial_renders_the_update_tile():
    dashboard = _HERE.parent
    hits = [
        str(p.relative_to(dashboard)) for p in (dashboard / "static" / "js").rglob("*.js")
        if "vendor" not in p.parts and "identity-action-software" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == ["static/js/identity-indicator.js"]
