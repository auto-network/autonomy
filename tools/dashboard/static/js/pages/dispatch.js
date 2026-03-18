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
//   _runDir: string                               — run dir name for snippet/live panel (active only)
//   _snippet: string                              — latest snippet text (reactive, x-text safe)
//   _tokens: string                               — formatted token estimate (reactive, x-text safe)
//
// Data is pushed via SSE (connectEvents) rather than polled.
// The 'dispatch' topic delivers {active, waiting, blocked} from the server.
// Snippet text is still fetched reactively via getLiveSnippet() from app.js.

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
      // Prefer server-pushed snippet; getLiveSnippet() refreshes it below.
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

        // Fetch fresh snippet text reactively for active beads.
        for (let i = 0; i < this.active.length; i++) {
          const b = this.active[i];
          if (!b._runDir) continue;
          getLiveSnippet(b._runDir).then(snippet => {
            if (!snippet) return;
            if (snippet.text) this.active[i]._snippet = snippet.text;
            if (snippet.file_size_bytes > 0) {
              this.active[i]._tokens = '~' + _formatTokenCount(snippet.file_size_bytes);
            }
          });
        }
      },

      // Called from x-init in the fragment.
      // Connects to SSE and stores handle for cleanup in destroy().
      startRefresh() {
        this._eventsHandle = connectEvents(['dispatch'], {
          dispatch: data => this.applyDispatch(data),
        });
      },

      destroy() {
        if (this._eventsHandle) {
          this._eventsHandle.close();
          this._eventsHandle = null;
        }
        // Clear any legacy polling interval left by older code.
        if (window.dispatchInterval) {
          clearInterval(window.dispatchInterval);
          window.dispatchInterval = null;
        }
      },
    }));
  });
})();
