// Settings UI plugin — frontend Alpine factory.
//
// Reads + writes Setting rows across orgs via the existing
// /api/orgs, /api/graph/sets, /api/graph/settings/<set_id>, and
// POST /api/graph/setting endpoints. The org picker is per-fetch
// override of X-Graph-Org so the page can show data from any org
// while the substrate's plugin-org stamping (autonomy) remains the
// default for everything else.
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

    // ── Loading / error gating ─────────────────────────────────
    loadingOrgs: false,
    loadingSets: false,
    loadingMembers: false,

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
        (s, r) => s + (r.count || 0), 0,
      );
    },

    get currentMember() {
      const list = this.members[this.selectedSetId] || [];
      return list.find(m => m.key === this.selectedKey) || null;
    },

    // ── Org picker ────────────────────────────────────────────
    async onOrgChange() {
      this.selectedSetId = null;
      this.selectedKey = null;
      this.editing = false;
      this.errorMessage = null;
      this._persistSelection();
      await this._loadSetsForOrg(this.selectedOrg);
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
      const m = this.currentMember;
      this.editorText = this.formatPayload(m && m.payload);
    },

    // ── Display helpers ───────────────────────────────────────
    formatPayload(p) { return JSON.stringify(p || {}, null, 2); },

    stateClass(s) {
      return ({
        canonical: 'bg-green-950/60 text-green-300',
        published: 'bg-blue-950/60 text-blue-300',
        curated:   'bg-amber-950/60 text-amber-300',
        raw:       'bg-gray-800 text-gray-400',
      })[s] || 'bg-gray-800 text-gray-400';
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
      const m = this.currentMember;
      const body = {
        set_id: setId,
        schema_revision: (m && m.schema_revision) || DEFAULT_SCHEMA_REVISION,
        key: key,
        payload: parsed,
        state: (m && m.state) || 'raw',
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
    },

    // ── One-click plugin toggle ───────────────────────────────
    async togglePlugin(m) {
      if (this.selectedSetId !== 'dashboard.plugin') return;
      const flipped = !(m.payload && m.payload.enabled);
      const body = {
        set_id: 'dashboard.plugin',
        schema_revision: m.schema_revision || DEFAULT_SCHEMA_REVISION,
        key: m.key,
        payload: { ...(m.payload || {}), enabled: flipped },
        state: m.state || 'raw',
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
    },

    // ── Data loading ──────────────────────────────────────────
    async _loadOrgs() {
      this.loadingOrgs = true;
      try {
        const res = await window.Autonomy.fetch('/api/orgs');
        if (!res.ok) { this.orgs = ['autonomy']; return; }
        const data = await res.json();
        const slugs = (data.orgs || [])
          .map(e => (e && e.org && e.org.slug) || e.slug)
          .filter(Boolean);
        // Always make autonomy available even if the orgs endpoint is empty.
        this.orgs = slugs.length ? slugs : ['autonomy'];
      } catch (e) {
        this.orgs = ['autonomy'];
      } finally {
        this.loadingOrgs = false;
      }
    },

    async _loadSetsForOrg(org) {
      this.loadingSets = true;
      try {
        const res = await this._fetchAsOrg('/api/graph/sets', org);
        if (!res.ok) { this.sets = { ...this.sets, [org]: [] }; return; }
        const data = await res.json();
        const ids = data.set_ids || [];
        const rows = ids.map(id => ({ set_id: id, count: 0 }));
        this.sets = { ...this.sets, [org]: rows };
      } catch (e) {
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
        const orgRows = (this.sets[this.selectedOrg] || []).map(r =>
          r.set_id === setId ? { ...r, count: list.length } : r,
        );
        this.sets = { ...this.sets, [this.selectedOrg]: orgRows };
      } catch (e) {
        this.members = { ...this.members, [setId]: [] };
      } finally {
        this.loadingMembers = false;
      }
    },

    // ── Cross-org fetch helper ────────────────────────────────
    // The substrate's ``Autonomy.fetch`` stamps the plugin's effective
    // org (autonomy) onto every request, *overwriting* any explicit
    // X-Graph-Org we pass — so it can't be used for picker-driven
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
      } catch (e) { /* storage disabled — selection is just session-local */ }
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
      } catch (e) {
        return null;
      }
    },
  };
}
