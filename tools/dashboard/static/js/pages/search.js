(function () {
  // Maps server `source_type` → display label, accent-rail class, type-pill class.
  // The keys mirror the design CSS variables (--note, --session, etc.).
  var TYPE_LABELS = {
    note: 'Note',
    session: 'Session',
    'agent-run': 'Agent run',
    conversation: 'Conversation',
    docs: 'Docs',
    status: 'Status',
    musing: 'Musing',
  };

  // Chip rail order — fixed left-to-right after the All chip.
  var CHIP_ORDER = ['note', 'session', 'agent-run', 'docs', 'conversation', 'status', 'musing'];
  var CHIP_LABELS = {
    note: 'Notes',
    session: 'Sessions',
    'agent-run': 'Agent runs',
    docs: 'Docs',
    conversation: 'Conversations',
    status: 'Status',
    musing: 'Musings',
  };

  // Publication-state dropdown options. The selection is a *minimum-state*
  // filter — picking a state clamps results to that state OR more
  // restrictive. Order is least → most restrictive (Raw is the floor;
  // Canonical the ceiling). ``bars`` drives the option's ladder glyph
  // (1..4 filled bars). ``hint`` is the trailing italic restrictiveness
  // tag ("any" / "+" / "strict") shown next to each option.
  //
  // Wire mapping (see _refetch below):
  //   raw       → ?include_raw=1            (no states= — clears the
  //                                          hidden "exclude raw rows
  //                                          from other sessions" filter
  //                                          baked into db.py defaults)
  //   curated   → ?states=curated,published,canonical
  //   published → ?states=published,canonical
  //   canonical → ?states=canonical
  var STATE_OPTIONS = [
    { key: 'raw',       label: 'Raw',       hint: 'any',    bars: 1, states: null,                                  includeRaw: true  },
    { key: 'curated',   label: 'Curated',   hint: '+',      bars: 2, states: ['curated', 'published', 'canonical'], includeRaw: false },
    { key: 'published', label: 'Published', hint: '+',      bars: 3, states: ['published', 'canonical'],            includeRaw: false },
    { key: 'canonical', label: 'Canonical', hint: 'strict', bars: 4, states: ['canonical'],                         includeRaw: false },
  ];

  var DEFAULT_STATE_KEY = 'raw';

  // Debounce window for global-search input → refetch on /search. Matches
  // the brief: "300ms".
  var GLOBAL_INPUT_DEBOUNCE_MS = 300;

  document.addEventListener('alpine:init', () => {
    Alpine.data('searchPage', () => ({
      query: '',
      results: [],
      loaded: false,
      activeType: 'all',
      // Org pin (mirrors the ?org= URL param; '' means "All orgs").
      selectedOrg: '',
      orgList: [],
      orgDropdownOpen: false,
      // Publication-state chip — mirrors ?state= URL param. Default is
      // ``raw`` (the floor; equivalent to the old "Any"). Non-recognised
      // values fall back to the default.
      selectedState: DEFAULT_STATE_KEY,
      stateDropdownOpen: false,
      stateOptions: STATE_OPTIONS,
      _refetchTimer: null,

      init() {
        var params = new URLSearchParams(window.location.search);
        this.query = params.get('q') || '';
        // Accept ?org= (canonical) or ?only_org= (legacy URL form). Note:
        // the org chip now sends X-Graph-Org instead of ?only_org= on the
        // wire — see _refetch() below — but we still parse legacy URLs so
        // bookmarks keep working.
        this.selectedOrg = params.get('org') || params.get('only_org') || '';
        var stateParam = params.get('state') || '';
        var match = STATE_OPTIONS.find(o => o.key === stateParam);
        this.selectedState = match ? match.key : DEFAULT_STATE_KEY;
        // Sync the global header input with our query so it isn't blank
        // when the page lands via deep link.
        this._syncGlobalInput();
        // Populate org list for the dropdown (best-effort; chip still works
        // with an empty list — only "All orgs" is selectable).
        fetch('/api/orgs')
          .then(r => r.ok ? r.json() : { orgs: [] })
          .then(d => { this.orgList = this._normalizeOrgs(d && d.orgs || []); })
          .catch(() => { this.orgList = []; });
        if (!this.query) { this.loaded = true; return; }
        this._refetch();
      },

      // ── Org chip + dropdown ────────────────────────────────────────
      _normalizeOrgs(raw) {
        // /api/orgs returns ``{orgs: [{org: {slug, type}, identity_resolved: {...}}]}``.
        // Flatten to ``{slug, name, color, favicon, initial, kind}`` for the
        // dropdown. ``kind`` (= bootstrap row's ``type``) labels the option
        // as "own" / "peer" / "shared" so the operator sees the relationship.
        return (raw || []).map(e => {
          var org = (e && e.org) || {};
          var ident = (e && e.identity_resolved) || {};
          var slug = org.slug || ident.slug || '';
          return {
            slug: slug,
            name: ident.name || slug,
            color: ident.color || '#6c63ff',
            favicon: ident.favicon || null,
            initial: ident.initial || (slug ? slug[0].toUpperCase() : '?'),
            kind: org.type || '',
          };
        }).filter(o => o.slug);
      },

      get orgChip() {
        if (!this.selectedOrg) {
          return {
            label: 'All', allOrgs: true, glyphStyle: '',
            initial: '∞', favicon: null,
            title: 'Pin search to an organization (sets caller_org for the request)',
          };
        }
        var picked = (this.orgList || []).find(o => o.slug === this.selectedOrg);
        if (!picked) {
          return {
            label: this.selectedOrg, allOrgs: false,
            glyphStyle: 'background:#6c63ff', initial: this.selectedOrg[0].toUpperCase(),
            favicon: null,
            title: 'Search caller pinned to ' + this.selectedOrg,
          };
        }
        return {
          label: picked.name, allOrgs: false,
          glyphStyle: picked.favicon ? '' : ('background:' + picked.color),
          initial: picked.initial, favicon: picked.favicon,
          title: 'Search caller pinned to ' + picked.name,
        };
      },

      toggleOrgDropdown() {
        this.stateDropdownOpen = false;
        this.orgDropdownOpen = !this.orgDropdownOpen;
      },

      pickOrg(slug) {
        this.orgDropdownOpen = false;
        if ((slug || '') === (this.selectedOrg || '')) return;
        this.selectedOrg = slug || '';
        this._writeUrl();
        if (this.query) this._refetch();
      },

      // ── Publication-state chip + dropdown ──────────────────────────
      _stateOption(key) {
        return STATE_OPTIONS.find(o => o.key === (key || '')) ||
               STATE_OPTIONS.find(o => o.key === DEFAULT_STATE_KEY);
      },

      get stateChipLabel() {
        var opt = this._stateOption(this.selectedState);
        // Echo "Any" hint inline on Raw so the chip's label communicates
        // its semantic ("Raw = floor / no filter") at a glance.
        if (opt.key === 'raw') return 'Raw (Any)';
        return opt.label;
      },

      get stateChipFilledBars() {
        return this._stateOption(this.selectedState).bars;
      },

      toggleStateDropdown() {
        this.orgDropdownOpen = false;
        this.stateDropdownOpen = !this.stateDropdownOpen;
      },

      pickState(key) {
        this.stateDropdownOpen = false;
        var resolved = this._stateOption(key).key;
        if (resolved === this.selectedState) return;
        this.selectedState = resolved;
        this._writeUrl();
        if (this.query) this._refetch();
      },

      // ── Global header input bridge ─────────────────────────────────
      _syncGlobalInput() {
        // Reflect the page's query into the top-bar input so the operator
        // sees the live query string while on /search.
        var gs = document.getElementById('global-search');
        if (gs && gs.value !== this.query) gs.value = this.query;
      },

      onGlobalSearchInput(ev) {
        // Fired by app.js on every input event of #global-search while the
        // /search route is active. Two-way bind to ``query`` and refetch
        // (debounced) so results update live as the operator types.
        var raw = (ev && ev.detail && typeof ev.detail.value === 'string')
          ? ev.detail.value : '';
        this.query = raw;
        // Update URL via replaceState so the q= reflects the live query
        // without spawning a navigation entry per keystroke.
        this._writeUrl();
        if (this._refetchTimer) clearTimeout(this._refetchTimer);
        var self = this;
        this._refetchTimer = setTimeout(function () {
          self._refetchTimer = null;
          if (!self.query) {
            self.results = [];
            self.loaded = true;
            return;
          }
          self._refetch();
        }, GLOBAL_INPUT_DEBOUNCE_MS);
      },

      onGlobalSearchEnter(ev) {
        // Enter pressed in #global-search while on /search → flush any
        // pending debounced refetch immediately (no extra navigation).
        if (this._refetchTimer) {
          clearTimeout(this._refetchTimer);
          this._refetchTimer = null;
        }
        var raw = (ev && ev.detail && typeof ev.detail.value === 'string')
          ? ev.detail.value : this.query;
        this.query = raw;
        this._writeUrl();
        if (this.query) this._refetch();
      },

      _writeUrl() {
        var url = new URL(window.location.href);
        // Drop legacy ?only_org= so we don't double-write it.
        url.searchParams.delete('only_org');
        if (this.query) url.searchParams.set('q', this.query);
        else url.searchParams.delete('q');
        if (this.selectedOrg) url.searchParams.set('org', this.selectedOrg);
        else url.searchParams.delete('org');
        if (this.selectedState && this.selectedState !== DEFAULT_STATE_KEY) {
          url.searchParams.set('state', this.selectedState);
        } else {
          url.searchParams.delete('state');
        }
        window.history.replaceState({}, '', url.toString());
      },

      _refetch() {
        this.loaded = false;
        // Org chip = caller_org. Sent as ``X-Graph-Org`` header — NOT as
        // ``?only_org=`` (which means "show me this org's PEER-VIEW
        // surface", a different intent kept around for explicit audit
        // calls). With caller=autonomy, /api/search returns the full
        // autonomy surface (raw + published + canonical + curated) — what
        // the operator expects when they pin "Autonomy".
        var url = '/api/search?q=' + encodeURIComponent(this.query) +
                  '&group=1&limit=50';
        // Minimum-state filter mapping (see STATE_OPTIONS):
        //   raw       → ?include_raw=1   (NO ?states= — the API's default
        //                                 hidden filter excludes raw rows
        //                                 from other sessions; include_raw
        //                                 clears that filter so Raw really
        //                                 means "any state, anywhere")
        //   curated   → ?states=curated,published,canonical
        //   published → ?states=published,canonical
        //   canonical → ?states=canonical
        var opt = this._stateOption(this.selectedState);
        if (opt.includeRaw) {
          url += '&include_raw=1';
        } else if (opt.states && opt.states.length) {
          url += '&states=' + encodeURIComponent(opt.states.join(','));
        }
        var headers = {};
        if (this.selectedOrg) headers['X-Graph-Org'] = this.selectedOrg;
        fetch(url, { headers: headers })
          .then(r => r.json())
          .then(d => {
            this.results = Array.isArray(d) ? d : (d.results || []);
            this.loaded = true;
          })
          .catch(() => { this.loaded = true; });
      },

      peerPillTitle(r) {
        var orgName = r && r.org && r.org.name ? r.org.name : 'this org';
        return 'Public surface of ' + orgName + ' — published or canonical only';
      },

      // ── chip filter ────────────────────────────────────────────────
      get chipTypes() {
        // Build counts from the result set in canonical order, but only
        // include chips for types actually present (plus the always-on
        // canonical CHIP_ORDER members so the rail looks like the design).
        var counts = {};
        for (var i = 0; i < this.results.length; i++) {
          var t = this.results[i].source_type || 'unknown';
          counts[t] = (counts[t] || 0) + 1;
        }
        var out = [];
        var seen = {};
        for (var j = 0; j < CHIP_ORDER.length; j++) {
          var key = CHIP_ORDER[j];
          out.push({ key: key, label: CHIP_LABELS[key], count: counts[key] || 0 });
          seen[key] = true;
        }
        // Append any other source_types we saw that aren't in CHIP_ORDER.
        Object.keys(counts).forEach(k => {
          if (!seen[k]) {
            out.push({ key: k, label: this._titleCase(k), count: counts[k] });
          }
        });
        return out;
      },

      get filteredResults() {
        if (this.activeType === 'all') return this.results;
        return this.results.filter(r => (r.source_type || 'unknown') === this.activeType);
      },

      get totalMatches() {
        var total = 0;
        for (var i = 0; i < this.filteredResults.length; i++) {
          total += (this.filteredResults[i].match_count || 1);
        }
        return total;
      },

      setType(t) { this.activeType = t; },

      // ── rendering helpers ──────────────────────────────────────────
      typeLabel(t) { return TYPE_LABELS[t] || (t ? this._titleCase(t) : 'Note'); },

      railClass(t) {
        if (!t) return 'sp-rail-default';
        return 'sp-rail-' + t;
      },

      pillClass(t) {
        if (!t) return 'sp-pill-default';
        var known = ['note', 'session', 'agent-run', 'conversation', 'docs', 'status', 'musing'];
        return known.indexOf(t) >= 0 ? 'sp-pill-' + t : 'sp-pill-default';
      },

      cardExcerpts(r) {
        // Grouped server response carries `excerpts: [{turn_number, content, ...}]`.
        // Legacy/single-row entries fall back to a synthetic excerpt built from
        // the row's own fields so the same renderer works for both shapes.
        if (Array.isArray(r.excerpts) && r.excerpts.length > 0) return r.excerpts;
        return [{
          turn_number: r.turn_number,
          content: r.content || '',
          result_type: r.result_type,
        }];
      },

      highlightTerms(text, q) {
        if (!q || !text) return '';
        var terms = q.split(/\s+/).filter(t => t.length > 2);
        var escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
        var snip = text.length > 240 ? text.slice(0, 240) + '…' : text;
        var safe = this._esc(snip);
        if (!escaped.length) return safe;
        var re = new RegExp('(' + escaped.join('|') + ')', 'gi');
        return safe.replace(re, '<mark>$1</mark>');
      },

      _esc(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
      },

      _titleCase(s) {
        return s.split(/[-_]/).map(p => p.charAt(0).toUpperCase() + p.slice(1)).join(' ');
      },
    }));
  });
})();
