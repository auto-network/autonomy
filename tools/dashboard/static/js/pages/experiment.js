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
    // In-memory store for Chat With session selection, keyed by series_id (or expId for standalone).
    if (!Alpine.store('chatWith')) Alpine.store('chatWith', {});

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
        _tmuxSession: null,

        // Capture state: 'idle' | 'working' | 'success' | 'error'
        captureState: 'idle',

        // Session picker
        pickerVisible: false,
        pickerSessions: [],

        // ── Lifecycle ─────────────────────────────────────────────────────

        init: function () {
          window._experimentPage = this;
          this.expId = _expIdFromPath();
          this._load();
        },

        destroy: function () {
          if (window._experimentPage === this) window._experimentPage = null;
          if (window._expSeriesCleanup) {
            window._expSeriesCleanup();
            window._expSeriesCleanup = null;
          }
          this._tmuxSession = null;
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
          if (!text) return;
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

        // ── Chat With session management ──────────────────────────────────

        _connectChat: async function () {
          if (this._tmuxSession) return; // already connected
          try {
            var resp = await fetch('/api/chatwith/spawn', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ page_type: 'experiment', context_id: this.expId }),
            });
            var data = await resp.json();
            if (data.error) {
              console.error('[experimentPage] chat connect error', data.error);
              return;
            }
            this._connectSession(data.session_name);
          } catch (e) {
            console.error('[experimentPage] chat connect failed:', e);
          }
        },

        _chatWithKey: function () {
          return (this.exp && this.exp.series_id) || this.expId;
        },

        _saveSession: function (tmuxSession) {
          Alpine.store('chatWith')[this._chatWithKey()] = { tmuxSession: tmuxSession };
        },

        _loadSession: function () {
          return Alpine.store('chatWith')[this._chatWithKey()] || null;
        },

        _connectSession: function (tmuxSession) {
          this._tmuxSession = tmuxSession;
          this.isLive = true;
          this._saveSession(tmuxSession);
          initDisplayCapture(this.expId).catch(function () {});
        },

        // ── Session picker ──────────────────────────────────────────────

        openSessionPicker: function () {
          var allSessions = Alpine.store('sessions');
          var sessions = [];
          var interactiveTypes = ['terminal', 'chatwith', 'host', 'container'];
          for (var id in allSessions) {
            var s = allSessions[id];
            if (!s.isLive) continue;
            if (!s.tmuxSession) continue;
            if (interactiveTypes.indexOf(s.sessionType || 'terminal') === -1) continue;
            var lastEntry = s.entries.length > 0 ? s.entries[s.entries.length - 1] : null;
            sessions.push({
              sessionId: id,
              tmuxSession: s.tmuxSession,
              project: s.project || '',
              label: s.label || '',
              type: s.sessionType || 'terminal',
              preview: lastEntry ? (lastEntry.content || '').slice(0, 100) : '',
            });
          }
          sessions.sort(function (a, b) {
            if (a.label && !b.label) return -1;
            if (!a.label && b.label) return 1;
            return (a.tmuxSession || '').localeCompare(b.tmuxSession || '');
          });
          this.pickerSessions = sessions;
          this.pickerVisible = true;
        },

        selectSession: function (session) {
          this.pickerVisible = false;
          this._connectSession(session.tmuxSession);
        },

        spawnNewSession: async function () {
          this.pickerVisible = false;
          try {
            var res = await fetch('/api/chatwith/spawn', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ page_type: 'experiment', context_id: this.expId }),
            });
            var result = await res.json();
            if (result.error) {
              console.error('[experimentPage] spawn error', result.error);
              return;
            }
            this._connectSession(result.session_name);
          } catch (e) {
            console.error('[experimentPage] spawnNewSession error', e);
          }
        },

        // ── Auto-reconnect Chat With ──────────────────────────────────────

        _checkChatWith: async function () {
          var saved = this._loadSession();
          if (!saved || !saved.tmuxSession) {
            // No saved session — auto-spawn on first visit
            this._connectChat();
            return;
          }

          // Check if the saved session is still live in the store
          var allSessions = Alpine.store('sessions');
          for (var id in allSessions) {
            var s = allSessions[id];
            if (s.tmuxSession === saved.tmuxSession && s.isLive) {
              this._connectSession(saved.tmuxSession);
              return;
            }
          }

          // Fallback: check via the chatwith/check API
          try {
            var check = await fetch(
              '/api/chatwith/check?session=' + encodeURIComponent(saved.tmuxSession)
            ).then(function (r) { return r.json(); });
            if (check && check.exists) {
              this._connectSession(saved.tmuxSession);
            } else {
              delete Alpine.store('chatWith')[this._chatWithKey()];
              // Saved session is dead — auto-spawn a new one
              this._connectChat();
            }
          } catch (e) { /* best-effort */ }
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
