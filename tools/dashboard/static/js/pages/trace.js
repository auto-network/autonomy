// Trace page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads the run name from window.location.pathname on init(), matching the
// pattern used by beadDetailPage() for the bead ID.
//
// State machine: loading → ready | error
// When trace.is_live, opens the live panel via global showLivePanel().

(function () {
  function _fmtDuration(secs) {
    if (secs == null) return '--';
    if (secs < 60) return Math.round(secs) + 's';
    if (secs < 3600) return Math.round(secs / 60) + 'm';
    const h = Math.floor(secs / 3600);
    const m = Math.round((secs % 3600) / 60);
    return h + 'h ' + m + 'm';
  }

  function _starsBool(score) {
    if (score == null) return [];
    const filled = Math.round(score);
    return Array.from({ length: 5 }, (_, i) => i < filled);
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('tracePage', () => ({
      loading: true,
      error: null,
      trace: null,
      bead: null,
      decision: {},
      resolvedRun: null,

      // Computed helpers (called in template)
      starsFor(score) {
        return _starsBool(score);
      },

      fmtDuration(secs) {
        return _fmtDuration(secs);
      },

      statusBadgeClass(status) {
        return status === 'DONE' ? 'badge-closed' : 'badge-open';
      },

      decisionHeadingClass(status) {
        return status === 'DONE' ? 'text-green-400'
          : status === 'BLOCKED' ? 'text-yellow-400'
          : 'text-red-400';
      },

      openSessionLog() {
        if (this.resolvedRun && typeof showCompletedPanel === 'function') {
          showCompletedPanel(this.resolvedRun);
        }
      },

      async init() {
        const run = window.location.pathname.split('/dispatch/trace/')[1] || '';
        this.resolvedRun = run;
        try {
          const data = await fetch(`/api/dispatch/trace/${run}`).then(r => r.json());
          if (data.error) {
            this.error = data.error;
            this.loading = false;
            return;
          }
          this.trace = data;
          this.resolvedRun = data.run || run;
          this.bead = Array.isArray(data.bead) ? data.bead[0] : data.bead;
          this.decision = data.decision || {};
          this.loading = false;
          if (data.is_live && typeof showLivePanel === 'function') {
            this.$nextTick(() => showLivePanel(this.resolvedRun));
          }
        } catch (e) {
          this.error = String(e);
          this.loading = false;
        }
      },
    }));
  });
})();
