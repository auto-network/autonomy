// Mission Control plugin — frontend Alpine factory.
//
// Read-only monitoring surface. Missions and site revisions are created
// and pushed by agent sessions calling the API directly (POST
// /api/missions, POST /api/missions/<id>/site) — the same way the
// coordinator session pushes OSS Insights updates. This page has no
// create/push/delete/activate controls; it only lists what agents have
// already published.
//
// IIFE wrapper: plugin pages load as classic scripts in alphabetical
// order sharing one global lexical environment, so every plugin factory
// is scoped this way to stay collision-proof against siblings.

(function () {

function missionControlPage() {
  return {
    missions: [],
    revisions: {},
    expanded: '',

    async init() {
      await this.refreshMissions();
    },

    async refreshMissions() {
      try {
        const res = await fetch('/api/missions');
        const data = await res.json();
        const list = (data && data.missions) || [];
        // The list endpoint doesn't include current_revision — fetch each
        // mission's detail for the summary line. Small N (missions are a
        // rare, coarse-grained entity), so no pagination/batching needed.
        this.missions = await Promise.all(list.map(async (m) => {
          try {
            const detail = await fetch('/api/missions/' + encodeURIComponent(m.mission_id));
            const body = await detail.json();
            return (body && body.mission) || m;
          } catch (_) {
            return m;
          }
        }));
      } catch (_) {
        this.missions = [];
      }
    },

    async toggleExpand(missionId) {
      if (this.expanded === missionId) {
        this.expanded = '';
        return;
      }
      this.expanded = missionId;
      await this.refreshRevisions(missionId);
    },

    async refreshRevisions(missionId) {
      try {
        const res = await fetch('/api/missions/' + encodeURIComponent(missionId) + '/site/revisions');
        const data = await res.json();
        this.revisions = {...this.revisions, [missionId]: (data && data.revisions) || []};
      } catch (_) {
        this.revisions = {...this.revisions, [missionId]: []};
      }
    },
  };
}

if (typeof window !== 'undefined') {
  window.missionControlPage = missionControlPage;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { missionControlPage };
}

})();
