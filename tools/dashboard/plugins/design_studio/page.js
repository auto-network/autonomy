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
          this._destroyed = false;
          this._loadGen = 0;   // invalidates in-flight fetches on nav/destroy
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
          if (window._designPage === this) window._designPage = null;
          if (this._designSeriesCleanup) {
            this._designSeriesCleanup();
            this._designSeriesCleanup = null;
          }
          if (this._popstateHandler) {
            window.removeEventListener('popstate', this._popstateHandler);
            this._popstateHandler = null;
          }
          this._tmuxSession = null;
          this.chatConnected = false;
          this.isLive = false;
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

            // Restore chat state from design-scoped key
            this.chatOpen = localStorage.getItem('design-chatOpen-' + this.designId) === 'true';

            // Post-render: inject iframe content. Guard at EXECUTION time —
            // $nextTick callbacks are not canceled by destroy/supersede, and
            // _injectIframe writes into the global #design-iframe.
            this.$nextTick(function () {
              if (this._destroyed || gen !== this._loadGen) return;
              this._injectIframe(data);
            }.bind(this));

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
              '<script defer src="/static/vendor/alpine.min.js"><\/script>';
            pickerHtml = _buildStatePickerHtml(stateKeys);
          } else {
            alpineHead = '<script>window.FIXTURE = ' + fixtureRaw + ';<\/script>' +
              '<script defer src="/static/vendor/alpine.min.js"><\/script>';
          }

          doc.open();
          doc.write('<!DOCTYPE html><html><head><meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
            '<script src="/static/vendor/tailwind-browser.min.js"><\/script>' +
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
            history.pushState({}, '', '/design/' + newRevisionId);
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
    actionStates: {},
    topbarHandle: null,
    _loadTimer: null,
    _activeCacheKey: '',
    _sessionRegistryHandler: null,

    init: function () {
      this.mode = window.location.pathname === '/design' ? 'library' : 'viewer';
      if (this.mode === 'library') {
        var hydrated = this._hydrateCachedDesigns();
        this._updateTopbar();
        this.loadDesigns({ background: hydrated });
        this._refreshPresenceDesigns();
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
      return !!String(this.query || '').trim() || this.status !== 'pending';
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
      } catch (e) {
        this.error = 'Design catalog failed: ' + (e.message || e);
      } finally {
        this.loading = false;
      }
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
      return 'Refresh preview and summary';
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
      if (this.status && this.status !== 'all' && design.status !== this.status) return false;
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
      var org = (window.Autonomy && (window.Autonomy._activePluginOrg || window.Autonomy._activeShellOrg)) || 'autonomy';
      var active = this.summary.pending_series || 0;
      var total = this.summary.series || 0;
      var live = this._liveDesignSessions();
      var orgTitle = _escapeDesignHtml(org + ': ' + active + ' active designs, ' + total + ' total');
      var avatarHtml = live.slice(0, 3).map(function (s) {
        var initial = (s.label || s.id || '?').trim().charAt(0).toUpperCase() || '?';
        return '<span class="design-topbar-avatar" title="' + _escapeDesignHtml(s.label || s.id) + '">' + _escapeDesignHtml(initial) + '</span>';
      }).join('');
      var liveRows = live.map(function (s) {
        var initial = (s.label || s.id || '?').trim().charAt(0).toUpperCase() || '?';
        return '<a class="design-presence-row" href="/session/autonomy/' + encodeURIComponent(s.id) + '">'
          + '<span class="design-topbar-avatar is-row">' + _escapeDesignHtml(initial) + '</span>'
          + '<span class="design-presence-copy"><strong>' + _escapeDesignHtml(s.title || 'Untitled Design') + '</strong>'
          + '<span>' + _escapeDesignHtml(s.label || s.id) + '</span></span>'
          + '</a>';
      }).join('');
      var presenceBody = liveRows || '<div class="design-presence-empty">No live design sessions</div>';
      return '<span class="design-topbar-strip">'
        + '<span class="design-topbar-orgtile" title="' + orgTitle + '" aria-label="' + orgTitle + '">'
        + '<img src="/static/icon.svg" alt="">'
        + '<span class="design-topbar-badge">' + active + '</span>'
        + '</span>'
        + '<details class="design-topbar-presence' + (live.length ? ' is-live' : '') + '">'
        + '<summary title="' + live.length + ' live design sessions" aria-label="' + live.length + ' live design sessions">'
        + '<span class="design-topbar-live-dot"></span>'
        + '<span class="design-topbar-avatars">' + avatarHtml + '</span>'
        + '<span class="design-topbar-badge">' + live.length + '</span>'
        + '</summary>'
        + '<div class="design-presence-menu">' + presenceBody + '</div>'
        + '</details>'
        + '</span>';
    },

    _updateTopbar: function () {
      if (!window.Autonomy || !window.Autonomy.topbar
          || typeof window.Autonomy.topbar.set !== 'function') {
        return;
      }
      var options = {
        title: 'Design',
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
