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
// Bead: auto-ct3ey. Design: graph://9b0bdb8e-a9c4. Worked example
// of the plugin pattern: graph://97ace518-788.

const _schemaRuntime = (typeof window !== 'undefined' && window.Schema)
  ? window.Schema
  : (typeof require === 'function' ? require('../../static/js/schemas.js') : null);

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
  };

  if (_schemaRuntime && typeof _schemaRuntime.alpine === 'function') {
    return _schemaRuntime.alpine(state, {
      schemas: {
        Scene: 'dashboard.nexus.scene',
        Tile:  'dashboard.nexus.tile',
      },
    });
  }
  return state;
}

if (typeof window !== 'undefined') {
  window.nexus = nexus;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { nexus };
}
