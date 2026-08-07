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
PRESENCE_JS = (
    PLUGIN_DIR.parents[1] / "static" / "js" / "surface-presence.js"
)


def _node_available() -> bool:
    return shutil.which("node") is not None


_NODE_DRIVER = r"""
const Schema = require(%(schemas_js)s);
// Surface presence library sniffs ``globalThis.Schema`` to attach its
// proxies — set it BEFORE we require page.js so the lazy resolver finds
// the same Schema instance the driver hands fetch overrides to.
globalThis.Schema = Schema;
const Presence = require(%(presence_js)s);

global.window = global.window || {};
global.window.crypto = { randomUUID: () => 'fixed-uuid-for-test' };
global.window.Autonomy = %(autonomy)s;
// Stash Presence on window too so the page.js sniff hits the browser
// path it would in production.
global.window.Schema = Schema;
global.window.Presence = Presence;

const { nexus } = require(%(page_js)s);

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
  'dashboard.surface.presence#1':
    meta('dashboard.surface.presence', 1, 'keyed_per_entity', 'natural'),
  'dashboard.surface.ping#1':
    meta('dashboard.surface.ping', 1, 'append_only_log', 'uuid_v4'),
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
const livePresence = (seeded.presence && seeded.presence.length)
  ? seeded.presence.map(m => Object.assign({}, m))
  : [];
const livePings = (seeded.pings && seeded.pings.length)
  ? seeded.pings.map(m => Object.assign({}, m))
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
    } else if (body.set_id === 'dashboard.surface.presence') {
      const idx = livePresence.findIndex(m => m.key === body.key);
      const row = {
        key: body.key,
        payload: body.payload,
        updated_at: '2026-05-03T00:00:00Z',
      };
      if (idx >= 0) livePresence[idx] = row;
      else livePresence.push(row);
    } else if (body.set_id === 'dashboard.surface.ping') {
      livePings.push({
        key: body.key,
        payload: body.payload,
        updated_at: '2026-05-03T00:00:00Z',
      });
    }
    return { ok: true, status: 200, json: async () => ({ id: 'stub', key: body.key }) };
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
    if (setId === 'dashboard.surface.presence') {
      const row = livePresence.find(m => m.key === key);
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
    if (setId === 'dashboard.surface.presence') {
      return { ok: true, status: 200, json: async () => ({ members: livePresence.slice() }) };
    }
    if (setId === 'dashboard.surface.ping') {
      return { ok: true, status: 200, json: async () => ({ members: livePings.slice() }) };
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
  // Stop the presence heartbeat (setInterval keeps the node loop
  // alive otherwise) and exit explicitly so the subprocess returns.
  try { c.destroy && c.destroy(); } catch (_) { /* ignore */ }
  process.exit(0);
}).catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  try { c.destroy && c.destroy(); } catch (_) { /* ignore */ }
  process.exit(1);
});
"""


def _run(
    snippet: str,
    *,
    seeded: dict | None = None,
    autonomy: dict | None = None,
) -> dict:
    src = _NODE_DRIVER % {
        "schemas_js": json.dumps(str(SCHEMAS_JS)),
        "presence_js": json.dumps(str(PRESENCE_JS)),
        "page_js": json.dumps(str(PAGE_JS)),
        "seeded": json.dumps(seeded or {}),
        "autonomy": json.dumps(autonomy or {}),
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


# ── Presence integration (bead auto-klu7q) ─────────────────────────


_PRESENT_SEED = {
    "scene": [{
        "key": "active",
        "payload": {"title": "Settings", "subtitle": ""},
        "updated_at": "2026-05-02T00:00:00Z",
    }],
    "tiles": [{
        "key": "welcome",
        "payload": {"kind": "markdown", "order": 100, "body": "hi"},
        "updated_at": "2026-05-02T00:00:00Z",
    }],
}


def _agent_row(participant_id, surface_id="settings-nexus", **overrides):
    payload = {
        "surface_id": surface_id,
        "participant_kind": "agent",
        "participant_id": participant_id,
        "participant_label": participant_id.title(),
        "accepts_pings": True,
        "state": "present",
        "position_kind": "none",
        "position_value": "",
        "intent": "",
        "heartbeat_at": "2026-05-02T22:00:00Z",
        "last_ping_id": "",
    }
    payload.update(overrides)
    return {
        "key": surface_id + ":" + participant_id,
        "payload": payload,
        "updated_at": "2026-05-02T22:00:00Z",
    }


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestNexusPresence:
    """Acceptance criteria #1-#5 for bead auto-klu7q."""

    def test_participants_hydrate_from_surface_presence_rows(self):
        # Acceptance #2: agent on the same surface appears in participants
        # filtered by surface_id. ``init()`` loads participants before
        # writing the operator's own row; in production the next
        # setting.changed SSE refreshes the list — the test re-runs the
        # load helper to prove the same eventual shape lands.
        out = _run(
            """
            await c._presenceLoadParticipants();
            return {
                participants: c.participants.map(p => ({
                    id: p.participant_id,
                    kind: p.participant_kind,
                    label: p.participant_label,
                    color: c.participantColor(p.participant_id),
                })),
                amHere: c.amHere,
            };
            """,
            seeded={
                **_PRESENT_SEED,
                "presence": [
                    _agent_row("alice"),
                    # Different surface — must be filtered out.
                    _agent_row("bob", surface_id="other-surface"),
                ],
            },
            autonomy={"operatorId": "jeremy", "operatorLabel": "Jeremy"},
        )
        ids = sorted(p["id"] for p in out["participants"])
        assert "alice" in ids
        assert "jeremy" in ids
        assert "bob" not in ids
        # Acceptance #2: deterministic HSL for each participant.
        for p in out["participants"]:
            assert p["color"].startswith("hsl(")
            assert p["color"].endswith("70% 60%)")
        # ``amHere`` flips true once the operator's row is written.
        assert out["amHere"] is True

    def test_summon_writes_ping_and_settles_on_acknowledgement(self):
        # Acceptance #3 + #4: pinging an agent writes a SurfacePing row;
        # the agent's next presence write (with matching last_ping_id)
        # settles the button back to idle.
        out = _run(
            """
            const target = c.participants.find(p => p.participant_id === 'alice');
            // Tap the summon button.
            await c.summon(target);
            const afterPing = {
                pingState: Object.assign({}, c.pingState),
                pings: writes.filter(w => w.setId === 'dashboard.surface.ping')
                            .map(w => ({key: w.key, payload: w.payload})),
            };

            // Find the ping we just wrote, then fake the agent
            // acknowledging by writing a presence row with the matching
            // last_ping_id and re-running the load step.
            const pingKey = afterPing.pings[afterPing.pings.length - 1].key;
            // Mutate the seeded presence row directly + invoke the
            // change handler so the page state reloads and settles.
            const row = livePresence.find(m => m.key === 'settings-nexus:alice');
            row.payload = Object.assign({}, row.payload, {last_ping_id: pingKey});
            await c._presenceLoadParticipants();
            c._settlePingsFromParticipants();

            return {
                afterPing,
                pingStateAfterAck: Object.assign({}, c.pingState),
                pingKey,
            };
            """,
            seeded={
                **_PRESENT_SEED,
                "presence": [_agent_row("alice")],
            },
            autonomy={"operatorId": "jeremy", "operatorLabel": "Jeremy"},
        )
        # Ping was written with the correct targeting + surface fields.
        assert len(out["afterPing"]["pings"]) == 1
        ping_payload = out["afterPing"]["pings"][0]["payload"]
        assert ping_payload["surface_id"] == "settings-nexus"
        assert ping_payload["from_participant_id"] == "jeremy"
        assert ping_payload["to_participant_id"] == "alice"
        # Button moved to 'requested' after the write resolved.
        assert out["afterPing"]["pingState"]["alice"] == "requested"
        # Acknowledgement settles the button back to idle.
        assert out["pingStateAfterAck"]["alice"] == "idle"

    def test_summon_skips_non_pingable_agents(self):
        # Acceptance #2 corollary: an agent with accepts_pings=False is
        # findable in the panel but the summon path is a no-op.
        out = _run(
            """
            const target = c.participants.find(p => p.participant_id === 'opted-out');
            await c.summon(target);
            return {
                pingState: Object.assign({}, c.pingState),
                pings: writes.filter(w => w.setId === 'dashboard.surface.ping'),
            };
            """,
            seeded={
                **_PRESENT_SEED,
                "presence": [_agent_row("opted-out", accepts_pings=False)],
            },
            autonomy={"operatorId": "jeremy"},
        )
        assert out["pings"] == []
        # No state change for an opted-out target.
        assert "opted-out" not in out["pingState"]

    def test_tile_markers_filter_by_position(self):
        # Per-tile markers render only for participants whose
        # ``position_kind="tile"`` matches the tile id.
        out = _run(
            """
            return {
                onTileWelcome: c.tileMarkers('welcome').map(p => p.participant_id),
                onTileMissing: c.tileMarkers('does-not-exist').map(p => p.participant_id),
            };
            """,
            seeded={
                **_PRESENT_SEED,
                "presence": [
                    _agent_row("alice", position_kind="tile", position_value="welcome"),
                    _agent_row("bob",   position_kind="tile", position_value="other"),
                    _agent_row("carol", position_kind="none"),
                ],
            },
        )
        assert out["onTileWelcome"] == ["alice"]
        assert out["onTileMissing"] == []

    def test_v1_state_still_present_after_presence_wrap(self):
        # Acceptance #5: no regression — scene/timeline/rail data still
        # populated end-to-end through the wrapped factory.
        out = _run(
            """
            return {
                sceneTitle: c.scene.title,
                timelineIds: c.timeline.map(t => t.id),
                phaseCount: c.phases.length,
                hasParticipantsArray: Array.isArray(c.participants),
                hasPingAgentMethod: typeof c.pingAgent === 'function',
            };
            """,
            seeded={
                **_PRESENT_SEED,
                "presence": [],
            },
        )
        assert out["sceneTitle"] == "Settings"
        assert out["timelineIds"] == ["welcome"]
        assert out["phaseCount"] == 6
        assert out["hasParticipantsArray"] is True
        assert out["hasPingAgentMethod"] is True


def test_summon_button_filter_excludes_guest_participants():
    """Regression pin for the guest participant_kind (operator-approved
    2026-08-07, tools/graph/surface.py::VALID_PARTICIPANT_KINDS).

    A static-source assertion rather than a driven Node-subprocess run:
    this repo's JS test harness (the ``_run`` helper above, and
    tools/dashboard/tests/test_surface_presence.js) currently can't
    execute in this environment -- static/js/schemas.js is an ES module
    and Node's CommonJS require() refuses it (ERR_REQUIRE_ESM),
    independent of this change. Confirmed pre-existing against a clean
    checkout before this commit.

    The summon-button template in page.html filters candidates with
    ``.filter(x => x.participant_kind === 'agent')`` -- strict equality
    against exactly 'agent', so a 'guest' row is excluded by construction
    without needing any change: guests must never be pingable (no
    CrossTalk endpoint behind a browser tab). This test pins the literal
    filter expression so a future edit can't accidentally widen it (e.g.
    to ``!== 'operator'``, which WOULD include guests).
    """
    html = PAGE_JS.parent.joinpath("page.html").read_text()
    assert (
        "x.participant_kind === 'agent' &amp;&amp; x.accepts_pings !== false"
        in html
    )
