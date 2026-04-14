// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads active sessions from Alpine.store('sessions') (fed by session:registry
// and session:messages SSE topics). Fetches recent sessions from DAO endpoint.

(function () {

  function _formatTimestamp(epoch) {
    if (!epoch) return '';
    var d = new Date(epoch * 1000);
    var now = new Date();
    var isToday = d.getFullYear() === now.getFullYear() &&
                  d.getMonth() === now.getMonth() &&
                  d.getDate() === now.getDate();
    if (isToday) {
      var h = d.getHours();
      var m = d.getMinutes();
      var ampm = h >= 12 ? 'PM' : 'AM';
      h = h % 12 || 12;
      return h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
    }
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[d.getMonth()] + ' ' + d.getDate();
  }

  function _formatRecency(epoch) {
    if (!epoch) return '';
    var secs = Math.round(Date.now() / 1000 - epoch);
    if (secs < 0) secs = 0;
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.round(secs / 60) + 'm';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h';
    return Math.floor(secs / 86400) + 'd';
  }

  function _formatCtx(tokens) {
    if (!tokens) return '';
    if (tokens >= 1000000) return (tokens / 1000000).toFixed(0) + 'M';
    if (tokens >= 1000) return Math.round(tokens / 1000) + 'K';
    return String(tokens);
  }

  function _setNag(tmux, interval, message) {
    fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: true, interval: interval, message: message || ''}),
    });
  }

  function _recencyColor(epoch) {
    if (!epoch) return 'gray';
    var secs = Math.round(Date.now() / 1000 - epoch);
    if (secs < 120) return 'green';
    if (secs < 600) return 'amber';
    return 'red';
  }

  // Derive role from explicit field or label patterns
  function _deriveRole(s) {
    if (s.role) return s.role;
    var label = (s.label || '').toLowerCase();
    if (label.indexOf('coordinator') !== -1) return 'coordinator';
    if (label.indexOf('reviewer') !== -1 || label.indexOf('review') !== -1) return 'reviewer';
    if (label.indexOf('builder') !== -1 || label.indexOf('build') !== -1) return 'builder';
    if (label.indexOf('designer') !== -1 || label.indexOf('design') !== -1) return 'designer';
    return '';
  }

  var _roleBadgeMap = {
    coordinator: 'Coordinator', reviewer: 'Reviewer',
    builder: 'Builder', designer: 'Designer',
  };
  var _roleClsMap = {
    coordinator: 'sc-role', reviewer: 'sc-role',
    builder: 'sc-role', designer: 'sc-role',
  };

  function _mapActive(s) {
    var role = _deriveRole(s);
    var isHost = s.type === 'host';
    return {
      ...s,
      _createdStr: _formatTimestamp(s.created_at),
      _lastActiveStr: _formatRecency(s.last_activity),
      _turnsStr: s.entry_count ? String(s.entry_count) : '',
      _ctxStr: _formatCtx(s.context_tokens),
      _ctxWarn: s.context_tokens > 700000,
      _recencyColor: _recencyColor(s.last_activity),
      _roleBadge: isHost ? 'Host' : (_roleBadgeMap[role] || ''),
      _roleCls: isHost ? 'sc-role sc-role-host' : (_roleClsMap[role] || ''),
    };
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

      _recentRoleBadge(t) {
        if (t === 'dispatch') return 'Dispatch';
        if (t === 'librarian') return 'Librarian';
        if (t === 'host') return 'Host';
        if (t === 'chatwith') return 'Chat-with';
        return '';
      },

      _recentRoleCls(t) {
        if (t === 'host') return 'sc-role sc-role-host';
        return 'sc-role';
      },

      _recentBorderCls(t) {
        if (t === 'host') return 'session-card-host';
        return 'session-card-container';
      },

      _recentTurnsStr(s) { return s.total_turns ? String(s.total_turns) : ''; },

      _recentTokensStr(s) {
        var t = s.total_tokens || 0;
        if (t >= 1000000) return (t / 1000000).toFixed(1) + 'M';
        if (t >= 1000) return Math.round(t / 1000) + 'K';
        return t ? String(t) : '';
      },

      _agoStr(iso) {
        if (!iso) return '';
        var secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
        if (secs < 60) return secs + 's';
        if (secs < 3600) return Math.round(secs / 60) + 'm';
        if (secs < 86400) return Math.floor(secs / 3600) + 'h';
        return Math.floor(secs / 86400) + 'd';
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
        store.label = s.title || '';
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
            realStore.label = data.label || s.title || '';
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

        // Handle new terminal creation from the + button dropdown
        this._onCreateTerminal = (e) => {
          var cmd = e.detail.cmd;
          this._creating = true;
          var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
          var wsUrl = proto + '//' + location.host + '/ws/terminal?cmd=' + encodeURIComponent(cmd);
          var ws = new WebSocket(wsUrl);
          var self = this;
          ws.onopen = function () {
            // Terminal created — close after 2s to let monitor register
            setTimeout(function () {
              ws.close();
              self._creating = false;
            }, 2000);
          };
          ws.onerror = function () { self._creating = false; };
          ws.onclose = function () { self._creating = false; };
        };
        window.addEventListener('create-terminal', this._onCreateTerminal);
      },

      _updateFromStore() {
        var allSessions = Alpine.store('sessions');
        var now = Date.now() / 1000;
        var all = [];
        for (var id in allSessions) {
          var s = allSessions[id];
          if (!s.isLive && !s._resuming) continue;
          var lastEntry = s.entries.length > 0 ? s.entries[s.entries.length - 1] : null;
          var sizeVal = s.sizeMB ? parseFloat(s.sizeMB) : 0;
          var hasData = s.entries.length > 0 || (s.sizeMB && sizeVal > 0) || s.entryCount > 0 || s.lastActivity > 0;
          all.push({
            session_id: id,
            project: s.project || '',
            label: s.label || '',
            role: s.role || '',
            is_live: s.isLive,
            created_at: s.startedAt || 0,
            last_activity: s.lastActivity || 0,
            latest: lastEntry ? (lastEntry.content || '').slice(0, 150) : (s.lastMessage || ''),
            type: s.sessionType || 'terminal',
            tmux_session: id,
            bead_id: s.beadId || '',
            entry_count: s.entryCount || s.entries.length,
            context_tokens: s.contextTokens || 0,
            topics: s.topics || [],
            nag_enabled: s.nagEnabled || false,
            nag_interval: s.nagInterval || 15,
            nag_message: s.nagMessage || '',
            _hasData: !!hasData,
          });
        }
        if (all.length > 0 || !this.loading) {
          // Sort by creation time descending — stable across navigations
          all.sort(function(a, b) { return (b.created_at || 0) - (a.created_at || 0); });
          var mapped = all.map(_mapActive);
          var interactiveTypes = ['terminal', 'chatwith', 'host', 'container'];
          this.interactive = mapped.filter(s =>
            s.session_id && interactiveTypes.indexOf(s.type) !== -1
          );
          this.loading = false;
        }
      },

      async _fetchRecent() {
        try {
          const data = await fetch('/api/dao/recent_sessions?limit=20').then(r => r.json());
          this.recent = Array.isArray(data) ? data : [];
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
