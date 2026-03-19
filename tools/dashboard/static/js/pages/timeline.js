// Timeline page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Data shape for each entry (pre-computed in _mapEntry):
//   _open:              boolean         — expand/collapse state
//   _key:               string          — unique key for x-for
//   _ts:                string          — formatted timestamp (HH:MM)
//   _dotCls:            string          — tl-dot-* class for status dot
//   _prioCls:           string          — tl-ft-p* class for priority badge
//   _stCls:             string          — tl-ft-* class for status badge
//   _starsAvg:          bool[]|null     — 5-element array for blended star rating
//   _avgFormatted:      string|null     — e.g. "3.3"
//   _avgRounded:        int|null        — Math.round(avg) for stars
//   _starsTooling:      bool[]|null     — 5-element array for tooling stars
//   _starsClarity:      bool[]|null     — 5-element array for clarity stars
//   _starsConfidence:   bool[]|null     — 5-element array for confidence stars
//   _hasBreakdown:      boolean         — true if any time_breakdown % > 0
//   _hasBottom:         boolean         — true if any bottom content exists
//   _barR, _barC, _barD, _barT: int    — time breakdown pcts
//   _isLibrarian:       boolean         — true if this is a standalone librarian run
//   _tokenFmt:          string|null     — formatted token count e.g. "35K"
//   _libTitle:          string          — human-readable librarian type name
//   _reviewCollapsed:   string|null     — collapsed review label e.g. "📚 Reviewed"
//   _reviewItems:       array|null      — extracted items from experience_reviewer results

(function () {
  function _starsBool(score) {
    if (score == null) return null;
    const filled = Math.round(score);
    return Array.from({ length: 5 }, (_, i) => i < filled);
  }

  function _fmtDuration(secs) {
    if (secs == null) return '--';
    if (secs < 60) return Math.round(secs) + 's';
    if (secs < 3600) return Math.round(secs / 60) + 'm';
    const h = Math.floor(secs / 3600);
    const m = Math.round((secs % 3600) / 60);
    return h + 'h ' + m + 'm';
  }

  function _fmtTokens(n) {
    if (n == null) return null;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return Math.round(n / 1000) + 'K';
    return String(n);
  }

  // Map librarian job_type to a human-readable display name.
  const _LIB_NAMES = {
    'review_report': 'Experience Review',
  };

  // Build collapsed review label from librarian_review payload.
  function _reviewCollapsedLabel(review) {
    if (!review) return null;
    if (review.status === 'running') return null; // shown separately
    // Structured results.json from experience_reviewer
    if (Array.isArray(review.extracted) || Array.isArray(review.skipped)) {
      const nExt = (review.extracted || []).length;
      const nSkip = (review.skipped || []).length;
      const parts = [];
      if (nExt > 0) parts.push(nExt + ' extracted');
      if (nSkip > 0) parts.push(nSkip + ' skipped');
      return parts.length > 0 ? parts.join(' · ') : 'Reviewed';
    }
    // Generic — just say "Reviewed"
    return 'Reviewed';
  }

  function _mapEntry(e, idx) {
    const rawTs = e.completed_at || e.started_at || '';
    const ts = rawTs
      ? new Date(rawTs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : '';

    const dotCls =
      e.status === 'DONE' ? 'tl-dot-done'
      : e.status === 'FAILED' ? 'tl-dot-failed'
      : e.status === 'BLOCKED' ? 'tl-dot-blocked'
      : 'tl-dot-default';

    const prio = e.priority;
    const prioCls =
      prio === 0 ? 'tl-ft-p0'
      : prio === 1 ? 'tl-ft-p1'
      : prio === 2 ? 'tl-ft-p2'
      : 'tl-ft-p3';

    const stCls =
      e.status === 'DONE' ? 'tl-ft-done'
      : e.status === 'FAILED' ? 'tl-ft-failed'
      : e.status === 'BLOCKED' ? 'tl-ft-blocked'
      : 'tl-ft-p3';

    const scores = e.scores;
    let starsAvg = null;
    let avgFormatted = null;
    let avgRounded = null;
    let starsTooling = null;
    let starsClarity = null;
    let starsConfidence = null;
    if (scores) {
      const avg = (scores.tooling + scores.clarity + scores.confidence) / 3;
      avgRounded = Math.round(avg);
      avgFormatted = avg.toFixed(1);
      starsAvg = _starsBool(avg);
      starsTooling = _starsBool(scores.tooling);
      starsClarity = _starsBool(scores.clarity);
      starsConfidence = _starsBool(scores.confidence);
    }

    const tb = e.time_breakdown;
    const barR = (tb && tb.research_pct) || 0;
    const barC = (tb && tb.coding_pct) || 0;
    const barD = (tb && tb.debugging_pct) || 0;
    const barT = (tb && tb.tooling_workaround_pct) || 0;
    const hasBreakdown = barR + barC + barD + barT > 0;
    const tokenFmt = _fmtTokens(e.token_count);
    const hasBottom = hasBreakdown || scores != null || e.lines_added != null || e.lines_removed != null || e.duration_secs != null || tokenFmt != null;

    const isLibrarian = !!e.librarian_type;
    const libTitle = isLibrarian ? (_LIB_NAMES[e.librarian_type] || e.librarian_type) : '';

    // Review items for experience_reviewer expanded view
    let reviewItems = null;
    const rev = e.librarian_review;
    if (rev && (Array.isArray(rev.extracted) || Array.isArray(rev.skipped))) {
      reviewItems = [
        ...(rev.extracted || []).map(item => ({ ...item, _cls: 'tl-lib-dot-' + (item.type || 'skip') })),
        ...(rev.skipped || []).map(item => ({ ...item, type: 'skip', _cls: 'tl-lib-dot-skip' })),
      ];
    }

    return {
      ...e,
      _open: false,
      _key: (e.run_id || e.bead_id || '') + '-' + idx,
      _ts: ts,
      _dotCls: dotCls,
      _prioCls: prioCls,
      _stCls: stCls,
      _starsAvg: starsAvg,
      _avgFormatted: avgFormatted,
      _avgRounded: avgRounded,
      _starsTooling: starsTooling,
      _starsClarity: starsClarity,
      _starsConfidence: starsConfidence,
      _hasBreakdown: hasBreakdown,
      _hasBottom: hasBottom,
      _barR: barR,
      _barC: barC,
      _barD: barD,
      _barT: barT,
      _isLibrarian: isLibrarian,
      _libTitle: libTitle,
      _tokenFmt: tokenFmt,
      _reviewCollapsed: _reviewCollapsedLabel(e.librarian_review),
      _reviewItems: reviewItems,
    };
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('timelinePage', () => ({
      range: '1D',
      stats: {},
      entries: [],
      loading: true,
      _intervalId: null,

      rangeToParam(r) {
        if (r === '1D') return '1d';
        if (r === '1W') return '7d';
        if (r === '1M') return '30d';
        if (r === 'All') return '';
        return '1d';
      },

      setRange(r) {
        this.range = r;
        this.refresh();
      },

      fmtDuration(secs) {
        return _fmtDuration(secs);
      },

      starsFor(score) {
        return _starsBool(score) || [];
      },

      async refresh() {
        const rangeParam = this.rangeToParam(this.range);
        const qs = rangeParam ? '?range=' + rangeParam : '';
        const [stats, entries] = await Promise.all([
          fetch('/api/timeline/stats' + qs).then(r => r.json()),
          fetch('/api/timeline' + qs).then(r => r.json()),
        ]);
        this.stats = stats;
        this.entries = Array.isArray(entries) ? entries.map(_mapEntry) : [];
        this.loading = false;
      },

      init() {
        this.refresh();
        this._intervalId = setInterval(() => this.refresh(), 15000);
      },

      destroy() {
        if (this._intervalId) {
          clearInterval(this._intervalId);
          this._intervalId = null;
        }
      },
    }));
  });
})();
