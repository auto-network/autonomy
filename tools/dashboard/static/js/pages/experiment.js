// Experiment page Alpine component.
// Manages experiment comparison UI: series navigation, variant cards,
// selection/ranking, Chat With panel toggle, screenshot status.
//
// The Chat With panel uses a session picker to select any live session,
// or spawn a new one. Session data is delivered via SSE (no polling).
// Screenshot capture triggers two-send image injection via the server.

(function () {

  function _expIdFromPath() {
    const m = window.location.pathname.match(/^\/experiments\/(.+)$/);
    return m ? m[1] : '';
  }

  document.addEventListener('alpine:init', () => {
    // In-memory store for Chat With session selection, keyed by experiment ID.
    // Survives SPA navigation (Alpine stores persist), lost only on full page reload.
    if (!Alpine.store('chatWith')) Alpine.store('chatWith', {});

    Alpine.data('experimentPage', () => ({
      // State machine
      state: 'loading',   // 'loading' | 'ready' | 'error'

      // Experiment data
      expId: '',
      exp: null,
      variants: [],
      isCompleted: false,

      // Series navigation
      seriesIdx: 0,
      seriesTotal: 1,
      prevId: null,
      nextId: null,

      // Selection state — plain object (not Map) for Alpine reactivity
      // { variantId: rank }
      selectedVariants: {},

      // Submit state
      submitBtnText: 'Submit Rankings',
      submitting: false,

      // Chat With panel
      chatWithVisible: false,
      chatWithCollapsed: false,
      chatWithStatus: '',
      chatWithStatusClass: 'text-xs text-gray-500 ml-2',
      chatWithBtnText: 'Chat With',
      chatWithBtnDisabled: false,
      chatWithPanelRef: null,  // reference to the chatWithPanel Alpine component

      // Session picker
      pickerVisible: false,
      pickerSessions: [],

      // Screenshot
      screenshotStatus: '',

      // ── Lifecycle ─────────────────────────────────────────────────────

      init() {
        window._experimentPage = this;
        this.expId = _expIdFromPath();
        this._injectHeaderActions();
        this._load();
      },

      destroy() {
        if (window._experimentPage === this) window._experimentPage = null;
        if (window._expSeriesCleanup) {
          window._expSeriesCleanup();
          window._expSeriesCleanup = null;
        }
        if (this.chatWithPanelRef) {
          this.chatWithPanelRef.destroy();
          this.chatWithPanelRef = null;
        }
        this.chatWithVisible = false;
      },

      // ── Data loading ──────────────────────────────────────────────────

      async _load() {
        this.state = 'loading';
        try {
          const data = await fetch(`/api/experiments/${this.expId}/full`).then(r => r.json());
          if (data.error) {
            this.state = 'error';
            return;
          }
          this._mapData(data);
          this.state = 'ready';

          // Post-render: inject iframe content once Alpine has rendered the skeleton.
          this.$nextTick(() => this._injectIframes());

          // Auto-reconnect Chat With if a session was previously selected
          this._checkChatWith();

          // SSE subscription for new series iterations
          this._subscribeToSeries();

          // If display stream is already active (from a previous Chat With), auto-capture
          if (!this.isCompleted && window._displayStream) {
            setTimeout(() => captureTabScreenshot(this.expId), 1500);
          }
        } catch (e) {
          console.error('[experimentPage] load error', e);
          this.state = 'error';
        }
      },

      _mapData(data) {
        this.exp = data;
        const variants = data.variants || [];
        this.isCompleted = data.status === 'completed';

        // Series navigation
        const siblingIds = data.sibling_ids || [this.expId];
        const idx = siblingIds.indexOf(this.expId);
        this.seriesIdx = idx >= 0 ? idx : 0;
        this.seriesTotal = siblingIds.length;
        this.prevId = this.seriesIdx > 0 ? siblingIds[this.seriesIdx - 1] : null;
        this.nextId = this.seriesIdx < this.seriesTotal - 1 ? siblingIds[this.seriesIdx + 1] : null;

        // Pre-compute rank options once (not inside x-for template expressions)
        const rankOptions = variants.map((_, i) => i + 1);

        // Selection state from completed results
        const sel = {};
        if (this.isCompleted) {
          variants.forEach(v => {
            if (v.selected && v.rank != null) sel[v.id] = v.rank;
          });
        }
        this.selectedVariants = sel;

        this.variants = variants.map(v => ({
          ...v,
          _rankOptions: rankOptions,
        }));
      },

      // ── Series navigation ─────────────────────────────────────────────

      get isInSeries() { return this.seriesTotal > 1; },

      // ── Selection ─────────────────────────────────────────────────────

      get selectedCount() { return Object.keys(this.selectedVariants).length; },

      isVariantSelected(vid) { return vid in this.selectedVariants; },

      variantRank(vid) { return this.selectedVariants[vid] || 1; },

      showRankFor(vid) {
        return this.selectedCount >= 2 && this.isVariantSelected(vid);
      },

      toggleVariant(vid) {
        if (this.isCompleted) return;
        const sel = { ...this.selectedVariants };
        if (vid in sel) {
          delete sel[vid];
        } else {
          sel[vid] = Object.keys(sel).length + 1;
        }
        this.selectedVariants = sel;
      },

      setRank(vid, rank) {
        if (!this.isVariantSelected(vid)) return;
        this.selectedVariants = { ...this.selectedVariants, [vid]: parseInt(rank) };
      },

      get selectionHint() {
        const count = this.selectedCount;
        if (count === 0) return 'Select variants to rank them';
        if (count === 1) return '1 selected \u2014 select more to rank, or submit as winner';
        return `${count} selected \u2014 set ranks and submit`;
      },

      get canSubmit() { return this.selectedCount > 0 && !this.submitting; },

      // ── Submit ────────────────────────────────────────────────────────

      async submit() {
        if (!this.canSubmit) return;
        this.submitting = true;
        this.submitBtnText = 'Submitting...';

        const selections = Object.entries(this.selectedVariants).map(([id, rank]) => ({ id, rank }));
        if (selections.length === 1) selections[0].rank = 1;

        try {
          const res = await fetch(`/api/experiments/${this.expId}/submit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ selections }),
          });
          const data = await res.json();
          if (data.ok) {
            this.submitBtnText = 'Submitted';
            const indicator = document.querySelector(`[data-exp-id="${this.expId}"]`);
            if (indicator) indicator.remove();
            setTimeout(() => this._reload(), 500);
          } else {
            this.submitting = false;
            this.submitBtnText = 'Submit Rankings';
            alert('Failed to submit: ' + (data.error || 'unknown error'));
          }
        } catch (e) {
          this.submitting = false;
          this.submitBtnText = 'Submit Rankings';
        }
      },

      _reload() {
        if (window._expSeriesCleanup) {
          window._expSeriesCleanup();
          window._expSeriesCleanup = null;
        }
        if (this.chatWithPanelRef) {
          this.chatWithPanelRef.destroy();
          this.chatWithPanelRef = null;
        }
        this.chatWithVisible = false;
        this.chatWithBtnText = 'Chat With';
        this.chatWithBtnDisabled = false;
        this.selectedVariants = {};
        this.submitting = false;
        this.submitBtnText = 'Submit Rankings';
        this._load();
      },

      // ── Chat With ─────────────────────────────────────────────────────

      _saveSession(sessionId, tmuxSession, project) {
        Alpine.store('chatWith')[this.expId] = {
          sessionId: sessionId,
          tmuxSession: tmuxSession,
          project: project,
        };
      },

      _loadSession() {
        return Alpine.store('chatWith')[this.expId] || null;
      },

      toggleChatWithPanel() {
        this.chatWithCollapsed = !this.chatWithCollapsed;
      },

      _configureChatWithPanel(sessionId, project, tmuxSession) {
        // Configure the chatWithPanel inner component once it's rendered.
        this.$nextTick(() => {
          if (this.chatWithPanelRef) {
            this.chatWithPanelRef.configure({
              sessionId: sessionId,
              project: project,
              tmuxSession: tmuxSession,
            });
          }
        });
      },

      // ── Session picker ──────────────────────────────────────────────

      openSessionPicker() {
        // Build list of live interactive sessions from the store
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
        // Sort by label presence, then alphabetically
        sessions.sort(function(a, b) {
          if (a.label && !b.label) return -1;
          if (!a.label && b.label) return 1;
          return (a.tmuxSession || '').localeCompare(b.tmuxSession || '');
        });
        this.pickerSessions = sessions;
        this.pickerVisible = true;
      },

      selectSession(session) {
        this.pickerVisible = false;
        this._saveSession(session.sessionId, session.tmuxSession, session.project);
        this.chatWithVisible = true;
        this.setChatWithStatus('connected', 'text-xs text-green-400 ml-2');
        this._configureChatWithPanel(session.sessionId, session.project, session.tmuxSession);
        initDisplayCapture(this.expId).catch(() => {});
      },

      async spawnNewSession() {
        this.pickerVisible = false;
        this.chatWithBtnText = 'Spawning...';
        this.chatWithBtnDisabled = true;
        this.setChatWithStatus('spawning...', 'text-xs text-yellow-400 ml-2');
        this.chatWithVisible = true;
        try {
          const res = await fetch('/api/chatwith/spawn', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ page_type: 'experiment', context_id: this.expId }),
          });
          const result = await res.json();
          if (result.error) {
            this.chatWithBtnText = 'Chat With';
            this.chatWithBtnDisabled = false;
            this.setChatWithStatus('Error: ' + result.error, 'text-xs text-red-400 ml-2');
            return;
          }
          var sessionName = result.session_name;
          // The spawn endpoint registers with project=context_id, session_id=session_name
          this._saveSession(sessionName, sessionName, this.expId);
          this.chatWithBtnText = 'Chat With';
          this.chatWithBtnDisabled = false;
          this.setChatWithStatus('connected', 'text-xs text-green-400 ml-2');
          this._configureChatWithPanel(sessionName, this.expId, sessionName);
          initDisplayCapture(this.expId).catch(() => {});
        } catch (e) {
          this.chatWithBtnText = 'Chat With';
          this.chatWithBtnDisabled = false;
          this.setChatWithStatus('spawn failed', 'text-xs text-red-400 ml-2');
          console.error('[experimentPage] spawnNewSession error', e);
        }
      },

      disconnectChatWith() {
        // Disconnect from the session (does NOT kill it — session continues independently)
        if (this.chatWithPanelRef) {
          this.chatWithPanelRef.destroy();
        }
        this.chatWithVisible = false;
        this.chatWithBtnText = 'Chat With';
        this.chatWithBtnDisabled = false;
        this.setChatWithStatus('', 'text-xs text-gray-500 ml-2');
        delete Alpine.store('chatWith')[this.expId];
      },

      reconnectChatWith() {
        var saved = this._loadSession();
        if (saved) {
          this.chatWithVisible = true;
          this.setChatWithStatus('connected', 'text-xs text-green-400 ml-2');
          this._configureChatWithPanel(saved.sessionId, saved.project, saved.tmuxSession);
        } else {
          this.openSessionPicker();
        }
      },

      // ── Screenshot ────────────────────────────────────────────────────
      // Now triggers two-send image injection via server when tmux session exists.

      async captureScreenshot() {
        var saved = this._loadSession();
        var tmux = saved ? saved.tmuxSession : '';
        await manualCaptureScreenshot(this.expId, tmux);
      },

      // ── Imperative → Alpine bridge ────────────────────────────────────

      setChatWithStatus(text, cls) {
        this.chatWithStatus = text;
        this.chatWithStatusClass = cls || 'text-xs text-gray-500 ml-2';
      },

      showChatWithPanel() {
        this.chatWithVisible = true;
      },

      setScreenshotStatus(msg) { this.screenshotStatus = msg; },

      // ── Iframe injection ──────────────────────────────────────────────
      // Called after Alpine renders the variant skeleton so DOM elements exist.

      _injectIframes() {
        const expId = this.expId;
        const exp = this.exp;
        const variants = this.variants;
        if (!variants.length) return;

        const _parentCSS = document.querySelector('style')?.textContent || '';
        let _loadCount = 0;

        variants.forEach(v => {
          const iframe = document.querySelector(`iframe[data-variant="${v.id}"]`);
          if (!iframe) return;
          const doc = iframe.contentDocument || iframe.contentWindow.document;

          // Wrap inline <script> bodies in a load listener so they execute after
          // Tailwind CDN has loaded and set up its MutationObserver.
          const _safeHtml = (v.html || '').replace(
            /<script(?![^>]*\bsrc\b)([^>]*)>([\s\S]*?)<\/script>/gi,
            (_, attrs, body) => `<script${attrs}>window.addEventListener("load",function(){${body}});<\/script>`
          );
          doc.open();
          doc.write(`<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/tailwind.css">
<style>${_parentCSS}</style>
<style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#111827;color:#e5e7eb;}</style>
</head><body>
<script>window.FIXTURE = ${exp.fixture || '{}'};<\/script>
${_safeHtml}
</body></html>`);
          doc.close();

          const resizeIframe = () => {
            try {
              const h = iframe.contentDocument.documentElement.scrollHeight;
              iframe.style.height = Math.max(200, Math.min(h, 800)) + 'px';
            } catch (e) {}
          };
          iframe.addEventListener('load', resizeIframe);
          // doc.write/close doesn't fire 'load' reliably — use timeouts too
          setTimeout(resizeIframe, 200);
          setTimeout(resizeIframe, 600);
          _loadCount++;
          if (_loadCount >= variants.length) {
            // All iframes injected — auto-capture after short delay for render
            setTimeout(() => captureTabScreenshot(expId), 1500);
          }
        });
      },

      // ── Auto-reconnect Chat With ──────────────────────────────────────

      async _checkChatWith() {
        var saved = this._loadSession();
        if (!saved) return;

        // Check if the saved session is still live in the store
        var store = Alpine.store('sessions')[saved.sessionId];
        if (store && store.isLive) {
          this.chatWithVisible = true;
          this.setChatWithStatus('connected', 'text-xs text-green-400 ml-2');
          this._configureChatWithPanel(saved.sessionId, saved.project, saved.tmuxSession);
          initDisplayCapture(this.expId).catch(() => {});
          return;
        }

        // Fallback: check via the chatwith/check API (session may not be in store yet)
        try {
          const check = await fetch(
            '/api/chatwith/check?session=' + encodeURIComponent(saved.tmuxSession)
          ).then(r => r.json());
          if (check && check.exists) {
            this.chatWithVisible = true;
            this.setChatWithStatus('connected', 'text-xs text-green-400 ml-2');
            this._configureChatWithPanel(saved.sessionId, saved.project, saved.tmuxSession);
            initDisplayCapture(this.expId).catch(() => {});
          } else {
            // Session no longer exists — clear saved state
            delete Alpine.store('chatWith')[this.expId];
          }
        } catch (e) { /* best-effort */ }
      },

      // ── SSE series subscription ───────────────────────────────────────

      _subscribeToSeries() {
        const seriesId = this.exp && this.exp.series_id;
        if (!seriesId) return;
        const seriesTopic = `experiments:${seriesId}`;
        const handler = (data) => {
          const currentId = window.location.pathname.split('/experiments/')[1];
          if (!currentId || data.experiment_id === currentId) return;
          if (!window.location.pathname.startsWith('/experiments/')) return;
          navigateTo(`/experiments/${data.experiment_id}`);
        };
        registerHandler(seriesTopic, handler);
        window._expSeriesCleanup = () => unregisterHandler(seriesTopic, handler);
      },

      // ── Header-actions injection ───────────────────────────────────

      _injectHeaderActions() {
        const ha = document.getElementById('header-actions');
        if (!ha) return;
        ha.innerHTML = `
          <span id="ha-screenshot-status" class="text-xs text-gray-400"></span>
          <button id="ha-capture-btn"
                  aria-label="Capture screenshot"
                  class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-500 hover:text-gray-300 hover:border-gray-500 transition-colors">
            Capture
          </button>
          <button id="ha-chatwith-btn"
                  aria-label="Open Chat With Claude panel"
                  class="px-3 py-1 bg-indigo-700 hover:bg-indigo-600 rounded text-sm text-white disabled:opacity-50">
          </button>
        `;

        // Wire click handlers via the component instance
        document.getElementById('ha-capture-btn').onclick = () => this.captureScreenshot();
        const cwBtn = document.getElementById('ha-chatwith-btn');
        cwBtn.onclick = () => {
          if (this.chatWithVisible) this.reconnectChatWith();
          else this.openSessionPicker();
        };

        // Sync reactive state to injected DOM via $watch
        cwBtn.textContent = this.chatWithBtnText;
        cwBtn.disabled = this.chatWithBtnDisabled;
        this.$watch('screenshotStatus', (val) => {
          const el = document.getElementById('ha-screenshot-status');
          if (el) el.textContent = val;
        });
        this.$watch('chatWithBtnText', (val) => {
          const el = document.getElementById('ha-chatwith-btn');
          if (el) el.textContent = val;
        });
        this.$watch('chatWithBtnDisabled', (val) => {
          const el = document.getElementById('ha-chatwith-btn');
          if (el) el.disabled = val;
        });
      },

      _clearHeaderActions() {
        const ha = document.getElementById('header-actions');
        if (ha) ha.innerHTML = '';
      },

    }));
  });
})();
