// Worktrees page Alpine component.
// Finalized commit-first review queue with full-screen commit/change review.

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

  function _commitStats(commit) {
    if (commit && commit.stats) return commit.stats;
    const files = (commit && commit.files) || [];
    return {
      files: files.length,
      additions: files.reduce((sum, file) => sum + (file.additions || 0), 0),
      deletions: files.reduce((sum, file) => sum + (file.deletions || 0), 0),
    };
  }

  function _normalizeRows(rows) {
    return (rows || []).map(row => ({
      ...row,
      commits: (row.commits || []).map(commit => ({
        ...commit,
        files: commit.files || [],
        stats: _commitStats(commit),
      })),
      dirty_files: row.dirty_files || [],
    }));
  }

  function _normalizePatchPath(value) {
    if (!value) return '';
    let path = String(value).trim();
    if (path === '/dev/null') return '';
    path = path.replace(/^a\//, '').replace(/^b\//, '');
    return path;
  }

  function _parseUnifiedPatch(patch) {
    if (!patch) return [];
    const files = [];
    const lines = String(patch).split('\n');
    let current = null;
    let oldLine = 0;
    let newLine = 0;
    let inHunk = false;

    function pushCurrent() {
      if (!current) return;
      current.path = current.path || current.newPath || current.oldPath || '';
      files.push(current);
    }

    lines.forEach(raw => {
      if (raw.startsWith('diff --git ')) {
        pushCurrent();
        current = {
          oldPath: '',
          newPath: '',
          path: '',
          lines: [],
        };
        inHunk = false;
        return;
      }

      if (!current) return;

      if (raw.startsWith('--- ')) {
        current.oldPath = _normalizePatchPath(raw.slice(4));
        return;
      }

      if (raw.startsWith('+++ ')) {
        current.newPath = _normalizePatchPath(raw.slice(4));
        current.path = current.newPath || current.oldPath || current.path;
        return;
      }

      if (raw.startsWith('@@')) {
        const match = raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (!match) return;
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
        inHunk = true;
        return;
      }

      if (!inHunk) return;
      if (raw.startsWith('\\ No newline at end of file')) return;
      if (!raw.length) return;

      const marker = raw.charAt(0);
      const text = raw.slice(1);
      if (marker === '+') {
        current.lines.push({
          kind: 'add',
          oldNo: '',
          newNo: newLine,
          text,
        });
        newLine += 1;
        return;
      }
      if (marker === '-') {
        current.lines.push({
          kind: 'del',
          oldNo: oldLine,
          newNo: '',
          text,
        });
        oldLine += 1;
        return;
      }
      if (marker === ' ') {
        current.lines.push({
          kind: 'context',
          oldNo: oldLine,
          newNo: newLine,
          text,
        });
        oldLine += 1;
        newLine += 1;
      }
    });

    pushCurrent();
    return files.filter(file => file.path);
  }

  function _patchIndex(patch) {
    const index = {};
    _parseUnifiedPatch(patch).forEach(file => {
      index[file.path] = file.lines || [];
    });
    return index;
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('worktreesPage', () => ({
      rows: [],
      loading: true,
      refreshing: false,
      detailLoading: false,
      dirtyDetailLoading: false,
      error: '',
      lastUpdated: '',
      viewMode: 'commits',
      selectedCommit: null,
      selectedDirtyRow: null,
      confirmDiscardRow: null,
      showDiff: false,
      mergeState: 'idle',
      mergeBurstActive: false,
      mergeBurstSeed: 0,
      poofingRowKey: '',
      cleaning: {},
      _timer: null,
      confettiPieces: [
        { id: 1, dx: -108, dy: -78, rot: -180, delay: 0, color: '#818cf8' },
        { id: 2, dx: -86, dy: -104, rot: -120, delay: 18, color: '#34d399' },
        { id: 3, dx: -62, dy: -70, rot: -160, delay: 42, color: '#f9a8d4' },
        { id: 4, dx: -28, dy: -118, rot: -90, delay: 0, color: '#fcd34d' },
        { id: 5, dx: -10, dy: -84, rot: -70, delay: 32, color: '#e879f9' },
        { id: 6, dx: 18, dy: -126, rot: 88, delay: 15, color: '#60a5fa' },
        { id: 7, dx: 42, dy: -76, rot: 132, delay: 48, color: '#34d399' },
        { id: 8, dx: 70, dy: -112, rot: 160, delay: 0, color: '#fb7185' },
        { id: 9, dx: 96, dy: -82, rot: 210, delay: 28, color: '#818cf8' },
        { id: 10, dx: -120, dy: -22, rot: -140, delay: 55, color: '#f472b6' },
        { id: 11, dx: 118, dy: -24, rot: 152, delay: 60, color: '#facc15' },
        { id: 12, dx: -54, dy: -132, rot: -220, delay: 24, color: '#38bdf8' },
        { id: 13, dx: 54, dy: -134, rot: 240, delay: 36, color: '#a78bfa' },
        { id: 14, dx: 0, dy: -144, rot: 180, delay: 12, color: '#4ade80' },
      ],

      get commitItems() {
        const items = [];
        this.rows.forEach(row => {
          const commits = this.commitList(row);
          commits.forEach((commit, index) => {
            items.push({
              row,
              commit,
              commitIndex: index,
              position: index + 1,
              total: commits.length,
            });
          });
        });
        return items.sort((a, b) => String(b.commit.date || '').localeCompare(String(a.commit.date || '')));
      },

      get commitStacks() {
        return this.rows
          .filter(row => this.commitList(row).length > 0)
          .map(row => {
            const commits = this.commitList(row);
            return {
              row,
              commit: commits[0],
              commitIndex: 0,
              position: 1,
              total: commits.length,
              sortDate: String((commits[commits.length - 1] && commits[commits.length - 1].date) || (commits[0] && commits[0].date) || ''),
            };
          })
          .sort((a, b) => String(b.sortDate || '').localeCompare(String(a.sortDate || '')));
      },

      get dirtyRows() {
        return this.rows
          .filter(row => row.is_dirty)
          .sort((a, b) => {
            const ac = this.commitList(a).length;
            const bc = this.commitList(b).length;
            if (ac !== bc) return bc - ac;
            return (a.session_name || '').localeCompare(b.session_name || '');
          });
      },

      get autonomyCommitCount() {
        return this.commitItems.filter(item => this.supportsDashboardMerge(item.row)).length;
      },

      get uncommittedOnlyCount() {
        return this.rows.filter(row => row.is_dirty && this.commitList(row).length === 0).length;
      },

      rowKey(row) {
        if (!row) return '';
        return (row.session_name || '') + '/' + (row.repo_name || '');
      },

      commitKey(item) {
        if (!item || !item.commit) return '';
        return this.rowKey(item.row) + '/' + (item.commit.sha || '');
      },

      mergeKey(item) {
        return this.commitKey(item);
      },

      commitList(row) {
        return (row && row.commits) || [];
      },

      dirtyFiles(row) {
        return (row && row.dirty_files) || [];
      },

      shortSha(sha) {
        return _shortSha(sha);
      },

      rowOrg(row) {
        const repo = (row && row.repo_name) || '';
        if (repo === 'autonomy') {
          return { name: 'Autonomy', initial: 'A', color: '#6366f1' };
        }
        if (repo === 'enterprise' || repo === 'enterprise_ng') {
          return { name: 'Anchore', initial: 'A', color: '#2D7DD2' };
        }
        const initial = repo ? repo.charAt(0).toUpperCase() : '?';
        return { name: repo || 'Unknown', initial, color: '#64748b' };
      },

      statusLabel(row) {
        if (row.session_live) return 'LIVE';
        if (row.is_dirty || row.commits_ahead > 0 || this.commitList(row).length > 0) return 'ORPHANED';
        return 'DEAD-CLEAN';
      },

      statusClass(row) {
        const status = this.statusLabel(row);
        if (status === 'LIVE') return 'bg-emerald-300/15 text-emerald-200';
        if (status === 'ORPHANED') return 'bg-amber-300/15 text-amber-200';
        return 'bg-slate-700 text-slate-300';
      },

      displayStatus(status) {
        if (status === '??') return 'U';
        return status || '';
      },

      statusTitle(status) {
        if (status === '??') return 'Untracked';
        if (status === 'M') return 'Modified';
        if (status === 'A') return 'Added';
        if (status === 'D') return 'Deleted';
        return status || '';
      },

      displayPath(path) {
        if (!path) return '';
        if (path.length <= 44) return path;
        const parts = path.split('/');
        const file = parts.pop() || path;
        if (file.length >= 40) return '.../' + file.slice(-40);
        let suffix = file;
        while (parts.length && (parts[parts.length - 1] + '/' + suffix).length <= 40) {
          suffix = parts.pop() + '/' + suffix;
        }
        return '.../' + suffix;
      },

      sourceBranch(row) {
        return (row && row.branch) || 'detached';
      },

      targetBranch(row) {
        if (!row) return 'main';
        if (row.target_branch) return row.target_branch;
        const source = this.sourceBranch(row);
        if (source === 'session/' + row.session_name) return 'main';
        return source;
      },

      supportsDashboardMerge(row) {
        return !!row && row.repo_name === 'autonomy';
      },

      canMergeCommit(item) {
        return !!item && !!item.commit && this.supportsDashboardMerge(item.row) && item.position === 1;
      },

      mergeCommitLabel(item) {
        if (!item || !item.commit) return 'No commit selected';
        if (!this.supportsDashboardMerge(item.row)) return 'Review only for this repo';
        if (item.position !== 1) return 'Merge earlier commit first';
        return 'Merge ' + (item.commit.short_sha || this.shortSha(item.commit.sha)) + ' into ' + this.targetBranch(item.row);
      },

      isPoofingRow(row) {
        return !!row && this.poofingRowKey === this.rowKey(row);
      },

      isCleaningRow(row) {
        return !!(row && this.cleaning[this.rowKey(row)]);
      },

      discardPromptTitle(row) {
        if (!row) return '';
        return this.rowOrg(row).name + ' ' + row.session_name;
      },

      commitAt(row, index) {
        const commits = this.commitList(row);
        if (!commits.length) return null;
        const safeIndex = Math.max(0, Math.min(index, commits.length - 1));
        return {
          row,
          commit: commits[safeIndex],
          commitIndex: safeIndex,
          position: safeIndex + 1,
          total: commits.length,
          patchFiles: {},
        };
      },

      patchLinesForFile(view, file) {
        if (!view || !view.patchFiles || !file) return [];
        return view.patchFiles[file.path] || [];
      },

      diffMarker(kind) {
        if (kind === 'add') return '+';
        if (kind === 'del') return '-';
        return ' ';
      },

      async refresh(manual) {
        if (manual) this.refreshing = true;
        this.error = '';
        try {
          const resp = await fetch('/api/worktrees');
          this.rows = _normalizeRows(await _jsonOrError(resp));
          this.lastUpdated = new Date().toLocaleTimeString();
        } catch (err) {
          this.error = err.message || String(err);
          _toast('Worktree refresh failed: ' + this.error, 'error');
        } finally {
          this.loading = false;
          this.refreshing = false;
        }
      },

      async openCommitAt(row, index, options) {
        const opts = options || {};
        const next = this.commitAt(row, index);
        if (!next) {
          this.selectedCommit = null;
          return;
        }

        const preserveShowDiff = !!opts.preserveShowDiff;
        this.selectedDirtyRow = null;
        this.confirmDiscardRow = null;
        this.detailLoading = true;
        this.mergeState = 'idle';
        this.mergeBurstActive = false;
        if (!preserveShowDiff) this.showDiff = false;

        this.selectedCommit = next;
        const requestKey = this.commitKey(next);

        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/commits/' +
              encodeURIComponent(next.commit.sha),
          );
          const detail = await _jsonOrError(resp);
          if (!this.selectedCommit || this.commitKey(this.selectedCommit) !== requestKey) return;
          this.selectedCommit = {
            ...this.selectedCommit,
            commit: {
              ...next.commit,
              ...detail,
              files: detail.files || next.commit.files || [],
              stats: _commitStats(detail),
            },
            patchFiles: _patchIndex(detail.patch || ''),
          };
        } catch (err) {
          _toast('Commit detail failed: ' + (err.message || String(err)), 'error');
        } finally {
          if (this.selectedCommit && this.commitKey(this.selectedCommit) === requestKey) {
            this.detailLoading = false;
          }
        }
      },

      async selectCommit(item) {
        if (!item || !item.row) return;
        await this.openCommitAt(item.row, item.commitIndex || 0);
      },

      hasEarlierCommit() {
        return !!this.selectedCommit && this.selectedCommit.commitIndex > 0;
      },

      hasLaterCommit() {
        return !!this.selectedCommit && this.selectedCommit.commitIndex < (this.selectedCommit.total - 1);
      },

      async viewEarlierCommit() {
        if (!this.hasEarlierCommit()) return;
        await this.openCommitAt(this.selectedCommit.row, this.selectedCommit.commitIndex - 1, { preserveShowDiff: true });
      },

      async viewLaterCommit() {
        if (!this.hasLaterCommit()) return;
        await this.openCommitAt(this.selectedCommit.row, this.selectedCommit.commitIndex + 1, { preserveShowDiff: true });
      },

      async selectDirtyRow(row) {
        if (!row) return;
        this.selectedCommit = null;
        this.confirmDiscardRow = null;
        this.mergeState = 'idle';
        this.mergeBurstActive = false;
        this.showDiff = true;
        this.dirtyDetailLoading = true;
        this.selectedDirtyRow = {
          ...row,
          files: this.dirtyFiles(row),
          patchFiles: {},
        };
        const requestKey = this.rowKey(row);
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/changes',
          );
          const detail = await _jsonOrError(resp);
          if (!this.selectedDirtyRow || this.rowKey(this.selectedDirtyRow) !== requestKey) return;
          this.selectedDirtyRow = {
            ...row,
            files: detail.files || this.dirtyFiles(row),
            patchFiles: _patchIndex(detail.patch || ''),
          };
        } catch (err) {
          _toast('Dirty diff detail failed: ' + (err.message || String(err)), 'error');
        } finally {
          if (this.selectedDirtyRow && this.rowKey(this.selectedDirtyRow) === requestKey) {
            this.dirtyDetailLoading = false;
          }
        }
      },

      openDiscardConfirm(row) {
        if (!row) return;
        this.selectedCommit = null;
        this.selectedDirtyRow = null;
        this.confirmDiscardRow = row;
      },

      async confirmDiscard() {
        const row = this.confirmDiscardRow;
        if (!row || this.isCleaningRow(row)) return;
        const key = this.rowKey(row);
        this.cleaning = { ...this.cleaning, [key]: true };
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/discard',
            { method: 'POST' },
          );
          const data = await _jsonOrError(resp);
          const removed = (data.removed || []).length;
          _toast('Discarded ' + removed + ' worktree' + (removed === 1 ? '' : 's'), 'warning');
          this.confirmDiscardRow = null;
          await this.refresh(false);
        } catch (err) {
          _toast('Discard failed: ' + (err.message || String(err)), 'error');
        } finally {
          const next = { ...this.cleaning };
          delete next[key];
          this.cleaning = next;
        }
      },

      async startMergeCommit() {
        const item = this.selectedCommit;
        if (!this.canMergeCommit(item) || this.mergeState !== 'idle') return;
        this.mergeState = 'working';
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(item.row.session_name) + '/' +
              encodeURIComponent(item.row.repo_name) + '/commits/' +
              encodeURIComponent(item.commit.sha) + '/merge',
            { method: 'POST' },
          );
          await _jsonOrError(resp);
          this.mergeState = 'success';
          this.mergeBurstSeed += 1;
          this.mergeBurstActive = true;
          window.setTimeout(() => {
            this.mergeBurstActive = false;
          }, 860);

          const row = item.row;
          const rowKey = this.rowKey(row);
          window.setTimeout(() => {
            this.poofingRowKey = rowKey;
            window.setTimeout(async () => {
              const commits = this.commitList(row).slice();
              if (commits.length) {
                commits.shift();
                row.commits = commits;
                row.commits_ahead = commits.length;
                row.ff_eligible = commits.length > 0 && !row.is_dirty;
                this.rows = this.rows.slice();
              }

              this.poofingRowKey = '';
              this.mergeState = 'idle';
              _toast('Merged ' + (item.commit.short_sha || this.shortSha(item.commit.sha)), 'warning');

              if (commits.length) {
                await this.openCommitAt(row, 0, { preserveShowDiff: true });
              } else {
                this.selectedCommit = null;
              }
            }, 420);
          }, 520);
        } catch (err) {
          this.mergeState = 'idle';
          this.mergeBurstActive = false;
          _toast('Merge failed: ' + (err.message || String(err)), 'error');
          try {
            await this.refresh(false);
          } catch (_ignored) {
            // refresh already toasts on failure
          }
        }
      },

      init() {
        this.refresh(false);
        this._timer = setInterval(() => {
          if (this.selectedCommit || this.selectedDirtyRow || this.confirmDiscardRow || this.mergeState !== 'idle') {
            return;
          }
          this.refresh(false);
        }, 30000);
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
