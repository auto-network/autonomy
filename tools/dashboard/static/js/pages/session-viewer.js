/**
 * Unified Session Viewer — one component for all 6 surfaces.
 *
 * Replaces three separate renderers: session-viewer.js, chatwith-panel.js,
 * live-panel-viewer.js. Entry point is configure({sessionId, runDir, project}).
 *
 * Modes:
 *   'page'    — session detail page, URL-driven (/session/{project}/{id})
 *   'panel'   — design page chat panel, configure() called by design.js
 *   'overlay' — bottom-docked overlay, controlled via _livePanelLoad/_livePanelReset
 *
 * Usage:
 *   x-data="sessionViewerPage()"             — page mode (default)
 *   x-data="sessionViewerPage({mode:'panel'})"   — panel mode
 *   x-data="sessionViewerPage({mode:'overlay'})" — overlay mode
 */
(function () {

  function _formatProject(project) {
    const cleaned = project
      .replace(/^-home-[^-]+-workspace-/, '')
      .replace(/^-home-[^-]+-/, '')
      .replace(/^-+/, '');
    return cleaned || 'home';
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('sessionViewerPage', (opts) => ({
      // Shared renderer methods (32 methods + constants)
      ...window.SessionRenderer,

      // ── Mode ────────────────────────────────────────────────────
      _mode: (opts && opts.mode) || 'page',

      // ── Page state ──────────────────────────────────────────────
      state: 'loading',   // 'loading' | 'ready' | 'error'
      errorMsg: '',

      // ── Session identity ────────────────────────────────────────
      sessionKey: '',       // store key (tmux_name)
      project: '',
      sessionId: '',
      projectLabel: '',

      // ── Store-backed getters ────────────────────────────────────
      // These read from Alpine.store('sessions')[sessionKey] directly.
      // No duplicated state, no sync watchers needed.

      get entries() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.entries : [];
      },
      get isLive() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.isLive : false;
      },
      get _tmuxSession() {
        // sessionKey is tmux_name — the stable identifier
        return this.sessionKey;
      },
      get _toolMap() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.toolMap : {};
      },
      get _resultMap() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.resultMap : {};
      },
      get _resolved() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.resolved : false;
      },
      get _label() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? (s.label || '') : '';
      },
      get sessionType() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.sessionType : '';
      },
      get role() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? (s.role || '') : '';
      },
      get activityState() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? (s.activityState || 'idle') : 'idle';
      },
      get isWorking() {
        return this.isLive && this.activityState !== 'idle';
      },
      get _linked() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.resolved : false;
      },
      get contextTokens() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.contextTokens : 0;
      },
      get topics() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return (s && s.topics) || [];
      },
      get entryCount() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return (s && s.entryCount) || this.entries.length;
      },
      get lastActivity() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return (s && s.lastActivity) || 0;
      },
      get org() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return (s && s.org) || null;
      },

      // ── View-only state ─────────────────────────────────────────
      displayEntries: [],
      autoScroll: true,
      _storeCleanups: [],
      _expanded: {},
      _expandView: {},
      _groupExpanded: {},
      _groupExpandView: {},

      // Backfill progress (page mode only)
      loadProgress: 0,
      loadedMB: '0',
      totalMB: '0',

      // Input (Tier 3) — contenteditable, no v-model
      hasContent: false,
      _draftTimer: null,
      sending: false,
      uploading: false,
      attachments: [],
      _nextAttachId: 0,

      // Header expand/collapse
      headerOpen: false,

      // Link terminal (Tier 3)
      linkState: 'idle',
      linkCandidates: [],
      selectedTmux: '',
      linkError: '',
      HANDSHAKE_STRING: '[dashboard] confirming terminal link \u2014 please reply with I SEE IT',

      // Elapsed-time tick for running tools (incremented every 1s)
      _tick: 0,
      _tickInterval: null,

      // Screenshot injection indicator
      screenshotInjected: false,
      _screenshotTimer: null,

      // Overlay mode state
      _runDir: '',
      _pollInterval: null,

      // API path
      _tailUrl: '',

      // ── State machine ───────────────────────────────────────────
      // Source of truth: store.loaded, store.isLive, store.resolved
      // Returns: 'connecting' | 'live' | 'unresolved' | 'complete'

      deriveState() {
        var s = Alpine.store('sessions')[this.sessionKey];
        if (!s || !s.loaded) return 'connecting';
        if (!s.isLive) return 'complete';
        if (s.resolved) return 'live';
        return 'unresolved';
      },

      // ── Configure ───────────────────────────────────────────────
      // Main entry point. Accepts {sessionId, runDir, project, tmuxSession}.

      async configure(cfgOpts) {
        var sessionId = cfgOpts.sessionId || '';
        var project = cfgOpts.project || '';
        var runDir = cfgOpts.runDir || '';
        var tmuxSession = cfgOpts.tmuxSession || '';
        var isLiveHint = cfgOpts._isLive;

        // ── RunDir path: fetch dispatch tail to resolve identity ──
        if (runDir && !sessionId) {
          this._runDir = runDir;
          this.state = 'loading';
          try {
            var res = await fetch('/api/dispatch/tail/' + encodeURIComponent(runDir) + '?after=0');
            if (!res.ok) {
              this.state = 'error';
              this.errorMsg = 'Failed to load dispatch run';
              return;
            }
            var data = await res.json();

            sessionId = data.tmux_name || data.session_id || runDir;
            project = data.project || project;
            tmuxSession = data.tmux_session || tmuxSession || sessionId;

            this.sessionKey = sessionId;
            this.sessionId = sessionId;
            this.project = project;

            var store = window.getSessionStore(sessionId);

            // Populate store from dispatch tail
            if (data.entries && data.entries.length > 0) {
              this._ingestEntries(store, data.entries);
              store.entries = data.entries;
            }
            store.isLive = isLiveHint !== undefined ? !!isLiveHint : !!data.is_live;
            if (data.resolved !== undefined) store.resolved = !!data.resolved;
            store.sessionType = data.type || '';
            store.role = data.role || '';
            store.activityState = data.activity_state || 'idle';
            if (data.offset !== undefined) store.offset = data.offset;
            if (data.seq !== undefined) store.seq = data.seq;
            store.loaded = true;

            this._rebuildDisplay();
            this.state = 'ready';
            this._scrollToBottom();

            // Connect to SSE if session identity is known
            if (data.session_id && data.project) {
              window.ensureSessionMessages();
              this._setupWatchers();
            } else if (store.isLive) {
              // Fallback: poll dispatch tail for live updates
              var self = this;
              var offset = data.offset || 0;
              this._pollInterval = setInterval(function () {
                self._pollTail(offset).then(function (newOffset) {
                  if (newOffset !== undefined) offset = newOffset;
                });
              }, 2000);
            }

            if (this._mode === 'overlay') this._updateHeader();
            return;
          } catch (e) {
            this.state = 'error';
            this.errorMsg = 'Failed to load: ' + (e.message || e);
            return;
          }
        }

        // ── SessionId path: standard session connection ──
        if (!sessionId) return;

        this.sessionKey = sessionId;
        this.sessionId = sessionId;
        this.project = project;
        this._tailUrl = '/api/session/' + encodeURIComponent(project) + '/' + encodeURIComponent(sessionId) + '/tail';

        var store = window.getSessionStore(sessionId);

        // Backfill org for dead sessions not seeded by /api/dao/active_sessions.
        if (!store.org) {
          fetch('/api/session/' + encodeURIComponent(sessionId))
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) { if (data && data.org) store.org = data.org; })
            .catch(function() {});
        }

        if (store.loaded) {
          // Instant render from cache — zero network
          this._rebuildDisplay();
          this.state = 'ready';
          this._scrollToBottom();
        } else {
          // First visit — subscribe SSE before fetch to not miss events
          store._loading = true;
          window.ensureSessionMessages();

          try {
            await this._fetchBacklog(store);
          } catch (e) {
            // New session with no JSONL yet — show empty ready state
            if (this.sessionKey) {
              store._loading = false;
              store.loaded = true;
              this._rebuildDisplay();
              this.state = 'ready';
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

          this._rebuildDisplay();
          if (this.state === 'loading') this.state = 'ready';
          this._scrollToBottom();
        }

        // Ensure SSE subscription (idempotent)
        window.ensureSessionMessages();

        // Set up reactive watchers
        this._setupWatchers();

        // Restore draft text into contenteditable + attach file-paste handler
        var self = this;
        this.$nextTick(function() {
          var el = self.$refs.messageInput;
          if (!el) return;
          var s = window.getSessionStore(self.sessionKey);
          if (s && s.draftText) {
            el.innerText = s.draftText;
            self.hasContent = el.innerText.trim().length > 0;
          }
          // Capture file pastes (text pastes handled by inline onpaste)
          el.addEventListener('paste', function(e) {
            var items = e.clipboardData && e.clipboardData.items;
            if (!items) return;
            var files = [];
            for (var i = 0; i < items.length; i++) {
              if (items[i].kind === 'file') {
                var f = items[i].getAsFile();
                if (f) files.push(f);
              }
            }
            if (files.length) { e.preventDefault(); self.addFiles(files); }
          });
        });
      },

      // ── Lifecycle ───────────────────────────────────────────────

      init() {
        // Keyboard padding toggle — applies to whichever .sv-input is present in the DOM.
        // Harmless when no .sv-input exists (e.g. overlay mode, pre-ready state).
        if (window.visualViewport && !window._svKeyboardListener) {
          window._svKeyboardListener = true;
          window.visualViewport.addEventListener('resize', function() {
            var bar = document.querySelector('.sv-input');
            if (!bar) return;
            var kbOpen = window.visualViewport.height < window.screen.height * 0.75;
            bar.style.paddingBottom = kbOpen ? '0px' : '';
          });
        }

        if (this._mode === 'overlay') {
          // Overlay: expose globals, wait for configure() calls
          var self = this;
          window._livePanelLoad = function(runDir, isLive) {
            self._reset();
            self.configure({ runDir: runDir, _isLive: isLive });
          };
          window._livePanelReset = function() {
            self._reset();
          };
          return;
        }

        if (this._mode === 'panel') {
          // Panel: wait for configure() from design.js
          return;
        }

        // Page mode: parse URL and configure
        var m = window.location.pathname.match(/^\/session\/([^/]+)\/(.+)$/);
        if (!m) {
          this.errorMsg = 'Invalid session URL';
          this.state = 'error';
          return;
        }
        this.project = decodeURIComponent(m[1]);
        this.sessionId = m[2];
        this.projectLabel = _formatProject(this.project);

        var params = new URLSearchParams(window.location.search);
        var tmuxFromUrl = params.get('tmux') || '';

        this.configure({
          sessionId: this.sessionId,
          project: this.project,
          tmuxSession: tmuxFromUrl,
        });
      },

      destroy() {
        // Do NOT unregister SSE — store keeps accumulating outside component lifecycle
        for (var i = 0; i < this._storeCleanups.length; i++) {
          if (typeof this._storeCleanups[i] === 'function') this._storeCleanups[i]();
        }
        this._storeCleanups = [];
        if (this._pollInterval) {
          clearInterval(this._pollInterval);
          this._pollInterval = null;
        }
        if (this._screenshotTimer) {
          clearTimeout(this._screenshotTimer);
          this._screenshotTimer = null;
        }
        if (this._tickInterval) {
          clearInterval(this._tickInterval);
          this._tickInterval = null;
        }
      },

      // ── Setup helpers ───────────────────────────────────────────

      _setupWatchers() {
        var self = this;
        var sid = this.sessionKey;

        // Tick interval: bump _tick every second so running-tool elapsed times refresh
        if (!this._tickInterval) {
          this._tickInterval = setInterval(function() {
            if (self.isAgentWorking()) self._tick++;
          }, 1000);
        }

        // Single watcher: incremental append + auto-scroll when entries change
        var lastLen = this.entries.length;
        this._storeCleanups.push(this.$watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.entries.length : 0;
          },
          function(newLen) {
            if (newLen > lastLen) {
              // Incremental: append each new entry (O(1) per entry)
              var s = Alpine.store('sessions')[sid];
              if (s) {
                for (var i = lastLen; i < newLen; i++) {
                  window.SessionDisplay.appendOne(self.displayEntries, s.entries, i);
                }
              }
              lastLen = newLen;
            } else {
              // Length decreased or reset — full rebuild
              self._rebuildDisplay();
              lastLen = newLen;
            }
            if (self.autoScroll) {
              self._scrollToBottom();
            }
            // Update overlay header if in overlay mode
            if (self._mode === 'overlay') self._updateHeader();
          }
        ));

        // Watch isLive for overlay header updates
        if (this._mode === 'overlay') {
          this._storeCleanups.push(this.$watch(
            function() {
              var s = Alpine.store('sessions')[sid];
              return s ? s.isLive : true;
            },
            function() { self._updateHeader(); }
          ));
        }
      },

      // ── Scroll helpers ──────────────────────────────────────────

      _scrollToBottom() {
        // Double-RAF after $nextTick: ensures Alpine has processed the x-for
        // template AND the browser has laid out all entries before we measure
        // scrollHeight. Critical for initial loads with 1000+ entries.
        var self = this;
        this.$nextTick(function() {
          requestAnimationFrame(function() {
            requestAnimationFrame(function() {
              var el = self.$refs.entriesContainer;
              if (el) el.scrollTop = el.scrollHeight;
            });
          });
        });
      },

      // ── Screenshot injection indicator ──────────────────────────

      showScreenshotInjected() {
        this.screenshotInjected = true;
        if (this._screenshotTimer) clearTimeout(this._screenshotTimer);
        var self = this;
        this._screenshotTimer = setTimeout(function() {
          self.screenshotInjected = false;
          self._screenshotTimer = null;
        }, 3000);
      },

      // ── Label editing ───────────────────────────────────────────

      saveLabel(event) {
        var store = Alpine.store('sessions')[this.sessionKey];
        if (!store || !this.sessionKey || !store.isLive) return;
        var newLabel = (event.target.textContent || '').trim();
        if (newLabel === this.sessionKey) newLabel = '';
        if (newLabel === (store.label || '')) return;
        store.label = newLabel;
        fetch('/api/session/' + encodeURIComponent(this.sessionKey) + '/label', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label: newLabel }),
        });
      },

      // ── Page-specific helpers ───────────────────────────────────

      toolLabel(toolId) {
        var info = this._toolMap[toolId];
        return info ? info.tool_name + ' result' : 'result';
      },

      addFiles(fileList) {
        for (var fi = 0; fi < fileList.length; fi++) {
          var file = fileList[fi];
          var id = ++this._nextAttachId;
          var isImage = file.type.startsWith('image/');
          var att = { id: id, name: file.name, isImage: isImage, dataUrl: null, path: null };
          this.attachments.push(att);

          if (isImage) {
            (function(attId, self) {
              var reader = new FileReader();
              reader.onload = function(e) {
                var found = self.attachments.find(function(a) { return a.id === attId; });
                if (found) found.dataUrl = e.target.result;
              };
              reader.readAsDataURL(file);
            })(id, this);
          }

          this.uploading = true;
          var form = new FormData();
          form.append('file', file);
          var tmux = this._tmuxSession;
          if (tmux) form.append('tmux_session', tmux);
          var self = this;
          (function(attId) {
            fetch('/api/upload', { method: 'POST', body: form })
              .then(function(r) { return r.json(); })
              .then(function(data) {
                if (data.ok) {
                  var found = self.attachments.find(function(a) { return a.id === attId; });
                  if (found) found.path = data.path;
                } else {
                  console.warn('[sessionViewer] upload error:', data.error);
                }
              })
              .catch(function(e) { console.warn('[sessionViewer] upload failed:', e); })
              .finally(function() {
                var pending = self.attachments.some(function(a) { return !a.path; });
                if (!pending) self.uploading = false;
              });
          })(id);
        }
      },

      // Contenteditable input handler — debounced draft persistence.
      onInput(el) {
        var text = el.innerText;
        this.hasContent = text.trim().length > 0;
        clearTimeout(this._draftTimer);
        var self = this;
        this._draftTimer = setTimeout(function() {
          var s = window.getSessionStore(self.sessionKey);
          if (s) s.draftText = text;
        }, 300);
      },

      async sendMessage() {
        var el = this.$refs.messageInput;
        if (!el) return;
        var text = el.innerText.trim();
        if ((this.attachments.length === 0 && !text) || this.sending) return;
        this.sending = true;
        var tmux = this._tmuxSession;
        try {
          var _send = async function(msg) {
            var res = await fetch('/api/session/send', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message: msg, tmux_session: tmux }),
            });
            return res.json();
          };

          for (var ai = 0; ai < this.attachments.length; ai++) {
            var att = this.attachments[ai];
            if (!att.path) continue;
            var pathData = await _send(att.path);
            if (!pathData.ok) {
              console.warn('[sessionViewer] send error (path):', pathData.error);
              return;
            }
            await new Promise(function(r) { setTimeout(r, 200); });
          }

          if (text) {
            var data = await _send(text);
            if (!data.ok) {
              console.warn('[sessionViewer] send error:', data.error);
              return;
            }
          }

          el.innerText = '';
          this.hasContent = false;
          var s = window.getSessionStore(this.sessionKey);
          if (s) s.draftText = '';
          this.clearAttachments();
          el.blur();
        } catch (e) {
          console.warn('[sessionViewer] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      // ── Interrupt (Escape key) ────────────────────────────────────

      async interrupt() {
        var tmux = this._tmuxSession;
        if (!tmux) return;
        try {
          await fetch('/api/session/' + encodeURIComponent(tmux) + '/interrupt', {
            method: 'POST',
          });
        } catch (e) {
          console.warn('[sessionViewer] interrupt failed:', e);
        }
      },

      // ── Link terminal ───────────────────────────────────────────

      async showLinkPicker() {
        try {
          var res = await fetch('/api/terminal/unclaimed');
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
          var hsResp = await fetch('/api/session/send-handshake', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tmux_session: this.selectedTmux }),
          });
          var hsData = await hsResp.json();
          var handshake = hsData.handshake || '';

          var deadline = Date.now() + 15000;
          while (Date.now() < deadline) {
            await new Promise(function(r) { setTimeout(r, 2000); });
            var resp = await fetch('/api/session/confirm-link', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                tmux_session: this.selectedTmux,
                handshake: handshake,
              }),
            });
            if (resp.ok) {
              var data = await resp.json();
              // Write to store (getters read from store)
              var ss = window.getSessionStore(this.sessionKey);
              ss.resolved = true;
              this.linkState = 'confirmed';
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

      // ── Backfill fetch ──────────────────────────────────────────

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

        if (data.error) throw new Error(data.error);

        store.offset = data.offset || 0;
        store.isLive = !!data.is_live;
        if (data.resolved !== undefined) store.resolved = !!data.resolved;
        store.sessionType = data.type || '';
        store.role = data.role || '';
        store.activityState = data.activity_state || 'idle';
        // tmux_session from response is for reference only; sessionKey is authoritative
        if (data.seq !== undefined) store.seq = data.seq;

        if (data.entries && data.entries.length > 0) {
          this._ingestEntries(store, data.entries);
          store.entries = data.entries;
        }

        this._rebuildDisplay();
      },

      // ── Ingest entries into store (toolMap + resultMap + enrichment init) ──

      _ingestEntries(store, entries) {
        for (var i = 0; i < entries.length; i++) {
          var entry = entries[i];
          if (entry.type === 'tool_use' && entry.tool_id) {
            store.toolMap[entry.tool_id] = { tool_name: entry.tool_name || '?' };
          }
          if (entry.type === 'tool_result' && entry.tool_id) {
            store.resultMap[entry.tool_id] = entry;
          }
        }
      },

      // ── Overlay: dispatch tail polling ──────────────────────────

      async _pollTail(currentOffset) {
        if (!this._runDir) return;
        try {
          var res = await fetch('/api/dispatch/tail/' + encodeURIComponent(this._runDir) + '?after=' + currentOffset);
          if (!res.ok) return currentOffset;
          var data = await res.json();
          var store = Alpine.store('sessions')[this.sessionKey];
          if (!store) return currentOffset;

          var wasLive = store.isLive;
          store.isLive = !!data.is_live;

          if (data.entries && data.entries.length > 0) {
            this._ingestEntries(store, data.entries);
            for (var i = 0; i < data.entries.length; i++) {
              store.entries.push(data.entries[i]);
            }
            this._rebuildDisplay();
            if (this.autoScroll) this._scrollToBottom();
          }

          if (wasLive !== store.isLive || (data.entries && data.entries.length > 0)) {
            if (this._mode === 'overlay') this._updateHeader();
          }

          // Stop polling if session completed and no new data
          if (!data.is_live && currentOffset > 0 && (!data.entries || data.entries.length === 0)) {
            if (this._pollInterval) {
              clearInterval(this._pollInterval);
              this._pollInterval = null;
            }
            if (this._mode === 'overlay') this._updateHeader();
          }
          return data.offset;
        } catch (_) {
          return currentOffset;
        }
      },

      // ── Overlay: reset ──────────────────────────────────────────

      _reset() {
        // Clean up watchers and polling
        for (var i = 0; i < this._storeCleanups.length; i++) {
          if (typeof this._storeCleanups[i] === 'function') this._storeCleanups[i]();
        }
        this._storeCleanups = [];
        if (this._pollInterval) {
          clearInterval(this._pollInterval);
          this._pollInterval = null;
        }
        if (this._tickInterval) {
          clearInterval(this._tickInterval);
          this._tickInterval = null;
        }
        // Reset view state
        this.state = 'loading';
        this.sessionKey = '';
        this.displayEntries = [];
        this._expanded = {};
        this._expandView = {};
        this._groupExpanded = {};
        this._groupExpandView = {};
        this.autoScroll = true;
        this._runDir = '';
        this._tailUrl = '';
        this._tick = 0;
      },

      // ── Overlay: header sync (imperative — outside Alpine scope) ──

      _updateHeader() {
        var statusEl = document.getElementById('live-panel-status');
        var pulseEl = document.getElementById('live-pulse');
        var badgeEl = document.getElementById('live-panel-badge');
        if (!statusEl) return;

        if (this.isLive) {
          statusEl.textContent = 'streaming';
          statusEl.className = 'text-xs text-green-400 ml-auto';
          if (pulseEl) { pulseEl.style.background = '#22c55e'; pulseEl.style.animation = ''; }
          if (badgeEl) { badgeEl.textContent = 'Live'; badgeEl.className = 'badge badge-open'; }
        } else {
          statusEl.textContent = this.entries.length + ' entries';
          statusEl.className = 'text-xs text-gray-500 ml-auto';
          if (pulseEl) { pulseEl.style.background = '#6b7280'; pulseEl.style.animation = 'none'; }
          if (badgeEl) { badgeEl.textContent = 'Complete'; badgeEl.className = 'badge badge-closed'; }
        }
      },

    }));
  });
})();
