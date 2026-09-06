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
  var FAST_OPEN_TAIL_LINES = 200;

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
      // Render the live voice buffer as a tile (no composer on this surface).
      _dictationTile: !!(opts && opts.dictationTile),
      // Page-global voice signals (body classes, body.dataset.svComposerSession,
      // voice.setViewedSession) describe "the viewer on this page" and assume
      // there is exactly one. A surface that mounts MANY viewers — the Session
      // Board, one per card — must not write them: every mount overwrote the
      // last, and the capsule reads that dataset to pick a retarget target
      // (voice-shell _capsuleClaimTarget → retargetDelivery), so the most
      // recently mounted card silently stole the microphone.
      _pageSignals: !(opts && opts.pageSignals === false),
      // Panel mode only: whether the design chat overlay is open. Driven by
      // design.js (off its chatOpen). Gates _composerActive so a collapsed
      // chat yields the passive caption instead of the active composer/tile.
      _panelChatOpen: true,

      // ── Page state ──────────────────────────────────────────────
      state: 'loading',   // 'loading' | 'ready' | 'error'
      errorMsg: '',

      // ── Session identity ────────────────────────────────────────
      sessionKey: '',       // store key (tmux_name)
      project: '',
      sessionId: '',
      projectLabel: '',
      _workspacePromise: null,

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
      get isClaudeHarness() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return !!(s && s.harness === 'claude');
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
      // auto-7v712 PART 1: build a row-shape compatible with the
      // window.Autonomy.lifecycle.* helpers from the current store
      // entry. Used by the pre-ready loading slot (session-view.html:5-18)
      // to render the SAME phase chip + tone as the list card —
      // visual parity, single derivation source. Returns null when the
      // store hasn't received a registry broadcast yet (the template
      // falls through to the legacy "Connecting to session..." text).
      get loadingPhaseRow() {
        var s = Alpine.store('sessions')[this.sessionKey];
        if (!s) {
          this._lcEmitSlot(null, 'no store row');
          return null;
        }
        var row = {
          is_live: s.isLive,
          harness: s.harness,
          startup_state: s.startupState || null,
          harness_state: s.harnessState,
          resumable: s.resumable,
          phase_progress: s.phaseProgress || null,
        };
        this._lcEmitSlot(row, null);
        return row;
      },
      // The title-bar Resume affordance shows only for a terminated
      // session whose JSONL still exists (mirrors the list card's
      // dead_resumable gate). _resumeMeta.resumable is the authoritative
      // backend signal fetched in configure().
      get canResume() {
        var m = this._resumeMeta;
        return !this.isLive && !!(m && m.resumable);
      },
      // Lifecycle row for the in-viewer resume boot indicator. Identical
      // to loadingPhaseRow EXCEPT resolved is pinned false: a resumed
      // session has historical JSONL, so the registry reports
      // resolved=true, which would short-circuit lifecycleState() straight
      // to "ready" and hide the boot phases entirely. With the unified
      // startup_state FSM, api_session_resume resets startup_state to
      // harness_starting on resume — no synthetic shaping needed in the
      // viewer's row.
      get _resumePhaseRow() {
        var s = Alpine.store('sessions')[this.sessionKey];
        if (!s) return null;
        return {
          is_live: s.isLive,
          harness: s.harness,
          startup_state: s.startupState || null,
          harness_state: s.harnessState,
          resumable: s.resumable,
          phase_progress: s.phaseProgress || null,
        };
      },
      // auto-ja51w C5: launching-until-first-response predicate. Mirrors
      // the lifecycle.js gate: a session is "still launching" while live
      // AND no entries have arrived AND not in a resumed-with-history
      // state. The viewer stays in state='loading' (pre-ready slot
      // rendered) until this predicate flips to false — at which point
      // the configure() flow OR the Alpine $watch in init() promotes
      // state to 'ready'.
      _isStillLaunching: function (store) {
        // Unified startup FSM. NULL = not in launching.
        return !!(store && store.isLive && store.startupState);
      },
      // [lc] viewer-loading slot emit helper. Logs a viewer-slot-render
      // record whenever the *result* of the slot's render condition
      // changes — distinguishes "state===loading but no row yet"
      // (early-mount no-data) from "state===loading and chip rendering"
      // from "state advanced past loading". Memoised on `this` so
      // repeated getter calls within the same Alpine reactivity tick
      // don't spam.
      _lcEmitSlot: function (row, noRowReason) {
        var L = window.Autonomy && window.Autonomy.lifecycle;
        if (!L || !L.emit || !L.phaseChip) return;
        var chip = row ? L.phaseChip(row) : null;
        var tone = row ? L.phaseTone(row) : null;
        var state = this.state;
        var sig = state + '|' + (row ? '1' : '0') + '|' + (chip || '') + '|' + (tone || '');
        if (this._lcSlotSig === sig) return;
        var prevSig = this._lcSlotSig;
        this._lcSlotSig = sig;
        L.emit({
          sid: this.sessionKey || this._tmuxSession || null,
          surface: 'viewer-loading',
          event: 'viewer-slot-render',
          from: prevSig || null,
          to: sig,
          state: state,
          has_row: !!row,
          chip_label: chip,
          chip_tone: tone,
          setup_phase: row ? row.setup_phase : null,
          harness_phase: row ? row.harness_phase : null,
          resolved: row ? row.resolved : null,
          reason: noRowReason || (state !== 'loading' ? 'state past loading' : null),
        });
      },
      // [lc] state-machine transition helper. Wraps every ``this.state =``
      // assignment so the timeline captures EVERY transition with the
      // caller's reason. Single point so we never miss one.
      _lcSetState: function (next, reason) {
        var prev = this.state;
        if (prev === next) return;
        this.state = next;
        var L = window.Autonomy && window.Autonomy.lifecycle;
        if (L && L.emit) {
          L.emit({
            sid: this.sessionKey || this._tmuxSession || null,
            surface: 'viewer-loading',
            event: 'state-machine',
            from: prev || null,
            to: next,
            reason: reason || null,
          });
        }
      },
      // [lc] viewer-loading slot emit helper. Logs a viewer-slot-render
      // record whenever the *result* of the slot's render condition
      // changes — distinguishes "state===loading but no row yet"
      // (early-mount no-data) from "state===loading and chip rendering"
      // from "state advanced past loading". Memoised on `this` so
      // repeated getter calls within the same Alpine reactivity tick
      // don't spam.
      _lcEmitSlot: function (row, noRowReason) {
        var L = window.Autonomy && window.Autonomy.lifecycle;
        if (!L || !L.emit || !L.phaseChip) return;
        var chip = row ? L.phaseChip(row) : null;
        var tone = row ? L.phaseTone(row) : null;
        var state = this.state;
        var sig = state + '|' + (row ? '1' : '0') + '|' + (chip || '') + '|' + (tone || '');
        if (this._lcSlotSig === sig) return;
        var prevSig = this._lcSlotSig;
        this._lcSlotSig = sig;
        L.emit({
          sid: this.sessionKey || this._tmuxSession || null,
          surface: 'viewer-loading',
          event: 'viewer-slot-render',
          from: prevSig || null,
          to: sig,
          state: state,
          has_row: !!row,
          chip_label: chip,
          chip_tone: tone,
          setup_phase: row ? row.setup_phase : null,
          harness_phase: row ? row.harness_phase : null,
          resolved: row ? row.resolved : null,
          reason: noRowReason || (state !== 'loading' ? 'state past loading' : null),
        });
      },
      // [lc] state-machine transition helper. Wraps every ``this.state =``
      // assignment so the timeline captures EVERY transition with the
      // caller's reason. Single point so we never miss one.
      _lcSetState: function (next, reason) {
        var prev = this.state;
        if (prev === next) return;
        this.state = next;
        var L = window.Autonomy && window.Autonomy.lifecycle;
        if (L && L.emit) {
          L.emit({
            sid: this.sessionKey || this._tmuxSession || null,
            surface: 'viewer-loading',
            event: 'state-machine',
            from: prev || null,
            to: next,
            reason: reason || null,
          });
        }
      },
      // Authoritative signal for "this viewer's bottom composer surface is
      // active" — the EXACT condition the composer (.sv-input) renders under
      // (session-view.html:289). The pending/outbox tile mounts in this same
      // surface, so this is the single source of truth the voice side keys
      // BOTH its caption-suppression and its send-path branch on (mirrored to
      // document.body via _syncComposerSignal). See contract note cbb8497c-a1f.
      get _composerActive() {
        // Panel mode (Design Studio chat): the composer is "active" only while
        // the chat overlay is actually open. Collapsed, the design page wants
        // the passive caption gutter instead — so the composer-active body
        // signals (caption suppression + the pending tile) must drop. The
        // panel viewer stays mounted while hidden (design.html toggles
        // `invisible h-0`, not unmount), so without this gate _composerActive
        // would stay true and keep `sv-viewer-composer-active` set, hiding the
        // caption in both states. Other modes are unaffected (_panelChatOpen
        // defaults true). design.js drives the flag off its chatOpen.
        if (this._mode === 'panel' && !this._panelChatOpen) return false;
        return !this.showTerminal && this.isLive && !!this._tmuxSession &&
               (this.sessionType !== 'host' || this._linked);
      },
      // Cross-session dictation: we're viewing THIS session, but voice is bound to
      // a DIFFERENT one — so anything dictated goes elsewhere. Drives the violet
      // send fill + the "→ ‹target›" tile so it can't be mistaken for local input.
      get _crossSessionDictation() {
        var voice = this.getVoiceStore();
        var delivery = voice && (voice.deliverySessionId || voice.boundSessionId);
        return !!(this._composerActive && voice && voice.enabled && delivery &&
                  delivery !== this._tmuxSession);
      },
      get _crossSessionText() {
        var voice = this.getVoiceStore();
        return (voice && typeof voice.bufferText === 'string') ? voice.bufferText : '';
      },
      get _localDictationText() {
        var voice = this.getVoiceStore();
        var delivery = voice && (voice.deliverySessionId || voice.boundSessionId);
        if (!this._composerActive || !voice || !voice.enabled || !delivery) return '';
        if (delivery !== this._tmuxSession) return '';
        var text = typeof voice.bufferText === 'string' ? voice.bufferText : '';
        return text.trim() ? text : '';
      },
      get _showLocalDictationMirror() {
        var o = this.outbox;
        // Surfaces with no composer (the Session Board's cards) have nowhere
        // else to show the live buffer: the outbox is only staged at SEND
        // time, so while the operator is still speaking the target had
        // nothing to render while every OTHER session rendered the violet
        // "dictating elsewhere" tile off the same buffer. Opt in with
        // sessionViewerPage({dictationTile: true}) and the target mirrors its
        // own buffer. The page viewer is unchanged: its composer shows it.
        if (this._dictationTile && this._localDictationText) return true;
        return !!(this._localDictationText && o && o.state !== 'capturing');
      },
      get _crossSessionTargetTitle() {
        var voice = this.getVoiceStore();
        var bound = voice && (voice.deliverySessionId || voice.boundSessionId);
        if (!bound) return '';
        var sessions = Alpine.store('sessions') || {};
        var s = sessions[bound];
        return (s && s.label) ? s.label : bound;
      },
      // Pending/optimistic message for this session (auto-xkdoi). null when idle.
      get outbox() {
        var s = Alpine.store('sessions')[this.sessionKey];
        var o = (s && s.outbox) || null;
        if (!o) return null;
        if (typeof o.text !== 'string' || !o.text.trim()) return null;
        return o;
      },
      // Skin class for the pending tile, by state.
      outboxTileClass() {
        var o = this.outbox;
        if (!o) return '';
        if (o.state === 'capturing') return 'is-capturing';
        if (o.state === 'unconfirmed') return 'is-unconfirmed';
        return 'is-sending';
      },
      // Re-attempt a message that never reached the log.
      resendOutbox() {
        if (window.resendOutbox) {
          window.resendOutbox(this.sessionKey, { tmuxSession: this._tmuxSession });
        }
      },
      // Discard an unconfirmed message — drop the optimistic tile without
      // resending. The text never reached the log; the operator chose to let it go.
      dismissOutbox() {
        if (window.dismissOutbox) window.dismissOutbox(this.sessionKey);
      },
      // Watch keys: send-key fires when an outbox enters 'sending' (the voice
      // path flips state without going through sendMessage); tail-key fires as
      // entries arrive so we can reconcile the optimistic tile against the log.
      get _outboxSendKey() {
        var o = this.outbox;
        return (o && o.state === 'sending') ? o.localId : null;
      },
      get _entryTailKey() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s && s.entries ? s.entries.length : 0;
      },
      // Delegate a sending outbox to the shared durable outbox engine. The
      // engine owns de-duping, persistence, POST, timeout, and reconciliation.
      _onOutboxSendKey() {
        if (window.sendCurrentOutbox) {
          return window.sendCurrentOutbox(this.sessionKey, { tmuxSession: this._tmuxSession });
        }
        return false;
      },
      // Publish 'sv-outbox-tile-present' on body EXACTLY while the tile is
      // rendered (composer active AND a pending message exists). The voice
      // side gates caption-suppression on this so the caption only hands off
      // once the tile is truly in the DOM (contract cbb8497c-a1f). Guarded so
      // it only clears its own sid.
      // Send the live voice buffer to this session. Used by the dictation
      // tile's Send on composer-less surfaces; the capsule's own Send runs the
      // same store method.
      async _sendDictation() {
        var voice = this.getVoiceStore();
        if (!voice || typeof voice.sendBuffer !== 'function') return false;
        // Delivery and bound can disagree (retargetDelivery moves one without
        // the other). The tile renders off either, but sendBuffer reads ONLY
        // deliverySessionId and returns false with an error that surfaces in
        // the capsule sheet — invisible on a board. Point delivery at the card
        // whose Send was pressed, then send.
        var me = this._tmuxSession || '';
        if (me && voice.deliverySessionId !== me && typeof voice.retargetDelivery === 'function') {
          voice.retargetDelivery(me);
        }
        this.sendTileError = '';
        var ok = false;
        try { ok = await voice.sendBuffer(); } catch (e) { ok = false; }
        if (!ok) this.sendTileError = voice.sheetError || 'Send failed.';
        return ok;
      },
      sendTileError: '',

      _syncTilePresent() {
        if (!this._pageSignals) return;
        if (typeof document === 'undefined' || !document.body) return;
        var sid = this._tmuxSession || '';
        if (this._composerActive && this.outbox) {
          document.body.classList.add('sv-outbox-tile-present');
          document.body.dataset.svComposerSession = sid;
        } else if (document.body.dataset.svComposerSession === sid) {
          document.body.classList.remove('sv-outbox-tile-present');
        }
      },

      // Compatibility wrapper for older tests/callers. The durable outbox
      // engine lives in session-store.js so sends do not depend on a mounted
      // session-viewer component.
      async _durableSend(body, localId) {
        var sid = this.sessionKey;
        var s = window.getSessionStore(sid);
        if (s && (!s.outbox || s.outbox.localId !== localId)) {
          s.outbox = {
            localId: localId,
            state: 'sending',
            source: 'manual',
            text: body,
            ts: Date.now(),
          };
        }
        return window.sendCurrentOutbox
          ? window.sendCurrentOutbox(sid, { tmuxSession: this._tmuxSession })
          : false;
      },

      _armOutboxTimeout(localId) {
        if (window.armOutboxTimeout) window.armOutboxTimeout(this.sessionKey, localId);
      },

      _markOutboxUnconfirmed(localId) {
        if (window.markOutboxUnconfirmed) window.markOutboxUnconfirmed(this.sessionKey, localId);
      },

      // Clear the pending tile once the real log entry shows up. JSONL doesn't
      // carry our localId, so match the newest user entries by text within a
      // recency window.
      _tryReconcileOutbox() {
        if (window.tryReconcileOutbox) return window.tryReconcileOutbox(this.sessionKey);
        return false;
      },

      // Restore a mid-flight outbox after a reload/eviction and re-arm.
      _restoreOutbox() {
        if (window.restoreOutbox) return window.restoreOutbox(this.sessionKey);
        return false;
      },
      get contextTokens() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.contextTokens : 0;
      },
      get agentTestProgress() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.agentTestProgress : null;
      },
      get topics() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return (s && s.topics) || [];
      },
      get todos() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return (s && Array.isArray(s.todos)) ? s.todos : [];
      },
      get hasTodos() {
        return this.todos.length > 0;
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
      get workspaceName() {
        if (this.projectLabel) return this.projectLabel;
        if (this.project) return _formatProject(this.project);
        return '';
      },
      get olderCursor() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return s ? s.olderCursor : null;
      },
      get hasMoreHistory() {
        var s = Alpine.store('sessions')[this.sessionKey];
        return !!(s && s.hasMoreHistory);
      },
      get viewportMode() {
        return this.viewportWidth < 768 ? 'mobile' : 'desktop';
      },
      get showInlineComposer() {
        return this.resolveComposerMode() === 'inline';
      },
      get canSendComposer() {
        return this.attachments.length > 0 || this.hasContent;
      },

      // Workspace-changes indicator: derived from the cached
      // ``_workspaceStatus`` (populated by ``_refreshWorkspaceStatus``).
      // Both the overlay title-bar anchor and the page-mode header
      // anchor bind to this getter via ``x-show`` so the indicator
      // stays in lock-step with the underlying state.
      get hasWorkspaceChanges() {
        return !!(this._workspaceStatus && this._workspaceStatus.hasChanges);
      },
      // Dual-state for the ⌥ button: dim when only dirty diffs, lit
      // amber when at least one commit is ready to merge. The button
      // itself shows on either condition (``hasWorkspaceChanges``); this
      // flag drives the colour modifier.
      get hasCommitsAhead() {
        return !!(this._workspaceStatus && this._workspaceStatus.commitsAhead > 0);
      },
      get workspaceStatusTooltip() {
        var ws = this._workspaceStatus;
        if (!ws || !ws.hasChanges) return '';
        var bits = [];
        if (ws.commitsAhead > 0) {
          bits.push(ws.commitsAhead + ' commit' + (ws.commitsAhead === 1 ? '' : 's') + ' to review');
        }
        if (ws.dirtyCount > 0) {
          bits.push(ws.dirtyCount + ' dirty file' + (ws.dirtyCount === 1 ? '' : 's'));
        }
        return 'Workspace: ' + bits.join(', ') + ' — open Worktrees review';
      },

      async openWorkspaceReview() {
        if (!this.sessionKey) return;
        this.workspaceReviewLoading = true;
        try {
          if (window.openWorktreeReviewOverlay) {
            var opened = await window.openWorktreeReviewOverlay(this.sessionKey);
            if (opened) return;
          }
        } catch (_ignored) {
          // Fall through to the route-based deep link.
        } finally {
          this.workspaceReviewLoading = false;
        }
        if (typeof navigateTo === 'function') {
          navigateTo('/worktrees?session=' + encodeURIComponent(this.sessionKey));
        }
      },

      get sessionContributionActions() {
        var service = window.Autonomy && window.Autonomy.sessionContributions;
        return service ? service.forSession(this.sessionKey, 'action') : [];
      },

      get sessionContributionBadges() {
        var service = window.Autonomy && window.Autonomy.sessionContributions;
        return service ? service.forSession(this.sessionKey, 'badge') : [];
      },

      openSessionContribution(item) {
        var service = window.Autonomy && window.Autonomy.sessionContributions;
        if (service) service.open(item);
      },

      // ── View-only state ─────────────────────────────────────────
      displayEntries: [],
      // Monotonic display-revision bookkeeping (auto-16g9t): the merge
      // revision + entry count displayEntries currently reflects.
      _displayedRev: 0,
      _displayedLen: 0,
      autoScroll: true,
      showJumpToBottom: false,
      loadingOlder: false,
      _workspaceStatus: null,
      // True from the moment the ⌥ button is tapped until the worktree
      // review overlay finishes hydrating (or the fetch fails) — drives
      // the spin affordance on the button so a slow fetch isn't silent.
      workspaceReviewLoading: false,
      _storeCleanups: [],
      _expanded: {},
      _expandView: {},
      _groupExpanded: {},
      _groupExpandView: {},
      // ── Turn-correction overlay (auto-edec1.4, delivery auto-hmow2) ──
      // The correction data lives in ONE place — the shared session store's
      // ``_turnCorrections`` map (session-store.js). The renderer joins user
      // tiles to it; the SSE handler, GET hydration, and accept/dismiss all
      // write only that map. The viewer keeps no duplicate correction map.
      // ``_correctionDisplayMode`` is pure view state (accepted-preview raw
      // toggle), not correction data. ``_correctionHydrateToken`` guards
      // against out-of-order GET hydration responses.
      _correctionDisplayMode: {},
      _correctionHydrateToken: 0,

      // Backfill progress (page mode only)
      loadProgress: 0,
      loadedMB: '0',
      totalMB: '0',

      // Input (Tier 3) — contenteditable, no v-model
      viewportWidth: (window.visualViewport && window.visualViewport.width) || window.innerWidth || 0,
      hasContent: false,
      _draftTimer: null,
      sending: false,
      uploading: false,
      attachments: [],
      _nextAttachId: 0,
      _viewportResizeHandler: null,

      _uploadProxy: null,
      _uploadUnsub: null,
      _uploadSeen: null,

      // Header expand/collapse
      headerOpen: false,
      // Drawer tab: 'topics' | 'todos'. Only meaningful when hasTodos is true.
      // Auto-resets to 'topics' whenever hasTodos transitions true → false
      // (handled by the $watch in init(), so there's no stuck-tab state).
      selectedDrawerTab: 'topics',
      copiedField: '',
      _copyFeedbackTimer: null,
      backToSessionsList() {
        var app = window.Autonomy || {};
        if (typeof app.closeSessionOverlayToList === 'function') {
          app.closeSessionOverlayToList();
        }
      },

      // Terminal toggle (full-screen xterm.js swaps the chat body)
      showTerminal: false,
      _termInstance: null,   // result of window.mountTerminal(), or null

      // Identity-refresh nudge: drawer button state machine. 'idle' →
      // 'sending' (POST in flight) → 'sent' (CrossTalk delivered, waiting
      // for the agent to react). When the agent writes set-label or
      // set-topics, the resulting tmux_sessions row update flips _label
      // / topics, the x-if condition on the button collapses, and the
      // button disappears entirely. Reset to 'idle' if the request fails.
      identityRefreshState: 'idle',
      _identityRefreshClearTimer: null,
      async requestIdentityRefresh() {
        if (this.identityRefreshState !== 'idle' || !this._tmuxSession) return;
        this.identityRefreshState = 'sending';
        try {
          const resp = await fetch(
            '/api/session/' + encodeURIComponent(this._tmuxSession) + '/request-identity-refresh',
            { method: 'POST' },
          );
          if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || ('status ' + resp.status));
          }
          this.identityRefreshState = 'sent';
          // If the agent never reacts, recover the button after 60s so
          // the operator can resend rather than being stuck in 'sent'.
          if (this._identityRefreshClearTimer) clearTimeout(this._identityRefreshClearTimer);
          this._identityRefreshClearTimer = window.setTimeout(() => {
            if (this.identityRefreshState === 'sent') this.identityRefreshState = 'idle';
          }, 60000);
        } catch (err) {
          this.identityRefreshState = 'idle';
          if (typeof window.showToast === 'function') {
            window.showToast('Identity refresh failed: ' + (err.message || err), 'error');
          }
        }
      },

      // Viewer-attachment lightbox: when src is set, the overlay shows the
      // full-resolution image; any tap or ESC closes. Pinch-zoom is
      // a multi-touch gesture that doesn't fire click, so it stays.
      lightboxSrc: '',
      lightboxAlt: '',
      // 'image' (default — tap-to-close img) | 'iframe' (PDF/text/viewable) |
      // 'shortcut' (signed Apple Shortcut installer) | 'download' (binary).
      // File attachments open HERE as an in-page overlay
      // instead of navigating via <a target=_blank>, which on an iOS PWA kicks
      // out to Safari, backgrounds the app, and drops the voice connection.
      lightboxKind: 'image',
      lightboxName: '',
      lightboxFile: null,
      lightboxFileState: 'idle',
      lightboxFileError: '',
      _lightboxFileRequest: 0,
      // Rendered markdown HTML when lightboxKind === 'markdown'. Browsers have
      // no native text/markdown renderer, so we fetch the raw bytes and render
      // client-side with marked + DOMPurify rather than dropping the file into
      // an iframe (which white-screens).
      lightboxHtml: '',
      _lightboxPrevViewport: null,
      lightboxKindForMime(mime, name) {
        if (typeof name === 'string' && /\.shortcut$/i.test(name)) return 'shortcut';
        if (typeof mime !== 'string' || !mime) return 'download';
        if (mime.indexOf('image/') === 0) return 'image';
        if (mime === 'text/markdown' || mime === 'text/x-markdown') return 'markdown';
        if (mime === 'application/pdf' || mime.indexOf('text/') === 0) return 'iframe';
        return 'download';
      },
      _prepareShortcutFile(src, name) {
        var request = ++this._lightboxFileRequest;
        this.lightboxFile = null;
        this.lightboxFileState = 'loading';
        this.lightboxFileError = '';
        fetch(src, { credentials: 'same-origin' })
          .then((r) => (r.ok ? r.blob() : Promise.reject(new Error('status ' + r.status))))
          .then((blob) => {
            if (request !== this._lightboxFileRequest || this.lightboxSrc !== src) return;
            if (typeof File !== 'function') throw new Error('file sharing is unavailable');
            this.lightboxFile = new File(
              [blob],
              name || 'Autonomy Capture.shortcut',
              { type: blob.type || 'application/octet-stream' },
            );
            this.lightboxFileState = 'ready';
          })
          .catch((err) => {
            if (request !== this._lightboxFileRequest || this.lightboxSrc !== src) return;
            this.lightboxFileState = 'error';
            this.lightboxFileError = 'The signed file could not be prepared for sharing.';
            console.warn('Shortcut attachment preparation failed', err);
          });
      },
      canShareLightboxFile() {
        if (!this.lightboxFile || typeof navigator === 'undefined' ||
            typeof navigator.share !== 'function') return false;
        if (typeof navigator.canShare !== 'function') return true;
        try {
          return navigator.canShare({ files: [this.lightboxFile] });
        } catch (_err) {
          return false;
        }
      },
      openLightboxInNewContext() {
        if (!this.lightboxSrc) return;
        // A real anchor click preserves the initiating user gesture on iOS.
        // target=_blank keeps Quick Look out of the standalone PWA's only
        // history entry; deliberately omit download, which is what replaces
        // the PWA with the system download/preview surface.
        var anchor = document.createElement('a');
        anchor.href = this.lightboxSrc;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      },
      openShortcutInstaller() {
        if (this.lightboxFileState === 'loading') return;
        if (!this.canShareLightboxFile()) {
          this.openLightboxInNewContext();
          return;
        }

        // navigator.share() must begin inside this click's transient user
        // activation. The file was prefetched when the sheet opened, so there
        // is no await before invoking the native share controller.
        var result;
        try {
          result = navigator.share({
            files: [this.lightboxFile],
            title: (this.lightboxName || 'Autonomy Capture.shortcut').replace(/\.shortcut$/i, ''),
          });
        } catch (err) {
          this.lightboxFileError = 'Could not open the iOS share sheet.';
          return;
        }
        Promise.resolve(result)
          .then(() => this.closeLightbox())
          .catch((err) => {
            // Cancelling the system sheet is normal; leave our install sheet
            // open so the operator can try again.
            if (err && err.name === 'AbortError') return;
            this.lightboxFileError = 'Could not open the iOS share sheet.';
          });
      },
      openLightbox(src, alt, opts) {
        if (!src) return;
        opts = opts || {};
        this.lightboxSrc = src;
        this.lightboxAlt = alt || '';
        this.lightboxKind = opts.kind || 'image';
        this.lightboxName = opts.name || '';
        this.lightboxFile = null;
        this.lightboxFileState = 'idle';
        this.lightboxFileError = '';
        if (this.lightboxKind === 'shortcut') {
          this._prepareShortcutFile(src, this.lightboxName);
        }
        if (this.lightboxKind === 'markdown') {
          this.lightboxHtml = '';
          fetch(src, { credentials: 'same-origin' })
            .then((r) => (r.ok ? r.text() : Promise.reject(r.status)))
            .then((md) => {
              if (window.marked && window.DOMPurify) {
                this.lightboxHtml = window.DOMPurify.sanitize(window.marked.parse(md));
              } else {
                // Graceful fallback if marked/DOMPurify aren't loaded yet
                this.lightboxHtml =
                  '<pre style="white-space:pre-wrap">' +
                  md.replace(/[&<>]/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[s])) +
                  '</pre>';
              }
            })
            .catch((err) => {
              this.lightboxHtml = '<div style="color:#f87171">Failed to load: ' + err + '</div>';
            });
        }
        // The base layout pins the viewport to maximum-scale=1,
        // user-scalable=no so the chat UI doesn't accidentally zoom on
        // mobile. We want pinch-zoom inside the lightbox though, so swap
        // the meta tag content while it's open and restore on close. iOS
        // re-evaluates the zoom limits on mutation.
        var meta = document.querySelector('meta[name="viewport"]');
        if (meta && this._lightboxPrevViewport === null) {
          this._lightboxPrevViewport = meta.getAttribute('content');
          meta.setAttribute(
            'content',
            'width=device-width, initial-scale=1, maximum-scale=5, user-scalable=yes, viewport-fit=cover',
          );
        }
      },
      closeLightbox() {
        this._lightboxFileRequest += 1;
        this.lightboxSrc = '';
        this.lightboxAlt = '';
        this.lightboxKind = 'image';
        this.lightboxName = '';
        this.lightboxHtml = '';
        this.lightboxFile = null;
        this.lightboxFileState = 'idle';
        this.lightboxFileError = '';
        var meta = document.querySelector('meta[name="viewport"]');
        if (meta && this._lightboxPrevViewport !== null) {
          meta.setAttribute('content', this._lightboxPrevViewport);
          this._lightboxPrevViewport = null;
        }
      },

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
      _resumeRecoveryInstalled: false,
      _resumeHeartbeatAt: 0,
      _resumeRefreshInFlight: null,
      _resumeHeartbeatInterval: null,

      // ── Resume-from-viewer (terminated → starting → ready in place) ──
      // Distinct from the _resume* connection-recovery fields above: this
      // is the operator action that relaunches a dead session via
      // /api/session/resume, the same call the session-list Resume button
      // makes. _resumeMeta is fetched from /api/session/{name} on load and
      // carries the identity + resumability needed to fire it.
      _resumeMeta: null,        // {resumable, sourceId, sessionUuid, filePath}
      resumeStarting: false,    // boot transition in flight (drives row-1 chip)
      resumeStartErr: '',       // transient error surfaced under the button
      _resumeStartTimer: null,  // polls lifecycle derivation until ready

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

      resolveComposerMode() {
        if (this.viewportMode === 'desktop') return 'inline';
        return 'inline';
      },

      refreshViewportWidth() {
        this.viewportWidth = (window.visualViewport && window.visualViewport.width) || window.innerWidth || 0;
        this._scheduleJumpToBottomUpdate();
      },

      getVoiceStore() {
        try {
          if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') return null;
          return Alpine.store('voice') || null;
        } catch (_err) {
          return null;
        }
      },

      getComposerStore() {
        if (!this.sessionKey || !window.getSessionStore) return null;
        return window.getSessionStore(this.sessionKey);
      },

      readComposerTextFromElement(el) {
        return el ? el.innerText : '';
      },

      readComposerText() {
        var el = this.$refs.messageInput;
        if (el) return this.readComposerTextFromElement(el);
        var s = this.getComposerStore();
        return s ? (s.draftText || '') : '';
      },

      persistComposerDraft(text) {
        var normalized = text || '';
        this.hasContent = normalized.trim().length > 0;
        var s = this.getComposerStore();
        if (s) s.draftText = normalized;
        // Mirror to localStorage so the draft survives full reload and iOS
        // backgrounding/eviction, not just soft SPA navigation (auto-xkdoi).
        // saveDraft removes the key on empty text, so clearComposer() also
        // clears the persisted draft for free.
        if (window.saveDraft) window.saveDraft(this.sessionKey, normalized);
      },

      writeComposerText(text) {
        var normalized = text || '';
        var el = this.$refs.messageInput;
        if (el && el.innerText !== normalized) el.innerText = normalized;
        this.persistComposerDraft(normalized);
      },

      clearComposer() {
        this.writeComposerText('');
      },

      restoreComposerDraft() {
        var s = this.getComposerStore();
        // Prefer the in-memory draft (survives soft SPA nav); fall back to the
        // localStorage mirror, which survives full reload and iOS eviction
        // where the in-memory store is gone (auto-xkdoi).
        var draft = (s && s.draftText) ? s.draftText
                  : (window.loadDraft ? window.loadDraft(this.sessionKey) : '');
        this.writeComposerText(draft || '');
      },

      _selectComposerContents(el, collapseToEnd) {
        if (!el || typeof document === 'undefined' || typeof document.createRange !== 'function') return false;
        if (typeof window.getSelection !== 'function') return false;
        var sel = window.getSelection();
        if (!sel) return false;
        var range = document.createRange();
        range.selectNodeContents(el);
        if (collapseToEnd) range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
        return true;
      },

      focusComposerAtEnd() {
        var el = this.$refs.messageInput;
        if (!el) return false;
        if (typeof el.focus === 'function') el.focus();
        this._selectComposerContents(el, true);
        return true;
      },

      writeComposerTextWithUndo(text) {
        var normalized = text || '';
        var el = this.$refs.messageInput;
        if (!el) {
          this.writeComposerText(normalized);
          return true;
        }
        if (typeof el.focus === 'function') el.focus();
        var usedExec = false;
        try {
          if (typeof document !== 'undefined' &&
              typeof document.execCommand === 'function' &&
              this._selectComposerContents(el, false)) {
            usedExec = document.execCommand('insertText', false, normalized) === true;
          }
        } catch (_err) {
          usedExec = false;
        }
        if (!usedExec) {
          this.writeComposerText(normalized);
        } else {
          this.persistComposerDraft(this.readComposerTextFromElement(el));
        }
        this.focusComposerAtEnd();
        return true;
      },

      canSendComposerText(text) {
        return this.attachments.length > 0 || !!((text || '').trim());
      },

      get showDesktopVoiceImport() {
        // Superseded. Desktop now uses the same voice-first capsule + dictation
        // tile as mobile (the inline composer is hidden when voice is bound), so
        // the desktop "preview strip + import-to-box" flow no longer renders.
        // Kept as a getter returning false so any remaining references no-op.
        return false;
      },

      get desktopVoicePreview() {
        var voice = this.getVoiceStore();
        var shell = window.Autonomy && window.Autonomy.voice && window.Autonomy.voice.shell;
        if (!shell || typeof shell.previewWords !== 'function') return '';
        return shell.previewWords((voice && voice.bufferText) || '', 12);
      },

      importVoiceBufferToComposer() {
        if (!this.showDesktopVoiceImport) return false;
        var voice = this.getVoiceStore();
        if (!voice) return false;
        var snapshot = typeof voice.bufferText === 'string' ? voice.bufferText : '';
        if (!snapshot.trim()) return false;
        var composerStore = this.getComposerStore();
        var draftText = composerStore ? (composerStore.draftText || '') : this.readComposerText();
        var currentDraft = this.readComposerText();
        var nextDraft = draftText.trim() === '' ? snapshot : ((currentDraft || draftText) + '\n\n' + snapshot);
        // Do NOT auto-mute on import. Muting here forced the operator to click
        // Unmute before dictating again — the third click in the desktop
        // dictate->import->send->unmute slog. Leaving the mic live lets the next
        // utterance keep flowing; Send folds in whatever's accumulated.
        this.writeComposerTextWithUndo(nextDraft);
        if (typeof voice.clearBuffer === 'function') {
          voice.clearBuffer();
        } else if (typeof voice.setBufferText === 'function') {
          voice.setBufferText('');
        }
        return true;
      },

      buildComposerBody(text) {
        var trimmed = (text || '').trim();
        var lines = [];
        for (var ai = 0; ai < this.attachments.length; ai++) {
          var att = this.attachments[ai];
          if (att.path) lines.push(att.path);
        }
        if (trimmed) {
          if (lines.length) lines.push('');
          lines.push(trimmed);
        }
        return lines.join('\n');
      },

      // ── Resume from the viewer ──────────────────────────────────
      // Relaunch a terminated session via the SAME /api/session/resume
      // call the session-list Resume button makes (sessions.js). The
      // difference is the operator is already watching this session, so
      // we keep the conversation on screen and drive the
      // terminated→starting→ready transition in place: prime the store so
      // host-0531's lifecycle phase chip lights up in row 1, then poll the
      // (resolved-neutralised) derivation until the harness reports ready.
      async resumeFromViewer() {
        if (this.resumeStarting) return;
        var m = this._resumeMeta;
        if (!m || !m.resumable) return;
        this.resumeStarting = true;
        this.resumeStartErr = '';

        var body = m.sourceId
          ? { source_id: m.sourceId }
          : { session_uuid: m.sessionUuid, file_path: m.filePath };
        try {
          var res = await fetch('/api/session/resume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!res.ok) {
            var err = await res.json().catch(function () { return {}; });
            throw new Error(err.error || 'Resume failed');
          }
          var data = await res.json();

          // A dead session found in dashboard.db is relaunched under its
          // ORIGINAL tmux_name, so newKey usually equals sessionKey and the
          // existing store entry simply starts receiving live registry +
          // SSE updates. A generated name (dead row absent) re-points the
          // viewer at the fresh session.
          var newKey = data.tmux_name || this.sessionKey;
          var store = window.getSessionStore(newKey);
          // Deliberately DO NOT set store._resuming / store._launching here.
          // Those are a crutch for the session-LIST create flow, which shows
          // an optimistic ``pending-`` placeholder before any real session
          // exists; the list's tile-expiry reconciler only ever clears flags
          // off ``pending-`` keys (sessions.js). A resumed session is a REAL
          // registry row under its own tmux_name, so pinning those sticky
          // flags here desynced the list from the real lifecycle: the card
          // stuck in LAUNCHING after boot (_launching || startupVisible never
          // went false), became non-tappable (sessions.html gates tap on
          // !_launching), and on Close showed "Launching + Ended + Resume"
          // at once (is_live→false derived dead_resumable while _launching/
          // _resuming kept it pinned in the Launching section). startupVisible
          // + is_live already drive section membership correctly for a real
          // session, so we let the registry own it. (Diagnosed by
          // host-0531-020038 from Jeremy's live test.)
          store.isLive = true;          // optimistic; registry confirms + drives phases
          store.resolved = false;       // show boot phases, not the historical "ready"
          if (!store.setupPhase || store.setupPhase === 'pending') {
            store.setupPhase = 'container_starting';
          }
          store.harnessPhase = 'pending';
          store.label = data.label || store.label || this._label;

          if (newKey !== this.sessionKey) {
            this.sessionKey = newKey;
            this.sessionId = newKey;
            if (this._mode === 'page') {
              history.replaceState({}, '',
                '/session/' + encodeURIComponent(this.project) + '/' + encodeURIComponent(newKey));
            }
            this._tailUrl = '/api/session/' + encodeURIComponent(this.project)
              + '/' + encodeURIComponent(newKey) + '/tail';
          }

          // Button is now gated off (isLive true); resumeStarting drives the
          // row-1 boot chip until the harness reports ready.
          this._resumeMeta = { resumable: false, sourceId: m.sourceId,
            sessionUuid: m.sessionUuid, filePath: m.filePath };

          window.ensureSessionMessages();
          this._setupWatchers();
          this._armResumeReadyWatch();
        } catch (e) {
          this.resumeStarting = false;
          this.resumeStartErr = (e && e.message) || 'Resume failed';
          var self = this;
          setTimeout(function () { self.resumeStartErr = ''; }, 4000);
        }
      },

      // Clear resumeStarting once the relaunched harness is genuinely up.
      // Drives off the resolved-neutralised _resumePhaseRow so the
      // historical JSONL can't fake an early "ready"; a 120s safety net
      // guarantees the boot chip never sticks if phase markers never land
      // (e.g. a host session that doesn't emit setup_phase).
      _armResumeReadyWatch() {
        var self = this;
        if (this._resumeStartTimer) clearInterval(this._resumeStartTimer);
        var started = Date.now();
        this._resumeStartTimer = setInterval(function () {
          var L = window.Autonomy && window.Autonomy.lifecycle;
          var row = self._resumePhaseRow;
          var done = false;
          if (L && row && self.isLive && L.lifecycleState(row) === 'ready') done = true;
          if (Date.now() - started > 120000) done = true;
          if (done) {
            clearInterval(self._resumeStartTimer);
            self._resumeStartTimer = null;
            self.resumeStarting = false;
          }
        }, 500);
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
          this._lcSetState('loading', 'configure: runDir path begin');
          try {
            var res = await fetch('/api/dispatch/tail/' + encodeURIComponent(runDir) + '?after=0');
            if (!res.ok) {
              try {
                var detailRes = await fetch('/api/session/' + encodeURIComponent(runDir));
                if (detailRes.ok) {
                  var detail = await detailRes.json();
                  await this.configure({
                    sessionId: detail.session_id || runDir,
                    project: detail.project || project,
                    tmuxSession: detail.session_id || tmuxSession || runDir,
                    _isLive: isLiveHint !== undefined ? !!isLiveHint : !!detail.is_live,
                  });
                  return;
                }
              } catch (fallbackErr) {}
              this._lcSetState('error', 'configure: dispatch tail fetch failed');
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
            this.projectLabel = _formatProject(project);
            this._ensureWorkspaceName(project);
            if (this._mode === 'page') window._diagFocusedViewerId = sessionId;

            var store = window.getSessionStore(sessionId);

            // Hydrate uploads before the tail ingest so they merge inline.
            await this._initUploads();

            if (data.entries && data.entries.length > 0) {
              window.mergeSessionEntries(store, data, 'fetch');
            }
            if (data.cursor) store.committed = data.cursor;
            store.isLive = isLiveHint !== undefined ? !!isLiveHint : !!data.is_live;
            if (data.resolved !== undefined) store.resolved = !!data.resolved;
            store.sessionType = data.type || '';
            store.role = data.role || '';
            store.activityState = data.activity_state || 'idle';
            if (data.offset !== undefined) store.offset = data.offset;
            if (data.seq !== undefined) store.seq = data.seq;
            store.loaded = true;

            this._rebuildDisplay();
            this._lcSetState('ready', 'configure: dispatch tail loaded');
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
            // Worktree status is rendered in both modes — page-mode header
            // and overlay title-bar both x-show on ``hasWorkspaceChanges``.
            this._refreshWorkspaceStatus();
            return;
          } catch (e) {
            try {
              var sessionDetailRes = await fetch('/api/session/' + encodeURIComponent(runDir));
              if (sessionDetailRes.ok) {
                var sessionDetail = await sessionDetailRes.json();
                await this.configure({
                  sessionId: sessionDetail.session_id || runDir,
                  project: sessionDetail.project || project,
                  tmuxSession: sessionDetail.session_id || tmuxSession || runDir,
                  _isLive: isLiveHint !== undefined ? !!isLiveHint : !!sessionDetail.is_live,
                });
                return;
              }
            } catch (fallbackErr2) {}
            this._lcSetState('error', 'configure: dispatch tail catch');
            this.errorMsg = 'Failed to load: ' + (e.message || e);
            return;
          }
        }

        // ── SessionId path: standard session connection ──
        if (!sessionId) return;

        this.sessionKey = sessionId;
        this.sessionId = sessionId;
        this.project = project;
        this.projectLabel = _formatProject(project);
        this._ensureWorkspaceName(project);
        // Track the session id the page is currently rendering so /api/diag
        // collectors can flag is_focused_viewer correctly. Page mode owns
        // this; panel/overlay sessions read but do not claim focus.
        if (this._mode === 'page') window._diagFocusedViewerId = sessionId;
        this._tailUrl = '/api/session/' + encodeURIComponent(project) + '/' + encodeURIComponent(sessionId) + '/tail';

        var store = window.getSessionStore(sessionId);

        // Backfill org + resume metadata for dead sessions not seeded by
        // /api/dao/active_sessions. graph_source_id/session_uuid/file_path
        // + resumable drive the title-bar Resume affordance; org keeps the
        // legacy dead-session behaviour. Runs once per mount (guarded on
        // _resumeMeta being unfetched) so cache-hit reopens don't refetch.
        if (!store.org || this._resumeMeta === null) {
          var self0 = this;
          fetch('/api/session/' + encodeURIComponent(sessionId))
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
              if (!data) return;
              if (data.org && !store.org) store.org = data.org;
              if (data.resumable !== undefined) store.resumable = !!data.resumable;
              self0._resumeMeta = {
                resumable: !!data.resumable,
                sourceId: data.graph_source_id || '',
                sessionUuid: data.session_uuid || '',
                filePath: data.file_path || '',
              };
            })
            .catch(function() {});
        }

        if (store.loaded) {
          // Instant render from cache — zero network
          this._rebuildDisplay();
          this._lcSetState('ready', 'configure: cache hit, no fetch');
          this._scrollToBottom();
        } else {
          // First visit — fetch is the authoritative initial render.
          // (With tuple-keyed merge, batch order can no longer invert the
          // buffer — the reset just guarantees a clean cold-open.)
          store.entries = [];
          store.localEntries = [];
          store._byRef = {};
          store.toolMap = {};
          store.resultMap = {};
          store._pendingSSE = [];
          store.chain = [];
          store.committed = null;
          store.olderCursor = null;
          store.hasMoreHistory = false;

          store._loading = true;
          window.ensureSessionMessages();
          try { console.log('[lc] '+JSON.stringify({event:'cfg-before-initUploads',wallt:Date.now()})); } catch(e){}

          // _initUploads only builds the attachments list — it's independent of
          // the message backlog and SSE ordering, but it was awaiting ~4.8s of
          // slow /api/graph/settings schema fetches and blocking the viewer
          // settle for no reason. Run it in the background; attachments render
          // when it resolves. (Verified: this await was ~4.8s of the ~7.5s
          // pre-settle block.)
          this._initUploads().catch(function () {});
          try { console.log('[lc] '+JSON.stringify({event:'cfg-after-initUploads-bg',wallt:Date.now()})); } catch(e){}

          // Fetch a backlog whenever one might EXIST. store.resolved is the
          // fast seeded signal (list/registry). But it defaults false, and a
          // dead session opened COLD — direct URL, source-view crosslink, or
          // the inactive-session list — is never seeded, so resolved stayed
          // false and a session with thousands of turns rendered blank ("0
          // entries / Send a message to begin"). The tail endpoint serves a
          // dead session's history straight from the graph even when no JSONL
          // path is recorded, so the right gate is "resolved OR not live":
          //   • live + starting (isLive=true, resolved=false) → still skipped,
          //     preserving the ~2.6–5s settle optimization (the ONLY case it
          //     was ever meant to cover — a brand-new session has no backlog);
          //   • dead (isLive=false) → fetch, and _fetchBacklog's 404 probe
          //     degrades a genuinely-empty/pruned session to the empty state.
          if (store.resolved || !store.isLive) try {
            await this._fetchBacklog(store);
            try { console.log('[lc] '+JSON.stringify({event:'cfg-after-fetchBacklog',wallt:Date.now()})); } catch(e){}
          } catch (e) {
            if (e && e.missingSession) {
              // Pruned or unknown session: surface as an explicit error.
              store._loading = false;
              this.errorMsg = (e && e.message) || 'Session not found';
              this._lcSetState('error', 'configure: messages fetch failed');
              return;
            }
            // New session with no JSONL yet. auto-ja51w C5: gate the
            // flip-to-ready on _isStillLaunching — if the session is genuinely
            // still booting (live, no resolved JSONL, no entries, harness
            // hasn't reached composer_ready), keep state='loading' so the
            // pre-ready slot's phase chip stays rendered. The Alpine $watch
            // below flips state→'ready' automatically once SSE updates the
            // store to a no-longer-launching condition.
            if (this.sessionKey) {
              store._loading = false;
              store.loaded = true;
              this._rebuildDisplay();
              if (this._isStillLaunching(store)) {
                // Stay in loading — pre-ready slot renders the phase chip.
                this._lcEmitSlot(this.loadingPhaseRow, 'configure: still launching, holding loading state');
              } else {
                this._lcSetState('ready', 'configure: no JSONL yet, but session not launching (composer_ready+entries or resolved)');
              }
            } else {
              if (this.state === 'loading') {
                this.errorMsg = 'Failed to connect to session';
                this._lcSetState('error', 'configure: no sessionKey after no-JSONL branch');
              }
              store._loading = false;
              return;
            }
          }

          // Flush pending SSE events that arrived during fetch — the
          // tuple merge makes replay order irrelevant; spans still
          // advance the committed high-water in arrival order.
          var pending = store._pendingSSE;
          store._pendingSSE = [];
          store._loading = false;
          store.loaded = true;
          for (var i = 0; i < pending.length; i++) {
            if (pending[i].span) window.advanceCommittedSpan(store, pending[i].span);
            window.mergeSessionEntries(store, pending[i], 'sse');
          }

          this._rebuildDisplay();
          if (this.state === 'loading' && !this._isStillLaunching(store)) {
            this._lcSetState('ready', 'configure: messages flushed, exiting loading');
          }
          this._scrollToBottom();
        }

        // Ensure SSE subscription (idempotent)
        window.ensureSessionMessages();

        // Hydrate sparse turn-correction overlay state.
        // Sparse: empty payload → empty map → no overlay rendered.
        this._hydrateCorrections();

        // Set up reactive watchers
        this._setupWatchers();

        // Worktree status — pushed live via the ``worktrees`` SSE
        // topic. registerHandler immediately replays the cached
        // payload (so the indicator paints on first render without a
        // round-trip) and then runs on every change. The /api/worktrees
        // fallback fires only if SSE hasn't delivered anything yet.
        var self = this;
        this._workspaceHandler = function (rows) { self._applyWorkspaceRows(rows); };
        if (typeof window.registerHandler === 'function') {
          window.registerHandler('worktrees', this._workspaceHandler);
        }
        if (!this._workspaceStatus) this._refreshWorkspaceStatus();

        // Restore draft text into contenteditable + attach file-paste handler
        var self = this;
        this.$nextTick(function() {
          var el = self.$refs.messageInput;
          if (!el) return;
          self.restoreComposerDraft();
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
        this.refreshViewportWidth();
        // Open the approval overlay when a request from this session awaits the
        // operator (e.g. a commit signature). Push-driven: the ``approval:pending``
        // SSE event opens it, ``approval:decided`` clears the dedup. The one-shot
        // durable-field read below covers a viewer that mounts after the event
        // fired (openApprovalRequest ignores already-decided rows, so a stale
        // cached replay is harmless). The shared overlay's openApprovalOverlay
        // dispatches on the request's kind.
        this._lastApprovalPendingId = null;
        // sessionKey is empty at init (configure() populates it later), so
        // the durable-field read must also fire when the key lands.
        this._checkPendingApproval();
        this.$watch('sessionKey', (key) => {
          this._lastApprovalPendingId = null;
          if (key) this._checkPendingApproval();
          var service = window.Autonomy && window.Autonomy.sessionContributions;
          if (key && service) service.load([key]);
        });
        var approvalSelf = this;
        this._approvalPendingHandler = function (d) {
          if (!d || d.session !== approvalSelf.sessionKey || !d.id) return;
          if (d.id !== approvalSelf._lastApprovalPendingId && window.openApprovalOverlay) {
            approvalSelf._lastApprovalPendingId = d.id;
            window.openApprovalOverlay(d.id);
          }
        };
        this._approvalDecidedHandler = function (d) {
          if (d && d.session === approvalSelf.sessionKey &&
              d.id === approvalSelf._lastApprovalPendingId) {
            approvalSelf._lastApprovalPendingId = null;
          }
        };
        if (typeof window.registerHandler === 'function') {
          window.registerHandler('approval:pending', this._approvalPendingHandler);
          window.registerHandler('approval:decided', this._approvalDecidedHandler);
        }
        if (!this._viewportResizeHandler) {
          var self = this;
          this._viewportResizeHandler = function() {
            self.refreshViewportWidth();
          };
          if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', this._viewportResizeHandler);
          } else {
            window.addEventListener('resize', this._viewportResizeHandler);
          }
        }

        // Auto-reset the drawer tab to 'topics' whenever the session's todo
        // list empties out. Without this, clearing the final todo would leave
        // the viewer stuck on an empty Todos panel after the tab strip hides.
        this.$watch('hasTodos', (now) => {
          if (!now) this.selectedDrawerTab = 'topics';
        });

        // auto-ja51w C5: promote state→'ready' when launching ends.
        // configure() may have left the viewer in state='loading' because
        // the session was still launching (no entries, harness not yet
        // composer_ready). When SSE later delivers an entry OR resolves
        // the registry to a no-longer-launching condition, the viewer
        // must flip state to 'ready' so the entries view renders.
        // The watched expression evaluates _isStillLaunching against the
        // current session store row on every reactivity tick.
        this.$watch(
          () => {
            var store = Alpine.store('sessions')[this.sessionKey];
            return store ? this._isStillLaunching(store) : false;
          },
          (stillLaunching) => {
            if (!stillLaunching && this.state === 'loading') {
              this._lcSetState('ready', 'watch: launching ended (entries arrived or session resolved)');
              // Repaint so the entries view replaces the loading slot.
              this._rebuildDisplay();
              this._scrollToBottom();
            }
          }
        );

        // Publish the authoritative "composer surface active" signal to
        // document.body so the voice side branches caption-suppression and its
        // send path on the IDENTICAL condition where the outbox tile mounts
        // (contract note cbb8497c-a1f). One source of truth → we can't disagree.
        this._syncComposerSignal();
        this.$watch('_composerActive', () => this._syncComposerSignal());
        this.$watch('_tmuxSession', () => this._syncComposerSignal());
        // Re-sync when the voice binding changes (cross-session flips without any
        // local state change) — the getter reads the reactive voice store.
        this.$watch('_crossSessionDictation', () => this._syncComposerSignal());

        // Durable outbox wiring (auto-xkdoi Phase 2): commit on external
        // send-flip (voice), reconcile the optimistic tile as log entries
        // arrive, and restore a mid-flight outbox after reload/eviction.
        this.$watch('_outboxSendKey', () => this._onOutboxSendKey());
        this.$watch('_entryTailKey', () => this._tryReconcileOutbox());
        this.$watch('sessionKey', () => {
          if (!this._outboxRestored) { this._outboxRestored = true; this._restoreOutbox(); }
        });
        if (this.sessionKey && !this._outboxRestored) {
          this._outboxRestored = true; this._restoreOutbox();
        }

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

        // Re-fit the active terminal on viewport resize (keyboard open/close,
        // orientation change). Only one session viewer is mounted at a time in
        // page mode, so we refit whichever component has a live terminal.
        if (window.visualViewport && !window._svTermFitListener) {
          window._svTermFitListener = true;
          window.visualViewport.addEventListener('resize', function () {
            document.querySelectorAll('.session-viewer').forEach(function (el) {
              var cmp = window.Alpine && Alpine.$data(el);
              if (cmp && cmp._termInstance) {
                try { cmp._termInstance.fit(); } catch (e) {}
              }
            });
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
          this._lcSetState('error', 'init: URL pattern mismatch');
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

      // Mirror _composerActive onto document.body as the cross-component
      // signal (class + dataset sid). Guarded so we only clear the flag when
      // it's ours, never stomping another mounted viewer's signal.
      _syncComposerSignal() {
        // The shell mirrors the live buffer into the delivery session's outbox
        // so the capturing tile renders. That is NOT a page-global write, so it
        // runs on every surface — gating it with the body signals below stopped
        // board cards from ever staging a capture tile.
        var shell0 = window.Autonomy && window.Autonomy.voice && window.Autonomy.voice.shell;
        if (!this._pageSignals) {
          if (shell0 && typeof shell0.syncViewerOutboxCapture === 'function') shell0.syncViewerOutboxCapture();
          return;
        }
        if (typeof document === 'undefined' || !document.body) return;
        var sid = this._tmuxSession || '';
        if (this._composerActive) {
          document.body.classList.add('sv-viewer-composer-active');
          document.body.dataset.svComposerSession = sid;
        } else if (document.body.dataset.svComposerSession === sid) {
          document.body.classList.remove('sv-viewer-composer-active');
          delete document.body.dataset.svComposerSession;
        }
        // Cross-session cue — CSS-gated body class so the capsule's send fill
        // reacts across navigation without per-component JS reactivity.
        document.body.classList.toggle('sv-cross-session-dictation', this._crossSessionDictation);
        // Publish the viewed session into the voice store (reactive) so the
        // keyboard sheet — which has no per-viewer context — can show the same
        // cross-session "→ target" banner the dictation tile does (#23).
        var voice = this.getVoiceStore();
        if (voice && typeof voice.setViewedSession === 'function') {
          voice.setViewedSession(this._composerActive ? sid : '');
        }
        var shell = window.Autonomy && window.Autonomy.voice && window.Autonomy.voice.shell;
        if (shell && typeof shell.syncViewerOutboxCapture === 'function') {
          shell.syncViewerOutboxCapture();
        }
      },

      // One-shot durable-field read (viewer mount / reload) — live triggers
      // arrive over SSE, this only recovers a pending request the viewer
      // wasn't connected to see.
      async _checkPendingApproval() {
        const key = this.sessionKey;
        if (!key || !window.openApprovalOverlay) return;
        try {
          const r = await fetch('/api/session/' + encodeURIComponent(key));
          if (!r.ok) return;
          const p = (await r.json()).pending_approval || null;
          if (p && p.id && p.id !== this._lastApprovalPendingId) {
            this._lastApprovalPendingId = p.id;
            window.openApprovalOverlay(p.id);
          }
        } catch (e) { /* best-effort */ }
      },

      destroy() {
        // Do NOT unregister SSE — store keeps accumulating outside component lifecycle
        for (var i = 0; i < this._storeCleanups.length; i++) {
          if (typeof this._storeCleanups[i] === 'function') this._storeCleanups[i]();
        }
        this._storeCleanups = [];
        // Drop the composer-active body signal if it's ours, so the voice side
        // doesn't keep suppressing the caption / branching its send path after
        // we've left the viewer (stale-flag bug those branches must avoid).
        if (typeof document !== 'undefined' && document.body &&
            document.body.dataset.svComposerSession === (this._tmuxSession || '')) {
          document.body.classList.remove('sv-viewer-composer-active');
          document.body.classList.remove('sv-outbox-tile-present');
          document.body.classList.remove('sv-cross-session-dictation');
          delete document.body.dataset.svComposerSession;
          var voice = this.getVoiceStore();
          if (voice && typeof voice.setViewedSession === 'function' &&
              voice.viewedSessionId === (this._tmuxSession || '')) {
            voice.setViewedSession('');
          }
        }
        if (this._mode === 'page' && window._diagFocusedViewerId === this.sessionKey) {
          window._diagFocusedViewerId = null;
        }
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
        if (typeof window.unregisterHandler === 'function') {
          if (this._approvalPendingHandler) {
            window.unregisterHandler('approval:pending', this._approvalPendingHandler);
            this._approvalPendingHandler = null;
          }
          if (this._approvalDecidedHandler) {
            window.unregisterHandler('approval:decided', this._approvalDecidedHandler);
            this._approvalDecidedHandler = null;
          }
        }
        if (this._copyFeedbackTimer) {
          clearTimeout(this._copyFeedbackTimer);
          this._copyFeedbackTimer = null;
        }
        if (this._resumeHeartbeatInterval) {
          clearInterval(this._resumeHeartbeatInterval);
          this._resumeHeartbeatInterval = null;
        }
        if (this._resumeStartTimer) {
          clearInterval(this._resumeStartTimer);
          this._resumeStartTimer = null;
        }
        if (this._workspaceHandler && typeof window.unregisterHandler === 'function') {
          window.unregisterHandler('worktrees', this._workspaceHandler);
          this._workspaceHandler = null;
        }
        if (this._uploadUnsub) {
          try { this._uploadUnsub(); } catch (_) {}
          this._uploadUnsub = null;
        }
        if (this._viewportResizeHandler) {
          if (window.visualViewport) {
            window.visualViewport.removeEventListener('resize', this._viewportResizeHandler);
          } else {
            window.removeEventListener('resize', this._viewportResizeHandler);
          }
          this._viewportResizeHandler = null;
        }
        // Dispose terminal WS + xterm if the toggle was active. Leaking these
        // holds a server-side tmux attach and exhausts WebSocket slots.
        this._disposeTerminal();
      },

      // ── Setup helpers ───────────────────────────────────────────

      _setupWatchers() {
        var self = this;
        var sid = this.sessionKey;

        // Use ``Alpine.watch`` (global) rather than ``this.$watch``
        // (magic property): the global form returns a teardown function,
        // the magic property returns ``undefined``. The previous code
        // pushed ``this.$watch(...)``'s return into ``_storeCleanups``,
        // so on _reset() the cleanup loop's ``typeof === 'function'``
        // guard rejected every entry and no watcher was ever
        // unsubscribed. After N opens of the overlay, every new SSE
        // entry got ``appendOne()``'d N times — that was the
        // duplicate-message bug.
        function track(teardown) {
          if (typeof teardown === 'function') self._storeCleanups.push(teardown);
        }

        // Tick interval: bump _tick every second so running-tool elapsed times refresh
        if (!this._tickInterval) {
          this._tickInterval = setInterval(function() {
            if (self.isAgentWorking()) self._tick++;
          }, 1000);
        }

        // Single watcher on the store's monotonic merge revision.
        // The old design watched entries.length and coordinated with the
        // merge through a clearable _displayDirty flag — loadOlder's
        // synchronous rebuild cleared the flag before this watcher ran,
        // so the watcher "incrementally appended" indices that were now
        // the shifted OLD tail: duplicate tail tiles after every
        // scroll-up. Revisions are never cleared, so any interleaving of
        // rebuilds and watcher runs converges via _syncDisplayNow().
        track(Alpine.watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? (s._mergeRev || 0) : 0;
          },
          function() {
            self._syncDisplayNow();
            if (self.autoScroll) {
              self._scrollToBottom();
            } else {
              self._scheduleJumpToBottomUpdate();
            }
            // Update overlay header if in overlay mode
            if (self._mode === 'overlay') self._updateHeader();
          }
        ));

        // Watch isLive for overlay header updates
        if (this._mode === 'overlay') {
          track(Alpine.watch(
            function() {
              var s = Alpine.store('sessions')[sid];
              return s ? s.isLive : true;
            },
            function() { self._updateHeader(); }
          ));
        }

        // Dispose terminal only on an isLive true -> false transition.
        // A blanket x-effect on the root reacts to any dep change, which
        // caused a race with toggleTerminal's $nextTick mount on dead
        // sessions (open -> effect-dispose -> nextTick-remount -> stuck).
        track(Alpine.watch(
          function() {
            var s = Alpine.store('sessions')[sid];
            return s ? s.isLive : true;
          },
          function(val) {
            if (val === false && self._termInstance) {
              self._disposeTerminal();
              self.showTerminal = false;
            }
          }
        ));

        // On-the-fly gap detection: an SSE payload whose span starts past
        // the committed high-water (or switches files — rollover) fires
        // the same ranged catch-up immediately. Registered per session in
        // a global map the SSE handler consults; torn down with the rest
        // of the watchers.
        window._sessionGapHandlers = window._sessionGapHandlers || {};
        window._sessionGapHandlers[sid] = function (state, span) {
          var s = Alpine.store('sessions')[sid];
          if (!s) return;
          if (s._counters) s._counters.on_the_fly_catchups += 1;
          self._catchUp(s, 'span_' + state).catch(function (e) {
            console.warn('[sessionViewer] span-gap catch-up failed', e);
          });
        };
        track(function () {
          if (window._sessionGapHandlers &&
              window._sessionGapHandlers[sid]) {
            delete window._sessionGapHandlers[sid];
          }
        });

        this._setupResumeRecovery();
      },

      async _ensureWorkspaceName(workspaceId) {
        if (!workspaceId) return '';
        var fallback = _formatProject(workspaceId);
        if (this.project === workspaceId && this.projectLabel && this.projectLabel !== fallback) {
          return this.projectLabel;
        }
        if (this._workspacePromise && this._workspacePromise.id === workspaceId) {
          return this._workspacePromise.promise;
        }
        var self = this;
        var promise = (async function() {
          try {
            // base.html loads schemas.js before page-init runs; the
            // typeof guards against Schema.of were dead. Failures
            // inside the resolver still fall through to ``fallback``
            // via the catch below.
            var Workspace = await window.Schema.of('autonomy.workspace');
            var row = await Workspace.read(workspaceId);
            var payload = row && row.payload;
            var resolved = (payload && typeof payload.name === 'string' && payload.name.trim())
              ? payload.name.trim()
              : fallback;
            if (self.project === workspaceId) self.projectLabel = resolved;
            return resolved;
          } catch (_) {
            return fallback;
          } finally {
            if (self._workspacePromise && self._workspacePromise.id === workspaceId) {
              self._workspacePromise = null;
            }
          }
        })();
        this._workspacePromise = { id: workspaceId, promise: promise };
        return promise;
      },

      async _copyText(text) {
        if (!text) return false;
        try {
          if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
          }
        } catch (_) {}
        try {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          ta.setSelectionRange(0, ta.value.length);
          var ok = document.execCommand('copy');
          document.body.removeChild(ta);
          return !!ok;
        } catch (_) {
          return false;
        }
      },

      _markCopied(field) {
        this.copiedField = field;
        if (this._copyFeedbackTimer) clearTimeout(this._copyFeedbackTimer);
        var self = this;
        this._copyFeedbackTimer = setTimeout(function () {
          if (self.copiedField === field) self.copiedField = '';
        }, 1200);
      },

      async copyTmuxSession() {
        var tmux = this._tmuxSession;
        if (!tmux) return;
        var copied = await this._copyText(tmux);
        if (!copied) {
          if (window.showToast) window.showToast('Clipboard copy failed', 'error');
          return;
        }
        this._markCopied('tmux');
        if (window.showToast) window.showToast('Copied teamwork session', 'warning');
      },

      _setupResumeRecovery() {
        if (this._resumeRecoveryInstalled || !this.sessionKey || !this._tailUrl) return;
        this._resumeRecoveryInstalled = true;
        this._resumeHeartbeatAt = Date.now();

        var self = this;
        function onVisibility() {
          if (document.visibilityState === 'visible') {
            self._recoverSessionSync('visibility');
          }
        }
        function onPageShow(ev) {
          if (ev && ev.persisted) {
            self._recoverSessionSync('pageshow');
          }
        }
        function onFocus() {
          self._recoverSessionSync('focus');
        }
        function onOnline() {
          self._recoverSessionSync('online');
        }

        document.addEventListener('visibilitychange', onVisibility);
        window.addEventListener('pageshow', onPageShow);
        window.addEventListener('focus', onFocus);
        window.addEventListener('online', onOnline);
        this._storeCleanups.push(function() {
          document.removeEventListener('visibilitychange', onVisibility);
          window.removeEventListener('pageshow', onPageShow);
          window.removeEventListener('focus', onFocus);
          window.removeEventListener('online', onOnline);
          self._resumeRecoveryInstalled = false;
        });

        if (!this._resumeHeartbeatInterval) {
          this._resumeHeartbeatInterval = setInterval(function() {
            self._checkResumeHeartbeat(Date.now());
          }, 5000);
          this._storeCleanups.push(function() {
            if (self._resumeHeartbeatInterval) {
              clearInterval(self._resumeHeartbeatInterval);
              self._resumeHeartbeatInterval = null;
            }
          });
        }
      },

      _checkResumeHeartbeat(now) {
        var current = now || Date.now();
        if (!this._resumeHeartbeatAt) {
          this._resumeHeartbeatAt = current;
          return;
        }
        var elapsed = current - this._resumeHeartbeatAt;
        this._resumeHeartbeatAt = current;
        if (elapsed > 15000) {
          this._recoverSessionSync('heartbeat');
        }
      },

      // Two-round-trip wake protocol (auto-16g9t). On EVERY wake trigger:
      //   1. Unconditionally tear down and re-open the SSE CONNECTION —
      //      a few hundred bytes. readyState is never consulted: iOS
      //      zombie streams report OPEN while delivering nothing (the
      //      old `readyState === 2` gate was the silent
      //      "foregrounded but never catches up" hole). The buffer is
      //      never discarded.
      //   2. ONE ranged catch-up from the last COMMITTED (file, offset)
      //      pair — never a bare byte count, never advanced past
      //      dropped entries. Happy path: a small response with no
      //      entries and the current (file, size).
      async _recoverSessionSync(reason) {
        if (!this.sessionKey || !this._tailUrl || this.state === 'error') return;
        var store = Alpine.store('sessions')[this.sessionKey];
        if (!store) return;
        var c = store._counters;
        if (c) {
          c.wakeups_by_trigger[reason] = (c.wakeups_by_trigger[reason] || 0) + 1;
        }
        if (this._resumeRefreshInFlight) return this._resumeRefreshInFlight;

        var self = this;
        this._resumeRefreshInFlight = (async function() {
          try {
            if (typeof window.reconnectEvents === 'function') {
              var wasDead = !!(window._es && window._es.readyState === 2);
              window.reconnectEvents();
              if (c) {
                c.stream_rebuilds += 1;
                if (wasDead) c.stream_rebuilds_dead += 1;
              }
            }
            await self._catchUp(store, reason);
          } catch (e) {
            console.warn('[sessionViewer] resume catch-up failed (' + reason + ')', e);
          } finally {
            self._resumeRefreshInFlight = null;
          }
          // Also refresh worktree status — files may have changed while
          // the panel was hidden (operator committed elsewhere, agent
          // wrote new files, etc.). Both modes consume this state.
          self._refreshWorkspaceStatus();
        })();
        return this._resumeRefreshInFlight;
      },

      async _initUploads() {
        if (this._uploadProxy || !this._tmuxSession) return;
        if (!window.Schema || typeof window.Schema.of !== 'function') return;
        this._uploadProxy = await window.Schema.of('dashboard.session.upload');
        var members = await this._uploadProxy.all();
        var tmux = this._tmuxSession;
        var store = window.getSessionStore(this.sessionKey);
        this._uploadSeen = new Set();
        store._pendingAttachments = [];
        for (var i = 0; i < (members || []).length; i++) {
          var m = members[i];
          var p = m && m.payload;
          if (!p || p.target_session !== tmux) continue;
          this._uploadSeen.add(m.key);
          store._pendingAttachments.push(this._buildUploadEntry(p));
        }
        store._pendingAttachments.sort(function(a, b) {
          return (a.timestamp || '').localeCompare(b.timestamp || '');
        });
        if ((store.loaded || store.entries.length > 0) && window.flushPendingSessionAttachments) {
          window.flushPendingSessionAttachments(store, 'fetch');
        }
        var self = this;
        this._uploadUnsub = this._uploadProxy.onChange(function() {
          self._handleNewUpload();
        });
      },

      async _handleNewUpload() {
        if (!this._uploadProxy || !this._tmuxSession) return;
        var members;
        try {
          members = await this._uploadProxy.all();
        } catch (err) { return; }
        var store = window.getSessionStore && window.getSessionStore(this.sessionKey);
        if (!store) return;
        var tmux = this._tmuxSession;
        for (var i = 0; i < (members || []).length; i++) {
          var m = members[i];
          if (this._uploadSeen.has(m.key)) continue;
          var p = m && m.payload;
          if (!p || p.target_session !== tmux) continue;
          this._uploadSeen.add(m.key);
          var entry = this._buildUploadEntry(p);
          store._pendingAttachments = store._pendingAttachments || [];
          store._pendingAttachments.push(entry);
          window.flushPendingSessionAttachments(store, 'sse');
        }
      },

      _buildUploadEntry(p) {
        return {
          type: 'viewer_attachment',
          role: 'tool',
          timestamp: p.timestamp || '',
          rel_path: p.rel_path,
          filename: p.filename,
          mime: p.mime,
          size: p.size,
          session: p.target_session,
        };
      },

      _applyTailPayload(store, data) {
        if (!store || !data) return;
        if (data.offset !== undefined) store.offset = data.offset || 0;
        if (data.is_live !== undefined) store.isLive = !!data.is_live;
        if (data.resolved !== undefined) store.resolved = !!data.resolved;
        if (data.type !== undefined) store.sessionType = data.type || '';
        if (data.role !== undefined) store.role = data.role || '';
        if (data.activity_state !== undefined) store.activityState = data.activity_state || 'idle';
        if (data.pending_tool_ids !== undefined) {
          var ptids = {};
          for (var p = 0; p < data.pending_tool_ids.length; p++) {
            ptids[data.pending_tool_ids[p]] = true;
          }
          store.pendingToolIds = ptids;
        }
        // APPLY BEFORE ACK (review B7): merge the served batch first; the
        // committed high-water and scroll cursor advance only when the
        // application succeeded. Ack-before-apply marks progress past
        // unapplied content — the loss invariant.
        var applied = true;
        if (data.entries && data.entries.length > 0) {
          try {
            window.mergeSessionEntries(store, data, 'fetch');
          } catch (e) {
            applied = false;
            console.warn('[sessionViewer] merge failed; cursors not committed', e);
          }
        } else if (Array.isArray(data.chain) && data.chain.length && store.chain.length === 0) {
          store.chain = data.chain.slice();
        }
        if (!applied) return;
        if (data.older_cursor !== undefined) store.olderCursor = data.older_cursor;
        if (data.has_more !== undefined) store.hasMoreHistory = !!data.has_more;
        // Forward-delta cursor advances the committed high-water — a
        // fetch's cursor is complete-line-aligned server truth for
        // "everything up to here has been served AND applied".
        // MONOTONIC (round-1 addendum item 2): two racing catch-ups can
        // resolve out of order; a stale response's cursor must never
        // regress committed (executed race: 900 → 700, self-healing but
        // wasteful). Advance only forward in (chain position, off) order.
        if (data.cursor && data.cursor.file) {
          var curC = store.committed;
          var advance = !curC;
          if (curC) {
            if (data.cursor.file === curC.file) {
              advance = data.cursor.off >= curC.off;
            } else {
              var ci = store.chain.indexOf(curC.file);
              var ni = store.chain.indexOf(data.cursor.file);
              advance = ci !== -1 && ni !== -1 && ni > ci;
            }
          }
          if (advance) {
            store.committed = { file: data.cursor.file, off: data.cursor.off };
          }
        } else if (Array.isArray(data.window_spans) && data.window_spans.length &&
                   !store.committed) {
          // Cold-open reverse window: anchor committed at the newest
          // span's end so the first SSE span has a contiguity baseline.
          var lastSpan = data.window_spans[data.window_spans.length - 1];
          store.committed = { file: lastSpan.file, off: lastSpan.to };
        }
      },

      // Bring displayEntries up to the store's merge revision. Idempotent
      // and monotonic — safe to call from the watcher, loadOlder, and
      // configure in any interleaving (the _displayDirty race is gone).
      _syncDisplayNow() {
        var s = Alpine.store('sessions')[this.sessionKey];
        if (!s) return;
        var rev = s._mergeRev || 0;
        if (rev === (this._displayedRev || 0) &&
            s.entries.length === (this._displayedLen || 0)) return;
        if ((s._structureRev || 0) > (this._displayedRev || 0) ||
            s.entries.length < (this._displayedLen || 0)) {
          this._rebuildDisplay();
          return;
        }
        // Pure tail-appends since the last handled revision.
        for (var i = (this._displayedLen || 0); i < s.entries.length; i++) {
          window.SessionDisplay.appendOne(this.displayEntries, s.entries, i);
        }
        this._displayedRev = rev;
        this._displayedLen = s.entries.length;
      },

      _initialTailUrl() {
        return this._tailUrl + '?tail_entries=' + FAST_OPEN_TAIL_LINES;
      },

      _olderTailUrl(cursor) {
        return this._tailUrl
          + '?tail_entries=' + FAST_OPEN_TAIL_LINES
          + '&before_file=' + encodeURIComponent(cursor.file)
          + '&before=' + encodeURIComponent(cursor.off);
      },

      // ── Scroll helpers ──────────────────────────────────────────

      _scrollToBottom() {
        // Double-RAF after $nextTick: ensures Alpine has processed the x-for
        // template AND the browser has laid out all entries before we measure
        // scrollHeight. Critical for initial loads with 1000+ entries.
        // The container can legitimately be missing for a few frames while
        // the ready-state subtree renders, so retry briefly rather than
        // dropping the pin — a silent no-op here is exactly what shipped
        // the open-at-top regression.
        var self = this;
        var attempts = 0;
        function pin() {
          var el = self._entriesEl();
          if (!el) {
            if (attempts++ < 10) { requestAnimationFrame(pin); return; }
            console.warn('[sessionViewer] scroll-to-bottom skipped: entries container never appeared');
            return;
          }
          el.scrollTop = el.scrollHeight;
          self._lastScrollTop = el.scrollTop;
          self.showJumpToBottom = false;
        }
        this.$nextTick(function() {
          requestAnimationFrame(function() {
            requestAnimationFrame(pin);
          });
        });
      },

      async loadOlder() {
        if (this.loadingOlder || !this.hasMoreHistory || !this.olderCursor || !this._tailUrl) return;
        var store = Alpine.store('sessions')[this.sessionKey];
        if (!store) return;
        var el = this._entriesEl();
        var prevHeight = el ? el.scrollHeight : 0;
        var prevTop = el ? el.scrollTop : 0;
        this.loadingOlder = true;
        try {
          var res = await fetch(this._olderTailUrl(this.olderCursor));
          if (!res.ok) {
            throw new Error('History fetch failed (' + res.status + ')');
          }
          var data = await res.json();
          if (data.error) throw new Error(data.error);
          if (data.offset !== undefined) store.offset = data.offset || store.offset || 0;
          if (data.is_live !== undefined) store.isLive = !!data.is_live;
          if (data.resolved !== undefined) store.resolved = !!data.resolved;
          if (data.older_cursor !== undefined) store.olderCursor = data.older_cursor;
          if (data.has_more !== undefined) store.hasMoreHistory = !!data.has_more;
          if (data.entries && data.entries.length > 0) {
            window.mergeSessionEntries(store, data, 'fetch');
            // Bring the display current NOW so the scroll anchor math
            // sees the inserted tiles; the merge-revision watcher will
            // find nothing left to do (idempotent, no cleared flag).
            this._syncDisplayNow();
            var self = this;
            this.$nextTick(function() {
              requestAnimationFrame(function() {
                var scroller = self._entriesEl();
                if (!scroller) return;
                var delta = scroller.scrollHeight - prevHeight;
                scroller.scrollTop = prevTop + delta;
              });
            });
          }
        } catch (err) {
          console.warn('[sessionViewer] older-history fetch failed', err);
        } finally {
          this.loadingOlder = false;
        }
      },

      onScroll() {
        window.SessionRenderer.onScroll.call(this);
        this._updateJumpToBottom();
        var el = this._entriesEl();
        if (!el) return;
        if (el.scrollTop < 80 && this.hasMoreHistory && !this.loadingOlder) {
          this.loadOlder();
        }
      },

      _updateJumpToBottom() {
        // The state guard must come before any $refs read: this runs from
        // init() (via refreshViewportWidth), and touching $refs before the
        // ready subtree has rendered its first x-ref permanently caches an
        // empty $refs proxy for this component's root context — every later
        // read (scroll pin, composer, terminal) then silently fails. That
        // cache poisoning was the open-at-top regression (graph note
        // efb90d5d-f5b).
        if (this.state !== 'ready') {
          this.showJumpToBottom = false;
          return;
        }
        var el = this._entriesEl();
        if (!el || this._mode !== 'page' || this.showTerminal || !this.entries.length) {
          this.showJumpToBottom = false;
          return;
        }
        var distance = el.scrollHeight - el.scrollTop - el.clientHeight;
        this.showJumpToBottom = el.clientHeight > 0 && distance > el.clientHeight * 3;
      },

      _scheduleJumpToBottomUpdate() {
        var self = this;
        this.$nextTick(function () {
          requestAnimationFrame(function () { self._updateJumpToBottom(); });
        });
      },

      jumpToBottom() {
        this.showJumpToBottom = false;
        this.resumeScroll();
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
          var att = {
            id: id, name: file.name, isImage: isImage, dataUrl: null,
            path: null, rel_path: null, mime: null, size: null,
          };
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
            var doFetch = (window.Autonomy && window.Autonomy.fetch) || fetch;
            doFetch('/api/upload', { method: 'POST', body: form })
              .then(function(r) { return r.json(); })
              .then(function(data) {
                if (data.ok) {
                  var found = self.attachments.find(function(a) { return a.id === attId; });
                  if (found) {
                    var meta = (data.files && data.files[0]) || data;
                    found.path = meta.path;
                    found.rel_path = meta.rel_path || '';
                    found.mime = meta.mime || '';
                    found.size = meta.size || 0;
                  }
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

      // Contenteditable input handler — synchronous draft persistence.
      // The previous 300ms debounce raced with sendMessage: the pending timer
      // could fire after send cleared s.draftText, writing the sent text back
      // and resurfacing it as a "ghost" in the input on navigate-back. The
      // store write is a plain in-memory Alpine assignment; no I/O, so the
      // debounce wasn't buying anything. _draftTimer is now unused but left
      // in place to avoid churning the data-fields block.
      onInput(el) {
        this.persistComposerDraft(this.readComposerTextFromElement(el));
      },

      async sendMessage() {
        var el = this.$refs.messageInput;
        if (!el) return;
        // Desktop voice: fold any live dictation into the composer BEFORE
        // reading it, so a single Send click captures speech + typed text. This
        // removes the separate "import to box" click (and, with the auto-mute
        // gone, the unmute click) — desktop dictation is now one click like
        // mobile instead of import->send->unmute.
        if (this.showDesktopVoiceImport) {
          this.importVoiceBufferToComposer();
        }
        var text = this.readComposerText();
        if (!this.canSendComposerText(text) || this.sending) return;
        // Block if any attachment upload is still in flight — sending now
        // would post a body without paths and skip the substrate write.
        if (this.uploading) return;
        this.sending = true;
        var tmux = this._tmuxSession;
        try {
          // Compose attachments + text into one tmux paste so the agent's
          // transcript records a single user turn whose content is the
          // image path(s) plus the operator's description, instead of the
          // previous N+1 turns (one per attachment, one for the text). The
          // viewer's user-turn renderer detects ``/tmp/<filename>.<ext>``
          // lines in the body and surfaces inline thumbnails for each.
          var body = this.buildComposerBody(text);
          if (!body) return;

          // Durable optimistic send: stage the message in the outbox (shows the
          // pending tile + persists to localStorage), clear the composer right
          // away (the text is now safely held in the outbox, never the ether),
          // then run the durable send. The composer clears on STAGING, not on
          // HTTP 200 — confirmation is the JSONL log echo (reconciliation), and
          // a failed/timed-out send parks the tile in 'unconfirmed', recoverable.
          var sid = this.sessionKey;
          var s = window.getSessionStore(sid);
          // One message in flight per session. If an outbox is already pending
          // (sending or unconfirmed), do NOT stage a second send — this is the
          // guard the capsule path (_commitBuffer) already has; the viewer Send
          // button was missing it, so repeated taps re-POSTed the same text (the
          // duplicate-send the operator demonstrated). A stuck message is retried
          // explicitly via resendOutbox(), not by tapping Send again.
          if (s && s.outbox && (s.outbox.state === 'sending' || s.outbox.state === 'unconfirmed')) {
            this.sending = false;
            return;
          }
          var localId = window.newOutboxId ? window.newOutboxId() : ('ob_' + (s ? (s.seq || 0) : 0));
          if (!s || !window.stageOutboxSend) {
            console.warn('[sessionViewer] durable outbox engine unavailable; composer left intact');
            return;
          }
          var sendPromise = window.stageOutboxSend(sid, {
            localId: localId,
            state: 'sending',
            source: 'manual',
            text: body,
            ts: Date.now(),
          }, { tmuxSession: tmux });
          this.clearComposer();   // text safely staged in outbox; also clears the persisted draft
          el.blur();

          var ok = await sendPromise;

          // Substrate write fires only after a tmux-accepted send — so the
          // viewer tile only appears for attachments actually sent.
          if (ok && this._uploadProxy && tmux) {
            var ts = new Date().toISOString();
            for (var si = 0; si < this.attachments.length; si++) {
              var sa = this.attachments[si];
              if (!sa.rel_path) continue;
              try {
                await this._uploadProxy.append({
                  target_session: tmux,
                  filename: sa.name,
                  rel_path: sa.rel_path,
                  mime: sa.mime || '',
                  size: sa.size || 0,
                  timestamp: ts,
                });
              } catch (err) {
                console.warn('[sessionViewer] upload row write failed:', err);
              }
            }
          }
          this.clearAttachments();
        } catch (e) {
          console.warn('[sessionViewer] send failed:', e);
        } finally {
          this.sending = false;
        }
      },

      // ── Terminal toggle ──────────────────────────────────────────

      toggleTerminal() {
        if (!this._tmuxSession) return;  // guard: nothing to attach to
        this.showTerminal = !this.showTerminal;
        if (this.showTerminal) {
          var self = this;
          this.$nextTick(function () {
            // State may have flipped back to false between the toggle call
            // and $nextTick (e.g. the isLive-transition watch disposed us
            // after the user tapped a dead session's toggle). Bail cleanly
            // instead of mounting into a hidden/disposed container.
            if (!self.showTerminal) return;
            var container = self.$refs.termContainer;
            if (!container || typeof window.mountTerminal !== 'function') return;
            // The emulator is fetched on first use rather than by every page
            // in the shell, so mounting waits for it. Re-check the toggle
            // afterwards: the wait is a real gap, and the reader may have
            // closed the terminal or left the session inside it.
            var ready = typeof window.ensureTerminalLibs === 'function'
              ? window.ensureTerminalLibs()
              : Promise.resolve();
            ready.then(function () {
              if (!self.showTerminal || !self.$refs.termContainer) return;
              self._termInstance = window.mountTerminal(container, self._tmuxSession);
              // Fit after mount — the grid row is now sized and the terminal can measure
              setTimeout(function () {
                if (self._termInstance) self._termInstance.fit();
              }, 50);
            }).catch(function (err) {
              console.warn('terminal libraries failed to load', err);
            });
          });
        } else {
          if (this._termInstance) {
            try { this._termInstance.dispose(); } catch (e) {}
            this._termInstance = null;
          }
          // The composer is behind x-if while the terminal is open, so Alpine
          // creates a fresh (empty) contenteditable when this flips back. Its
          // draft is still durable in the session store/localStorage; hydrate
          // the replacement element once the DOM has been remounted.
          var self = this;
          this.$nextTick(function () {
            if (!self.showTerminal) self.restoreComposerDraft();
          });
        }
      },

      _disposeTerminal() {
        if (this._termInstance) {
          try { this._termInstance.dispose(); } catch (e) {}
          this._termInstance = null;
        }
        this.showTerminal = false;
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

      // ── Background (Ctrl-B) — Claude harness only ───────────────
      // Claude reads Ctrl-B as "background the running tool" rather
      // than cancelling it (which is what Escape does).

      async background() {
        var tmux = this._tmuxSession;
        if (!tmux) return;
        try {
          await fetch('/api/session/' + encodeURIComponent(tmux) + '/background', {
            method: 'POST',
          });
        } catch (e) {
          console.warn('[sessionViewer] background failed:', e);
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
                this.projectLabel = _formatProject(data.project);
                this._ensureWorkspaceName(data.project);
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

      // ── Turn-correction overlay hydration / mutation ───────────
      //
      // All correction state lives in the shared session store's single
      // ``_turnCorrections`` map, written through ``window.applyTurnCorrection``.
      // Hydration seeds it from persisted rows; accept/dismiss update it
      // optimistically and reconcile against the server row. The viewer holds
      // no duplicate map, appends nothing to ``entries``, and never emits a
      // synthetic message entry — the renderer joins tiles to this one map.

      // Pull persisted correction rows for this session and seed the store
      // map. Sparse: empty response → empty map. Failures are swallowed so a
      // transient persistence outage doesn't block the rest of the viewer.
      async _hydrateCorrections(opts) {
        if (!this.sessionKey) return;
        var token = ++this._correctionHydrateToken;
        try {
          var url = '/api/session/' + encodeURIComponent(this.sessionKey) + '/turn-corrections';
          if (opts && opts.fresh) {
            url += '?_=' + Date.now();
          }
          var res = await fetch(url, {
            cache: 'no-store',
            headers: { 'Cache-Control': 'no-cache' },
          });
          if (!res.ok) return;
          var data = await res.json();
          if (token !== this._correctionHydrateToken) return;
          var ss = window.getSessionStore(this.sessionKey);
          if (!ss) return;
          var rows = (data && data.corrections) || [];
          for (var i = 0; i < rows.length; i++) {
            window.applyTurnCorrection(ss, rows[i]);
          }
        } catch (e) {
          // best-effort
        }
      },

      async acceptCorrection(entry) {
        return this._transitionCorrection(entry, 'accept', 'accepted');
      },

      async dismissCorrection(entry) {
        return this._transitionCorrection(entry, 'dismiss', 'dismissed');
      },

      async _transitionCorrection(entry, action, terminalStatus) {
        if (!entry || !entry.message_id) return;
        var ss = window.getSessionStore(this.sessionKey);
        if (!ss) return;
        var c = (ss._turnCorrections || {})[entry.message_id];
        if (!c) return;
        // Optimistic UI: flip the store state immediately so the tile reacts
        // without a round-trip; the network call confirms or rolls back.
        var prev = c.status;
        window.applyTurnCorrection(ss, Object.assign({}, c, { status: terminalStatus }));
        try {
          var url = '/api/session/' + encodeURIComponent(this.sessionKey)
            + '/turn-corrections/' + encodeURIComponent(entry.message_id)
            + '/' + action;
          var res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ original_sha256: c.original_sha256 || '' }),
          });
          if (!res.ok) {
            if (res.status === 409) {
              // Server rejected our transition (stale/already-terminal) —
              // rehydrate the authoritative row rather than guess.
              await this._hydrateCorrections({ fresh: true });
              return;
            }
            // Roll back on failure so the operator can retry.
            window.applyTurnCorrection(ss, Object.assign({}, c, { status: prev }));
            return;
          }
          var body = await res.json();
          var serverRow = body && body.correction;
          if (serverRow) {
            window.applyTurnCorrection(ss, serverRow);
          }
        } catch (e) {
          window.applyTurnCorrection(ss, Object.assign({}, c, { status: prev }));
        }
      },

      // ── Backfill fetch ──────────────────────────────────────────

      async _fetchBacklog(store) {
        // Probe first: 404 means the session does not exist and the viewer
        // must surface an error state (auto-ylj6r test #19). We do a HEAD-
        // equivalent lightweight GET so we can inspect response.status.
        var probe = await fetch(this._initialTailUrl());
        if (probe.status === 404) {
          var err = new Error('Session not found');
          err.missingSession = true;
          try {
            var body = await probe.json();
            if (body && body.error) err.message = body.error;
          } catch (e) { /* best-effort */ }
          throw err;
        }
        if (!probe.ok) {
          var perr = new Error('Tail request failed (' + probe.status + ')');
          perr.failed = true;
          throw perr;
        }
        var data = await probe.json();

        if (data.error) throw new Error(data.error);

        this._applyTailPayload(store, data);

        this._rebuildDisplay();
      },

      // Ranged catch-up from the committed (file, offset) high-water.
      // Bounded per wake: responses are server-byte-capped and this loop
      // runs at most _CATCHUP_MAX_ROUNDS before rescheduling itself on a
      // fresh task — a huge backlog never blocks the UI.
      async _catchUp(store, why) {
        var c = store._counters;
        var t0 = Date.now();
        var committedAtStart = store.committed
          ? { file: store.committed.file, off: store.committed.off }
          : null;
        var fetchedEntries = 0;
        var fetchedBytes = 0;
        var rounds = 0;
        var hasMore = false;
        var MAX_ROUNDS = 6;
        do {
          var url;
          if (store.committed && store.committed.file) {
            url = this._tailUrl
              + '?after_file=' + encodeURIComponent(store.committed.file)
              + '&after=' + encodeURIComponent(store.committed.off);
          } else {
            // No committed anchor yet (never-loaded store): the initial
            // reverse window is the right first fetch, not a full replay.
            url = this._initialTailUrl();
          }
          var res = await fetch(url);
          if (!res.ok) throw new Error('Tail request failed (' + res.status + ')');
          var data = await res.json();
          if (data.error) throw new Error(data.error);
          var insertedBefore = (c && c.merge_inserted) || 0;
          var prevCommitted = store.committed;
          this._applyTailPayload(store, data);
          var inserted = ((c && c.merge_inserted) || 0) - insertedBefore;
          fetchedEntries += inserted;
          if (Array.isArray(data.window_spans)) {
            for (var si = 0; si < data.window_spans.length; si++) {
              fetchedBytes += Math.max(0,
                data.window_spans[si].to - data.window_spans[si].from);
            }
          }
          // Conclusion-contradicted: this fetch inserted entries at or
          // below the position we already believed committed — we held a
          // hole we thought was closed. Must trend to zero.
          if (c && inserted > 0 && committedAtStart && store._lastMinInsertRef) {
            var ref = store._lastMinInsertRef;
            var refIdx = store.chain.indexOf(ref.file);
            var comIdx = store.chain.indexOf(committedAtStart.file);
            if (refIdx !== -1 && comIdx !== -1 &&
                (refIdx < comIdx ||
                 (refIdx === comIdx && ref.off < committedAtStart.off))) {
              c.conclusion_contradicted += 1;
            }
          }
          hasMore = !!data.has_more_forward;
          // Progress guard: a capped response that failed to advance the
          // cursor must not spin (the server guarantees whole-line
          // progress now; this is defense in depth).
          if (hasMore && prevCommitted && store.committed &&
              store.committed.file === prevCommitted.file &&
              store.committed.off <= prevCommitted.off) {
            store._catchupStall = (store._catchupStall || 0) + 1;
            if (c) c.catchup_stalls += 1;
            break;
          }
          store._catchupStall = 0;
          rounds++;
        } while (hasMore && rounds < MAX_ROUNDS);

        if (c) {
          c.catchup_count += 1;
          c.catchup_latency_ms_total += Date.now() - t0;
          if (fetchedEntries === 0) c.wake_happy += 1;
          else {
            c.wake_gap += 1;
            c.gap_entries_total += fetchedEntries;
            c.gap_bytes_total += fetchedBytes;
          }
        }
        if (hasMore) {
          // Reschedule the remainder — never block this task on a huge
          // backlog (the server caps each response's bytes). Review B2:
          // a stalled cursor must NOT hot-loop at zero delay — back off
          // exponentially and, past the attempt bound, hand responsibility
          // back to the wake triggers (heartbeat fires within 15s) rather
          // than chaining forever.
          var stalls = store._catchupStall || 0;
          if (stalls > 6) {
            console.warn('[sessionViewer] catch-up stalled repeatedly; deferring to next wake');
            return fetchedEntries;
          }
          var delay = stalls > 0 ? Math.min(500 * Math.pow(2, stalls - 1), 15000) : 0;
          var self = this;
          setTimeout(function () {
            self._catchUp(store, 'continuation').catch(function (e) {
              console.warn('[sessionViewer] catch-up continuation failed', e);
            });
          }, delay);
        }
        return fetchedEntries;
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
            window.mergeSessionEntries(store, data, 'fetch');
            this._syncDisplayNow();
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
        // Tear down every watcher registered by _setupWatchers. Each
        // entry is the teardown function returned by Alpine.watch().
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
        this._disposeTerminal();
        // Reset view state
        this._lcSetState('loading', '_reset: viewer dismounted/recycled');
        this.sessionKey = '';
        this.displayEntries = [];
        this._displayedRev = 0;
        this._displayedLen = 0;
        this._expanded = {};
        this._expandView = {};
        this._groupExpanded = {};
        this._groupExpandView = {};
        this._correctionDisplayMode = {};
        this._correctionHydrateToken = 0;
        this.autoScroll = true;
        this.showJumpToBottom = false;
        this._runDir = '';
        this._tailUrl = '';
        this._tick = 0;
        this._resumeRecoveryInstalled = false;
        this._resumeHeartbeatAt = 0;
        this._resumeRefreshInFlight = null;
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

      // Update ``_workspaceStatus`` from a worktree-rows payload (either
      // pushed via the ``worktrees`` SSE topic or fetched as a one-shot
      // fallback). Both the overlay title-bar anchor and the page-mode
      // header anchor bind to ``hasWorkspaceChanges`` /
      // ``workspaceStatusTooltip`` via Alpine, so updating the cache
      // is the only thing this method needs to do — the DOM follows.
      _applyWorkspaceRows(rows) {
        if (!this.sessionKey || !Array.isArray(rows)) return;
        var sid = this.sessionKey;
        var matching = rows.filter(function (r) { return r.session_name === sid; });
        var dirtyCount = 0;
        var commitsAhead = 0;
        for (var i = 0; i < matching.length; i++) {
          var r = matching[i];
          if (r.is_dirty) dirtyCount += (typeof r.dirty_count === 'number')
            ? r.dirty_count
            : (r.dirty_files || []).length;
          commitsAhead += r.commits_ahead || 0;
        }
        this._workspaceStatus = {
          hasChanges: (dirtyCount + commitsAhead) > 0,
          dirtyCount: dirtyCount,
          commitsAhead: commitsAhead,
        };
      },
      async _refreshWorkspaceStatus() {
        if (!this.sessionKey) return;
        try {
          var res = await fetch('/api/worktrees');
          if (!res.ok) return;
          this._applyWorkspaceRows(await res.json());
        } catch (e) {
          // Best-effort; don't poison the session viewer on fetch failure.
        }
      },

    }));
  });
})();
