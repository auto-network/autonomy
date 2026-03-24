// Experiment page Alpine component — Chrome v2.
// Full-bleed surface, control strip, persistent input bar, two-state chat toggle.
// Removed: variant selection, ranking, prev/next navigation, multi-iframe injection.
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

        // Chat toggle
        chatOpen: false,
        chatMessages: [],
        inputText: '',
        inputFocused: false,
        isLive: false,

        // Chat With session
        chatConnected: false,
        chatSessions: [],
        _tmuxSession: null,

        // Capture state: 'idle' | 'working' | 'success' | 'error'
        captureState: 'idle',

        // Primer injection
        primerInjecting: false,
        primerInjected: false,

        // ── Lifecycle ─────────────────────────────────────────────────────

        init: function () {
          window._experimentPage = this;
          this.expId = _expIdFromPath();
          this._load();
          var self = this;
          this.$watch('chatOpen', function (open) {
            if (open && !self.chatConnected) self._loadChatSessions();
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
            this.iterCount = (data.sibling_ids || []).length || 1;
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
          var isAlpine = !!(data.alpine);

          // For Alpine experiments: don't wrap scripts in load listener —
          // Alpine needs to run its init before DOM is parsed.
          var safeHtml = isAlpine ? (v.html || '') : (v.html || '').replace(
            /<script(?![^>]*\bsrc\b)([^>]*)>([\s\S]*?)<\/script>/gi,
            function (_, attrs, body) {
              return '<script' + attrs + '>window.addEventListener("load",function(){' + body + '});<\/script>';
            }
          );

          var alpineHead = isAlpine
            ? '<link rel="stylesheet" href="/static/css/session-cards.css">' +
              '<script>window.FIXTURE = ' + (data.fixture || '{}') + ';<\/script>' +
              '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"><\/script>'
            : '<script>window.FIXTURE = ' + (data.fixture || '{}') + ';<\/script>';

          doc.open();
          doc.write('<!DOCTYPE html><html><head><meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
            '<link rel="stylesheet" href="/static/tailwind.css">' +
            '<style>' + parentCSS + '</style>' +
            '<style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#111827;color:#e5e7eb;}</style>' +
            alpineHead +
            '</head><body>' + safeHtml + '</body></html>');
          doc.close();

          // Auto-capture after render
          var expId = this.expId;
          setTimeout(function () { captureTabScreenshot(expId); }, 1500);
        },

        // ── Chat ──────────────────────────────────────────────────────────

        sendMessage: async function () {
          var text = (this.inputText || '').trim();
          if (!text || !this.chatConnected) return;
          this.chatMessages.push({ id: Date.now(), role: 'user', text: text });
          this.inputText = '';

          // Auto-open chat if not already open
          if (!this.chatOpen) this.chatOpen = true;

          // Send to Chat With session via API
          if (this._tmuxSession) {
            try {
              await fetch('/api/session/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tmux_session: this._tmuxSession, message: text }),
              });
            } catch (e) {
              console.error('[experimentPage] sendMessage error', e);
            }
          }

          // Auto-scroll chat
          this.$nextTick(function () {
            var el = document.getElementById('chat-messages');
            if (el) el.scrollTop = el.scrollHeight;
          });
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
          if (this.primerInjecting || !this._tmuxSession) return;
          this.primerInjecting = true;
          try {
            var res = await fetch('/api/chatwith/primer/experiment?context=' + this.expId);
            var data = await res.json();
            if (data.error) { console.error('Primer error:', data.error); return; }

            await fetch('/api/session/send', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ tmux_session: this._tmuxSession, message: data.primer_text }),
            });
            this.primerInjected = true;
            var self = this;
            setTimeout(function () { self.primerInjected = false; }, 5000);
          } catch (e) {
            console.error('Primer injection failed:', e);
          } finally {
            this.primerInjecting = false;
          }
        },

        // ── Chat With session management ──────────────────────────────────

        _connectSession: function (sessionId) {
          this._tmuxSession = sessionId;
          this.chatConnected = true;
          this.isLive = true;
          localStorage.setItem('exp-chat-' + this.expId, sessionId);
          initDisplayCapture(this.expId).catch(function () {});
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
            var terminals = await fetch('/api/terminals').then(function (r) { return r.json(); });
            var dashSessions = [];
            try { dashSessions = await fetch('/api/active').then(function (r) { return r.json(); }); } catch (_) {}
            var dashMap = {};
            dashSessions.forEach(function (s) { dashMap[s.tmux_session || s.session_id] = s; });

            this.chatSessions = terminals
              .filter(function (t) { return t.alive; })
              .filter(function (t) { return !t.id.startsWith('chatwith-') && !t.id.startsWith('chat-'); })
              .map(function (t) {
                var dash = dashMap[t.id] || {};
                return {
                  id: t.id,
                  label: dash.label || '',
                  env: t.env || 'container',
                  preview: (dash.last_message || '').slice(0, 80),
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
              var resp = await fetch('/api/terminals');
              var terminals = await resp.json();
              var alive = false;
              for (var i = 0; i < terminals.length; i++) {
                if (terminals[i].id === savedSession && terminals[i].alive) {
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
