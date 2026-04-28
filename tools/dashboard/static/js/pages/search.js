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

  document.addEventListener('alpine:init', () => {
    Alpine.data('searchPage', () => ({
      query: '',
      results: [],
      loaded: false,
      activeType: 'all',

      init() {
        var params = new URLSearchParams(window.location.search);
        this.query = params.get('q') || '';
        if (!this.query) { this.loaded = true; return; }
        fetch('/api/search?q=' + encodeURIComponent(this.query) + '&group=1&limit=50')
          .then(r => r.json())
          .then(d => {
            this.results = Array.isArray(d) ? d : (d.results || []);
            this.loaded = true;
          });
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

      goBack() {
        if (window.history.length > 1) window.history.back();
        else navigateTo('/');
      },

      submitQuery() {
        var q = (this.query || '').trim();
        if (!q) return;
        navigateTo('/search?q=' + encodeURIComponent(q));
      },

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
