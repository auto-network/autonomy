// Experiment page Alpine component — Unified toolbar (Design Studio experiment a8ee8212).
// 4-state toolbar: DISCONNECTED, PICKER, LIVE_UI, LIVE_CHAT.
// Kept: experiment fetch, single iframe injection, capture, Chat With integration, SSE series subscription.

(function () {

  function _expIdFromPath() {
    var m = window.location.pathname.match(/^\/experiments\/(.+)$/);
    return m ? m[1] : '';
  }

  document.addEventListener('alpine:init', function () {
    Alpine.data('experimentPage', function () {
      return {
        // State machine
        state: 'loading',   // 'loading' | 'ready' | 'error'

        // Experiment data
        expId: '',
        exp: null,
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
          window._experimentPage = this;
          this.expId = _expIdFromPath();
          this.chatOpen = localStorage.getItem('exp-chatOpen-' + this.expId) === 'true';
          this._load();
          var self = this;
          this.$watch('chatOpen', function (open) {
            localStorage.setItem('exp-chatOpen-' + self.expId, open ? 'true' : 'false');
            if (open && !self.chatConnected) self._loadChatSessions();
            if (open && self.chatConnected) {
              self.$nextTick(function () {
                var panelEl = document.getElementById('exp-chat-panel');
                if (panelEl) {
                  var panelData = Alpine.$data(panelEl);
                  if (panelData && panelData._scrollToBottom) panelData._scrollToBottom();
                }
              });
            }
          });
        },

        destroy: function () {
          if (window._experimentPage === this) window._experimentPage = null;
          if (window._expSeriesCleanup) {
            window._expSeriesCleanup();
            window._expSeriesCleanup = null;
          }
          this._tmuxSession = null;
          this.chatConnected = false;
          this.isLive = false;
        },

        // ── Data loading ──────────────────────────────────────────────────

        _load: async function () {
          this.state = 'loading';
          try {
            var resp = await fetch('/api/experiments/' + this.expId + '/full');
            var data = await resp.json();
            if (data.error) {
              this.state = 'error';
              return;
            }
            this.exp = data;
            var siblings = data.sibling_ids || [];
            this.iterCount = siblings.length || 1;
            this.iterIndex = siblings.length > 0 ? siblings.indexOf(this.expId) : 0;
            if (this.iterIndex < 0) this.iterIndex = siblings.length - 1;
            this.state = 'ready';

            // Post-render: inject iframe content
            this.$nextTick(function () { this._injectIframe(data); }.bind(this));

            // Auto-reconnect Chat With if session was previously selected
            this._checkChatWith();

            // SSE subscription for new series iterations
            this._subscribeToSeries();
          } catch (e) {
            console.error('[experimentPage] load error', e);
            this.state = 'error';
          }
        },

        // ── Iframe injection (single iframe, latest variant) ──────────────

        _injectIframe: function (data) {
          var variants = data.variants || [];
          var v = variants.length > 0 ? variants[variants.length - 1] : null;
          if (!v) return;

          var iframe = document.getElementById('exp-iframe');
          if (!iframe) return;
          var doc = iframe.contentDocument || iframe.contentWindow.document;

          var parentCSS = (document.querySelector('style') || {}).textContent || '';
          var safeHtml = v.html || '';
          var alpineHead = '<link rel="stylesheet" href="/static/css/session-cards.css">' +
            '<script>window.FIXTURE = ' + (data.fixture || '{}') + ';<\/script>' +
            '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"><\/script>';

          doc.open();
          doc.write('<!DOCTYPE html><html><head><meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
            '<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"><\/script>' +
            '<style>' + parentCSS + '</style>' +
            '<style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#111827;color:#e5e7eb;}</style>' +
            alpineHead +
            '</head><body>' + safeHtml + '</body></html>');
          doc.close();

          // Auto-capture after render
          var expId = this.expId;
          setTimeout(function () { captureTabScreenshot(expId); }, 1500);
        },

        // ── Screenshot ────────────────────────────────────────────────────

        captureScreenshot: async function () {
          if (this.captureState === 'working') return; // prevent double-click
          this.captureState = 'working';
          var self = this;
          try {
            await manualCaptureScreenshot(this.expId, this._tmuxSession || '');
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
            var res = await fetch('/api/chatwith/primer/experiment?context=' + this.expId);
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
          var siblings = this.exp && this.exp.sibling_ids;
          if (!siblings || this.iterIndex <= 0) return;
          navigateTo('/experiments/' + siblings[this.iterIndex - 1]);
        },

        nextIteration: function () {
          var siblings = this.exp && this.exp.sibling_ids;
          if (!siblings || this.iterIndex >= siblings.length - 1) return;
          navigateTo('/experiments/' + siblings[this.iterIndex + 1]);
        },

        jumpToLatest: function () {
          var siblings = this.exp && this.exp.sibling_ids;
          if (!siblings || siblings.length === 0) return;
          navigateTo('/experiments/' + siblings[siblings.length - 1]);
        },

        // ── Chat With session management ──────────────────────────────────

        _connectSession: function (sessionId) {
          this._tmuxSession = sessionId;
          this.chatConnected = true;
          this.isLive = true;
          localStorage.setItem('exp-chat-' + this.expId, sessionId);
          initDisplayCapture(this.expId).catch(function () {});

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
              var panelEl = document.getElementById('exp-chat-panel');
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
          localStorage.removeItem('exp-chat-' + this.expId);
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
          var savedSession = localStorage.getItem('exp-chat-' + this.expId);
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
            localStorage.removeItem('exp-chat-' + this.expId);
          }
          // No saved session — load picker options
          this._loadChatSessions();
        },

        // ── SSE series subscription ───────────────────────────────────────

        _subscribeToSeries: function () {
          var seriesId = this.exp && this.exp.series_id;
          if (!seriesId) return;
          var self = this;
          var seriesTopic = 'experiments:' + seriesId;
          var handler = function (data) {
            var currentId = window.location.pathname.split('/experiments/')[1];
            if (!currentId || data.experiment_id === currentId) return;
            if (!window.location.pathname.startsWith('/experiments/')) return;
            // New iteration: update counter and navigate
            self.iterCount = (data.sibling_ids || []).length || self.iterCount + 1;
            navigateTo('/experiments/' + data.experiment_id);
          };
          registerHandler(seriesTopic, handler);
          window._expSeriesCleanup = function () { unregisterHandler(seriesTopic, handler); };
        },

      };
    });
  });
})();
