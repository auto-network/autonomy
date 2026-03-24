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
    if (!epoch) return 'recency-gray';
    var secs = Math.round(Date.now() / 1000 - epoch);
    if (secs < 120) return 'recency-green';
    if (secs < 600) return 'recency-amber';
    return 'recency-red';
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
    coordinator: 'CO', reviewer: 'RV', builder: 'BL', designer: 'DS',
  };
  var _roleClsMap = {
    coordinator: 'sc-role-co', reviewer: 'sc-role-rv',
    builder: 'sc-role-bl', designer: 'sc-role-ds',
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
      _roleBadge: isHost ? 'HOST' : (_roleBadgeMap[role] || ''),
      _roleCls: isHost ? 'sc-role-host' : (_roleClsMap[role] || ''),
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
      init() {
        // Ensure global SSE handlers are registered
        window.ensureSessionMessages();

        // Build session list from store (poll every 500ms)
        this._updateFromStore();
        this._storeWatcher = setInterval(() => this._updateFromStore(), 500);

        // Fetch recent sessions (from graph.db, not monitor)
        this._fetchRecent();
        this._recentTimer = setInterval(() => this._fetchRecent(), 30000);

        // Seed stores from HTTP if SSE hasn't delivered registry yet
        this._fetchActiveFallback();

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
          if (!s.isLive) continue;
          var lastEntry = s.entries.length > 0 ? s.entries[s.entries.length - 1] : null;
          var sizeVal = s.sizeMB ? parseFloat(s.sizeMB) : 0;
          var hasData = s.entries.length > 0 || (s.sizeMB && sizeVal > 0) || s.entryCount > 0 || s.lastActivity > 0;
          all.push({
            session_id: id,
            project: s.project || '',
            label: s.label || '',
            is_live: s.isLive,
            created_at: s.startedAt || 0,
            last_activity: s.lastActivity || 0,
            latest: lastEntry ? (lastEntry.content || '').slice(0, 150) : (s.lastMessage || ''),
            type: s.sessionType || 'terminal',
            tmux_session: s.tmuxSession || '',
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
            s.tmux_session && interactiveTypes.indexOf(s.type) !== -1
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

      async _fetchActiveFallback() {
        try {
          const data = await fetch('/api/dao/active_sessions').then(r => r.json());
          if (Array.isArray(data)) {
            for (var i = 0; i < data.length; i++) {
              var s = data[i];
              var store = window.getSessionStore(s.session_id);
              store.project = s.project || '';
              store.sessionType = s.type || '';
              store.tmuxSession = s.tmux_session || '';
              store.label = s.label || '';
              store.isLive = s.is_live !== false;
              store.startedAt = s.started_at || 0;
              if (s.last_activity) store.lastActivity = s.last_activity;
              if (s.entry_count) store.entryCount = s.entry_count;
              if (s.context_tokens) store.contextTokens = s.context_tokens;
              if (s.last_message !== undefined) store.lastMessage = s.last_message;
              if (s.linked !== undefined) store.linked = !!s.linked;
            }
          }
        } catch (e) {
          console.warn('[sessionsPage] fallback fetch error', e);
        }
      },

      navigate(s) {
        if (window.actionSheet.isOpen()) return;
        var path = '/session/' + encodeURIComponent(s.project) + '/' + s.session_id
          + (s.tmux_session ? '?tmux=' + encodeURIComponent(s.tmux_session) : '');
        navigateTo(path);
      },

      showSessionActions(s) {
        var label = s.label || s.tmux_session || s.session_id.slice(0, 12);
        var tmux = s.tmux_session;
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
