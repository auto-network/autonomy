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
        paused: 'Paused', away: 'Disconnected', failing: 'Failing',
        first: 'Not synced yet', synced: 'Synced', idle: '',
        link_off: 'Invite link off',
        degraded: 'Some tunnels down',
      },
      note: {
        tunnel: "Your other devices can't reach this dashboard from outside. Bringing the tunnel back needs your root key.",
        degraded: 'This dashboard still serves your fleet, but not every organization it is set up to serve.',
        first: 'This machine has joined your fleet but has not synchronized with this dashboard yet. Syncing starts automatically once it comes online.',
        link_off: 'This machine was approved, but its invitation link is not active — reactivate the link on this dashboard to finish setup and start syncing.',
        paused: 'This machine is running a different version of Autonomy. Synchronization will resume once the versions match.',
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
        attemptsFailed: row.failedIterations || 0,
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
      if (typeof window.openApprovalOverlay === 'function') {
        window.openApprovalOverlay(id);
        return;
      }
      // The shared activity screen opens the same central approval when the
      // overlay bundle is not present yet (for example on a cold PWA).
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
        await this._postJson('/api/approvals', {
          kind: 'link_publish',
          session: 'Fleet invitation',
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
        // Tear the published rendezvous route down through the approvals
        // rendezvous, and stop honouring the invitation locally right away.
        await this._postJson('/api/approvals', {
          kind: 'link_revoke',
          session: 'Fleet invitation',
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
      if (machine.isLocalMachine) {
        const serving = this.servingMachineId === machine.entryId;
        if (serving && !machine.connectorArmed) { id = 'locked'; tone = 'failed'; }
        else if (serving && this.tunnelDegraded(machine)) { id = 'degraded'; tone = 'warn'; }
        else if (serving && !machine.tunnelServing) { id = 'tunnel'; tone = 'failed'; }
        else if (serving && machine.certValidUntil != null && Date.now() > machine.certValidUntil) { id = 'cert'; tone = 'failed'; }
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
        note: this.STRINGS.note[id] || '',
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
