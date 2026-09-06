/**
 * Session Board — /sessions/board (wide viewports).
 *
 * Columns are groups. Live session cards stack top-to-bottom inside a column,
 * never in front of or behind one another. Column width and card height are
 * adjustable; a card is moved by dragging its header; a column collapses the
 * moment its last session leaves. Every card hosts the production session
 * viewer in panel mode (sessionViewerPage({mode:'panel'}) rendering
 * partials/session-entries.html) and the production card partial
 * (partials/session-card.html) as its head. Nothing here formats a turn.
 *
 * Design of record: Design Studio 2c31c67a-ca7d-4f92-aee3-e3e2a15de87d
 * (graph://e61d916d-34b). This file ports that design's behaviour, with two
 * deliberate differences: a card click binds dictation through the standard
 * voice path (window.Autonomy.voice.ui.onClick), and the org glyph opens an
 * inline popover in the card rather than the global slide-up tray, which is
 * the phone's affordance and dims the whole screen.
 *
 * Dictation is shown by the card itself: each card's panel viewer renders
 * partials/session-pending-tiles.html, the same outbox/dictation surface the
 * session viewer uses, so the operator's words appear in the card they are
 * talking to. There is no floating capsule.
 *
 * State ownership. Membership (which session sits in which column) is the
 * shared session-group record: the store carries groupId / groupTab / group
 * from the registry broadcast, and the board derives its columns from them
 * on every registry event. Operator drags and ⤢ write back through
 * PUT /api/session/{name}/group and POST/PUT /api/groups — the same records
 * agents write with `graph group`. Layout (column order and width, card
 * height, presentation) is the operator's dashboard.session.board.layout
 * Settings member, read and written through /api/session-board/layout, so
 * the same board appears on every desktop. Nothing durable lives in the
 * browser.
 *
 * Depends on: session-store.js (Alpine.store('sessions'), sessionStoreReady,
 * getSessionStore), sessions.js (window.sessionCardHelpers), lifecycle.js,
 * voice-ui.js, events.js (registerHandler for the 'resources' topic).
 */
(function () {
  var LAYOUT_URL = '/api/session-board/layout';
  var GROUP_SET_ID = 'dashboard.session.group', LAYOUT_SET_ID = 'dashboard.session.board.layout';
  var MIN_COL = 340, MIN_CARD = 160, DEFAULT_CARD = 340, DEFAULT_COL = 520, STACK_GAP = 10;
  var SPARK_SAMPLES = 100;

  function fmtBytes(n) {
    if (n == null || isNaN(n)) return '—';
    if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'GB';
    if (n >= 1e6) return (n / 1e6).toFixed(0) + 'MB';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + 'KB';
    return n + 'B';
  }
  function orgColor(row) { return (row && row.org && row.org.color) || '#334155'; }
  // git log dates arrive as "YYYY-MM-DD HH:MM" in the host's local time.
  function _commitTime(text) {
    if (!text) return 0;
    var t = Date.parse(String(text).replace(' ', 'T'));
    return isNaN(t) ? 0 : t;
  }
  function _ago(ms) {
    var m = Math.round(ms / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return m + 'm ago';
    return Math.round(m / 60) + 'h ago';
  }
  function helper(name) {
    var h = window.sessionCardHelpers;
    return function () { return h && h[name] ? h[name].apply(h, arguments) : ''; };
  }

  /**
   * Pure slot math — exported for the node tests. Given the frame snapshotted
   * at drag start (per column: the resting midpoint of every OTHER card, i.e.
   * the stack as it would be with the moving tile lifted out) and a pointer
   * position, return {colId, idx} or null. Depends on nothing but its inputs.
   */
  function slotFor(frame, x, y) {
    if (!frame) return null;
    if (frame.zone && x >= frame.zone.left && x <= frame.zone.right) return { colId: null, idx: 0, zone: true };
    for (var c = 0; c < frame.cols.length; c++) {
      var col = frame.cols[c];
      if (x < col.left || x > col.right) continue;
      var yy = y + (col.scrollDelta || 0);
      var idx = 0;
      for (var m = 0; m < col.mids.length; m++) if (yy > col.mids[m]) idx++;
      return { colId: col.id, idx: idx, zone: false };
    }
    return null;
  }

  /**
   * Pure layout invariant — exported for the node tests. Every live id appears
   * exactly once; unknown ids drop; empty columns collapse (except the solo
   * column, a column being dragged from, and a provisional column); the solo
   * column keeps its arranged order and its position, and any newly ungrouped
   * id is appended to it.
   */
  function normaliseColumns(cols, ids, keepAlive) {
    keepAlive = keepAlive || {};
    var seen = {};
    var soloAt = -1, soloPrev = null;
    cols.forEach(function (c, i) { if (c.id === 'solo') { soloAt = i; soloPrev = c; } });
    var out = cols.filter(function (c) { return c.id !== 'solo'; }).map(function (c) {
      c.members = c.members.filter(function (m) { if (seen[m] || ids.indexOf(m) === -1) return false; seen[m] = true; return true; });
      return c;
    }).filter(function (c) { return c.members.length > 0 || keepAlive[c.id]; });
    out.forEach(function (c) { if (c.focus && c.members.indexOf(c.focus) === -1) c.focus = null; });
    var solo = soloPrev ? soloPrev.members.filter(function (id) { return !seen[id] && ids.indexOf(id) !== -1; }) : [];
    ids.forEach(function (id) { if (!seen[id] && solo.indexOf(id) === -1) solo.push(id); });
    var soloCol = {
      id: 'solo', title: 'Ungrouped', color: '#334155',
      width: soloPrev ? soloPrev.width : DEFAULT_COL, _sized: soloPrev ? !!soloPrev._sized : false,
      members: solo,
    };
    var at = soloAt === -1 ? 0 : Math.min(soloAt, out.length);
    out.splice(at, 0, soloCol);
    out.forEach(function (c) { if (!c._sized) c.width = Math.max(MIN_COL, Math.min(760, c.width || DEFAULT_COL)); });
    return out.filter(function (c) { return c.id !== 'solo' || c.members.length > 0 || out.length === 1 || keepAlive.solo; });
  }

  /**
   * Pure column-order math — exported for the node tests. `mids` are the
   * resting horizontal midpoints of every OTHER column (the row as it would
   * be with the moving column lifted out), left to right; the answer is the
   * index the moving column takes for a pointer at `x`.
   */
  function columnSlotFor(mids, x) {
    var idx = 0;
    for (var i = 0; i < mids.length; i++) if (x > mids[i]) idx++;
    return idx;
  }

  window.SessionBoardLogic = { slotFor: slotFor, normaliseColumns: normaliseColumns, columnSlotFor: columnSlotFor };

  document.addEventListener('alpine:init', function () {
    Alpine.data('sessionsBoard', function () { return {
      rows: [], columns: [], presentation: 'transcript',
      cardPresentations: {}, cardHeights: {}, resources: {}, _focusSession: '',
      boundId: '', dragId: '', dragging: false, movingCol: '', viewportTick: 0,
      organize: { state: 'idle', status: '', runId: '' }, commitTick: 0,
      menuFor: '', menuActions: [],
      modelMenuFor: '', modelOptions: [], modelBusy: '', _modelCache: {},
      commits: {}, workspaces: {},
      // session -> column slug the operator just moved it to, held until the
      // store's groupId agrees. Without it a registry broadcast that lands
      // between the drop and the server write re-derives columns from the
      // STALE groupId, so the card snaps back to its old column and then
      // forward again — the operator saw it in two places at once.
      _pending: {},
      _dragSource: null, _provisional: null, _frame: null, _drag: null,
      _resourceTipOpen: null, _diskRefreshing: {}, resumeError: {}, resuming: {}, resumed: {},
      _workspaceStatusByTmux: {},

      // ── lifecycle ──
      init() {
        var self = this;
        var saved = null;
        try { localStorage.removeItem('sessions.board.layout'); } catch (e) {}   // pre-Settings key from the first release
        // Nothing is derived or persisted until the session store has its
        // first roster. Deriving earlier saw zero live sessions, collapsed
        // every saved column as empty, and wrote that empty layout back over
        // the operator's arrangement — the "refresh and everything is gone"
        // bug reported on 2026-09-06.
        this._ready = false;
        this._saved = saved;
        this._onStoreChanged = function () { self.refresh(); };
        window.addEventListener('sessions:store-changed', this._onStoreChanged);
        window.addEventListener('sessions:registry-changed', this._onStoreChanged);
        this._onResize = function () { self.viewportTick++; };
        window.addEventListener('resize', this._onResize);
        this._onDocClick = function (e) {
          if (self.menuFor && !e.target.closest('.sb-menu, .sc-org')) self.menuFor = '';
          if (self.modelMenuFor && !e.target.closest('.sb-model-menu, .sc-harness')) self.modelMenuFor = '';
        };
        document.addEventListener('click', this._onDocClick, true);
        // A CrossTalk message names its sender as a link to that session's
        // full-page viewer. On the board the sender is usually a card already
        // on screen, so the link reveals it instead of leaving the board:
        // flip it to its transcript, make it the dictation target, bring it
        // into view. Bubbles to us before app.js's document-level router, so
        // stopping here keeps the SPA from navigating. A sender with no card
        // (dead, or filtered out) navigates normally.
        // The model badge opens a switcher for its own session.
        this._onBadgeClick = function (e) {
          var badge = e.target.closest('.sc-harness'); if (!badge) return;
          var card = badge.closest('.sb-card'); if (!card) return;
          e.preventDefault(); e.stopPropagation();
          self.openModelMenu(card.dataset.session);
        };
        this.$refs.board.addEventListener('click', this._onBadgeClick, true);
        this._onSenderClick = function (e) {
          var a = e.target.closest('a.sc-ct-sender'); if (!a) return;
          var m = String(a.getAttribute('href') || '').match(/^\/session\/[^/]+\/([^/?#]+)/);
          if (!m) return;
          if (!self.revealSession(decodeURIComponent(m[1]))) return;
          e.preventDefault(); e.stopPropagation();
        };
        this.$refs.board.addEventListener('click', this._onSenderClick);
        this._resourceHandler = function (d) { if (d && typeof d === 'object') self._applyResourceRows(d.sessions || d); };
        if (window.registerHandler) window.registerHandler('resources', this._resourceHandler);
        this._hydrateResources();
        // Commits land on the card that made them: /api/worktrees carries each
        // branch's session_name and its commits, and the collector re-broadcasts
        // on the 'worktrees' topic whenever a tree changes.
        this._worktreeHandler = function () { self._hydrateWorktrees(); };
        if (window.registerHandler) window.registerHandler('worktrees', this._worktreeHandler);
        this._hydrateWorktrees();
        this._commitTimer = setInterval(function () { self.commitTick++; }, 60000);
        var voice = Alpine.store('voice');
        if (voice) this.boundId = voice.boundSessionId || '';
        this.$watch('columns', function () { self.persist(); }, { deep: true });
        // The layout member and the first store roster both have to be in
        // before anything is derived (or written back).
        var ready = window.sessionStoreReady || Promise.resolve();
        Promise.all([ready, this.loadLayout()]).then(function (r) {
          var layout = r[1] || {};
          if (layout.presentation === 'stats' || layout.presentation === 'transcript') self.presentation = layout.presentation;
          self.cardHeights = layout.heights || {};
          self.cardPresentations = layout.presentations || {};
          self._focusSession = layout.focus_session || '';
          self._ready = true;
          self.refresh({ columns: (layout.column_order || []).map(function (id) { return { id: id, members: [] }; }), widths: layout.widths || {} });
        });
        // Group or layout writes from any session, tab or node land here.
        if (window.dashboardEvents && window.dashboardEvents.onSettingChanged) {
          this._offGroupChange = window.dashboardEvents.onSettingChanged(GROUP_SET_ID, function () { self.refresh(); });
          this._offLayoutChange = window.dashboardEvents.onSettingChanged(LAYOUT_SET_ID, function () { self.reloadLayout(); });
        }
      },
      destroy() {
        document.removeEventListener('click', this._onDocClick, true);
        if (this.$refs.board && this._onSenderClick) this.$refs.board.removeEventListener('click', this._onSenderClick);
        if (this.$refs.board && this._onBadgeClick) this.$refs.board.removeEventListener('click', this._onBadgeClick, true);
        if (this._offGroupChange) this._offGroupChange();
        if (this._offLayoutChange) this._offLayoutChange();
        window.removeEventListener('sessions:store-changed', this._onStoreChanged);
        window.removeEventListener('sessions:registry-changed', this._onStoreChanged);
        window.removeEventListener('resize', this._onResize);
        if (window.unregisterHandler && this._resourceHandler) window.unregisterHandler('resources', this._resourceHandler);
        if (window.unregisterHandler && this._worktreeHandler) window.unregisterHandler('worktrees', this._worktreeHandler);
        clearInterval(this._commitTimer);
      },
      refresh(saved) {
        if (!this._ready) return;
        if (this.dragging) { this._refreshPending = true; return; }
        this._refreshPending = false;
        this.rows = this.rowsFromStore();
        this.columns = this.normalise(this.columnsFromStore(saved));
        // A full-height card survives a refresh: re-seat the saved focus on
        // whichever column now holds that session.
        if (this._focusSession) {
          var want = this._focusSession;
          this.columns.forEach(function (c) { c.focus = c.members.indexOf(want) !== -1 ? want : null; });
          if (!this.columns.some(function (c) { return c.focus; })) this._focusSession = '';
        }
        var voice = Alpine.store('voice');
        if (voice) this.boundId = voice.boundSessionId || '';
      },
      // Columns = the store's group membership. Previous columns (or the saved
      // layout on first paint) contribute only order, width, focus and the
      // arranged order of members inside a column; membership itself is server truth.
      // The column a session belongs to right now: the operator's un-acked
      // move if there is one, else the shared record.
      _groupOf(id) {
        var pend = this._pending[id];
        var store = Alpine.store('sessions')[id];
        var actual = (store && store.groupId) || null;
        if (pend !== undefined) {
          if (pend === actual || (pend === null && !actual)) { delete this._pending[id]; return actual; }
          return pend;
        }
        return actual;
      },
      columnsFromStore(saved) {
        var all = Alpine.store('sessions'), self = this;
        var prev = this.columns.length ? this.columns : ((saved && saved.columns) ? saved.columns : []);
        var order = prev.map(function (c) { return c.id; });
        var byId = {}; prev.forEach(function (c) { byId[c.id] = c; });
        var groups = {};
        this.rows.forEach(function (r) {
          var st = all[r.id]; var gid = self._groupOf(r.id); if (!gid) return;
          var g = (st && st.group) || {};
          var col = groups[gid];
          if (!col) {
            var was = byId[gid] || {};
            col = { id: gid, title: g.name || was.title || gid, color: g.color || was.color || orgColor(r), why: g.why || '',
                    width: was.width || DEFAULT_COL, _sized: !!was._sized, focus: was.focus || null, members: [], _synced: true };
            if (saved && saved.widths && saved.widths[gid]) { col.width = saved.widths[gid]; col._sized = true; }
            groups[gid] = col;
          }
          col.members.push(r.id);
        });
        Object.keys(groups).forEach(function (gid) {
          var was = byId[gid]; if (!was || !was.members) return;
          var kept = was.members.filter(function (m) { return groups[gid].members.indexOf(m) !== -1; });
          groups[gid].members = kept.concat(groups[gid].members.filter(function (m) { return kept.indexOf(m) === -1; }));
        });
        var cols = order.filter(function (id) { return groups[id]; }).map(function (id) { return groups[id]; })
          .concat(Object.keys(groups).filter(function (id) { return order.indexOf(id) === -1; }).map(function (id) { return groups[id]; }));
        var solo = byId.solo ? Object.assign({}, byId.solo, { members: (byId.solo.members || []).slice() }) : null;
        if (solo) {
          if (saved && saved.widths && saved.widths.solo) { solo.width = saved.widths.solo; solo._sized = true; }
          cols.splice(Math.max(0, Math.min(order.indexOf('solo'), cols.length)), 0, solo);
        }
        return cols;
      },

      // ── writes: the same records agents write with `graph group` ──
      _put(url, body, method) {
        return fetch(url, { method: method || 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
          .then(function (r) { if (!r.ok) return r.json().catch(function () { return {}; }).then(function (e) { throw new Error(e.error || ('HTTP ' + r.status)); }); return r.json(); });
      },
      ensureGroup(col) {
        if (!col || col.id === 'solo' || col._synced) return Promise.resolve();
        col._synced = true;
        return this._put('/api/groups', { slug: col.id, name: col.title, color: col.color || '' }, 'POST')
          .catch(function (e) { col._synced = false; console.warn('[board] group create failed', e.message); });
      },
      // Commit a session's current column to the shared record.
      commitMembership(id) {
        var self = this, col = this.columnOf(id);
        var body = (!col || col.id === 'solo') ? { group: null } : { group: col.id, joined_by: 'operator' };
        this._pending[id] = body.group;   // authoritative until the record agrees
        var ready = (col && col.id !== 'solo') ? this.ensureGroup(col) : Promise.resolve();
        return ready.then(function () { return self._put('/api/session/' + encodeURIComponent(id) + '/group', body); })
          .catch(function (e) {
            delete self._pending[id];      // the move did not happen; show the truth
            console.warn('[board] membership write failed', e.message);
            self.refresh();
          });
      },

      // ── rows: the same projection the Sessions page hands the card partial ──
      rowsFromStore() {
        var all = Alpine.store('sessions'), out = [];
        for (var id in all) {
          var s = all[id];
          if (!s || !s.isLive) continue;
          if (id.indexOf('chatwith-') === 0 || id.indexOf('chat-') === 0) continue;
          var role = s.role ? s.role.charAt(0).toUpperCase() + s.role.slice(1) : '';
          var storeType = s.sessionType || 'terminal';
          out.push({
            id: id, session_id: id, tmux_session: id, project: s.project || '',
            label: s.label || '', role: role, is_live: true,
            created_at: s.startedAt || 0, last_activity: s.lastActivity || 0, last_input_at: s.lastInputAt || 0,
            latest: s.lastMessage || '', type: storeType,
            session_type: storeType === 'host' ? 'host' : storeType === 'chatwith' ? 'chatwith' : (storeType === 'container' && s.beadId) ? 'dispatch' : 'interactive',
            bead_id: s.beadId || '', entry_count: s.entryCount || (s.entries ? s.entries.length : 0),
            context_tokens: s.contextTokens || 0, topics: s.topics || [],
            nag_enabled: !!s.nagEnabled, nag_interval: s.nagInterval || 15, nag_message: s.nagMessage || '',
            dispatch_nag_enabled: !!s.dispatchNagEnabled, activity_state: s.activityState || 'idle',
            org: s.org || null, resumable: false, harness: s.harness || null, model: s.model || null,
            startup_state: s.startupState || null, state: s.state || null, attention: s.attention || null,
            harness_state: s.harnessState || {}, resolved: s.resolved === true, phase_progress: s.phaseProgress || null,
          });
        }
        return out;
      },
      rowFor(id) {
        for (var i = 0; i < this.rows.length; i++) if (this.rows[i].id === id) return this.rows[i];
        return { id: id, session_id: id, label: id, tmux_session: id, is_live: true, topics: [], org: null, harness_state: {} };
      },
      labelFor(id) { var r = this.rowFor(id); return r.label || id; },
      // Show a session on the board rather than navigating to it. Returns
      // false when it has no card here, so the caller can fall back to the link.
      revealSession(name) {
        if (!name || !this.columnOf(name)) return false;
        this.cardPresentations[name] = 'transcript';
        this.persist();
        var self = this;
        this.$nextTick(function () {
          var board = self.$refs.board;
          var el = board.querySelector('.sb-card[data-session="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
          if (!el) return;
          // scrollIntoView walks up and scrolls ANY scrollable ancestor, which
          // dragged the page shell sideways under the nav rail and left the
          // first column clipped once the operator scrolled back. Move only the
          // board's own horizontal scroll, clamped to its real range.
          var r = el.getBoundingClientRect(), br = board.getBoundingClientRect();
          var want = board.scrollLeft + (r.left + r.width / 2) - (br.left + br.width / 2);
          var max = Math.max(0, board.scrollWidth - board.clientWidth);
          board.scrollTo({ left: Math.max(0, Math.min(want, max)), behavior: 'smooth' });
          self.bindDictation({ target: el }, name);
        });
        return true;
      },
      orgColorFor(id) { return orgColor(this.rowFor(id)); },
      panelConfig(id) { var r = this.rowFor(id); return { sessionId: id, project: r.project || 'default', tmuxSession: id, _isLive: true }; },
      columnOf(id) { return this.columns.filter(function (c) { return c.members.indexOf(id) !== -1; })[0] || null; },

      // ── columns ──
      normalise(cols) {
        var keep = {};
        if (this._dragSource) keep[this._dragSource] = true;
        if (this._provisional) keep[this._provisional] = true;
        return normaliseColumns(cols, this.rows.map(function (r) { return r.id; }), keep);
      },
      canStartGroup(id) {
        var col = this.columnOf(id);
        return !col || col.id === 'solo' || col.members.length > 1;
      },
      renameColumn(col, title) {
        title = (title || '').trim();
        if (!title || col.id === 'solo') return;
        col.title = title;
        var self = this;
        this.ensureGroup(col).then(function () { return self._put('/api/groups/' + encodeURIComponent(col.id), { name: title }); })
          .catch(function (e) { console.warn('[board] rename failed', e.message); });
      },
      // ⤢ Full height: the column becomes this one session, in place. Other
      // members move to a new column on its right that keeps the group's title.
      focusCard(id) {
        var col = this.columnOf(id); if (!col) return;
        if (col.focus === id) { col.focus = null; this._focusSession = ''; this.persist(); return; }
        var self = this, idx = this.columns.indexOf(col), target = col;
        var others = col.members.filter(function (m) { return m !== id; });
        if (col.id === 'solo') {
          // An ungrouped session becomes a one-session group titled by its label, in Ungrouped's slot.
          target = { id: 'g-' + Date.now().toString(36), title: this.labelFor(id), color: orgColor(this.rowFor(id)), width: Math.max(col.width, 640), _sized: true, members: [] };
          this.columns.splice(idx, 0, target);
          this.placeCard(id, target.id, 0);
          target = this.columns.filter(function (c) { return c.id === target.id; })[0];
          this.commitMembership(id);
        } else if (others.length) {
          // The focused session keeps the group; the others move to a new group to its right that keeps the title.
          var spill = { id: 'g-' + Date.now().toString(36), title: col.title, color: col.color, width: col.width, _sized: col._sized, members: [] };
          this.columns.splice(idx + 1, 0, spill);
          others.forEach(function (m) { self.placeCard(m, spill.id, 1e9); });
          target = this.columns.filter(function (c) { return c.id === col.id; })[0] || target;
          target.width = Math.max(target.width, 640); target._sized = true;
          this.ensureGroup(spill).then(function () { others.forEach(function (m) { self.commitMembership(m); }); });
        } else {
          target.width = Math.max(target.width, 640); target._sized = true;
        }
        if (!target) return;
        this.columns.forEach(function (c) { if (c !== target) c.focus = null; });
        target.focus = id;
        this._focusSession = id;
        this.cardPresentations[id] = 'transcript';
        this.persist();
      },
      // Remove from wherever it is, insert at `idx` among the target's members.
      placeCard(id, colId, idx) {
        this.columns.forEach(function (c) { if (c.focus === id && c.id !== colId) c.focus = null; });
        this.columns.forEach(function (c) { var i = c.members.indexOf(id); if (i !== -1) c.members.splice(i, 1); });
        var to = this.columns.filter(function (c) { return c.id === colId; })[0];
        if (!to && colId === 'solo') {
          // Ungrouped is absent while every session is grouped; leaving a group recreates it.
          to = { id: 'solo', title: 'Ungrouped', color: '#334155', width: DEFAULT_COL, members: [] };
          this.columns.unshift(to);
        }
        if (!to) return;
        to.members.splice(Math.max(0, Math.min(idx, to.members.length)), 0, id);
        this.columns = this.normalise(this.columns);
      },
      moveCard(id, colId, idx) {
        var cur = this.columnOf(id);
        if (cur && cur.id === colId && cur.members.indexOf(id) < idx) idx--;
        this.placeCard(id, colId, idx);
        if (!this.dragging) this.commitMembership(id);
      },

      // ── Auto-organize: one click dispatches a librarian ──
      // The digest is the run's custom_input: every live session's identity,
      // title, role, topics, current group, and the last 10 turns — the same
      // tail the cards render. It lands verbatim in the librarian's first
      // turn through the asset-less agent-actions dispatch
      // (member_key session.auto-organize). The librarian writes groups with
      // `graph group`; they come back to the board through the registry
      // broadcast like any other agent write. The board never invents groups.
      organizeDigest() {
        var all = Alpine.store('sessions'), self = this;
        return this.rows.map(function (r) {
          var store = all[r.id];
          var tail = (store && store.entries ? store.entries : []).filter(function (e) { return !e.internal && (e.content || '').trim(); }).slice(-10)
            .map(function (e) { return { type: e.type, role: e.role, sender: e.sender || undefined, text: String(e.content || '').slice(0, 400) }; });
          return { session: r.id, org: r.org && r.org.slug, harness: r.harness, model: r.model, role: r.role,
                   title: r.label, topics: r.topics, group: (store && store.groupId) || undefined, tail: tail };
        });
      },
      autoOrganize() {
        if (this.organize.state === 'running') return;
        var self = this, digest = this.organizeDigest();
        if (!digest.length) { this.organize = { state: 'idle', status: 'no live sessions', runId: '' }; return; }
        this.organize = { state: 'running', status: 'librarian reading ' + digest.length + ' session' + (digest.length === 1 ? '' : 's') + '…', runId: '' };
        fetch('/api/agent-actions/dispatch', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ member_key: 'session.auto-organize', custom_input: JSON.stringify(digest) }) })
          .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status)); return d; }); })
          .then(function (d) {
            self.organize = { state: 'running', status: 'run ' + (d.run_id || '?') + ' — groups land as the librarian writes them', runId: d.run_id || '' };
            self._watchOrganizeRun(d.run_id || '', Date.now());
          })
          .catch(function (e) { self.organize = { state: 'idle', status: 'dispatch failed: ' + e.message, runId: '' }; });
      },
      _watchOrganizeRun(runId, startedAt) {
        var self = this;
        if (!runId) return;
        var tick = function () {
          if (self.organize.runId !== runId) return;
          fetch('/api/dispatch/runs?limit=50', { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : []; }).then(function (rows) {
            var row = (rows || []).filter(function (x) { return String(x.dir || '').indexOf(runId) !== -1 || x.run_id === runId; })[0];
            var status = row && row.status;
            if (status === 'DONE' || status === 'FAILED') {
              var groups = self.columns.filter(function (c) { return c.id !== 'solo'; }).length;
              self.organize = { state: status === 'DONE' ? 'done' : 'idle', status: status === 'DONE' ? (self.rows.length + ' sessions → ' + groups + ' groups') : 'librarian run failed', runId: '' };
              if (status === 'DONE') setTimeout(function () { if (self.organize.state === 'done') self.organize = { state: 'idle', status: '', runId: '' }; }, 8000);
              return;
            }
            if (Date.now() - startedAt > 15 * 60 * 1000) { self.organize = { state: 'idle', status: 'librarian run still going (run ' + runId + ')', runId: '' }; return; }
            setTimeout(tick, 5000);
          }).catch(function () { setTimeout(tick, 8000); });
        };
        setTimeout(tick, 5000);
      },

      // ── column move (desktop pointer; the column header row is the handle) ──
      // Press on a column header (not its title field), move 6 px, and the
      // column follows the pointer left or right while the others slide out
      // of the way; order is decided from a frame snapshotted at drag start.
      onColHeadPointerDown(ev, col) {
        if (ev.button !== 0) return;
        if (ev.target.closest('button, a, input, select, textarea, [contenteditable="true"]')) return;
        var self = this, sx = ev.clientX, sy = ev.clientY, started = false, ghost = null, raf = 0, last = null, frame = null;
        var onMove = function (e) {
          last = e;
          if (!started) {
            if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) < 6) return;
            started = true;
            frame = self.snapshotColumnFrame(col.id);
            self.movingCol = col.id;
            ghost = self.makeColumnGhost(col);
            document.body.classList.add('sb-moving');
          }
          if (!raf) raf = requestAnimationFrame(function () {
            raf = 0; if (!last) return;
            ghost.style.transform = 'translate(' + (last.clientX + 14) + 'px,' + (last.clientY - 12) + 'px)';
            var idx = columnSlotFor(frame.mids, last.clientX + (self.$refs.board.scrollLeft - frame.scrollLeft0));
            var cur = self.columns.indexOf(col);
            if (cur === -1 || cur === idx) return;
            var cols = self.columns.slice(); cols.splice(cur, 1); cols.splice(idx, 0, col);
            self.columns = cols;
          });
        };
        var onUp = function () {
          window.removeEventListener('pointermove', onMove, true);
          window.removeEventListener('pointerup', onUp, true);
          window.removeEventListener('pointercancel', onUp, true);
          window.removeEventListener('blur', onUp);
          if (raf) cancelAnimationFrame(raf);
          if (!started) return;
          if (ghost) ghost.remove();
          document.body.classList.remove('sb-moving');
          self.movingCol = '';
          self.persist();
        };
        window.addEventListener('pointermove', onMove, true);
        window.addEventListener('pointerup', onUp, true);
        window.addEventListener('pointercancel', onUp, true);
        window.addEventListener('blur', onUp);
      },
      snapshotColumnFrame(colId) {
        var board = this.$refs.board, cols = board.querySelectorAll('.sb-col'), mids = [], shift = 0;
        for (var i = 0; i < cols.length; i++) {
          var r = cols[i].getBoundingClientRect();
          if (cols[i].dataset.col === colId) { shift = r.width + 14; continue; }   // column + board gap
          mids.push((r.left + r.right) / 2 - shift);
        }
        return { mids: mids, scrollLeft0: board.scrollLeft };
      },
      makeColumnGhost(col) {
        var g = document.createElement('div');
        g.className = 'sb-ghost';
        var t = document.createElement('div'); t.className = 't'; t.textContent = col.title; g.appendChild(t);
        var d = document.createElement('div'); d.className = 'topic'; d.textContent = col.members.length + ' session' + (col.members.length === 1 ? '' : 's'); g.appendChild(d);
        document.body.appendChild(g);
        return g;
      },

      // ── move (desktop pointer; the card header is the handle) ──
      onHeadPointerDown(ev, id) {
        if (ev.button !== 0) return;
        if (ev.target.closest('button, a, input, select, textarea, [contenteditable="true"]')) return;
        var self = this, sx = ev.clientX, sy = ev.clientY, started = false, ghost = null, raf = 0, last = null;
        var onMove = function (e) {
          last = e;
          if (!started) {
            if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) < 6) return;
            started = true;
            self._frame = self.snapshotFrame(id);
            self.dragId = id; self.dragging = true; self._dragSource = (self.columnOf(id) || {}).id || null;
            ghost = self.makeGhost(id);
            document.body.classList.add('sb-moving');
          }
          if (!raf) raf = requestAnimationFrame(function () {
            raf = 0; if (!last) return;
            ghost.style.transform = 'translate(' + (last.clientX + 14) + 'px,' + (last.clientY - 12) + 'px)';
            self.previewAt(id, last.clientX, last.clientY);
          });
        };
        var onUp = function () {
          window.removeEventListener('pointermove', onMove, true);
          window.removeEventListener('pointerup', onUp, true);
          window.removeEventListener('pointercancel', onUp, true);
          window.removeEventListener('blur', onUp);
          if (raf) cancelAnimationFrame(raf);
          if (!started) return;
          if (ghost) ghost.remove();
          document.body.classList.remove('sb-moving');
          self.endMove(id);
        };
        window.addEventListener('pointermove', onMove, true);
        window.addEventListener('pointerup', onUp, true);
        window.addEventListener('pointercancel', onUp, true);
        window.addEventListener('blur', onUp);
      },
      makeGhost(id) {
        var r = this.rowFor(id), g = document.createElement('div');
        g.className = 'sb-ghost';
        var t = document.createElement('div'); t.className = 't'; t.textContent = r.label || id; g.appendChild(t);
        (r.topics || []).slice(0, 2).forEach(function (tp) { var d = document.createElement('div'); d.className = 'topic'; d.textContent = tp; g.appendChild(d); });
        document.body.appendChild(g);
        return g;
      },
      // Reference frame, measured ONCE at drag start from the pre-drag layout.
      snapshotFrame(id) {
        var frame = { cols: [], zone: null };
        var root = this.$refs.board;
        var cols = root.querySelectorAll('.sb-col');
        for (var c = 0; c < cols.length; c++) {
          var colEl = cols[c], r = colEl.getBoundingClientRect();
          var body = colEl.querySelector('.sb-col-body');
          var entry = { id: colEl.dataset.col, left: r.left, right: r.right, body: body, scrollTop0: body ? body.scrollTop : 0, scrollDelta: 0, mids: [] };
          var cards = colEl.querySelectorAll('.sb-col-body .sb-card'), shift = 0;
          for (var i = 0; i < cards.length; i++) {
            if (cards[i].dataset.session === id) {
              var wrap = cards[i].parentElement;
              shift = (wrap ? wrap.getBoundingClientRect().height : cards[i].getBoundingClientRect().height) + STACK_GAP;
              continue;
            }
            var cr = cards[i].getBoundingClientRect();
            entry.mids.push((cr.top + cr.bottom) / 2 - shift);
          }
          frame.cols.push(entry);
        }
        var zone = root.querySelector('.sb-col-new');
        if (zone && zone.offsetParent !== null) { var zr = zone.getBoundingClientRect(); frame.zone = { left: zr.left, right: zr.right }; }
        return frame;
      },
      previewAt(id, x, y) {
        var frame = this._frame; if (!frame) return;
        frame.cols.forEach(function (c) { c.scrollDelta = (c.body ? c.body.scrollTop : 0) - c.scrollTop0; });
        var slot = slotFor(frame, x, y); if (!slot) return;
        if (slot.zone) {
          if (!this.canStartGroup(id)) return;
          if (!this._provisional) {
            var col = { id: 'g-' + Date.now().toString(36), title: 'New group', color: orgColor(this.rowFor(id)), width: DEFAULT_COL, members: [] };
            this.columns.push(col); this._provisional = col.id;
            this.ensureGroup(col);
          }
          if ((this.columnOf(id) || {}).id !== this._provisional) this.placeCard(id, this._provisional, 0);
          return;
        }
        var target = this.columns.filter(function (k) { return k.id === slot.colId; })[0];
        if (!target || target.focus) return;
        var cur = this.columnOf(id);
        if (cur && cur.id === slot.colId && cur.members.indexOf(id) === slot.idx) return;
        this.placeCard(id, slot.colId, slot.idx);
      },
      endMove(id) {
        this.dragId = ''; this.dragging = false;
        var prov = this._provisional; this._provisional = null; this._dragSource = null; this._frame = null;
        this.columns = this.normalise(this.columns);
        var self = this;
        this.commitMembership(id).then(function () { if (self._refreshPending) self.refresh(); });
        if (prov && this.columns.some(function (c) { return c.id === prov; })) {
          setTimeout(function () {
            var input = document.querySelector('.sb-col[data-col="' + prov + '"] .sb-col-title');
            if (input) { input.focus(); input.select(); }
          }, 60);
        }
      },

      // ── resize (desktop pointer; live and direct) ──
      _beginDrag(ev, spec) {
        if (ev.button !== 0) return;
        ev.preventDefault();
        var self = this, raf = 0, last = null, body = document.body, handle = spec.handle;
        body.classList.add('sb-resizing', spec.axis === 'x' ? 'sb-resizing-col' : 'sb-resizing-row');
        handle.classList.add('active');
        try { handle.setPointerCapture(ev.pointerId); } catch (e) {}
        var apply = function () { raf = 0; if (last) spec.apply(last); };
        var onMove = function (e) { last = e; if (!raf) raf = requestAnimationFrame(apply); };
        var onEnd = function (e) {
          if (raf) cancelAnimationFrame(raf);
          if (e && e.type === 'pointermove') return;
          if (last) spec.apply(last);
          window.removeEventListener('pointermove', onMove, true);
          window.removeEventListener('pointerup', onEnd, true);
          window.removeEventListener('pointercancel', onEnd, true);
          window.removeEventListener('blur', onEnd);
          handle.removeEventListener('lostpointercapture', onEnd);
          try { handle.releasePointerCapture(ev.pointerId); } catch (e2) {}
          body.classList.remove('sb-resizing', 'sb-resizing-col', 'sb-resizing-row');
          handle.classList.remove('active');
          spec.commit();
          self._drag = null;
        };
        window.addEventListener('pointermove', onMove, true);
        window.addEventListener('pointerup', onEnd, true);
        window.addEventListener('pointercancel', onEnd, true);
        window.addEventListener('blur', onEnd);
        handle.addEventListener('lostpointercapture', onEnd);
        this._drag = { end: onEnd };
      },
      startColResize(ev, col) {
        var startX = ev.clientX, startW = col.width, el = ev.currentTarget.closest('.sb-col'), width = startW;
        this._beginDrag(ev, {
          axis: 'x', handle: ev.currentTarget,
          apply: function (e) { width = Math.max(MIN_COL, startW + (e.clientX - startX)); el.style.width = width + 'px'; },
          commit: function () { col.width = width; col._sized = true; },
        });
      },
      startCardResize(ev, id) {
        var startY = ev.clientY, startH = this.cardHeight(id), self = this, el = ev.currentTarget.closest('.sb-card'), height = startH;
        this._beginDrag(ev, {
          axis: 'y', handle: ev.currentTarget,
          apply: function (e) { height = self.clampHeight(startH + (e.clientY - startY)); el.style.height = height + 'px'; },
          commit: function () { self.cardHeights[id] = height; self.persist(); },
        });
      },
      maxCardHeight() { return Math.max(MIN_CARD, Math.floor(window.innerHeight * 0.8)); },
      clampHeight(h) { return Math.max(MIN_CARD, Math.min(this.maxCardHeight(), h || DEFAULT_CARD)); },
      cardHeight(id) { return this.clampHeight(this.cardHeights[id] || DEFAULT_CARD); },
      cardStyle(id) {
        var t = this.viewportTick; // reactive dependency: re-clamp on viewport resize
        var h = this.cardPresentation(id) === 'transcript' ? 'height:' + Math.round(this.cardHeight(id) * (this.dragId === id ? 0.75 : 1)) + 'px;' : '';
        return h + '--org:' + this.orgColorFor(id) + (t < 0 ? '' : '');
      },

      // ── persistence: the operator's layout member (dashboard.session.board.layout) ──
      loadLayout() {
        return fetch(LAYOUT_URL, { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (d) { return (d && d.layout) || {}; })
          .catch(function () { return {}; });
      },
      reloadLayout() {
        var self = this;
        if (this.dragging || this._drag) return;
        this.loadLayout().then(function (layout) {
          if (!layout || !self._ready) return;
          if (layout.presentation === 'stats' || layout.presentation === 'transcript') self.presentation = layout.presentation;
          self.cardHeights = layout.heights || {};
          var order = layout.column_order || [];
          var byId = {}; self.columns.forEach(function (c) { byId[c.id] = c; });
          var cols = order.filter(function (id) { return byId[id]; }).map(function (id) { return byId[id]; })
            .concat(self.columns.filter(function (c) { return order.indexOf(c.id) === -1; }));
          cols.forEach(function (c) { if (layout.widths && layout.widths[c.id]) { c.width = layout.widths[c.id]; c._sized = true; } });
          self._suppressPersist = true;
          self.columns = self.normalise(cols);
          self._suppressPersist = false;
        });
      },
      layoutPayload() {
        var widths = {}, focus = '';
        this.columns.forEach(function (c) { if (c._sized) widths[c.id] = c.width; if (c.focus) focus = c.focus; });
        return { presentation: this.presentation, presentations: this.cardPresentations,
                 column_order: this.columns.map(function (c) { return c.id; }), widths: widths,
                 heights: this.cardHeights, focus_session: focus };
      },
      persist() {
        if (!this._ready || this._suppressPersist) return;   // never write a pre-roster or echoed layout
        var self = this, payload = JSON.stringify(this.layoutPayload());
        if (payload === this._lastPersisted) return;
        clearTimeout(this._persistTimer);
        this._persistTimer = setTimeout(function () {
          self._lastPersisted = payload;
          fetch(LAYOUT_URL, { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: payload })
            .catch(function (e) { console.warn('[board] layout write failed', e.message); });
        }, 250);
      },

      // ── presentation ──
      setPresentation(p) { this.presentation = p; this.cardPresentations = {}; this.persist(); },
      cardPresentation(id) {
        if (this.cardPresentations[id]) return this.cardPresentations[id];
        var store = Alpine.store('sessions')[id];
        var r = this.rowFor(id);
        if (r.session_type === 'host' && (!store || !store.entries || !store.entries.length)) return 'stats';
        return this.presentation;
      },
      togglePresentation(id) {
        this.cardPresentations[id] = this.cardPresentation(id) === 'transcript' ? 'stats' : 'transcript';
        this.persist();
      },

      // ── dictation: click binds through the standard voice path, never navigates ──
      // Click a card to talk to it. On the board the cards ARE the targets and
      // they are all on screen, so the click is itself the confirmation: a
      // switch takes effect immediately (the voice store preserves the buffer,
      // so in-flight dictation cuts over rather than being stranded). The
      // full-page viewer keeps its confirm prompt, where the target you would
      // be switching away from is not visible.
      bindDictation(ev, id) {
        this.menuFor = '';
        var voice = Alpine.store('voice');
        var ui = window.Autonomy && window.Autonomy.voice && window.Autonomy.voice.ui;
        if (!ui || typeof ui.onClick !== 'function' || !voice) return;
        var row = this.rowFor(id);
        var key = ui.voiceBindKey(row);
        var already = voice.boundSessionId;
        if (already && already !== key && row.is_live && typeof voice.bindSession === 'function') {
          voice.bindSession(key);          // explicit switch, no second prompt
          voice.pendingRebindTarget = '';
        } else {
          ui.onClick(ev, key, { isLive: !!row.is_live });
        }
        this.boundId = voice.boundSessionId || '';
      },
      isBound(id) { var voice = Alpine.store('voice'); return !!(voice && voice.boundSessionId === id); },

      // ── column aggregates (same feed as the Sessions page) ──
      async _hydrateResources() {
        try {
          var res = await fetch('/api/resources?history=1');
          if (res.ok) this._applyResourceRows((await res.json()).sessions || {});
        } catch (e) { /* SSE pushes will populate from here */ }
      },
      _applyResourceRows(rows) {
        var next = {};
        for (var key in rows) {
          var r = rows[key], prev = this.resources[key], hist;
          if (r.history && r.history.length) {
            hist = r.history.slice();
            if (prev && prev.history && prev.history.length) {
              var baseTs = hist[hist.length - 1][0];
              for (var i = 0; i < prev.history.length; i++) if (prev.history[i][0] > baseTs) hist.push(prev.history[i]);
            }
          } else {
            hist = (prev && prev.history) || [];
            if (r.sampled_at && r.cpu_pct != null) {
              var lastTs = hist.length ? hist[hist.length - 1][0] : 0;
              if (r.sampled_at > lastTs) hist = hist.concat([[r.sampled_at, r.cpu_pct, r.mem_bytes]]);
            }
          }
          if (hist.length > SPARK_SAMPLES) hist = hist.slice(hist.length - SPARK_SAMPLES);
          next[key] = Object.assign({}, r, { history: hist });
        }
        this.resources = next;
      },
      // ── commits: what this session landed, on its own card ──
      async _hydrateWorktrees() {
        try {
          var res = await fetch('/api/worktrees', { credentials: 'same-origin' });
          if (!res.ok) return;
          var data = await res.json();
          var rows = Array.isArray(data) ? data : (data.worktrees || data.rows || []);
          var commits = {}, workspaces = {};
          rows.forEach(function (r) {
            var name = r.session_name; if (!name) return;
            workspaces[name] = { hasChanges: !!(r.is_dirty || r.dirty_count > 0 || r.commits_ahead > 0),
                                 dirty: r.dirty_count || 0, ahead: r.commits_ahead || 0 };
            var list = r.commits || []; if (!list.length) return;
            var newest = null, newestAt = 0;
            list.forEach(function (c) {
              var at = _commitTime(c.date);
              if (at && at > newestAt) { newestAt = at; newest = c; }
            });
            if (!newest) return;
            var prev = commits[name];
            if (prev && prev.at >= newestAt) return;
            var stats = newest.stats || {};
            commits[name] = {
              at: newestAt, sha: newest.short_sha || (newest.sha || '').slice(0, 9),
              subject: newest.subject || '', branch: r.branch || '',
              files: (newest.files || []).length || stats.files || 0,
              additions: stats.additions || (newest.files || []).reduce(function (n, f) { return n + (f.additions || 0); }, 0),
              deletions: stats.deletions || (newest.files || []).reduce(function (n, f) { return n + (f.deletions || 0); }, 0),
            };
          });
          this.commits = commits;
          this.workspaces = workspaces;
        } catch (e) { /* the strip is decoration; never break the board over it */ }
      },
      // The card's most recent commit, only while it is fresh (one hour).
      commitFor(id) {
        var t = this.commitTick;   // reactive: re-evaluates as the hour ages out
        var c = this.commits[id];
        if (!c) return null;
        var age = Date.now() - c.at;
        if (age < 0 || age > 3600000) return null;
        return Object.assign({}, c, { ago: _ago(age) });
      },
      res(id) { return this.resources[id] || null; },
      colCpu(col) { var t = 0, n = 0, self = this; col.members.forEach(function (m) { var r = self.res(m); if (r && r.cpu_pct != null) { t += r.cpu_pct; n++; } }); return n ? t.toFixed(t >= 10 ? 0 : 1) + '%' : '—'; },
      colMem(col) { var t = 0, n = 0, self = this; col.members.forEach(function (m) { var r = self.res(m); if (r && r.mem_bytes) { t += r.mem_bytes; n++; } }); return n ? fmtBytes(t) : '—'; },
      colDisk(col) { var t = 0, n = 0, self = this; col.members.forEach(function (m) { var r = self.res(m); if (r && r.disk && r.disk.total) { t += r.disk.total; n++; } }); return n ? fmtBytes(t) : '—'; },
      // A column's summed CPU over time, as points.
      _colSeries(col) {
        var self = this, series = [];
        col.members.forEach(function (m) { var r = self.res(m); if (r && r.history && r.history.length) series.push(r.history); });
        if (!series.length) return [];
        var len = Math.max.apply(null, series.map(function (h) { return h.length; }));
        var pts = [];
        for (var i = 0; i < len; i++) {
          var sum = 0;
          series.forEach(function (h) { var p = h[h.length - len + i]; if (p) sum += (p[1] || 0); });
          pts.push(sum);
        }
        return pts;
      },
      // ONE scale for every sparkline on the board. Scaling each column to its
      // own peak made a lane idling at 1% look exactly as busy as a lane at
      // 437%; the shapes are only comparable against a shared ceiling.
      fleetCpuMax() {
        var self = this, max = 0;
        this.columns.forEach(function (c) {
          self._colSeries(c).forEach(function (v) { if (v > max) max = v; });
        });
        return max;
      },
      colSpark(col) {
        var pts = this._colSeries(col);
        if (!pts.length) return '0,19 90,19';
        var max = this.fleetCpuMax();
        if (!(max > 0)) return '0,19 90,19';
        var len = pts.length;
        return pts.map(function (v, i) {
          return (i * (90 / Math.max(1, len - 1))).toFixed(1) + ',' + (19 - Math.min(1, v / max) * 17).toFixed(1);
        }).join(' ');
      },
      // The shared ceiling, named on the busiest column so the scale is legible.
      colSparkTitle(col) {
        var max = this.fleetCpuMax();
        if (!(max > 0)) return 'cpu';
        var mine = this._colSeries(col);
        var peak = mine.length ? Math.max.apply(null, mine) : 0;
        return 'peak ' + peak.toFixed(peak >= 10 ? 0 : 1) + '% of ' + max.toFixed(max >= 10 ? 0 : 1) + '% fleet max';
      },
      attnClass(id) {
        var store = Alpine.store('sessions')[id]; var a = (store && (store.attention || store.activityState)) || 'idle';
        return a === 'tool_running' ? 'working' : a === 'thinking' ? 'thinking' : '';
      },
      attnTitle(col) { var self = this, w = 0, t = 0; col.members.forEach(function (m) { var c = self.attnClass(m); if (c === 'working') w++; else if (c === 'thinking') t++; }); return w + ' working · ' + t + ' thinking · ' + (col.members.length - w - t) + ' idle'; },

      // ── session actions: the org glyph's menu, same actions as the Sessions page ──
      // The org glyph's menu. This surface is desktop-only, so it opens as an
      // inline popover anchored to the card — never the global action sheet,
      // which is the phone's slide-up tray and dims the whole screen.
      sessionActions(s) {
        var tmux = s.session_id || s.id, actions = [];
        var put = function (url, opts) { return fetch(url, opts || { method: 'POST' }); };
        if (s.is_live) {
          actions.push({ icon: s.nag_enabled ? '🔕' : '🔔', label: s.nag_enabled ? 'Disable nag' : 'Enable nag (15m)', handler: function () {
            put('/api/session/' + encodeURIComponent(tmux) + '/nag', {
              method: s.nag_enabled ? 'DELETE' : 'PUT', headers: { 'Content-Type': 'application/json' },
              body: s.nag_enabled ? undefined : JSON.stringify({ enabled: true, interval: 15 }),
            });
          } });
          actions.push({ icon: '⤢', label: 'Open full viewer', handler: function () {
            var url = '/session/' + encodeURIComponent(s.project || 'default') + '/' + encodeURIComponent(tmux);
            if (window.navigateTo) window.navigateTo(url); else window.location.href = url;
          } });
          actions.push({ icon: '⧉', label: 'Copy session name', handler: function () { try { navigator.clipboard.writeText(tmux); } catch (e) {} } });
          actions.push({ icon: '↻', label: 'Restart session', handler: function () { put('/api/session/' + encodeURIComponent(tmux) + '/restart'); } });
          actions.push({ icon: '✕', label: 'Close session', style: 'destructive', handler: function () { put('/api/terminal/' + encodeURIComponent(tmux) + '/kill'); } });
        }
        return actions;
      },
      // Switch the model a live session is running, by typing the harness's
      // own command into it. The list comes from the dispatcher's alias table
      // (/api/session-models), so the board offers exactly the names a bead's
      // `model:` label accepts. A harness whose switch command is not known
      // answers with an empty list and gets no menu — the board never types an
      // unverified command into a live agent.
      async openModelMenu(id) {
        this.menuFor = '';
        if (this.modelMenuFor === id) { this.modelMenuFor = ''; return; }
        var row = this.rowFor(id), harness = row.harness || '';
        if (!row.is_live || !harness) return;
        var cached = this._modelCache[harness];
        if (!cached) {
          try {
            var res = await fetch('/api/session-models?harness=' + encodeURIComponent(harness), { credentials: 'same-origin' });
            cached = res.ok ? await res.json() : { models: [], command: null };
          } catch (e) { cached = { models: [], command: null }; }
          this._modelCache[harness] = cached;
        }
        if (!cached.command || !cached.models.length) return;
        this.modelOptions = cached.models.map(function (m) {
          return { alias: m.alias, model: m.model, current: (row.model || '') === m.model };
        });
        this.modelMenuFor = id;
      },
      async chooseModel(id, alias) {
        var row = this.rowFor(id);
        var harness = row.harness || '';
        var cmd = (this._modelCache[harness] || {}).command;
        if (!cmd) return;
        this.modelMenuFor = '';
        this.modelBusy = id;
        try {
          var res = await fetch('/api/session/send', {
            method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tmux_session: id, message: cmd + ' ' + alias }),
          });
          if (!res.ok) {
            var err = await res.json().catch(function () { return {}; });
            console.warn('[board] model switch failed', err.error || res.status);
          }
        } catch (e) { console.warn('[board] model switch failed', e.message); }
        this.modelBusy = '';
      },

      showSessionActions(s) {
        var tmux = s.session_id || s.id;
        if (this.menuFor === tmux) { this.menuFor = ''; return; }
        this.menuActions = this.sessionActions(s);
        this.menuFor = tmux;
      },
      runMenuAction(a) {
        this.menuFor = '';
        if (a && typeof a.handler === 'function') a.handler();
      },

      // ── contract the production card partial expects from its host page ──
      borderCls: helper('borderCls'), typeBadge: helper('typeBadge'), typeCls: helper('typeCls'),
      turnsStr: helper('turnsStr'), ctxStr: helper('ctxStr'), idleStr: helper('idleStr'), ctxWarn: helper('ctxWarn'),
      recencyColor: helper('recencyColor'), endedOrIdleLabel: helper('endedOrIdleLabel'), endedOrIdleValue: helper('endedOrIdleValue'),
      showTmuxColumn: helper('showTmuxColumn'),
      resourceFor(s) { return this.res((s && (s.tmux_session || s.tmux_name || s.id)) || ''); },
      resourceTipKey(s) { return (s && (s.tmux_session || s.tmux_name || s.id)) || ''; },
      toggleResourceTip(s) { var k = this.resourceTipKey(s); this._resourceTipOpen = this._resourceTipOpen === k ? null : k; },
      fmtBytes: fmtBytes,
      cpuStr(s) { var r = this.resourceFor(s); return (r && r.cpu_pct != null) ? r.cpu_pct.toFixed(r.cpu_pct >= 10 ? 0 : 1) + '%' : '—'; },
      ramStr(s) { var r = this.resourceFor(s); return (r && r.mem_bytes != null) ? fmtBytes(r.mem_bytes) : '—'; },
      diskStr(s) { var r = this.resourceFor(s); return (r && r.disk && r.disk.total != null) ? fmtBytes(r.disk.total) : '—'; },
      diskDetail(s) { var r = this.resourceFor(s); return (r && r.disk && r.disk.components) ? Object.keys(r.disk.components).map(function (k) { return [k, fmtBytes(r.disk.components[k])]; }) : []; },
      sparkSvg() { return ''; },
      hasWorkspaceChanges(s) {
        var w = this.workspaces[(s && (s.tmux_session || s.id)) || ''];
        return !!(w && w.hasChanges);
      },
      workspaceStatusTooltip(s) {
        var w = this.workspaces[(s && (s.tmux_session || s.id)) || ''];
        if (!w || !w.hasChanges) return '';
        var bits = [];
        if (w.ahead) bits.push(w.ahead + ' commit' + (w.ahead === 1 ? '' : 's') + ' to review');
        if (w.dirty) bits.push(w.dirty + ' uncommitted file' + (w.dirty === 1 ? '' : 's'));
        return bits.join(' · ');
      },
      whenLine() { return ''; },
      refreshDisk() {},
      resumeSession() {},
    }; });
  });
})();
