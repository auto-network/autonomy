// Coordinator Board plugin — frontend Alpine factory.
// Mirrors design 59dd05c1 (revision 197+ post auto-1aef5). Bead
// auto-lffg5 retired the v1 ``api.py`` facade; bead auto-obo63 swapped
// the polled decision-log for ``setting.changed`` SSE; bead auto-1aef5
// adds Sprints + the four Tracking-tab sets, the tile expansion +
// detail panel, the quick-reply chosen-state migration, and the
// peer-session-only keyshape for tile + thread.
//
//   reads + setting.changed subscribes:
//     dashboard.coordinator-canvas
//     dashboard.operator-message-to-coordinator
//     dashboard.coordinator-tile  (target_revision=2)
//     dashboard.coordinator-thread (target_revision=2)
//     dashboard.coordinator-decision
//     dashboard.coordinator-sprint
//     dashboard.coordinator-bead
//     dashboard.coordinator-convergent-decision
//     dashboard.coordinator-open-followup
//     dashboard.coordinator-docs
//
//   writes:
//     POST /api/graph/setting   (operator-message + decision rows)
const SET_CANVAS = 'dashboard.coordinator-canvas';
const SET_OPERATOR_MSG = 'dashboard.operator-message-to-coordinator';
const SET_TILE = 'dashboard.coordinator-tile';
const SET_THREAD = 'dashboard.coordinator-thread';
const SET_DECISION = 'dashboard.coordinator-decision';
const SET_SPRINT = 'dashboard.coordinator-sprint';
const SET_BEAD = 'dashboard.coordinator-bead';
const SET_CONVERGENT_DECISION = 'dashboard.coordinator-convergent-decision';
const SET_OPEN_FOLLOWUP = 'dashboard.coordinator-open-followup';
const SET_DOCS = 'dashboard.coordinator-docs';
// Default revision for sets the page writes at (canvas, operator-message,
// decision). Tile + thread are read-only from the page's perspective —
// peers self-publish — so the page only needs to read them at revision 2.
const SCHEMA_REVISION = 1;
const TILE_SCHEMA_REVISION = 2;
const THREAD_SCHEMA_REVISION = 2;
const OPERATOR_MSG_KEY = 'default';
const DOCS_KEY = 'default';

function _cssEscape(s) {
  return (window.CSS && window.CSS.escape)
    ? window.CSS.escape(s)
    : String(s).replace(/["\\]/g, '\\$&');
}

function coordinatorBoard() {
  return {
    // ─────── UI state ───────
    tab: 'primary',
    tabs: [
      { key: 'primary',  label: 'One thing' },
      { key: 'tracking', label: 'Tracking' },
      { key: 'sprints',  label: 'Sprints' },
    ],
    lastAction: '',
    operatorDraft: '',
    refreshState: 'idle',
    wins: 0,
    winCelebrating: false,
    // Transient — drives the picked-pill `.qr-confirm` + `.qr-check-pop`
    // animations on a verbatim send.
    justChosen: null,
    // Transient — `<session>:<choice>` after a tile-choice tap.
    justTileChoice: null,
    // Transient — `<session>:<yes|no>` after a tile yes/no thumb tap.
    justPickedTile: null,
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
      // ``chosenReply`` carries the most-recent verbatim quick-reply
      // pick. The picked pill flips to a chosen state and stays until
      // the next canvas refresh resets it.
      canvas: {
        ageMin: 0, question: '', context: '', quickReplies: [],
        chosenReply: null,
      },
      operatorMessage: { text: '', sentAt: null },
      tiles: [],
      threads: [],
      // tile_id → { kind, choice, sentAt } for the most-recent decision
      // applied to that tile.
      decisionsByTile: {},
      sprints: [],
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
      const [
        canvas, op, tiles, threads, decisions,
        sprints, beads, convergent, followups, docs,
      ] = await Promise.all([
        this._readSet(SET_CANVAS),
        this._readSet(SET_OPERATOR_MSG),
        this._readSet(SET_TILE, TILE_SCHEMA_REVISION),
        this._readSet(SET_THREAD, THREAD_SCHEMA_REVISION),
        this._readSet(SET_DECISION),
        this._readSet(SET_SPRINT),
        this._readSet(SET_BEAD),
        this._readSet(SET_CONVERGENT_DECISION),
        this._readSet(SET_OPEN_FOLLOWUP),
        this._readSet(SET_DOCS),
      ]);

      this.data.canvas = this._normalizeCanvas(this._latest(canvas));
      this.data.operatorMessage = this._normalizeOperatorMessage(this._latest(op));
      this.data.tiles = (tiles || []).map(m => this._normalizeTile(m));
      this.data.threads = (threads || []).map(m => this._normalizeThread(m));
      this.data.decisionsByTile = this._buildDecisionsByTile(decisions);
      this.data.sprints = (sprints || []).map(m => this._normalizeSprint(m));
      this.data.beads = (beads || []).map(m => this._normalizeBead(m));
      this.data.convergentDecisions = (convergent || [])
        .map(m => this._normalizeConvergentDecision(m))
        .filter(d => d.title);
      this.data.openFollowups = (followups || [])
        .map(m => this._normalizeOpenFollowup(m))
        .filter(t => t);
      this.data.docs = this._normalizeDocs(this._latest(docs));
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
      this._unsubscribers.push(events.onSettingChanged(SET_CANVAS,              () => this._refreshCanvas()));
      this._unsubscribers.push(events.onSettingChanged(SET_OPERATOR_MSG,        () => this._refreshOperatorMessage()));
      this._unsubscribers.push(events.onSettingChanged(SET_TILE,                () => this._refreshTiles()));
      this._unsubscribers.push(events.onSettingChanged(SET_THREAD,              () => this._refreshThreads()));
      this._unsubscribers.push(events.onSettingChanged(SET_DECISION,            () => this._refreshDecisions()));
      this._unsubscribers.push(events.onSettingChanged(SET_SPRINT,              () => this._refreshSprints()));
      this._unsubscribers.push(events.onSettingChanged(SET_BEAD,                () => this._refreshBeads()));
      this._unsubscribers.push(events.onSettingChanged(SET_CONVERGENT_DECISION, () => this._refreshConvergentDecisions()));
      this._unsubscribers.push(events.onSettingChanged(SET_OPEN_FOLLOWUP,       () => this._refreshOpenFollowups()));
      this._unsubscribers.push(events.onSettingChanged(SET_DOCS,                () => this._refreshDocs()));
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
      const members = await this._readSet(SET_TILE, TILE_SCHEMA_REVISION);
      this.data.tiles = (members || []).map(m => this._normalizeTile(m));
    },

    async _refreshThreads() {
      const members = await this._readSet(SET_THREAD, THREAD_SCHEMA_REVISION);
      this.data.threads = (members || []).map(m => this._normalizeThread(m));
    },

    async _refreshDecisions() {
      const members = await this._readSet(SET_DECISION);
      this.data.decisionsByTile = this._buildDecisionsByTile(members);
    },

    async _refreshSprints() {
      const members = await this._readSet(SET_SPRINT);
      this.data.sprints = (members || []).map(m => this._normalizeSprint(m));
    },

    async _refreshBeads() {
      const members = await this._readSet(SET_BEAD);
      this.data.beads = (members || []).map(m => this._normalizeBead(m));
    },

    async _refreshConvergentDecisions() {
      const members = await this._readSet(SET_CONVERGENT_DECISION);
      this.data.convergentDecisions = (members || [])
        .map(m => this._normalizeConvergentDecision(m))
        .filter(d => d.title);
    },

    async _refreshOpenFollowups() {
      const members = await this._readSet(SET_OPEN_FOLLOWUP);
      this.data.openFollowups = (members || [])
        .map(m => this._normalizeOpenFollowup(m))
        .filter(t => t);
    },

    async _refreshDocs() {
      const members = await this._readSet(SET_DOCS);
      this.data.docs = this._normalizeDocs(this._latest(members));
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

    async _readSet(setId, targetRevision) {
      const base = `/api/graph/settings/${encodeURIComponent(setId)}`;
      const url = targetRevision
        ? `${base}?target_revision=${encodeURIComponent(targetRevision)}`
        : base;
      try {
        const res = await window.Autonomy.fetch(url, { credentials: 'same-origin' });
        if (!res.ok) return [];
        const body = await res.json().catch(() => ({}));
        return Array.isArray(body.members) ? body.members : [];
      } catch (e) {
        return [];
      }
    },

    async _writeSetting(setId, key, payload, schemaRevision) {
      try {
        const res = await window.Autonomy.fetch('/api/graph/setting', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            set_id: setId,
            schema_revision: schemaRevision || SCHEMA_REVISION,
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
        // chosenReply is page-side transient, not stored in the canvas
        // payload. Reset on every canvas read so a fresh banger clears
        // the picked-pill state from the previous one.
        chosenReply: null,
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
      // Bead auto-1aef5 rekeyed tile members from ``<coord>:<peer>``
      // (v1) to ``<peer>`` alone (v2). The page reads at v2 + handles
      // both shapes for safety: a stray legacy row with a colon-prefixed
      // key still resolves to the peer half rather than rendering as a
      // composite session id.
      const key = (member && member.key) || '';
      const sep = key.indexOf(':');
      const session = sep >= 0 ? key.slice(sep + 1) : key;
      // ``detail`` was a string in v1; v2 promoted it to {context, choices}.
      // Normalize either shape into the v2 form so the template branches
      // on a single contract.
      let detail = null;
      if (typeof p.detail === 'string' && p.detail) {
        detail = { context: p.detail, choices: [] };
      } else if (p.detail && typeof p.detail === 'object') {
        detail = {
          context: p.detail.context || '',
          choices: Array.isArray(p.detail.choices) ? p.detail.choices : [],
        };
      }
      return {
        session,
        role: p.role || '',
        label: p.label || session,
        thing: p.thing || '',
        asks: p.asks || 'fyi',
        ageMin: typeof p.ageMin === 'number' ? p.ageMin : 0,
        updateKind: p.updateKind || 'refresh',
        detail,
        // Per-tile UI state (not persisted in the Setting payload).
        expanded: false,
        tileChoice: null,
        answer: null,
        sitrepQueued: false,
        _refreshState: 'idle',
        _customDraft: '',
      };
    },

    _normalizeThread(member) {
      const p = (member && member.payload) || {};
      // Same v1→v2 keyshape note as _normalizeTile.
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

    _normalizeSprint(member) {
      const p = (member && member.payload) || {};
      const key = (member && member.key) || '';
      return {
        id: key,
        title: p.title || '',
        status: p.status || 'active',
        ageMin: typeof p.ageMin === 'number' ? p.ageMin : 0,
        participants: Array.isArray(p.participants) ? p.participants : [],
        commitCount: typeof p.commitCount === 'number' ? p.commitCount : 0,
        beadCount: typeof p.beadCount === 'number' ? p.beadCount : 0,
        arc: p.arc || '',
        shipped: Array.isArray(p.shipped) ? p.shipped : [],
        inFlight: Array.isArray(p.inFlight) ? p.inFlight : [],
        needs: p.needs || '',
      };
    },

    _normalizeBead(member) {
      const p = (member && member.payload) || {};
      const key = (member && member.key) || '';
      return {
        id: key,
        commit: p.commit || '',
        scope: p.scope || '',
        status: p.status || 'landed',
        note: p.note || '',
      };
    },

    _normalizeConvergentDecision(member) {
      const p = (member && member.payload) || {};
      return {
        title: p.title || '',
        raisedBy: Array.isArray(p.raisedBy) ? p.raisedBy : [],
      };
    },

    _normalizeOpenFollowup(member) {
      const p = (member && member.payload) || {};
      return p.text || '';
    },

    _normalizeDocs(member) {
      const p = (member && member.payload) || {};
      return {
        coordMap: p.coordMap || '',
        walkthrough: p.walkthrough || '',
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
      const statusOrder = { blocked: 0, investigating: 1, shipping: 2, researching: 3, designing: 4, paused: 5, compacted: 6 };
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

    get sortedSprints() {
      const order = { active: 0, shipping: 1, parked: 2, design: 3, nascent: 4, done: 5 };
      return [...this.data.sprints].sort((a, b) => {
        if (!!a.needs !== !!b.needs) return a.needs ? -1 : 1;
        const sa = order[a.status] ?? 99;
        const sb = order[b.status] ?? 99;
        if (sa !== sb) return sa - sb;
        return a.ageMin - b.ageMin;
      });
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
        this.celebrateWin(text);
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

    onTileThumbYes(tile) {
      if (tile.answer) return;
      tile.answer = 'yes';
      this._tileWinFlourish(tile, 'yes');
      return this._writeDecision(tile, 'thumb_yes');
    },
    onTileThumbNo(tile) {
      if (tile.answer) return;
      tile.answer = 'no';
      this._tileWinFlourish(tile, 'no');
      return this._writeDecision(tile, 'thumb_no');
    },
    onTileChoice(tile, choice) {
      if (tile.tileChoice) return;
      tile.tileChoice = choice;
      this.wins += 1;
      this.winCelebrating = true;
      this.justTileChoice = tile.session + ':' + choice;
      this.$nextTick(() => {
        const sel = `article[data-tile-session="${_cssEscape(tile.session)}"] `
          + `.quick-reply[data-just-chosen="true"]`;
        const btn = this.$root.querySelector(sel);
        if (btn) this._burstAt(btn, { count: 22, spread: 'radial' });
      });
      setTimeout(() => { this.winCelebrating = false; }, 700);
      setTimeout(() => { this.justTileChoice = null; }, 750);
      return this._writeDecision(tile, 'choice', choice);
    },
    onTileCustom(tile, text) {
      const trimmed = (text || '').trim();
      if (!trimmed) return;
      tile.tileChoice = trimmed;
      tile._customDraft = '';
      return this._writeDecision(tile, 'custom', trimmed);
    },
    onTileSitrep(tile) {
      if (tile.sitrepQueued) return;
      tile.sitrepQueued = true;
      return this._writeDecision(tile, 'sitrep_request');
    },
    onTileRefresh(tile) {
      if (tile._refreshState === 'requested') {
        tile._refreshState = 'idle';
        return null;
      }
      if (tile._refreshState && tile._refreshState !== 'idle') return null;
      tile._refreshState = 'pending';
      setTimeout(() => {
        if (tile._refreshState === 'pending') tile._refreshState = 'requested';
      }, 700);
      return this._writeDecision(tile, 'refresh_request');
    },

    _tileWinFlourish(tile, kind) {
      this.wins += 1;
      this.winCelebrating = true;
      this.justPickedTile = tile.session + ':' + kind;
      this.$nextTick(() => {
        const sel = `article[data-tile-session="${_cssEscape(tile.session)}"] `
          + `.thumb-btn[data-kind="${kind}"]`;
        const btn = this.$root.querySelector(sel);
        if (btn) this._burstAt(btn, { count: 18, spread: 'radial' });
      });
      setTimeout(() => { this.winCelebrating = false; }, 700);
      setTimeout(() => { this.justPickedTile = null; }, 600);
    },

    // ─────── Misc UI ───────
    celebrateWin(reply) {
      this.wins += 1;
      this.winCelebrating = true;
      // Persist the picked pill's chosen state until the next canvas
      // refresh — operator's eye-anchor for "you picked this".
      if (reply) {
        this.data.canvas.chosenReply = reply;
        this.justChosen = reply;
        this.$nextTick(() => {
          const sel = `.coord-board .quick-reply[data-reply="${_cssEscape(reply)}"]`;
          const pill = this.$root.querySelector(sel);
          if (pill) this._burstAt(pill, { count: 22, spread: 'radial' });
        });
        setTimeout(() => { this.justChosen = null; }, 750);
      } else {
        // Fallback: a non-verbatim send still pulses the win badge.
        this.$nextTick(() => {
          if (this.$refs.winBadge) this._burstAt(this.$refs.winBadge);
        });
      }
      setTimeout(() => { this.winCelebrating = false; }, 700);
    },
    _burstAt(anchor, opts) {
      if (!anchor) return;
      opts = opts || {};
      const count = opts.count || 14;
      const radial = opts.spread === 'radial';
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

      const colors = ['#fcd34d', '#a78bfa', '#34d399', '#f472b6', '#60a5fa', '#22d3ee'];
      for (let i = 0; i < count; i++) {
        const c = document.createElement('span');
        c.className = 'confetti-chip';
        c.style.left = (cx - 3) + 'px';
        c.style.top  = (cy - 5) + 'px';
        c.style.background = colors[i % colors.length];
        const angle = radial
          ? (Math.PI * 2 * i / count) + (Math.random() * 0.4 - 0.2)
          : (Math.random() * Math.PI) - Math.PI;
        const dist = 60 + Math.random() * 40;
        c.style.setProperty('--dx', Math.cos(angle) * dist + 'px');
        c.style.setProperty('--dy', Math.sin(angle) * dist + 'px');
        c.style.setProperty('--rot', (Math.random() * 720 - 360) + 'deg');
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
        compacted:     'bg-slate-800/70 text-slate-400',
      })[s] || 'bg-slate-700/60 text-slate-300';
    },
    sprintStatusBadge(s) {
      return ({
        active:    'bg-violet-500/20 text-violet-200',
        shipping:  'bg-emerald-500/15 text-emerald-300',
        parked:    'bg-slate-700/60 text-slate-300',
        design:    'bg-violet-500/15 text-violet-300',
        nascent:   'bg-sky-500/15 text-sky-300',
        done:      'bg-slate-800/60 text-slate-500',
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
