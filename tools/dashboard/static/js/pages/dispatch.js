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
//   _last:       string                              — elapsed since last activity, e.g. "12s"
//
// Server fields consumed (active beads):
//   cpu_pct, cpu_usec, mem_mb, token_count, tool_count, turn_count, duration_secs, last_activity, container, run_dir
//
// Data is pushed via SSE (connectEvents) rather than polled.
// The 'dispatch' topic delivers {active, waiting, blocked} from the server.
// The 'nav' topic delivers badge counts (open_beads, running_agents, approved_waiting).

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('dispatchPage', () => ({
      ...window.DispatchCards.alpine(),
      paused: {},          // { label: bool } — plain object for Alpine reactivity
      reasons: {},         // { label: string } — why each label is paused (e.g. smoke failure)
      dispatcherState: { paused: false, reason: null },  // SQLite dispatcher pause (auth failure etc.)
      _dispatchHandler: null,
      _pauseHandler: null,
      _dispatcherStateHandler: null,

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
