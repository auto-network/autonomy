function fleetPage() {
  return {
    view: null,
    loading: false,
    error: null,
    expandedId: null,
    copied: false,
    bootstrapCopied: false,
    inviteBusy: false,
    invitePassword: '',
    invitationError: null,
    refreshTimer: null,

    async init() {
      await this.load();
      this.refreshTimer = setInterval(() => this.load({ quiet: true }), 5000);
    },

    destroy() {
      if (this.refreshTimer) clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    },

    async load(options) {
      const quiet = !!(options && options.quiet);
      if (!quiet) this.loading = true;
      try {
        const response = await fetch('/api/plugins/fleet/view', {
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        this.view = body;
        this.error = null;
      } catch (error) {
        this.error = (error && error.message) || String(error);
      } finally {
        this.loading = false;
      }
    },

    get summary() {
      return (this.view && this.view.summary) || {
        authorizedMachines: 0, connectedMachines: null,
        lastSuccessfulSyncAt: null, joinRequests: 0,
      };
    },

    get machines() { return (this.view && this.view.machines) || []; },

    // Count of machines whose sync outcomes read as failing (see syncTrouble),
    // so the fleet-level summary can warn without the operator opening a card.
    get failingMachines() {
      return this.machines.filter((machine) => this.syncTrouble(machine)).length;
    },
    get invitation() {
      return (this.view && this.view.invitation) || {
        status: 'none', url: null, bootstrapCode: null, publishedAt: null, expiresAt: null,
        publishingOrg: 'personal', error: null,
      };
    },
    get activity() {
      return (this.view && this.view.activity) || {
        transactionsApplied: 0, bytesSent: 0, bytesReceived: 0,
      };
    },

    get headline() {
      const authorized = this.summary.authorizedMachines;
      const requests = this.summary.joinRequests;
      let value = `${authorized} authorized ${authorized === 1 ? 'machine' : 'machines'}`;
      if (requests) value += ` · ${requests} ${requests === 1 ? 'request' : 'requests'}`;
      return value;
    },

    get publishedLabel() {
      return this.invitation.publishedAt
        ? `Published ${this.timeLabel(this.invitation.publishedAt, '')}` : '';
    },

    get expiryLabel() {
      if (!this.invitation.expiresAt) return 'No expiration';
      const remaining = this.invitation.expiresAt - Date.now();
      if (remaining <= 0) return 'Expired';
      const days = Math.ceil(remaining / 86400000);
      return `${days} ${days === 1 ? 'day' : 'days'} left`;
    },

    rowId(machine) {
      return machine.entryId || machine.sourceApprovalId || machine.machineId;
    },

    toggle(id) { this.expandedId = this.expandedId === id ? null : id; },

    reviewApproval(machine) {
      const id = machine && machine.sourceApprovalId;
      if (!id) return;
      if (typeof window.openApprovalOverlay === 'function') {
        window.openApprovalOverlay(id);
        return;
      }
      // The shared activity screen will open the same central approval when
      // the overlay bundle is not present yet (for example on a cold PWA).
      window.location.assign('/activity?focus=approval&id=' + encodeURIComponent(id));
    },

    // A machine that is failing to sync is not healthy, however good its
    // standing. "Trying and failing" must show; "not yet started" must stay
    // quiet, so a machine mid-first-sync is never marked failed. With only
    // cumulative counters the honest signal is: failures observed with no pull
    // ever completed. A machine with a recent success reads healthy even with
    // old failures behind it; a machine with no attempts at all is not failed.
    syncTrouble(machine) {
      if (!machine || machine.isLocalMachine) return false;
      if (machine.rowKind === 'pending_admission') return false;
      const failed = Number(machine.failedIterations || 0);
      if (failed <= 0) return false;
      const succeeded = Number(machine.successfulIterations || 0);
      return succeeded <= 0 || !machine.lastSuccessfulSyncAt;
    },

    machineClass(machine) {
      if (machine.standing === 'revoked' || machine.standing === 'admission_failed') return 'failed';
      if (machine.rowKind === 'pending_admission') return 'pending';
      if (this.syncTrouble(machine)) return 'warn';
      return '';
    },

    dotClass(machine) {
      if (machine.standing === 'revoked' || machine.standing === 'admission_failed') return 'failed';
      if (machine.rowKind === 'pending_admission') return 'pending';
      if (this.syncTrouble(machine)) return 'warn';
      return machine.presence === 'connected' ? 'connected' : '';
    },

    statusTone(machine) {
      if (machine.standing === 'revoked' || machine.standing === 'admission_failed') return 'failed';
      if (machine.rowKind === 'pending_admission') return 'pending';
      if (this.syncTrouble(machine)) return 'warn';
      return '';
    },

    standingLabel(standing) {
      const labels = {
        authorized: 'Authorized', revoked: 'Revoked',
        pending_approval: 'Pending approval',
        admission_in_progress: 'Adding machine',
        admission_failed: 'Admission failed',
      };
      return labels[standing] || 'Unknown';
    },

    assignmentLabel(assignment) {
      if (!assignment) return 'Not assigned';
      return String(assignment).split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
    },

    detailLabel(machine) {
      if (machine.machineId) return this.shortId(machine.machineId);
      const verb = machine.standing === 'admission_failed' ? 'failed'
        : machine.standing === 'admission_in_progress' ? 'approved' : 'received';
      return `Request ${verb} ${this.timeLabel(machine.standingChangedAt, '')}`;
    },

    syncLabel(machine) {
      if (machine.isLocalMachine) return 'Local database';
      return this.timeLabel(machine.lastSuccessfulSyncAt, 'Never');
    },

    resultLabel(machine) {
      if (machine.rowKind === 'pending_admission') {
        if (machine.standing === 'pending_approval') return 'Decision in approval inbox';
        if (machine.standing === 'admission_in_progress') return 'Authorization is being committed';
        return 'Recovery in approval inbox';
      }
      if (machine.standing === 'revoked') return 'Authorization revoked';
      if (machine.isLocalMachine) return 'Local database';
      if (machine.lastErrorCode) return machine.lastErrorCode;
      return machine.lastSuccessfulSyncAt ? 'Successful pull' : 'No successful pull observed';
    },

    shortId(value) {
      const id = String(value || '');
      return id.length > 12 ? `${id.slice(0, 6)}…${id.slice(-6)}` : id;
    },

    timeLabel(value, fallback) {
      if (!value) return fallback;
      const milliseconds = Number(value) > 1e15 ? Number(value) / 1e6 : Number(value);
      const delta = Date.now() - milliseconds;
      if (delta >= 0 && delta < 60000) return `${Math.max(1, Math.floor(delta / 1000))} sec ago`;
      if (delta >= 0 && delta < 3600000) return `${Math.floor(delta / 60000)} min ago`;
      if (delta >= 0 && delta < 86400000) return `${Math.floor(delta / 3600000)} hr ago`;
      return new Date(milliseconds).toLocaleString([], {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      });
    },

    numberLabel(value) { return Number(value || 0).toLocaleString(); },

    bytesLabel(value) {
      let bytes = Number(value || 0);
      if (bytes < 1024) return `${bytes} B`;
      const units = ['KB', 'MB', 'GB', 'TB'];
      let unit = -1;
      do { bytes /= 1024; unit += 1; } while (bytes >= 1024 && unit < units.length - 1);
      return `${bytes >= 10 ? bytes.toFixed(0) : bytes.toFixed(1)} ${units[unit]}`;
    },

    durationLabel(value) {
      const milliseconds = Math.max(0, Number(value || 0));
      if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
      const seconds = milliseconds / 1000;
      if (seconds < 60) return `${seconds >= 10 ? seconds.toFixed(0) : seconds.toFixed(1)} sec`;
      const minutes = seconds / 60;
      if (minutes < 60) return `${minutes >= 10 ? minutes.toFixed(0) : minutes.toFixed(1)} min`;
      return `${(minutes / 60).toFixed(1)} hr`;
    },

    async copyInvitation() {
      if (!this.invitation.url || !navigator.clipboard) return;
      await navigator.clipboard.writeText(this.invitation.url);
      this.copied = true;
      setTimeout(() => { this.copied = false; }, 1400);
    },

    async copyBootstrapCode() {
      if (!this.invitation.bootstrapCode || !navigator.clipboard) return;
      await navigator.clipboard.writeText(this.invitation.bootstrapCode);
      this.bootstrapCopied = true;
      setTimeout(() => { this.bootstrapCopied = false; }, 1400);
    },

    async createInvitation() {
      if (this.inviteBusy) return;
      this.inviteBusy = true;
      this.invitationError = null;
      try {
        const org = this.invitation.publishingOrg || 'personal';
        const response = await fetch('/api/approvals', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({
            kind: 'link_publish',
            session: 'Fleet invitation',
            request: {
              org,
              target_uuid: crypto.randomUUID(),
              target_type: 'fleet:join',
              meta: { ttl: 604800, label: 'Fleet machine invitation' },
            },
          }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        await this.load({ quiet: true });
      } catch (error) {
        this.invitationError = (error && error.message) || String(error);
      } finally {
        this.inviteBusy = false;
      }
    },

    async finishInvitation() {
      if (this.inviteBusy) return;
      this.inviteBusy = true;
      this.invitationError = null;
      let opened = null;
      try {
        // ONE common factor-aware unlock — password, passkey, or both, chosen
        // per the armor's own factors. No password field lives here anymore.
        const { openRoot } = await import('/static/js/ceremony/open-root.js');
        opened = await openRoot({
          title: 'Create fleet invitation',
          detail: 'Unlock your personal root to sign this invitation.',
        });
        if (!opened) { this.inviteBusy = false; return; }
        const ceremony = await import('/static/js/ceremony/fleet-enrollment.js');
        const minted = await ceremony.mintFleetInvite({
          personalRootSeed: opened.seed,
          rootPub: opened.rootPub,
          rendezvous: this.invitation.rendezvous,
          expiresAt: this.invitation.expiresAt || 0,
        });
        opened.seed = null;
        const registered = await fetch('/api/fleet/invitations/register', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({
            org: this.invitation.publishingOrg,
            target_uuid: this.invitation.targetUuid,
            grant_token: this.invitation.grantToken,
            invite: minted.invite,
          }),
        });
        const body = await registered.json().catch(() => ({}));
        if (!registered.ok) throw new Error(body.error || `HTTP ${registered.status}`);
        this.invitePassword = '';
        await this.load({ quiet: true });
      } catch (error) {
        this.invitationError = (error && error.message) || String(error);
      } finally {
        if (opened && opened.seed) {
          opened.seed.fill(0);
          opened.seed = null;
        }
        this.invitePassword = '';
        this.inviteBusy = false;
      }
    },
  };
}

if (window.Alpine) Alpine.data('fleetPage', fleetPage);
