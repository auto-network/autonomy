// Unified Activity page Alpine component.
//
// Bead auto-140q0.2 keeps /timeline as the implementation base while
// turning the page into the merged Activity feed slice: compact pulse,
// live dispatch cards at the top, collapsed waiting/blocked cues, and
// historical timeline entries below. Attention + Notifications land in
// later beads, so this page only implements the Feed slice.

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

  function _formatDuration(secs) {
    if (!secs && secs !== 0) return '';
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return m + 'm' + String(s).padStart(2, '0') + 's';
  }

  function _formatLastActivity(ts) {
    if (!ts) return '';
    const secs = Math.floor(Date.now() / 1000 - ts);
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm';
    return Math.floor(secs / 3600) + 'h';
  }

  function _formatPct(value) {
    return value == null ? '--' : Math.round(value * 100) + '%';
  }

  const _LIB_NAMES = {
    'review_report': 'Experience Review',
  };

  const _STATE_COLORS = {
    queued: 'blue',
    launching: 'yellow',
    running: 'green',
    collecting: 'purple',
    merging: 'indigo',
  };

  function _reviewCollapsedLabel(review) {
    if (!review) return null;
    if (review.status === 'running') return null;
    if (Array.isArray(review.extracted) || Array.isArray(review.skipped)) {
      const counts = {};
      for (const item of (review.extracted || [])) {
        const t = item.type || 'other';
        counts[t] = (counts[t] || 0) + 1;
      }
      const nSkip = (review.skipped || []).length;
      const parts = [];
      if (counts.pitfall) parts.push(counts.pitfall + ' pitfall' + (counts.pitfall > 1 ? 's' : ''));
      if (counts.bead || counts.bug || counts.work) {
        const n = (counts.bead || 0) + (counts.bug || 0) + (counts.work || 0);
        parts.push(n + ' bead' + (n > 1 ? 's' : ''));
      }
      if (nSkip > 0) parts.push(nSkip + ' skipped');
      return parts.length > 0 ? parts.join(', ') : 'Reviewed';
    }
    return null;
  }

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

  function _formatSmokeBadge(smoke) {
    if (!smoke) return null;
    const { t1, t2, t1Checks, t1Pass, t1Total, t2Pages, t2Pass, t2Total, t2Skipped } = _smokeTierInfo(smoke);
    const durS = smoke.duration_ms != null ? (smoke.duration_ms / 1000).toFixed(1) + 's' : null;

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
    }

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

  function _formatSmokeIcon(smoke) {
    if (!smoke) return null;
    const { t1, t2, t1Checks, t1Pass, t1Total, t2Pages, t2Pass, t2Total, t2Skipped } = _smokeTierInfo(smoke);

    if (!t1 && t2 && t2Skipped) {
      return { cls: 'tl-smoke-skip', icon: '~', tip: 'Smoke skipped (' + (t2.reason || 'tier2') + ')' };
    }

    const tipParts = [];
    if (t1Total > 0) tipParts.push('tier1 ' + t1Pass + '/' + t1Total);
    if (t2Pages) tipParts.push('tier2 ' + t2Pass + '/' + t2Total);
    else if (t2Skipped) tipParts.push('tier2 skipped');

    if (smoke.pass) {
      return { cls: 'tl-smoke-pass', icon: '✓', tip: tipParts.join(', ') };
    }

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

  function _getDispatchState(bead) {
    for (const l of (bead.labels || [])) {
      if (l.startsWith('dispatch:')) return l.split(':')[1];
    }
    return null;
  }

  function _computeDot(bead) {
    if (bead.container) return { color: 'green', pulse: true };
    const now = Date.now() / 1000;
    if (bead.last_activity && (now - bead.last_activity) < 30) return { color: 'green', pulse: true };
    if (bead.last_activity) return { color: 'yellow', pulse: false };
    return { color: 'gray', pulse: false };
  }

  function _mapActive(bead) {
    const ds = _getDispatchState(bead);
    const dot = _computeDot(bead);
    const cpuPct = bead.cpu_pct != null ? bead.cpu_pct.toFixed(1) + '%' : '';
    return {
      ...bead,
      id: bead.bead_id || bead.id,
      _section: 'active',
      _ds: ds,
      _stateColor: _STATE_COLORS[ds] || 'gray',
      _runDir: bead.run_dir || bead.dir || '',
      _snippet: bead.last_snippet || bead.snippet || '',
      _dotColor: dot.color,
      _dotPulse: dot.pulse,
      _duration: _formatDuration(bead.duration_secs),
      _cpu_pct: cpuPct,
      _mem_mb: bead.mem_mb != null ? Math.round(bead.mem_mb) + 'MB' : '',
      _tok: _fmtTokens(bead.token_count) || '',
      _tools: bead.tool_count != null ? String(bead.tool_count) : '',
      _turns: bead.turn_count != null ? String(bead.turn_count) : '',
      _last: _formatLastActivity(bead.last_activity),
    };
  }

  function _mapWaiting(bead) {
    return {
      ...bead,
      _section: 'waiting',
      _ds: null,
      _stateColor: 'gray',
      _runDir: '',
      _snippet: '',
      _dotColor: 'gray',
      _dotPulse: false,
      _duration: '',
      _cpu_pct: '',
      _mem_mb: '',
      _tok: '',
      _tools: '',
      _turns: '',
      _last: '',
    };
  }

  function _mapBlocked(bead) {
    return {
      ...bead,
      _section: 'blocked',
      _ds: null,
      _stateColor: 'gray',
      _runDir: '',
      _snippet: '',
      _dotColor: 'gray',
      _dotPulse: false,
      _duration: '',
      _cpu_pct: '',
      _mem_mb: '',
      _tok: '',
      _tools: '',
      _turns: '',
      _last: '',
    };
  }

  function _mapEntry(e, idx) {
    const runId = e.run_id || e.id || '';
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
    const hasBottom = hasBreakdown || scores != null || e.lines_added != null || e.lines_removed != null || e.duration_secs != null || tokenFmt != null || e.smoke_result != null || e.librarian_review != null;

    const isLibrarian = !!e.librarian_type;
    const libTitle = isLibrarian ? (_LIB_NAMES[e.librarian_type] || e.librarian_type) : '';
    const isAgentic = e.kind === 'agentic';

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
      run_id: runId,
      _open: false,
      _key: (runId || e.bead_id || '') + '-' + idx,
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
      _isAgentic: isAgentic,
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

  function _formatHHMM(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('activityPage', () => ({
      range: '24h',
      stats: {},
      entries: [],
      loading: true,
      active: [],
      waiting: [],
      blocked: [],
      paused: {},
      reasons: {},
      dispatcherState: { paused: false, reason: null, merge_health: { status: 'ok' } },
      showWaiting: false,
      showBlocked: false,
      tab: 'feed',
      attentionZoom: 'normal',
      attentionEntries: [],
      routeForRun: window.routeForRun || null,
      _intervalId: null,
      _dispatchHandler: null,
      _pauseHandler: null,
      _dispatcherStateHandler: null,

      rangeToParam(range) {
        if (range === '6h') return '6h';
        if (range === '24h') return '1d';
        if (range === '7d') return '7d';
        if (range === 'All') return 'all';
        return '1d';
      },

      _journalRangeParam(range) {
        if (range === '6h') return '6h';
        if (range === '24h') return '24h';
        if (range === '7d') return '7d';
        return null;
      },

      setRange(range) {
        this.range = range;
        this.refreshTimeline();
        this.refreshAttention();
      },

      setTab(tab) {
        this.tab = tab;
      },

      setAttentionZoom(mode) {
        this.attentionZoom = mode;
      },

      formatAttentionTimeRange(entry) {
        const start = _formatHHMM(entry && entry.timestamp_start);
        const end = _formatHHMM(entry && entry.timestamp_end);
        if (start && end) return start + '–' + end;
        return start || end || '';
      },

      fmtDuration(secs) {
        return _fmtDuration(secs);
      },

      pulseItems() {
        return [
          { label: 'live', value: this.active.length, cls: 'text-green-400' },
          { label: 'queued', value: this.waiting.length, cls: 'text-blue-400' },
          { label: 'blocked', value: this.blocked.length, cls: 'text-amber-400' },
          { label: 'done', value: this.stats.completed_count || 0, cls: 'text-gray-200' },
          { label: 'success', value: _formatPct(this.stats.success_rate), cls: 'text-gray-200' },
        ];
      },

      applyDispatch(data) {
        this.waiting = (data.waiting || []).map(_mapWaiting);
        this.blocked = (data.blocked || []).map(_mapBlocked);
        this.active = (data.active || []).map(_mapActive);
        if (data.paused != null) {
          this.paused = { ...data.paused };
        }
        if (data.pause_reasons != null) {
          this.reasons = { ...data.pause_reasons };
        }
      },

      applyPause(pauseState) {
        if (pauseState.paused != null) {
          this.paused = { ...pauseState.paused };
          this.reasons = { ...(pauseState.reasons || {}) };
        } else {
          this.paused = { ...pauseState };
        }
      },

      applyDispatcherState(state) {
        this.dispatcherState = {
          paused: !!state.paused,
          reason: state.reason || null,
          merge_health: state.merge_health || { status: 'ok' },
        };
      },

      async togglePause(label) {
        const nowPaused = !this.paused[label];
        this.paused = { ...this.paused, [label]: nowPaused };
        if (!nowPaused) {
          const { [label]: _, ...rest } = this.reasons;
          this.reasons = rest;
        }
        try {
          const resp = await fetch('/api/dispatch/pause', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label, paused: nowPaused }),
          });
          if (resp.ok) {
            const data = await resp.json();
            this.paused = { ...data.paused };
            this.reasons = { ...(data.reasons || {}) };
          } else {
            this.paused = { ...this.paused, [label]: !nowPaused };
          }
        } catch (_) {
          this.paused = { ...this.paused, [label]: !nowPaused };
        }
      },

      async resumeDispatcher() {
        try {
          const resp = await fetch('/api/dispatch/resume', { method: 'POST' });
          if (resp.ok) {
            this.dispatcherState = { paused: false, reason: null, merge_health: this.dispatcherState.merge_health || { status: 'ok' } };
          }
        } catch (_) {}
      },

      async refreshAttention() {
        const param = this._journalRangeParam(this.range);
        const limit = param ? 50 : 200;
        const qs = param
          ? '?since=' + encodeURIComponent(param) + '&limit=' + limit
          : '?limit=' + limit;
        try {
          const data = await fetch('/api/journal' + qs).then(r => r.json());
          this.attentionEntries = Array.isArray(data && data.entries) ? data.entries : [];
        } catch (_) {
          this.attentionEntries = [];
        }
      },

      async refreshTimeline() {
        const rangeParam = this.rangeToParam(this.range);
        const qs = rangeParam && rangeParam !== 'all' ? '?range=' + encodeURIComponent(rangeParam) : '?range=all';
        const [stats, entries] = await Promise.all([
          fetch('/api/timeline/stats' + qs).then(r => r.json()),
          fetch('/api/timeline' + qs).then(r => r.json()),
        ]);
        this.stats = stats;
        const mapped = Array.isArray(entries) ? entries.map(_mapEntry) : [];
        const runIdsInBatch = new Set(
          mapped.filter(e => !e._isLibrarian && e.run_id).map(e => e.run_id)
        );
        for (const e of mapped) {
          if (e._isLibrarian && e.parent_run_id && runIdsInBatch.has(e.parent_run_id)) {
            e._hiddenByParent = true;
          }
        }
        const parentRunMap = {};
        for (const e of mapped) {
          if (!e._isLibrarian && e.run_id) parentRunMap[e.run_id] = e;
        }
        for (const e of mapped) {
          if (e._isLibrarian && e._hiddenByParent && e.parent_run_id) {
            const parent = parentRunMap[e.parent_run_id];
            if (parent && parent._supportsIntegratedLibrarian) {
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

      async loadDispatchFallback() {
        try {
          const [statusResp, approvedResp] = await Promise.all([
            fetch('/api/dispatch/status').then(r => r.json()),
            fetch('/api/dispatch/approved').then(r => r.json()),
          ]);
          this.applyDispatch({
            active: statusResp.running_runs || [],
            waiting: approvedResp.waiting || [],
            blocked: approvedResp.blocked || [],
          });
        } catch (_) {}
      },

      async loadInitialPauseState() {
        try {
          if (!window._sseCache.dispatch_pause) {
            const pauseState = await fetch('/api/dispatch/pause').then(r => r.json());
            this.applyPause(pauseState);
          }
        } catch (_) {}
        try {
          if (!window._sseCache.dispatcher_state) {
            const state = await fetch('/api/dispatch/pause-state').then(r => r.json());
            this.applyDispatcherState({
              paused: state.paused,
              reason: state.reason,
              merge_health: this.dispatcherState.merge_health || { status: 'ok' },
            });
          }
        } catch (_) {}
      },

      init() {
        if (window._sseCache && window._sseCache.dispatch) {
          this.applyDispatch(window._sseCache.dispatch);
        } else {
          this.loadDispatchFallback();
        }
        if (window._sseCache && window._sseCache.dispatch_pause) {
          this.applyPause(window._sseCache.dispatch_pause);
        }
        if (window._sseCache && window._sseCache.dispatcher_state) {
          this.applyDispatcherState(window._sseCache.dispatcher_state);
        } else {
          this.loadInitialPauseState();
        }

        this._dispatchHandler = data => this.applyDispatch(data);
        this._pauseHandler = data => this.applyPause(data);
        this._dispatcherStateHandler = data => this.applyDispatcherState(data);
        registerHandler('dispatch', this._dispatchHandler);
        registerHandler('dispatch_pause', this._pauseHandler);
        registerHandler('dispatcher_state', this._dispatcherStateHandler);

        this.refreshTimeline();
        this.refreshAttention();
        this._intervalId = setInterval(() => this.refreshTimeline(), 15000);
      },

      destroy() {
        if (this._intervalId) {
          clearInterval(this._intervalId);
          this._intervalId = null;
        }
        if (this._dispatchHandler) {
          unregisterHandler('dispatch', this._dispatchHandler);
          this._dispatchHandler = null;
        }
        if (this._pauseHandler) {
          unregisterHandler('dispatch_pause', this._pauseHandler);
          this._pauseHandler = null;
        }
        if (this._dispatcherStateHandler) {
          unregisterHandler('dispatcher_state', this._dispatcherStateHandler);
          this._dispatcherStateHandler = null;
        }
      },
    }));
  });
})();
