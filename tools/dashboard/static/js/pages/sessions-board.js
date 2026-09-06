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
 * (graph://e61d916d-34b). This file ports that design's behaviour; the two
 * deliberate differences are that a card click binds dictation through the
 * standard voice path (window.Autonomy.voice.ui.onClick) and that the org
 * glyph's actions go through the shared window.actionSheet.
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
      cardPresentations: {}, cardHeights: {}, resources: {},
      boundId: '', dragId: '', dragging: false, movingCol: '', viewportTick: 0,
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
        this._resourceHandler = function (d) { if (d && typeof d === 'object') self._applyResourceRows(d.sessions || d); };
        if (window.registerHandler) window.registerHandler('resources', this._resourceHandler);
        this._hydrateResources();
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
        if (this._offGroupChange) this._offGroupChange();
        if (this._offLayoutChange) this._offLayoutChange();
        window.removeEventListener('sessions:store-changed', this._onStoreChanged);
        window.removeEventListener('sessions:registry-changed', this._onStoreChanged);
        window.removeEventListener('resize', this._onResize);
        if (window.unregisterHandler && this._resourceHandler) window.unregisterHandler('resources', this._resourceHandler);
      },
      refresh(saved) {
        if (!this._ready) return;
        if (this.dragging) { this._refreshPending = true; return; }
        this._refreshPending = false;
        this.rows = this.rowsFromStore();
        this.columns = this.normalise(this.columnsFromStore(saved));
        var voice = Alpine.store('voice');
        if (voice) this.boundId = voice.boundSessionId || '';
      },
      // Columns = the store's group membership. Previous columns (or the saved
      // layout on first paint) contribute only order, width, focus and the
      // arranged order of members inside a column; membership itself is server truth.
      columnsFromStore(saved) {
        var all = Alpine.store('sessions'), self = this;
        var prev = this.columns.length ? this.columns : ((saved && saved.columns) ? saved.columns : []);
        var order = prev.map(function (c) { return c.id; });
        var byId = {}; prev.forEach(function (c) { byId[c.id] = c; });
        var groups = {};
        this.rows.forEach(function (r) {
          var st = all[r.id]; var gid = st && st.groupId; if (!gid) return;
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
        var ready = (col && col.id !== 'solo') ? this.ensureGroup(col) : Promise.resolve();
        return ready.then(function () { return self._put('/api/session/' + encodeURIComponent(id) + '/group', body); })
          .catch(function (e) { console.warn('[board] membership write failed', e.message); self.refresh(); });
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
        if (col.focus === id) { col.focus = null; return; }
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
        this.cardPresentations[id] = 'transcript';
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
        var widths = {}; this.columns.forEach(function (c) { if (c._sized) widths[c.id] = c.width; });
        return { presentation: this.presentation, column_order: this.columns.map(function (c) { return c.id; }), widths: widths, heights: this.cardHeights };
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
      togglePresentation(id) { this.cardPresentations[id] = this.cardPresentation(id) === 'transcript' ? 'stats' : 'transcript'; },

      // ── dictation: click binds through the standard voice path, never navigates ──
      bindDictation(ev, id) {
        var ui = window.Autonomy && window.Autonomy.voice && window.Autonomy.voice.ui;
        if (!ui || typeof ui.onClick !== 'function') return;
        var row = this.rowFor(id);
        ui.onClick(ev, ui.voiceBindKey(row), { isLive: !!row.is_live });
        var voice = Alpine.store('voice');
        this.boundId = voice ? (voice.boundSessionId || '') : '';
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
      res(id) { return this.resources[id] || null; },
      colCpu(col) { var t = 0, n = 0, self = this; col.members.forEach(function (m) { var r = self.res(m); if (r && r.cpu_pct != null) { t += r.cpu_pct; n++; } }); return n ? t.toFixed(t >= 10 ? 0 : 1) + '%' : '—'; },
      colMem(col) { var t = 0, n = 0, self = this; col.members.forEach(function (m) { var r = self.res(m); if (r && r.mem_bytes) { t += r.mem_bytes; n++; } }); return n ? fmtBytes(t) : '—'; },
      colDisk(col) { var t = 0, n = 0, self = this; col.members.forEach(function (m) { var r = self.res(m); if (r && r.disk && r.disk.total) { t += r.disk.total; n++; } }); return n ? fmtBytes(t) : '—'; },
      colSpark(col) {
        var self = this, series = [];
        col.members.forEach(function (m) { var r = self.res(m); if (r && r.history && r.history.length) series.push(r.history); });
        if (!series.length) return '0,19 90,19';
        var len = Math.max.apply(null, series.map(function (h) { return h.length; }));
        var pts = [], max = 1;
        for (var i = 0; i < len; i++) { var sum = 0; series.forEach(function (h) { var p = h[h.length - len + i]; if (p) sum += (p[1] || 0); }); pts.push(sum); if (sum > max) max = sum; }
        return pts.map(function (v, i) { return (i * (90 / Math.max(1, len - 1))).toFixed(1) + ',' + (19 - (v / max) * 17).toFixed(1); }).join(' ');
      },
      attnClass(id) {
        var store = Alpine.store('sessions')[id]; var a = (store && (store.attention || store.activityState)) || 'idle';
        return a === 'tool_running' ? 'working' : a === 'thinking' ? 'thinking' : '';
      },
      attnTitle(col) { var self = this, w = 0, t = 0; col.members.forEach(function (m) { var c = self.attnClass(m); if (c === 'working') w++; else if (c === 'thinking') t++; }); return w + ' working · ' + t + ' thinking · ' + (col.members.length - w - t) + ' idle'; },

      // ── session actions: the org glyph's menu, same actions as the Sessions page ──
      showSessionActions(s) {
        var tmux = s.session_id || s.id, actions = [];
        if (s.is_live) {
          actions.push({ label: s.nag_enabled ? 'Disable Nag' : 'Enable Nag (15m)', handler: function () {
            fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', {
              method: s.nag_enabled ? 'DELETE' : 'PUT', headers: { 'Content-Type': 'application/json' },
              body: s.nag_enabled ? undefined : JSON.stringify({ enabled: true, interval: 15 }),
            });
          } });
          if (s.nag_enabled) {
            [5, 15, 30, 60].forEach(function (mins) {
              if (mins !== s.nag_interval) actions.push({ label: 'Nag every ' + mins + 'm', handler: function () {
                fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: true, interval: mins, message: s.nag_message || '' }) });
              } });
            });
          }
          actions.push({ label: 'Open full viewer', handler: function () { if (window.navigateTo) window.navigateTo('/session/' + encodeURIComponent(s.project || 'default') + '/' + encodeURIComponent(tmux)); } });
          actions.push({ label: 'Restart Session', handler: function () { fetch('/api/session/' + encodeURIComponent(tmux) + '/restart', { method: 'POST' }); } });
          actions.push({ label: 'Close Session', style: 'destructive', handler: function () { fetch('/api/terminal/' + encodeURIComponent(tmux) + '/kill', { method: 'POST' }); } });
        }
        if (window.actionSheet && window.actionSheet.show) window.actionSheet.show({ title: s.label || tmux, actions: actions });
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
      hasWorkspaceChanges() { return false; },
      workspaceStatusTooltip() { return ''; },
      whenLine() { return ''; },
      refreshDisk() {},
      resumeSession() {},
    }; });
  });
})();
