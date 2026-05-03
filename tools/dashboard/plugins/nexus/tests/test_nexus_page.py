"""L2 tests for the Settings Nexus page.js Alpine factory (bead auto-ct3ey).

Drives page.js in a Node subprocess — same pattern the
coordinator-board canvas-refresh tests use. The driver:

1. Loads schemas.js + page.js.
2. Installs a fetch override that
   - serves schema-meta payloads each Schema.of proxy needs to
     bootstrap (one per (set_id, revision) the page binds);
   - serves seeded scene/tile reads;
   - captures every POST /api/graph/setting write.
3. Calls ``await c.init()`` which triggers Schema.alpine's wrapped
   init (proxy attachment + scene/tile read + bootstrap-if-empty).
4. Returns the resulting state + write log so assertions can verify
   the bootstrap contract and the timeline ordering.

Run explicitly with::

    pytest tools/dashboard/plugins/nexus/tests/test_nexus_page.py
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


_NODE_DRIVER = r"""
const Schema = require(%(schemas_js)s);
const { nexus } = require(%(page_js)s);

global.window = global.window || {};
global.window.crypto = { randomUUID: () => 'fixed-uuid-for-test' };
global.window.Autonomy = {};

const writes = [];
const seeded = %(seeded)s;

function meta(setId, revision, accessPattern, keyStrategy) {
  return {
    set_id: setId,
    schema_revision: revision,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: accessPattern || null,
    key_strategy: keyStrategy || null,
    variants: {},
  };
}

const META = {
  'dashboard.nexus.scene#1':
    meta('dashboard.nexus.scene', 1, 'singleton', 'fixed:active'),
  'dashboard.nexus.tile#1':
    meta('dashboard.nexus.tile', 1, 'keyed_per_entity', 'natural'),
};

const META_PREFIX = '/api/graph/settings/autonomy.schema/';
// Mutable seed: the bootstrap path writes scene/tiles, then re-reads —
// the driver mirrors writes into the seed so the re-read sees them.
const liveScene = (seeded.scene && seeded.scene.length)
  ? seeded.scene.map(m => Object.assign({}, m))
  : [];
const liveTiles = (seeded.tiles && seeded.tiles.length)
  ? seeded.tiles.map(m => Object.assign({}, m))
  : [];

Schema._clearCache();
Schema._setFetchOverride(async (path, opts) => {
  // 1) schema-meta fetch
  if (path.indexOf(META_PREFIX) === 0) {
    const key = decodeURIComponent(path.slice(META_PREFIX.length));
    const payload = META[key];
    if (!payload) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({ payload }) };
  }
  // 2) write — POST /api/graph/setting
  if (path === '/api/graph/setting' && opts && opts.method === 'POST') {
    const body = JSON.parse(opts.body);
    writes.push({
      setId: body.set_id,
      key: body.key,
      payload: body.payload,
      schemaRevision: body.schema_revision || null,
    });
    if (body.set_id === 'dashboard.nexus.scene') {
      // Singleton — clear and replace
      liveScene.length = 0;
      liveScene.push({
        key: body.key,
        payload: body.payload,
        updated_at: '2026-05-03T00:00:00Z',
      });
    } else if (body.set_id === 'dashboard.nexus.tile') {
      // Upsert by key
      const idx = liveTiles.findIndex(m => m.key === body.key);
      const row = {
        key: body.key,
        payload: body.payload,
        updated_at: '2026-05-03T00:00:00Z',
      };
      if (idx >= 0) liveTiles[idx] = row;
      else liveTiles.push(row);
    }
    return { ok: true, status: 200, json: async () => ({ id: 'stub' }) };
  }
  // 3) singleton read — /api/graph/settings/<set_id>/<key>
  // 4) list read    — /api/graph/settings/<set_id>(?target_revision=N)
  const single = path.match(/^\/api\/graph\/settings\/([^/?]+)\/([^/?]+)(?:\?.*)?$/);
  if (single) {
    const setId = decodeURIComponent(single[1]);
    const key = decodeURIComponent(single[2]);
    if (setId === 'dashboard.nexus.scene') {
      const row = liveScene.find(m => m.key === key);
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
    if (setId === 'dashboard.nexus.tile') {
      const row = liveTiles.find(m => m.key === key);
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  }
  const list = path.match(/^\/api\/graph\/settings\/([^/?]+)(?:\?.*)?$/);
  if (list) {
    const setId = decodeURIComponent(list[1]);
    if (setId === 'dashboard.nexus.scene') {
      return { ok: true, status: 200, json: async () => ({ members: liveScene.slice() }) };
    }
    if (setId === 'dashboard.nexus.tile') {
      return { ok: true, status: 200, json: async () => ({ members: liveTiles.slice() }) };
    }
    return { ok: true, status: 200, json: async () => ({ members: [] }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
});

const c = nexus();

(async () => {
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
class TestNexusBootstrap:
    """Acceptance #2 — first visit with no rows seeds canonical content."""

    def test_empty_state_writes_scene_and_initial_tiles(self):
        out = _run(
            """
            return {
                writes: writes,
                scene: c.scene,
                timeline: c.timeline.map(t => ({id: t.id, kind: t.kind, order: t.order})),
            };
            """,
            seeded={"scene": [], "tiles": []},
        )
        # One scene write + three tile writes (welcome, status, cookbook).
        scene_writes = [w for w in out["writes"]
                        if w["setId"] == "dashboard.nexus.scene"]
        tile_writes = [w for w in out["writes"]
                       if w["setId"] == "dashboard.nexus.tile"]
        assert len(scene_writes) == 1, scene_writes
        assert len(tile_writes) == 3, tile_writes
        # Singleton key is the substrate-resolved 'active'.
        assert scene_writes[0]["key"] == "active"
        # Scene payload carries the bootstrap title + anchor sentence.
        sp = scene_writes[0]["payload"]
        assert sp["title"] == "Settings"
        assert "machine-readable configuration substrate" in sp["anchor_sentence"]
        # Tile keys match the bootstrap set.
        assert {w["key"] for w in tile_writes} == {"welcome", "status", "cookbook"}
        # The page state reflects the seeded reads after bootstrap.
        assert out["scene"]["title"] == "Settings"
        assert out["scene"]["layout"] == "stream"
        # Timeline sorted by order desc.
        ids_in_order = [t["id"] for t in out["timeline"]]
        assert ids_in_order == ["welcome", "status", "cookbook"]
        assert out["timeline"][0]["order"] >= out["timeline"][-1]["order"]

    def test_existing_scene_is_not_overwritten(self):
        out = _run(
            """
            return {
                writes: writes,
                scene: c.scene,
                timeline: c.timeline.map(t => t.id),
            };
            """,
            seeded={
                "scene": [{
                    "key": "active",
                    "payload": {"title": "Custom", "subtitle": "set already"},
                    "updated_at": "2026-05-02T00:00:00Z",
                }],
                "tiles": [],
            },
        )
        # Bootstrap must NOT fire — present rows always win.
        assert out["writes"] == [], out["writes"]
        assert out["scene"]["title"] == "Custom"
        assert out["scene"]["subtitle"] == "set already"
        assert out["timeline"] == []

    def test_existing_tiles_are_not_overwritten(self):
        out = _run(
            """
            return {
                writes: writes,
                scene: c.scene,
                timeline: c.timeline.map(t => ({id: t.id, kind: t.kind, order: t.order})),
            };
            """,
            seeded={
                "scene": [],
                "tiles": [{
                    "key": "operator-tile",
                    "payload": {
                        "kind": "markdown", "order": 5,
                        "title": "", "body": "operator-set", "ts": "",
                        "data": {},
                    },
                    "updated_at": "2026-05-02T00:00:00Z",
                }],
            },
        )
        assert out["writes"] == [], out["writes"]
        assert out["timeline"] == [
            {"id": "operator-tile", "kind": "markdown", "order": 5}
        ]

    def test_timeline_sorted_by_order_descending(self):
        out = _run(
            """
            return { ids: c.timeline.map(t => t.id) };
            """,
            seeded={
                "scene": [{
                    "key": "active",
                    "payload": {"title": "S", "subtitle": ""},
                    "updated_at": "2026-05-02T00:00:00Z",
                }],
                "tiles": [
                    {"key": "low",  "payload": {"kind": "markdown", "order":  1}, "updated_at": "2026-05-02T00:00:00Z"},
                    {"key": "high", "payload": {"kind": "markdown", "order": 99}, "updated_at": "2026-05-02T00:00:00Z"},
                    {"key": "mid",  "payload": {"kind": "markdown", "order": 50}, "updated_at": "2026-05-02T00:00:00Z"},
                ],
            },
        )
        assert out["ids"] == ["high", "mid", "low"]


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestNexusBindings:
    """Schema.alpine wires Scene + Tile proxies onto the Alpine state."""

    def test_proxies_attach_via_schema_alpine(self):
        out = _run(
            """
            return {
                hasScene: typeof c.Scene === 'object' && c.Scene !== null,
                hasTile:  typeof c.Tile === 'object' && c.Tile !== null,
                sceneId:  c.Scene && c.Scene.set_id,
                tileId:   c.Tile && c.Tile.set_id,
                hasSceneSet:    typeof (c.Scene && c.Scene.set) === 'function',
                hasTileUpsert:  typeof (c.Tile && c.Tile.upsert) === 'function',
            };
            """,
            seeded={
                "scene": [{
                    "key": "active",
                    "payload": {"title": "exists"},
                    "updated_at": "2026-05-02T00:00:00Z",
                }],
                "tiles": [{
                    "key": "anchor",
                    "payload": {"kind": "markdown"},
                    "updated_at": "2026-05-02T00:00:00Z",
                }],
            },
        )
        assert out["hasScene"] and out["hasTile"]
        assert out["sceneId"] == "dashboard.nexus.scene"
        assert out["tileId"] == "dashboard.nexus.tile"
        assert out["hasSceneSet"]
        assert out["hasTileUpsert"]
