function fleetPage() {
  return {
    view: null,
    loading: false,
    error: null,
    expandedId: null,
    copied: false,
    bootstrapCopied: false,
    inviteBusy: false,
    invitationError: null,
    refreshTimer: null,
    renamingId: null,
    renameDraft: '',
    pendingNames: {},
    confirmRemove: null,
    actionBusy: null,
    actionErrors: {},

    // ONE classifier drives every status rendering; these are its words.
    // A state with no honest server-side probe has no entry here.
    STRINGS: {
      word: {
        serving: 'Serving', locked: 'Needs unlock', tunnel: 'Tunnel down',
        cert: 'Certificate expired', restart: 'Restart needed',
        cert_expiring: 'Dashboard certificate expires soon',
        cert_expired: 'Dashboard certificate expired',
        paused: 'Paused', away: 'Disconnected', failing: 'Failing',
        first: 'Not synced yet', synced: 'Synced', idle: '',
        link_off: 'Invite link off',
        degraded: 'Some tunnels down',
        rearm: 'Re-arm failed',
        launch_failing: 'Connector not starting',
      },
      note: {
        locked: 'The serving connector for {scopes} has no key and this dashboard holds no cached credential. Unlock this dashboard to arm it.',
        rearm: 'The serving connector for {scopes} keeps exiting because its key file was never written, although this dashboard holds the credential. Run the runtime re-arm or restart the dashboard; an unlock is not needed.',
        launch_failing: 'The serving connector for {scopes} keeps exiting at launch with its key file present. Read its serve log.',
        tunnel: "Your other devices can't reach this dashboard from outside. Bringing the tunnel back needs your root key.",
        degraded: 'This dashboard still serves your fleet, but not every organization it is set up to serve.',
        first: 'This machine has joined your fleet but has not synchronized with this dashboard yet. Syncing starts automatically once it comes online.',
        link_off: 'This machine was approved, but its invitation link is not active — reactivate the link on this dashboard to finish setup and start syncing.',
        paused: 'This machine is running a different version of Autonomy. Synchronization will resume once the versions match.',
        cert_expiring: 'The certificate this dashboard serves expires in {days} days. The monthly renewal did not replace it: run tools/dashboard/renew-tls-cert.sh on this machine and read data/cert-renew.log.',
        cert_expired: 'The certificate this dashboard serves has expired. Run tools/dashboard/renew-tls-cert.sh on this machine and read data/cert-renew.log.',
        away: 'Not responding — it may be asleep or offline. Syncing resumes automatically when it comes back.',
        failing: 'Sync attempts are failing and will keep retrying. If this keeps happening, make sure both machines are up to date.',
      },
    },

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
        const body = await this._fetchJson('/api/plugins/fleet/view');
        this.view = body;
        this.error = null;
      } catch (error) {
        this.error = (error && error.message) || String(error);
      } finally {
        this.loading = false;
      }
    },

    async _fetchJson(url) {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      return body;
    },

    async _postJson(url, payload) {
      const response = await fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok || body.ok === false) {
        throw new Error(body.error || `HTTP ${response.status}`);
      }
      return body;
    },

    // ── the projection, adapted to the view state the markup binds ──
    // Roster rows carry sync counters under projection names; the render
    // vocabulary is the design's. Revoked tombstones are not machines.
    _adaptRoster(row) {
      const adapted = Object.assign({}, row, {
        displayLabel: this.pendingNames[row.machineId] || row.displayLabel,
        attemptsOk: row.successfulIterations || 0,
        // attemptsFailed is NOT re-derived from failedIterations here: the
        // projection already subtracts the operator's reset baseline from it,
        // and rebuilding it from the raw lifetime counter put that number
        // straight back on the card after a reset.
        lastOutcome: row.lastSyncOutcome || null,
      });
      if (row.isLocalMachine) {
        const local = (this.view && this.view.localMachine) || {};
        adapted.connectorArmed = local.connectorArmed;
        adapted.tunnelServing = local.tunnelServing;
        adapted.tunnelScopesDown = local.tunnelScopesDown || [];
        adapted.runningStale = local.runningStale === true;
        adapted.certStatus = local.certStatus || null;
        adapted.certValidUntil = local.certValidUntil;
        adapted.dashboardCertificate = local.dashboardCertificate || null;
      }
      return adapted;
    },

    get machines() {
      const rows = (this.view && this.view.machines) || [];
      return rows
        .filter((row) => row.rowKind === 'roster_machine' && row.standing === 'authorized')
        .map((row) => this._adaptRoster(row));
    },

    get admissions() {
      const rows = (this.view && this.view.machines) || [];
      return rows.filter((row) => row.rowKind === 'pending_admission');
    },

    get servingMachineId() {
      const serving = this.machines.find((machine) => machine.isTunnelServer);
      return serving ? serving.entryId : null;
    },

    get invitation() {
      return (this.view && this.view.invitation) || {
        status: 'none', url: null, bootstrapCode: null, targetUuid: null,
        publishedAt: null, expiresAt: null, publishingOrg: 'personal', error: null,
      };
    },

    // The invitation link exists but is NOT serving joins: it was published
    // then deactivated (or never signed), so it needs reactivation before any
    // approved-but-unsynced machine can finish. 'awaiting_signature' is exactly
    // the "Activate invitation — sign with your personal root" state. A machine
    // blocked on this reads 'link_off', never the false-reassuring 'first'.
    get inviteInactive() {
      // Either the invite was signed then deactivated ('inactive') or never
      // signed ('awaiting_signature'). Both mean the link is not serving joins,
      // so an approved-but-unsynced machine is blocked until the operator acts.
      return this.invitation.status === 'inactive'
        || this.invitation.status === 'awaiting_signature';
    },

    // A connected machine syncs at least every ~10s; minutes of silence IS
    // disconnection. Without this, a peer that stops contacting us leaves no
    // new evidence and the last stale success would render "Synced" forever.
    AWAY_AFTER_MS: 5 * 60 * 1000,

    // ── the classifier (the design's, minus states with no honest probe) ──

    // A build's committer date, formatted for humans (null if absent/invalid).
    buildLabel(iso) {
      if (!iso) return null;
      const when = new Date(iso);
      if (isNaN(when.getTime())) return null;
      return when.toLocaleDateString([], {
        year: 'numeric', month: 'short', day: 'numeric',
      });
    },

    // The note for a state; for a version mismatch ("paused") it names WHICH
    // build each side runs (the digest stays the internal comparison key), so
    // the operator sees a real "this build … / their build …", not an opaque
    // "different version".
    noteFor(id, machine) {
      let base = this.STRINGS.note[id] || '';
      if (id === 'paused') {
        const mine = this.buildLabel(this.view && this.view.localBuiltAt);
        const theirs = this.buildLabel(machine && machine.peerBuiltAt);
        if (mine || theirs) {
          base += ' This machine’s build: ' + (mine || 'unknown')
            + '; the other machine’s build: ' + (theirs || 'unknown') + '.';
        }
      }
      return base;
    },

    certExpired(machine) {
      return machine.certValidUntil != null && Date.now() > machine.certValidUntil;
    },








    admissionWord(admission) {
      if (admission.standing === 'admission_in_progress') return 'Adding machine…';
      if (admission.standing === 'admission_failed') return 'Admission failed';
      return 'Awaiting approval';
    },

    admissionLine(admission) {
      if (admission.standing === 'admission_failed') {
        return admission.lastErrorCode
          ? 'Failed: ' + admission.lastErrorCode
          : 'Open to retry from the approval record';
      }
      const when = this.timeLabel(admission.standingChangedAt, '');
      return admission.standing === 'admission_in_progress'
        ? 'Approved ' + when : 'Asked to join ' + when;
    },

    reviewApproval(admission) {
      const id = admission && admission.sourceApprovalId;
      if (!id) return;
      window.location.assign('/activity?focus=approval&id=' + encodeURIComponent(id));
    },

    // ── formatters ──





    // ── rename: edit in place, one Settings write behind a PATCH ──

    async commitRename(machine) {
      if (this.renamingId !== machine.entryId) return;
      this.renamingId = null;
      const name = this.renameDraft.trim();
      if (!name || name === machine.displayLabel) return;
      this.pendingNames = Object.assign({}, this.pendingNames, { [machine.machineId]: name });
      try {
        const response = await fetch(
          '/api/plugins/fleet/machines/' + encodeURIComponent(machine.machineId) + '/name',
          {
            method: 'PATCH', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ display_name: name }),
          },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        await this.load({ quiet: true });
      } catch (error) {
        this.actionErrors = Object.assign({}, this.actionErrors, {
          [machine.entryId]: (error && error.message) || String(error),
        });
      } finally {
        const pending = Object.assign({}, this.pendingNames);
        delete pending[machine.machineId];
        this.pendingNames = pending;
      }
    },

    // ── unlock: re-arm the serving connector with a freshly opened root,
    // the same maintenance sequence a sign-in unlock performs ──
    async beginUnlock(machine) {
      if (this.actionBusy) return;
      this.actionBusy = 'unlock';
      this.actionErrors = {};
      let opened = null;
      try {
        const { openRoot } = await import('/static/js/ceremony/open-root.js');
        opened = await openRoot({
          title: 'Unlock this dashboard',
          detail: 'Unlock your personal root to bring the serving connector back online.',
        });
        if (!opened) return;
        let rc = await this._fetchJson('/api/fleet/runtime');
        if (!rc.enabled) throw new Error('the fleet runtime is not enabled on this machine');
        if (rc.personal_org_uuid && (!rc.org_uuid || rc.serves)) {
          try {
            const signon = window.AutonomyNetworkSession && window.AutonomyNetworkSession._internals;
            if (signon) {
              await signon.provisionPersonalNetworkIdentity({
                personalRootSeed: new Uint8Array(opened.seed),
                orgUuid: rc.personal_org_uuid,
                rootPub: rc.personal_root_pub,
                serve: !!rc.serves,
              });
              rc = await this._fetchJson('/api/fleet/runtime');
            }
          } catch (error) {
            if (window.console && console.warn) {
              console.warn('personal tunnel provisioning failed:',
                (error && error.message) || error);
            }
          }
        }
        const ceremony = await import('/static/js/ceremony/fleet-enrollment.js');
        const credential = await ceremony.mintFleetRuntimeCredential({
          personalRootSeed: new Uint8Array(opened.seed),
          rootPub: rc.personal_root_pub,
          machineId: rc.machine_id,
          machinePub: rc.machine_pub,
          orgUuid: rc.org_uuid || null,
        });
        await this._postJson('/api/fleet/runtime', credential);
        await this.load({ quiet: true });
      } catch (error) {
        this.actionErrors = Object.assign({}, this.actionErrors, {
          [machine.entryId]: (error && error.message) || String(error),
        });
      } finally {
        if (opened && opened.seed) { opened.seed.fill(0); opened.seed = null; }
        this.actionBusy = null;
      }
    },

    // ── remove: root-signed KICK tombstone via the fleet-kick ceremony ──
    async beginRemove(machine) {
      if (this.actionBusy) return;
      this.actionBusy = 'remove';
      this.actionErrors = {};
      let opened = null;
      try {
        const { openRoot } = await import('/static/js/ceremony/open-root.js');
        opened = await openRoot({
          title: 'Remove ' + machine.displayLabel + ' from your fleet',
          detail: 'Unlock your personal root to sign the revocation.',
        });
        if (!opened) return;
        const kick = await import('/static/js/ceremony/fleet-kick.js');
        const signed = await kick.signFleetKick(opened, machine);
        await this._postJson('/api/fleet/machines/kick', {
          roster_entry: signed.roster_entry,
        });
        this.confirmRemove = null;
        await this.load({ quiet: true });
      } catch (error) {
        this.actionErrors = Object.assign({}, this.actionErrors, {
          [machine.entryId]: (error && error.message) || String(error),
        });
      } finally {
        if (opened && opened.seed) { opened.seed.fill(0); opened.seed = null; }
        this.actionBusy = null;
      }
    },

    // ── invitation ──
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
        // The operator publishes the invitation route from this Dashboard:
        // review, sign on confirm, publish (auto-fkhq0.10a).
        const links = await import('/static/js/components/link-central-approval.js');
        await links.operateLinkDirectly({
          op: 'publish',
          requester: 'Fleet invitation',
          request: {
            org,
            target_uuid: crypto.randomUUID(),
            target_type: 'fleet:join',
            meta: { ttl: 604800, label: 'Fleet machine invitation' },
          },
        });
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
        // per the armor's own factors.
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
        await this._postJson('/api/fleet/invitations/register', {
          org: this.invitation.publishingOrg,
          target_uuid: this.invitation.targetUuid,
          grant_token: this.invitation.grantToken,
          invite: minted.invite,
        });
        await this.load({ quiet: true });
      } catch (error) {
        this.invitationError = (error && error.message) || String(error);
      } finally {
        if (opened && opened.seed) {
          opened.seed.fill(0);
          opened.seed = null;
        }
        this.inviteBusy = false;
      }
    },

    async reactivateInvitation() {
      if (this.inviteBusy) return;
      this.inviteBusy = true;
      this.invitationError = null;
      try {
        const targetUuid = this.invitation.targetUuid;
        if (!targetUuid) throw new Error('the invitation record is missing its target');
        // Flip the SAME stored invite active again — no re-mint. A machine
        // pinned to this invitation resumes and finishes on its next attempt.
        await this._postJson('/api/fleet/invitations/reactivate', {
          target_uuid: targetUuid,
        });
        await this.load({ quiet: true });
      } catch (error) {
        this.invitationError = (error && error.message) || String(error);
      } finally {
        this.inviteBusy = false;
      }
    },

    async disableInvitation() {
      if (this.inviteBusy) return;
      this.inviteBusy = true;
      this.invitationError = null;
      try {
        const targetUuid = this.invitation.targetUuid;
        if (!targetUuid) throw new Error('the invitation record is missing its target');
        // Tear the published rendezvous route down (review, sign on confirm,
        // revoke), and stop honouring the invitation locally either way.
        const links = await import('/static/js/components/link-central-approval.js');
        const revoked = await links.operateLinkDirectly({
          op: 'revoke',
          requester: 'Fleet invitation',
          request: {
            org: this.invitation.publishingOrg || 'personal',
            target_uuid: targetUuid,
            target_type: 'fleet:join',
          },
        });
        await this._postJson('/api/fleet/invitations/deactivate', {
          target_uuid: targetUuid,
        });
        await this.load({ quiet: true });
        if (!revoked) {
          this.invitationError = 'The invitation no longer works on this machine, but its public route was not revoked.';
        }
      } catch (error) {
        this.invitationError = (error && error.message) || String(error);
      } finally {
        this.inviteBusy = false;
      }
    },

    // The design names its two invitation controls; production owns what they
    // do. An invitation that exists but is not serving joins is reactivated,
    // never re-minted -- re-minting would strand every machine holding the
    // old link.
    beginActivateInvite() {
      if (this.invitation.status === 'awaiting_signature') return this.finishInvitation();
      return this.invitation.status === 'inactive'
        ? this.reactivateInvitation()
        : this.createInvitation();
    },

    beginDisableInvite() { return this.disableInvitation(); },

    // ── view state the statistics rendering owns ──
    period: '1h',
    selectedScope: 'all',
    refreshing: false,
    copyError: '',

    get organizations() { return (this.view && this.view.organizations) || []; },
    get trafficHistory() { return (this.view && this.view.trafficHistory) || []; },
    get serverTime() { return (this.view && this.view.serverTime) || Date.now(); },

    // Clears THIS machine's counters. The optimistic zeroing keeps the button
    // instant; the reload is what makes it true.
    async resetCounters() {
      for (const machine of this.machines) {
        machine.bytesReceived = 0; machine.bytesSent = 0;
        machine.transactionsApplied = 0; machine.attemptsFailed = 0;
        for (const scope of machine.scopes || []) {
          scope.bytesIn = 0; scope.bytesOut = 0;
        }
      }
      try {
        await this._postJson('/api/plugins/fleet/counters/reset', {});
      } finally {
        await this.load({ quiet: true });
      }
    },

    // Down for SOME provisioned scopes but not personal: this dashboard still
    // serves the fleet and is unreachable for the named orgs. Warn, not
    // failed — "Tunnel down" would overstate it exactly as the old
    // personal-only "Serving" understated it.
    tunnelDegraded(machine) {
      const down = machine.tunnelScopesDown || [];
      return down.length > 0 && down.indexOf('personal') === -1;
    },
    tunnelWord(machine) {
      if (machine.tunnelServing == null) return 'Unknown';
      if (machine.tunnelServing) return 'Serving';
      return this.tunnelDegraded(machine) ? 'Degraded' : 'Down';
    },
    tunnelTone(machine) {
      if (machine.tunnelServing == null) return 'muted';
      if (machine.tunnelServing) return 'good';
      return this.tunnelDegraded(machine) ? 'warn' : 'bad';
    },
    tunnelTitle(machine) {
      const down = machine.tunnelScopesDown || [];
      return down.length ? `Not serving: ${down.join(', ')}` : '';
    },
    machineState(machine) {
      let id, tone;
      let scopes = [];
      if (machine.isLocalMachine) {
        const serving = this.servingMachineId === machine.entryId;
        // Per-scope supervisor facts (graph://1418ca10-588 section 4): an
        // UNARMED connector is "Needs unlock" ONLY when this dashboard holds
        // no cached credential either; with one present, unlocking cannot
        // help and the honest word is "Re-arm failed" naming the scope.
        const states = machine.scopeStates || [];
        const name = (s) => s.label || s.scope;
        const unarmed = states.filter((s) => s.state === 'unarmed').map(name);
        const failing = states.filter((s) => s.state === 'launch-failing').map(name);
        if (serving && unarmed.length) {
          id = machine.dashboardCredentialPresent === false ? 'locked' : 'rearm'; tone = 'failed'; scopes = unarmed;
        }
        else if (serving && failing.length) { id = 'launch_failing'; tone = 'failed'; scopes = failing; }
        // A null probe is UNKNOWN, not unarmed: `!null` read Home's warm,
        // unprobed credential as "Needs unlock" (2026-09-17).
        else if (serving && machine.connectorArmed === false) { id = 'locked'; tone = 'failed'; scopes = ['this dashboard']; }
        else if (serving && this.tunnelDegraded(machine)) { id = 'degraded'; tone = 'warn'; }
        else if (serving && !machine.tunnelServing) { id = 'tunnel'; tone = 'failed'; }
        else if (serving && machine.certValidUntil != null && Date.now() > machine.certValidUntil) { id = 'cert'; tone = 'failed'; }
        else if (machine.dashboardCertificate && machine.dashboardCertificate.daysRemaining <= 0) { id = 'cert_expired'; tone = 'failed'; }
        else if (machine.dashboardCertificate && machine.dashboardCertificate.expiring) { id = 'cert_expiring'; tone = 'warn'; }
        else if (machine.runningBuild !== machine.installedBuild) { id = 'restart'; tone = 'warn'; }
        else if (serving) { id = 'serving'; tone = 'good'; }
        else { id = 'idle'; tone = 'good'; }
      } else if (!machine.lastSuccessfulSyncAt) {
        // A machine we have never synced with is bootstrapping or absent;
        // failed attempts toward it are expected, not a connection LOST.
        // UNLESS the invitation link is not serving joins, in which case it is
        // blocked and cannot proceed without the operator — the distinction
        // inviteInactive was written for. That branch was lost with the
        // duplicate STRINGS block above (its words were unreachable), so this
        // read the false-reassuring 'Not synced yet' instead.
        const blocked = this.inviteInactive;
        id = blocked ? 'link_off' : 'first';
        tone = 'warn';
      } else if (machine.lastErrorCode === 'schema_mismatch') {
        id = 'paused'; tone = 'warn';
      } else if (machine.lastOutcome === 'failed') {
        const connection = ['connection_failed', 'dial_timeout', 'unreachable', 'relay_close']
          .includes(machine.lastErrorCode);
        id = connection ? 'away' : 'failing';
        tone = connection ? 'warn' : 'failed';
      } else if (Date.now() - machine.lastSuccessfulSyncAt > 5 * 60 * 1000) {
        // A silent peer leaves no failure evidence; staleness IS the evidence.
        id = 'away'; tone = 'warn';
      } else {
        id = 'synced'; tone = 'good';
      }
      return {
        id, tone,
        word: this.STRINGS.word[id] || '',
        note: (this.STRINGS.note[id] || '').replace('{scopes}', scopes.join(', ') || 'this dashboard')
          .replace('{days}', String(Math.max(0, Math.floor(((machine.dashboardCertificate || {}).daysRemaining) || 0)))),
      };
    },
    machineHealthy(machine) { return this.machineState(machine).tone === 'good'; },
    statusWord(machine) { return this.machineState(machine).word; },
    statusTone(machine) { const tone = this.machineState(machine).tone; return tone === 'good' ? 'good' : tone; },
    dotClass(machine) { const tone = this.machineState(machine).tone; return tone === 'good' ? 'connected' : tone; },
    machineClass(machine) { const tone = this.machineState(machine).tone; return tone === 'good' ? '' : tone; },
    localLastSync() {
      const times = this.machines.filter((machine) => !machine.isLocalMachine)
        .map((machine) => machine.lastSuccessfulSyncAt).filter(Boolean);
      return times.length ? Math.max.apply(null, times) : null;
    },
    subLine(machine) {
      if (machine.isLocalMachine) return 'This machine';
      return this.servingMachineId === machine.entryId ? 'Serves your fleet' : '';
    },
    unhealthyMachines() { return this.machines.filter((machine) => !this.machineHealthy(machine)); },
    fleetTone() {
      const unhealthy = this.unhealthyMachines();
      if (!unhealthy.length) return 'ok';
      return unhealthy.some((machine) => this.machineState(machine).tone === 'failed') ? 'err' : 'warn';
    },
    fleetStatusLine() {
      const total = this.machines.length;
      return total + (total === 1 ? ' machine' : ' machines');
    },

    // ---- proposed statistics (auto-gswbg) --------------------------
    traffic(window, transport) {
      const t = this.trafficData || {};
      let total = 0;
      for (const [tp, windows] of Object.entries(t)) {
        if (transport && tp !== transport) continue;
        total += (windows[window] || 0);
      }
      return total;
    },
    minuteSeries() { return this.minuteBytes || []; },
    rateLabel() {
      const s = this.minuteSeries();
      const last = s.length ? s[s.length - 1] : 0;
      return this.bytesLabel(Math.round(last / 60)) + '/s';
    },
    sparkline(series) {
      if (!series.length) return '';
      const max = Math.max(...series, 1);
      const bars = '▁▂▃▄▅▆▇█';
      return series.map((v) => bars[Math.min(7, Math.floor(v / max * 7))]).join('');
    },
    lagLabel(ms) {
      if (ms == null) return '—';
      // A tick, not the word: in a Lag column every other value is a
      // duration, so the one row with nothing to report should read as a
      // mark rather than as the longest string in the column.
      if (ms < 1000) return '✓';
      if (ms < 60000) return Math.round(ms / 1000) + 's';
      if (ms < 3600000) return Math.round(ms / 60000) + 'm';
      return Math.round(ms / 3600000) + 'h';
    },
    maxLag() {
      const all = this.machines.flatMap((m) => (m.scopes || []).map((r) => r.lag));
      return all.length ? Math.max(...all) : 0;
    },
    behindCount() {
      return this.machines.filter(
        (m) => (m.scopes || []).some((r) => r.lag > 300000)).length;
    },
    peerMaxLag(machine) {
      const rows = machine.scopes || [];
      return rows.length ? Math.max(...rows.map((r) => r.lag)) : 0;
    },
    scopeRows(machine) { return machine.scopes || []; },
    fleetChanges() {
      return this.machines.filter((machine) => !machine.isLocalMachine)
        .reduce((sum, machine) => sum + (machine.transactionsApplied || 0), 0);
    },
    fleetReceived() {
      return this.machines.filter((machine) => !machine.isLocalMachine)
        .reduce((sum, machine) => sum + (machine.bytesReceived || 0), 0);
    },
    fleetSent() {
      return this.machines.filter((machine) => !machine.isLocalMachine)
        .reduce((sum, machine) => sum + (machine.bytesSent || 0), 0);
    },
    timeLabel(value, fallback) {
      if (!value) return fallback;
      const ms = Number(value);
      const delta = Date.now() - ms;
      if (delta >= 0 && delta < 60000) return Math.max(1, Math.floor(delta / 1000)) + ' sec ago';
      if (delta >= 0 && delta < 3600000) return Math.floor(delta / 60000) + ' min ago';
      if (delta >= 0 && delta < 86400000) return Math.floor(delta / 3600000) + ' hr ago';
      return new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    },
    numberLabel(value) { return Number(value || 0).toLocaleString(); },
    bytesLabel(value) {
      let bytes = Number(value || 0);
      if (bytes < 1024) return bytes + ' B';
      const units = ['KB', 'MB', 'GB', 'TB']; let unit = -1;
      do { bytes /= 1024; unit += 1; } while (bytes >= 1024 && unit < units.length - 1);
      return (bytes >= 10 ? bytes.toFixed(0) : bytes.toFixed(1)) + ' ' + units[unit];
    },
    durationLabel(value) {
      const ms = Math.max(0, Number(value || 0));
      if (ms < 1000) return Math.round(ms) + ' ms';
      const s = ms / 1000; if (s < 60) return (s >= 10 ? s.toFixed(0) : s.toFixed(1)) + ' sec';
      const m = s / 60; if (m < 60) return (m >= 10 ? m.toFixed(0) : m.toFixed(1)) + ' min';
      return (m / 60).toFixed(1) + ' hr';
    },
    remainingLabel(value) {
      const remaining = Number(value) - Date.now();
      if (remaining <= 0) return 'Expired';
      const days = Math.ceil(remaining / 86400000);
      if (days >= 2) return 'Expires in ' + days + ' days';
      const hours = Math.ceil(remaining / 3600000);
      return 'Expires in ' + hours + (hours === 1 ? ' hour' : ' hours');
    },
    expiryLabel() {
      if (!this.invitation.expiresAt) return 'No expiration';
      const remaining = this.invitation.expiresAt - Date.now();
      if (remaining <= 0) return 'Expired';
      const days = Math.ceil(remaining / 86400000);
      return 'Expires in ' + days + (days === 1 ? ' day' : ' days');
    },
    orderedMachines() {
      return this.machines.slice().sort((a, b) => (b.isLocalMachine ? 1 : 0) - (a.isLocalMachine ? 1 : 0));
    },
    beginRename(machine) {
      this.renamingId = machine.entryId;
      this.renameDraft = machine.displayLabel;
    },
    period:'1h',selectedScope:'all',refreshing:false,copyError:'',
    orgIdentity(slug){return this.organizations.find(org=>org.slug===slug);},
    orgLabelFit(){return {observer:null,frame:null,fit(){const el=this.$el,width=el.clientWidth;if(!width)return;const style=getComputedStyle(el),ctx=document.createElement('canvas').getContext('2d');ctx.font=style.fontWeight+' 13px '+style.fontFamily;const measured=ctx.measureText(el.textContent).width;el.style.fontSize=Math.min(13,13*Math.max(1,width-1)/Math.max(1,measured))+'px';},init(){this.observer=new ResizeObserver(()=>{cancelAnimationFrame(this.frame);this.frame=requestAnimationFrame(()=>this.fit());});this.observer.observe(this.$el);this.$nextTick(()=>this.fit());},destroy(){this.observer?.disconnect();cancelAnimationFrame(this.frame);}};},
    pickerOptions(){return {orgs:this.organizations,value:this.selectedScope==='all'?'':this.selectedScope,allowAll:true,allLabel:'All organizations',onChange:slug=>{this.selectedScope=slug||'all';}};},
    allScopes(){return [...new Set(this.machines.flatMap(m=>(m.scopes||[]).map(s=>s.scope)))];},
    periodLabel(){return {'1h':'1 hour','24h':'24 hours'}[this.period];},
    currentScopes(m){return this.scopeRows(m).filter(s=>s.lag<=300000).length;},
    cardStatus(m){if(!this.machineHealthy(m))return this.statusWord(m);return this.peerMaxLag(m)>300000?'Behind':'Up to date';},
    cardTone(m){if(!this.machineHealthy(m))return this.statusTone(m)==='failed'?'bad':'warn';return this.peerMaxLag(m)>300000?'warn':'good';},
    shortSync(m){const t=m.isLocalMachine?this.localLastSync():m.lastSuccessfulSyncAt;if(!t)return '—';const secs=Math.max(1,Math.round((Date.now()-t)/1000));return secs<60?secs+'s ago':secs<3600?Math.floor(secs/60)+'m ago':Math.floor(secs/3600)+'h ago';},
    detailHeadline(m){return this.peerMaxLag(m)>300000?this.lagLabel(this.peerMaxLag(m))+' behind on '+this.scopeRows(m).filter(s=>s.lag>300000).map(s=>s.scope).join(', '):this.cardStatus(m);},
    detailReason(m){return this.machineState(m).note || 'Other organizations are up to date. Last sync '+this.shortSync(m)+'.';},
    selectedHistory(){const h=this.trafficHistory||[];return h.filter(r=>this.selectedScope==='all'||r.scope===this.selectedScope);},

    bucketCount(){return {'1h':60,'24h':24}[this.period];},
    bucketSeconds(){return this.period==='24h'?3600:60;},
    bucketEpoch(){return Math.floor(this.serverTime/(this.bucketSeconds()*1000));},
    bucketDurations(){const unit=this.bucketSeconds(),count=this.bucketCount();return Array.from({length:count},(_,i)=>i===count-1?Math.max(.001,(this.serverTime-this.bucketEpoch()*unit*1000)/1000):unit);},
    chartSeries(transport,direction){
     const count=this.bucketCount(),unit=this.bucketSeconds(),current=this.bucketEpoch();
     const sums=Array(count).fill(0),size=unit===3600?24:60;
     for(const row of this.selectedHistory()){
      if(transport&&row.transport!==transport)continue;
      if(direction&&row.direction!==direction)continue;
      const bytes=unit===3600?row.hour_bytes:row.minute_bytes;
      const stamps=unit===3600?row.hour_stamp:row.minute_stamp;
      for(let i=0;i<count;i++){const epoch=current-count+1+i,slot=epoch%size;
       if(stamps?.[slot]===epoch)sums[i]+=bytes?.[slot]||0;
      }
     }
     return sums;
    },
    directionTotal(direction){return this.chartSeries(null,direction).reduce((a,b)=>a+b,0);},
    rateSeries(direction){const durations=this.bucketDurations();return this.chartSeries(null,direction).map((bytes,i)=>bytes/durations[i]);},
    chartMax(){return Math.max(1024,Math.ceil(Math.max(...this.rateSeries('received'),...this.rateSeries('sent'),0)/1024)*1024);},
    chartSvg(){
     const colors={received:'#7bb8ff',sent:'#61ddb0'},count=this.bucketCount(),durations=this.bucketDurations(),total=durations.reduce((a,b)=>a+b,0),edges=[0];durations.forEach(d=>edges.push(edges[edges.length-1]+d/total*320));
     let out='<svg viewBox="0 0 320 88" preserveAspectRatio="none" data-bucket-count="'+count+'" aria-hidden="true">';
     out+='<rect x="'+edges[count-1]+'" y="0" width="'+(320-edges[count-1])+'" height="88" fill="#ffffff06"/>';
     for(const y of [0,29,58,87])out+='<line x1="0" x2="320" y1="'+y+'" y2="'+y+'" stroke="#ffffff13"/>';
     for(const direction of ['received','sent']){
      const rates=this.rateSeries(direction),points=[];
      rates.forEach((rate,i)=>{const y=(87-rate/this.chartMax()*85).toFixed(2);points.push(edges[i].toFixed(2)+','+y,edges[i+1].toFixed(2)+','+y);});
      out+='<polyline points="'+points.join(' ')+'" data-series="'+direction+'" data-sample-count="'+rates.length+'" stroke="'+colors[direction]+'" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" fill="none"/>';
     }
     return out+'</svg>';
    },
    windowTraffic(tp){return this.chartSeries(tp).reduce((a,b)=>a+b,0);},
    averageRate(){return Math.round(this.windowTraffic()/this.bucketDurations().reduce((a,b)=>a+b,0));},
    axisStart(){const ms=(this.bucketEpoch()-this.bucketCount()+1)*this.bucketSeconds()*1000;return new Date(ms).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true});},
    axisEnd(){return new Date(this.serverTime).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true});},
    directPercent(){const all=this.windowTraffic();return all?Math.round(this.windowTraffic('direct')/all*100):0;},
    refreshView(){this.refreshing=true;setTimeout(()=>{this.refreshing=false;},450);},
    async copyValue(text,kind){try{await navigator.clipboard.writeText(text);this.copyError='';if(kind==='link')this.copied=true;else this.bootstrapCopied=true;}catch(e){this.copyError='Select and copy the invitation link above.';}}
  };
}

if (window.Alpine) Alpine.data('fleetPage', fleetPage);

// ── Remote access (bead auto-fnj20, design 58b3dd5b) ──────────────────────
//
// One control per machine card: how the machine is reached (Autonomy
// Network / Tailscale / Local only), where a relay publication stands
// (certificate, route, passkey gate), the enrolled gate passkeys with
// Revoke, and Enrol a passkey (a one-use link and its QR). For this
// machine the control speaks to the local routes; for another machine of
// the fleet the same routes carry `machine`, and that machine's dashboard
// performs the operation over session-control. Through the gated relay
// route the control is read-only: those operations are refused there by
// design, so no button is offered.
function remoteAccessControl(machine) {
  const isLocal = !!(machine && machine.isLocalMachine);
  const machineParam = isLocal ? '' : String((machine && machine.machinePublicKey) || '');
  return {
    machine: machine || {},
    status: null,          // GET /api/network/remote-access/status
    enrolment: null,       // {url, expires_at} while the link is shown here
    loading: false,
    busy: null,            // 'open' | 'close' | 'revoke' | 'publish'
    error: null,
    choosing: false,
    draft: 'local',
    confirmRevoke: null,
    copied: false,
    qrSvg: '',
    qrFor: '',
    _timer: null,
    _scripts: {},

    isLocal,
    machineParam,

    // ── polling while the card is expanded ──
    start() {
      if (this._timer) return;
      this.refresh();
      this._timer = setInterval(() => this.refresh({ quiet: true }), 5000);
    },
    stop() {
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
    },
    destroy() { this.stop(); },

    _url(path, params) {
      const query = Object.assign({}, params || {});
      if (machineParam) query.machine = machineParam;
      const keys = Object.keys(query);
      return path + (keys.length ? '?' + keys.map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(query[k])).join('&') : '');
    },
    async _call(method, path, body) {
      const init = { method, headers: { 'Accept': 'application/json' }, cache: 'no-store' };
      if (body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(machineParam ? Object.assign({ machine: machineParam }, body) : body);
      }
      const response = await fetch(method === 'GET' || method === 'DELETE' ? this._url(path) : path, init);
      let payload = {};
      try { payload = await response.json(); } catch (e) { payload = {}; }
      if (!response.ok || payload.ok === false) {
        const err = new Error(payload.error || payload.detail || ('HTTP ' + response.status));
        err.code = payload.error || null;
        throw err;
      }
      return payload;
    },

    async refresh(options) {
      const quiet = !!(options && options.quiet);
      if (!quiet) this.loading = true;
      try {
        const payload = await this._call('GET', '/api/network/remote-access/status');
        this.status = payload.status || null;
        this.error = null;
        if (!this.choosing) this.draft = (this.status && this.status.mode) || 'local';
        // The status carries the open enrollment link for a local caller:
        // show it (a reload must not lose a link that is still good).
        if (this.status && this.status.enrollment === 'open' && this.status.enrollment_url) {
          if (!this.enrolment || this.enrolment.url !== this.status.enrollment_url) {
            this.enrolment = { url: this.status.enrollment_url, expires_at: this.status.enrollment_expires_at || null };
          }
        } else if (this.status && this.status.enrollment !== 'open') {
          this.enrolment = null;
        }
        this.renderQr();
      } catch (error) {
        this.error = error.code === 'through_gateway' ? null : ((error && error.message) || String(error));
      } finally {
        this.loading = false;
      }
    },

    // ── derivations (pure; the same rules welcome.js uses for the reach step) ──
    get passkeys() { return (this.status && this.status.gate_passkeys) || []; },
    // Viewed at the relay address itself: the recovery operations are refused
    // through the gate by design, so the control offers none of them.
    get readOnly() {
      const origin = this.status && this.status.mode === 'autonomy' && this.status.origin;
      if (!origin || typeof window === 'undefined' || !window.location) return false;
      return origin.replace(/^https?:\/\//, '') === window.location.host;
    },
    live() {
      const st = this.status || {};
      return !!(st.advertised && st.gate === 'up' && st.gateway_state === 'healthy');
    },
    mode() { return (this.status && this.status.mode) || null; },
    modeWord() {
      return { autonomy: 'Autonomy Network', tailscale: 'Tailscale', local: 'Local only' }[this.mode()] || 'Not set';
    },
    statusWord() {
      if (!this.status) return this.error ? 'Unavailable' : '…';
      if (this.mode() !== 'autonomy') return this.modeWord();
      if (this.live()) return this.passkeys.length ? 'Reachable' : 'Reachable · no passkey';
      return this.status.certificate === 'failed' ? 'Certificate failed' : 'Setting up';
    },
    tone() {
      if (!this.status) return this.error ? 'warn' : 'muted';
      if (this.mode() !== 'autonomy') return '';
      if (this.status.certificate === 'failed') return 'bad';
      return this.live() && this.passkeys.length ? 'good' : 'warn';
    },
    certWord() { return { ok: 'Issued', retrying: 'Retrying', pending: 'Pending', failed: 'Failed' }[this.status && this.status.certificate] || '—'; },
    certTone() { return { ok: 'good', retrying: 'warn', pending: 'muted', failed: 'bad' }[this.status && this.status.certificate] || 'muted'; },
    routeWord() {
      if (this.live()) return 'Live';
      const st = this.status || {};
      return st.advertised ? 'Advertised · gateway ' + (st.gateway_state || '—') : 'Not yet';
    },
    routeTone() { return this.live() ? 'good' : 'warn'; },
    progress() {
      const st = this.status || {};
      return [
        { name: 'certificate', label: st.certificate === 'retrying' ? 'Certificate (retrying)' : 'Certificate', done: st.certificate === 'ok' },
        { name: 'route', label: 'Route on the relay', done: !!st.advertised },
        { name: 'gate', label: 'Passkey gate', done: st.gate === 'up' },
      ].map((r) => ({ name: r.name, label: r.label, tone: r.done ? 'ok' : '', mark: r.done ? '✓' : '…' }));
    },
    showProgress() {
      return this.mode() === 'autonomy' && !this.live() && !!this.status && this.status.certificate !== 'failed';
    },
    minutesLeft() {
      if (!this.enrolment || !this.enrolment.expires_at) return null;
      return Math.max(0, Math.round((Number(this.enrolment.expires_at) * 1000 - Date.now()) / 60000));
    },
    enrolLine() {
      const minutes = this.minutesLeft();
      return 'Open this on the device to enrol · ' + (minutes == null ? 'one use' : 'expires in ' + minutes + ' min · one use');
    },
    passkeyLabel(p) {
      const transports = (p && p.transports) || [];
      const kind = transports.indexOf('internal') >= 0 ? 'Device passkey'
        : (transports.indexOf('hybrid') >= 0 || transports.indexOf('cable') >= 0) ? 'Phone passkey'
        : (transports.indexOf('usb') >= 0 || transports.indexOf('nfc') >= 0) ? 'Security key' : 'Passkey';
      return kind;
    },
    passkeyWhen(p) {
      const when = p && p.created_at ? new Date(p.created_at) : null;
      if (!when || isNaN(when.getTime())) return '';
      return 'enrolled ' + when.toLocaleDateString([], { month: 'short', day: 'numeric' });
    },
    canEnrol() { return this.mode() === 'autonomy' && !this.enrolment && !this.readOnly && !this.busy; },
    canChooseMode() { return isLocal && !this.readOnly && !!this.status && !this.busy; },

    // ── actions ──
    async openEnrolment() {
      this.busy = 'open';
      try {
        const reply = await this._call('POST', '/api/network/remote-access/enrollment/open', {});
        this.enrolment = { url: reply.enrollment_url, expires_at: reply.expires_at || null };
        this.error = null;
        this.renderQr();
        this.refresh({ quiet: true });
      } catch (error) {
        this.error = (error && error.message) || String(error);
      } finally { this.busy = null; }
    },
    async closeEnrolment() {
      this.busy = 'close';
      try {
        await this._call('POST', '/api/network/remote-access/enrollment/close', {});
        this.enrolment = null;
        this.error = null;
        this.refresh({ quiet: true });
      } catch (error) {
        this.error = (error && error.message) || String(error);
      } finally { this.busy = null; }
    },
    async revoke(p) {
      this.busy = 'revoke';
      try {
        await this._call('DELETE', '/api/network/remote-access/gate/passkeys/' + encodeURIComponent(p.credential_id));
        this.confirmRevoke = null;
        this.error = null;
        if (this.status) this.status.gate_passkeys = this.passkeys.filter((x) => x.credential_id !== p.credential_id);
        this.refresh({ quiet: true });
      } catch (error) {
        this.error = (error && error.message) || String(error);
      } finally { this.busy = null; }
    },
    async applyMode() {
      if (!this.canChooseMode() || this.draft === this.mode()) { this.choosing = false; return; }
      this.busy = 'publish';
      try {
        await this._call('POST', '/api/network/remote-access/publish', { mode: this.draft });
        this.choosing = false;
        this.error = null;
        if (typeof window !== 'undefined' && window.dispatchEvent) {
          window.dispatchEvent(new Event('autonomy:remote-access-changed'));
        }
        await this.refresh({ quiet: true });
      } catch (error) {
        this.error = (error && error.message) || String(error);
      } finally { this.busy = null; }
    },
    cancelChoice() { this.choosing = false; this.draft = this.mode() || 'local'; },
    copy(text) {
      const clip = (typeof navigator !== 'undefined' && navigator.clipboard) ? navigator.clipboard.writeText(text) : Promise.resolve();
      clip.then(() => { this.copied = true; setTimeout(() => { this.copied = false; }, 1500); }).catch(() => {});
    },

    // ── the QR of the enrolment link (display only; logic never depends on it) ──
    _loadScript(src) {
      if (typeof document === 'undefined') return Promise.reject(new Error('no document'));
      if (!this._scripts[src]) {
        this._scripts[src] = new Promise((ok, fail) => {
          const el = document.createElement('script');
          el.src = src; el.async = true;
          el.onload = ok; el.onerror = () => fail(new Error('failed to load ' + src));
          setTimeout(() => fail(new Error('timed out loading ' + src)), 4000);
          document.head.appendChild(el);
        });
      }
      return this._scripts[src];
    },
    async renderQr() {
      const url = this.enrolment ? this.enrolment.url : '';
      if (url === this.qrFor) return;
      this.qrFor = url; this.qrSvg = '';
      if (!url) return;
      try {
        if (typeof window === 'undefined' || !window.qrcode) {
          await this._loadScript('/static/vendor/qrcode-generator-1.4.4.min.js');
        }
        if (this.qrFor !== url) return;
        const qr = window.qrcode(0, 'M');
        qr.addData(url); qr.make();
        this.qrSvg = qr.createSvgTag({ cellSize: 3, margin: 2, scalable: true });
      } catch (e) { this.qrSvg = ''; }
    },
  };
}
