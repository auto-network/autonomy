// Design Studio page Alpine component — Unified toolbar (Design Studio experiment a8ee8212).
// 4-state toolbar: DISCONNECTED, PICKER, LIVE_UI, LIVE_CHAT.
// Kept: design fetch, single iframe injection, capture, Chat With integration, SSE design subscription.

(function () {

  function _revisionIdFromPath() {
    var m = window.location.pathname.match(/^\/design\/(.+)$/);
    return m ? m[1] : '';
  }

  // ── State picker HTML generator (for multi-state fixtures) ────────────────
  function _buildStatePickerHtml(stateKeys) {
    var pills = stateKeys.map(function (key, i) {
      var isActive = i === 0;
      var bg = isActive ? '#334155' : 'transparent';
      var color = isActive ? '#f1f5f9' : '#94a3b8';
      var esc = key.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      return '<button data-fixture-state="' + esc + '" style="padding:5px 12px;border:none;border-radius:8px;' +
        'font-size:12px;font-weight:500;cursor:pointer;background:' + bg + ';color:' + color +
        ';font-family:inherit;transition:all 0.15s;">' + esc + '</button>';
    }).join('');

    var script = '<script>' +
      'document.getElementById("fixture-state-picker").addEventListener("click",function(e){' +
      'var btn=e.target.closest("[data-fixture-state]");if(!btn)return;' +
      'var key=btn.dataset.fixtureState;' +
      'window.FIXTURE=window.FIXTURE_STATES[key];' +
      'var root=document.querySelector("[x-data]");' +
      'if(root&&typeof Alpine!=="undefined"){' +
      'var d=Alpine.$data(root),s=window.FIXTURE_STATES[key];' +
      'Object.keys(s).forEach(function(k){d[k]=s[k]});' +
      '}' +
      'this.querySelectorAll("[data-fixture-state]").forEach(function(b){' +
      'var a=b.dataset.fixtureState===key;' +
      'b.style.background=a?"#334155":"transparent";' +
      'b.style.color=a?"#f1f5f9":"#94a3b8"});' +
      'window.dispatchEvent(new CustomEvent("fixture-state-change",{detail:{state:key,data:window.FIXTURE}}))' +
      '});' +
      '<\/script>';

    return '<div id="fixture-state-picker" style="position:fixed;bottom:12px;left:50%;transform:translateX(-50%);' +
      'display:flex;align-items:center;gap:2px;background:#0f172a;border:1px solid #1e293b;' +
      'border-radius:10px;padding:3px;box-shadow:0 4px 24px rgba(0,0,0,0.5);z-index:100;">' +
      pills + '</div>' + script;
  }

  document.addEventListener('alpine:init', function () {
    Alpine.data('designPage', function () {
      return {
        // State machine
        state: 'loading',   // 'loading' | 'ready' | 'error'

        // Design data
        revisionId: '',
        design: null,
        designId: '',       // stable design ID (shared across revisions)
        iterCount: 0,
        iterIndex: 0,

        // Chat toggle
        chatOpen: false,
        isLive: false,

        // Chat With session
        chatConnected: false,
        chatSessions: [],
        chatSessionLabel: '',
        _tmuxSession: null,

        // Capture state: 'idle' | 'working' | 'success' | 'error'
        captureState: 'idle',

        // Primer state: 'idle' | 'working' | 'done'
        primerState: 'idle',

        // ── Computed: toolbar state machine ─────────────────────────────
        get toolbarState() { return deriveToolbarState(this.chatOpen, this.chatConnected); },
        get toolbar() { return toolbarElements(this.toolbarState); },
        get canGoBack() { return this.iterIndex > 0; },
        get canGoForward() { return this.iterIndex < this.iterCount - 1; },

        // ── Lifecycle ─────────────────────────────────────────────────────

        init: function () {
          window._designPage = this;
          this.revisionId = _revisionIdFromPath();
          this._load();
          var self = this;
          this.$watch('chatOpen', function (open) {
            localStorage.setItem('design-chatOpen-' + self.designId, open ? 'true' : 'false');
            if (open && !self.chatConnected) self._loadChatSessions();
            if (open && self.chatConnected) {
              self.$nextTick(function () {
                var panelEl = document.getElementById('design-chat-panel');
                if (panelEl) {
                  var panelData = Alpine.$data(panelEl);
                  if (panelData && panelData._scrollToBottom) panelData._scrollToBottom();
                }
              });
            }
          });
        },

        destroy: function () {
          if (window._designPage === this) window._designPage = null;
          if (window._designSeriesCleanup) {
            window._designSeriesCleanup();
            window._designSeriesCleanup = null;
          }
          this._tmuxSession = null;
          this.chatConnected = false;
          this.isLive = false;
        },

        // ── Data loading ──────────────────────────────────────────────────

        _load: async function () {
          this.state = 'loading';
          try {
            var resp = await fetch('/api/design/' + this.revisionId + '/full');
            var data = await resp.json();
            if (data.error) {
              this.state = 'error';
              return;
            }
            this.design = data;
            this.designId = data.design_id || this.revisionId;
            var revisions = data.revisions || [];
            this.iterCount = revisions.length || 1;
            this.iterIndex = revisions.length > 0 ? revisions.indexOf(this.revisionId) : 0;
            if (this.iterIndex < 0) this.iterIndex = revisions.length - 1;
            this.state = 'ready';

            // Migrate localStorage from revision-scoped to design-scoped
            this._migrateLocalStorage();

            // Restore chat state from design-scoped key
            this.chatOpen = localStorage.getItem('design-chatOpen-' + this.designId) === 'true';

            // Post-render: inject iframe content
            this.$nextTick(function () { this._injectIframe(data); }.bind(this));

            // Auto-reconnect Chat With if session was previously selected
            this._checkChatWith();

            // SSE subscription for new design iterations
            this._subscribeToDesign();
          } catch (e) {
            console.error('[designPage] load error', e);
            this.state = 'error';
          }
        },

        // ── localStorage migration (revision-scoped → design-scoped) ──────

        _migrateLocalStorage: function () {
          // Migrate chat open state
          var oldKey = 'design-chatOpen-' + this.revisionId;
          var newKey = 'design-chatOpen-' + this.designId;
          if (this.revisionId !== this.designId) {
            var oldVal = localStorage.getItem(oldKey);
            if (oldVal !== null && localStorage.getItem(newKey) === null) {
              localStorage.setItem(newKey, oldVal);
            }
          }
          // Migrate saved chat session
          var oldChatKey = 'design-chat-' + this.revisionId;
          var newChatKey = 'design-chat-' + this.designId;
          if (this.revisionId !== this.designId) {
            var oldChatVal = localStorage.getItem(oldChatKey);
            if (oldChatVal !== null && localStorage.getItem(newChatKey) === null) {
              localStorage.setItem(newChatKey, oldChatVal);
            }
          }
        },

        // ── Iframe injection (single iframe, latest variant) ──────────────

        _injectIframe: function (data) {
          var variants = data.variants || [];
          var v = variants.length > 0 ? variants[variants.length - 1] : null;
          if (!v) return;

          var iframe = document.getElementById('design-iframe');
          if (!iframe) return;
          var doc = iframe.contentDocument || iframe.contentWindow.document;

          var parentCSS = (document.querySelector('style') || {}).textContent || '';
          var safeHtml = v.html || '';

          // Parse fixture — multi-state fixtures get a picker bar
          var fixtureRaw = data.fixture || '{}';
          var fixtureObj;
          try { fixtureObj = JSON.parse(fixtureRaw); } catch (e) { fixtureObj = null; }

          var alpineHead, pickerHtml = '';
          if (fixtureObj && fixtureObj.states && typeof fixtureObj.states === 'object' &&
              Object.keys(fixtureObj.states).length > 0) {
            var stateKeys = Object.keys(fixtureObj.states);
            var firstState = fixtureObj.states[stateKeys[0]];
            alpineHead = '<script>window.FIXTURE = ' + JSON.stringify(firstState) + ';' +
              'window.FIXTURE_STATES = ' + JSON.stringify(fixtureObj.states) + ';<\/script>' +
              '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"><\/script>';
            pickerHtml = _buildStatePickerHtml(stateKeys);
          } else {
            alpineHead = '<script>window.FIXTURE = ' + fixtureRaw + ';<\/script>' +
              '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"><\/script>';
          }

          doc.open();
          doc.write('<!DOCTYPE html><html><head><meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
            '<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"><\/script>' +
            '<style>' + parentCSS + '</style>' +
            '<style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#111827;color:#e5e7eb;}</style>' +
            alpineHead +
            '</head><body>' + safeHtml + pickerHtml + '</body></html>');
          doc.close();

          // Auto-capture after render
          var revisionId = this.revisionId;
          setTimeout(function () { captureTabScreenshot(revisionId); }, 1500);
        },

        // ── Screenshot ────────────────────────────────────────────────────

        captureScreenshot: async function () {
          if (this.captureState === 'working') return; // prevent double-click
          this.captureState = 'working';
          var self = this;
          try {
            await manualCaptureScreenshot(this.revisionId, this._tmuxSession || '');
            self.captureState = 'success';
          } catch (e) {
            self.captureState = 'error';
          }
          setTimeout(function () { self.captureState = 'idle'; }, 3000);
        },

        // ── Primer injection ─────────────────────────────────────────────

        injectPrimer: async function () {
          if (this.primerState === 'working' || !this._tmuxSession) return;
          this.primerState = 'working';
          try {
            var res = await fetch('/api/chatwith/primer/design?context=' + this.revisionId);
            var data = await res.json();
            if (data.error) { console.error('Primer error:', data.error); this.primerState = 'idle'; return; }

            await fetch('/api/session/send', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ tmux_session: this._tmuxSession, message: data.primer_text }),
            });
            this.primerState = 'done';
            var self = this;
            setTimeout(function () { self.primerState = 'idle'; }, 5000);
          } catch (e) {
            console.error('Primer injection failed:', e);
            this.primerState = 'idle';
          }
        },

        // ── Iteration navigation ────────────────────────────────────────

        prevIteration: function () {
          var revisions = this.design && this.design.revisions;
          if (!revisions || this.iterIndex <= 0) return;
          navigateTo('/design/' + revisions[this.iterIndex - 1]);
        },

        nextIteration: function () {
          var revisions = this.design && this.design.revisions;
          if (!revisions || this.iterIndex >= revisions.length - 1) return;
          navigateTo('/design/' + revisions[this.iterIndex + 1]);
        },

        jumpToLatest: function () {
          var revisions = this.design && this.design.revisions;
          if (!revisions || revisions.length === 0) return;
          navigateTo('/design/' + revisions[revisions.length - 1]);
        },

        // ── Chat With session management ──────────────────────────────────

        _connectSession: function (sessionId) {
          this._tmuxSession = sessionId;
          this.chatConnected = true;
          this.isLive = true;
          localStorage.setItem('design-chat-' + this.designId, sessionId);
          initDisplayCapture(this.revisionId).catch(function () {});

          // Resolve project and label from picker data
          var session = this.chatSessions.find(function (s) { return s.id === sessionId; });
          this.chatSessionLabel = session ? (session.label || session.id) : sessionId;
          var project = session ? session.project : 'default';

          // Configure unified viewer after Alpine renders the x-if template.
          // setTimeout(100ms) needed: x-if="chatConnected" template doesn't exist
          // until Alpine processes the flag change. $nextTick fires before the child
          // component's x-init has run (Alpine timing race — see pitfall notes).
          var self = this;
          this.$nextTick(function () {
            setTimeout(function () {
              var panelEl = document.getElementById('design-chat-panel');
              if (panelEl) {
                var panelData = Alpine.$data(panelEl);
                if (panelData && panelData.configure) {
                  panelData.configure({
                    sessionId: sessionId,
                    project: project,
                    tmuxSession: sessionId,
                  });
                }
              }
            }, 100);
          });
        },

        disconnectSession: function () {
          this._tmuxSession = null;
          this.chatConnected = false;
          this.isLive = false;
          localStorage.removeItem('design-chat-' + this.designId);
          this._loadChatSessions();
        },

        _loadChatSessions: async function () {
          try {
            var data = await fetch('/api/dao/active_sessions').then(function (r) { return r.json(); });
            if (!Array.isArray(data)) data = [];
            this.chatSessions = data
              .filter(function (s) {
                var id = s.session_id || s.tmux_session || '';
                return !id.startsWith('chatwith-') && !id.startsWith('chat-');
              })
              .map(function (s) {
                var label = s.label || '';
                var lower = label.toLowerCase();
                var role = s.role || '';
                if (!role) {
                  if (lower.indexOf('coordinator') !== -1) role = 'Coordinator';
                  else if (lower.indexOf('reviewer') !== -1 || lower.indexOf('review') !== -1) role = 'Reviewer';
                  else if (lower.indexOf('builder') !== -1 || lower.indexOf('build') !== -1) role = 'Builder';
                  else if (lower.indexOf('designer') !== -1 || lower.indexOf('design') !== -1) role = 'Designer';
                  else if (lower.indexOf('validator') !== -1 || lower.indexOf('validat') !== -1) role = 'Reviewer';
                }
                var isHost = s.type === 'host';
                if (!role && isHost) role = 'Host';
                return {
                  id: s.session_id || s.tmux_session,
                  label: label,
                  role: role,
                  project: s.project || 'default',
                  isHost: isHost,
                  isLive: s.is_live !== false,
                  preview: (s.last_message || '').slice(0, 80).replace(/</g, '').replace(/>/g, ''),
                };
              })
              .sort(function (a, b) {
                if (a.label && !b.label) return -1;
                if (!a.label && b.label) return 1;
                return a.id.localeCompare(b.id);
              });
          } catch (_) { this.chatSessions = []; }
        },

        // ── Auto-reconnect Chat With ──────────────────────────────────────

        _checkChatWith: async function () {
          var savedSession = localStorage.getItem('design-chat-' + this.designId);
          if (savedSession) {
            // Verify saved session is still alive
            try {
              var data = await fetch('/api/dao/active_sessions').then(function (r) { return r.json(); });
              if (!Array.isArray(data)) data = [];
              var alive = false;
              for (var i = 0; i < data.length; i++) {
                var id = data[i].session_id || data[i].tmux_session;
                if (id === savedSession && data[i].is_live !== false) {
                  alive = true;
                  break;
                }
              }
              if (alive) {
                this._connectSession(savedSession);
                return;
              }
            } catch (_) {}
            // Saved session is dead — clear and fall through to picker
            localStorage.removeItem('design-chat-' + this.designId);
          }
          // No saved session — load picker options
          this._loadChatSessions();
        },

        // ── SSE design subscription ───────────────────────────────────────

        _subscribeToDesign: function () {
          var designId = this.designId;
          if (!designId) return;
          var self = this;
          var designTopic = 'design:' + designId;
          var handler = function (data) {
            var currentId = window.location.pathname.split('/design/')[1];
            if (!currentId || data.revision_id === currentId) return;
            if (!window.location.pathname.startsWith('/design/')) return;
            // New iteration: update counter and navigate
            self.iterCount = (data.revisions || []).length || self.iterCount + 1;
            navigateTo('/design/' + data.revision_id);
          };
          registerHandler(designTopic, handler);
          window._designSeriesCleanup = function () { unregisterHandler(designTopic, handler); };
        },

      };
    });
  });
})();
