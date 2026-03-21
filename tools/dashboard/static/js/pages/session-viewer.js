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
      autoScroll: true,
      _pollTimer: null,

      // Tool ID tracking (for matching tool_result to tool_use)
      _toolMap: {},
      // tool_id → tool_result entry (for pairing — chip metadata)
      _resultMap: {},
      // Expand/collapse state: index → boolean
      _expanded: {},

      // Input
      inputText: '',
      sending: false,

      // Multi-attach
      uploading: false,
      attachments: [],
      _nextAttachId: 0,

      // Session type (host or container)
      sessionType: '',

      // SSE dedup sequence number
      _lastSeq: 0,
      _initialLoadDone: false,
      _pendingSSE: [],

      // Link terminal state
      linkState: 'idle',   // 'idle' | 'picking' | 'handshaking' | 'confirmed' | 'failed'
      linkCandidates: [],
      selectedTmux: '',
      linkError: '',
      HANDSHAKE_STRING: '[dashboard] confirming terminal link \u2014 please reply with I SEE IT',

      // API paths set from URL params
      _tailUrl: '',

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
        if (entry.type === 'tool_use') return TOOL_BORDERS[entry.tool_name] || 'sc-border-default';
        if (entry.type === 'user') return 'sc-border-user';
        if (entry.type === 'assistant_text') return 'sc-border-assistant';
        if (entry.type === 'thinking') return 'sc-border-thinking';
        if (entry.type === 'system') return 'sc-border-system';
        return 'sc-border-default';
      },

      headline(entry) {
        if (entry.type !== 'tool_use') return '';
        const inp = entry.input || {};
        const name = entry.tool_name || '';
        switch (name) {
          case 'Bash':
            return this._smartPath(inp.command || '');
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

      expandContent(entry) {
        if (entry.type === 'tool_use') {
          const result = this._resultMap[entry.tool_id];
          const name = entry.tool_name || '';
          if (name === 'Edit') {
            // Show old/new diff then result
            const inp = entry.input || {};
            let parts = [];
            if (inp.old_string) parts.push('--- old\n' + inp.old_string);
            if (inp.new_string) parts.push('+++ new\n' + inp.new_string);
            if (result && result.content) parts.push('--- result\n' + result.content);
            return parts.join('\n\n');
          }
          // For other tools: show result content, fallback to input JSON
          if (result && result.content) return result.content;
          return JSON.stringify(entry.input || {}, null, 2);
        }
        if (entry.type === 'thinking') return entry.content || '';
        if (entry.type === 'system') return entry.content || '';
        return '';
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
        const prev = this.entries[idx - 1];
        const curr = this.entries[idx];
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

      // ── Internal helpers ───────────────────────────────────────

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
          await fetch('/api/session/send-handshake', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tmux_session: this.selectedTmux }),
          });
          // Poll JSONL for handshake string to appear (up to 10s)
          const deadline = Date.now() + 10000;
          while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 1500));
            await this._poll();
            const found = this.entries.slice(-5).some(
              e => e.type === 'user' && (e.content || '').includes(this.HANDSHAKE_STRING)
            );
            if (found) {
              await fetch('/api/session/confirm-link', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  project: this.project,
                  session_id: this.sessionId,
                  tmux_session: this.selectedTmux,
                }),
              });
              this._tmuxSession = this.selectedTmux;
              this.linkState = 'confirmed';
              return;
            }
          }
          this.linkState = 'failed';
          this.linkError = 'Handshake timed out \u2014 handshake message did not appear in session log';
        } catch (e) {
          this.linkState = 'failed';
          this.linkError = 'Error during handshake: ' + (e.message || e);
        }
      },

      resetLink() {
        this.linkState = 'idle';
        this.selectedTmux = '';
        this.linkError = '';
        this.linkCandidates = [];
      },

      async _poll() {
        try {
          const res = await fetch(`${this._tailUrl}?after=${this.offset}`);
          const data = await res.json();

          this.isLive = data.is_live;
          if (data.offset !== undefined) this.offset = data.offset;
          if (data.seq !== undefined) this._lastSeq = data.seq;

          // Set session type from first response
          if (data.type && !this.sessionType) this.sessionType = data.type;

          // Auto-detect tmux session from per-file meta (handles page reload after linking)
          if (data.tmux_session && !this._tmuxSession) this._tmuxSession = data.tmux_session;

          if (data.entries && data.entries.length > 0) {
            this._ingestEntries(data.entries);
          }

          if (this.state === 'loading') this.state = 'ready';
        } catch (e) {
          if (this.state === 'loading') {
            this.errorMsg = 'Failed to connect to session';
            this.state = 'error';
          }
        }
      },

      /** Track tool IDs, pair results, append entries, auto-scroll. Shared by _poll and SSE. */
      _ingestEntries(entries) {
        for (const entry of entries) {
          if (entry.type === 'tool_use' && entry.tool_id) {
            this._toolMap[entry.tool_id] = {
              tool_name: entry.tool_name || '?',
            };
          }
          if (entry.type === 'tool_result' && entry.tool_id) {
            this._resultMap[entry.tool_id] = entry;
          }
        }
        this.entries = [...this.entries, ...entries];

        if (this.autoScroll) {
          this.$nextTick(() => {
            const el = this.$refs.entriesContainer;
            if (el) el.scrollTop = el.scrollHeight;
          });
        }
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

        // Subscribe to SSE BEFORE fetching so we don't miss anything
        this._lastSeq = 0;
        this._initialLoadDone = false;
        this._pendingSSE = [];
        var sseTopic = 'session:' + this.sessionId;
        this._sseHandler = (data) => {
          if (!this._initialLoadDone) {
            this._pendingSSE.push(data);
            return;
          }
          if (data.seq !== undefined && data.seq <= this._lastSeq) return;
          if (data.seq !== undefined) this._lastSeq = data.seq;
          if (data.entries && data.entries.length > 0) {
            this._ingestEntries(data.entries);
          }
          if (data.is_live !== undefined) {
            this.isLive = data.is_live;
          }
        };
        window.registerHandler(sseTopic, this._sseHandler);

        // Fetch full backlog
        await this._poll();

        // If no entries came back and state is still loading, set ready (empty session)
        if (this.state === 'loading') this.state = 'ready';

        // Process any SSE events that arrived during fetch
        this._initialLoadDone = true;
        for (const pending of this._pendingSSE) {
          if (pending.seq !== undefined && pending.seq <= this._lastSeq) continue;
          if (pending.seq !== undefined) this._lastSeq = pending.seq;
          if (pending.entries && pending.entries.length > 0) {
            this._ingestEntries(pending.entries);
          }
          if (pending.is_live !== undefined) {
            this.isLive = pending.is_live;
          }
        }
        this._pendingSSE = [];

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
        if (this._sseHandler) {
          window.unregisterHandler('session:' + this.sessionId, this._sseHandler);
          this._sseHandler = null;
        }
        if (this._pollTimer) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
      },
    }));
  });
})();
