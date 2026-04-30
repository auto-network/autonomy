"""L2-style tests for the canvas-corner refresh button (bead auto-n64je).

The button's Alpine handler is ``onRefreshAll`` in
``tools/dashboard/plugins/coordinator_board/page.js``. Two regressions
the bead fixes:

* The decision row was never written — only a local ``loadBoard`` re-read
  fired, so the mediator never forwarded "operator requests a canvas
  refresh" to the coordinator session.
* ``refreshState`` raced ``loadBoard``'s completion: ``loadBoard`` reset
  the state to ``'idle'`` faster than the 700ms fallback timer, so the
  operator never saw the "Refresh requested" label.

These tests drive ``page.js`` in a Node subprocess so we exercise the
real Alpine factory without a headless browser. We stub the page's two
side-effecting helpers (``_readSet`` returning seeded members, and
``_writeSetting`` capturing writes) — that lets us verify the decision
payload, the verbatim ``target_session``, and the post-tap state in a
single eval.

The tests live with the plugin per the operator's structural rule
(comment 1ee79942 on ``graph://f6c6c43e-24a``); the main pytest
``testpaths = ["tools/dashboard/tests"]`` does NOT collect them. Run
explicitly with::

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


# ── Driver ──────────────────────────────────────────────────────────────
#
# The Node subprocess loads page.js, instantiates the factory, monkey-
# patches ``_readSet`` + ``_writeSetting`` against an in-memory state,
# runs the test snippet, then prints a JSON line on stdout. Each test
# captures stdout and asserts on the parsed dict.


_NODE_DRIVER = r"""
const { coordinatorBoard } = require(%(page_js)s);

// Minimal browser globals the factory touches.
global.window = global.window || {};
global.window.crypto = { randomUUID: () => 'fixed-uuid-for-test' };
global.window.Autonomy = { fetch: async () => ({ ok: true, json: async () => ({}) }) };

const c = coordinatorBoard();
const writes = [];
const seeded = %(seeded)s;

// ``_readSet`` is the only path loadBoard takes to fetch members.
// Return seeded members for the canvas set; an empty list for everything
// else so the rest of loadBoard runs without errors.
c._readSet = async (setId /*, targetRevision */) => {
  if (setId === 'dashboard.coordinator-canvas') {
    return seeded.canvas || [];
  }
  return [];
};

// Capture every decision-row write so the test can assert payload shape.
c._writeSetting = async (setId, key, payload, schemaRevision) => {
  writes.push({ setId, key, payload, schemaRevision: schemaRevision || null });
  return { id: 'stub' };
};

(async () => {
  %(snippet)s
})().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
"""


def _run(snippet: str, *, seeded: dict | None = None) -> dict:
    """Run a JS *snippet* against a fresh factory instance and return the JSON dict."""
    src = _NODE_DRIVER % {
        "page_js": json.dumps(str(PAGE_JS)),
        "seeded": json.dumps(seeded or {}),
        "snippet": snippet,
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


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestCanvasRefreshButton:
    """``onRefreshAll`` writes a decision row + settles to 'requested'."""

    def test_writes_refresh_request_decision_with_coord_session(self):
        """Acceptance #1 — operator tap writes a decision row whose
        ``target_session`` equals the coord-session pulled from the
        canvas member's key."""
        seeded = {
            "canvas": [
                {
                    "key": "auto-coord-9",
                    "payload": {"question": "ship or hold?"},
                    "updated_at": "2026-04-30T00:00:00Z",
                },
            ],
        }
        out = _run(
            """
            await c.loadBoard();
            c.onRefreshAll();
            // Drain microtasks so the awaited _writeSetting resolves.
            await new Promise((resolve) => setImmediate(resolve));
            return {
                writes,
                refreshState: c.refreshState,
                coordSession: c._coordSession,
            };
            """,
            seeded=seeded,
        )
        assert out["coordSession"] == "auto-coord-9", out
        assert len(out["writes"]) == 1, (
            f"expected exactly one decision write; got {out['writes']}"
        )
        w = out["writes"][0]
        assert w["setId"] == "dashboard.coordinator-decision", w
        payload = w["payload"]
        assert payload["kind"] == "refresh_request", payload
        assert payload["target_session"] == "auto-coord-9", payload
        # ``tile_id`` mirrors target_session for canvas-wide refreshes —
        # the existing decision schema requires the field; we point it at
        # the coordinator session itself so the row validates without a
        # schema bump.
        assert payload["tile_id"] == "auto-coord-9", payload

    def test_state_settles_to_requested_regardless_of_loadboard_timing(self):
        """Acceptance #3 — ``refreshState`` is ``'requested'`` after the
        tap, even though ``loadBoard`` typically completes before the
        decision write — i.e. the legacy 700ms-timer race is gone."""
        seeded = {
            "canvas": [
                {
                    "key": "auto-coord-fast",
                    "payload": {"question": "?"},
                    "updated_at": "2026-04-30T00:00:00Z",
                },
            ],
        }
        out = _run(
            """
            await c.loadBoard();
            // Wedge loadBoard so it deliberately resolves AFTER the
            // synchronous body of onRefreshAll runs — proves the post-
            // refresh state is NOT clobbered by a late loadBoard finish.
            const realLoadBoard = c.loadBoard.bind(c);
            c.loadBoard = () => new Promise((resolve) => {
                setTimeout(async () => {
                    await realLoadBoard();
                    resolve();
                }, 50);
            });
            c.onRefreshAll();
            // State right after the synchronous body:
            const stateAfterTap = c.refreshState;
            // Drain the microtask queue so _writeSetting and the
            // wedged loadBoard both resolve.
            await new Promise((resolve) => setTimeout(resolve, 120));
            return {
                stateAfterTap,
                stateAfterSettle: c.refreshState,
                writeCount: writes.length,
            };
            """,
            seeded=seeded,
        )
        assert out["stateAfterTap"] == "requested", (
            f"state right after the tap must be 'requested'; got {out}"
        )
        assert out["stateAfterSettle"] == "requested", (
            "delayed loadBoard completion must NOT reset refreshState — "
            f"the legacy race is regressing: {out}"
        )
        assert out["writeCount"] == 1, out

    def test_second_tap_dismisses_to_idle(self):
        """Acceptance #3 — tapping the button while it shows 'Refresh
        requested' returns it to idle; no second decision row is written."""
        seeded = {
            "canvas": [
                {
                    "key": "auto-coord-x",
                    "payload": {"question": "?"},
                    "updated_at": "2026-04-30T00:00:00Z",
                },
            ],
        }
        out = _run(
            """
            await c.loadBoard();
            c.onRefreshAll();
            await new Promise((resolve) => setImmediate(resolve));
            const stateAfterFirstTap = c.refreshState;
            const writesAfterFirstTap = writes.length;
            c.onRefreshAll();  // second tap — should dismiss only.
            await new Promise((resolve) => setImmediate(resolve));
            return {
                stateAfterFirstTap,
                writesAfterFirstTap,
                stateAfterSecondTap: c.refreshState,
                writesAfterSecondTap: writes.length,
            };
            """,
            seeded=seeded,
        )
        assert out["stateAfterFirstTap"] == "requested", out
        assert out["writesAfterFirstTap"] == 1, out
        assert out["stateAfterSecondTap"] == "idle", out
        assert out["writesAfterSecondTap"] == 1, (
            "second tap must not write a second decision row; got "
            f"{out['writesAfterSecondTap']} writes"
        )

    def test_no_canvas_skip_decision_write_but_still_settle_state(self):
        """Edge — operator hits refresh before the canvas has loaded
        (no member yet). No decision row is written (no target session
        to forward to), but the state still settles to 'requested' so
        the affordance feels alive."""
        out = _run(
            """
            await c.loadBoard();  // canvas read returns []
            c.onRefreshAll();
            await new Promise((resolve) => setImmediate(resolve));
            return {
                writeCount: writes.length,
                refreshState: c.refreshState,
                coordSession: c._coordSession,
            };
            """,
            seeded={"canvas": []},
        )
        assert out["coordSession"] == "", out
        assert out["writeCount"] == 0, (
            f"no canvas member → no coord session → no decision row: {out}"
        )
        assert out["refreshState"] == "requested", out
