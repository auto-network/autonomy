// Dispatch page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Data shape for each bead:
//   _section: 'active' | 'waiting' | 'blocked'  — drives conditional rendering in bead-card.html
//   _borderColor: 'green' | 'blue' | 'yellow'   — left border colour
//   _ds: string | null                            — dispatch: label value
//   _stateColor: string                           — Tailwind colour name for _ds badge
//   _container: object | null                     — matching Docker container (active only)
//   _runDir: string                               — run dir name for live panel (active only)
//   _snippet: string                              — latest snippet text pushed by server
//   _tokens: string                               — formatted token estimate pushed by server
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

  function _formatTokenCount(bytes) {
    const tokens = Math.round(bytes / 4);
    if (tokens >= 1000000) return (tokens / 1000000).toFixed(1) + 'M tok';
    if (tokens >= 1000) return (tokens / 1000).toFixed(0) + 'k tok';
    return tokens + ' tok';
  }

  function _mapActive(b) {
    const ds = _getDispatchState(b);
    return {
      ...b,
      _section: 'active',
      _borderColor: 'green',
      _ds: ds,
      _stateColor: _STATE_COLORS[ds] || 'gray',
      _container: b.container || null,
      _runDir: b.run_dir || '',
      _snippet: b.last_snippet || '',
      _tokens: b.token_count ? '~' + _formatTokenCount(b.token_count) : '',
    };
  }

  function _mapWaiting(b) {
    return { ...b, _section: 'waiting', _borderColor: 'blue', _ds: null, _stateColor: 'gray', _container: null, _runDir: '', _snippet: '', _tokens: '' };
  }

  function _mapBlocked(b) {
    return { ...b, _section: 'blocked', _borderColor: 'yellow', _ds: null, _stateColor: 'gray', _container: null, _runDir: '', _snippet: '', _tokens: '' };
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('dispatchPage', () => ({
      active: [],
      waiting: [],
      blocked: [],
      _eventsHandle: null,

      applyDispatch(data) {
        this.waiting = (data.waiting || []).map(_mapWaiting);
        this.blocked = (data.blocked || []).map(_mapBlocked);
        this.active  = (data.active  || []).map(_mapActive);
      },

      // Alpine lifecycle — called automatically when the component initialises.
      // Connects to SSE for dispatch data and nav badge updates.
      init() {
        this._eventsHandle = connectEvents(['dispatch', 'nav'], {
          dispatch: data => this.applyDispatch(data),
          nav: () => {}, // nav events handled globally in app.js
        });
      },

      destroy() {
        if (this._eventsHandle) {
          this._eventsHandle.close();
          this._eventsHandle = null;
        }
      },
    }));
  });
})();
