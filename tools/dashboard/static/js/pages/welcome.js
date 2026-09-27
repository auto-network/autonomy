// The welcome rail's Alpine component (design graph://9a4219b3). Markup lives
// in templates/pages/welcome.html and is injected by the shell router; this
// file only sequences the delivered onboarding, create-org and join flows.
// When the sign-in scan finds nothing usable, say where it looked and what
// to do, without naming commands that do not run inside a Compose node.
var HARNESS_LABELS = { claude: 'Claude', codex: 'Codex', grok: 'Grok' };
function noSignInMessage(found) {
  var home = found && found.home;
  return 'No Claude, Codex or Grok sign-in was found' +
    (home ? ' in ' + home : ' on this machine') +
    '. Sign in to one of them on this computer, then press Go to your workspace again.';
}
function noSignInDetails(found) {
  var home = found && found.home;
  return ((found && found.harnesses) || []).map(function (h) {
    var why = h.detail || h.status || 'not found';
    // The scan runs in the dashboard container, where the operator's home is
    // mounted at /host-home; show the path the operator knows.
    if (home) why = why.split('/host-home').join(home);
    return (HARNESS_LABELS[h.harness] || h.harness) + ': ' + why;
  });
}

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
    // Step 2 — how the operator reaches this dashboard (auto-1zjk8). The
    // recorded row (GET /api/network/remote-access/status) decides whether
    // the step is done; every transition below is an API result.
    reach: null,               // the status payload once recorded
    reachChoice: 'autonomy',   // Autonomy Network is the default
    reachLabel: '',
    reachLabelCheck: null,     // last label/check result
    reachLabelBound: '',       // the persona's permanent label, when it has one
    reachOrigin: '',           // Tailscale: the Tailnet address the operator names
    reachBusy: false,
    reachError: '',
    reachStatus: null,         // live status while the relay publish converges
    reachTimer: null,
    reachLabelTimer: null,
    reachPolls: 0,             // polls so far: 2 s for two minutes, then 10 s, then stops at the gate
    reachPollingStopped: false,
    // Software updates (design c35fe726; operator decision D8): check and
    // notify, on by default; install automatically, off by default and only
    // offered while checking is on. Read from / saved to the operator's
    // preference (GET / PUT /api/software/preference).
    updatePref: { auto_check: true, auto_install: false },
    updatePrefSaving: false,
    updatePrefError: '',

    get step() {
      if (!this.hasIdentity) return 1;
      if (!(this.reach && this.reach.recorded)) return 2;
      if (!this.hasOrg) return 3;
      return 4;
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
      this.loadUpdatePref();
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
      if (this.hasIdentity) await this.refreshReach();
      this.ready = true;
    },

    // ── step 2: reach this dashboard from anywhere ───────────────────
    async refreshReach() {
      try {
        var response = await fetch('/api/network/remote-access/status', { cache: 'no-store' });
        var body = await response.json().catch(function () { return {}; });
        if (response.ok && body.status) {
          this.reach = body.status;
          this.reachStatus = body.status;
        }
      } catch (e) { /* unreadable → the step stays current; the operator chooses */ }
      if (this.reach && !this.reach.recorded) {
        // Not recorded yet: the status carries what the question needs.
        this.reachLabelBound = this.reach.bound_label || '';
        if (!this.reachOrigin && this.reach.tailnet_origin) this.reachOrigin = this.reach.tailnet_origin;
      }
      if (this.reach && this.reach.recorded && this.reach.mode === 'autonomy' && !this.reachLive()) this.startReachPolling();
    },
    destroy() {
      // Alpine calls this when the component leaves the page: no interval
      // may outlive the welcome rail.
      if (this.reachTimer) clearInterval(this.reachTimer);
      this.reachTimer = null;
      if (this.reachLabelTimer) clearTimeout(this.reachLabelTimer);
      if (this.fleetSyncTimer) clearInterval(this.fleetSyncTimer);
      if (this.fleetResumeTimer) clearInterval(this.fleetResumeTimer);
    },
    reachCanSubmit() {
      if (this.reachChoice === 'autonomy') {
        var label = (this.reachLabel || '').trim();
        return !label || (this.reachLabelCheck && this.reachLabelCheck.ok && this.reachLabelCheck.label === label.toLowerCase());
      }
      if (this.reachChoice === 'tailscale') return /^https?:\/\/[^\s/]+$/.test((this.reachOrigin || '').trim());
      return true;
    },
    reachSubmitText() {
      if (this.reachBusy) return 'Publishing…';
      return { autonomy: 'Publish on the Autonomy Network', tailscale: 'Use Tailscale', local: 'Keep it local' }[this.reachChoice] || 'Continue';
    },
    reachLabelText() {
      var label = (this.reachLabel || '').trim();
      if (!label) return '';
      if (!this.reachLabelCheck) return 'Checking…';
      if (this.reachLabelCheck.ok) return 'Available: ' + this.reachLabelCheck.label;
      return this.reachLabelCheck.reason || 'Not available.';
    },
    checkReachLabel() {
      var self = this;
      var label = (this.reachLabel || '').trim();
      this.reachLabelCheck = null;
      if (this.reachLabelTimer) clearTimeout(this.reachLabelTimer);
      if (!label) return;
      this.reachLabelTimer = setTimeout(async function () {
        try {
          var response = await fetch('/api/network/remote-access/label/check?label=' + encodeURIComponent(label), { cache: 'no-store' });
          var body = await response.json().catch(function () { return {}; });
          if ((self.reachLabel || '').trim() !== label) return;   // superseded
          self.reachLabelCheck = (body && body.label) || { ok: false, reason: 'The label could not be checked.' };
        } catch (e) {
          self.reachLabelCheck = { ok: false, reason: 'The label could not be checked right now.' };
        }
      }, 250);
    },
    async submitReach() {
      if (this.reachBusy || !this.reachCanSubmit()) return;
      this.reachBusy = true;
      this.reachError = '';
      var body = { mode: this.reachChoice };
      if (this.reachChoice === 'autonomy' && (this.reachLabel || '').trim()) body.label = this.reachLabel.trim().toLowerCase();
      if (this.reachChoice === 'tailscale') body.origin = this.reachOrigin.trim();
      try {
        var response = await fetch('/api/network/remote-access/publish', {
          method: 'POST', cache: 'no-store',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        var payload = await response.json().catch(function () { return {}; });
        if (!response.ok || payload.ok === false) {
          throw new Error(this.reachErrorText(payload.error, payload.detail, response.status));
        }
        this.reach = Object.assign({ recorded: true }, payload.remote_access);
        this.reachStatus = null;
        if (this.reachChoice === 'autonomy') this.startReachPolling();
        window.dispatchEvent(new Event('autonomy:remote-access-changed'));
      } catch (error) {
        this.reachError = (error && error.message) || String(error);
      } finally {
        this.reachBusy = false;
      }
    },
    reachErrorText(code, detail, status) {
      var texts = {
        origin_invalid: 'That address does not fit the choice: a Tailscale address ends in .ts.net, a local one is this machine.',
        origin_not_this_node: 'That is not this node\u2019s Tailnet name.',
        through_gateway: 'Choose this from the dashboard itself, not through its published address.',
        dashboard_container_unavailable: 'This dashboard is not running as a node container, so it cannot be published yet.',
        persona_not_configured: 'Create your identity first.',
        invalid_app_label: 'That name is not a valid address part.',
      };
      if (code && code.indexOf('label_') === 0) return detail || 'That name cannot be used.';
      if (code === 'origin_not_this_node' && detail) return texts[code] + ' ' + detail + '.';
      return texts[code] || (code ? code.replace(/_/g, ' ') : ('The request failed (' + status + ').'));
    },
    reachLive() {
      var st = this.reachStatus || this.reach || {};
      return !!(st.advertised && st.gate === 'up');
    },
    startReachPolling() {
      var self = this;
      if (this.reachTimer) return;
      this.reachPollingStopped = false;
      this.reachPolls = 0;
      this.reachTimer = setInterval(function () { self.pollReach(); }, 2000);
      this.pollReach();
    },
    stopReachPolling(stopped) {
      if (this.reachTimer) clearInterval(this.reachTimer);
      this.reachTimer = null;
      this.reachPollingStopped = !!stopped;
    },
    // The status polls while the publish converges: every 2 s for two
    // minutes (the note's Q3 budget), then every 10 s, and it STOPS once the
    // route is advertised with the gate still pending, the state that waits on
    // the passkey helper, not on time. "Check again" polls once more.
    async pollReach() {
      this.reachPolls += 1;
      if (this.reachPolls === 60 && this.reachTimer) {
        var self = this;
        clearInterval(this.reachTimer);
        this.reachTimer = setInterval(function () { self.pollReach(); }, 10000);
      }
      try {
        var response = await fetch('/api/network/remote-access/status', { cache: 'no-store' });
        var body = await response.json().catch(function () { return {}; });
        if (!response.ok || !body.status) return;
        this.reachStatus = body.status;
        if (body.status.enrollment_url && this.reachLive()) {
          // The gate is up: enrol the gate passkey on the live address.
          this.stopReachPolling(false);
          location.assign(body.status.enrollment_url);
          return;
        }
        // A certificate attempt that the manager will retry (its first try
        // often races the connector) is not a stop: keep polling.
        if (body.status.mode !== 'autonomy' || this.reachLive() ||
            (body.status.advertised && body.status.gate !== 'up') ||
            body.status.certificate === 'failed') {
          this.stopReachPolling(true);
        }
      } catch (e) { /* keep polling; the next tick may answer */ }
    },
    checkReachAgain() {
      this.pollReach();
    },
    reachProgress() {
      var st = this.reachStatus || {};
      var failed = st.failed_stage || '';
      var rows = [
        { name: 'reservation', label: 'Address reserved', done: !!st.reservation_id, bad: failed === 'reservation' },
        { name: 'certificate', label: st.certificate === 'retrying' ? 'Certificate issued (retrying)' : 'Certificate issued',
          done: st.certificate === 'ok', bad: st.certificate === 'failed' },
        { name: 'route', label: 'Route live on the relay', done: !!st.advertised, bad: false },
        { name: 'gate', label: 'Passkey gate ready', done: st.gate === 'up', bad: false },
      ];
      return rows.map(function (row) {
        return { name: row.name, label: row.label, tone: row.bad ? 'bad' : (row.done ? 'ok' : ''),
                 mark: row.bad ? '✗' : (row.done ? '✓' : '…') };
      });
    },
    reachTitle() {
      var mode = (this.reach || {}).mode;
      return { autonomy: 'Reachable on the Autonomy Network', tailscale: 'Reachable on your Tailnet', local: 'This machine only' }[mode] || 'Reach this dashboard';
    },
    reachSummary() {
      var st = this.reachStatus || this.reach || {};
      if (!st.origin) return '';
      if (st.mode === 'tailscale') return st.origin + (st.origin_verified === false ? ' — not confirmed by this node\u2019s certificate.' : '');
      if (st.mode !== 'autonomy') return st.origin;
      if (this.reachLive()) return st.origin;
      if (st.certificate === 'failed') return st.origin + ' — certificate issuance failed; see Published Links.';
      if (st.certificate === 'retrying') return st.origin + ' — setting up (certificate; the last attempt failed, retrying)…';
      if (st.gate !== 'up' && st.advertised) return st.origin + ' — waiting for the passkey gate.';
      return st.origin + ' — setting up (' + (st.certificate === 'ok' ? 'route' : 'certificate') + ')…';
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
    // Step 3 finishes onboarding: it starts the first session in the
    // Getting Started workspace through the ordinary session-create path
    // and lands on it (design of record graph://5f2f5a49-00d v11 §10.6).
    // This step runs once, so nothing records that the session started.
    async loadUpdatePref() {
      try {
        var res = await fetch('/api/software/preference', { cache: 'no-store' });
        if (!res.ok) return;
        var body = await res.json();
        this.updatePref = { auto_check: !!body.auto_check, auto_install: !!body.auto_install };
      } catch (e) { /* the defaults shown stay the server's defaults */ }
    },

    async saveUpdatePref(next) {
      var before = Object.assign({}, this.updatePref);
      this.updatePref = Object.assign({}, this.updatePref, next);
      this.updatePrefSaving = true;
      this.updatePrefError = '';
      try {
        var res = await fetch('/api/software/preference', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin', body: JSON.stringify(next),
        });
        if (!res.ok) {
          throw new Error('Could not save the update preference (HTTP ' + res.status
            + '). Your previous choice stays in effect.');
        }
        var body = await res.json();
        this.updatePref = { auto_check: !!body.auto_check, auto_install: !!body.auto_install };
      } catch (e) {
        this.updatePref = before;
        this.updatePrefError = (e && e.message) || String(e);
      } finally {
        this.updatePrefSaving = false;
      }
    },

    startError: '',
    // One line per harness when the sign-in scan found nothing usable: what
    // was looked for and why it cannot be used, from the scan's own report.
    startDetails: [],
    startBusy: false,
    async goToWorkspace() {
      if (this.startBusy) return;
      this.startBusy = true;
      this.startError = '';
      this.startDetails = [];
      try {
        // The first step in Getting Started is the harness sign-in
        // (record v12 FR7a): scan this machine's well-known locations,
        // import what is there, and start with it. Nothing found means the
        // session cannot run inference, so it is not started.
        var scan = await fetch('/api/plugins/getting_started/harnesses/import', {
          method: 'POST', credentials: 'same-origin',
        });
        var found = await scan.json().catch(function () { return {}; });
        var usable = (found && found.usable) || [];
        if (!scan.ok || !usable.length) {
          if (scan.ok) this.startDetails = noSignInDetails(found);
          throw new Error(scan.ok
            ? noSignInMessage(found)
            : 'Could not check this machine for a harness sign-in (HTTP ' + scan.status + ').');
        }
        var response = await fetch('/api/session/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ project: 'getting-started', harness: usable[0] }),
        });
        var body = await response.json().catch(function () { return {}; });
        if (!response.ok || !body.tmux_name) {
          throw new Error(body.error || ('Could not start the Getting Started session (HTTP ' + response.status + ').'));
        }
        location.assign('/session/' + encodeURIComponent(body.tmux_name));
      } catch (e) {
        this.startError = e.message || 'Could not start the Getting Started session.';
        this.startBusy = false;
      }
    },
  };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { noSignInMessage: noSignInMessage, noSignInDetails: noSignInDetails };
}
