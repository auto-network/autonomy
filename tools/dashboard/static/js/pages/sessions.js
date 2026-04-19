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
    // Tailwind class map for per-org dropdown headers. Full class strings
    // live here (not assembled dynamically) so the Tailwind v4 scanner picks
    // them up from the JS source.
    const orgColors = {
      autonomy: 'text-emerald-400',
      anchore: 'text-blue-400',
    };

    Alpine.data('sessionsPage', () => ({
      interactive: [],
      recent: [],
      loading: true,
      _creating: false,
      zoom: localStorage.getItem('sessionZoom') || 'normal',

      // --- Workspace dropdown state (fetched from /api/projects) ---
      projects: [],
      orgGroups: [],
      orgColorClass(org) {
        return orgColors[(org || '').toLowerCase()] || 'text-gray-400';
      },

      // --- Card helper methods (referenced by session-card.html partial) ---
      borderCls: _borderCls,
      typeBadge: _typeBadge,
      typeCls: _typeCls,
      turnsStr: _turnsStr,
      ctxStr: _ctxStr,
      idleStr: _idleStr,
      ctxWarn: _ctxWarn,
      recencyColor: _recencyColor,

      // --- Active Sessions: client-side sort mode (cycled by toggle) ---
      activeSort: (function() {
        var saved = localStorage.getItem('sessionsActiveSort');
        var allowed = ['lastActivity', 'idle', 'turns', 'ctx'];
        return allowed.indexOf(saved) !== -1 ? saved : 'lastActivity';
      })(),
      activeSortOptions: ['lastActivity', 'idle', 'turns', 'ctx'],
      activeSortLabels: {
        lastActivity: 'Activity',
        idle: 'Idle time',
        turns: 'Turns',
        ctx: 'Context',
      },

      get activeSortLabel() {
        return this.activeSortLabels[this.activeSort] || 'Activity';
      },

      cycleActiveSort() {
        var i = this.activeSortOptions.indexOf(this.activeSort);
        var next = this.activeSortOptions[(i + 1) % this.activeSortOptions.length];
        this.activeSort = next;
        localStorage.setItem('sessionsActiveSort', next);
      },

      get sortedInteractive() {
        var arr = this.interactive.slice();
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
      resuming: {},
      resumeError: {},
      resumed: {},

      setRecentFilter(f) {
        this.recentFilter = f;
        localStorage.setItem('recentSessionFilter', f);
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
      init() {
        // Ensure global SSE handlers are registered
        window.ensureSessionMessages();

        // Build session list from store (poll every 500ms)
        this._updateFromStore();
        this._storeWatcher = setInterval(() => this._updateFromStore(), 500);

        // Fetch recent sessions (from graph.db, not monitor)
        this._fetchRecent();
        this._recentTimer = setInterval(() => this._fetchRecent(), 30000);

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
          // Group by graph_project, preserving first-seen order
          const order = [];
          const byOrg = {};
          for (const p of projects) {
            const org = p.graph_project || 'other';
            if (!byOrg[org]) { byOrg[org] = []; order.push(org); }
            byOrg[org].push(p);
          }
          this.orgGroups = order.map(org => ({org: org, projects: byOrg[org]}));
        } catch (e) {
          console.warn('[sessionsPage] projects fetch error', e);
          this.projects = [];
          this.orgGroups = [];
        }
      },

      _updateFromStore() {
        var allSessions = Alpine.store('sessions');
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
            activity_state: s.activityState || 'idle',
            resumable: false,
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

      async _fetchRecent() {
        try {
          const data = await fetch('/api/dao/recent_sessions?limit=20').then(r => r.json());
          if (!Array.isArray(data)) { this.recent = []; return; }
          this.recent = data.map(function(r) {
            return {
              id: r.id,
              session_id: r.id,
              label: r.title || '',
              session_type: r.session_type || 'interactive',
              type: r.type || 'container',
              is_live: false,
              project: r.project || '',
              topics: [],
              latest: '',
              entry_count: r.total_turns || 0,
              context_tokens: r.total_tokens || 0,
              last_activity: r.created_at ? Math.round(new Date(r.created_at).getTime() / 1000) : 0,
              created_at: r.created_at || '',
              tmux_session: r.id,
              nag_enabled: false,
              role: '',
              resumable: r.resumable || false,
              bead_id: '',
            };
          });
        } catch (e) {
          console.warn('[sessionsPage] recent fetch error', e);
        }
      },

      navigate(s) {
        if (window.actionSheet.isOpen()) return;
        var path = '/session/' + encodeURIComponent(s.project) + '/' + s.session_id
          + '?tmux=' + encodeURIComponent(s.session_id);
        navigateTo(path);
      },

      showSessionActions(s) {
        var label = s.label || s.session_id.slice(0, 12);
        var tmux = s.session_id;
        var nagLabel = s.nag_enabled ? 'Disable Nag' : 'Enable Nag (15m)';
        var actions = [];

        // Nag toggle
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

        // Nag interval presets (only show if nag is enabled)
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

        // Close session (always last, destructive)
        actions.push({
          label: 'Close Session',
          style: 'destructive',
          handler: async function() {
            if (tmux) {
              await fetch('/api/terminal/' + encodeURIComponent(tmux) + '/kill', { method: 'POST' });
            }
          },
        });

        window.actionSheet.show({title: label, actions: actions});
      },

      destroy() {
        if (this._storeWatcher) clearInterval(this._storeWatcher);
        if (this._recentTimer) clearInterval(this._recentTimer);
        if (this._onCreateTerminal) window.removeEventListener('create-terminal', this._onCreateTerminal);
      },
    }));
  });
})();
