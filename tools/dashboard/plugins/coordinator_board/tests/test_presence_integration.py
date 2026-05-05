"""L2 tests for the coordinator-board Presence integration (bead auto-9eypf).

Drives ``page.js`` in a Node subprocess — same pattern as
``tools/dashboard/plugins/nexus/tests/test_nexus_page.py``. The driver:

1. Loads ``schemas.js`` + ``surface-presence.js`` + the plugin's
   ``page.js``.
2. Installs a fetch override that
   - serves schema-meta payloads for every set the page binds (the
     coordinator-board's eleven Settings sets + the two SurfacePresence
     sets);
   - serves seeded reads for whichever sets a test cares about;
   - captures every ``POST /api/graph/setting`` write so the test can
     assert on the operator's own presence row, etc.
3. Calls ``await c.init()`` which fires Schema.alpine's wrapped init
   (proxy attachment) → Presence.alpine's init (presence proxies +
   participants load + heartbeat) → the original coordinator-board
   init (loadBoard + setting.changed subscribe).
4. Returns the resulting state + write log so assertions can verify
   the presence wiring + participant resolution + updater attribution.

Run explicitly with::

    pytest tools/dashboard/plugins/coordinator_board/tests/test_presence_integration.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PAGE_JS = PLUGIN_DIR / "page.js"
SCHEMAS_JS = PLUGIN_DIR.parents[1] / "static" / "js" / "schemas.js"
PRESENCE_JS = PLUGIN_DIR.parents[1] / "static" / "js" / "surface-presence.js"

SURFACE_ID = "coordinator-board"


def _node_available() -> bool:
    return shutil.which("node") is not None


_NODE_DRIVER = r"""
const Schema = require(%(schemas_js)s);
// Surface-presence sniffs ``globalThis.Schema`` to attach its proxies.
// Set it BEFORE we require page.js so the lazy resolver finds the same
// Schema instance the driver hands fetch overrides to.
globalThis.Schema = Schema;
const Presence = require(%(presence_js)s);

global.window = global.window || {};
global.window.crypto = { randomUUID: () => 'fixed-uuid-for-test' };
global.window.Autonomy = %(autonomy)s;
// Stash both runtimes on window too so the page.js sniff hits the
// browser path it would in production.
global.window.Schema = Schema;
global.window.Presence = Presence;

const { coordinatorBoard } = require(%(page_js)s);

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
  // Coordinator board's eleven Settings sets — meta payloads only need
  // enough shape for Schema.alpine's bootstrap to succeed.
  'dashboard.coordinator#1':
    meta('dashboard.coordinator', 1, 'singleton', 'fixed:default'),
  'dashboard.coordinator-canvas#1':
    meta('dashboard.coordinator-canvas', 1, 'append_only_log', 'uuid_v4'),
  'dashboard.operator-message-to-coordinator#1':
    meta('dashboard.operator-message-to-coordinator', 1, 'singleton', 'fixed:default'),
  'dashboard.coordinator-tile#2':
    meta('dashboard.coordinator-tile', 2, 'keyed_per_entity', 'natural'),
  // Forward-compat: auto-fwwfu bumps tile/thread to #3 when ageMin is
  // dropped. Keep the presence driver accepting both revs so the slice
  // stays valid regardless of merge order.
  'dashboard.coordinator-tile#3':
    meta('dashboard.coordinator-tile', 3, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-thread#2':
    meta('dashboard.coordinator-thread', 2, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-thread#3':
    meta('dashboard.coordinator-thread', 3, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-decision#1':
    meta('dashboard.coordinator-decision', 1, 'append_only_log', 'uuid_v4'),
  'dashboard.coordinator-sprint#1':
    meta('dashboard.coordinator-sprint', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-sprint#2':
    meta('dashboard.coordinator-sprint', 2, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-bead#1':
    meta('dashboard.coordinator-bead', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-convergent-decision#1':
    meta('dashboard.coordinator-convergent-decision', 1, 'append_only_log', 'uuid_v4'),
  'dashboard.coordinator-open-followup#1':
    meta('dashboard.coordinator-open-followup', 1, 'append_only_log', 'uuid_v4'),
  'dashboard.coordinator-docs#1':
    meta('dashboard.coordinator-docs', 1, 'singleton', 'fixed:default'),
  // SurfacePresence + SurfacePing — the substrate.B sets the
  // Presence.alpine wrapper attaches.
  'dashboard.surface.presence#1':
    meta('dashboard.surface.presence', 1, 'keyed_per_entity', 'natural'),
  'dashboard.surface.ping#1':
    meta('dashboard.surface.ping', 1, 'append_only_log', 'uuid_v4'),
};

const META_PREFIX = '/api/graph/settings/autonomy.schema/';

// Mutable seeded reads — the operator's own presence write is mirrored
// into ``livePresence`` so subsequent reads see the row.
const liveTiles = (seeded.tiles || []).map(m => Object.assign({}, m));
const liveThreads = (seeded.threads || []).map(m => Object.assign({}, m));
const livePresence = (seeded.presence || []).map(m => Object.assign({}, m));
const livePings = (seeded.pings || []).map(m => Object.assign({}, m));

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
    if (body.set_id === 'dashboard.surface.presence') {
      const idx = livePresence.findIndex(m => m.key === body.key);
      const row = {
        key: body.key,
        payload: body.payload,
        updated_at: '2026-05-05T00:00:00Z',
      };
      if (idx >= 0) livePresence[idx] = row;
      else livePresence.push(row);
    } else if (body.set_id === 'dashboard.surface.ping') {
      livePings.push({
        key: body.key,
        payload: body.payload,
        updated_at: '2026-05-05T00:00:00Z',
      });
    }
    return { ok: true, status: 200, json: async () => ({ id: 'stub', key: body.key }) };
  }
  // 3) singleton read — /api/graph/settings/<set_id>/<key>
  const single = path.match(/^\/api\/graph\/settings\/([^/?]+)\/([^/?]+)(?:\?.*)?$/);
  if (single) {
    const setId = decodeURIComponent(single[1]);
    const key = decodeURIComponent(single[2]);
    if (setId === 'dashboard.surface.presence') {
      const row = livePresence.find(m => m.key === key);
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  }
  // 4) list read — /api/graph/settings/<set_id>(?target_revision=N)
  const list = path.match(/^\/api\/graph\/settings\/([^/?]+)(?:\?.*)?$/);
  if (list) {
    const setId = decodeURIComponent(list[1]);
    if (setId === 'dashboard.coordinator-tile') {
      return { ok: true, status: 200, json: async () => ({ members: liveTiles.slice() }) };
    }
    if (setId === 'dashboard.coordinator-thread') {
      return { ok: true, status: 200, json: async () => ({ members: liveThreads.slice() }) };
    }
    if (setId === 'dashboard.surface.presence') {
      return { ok: true, status: 200, json: async () => ({ members: livePresence.slice() }) };
    }
    if (setId === 'dashboard.surface.ping') {
      return { ok: true, status: 200, json: async () => ({ members: livePings.slice() }) };
    }
    // Every other coordinator-board set — empty list keeps the
    // page rendering without breaking schema-bound proxies.
    return { ok: true, status: 200, json: async () => ({ members: [] }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
});

const c = coordinatorBoard();

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
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node driver failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def _agent_row(participant_id, *, surface_id=SURFACE_ID, **overrides):
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
        "heartbeat_at": "2026-05-05T00:00:00Z",
        "last_ping_id": "",
    }
    payload.update(overrides)
    return {
        "key": surface_id + ":" + participant_id,
        "payload": payload,
        "updated_at": "2026-05-05T00:00:00Z",
    }


# ── Acceptance #1 — Presence.alpine wraps the factory ────────────────


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestPresenceWrap:
    """The factory's returned state exposes the SurfacePresence API."""

    def test_factory_exposes_participants_array(self):
        out = _run(
            """
            return {
                hasParticipants: Array.isArray(c.participants),
                hasPingAgent: typeof c.pingAgent === 'function',
                hasSetPresenceState: typeof c.setPresenceState === 'function',
                hasAcknowledgePing: typeof c.acknowledgePing === 'function',
                hasParticipantColor: typeof c.participantColor === 'function',
                hasUpdaterFor: typeof c.updaterFor === 'function',
                surfaceId: c._presenceSurfaceId,
                amHere: c.amHere,
            };
            """,
        )
        assert out["hasParticipants"] is True
        assert out["hasPingAgent"] is True
        assert out["hasSetPresenceState"] is True
        assert out["hasAcknowledgePing"] is True
        assert out["hasParticipantColor"] is True
        assert out["hasUpdaterFor"] is True
        assert out["surfaceId"] == SURFACE_ID
        # No operator id was provided, so the operator's own row is
        # skipped — ``amHere`` stays false. The page must still degrade
        # cleanly (acceptance state #5).
        assert out["amHere"] is False

    def test_schema_alpine_runtime_still_attached(self):
        """Acceptance #1 — Schema.alpine remains the inner substrate."""
        out = _run(
            """
            return {
                hasCoordinator:    typeof c.Coordinator === 'object' && c.Coordinator !== null,
                hasTile:           typeof c.Tile === 'object' && c.Tile !== null,
                hasThread:         typeof c.Thread === 'object' && c.Thread !== null,
                tileSetId:         c.Tile && c.Tile.set_id,
                threadSetId:       c.Thread && c.Thread.set_id,
                hasOnOperatorMsg:  typeof c.onOperatorMessage === 'function',
            };
            """,
        )
        assert out["hasCoordinator"] is True
        assert out["hasTile"] is True
        assert out["hasThread"] is True
        assert out["tileSetId"] == "dashboard.coordinator-tile"
        assert out["threadSetId"] == "dashboard.coordinator-thread"
        assert out["hasOnOperatorMsg"] is True


# ── Acceptance #2 — top-right participant stack hydrates ─────────────


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestParticipantStack:
    """Acceptance #2 — agents on this surface appear in ``participants``.

    Mirrors the state matrix in the bead description: empty / single /
    multiple participants and a foreign-surface filter."""

    def test_no_active_participants_renders_empty_state(self):
        # Matrix #1: zero participants → ``participants`` is an empty
        # array (template flips to the "no one here yet" pill).
        out = _run(
            """
            await c._presenceLoadParticipants();
            return {
                count: c.participants.length,
                participants: c.participants,
            };
            """,
            seeded={"presence": []},
        )
        assert out["count"] == 0
        assert out["participants"] == []

    def test_single_participant_hydrates(self):
        # Matrix #2: one participant.
        out = _run(
            """
            await c._presenceLoadParticipants();
            return {
                count: c.participants.length,
                first: c.participants[0] && {
                    id: c.participants[0].participant_id,
                    label: c.participants[0].participant_label,
                    color: c.participantColor(c.participants[0].participant_id),
                },
            };
            """,
            seeded={"presence": [_agent_row("alice")]},
        )
        assert out["count"] == 1
        assert out["first"]["id"] == "alice"
        assert out["first"]["label"] == "Alice"
        assert out["first"]["color"].startswith("hsl(")

    def test_multiple_participants_hydrate_and_filter_by_surface(self):
        # Matrix #3: multiple participants. A foreign surface row is
        # filtered out by the Presence.alpine surface predicate.
        out = _run(
            """
            await c._presenceLoadParticipants();
            return {
                ids: c.participants.map(p => p.participant_id).sort(),
            };
            """,
            seeded={
                "presence": [
                    _agent_row("alice"),
                    _agent_row("bob"),
                    _agent_row("carol"),
                    # Different surface — must NOT appear.
                    _agent_row("intruder", surface_id="other-surface"),
                ],
            },
        )
        assert out["ids"] == ["alice", "bob", "carol"]

    def test_operator_row_written_when_identity_present(self):
        # Acceptance state matrix #2 (single participant) — when the
        # shell exposes ``window.Autonomy.operatorId`` the wrapper
        # writes the operator's own presence row during ``init()``.
        out = _run(
            """
            return {
                amHere: c.amHere,
                operatorWrites: writes.filter(
                    w => w.setId === 'dashboard.surface.presence'
                         && w.payload.participant_id === 'jeremy'
                ).length,
            };
            """,
            seeded={"presence": []},
            autonomy={"operatorId": "jeremy", "operatorLabel": "Jeremy"},
        )
        assert out["amHere"] is True
        assert out["operatorWrites"] >= 1

    def test_no_operator_id_in_shell_does_not_block_init(self):
        # Acceptance state matrix #5 — board degrades cleanly when
        # ``window.Autonomy.operatorId`` is absent: no presence row is
        # written, but ``participants`` still hydrates from existing
        # rows for read-only multiplayer awareness.
        out = _run(
            """
            await c._presenceLoadParticipants();
            return {
                amHere: c.amHere,
                participantCount: c.participants.length,
                presenceWrites: writes.filter(
                    w => w.setId === 'dashboard.surface.presence'
                ).length,
            };
            """,
            seeded={"presence": [_agent_row("alice")]},
            autonomy={},  # no operatorId
        )
        assert out["amHere"] is False
        assert out["participantCount"] == 1
        assert out["presenceWrites"] == 0


# ── Acceptance #3 — updater attribution resolves live + fallback ─────


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestUpdaterAttribution:
    """Tile + thread headers render attribution that resolves the live
    participant when the peer session is online and falls back to the
    row's own ``label`` (and a deterministic colour) when it isn't."""

    def test_tile_updater_resolves_live_participant_label(self):
        # Matrix #2: peer session is online + publishing presence rows.
        # ``updaterFor(t).live`` is true and ``label`` matches the live
        # ``participant_label`` (which may differ from the tile's own
        # ``label`` field).
        out = _run(
            """
            await c._presenceLoadParticipants();
            await c._refreshTiles();
            const tile = c.data.tiles.find(t => t.session === 'auto-foo');
            const updater = c.updaterFor(tile);
            return {
                tileSession: tile && tile.session,
                tileLabel: tile && tile.label,
                updater: updater,
            };
            """,
            seeded={
                "presence": [
                    _agent_row(
                        "auto-foo",
                        participant_label="Foo (live)",
                    ),
                ],
                "tiles": [{
                    "key": "auto-foo",
                    "payload": {
                        "label": "Foo (stale)",
                        "role": "implementer",
                        "thing": "doing it",
                        "asks": "fyi",
                    },
                    "updated_at": "2026-05-05T00:00:00Z",
                }],
            },
        )
        assert out["tileSession"] == "auto-foo"
        assert out["updater"]["live"] is True
        # Live label wins over the tile's own ``label`` field.
        assert out["updater"]["label"] == "Foo (live)"
        assert out["updater"]["color"].startswith("hsl(")

    def test_tile_updater_falls_back_when_session_offline(self):
        # Matrix #4: peer session is offline (no row in
        # ``participants``). Updater label falls back to the tile's
        # own ``label`` field and the deterministic ``participantColor``
        # hue is still computed off the session id.
        out = _run(
            """
            await c._presenceLoadParticipants();
            await c._refreshTiles();
            const tile = c.data.tiles.find(t => t.session === 'auto-offline');
            const updater = c.updaterFor(tile);
            return {
                updater: updater,
                directColor: c.participantColor('auto-offline'),
            };
            """,
            seeded={
                "presence": [],  # nobody on this surface
                "tiles": [{
                    "key": "auto-offline",
                    "payload": {
                        "label": "Offline Author",
                        "role": "implementer",
                        "thing": "did it",
                        "asks": "fyi",
                    },
                    "updated_at": "2026-05-05T00:00:00Z",
                }],
            },
        )
        assert out["updater"]["live"] is False
        # Fallback uses the tile's own label.
        assert out["updater"]["label"] == "Offline Author"
        # Color comes from the deterministic hue derived from session id.
        assert out["updater"]["color"] == out["directColor"]
        assert out["updater"]["color"].startswith("hsl(")

    def test_tile_updater_label_falls_back_to_session_when_no_label(self):
        # Matrix #4 corner: even the tile's own ``label`` is missing —
        # the substrate normalizer fills ``tile.label`` from the
        # session, so attribution still has a non-empty string to
        # render. This proves the fallback never produces ``''``.
        out = _run(
            """
            await c._presenceLoadParticipants();
            await c._refreshTiles();
            const tile = c.data.tiles.find(t => t.session === 'auto-bare');
            const updater = c.updaterFor(tile);
            return { updater: updater };
            """,
            seeded={
                "presence": [],
                "tiles": [{
                    "key": "auto-bare",
                    "payload": {
                        # ``label`` omitted — _normalizeTile fills it
                        # from the session id, so the fallback path
                        # still renders a non-empty string.
                        "role": "implementer",
                        "thing": "x",
                        "asks": "fyi",
                    },
                    "updated_at": "2026-05-05T00:00:00Z",
                }],
            },
        )
        assert out["updater"]["live"] is False
        assert out["updater"]["label"] == "auto-bare"

    def test_thread_updater_resolves_live_participant_label(self):
        # Threads share the same ``updaterFor`` helper and v2 keyshape
        # (``<peer-session>``) — the Tracking-tab attribution behaves
        # identically to the One-thing tab.
        out = _run(
            """
            await c._presenceLoadParticipants();
            await c._refreshThreads();
            const thread = c.data.threads.find(t => t.session === 'auto-thr');
            const updater = c.updaterFor(thread);
            return {
                threadSession: thread && thread.session,
                updater: updater,
            };
            """,
            seeded={
                "presence": [
                    _agent_row(
                        "auto-thr",
                        participant_label="Thread Author",
                    ),
                ],
                "threads": [{
                    "key": "auto-thr",
                    "payload": {
                        "label": "Thread (stale)",
                        "role": "pair",
                        "status": "shipping",
                        "lead": "ships",
                    },
                    "updated_at": "2026-05-05T00:00:00Z",
                }],
            },
        )
        assert out["threadSession"] == "auto-thr"
        assert out["updater"]["live"] is True
        assert out["updater"]["label"] == "Thread Author"

    def test_thread_updater_falls_back_when_session_offline(self):
        out = _run(
            """
            await c._presenceLoadParticipants();
            await c._refreshThreads();
            const thread = c.data.threads.find(t => t.session === 'auto-thr-off');
            const updater = c.updaterFor(thread);
            return { updater: updater };
            """,
            seeded={
                "presence": [],
                "threads": [{
                    "key": "auto-thr-off",
                    "payload": {
                        "label": "Quiet Thread",
                        "role": "pair",
                        "status": "paused",
                        "lead": "x",
                    },
                    "updated_at": "2026-05-05T00:00:00Z",
                }],
            },
        )
        assert out["updater"]["live"] is False
        assert out["updater"]["label"] == "Quiet Thread"

