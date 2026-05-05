"""Unit tests for the ``relativeTime`` helper exposed by ``page.js``.

The helper drives every per-tile / per-thread / per-sprint "Nm ago"
label and the top-of-canvas snapshotTime line (bead auto-fwwfu).
Driving it through Node lets us pin the formatting thresholds the
operator specified verbatim — ``just now`` / ``Nm ago`` / ``Nh Mm ago``
/ localized date for >24h — without booting the dashboard.

These tests live with the plugin per the operator's structural rule
(graph://f6c6c43e-24a). The main pytest ``testpaths`` does not collect
them; run explicitly with::

    pytest tools/dashboard/plugins/coordinator_board/tests/
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PAGE_JS = PLUGIN_DIR / "page.js"


def _node_available() -> bool:
    return shutil.which("node") is not None


_NODE_DRIVER = r"""
// Stub the schema runtime — page.js falls back to ``state`` when
// Schema.alpine isn't a function, which is enough to surface the
// module-scope ``relativeTime`` since we re-export it.
global.window = global.window || {};
global.window.Schema = null;
const mod = require(%(page_js)s);
// page.js does not export relativeTime directly; reach it through eval
// against the module file. Reimport via Function to keep the helper
// reachable for tests without changing the plugin's public surface.
const src = require('fs').readFileSync(%(page_js)s, 'utf-8');
const fn = new Function(src + '\nreturn relativeTime;');
const relativeTime = fn();

const cases = %(cases)s;
const results = cases.map(c => {
  return { id: c.id, out: relativeTime(c.input) };
});
process.stdout.write(JSON.stringify(results));
"""


def _run_cases(cases: list[dict]) -> list[dict]:
    src = _NODE_DRIVER % {
        "page_js": json.dumps(str(PAGE_JS)),
        "cases": json.dumps(cases),
    }
    proc = subprocess.run(
        ["node", "-e", src],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node driver failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def _iso_offset_minutes(minutes: float) -> str:
    """Return an ISO timestamp ``minutes`` minutes before Date.now().

    Computed inside the Node subprocess so we don't drift across
    py↔node clock skew.
    """
    return f"__NOW_MINUS_MIN__:{minutes}"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestRelativeTime:
    """Stop-the-clock thresholds the operator pinned in bead auto-fwwfu."""

    def test_threshold_thresholds(self):
        # Run a single Node call that resolves the ``__NOW_MINUS_MIN__``
        # markers internally so wall-clock skew across the py↔node
        # boundary doesn't tip us across a threshold.
        # Set window.Schema to a non-null stub so page.js skips the
        # ``require('../../static/js/schemas.js')`` fallback that breaks
        # under a Function-eval'd source string.
        driver = (
            "const fs = require('fs'); "
            f"const src = fs.readFileSync({json.dumps(str(PAGE_JS))}, 'utf-8'); "
            "global.window = global.window || {}; "
            "global.window.Schema = { alpine: null, _setFetchOverride: () => {} }; "
            "const fn = new Function(src + '\\nreturn relativeTime;'); "
            "const relativeTime = fn(); "
            "const now = Date.now(); "
            "function iso(secAgo) { return new Date(now - secAgo * 1000).toISOString(); } "
            "const out = { "
            "  empty: relativeTime(''), "
            "  bogus: relativeTime('not-an-iso'), "
            "  thirtyS: relativeTime(iso(30)), "
            "  fiveM: relativeTime(iso(5 * 60)), "
            "  fiftyNineM: relativeTime(iso(59 * 60 + 30)), "
            "  oneH: relativeTime(iso(60 * 60)), "
            "  oneHThirty: relativeTime(iso(90 * 60)), "
            "  twoH: relativeTime(iso(2 * 60 * 60)), "
            "  twentySixH: relativeTime(iso(26 * 60 * 60)), "
            "}; "
            "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(
            ["node", "-e", driver],
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0, (
            f"node driver failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        out = json.loads(proc.stdout)
        assert out["empty"] == ""
        assert out["bogus"] == ""
        assert out["thirtyS"] == "just now", out
        assert out["fiveM"] == "5m ago", out
        assert out["fiftyNineM"] == "59m ago", out
        assert out["oneH"] == "1h ago", out
        assert out["oneHThirty"] == "1h 30m ago", out
        assert out["twoH"] == "2h ago", out
        # >24h — falls through to a localized date string. We can't pin
        # the exact format (locale-dependent) but it must be non-empty,
        # contain digits, and contain the year.
        twenty_six = out["twentySixH"]
        assert twenty_six and len(twenty_six) > 5, twenty_six
        assert any(c.isdigit() for c in twenty_six), twenty_six
        # Locale rendering varies but the year always appears as 4 digits
        # somewhere in the output for the wide-time format we requested.
        from datetime import datetime, timezone
        year = str(datetime.now(timezone.utc).year)
        # Allow the test to span a year boundary by also accepting last year.
        last_year = str(datetime.now(timezone.utc).year - 1)
        assert year in twenty_six or last_year in twenty_six, twenty_six
