// Worktrees page Alpine component.
// Polls the dashboard worktree API and exposes explicit merge/cleanup actions.

(function () {
  function _shortSha(sha) {
    return sha ? sha.slice(0, 7) : '';
  }

  function _toast(message, type) {
    if (window.showToast) {
      window.showToast(message, type || 'error');
    }
  }

  async function _jsonOrError(resp) {
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    return data;
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('worktreesPage', () => ({
      rows: [],
      loading: true,
      refreshing: false,
      error: '',
      lastUpdated: '',
      merging: {},
      cleaning: {},
      _timer: null,

      get sortedRows() {
        return this.rows.slice().sort((a, b) => {
          const as = a.session_name || '';
          const bs = b.session_name || '';
          if (as !== bs) return as.localeCompare(bs);
          return (a.repo_name || '').localeCompare(b.repo_name || '');
        });
      },

      get liveCount() {
        return this.rows.filter(r => r.session_live).length;
      },

      get mergeableCount() {
        return this.rows.filter(r => r.ff_eligible).length;
      },

      get orphanedCount() {
        return this.rows.filter(r => !r.session_live && (r.is_dirty || r.commits_ahead > 0)).length;
      },

      rowKey(row) {
        return (row.session_name || '') + '/' + (row.repo_name || '');
      },

      statusLabel(row) {
        if (row.session_live) return 'LIVE';
        if (row.is_dirty || row.commits_ahead > 0) return 'ORPHANED';
        return 'DEAD-CLEAN';
      },

      statusClass(row) {
        const status = this.statusLabel(row);
        if (status === 'LIVE') return 'bg-green-900/70 text-green-200';
        if (status === 'ORPHANED') return 'bg-amber-900/70 text-amber-200';
        return 'bg-gray-800 text-gray-400';
      },

      isMerging(row) {
        return !!this.merging[this.rowKey(row)];
      },

      isCleaning(row) {
        return !!this.cleaning[row.session_name];
      },

      isBusy(row) {
        return this.isMerging(row) || this.isCleaning(row);
      },

      canMerge(row) {
        return row.repo_name === 'autonomy' && row.ff_eligible;
      },

      mergeTitle(row) {
        if (row.repo_name !== 'autonomy') return 'Only autonomy worktrees can be merged from the dashboard today';
        if (row.ff_eligible) return 'Fast-forward merge from managed clone';
        if (row.is_dirty) return 'Dirty worktree cannot be merged';
        if (!row.commits_ahead) return 'No commits ahead of base';
        return 'Not fast-forward eligible';
      },

      async refresh(manual) {
        if (manual) this.refreshing = true;
        this.error = '';
        try {
          const resp = await fetch('/api/worktrees');
          this.rows = await _jsonOrError(resp);
          this.lastUpdated = new Date().toLocaleTimeString();
        } catch (err) {
          this.error = err.message || String(err);
          _toast('Worktree refresh failed: ' + this.error, 'error');
        } finally {
          this.loading = false;
          this.refreshing = false;
        }
      },

      async merge(row) {
        if (!this.canMerge(row) || this.isBusy(row)) return;
        const key = this.rowKey(row);
        this.merging = { ...this.merging, [key]: true };
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/merge',
            { method: 'POST' },
          );
          const data = await _jsonOrError(resp);
          _toast('Merged ' + key + ' at ' + _shortSha(data.commit), 'warning');
          await this.refresh(false);
        } catch (err) {
          _toast('Merge failed: ' + (err.message || String(err)), 'error');
        } finally {
          const next = { ...this.merging };
          delete next[key];
          this.merging = next;
        }
      },

      async cleanup(row) {
        if (row.session_live || this.isBusy(row)) return;
        const needsForce = row.is_dirty || row.commits_ahead > 0;
        let force = false;
        if (needsForce) {
          const msg = 'This worktree has local changes or commits. Cleanup will use force. Continue?';
          if (!window.confirm(msg)) return;
          force = true;
        }

        this.cleaning = { ...this.cleaning, [row.session_name]: true };
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/cleanup',
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ force }),
            },
          );
          const data = await _jsonOrError(resp);
          const removed = (data.removed || []).length;
          const preserved = (data.preserved || []).length;
          _toast('Cleanup removed ' + removed + ', preserved ' + preserved, 'warning');
          await this.refresh(false);
        } catch (err) {
          _toast('Cleanup failed: ' + (err.message || String(err)), 'error');
        } finally {
          const next = { ...this.cleaning };
          delete next[row.session_name];
          this.cleaning = next;
        }
      },

      init() {
        this.refresh(false);
        this._timer = setInterval(() => this.refresh(false), 30000);
      },

      destroy() {
        if (this._timer) {
          clearInterval(this._timer);
          this._timer = null;
        }
      },
    }));
  });
})();
