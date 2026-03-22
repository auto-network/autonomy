// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads active sessions from Alpine.store('sessions') (fed by session:registry
// and session:messages SSE topics). Fetches recent sessions from DAO endpoint.

(function () {

  function _formatAge(secs) {
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.round(secs / 60) + 'm ago';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h ' + Math.round((secs % 3600) / 60) + 'm ago';
    return Math.floor(secs / 86400) + 'd ' + Math.round((secs % 86400) / 3600) + 'h ago';
  }

  function _formatSize(bytes) {
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  function _formatProject(project) {
    // Strip /home/<user>/workspace/ path prefix (encoded as dashes)
    const cleaned = project
      .replace(/^-home-[^-]+-workspace-/, '')
      .replace(/^-home-[^-]+-/, '')
      .replace(/^-+/, '');
    return cleaned || 'home';
  }

  function _mapActive(s) {
    return {
      ...s,
      _ageStr: _formatAge(s.age_seconds),
      _sizeStr: _formatSize(s.size_bytes),
      _project: _formatProject(s.project),
    };
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('sessionsPage', () => ({
      interactive: [],
      agents: [],
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
          var ageSeconds = Math.round(now - (s.startedAt || now));
          var hasData = s.entries.length > 0 || (s.sizeMB && sizeVal > 0);
          var isNew = ageSeconds < 30;
          all.push({
            session_id: id,
            project: s.project || '',
            label: s.label || '',
            size_bytes: sizeVal * 1048576,
            age_seconds: ageSeconds,
            active: hasData && s.lastActivity > 0 && (now - s.lastActivity) < 60,
            latest: lastEntry ? (lastEntry.content || '').slice(0, 150) : '',
            type: s.sessionType || 'terminal',
            tmux_session: s.tmuxSession || '',
            _starting: isNew && !hasData,
            _hasData: !!hasData,
          });
        }
        if (all.length > 0 || !this.loading) {
          var mapped = all.map(_mapActive);
          var interactiveTypes = ['terminal', 'chatwith', 'host', 'container'];
          this.interactive = mapped.filter(s =>
            s.tmux_session && interactiveTypes.indexOf(s.type) !== -1
          );
          this.agents = mapped.filter(s =>
            !s.tmux_session || interactiveTypes.indexOf(s.type) === -1
          ).filter(s =>
            !s.session_id.startsWith('agent-') && s.project !== 'subagents'
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
            }
          }
        } catch (e) {
          console.warn('[sessionsPage] fallback fetch error', e);
        }
      },

      destroy() {
        if (this._storeWatcher) clearInterval(this._storeWatcher);
        if (this._recentTimer) clearInterval(this._recentTimer);
        if (this._onCreateTerminal) window.removeEventListener('create-terminal', this._onCreateTerminal);
      },
    }));
  });
})();
