// Settings UI plugin — frontend Alpine factory.
//
// Browse mode reads + writes Setting rows across orgs via the existing
// /api/orgs, /api/graph/sets, /api/graph/settings/<set_id>, and
// POST /api/graph/setting endpoints. Diagnostics mode adds read-only
// visibility into Settings throughput, storage footprint, and per-key
// storage using the dashboard's diag surfaces.
//
// Selection state (org / set / member) persists to localStorage so
// SPA navigations restore where the operator left off.

const SETTINGS_LS_KEY = 'autonomy.plugin.settings.selection';
// Default schema_revision for POSTs. Server-side validation will
// reject revisions that don't match the registered Pydantic model;
// v1 surfaces that error inline rather than choosing a revision per
// set_id (a v2 concern that needs schema doc surfacing).
const DEFAULT_SCHEMA_REVISION = 1;

function settingsPage() {
  return {
    // ── Selection ──────────────────────────────────────────────
    activeTab: 'browse',
    orgs: [],
    selectedOrg: 'autonomy',
    sets: {},          // { org: [{set_id, count}, ...] }
    members: {},       // { set_id: [{key, payload, state, stored_revision, ...}, ...] }
    selectedSetId: null,
    selectedKey: null,

    // ── Editor state ───────────────────────────────────────────
    editing: false,
    editorText: '',
    errorMessage: null,

    // ── Diagnostics state ──────────────────────────────────────
    diagSummary: null,
    diagMediator: null,
    diagSets: [],
    diagSelectedSetId: null,
    diagSetDetail: null,
    diagWindow: 'last_60s',
    diagLoadedOrg: null,
    diagError: null,

    // ── Loading / error gating ─────────────────────────────────
    loadingOrgs: false,
    loadingSets: false,
    loadingMembers: false,
    diagLoading: false,

    // ── Lifecycle ──────────────────────────────────────────────
    async init() {
      // Read persisted selection but do NOT apply it to selectedOrg
      // until /api/orgs has populated this.orgs — otherwise Alpine
      // sets the <select> value before the matching <option> exists,
      // and the picker silently falls back to the first option.
      const saved = this._readPersistedSelection();
      await this._loadOrgs();
      if (saved && saved.org && this.orgs.includes(saved.org)) {
        this.selectedOrg = saved.org;
        this.selectedSetId = saved.set_id || null;
        this.selectedKey = saved.key || null;
      } else if (!this.orgs.includes(this.selectedOrg)) {
        this.selectedOrg = this.orgs.includes('autonomy')
          ? 'autonomy'
          : (this.orgs[0] || 'autonomy');
      }
      await this._loadSetsForOrg(this.selectedOrg);
      if (this.selectedSetId) {
        await this._loadMembersForSet(this.selectedSetId);
        if (this.selectedKey) {
          this._refreshEditorFromMember();
        }
      }
    },

    // ── Computed-style getters ─────────────────────────────────
    get totalMembers() {
      return (this.sets[this.selectedOrg] || []).reduce(
        (sum, row) => sum + this._setRowCount(row), 0,
      );
    },

    get currentMember() {
      const list = this.members[this.selectedSetId] || [];
      return list.find(m => m.key === this.selectedKey) || null;
    },

    get diagWindowBlock() {
      return this.diagSummary && this.diagSummary[this.diagWindow]
        ? this.diagSummary[this.diagWindow]
        : null;
    },

    get diagSortedSets() {
      return this._sortDiagRows(this.diagSets);
    },

    get diagTopSets() {
      return this.diagSortedSets.slice(0, 8);
    },

    get currentDiagSet() {
      return this.diagSetDetail && this.diagSetDetail.set
        ? this.diagSetDetail.set
        : null;
    },

    // ── Tab state ──────────────────────────────────────────────
    async switchTab(tab) {
      this.activeTab = tab;
      if (tab === 'diagnostics') {
        await this.refreshDiagnostics();
      }
    },

    setDiagWindow(windowName) {
      this.diagWindow = windowName;
    },

    // ── Org picker ────────────────────────────────────────────
    async onOrgChange() {
      this.selectedSetId = null;
      this.selectedKey = null;
      this.editing = false;
      this.errorMessage = null;
      this.diagLoadedOrg = null;
      this.diagSets = [];
      this.diagSelectedSetId = null;
      this.diagSetDetail = null;
      this.diagError = null;
      this._persistSelection();
      await this._loadSetsForOrg(this.selectedOrg);
      if (this.activeTab === 'diagnostics') {
        await this.refreshDiagnostics();
      }
    },

    // ── Set selection ─────────────────────────────────────────
    async selectSet(setId) {
      this.selectedSetId = setId;
      this.selectedKey = null;
      this.editing = false;
      this.errorMessage = null;
      this._persistSelection();
      await this._loadMembersForSet(setId);
    },

    // ── Member selection ──────────────────────────────────────
    selectMember(key) {
      this.selectedKey = key;
      this.editing = false;
      this.errorMessage = null;
      this._refreshEditorFromMember();
      this._persistSelection();
    },

    toggleEdit() {
      this.editing = !this.editing;
      this.errorMessage = null;
      if (this.editing) {
        this._refreshEditorFromMember();
      }
    },

    _refreshEditorFromMember() {
      const member = this.currentMember;
      this.editorText = this.formatPayload(member && member.payload);
    },

    // ── Display helpers ───────────────────────────────────────
    formatPayload(payload) { return JSON.stringify(payload || {}, null, 2); },

    formatBytes(value) {
      const bytes = Number(value || 0);
      if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
      if (bytes < 1024) return bytes.toLocaleString() + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    },

    formatCount(value) {
      return Number(value || 0).toLocaleString();
    },

    formatTimestamp(value) {
      return value || 'n/a';
    },

    formatFloat(value, digits) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return 'n/a';
      }
      return Number(value).toFixed(digits);
    },

    stateClass(state) {
      return ({
        canonical: 'bg-green-950/60 text-green-300',
        published: 'bg-blue-950/60 text-blue-300',
        curated: 'bg-amber-950/60 text-amber-300',
        raw: 'bg-gray-800 text-gray-400',
      })[state] || 'bg-gray-800 text-gray-400';
    },

    diagActivityFor(row) {
      if (!row || !row.activity) return this._zeroActivity();
      return this.diagActivityForWindow(row, this.diagWindow);
    },

    diagActivityForWindow(row, windowName) {
      const block = row && row.activity && row.activity[windowName];
      if (!block) return this._zeroActivity();
      return {
        calls: Number(block.calls || 0),
        reads: Number(block.reads || 0),
        writes: Number(block.writes || 0),
        upserts: Number(block.upserts || 0),
      };
    },

    diagOperationsUpserts() {
      const block = this.diagWindowBlock;
      if (!block || !block.operations) return 0;
      return Number(block.operations.upsert_by_key || 0);
    },

    diagTopBarWidth(row) {
      const rows = this.diagTopSets;
      if (!rows.length) return '0%';
      const maxCalls = Math.max(...rows.map(item => this.diagActivityFor(item).calls), 1);
      const pct = (this.diagActivityFor(row).calls / maxCalls) * 100;
      return Math.max(Math.round(pct), 6) + '%';
    },

    // ── Save (edited payload) ─────────────────────────────────
    async save() {
      let parsed;
      try {
        parsed = JSON.parse(this.editorText);
      } catch (e) {
        this.errorMessage = 'Invalid JSON: ' + e.message;
        return;
      }
      const setId = this.selectedSetId;
      const key = this.selectedKey;
      if (!setId || !key) return;
      const member = this.currentMember;
      const body = {
        set_id: setId,
        schema_revision: (member && member.schema_revision) || DEFAULT_SCHEMA_REVISION,
        key: key,
        payload: parsed,
        state: (member && member.state) || 'raw',
      };
      const res = await this._postSetting(body, this.selectedOrg);
      if (!res.ok) {
        this.errorMessage = res.message || 'Save failed';
        return;
      }
      this.editing = false;
      this.errorMessage = null;
      // Re-fetch members so the row reflects the new payload + revision.
      await this._loadMembersForSet(setId);
      this._refreshEditorFromMember();
      this.diagLoadedOrg = null;
    },

    // ── One-click plugin toggle ───────────────────────────────
    async togglePlugin(member) {
      if (this.selectedSetId !== 'dashboard.plugin') return;
      const flipped = !(member.payload && member.payload.enabled);
      const body = {
        set_id: 'dashboard.plugin',
        schema_revision: member.schema_revision || DEFAULT_SCHEMA_REVISION,
        key: member.key,
        payload: { ...(member.payload || {}), enabled: flipped },
        state: member.state || 'raw',
      };
      const res = await this._postSetting(body, this.selectedOrg);
      if (!res.ok) {
        this.errorMessage = res.message || 'Toggle failed';
        return;
      }
      // Re-fetch members so the toggle button re-renders against the
      // new payload, then refresh the sidebar plugin list so the
      // toggled plugin's nav-link appears or disappears immediately.
      await this._loadMembersForSet('dashboard.plugin');
      if (window.Autonomy && typeof window.Autonomy.refreshPlugins === 'function') {
        await window.Autonomy.refreshPlugins();
      }
      this.diagLoadedOrg = null;
    },

    // ── Diagnostics loading ───────────────────────────────────
    async refreshDiagnostics() {
      if (this.diagLoading && this.diagLoadedOrg === this.selectedOrg) return;
      this.diagLoading = true;
      this.diagError = null;
      try {
        const [summaryRes, mediatorRes, setsRes] = await Promise.all([
          this._fetchAsOrg('/api/diag/settings', this.selectedOrg),
          this._fetchAsOrg('/api/diag/settings_mediator', this.selectedOrg),
          this._fetchAsOrg('/api/diag/settings/sets', this.selectedOrg),
        ]);
        if (!summaryRes.ok || !mediatorRes.ok || !setsRes.ok) {
          throw new Error(
            'Diagnostics request failed (' +
            [summaryRes.status, mediatorRes.status, setsRes.status].join(', ') + ')',
          );
        }
        const [summary, mediator, setsPayload] = await Promise.all([
          summaryRes.json(),
          mediatorRes.json(),
          setsRes.json(),
        ]);
        const rows = setsPayload.sets || [];
        const availableWindows = setsPayload.windows || ['totals', 'last_10s', 'last_60s'];
        this.diagSummary = summary;
        this.diagMediator = mediator;
        this.diagSets = rows;
        this.diagLoadedOrg = this.selectedOrg;
        this.diagWindow = availableWindows.includes(this.diagWindow)
          ? this.diagWindow
          : (availableWindows.includes('last_60s') ? 'last_60s' : availableWindows[0]);
        const sortedRows = this._sortDiagRows(rows);
        const stillSelected = rows.some(row => row.set_id === this.diagSelectedSetId);
        this.diagSelectedSetId = stillSelected
          ? this.diagSelectedSetId
          : (sortedRows[0] ? sortedRows[0].set_id : null);
        if (this.diagSelectedSetId) {
          await this.loadDiagSetDetail(this.diagSelectedSetId);
        } else {
          this.diagSetDetail = null;
        }
      } catch (e) {
        this.diagError = e && e.message ? e.message : String(e);
        this.diagSetDetail = null;
      } finally {
        this.diagLoading = false;
      }
    },

    async loadDiagSetDetail(setId) {
      this.diagSelectedSetId = setId;
      try {
        const res = await this._fetchAsOrg(
          '/api/diag/settings/sets/' + encodeURIComponent(setId),
          this.selectedOrg,
        );
        if (!res.ok) {
          this.diagSetDetail = null;
          return;
        }
        this.diagSetDetail = await res.json();
      } catch (_e) {
        this.diagSetDetail = null;
      }
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
        // Always make autonomy available even if the orgs endpoint is empty.
        this.orgs = slugs.length ? slugs : ['autonomy'];
      } catch (_e) {
        this.orgs = ['autonomy'];
      } finally {
        this.loadingOrgs = false;
      }
    },

    async _loadSetsForOrg(org) {
      this.loadingSets = true;
      try {
        const res = await this._fetchAsOrg('/api/graph/sets?summary=1', org);
        if (!res.ok) { this.sets = { ...this.sets, [org]: [] }; return; }
        const data = await res.json();
        let rows = [];
        if (Array.isArray(data.sets) && data.sets.length) {
          rows = data.sets.map(row => ({
            ...row,
            count: this._setRowCount(row),
          }));
        } else {
          const ids = data.set_ids || [];
          rows = ids.map(id => ({ set_id: id, count: 0, member_count: 0 }));
        }
        this.sets = { ...this.sets, [org]: rows };
      } catch (_e) {
        this.sets = { ...this.sets, [org]: [] };
      } finally {
        this.loadingSets = false;
      }
    },

    async _loadMembersForSet(setId) {
      this.loadingMembers = true;
      try {
        const res = await this._fetchAsOrg(
          '/api/graph/settings/' + encodeURIComponent(setId),
          this.selectedOrg,
        );
        if (!res.ok) { this.members = { ...this.members, [setId]: [] }; return; }
        const data = await res.json();
        const list = data.members || [];
        this.members = { ...this.members, [setId]: list };
        // Update the count in the left rail for this set.
        const orgRows = (this.sets[this.selectedOrg] || []).map(row =>
          row.set_id === setId
            ? { ...row, count: list.length, member_count: list.length }
            : row,
        );
        this.sets = { ...this.sets, [this.selectedOrg]: orgRows };
      } catch (_e) {
        this.members = { ...this.members, [setId]: [] };
      } finally {
        this.loadingMembers = false;
      }
    },

    // ── Cross-org fetch helper ────────────────────────────────
    // The substrate's ``Autonomy.fetch`` stamps the plugin's effective
    // org (autonomy) onto every request, overwriting any explicit
    // X-Graph-Org we pass, so it can't be used for picker-driven
    // cross-org reads. We call ``fetch`` directly with the header the
    // picker dictates; non-picker reads (``/api/orgs``) still go
    // through ``Autonomy.fetch`` so they pick up the substrate default.
    _fetchAsOrg(path, org) {
      return fetch(path, {
        headers: { 'X-Graph-Org': org },
      });
    },

    async _postSetting(body, org) {
      try {
        const res = await fetch('/api/graph/setting', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Graph-Org': org,
          },
          body: JSON.stringify(body),
        });
        if (res.ok) return { ok: true };
        let message = 'Server returned ' + res.status;
        try {
          const err = await res.json();
          if (err && err.error) {
            message = err.error;
            if (err.detail) message += ': ' + err.detail;
          }
        } catch (_) { /* keep status-only message */ }
        return { ok: false, message };
      } catch (e) {
        return { ok: false, message: 'Network error: ' + (e.message || e) };
      }
    },

    // ── localStorage persistence ──────────────────────────────
    _persistSelection() {
      try {
        localStorage.setItem(SETTINGS_LS_KEY, JSON.stringify({
          org: this.selectedOrg,
          set_id: this.selectedSetId,
          key: this.selectedKey,
        }));
      } catch (_e) { /* storage disabled — selection is just session-local */ }
    },

    _readPersistedSelection() {
      try {
        const raw = localStorage.getItem(SETTINGS_LS_KEY);
        if (!raw) return null;
        const saved = JSON.parse(raw);
        if (!saved || typeof saved !== 'object') return null;
        return {
          org: typeof saved.org === 'string' ? saved.org : null,
          set_id: typeof saved.set_id === 'string' ? saved.set_id : null,
          key: typeof saved.key === 'string' ? saved.key : null,
        };
      } catch (_e) {
        return null;
      }
    },

    _setRowCount(row) {
      return Number(
        row && row.count !== undefined
          ? row.count
          : (row && row.member_count !== undefined ? row.member_count : 0),
      ) || 0;
    },

    _zeroActivity() {
      return { calls: 0, reads: 0, writes: 0, upserts: 0 };
    },

    _sortDiagRows(rows) {
      return [...(rows || [])].sort((left, right) => {
        const leftActivity = this.diagActivityFor(left);
        const rightActivity = this.diagActivityFor(right);
        return (
          rightActivity.calls - leftActivity.calls
          || rightActivity.upserts - leftActivity.upserts
          || rightActivity.writes - leftActivity.writes
          || this._setRowCount(right) - this._setRowCount(left)
          || String(left.set_id || '').localeCompare(String(right.set_id || ''))
        );
      });
    },
  };
}
