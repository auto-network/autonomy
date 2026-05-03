// Settings Nexus plugin — frontend Alpine factory.
//
// First consumer of the Nexus presentation substrate. The page reads
// two Settings:
//
//   dashboard.nexus.scene  (singleton, key=active)  → banner state
//   dashboard.nexus.tile   (keyed-per-entity)       → timeline tiles
//
// Both are bound through Schema.alpine() so the proxies attach to
// `this.Scene` / `this.Tile` before init() runs. setting.changed SSE
// re-resolves either set into reactive state with no polling.
//
// On the first visit to an empty deployment, init() bootstraps the
// canonical scene + a small starter set of tiles (welcome / status /
// cookbook) so the page has something to render before any other
// session has written anything. The bootstrap is guarded by a probe
// of both sets — present rows always win over the seed.
//
// v1.5 (bead auto-klu7q) wraps the factory output with
// ``Presence.alpine()`` so this surface joins the SurfacePresence
// substrate (substrate.B). Operator + agents render in the banner's
// presence panel and per-tile markers track each participant's claimed
// focus. The summon button per agent writes a ``SurfacePing`` row that
// substrate.C delivers to that exact session over CrossTalk.
//
// Bead: auto-ct3ey (v1) + auto-klu7q (v1.5). Design: graph://9b0bdb8e-a9c4.
// Worked example of the plugin pattern: graph://97ace518-788.
// Surface Presence signpost: graph://dff97eec-c59.

const _schemaRuntime = (typeof window !== 'undefined' && window.Schema)
  ? window.Schema
  : (typeof require === 'function' ? require('../../static/js/schemas.js') : null);

// Surface presence library — same dual-export sniff. ``window.Presence``
// is set by ``static/js/surface-presence.js`` in the browser; node
// tests ``require('../../static/js/surface-presence.js')`` directly.
const _presenceRuntime = (typeof window !== 'undefined' && window.Presence)
  ? window.Presence
  : (typeof require === 'function' ? require('../../static/js/surface-presence.js') : null);

// The static rail content for v1. Per the bead spec the phase tracker,
// merge queue, dispatch counters, sessions list, phase board, backlog,
// and substrate gaps render from these built-in defaults until live
// wiring lands in subsequent beads (worktrees / dispatch / sessions).
const _RAIL_DEFAULTS = {
  phases: [
    {label: "Phase 1\nPython substrate", state: "done",        detail: "1A field()  ·  1B decorators  ·  1C variants  ·  1D export"},
    {label: "Phase 2\nJS substrate",      state: "done",        detail: "2A proxy  ·  2B patterns  ·  2C variants  ·  2D alpine  ·  2E typegen"},
    {label: "Phase 3\nCapability layer",  state: "done-enough", detail: "3B impl  ·  3C contract (flat)  ·  3D consumers  ·  3.5 skill/primer"},
    {label: "Phase 4\nCoordinator-board", state: "done",        detail: "4A schemas  ·  4B page.js → Schema.alpine"},
    {label: "Phase 5\nNexus surface",     state: "in-flight",   detail: "scene + tile schemas + plugin"},
    {label: "Phase 6\nLock-in",           state: "pending",     detail: "drop legacy  ·  canonical refs rewrite"},
  ],
  merges: [],
  dispatch: {running: 0, queued: 0, recent: []},
  sessions: [],
  phaseBoard: [
    {phase: "Phase 1", beads: [
      {id: "auto-4n966", title: "1A — typed field()",       bdState: "closed", commit: "b107ede"},
      {id: "auto-ruuyc", title: "1B — decorators",            bdState: "closed", commit: "341f74f"},
      {id: "auto-wl1kh", title: "1C — variant subclasses",    bdState: "closed", commit: "0cce13f"},
      {id: "auto-eqwoh", title: "1D — export_json_schema",    bdState: "closed", commit: "a80f451"},
    ]},
    {phase: "Phase 2", beads: [
      {id: "auto-upim6", title: "2A — proxy runtime",         bdState: "closed", commit: "4a506fa"},
      {id: "auto-6gqnf", title: "2B — pattern methods",        bdState: "closed", commit: "c6fa283"},
      {id: "auto-mutc2", title: "2C — variant methods",        bdState: "closed", commit: "c59ebaf"},
      {id: "auto-tc6jt", title: "2D — Schema.alpine()",        bdState: "closed", commit: "97ee46d"},
    ]},
    {phase: "Phase 3", beads: [
      {id: "(3B)",       title: "capability_impl#1 typed",     bdState: "closed", commit: "—"},
      {id: "(3C)",       title: "capability_contract#1 typed", bdState: "closed", commit: "—"},
      {id: "(3D)",       title: "consumers adopt typed",       bdState: "closed", commit: "6d9df84"},
    ]},
    {phase: "Phase 4", beads: [
      {id: "auto-jspv8", title: "4A — coordinator schemas",    bdState: "closed", commit: "cc5ba6f"},
      {id: "(4B)",       title: "4B — page.js → Schema.alpine", bdState: "closed", commit: "1ca4fb7"},
    ]},
    {phase: "Phase 5", beads: [
      {id: "auto-ct3ey", title: "Settings Nexus plugin v1",    bdState: "open",   commit: "—"},
    ]},
    {phase: "Phase 6", beads: [
      {id: "—",          title: "drop legacy",                 bdState: "pending", commit: "—"},
    ]},
  ],
  openBacklog: [],
  substrateGaps: [],
};

// Canonical bootstrap content written exactly once per deployment when
// no scene + no tiles exist. The scene's anchor sentence + tile bodies
// are the same prose the design study shipped with — readable on first
// load, useful if the operator never personalises the surface.
const _BOOTSTRAP_SCENE = {
  title: "Settings",
  subtitle: "Substrate done. Productization the next mile.",
  anchor_sentence:
    "Settings is no longer just a storage primitive; it is the " +
    "machine-readable configuration substrate, and the emerging " +
    "product skill is learning to read it directly from the layer " +
    "you're already in instead of rebuilding custom lookup paths " +
    "around it.",
  presenter: "",
  presenter_label: "Settings Nexus",
  focus_tile_id: "",
  layout: "stream",
};

const _BOOTSTRAP_TILES = [
  {
    key: "welcome",
    payload: {
      kind: "markdown",
      order: 100,
      width: "full",
      title: "",
      body:
        "**You are looking at the Nexus surface.** Each tile here is a " +
        "row in `dashboard.nexus.tile#1`. Each banner field is a row in " +
        "`dashboard.nexus.scene#1`. **No new endpoints.** The page is " +
        "itself a no-backend frontend — every read is `.all()` / `.read()` " +
        "on a schema proxy; every change re-renders via `setting.changed`.",
      ts: "",
      data: {},
    },
  },
  {
    key: "status",
    payload: {
      kind: "status",
      order: 90,
      width: "third",
      title: "Right now",
      body: "First visit — bootstrap row. Overwrite this tile to drive the surface from your session.",
      ts: "",
      data: {state: "running", label: "Right now", detail: ""},
    },
  },
  {
    key: "cookbook",
    payload: {
      kind: "code",
      order: 80,
      width: "full",
      title: "If you have the key, just read the Setting.",
      body:
        "// Don't fetch /api/projects and join client-side.\n" +
        "// Don't write a custom endpoint.\n" +
        "// Don't poll.\n\n" +
        "const Workspace = await Schema.of('autonomy.workspace');\n" +
        "const row = await Workspace.read(workspaceId);\n" +
        "const name = row?.payload?.name || workspaceId;",
      ts: "",
      data: {language: "javascript", caption: "Schema.alpine() proxy — direct Setting read."},
    },
  },
];

// Surface id this plugin claims for SurfacePresence / SurfacePing rows.
// Stable across both /nexus and /settings-nexus paths so any session
// landing on either route joins the same multiplayer surface.
const _SURFACE_ID = 'settings-nexus';

function nexus() {
  const state = {
    // ─────── Scene + tile state ───────
    scene: {
      title: "",
      subtitle: "",
      anchor_sentence: "",
      presenter: "",
      presenter_label: "",
      focus_tile_id: "",
      layout: "grid",
    },
    timeline: [],
    lastUpdated: "",

    // ─────── Per-participant summon button state ───────
    // Map participant_id → 'idle' | 'pending' | 'requested'. The
    // req-button contract from coordinator-board: the owning component
    // resets to 'idle' when fresh data arrives. Here that means: when
    // the target participant writes a presence row whose ``last_ping_id``
    // matches the ping we just sent, settle back to idle.
    pingState: {},
    // Map participant_id → ping_id we last sent them. Used to decide
    // whether an incoming presence change settles their button.
    _pingsSent: {},

    // ─────── Hardcoded rail data (v1) ───────
    phases: _RAIL_DEFAULTS.phases.map(p => ({...p})),
    merges: _RAIL_DEFAULTS.merges.map(m => ({...m})),
    dispatch: {..._RAIL_DEFAULTS.dispatch, recent: _RAIL_DEFAULTS.dispatch.recent.map(r => ({...r}))},
    sessions: _RAIL_DEFAULTS.sessions.map(s => ({...s})),
    phaseBoard: _RAIL_DEFAULTS.phaseBoard.map(c => ({phase: c.phase, beads: c.beads.map(b => ({...b}))})),
    openBacklog: _RAIL_DEFAULTS.openBacklog.map(b => ({...b})),
    substrateGaps: _RAIL_DEFAULTS.substrateGaps.map(g => ({...g})),

    dimensions: [
      {id: "d6", num: "6", label: "Surfacing now"},
      {id: "d2", num: "2", label: "Live ops"},
      {id: "d5", num: "5", label: "Roadmap"},
      {id: "d4", num: "4", label: "Productization"},
    ],

    // ─────── Lifecycle ───────
    _unsubscribers: [],
    _bootstrapAttempted: false,

    async init() {
      await this.refreshScene();
      await this.refreshTiles();
      await this._bootstrapIfEmpty();
      this._subscribe();
    },

    destroy() {
      for (const u of this._unsubscribers) {
        try { u(); } catch (_) { /* ignore */ }
      }
      this._unsubscribers = [];
    },

    async refreshScene() {
      // Singleton — read at the fixed key and apply onto reactive state.
      // ``Scene.read('active')`` returns ``{key, payload, updated_at, ...}``
      // or ``null`` when no row exists.
      let row = null;
      try {
        row = await this.Scene.read('active');
      } catch (_) { row = null; }
      const payload = (row && row.payload) || {};
      this.scene = {
        title:           typeof payload.title === 'string' ? payload.title : '',
        subtitle:        typeof payload.subtitle === 'string' ? payload.subtitle : '',
        anchor_sentence: typeof payload.anchor_sentence === 'string' ? payload.anchor_sentence : '',
        presenter:       typeof payload.presenter === 'string' ? payload.presenter : '',
        presenter_label: typeof payload.presenter_label === 'string' ? payload.presenter_label : '',
        focus_tile_id:   typeof payload.focus_tile_id === 'string' ? payload.focus_tile_id : '',
        layout:          typeof payload.layout === 'string' ? payload.layout : 'grid',
      };
      const updated = (row && (row.updated_at || row.created_at)) || '';
      this.lastUpdated = updated ? updated.replace('T', ' ').replace('Z', '') : '';
    },

    async refreshTiles() {
      let members = [];
      try {
        members = await this.Tile.all();
      } catch (_) { members = []; }
      this.timeline = (members || []).map(m => {
        const p = m.payload || {};
        return {
          id:    m.key,
          kind:  typeof p.kind === 'string' ? p.kind : 'markdown',
          order: typeof p.order === 'number' ? p.order : 0,
          width: typeof p.width === 'string' ? p.width : 'full',
          title: typeof p.title === 'string' ? p.title : '',
          body:  typeof p.body === 'string' ? p.body : '',
          ts:    typeof p.ts === 'string' ? p.ts : '',
          data:  (p.data && typeof p.data === 'object') ? p.data : {},
        };
      }).sort((a, b) => (b.order || 0) - (a.order || 0));
    },

    _subscribe() {
      this._unsubscribers.push(this.Scene.onChange(() => this.refreshScene()));
      this._unsubscribers.push(this.Tile.onChange(() => this.refreshTiles()));
    },

    async _bootstrapIfEmpty() {
      // Only seed when both reads come back empty — present rows always
      // win. Guarded against double-fire by a per-instance latch so a
      // setting.changed during the same init() can't trigger a second
      // attempt.
      if (this._bootstrapAttempted) return;
      const sceneEmpty = !this.scene.title;
      const tilesEmpty = !this.timeline.length;
      if (!sceneEmpty || !tilesEmpty) return;
      this._bootstrapAttempted = true;
      try {
        await this.Scene.set({..._BOOTSTRAP_SCENE});
        for (const t of _BOOTSTRAP_TILES) {
          await this.Tile.upsert(t.key, {...t.payload, data: {...t.payload.data}});
        }
      } catch (_) {
        // A failed seed leaves the page rendering an empty state; the
        // operator can write Settings manually. We don't retry — the
        // setting.changed subscription will pick up whatever lands next.
        return;
      }
      // Re-read so the local state reflects the seed without waiting
      // for the SSE round-trip (which may not be wired in tests).
      await this.refreshScene();
      await this.refreshTiles();
    },

    // ─────── Computed splits (template helpers) ───────
    get nonCodeTiles() {
      return this.timeline.filter(t => t.kind !== 'code');
    },
    get codeTiles() {
      return this.timeline.filter(t => t.kind === 'code');
    },

    // ─────── Renderers (pure helpers, no I/O) ───────
    tileState(t) {
      // Status tiles store their state in ``data.state`` per the design
      // fixture; fall back to nothing otherwise.
      if (t && t.data && typeof t.data.state === 'string') return t.data.state;
      return '';
    },

    phaseClass(state) {
      return ({
        'done':         'border-emerald-700/60 bg-emerald-950/30 text-emerald-200',
        'done-enough':  'border-emerald-700/40 bg-emerald-950/20 text-emerald-200/90',
        'in-flight':    'border-amber-700/60 bg-amber-950/30 text-amber-200',
        'pending':      'border-gray-700/60 bg-gray-900/40 text-gray-400',
      }[state] || 'border-gray-700 bg-gray-900 text-gray-300');
    },

    statusDot(state) {
      return ({
        'done': 'bg-emerald-400',
        'running': 'bg-amber-400 animate-pulse',
        'pending': 'bg-gray-500',
        'blocked': 'bg-rose-500',
        'stale': 'bg-violet-500',
      }[state] || 'bg-gray-500');
    },

    statusText(state) {
      return ({
        'done': 'text-emerald-400',
        'running': 'text-amber-400',
        'pending': 'text-gray-400',
        'blocked': 'text-rose-400',
        'stale': 'text-violet-400',
      }[state] || 'text-gray-400');
    },

    tileClass(t) {
      // Width-aware grid spans land in the focus-tile follow-up; v1
      // treats every non-code tile as one cell of the 3-column grid.
      return 'rounded-md border border-gray-800 bg-gray-900/40 p-4';
    },

    bdStateClass(state) {
      return ({
        'closed':  'border-emerald-700/40 bg-emerald-950/20 text-emerald-100',
        'open*':   'border-amber-700/40 bg-amber-950/20 text-amber-100',
        'open':    'border-amber-700/40 bg-amber-950/20 text-amber-100',
        'pending': 'border-gray-700/60 bg-gray-900/40 text-gray-400',
      }[state] || 'border-gray-700 bg-gray-900 text-gray-300');
    },

    tagClass(tag) {
      return ({
        'ergonomics': 'bg-cyan-800/40 text-cyan-200',
        'correctness': 'bg-rose-800/40 text-rose-200',
        'bug':        'bg-rose-800/40 text-rose-200',
        'schema':     'bg-indigo-800/40 text-indigo-200',
        'docs':       'bg-violet-800/40 text-violet-200',
        'cache':      'bg-amber-800/40 text-amber-200',
        'consumer':   'bg-emerald-800/40 text-emerald-200',
        'cli':        'bg-cyan-800/40 text-cyan-200',
        'rest':       'bg-blue-800/40 text-blue-200',
        'plugin':     'bg-fuchsia-800/40 text-fuchsia-200',
        'test':       'bg-yellow-800/40 text-yellow-200',
      }[tag] || 'bg-gray-700 text-gray-300');
    },

    renderMarkdown(md) {
      // Tiny inline markdown — bold, code, links. Production would use
      // the dashboard's marked.js + DOMPurify; the inline path keeps the
      // tile renderer self-contained and dependency-free for the L1 test
      // path. Kept identical to the design fixture's helper so
      // copy-pasted tile bodies render the same as the mock.
      const esc = (s) => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      let s = esc(md || '');
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong class="text-white">$1</strong>');
      s = s.replace(/`([^`]+)`/g, '<code class="bg-gray-800 px-1 py-0.5 rounded text-[12px] text-indigo-300">$1</code>');
      return s;
    },

    // ─────── Surface presence helpers (substrate.B view layer) ───────

    participantColor(participantId) {
      // Deterministic HSL hash, mirrored across Python + JS so the
      // operator and the dashboard agree on a participant's hue. The
      // helper lives view-side per pitfall graph://73af2694-562 — agents
      // never claim a color in their own row.
      if (_presenceRuntime && typeof _presenceRuntime.participantColor === 'function') {
        return _presenceRuntime.participantColor(participantId);
      }
      return 'hsl(0 70% 60%)';
    },

    participantInitial(p) {
      const label = (p && (p.participant_label || p.participant_id)) || '?';
      return String(label).trim().charAt(0).toUpperCase() || '?';
    },

    tileMarkers(tileId) {
      // Participants whose claimed focus is this tile. Drives the small
      // colored dots rendered on each tile header.
      if (!Array.isArray(this.participants)) return [];
      return this.participants.filter(p => p
        && p.position_kind === 'tile'
        && p.position_value === tileId);
    },

    scrollToTile(tileId) {
      // Default onPing handler. Scrolls the tile into view so the
      // operator can answer the ping without hunting through the
      // timeline. Guards against missing IDs and a non-DOM env (tests).
      if (!tileId || typeof document === 'undefined') return;
      const el = document.querySelector('[data-testid="nx-tile-' + tileId + '"]');
      if (el && typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({behavior: 'smooth', block: 'center'});
      }
    },

    async summon(participant) {
      // Operator taps the per-participant ping button. Writes a
      // SurfacePing row directed at the explicit participant_id —
      // never a role lookup (see graph://1ba4d2e0-c5f). The button
      // moves idle → pending → requested and settles back to idle
      // when the agent's next presence row reflects ``last_ping_id``.
      if (!participant || !participant.participant_id) return;
      if (!participant.accepts_pings) return;
      const targetId = participant.participant_id;
      this.pingState = {...this.pingState, [targetId]: 'pending'};
      try {
        const focusId = (this.scene && this.scene.focus_tile_id) || '';
        const result = await this.pingAgent(
          targetId,
          {kind: focusId ? 'tile' : 'none', value: focusId},
          '',
        );
        // The append helper on the proxy returns the written row; we
        // capture the key so the settle handler can match it against
        // the agent's ``last_ping_id``.
        const pingId = (result && (result.key || (result.payload && result.payload.id))) || '';
        if (pingId) this._pingsSent = {...this._pingsSent, [targetId]: pingId};
        this.pingState = {...this.pingState, [targetId]: 'requested'};
      } catch (err) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('[nexus] summon failed for', targetId, err);
        }
        this.pingState = {...this.pingState, [targetId]: 'idle'};
      }
    },

    _settlePingsFromParticipants() {
      // After every presence refresh, walk participants and clear any
      // 'requested' button whose target has acknowledged the matching
      // ping. We compare against ``_pingsSent`` rather than the most
      // recent ping seen on the wire so an unrelated ping from another
      // operator can't settle our button.
      if (!Array.isArray(this.participants)) return;
      let dirty = false;
      const next = {...this.pingState};
      for (const p of this.participants) {
        if (!p || !p.participant_id) continue;
        const sentId = this._pingsSent[p.participant_id];
        if (!sentId) continue;
        if (p.last_ping_id && p.last_ping_id === sentId
            && next[p.participant_id] === 'requested') {
          next[p.participant_id] = 'idle';
          dirty = true;
        }
      }
      if (dirty) this.pingState = next;
    },
  };

  // Compose Presence.alpine then Schema.alpine. Init order is
  // outer-first per the wrapper contract: Schema.alpine attaches Scene
  // and Tile, then calls the Presence-wrapped init (presence proxies +
  // participants load + heartbeat), then calls the original nexus init
  // (refreshScene + refreshTiles + bootstrap-if-empty). This means by
  // the time the bootstrap probe runs, ``this.participants`` is already
  // hydrated for any later UI work that needs it.
  let composed = state;
  if (_presenceRuntime && typeof _presenceRuntime.alpine === 'function') {
    composed = _presenceRuntime.alpine({
      surfaceId: _SURFACE_ID,
      // Regular function (NOT arrow) so Presence's ``onPing.call(this, ...)``
      // delivers the nexus state as ``this``. Arrow functions ignore
      // ``.call(this)`` per ECMA semantics.
      onPing: function(ping) {
        this.scrollToTile(ping && ping.position_value);
      },
      onParticipantChange: function() {
        this._settlePingsFromParticipants();
      },
    }, state);
  }

  if (_schemaRuntime && typeof _schemaRuntime.alpine === 'function') {
    return _schemaRuntime.alpine(composed, {
      schemas: {
        Scene: 'dashboard.nexus.scene',
        Tile:  'dashboard.nexus.tile',
      },
    });
  }
  return composed;
}

if (typeof window !== 'undefined') {
  window.nexus = nexus;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { nexus };
}
