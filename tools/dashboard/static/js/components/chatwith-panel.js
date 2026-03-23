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

  document.addEventListener('alpine:init', () => {
    Alpine.data('chatWithPanel', () => ({
      // Entries (synced from session store)
      entries: [],
      isLive: false,
      entryCount: 0,
      autoScroll: true,

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
          if (!this._tmuxSession) this._tmuxSession = store.tmuxSession;
          this.entryCount = this.entries.length;
          this.state = 'ready';

          this.$nextTick(() => {
            var el = this.$refs.cwEntriesContainer;
            if (el) el.scrollTop = el.scrollHeight;
          });
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
              if (!this._tmuxSession) this._tmuxSession = store.tmuxSession;
              this.entryCount = this.entries.length;
              this.state = 'ready';

              this.$nextTick(() => {
                var el = this.$refs.cwEntriesContainer;
                if (el) el.scrollTop = el.scrollHeight;
              });
            }
          } catch (e) {
            // Session may not be ready yet — show empty but live
            store._loading = false;
            store.loaded = true;
            this.entries = [];
            this.isLive = true;
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
            self.entryCount = s.entries.length;
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

      formatTime(ts) {
        if (!ts) return '';
        try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
      },

      toolLabel(toolId) {
        const info = this._toolMap[toolId];
        return info ? info.tool_name + ' result' : 'result';
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
