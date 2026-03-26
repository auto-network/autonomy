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

    // ── Constants ───────────────────────────────────────────────

    _GROUPABLE: new Set(['Bash', 'Read', 'Edit', 'Grep', 'Glob']),

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
        default:
          // Generic: show first string value from input
          for (const v of Object.values(inp)) {
            if (typeof v === 'string' && v.length > 0) return v.slice(0, 80);
          }
          return name;
      }
    },

    metaDisplay(entry) {
      if (entry.type !== 'tool_use') return [];
      const name = entry.tool_name || '';
      const result = this._resultMap[entry.tool_id];
      const badges = [];

      switch (name) {
        case 'Bash': {
          const dur = this._duration(entry, result);
          if (dur != null) badges.push({ text: this.fmtDuration(dur), cls: 'sc-meta-gray' });
          if (result && result.is_error) badges.push({ text: '\u2717', cls: 'sc-meta-red' });
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
      }
      return badges;
    },

    expandViewMode(idx) {
      return this._expandView[idx] || 'output';
    },

    toggleView(idx) {
      const current = this._expandView[idx] || 'output';
      this._expandView[idx] = current === 'output' ? 'input' : 'output';
      this._expandView = { ...this._expandView };
    },

    expandContent(entry, idx) {
      if (entry.type === 'tool_use') {
        const result = this._resultMap[entry.tool_id];
        const mode = this._expandView[idx] || 'output';

        if (mode === 'input') {
          return this._inputSummary(entry);
        }
        // Output mode: show result content, fallback to input summary
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
      const prev = this.displayEntries[idx - 1];
      const curr = this.displayEntries[idx];
      if (!prev || !curr) return false;
      // Skip hidden entries (tool_result) for gap calculation
      // Gap when transitioning from user to non-user, or non-user to user
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

    groupItemViewMode(dIdx, subIdx) {
      return this._groupExpandView[dIdx + '-' + subIdx] || 'output';
    },

    toggleGroupItemView(dIdx, subIdx) {
      const key = dIdx + '-' + subIdx;
      this._groupExpandView[key] = (this._groupExpandView[key] || 'output') === 'output' ? 'input' : 'output';
      this._groupExpandView = { ...this._groupExpandView };
    },

    groupItemExpandContent(item, dIdx, subIdx) {
      const mode = this._groupExpandView[dIdx + '-' + subIdx] || 'output';
      if (mode === 'input') return this._inputSummary(item);
      const result = this._resultMap[item.tool_id];
      if (result && result.content) return result.content;
      return this._inputSummary(item);
    },

    // ── Internal helpers ───────────────────────────────────────

    _buildDisplayEntries() {
      const out = [];
      const entries = this.entries;
      let i = 0;
      while (i < entries.length) {
        const e = entries[i];
        if (e.type === 'tool_use' && this._GROUPABLE.has(e.tool_name)) {
          let j = i + 1;
          while (j < entries.length &&
                 entries[j].type === 'tool_use' &&
                 entries[j].tool_name === e.tool_name) {
            j++;
          }
          if (j - i >= 2) {
            out.push({
              type: 'tool_group',
              tool_name: e.tool_name,
              items: entries.slice(i, j),
              timestamp: e.timestamp,
            });
            i = j;
            continue;
          }
        }
        out.push(e);
        i++;
      }
      return out;
    },

    _rebuildDisplay() {
      this.displayEntries = this._buildDisplayEntries();
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
