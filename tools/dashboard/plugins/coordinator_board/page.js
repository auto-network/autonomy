// Coordinator Board plugin — frontend Alpine factory.
// Mirrors the design at revision f710c702 of design 59dd05c1-... (graph
// id 81e126c8-75e). Bead auto-lffg5 retired the v1 ``api.py`` facade —
// the page reads + writes graph Settings directly:
//
//   reads:
//     GET /api/graph/settings/dashboard.coordinator-canvas
//     GET /api/graph/settings/dashboard.operator-message-to-coordinator
//     GET /api/graph/settings/dashboard.coordinator-tile
//     GET /api/graph/settings/dashboard.coordinator-thread
//     GET /api/graph/settings/dashboard.coordinator-decision
//
//   writes:
//     POST /api/graph/setting   (set_id=<one of the above>)
//
// Bead auto-obo63 retired the 5s decision-log poll: the page now
// subscribes to ``setting.changed`` per-set_id via
// ``window.dashboardEvents.onSettingChanged`` (auto-5mz65) and
// re-resolves the affected set on each event. The substrate's mediator
// watchdog covers missed events at the handler layer; the operator gets
// the existing canvas-corner ↻ button as a manual escape hatch.
const SET_CANVAS = 'dashboard.coordinator-canvas';
const SET_OPERATOR_MSG = 'dashboard.operator-message-to-coordinator';
const SET_TILE = 'dashboard.coordinator-tile';
const SET_THREAD = 'dashboard.coordinator-thread';
const SET_DECISION = 'dashboard.coordinator-decision';
const SCHEMA_REVISION = 1;
const OPERATOR_MSG_KEY = 'default';

function coordinatorBoard() {
  return {
    // ─────── UI state ───────
    tab: 'primary',
    tabs: [
      { key: 'primary',  label: 'One thing' },
      { key: 'tracking', label: 'Tracking' },
    ],
    lastAction: '',
    operatorDraft: '',
    refreshState: 'idle',
    wins: 0,
    winCelebrating: false,
    sortKey: 'urgent',
    sortOptions: [
      { key: 'urgent', label: 'Urgent' },
      { key: 'recent', label: 'Recent' },
      { key: 'turns',  label: 'Turns' },
      { key: 'name',   label: 'Name' },
    ],
    sending: false,

    // ─────── Data ───────
    data: {
      snapshotTime: '',
      broadcastPlaceholder: 'Message me…',
      canvas: { ageMin: 0, question: '', context: '', quickReplies: [] },
      operatorMessage: { text: '', sentAt: null },
      tiles: [],
      threads: [],
      // tile_id → { kind, choice, sentAt } for the most-recent decision
      // applied to that tile. Iterated on render to surface the "you
      // said: …" mark beneath the tile body.
      decisionsByTile: {},
      beads: [],
      convergentDecisions: [],
      openFollowups: [],
      docs: { coordMap: '', walkthrough: '' },
    },

    // Active onSettingChanged unsubscribe callbacks; drained by destroy().
    _unsubscribers: [],

    // ─────── Lifecycle ───────
    async init() {
      await this.loadBoard();
      this._subscribeSettings();
    },

    destroy() {
      for (const u of this._unsubscribers) {
        try { u(); } catch (_) { /* ignore unsubscribe errors */ }
      }
      this._unsubscribers = [];
    },

    async loadBoard() {
      // Parallel fetches — each set is independent.
      const [canvas, op, tiles, threads, decisions] = await Promise.all([
        this._readSet(SET_CANVAS),
        this._readSet(SET_OPERATOR_MSG),
        this._readSet(SET_TILE),
        this._readSet(SET_THREAD),
        this._readSet(SET_DECISION),
      ]);

      this.data.canvas = this._normalizeCanvas(this._latest(canvas));
      this.data.operatorMessage = this._normalizeOperatorMessage(this._latest(op));
      this.data.tiles = (tiles || []).map(m => this._normalizeTile(m));
      this.data.threads = (threads || []).map(m => this._normalizeThread(m));
      this.data.decisionsByTile = this._buildDecisionsByTile(decisions);
      // Snapshot timestamp: render-time stamp; the canvas's ageMin is
      // authoritative for "when did the coordinator publish this".
      this.data.snapshotTime = new Date().toLocaleString(undefined, {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
      });
      if (this.refreshState !== 'idle') this.refreshState = 'idle';
    },

    _subscribeSettings() {
      const events = window.dashboardEvents;
      if (!events || typeof events.onSettingChanged !== 'function') return;
      // Listed individually (not in a loop) so a single grep can prove
      // each subscribed set_id — see auto-obo63 acceptance #2.
      this._unsubscribers.push(events.onSettingChanged(SET_CANVAS,       () => this._refreshCanvas()));
      this._unsubscribers.push(events.onSettingChanged(SET_OPERATOR_MSG, () => this._refreshOperatorMessage()));
      this._unsubscribers.push(events.onSettingChanged(SET_TILE,         () => this._refreshTiles()));
      this._unsubscribers.push(events.onSettingChanged(SET_THREAD,       () => this._refreshThreads()));
      this._unsubscribers.push(events.onSettingChanged(SET_DECISION,     () => this._refreshDecisions()));
    },

    async _refreshCanvas() {
      const members = await this._readSet(SET_CANVAS);
      this.data.canvas = this._normalizeCanvas(this._latest(members));
    },

    async _refreshOperatorMessage() {
      const members = await this._readSet(SET_OPERATOR_MSG);
      this.data.operatorMessage = this._normalizeOperatorMessage(this._latest(members));
    },

    async _refreshTiles() {
      const members = await this._readSet(SET_TILE);
      this.data.tiles = (members || []).map(m => this._normalizeTile(m));
    },

    async _refreshThreads() {
      const members = await this._readSet(SET_THREAD);
      this.data.threads = (members || []).map(m => this._normalizeThread(m));
    },

    async _refreshDecisions() {
      const members = await this._readSet(SET_DECISION);
      this.data.decisionsByTile = this._buildDecisionsByTile(members);
    },

    _buildDecisionsByTile(members) {
      const out = {};
      const sorted = this._sortByTime(members || []);
      for (const m of sorted) {
        const p = m.payload || {};
        if (!p.tile_id) continue;
        out[p.tile_id] = {
          kind: p.kind, choice: p.choice || '', sentAt: p.sentAt || '',
        };
      }
      return out;
    },

    async _readSet(setId) {
      try {
        const res = await fetch(
          `/api/graph/settings/${encodeURIComponent(setId)}`,
          { credentials: 'same-origin' },
        );
        if (!res.ok) return [];
        const body = await res.json().catch(() => ({}));
        return Array.isArray(body.members) ? body.members : [];
      } catch (e) {
        return [];
      }
    },

    async _writeSetting(setId, key, payload) {
      try {
        const res = await fetch('/api/graph/setting', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            set_id: setId,
            schema_revision: SCHEMA_REVISION,
            key,
            payload,
          }),
          credentials: 'same-origin',
        });
        if (!res.ok) return null;
        return await res.json().catch(() => ({}));
      } catch (e) {
        return null;
      }
    },

    // Pick the latest member from a list (highest updated_at or
    // created_at; fallback to last entry when timestamps absent).
    _latest(members) {
      if (!members || !members.length) return null;
      const sorted = this._sortByTime(members);
      return sorted[sorted.length - 1];
    },

    _memberTime(m) {
      return (m && (m.updated_at || m.created_at)) || '';
    },

    _sortByTime(members) {
      return [...members].sort((a, b) => {
        const ta = this._memberTime(a);
        const tb = this._memberTime(b);
        if (ta < tb) return -1;
        if (ta > tb) return 1;
        return 0;
      });
    },

    _normalizeCanvas(member) {
      const p = (member && member.payload) || {};
      return {
        ageMin: typeof p.ageMin === 'number' ? p.ageMin : 0,
        question: p.question || '',
        context: p.context || '',
        quickReplies: Array.isArray(p.quickReplies) ? p.quickReplies : [],
      };
    },

    _normalizeOperatorMessage(member) {
      const p = (member && member.payload) || {};
      return {
        text: p.text || '',
        sentAt: p.sentAt || null,
      };
    },

    _normalizeTile(member) {
      const p = (member && member.payload) || {};
      // Key is ``<coord>:<tile-session>`` per the schema; the tile's
      // own session is the segment after the colon. Members without
      // a colon-separated key fall back to the bare key.
      const key = (member && member.key) || '';
      const sep = key.indexOf(':');
      const session = sep >= 0 ? key.slice(sep + 1) : key;
      return {
        session,
        role: p.role || '',
        label: p.label || session,
        thing: p.thing || '',
        asks: p.asks || 'fyi',
        ageMin: typeof p.ageMin === 'number' ? p.ageMin : 0,
        updateKind: p.updateKind || 'refresh',
        detail: p.detail || '',
      };
    },

    _normalizeThread(member) {
      const p = (member && member.payload) || {};
      const key = (member && member.key) || '';
      const sep = key.indexOf(':');
      const session = sep >= 0 ? key.slice(sep + 1) : key;
      return {
        session,
        role: p.role || '',
        label: p.label || session,
        status: p.status || 'paused',
        lead: p.lead || '',
        bullets: Array.isArray(p.bullets) ? p.bullets : [],
        ageMin: typeof p.ageMin === 'number' ? p.ageMin : 0,
        totalTurns: typeof p.totalTurns === 'number' ? p.totalTurns : 0,
        needs: p.needs || '',
      };
    },

    // ─────── Computed ───────
    get needYourCall() {
      const fromTiles = this.data.tiles.filter(t => t.asks && t.asks !== 'fyi').length;
      const fromThreads = this.data.threads.filter(t => t.needs).length;
      return Math.max(fromTiles, fromThreads);
    },
    get pendingCommitCount() {
      return Number(this.data.pendingCommitCount || 0);
    },
    get beadsLandedCount() {
      return this.data.beads.filter(b => b.status === 'landed').length;
    },

    get sortedThreads() {
      const arr = [...this.data.threads];
      const statusOrder = { blocked: 0, investigating: 1, shipping: 2, researching: 3, designing: 4, paused: 5 };
      switch (this.sortKey) {
        case 'urgent': return arr.sort((a, b) => {
          if (!!a.needs !== !!b.needs) return a.needs ? -1 : 1;
          const sa = statusOrder[a.status] ?? 99;
          const sb = statusOrder[b.status] ?? 99;
          if (sa !== sb) return sa - sb;
          return a.ageMin - b.ageMin;
        });
        case 'recent': return arr.sort((a, b) => a.ageMin - b.ageMin);
        case 'turns':  return arr.sort((a, b) => b.totalTurns - a.totalTurns);
        case 'name':   return arr.sort((a, b) => a.session.localeCompare(b.session));
      }
      return arr;
    },

    decisionForTile(session) {
      return this.data.decisionsByTile[session] || null;
    },

    decisionLabel(d) {
      if (!d) return '';
      switch (d.kind) {
        case 'thumb_yes':       return 'thumb yes';
        case 'thumb_no':        return 'thumb no';
        case 'choice':          return d.choice || 'chose';
        case 'custom':          return d.choice || 'replied';
        case 'sitrep_request':  return 'requested sitrep';
        case 'refresh_request': return 'requested refresh';
      }
      return d.kind || '';
    },

    // ─────── Operator message ───────
    async onOperatorMessage() {
      const text = this.operatorDraft.trim();
      if (!text || this.sending) return;
      const replies = (this.data.canvas.quickReplies || []).map(r => r.trim());
      const isVerbatim = replies.includes(text);

      this.sending = true;
      const sentAt = new Date().toISOString();
      const result = await this._writeSetting(
        SET_OPERATOR_MSG, OPERATOR_MSG_KEY, { text, sentAt },
      );
      const ok = result !== null && (result.id || result.ok !== false);
      // Optimistic update — show the message even before the next read.
      this.data.operatorMessage = { text, sentAt };
      this.sending = false;

      this.operatorDraft = '';
      if (this.$refs.composer) this.$refs.composer.innerText = '';
      if (isVerbatim && ok) {
        this.celebrateWin();
        this.lastAction = '';
      } else if (ok) {
        this.lastAction = 'Sent';
        setTimeout(() => { this.lastAction = ''; }, 2500);
      } else {
        this.lastAction = 'Send failed';
        setTimeout(() => { this.lastAction = ''; }, 2500);
      }
    },

    // ─────── Tile decisions ───────
    async _writeDecision(tile, kind, choice) {
      // Each decision row is append-only — uuidv4 key so the substrate
      // stores them all rather than collapsing to one.
      const key = (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : `dec-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const payload = {
        tile_id: tile.session,
        kind,
        target_session: tile.session,
        sentAt: new Date().toISOString(),
      };
      if (choice !== undefined && choice !== null && choice !== '') {
        payload.choice = choice;
      }
      const result = await this._writeSetting(SET_DECISION, key, payload);
      // Optimistic decoration so the operator gets immediate visual
      // feedback; the SET_DECISION setting.changed subscription confirms
      // via re-resolve on the next event tick.
      this.data.decisionsByTile = {
        ...this.data.decisionsByTile,
        [tile.session]: { kind, choice: choice || '', sentAt: payload.sentAt },
      };
      return result;
    },

    onTileThumbYes(tile)        { return this._writeDecision(tile, 'thumb_yes'); },
    onTileThumbNo(tile)         { return this._writeDecision(tile, 'thumb_no'); },
    onTileChoice(tile, choice)  { return this._writeDecision(tile, 'choice', choice); },
    onTileCustom(tile, text)    { return this._writeDecision(tile, 'custom', text); },
    onTileSitrep(tile)          { return this._writeDecision(tile, 'sitrep_request'); },
    onTileRefresh(tile)         { return this._writeDecision(tile, 'refresh_request'); },

    // ─────── Misc UI ───────
    celebrateWin() {
      this.wins += 1;
      this.winCelebrating = true;
      setTimeout(() => { this.winCelebrating = false; }, 700);
      this._burstAt(this.$refs.winBadge);
    },
    _burstAt(anchor) {
      if (!anchor) return;
      const host = anchor.offsetParent || document.body;
      const r = anchor.getBoundingClientRect();
      const h = host.getBoundingClientRect();
      const cx = r.left - h.left + r.width / 2;
      const cy = r.top  - h.top  + r.height / 2;

      const plus = document.createElement('div');
      plus.className = 'win-burst';
      plus.textContent = '+1';
      plus.style.left = (cx - 8) + 'px';
      plus.style.top  = (cy - 16) + 'px';
      host.appendChild(plus);
      setTimeout(() => plus.remove(), 1200);

      const colors = ['#fcd34d', '#a78bfa', '#34d399', '#f472b6', '#60a5fa'];
      for (let i = 0; i < 14; i++) {
        const c = document.createElement('span');
        c.className = 'confetti-chip';
        c.style.left = (cx - 3) + 'px';
        c.style.top  = (cy - 5) + 'px';
        c.style.background = colors[i % colors.length];
        const angle = (Math.random() * Math.PI) - Math.PI;
        const dist = 36 + Math.random() * 32;
        c.style.setProperty('--dx', Math.cos(angle) * dist + 'px');
        c.style.setProperty('--dy', (Math.sin(angle) * dist - 16) + 'px');
        c.style.setProperty('--rot', (Math.random() * 540 - 270) + 'deg');
        host.appendChild(c);
        setTimeout(() => c.remove(), 1100);
      }
    },

    onRefreshAll() {
      if (this.refreshState === 'requested') {
        this.refreshState = 'idle';
        return;
      }
      if (this.refreshState !== 'idle') return;
      this.refreshState = 'pending';
      setTimeout(() => {
        if (this.refreshState === 'pending') this.refreshState = 'requested';
      }, 700);
      this.loadBoard();
    },

    renderInlineLinks(s) {
      if (!s) return '';
      const esc = (t) => t
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      const re = /\[([^\]]+)\]\(([^)]+)\)/g;
      let out = '', last = 0, m;
      while ((m = re.exec(s)) !== null) {
        out += esc(s.slice(last, m.index));
        const href = esc(m[2]);
        const label = esc(m[1]);
        out += `<a href="${href}" target="_top">${label}</a>`;
        last = m.index + m[0].length;
      }
      out += esc(s.slice(last));
      return out;
    },

    useQuickReply(text) {
      this.operatorDraft = text;
      if (this.$refs.composer) {
        this.$refs.composer.innerText = text;
        this.$refs.composer.focus();
      }
    },

    // ─────── Pure renderers ───────
    askLabel(a) {
      return ({ yes_no: 'yes/no', decide: 'decide', merge: 'merge', approve: 'approve', fyi: 'fyi' })[a] || a;
    },
    statusBadge(s) {
      return ({
        shipping:      'bg-emerald-500/15 text-emerald-300',
        blocked:       'bg-amber-500/15 text-amber-300',
        designing:     'bg-violet-500/15 text-violet-300',
        researching:   'bg-sky-500/15 text-sky-300',
        investigating: 'bg-amber-500/15 text-amber-300',
        paused:        'bg-slate-700/60 text-slate-300',
      })[s] || 'bg-slate-700/60 text-slate-300';
    },
    askBadge(a) {
      return ({
        decide:  'bg-amber-500/15 text-amber-300',
        merge:   'bg-amber-500/15 text-amber-300',
        approve: 'bg-amber-500/15 text-amber-300',
        yes_no:  'bg-rose-500/15 text-rose-300',
        fyi:     'bg-slate-700/50 text-slate-300',
      })[a] || 'bg-slate-700/50 text-slate-300';
    },
    askBorder(a) {
      if (a === 'yes_no') return 'border-rose-500/30';
      if (a === 'decide' || a === 'merge' || a === 'approve') return 'border-amber-500/25';
      return 'border-slate-800';
    },
    ageStr(min) {
      if (!min || min < 1) return 'just now';
      if (min < 60) return `${min}m ago`;
      const h = Math.floor(min / 60);
      const m = min % 60;
      return m ? `${h}h ${m}m ago` : `${h}h ago`;
    },
  };
}

if (typeof window !== 'undefined') {
  window.coordinatorBoard = coordinatorBoard;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { coordinatorBoard };
}
