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
//   _smokeIcon:         {cls,icon,tip}|null — smoke test collapsed icon (✓/✗/~)
//   _smokeBadge:        {cls,label}|null — smoke test result pill
//   _hiddenByParent:    boolean         — true when parent dispatch entry exists in batch (matched via parent_run_id)
//   _supportsIntegratedLibrarian: boolean — true for dispatch entries that can show integrated librarian sub-row
//   _integratedLibrarian: object|null   — forwarded librarian data {duration_secs, token_count, _tokenFmt, _reviewItems, _countSummary, run_id}

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
    // Generic — no structured data, return null so template shows just "📖 Reviewed"
    // without appending " · Reviewed" redundantly.
    return null;
  }

  // Shared tier counting for smoke_result payloads.
  function _smokeTierInfo(smoke) {
    const t1 = smoke.tier1;
    const t2 = smoke.tier2;
    const t1Checks = (t1 && t1.checks) ? t1.checks : [];
    const t1Pass = t1Checks.filter(c => c.pass).length;
    const t1Total = t1Checks.length;
    const t2Pages = (t2 && !t2.skipped && t2.pages) ? t2.pages : null;
    const t2Pass = t2Pages ? t2Pages.filter(p => p.pass).length : null;
    const t2Total = t2Pages ? t2Pages.length : null;
    const t2Skipped = !!(t2 && t2.skipped);
    return { t1, t2, t1Checks, t1Pass, t1Total, t2Pages, t2Pass, t2Total, t2Skipped };
  }

  // Build smoke badge from smoke_result.json payload (expanded card).
  function _formatSmokeBadge(smoke) {
    if (!smoke) return null;
    const { t1, t2, t1Checks, t1Pass, t1Total, t2Pages, t2Pass, t2Total, t2Skipped } = _smokeTierInfo(smoke);
    const durS = smoke.duration_ms != null ? (smoke.duration_ms / 1000).toFixed(1) + 's' : null;

    // tier2-only skip (no tier1 at all)
    if (!t1 && t2 && t2Skipped) {
      return { cls: 'smoke-skip', label: '~ Smoke skipped (' + (t2.reason || 'tier2') + ')' };
    }

    if (smoke.pass) {
      const parts = ['✓ Smoke PASS'];
      if (t1Total > 0) parts.push('tier1 ' + t1Pass + '/' + t1Total);
      if (t2Pages) parts.push('tier2 ' + t2Pass + '/' + t2Total);
      else if (t2Skipped) parts.push('tier2 skipped');
      if (durS) parts.push(durS);
      return { cls: 'smoke-pass', label: parts.join('  ') };
    } else {
      let failDetail = '';
      const failingCheck = t1Checks.find(c => !c.pass);
      if (failingCheck) {
        failDetail = failingCheck.detail || failingCheck.name;
      } else if (t2Pages) {
        const failPage = t2Pages.find(p => !p.pass);
        if (failPage) failDetail = failPage.detail || failPage.page;
      }
      const parts = ['✗ Smoke FAIL'];
      if (t1Total > 0) parts.push('tier1 ' + t1Pass + '/' + t1Total);
      if (t2Pages) parts.push('tier2 ' + t2Pass + '/' + t2Total);
      if (failDetail) parts.push(failDetail);
      return { cls: 'smoke-fail', label: parts.join('  ') };
    }
  }

  // Build collapsed smoke icon from smoke_result.json payload.
  function _formatSmokeIcon(smoke) {
    if (!smoke) return null;
    const { t1, t2, t1Checks, t1Pass, t1Total, t2Pages, t2Pass, t2Total, t2Skipped } = _smokeTierInfo(smoke);

    // tier2-only skip (no tier1 at all)
    if (!t1 && t2 && t2Skipped) {
      return { cls: 'tl-smoke-skip', icon: '~', tip: 'Smoke skipped (' + (t2.reason || 'tier2') + ')' };
    }

    const tipParts = [];
    if (t1Total > 0) tipParts.push('tier1 ' + t1Pass + '/' + t1Total);
    if (t2Pages) tipParts.push('tier2 ' + t2Pass + '/' + t2Total);
    else if (t2Skipped) tipParts.push('tier2 skipped');

    if (smoke.pass) {
      return { cls: 'tl-smoke-pass', icon: '✓', tip: tipParts.join(', ') };
    } else {
      const failingCheck = t1Checks.find(c => !c.pass);
      let failDetail = '';
      if (failingCheck) failDetail = failingCheck.detail || failingCheck.name;
      else if (t2Pages) {
        const failPage = t2Pages.find(p => !p.pass);
        if (failPage) failDetail = failPage.detail || failPage.page;
      }
      if (failDetail) tipParts.push(failDetail);
      return { cls: 'tl-smoke-fail', icon: '✗', tip: tipParts.join(', ') };
    }
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
    const smokeIcon = _formatSmokeIcon(e.smoke_result);
    const hasBottom = hasBreakdown || scores != null || e.lines_added != null || e.lines_removed != null || e.duration_secs != null || tokenFmt != null || e.smoke_result != null;

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
      _smokeIcon: smokeIcon,
      _smokeBadge: _formatSmokeBadge(e.smoke_result),
      _hiddenByParent: false,
      _supportsIntegratedLibrarian: !isLibrarian,
      _integratedLibrarian: null,
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
        const mapped = Array.isArray(entries) ? entries.map(_mapEntry) : [];
        // Hide librarian rows when their parent dispatch entry is in the same batch.
        // Match on parent_run_id (populated by server from librarian_jobs.payload.run_id)
        // instead of bead_id, which is empty on librarian dispatch_runs rows.
        const runIdsInBatch = new Set(
          mapped.filter(e => !e._isLibrarian && e.run_id).map(e => e.run_id)
        );
        for (const e of mapped) {
          if (e._isLibrarian && e.parent_run_id && runIdsInBatch.has(e.parent_run_id)) {
            e._hiddenByParent = true;
          }
        }
        // Forward hidden librarian data to parent as _integratedLibrarian
        const parentRunMap = {};
        for (const e of mapped) {
          if (!e._isLibrarian && e.run_id) parentRunMap[e.run_id] = e;
        }
        for (const e of mapped) {
          if (e._isLibrarian && e._hiddenByParent && e.parent_run_id) {
            const parent = parentRunMap[e.parent_run_id];
            if (parent && parent._supportsIntegratedLibrarian) {
              // Count review items by type for header summary
              let countParts = [];
              if (e._reviewItems) {
                const counts = {};
                for (const item of e._reviewItems) {
                  const t = item.type || 'other';
                  counts[t] = (counts[t] || 0) + 1;
                }
                if (counts.pitfall) countParts.push(counts.pitfall + ' pitfall' + (counts.pitfall > 1 ? 's' : ''));
                if (counts.bead) countParts.push(counts.bead + ' bead' + (counts.bead > 1 ? 's' : ''));
                if (counts.skip) countParts.push(counts.skip + ' skipped');
              }
              parent._integratedLibrarian = {
                duration_secs: e.duration_secs,
                token_count: e.token_count,
                _tokenFmt: e._tokenFmt,
                _reviewItems: e._reviewItems,
                _countSummary: countParts.join(', '),
                run_id: e.run_id,
              };
            }
          }
        }
        this.entries = mapped;
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
