// Dispatch page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Data shape for each bead:
//   _section:    'active' | 'waiting' | 'blocked'  — drives conditional rendering in bead-card.html
//   _ds:         string | null                       — dispatch: label value
//   _stateColor: string                              — Tailwind colour name for _ds badge
//   _runDir:     string                              — run dir name for live panel (active only)
//   _snippet:    string                              — latest snippet text pushed by server
//   _dotColor:   'green' | 'yellow' | 'gray'        — status dot colour
//   _dotPulse:   boolean                             — whether dot should animate
//   _duration:   string                              — formatted elapsed time, e.g. "4m00s"
//   _cpu_pct:    string                              — formatted CPU %, e.g. "12.4%"
//   _cpu_secs:   string                              — formatted CPU time, e.g. "45.2s"
//   _mem_mb:     string                              — formatted memory, e.g. "487MB"
//   _tok:        string                              — formatted token count, e.g. "18.2K"
//   _tools:      string                              — formatted tool call count, e.g. "14"
//   _turns:      string                              — formatted turn count, e.g. "8"
//
// Server fields consumed (active beads):
//   cpu_pct, cpu_usec, mem_mb, token_count, tool_count, turn_count, duration_secs, last_activity, container, run_dir
//
// Data is pushed via SSE (connectEvents) rather than polled.
// The 'dispatch' topic delivers {active, waiting, blocked} from the server.
// The 'nav' topic delivers badge counts (open_beads, running_agents, approved_waiting).

(function () {
  const _STATE_COLORS = {
    queued: 'blue',
    launching: 'yellow',
    running: 'green',
    collecting: 'purple',
    merging: 'indigo',
  };

  function _getDispatchState(bead) {
    for (const l of (bead.labels || [])) {
      if (l.startsWith('dispatch:')) return l.split(':')[1];
    }
    return null;
  }

  function _formatDuration(secs) {
    if (!secs && secs !== 0) return '';
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return m + 'm' + String(s).padStart(2, '0') + 's';
  }

  function _formatCpuTime(usec) {
    if (!usec) return '';
    const secs = usec / 1e6;
    if (secs >= 60) {
      const m = Math.floor(secs / 60);
      const s = Math.floor(secs % 60);
      return m + 'm' + String(s).padStart(2, '0') + 's';
    }
    return secs.toFixed(1) + 's';
  }

  function _formatTokens(count) {
    if (!count) return '';
    if (count >= 1e6) return (count / 1e6).toFixed(1) + 'M';
    if (count >= 1000) return (count / 1000).toFixed(1) + 'K';
    return String(count);
  }

  function _computeDot(b) {
    if (b.container) return { color: 'green', pulse: true };
    const now = Date.now() / 1000;
    if (b.last_activity && (now - b.last_activity) < 30) return { color: 'green', pulse: true };
    if (b.last_activity) return { color: 'yellow', pulse: false };
    return { color: 'gray', pulse: false };
  }

  function _mapActive(b) {
    const ds = _getDispatchState(b);
    const dot = _computeDot(b);
    const cpuPct = b.cpu_pct != null ? b.cpu_pct.toFixed(1) + '%' : '';
    return {
      ...b,
      _section: 'active',
      _ds: ds,
      _stateColor: _STATE_COLORS[ds] || 'gray',
      _runDir: b.run_dir || '',
      _snippet: b.last_snippet || '',
      _dotColor: dot.color,
      _dotPulse: dot.pulse,
      _duration: _formatDuration(b.duration_secs),
      _cpu_pct: cpuPct,
      _cpu_secs: _formatCpuTime(b.cpu_usec),
      _mem_mb: b.mem_mb != null ? Math.round(b.mem_mb) + 'MB' : '',
      _tok: _formatTokens(b.token_count),
      _tools: b.tool_count != null ? String(b.tool_count) : '',
      _turns: b.turn_count != null ? String(b.turn_count) : '',
    };
  }

  function _mapWaiting(b) {
    return {
      ...b,
      _section: 'waiting',
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
    };
  }

  function _mapBlocked(b) {
    return {
      ...b,
      _section: 'blocked',
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
    };
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('dispatchPage', () => ({
      active: [],
      waiting: [],
      blocked: [],
      paused: {},          // { label: bool } — plain object for Alpine reactivity
      reasons: {},         // { label: string } — why each label is paused (e.g. smoke failure)
      dispatcherState: { paused: false, reason: null },  // SQLite dispatcher pause (auth failure etc.)
      _dispatchHandler: null,
      _pauseHandler: null,
      _dispatcherStateHandler: null,

      applyDispatch(data) {
        this.waiting = (data.waiting || []).map(_mapWaiting);
        this.blocked = (data.blocked || []).map(_mapBlocked);
        this.active  = (data.active  || []).map(_mapActive);
        if (data.paused != null) {
          this.paused = { ...data.paused };
        }
        if (data.pause_reasons != null) {
          this.reasons = { ...data.pause_reasons };
        }
      },

      applyPause(pauseState) {
        // dispatch_pause SSE now sends {paused: {...}, reasons: {...}}
        if (pauseState.paused != null) {
          this.paused = { ...pauseState.paused };
          this.reasons = { ...(pauseState.reasons || {}) };
        } else {
          // Backwards compat: plain {label: bool} map
          this.paused = { ...pauseState };
        }
      },

      applyDispatcherState(state) {
        this.dispatcherState = { ...state };
      },

      async resumeDispatcher() {
        try {
          const resp = await fetch('/api/dispatch/resume', { method: 'POST' });
          if (resp.ok) {
            this.dispatcherState = { paused: false, reason: null };
          }
        } catch (_) {}
      },

      async togglePause(label) {
        const nowPaused = !this.paused[label];
        // Optimistic UI update
        this.paused = { ...this.paused, [label]: nowPaused };
        if (!nowPaused) {
          // Optimistically clear reason on unpause
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
            // Revert on failure
            this.paused = { ...this.paused, [label]: !nowPaused };
          }
        } catch (_) {
          this.paused = { ...this.paused, [label]: !nowPaused };
        }
      },

      // Alpine lifecycle — called automatically when the component initialises.
      // Reads from the global SSE cache for an instant render, then registers
      // for live updates. Does NOT open a new SSE connection.
      init() {
        // Instant render: if SSE has already delivered dispatch data, use it.
        if (window._sseCache && window._sseCache.dispatch) {
          this.applyDispatch(window._sseCache.dispatch);
        }
        // Register for live dispatch + pause updates via the shared persistent connection.
        this._dispatchHandler = data => this.applyDispatch(data);
        this._pauseHandler = data => this.applyPause(data);
        this._dispatcherStateHandler = data => this.applyDispatcherState(data);
        registerHandler('dispatch', this._dispatchHandler);
        registerHandler('dispatch_pause', this._pauseHandler);
        registerHandler('dispatcher_state', this._dispatcherStateHandler);
      },

      destroy() {
        // Unregister only — do NOT close the shared SSE connection.
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
