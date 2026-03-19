// Experiment page Alpine component.
// Manages experiment comparison UI: series navigation, variant cards,
// selection/ranking, Chat With panel toggle, screenshot status.
//
// Exposes window._experimentPage so imperative functions (connectChatWithTerminal,
// screenshot capture) can push reactive state updates without depending on Alpine
// internals — the same bridge pattern as window._terminalPage.
//
// Iframe injection, xterm.js Chat With terminal, display capture, and SSE
// subscription are kept imperative; Alpine manages the chrome (button states,
// panel visibility, selection/ranking logic, status text).

(function () {

  function _expIdFromPath() {
    const m = window.location.pathname.match(/^\/experiments\/(.+)$/);
    return m ? m[1] : '';
  }

  document.addEventListener('alpine:init', () => {
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
      chatWithReconnectVisible: false,
      chatWithKillVisible: false,

      // Screenshot
      screenshotStatus: '',

      // ── Lifecycle ─────────────────────────────────────────────────────

      init() {
        window._experimentPage = this;
        this.expId = _expIdFromPath();
        this._load();
      },

      destroy() {
        if (window._experimentPage === this) window._experimentPage = null;
        if (window._expSeriesCleanup) {
          window._expSeriesCleanup();
          window._expSeriesCleanup = null;
        }
        destroyChatWith();
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
          // $nextTick waits for Alpine's DOM flush before running the callback.
          this.$nextTick(() => this._injectIframes());

          // Auto-reconnect Chat With if a session already exists
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
        destroyChatWith();
        this.chatWithVisible = false;
        this.chatWithBtnText = 'Chat With';
        this.chatWithBtnDisabled = false;
        this.selectedVariants = {};
        this.submitting = false;
        this.submitBtnText = 'Submit Rankings';
        this._load();
      },

      // ── Chat With ─────────────────────────────────────────────────────

      get sessionCtx() { return (this.exp && this.exp.series_id) || this.expId; },

      get sessionName() { return `chatwith-${this.sessionCtx}`; },

      toggleChatWithPanel() {
        this.chatWithCollapsed = !this.chatWithCollapsed;
        if (!this.chatWithCollapsed) {
          // Re-fit xterm.js after expanding; _fitChatWithAddon is a helper in app.js
          setTimeout(() => _fitChatWithAddon(), 50);
        }
      },

      async spawnChatWith() {
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
            this.setChatWithStatus(`Error: ${result.error}`, 'text-xs text-red-400 ml-2');
            return;
          }
          this.chatWithBtnText = 'Reconnect';
          this.chatWithBtnDisabled = false;
          connectChatWithTerminal(result.session_name);
          initDisplayCapture(this.expId).catch(() => {});
        } catch (e) {
          this.chatWithBtnText = 'Chat With';
          this.chatWithBtnDisabled = false;
          this.setChatWithStatus('spawn failed', 'text-xs text-red-400 ml-2');
          console.error('[experimentPage] spawnChatWith error', e);
        }
      },

      async killChatWith() {
        const name = this.sessionName;
        destroyChatWith();
        this.chatWithVisible = false;
        this.chatWithKillVisible = false;
        this.chatWithBtnText = 'Chat With';
        this.chatWithBtnDisabled = false;
        try {
          await fetch(`/api/terminal/${name}/kill`);
        } catch (e) {
          console.warn('[experimentPage] killChatWith error', e);
        }
      },

      reconnectChatWith() {
        connectChatWithTerminal(this.sessionName);
      },

      // ── Screenshot ────────────────────────────────────────────────────

      async captureScreenshot() {
        await manualCaptureScreenshot(this.expId);
      },

      // ── Imperative → Alpine bridge ────────────────────────────────────
      // These methods are called by connectChatWithTerminal() and screenshot
      // functions in app.js to push state back into Alpine.

      setChatWithStatus(text, cls) {
        this.chatWithStatus = text;
        this.chatWithStatusClass = cls || 'text-xs text-gray-500 ml-2';
      },

      showChatWithPanel() {
        this.chatWithVisible = true;
      },

      setReconnectVisible(v) { this.chatWithReconnectVisible = v; },
      setKillVisible(v) { this.chatWithKillVisible = v; },
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
        try {
          const sessionName = this.sessionName;
          const check = await fetch(
            `/api/chatwith/check?session=${encodeURIComponent(sessionName)}`
          ).then(r => r.json());
          if (check && check.exists) {
            this.chatWithBtnText = 'Reconnect';
            connectChatWithTerminal(sessionName);
            initDisplayCapture(this.expId).catch(() => {});
          }
        } catch (e) { /* best-effort — session check is non-critical */ }
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

    }));
  });
})();
