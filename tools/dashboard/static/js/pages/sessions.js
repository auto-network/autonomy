// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads active sessions from Alpine.store('sessions') (fed by session:registry
// and session:messages SSE topics). Fetches recent sessions from DAO endpoint.

(function () {

  // ── Shared card helper methods ──────────────────────────────────────────
  // Exposed as window globals so both sessionsPage and designPage can use them.
  // The session-card.html partial references these by name.

  function _borderCls(t) {
    if (t === 'host') return 'session-card-host';
    if (t === 'dispatch') return 'session-card-dispatch';
    if (t === 'librarian') return 'session-card-librarian';
    if (t === 'chatwith') return 'session-card-chatwith';
    return 'session-card-container';
  }

  function _typeBadge(t) {
    if (t === 'dispatch') return 'Dispatch';
    if (t === 'librarian') return 'Librarian';
    if (t === 'host') return 'Host';
    if (t === 'chatwith') return 'Chat-with';
    return '';
  }

  function _typeCls(t) {
    if (t === 'host') return 'sc-type-host';
    if (t === 'dispatch') return 'sc-type-dispatch';
    if (t === 'librarian') return 'sc-type-librarian';
    if (t === 'chatwith') return 'sc-type-chatwith';
    return '';
  }

  // Stat formatters live in /static/js/lib/session-stats.js (window.SessionStats)
  // so the session-viewer can reuse them. Thin aliases keep Alpine-scoped callsites
  // identical.
  function _turnsStr(s) { return window.SessionStats.turnsStr(s); }
  function _ctxStr(s) { return window.SessionStats.ctxStr(s); }
  function _idleStr(s) { return window.SessionStats.idleStr(s); }
  function _ctxWarn(s) { return window.SessionStats.ctxWarn(s); }
  function _recencyColor(s) { return window.SessionStats.recencyColor(s); }

  // Format ISO timestamp → "YYYY-MM-DD HH:MM" in operator-local timezone.
  function _fmtEndedAt(s) {
    var ts = s.ended_at || s.last_activity_at;
    if (!ts) return '—';
    var d = new Date(ts);
    if (isNaN(d.getTime())) return '—';
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  // Footer column 3: 'idle' (relative duration) for live, 'ended' (absolute) for dead.
  function _endedOrIdleLabel(s) { return s.is_live ? 'idle' : 'ended'; }
  function _endedOrIdleValue(s) {
    return s.is_live ? (_idleStr(s) || '—') : _fmtEndedAt(s);
  }
  // Footer column 4: tmux is meaningful for interactive sessions but synthetic
  // for dispatch/librarian — hide entirely on dead dispatch/librarian rows.
  function _showTmuxColumn(s) {
    if (s.is_live) return true;
    return s.session_type !== 'dispatch' && s.session_type !== 'librarian';
  }

  // Expose helpers globally for session-card.html partial (used by sessionsPage and designPage)
  window.sessionCardHelpers = {
    borderCls: _borderCls,
    typeBadge: _typeBadge,
    typeCls: _typeCls,
    turnsStr: _turnsStr,
    ctxStr: _ctxStr,
    idleStr: _idleStr,
    ctxWarn: _ctxWarn,
    recencyColor: _recencyColor,
    endedOrIdleLabel: _endedOrIdleLabel,
    endedOrIdleValue: _endedOrIdleValue,
    showTmuxColumn: _showTmuxColumn,
  };

  // ── Private utilities ───────────────────────────────────────────────────

  function _setNag(tmux, interval, message) {
    fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: true, interval: interval, message: message || ''}),
    });
  }

  // Derive role from explicit field or label patterns (always capitalized)
  function _deriveRole(s) {
    var raw = s.role || '';
    if (!raw) {
      var label = (s.label || '').toLowerCase();
      if (label.indexOf('coordinator') !== -1) raw = 'Coordinator';
      else if (label.indexOf('reviewer') !== -1 || label.indexOf('review') !== -1) raw = 'Reviewer';
      else if (label.indexOf('builder') !== -1 || label.indexOf('build') !== -1) raw = 'Builder';
      else if (label.indexOf('designer') !== -1 || label.indexOf('design') !== -1) raw = 'Designer';
    }
    if (raw) return raw.charAt(0).toUpperCase() + raw.slice(1);
    return '';
  }

  // Build a human-readable title for a librarian recent-card row.
  // The DAO populates librarian_type + librarian_target_bead_id from
  // librarian_jobs.payload. We format as:
  //   "{type} · {bead_id}"         — when target resolves
  //   "{type}"                     — type known but no target
  //   null                         — neither: caller falls back to r.title
  // Separate from the bead title which renders in muted color next to the id.
  function _librarianTitle(r) {
    var type = r.librarian_type;
    var target = r.librarian_target_bead_id;
    if (!type && !target) return null;
    if (type && target) return type + ' · ' + target;
    return type || null;
  }

  // Derive canonical session_type from store sessionType
  function _deriveSessionType(s) {
    var t = s.sessionType || 'terminal';
    if (t === 'host') return 'host';
    if (t === 'chatwith') return 'chatwith';
    if (t === 'container' && s.beadId) return 'dispatch';
    if (t === 'terminal') return 'interactive';
    return 'interactive';
  }

  // --- Zoom level persistence ---
  var _savedZoom = localStorage.getItem('sessionZoom') || 'normal';
  function _applyZoom(level) {
    document.body.classList.remove('zoom-compact', 'zoom-normal', 'zoom-expanded');
    document.body.classList.add('zoom-' + level);
  }
  _applyZoom(_savedZoom);

  // Expose setZoom globally so template buttons can call it
  window.setZoom = function(level) {
    localStorage.setItem('sessionZoom', level);
    _applyZoom(level);
    Alpine.store('sessionZoom', level);
  };

  document.addEventListener('alpine:init', () => {
    Alpine.store('sessionZoom', _savedZoom);

    Alpine.data('sessionsPage', () => ({
      interactive: [],
      recent: [],
      loading: true,
      _creating: false,
      zoom: localStorage.getItem('sessionZoom') || 'normal',

      // Workspace-changes state per tmux name. Populated by
      // _fetchWorkspaceStatus and consumed by ``wsStatus(s)`` /
      // ``hasWorkspaceChanges(s)`` so the session-card partial can
      // surface the same ⌥ indicator the page-mode header shows.
      _workspaceStatusByTmux: {},

      hasWorkspaceChanges(s) {
        var key = (s && (s.tmux_session || s.id)) || '';
        var ws = this._workspaceStatusByTmux[key];
        return !!(ws && ws.hasChanges);
      },
      workspaceStatusTooltip(s) {
        var key = (s && (s.tmux_session || s.id)) || '';
        var ws = this._workspaceStatusByTmux[key];
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
      workspaceStatusHref(s) {
        var key = (s && (s.tmux_session || s.id)) || '';
        return '/worktrees?session=' + encodeURIComponent(key);
      },

      // --- Workspace dropdown state (fetched from /api/projects) ---
      // orgGroups[i].org is a resolved identity object
      // {slug,name,color,favicon,initial,resolved} — the header renders a
      // glyph + name and inline-colors the text from org.color.
      projects: [],
      orgGroups: [],

      // --- Card helper methods (referenced by session-card.html partial) ---
      borderCls: _borderCls,
      typeBadge: _typeBadge,
      typeCls: _typeCls,
      turnsStr: _turnsStr,
      ctxStr: _ctxStr,
      idleStr: _idleStr,
      ctxWarn: _ctxWarn,
      recencyColor: _recencyColor,
      endedOrIdleLabel: _endedOrIdleLabel,
      endedOrIdleValue: _endedOrIdleValue,
      showTmuxColumn: _showTmuxColumn,

      // --- Active Sessions: client-side sort mode ---
      // Options match the dropdown-toggle partial contract: [{k, l}, ...]
      activeSortOptions: [
        {k: 'lastActivity', l: 'Recent Activity'},
        {k: 'idle',         l: 'Longest Idle'},
        {k: 'turns',        l: 'Most Turns'},
        {k: 'ctx',          l: 'Most Context'},
      ],
      activeSort: (function() {
        var saved = localStorage.getItem('sessionsActiveSort');
        var allowed = ['lastActivity', 'idle', 'turns', 'ctx'];
        return allowed.indexOf(saved) !== -1 ? saved : 'lastActivity';
      })(),

      // Booting sessions: optimistic pending tiles + any live session the
      // lifecycle derivation still marks as starting up (startupVisible).
      // Rendered in a dedicated "Launching" section pinned to the top of
      // the list, newest-first, so a booting session is always reachable
      // regardless of the Active sort (turns/idle/ctx).
      _isLaunching(s) {
        if (s._launching) return true;
        var L = window.Autonomy && window.Autonomy.lifecycle;
        return !!(L && L.startupVisible(s));
      },
      get launching() {
        var self = this;
        var arr = this.interactive.filter(function(s) { return self._isLaunching(s); });
        arr.sort(function(a, b) { return (b.created_at || 0) - (a.created_at || 0); });
        return arr;
      },
      get activeInteractive() {
        var self = this;
        return this.interactive.filter(function(s) { return !self._isLaunching(s); });
      },

      get sortedInteractive() {
        var arr = this.activeInteractive.slice();
        var mode = this.activeSort;
        var now = Date.now() / 1000;
        arr.sort(function(a, b) {
          switch (mode) {
            case 'idle': {
              var ai = a.last_activity ? now - a.last_activity : -Infinity;
              var bi = b.last_activity ? now - b.last_activity : -Infinity;
              return bi - ai;
            }
            case 'turns':
              return (b.entry_count || 0) - (a.entry_count || 0);
            case 'ctx':
              return (b.context_tokens || 0) - (a.context_tokens || 0);
            case 'lastActivity':
            default: {
              var av = a.last_activity || -Infinity;
              var bv = b.last_activity || -Infinity;
              return bv - av;
            }
          }
        });
        return arr;
      },

      // --- Recent Sessions: filter + resume state ---
      recentFilter: localStorage.getItem('recentSessionFilter') || 'all',
      filterOptions: [
        {key: 'all', label: 'All'},
        {key: 'interactive', label: 'Interactive'},
        {key: 'dispatch', label: 'Dispatch'},
        {key: 'librarian', label: 'Librarian'},
      ],
      // --- Recent Sessions: sort + since (per design acb2829b-4fc0 rev b39626f2) ---
      recentSortOptions: [
        {k: 'lastActivity', l: 'End time'},
        {k: 'created',      l: 'Start time'},
        {k: 'turns',        l: 'Most Turns'},
        {k: 'ctx',          l: 'Most Context'},
        {k: 'duration',     l: 'Duration'},
      ],
      recentSinceOptions: [
        {k: '6h',  l: '6 hours'},
        {k: '1d',  l: '1 day'},
        {k: '1w',  l: '1 week'},
        {k: 'all', l: 'All time'},
      ],
      recentSort: (function() {
        var saved = localStorage.getItem('recentSort');
        var allowed = ['lastActivity', 'created', 'turns', 'ctx', 'duration'];
        return allowed.indexOf(saved) !== -1 ? saved : 'lastActivity';
      })(),
      recentSince: (function() {
        var saved = localStorage.getItem('recentSince');
        var allowed = ['6h', '1d', '1w', 'all'];
        return allowed.indexOf(saved) !== -1 ? saved : '1d';
      })(),
      resuming: {},
      resumeError: {},
      resumed: {},

      setRecentFilter(f) {
        if (this.recentFilter === f) return;
        this.recentFilter = f;
        localStorage.setItem('recentSessionFilter', f);
        // Server applies per-type quotas based on ?type=, so a chip change
        // needs a refetch to rebalance the budget toward the selected group.
        this._fetchRecent();
      },

      get filtered() {
        if (this.recentFilter === 'all') return this.recent;
        var self = this;
        return this.recent.filter(function(s) { return self._matchesFilter(s, self.recentFilter); });
      },

      _matchesFilter(s, f) {
        var t = s.session_type || 'interactive';
        if (f === 'dispatch') return t === 'dispatch';
        if (f === 'librarian') return t === 'librarian';
        return t === 'terminal' || t === 'host' || t === 'chatwith' || t === 'session' || t === 'interactive';
      },

      async resumeSession(s, $event) {
        $event.preventDefault();
        $event.stopPropagation();
        if (this.resuming[s.id]) return;
        this.resuming[s.id] = true;
        this.resumeError[s.id] = '';

        // ── Optimistic UI: move card from Recent → Active ──
        var recentIdx = this.recent.indexOf(s);
        if (recentIdx !== -1) this.recent.splice(recentIdx, 1);

        // Placeholder key — will be replaced by real tmux_name from API
        var placeholderKey = 'resume-' + s.id.slice(0, 8);
        var store = window.getSessionStore(placeholderKey);
        store.isLive = false;    // gray dot until monitor picks it up
        store._resuming = true;  // bypass isLive filter in _updateFromStore
        store.label = s.label || '';
        // Map DAO session_type → store sessionType that appears in active grid
        var typeMap = {dispatch: 'container', librarian: 'container', interactive: 'host'};
        store.sessionType = typeMap[s.session_type] || 'container';
        store.project = (s.project || '').replace(/^\[|\]$/g, '');
        store.entries = [];
        store.entryCount = 0;
        store.contextTokens = 0;
        store.startedAt = Date.now() / 1000;

        try {
          var res = await fetch('/api/session/resume', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({source_id: s.id}),
          });
          if (!res.ok) {
            var err = await res.json().catch(function() { return {}; });
            throw new Error(err.error || 'Resume failed');
          }
          var data = await res.json();
          this.resuming[s.id] = false;
          this.resumed[s.id] = true;

          // API returns real tmux_name — migrate store if different from placeholder
          var realKey = data.tmux_name || placeholderKey;
          if (realKey !== placeholderKey) {
            var realStore = window.getSessionStore(realKey);
            realStore.isLive = false;
            realStore._resuming = true;
            realStore.label = data.label || s.label || '';
            realStore.sessionType = store.sessionType;
            realStore.project = store.project;
            realStore.startedAt = store.startedAt;
            // Remove placeholder
            delete Alpine.store('sessions')[placeholderKey];
          }
        } catch (e) {
          this.resuming[s.id] = false;
          this.resumeError[s.id] = e.message || 'Failed';

          // ── Rollback: remove from store, re-insert into recent ──
          delete Alpine.store('sessions')[placeholderKey];
          if (recentIdx !== -1) {
            this.recent.splice(recentIdx, 0, s);
          } else {
            this.recent.unshift(s);
          }

          var self = this;
          setTimeout(function() { self.resumeError[s.id] = ''; }, 3000);
        }
      },

      setZoom(level) {
        this.zoom = level;
        localStorage.setItem('sessionZoom', level);
        document.body.classList.remove('zoom-compact', 'zoom-normal', 'zoom-expanded');
        document.body.classList.add('zoom-' + level);
      },
      _scheduleStoreSync() {
        if (this._storeSyncQueued) return;
        this._storeSyncQueued = true;
        var self = this;
        var raf = window.requestAnimationFrame || function(cb) { return setTimeout(cb, 0); };
        raf(function() {
          self._storeSyncQueued = false;
          self._updateFromStore();
        });
      },
      _scheduleRecentRefresh() {
        if (this._recentRefreshQueued) return;
        this._recentRefreshQueued = true;
        var self = this;
        setTimeout(function() {
          self._recentRefreshQueued = false;
          self._fetchRecent();
        }, 150);
      },
      init() {
        this.$watch('activeSort', (v) => localStorage.setItem('sessionsActiveSort', v));
        this.$watch('recentSort', (v) => {
          localStorage.setItem('recentSort', v);
          this._fetchRecent();
        });
        this.$watch('recentSince', (v) => {
          localStorage.setItem('recentSince', v);
          this._fetchRecent();
        });

        // Ensure global SSE handlers are registered
        window.ensureSessionMessages();

        // Build session list from the shared session store.
        this._updateFromStore();
        var self = this;
        this._onStoreChanged = function() { self._scheduleStoreSync(); };
        window.addEventListener('sessions:store-changed', this._onStoreChanged);

        // Fetch recent sessions (from graph.db, not monitor). Registry churn
        // invalidates the recent list; we refresh on that signal instead of
        // polling every 30s.
        this._fetchRecent();
        this._onRegistryChanged = function() { self._scheduleRecentRefresh(); };
        window.addEventListener('sessions:registry-changed', this._onRegistryChanged);

        // Workspace-changes status per session (drives the ⌥ indicator
        // on each session card). Pushed live via the ``worktrees`` SSE
        // topic — the bus replays cached state on registerHandler so
        // the cards paint immediately on mount, and updates flow
        // through the same path within ~5s of the backend monitor
        // detecting a change.
        this._workspaceHandler = function (rows) { self._applyWorkspaceRows(rows); };
        if (typeof window.registerHandler === 'function') {
          window.registerHandler('worktrees', this._workspaceHandler);
        }
        // One-shot fallback for the cold-start window before the
        // backend has emitted its first 'worktrees' broadcast.
        if (!Object.keys(this._workspaceStatusByTmux).length) {
          this._fetchWorkspaceStatus();
        }

        // Fetch workspace registry for the launch dropdown
        this._fetchProjects();

        // Handle new terminal creation from the + button dropdown.
        // Session creation goes through POST /api/session/create, which is the
        // sole creation path. After the session exists, ws_terminal is used
        // only for PTY bridging (attach).
        //   detail.project   → workspace container
        //   detail.type='host' → host session
        //   (empty)          → default autonomy container
        this._onCreateTerminal = async (e) => {
          var detail = e.detail || {};
          this._creating = true;
          // ── Optimistic launching tile (auto-yfcoc PART 2) ──
          // Render a tile the instant the workspace is picked — before the
          // POST returns — so the operator gets immediate feedback and the
          // session appears in the Launching section at the top of the list.
          // Reconciled away in _updateFromStore when the real
          // session:registry row for this workspace arrives, or expired
          // after the TTL if creation failed.
          var isHost = detail.type === 'host' && !detail.project;
          var pendingId = 'pending-' + (
            (window.crypto && crypto.randomUUID)
              ? crypto.randomUUID()
              : String(Date.now()) + '-' + Math.floor(Math.random() * 1e9)
          );
          try {
            var org = null;
            if (detail.project) {
              for (var pi = 0; pi < (this.projects || []).length; pi++) {
                if (this.projects[pi].graph_project === detail.project) {
                  org = this.projects[pi].org || null; break;
                }
              }
            }
            var pstore = window.getSessionStore(pendingId);
            pstore.isLive = true;
            pstore.project = detail.project || '';
            pstore.label = detail.label || (isHost ? 'New host session' : (detail.project || 'New session'));
            pstore.sessionType = isHost ? 'host' : 'container';
            pstore.startedAt = Date.now() / 1000;
            pstore.setupPhase = 'pending';
            pstore.harnessPhase = 'pending';
            pstore.activityState = 'thinking';
            pstore.org = org;
            pstore._launching = true;
            this._updateFromStore();
          } catch (_) { /* optimistic tile is best-effort */ }
          try {
            var body = {};
            if (detail.project) body.project = detail.project;
            else if (detail.type === 'host') body.type = 'host';
            var res = await fetch('/api/session/create', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(body),
            });
            if (!res.ok && res.status !== 202) {
              var err = await res.json().catch(function () { return {}; });
              throw new Error(err.error || 'Session create failed');
            }
            await res.json();
          } catch (err) {
            // Create failed — drop the optimistic tile immediately so it
            // doesn't linger as a ghost in the Launching section.
            try {
              delete Alpine.store('sessions')[pendingId];
              this._updateFromStore();
            } catch (_) {}
            console.warn('[sessionsPage] create-terminal failed', err);
          } finally {
            this._creating = false;
          }
        };
        window.addEventListener('create-terminal', this._onCreateTerminal);
      },

      async _fetchProjects() {
        try {
          const data = await fetch('/api/projects').then(r => r.json());
          const projects = Array.isArray(data.projects) ? data.projects : [];
          this.projects = projects;
          // Group by graph_project, preserving first-seen order. Each
          // group's `org` is the resolved identity object returned by
          // /api/projects, ready for the picker header to render.
          const order = [];
          const byOrg = {};
          const orgObjs = {};
          for (const p of projects) {
            const slug = (p.org && p.org.slug) || p.graph_project || 'other';
            if (!byOrg[slug]) {
              byOrg[slug] = [];
              order.push(slug);
              orgObjs[slug] = p.org || {slug, name: slug, color: '#4b5563', favicon: null, initial: '?', resolved: false};
            }
            byOrg[slug].push(p);
          }
          this.orgGroups = order.map(slug => ({org: orgObjs[slug], projects: byOrg[slug]}));
        } catch (e) {
          console.warn('[sessionsPage] projects fetch error', e);
          this.projects = [];
          this.orgGroups = [];
        }
      },

      _updateFromStore() {
        var allSessions = Alpine.store('sessions');
        // ── Reconcile / expire optimistic launching tiles (auto-yfcoc) ──
        // Drop a pending tile once a real (non-pending) live session for the
        // same workspace has arrived via registry, or after _LAUNCH_TTL_S
        // if creation never produced one (failed POST, lost broadcast).
        var _LAUNCH_TTL_S = 120;
        var nowS = Date.now() / 1000;
        var realByKey = {};
        for (var rid in allSessions) {
          if (rid.indexOf('pending-') === 0) continue;
          var rs = allSessions[rid];
          if (rs && rs.isLive) {
            var rk = (rs.project || '') + '|' + (rs.sessionType || '');
            var rst = rs.startedAt || 0;
            if (realByKey[rk] === undefined || rst > realByKey[rk]) realByKey[rk] = rst;
          }
        }
        for (var pid in allSessions) {
          if (pid.indexOf('pending-') !== 0) continue;
          var p = allSessions[pid];
          var pk = (p.project || '') + '|' + (p.sessionType || '');
          // A real session created at/after this tile (5s skew tolerance)
          // means the launch resolved — retire the placeholder.
          var matched = realByKey[pk] !== undefined && realByKey[pk] >= (p.startedAt || 0) - 5;
          var expired = (nowS - (p.startedAt || 0)) > _LAUNCH_TTL_S;
          if (matched || expired) delete allSessions[pid];
        }
        var all = [];
        for (var id in allSessions) {
          var s = allSessions[id];
          if (!s.isLive && !s._resuming) continue;
          var lastEntry = s.entries.length > 0 ? s.entries[s.entries.length - 1] : null;
          var sizeVal = s.sizeMB ? parseFloat(s.sizeMB) : 0;
          var hasData = s.entries.length > 0 || (s.sizeMB && sizeVal > 0) || s.entryCount > 0 || s.lastActivity > 0;
          var role = _deriveRole(s);
          all.push({
            id: id,
            session_id: id,
            project: s.project || '',
            label: s.label || '',
            role: role,
            is_live: s.isLive,
            created_at: s.startedAt || 0,
            last_activity: s.lastActivity || 0,
            latest: lastEntry ? (lastEntry.content || '').slice(0, 150) : (s.lastMessage || ''),
            type: s.sessionType || 'terminal',
            session_type: _deriveSessionType(s),
            tmux_session: id,
            bead_id: s.beadId || '',
            entry_count: s.entryCount || s.entries.length,
            context_tokens: s.contextTokens || 0,
            topics: s.topics || [],
            nag_enabled: s.nagEnabled || false,
            nag_interval: s.nagInterval || 15,
            nag_message: s.nagMessage || '',
            dispatch_nag_enabled: s.dispatchNagEnabled || false,
            activity_state: s.activityState || 'idle',
            org: s.org || null,
            // Live sessions are not "resumable" in the same sense as
            // dead-and-ingested sessions; they are already alive. The
            // store row carries no JSONL-path concept for the resumable
            // check. Dead recent sessions get their resumable flag from
            // the server-side DAO (dao_sessions.get_recent_sessions
            // computes it from file_path existence).
            resumable: s.resumable === true,
            // auto-ngis4: harness + model from session store
            // (graph://553c7437-036 icon-rail).
            harness: s.harness || null,
            model: s.model || null,
            // auto-yfcoc: startup-phase fields. The store carries
            // camelCase; the partial expects snake_case so we map
            // here. Defaults match the store-creation defaults so
            // missing fields don't break the lifecycle derivation.
            // ``resolved`` (carried as-is from the registry payload)
            // is the migration-artifact guard for pre-existing
            // sessions whose setup_phase + harness_phase columns
            // defaulted to 'pending' from the schema migration —
            // see lifecycle.js for the bypass.
            setup_phase: s.setupPhase || 'pending',
            harness_phase: s.harnessPhase || 'pending',
            harness_state: s.harnessState || {},
            resolved: s.resolved === true,
            // auto-yfcoc PART 2: optimistic launching-tile marker. Drives
            // placement into the Launching section (and the chip suppresses
            // navigation until the real session reconciles in).
            _launching: s._launching === true,
            _hasData: !!hasData,
          });
        }
        if (all.length > 0 || !this.loading) {
          // Sort by creation time descending — stable across navigations
          all.sort(function(a, b) { return (b.created_at || 0) - (a.created_at || 0); });
          var interactiveTypes = ['terminal', 'chatwith', 'host', 'container'];
          this.interactive = all.filter(s =>
            s.session_id && interactiveTypes.indexOf(s.type) !== -1
          );
          this.loading = false;
        }
      },

      // Update _workspaceStatusByTmux from a worktree-rows payload.
      // Aggregate per tmux_name — a session can own multiple repo
      // worktrees (autonomy + enterprise_ng), and the indicator
      // should reflect the union of dirty + commits-ahead.
      _applyWorkspaceRows(rows) {
        if (!Array.isArray(rows)) return;
        var by = {};
        for (var i = 0; i < rows.length; i++) {
          var r = rows[i];
          var key = r.session_name;
          if (!key) continue;
          if (!by[key]) by[key] = { dirtyCount: 0, commitsAhead: 0 };
          if (r.is_dirty) by[key].dirtyCount += (r.dirty_files || []).length;
          by[key].commitsAhead += r.commits_ahead || 0;
        }
        for (var k in by) {
          by[k].hasChanges = (by[k].dirtyCount + by[k].commitsAhead) > 0;
        }
        this._workspaceStatusByTmux = by;
      },
      async _fetchWorkspaceStatus() {
        try {
          var rows = await fetch('/api/worktrees').then(function (r) {
            return r.ok ? r.json() : [];
          });
          this._applyWorkspaceRows(rows);
        } catch (_) {
          // Best-effort; sessions list shouldn't crash on a worktree fetch hiccup.
        }
      },

      async _fetchRecent() {
        try {
          const url = '/api/dao/recent_sessions?type=' + encodeURIComponent(this.recentFilter || 'all')
            + '&sort=' + encodeURIComponent(this.recentSort)
            + '&since=' + encodeURIComponent(this.recentSince);
          const data = await fetch(url).then(r => r.json());
          if (!Array.isArray(data)) { this.recent = []; return; }
          this.recent = data.map(function(r) {
            // tmux_session is the viewer's keying field — the unified tail
            // endpoint also resolves dispatch/librarian UUIDs, but tmux_name
            // remains authoritative. Falling back to session_uuid or graph
            // source id used to yield phantom Active cards (auto-ylj6r).
            var sessionId = r.tmux_session;
            if (!sessionId) return null;
            var lastTs = r.last_activity_at || r.created_at || '';
            // For librarian rows, the raw title is the process name
            // ("librarian-review_report-$pid-$job_uuid"). Replace with a
            // type + target formatting derived from librarian_jobs.payload.
            var label = r.title || '';
            if (r.session_type === 'librarian') {
              var libLabel = _librarianTitle(r);
              if (libLabel) label = libLabel;
            }
            return {
              id: r.id,
              session_id: sessionId,
              label: label,
              session_type: r.session_type || 'interactive',
              type: r.type || 'container',
              is_live: !!r.is_live,
              project: r.project || '',
              topics: [],
              latest: '',
              entry_count: r.entry_count || r.total_turns || 0,
              context_tokens: r.context_tokens || r.total_tokens || 0,
              last_activity: lastTs ? Math.round(new Date(lastTs).getTime() / 1000) : 0,
              created_at: r.created_at || '',
              last_activity_at: r.last_activity_at || '',
              ended_at: r.ended_at || r.last_activity_at || '',
              tmux_session: r.tmux_session,
              nag_enabled: false,
              dispatch_nag_enabled: false,
              role: r.role || '',
              resumable: r.resumable || false,
              bead_id: r.bead_id || '',
              org: r.org || null,
              librarian_type: r.librarian_type || null,
              librarian_target_bead_id: r.librarian_target_bead_id || null,
              librarian_target_bead_title: r.librarian_target_bead_title || '',
              // auto-ngis4 — pass harness + model through so dead recent
              // cards still render the icon-rail badge.
              harness: r.harness || null,
              model: r.model || null,
              // auto-yfcoc — dead recent rows carry setup_phase /
              // harness_phase from the DAO; passthrough so the
              // lifecycle derivation correctly classifies them as
              // dead_resumable / dead_not_resumable. The chip
              // self-suppresses on dead rows; the inline Resume
              // action renders when resumable=true. Dead rows always
              // have a JSONL on disk (that's how they were ingested),
              // so resolved=true here is correct — irrelevant to dead
              // classification but mirrors the live path's shape.
              setup_phase: r.setup_phase || 'pending',
              harness_phase: r.harness_phase || 'pending',
              harness_state: r.harness_state || {},
              resolved: true,
            };
          }).filter(function(x) { return x !== null; });
        } catch (e) {
          console.warn('[sessionsPage] recent fetch error', e);
        }
      },

      navigate(s) {
        if (window.actionSheet.isOpen()) return;
        // Recent cards carry project wrapped in brackets for legacy display
        // (dao/sessions.py:get_recent_sessions). Strip them before routing.
        var proj = (s.project || '').replace(/^\[|\]$/g, '') || 'session';
        var path = '/session/' + encodeURIComponent(proj) + '/' + s.session_id
          + '?tmux=' + encodeURIComponent(s.session_id);
        navigateTo(path);
      },

      showSessionActions(s) {
        var label = s.label || s.session_id.slice(0, 12);
        var tmux = s.session_id;
        var actions = [];
        var self = this;

        if (s.is_live) {
          // ── Live session: nag toggle, nag presets, destructive close ──
          var nagLabel = s.nag_enabled ? 'Disable Nag' : 'Enable Nag (15m)';
          actions.push({
            label: nagLabel,
            handler: function() {
              fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', {
                method: s.nag_enabled ? 'DELETE' : 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: s.nag_enabled ? undefined : JSON.stringify({enabled: true, interval: 15}),
              });
            },
          });

          if (s.nag_enabled) {
            [5, 15, 30, 60].forEach(function(mins) {
              if (mins !== s.nag_interval) {
                actions.push({
                  label: 'Nag every ' + mins + 'm',
                  handler: function() { _setNag(tmux, mins, s.nag_message); },
                });
              }
            });
          }

          actions.push({
            label: 'Close Session',
            style: 'destructive',
            handler: async function() {
              if (tmux) {
                await fetch('/api/terminal/' + encodeURIComponent(tmux) + '/kill', { method: 'POST' });
              }
            },
          });
        } else {
          // ── Dead (recent) session: Open + optional Resume ──
          actions.push({
            label: 'Open',
            handler: function() { self.navigate(s); },
          });
          if (s.resumable) {
            actions.push({
              label: 'Resume',
              handler: function() {
                // resumeSession expects an $event object; synthesize one.
                var fake = {preventDefault: function(){}, stopPropagation: function(){}};
                self.resumeSession(s, fake);
              },
            });
          }
        }

        window.actionSheet.show({title: label, actions: actions});
      },

      destroy() {
        if (this._onStoreChanged) window.removeEventListener('sessions:store-changed', this._onStoreChanged);
        if (this._onRegistryChanged) window.removeEventListener('sessions:registry-changed', this._onRegistryChanged);
        if (this._workspaceHandler && typeof window.unregisterHandler === 'function') {
          window.unregisterHandler('worktrees', this._workspaceHandler);
          this._workspaceHandler = null;
        }
        if (this._onCreateTerminal) window.removeEventListener('create-terminal', this._onCreateTerminal);
      },
    }));
  });
})();
