/**
 * Live Panel Viewer — Alpine component for the bottom-docked session panel.
 *
 * Renders session entries with the same tool chips, markdown, and metadata
 * badges as the full-page session viewer. Connects to the session store and
 * SSE for live updates when session identity is available; falls back to
 * dispatch tail polling for container-only sessions.
 *
 * Usage: <div x-data="livePanelViewer()"> ... </div>
 * Control: window._livePanelLoad(runDir, isLive) / window._livePanelReset()
 */
(function () {

  // ── Tool color tables (shared with session-viewer.js) ──────────
  var TOOL_CHIPS = {
    Bash:  'sc-chip-bash',
    Read:  'sc-chip-read',
    Write: 'sc-chip-write',
    Edit:  'sc-chip-edit',
    Grep:  'sc-chip-grep',
    Glob:  'sc-chip-glob',
    Agent: 'sc-chip-agent',
  };
  var TOOL_BORDERS = {
    Bash:  'sc-border-bash',
    Read:  'sc-border-read',
    Write: 'sc-border-write',
    Edit:  'sc-border-edit',
    Grep:  'sc-border-grep',
    Glob:  'sc-border-glob',
    Agent: 'sc-border-agent',
  };

  document.addEventListener('alpine:init', function () {
    Alpine.data('livePanelViewer', function () {
      return {
        // State
        state: 'idle',  // 'idle' | 'loading' | 'ready' | 'error'
        errorMsg: '',
        entries: [],
        isLive: false,
        autoScroll: true,
        _toolMap: {},
        _resultMap: {},
        _expanded: {},
        _expandView: {},
        _storeCleanups: [],
        _pollInterval: null,
        _runDir: '',
        _sessionId: '',
        _project: '',

        // ── Lifecycle ──────────────────────────────────────────────

        init() {
          var self = this;
          // Expose control functions globally for showLivePanel/showCompletedPanel
          window._livePanelLoad = function (runDir, isLive) {
            self._load(runDir, isLive);
          };
          window._livePanelReset = function () {
            self._reset();
          };
        },

        destroy() {
          this._cleanup();
        },

        _cleanup() {
          for (var i = 0; i < this._storeCleanups.length; i++) {
            if (typeof this._storeCleanups[i] === 'function') this._storeCleanups[i]();
          }
          this._storeCleanups = [];
          if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
          }
        },

        _reset() {
          this._cleanup();
          this.state = 'idle';
          this.entries = [];
          this._toolMap = {};
          this._resultMap = {};
          this._expanded = {};
          this._expandView = {};
          this.isLive = false;
          this._runDir = '';
          this._sessionId = '';
          this._project = '';
          this.autoScroll = true;
        },

        async _load(runDir, isLive) {
          // Clean up previous session
          this._cleanup();
          this._runDir = runDir;
          this.state = 'loading';
          this.entries = [];
          this._toolMap = {};
          this._resultMap = {};
          this._expanded = {};
          this._expandView = {};
          this.autoScroll = true;
          this.isLive = isLive;

          try {
            var data = await this._fetch(0);
            if (!data) {
              this.state = 'error';
              this.errorMsg = 'Failed to load session';
              return;
            }

            // Populate entries and tool maps from initial fetch
            this._ingestEntries(data.entries || []);
            this.isLive = !!data.is_live;

            // If server returned session identity, connect to session store + SSE
            if (data.session_id && data.project) {
              this._sessionId = data.session_id;
              this._project = data.project;
              this._connectStore(data);
            } else if (isLive) {
              // Container fallback: poll dispatch tail API
              var self = this;
              var offset = data.offset || 0;
              this._pollInterval = setInterval(function () {
                self._pollTail(offset).then(function (newOffset) {
                  if (newOffset !== undefined) offset = newOffset;
                });
              }, 2000);
            }

            this.state = 'ready';
            this._updateHeader();
            this._scrollToBottom();
          } catch (e) {
            this.state = 'error';
            this.errorMsg = 'Failed to load session: ' + (e.message || e);
            this._setHeaderStatus('error', 'text-xs text-red-400 ml-auto');
          }
        },

        async _fetch(after) {
          var res = await fetch('/api/dispatch/tail/' + encodeURIComponent(this._runDir) + '?after=' + after);
          if (!res.ok) return null;
          return res.json();
        },

        _ingestEntries(newEntries) {
          for (var i = 0; i < newEntries.length; i++) {
            var entry = newEntries[i];
            if (entry.type === 'tool_use' && entry.tool_id) {
              this._toolMap[entry.tool_id] = { tool_name: entry.tool_name || '?' };
            }
            if (entry.type === 'tool_result' && entry.tool_id) {
              this._resultMap[entry.tool_id] = entry;
            }
            this.entries.push(entry);
          }
        },

        _connectStore(data) {
          var sid = this._sessionId;
          var store = window.getSessionStore(sid);

          // Seed the store if it hasn't been loaded yet
          if (!store.loaded) {
            store.offset = data.offset || 0;
            store.isLive = !!data.is_live;
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
            store.loaded = true;
          }

          // Ensure SSE subscription
          window.ensureSessionMessages();

          // Watch store for live updates
          var self = this;
          this._storeCleanups.push(this.$watch(
            function () {
              var s = Alpine.store('sessions')[sid];
              return s ? s.entries.length : 0;
            },
            function () {
              var s = Alpine.store('sessions')[sid];
              if (!s) return;
              self.entries = s.entries;
              self._toolMap = s.toolMap;
              self._resultMap = s.resultMap;
              if (self.autoScroll) self._scrollToBottom();
            }
          ));
          this._storeCleanups.push(this.$watch(
            function () {
              var s = Alpine.store('sessions')[sid];
              return s ? s.isLive : true;
            },
            function (val) { self.isLive = val; self._updateHeader(); }
          ));
        },

        async _pollTail(currentOffset) {
          if (!this._runDir) return;
          try {
            var data = await this._fetch(currentOffset);
            if (!data) return;
            var wasLive = this.isLive;
            this.isLive = !!data.is_live;
            if (data.entries && data.entries.length > 0) {
              this._ingestEntries(data.entries);
              // Force Alpine reactivity
              this.entries = this.entries.slice();
              if (this.autoScroll) this._scrollToBottom();
            }
            if (wasLive !== this.isLive) this._updateHeader();
            // Stop polling if session completed and no new data
            if (!data.is_live && currentOffset > 0 && (!data.entries || data.entries.length === 0)) {
              if (this._pollInterval) {
                clearInterval(this._pollInterval);
                this._pollInterval = null;
              }
              this._updateHeader();
            }
            return data.offset;
          } catch (_) {
            return currentOffset;
          }
        },

        _scrollToBottom() {
          var self = this;
          this.$nextTick(function () {
            var el = self.$refs.panelEntries;
            if (el) el.scrollTop = el.scrollHeight;
          });
        },

        // ── Render helpers (same as session-viewer.js) ──────────────

        formatTime: function (ts) {
          if (!ts) return '';
          try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
        },

        fmtDuration: function (seconds) {
          if (seconds == null || seconds < 0) return '';
          if (seconds < 1) return Math.round(seconds * 1000) + 'ms';
          if (seconds < 60) return seconds.toFixed(1) + 's';
          return Math.floor(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's';
        },

        chipClass: function (toolName) {
          return TOOL_CHIPS[toolName] || 'sc-chip-default';
        },

        borderClass: function (entry) {
          if (entry.type === 'tool_use') return TOOL_BORDERS[entry.tool_name] || 'sc-border-default';
          if (entry.type === 'user') return 'sc-border-user';
          if (entry.type === 'assistant_text') return 'sc-border-assistant';
          if (entry.type === 'thinking') return 'sc-border-thinking';
          if (entry.type === 'system') return 'sc-border-system';
          return 'sc-border-default';
        },

        headline: function (entry) {
          if (entry.type !== 'tool_use') return '';
          var inp = entry.input || {};
          var name = entry.tool_name || '';
          switch (name) {
            case 'Bash':   return inp.description || inp.command || '';
            case 'Read':   return this._smartPath(inp.file_path || '');
            case 'Write':  return this._smartPath(inp.file_path || '');
            case 'Edit':   return this._smartPath(inp.file_path || '');
            case 'Grep':   return (inp.pattern || '') + (inp.path ? ' in ' + this._smartPath(inp.path) : '');
            case 'Glob':   return inp.pattern || '';
            case 'Agent':  return inp.description || (inp.prompt ? inp.prompt.slice(0, 60) : '') || '';
            default:
              var vals = Object.values(inp);
              for (var i = 0; i < vals.length; i++) {
                if (typeof vals[i] === 'string' && vals[i].length > 0) return vals[i].slice(0, 80);
              }
              return name;
          }
        },

        metaDisplay: function (entry) {
          if (entry.type !== 'tool_use') return [];
          var name = entry.tool_name || '';
          var result = this._resultMap[entry.tool_id];
          var badges = [];

          switch (name) {
            case 'Bash': {
              var dur = this._duration(entry, result);
              if (dur != null) badges.push({ text: this.fmtDuration(dur), cls: 'sc-meta-gray' });
              if (result && result.is_error) badges.push({ text: '\u2717', cls: 'sc-meta-red' });
              break;
            }
            case 'Read': {
              if (result && result.content) {
                badges.push({ text: '+' + this._countLines(result.content), cls: 'sc-meta-green' });
              }
              break;
            }
            case 'Write': {
              var inp = entry.input || {};
              if (inp.content) {
                badges.push({ text: '+' + this._countLines(inp.content), cls: 'sc-meta-green' });
              }
              break;
            }
            case 'Edit': {
              var einp = entry.input || {};
              if (einp.new_string) badges.push({ text: '+' + this._countLines(einp.new_string), cls: 'sc-meta-green' });
              if (einp.old_string) badges.push({ text: '\u2212' + this._countLines(einp.old_string), cls: 'sc-meta-red' });
              break;
            }
            case 'Agent': {
              var adur = this._duration(entry, result);
              if (adur != null) badges.push({ text: this.fmtDuration(adur), cls: 'sc-meta-gray' });
              if (result && result.tool_calls != null) badges.push({ text: result.tool_calls + ' calls', cls: 'sc-meta-gray' });
              break;
            }
          }
          return badges;
        },

        isExpanded: function (idx) { return !!this._expanded[idx]; },
        toggleExpand: function (idx) {
          this._expanded[idx] = !this._expanded[idx];
          this._expanded = Object.assign({}, this._expanded);
        },

        expandViewMode: function (idx) { return this._expandView[idx] || 'output'; },
        toggleView: function (idx) {
          var current = this._expandView[idx] || 'output';
          this._expandView[idx] = current === 'output' ? 'input' : 'output';
          this._expandView = Object.assign({}, this._expandView);
        },

        expandContent: function (entry, idx) {
          if (entry.type === 'tool_use') {
            var result = this._resultMap[entry.tool_id];
            var mode = this._expandView[idx] || 'output';
            if (mode === 'input') return this._inputSummary(entry);
            if (result && result.content) return result.content;
            return this._inputSummary(entry);
          }
          if (entry.type === 'thinking') return entry.content || '';
          if (entry.type === 'system') return entry.content || '';
          return '';
        },

        hasGap: function (idx) {
          if (idx === 0) return false;
          var prev = this.entries[idx - 1];
          var curr = this.entries[idx];
          if (!prev || !curr) return false;
          return (prev.type === 'user') !== (curr.type === 'user');
        },

        sysIcon: function (entry) {
          var c = (entry.content || '').toLowerCase();
          if (c.includes('fail') || c.includes('error') || c.includes('block')) return '\u2717';
          return '\u2713';
        },
        sysIconColor: function (entry) {
          var c = (entry.content || '').toLowerCase();
          if (c.includes('fail') || c.includes('error') || c.includes('block')) return 'color: #ef4444';
          return 'color: #22c55e';
        },

        onScroll: function () {
          var el = this.$refs.panelEntries;
          if (!el) return;
          var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
          this.autoScroll = atBottom;
        },

        resumeScroll: function () {
          this.autoScroll = true;
          var el = this.$refs.panelEntries;
          if (el) el.scrollTop = el.scrollHeight;
        },

        // ── Header sync (imperative — outside Alpine scope) ────────

        _updateHeader: function () {
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

        _setHeaderStatus: function (text, cls) {
          var statusEl = document.getElementById('live-panel-status');
          if (statusEl) {
            statusEl.textContent = text;
            if (cls) statusEl.className = cls;
          }
        },

        // ── Internal helpers ────────────────────────────────────────

        _smartPath: function (path) {
          if (!path || typeof path !== 'string') return '';
          var p = path.replace(/^\/workspace\/repo\//, '');
          if (p.length > 40) {
            var parts = p.split('/');
            if (parts.length > 2) {
              p = '\u2026/' + parts[parts.length - 2] + '/' + parts[parts.length - 1];
            }
          }
          return p;
        },

        _duration: function (entry, result) {
          if (!result || !entry.timestamp || !result.timestamp) return null;
          try {
            var t0 = new Date(entry.timestamp).getTime();
            var t1 = new Date(result.timestamp).getTime();
            var diff = (t1 - t0) / 1000;
            return diff >= 0 ? diff : null;
          } catch (_) { return null; }
        },

        _countLines: function (str) {
          if (!str) return 0;
          var n = 1;
          for (var i = 0; i < str.length; i++) {
            if (str.charCodeAt(i) === 10) n++;
          }
          return n;
        },

        _inputSummary: function (entry) {
          var inp = entry.input || {};
          var name = entry.tool_name || '';
          switch (name) {
            case 'Bash':  return inp.command || '';
            case 'Edit': {
              var parts = [];
              if (inp.old_string) parts.push('--- old\n' + inp.old_string);
              if (inp.new_string) parts.push('+++ new\n' + inp.new_string);
              return parts.join('\n\n') || inp.file_path || '';
            }
            case 'Read':  return inp.file_path || '';
            case 'Write': return (inp.file_path || '') + (inp.content ? '\n' + inp.content : '');
            case 'Grep':  return (inp.pattern || '') + (inp.path ? '\nin ' + inp.path : '');
            case 'Glob':  return (inp.pattern || '') + (inp.path ? '\nin ' + inp.path : '');
            case 'Agent': return inp.prompt || '';
            default: {
              var vals = [];
              for (var k in inp) {
                if (typeof inp[k] === 'string' && inp[k].length > 0 && k !== 'description') {
                  vals.push(inp[k]);
                }
              }
              return vals.join('\n') || name;
            }
          }
        },
      };
    });
  });
})();
