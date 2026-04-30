// Primers UI plugin — frontend Alpine factory.
//
// Lists workspaces (one entry per ``load_workspaces()`` member, served
// by /api/primers/workspaces) and renders each one's markdown primer
// on demand via /api/primers/workspace/{id}.
//
// Selection (the workspace id) persists to localStorage so the SPA
// restores it after navigation away and back. Markdown rendering goes
// through the dashboard's already-loaded ``marked`` + ``DOMPurify``
// (see templates/base.html); no per-page CDN fetch.

const PRIMERS_LS_KEY = 'autonomy.plugin.primers.selection';

function primersPage() {
  return {
    // ── Data ───────────────────────────────────────────────────
    workspaces: [],
    primers: {},          // { workspace_id: {markdown, token_estimate, workspace} }

    // ── Selection / view ──────────────────────────────────────
    selectedId: null,
    loading: false,
    error: null,
    copied: false,
    errorCopied: false,
    showListOnMobile: false,

    // ── Lifecycle ──────────────────────────────────────────────
    async init() {
      const saved = this._readPersistedSelection();
      await this._loadWorkspaces();
      if (saved && saved.workspace_id
          && this.workspaces.some(w => w.id === saved.workspace_id)) {
        this.selectedId = saved.workspace_id;
        await this._loadPrimer(this.selectedId);
      }
    },

    // ── Computed-style getters ─────────────────────────────────
    get selectedWorkspace() {
      return this.workspaces.find(w => w.id === this.selectedId) || {};
    },

    get currentPrimer() {
      return this.selectedId ? (this.primers[this.selectedId] || null) : null;
    },

    get writability() {
      const ws = this.selectedWorkspace;
      if (!ws.id) return { label: '', color: 'text-gray-500' };
      return ws.writable
        ? { label: 'writable repos', color: 'text-emerald-400' }
        : { label: 'read-only', color: 'text-amber-400' };
    },

    get renderedMarkdown() {
      const cur = this.currentPrimer;
      if (!cur) return '';
      const md = cur.markdown || '';
      if (!window.marked || !window.DOMPurify) return md;
      return window.DOMPurify.sanitize(window.marked.parse(md));
    },

    // ── Actions ────────────────────────────────────────────────
    async select(id) {
      this.selectedId = id;
      this.showListOnMobile = false;
      this._persistSelection();
      await this._loadPrimer(id);
    },

    async retry() {
      if (!this.selectedId) return;
      await this._loadPrimer(this.selectedId);
    },

    copy() {
      const cur = this.currentPrimer;
      if (cur && navigator.clipboard) {
        navigator.clipboard.writeText(cur.markdown || '');
      }
      this.copied = true;
      setTimeout(() => { this.copied = false; }, 1200);
    },

    copyError() {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(String(this.error || ''));
      }
      this.errorCopied = true;
      setTimeout(() => { this.errorCopied = false; }, 1200);
    },

    // ── Data loading ──────────────────────────────────────────
    async _loadWorkspaces() {
      try {
        const res = await window.Autonomy.fetch('/api/primers/workspaces');
        if (!res.ok) { this.workspaces = []; return; }
        const data = await res.json();
        this.workspaces = Array.isArray(data.workspaces) ? data.workspaces : [];
      } catch (e) {
        this.workspaces = [];
      }
    },

    async _loadPrimer(id) {
      this.loading = true;
      this.error = null;
      try {
        const res = await window.Autonomy.fetch(
          '/api/primers/workspace/' + encodeURIComponent(id),
        );
        if (!res.ok) {
          let message = 'Render failed (HTTP ' + res.status + ')';
          try {
            const body = await res.json();
            if (body && body.error) {
              message = body.error;
              if (body.detail) message += ': ' + body.detail;
            }
          } catch (_) { /* keep status-only message */ }
          this.error = message;
          return;
        }
        const data = await res.json();
        this.primers = { ...this.primers, [id]: data };
      } catch (e) {
        this.error = 'Network error: ' + (e.message || e);
      } finally {
        this.loading = false;
      }
    },

    // ── localStorage persistence ──────────────────────────────
    _persistSelection() {
      try {
        localStorage.setItem(PRIMERS_LS_KEY, JSON.stringify({
          workspace_id: this.selectedId,
        }));
      } catch (e) { /* storage disabled — selection is session-local */ }
    },

    _readPersistedSelection() {
      try {
        const raw = localStorage.getItem(PRIMERS_LS_KEY);
        if (!raw) return null;
        const saved = JSON.parse(raw);
        if (!saved || typeof saved !== 'object') return null;
        return {
          workspace_id: typeof saved.workspace_id === 'string'
            ? saved.workspace_id : null,
        };
      } catch (e) {
        return null;
      }
    },
  };
}
