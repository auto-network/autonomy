// Chat With panel — reusable Alpine component for rich session messaging.
//
// Uses the shared session store (session-store.js) and SSE for live updates.
// No polling — all updates come through SSE session:messages topic.
//
// Usage in template:
//   <div x-data="chatWithPanel()" x-init="configure({sessionId, project, tmuxSession})">
//
// The component expects configure() to be called with:
//   sessionId:    session ID (tmux name) for session store + SSE routing
//   project:      project identifier for the tail API endpoint
//   tmuxSession:  tmux session name for sending messages

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

  document.addEventListener('alpine:init', () => {
    Alpine.data('chatWithPanel', () => ({
      // Entries (synced from session store)
      entries: [],
      isLive: false,
      entryCount: 0,
      autoScroll: true,

      // Display layer (grouped entries)
      displayEntries: [],
      _resultMap: {},
      _expanded: {},
      _expandView: {},
      _groupExpanded: {},
      _groupExpandView: {},

      // Tool ID tracking (for matching tool_result to tool_use)
      _toolMap: {},

      // Input
      inputText: '',
      sending: false,

      // Multi-attach
      uploading: false,
      attachments: [],
      _nextAttachId: 0,

      // Configuration (set via configure())
      _sessionId: '',
      _project: '',
      _tmuxSession: '',

      // Screenshot injection indicator
      screenshotInjected: false,
      _screenshotTimer: null,

      // State
      state: 'waiting',  // 'waiting' | 'ready'

      // Store watchers (for cleanup)
      _storeCleanups: [],

      _scrollToBottom() {
        // Double-RAF after $nextTick: ensures Alpine has processed the x-for
        // template AND the browser has laid out all entries before we measure
        // scrollHeight. Critical for initial loads with 1000+ entries.
        this.$nextTick(() => {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              var el = this.$refs.cwEntriesContainer;
              if (el) el.scrollTop = el.scrollHeight;
            });
          });
        });
      },

      configure(opts) {
        this._sessionId = opts.sessionId || '';
        this._project = opts.project || '';
        this._tmuxSession = opts.tmuxSession || '';

        if (this._sessionId) {
          this._connectToStore();
        }

        // Clipboard paste support — attach pasted files
        this.$nextTick(() => {
          const ta = this.$refs.cwMessageInput;
          if (ta) {
            ta.addEventListener('paste', (e) => {
              const items = e.clipboardData && e.clipboardData.items;
              if (!items) return;
              const files = [];
              for (let i = 0; i < items.length; i++) {
                if (items[i].kind === 'file') {
                  const f = items[i].getAsFile();
                  if (f) files.push(f);
                }
              }
              if (files.length) { e.preventDefault(); this.addFiles(files); }
            });
          }
        });
      },

      async _connectToStore() {
        var sessionId = this._sessionId;
        var project = this._project;
        var store = window.getSessionStore(sessionId);

        // Ensure SSE subscription (idempotent)
        window.ensureSessionMessages();

        if (store.loaded) {
          // Already loaded — instant render from cache
          this.entries = store.entries;
          this.isLive = store.isLive;
          this._toolMap = store.toolMap;
          this._resultMap = store.resultMap;
          if (!this._tmuxSession) this._tmuxSession = store.tmuxSession;
          this.entryCount = this.entries.length;
          this._rebuildDisplay();
          this.state = 'ready';
          this._scrollToBottom();
        } else {
          // First visit — fetch backfill
          store._loading = true;
          var tailUrl = '/api/session/' + encodeURIComponent(project) + '/' + encodeURIComponent(sessionId) + '/tail';

          try {
            var data = await fetch(tailUrl + '?after=0').then(r => r.json());

            if (data.error) {
              // Session may not have written JSONL yet — show empty state
              store._loading = false;
              store.loaded = true;
              this.entries = [];
              this.isLive = true;
              this._rebuildDisplay();
              this.state = 'ready';
            } else {
              store.offset = data.offset || 0;
              store.isLive = !!data.is_live;
              store.tmuxSession = data.tmux_session || '';
              if (data.seq !== undefined) store.seq = data.seq;

              if (data.entries && data.entries.length > 0) {
                for (var i = 0; i < data.entries.length; i++) {
                  var entry = data.entries[i];
                  if (entry.type === 'tool_use' && entry.tool_id) {
                    store.toolMap[entry.tool_id] = { tool_name: entry.tool_name || '?' };
                  }
                  if (entry.type === 'tool_result' && entry.tool_id) {
                    store.resultMap[entry.tool_id] = entry;
                  }
                }
                store.entries = data.entries;
              }

              // Flush pending SSE events that arrived during fetch
              var pending = store._pendingSSE;
              store._pendingSSE = [];
              store._loading = false;
              store.loaded = true;
              for (var j = 0; j < pending.length; j++) {
                window.appendSessionEntries(store, pending[j]);
              }

              this.entries = store.entries;
              this.isLive = store.isLive;
              this._toolMap = store.toolMap;
              this._resultMap = store.resultMap;
              if (!this._tmuxSession) this._tmuxSession = store.tmuxSession;
              this.entryCount = this.entries.length;
              this._rebuildDisplay();
              this.state = 'ready';
              this._scrollToBottom();
            }
          } catch (e) {
            // Session may not be ready yet — show empty but live
            store._loading = false;
            store.loaded = true;
            this.entries = [];
            this.isLive = true;
            this._rebuildDisplay();
            this.state = 'ready';
          }
        }

        // Reactive sync — watch store for new entries from SSE
        var self = this;
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sessionId];
            return s ? s.entries.length : 0;
          },
          function(newLen) {
            var s = Alpine.store('sessions')[sessionId];
            if (!s) return;
            self.entries = s.entries;
            self._toolMap = s.toolMap;
            self._resultMap = s.resultMap;
            self.entryCount = s.entries.length;
            self._rebuildDisplay();
            if (self.state === 'waiting') self.state = 'ready';
            if (self.autoScroll) {
              self.$nextTick(function() {
                var el = self.$refs.cwEntriesContainer;
                if (el) el.scrollTop = el.scrollHeight;
              });
            }
          }
        ));

        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sessionId];
            return s ? s.isLive : true;
          },
          function(val) { self.isLive = val; }
        ));
      },

      destroy() {
        for (var i = 0; i < this._storeCleanups.length; i++) {
          if (typeof this._storeCleanups[i] === 'function') this._storeCleanups[i]();
        }
        this._storeCleanups = [];
        if (this._screenshotTimer) {
          clearTimeout(this._screenshotTimer);
          this._screenshotTimer = null;
        }
      },

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
          case 'Bash': return inp.command || '';
          case 'Edit': {
            const parts = [];
            if (inp.old_string) parts.push('--- old\n' + inp.old_string);
            if (inp.new_string) parts.push('+++ new\n' + inp.new_string);
            return parts.join('\n\n') || inp.file_path || '';
          }
          case 'Read': return inp.file_path || '';
          case 'Write': return (inp.file_path || '') + (inp.content ? '\n' + inp.content : '');
          case 'Grep': return (inp.pattern || '') + (inp.path ? '\nin ' + inp.path : '');
          case 'Glob': return (inp.pattern || '') + (inp.path ? '\nin ' + inp.path : '');
          case 'Agent': return inp.prompt || '';
          default: {
            const parts = [];
            for (const [k, v] of Object.entries(inp)) {
              if (typeof v === 'string' && v.length > 0 && k !== 'description') parts.push(v);
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
        this._expanded = { ...this._expanded };
      },

      hasGap(idx) {
        if (idx === 0) return false;
        const prev = this.displayEntries[idx - 1];
        const curr = this.displayEntries[idx];
        if (!prev || !curr) return false;
        const prevIsUser = prev.type === 'user';
        const currIsUser = curr.type === 'user';
        return prevIsUser !== currIsUser;
      },

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

      _GROUPABLE: new Set(['Bash', 'Read', 'Edit', 'Grep', 'Glob']),

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
        let p = path.replace(/^\/workspace\/repo\//, '');
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
        let n = 1;
        for (let i = 0; i < str.length; i++) {
          if (str.charCodeAt(i) === 10) n++;
        }
        return n;
      },

      onScroll() {
        const el = this.$refs.cwEntriesContainer;
        if (!el) return;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
        this.autoScroll = atBottom;
      },

      resumeScroll() {
        this.autoScroll = true;
        const el = this.$refs.cwEntriesContainer;
        if (el) el.scrollTop = el.scrollHeight;
      },

      addFiles(fileList) {
        for (const file of fileList) {
          const id = ++this._nextAttachId;
          const isImage = file.type.startsWith('image/');
          const att = { id, name: file.name, isImage, dataUrl: null, path: null };
          this.attachments.push(att);

          // FileReader for preview (mobile Safari compat — NOT URL.createObjectURL)
          if (isImage) {
            const reader = new FileReader();
            reader.onload = (e) => {
              const found = this.attachments.find(a => a.id === id);
              if (found) found.dataUrl = e.target.result;
            };
            reader.readAsDataURL(file);
          }

          // Upload to server
          this.uploading = true;
          const form = new FormData();
          form.append('file', file);
          if (this._tmuxSession) form.append('tmux_session', this._tmuxSession);
          fetch('/api/upload', { method: 'POST', body: form })
            .then(r => r.json())
            .then(data => {
              if (data.ok) {
                const found = this.attachments.find(a => a.id === id);
                if (found) found.path = data.path;
              } else {
                console.warn('[chatWithPanel] upload error:', data.error);
              }
            })
            .catch(e => console.warn('[chatWithPanel] upload failed:', e))
            .finally(() => {
              // Only clear uploading when all attachments have resolved
              const pending = this.attachments.some(a => !a.path);
              if (!pending) this.uploading = false;
            });
        }
      },

      removeAttachment(id) {
        this.attachments = this.attachments.filter(a => a.id !== id);
      },

      clearAttachments() {
        this.attachments = [];
      },

      async sendMessage() {
        const text = this.inputText.trim();
        if ((this.attachments.length === 0 && !text) || this.sending) return;
        this.sending = true;
        try {
          const _send = async (msg) => {
            const res = await fetch('/api/session/send', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message: msg, tmux_session: this._tmuxSession }),
            });
            return res.json();
          };

          // Two-send strategy: each attachment path sent bare with 200ms gaps, text last
          for (const att of this.attachments) {
            if (!att.path) continue;
            const pathData = await _send(att.path);
            if (!pathData.ok) {
              console.warn('[chatWithPanel] send error (path):', pathData.error);
              return;
            }
            await new Promise(r => setTimeout(r, 200));
          }

          if (text) {
            const data = await _send(text);
            if (!data.ok) {
              console.warn('[chatWithPanel] send error:', data.error);
              return;
            }
          }

          this.inputText = '';
          this.clearAttachments();
          if (this.$refs.cwMessageInput) {
            this.$refs.cwMessageInput.style.height = '';
            this.$refs.cwMessageInput.style.overflowY = 'hidden';
          }
        } catch (e) {
          console.warn('[chatWithPanel] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      showScreenshotInjected() {
        this.screenshotInjected = true;
        if (this._screenshotTimer) clearTimeout(this._screenshotTimer);
        this._screenshotTimer = setTimeout(() => {
          this.screenshotInjected = false;
          this._screenshotTimer = null;
        }, 3000);
      },
    }));
  });
})();
