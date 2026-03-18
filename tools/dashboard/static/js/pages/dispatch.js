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

(function () {
  const _STATE_COLORS = {
    queued: 'blue',
    launching: 'yellow',
    running: 'green',
    collecting: 'purple',
    merging: 'indigo',
  };
  const _ACTIVE_DISPATCH_STATES = new Set(['queued', 'launching', 'running', 'collecting', 'merging']);

  function _getDispatchState(bead) {
    for (const l of (bead.labels || [])) {
      if (l.startsWith('dispatch:')) return l.split(':')[1];
    }
    return null;
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('dispatchPage', () => ({
      active: [],
      waiting: [],
      blocked: [],

      async refresh() {
        const [status, allBeads, approvedData] = await Promise.all([
          api('/api/dispatch/status'),
          api('/api/beads/list'),
          api('/api/dispatch/approved'),
        ]);

        const beadList = Array.isArray(allBeads) ? allBeads : [];

        this.waiting = (Array.isArray(approvedData?.waiting) ? approvedData.waiting : [])
          .map(b => ({ ...b, _section: 'waiting', _borderColor: 'blue', _ds: null, _stateColor: 'gray', _container: null, _runDir: '', _snippet: '', _tokens: '' }));

        this.blocked = (Array.isArray(approvedData?.blocked) ? approvedData.blocked : [])
          .map(b => ({ ...b, _section: 'blocked', _borderColor: 'yellow', _ds: null, _stateColor: 'gray', _container: null, _runDir: '', _snippet: '', _tokens: '' }));

        // Build container lookup (skip slack agent containers)
        const containersByBead = {};
        for (const c of (status.containers || [])) {
          if (c.name.startsWith('agent-slack')) continue;
          const parts = c.name.replace('agent-', '').split('-');
          parts.pop();
          containersByBead[parts.join('-')] = c;
        }

        // Filter beads that are actively dispatched or in_progress
        const filteredActive = beadList.filter(b => {
          const ds = _getDispatchState(b);
          return (ds && _ACTIVE_DISPATCH_STATES.has(ds)) || b.status === 'in_progress';
        });

        // Build runs lookup (only when there are active beads — avoids extra round-trip)
        const runsByBead = {};
        if (filteredActive.length > 0) {
          const runsData = await api('/api/dispatch/runs');
          const runsList = Array.isArray(runsData) ? runsData : [];
          for (const r of runsList) {
            if (!runsByBead[r.bead_id]) runsByBead[r.bead_id] = r;
          }
        }

        this.active = filteredActive.map(b => {
          const ds = _getDispatchState(b);
          const run = runsByBead[b.id];
          return {
            ...b,
            _section: 'active',
            _borderColor: 'green',
            _ds: ds,
            _stateColor: _STATE_COLORS[ds] || 'gray',
            _container: containersByBead[b.id] || null,
            _runDir: run ? run.dir : '',
            _snippet: '',
            _tokens: '',
          };
        });

        // Fetch snippet data reactively — store on bead objects so x-text renders them.
        // Uses getLiveSnippet() and _formatTokenCount() from app.js (shared utilities).
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

      // Called from x-init in the fragment. Registers the interval on window.dispatchInterval
      // so the SPA router can clear it when navigating away.
      startRefresh() {
        this.refresh();
        window.dispatchInterval = setInterval(() => this.refresh(), 5000);
      },
    }));
  });
})();
