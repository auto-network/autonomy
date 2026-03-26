(function () {

  function _formatProject(project) {
    // Strip /home/<user>/workspace/ path prefix (encoded as dashes)
    const cleaned = project
      .replace(/^-home-[^-]+-workspace-/, '')
      .replace(/^-home-[^-]+-/, '')
      .replace(/^-+/, '');
    return cleaned || 'home';
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('sessionViewerPage', () => ({
      // Shared renderer methods (32 methods + constants)
      ...window.SessionRenderer,

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

      // Cache for enriched tile data (survives scroll, keyed by source_id)
      _enrichCache: {},

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

      // ── Lazy tile enhancement ──────────────────────────────────

      async lazyEnhance(entry) {
        if (entry._enhanced) return;
        var sid = entry.source_id;
        if (!sid) return;

        // Mark immediately to prevent double-fetch
        entry._enhanced = 'loading';

        // Check cache first
        if (this._enrichCache[sid]) {
          var cached = this._enrichCache[sid];
          entry._enriched_title = cached.title;
          entry._enriched_preview = cached.preview;
          entry._enriched_tags = cached.tags;
          entry._enhanced = true;
          return;
        }

        // Delay 1 second — only fetch if still visible (no drive-by fetches)
        await new Promise(function(r) { setTimeout(r, 1000); });
        if (entry._enhanced !== 'loading') return; // was scrolled away or already done

        try {
          var resp = await fetch('/api/graph/' + encodeURIComponent(sid));
          if (!resp.ok) { entry._enhanced = true; return; }
          var data = await resp.json();

          var src = data.source || {};
          var title = (src.title || '').replace(/^#+\s*/, '');
          var meta = {};
          try { meta = typeof src.metadata === 'string' ? JSON.parse(src.metadata) : (src.metadata || {}); } catch(_) {}
          var tags = meta.tags || [];

          // Preview: first 120 chars of content from first entry, skip title/heading lines
          var content = (data.entries && data.entries[0] && data.entries[0].content) || '';
          var lines = content.split('\n').filter(function(l) { return !l.startsWith('#') && l.trim(); });
          var preview = lines.slice(0, 2).join(' ').slice(0, 120);

          entry._enriched_title = title || entry.content;
          entry._enriched_preview = preview;
          entry._enriched_tags = tags;
          entry._enhanced = true;

          this._enrichCache[sid] = { title: title, preview: preview, tags: tags };
        } catch(_) {
          entry._enhanced = true; // fail silently, keep original content
        }
      },

      // ── Page-specific helpers ────────────────────────────────

      toolLabel(toolId) {
        const info = this._toolMap[toolId];
        return info ? info.tool_name + ' result' : 'result';
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
          var s = window.getSessionStore(this.sessionId);
          if (s) s.draftText = '';
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
            // Pre-init enrichment properties so Alpine tracks them reactively
            if (entry.type === 'semantic_bash' && entry.source_id && !entry.hasOwnProperty('_enhanced')) {
              entry._enhanced = false;
              entry._enriched_title = null;
              entry._enriched_preview = null;
              entry._enriched_tags = null;
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
        this.inputText = store.draftText || '';

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

        // Persist draft text to store on every keystroke
        var self = this;
        this.$watch('inputText', function(val) {
          var s = window.getSessionStore(self.sessionId);
          if (s) s.draftText = val;
        });

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
