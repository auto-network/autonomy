(function () {

  function _formatProject(project) {
    // Strip /home/<user>/workspace/ path prefix (encoded as dashes)
    const cleaned = project
      .replace(/^-home-[^-]+-workspace-/, '')
      .replace(/^-home-[^-]+-/, '')
      .replace(/^-+/, '');
    return cleaned || 'home';
  }

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
    Alpine.data('sessionViewerPage', () => ({
      // State machine
      state: 'loading',   // 'loading' | 'ready' | 'error'
      errorMsg: '',

      // Session identity (read from URL on init)
      project: '',
      sessionId: '',
      projectLabel: '',

      // Entries and polling
      entries: [],
      offset: 0,
      isLive: false,
      loadProgress: 0,      // 0-100
      loadedMB: '0',
      totalMB: '0',
      autoScroll: true,
      _storeCleanups: [],

      // Tool ID tracking (for matching tool_result to tool_use)
      _toolMap: {},
      // tool_id → tool_result entry (for pairing — chip metadata)
      _resultMap: {},
      // Context window tokens (from usage)
      contextTokens: 0,

      // Expand/collapse state: index → boolean
      _expanded: {},
      // Expand view mode: index → 'output' | 'input'
      _expandView: {},

      // Grouped display layer
      displayEntries: [],
      // Group accordion: dIdx → expanded subIdx (or null)
      _groupExpanded: {},
      // Group item view mode: 'dIdx-subIdx' → 'output' | 'input'
      _groupExpandView: {},

      // Input
      inputText: '',
      sending: false,

      // Multi-attach
      uploading: false,
      attachments: [],
      _nextAttachId: 0,

      // User-settable label
      _label: '',

      // Session type (host or container)
      sessionType: '',

      // Link terminal state
      linkState: 'idle',   // 'idle' | 'picking' | 'handshaking' | 'confirmed' | 'failed'
      linkCandidates: [],
      selectedTmux: '',
      linkError: '',
      HANDSHAKE_STRING: '[dashboard] confirming terminal link \u2014 please reply with I SEE IT',

      // API paths set from URL params
      _tailUrl: '',

      // ── Label editing ──────────────────────────────────────────

      saveLabel(event) {
        if (!this._tmuxSession || !this.isLive) return;
        var newLabel = (event.target.textContent || '').trim();
        // If cleared to empty or same as tmux name, treat as "no label"
        if (newLabel === this._tmuxSession) newLabel = '';
        if (newLabel === this._label) return;
        this._label = newLabel;
        // Update the store so cards reflect it too
        var store = Alpine.store('sessions')[this.sessionId];
        if (store) store.label = newLabel;
        fetch('/api/session/' + encodeURIComponent(this._tmuxSession) + '/label', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label: newLabel }),
        });
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

      toolLabel(toolId) {
        const info = this._toolMap[toolId];
        return info ? info.tool_name + ' result' : 'result';
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
                console.warn('[sessionViewer] upload error:', data.error);
              }
            })
            .catch(e => console.warn('[sessionViewer] upload failed:', e))
            .finally(() => {
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
              console.warn('[sessionViewer] send error (path):', pathData.error);
              return;
            }
            await new Promise(r => setTimeout(r, 200));
          }

          if (text) {
            const data = await _send(text);
            if (!data.ok) {
              console.warn('[sessionViewer] send error:', data.error);
              return;
            }
          }

          this.inputText = '';
          this.clearAttachments();
          // Dismiss mobile keyboard + reset textarea height
          if (this.$refs.messageInput) {
            this.$refs.messageInput.style.height = '';
            this.$refs.messageInput.style.overflowY = 'hidden';
            this.$refs.messageInput.blur();
          }
        } catch (e) {
          console.warn('[sessionViewer] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      async showLinkPicker() {
        try {
          const res = await fetch('/api/terminal/unclaimed');
          this.linkCandidates = await res.json();
        } catch (e) {
          this.linkCandidates = [];
        }
        this.selectedTmux = '';
        this.linkError = '';
        this.linkState = 'picking';
      },

      async confirmLink() {
        if (!this.selectedTmux) return;
        this.linkState = 'handshaking';
        try {
          const hsResp = await fetch('/api/session/send-handshake', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tmux_session: this.selectedTmux }),
          });
          const hsData = await hsResp.json();
          const handshake = hsData.handshake || '';

          // Poll confirm-link (filesystem scan) until it finds the handshake
          const deadline = Date.now() + 15000;
          while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 2000));
            const resp = await fetch('/api/session/confirm-link', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                tmux_session: this.selectedTmux,
                handshake: handshake,
              }),
            });
            if (resp.ok) {
              const data = await resp.json();
              this._tmuxSession = this.selectedTmux;
              var ss = window.getSessionStore(this.sessionId);
              ss.tmuxSession = this.selectedTmux;
              ss.linked = true;
              this._linked = true;
              this.linkState = 'confirmed';
              // Update project if filesystem scan found a different one
              if (data.project && data.project !== this.project) {
                this.project = data.project;
              }
              return;
            }
          }
          this.linkState = 'failed';
          this.linkError = 'Handshake timed out \u2014 file not found';
        } catch (e) {
          this.linkState = 'failed';
          this.linkError = 'Error: ' + (e.message || e);
        }
      },

      resetLink() {
        this.linkState = 'idle';
        this.selectedTmux = '';
        this.linkError = '';
        this.linkCandidates = [];
      },

      async _fetchBacklog(store) {
        var self = this;
        var data = await window.fetchWithProgress(
          this._tailUrl + '?after=0',
          function(received, total) {
            self.loadProgress = Math.round(received / total * 100);
            self.loadedMB = (received / 1048576).toFixed(1);
            self.totalMB = (total / 1048576).toFixed(1);
          }
        );

        // Handle error responses from server
        if (data.error) throw new Error(data.error);

        store.offset = data.offset || 0;
        store.isLive = !!data.is_live;
        store.sessionType = data.type || '';
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

        // Sync to component
        this.entries = store.entries;
        this.offset = store.offset;
        this.isLive = store.isLive;
        this.sessionType = store.sessionType;
        this._label = store.label || '';
        if (!this._tmuxSession) this._tmuxSession = store.tmuxSession;
        this._toolMap = store.toolMap;
        this._resultMap = store.resultMap;
        this._rebuildDisplay();
      },

      async init() {
        // Parse URL: /session/{project}/{session_id}
        const m = window.location.pathname.match(/^\/session\/([^/]+)\/(.+)$/);
        if (!m) {
          this.errorMsg = 'Invalid session URL';
          this.state = 'error';
          return;
        }
        this.project = decodeURIComponent(m[1]);
        this.sessionId = m[2];
        this.projectLabel = _formatProject(this.project);

        this._tailUrl = `/api/session/${encodeURIComponent(this.project)}/${encodeURIComponent(this.sessionId)}/tail`;

        // tmux session name: from query param only (no auto-detect — avoids misfiring to wrong session)
        const params = new URLSearchParams(window.location.search);
        this._tmuxSession = params.get('tmux') || '';

        var store = window.getSessionStore(this.sessionId);
        this._linked = store.linked || false;

        if (store.loaded) {
          // Instant render from cache — zero network
          this.entries = store.entries;
          this.offset = store.offset;
          this.isLive = store.isLive;
          this.sessionType = store.sessionType;
          this.contextTokens = store.contextTokens || 0;
          this._label = store.label || '';
          if (!this._tmuxSession) this._tmuxSession = store.tmuxSession;
          this._toolMap = store.toolMap;
          this._resultMap = store.resultMap;
          this._rebuildDisplay();
          this.state = 'ready';

          // Auto-scroll to bottom
          this.$nextTick(() => {
            var el = this.$refs.entriesContainer;
            if (el) el.scrollTop = el.scrollHeight;
          });
        } else {
          // First visit — subscribe SSE before fetch to not miss events
          store._loading = true;
          window.ensureSessionMessages();

          try {
            await this._fetchBacklog(store);
          } catch (e) {
            // New session with no JSONL yet — show empty ready state
            if (this._tmuxSession) {
              store._loading = false;
              store.loaded = true;
              this.entries = [];
              this._rebuildDisplay();
              this.isLive = true;
              this.sessionType = store.sessionType || 'terminal';
              this.state = 'ready';
              // Continue to set up watchers below
            } else {
              if (this.state === 'loading') {
                this.errorMsg = 'Failed to connect to session';
                this.state = 'error';
              }
              store._loading = false;
              return;
            }
          }

          // Flush pending SSE events that arrived during fetch
          var pending = store._pendingSSE;
          store._pendingSSE = [];
          store._loading = false;
          store.loaded = true;
          for (var i = 0; i < pending.length; i++) {
            window.appendSessionEntries(store, pending[i]);
          }

          // Sync any SSE additions to component
          this.entries = store.entries;
          this._toolMap = store.toolMap;
          this._resultMap = store.resultMap;
          this._rebuildDisplay();
          this.contextTokens = store.contextTokens || 0;

          if (this.state === 'loading') this.state = 'ready';

          // Auto-scroll to bottom (matches cached path above)
          this.$nextTick(() => {
            var el = this.$refs.entriesContainer;
            if (el) el.scrollTop = el.scrollHeight;
          });
        }

        // Ensure SSE subscription (idempotent — for already-loaded live sessions)
        window.ensureSessionMessages();

        // Reactive sync — Alpine.store mutations trigger $watch automatically
        var self = this;
        var sid = this.sessionId;
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.entries.length : 0;
          },
          function(newLen) {
            var s = Alpine.store('sessions')[sid];
            if (!s) return;
            self.entries = s.entries;
            self._toolMap = s.toolMap;
            self._resultMap = s.resultMap;
            self._rebuildDisplay();
            if (self.autoScroll) {
              self.$nextTick(function() {
                var el = self.$refs.entriesContainer;
                if (el) el.scrollTop = el.scrollHeight;
              });
            }
          }
        ));
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.isLive : true;
          },
          function(val) { self.isLive = val; }
        ));
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.contextTokens : 0;
          },
          function(val) { self.contextTokens = val; }
        ));
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.label : '';
          },
          function(val) { self._label = val || ''; }
        ));
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.linked : false;
          },
          function(val) { self._linked = val; }
        ));

        // Clipboard paste support — attach pasted files
        this.$nextTick(() => {
          const ta = this.$refs.messageInput;
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

      destroy() {
        // Do NOT unregister SSE — store keeps accumulating outside component lifecycle
        // Do NOT clear store — it persists across navigations
        for (var i = 0; i < this._storeCleanups.length; i++) {
          if (typeof this._storeCleanups[i] === 'function') this._storeCleanups[i]();
        }
        this._storeCleanups = [];
      },
    }));
  });
})();
