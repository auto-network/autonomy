// Worktrees page Alpine component.
// Finalized commit-first review queue with full-screen commit/change review.

(function () {
  const _pathMeasureCanvas =
    typeof document !== 'undefined' ? document.createElement('canvas') : null;
  const _pathMeasureContext = _pathMeasureCanvas ? _pathMeasureCanvas.getContext('2d') : null;

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
      const err = new Error(data.message || data.error || ('HTTP ' + resp.status));
      err.status = resp.status;
      err.payload = data;
      throw err;
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
      clone_stale: !!row.clone_stale,
      rebase_required: !!row.rebase_required,
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

  function _escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function _diffLanguageForPath(path) {
    if (!path) return '';
    const name = String(path).split('/').pop().toLowerCase();
    const basenameMap = {
      'dockerfile': 'dockerfile',
      'makefile': 'makefile',
      'gnumakefile': 'makefile',
      'cmakelists.txt': 'cmake',
      'jenkinsfile': 'groovy',
    };
    if (basenameMap[name]) return basenameMap[name];

    const extensionMap = {
      'bash': 'bash',
      'c': 'c',
      'cc': 'cpp',
      'cpp': 'cpp',
      'cs': 'csharp',
      'css': 'css',
      'cxx': 'cpp',
      'go': 'go',
      'gradle': 'groovy',
      'groovy': 'groovy',
      'h': 'c',
      'hh': 'cpp',
      'hpp': 'cpp',
      'htm': 'xml',
      'html': 'xml',
      'ini': 'ini',
      'java': 'java',
      'js': 'javascript',
      'json': 'json',
      'jsx': 'javascript',
      'kt': 'kotlin',
      'kts': 'kotlin',
      'less': 'less',
      'lua': 'lua',
      'mjs': 'javascript',
      'md': 'markdown',
      'php': 'php',
      'properties': 'properties',
      'py': 'python',
      'rb': 'ruby',
      'rs': 'rust',
      'scss': 'scss',
      'sh': 'bash',
      'sql': 'sql',
      'svg': 'xml',
      'svelte': 'svelte',
      'toml': 'toml',
      'ts': 'typescript',
      'tsx': 'typescript',
      'txt': 'plaintext',
      'vue': 'xml',
      'xml': 'xml',
      'yaml': 'yaml',
      'yml': 'yaml',
      'zsh': 'bash',
    };
    const parts = name.split('.');
    for (let idx = parts.length - 1; idx >= 1; idx -= 1) {
      const language = extensionMap[parts[idx]];
      if (language) return language;
    }
    return '';
  }

  function _highlightDiffText(path, text) {
    if (text == null || text === '') return '&nbsp;';
    const source = String(text);
    const hljs = window.hljs;
    if (!hljs) return _escapeHtml(source);
    const language = _diffLanguageForPath(path);
    try {
      if (language && hljs.getLanguage(language)) {
        return hljs.highlight(source, { language, ignoreIllegals: true }).value || '&nbsp;';
      }
    } catch (_) {
      // Fall through to escaped plaintext below.
    }
    return _escapeHtml(source);
  }

  function _patchIndex(patch) {
    const index = {};
    _parseUnifiedPatch(patch).forEach(file => {
      index[file.path] = (file.lines || []).map(line => ({
        ...line,
        html: _highlightDiffText(file.path, line.text),
      }));
    });
    return index;
  }

  function _fontForElement(el) {
    const style = window.getComputedStyle(el);
    return (
      style.font ||
      [
        style.fontStyle,
        style.fontVariant,
        style.fontWeight,
        style.fontSize,
        style.fontFamily,
      ].join(' ')
    );
  }

  function _fitPathToWidth(path, width, el) {
    if (!path || !_pathMeasureContext || !width) return path || '';
    _pathMeasureContext.font = _fontForElement(el);
    if (_pathMeasureContext.measureText(path).width <= width) return path;

    const ellipsis = '...';
    if (_pathMeasureContext.measureText(ellipsis).width >= width) return ellipsis;

    let low = 0;
    let high = path.length;
    let best = ellipsis;

    while (low <= high) {
      const keep = Math.floor((low + high) / 2);
      const candidate = ellipsis + path.slice(path.length - keep);
      if (_pathMeasureContext.measureText(candidate).width <= width) {
        best = candidate;
        low = keep + 1;
      } else {
        high = keep - 1;
      }
    }

    return best;
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
      rebaseRequiredDialog: null,
      rebaseRequesting: false,
      cherryPicking: false,
      commitStickyTop: 0,
      commitFileRowStickyTop: 0,
      reviewTitlePinned: false,
      pathMeasureTick: 0,
      cleaning: {},
      syncingBase: {},
      _timer: null,
      _resizeHandler: null,
      _commitStickyResizeObserver: null,
      _reviewTitleObserver: null,
      _commitStickyRaf: 0,
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

      get uncommittedChangesCount() {
        return this.rows.filter(row => row.is_dirty).length;
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

      // ── source_control / autonomy/github capability bridge ────────
      // Backend exposes review state under row.source_control.review
      // (see auto-4ze9o). The settled design template uses a flatter
      // ``row.pr`` shape with ``state`` ∈ green|yellow. rowPr() adapts
      // between them so the design's HTML/Alpine bindings can be used
      // verbatim.
      rowPr(row) {
        const review = row && row.source_control && row.source_control.review;
        if (!review) return null;
        const mode = this.rowNagMode(row);
        return {
          number: review.number,
          url: review.url,
          title: review.title,
          body: review.body,
          state: review.aggregate_state,
          running: review.running,
          // animate-pulse fires only when the user opted in to nags AND
          // something is in flight (settled design's running-overlay
          // semantics — running alone is a state, watch_active is the
          // user's intent to be notified).
          watch_active: mode !== 'silent',
          pr_checks: review.checks || [],
        };
      },

      prIsFlashing(pr) {
        return !!pr && !!pr.watch_active && !!pr.running;
      },

      prBadgeClass(pr) {
        if (!pr) return 'border-white/10 bg-white/[0.03] text-slate-300';
        if (pr.state === 'green') return 'border-emerald-300/20 bg-emerald-300/10 text-emerald-100';
        return 'border-amber-300/20 bg-amber-300/10 text-amber-100';
      },

      prDotClass(pr) {
        if (!pr) return 'bg-slate-400';
        const tone = pr.state === 'green' ? 'bg-emerald-300' : 'bg-amber-200';
        return tone + (this.prIsFlashing(pr) ? ' animate-pulse' : '');
      },

      // Per-PR check list for the on-card navigator. Each entry has
      // ``{id, icon, label, status, detail}`` from the capability's
      // normalize_review_payload (settled design 3435e03f, lines 215-226).
      rowPrChecks(row) {
        const pr = this.rowPr(row);
        return (pr && pr.pr_checks) || [];
      },

      // ── Nag controls (Silent / Nag All Changes / Nag When Done) ─────
      // Backend mode strings (worktree_monitor.py NAG_*) are
      // 'silent' / 'nag_all' / 'nag_done'. UI labels match auto-r098a.
      rowNagMode(row) {
        const watch = row && row.source_control && row.source_control.watch;
        return (watch && watch.mode) || 'silent';
      },

      nagButtonClass(row, mode) {
        const active = this.rowNagMode(row) === mode;
        return active
          ? 'border-white/10 bg-white/10 text-white'
          : 'text-slate-400 hover:border-white/10 hover:bg-white/[0.05] hover:text-slate-200';
      },

      async setNagMode(row, mode) {
        if (!row) return;
        // Optimistic update — write the new mode locally so the click
        // feels instant; revert on error.
        const prev = this.rowNagMode(row);
        if (!row.source_control) row.source_control = {state: 'unavailable', review: null, watch: {mode}};
        if (!row.source_control.watch) row.source_control.watch = {mode};
        row.source_control.watch.mode = mode;
        try {
          const url = '/api/worktrees/'
            + encodeURIComponent(row.session_name) + '/'
            + encodeURIComponent(row.repo_name) + '/watch';
          const resp = await fetch(url, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode}),
          });
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
        } catch (err) {
          row.source_control.watch.mode = prev;
          _toast('Failed to update nag mode: ' + (err.message || err));
        }
      },

      // Per-commit checks. gh's statusCheckRollup is keyed to the PR's
      // head SHA, not individual commits, so the v1 capability surface
      // doesn't expose per-commit checks. Return [] until that lands.
      reviewCommitChecks(_commit) {
        return [];
      },

      // Disc styling for navigator check icons. Lifted verbatim from
      // the settled design's checkIconClass helper.
      checkIconClass(status) {
        if (status === 'pass') return 'border-emerald-300/20 bg-emerald-300/12 text-emerald-100';
        if (status === 'running') return 'border-amber-300/20 bg-amber-300/12 text-amber-100';
        if (status === 'pending') return 'border-white/10 bg-white/[0.03] text-slate-300';
        if (status === 'fail') return 'border-rose-300/20 bg-rose-300/12 text-rose-100';
        return 'border-white/10 bg-white/[0.03] text-slate-300';
      },

      // Compact "X commits" label used by the navigator card header.
      commitCountLabel(row) {
        const n = this.commitList(row).length;
        if (n === 0) return 'no commits';
        if (n === 1) return '1 commit';
        return n + ' commits';
      },

      // Navigator click handlers. Until the PR-mode review overlay lands
      // (separate chunk), all three route to the existing commit-review
      // path: PR row falls through to commit 0, individual rows pick the
      // chosen commit. The bindings are faithful to the settled design so
      // the PR-mode overlay can replace these without template churn.
      _itemForCommit(row, idx) {
        const commits = this.commitList(row);
        const commit = commits[idx];
        if (!commit) return null;
        return {
          row,
          commit,
          commitIndex: idx,
          position: idx + 1,
          total: commits.length,
        };
      },

      openReviewCommit(row, idx) {
        const item = this._itemForCommit(row, idx);
        if (item) this.selectCommit(item);
      },

      openReviewPr(row) {
        // PR-mode overlay arrives in a follow-up chunk. For now, open
        // the first commit as a stand-in so the PR row click is not a
        // dead button.
        this.openReviewCommit(row, 0);
      },

      openReviewDefault(row) {
        if (this.rowPr(row)) {
          this.openReviewPr(row);
        } else {
          this.openReviewCommit(row, 0);
        }
      },

      repoName(row) {
        return (row && row.repo_name) || 'unknown';
      },

      changesCompanionCommitLabel(row) {
        const count = this.commitList(row).length;
        if (!count) return 'changes only';
        return count === 1 ? '1 commit also present' : count + ' commits also present';
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

      fitPath(path, el) {
        void this.pathMeasureTick;
        if (!path || !el) return path || '';
        const width = Math.floor(el.getBoundingClientRect().width);
        if (!width) return path;
        return _fitPathToWidth(path, width, el);
      },

      queuePathMeasurements() {
        this.$nextTick(() => {
          this.pathMeasureTick += 1;
        });
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

      cloneStale(row) {
        return !!(row && row.clone_stale);
      },

      canDiscardDirtyRow(row) {
        return !!row && !row.session_live;
      },

      supportsDashboardMerge(row) {
        return !!row && row.repo_name === 'autonomy';
      },

      canMergeCommit(item) {
        return !!item &&
          !!item.commit &&
          this.supportsDashboardMerge(item.row) &&
          !this.cloneStale(item.row) &&
          item.position === 1 &&
          !!item.row.ff_eligible;
      },

      canRequestRebase(item) {
        return !!item &&
          this.supportsDashboardMerge(item.row) &&
          !this.cloneStale(item.row) &&
          item.position === 1 &&
          !!item.row.rebase_required &&
          !!item.row.session_live;
      },

      canCherryPick(item) {
        // Cherry-pick is the dead-session-friendly path: when FF isn't
        // possible (master moved past the worktree's fork point) but the
        // single ahead commit would auto-merge cleanly, surface an enabled
        // "Cherry Pick to <branch>" button. No session_live gate — the
        // whole point is to land orphaned commits from dead sessions.
        return !!item &&
          this.supportsDashboardMerge(item.row) &&
          !this.cloneStale(item.row) &&
          item.position === 1 &&
          !item.row.ff_eligible &&
          !!item.row.cherry_pick_eligible;
      },

      mergeDisabledReason(item) {
        if (!item || !item.commit) return 'No commit selected';
        if (!this.supportsDashboardMerge(item.row)) return 'Review only for this repo';
        if (this.cloneStale(item.row)) return 'Sync Worktree to Latest before merging';
        if (item.position !== 1) return 'Merge earlier commit first';
        if (item.row.rebase_required && item.row.is_dirty) {
          return 'Parent has advanced; stash or commit uncommitted changes before rebasing';
        }
        if (item.row.rebase_required) return 'Parent has advanced; rebase required before merge';
        return '';
      },

      isSyncingBase(row) {
        return !!(row && this.syncingBase[this.rowKey(row)]);
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

      syncOverlayRows() {
        if (this.selectedCommit) {
          const row = this.rows.find(item => this.rowKey(item) === this.rowKey(this.selectedCommit.row));
          if (!row) {
            this.selectedCommit = null;
          } else {
            const commits = this.commitList(row);
            if (!commits.length) {
              this.selectedCommit = null;
            } else {
              const nextIndex = Math.max(0, Math.min(this.selectedCommit.commitIndex || 0, commits.length - 1));
              const nextCommit = commits[nextIndex];
              const sameCommit = this.selectedCommit.commit && this.selectedCommit.commit.sha === nextCommit.sha;
              this.selectedCommit = {
                ...this.selectedCommit,
                row,
                commit: sameCommit
                  ? {
                    ...nextCommit,
                    patch: this.selectedCommit.commit.patch,
                    files: this.selectedCommit.commit.files || nextCommit.files,
                    body: this.selectedCommit.commit.body || nextCommit.body,
                  }
                  : nextCommit,
                commitIndex: nextIndex,
                position: nextIndex + 1,
                total: commits.length,
                patchFiles: sameCommit ? this.selectedCommit.patchFiles : {},
              };
            }
          }
        }

        if (this.selectedDirtyRow) {
          const row = this.rows.find(item => this.rowKey(item) === this.rowKey(this.selectedDirtyRow));
          if (!row || !row.is_dirty) {
            this.selectedDirtyRow = null;
          } else {
            this.selectedDirtyRow = {
              ...row,
              files: this.selectedDirtyRow.files || this.dirtyFiles(row),
              patchFiles: this.selectedDirtyRow.patchFiles || {},
            };
          }
        }

        if (this.confirmDiscardRow) {
          const row = this.rows.find(item => this.rowKey(item) === this.rowKey(this.confirmDiscardRow));
          this.confirmDiscardRow = row || null;
        }
      },

      patchLinesForFile(view, file) {
        if (!view || !view.patchFiles || !file) return [];
        return view.patchFiles[file.path] || [];
      },

      queueBranchLayouts() {
        this.$nextTick(() => {
          const root = this.$root;
          if (!root) return;
          root.querySelectorAll('[data-branch-row]').forEach((el) => {
            this.syncBranchLayout(el);
          });
        });
      },

      syncBranchLayout(el) {
        if (!el) return;
        const src = el.querySelector('[data-branch-part="src"]');
        const dst = el.querySelector('[data-branch-part="dst"]');
        const arrow = el.querySelector('[data-branch-part="arrow"]');
        if (!src || !dst) return;
        const available = el.clientWidth;
        if (!available) return;
        const forceStacked = window.matchMedia('(max-width: 639px)').matches;

        const styles = window.getComputedStyle(el);
        const gap = Number.parseFloat(styles.columnGap || styles.gap || '0') || 0;
        const totalWidth =
          src.getBoundingClientRect().width +
          dst.getBoundingClientRect().width +
          (arrow ? arrow.getBoundingClientRect().width : 0) +
          gap * (arrow ? 2 : 1);
        const inlineFits = !forceStacked && totalWidth <= (available + 1);

        el.dataset.branchLayout = inlineFits ? 'inline' : 'stacked';
        el.style.flexDirection = inlineFits ? 'row' : 'column';
        el.style.alignItems = inlineFits ? 'center' : 'flex-start';
        el.style.flexWrap = 'nowrap';
        if (arrow) {
          arrow.style.display = inlineFits ? 'inline' : 'none';
        }
      },

      updateCommitStickyOffsets() {
        const titleBar = this.$refs.commitTitleBar || this.$refs.dirtyTitleBar;
        const filesHeader = this.$refs.commitFilesHeader || this.$refs.dirtyFilesHeader;
        this.commitStickyTop = titleBar ? Math.max(0, Math.round(titleBar.getBoundingClientRect().height)) : 0;
        this.commitFileRowStickyTop = this.commitStickyTop + (filesHeader ? filesHeader.getBoundingClientRect().height : 0);
      },

      queueCommitStickyOffsets() {
        this.$nextTick(() => {
          this.updateCommitStickyOffsets();
          this.observeCommitStickyElements();
        });
      },

      queueReviewHeaderState() {
        this.$nextTick(() => {
          this.observeReviewTitleSentinel();
          this.syncReviewHeaderState();
        });
      },

      syncReviewHeaderState() {
        const scroller = this.$refs.commitDetailScroller || this.$refs.dirtyDetailScroller;
        const titleBar = this.$refs.commitTitleBar || this.$refs.dirtyTitleBar;
        if (!scroller || !titleBar) return;
        if (this._reviewTitleObserver) {
          this.updateCommitStickyOffsets();
          return;
        }
        const isMobile = window.matchMedia('(max-width: 639px)').matches;
        const enterCompactAt = titleBar.offsetTop - 1;
        const exitCompactAt = enterCompactAt - 48;
        let compact = false;
        if (isMobile) {
          compact = this.reviewTitlePinned
            ? scroller.scrollTop >= exitCompactAt
            : scroller.scrollTop >= enterCompactAt;
        }
        if (this.reviewTitlePinned !== compact) {
          this.reviewTitlePinned = compact;
          this.queueCommitStickyOffsets();
          return;
        }
        this.updateCommitStickyOffsets();
      },

      observeReviewTitleSentinel() {
        this.disconnectReviewTitleObserver();
        if (typeof window.IntersectionObserver !== 'function') return;
        const isMobile = window.matchMedia('(max-width: 639px)').matches;
        if (!isMobile) {
          this.reviewTitlePinned = false;
          return;
        }

        const scroller = this.$refs.commitDetailScroller || this.$refs.dirtyDetailScroller;
        const sentinel = this.$refs.commitTitleSentinel || this.$refs.dirtyTitleSentinel;
        if (!scroller || !sentinel) return;

        this._reviewTitleObserver = new window.IntersectionObserver((entries) => {
          const entry = entries && entries[0];
          if (!entry) return;
          const rootTop = entry.rootBounds ? entry.rootBounds.top : 0;
          const compact = !entry.isIntersecting && entry.boundingClientRect.top < rootTop;
          if (this.reviewTitlePinned !== compact) {
            this.reviewTitlePinned = compact;
            this.queueCommitStickyOffsets();
            return;
          }
          this.updateCommitStickyOffsets();
        }, {
          root: scroller,
          threshold: [0, 1],
        });
        this._reviewTitleObserver.observe(sentinel);
      },

      scheduleCommitStickyOffsetUpdate() {
        if (this._commitStickyRaf) return;
        this._commitStickyRaf = window.requestAnimationFrame(() => {
          this._commitStickyRaf = 0;
          this.updateCommitStickyOffsets();
        });
      },

      observeCommitStickyElements() {
        this.disconnectCommitStickyObserver();
        if (typeof window.ResizeObserver !== 'function') return;
        const titleBar = this.$refs.commitTitleBar || this.$refs.dirtyTitleBar;
        const filesHeader = this.$refs.commitFilesHeader || this.$refs.dirtyFilesHeader;
        if (!titleBar && !filesHeader) return;
        this._commitStickyResizeObserver = new window.ResizeObserver(() => {
          this.scheduleCommitStickyOffsetUpdate();
        });
        if (titleBar) this._commitStickyResizeObserver.observe(titleBar);
        if (filesHeader) this._commitStickyResizeObserver.observe(filesHeader);
      },

      disconnectCommitStickyObserver() {
        if (this._commitStickyResizeObserver) {
          this._commitStickyResizeObserver.disconnect();
          this._commitStickyResizeObserver = null;
        }
        if (this._commitStickyRaf) {
          window.cancelAnimationFrame(this._commitStickyRaf);
          this._commitStickyRaf = 0;
        }
      },

      disconnectReviewTitleObserver() {
        if (this._reviewTitleObserver) {
          this._reviewTitleObserver.disconnect();
          this._reviewTitleObserver = null;
        }
      },

      hasOverlayOpen() {
        return !!(this.selectedCommit || this.selectedDirtyRow || this.confirmDiscardRow || this.rebaseRequiredDialog);
      },

      syncScrollLock() {
        const locked = this.hasOverlayOpen();
        document.documentElement.style.overflow = locked ? 'hidden' : '';
        document.body.style.overflow = locked ? 'hidden' : '';
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
          const resp = manual
            ? await fetch('/api/worktrees/refresh', { method: 'POST' })
            : await fetch('/api/worktrees');
          this.rows = _normalizeRows(await _jsonOrError(resp));
          this.syncOverlayRows();
          this.lastUpdated = new Date().toLocaleTimeString();
          this.queueBranchLayouts();
          this.queuePathMeasurements();
          this.queueCommitStickyOffsets();
          this.queueReviewHeaderState();
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
        this.reviewTitlePinned = false;
        if (!preserveShowDiff) this.showDiff = false;

        this.selectedCommit = next;
        this.queueCommitStickyOffsets();
        this.queueBranchLayouts();
        this.queuePathMeasurements();
        this.queueReviewHeaderState();
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
          this.queueCommitStickyOffsets();
          this.queueBranchLayouts();
          this.queuePathMeasurements();
          this.queueReviewHeaderState();
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
        this.reviewTitlePinned = false;
        this.showDiff = true;
        this.dirtyDetailLoading = true;
        this.selectedDirtyRow = {
          ...row,
          files: this.dirtyFiles(row),
          patchFiles: {},
        };
        this.queueCommitStickyOffsets();
        this.queuePathMeasurements();
        this.queueReviewHeaderState();
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
          this.queueCommitStickyOffsets();
          this.queuePathMeasurements();
          this.queueReviewHeaderState();
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
        this.rebaseRequiredDialog = null;
        this.confirmDiscardRow = row;
      },

      async syncBase(row) {
        if (!row || this.isSyncingBase(row)) return;
        const key = this.rowKey(row);
        this.syncingBase = { ...this.syncingBase, [key]: true };
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/sync-base',
            { method: 'POST' },
          );
          await _jsonOrError(resp);
          _toast('Worktree base synced to latest ' + this.targetBranch(row), 'warning');
          await this.refresh(false);
        } catch (err) {
          _toast('Sync failed: ' + (err.message || String(err)), 'error');
        } finally {
          const next = { ...this.syncingBase };
          delete next[key];
          this.syncingBase = next;
        }
      },

      openRebaseRequiredDialog(item, payload) {
        this.rebaseRequiredDialog = {
          row: item.row,
          message: payload.message || 'Rebase required before merge.',
          session_live: !!payload.session_live,
        };
      },

      async cherryPickCommit(row) {
        if (!row || this.cherryPicking) return;
        this.cherryPicking = true;
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/cherry-pick',
            { method: 'POST' },
          );
          const data = await _jsonOrError(resp);

          // Same celebration shape as a successful FF merge — increment the
          // seed (forces a re-trigger even if the user fires twice in a row),
          // flash mergeBurstActive for the confetti window, then poof the
          // row. Refresh reconciles backend state after the animation lands.
          this.mergeBurstSeed += 1;
          this.mergeBurstActive = true;
          window.setTimeout(() => { this.mergeBurstActive = false; }, 860);
          const rowKey = this.rowKey(row);
          window.setTimeout(() => {
            this.poofingRowKey = rowKey;
            window.setTimeout(() => {
              this.poofingRowKey = '';
            }, 420);
          }, 520);

          _toast('Cherry-picked ' + (data.commit || '').slice(0, 8) +
                 ' to ' + this.targetBranch(row), 'success');
          await this.refresh(false);
        } catch (err) {
          _toast('Cherry-pick failed: ' + (err.message || String(err)), 'error');
        } finally {
          this.cherryPicking = false;
        }
      },

      async requestRebase(row) {
        if (!row || this.rebaseRequesting) return;
        this.rebaseRequesting = true;
        try {
          const resp = await fetch(
            '/api/worktrees/' + encodeURIComponent(row.session_name) + '/' +
              encodeURIComponent(row.repo_name) + '/request-rebase',
            { method: 'POST' },
          );
          await _jsonOrError(resp);
          _toast('Rebase request sent to ' + row.session_name, 'warning');
          this.rebaseRequiredDialog = null;
          await this.refresh(false);
        } catch (err) {
          _toast('Request Rebase failed: ' + (err.message || String(err)), 'error');
        } finally {
          this.rebaseRequesting = false;
        }
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
                row.ff_eligible = commits.length > 0;
                row.rebase_required = false;
                this.rows = this.rows.slice();
                this.queueBranchLayouts();
                this.queuePathMeasurements();
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
          if (err && err.payload && err.payload.error === 'rebase_required') {
            this.openRebaseRequiredDialog(item, err.payload);
          } else {
            _toast('Merge failed: ' + (err.message || String(err)), 'error');
          }
          try {
            await this.refresh(false);
          } catch (_ignored) {
            // refresh already toasts on failure
          }
        }
      },

      init() {
        this.refresh(false).then(() => this._handleDeeplink());
        this.$watch('selectedCommit', (value) => {
          if (!value) {
            this.reviewTitlePinned = false;
            this.disconnectCommitStickyObserver();
            this.disconnectReviewTitleObserver();
          }
          this.syncScrollLock();
        });
        this.$watch('selectedDirtyRow', (value) => {
          if (!value) {
            this.reviewTitlePinned = false;
            this.disconnectCommitStickyObserver();
            this.disconnectReviewTitleObserver();
          }
          this.syncScrollLock();
        });
        this.$watch('confirmDiscardRow', () => {
          this.syncScrollLock();
        });
        this.$watch('rebaseRequiredDialog', () => {
          this.syncScrollLock();
        });
        this._resizeHandler = () => {
          if (this.selectedCommit || this.selectedDirtyRow) this.queueCommitStickyOffsets();
          this.queueBranchLayouts();
          this.queuePathMeasurements();
          this.queueReviewHeaderState();
        };
        window.addEventListener('resize', this._resizeHandler);
        this._timer = setInterval(() => {
          if (this.selectedCommit || this.selectedDirtyRow || this.confirmDiscardRow || this.mergeState !== 'idle') {
            return;
          }
          this.refresh(false);
        }, 30000);
      },

      // Deeplink entry point. The session-viewer's workspace-changes
      // anchor lands here with ``?session=<tmux_name>``; auto-open the
      // review screen for that session — commits-ahead first (the
      // ready-to-merge case, opened at the oldest commit so the
      // review walks chronologically toward HEAD), falling back to
      // dirty-files when there are no commits. Multi-repo sessions:
      // take the first matching row; the operator can hop to
      // siblings via the in-page nav.
      _handleDeeplink() {
        const params = new URLSearchParams(window.location.search);
        const target = params.get('session');
        if (!target) return;
        const matches = this.rows.filter((r) => r.session_name === target);
        if (!matches.length) return;
        const withCommits = matches.find((r) => (this.commitList(r) || []).length > 0);
        if (withCommits) {
          this.openCommitAt(withCommits, 0);
          return;
        }
        const dirtyMatch = matches.find((r) => r.is_dirty);
        if (dirtyMatch) this.selectDirtyRow(dirtyMatch);
      },

      destroy() {
        this.disconnectCommitStickyObserver();
        this.disconnectReviewTitleObserver();
        document.documentElement.style.overflow = '';
        document.body.style.overflow = '';
        if (this._timer) {
          clearInterval(this._timer);
          this._timer = null;
        }
        if (this._resizeHandler) {
          window.removeEventListener('resize', this._resizeHandler);
          this._resizeHandler = null;
        }
      },
    }));
  });
})();
