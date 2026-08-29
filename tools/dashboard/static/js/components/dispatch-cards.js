/**
 * Shared presentation state for dispatch cards.
 *
 * Both /dispatch and Activity render templates/partials/bead-card.html and
 * consume the same `dispatch` SSE payload.  This mixin is the single place
 * that turns those server rows into the view model expected by that partial.
 */
(function () {
  const STATE_COLORS = {
    queued: 'blue',
    launching: 'yellow',
    running: 'green',
    collecting: 'purple',
    merging: 'indigo',
  };

  function dispatchState(row) {
    for (const label of (row.labels || [])) {
      if (label.startsWith('dispatch:')) return label.split(':')[1];
    }
    return null;
  }

  function formatDuration(secs) {
    if (secs == null) return '';
    const minutes = Math.floor(secs / 60);
    const seconds = Math.floor(secs % 60);
    return minutes + 'm' + String(seconds).padStart(2, '0') + 's';
  }

  function formatCpuTime(usec) {
    if (!usec) return '';
    const secs = usec / 1e6;
    if (secs >= 60) {
      const minutes = Math.floor(secs / 60);
      const seconds = Math.floor(secs % 60);
      return minutes + 'm' + String(seconds).padStart(2, '0') + 's';
    }
    return secs.toFixed(1) + 's';
  }

  function formatTokens(count) {
    if (!count) return '';
    if (count >= 1e6) return (count / 1e6).toFixed(1) + 'M';
    if (count >= 1000) return (count / 1000).toFixed(1) + 'K';
    return String(count);
  }

  function formatLastActivity(timestamp) {
    if (!timestamp) return '';
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm';
    return Math.floor(secs / 3600) + 'h';
  }

  function dotFor(row) {
    if (row.container) return { color: 'green', pulse: true };
    const recent = row.last_activity && (Date.now() / 1000 - row.last_activity) < 30;
    if (recent) return { color: 'green', pulse: true };
    if (row.last_activity) return { color: 'yellow', pulse: false };
    return { color: 'gray', pulse: false };
  }

  function active(row) {
    const state = dispatchState(row);
    const dot = dotFor(row);
    const cpu = row.cpu_pct == null ? '' : Number(row.cpu_pct).toFixed(1) + '%';
    return {
      ...row,
      id: row.bead_id || row.id,
      _section: 'active',
      _ds: state,
      _stateColor: STATE_COLORS[state] || 'gray',
      _runDir: row.run_dir || row.dir || '',
      _snippet: row.last_snippet || row.snippet || '',
      _dotColor: dot.color,
      _dotPulse: dot.pulse,
      _duration: formatDuration(row.duration_secs),
      _cpu_pct: cpu,
      _cpu_secs: formatCpuTime(row.cpu_usec),
      _mem_mb: row.mem_mb == null ? '' : Math.round(row.mem_mb) + 'MB',
      _tok: formatTokens(row.token_count),
      _tools: row.tool_count == null ? '' : String(row.tool_count),
      _turns: row.turn_count == null ? '' : String(row.turn_count),
      _last: formatLastActivity(row.last_activity),
    };
  }

  function inactive(row, section) {
    return {
      ...row,
      _section: section,
      _ds: null,
      _stateColor: 'gray',
      _runDir: '',
      _snippet: '',
      _dotColor: 'gray',
      _dotPulse: false,
      _duration: '',
      _cpu_pct: '',
      _cpu_secs: '',
      _mem_mb: '',
      _tok: '',
      _tools: '',
      _turns: '',
      _last: '',
    };
  }

  function routeForRun(row) {
    if (!row) return null;
    const kind = row.kind || 'bead';
    if (kind === 'agentic') {
      if (row.target_kind === 'bead') {
        return row.target_source_id
          ? '/bead/' + encodeURIComponent(row.target_source_id)
          : null;
      }
      const assetId = row.target_source_id || row.agentic_source_id;
      return assetId ? '/graph/' + encodeURIComponent(assetId) : null;
    }
    return row.id ? '/bead/' + encodeURIComponent(row.id) : null;
  }

  window.DispatchCards = {
    alpine() {
      return {
        active: [],
        waiting: [],
        waitingTotal: 0,
        blocked: [],
        routeForRun,
        applyDispatch(data) {
          this.active = (data.active || []).map(active);
          this.waiting = (data.waiting || []).map(row => inactive(row, 'waiting'));
          // The list is the top few; the badge carries the real count.
          this.waitingTotal = (data.waiting_total != null)
            ? data.waiting_total : this.waiting.length;
          this.blocked = (data.blocked || []).map(row => inactive(row, 'blocked'));
          if (data.paused != null) this.paused = { ...data.paused };
          if (data.pause_reasons != null) this.reasons = { ...data.pause_reasons };
        },
      };
    },
    routeForRun,
  };

  // The shared Jinja partial can also render outside either owning page
  // component, so retain the global routing hook it already consults.
  window.routeForRun = routeForRun;
})();
