// The welcome rail's Alpine component (design graph://9a4219b3). Markup lives
// in templates/pages/welcome.html and is injected by the shell router; this
// file only sequences the delivered onboarding, create-org and join flows.
function welcomeApp() {
  return {
    ready: false,
    hasIdentity: false,
    userName: '',
    hasOrg: false,
    orgName: '',
    // Arrived-via-invitation: the same shell, org-attached. The context
    // rides the URL exactly as /network/join reads it (auto-yw5gz) — this
    // shell only detects it and hands it on, one source of truth.
    invited: false,
    inviteName: '',
    inviteInitial: '?',
    fleetEnrollment: null,
    fleetSync: false,
    localStoreSlugs: [],
    fleetSyncStatus: 'synchronizing',
    fleetSyncTimer: null,
    fleetResumeTimer: null,
    fleetResumeBusy: false,

    get step() {
      if (!this.hasIdentity) return 1;
      if (!this.hasOrg) return 2;
      return 3;
    },

    async init() {
      // Server first-render facts ride on the fragment root (see
      // templates/pages/welcome.html); read them before the first refresh.
      var data = (this.$root && this.$root.dataset) || {};
      try { this.fleetEnrollment = JSON.parse(data.fleetEnrollment || 'null'); } catch (e) {}
      this.fleetSync = data.fleetSync === 'true';
      try { this.localStoreSlugs = JSON.parse(data.localStoreSlugs || '[]'); } catch (e) {}
      var q = new URLSearchParams(location.search);
      this.invited = (location.search.length > 1 || location.hash.length > 1);
      var org = q.get('org') || '';
      this.inviteName = org;
      this.inviteInitial = (org.charAt(0) || '?').toUpperCase();
      await this.refresh();
      if (this.fleetEnrollment) {
        await this.resumeFleetEnrollment();
        this.fleetResumeTimer = setInterval(
          () => this.resumeFleetEnrollment(), 3000);
      } else if (this.fleetSync) {
        await this.refreshFleetSync();
        this.fleetSyncTimer = setInterval(() => this.refreshFleetSync(), 1000);
      }
      // Completing a step elsewhere (the ceremony overlay, the create-org
      // screen) fires these — the rail re-reads and advances live.
      window.addEventListener('autonomy:identity-changed', () => this.refresh());
      window.addEventListener('autonomy:orgs-changed', () => this.refresh());
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') this.refresh();
      });
    },

    async refresh() {
      try {
        var s = await (await fetch('/api/identity/status', { cache: 'no-store' })).json();
        this.hasIdentity = !!s.personal_identity;
        this.userName = (s.personal_identity || {}).display_name || '';
      } catch (e) { /* fresh install: no status yet → step 1 */ }
      try {
        var o = await (await fetch('/api/orgs')).json();
        var orgs = (o.orgs || []).map(function (e) {
          var id = e.identity_resolved || {};
          return { slug: id.slug || (e.org || {}).slug || '',
                   name: id.name || (e.org || {}).slug || '' };
        }).filter((x) => {
          // Same reserved-store list used by the server's onboarding gate.
          return x.slug && !this.localStoreSlugs.includes(x.slug);
        });
        this.hasOrg = orgs.length > 0;
        this.orgName = orgs.length ? (orgs[0].name || orgs[0].slug) : '';
      } catch (e) { /* org list unreadable → stay on the org step */ }
      this.ready = true;
    },

    async resumeFleetEnrollment() {
      if (!this.fleetEnrollment || this.fleetEnrollment.status !== 'pending' ||
          this.fleetResumeBusy) return;
      this.fleetResumeBusy = true;
      try {
        var response = await fetch('/api/fleet/enrollment/local-resume', {
          method: 'POST', cache: 'no-store',
        });
        var body = await response.json().catch(function () { return {}; });
        if (body.status === 'unavailable') {
          // The home dashboard reached us and named the invitation fault
          // (e.g. the link was deactivated). Show the exact cause + fix, and
          // KEEP waiting — the moment the link is reactivated there, the next
          // check finishes the join automatically. Do not throw a generic
          // "check failed" for this: it is a specific, actionable state.
          this.fleetEnrollment.error = body.message ||
            'This invitation link is not active on the home dashboard — reactivate it there to finish setup.';
          return;
        }
        if (!response.ok || body.ok === false) {
          throw new Error(body.error || ('Enrollment check failed (' + response.status + ').'));
        }
        if (body.status === 'approved' || body.status === 'declined' ||
            body.status === 'expired') {
          this.fleetEnrollment.status = body.status;
          if (this.fleetResumeTimer) clearInterval(this.fleetResumeTimer);
          this.fleetResumeTimer = null;
        }
      } catch (error) {
        // A transient relay outage does not discard the request. Keep the
        // waiting state and expose the last check failure without spinning.
        this.fleetEnrollment.error = (error && error.message) || String(error);
      } finally {
        this.fleetResumeBusy = false;
      }
    },

    async refreshFleetSync() {
      if (!this.fleetSync || this.fleetSyncStatus === 'complete') return;
      try {
        var response = await fetch('/api/fleet/enrollment/local-sync-status', {
          cache: 'no-store', credentials: 'same-origin',
        });
        var body = await response.json().catch(function () { return {}; });
        if (!response.ok || body.ok === false) return;
        if (body.status === 'complete') {
          this.fleetSyncStatus = 'complete';
          if (this.fleetSyncTimer) clearInterval(this.fleetSyncTimer);
          this.fleetSyncTimer = null;
          setTimeout(() => location.replace('/'), 1400);
        }
      } catch (e) { /* the compact row stays synchronizing */ }
    },

    // Step 1 → the delivered identity ceremony (network-onboarding.js).
    beginIdentity() {
      if (window.AutonomyOnboarding) window.AutonomyOnboarding.open({ step: 1 });
    },
    // Step 2 create → the delivered create-org screen (create-org.js).
    createOrg() {
      if (window.AutonomyCreateOrg) window.AutonomyCreateOrg.open({ entry: 'standalone' });
    },
    // Step 2 join → the delivered stepped accept flow, carrying whatever
    // invitation context the URL holds (the bearer in #t= rides along and
    // never touches this shell).
    joinOrg() {
      location.assign('/network/join' + location.search + location.hash);
    },
    // Step 3 → the session UI, where the first workspace is opened.
    goToWorkspace() {
      location.assign('/beads');
    },
  };
}
