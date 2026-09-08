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

  // Round 7k pill rework. The two row-level chips that used to discriminate
  // by ``source_type`` (Sessions vs "Agent runs") now read from
  // ``metadata.session_type`` instead. ``source.type='agent-run'`` no
  // longer drives a pill — those 87 subagent-trace rows can still be
  // searched but lose their dedicated chip, which was confusing because
  // it mixed traces with dispatch runs.
  //
  //   Sessions → metadata.session_type IN ('terminal','chatwith')
  //   Dispatch → metadata.session_type IN ('dispatch','librarian','agentic')
  //
  // Strict NULL semantics: rows whose session_type is null/missing match
  // NEITHER pill. ~615 such rows exist in autonomy.db (separate
  // data-hygiene bead) — they remain visible under "All".
  var SESSION_TYPES_INTERACTIVE = ['terminal', 'chatwith'];
  var SESSION_TYPES_DISPATCH = ['dispatch', 'librarian', 'agentic'];

  // Chip rail order — fixed left-to-right after the All chip. ``session``
  // and ``dispatch`` are now session_type-derived virtual categories,
  // not source_type values. The remaining keys still map directly to
  // ``source_type`` (note/docs/conversation/status/musing).
  var CHIP_ORDER = ['note', 'session', 'dispatch', 'docs', 'conversation', 'status', 'musing'];
  var CHIP_LABELS = {
    note: 'Notes',
    session: 'Sessions',
    dispatch: 'Dispatch',
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

  // Sort chip — toggles between Relevance (BM25) and Recent (created_at
  // DESC). URL is the source of truth: ?order=recent. Default is
  // Relevance (no ?order= written).
  var ORDER_OPTIONS = [
    { key: 'relevance', label: 'Relevance', hint: 'BM25' },
    { key: 'recent',    label: 'Recent',    hint: 'newest first' },
  ];
  var DEFAULT_ORDER_KEY = 'relevance';

  // Ranking lens — only meaningful when order=relevance. Legacy is the
  // production default; Smart is the opt-in whole-query + per-term RRF
  // experiment. URL state makes comparisons bookmarkable and reversible.
  var RANKER_OPTIONS = [
    {
      key: 'legacy', label: 'Legacy', hint: 'default',
      description: 'BM25 with title, tag, and hit-count signals.',
    },
    {
      key: 'smart', label: 'Smart', hint: 'experimental',
      description: 'Fuses the whole query with one ranking per term.',
    },
  ];
  var DEFAULT_RANKER_KEY = 'legacy';

  // Debounce window for global-search input → refetch on /search. Matches
  // the brief: "300ms".
  var GLOBAL_INPUT_DEBOUNCE_MS = 300;

  // Map a row's ``source_type`` + ``session_type`` to the pill key used
  // by the chip rail. Round 7k strict semantics:
  //   * session_type IN ('terminal','chatwith') → 'session' (interactive)
  //   * session_type IN ('dispatch','librarian','agentic') → 'dispatch'
  //   * Session-shaped source_types (session/agent-run/agentic) WITHOUT
  //     a metadata.session_type are bucketed as 'unknown' — invisible
  //     to both pills (the strict NULL contract) but still visible
  //     under "All". Pinned this way so the data-hygiene bead for the
  //     ~615 NULL-session_type rows in autonomy.db can land
  //     independently without changing pill behaviour.
  //   * Everything else falls back to source_type (note/docs/etc.).
  function rowChipKey(r) {
    var st = (r && (r.session_type || _metaSessionType(r))) || null;
    if (SESSION_TYPES_INTERACTIVE.indexOf(st) >= 0) return 'session';
    if (SESSION_TYPES_DISPATCH.indexOf(st) >= 0) return 'dispatch';
    var srcType = r && r.source_type;
    if (srcType === 'session' || srcType === 'agent-run' ||
        srcType === 'agentic') {
      return 'unknown';
    }
    return srcType || 'unknown';
  }

  // Pull session_type out of a row's source_metadata (JSON string or dict).
  function _metaSessionType(r) {
    if (!r) return null;
    var meta = r.source_metadata;
    if (typeof meta === 'string') {
      try { meta = JSON.parse(meta); } catch (_) { return null; }
    }
    if (meta && typeof meta === 'object' && typeof meta.session_type === 'string') {
      return meta.session_type;
    }
    return null;
  }

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
      // Sort chip — mirrors ?order= URL param. Default is Relevance.
      selectedOrder: DEFAULT_ORDER_KEY,
      orderDropdownOpen: false,
      orderOptions: ORDER_OPTIONS,
      // Ranking lens — mirrors ?ranker=smart. Hidden under Recent because
      // relevance rankers do not affect chronological ordering.
      selectedRanker: DEFAULT_RANKER_KEY,
      rankerDropdownOpen: false,
      rankerOptions: RANKER_OPTIONS,
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
        // Sort chip — ?order=recent flips to recency. Anything else
        // (missing, typo, legacy ?order=relevance) lands on the default.
        var orderParam = params.get('order') || '';
        var orderMatch = ORDER_OPTIONS.find(o => o.key === orderParam);
        this.selectedOrder = orderMatch ? orderMatch.key : DEFAULT_ORDER_KEY;
        var rankerParam = params.get('ranker') || '';
        var rankerMatch = RANKER_OPTIONS.find(o => o.key === rankerParam);
        this.selectedRanker = rankerMatch ? rankerMatch.key : DEFAULT_RANKER_KEY;
        // Sync the global header input with our query so it isn't blank
        // when the page lands via deep link.
        this._syncGlobalInput();
        // Populate identities for the shared picker; an empty inventory leaves
        // the trigger disabled instead of inventing a selectable organization.
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
        this.orderDropdownOpen = false;
        this.rankerDropdownOpen = false;
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
        this.orderDropdownOpen = false;
        this.rankerDropdownOpen = false;
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

      // ── Sort chip + dropdown ───────────────────────────────────────
      _orderOption(key) {
        return ORDER_OPTIONS.find(o => o.key === (key || '')) ||
               ORDER_OPTIONS.find(o => o.key === DEFAULT_ORDER_KEY);
      },

      get orderChipLabel() {
        return this._orderOption(this.selectedOrder).label;
      },

      toggleOrderDropdown() {
        this.orgDropdownOpen = false;
        this.stateDropdownOpen = false;
        this.rankerDropdownOpen = false;
        this.orderDropdownOpen = !this.orderDropdownOpen;
      },

      pickOrder(key) {
        this.orderDropdownOpen = false;
        this.rankerDropdownOpen = false;
        var resolved = this._orderOption(key).key;
        if (resolved === this.selectedOrder) return;
        this.selectedOrder = resolved;
        this._writeUrl();
        if (this.query) this._refetch();
      },

      // ── Ranking lens chip + dropdown ──────────────────────────────
      _rankerOption(key) {
        return RANKER_OPTIONS.find(o => o.key === (key || '')) ||
               RANKER_OPTIONS.find(o => o.key === DEFAULT_RANKER_KEY);
      },

      get rankerChipLabel() {
        return this._rankerOption(this.selectedRanker).label;
      },

      toggleRankerDropdown() {
        this.orgDropdownOpen = false;
        this.stateDropdownOpen = false;
        this.orderDropdownOpen = false;
        this.rankerDropdownOpen = !this.rankerDropdownOpen;
      },

      pickRanker(key) {
        this.rankerDropdownOpen = false;
        var resolved = this._rankerOption(key).key;
        if (resolved === this.selectedRanker) return;
        this.selectedRanker = resolved;
        this._writeUrl();
        if (this.query && this.selectedOrder === 'relevance') this._refetch();
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
        if (this.selectedOrder && this.selectedOrder !== DEFAULT_ORDER_KEY) {
          url.searchParams.set('order', this.selectedOrder);
        } else {
          url.searchParams.delete('order');
        }
        if (this.selectedRanker && this.selectedRanker !== DEFAULT_RANKER_KEY) {
          url.searchParams.set('ranker', this.selectedRanker);
        } else {
          url.searchParams.delete('ranker');
        }
        window.history.replaceState({}, '', url.toString());
      },

      // Keep the pinned caller org attached when a result opens. Search
      // sends the org as X-Graph-Org, but SPA navigation itself carries no
      // request headers; the source page reads this query param and restores
      // the same header for its /api/graph request. Without this handoff, a
      // raw result from a non-default org renders in search and then 404s as
      // soon as it is opened.
      sourceHref(r, turnNumber) {
        var sourceId = (r && (r.source_id || r.id)) || '';
        var path = '/graph/' + sourceId.slice(0, 12);
        var params = new URLSearchParams();
        if (turnNumber != null) params.set('turn', String(turnNumber));
        if (this.selectedOrg) params.set('org', this.selectedOrg);
        var query = params.toString();
        return path + (query ? '?' + query : '');
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
        if (this.selectedOrder && this.selectedOrder !== DEFAULT_ORDER_KEY) {
          url += '&order=' + encodeURIComponent(this.selectedOrder);
        }
        // Ranker is omitted for the default and under Recent. Keeping the
        // preference in the page URL means switching back to Relevance
        // restores the selected comparison lens without sending a no-op knob.
        if (this.selectedOrder === 'relevance' &&
            this.selectedRanker !== DEFAULT_RANKER_KEY) {
          url += '&ranker=' + encodeURIComponent(this.selectedRanker);
        }
        // Round 7l: pill click re-fetches with the session_type filter
        // pushed to the API (not just a client-side filter over
        // ``this.results``). Sessions / Dispatch are the only chips
        // that map to a server-side ``session_type`` — other chips
        // (Notes, Docs, etc.) are still narrowed client-side via
        // ``filteredResults``, since source_type filtering is not (yet)
        // a /api/search knob.
        var stFilter = this._sessionTypeFilterFor(this.activeType);
        if (stFilter) {
          url += '&session_type=' + encodeURIComponent(stFilter.join(','));
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

      // Map an active chip key to the session_type list to push to the
      // API. Returns null when the chip doesn't map to a session_type
      // filter (Notes, Docs, etc., or the 'all' clear-filter chip).
      _sessionTypeFilterFor(chipKey) {
        if (chipKey === 'session') return SESSION_TYPES_INTERACTIVE;
        if (chipKey === 'dispatch') return SESSION_TYPES_DISPATCH;
        return null;
      },

      peerPillTitle(r) {
        var orgName = r && r.org && r.org.name ? r.org.name : 'this org';
        return 'Public surface of ' + orgName + ' — published or canonical only';
      },

      // ── chip filter ────────────────────────────────────────────────
      // Surface session_type for renderers + chip filtering. Reads first
      // from the row's top-level field, then from source_metadata JSON.
      rowSessionType(r) {
        if (!r) return null;
        if (typeof r.session_type === 'string') return r.session_type;
        return _metaSessionType(r);
      },

      // The pill key for a row — drives both chipTypes counts and the
      // active-pill filter. Round 7k: session_type-driven categories
      // override raw source_type for sessions / dispatch.
      rowChipKey(r) { return rowChipKey(r); },

      get chipTypes() {
        // Build counts from the result set in canonical order, but only
        // include chips for keys actually present (plus the always-on
        // canonical CHIP_ORDER members so the rail looks like the design).
        var counts = {};
        for (var i = 0; i < this.results.length; i++) {
          var key = rowChipKey(this.results[i]);
          counts[key] = (counts[key] || 0) + 1;
        }
        var out = [];
        var seen = {};
        for (var j = 0; j < CHIP_ORDER.length; j++) {
          var key = CHIP_ORDER[j];
          out.push({ key: key, label: CHIP_LABELS[key], count: counts[key] || 0 });
          seen[key] = true;
        }
        // Append any other chip keys we saw that aren't in CHIP_ORDER
        // (e.g. legacy 'agent-run' rows still surface as their own bucket
        // under "All", but only if present — no permanent chip).
        Object.keys(counts).forEach(k => {
          if (!seen[k] && k !== 'unknown') {
            out.push({ key: k, label: this._titleCase(k), count: counts[k] });
          }
        });
        return out;
      },

      get filteredResults() {
        if (this.activeType === 'all') return this.results;
        return this.results.filter(r => rowChipKey(r) === this.activeType);
      },

      get totalMatches() {
        var total = 0;
        for (var i = 0; i < this.filteredResults.length; i++) {
          total += (this.filteredResults[i].match_count || 1);
        }
        return total;
      },

      setType(t) {
        // Round 7l: chip click re-fetches when the chip maps to a
        // server-side filter (Sessions / Dispatch / All). Without this,
        // a global LIMIT-N query that trimmed away all rows of the
        // clicked type would render the chip as an empty filter even
        // though more matches exist further down the result set. For
        // chips that don't push a server filter (Notes, Docs, etc.),
        // we skip the network round-trip — the source-aware LIMIT
        // returns N distinct sources of all types, so client-side
        // filter is sufficient.
        var prev = this.activeType;
        this.activeType = t;
        if (!this.query) return;
        var prevWasServerFiltered = !!this._sessionTypeFilterFor(prev);
        var nextIsServerFiltered = !!this._sessionTypeFilterFor(t);
        if (prevWasServerFiltered || nextIsServerFiltered) {
          this._refetch();
        }
      },

      // ── rendering helpers ──────────────────────────────────────────
      typeLabel(t) {
        // Per-row badge label. Round 7k: session_type-derived rows show
        // "Session" or "Dispatch" instead of the raw source_type.
        if (t === 'session') return 'Session';
        if (t === 'dispatch') return 'Dispatch';
        return TYPE_LABELS[t] || (t ? this._titleCase(t) : 'Note');
      },

      // Compute the per-row pill key (for the badge under the title +
      // the accent rail). Sessions get green; dispatch (incl. legacy
      // agent-run rows that retain dispatch session_type via metadata)
      // gets the orange "agent-run" hue for visual continuity with the
      // pre-Round-7k color (note 24).
      rowPillKey(r) {
        var key = rowChipKey(r);
        if (key === 'dispatch') return 'agent-run';
        if (key === 'session') return 'session';
        return r && r.source_type;
      },

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
