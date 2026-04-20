// Shared session entry renderer — 32 methods + constants extracted from
// session-viewer.js, chatwith-panel.js, and live-panel-viewer.js.
//
// Usage: spread into an Alpine data component:
//   Alpine.data('myComponent', () => ({ ...window.SessionRenderer, ... }))
//
// Required component state (must be defined by the consuming component):
//   entries, displayEntries, autoScroll,
//   _resultMap, _expanded, _expandView,
//   _groupExpanded, _groupExpandView, attachments
//
// Required $refs (must be defined in the consuming template):
//   entriesContainer — the scrollable entries div

(function () {

  // ── Tool color tables ────────────────────────────────────────
  const TOOL_CHIPS = {
    Bash:  'sc-chip-bash',
    Read:  'sc-chip-read',
    Write: 'sc-chip-write',
    Edit:  'sc-chip-edit',
    Grep:  'sc-chip-grep',
    Glob:  'sc-chip-glob',
    Agent: 'sc-chip-agent',
  };
  const TOOL_BORDERS = {
    Bash:  'sc-border-bash',
    Read:  'sc-border-read',
    Write: 'sc-border-write',
    Edit:  'sc-border-edit',
    Grep:  'sc-border-grep',
    Glob:  'sc-border-glob',
    Agent: 'sc-border-agent',
  };

  window.SessionRenderer = {

    // ── Render helpers ─────────────────────────────────────────

    formatTime(ts) {
      if (!ts) return '';
      try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
    },

    fmtDuration(seconds) {
      if (seconds == null || seconds < 0) return '';
      if (seconds < 1) return Math.round(seconds * 1000) + 'ms';
      if (seconds < 60) return seconds.toFixed(1) + 's';
      return Math.floor(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's';
    },

    chipClass(toolName) {
      return TOOL_CHIPS[toolName] || 'sc-chip-default';
    },

    borderClass(entry) {
      if (entry.type === 'tool_use' || entry.type === 'tool_group') return TOOL_BORDERS[entry.tool_name] || 'sc-border-default';
      if (entry.type === 'user') return 'sc-border-user';
      if (entry.type === 'assistant_text') return 'sc-border-assistant';
      if (entry.type === 'thinking') return 'sc-border-thinking';
      if (entry.type === 'system') return 'sc-border-system';
      if (entry.type === 'compact_summary') return 'sc-border-compact-summary';
      if (entry.type === 'crosstalk') return 'sc-border-crosstalk';
      if (entry.type === 'semantic_bash') return 'sc-border-default';
      return 'sc-border-default';
    },

    headline(entry) {
      if (entry.type !== 'tool_use') return '';
      const inp = entry.input || {};
      const name = entry.tool_name || '';
      switch (name) {
        case 'Bash':
          return inp.description || inp.command || '';
        case 'Read':
          return this._smartPath(inp.file_path || '');
        case 'Write':
          return this._smartPath(inp.file_path || '');
        case 'Edit':
          return this._smartPath(inp.file_path || '');
        case 'Grep':
          return (inp.pattern || '') + (inp.path ? ' in ' + this._smartPath(inp.path) : '');
        case 'Glob':
          return inp.pattern || '';
        case 'Agent':
          return inp.description || inp.prompt?.slice(0, 60) || '';
        case 'TaskCreate': {
          // Server-annotated subject resolves the rename history deterministically.
          const ann = entry.todo_annotation || {};
          return ann.subject || inp.subject || '';
        }
        case 'TaskUpdate': {
          const ann = entry.todo_annotation || {};
          // Fallback to taskId only when the server-side tracker couldn't resolve
          // the subject (e.g. partial-history HTTP poll for a stale client).
          return ann.subject || (inp.taskId ? 'Task #' + inp.taskId : '');
        }
        default:
          // Generic: show first string value from input
          for (const v of Object.values(inp)) {
            if (typeof v === 'string' && v.length > 0) return v.slice(0, 80);
          }
          return name;
      }
    },

    /** Task* tiles: status icon + class for the leading indicator. */
    todoStatusIcon(entry) {
      const status = (entry.todo_annotation && entry.todo_annotation.status) || '';
      if (status === 'completed') return '\u2713';
      if (status === 'in_progress') return '';  // spinner via CSS ::before
      return '';  // pending — hollow circle via CSS
    },

    todoStatusClass(entry) {
      const status = (entry.todo_annotation && entry.todo_annotation.status) || 'pending';
      return 'todo-status todo-status--' + status.replace(/[^a-z_]/g, '');
    },

    /** Flowing-body text shown when a Task* tile is expanded. */
    todoBodyText(entry) {
      const ann = entry.todo_annotation || {};
      return ann.description || ann.activeForm || ann.subject || '';
    },

    hasTodoAnnotation(entry) {
      return !!(entry && entry.todo_annotation);
    },

    /** Returns true if a tool_use entry has no corresponding result yet.
     *  Uses server-provided pendingToolIds when the session is in tool_running
     *  state. Dead sessions cannot have running tools. Falls back to local
     *  _resultMap for history/replay and during initial load.
     */
    isToolRunning(entry) {
      if (entry.type !== 'tool_use') return false;
      var store = Alpine.store('sessions')[this.sessionKey];
      if (store) {
        // Dead sessions: no tool can be running (killed mid-flight)
        if (store.activityState === 'dead') return false;
        // Server says tools are running — check the server-provided set
        if (store.activityState === 'tool_running') {
          return !!store.pendingToolIds[entry.tool_id];
        }
      }
      // Fallback to local resultMap (history, initial load, non-tool_running states)
      return !this._resultMap[entry.tool_id];
    },

    /** Elapsed seconds since entry.timestamp (for running tools). */
    _elapsed(entry) {
      if (!entry.timestamp) return null;
      try {
        var diff = (Date.now() - new Date(entry.timestamp).getTime()) / 1000;
        return diff >= 0 ? diff : null;
      } catch (_) { return null; }
    },

    metaDisplay(entry) {
      if (entry.type !== 'tool_use') return [];
      const name = entry.tool_name || '';
      const result = this._resultMap[entry.tool_id];
      const badges = [];

      // Dead session: unmatched tools show "Killed" instead of "Running"
      if (!result) {
        var store = Alpine.store('sessions')[this.sessionKey];
        if (store && store.activityState === 'dead') {
          badges.push({ text: 'Killed', cls: 'sc-meta-error' });
          return badges;
        }
      }

      // Running tool: show elapsed time + "Running" badge
      // Touch _tick to force Alpine re-evaluation every second
      if (!result) {
        void this._tick;
        const elapsed = this._elapsed(entry);
        if (elapsed != null) badges.push({ text: this.fmtDuration(elapsed), cls: 'sc-meta-running' });
        badges.push({ text: 'Running', cls: 'sc-meta-running' });
        return badges;
      }

      switch (name) {
        case 'Bash': {
          const dur = this._duration(entry, result);
          if (dur != null) badges.push({ text: this.fmtDuration(dur), cls: 'sc-meta-gray' });
          if (result && result.is_error) badges.push({ text: '\u2717', cls: 'sc-meta-red' });
          else if (result && !result.is_error) badges.push({ text: '\u2713', cls: 'sc-meta-green' });
          break;
        }
        case 'Read': {
          if (result && result.content) {
            const n = this._countLines(result.content);
            badges.push({ text: '+' + n, cls: 'sc-meta-green' });
          }
          break;
        }
        case 'Write': {
          const inp = entry.input || {};
          if (inp.content) {
            const n = this._countLines(inp.content);
            badges.push({ text: '+' + n, cls: 'sc-meta-green' });
          }
          break;
        }
        case 'Edit': {
          const inp = entry.input || {};
          if (inp.new_string) {
            const added = this._countLines(inp.new_string);
            badges.push({ text: '+' + added, cls: 'sc-meta-green' });
          }
          if (inp.old_string) {
            const removed = this._countLines(inp.old_string);
            badges.push({ text: '\u2212' + removed, cls: 'sc-meta-red' });
          }
          break;
        }
        case 'Agent': {
          const dur = this._duration(entry, result);
          if (dur != null) badges.push({ text: this.fmtDuration(dur), cls: 'sc-meta-gray' });
          if (result && result.tool_calls != null) {
            badges.push({ text: result.tool_calls + ' calls', cls: 'sc-meta-gray' });
          }
          break;
        }
        case 'TaskCreate':
          // Leading chip is the tool label; a status badge is redundant at creation.
          break;
        case 'TaskUpdate': {
          // Leading chip is hidden by the template (see session-entries.html);
          // the status icon rendered by todoStatusIcon() serves as the indicator.
          const ann = entry.todo_annotation || entry.input || {};
          const status = ann.status || '';
          if (status && status !== 'pending') {
            badges.push({
              text: status.replace('_', ' '),
              cls: status === 'completed' ? 'sc-meta-green' : 'sc-meta-running',
            });
          }
          break;
        }
      }
      return badges;
    },

    hasOutput(entry) {
      if (!entry || entry.type !== 'tool_use') return false;
      const result = this._resultMap[entry.tool_id];
      return !!(result && result.content);
    },

    expandViewMode(idx, entry) {
      const stored = this._expandView[idx];
      if (stored) return stored;
      return this.hasOutput(entry) ? 'output' : 'input';
    },

    toggleView(idx, entry) {
      if (!this.hasOutput(entry)) return; // no output → no toggle
      const current = this.expandViewMode(idx, entry);
      this._expandView[idx] = current === 'output' ? 'input' : 'output';
      this._expandView = { ...this._expandView };
    },

    expandContent(entry, idx) {
      if (entry.type === 'tool_use') {
        const result = this._resultMap[entry.tool_id];
        const mode = this.expandViewMode(idx, entry);
        if (mode === 'input') return this._inputSummary(entry);
        if (result && result.content) return result.content;
        return this._inputSummary(entry);
      }
      if (entry.type === 'thinking') return entry.content || '';
      if (entry.type === 'system') return entry.content || '';
      return '';
    },

    _inputSummary(entry) {
      const inp = entry.input || {};
      const name = entry.tool_name || '';
      switch (name) {
        case 'Bash':
          return inp.command || '';
        case 'Edit': {
          const parts = [];
          if (inp.old_string) parts.push('--- old\n' + inp.old_string);
          if (inp.new_string) parts.push('+++ new\n' + inp.new_string);
          return parts.join('\n\n') || inp.file_path || '';
        }
        case 'Read':
          return inp.file_path || '';
        case 'Write':
          return (inp.file_path || '') + (inp.content ? '\n' + inp.content : '');
        case 'Grep':
          return (inp.pattern || '') + (inp.path ? '\nin ' + inp.path : '');
        case 'Glob':
          return (inp.pattern || '') + (inp.path ? '\nin ' + inp.path : '');
        case 'Agent':
          return inp.prompt || '';
        case 'TaskCreate':
        case 'TaskUpdate': {
          const ann = entry.todo_annotation || {};
          return ann.description || ann.subject || inp.description || inp.subject || '';
        }
        default: {
          const parts = [];
          for (const [k, v] of Object.entries(inp)) {
            if (typeof v === 'string' && v.length > 0 && k !== 'description') {
              parts.push(v);
            }
          }
          return parts.join('\n') || name;
        }
      }
    },

    isExpanded(idx) {
      return !!this._expanded[idx];
    },

    toggleExpand(idx) {
      this._expanded[idx] = !this._expanded[idx];
      // Force Alpine reactivity
      this._expanded = { ...this._expanded };
    },

    /** Returns true if a role-transition gap should precede this entry. */
    hasGap(idx) {
      if (idx === 0) return false;
      const prevD = this.displayEntries[idx - 1];
      const currD = this.displayEntries[idx];
      if (!prevD || !currD) return false;
      // Resolve descriptors to get actual entry types
      const prev = window.SessionDisplay.resolve(prevD, this.entries);
      const curr = window.SessionDisplay.resolve(currD, this.entries);
      if (!prev || !curr) return false;
      const prevIsUser = prev.type === 'user';
      const currIsUser = curr.type === 'user';
      return prevIsUser !== currIsUser;
    },

    /** System entry: success or failure icon. */
    sysIcon(entry) {
      const c = (entry.content || '').toLowerCase();
      if (c.includes('fail') || c.includes('error') || c.includes('block')) return '\u2717';
      return '\u2713';
    },

    sysIconColor(entry) {
      const c = (entry.content || '').toLowerCase();
      if (c.includes('fail') || c.includes('error') || c.includes('block')) return 'color: #ef4444';
      return 'color: #22c55e';
    },

    // ── Group card helpers ──────────────────────────────────────

    /** Aggregate metadata badges for a tool group. */
    groupMeta(group) {
      const badges = [];
      switch (group.tool_name) {
        case 'Bash': {
          let total = 0, has = false;
          for (const item of group.items) {
            const dur = this._duration(item, this._resultMap[item.tool_id]);
            if (dur != null) { total += dur; has = true; }
          }
          if (has) badges.push({ text: this.fmtDuration(total), cls: 'sc-meta-gray' });
          break;
        }
        case 'Read': {
          let total = 0, has = false;
          for (const item of group.items) {
            const r = this._resultMap[item.tool_id];
            if (r && r.content) { total += this._countLines(r.content); has = true; }
          }
          if (has) badges.push({ text: '+' + total, cls: 'sc-meta-green' });
          break;
        }
        case 'Edit': {
          let added = 0, removed = 0;
          for (const item of group.items) {
            const inp = item.input || {};
            if (inp.new_string) added += this._countLines(inp.new_string);
            if (inp.old_string) removed += this._countLines(inp.old_string);
          }
          if (added) badges.push({ text: '+' + added, cls: 'sc-meta-green' });
          if (removed) badges.push({ text: '\u2212' + removed, cls: 'sc-meta-red' });
          break;
        }
        // Grep, Glob: no badge
      }
      return badges;
    },

    bashStatus(item) {
      const r = this._resultMap[item.tool_id];
      if (!r) return '';
      return r.is_error ? '\u2717' : '\u2713';
    },

    bashStatusColor(item) {
      const r = this._resultMap[item.tool_id];
      if (!r) return '';
      return r.is_error ? 'color: #ef4444' : 'color: #22c55e';
    },

    toggleGroupItem(dIdx, subIdx) {
      const cur = this._groupExpanded[dIdx];
      this._groupExpanded[dIdx] = (cur === subIdx) ? null : subIdx;
      this._groupExpanded = { ...this._groupExpanded };
    },

    isGroupItemExpanded(dIdx, subIdx) {
      return this._groupExpanded[dIdx] === subIdx;
    },

    groupItemViewMode(dIdx, subIdx, item) {
      const stored = this._groupExpandView[dIdx + '-' + subIdx];
      if (stored) return stored;
      return this.hasOutput(item) ? 'output' : 'input';
    },

    toggleGroupItemView(dIdx, subIdx, item) {
      if (!this.hasOutput(item)) return;
      const key = dIdx + '-' + subIdx;
      const current = this.groupItemViewMode(dIdx, subIdx, item);
      this._groupExpandView[key] = current === 'output' ? 'input' : 'output';
      this._groupExpandView = { ...this._groupExpandView };
    },

    groupItemExpandContent(item, dIdx, subIdx) {
      const mode = this.groupItemViewMode(dIdx, subIdx, item);
      if (mode === 'input') return this._inputSummary(item);
      const result = this._resultMap[item.tool_id];
      if (result && result.content) return result.content;
      return this._inputSummary(item);
    },

    // ── Internal helpers ───────────────────────────────────────

    resolveEntry(dEntry) {
      return window.SessionDisplay.resolve(dEntry, this.entries);
    },

    _rebuildDisplay() {
      this.displayEntries = window.SessionDisplay.buildAll(this.entries);
    },

    _smartPath(path) {
      if (!path || typeof path !== 'string') return '';
      // Strip common prefix
      let p = path.replace(/^\/workspace\/repo\//, '');
      // Collapse leading segments if too long
      if (p.length > 40) {
        const parts = p.split('/');
        if (parts.length > 2) {
          const file = parts[parts.length - 1];
          const dir = parts[parts.length - 2];
          p = '\u2026/' + dir + '/' + file;
        }
      }
      return p;
    },

    _duration(entry, result) {
      if (!result || !entry.timestamp || !result.timestamp) return null;
      try {
        const t0 = new Date(entry.timestamp).getTime();
        const t1 = new Date(result.timestamp).getTime();
        const diff = (t1 - t0) / 1000;
        return diff >= 0 ? diff : null;
      } catch (_) { return null; }
    },

    /** Returns true when the agent is generating a response (thinking/tool_running).
     *  Uses server-provided activityState when available. Falls back to local
     *  entries scan for history pages where server state is unavailable.
     */
    isAgentWorking() {
      if (!this.isLive) return false;
      // Server-provided activity state takes precedence
      var store = Alpine.store('sessions')[this.sessionKey];
      if (store && store.activityState) {
        var st = store.activityState;
        return st === 'thinking' || st === 'tool_running';
      }
      // Fallback to local entries scan
      var entries = this.entries;
      if (!entries || entries.length === 0) return false;
      var last = entries[entries.length - 1];
      if (last.type === 'user' || last.type === 'crosstalk') return true;
      if (last.type === 'tool_result') return true;
      if (last.type === 'tool_use' && !this._resultMap[last.tool_id]) return true;
      return false;
    },

    _countLines(str) {
      if (!str) return 0;
      // Count newlines + 1 (unless empty)
      let n = 1;
      for (let i = 0; i < str.length; i++) {
        if (str.charCodeAt(i) === 10) n++;
      }
      return n;
    },

    onScroll() {
      const el = this.$refs.entriesContainer;
      if (!el) return;
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
      this.autoScroll = atBottom;
    },

    resumeScroll() {
      this.autoScroll = true;
      const el = this.$refs.entriesContainer;
      if (el) el.scrollTop = el.scrollHeight;
    },

    removeAttachment(id) {
      this.attachments = this.attachments.filter(a => a.id !== id);
    },

    clearAttachments() {
      this.attachments = [];
    },

  };
})();
