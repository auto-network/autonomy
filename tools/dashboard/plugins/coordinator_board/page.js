// Coordinator Board plugin — frontend Alpine factory.
// Mirrors the design at revision f710c702 of design 59dd05c1-...
// (graph://81e126c8-75e App spec). The mock's inline `data: { ... }`
// literal is replaced by a fetch from `/api/coordinator/board`; the
// composer POSTs to `/api/coordinator/message`.
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
    // Populated from /api/coordinator/board on init; the empty shape
    // here is just a render-safe placeholder so the template doesn't
    // throw on its first paint before the fetch resolves.
    data: {
      snapshotTime: '',
      broadcastPlaceholder: 'Message me…',
      canvas: { ageMin: 0, question: '', context: '', quickReplies: [] },
      operatorMessage: { text: '', sentAt: null },
      tiles: [],
      threads: [],
      beads: [],
      convergentDecisions: [],
      openFollowups: [],
      docs: { coordMap: '', walkthrough: '' },
    },

    // ─────── Lifecycle ───────
    async init() {
      await this.loadBoard();
    },

    async loadBoard() {
      try {
        const res = await fetch('/api/coordinator/board', { credentials: 'same-origin' });
        if (!res.ok) return;
        const payload = await res.json();
        this.data = this._normalizePayload(payload);
        // Fresh data landed → release any pending refresh affordance.
        if (this.refreshState !== 'idle') this.refreshState = 'idle';
      } catch (e) {
        // Silent — UI keeps the prior payload.
      }
    },

    // Normalize a server payload into the render-safe shape. Any field
    // the server omits gets a sensible default so x-show / template
    // iteration doesn't blow up downstream.
    _normalizePayload(p) {
      p = p || {};
      const canvas = p.canvas || {};
      return {
        snapshotTime: p.snapshotTime || '',
        broadcastPlaceholder: p.broadcastPlaceholder || 'Message me…',
        canvas: {
          ageMin: typeof canvas.ageMin === 'number' ? canvas.ageMin : 0,
          question: canvas.question || '',
          context: canvas.context || '',
          quickReplies: Array.isArray(canvas.quickReplies) ? canvas.quickReplies : [],
        },
        operatorMessage: {
          text: (p.operatorMessage && p.operatorMessage.text) || '',
          sentAt: (p.operatorMessage && p.operatorMessage.sentAt) || null,
        },
        tiles: Array.isArray(p.tiles) ? p.tiles : [],
        threads: Array.isArray(p.threads) ? p.threads : [],
        beads: Array.isArray(p.beads) ? p.beads : [],
        convergentDecisions: Array.isArray(p.convergentDecisions) ? p.convergentDecisions : [],
        openFollowups: Array.isArray(p.openFollowups) ? p.openFollowups : [],
        docs: p.docs || { coordMap: '', walkthrough: '' },
      };
    },

    // ─────── Computed ───────
    get needYourCall() {
      const fromTiles = this.data.tiles.filter(t => t.asks && t.asks !== 'fyi').length;
      const fromThreads = this.data.threads.filter(t => t.needs).length;
      return Math.max(fromTiles, fromThreads);
    },
    get pendingCommitCount() {
      // Server-provided when wired; otherwise zero.
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

    // ─────── Handlers ───────
    async onOperatorMessage() {
      const text = this.operatorDraft.trim();
      if (!text || this.sending) return;
      const replies = (this.data.canvas.quickReplies || []).map(r => r.trim());
      const isVerbatim = replies.includes(text);

      this.sending = true;
      let ok = false;
      try {
        const res = await fetch('/api/coordinator/message', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
          credentials: 'same-origin',
        });
        const body = await res.json().catch(() => ({}));
        ok = res.ok && body.ok !== false;
        // Optimistic update — surfaces the message even before the
        // next /api/coordinator/board refresh.
        this.data.operatorMessage = { text, sentAt: body.sentAt || new Date().toISOString() };
      } catch (e) {
        // Treat network error as non-fatal; the optimistic update
        // already gave the operator visual confirmation.
      } finally {
        this.sending = false;
      }

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
      // Tap-to-dismiss when already requested.
      if (this.refreshState === 'requested') {
        this.refreshState = 'idle';
        return;
      }
      if (this.refreshState !== 'idle') return;
      this.refreshState = 'pending';
      // Brief spinner while the refetch is firing, then transition to
      // 'requested' until fresh data arrives (loadBoard resets to 'idle').
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

// Node-friendly export so L1 unit tests can pull the helpers without
// touching a browser. Browsers ignore `module`.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { coordinatorBoard };
}
