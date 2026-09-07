// Design Studio page Alpine component — Unified toolbar (Design Studio experiment a8ee8212).
// 4-state toolbar: DISCONNECTED, PICKER, LIVE_UI, LIVE_CHAT.
// Kept: design fetch, single iframe injection, capture, Chat With integration, SSE design subscription.

(function () {

  function _linkedSessionFromQuery() {
    var value = new URLSearchParams(window.location.search || '').get('from_session') || '';
    value = value.trim();
    return /^[A-Za-z0-9._-]{1,160}$/.test(value) ? value : '';
  }

  function _revisionIdFromPath() {
    var m = window.location.pathname.match(/^\/design\/(.+)$/);
    return m ? m[1] : '';
  }

  function _linkedViewportRect() {
    var vv = window.visualViewport || null;
    var scrollX = Number(window.scrollX) || 0;
    var scrollY = Number(window.scrollY) || 0;
    var offsetLeft = vv && Number(vv.offsetLeft) || 0;
    var offsetTop = vv && Number(vv.offsetTop) || 0;
    var pageLeft = vv && Number.isFinite(Number(vv.pageLeft))
      ? Number(vv.pageLeft)
      : scrollX + offsetLeft;
    var pageTop = vv && Number.isFinite(Number(vv.pageTop))
      ? Number(vv.pageTop)
      : scrollY + offsetTop;
    var width = vv && Number(vv.width) > 0 ? Number(vv.width) : Number(window.innerWidth) || 0;
    var height = vv && Number(vv.height) > 0 ? Number(vv.height) : Number(window.innerHeight) || 0;
    return {
      left: Math.max(0, pageLeft),
      top: Math.max(0, pageTop),
      width: Math.max(0, width),
      height: Math.max(0, height),
    };
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

    // Pinned to the TOP of the iframe, not the bottom. The bottom band is
    // owned by realistic designs (composer / .sv-input) and, on mobile, by the
    // voice live-caption gutter — both of which buried this picker and made it
    // untappable. z-index is maxed so no overlay (caption gutter included) can
    // cover it.
    return '<div id="fixture-state-picker" style="position:fixed;top:12px;left:50%;transform:translateX(-50%);' +
      'display:flex;align-items:center;gap:2px;background:#0f172a;border:1px solid #1e293b;' +
      'border-radius:10px;padding:3px;box-shadow:0 4px 24px rgba(0,0,0,0.5);z-index:2147483000;">' +
      pills + '</div>' + script;
  }

  function _registerDesignPage() {
    if (!window.Alpine || window.__designStudioDesignPageRegistered) return;
    window.__designStudioDesignPageRegistered = true;
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
        linkedSessionId: '',

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

        // Series (plugin catalog row): revisions with their creator sessions
        // and the design's share state. Feeds the presence dropdown.
        series: null,
        shareState: 'idle',   // 'idle' | 'requesting' | 'awaiting' | 'error'
        shareError: '',
        _seriesGen: 0,
        _sharePollTimer: null,

        // Primer state: 'idle' | 'working' | 'done'
        primerState: 'idle',

        // ── Computed: toolbar state machine ─────────────────────────────
        get toolbarState() { return deriveToolbarState(this.chatOpen, this.chatConnected); },
        get toolbar() { return toolbarElements(this.toolbarState); },
        get canGoBack() { return this.iterIndex > 0; },
        get canGoForward() { return this.iterIndex < this.iterCount - 1; },
        get linkedSessionMode() { return !!this.linkedSessionId; },
        get linkedSessionHref() {
          var org = (this.design && this.design.org) || 'autonomy';
          return '/session/' + encodeURIComponent(org) + '/' + encodeURIComponent(this.linkedSessionId);
        },
        get linkedSessionStore() {
          if (!this.linkedSessionId || !Alpine.store) return null;
          var sessions = Alpine.store('sessions') || {};
          return sessions[this.linkedSessionId] || null;
        },
        get linkedSessionLive() {
          return !!(this.linkedSessionStore && this.linkedSessionStore.isLive);
        },
        get linkedSessionLabel() {
          var store = this.linkedSessionStore;
          return (store && store.label) || this.linkedSessionId;
        },

        // ── Presence: every session that pushed a revision, newest first ──
        get presenceSessions() {
          var revisions = (this.series && this.series.revisions) || [];
          var sessions = (window.Alpine && Alpine.store && Alpine.store('sessions')) || {};
          var org = (this.design && this.design.org) || 'autonomy';
          var byId = {};
          var order = [];
          for (var i = 0; i < revisions.length; i++) {
            var r = revisions[i];
            var id = r && r.creator_session_id;
            if (!id) continue;
            if (!byId[id]) {
              byId[id] = { id: id, count: 0, last_push: '', label: r.creator_session_label || id };
              order.push(id);
            }
            byId[id].count += 1;
            if (String(r.created_at || '') > String(byId[id].last_push || '')) byId[id].last_push = r.created_at || '';
          }
          return order.map(function (id) {
            var s = byId[id];
            var live = sessions[id] || null;
            s.live = !!(live && live.isLive);
            if (live && live.label) s.label = live.label;
            s.initial = (s.label || id).trim().charAt(0).toUpperCase() || '?';
            s.href = '/session/' + encodeURIComponent(org) + '/' + encodeURIComponent(id);
            return s;
          }).sort(function (a, b) {
            if (a.live !== b.live) return a.live ? -1 : 1;
            return String(b.last_push).localeCompare(String(a.last_push));
          });
        },
        get presenceLive() {
          return this.presenceSessions.some(function (s) { return s.live; });
        },
        get presenceSummaryTitle() {
          var n = this.presenceSessions.length;
          var live = this.presenceSessions.filter(function (s) { return s.live; }).length;
          var text = n === 0 ? 'No sessions on this design' : (n + (n === 1 ? ' session' : ' sessions') + (live ? ', ' + live + ' live' : ''));
          return text + (this.share.shared ? ' · shared by link' : ' · not shared');
        },
        get share() {
          var share = this.series && this.series.share;
          return share && typeof share === 'object' ? share : { shared: false, grants: [] };
        },
        get primaryGrant() {
          var grants = this.share.grants || [];
          return grants.length ? grants[0] : null;
        },
        get shareExpiryText() {
          var grant = this.primaryGrant;
          if (!grant) return '';
          if (!grant.expires_at) return 'no expiry';
          var ms = grant.expires_at * 1000 - Date.now();
          if (ms <= 0) return 'expired';
          var days = Math.floor(ms / 86400000);
          if (days >= 1) return 'expires in ' + days + (days === 1 ? ' day' : ' days');
          var hours = Math.max(1, Math.floor(ms / 3600000));
          return 'expires in ' + hours + (hours === 1 ? ' hour' : ' hours');
        },

        // ── Lifecycle ─────────────────────────────────────────────────────

        init: function () {
          window._designPage = this;
          this._destroyed = false;
          this._loadGen = 0;   // invalidates in-flight fetches on nav/destroy
          this.linkedSessionId = _linkedSessionFromQuery();
          document.body.classList.toggle('route-design-linked', this.linkedSessionMode);
          if (this.linkedSessionMode) this._bindLinkedViewport();
          this.revisionId = _revisionIdFromPath();
          this._load();
          var self = this;
          // Browser back/forward: swap revision without full reload
          this._popstateHandler = function () {
            var rid = _revisionIdFromPath();
            if (rid && rid !== self.revisionId) self._swapRevision(rid);
          };
          window.addEventListener('popstate', this._popstateHandler);

          this.$watch('chatOpen', function (open) {
            if (self.linkedSessionMode) return;
            localStorage.setItem('design-chatOpen-' + self.designId, open ? 'true' : 'false');
            // Mirror chat-open into the panel viewer so it switches between the
            // active composer + pending/dictation tile (open) and the passive
            // caption gutter (collapsed). The panel stays mounted while hidden,
            // so this fires for both directions. See session-viewer.js
            // _composerActive / _panelChatOpen.
            var panelEl = document.getElementById('design-chat-panel');
            if (panelEl) {
              var pd = Alpine.$data(panelEl);
              if (pd) pd._panelChatOpen = open;
            }
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
          this._destroyed = true;
          this._stopSharePoll();
          document.body.classList.remove('route-design-linked');
          if (window._designPage === this) window._designPage = null;
          if (this._designSeriesCleanup) {
            this._designSeriesCleanup();
            this._designSeriesCleanup = null;
          }
          if (this._popstateHandler) {
            window.removeEventListener('popstate', this._popstateHandler);
            this._popstateHandler = null;
          }
          this._unbindLinkedViewport();
          this._tmuxSession = null;
          this.chatConnected = false;
          this.isLive = false;
        },

        // iOS can move the visual viewport after status-bar, keyboard, or app
        // transitions without moving the layout viewport with it. Anchor this
        // full-bleed shell to the live visual rectangle so the whole surface
        // cannot drift upward and leave an equal gutter at the bottom.
        _syncLinkedViewport: function () {
          if (!this.linkedSessionMode || !this.$root || !this.$root.style) return;
          var rect = _linkedViewportRect();
          this.$root.style.setProperty('--design-viewport-left', rect.left + 'px');
          this.$root.style.setProperty('--design-viewport-top', rect.top + 'px');
          this.$root.style.setProperty('--design-viewport-width', rect.width + 'px');
          this.$root.style.setProperty('--design-viewport-height', rect.height + 'px');
        },

        _bindLinkedViewport: function () {
          if (this._linkedViewportHandler) return;
          var self = this;
          this._linkedViewportHandler = function () {
            if (self._linkedViewportFrame) window.cancelAnimationFrame(self._linkedViewportFrame);
            self._linkedViewportFrame = window.requestAnimationFrame(function () {
              self._linkedViewportFrame = null;
              self._syncLinkedViewport();
            });
          };
          var vv = window.visualViewport;
          if (vv && vv.addEventListener) {
            vv.addEventListener('resize', this._linkedViewportHandler);
            vv.addEventListener('scroll', this._linkedViewportHandler);
          }
          window.addEventListener('resize', this._linkedViewportHandler);
          window.addEventListener('orientationchange', this._linkedViewportHandler);
          window.addEventListener('pageshow', this._linkedViewportHandler);
          window.addEventListener('scroll', this._linkedViewportHandler);
          this._syncLinkedViewport();
        },

        _unbindLinkedViewport: function () {
          if (this._linkedViewportFrame) {
            window.cancelAnimationFrame(this._linkedViewportFrame);
            this._linkedViewportFrame = null;
          }
          if (!this._linkedViewportHandler) return;
          var vv = window.visualViewport;
          if (vv && vv.removeEventListener) {
            vv.removeEventListener('resize', this._linkedViewportHandler);
            vv.removeEventListener('scroll', this._linkedViewportHandler);
          }
          window.removeEventListener('resize', this._linkedViewportHandler);
          window.removeEventListener('orientationchange', this._linkedViewportHandler);
          window.removeEventListener('pageshow', this._linkedViewportHandler);
          window.removeEventListener('scroll', this._linkedViewportHandler);
          this._linkedViewportHandler = null;
        },

        // ── Data loading ──────────────────────────────────────────────────

        _load: async function () {
          var gen = ++this._loadGen;
          this.state = 'loading';
          try {
            var resp = await fetch('/api/design/' + this.revisionId + '/full');
            if (this._destroyed || gen !== this._loadGen) return;  // navigated/destroyed mid-fetch
            var data = await resp.json();
            if (this._destroyed || gen !== this._loadGen) return;
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

            // Session-linked entry is a focused canvas, not a chat workspace.
            // Direct/library entry retains the existing design-scoped state.
            this.chatOpen = this.linkedSessionMode
              ? false
              : localStorage.getItem('design-chatOpen-' + this.designId) === 'true';

            // Post-render: inject iframe content. Guard at EXECUTION time —
            // $nextTick callbacks are not canceled by destroy/supersede, and
            // _injectIframe writes into the global #design-iframe.
            this.$nextTick(function () {
              if (this._destroyed || gen !== this._loadGen) return;
              this._injectIframe(data);
            }.bind(this));

            // Auto-reconnect Chat With only in the full workspace. The linked
            // viewer deliberately never mounts the picker/chat state machine.
            if (!this.linkedSessionMode) this._checkChatWith();

            // SSE subscription for new design iterations
            this._subscribeToDesign();
            this._loadSeries();
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
              '<script defer src="/static/vendor/alpine-3.15.12.min.js"><\/script>';
            pickerHtml = _buildStatePickerHtml(stateKeys);
          } else {
            alpineHead = '<script>window.FIXTURE = ' + fixtureRaw + ';<\/script>' +
              '<script defer src="/static/vendor/alpine-3.15.12.min.js"><\/script>';
          }

          doc.open();
          doc.write('<!DOCTYPE html><html><head><meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
            '<script src="/static/vendor/tailwind-browser-4.3.3.min.js"><\/script>' +
            '<style>' + parentCSS + '</style>' +
            '<style>html,body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#111827;color:#e5e7eb;overflow:auto !important;}</style>' +
            alpineHead +
            '</head><body>' + safeHtml + pickerHtml +
            '<script>document.addEventListener("wheel",function(e){' +
            'var el=document.scrollingElement||document.documentElement;' +
            'el.scrollBy(0,e.deltaY);' +
            '},{passive:true});<\/script>' +
            '</body></html>');
          doc.close();

          // Auto-capture after render
          var revisionId = this.revisionId;
          setTimeout(function () { captureTabScreenshot(revisionId); }, 1500);
        },

        // ── Series: presence + share state ────────────────────────────────

        _loadSeries: async function () {
          if (!this.designId) return;
          var gen = ++this._seriesGen;
          try {
            var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
            var res = await fetcher('/api/design-studio/designs/' + encodeURIComponent(this.designId));
            if (this._destroyed || gen !== this._seriesGen || !res.ok) return;
            var data = await res.json();
            if (this._destroyed || gen !== this._seriesGen) return;
            this.series = data;
            if (this.share.shared && this.shareState === 'awaiting') {
              this.shareState = 'idle';
              this._stopSharePoll();
            }
          } catch (e) { /* the dropdown keeps its last known state */ }
        },

        onPresenceToggle: function (event) {
          var el = event && event.target;
          if (el && el.open) this._loadSeries();
        },

        formatPush: function (value) {
          if (!value) return 'unknown';
          var raw = String(value).trim();
          var normalized = raw.indexOf('T') >= 0 ? raw : raw.replace(' ', 'T') + 'Z';
          var parsed = new Date(normalized);
          if (isNaN(parsed.getTime())) return raw;
          var diff = Date.now() - parsed.getTime();
          if (diff < 60000) return 'just now';
          if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
          if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
          return parsed.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        },

        openPresenceSession: function (s) {
          if (!s || !s.href) return;
          navigateTo(s.href);
        },

        chatWithSession: function (s) {
          if (!s || !s.id || this.linkedSessionMode) return;
          this.chatOpen = true;
          if (this._tmuxSession !== s.id) this._connectSession(s.id);
        },

        // Sharing rides the existing link_publish approval: nothing is minted
        // until the operator approves in Central. The dropdown then polls the
        // series until the grant shows up (or gives up quietly).
        shareDesign: async function () {
          if (this.shareState === 'requesting' || this.shareState === 'awaiting') return;
          this.shareError = '';
          this.shareState = 'requesting';
          try {
            var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
            var res = await fetcher('/api/approvals', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                kind: 'link_publish',
                session: 'dashboard-ui',
                request: {
                  org: (this.design && this.design.org) || 'autonomy',
                  target_type: 'design',
                  target_uuid: this.designId,
                  meta: {},
                },
              }),
            });
            var data = await res.json().catch(function () { return {}; });
            if (!res.ok || data.error) {
              this.shareState = 'error';
              this.shareError = data.error || ('Share request failed (HTTP ' + res.status + ')');
              return;
            }
            this.shareState = 'awaiting';
            if (data.id && typeof window.openApprovalOverlay === 'function') {
              try { window.openApprovalOverlay(data.id); } catch (e) { /* Central still has it */ }
            }
            this._startSharePoll();
          } catch (e) {
            this.shareState = 'error';
            this.shareError = 'Share request failed: ' + (e.message || e);
          }
        },

        _startSharePoll: function () {
          this._stopSharePoll();
          var self = this;
          var remaining = 40; // ~3 minutes at 4.5s
          var tick = function () {
            self._sharePollTimer = null;
            if (self._destroyed || self.shareState !== 'awaiting') return;
            self._loadSeries().then(function () {
              if (self._destroyed || self.shareState !== 'awaiting') return;
              if (--remaining <= 0) { self.shareState = 'idle'; return; }
              self._sharePollTimer = setTimeout(tick, 4500);
            });
          };
          this._sharePollTimer = setTimeout(tick, 4500);
        },

        _stopSharePoll: function () {
          if (this._sharePollTimer) { clearTimeout(this._sharePollTimer); this._sharePollTimer = null; }
        },

        shareLink: async function () {
          var grant = this.primaryGrant;
          if (!grant || !grant.url) return;
          var title = (this.design && this.design.title) || 'Design';
          try {
            if (navigator.share) { await navigator.share({ title: title, url: grant.url }); return; }
          } catch (e) { if (e && e.name === 'AbortError') return; }
          try {
            await navigator.clipboard.writeText(grant.url);
            this.shareError = '';
          } catch (e) {
            this.shareError = 'Could not copy the link: ' + grant.url;
          }
        },

        openShareLink: function () {
          var grant = this.primaryGrant;
          if (!grant || !grant.url) return;
          window.open(grant.url, '_blank', 'noopener');
        },

        manageShare: function () {
          var grant = this.primaryGrant;
          var org = (this.design && this.design.org) || 'autonomy';
          if (window.AutonomyOrgSettings && typeof window.AutonomyOrgSettings.open === 'function') {
            window.AutonomyOrgSettings.open(org, { screen: 'published-links', focus: grant ? grant.token : '' });
          }
        },

        // ── Screenshot ────────────────────────────────────────────────────

        captureScreenshot: async function () {
          if (this.captureState === 'working') return; // prevent double-click
          this.captureState = 'working';
          var self = this;
          try {
            var targetSession = this.linkedSessionLive
              ? this.linkedSessionId
              : (this._tmuxSession || '');
            await manualCaptureScreenshot(this.revisionId, targetSession);
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

        // ── Iteration navigation (soft swap — no full page reload) ──────

        _swapRevision: async function (newRevisionId) {
          if (newRevisionId === this.revisionId) return;
          var gen = ++this._loadGen;
          try {
            var resp = await fetch('/api/design/' + newRevisionId + '/full');
            if (this._destroyed || gen !== this._loadGen) return;  // navigated/destroyed mid-fetch
            var data = await resp.json();
            if (this._destroyed || gen !== this._loadGen) return;
            if (data.error) return;

            this.revisionId = newRevisionId;
            this.design = data;
            // Keep the design identity in sync with the body so the title bar
            // can never lag behind the revision shown; if the swap crossed into a
            // different design (e.g. browser back/forward), move the live
            // subscription onto that design's topic too.
            if (data.design_id && data.design_id !== this.designId) {
              this.designId = data.design_id;
              this._subscribeToDesign();
              this._loadSeries();
              // registerHandler can synchronously replay cached topic data and
              // re-enter _swapRevision (bumping _loadGen); bail if this call was
              // superseded so it can't schedule a stale injection or history push.
              if (this._destroyed || gen !== this._loadGen) return;
            }
            var revisions = data.revisions || [];
            this.iterCount = revisions.length || 1;
            this.iterIndex = revisions.indexOf(newRevisionId);
            if (this.iterIndex < 0) this.iterIndex = revisions.length - 1;

            // Re-inject iframe with new content. Guard at EXECUTION time —
            // $nextTick callbacks survive destroy/supersede and _injectIframe
            // writes into the global #design-iframe.
            this.$nextTick(function () {
              if (this._destroyed || gen !== this._loadGen) return;
              this._injectIframe(data);
            }.bind(this));

            // Update URL without navigation
            var nextPath = '/design/' + newRevisionId;
            if (this.linkedSessionMode) {
              nextPath += '?from_session=' + encodeURIComponent(this.linkedSessionId);
            }
            history.pushState({}, '', nextPath);
          } catch (e) {
            console.error('[designPage] revision swap failed', e);
          }
        },

        prevIteration: function () {
          var revisions = this.design && this.design.revisions;
          if (!revisions || this.iterIndex <= 0) return;
          this._swapRevision(revisions[this.iterIndex - 1]);
        },

        nextIteration: function () {
          var revisions = this.design && this.design.revisions;
          if (!revisions || this.iterIndex >= revisions.length - 1) return;
          this._swapRevision(revisions[this.iterIndex + 1]);
        },

        jumpToLatest: function () {
          var revisions = this.design && this.design.revisions;
          if (!revisions || revisions.length === 0) return;
          this._swapRevision(revisions[revisions.length - 1]);
        },

        returnToSession: function () {
          if (!this.linkedSessionId) return;
          navigateTo(this.linkedSessionHref);
        },

        returnToGallery: function () {
          navigateTo('/design');
        },

        // The Chat With picker (PICKER toolbar state) for connecting any live
        // session, not only one already on the design.
        openSessionPicker: function () {
          if (this.linkedSessionMode) return;
          this.chatOpen = true;
          if (!this.chatConnected) this._loadChatSessions();
        },

        // ── Chat With session management ──────────────────────────────────

        _connectSession: function (sessionId) {
          this._tmuxSession = sessionId;
          this.chatConnected = true;
          this.isLive = true;
          localStorage.setItem('design-chat-' + this.designId, sessionId);
          // Connecting a session never asks the browser for a screen-share
          // stream: the auto-reconnect path runs on every library open, and a
          // getDisplayMedia prompt on open is a cancel-every-time nuisance.
          // Only the capture button (manualCaptureScreenshot) may acquire one.

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
                  // Seed the panel's chat-open state on first connect so the
                  // composer/tile-vs-caption choice is correct before the next
                  // chatOpen toggle. (Watcher only fires on change.)
                  panelData._panelChatOpen = self.chatOpen;
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

        // --- Card helper methods (referenced by session-card.html partial) ---
        borderCls: function(t) { return window.sessionCardHelpers.borderCls(t); },
        typeBadge: function(t) { return window.sessionCardHelpers.typeBadge(t); },
        typeCls: function(t) { return window.sessionCardHelpers.typeCls(t); },
        turnsStr: function(s) { return window.sessionCardHelpers.turnsStr(s); },
        ctxStr: function(s) { return window.sessionCardHelpers.ctxStr(s); },
        idleStr: function(s) { return window.sessionCardHelpers.idleStr(s); },
        ctxWarn: function(s) { return window.sessionCardHelpers.ctxWarn(s); },
        recencyColor: function(s) { return window.sessionCardHelpers.recencyColor(s); },
        endedOrIdleLabel: function(s) { return window.sessionCardHelpers.endedOrIdleLabel(s); },
        endedOrIdleValue: function(s) { return window.sessionCardHelpers.endedOrIdleValue(s); },
        showTmuxColumn: function(s) { return window.sessionCardHelpers.showTmuxColumn(s); },

        _loadChatSessions: function () {
          var allSessions = Alpine.store('sessions');
          var results = [];
          for (var id in allSessions) {
            var s = allSessions[id];
            if (!s.isLive) continue;
            if (id.startsWith('chatwith-') || id.startsWith('chat-')) continue;
            var label = s.label || '';
            var lower = label.toLowerCase();
            var role = s.role || '';
            if (!role) {
              if (lower.indexOf('coordinator') !== -1) role = 'coordinator';
              else if (lower.indexOf('reviewer') !== -1 || lower.indexOf('review') !== -1) role = 'reviewer';
              else if (lower.indexOf('builder') !== -1 || lower.indexOf('build') !== -1) role = 'builder';
              else if (lower.indexOf('designer') !== -1 || lower.indexOf('design') !== -1) role = 'designer';
              else if (lower.indexOf('validator') !== -1 || lower.indexOf('validat') !== -1) role = 'reviewer';
            }
            if (role) role = role.charAt(0).toUpperCase() + role.slice(1);
            var storeType = s.sessionType || 'terminal';
            var sessionType = storeType === 'host' ? 'host'
              : storeType === 'chatwith' ? 'chatwith'
              : (storeType === 'container' && s.beadId) ? 'dispatch'
              : 'interactive';
            results.push({
              id: id,
              label: label,
              role: role,
              project: s.project || 'default',
              session_type: sessionType,
              is_live: true,
              // Startup-phase fields (auto-yfcoc). The shared session-card
              // partial derives a phase chip via window.Autonomy.lifecycle.*;
              // without these the derivation defaults to 'pending' and paints
              // a "Queued" startup chip on EVERY already-running session in
              // the picker. Pass the real store values so a session that has
              // actually started (resolved / composer_ready) resolves to
              // "ready" and shows no startup chrome.
              setup_phase: s.setupPhase,
              harness_phase: s.harnessPhase,
              harness_state: s.harnessState,
              resolved: s.resolved === true,
              entry_count: s.entryCount || s.entries.length,
              context_tokens: s.contextTokens || 0,
              last_activity: s.lastActivity || 0,
              created_at: s.startedAt || 0,
              tmux_session: id,
              nag_enabled: false,
              dispatch_nag_enabled: false,
              topics: [],
              latest: '',
              resumable: false,
              bead_id: s.beadId || '',
            });
          }
          // Most-recently-active first (descending last_activity), so the
          // session you're most likely to chat with sits at the top; fall back
          // to id for stable ordering when activity ties.
          this.chatSessions = results.sort(function (a, b) {
            var d = (b.last_activity || 0) - (a.last_activity || 0);
            return d !== 0 ? d : a.id.localeCompare(b.id);
          });
        },

        // ── Auto-reconnect Chat With ──────────────────────────────────────

        _checkChatWith: function () {
          // Your explicit picker choice (localStorage) wins; otherwise fall
          // back to the server's linked_session — the session running the
          // watch on this design (graph ui-design stamps it). Connect whichever
          // is live, else show the picker. No self-destruct: a transient
          // not-live no longer wipes the link.
          var saved = localStorage.getItem('design-chat-' + this.designId)
            || (this.design && this.design.linked_session);
          var s = saved && Alpine.store('sessions')[saved];
          if (s && s.isLive) { this._connectSession(saved); return; }
          this._loadChatSessions();
        },

        // ── SSE design subscription ───────────────────────────────────────

        _subscribeToDesign: function () {
          if (this._destroyed) return;
          // Idempotent: drop any prior subscription so re-subscribing (e.g. a
          // cross-design swap, or a fetch that resolved late) never leaves two
          // handlers registered.
          if (this._designSeriesCleanup) { this._designSeriesCleanup(); this._designSeriesCleanup = null; }
          var designId = this.designId;
          if (!designId) return;
          var self = this;
          var designTopic = 'design:' + designId;
          var handler = function (data) {
            if (self._destroyed) return;
            if (!data.revision_id || data.revision_id === self.revisionId) return;
            // Only swap for THIS design's own revisions — never let a revision
            // pushed to a different design hijack the view being watched.
            if (data.design_id && data.design_id !== self.designId) return;
            // New iteration of the current design: soft swap (no page reload)
            self._swapRevision(data.revision_id);
            self._loadSeries();
          };
          registerHandler(designTopic, handler);
          // Instance-scoped cleanup. A single shared global gets clobbered when
          // another design mounts before this one destroys, stranding this
          // handler — so a revision pushed to a previously-viewed design would
          // hijack the design currently on screen. Keep the unsubscribe here.
          this._designSeriesCleanup = function () { unregisterHandler(designTopic, handler); };
        },

      };
    });
  }

  document.addEventListener('alpine:init', _registerDesignPage);
  _registerDesignPage();
})();

function designStudioPage() {
  window.__designStudioLibraryCache = window.__designStudioLibraryCache || {};
  var librarianMemberKey = 'design.refresh-preview';
  return {
    mode: 'library',
    loading: false,
    error: '',
    actionError: '',
    designs: [],
    presenceDesigns: [],
    summary: {},
    filteredCount: 0,
    query: '',
    status: 'pending',
    sort: 'updated',
    formFactor: 'all',
    liveOnly: false,
    dynamicOnly: false,
    sharedOnly: false,
    org: 'all',
    remoteShares: [],
    renderStatus: {},
    actionStates: {},
    topbarHandle: null,
    _loadTimer: null,
    _renderPollTimer: null,
    _activeCacheKey: '',
    _sessionRegistryHandler: null,

    init: function () {
      this.mode = window.location.pathname === '/design' ? 'library' : 'viewer';
      if (this.mode === 'library') {
        var hydrated = this._hydrateCachedDesigns();
        this._updateTopbar();
        this.loadDesigns({ background: hydrated });
        this._refreshPresenceDesigns();
        this._pollRenderStatus();
        var self = this;
        this._sessionRegistryHandler = function () {
          self._updateTopbar();
        };
        if (window.registerHandler) {
          window.registerHandler('session:registry', this._sessionRegistryHandler);
        }
      }
    },

    destroy: function () {
      if (this._loadTimer) {
        clearTimeout(this._loadTimer);
        this._loadTimer = null;
      }
      if (this._renderPollTimer) {
        clearTimeout(this._renderPollTimer);
        this._renderPollTimer = null;
      }
      if (this.topbarHandle && typeof this.topbarHandle.destroy === 'function') {
        this.topbarHandle.destroy();
      }
      this.topbarHandle = null;
      if (this._sessionRegistryHandler && window.unregisterHandler) {
        window.unregisterHandler('session:registry', this._sessionRegistryHandler);
      }
      this._sessionRegistryHandler = null;
    },

    get hasFilters() {
      return !!String(this.query || '').trim() || this.status !== 'pending'
        || this.formFactor !== 'all' || this.liveOnly || this.dynamicOnly
        || this.sharedOnly || this.org !== 'all';
    },

    // Orgs present in the catalog; the strip shows the select only when
    // there is more than one to choose from.
    get orgOptions() {
      var seen = {};
      var out = [];
      (this.designs || []).forEach(function (d) {
        var org = (d && d.org) || 'autonomy';
        if (!seen[org]) { seen[org] = true; out.push(org); }
      });
      return out.sort();
    },

    // Designs shared with this org from other members' machines that match
    // the strip (query + org); shown as link-out tiles under Shared.
    get visibleRemoteShares() {
      var self = this;
      var q = String(this.query || '').trim().toLowerCase();
      return (this.remoteShares || []).filter(function (share) {
        if (!share) return false;
        if (self.org !== 'all' && (share.org || 'autonomy') !== self.org) return false;
        if (!q) return true;
        return [share.label, share.target_uuid, share.org].join(' ').toLowerCase().indexOf(q) >= 0;
      });
    },

    // The strip's client-side axes (form factor, live, dynamic) narrow the
    // server-filtered catalog; query/status/sort still round-trip so the
    // sessionStorage cache key stays honest.
    get visibleDesigns() {
      var self = this;
      return (this.designs || []).filter(function (design) {
        if (!design) return false;
        if (self.formFactor !== 'all' && (design.form_factor || '') !== self.formFactor) return false;
        if (self.liveOnly && !self.isLiveDesign(design)) return false;
        if (self.dynamicOnly && !design.has_fixture) return false;
        if (self.sharedOnly && !design.shared) return false;
        if (self.org !== 'all' && (design.org || 'autonomy') !== self.org) return false;
        return true;
      });
    },

    get formFactorOptions() {
      return [
        { value: 'all', label: 'All', title: 'Every design', icon: _formFactorIcon('all') },
        { value: 'both', label: 'Responsive', title: 'Renders at desktop and phone widths', icon: _formFactorIcon('both') },
        { value: 'desktop', label: 'Desktop', title: 'Desktop-only layouts', icon: _formFactorIcon('desktop') },
        { value: 'mobile', label: 'Phone', title: 'Phone mockups', icon: _formFactorIcon('mobile') },
      ];
    },

    // The host renderer is optional: sessions render the revisions they
    // push. The chip only warns when the host has work it cannot do.
    get renderChip() {
      var st = this.renderStatus || {};
      var queued = (st.pending || 0) + (st.current ? 1 : 0);
      if (st.available === false) return queued > 0 ? 'No host renderer' : 'Renders from sessions';
      if (queued > 0) return 'Rendering ' + queued;
      return '';
    },

    get renderChipWarns() {
      var st = this.renderStatus || {};
      return st.available === false && ((st.pending || 0) > 0 || !!st.current);
    },

    get renderChipTitle() {
      var st = this.renderStatus || {};
      if (st.available === false) {
        return 'The dashboard host has no headless browser (agent-browser), so thumbnails render from the session '
          + 'that pushes a design. To render on the host: npm install -g agent-browser && agent-browser install. '
          + 'To backfill from any session: python -m tools.dashboard.design_thumbnails --remote https://localhost:8080';
      }
      if (st.last_error) return 'Last render error: ' + st.last_error;
      return 'Thumbnails render headlessly on the dashboard';
    },

    setFormFactor: function (value) {
      this.formFactor = value || 'all';
    },

    toggleArchived: function () {
      this.status = this.status === 'pending' ? 'dismissed,completed' : 'pending';
      this.loadDesigns();
    },

    formFactorIcon: function (value) {
      return _formFactorIcon(value);
    },

    formFactorTitle: function (value) {
      return { both: 'Responsive: desktop and phone', desktop: 'Desktop only', mobile: 'Phone mockup' }[value] || '';
    },

    blankNote: function (design) {
      var st = this.renderStatus || {};
      var key = this._designActionKey(design, 'render');
      if (this.actionStates[key] === 'working' || this.actionStates[key] === 'done') return 'Rendering';
      if (st.available === false) return 'Renders on next push';
      if (st.current || (st.pending || 0) > 0) return 'Queued';
      return 'No preview yet';
    },

    renderActionTitle: function (design) {
      var state = this.actionStates[this._designActionKey(design, 'render')] || 'idle';
      if (state === 'working') return 'Queueing render';
      if (state === 'done') return 'Render queued';
      if (state === 'error') return 'Render failed';
      return 'Render thumbnail';
    },

    renderDesignThumbnail: async function (design) {
      if (!design || !design.latest_revision_id) return;
      var key = this._designActionKey(design, 'render');
      if (this.actionStates[key] === 'working') return;
      this.actionError = '';
      this.actionStates[key] = 'working';
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/design-studio/revisions/' + encodeURIComponent(design.latest_revision_id) + '/render', {
          method: 'POST',
        });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok || data.error) {
          this.actionStates[key] = 'error';
          this.actionError = data.error || ('Render failed (HTTP ' + res.status + ')');
          if (data.status) this.renderStatus = data.status;
          return;
        }
        this.actionStates[key] = 'done';
        if (data.status) this.renderStatus = data.status;
        this._pollRenderStatus();
      } catch (e) {
        this.actionStates[key] = 'error';
        this.actionError = 'Render failed: ' + (e.message || e);
      }
    },

    // While the renderer has work, poll its status and refresh the catalog
    // as thumbnails land; otherwise check once and go quiet.
    _pollRenderStatus: async function () {
      if (this._renderPollTimer) {
        clearTimeout(this._renderPollTimer);
        this._renderPollTimer = null;
      }
      if (window.location.pathname !== '/design') return;
      var previousRendered = (this.renderStatus || {}).rendered || 0;
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/design-studio/render/status');
        if (res.ok) this.renderStatus = await res.json();
      } catch (e) { /* the chip simply stays as it was */ }
      var st = this.renderStatus || {};
      var busy = !!st.current || (st.pending || 0) > 0;
      if ((st.rendered || 0) !== previousRendered) this.loadDesigns({ background: true });
      if (busy) {
        var self = this;
        this._renderPollTimer = setTimeout(function () {
          self._renderPollTimer = null;
          self._pollRenderStatus();
        }, 4000);
      }
    },

    get statusOptions() {
      return [
        { value: 'all', label: 'All statuses' },
        { value: 'pending', label: 'Active' },
        { value: 'dismissed', label: 'Dismissed' },
        { value: 'completed', label: 'Completed' },
      ];
    },

    get sortOptions() {
      return [
        { value: 'updated', label: 'Latest update' },
        { value: 'created', label: 'First created' },
        { value: 'revisions', label: 'Revision count' },
        { value: 'title', label: 'Title' },
      ];
    },

    scheduleLoad: function () {
      if (this._loadTimer) clearTimeout(this._loadTimer);
      var self = this;
      this._loadTimer = setTimeout(function () {
        self._loadTimer = null;
        self.loadDesigns();
      }, 180);
    },

    loadDesigns: async function (options) {
      options = options || {};
      var cacheKey = this._cacheKey();
      var hydrated = this._hydrateCachedDesigns(cacheKey);
      if (!options.background && !hydrated && this._activeCacheKey !== cacheKey) {
        this.designs = [];
        this.summary = {};
        this.filteredCount = 0;
      }
      this.loading = !hydrated && this.designs.length === 0;
      this.error = '';
      try {
        var params = new URLSearchParams();
        if (this.query) params.set('q', this.query);
        if (this.status && this.status !== 'all') params.set('status', this.status);
        if (this.sort) params.set('sort', this.sort);
        params.set('limit', '500');
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/design-studio/designs?' + params.toString());
        if (!res.ok) {
          this.error = 'Design catalog failed (HTTP ' + res.status + ')';
          return;
        }
        var data = await res.json();
        this._applyCatalogData(data, cacheKey);
        this._loadRemoteShares();
      } catch (e) {
        this.error = 'Design catalog failed: ' + (e.message || e);
      } finally {
        this.loading = false;
      }
    },

    _loadRemoteShares: async function () {
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/design-studio/shared');
        if (!res.ok) return;
        var data = await res.json();
        this.remoteShares = Array.isArray(data.shares) ? data.shares : [];
      } catch (e) { /* remote shares are an enhancement over the local catalog */ }
    },

    openRemoteShare: function (share) {
      if (!share || !share.url) return;
      window.open(share.url, '_blank', 'noopener');
    },

    openDesign: function (design) {
      if (!design || !design.latest_revision_id) return;
      navigateTo('/design/' + encodeURIComponent(design.latest_revision_id));
    },

    openSession: function (design) {
      if (!design || !design.creator_session_id) return;
      navigateTo('/session/autonomy/' + encodeURIComponent(design.creator_session_id));
    },

    setDesignStatus: async function (design, status) {
      if (!design || !design.design_id || !status) return;
      var key = this._designActionKey(design, 'status');
      if (this.actionStates[key] === 'working') return;
      this.actionError = '';
      this.actionStates[key] = 'working';
      var previous = Object.assign({}, design);
      var optimistic = Object.assign({}, design, { status: status });
      this._replaceOrRemoveDesign(optimistic);
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/design-studio/designs/' + encodeURIComponent(design.design_id) + '/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: status }),
        });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok || data.error) {
          this._replaceOrRemoveDesign(previous, { force: true });
          this.actionStates[key] = 'error';
          this.actionError = data.error || ('Status update failed (HTTP ' + res.status + ')');
          return;
        }
        if (data.design) this._replaceOrRemoveDesign(data.design);
        this.actionStates[key] = 'done';
        this.loadDesigns({ background: true });
      } catch (e) {
        this._replaceOrRemoveDesign(previous, { force: true });
        this.actionStates[key] = 'error';
        this.actionError = 'Status update failed: ' + (e.message || e);
      }
    },

    dispatchDesignLibrarian: async function (design) {
      if (!design || !design.latest_revision_id) return;
      var key = this._designActionKey(design, 'librarian');
      if (this.actionStates[key] === 'working') return;
      this.actionError = '';
      this.actionStates[key] = 'working';
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/agent-actions/dispatch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            asset_kind: 'design',
            asset_id: design.latest_revision_id,
            member_key: librarianMemberKey,
          }),
        });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok || data.error) {
          this.actionStates[key] = 'error';
          this.actionError = data.error || ('Librarian dispatch failed (HTTP ' + res.status + ')');
          return;
        }
        this.actionStates[key] = 'done';
      } catch (e) {
        this.actionStates[key] = 'error';
        this.actionError = 'Librarian dispatch failed: ' + (e.message || e);
      }
    },

    designActionState: function (design, action) {
      var state = this.actionStates[this._designActionKey(design, action)] || 'idle';
      return 'is-' + state;
    },

    isDesignActionBusy: function (design, action) {
      return this.actionStates[this._designActionKey(design, action)] === 'working';
    },

    designActionTitle: function (design) {
      var state = this.actionStates[this._designActionKey(design, 'librarian')] || 'idle';
      if (state === 'working') return 'Librarian is queued';
      if (state === 'done') return 'Librarian dispatched';
      if (state === 'error') return 'Librarian dispatch failed';
      return 'Refresh summary (librarian)';
    },

    showStatusPill: function (design) {
      return !!design && (!this.status || this.status === 'all' || design.status !== this.status);
    },

    statusLabel: function (status) {
      if (status === 'pending') return 'Active';
      return status || 'Active';
    },

    statusClass: function (status) {
      return 'design-pill design-pill-' + (status || 'pending');
    },

    formatDateTime: function (value) {
      if (!value) return 'unknown';
      var raw = String(value).trim();
      var normalized = raw.indexOf('T') >= 0 ? raw : raw.replace(' ', 'T') + 'Z';
      var parsed = new Date(normalized);
      if (isNaN(parsed.getTime())) return value;
      return parsed.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    },

    sessionLabel: function (design) {
      if (!design) return '';
      var live = this._liveSession(design.creator_session_id);
      return (live && live.label) || design.creator_session_label || design.creator_session_id || '';
    },

    isLiveDesign: function (design) {
      return !!(design && this._liveSession(design.creator_session_id));
    },

    _liveSession: function (sessionId) {
      if (!sessionId || !window.Alpine || !Alpine.store) return null;
      var sessions = Alpine.store('sessions') || {};
      var s = sessions[sessionId];
      return s && s.isLive ? s : null;
    },

    _liveDesignSessions: function () {
      var seen = {};
      var out = [];
      var source = this.presenceDesigns.length ? this.presenceDesigns : this.designs;
      for (var i = 0; i < source.length; i++) {
        var design = source[i];
        var id = design && design.creator_session_id;
        if (!id || seen[id]) continue;
        var live = this._liveSession(id);
        if (!live) continue;
        seen[id] = true;
        out.push({
          id: id,
          label: live.label || design.creator_session_label || id,
          title: design.title || 'Untitled Design',
        });
      }
      return out;
    },

    _refreshPresenceDesigns: async function () {
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var res = await fetcher('/api/design-studio/designs?status=pending&sort=updated&limit=500');
        if (!res.ok) return;
        var data = await res.json();
        this.presenceDesigns = Array.isArray(data.designs) ? data.designs : [];
        this._updateTopbar();
      } catch (e) {
        /* Presence is an enhancement; the visible catalog remains authoritative. */
      }
    },

    _cacheKey: function () {
      return [
        'design-library',
        String(this.query || ''),
        String(this.status || 'pending'),
        String(this.sort || 'updated'),
      ].join('|');
    },

    _designActionKey: function (design, action) {
      return String((design && design.design_id) || (design && design.latest_revision_id) || '') + ':' + action;
    },

    _designMatchesCurrentFilter: function (design) {
      if (!design) return false;
      if (this.status && this.status !== 'all'
          && String(this.status).split(',').indexOf(design.status) < 0) return false;
      return _matchesDesignQuery(design, this.query);
    },

    _replaceOrRemoveDesign: function (updated, options) {
      options = options || {};
      if (!updated || !updated.design_id) return;
      var found = false;
      var next = [];
      for (var i = 0; i < this.designs.length; i++) {
        var current = this.designs[i];
        if (current.design_id !== updated.design_id) {
          next.push(current);
          continue;
        }
        found = true;
        if (options.force || this._designMatchesCurrentFilter(updated)) {
          next.push(Object.assign({}, current, updated));
        }
      }
      if (!found && (options.force || this._designMatchesCurrentFilter(updated))) {
        next.unshift(updated);
      }
      this.designs = next;
      this.filteredCount = next.length;
      this._writeActiveCache();
      this._updateTopbar();
    },

    _applyCatalogData: function (data, cacheKey) {
      var key = cacheKey || this._cacheKey();
      this.designs = Array.isArray(data.designs) ? data.designs : [];
      this.summary = data.summary || {};
      this.filteredCount = data.filtered_count || this.designs.length;
      this._activeCacheKey = key;
      this._writeActiveCache();
      this._updateTopbar();
    },

    _writeActiveCache: function () {
      var key = this._activeCacheKey || this._cacheKey();
      var cached = {
        designs: this.designs,
        summary: this.summary,
        filtered_count: this.filteredCount,
        cached_at: Date.now(),
      };
      window.__designStudioLibraryCache[key] = cached;
      try {
        sessionStorage.setItem(key, JSON.stringify(cached));
      } catch (e) { /* storage quota/privacy mode: memory cache still works */ }
    },

    _hydrateCachedDesigns: function (cacheKey) {
      var key = cacheKey || this._cacheKey();
      var cached = window.__designStudioLibraryCache[key] || null;
      if (!cached) {
        try {
          cached = JSON.parse(sessionStorage.getItem(key) || 'null');
        } catch (e) {
          cached = null;
        }
      }
      if (!cached || !Array.isArray(cached.designs)) return false;
      this.designs = cached.designs;
      this.summary = cached.summary || {};
      this.filteredCount = cached.filtered_count || this.designs.length;
      this._activeCacheKey = key;
      this.loading = false;
      this._updateTopbar();
      return true;
    },

    _topbarStatsHtml: function () {
      var live = this._liveDesignSessions();
      var avatarHtml = live.slice(0, 3).map(function (s) {
        var initial = (s.label || s.id || '?').trim().charAt(0).toUpperCase() || '?';
        return '<span class="nx-avatar design-presence-avatar is-live" title="' + _escapeDesignHtml(s.label || s.id) + '">' + _escapeDesignHtml(initial) + '</span>';
      }).join('');
      var liveRows = live.map(function (s) {
        var initial = (s.label || s.id || '?').trim().charAt(0).toUpperCase() || '?';
        return '<a class="design-presence-row" href="/session/autonomy/' + encodeURIComponent(s.id) + '">'
          + '<span class="design-topbar-avatar is-row">' + _escapeDesignHtml(initial) + '</span>'
          + '<span class="design-presence-copy"><strong>' + _escapeDesignHtml(s.title || 'Untitled Design') + '</strong>'
          + '<span>' + _escapeDesignHtml(s.label || s.id) + '</span></span>'
          + '</a>';
      }).join('');
      var presenceBody = liveRows || '<div class="design-presence-empty">No live session is designing right now</div>';
      var title = live.length === 0 ? 'No live design sessions'
        : live.length + (live.length === 1 ? ' live design session' : ' live design sessions');
      return '<details class="design-topbar-presence is-topbar' + (live.length ? ' is-live' : '') + '" data-testid="design-topbar-presence">'
        + '<summary class="design-presence-pill" title="' + _escapeDesignHtml(title) + '" aria-label="' + _escapeDesignHtml(title) + '">'
        + '<span class="design-topbar-live-dot"></span>'
        + '<span class="nx-avatar-stack design-presence-stack" style="--nx-stack-cap: 3;">' + avatarHtml + '</span>'
        + (live.length > 3 ? '<span class="design-presence-count">+' + (live.length - 3) + '</span>' : '')
        + (live.length === 0 ? '<span class="design-presence-count is-empty">—</span>' : '')
        + '</summary>'
        + '<div class="design-presence-menu"><div class="design-presence-section">Designing now</div>' + presenceBody + '</div>'
        + '</details>';
    },

    _updateTopbar: function () {
      // Own the shared app topbar ONLY while on the Design library page.
      // session:registry and design-revision events keep calling this after
      // navigation (the component/handlers can outlive the page on mobile SPA
      // nav); without this guard they overwrite another page's topbar with the
      // Design one — the "design presence on /sessions" corruption. Off /design,
      // release our handle and no-op so the current page keeps its own topbar.
      if (window.location.pathname !== '/design') {
        if (this.topbarHandle && typeof this.topbarHandle.destroy === 'function') {
          this.topbarHandle.destroy();
        }
        this.topbarHandle = null;
        return;
      }
      if (!window.Autonomy || !window.Autonomy.topbar
          || typeof window.Autonomy.topbar.set !== 'function') {
        return;
      }
      var options = {
        title: 'Design Studio',
        left: [
          { type: 'html', id: 'design-stats', html: this._topbarStatsHtml() },
        ],
      };
      if (this.topbarHandle && typeof this.topbarHandle.update === 'function') {
        this.topbarHandle.update(options);
      } else {
        this.topbarHandle = window.Autonomy.topbar.set(options);
      }
    },
  };
}

function _formFactorIcon(value) {
  var stroke = 'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
  var desktop = '<svg viewBox="0 0 16 16" ' + stroke + '><rect x="1.5" y="2.5" width="13" height="8.5" rx="1.2"/><path d="M5.5 13.5h5M8 11v2.5"/></svg>';
  var phone = '<svg viewBox="0 0 16 16" ' + stroke + '><rect x="4.5" y="1.5" width="7" height="13" rx="1.4"/><path d="M7 12.5h2"/></svg>';
  if (value === 'desktop') return desktop;
  if (value === 'mobile') return phone;
  if (value === 'both') return desktop + phone;
  if (value === 'all') return '<svg viewBox="0 0 16 16" ' + stroke + '><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>';
  return '';
}

function _escapeDesignHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
    return {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch];
  });
}

function _matchesDesignQuery(design, query) {
  var q = String(query || '').trim().toLowerCase();
  if (!q) return true;
  var haystack = [
    design.design_id,
    design.latest_revision_id,
    design.title,
    design.description,
    design.creator_session_id,
    design.creator_session_label,
  ].map(function (value) {
    return String(value || '');
  }).join(' ').toLowerCase();
  return haystack.indexOf(q) >= 0;
}
