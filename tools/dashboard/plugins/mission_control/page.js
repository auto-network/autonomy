// Mission Control plugin — frontend Alpine factory.
//
// Plain REST fetches against Mission Control's own API — this plugin owns
// its storage outright (tools/dashboard/dao/mission_control_db.py), so
// unlike Nexus/Coordinator-board there is no Settings-schema proxy layer
// to bind here.
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
    newMissionName: '',
    newMissionCoordinator: '',
    createError: '',
    pushDrafts: {},
    noteDrafts: {},

    async init() {
      await this.refreshMissions();
    },

    async refreshMissions() {
      try {
        const res = await fetch('/api/missions');
        const data = await res.json();
        this.missions = (data && data.missions) || [];
      } catch (_) {
        this.missions = [];
      }
    },

    async createMission() {
      this.createError = '';
      const name = (this.newMissionName || '').trim();
      if (!name) {
        this.createError = 'Name is required';
        return;
      }
      try {
        const res = await fetch('/api/missions', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name,
            coordinator_session: (this.newMissionCoordinator || '').trim(),
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          this.createError = err.error || ('Create failed (' + res.status + ')');
          return;
        }
        this.newMissionName = '';
        this.newMissionCoordinator = '';
        await this.refreshMissions();
      } catch (err) {
        this.createError = String(err);
      }
    },

    async deleteMission(missionId) {
      try {
        await fetch('/api/missions/' + encodeURIComponent(missionId), {method: 'DELETE'});
        await this.refreshMissions();
      } catch (_) {
        // Best-effort — refreshMissions on the next poll will reconcile.
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

    async pushRevision(missionId) {
      const html = this.pushDrafts[missionId];
      if (!html || !html.trim()) return;
      try {
        const res = await fetch('/api/missions/' + encodeURIComponent(missionId) + '/site', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            html,
            note: this.noteDrafts[missionId] || '',
          }),
        });
        if (!res.ok) return;
        this.pushDrafts = {...this.pushDrafts, [missionId]: ''};
        this.noteDrafts = {...this.noteDrafts, [missionId]: ''};
        await this.refreshMissions();
        await this.refreshRevisions(missionId);
      } catch (_) {
        // Leave the draft in place so the operator can retry.
      }
    },

    async activateRevision(missionId, revisionId) {
      try {
        await fetch(
          '/api/missions/' + encodeURIComponent(missionId) +
            '/site/revisions/' + encodeURIComponent(revisionId) + '/activate',
          {method: 'POST'},
        );
        await this.refreshMissions();
        await this.refreshRevisions(missionId);
      } catch (_) {
        // Best-effort.
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
