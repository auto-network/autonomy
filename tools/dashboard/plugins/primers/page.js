// Primers UI plugin — frontend Alpine factory.
//
// Lists workspaces for the selected org (served by
// /api/primers/workspaces) and renders each one's markdown primer on
// demand via /api/primers/workspace/{id}.
//
// Selection (the workspace id) persists to localStorage so the SPA
// restores it after navigation away and back. Markdown rendering goes
// through the dashboard's already-loaded ``marked`` + ``DOMPurify``
// (see templates/base.html); no per-page CDN fetch.

const PRIMERS_LS_KEY = 'autonomy.plugin.primers.selection';

function primersPage() {
  return {
    // ── Data ───────────────────────────────────────────────────
    orgs: [],
    selectedOrg: 'autonomy',
    workspaces: [],
    primers: {},          // { workspace_id: {markdown, token_estimate, workspace} }
    query: '',

    // ── Selection / view ──────────────────────────────────────
    selectedId: null,
    loading: false,
    error: null,
    copied: false,
    errorCopied: false,
    showListOnMobile: false,
    loadingOrgs: false,
    topbarHandle: null,

    // ── Lifecycle ──────────────────────────────────────────────
    async init() {
      const saved = this._readPersistedSelection();
      await this._loadOrgs();
      if (saved && saved.org && this.orgs.includes(saved.org)) {
        this.selectedOrg = saved.org;
      } else if (!this.orgs.includes(this.selectedOrg)) {
        this.selectedOrg = this.orgs.includes('autonomy')
          ? 'autonomy'
          : (this.orgs[0] || 'autonomy');
      }
      this._updateTopbar();
      await this._loadWorkspaces();
      if (saved && saved.workspace_id
          && this.workspaces.some(w => w.id === saved.workspace_id)) {
        this.selectedId = saved.workspace_id;
        await this._loadPrimer(this.selectedId);
      }
    },

    destroy() {
      if (this.topbarHandle && typeof this.topbarHandle.destroy === 'function') {
        this.topbarHandle.destroy();
      }
      this.topbarHandle = null;
    },

    // ── Computed-style getters ─────────────────────────────────
    get selectedWorkspace() {
      return this.workspaces.find(w => w.id === this.selectedId) || {};
    },

    get currentPrimer() {
      return this.selectedId ? (this.primers[this.selectedId] || null) : null;
    },

    get filteredWorkspaces() {
      const q = String(this.query || '').trim().toLowerCase();
      if (!q) return this.workspaces;
      return this.workspaces.filter(ws => {
        const haystack = [
          ws.id,
          ws.name,
          ws.org,
          ws.image,
        ].join(' ').toLowerCase();
        return haystack.indexOf(q) !== -1;
      });
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

    async onOrgChange() {
      this.selectedId = null;
      this.workspaces = [];
      this.primers = {};
      this.error = null;
      this.copied = false;
      this.errorCopied = false;
      this.showListOnMobile = false;
      this._persistSelection();
      this._updateTopbar();
      await this._loadWorkspaces();
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
    async _loadOrgs() {
      this.loadingOrgs = true;
      try {
        const res = await window.Autonomy.fetch('/api/orgs');
        if (!res.ok) { this.orgs = ['autonomy']; return; }
        const data = await res.json();
        const slugs = (data.orgs || [])
          .map(entry => (entry && entry.org && entry.org.slug) || entry.slug)
          .filter(Boolean);
        this.orgs = slugs.length ? slugs : ['autonomy'];
      } catch (e) {
        this.orgs = ['autonomy'];
      } finally {
        this.loadingOrgs = false;
        this._updateTopbar();
      }
    },

    async _loadWorkspaces() {
      try {
        const res = await this._fetchAsOrg(
          '/api/primers/workspaces',
          this.selectedOrg,
        );
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
        const res = await this._fetchAsOrg(
          '/api/primers/workspace/' + encodeURIComponent(id),
          this.selectedOrg,
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

    _fetchAsOrg(path, org) {
      return fetch(path, {
        headers: { 'X-Graph-Org': org },
      });
    },

    _updateTopbar() {
      if (!window.Autonomy || !window.Autonomy.topbar
          || typeof window.Autonomy.topbar.set !== 'function') {
        return;
      }
      const options = this._topbarOptions();
      if (this.topbarHandle && typeof this.topbarHandle.update === 'function') {
        this.topbarHandle.update(options);
      } else {
        this.topbarHandle = window.Autonomy.topbar.set(options);
      }
    },

    _topbarOptions() {
      return {
        title: 'Primers',
        subtitle: 'Workspace context previews',
        controls: [
          {
            type: 'search',
            id: 'primers-workspace-filter',
            testId: 'primers-topbar-search',
            placeholder: 'Filter workspaces',
            value: this.query,
            onInput: value => {
              this.query = value || '';
            },
          },
          {
            type: 'select',
            id: 'primers-org',
            testId: 'org-picker',
            label: 'Org',
            value: this.selectedOrg,
            options: this.orgs.length ? this.orgs : [this.selectedOrg],
            onChange: async value => {
              this.selectedOrg = value;
              await this.onOrgChange();
            },
          },
          {
            type: 'html',
            id: 'primers-scope-chip',
            html: '<span class="app-topbar-control-label" data-testid="primers-scope-chip">scoped</span>',
          },
        ],
      };
    },

    // ── localStorage persistence ──────────────────────────────
    _persistSelection() {
      try {
        localStorage.setItem(PRIMERS_LS_KEY, JSON.stringify({
          org: this.selectedOrg,
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
          org: typeof saved.org === 'string' ? saved.org : null,
          workspace_id: typeof saved.workspace_id === 'string'
            ? saved.workspace_id : null,
        };
      } catch (e) {
        return null;
      }
    },
  };
}
