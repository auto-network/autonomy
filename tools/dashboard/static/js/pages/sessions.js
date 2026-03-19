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
      active: [],
      recent: [],
      loading: true,

      async init() {
        try {
          const [activeData, recentData] = await Promise.all([
            fetch('/api/dao/active_sessions?threshold=600').then(r => r.json()),
            fetch('/api/dao/recent_sessions?limit=20').then(r => r.json()),
          ]);
          this.active = (Array.isArray(activeData) ? activeData : []).map(_mapActive);
          this.recent = Array.isArray(recentData) ? recentData : [];
        } catch (e) {
          console.warn('[sessionsPage] fetch error', e);
        } finally {
          this.loading = false;
        }
      },

      destroy() {},
    }));
  });
})();
