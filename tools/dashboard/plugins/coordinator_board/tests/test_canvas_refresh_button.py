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
real Alpine factory without a headless browser. Bead 4B routed every
read/write through the generated ``Schema.alpine()`` runtime; the test
driver now stubs ``Schema._setFetchOverride`` to seed canvas members,
satisfy the schema-meta fetches every proxy needs, and capture every
``POST /api/graph/setting`` write — that lets us verify the decision
payload, the verbatim ``target_session``, and the post-tap state in a
single eval without booting a real backend.

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
SCHEMAS_JS = (
    PLUGIN_DIR.parents[1] / "static" / "js" / "schemas.js"
)


def _node_available() -> bool:
    return shutil.which("node") is not None


# ── Driver ──────────────────────────────────────────────────────────────
#
# The Node subprocess loads schemas.js + page.js, installs a fetch
# override that
#
#   1. serves the schema-meta payloads each ``Schema.of`` proxy needs
#      to construct itself (one per (set_id, revision) the page binds);
#   2. returns a seeded singleton row for the coordinator binding,
#      seeded members for the coordinator-canvas read, and an empty
#      list for every other read; and
#   3. captures every ``POST /api/graph/setting`` body into ``writes``
#      so the test can assert payload shape.
#
# The test then calls ``await c.init()`` — which fires Schema.alpine's
# wrapped init (proxy attachment + loadBoard) — and runs the snippet.


_NODE_DRIVER = r"""
const Schema = require(%(schemas_js)s);
const { coordinatorBoard } = require(%(page_js)s);

// Minimal browser globals the factory touches. ``window.Schema`` is left
// unset so page.js falls through to the require() path it took at module
// load — both paths resolve to the same module because Node caches them.
global.window = global.window || {};
global.window.crypto = { randomUUID: () => 'fixed-uuid-for-test' };
global.window.Autonomy = {};

const writes = [];
const seeded = %(seeded)s;

// ── Schema-meta payloads ─────────────────────────────────────────
//
// Each ``Schema.of(setId, {revision})`` resolves a meta-Setting at
// ``/api/graph/settings/autonomy.schema/<setId>#<revision>``. We hand
// back just enough metadata for the proxy's pattern + variant
// extensions to produce the methods page.js calls (``.append`` on
// Decision, ``.set`` on OperatorMsg, ``.all`` on every read-only set).
function meta(setId, revision, accessPattern, keyStrategy, variants) {
  return {
    set_id: setId,
    schema_revision: revision,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: accessPattern || null,
    key_strategy: keyStrategy || null,
    variants: variants || {},
  };
}

const META = {
  'dashboard.coordinator#1':
    meta('dashboard.coordinator', 1, 'singleton', 'fixed:default'),
  'dashboard.coordinator-canvas#1':
    meta('dashboard.coordinator-canvas', 1, 'keyed_per_entity', 'natural'),
  'dashboard.operator-message-to-coordinator#1':
    meta('dashboard.operator-message-to-coordinator', 1, 'singleton', 'fixed:default'),
  'dashboard.coordinator-tile#3':
    meta('dashboard.coordinator-tile', 3, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-thread#3':
    meta('dashboard.coordinator-thread', 3, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-decision#1':
    meta('dashboard.coordinator-decision', 1, 'append_only_log', 'uuid_v4'),
  'dashboard.coordinator-sprint#2':
    meta('dashboard.coordinator-sprint', 2, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-bead#1':
    meta('dashboard.coordinator-bead', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-convergent-decision#1':
    meta('dashboard.coordinator-convergent-decision', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-open-followup#1':
    meta('dashboard.coordinator-open-followup', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-docs#1':
    meta('dashboard.coordinator-docs', 1, 'singleton', 'fixed:default'),
};

const META_PREFIX = '/api/graph/settings/autonomy.schema/';

Schema._clearCache();
Schema._setFetchOverride(async (path, opts) => {
  // 1) schema-meta fetch — returns the proxy bootstrap payload.
  if (path.indexOf(META_PREFIX) === 0) {
    const key = decodeURIComponent(path.slice(META_PREFIX.length));
    const payload = META[key];
    if (!payload) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({ payload }) };
  }
  // 2) write — POST /api/graph/setting.
  if (path === '/api/graph/setting' && opts && opts.method === 'POST') {
    const body = JSON.parse(opts.body);
    writes.push({
      setId: body.set_id,
      key: body.key,
      payload: body.payload,
      schemaRevision: body.schema_revision || null,
    });
    return { ok: true, status: 200, json: async () => ({ id: 'stub' }) };
  }
  // 3) keyed singleton read — /api/graph/settings/<set_id>/<key>.
  const keyed = path.match(/^\/api\/graph\/settings\/([^/?]+)\/([^/?]+)(?:\?.*)?$/);
  if (keyed) {
    const setId = decodeURIComponent(keyed[1]);
    const key = decodeURIComponent(keyed[2]);
    if (setId === 'dashboard.coordinator' && key === 'default') {
      const row = seeded.coordinator || null;
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
  }
  // 4) list read — /api/graph/settings/<set_id>(?target_revision=N).
  const m = path.match(/^\/api\/graph\/settings\/([^/?]+)(?:\?.*)?$/);
  if (m) {
    const setId = decodeURIComponent(m[1]);
    if (setId === 'dashboard.coordinator-canvas') {
      return {
        ok: true, status: 200,
        json: async () => ({ members: seeded.canvas || [] }),
      };
    }
    return { ok: true, status: 200, json: async () => ({ members: [] }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
});

const c = coordinatorBoard();

(async () => {
  // Schema.alpine wraps init so awaiting it attaches proxies AND runs
  // the original init body (which calls loadBoard + subscribes).
  await c.init();
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
        "schemas_js": json.dumps(str(SCHEMAS_JS)),
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
        ``target_session`` equals the explicit coordinator binding,
        not the latest canvas member's key."""
        seeded = {
            "coordinator": {
                "key": "default",
                "payload": {"session_id": "auto-coord-9"},
                "updated_at": "2026-04-30T00:01:00Z",
            },
            "canvas": [
                {
                    "key": "auto-canvas-owner",
                    "payload": {"question": "ship or hold?"},
                    "updated_at": "2026-04-30T00:00:00Z",
                },
            ],
        }
        out = _run(
            """
            // Schema.alpine's init() already ran loadBoard, seeding canvas.
            c.onRefreshAll();
            // Drain microtasks so the awaited Decision.append resolves.
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
            "coordinator": {
                "key": "default",
                "payload": {"session_id": "auto-coord-fast"},
                "updated_at": "2026-04-30T00:01:00Z",
            },
            "canvas": [
                {
                    "key": "auto-canvas-owner-fast",
                    "payload": {"question": "?"},
                    "updated_at": "2026-04-30T00:00:00Z",
                },
            ],
        }
        out = _run(
            """
            // Wedge loadBoard so the call onRefreshAll triggers resolves
            // AFTER its synchronous body — proves the post-refresh state
            // is NOT clobbered by a late loadBoard finish.
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
            "coordinator": {
                "key": "default",
                "payload": {"session_id": "auto-coord-x"},
                "updated_at": "2026-04-30T00:01:00Z",
            },
            "canvas": [
                {
                    "key": "auto-canvas-owner-x",
                    "payload": {"question": "?"},
                    "updated_at": "2026-04-30T00:00:00Z",
                },
            ],
        }
        out = _run(
            """
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

    def test_no_binding_skips_decision_write_even_with_canvas(self):
        """Edge — no explicit coordinator binding means no decision row
        is written, even if a canvas exists. The button still settles to
        'requested' so the affordance feels alive."""
        out = _run(
            """
            // init() saw a canvas but no dashboard.coordinator binding —
            // board-level refresh must not fall back to canvas ownership.
            c.onRefreshAll();
            await new Promise((resolve) => setImmediate(resolve));
            return {
                writeCount: writes.length,
                refreshState: c.refreshState,
                coordSession: c._coordSession,
            };
            """,
            seeded={
                "coordinator": None,
                "canvas": [
                    {
                        "key": "auto-canvas-owner-without-binding",
                        "payload": {"question": "?"},
                        "updated_at": "2026-04-30T00:00:00Z",
                    },
                ],
            },
        )
        assert out["coordSession"] == "", out
        assert out["writeCount"] == 0, (
            f"no dashboard.coordinator binding → no decision row: {out}"
        )
        assert out["refreshState"] == "requested", out
