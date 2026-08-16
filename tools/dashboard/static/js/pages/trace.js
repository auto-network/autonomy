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
      diffError: null,

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

      async _refreshDiffTarget() {
        if (!this.resolvedRun) return null;
        try {
          const resp = await fetch(
            '/api/dispatch/trace/' + encodeURIComponent(this.resolvedRun),
          );
          const data = await resp.json();
          if (!resp.ok || data.error) return null;
          this.trace = { ...this.trace, diff_target: data.diff_target || null };
          return this.trace.diff_target;
        } catch (_) {
          return null;
        }
      },

      async _openCommitDiff(target) {
        if (!target || !target.run_id || typeof window.openCommitOverlay !== 'function') {
          return false;
        }
        return await window.openCommitOverlay({
          runId: target.run_id,
          sessionName: this.resolvedRun || '',
          branch: target.branch || '',
          targetBranch: target.branch_base || '',
          subject: target.commit_hash
            ? 'Merged commit ' + target.commit_hash.slice(0, 10)
            : 'Merged changes',
        });
      },

      async openDiff() {
        this.diffError = null;
        let target = this.trace && this.trace.diff_target;
        if (!target) return;

        if (target.kind === 'commit') {
          if (!await this._openCommitDiff(target)) {
            this.diffError = 'The merged commit diff could not be opened.';
          }
          return;
        }

        if (target.kind === 'worktree') {
          if (typeof window.openWorktreeReviewOverlay === 'function') {
            const opened = await window.openWorktreeReviewOverlay(
              target.session_name || this.resolvedRun,
            );
            if (opened) return;
          }

          // The worktree may have been merged between rendering the button
          // and clicking it. Resolve once more and switch directly to the
          // immutable commit overlay when that happened.
          const refreshed = await this._refreshDiffTarget();
          if (refreshed && refreshed.kind === 'commit') {
            if (!await this._openCommitDiff(refreshed)) {
              this.diffError = 'The merged commit diff could not be opened.';
            }
            return;
          }

          const href = (refreshed && refreshed.href) || target.href;
          if (href) {
            window.location.href = href;
          } else {
            this.diffError = 'The worktree is no longer available.';
          }
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
