// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Fetches active and recent sessions from DAO-backed endpoints on init().
// Subscribes to SSE 'nav' topic so the Sessions nav badge stays current
// (badge update is handled by the nav handler in app.js, not here).

(function () {

  function _formatAge(secs) {
    if (secs < 60) return secs + 's ago';
    return Math.round(secs / 60) + 'm ago';
  }

  function _formatSize(bytes) {
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  function _formatProject(project) {
    return project.replace(/-home-jeremy-?/, '').replace(/workspace-/, '') || 'home';
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

      async init() {
        await this._refresh();
        this._refreshTimer = setInterval(() => this._refresh(), 5000);
      },

      async _refresh() {
        if (this._refreshing) return;
        this._refreshing = true;
        try {
          const [activeData, recentData] = await Promise.all([
            fetch('/api/dao/active_sessions?threshold=600').then(r => r.json()),
            fetch('/api/dao/recent_sessions?limit=20').then(r => r.json()),
          ]);
          const mapped = (Array.isArray(activeData) ? activeData : []).map(_mapActive);
          this.interactive = mapped.filter(s =>
            s.tmux_session && (s.type === 'terminal' || s.type === 'chatwith')
          );
          this.agents = mapped.filter(s =>
            !s.tmux_session || (s.type !== 'terminal' && s.type !== 'chatwith')
          ).filter(s =>
            s.type !== 'host' || s.age_seconds < 600
          ).filter(s =>
            !s.session_id.startsWith('agent-') && s.project !== 'subagents'
          );
          this.recent = Array.isArray(recentData) ? recentData : [];
        } catch (e) {
          console.warn('[sessionsPage] refresh error', e);
        } finally {
          this._refreshing = false;
          this.loading = false;
        }
      },

      destroy() {
        if (this._refreshTimer) clearInterval(this._refreshTimer);
      },
    }));
  });
})();
