(function () {
  'use strict';

  const MAX_PAGES = 20;
  const PAGE_SIZE = 100;
  const REFRESH_DELAY_MS = 80;
  const APP_META = {
    worktrees: { label: 'Worktrees', glyph: 'W', tone: 'violet' },
    jira: { label: 'Jira', glyph: 'J', tone: 'sky' },
    links: { label: 'Links', glyph: 'L', tone: 'emerald' },
    sessions: { label: 'Sessions', glyph: 'S', tone: 'sky' },
    mission_control: { label: 'Mission Control', glyph: 'MC', tone: 'violet' },
    vault: { label: 'Vault', glyph: 'V', tone: 'amber' },
    relay: { label: 'Relay', glyph: 'R', tone: 'emerald' },
    fleet: { label: 'Fleet', glyph: '▣', tone: 'indigo' },
    dropbox: { label: 'Dropbox', glyph: 'D', tone: 'sky' },
    messages: { label: 'Messages', glyph: 'M', tone: 'emerald' },
    photos: { label: 'Photos', glyph: 'P', tone: 'sky' },
    marketplace: { label: 'Market', glyph: '$', tone: 'amber' },
  };

  function emptyCounts() {
    return {
      total_needs_attention: 0,
      categories: { apps: 0, comms: 0, approvals: 0 },
      states: { needs_attention: 0, waiting: 0, resolved: 0 },
      applications: {},
    };
  }

  function jsonError(response, payload) {
    const error = new Error((payload && (payload.message || payload.error)) ||
      ('HTTP ' + response.status));
    error.status = response.status;
    error.payload = payload || {};
    return error;
  }

  async function jsonRequest(url, options) {
    const response = await fetch(url, Object.assign({
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    }, options || {}));
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw jsonError(response, payload);
    return payload;
  }

  function toneClasses(tone) {
    return tone === 'indigo' ? 'bg-indigo-600' :
      tone === 'emerald' ? 'bg-emerald-600' :
        tone === 'sky' ? 'bg-sky-600' :
          tone === 'amber' ? 'bg-amber-500 text-gray-950' : 'bg-violet-600';
  }

  function relativeTime(value) {
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(value || 0)));
    if (seconds < 60) return 'now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + 'm';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + 'h';
    const days = Math.floor(hours / 24);
    return days < 14 ? days + 'd' : new Date(Number(value) * 1000).toLocaleDateString();
  }

  function statusLabel(item) {
    if (item.attention_state === 'needs_attention') {
      return item.category === 'approvals' ? 'Approval requested' : 'Needs you';
    }
    if (item.attention_state === 'waiting') return 'Waiting';
    return 'Resolved';
  }

  function normalizeItem(source) {
    const application = source.application || {};
    const scope = application.scope || 'unknown';
    const meta = APP_META[scope] || {
      label: application.label || scope,
      glyph: (application.label || scope || '?').slice(0, 2).toUpperCase(),
      tone: 'violet',
    };
    return {
      id: source.attention_id,
      applicationScope: scope,
      application: application.label || meta.label,
      iconRef: application.icon_ref || '',
      glyph: meta.glyph,
      appTone: meta.tone,
      type: source.category === 'approvals' ? 'approval' :
        (source.category === 'comms' ? 'message' : 'application'),
      category: source.category,
      role: source.participant_role,
      attentionState: source.attention_state,
      title: source.title,
      summary: source.summary || '',
      statusLabel: source.category === 'comms' ?
        (application.label || meta.label) : statusLabel(source),
      actionLabel: source.category === 'approvals' ? 'Review approval' :
        ('Open ' + (application.label || meta.label)),
      detail: source.summary || '',
      sourceLabel: scope,
      timeLabel: relativeTime(source.occurred_at),
      occurredAt: source.occurred_at,
      sourceVersion: source.source_version,
      counterpartyRef: source.counterparty_ref || null,
      presentation: source.presentation || {},
      rendererId: source.open && source.open.renderer_id,
      unavailable: false,
      machine_name: '',
      machineNameTouched: false,
      decisionBusy: false,
      decisionError: '',
      safeReview: {},
      actions: [],
      authorityRequirement: null,
      resolution: null,
      requester: null,
    };
  }

  function sameItem(left, right) {
    return left && right && left.id === right.id &&
      left.sourceVersion === right.sourceVersion;
  }

  function canonicalCategory(item) {
    if (!item) return 'apps';
    if (item.category === 'comms' || item.type === 'message') return 'comms';
    if (item.category === 'approvals' || item.type === 'approval') return 'approvals';
    return 'apps';
  }

  function machineNameError(item) {
    if (!item || item.applicationScope !== 'fleet') return '';
    const raw = String(item.machine_name || '');
    const normalized = raw.trim();
    if (!normalized) return 'Enter a machine name.';
    if (Array.from(normalized).length > 80) {
      return 'Machine name must be 80 characters or fewer.';
    }
    if (/[\u0000-\u001f\u007f]/.test(raw)) {
      return 'Machine name cannot contain control characters.';
    }
    return '';
  }

  async function fleetDecision(item) {
    const error = machineNameError(item);
    if (error) throw new Error(error);
    const review = item.safeReview || {};
    const fleet = review.fleet || review;
    if (!fleet.request || !fleet.channel_binding ||
        !Number.isSafeInteger(fleet.issued_at) || !fleet.personal_root_pub) {
      throw new Error('This machine request has no server-frozen enrollment context.');
    }
    const { openRoot } = await import('../ceremony/open-root.js');
    const opened = await openRoot({
      title: 'Add this machine?',
      detail: 'Unlock your personal root to approve this machine.',
    });
    if (!opened) throw new Error('Approval cancelled.');
    try {
      if (opened.rootPub !== fleet.personal_root_pub) {
        throw new Error('This request belongs to a different personal fleet.');
      }
      const ceremony = await import('../ceremony/fleet-enrollment.js');
      const evidence = await ceremony.mintFleetEnrollmentEvidence({
        personalRootSeed: opened.seed,
        rootPub: opened.rootPub,
        request: fleet.request,
        channelBinding: fleet.channel_binding,
        localBootstrapMachineId: fleet.local_bootstrap_machine_id || null,
        issuedAt: fleet.issued_at,
        seq: 0,
      });
      opened.seed = null;
      const decision = {
        machine_name: String(item.machine_name).trim(),
        approval: evidence.approval,
        roster_entry: evidence.rosterEntry,
      };
      if (evidence.localRosterEntry) {
        decision.local_roster_entry = evidence.localRosterEntry;
        decision.local_runtime = evidence.localRuntime;
      }
      return decision;
    } finally {
      if (opened && opened.seed) {
        opened.seed.fill(0);
        opened.seed = null;
      }
    }
  }

  function categoryGlyphs() {
    return [
      { key: 'apps', label: 'Apps', glyph: '🌐' },
      { key: 'comms', label: 'Comms', glyph: '💬' },
      { key: 'approvals', label: 'Approvals', glyph: '▣' },
    ];
  }

  window.centralAttentionSurface = function () {
    return {
      inboxOpen: false,
      fullInbox: false,
      settingsOpen: false,
      selectedItem: null,
      declineConfirm: false,
      view: 'needs',
      categoryFilter: 'all',
      categories: categoryGlyphs(),
      items: [],
      apps: [],
      counts: emptyCounts(),
      badgeCount: 0,
      badgePulse: false,
      loading: true,
      unavailable: false,
      message: '',
      pushState: 'loading',
      pushLabel: 'Checking this device…',
      pushBusy: false,
      _events: null,
      _refreshTimer: null,
      _destroyed: false,

      async init() {
        await this.refresh();
        this.connectEvents();
        await this.refreshPush();
        const params = new URLSearchParams(window.location.search);
        const focus = params.get('focus');
        const id = params.get('id');
        if (focus === 'approval' && id) {
          this.fullInbox = true;
          const match = this.items.find(item => item.id === id);
          if (match) await this.openItem(match);
        }
      },

      destroy() {
        this._destroyed = true;
        clearTimeout(this._refreshTimer);
        if (this._events) this._events.close();
      },

      async refresh() {
        this.loading = true;
        let restarted = false;
        while (true) {
          try {
            const collected = [];
            let cursor = null;
            let first = null;
            for (let page = 0; page < MAX_PAGES; page += 1) {
              const url = new URL('/api/attention/items', window.location.origin);
              url.searchParams.set('limit', String(PAGE_SIZE));
              if (cursor) url.searchParams.set('cursor', cursor);
              const payload = await jsonRequest(url.toString());
              if (!first) first = payload;
              collected.push.apply(collected, payload.items || []);
              cursor = payload.next_cursor;
              if (!cursor) break;
              if (page === MAX_PAGES - 1) throw new Error('Attention list is too large to render safely.');
            }
            this.items = collected.map(normalizeItem);
            this.counts = (first && first.counts) || emptyCounts();
            this.badgeCount = Number(this.counts.total_needs_attention || 0);
            if (this.selectedItem) {
              const current = this.items.find(item => item.id === this.selectedItem.id);
              if (!sameItem(this.selectedItem, current)) {
                this.selectedItem = null;
                this.declineConfirm = false;
              }
            }
            this.syncApplications();
            this.unavailable = false;
            this.message = '';
            this.loading = false;
            return;
          } catch (error) {
            if (!restarted && error && error.payload && error.payload.error === 'refresh_required') {
              restarted = true;
              continue;
            }
            this.loading = false;
            this.unavailable = true;
            this.message = 'Attention is temporarily unavailable. Your items remain synchronized.';
            return;
          }
        }
      },

      syncApplications(labels) {
        const previous = Object.fromEntries(this.apps.map(app => [app.scope, app]));
        const scopes = new Set(Object.keys(this.counts.applications || {}));
        this.items.forEach(item => scopes.add(item.applicationScope));
        (labels || []).forEach(item => scopes.add(item.application));
        this.apps = Array.from(scopes).sort().map(scope => {
          const meta = APP_META[scope] || {
            label: scope, glyph: scope.slice(0, 2).toUpperCase(), tone: 'violet',
          };
          const supplied = (labels || []).find(item => item.application === scope) || {};
          const counts = (this.counts.applications || {})[scope] || {};
          const old = previous[scope] || {};
          return {
            scope,
            label: supplied.label || old.label || meta.label,
            glyph: old.glyph || meta.glyph,
            tone: old.tone || meta.tone,
            needs: Number(counts.needs_attention || 0),
            waiting: Number(counts.waiting || 0),
            foreground: 'quiet',
            pushMode: old.pushMode || 'off',
            pushBusy: false,
          };
        });
      },

      connectEvents() {
        if (!window.EventSource || this._events) return;
        const source = new EventSource('/api/attention/events', { withCredentials: true });
        this._events = source;
        const refresh = () => this.scheduleRefresh();
        source.addEventListener('attention:changed', refresh);
        source.addEventListener('attention:presentation', refresh);
        source.addEventListener('attention:refresh', refresh);
        source.onerror = () => {
          if (source.readyState === EventSource.CLOSED) this._events = null;
        };
      },

      scheduleRefresh() {
        clearTimeout(this._refreshTimer);
        this._refreshTimer = setTimeout(async () => {
          const previous = this.badgeCount;
          await this.refresh();
          if (this.badgeCount !== previous) {
            this.badgePulse = true;
            setTimeout(() => { this.badgePulse = false; }, 650);
          }
        }, REFRESH_DELAY_MS);
      },

      toggleInbox() {
        this.settingsOpen = false;
        this.selectedItem = null;
        this.declineConfirm = false;
        if (this.fullInbox) {
          this.fullInbox = false;
          this.inboxOpen = false;
          return;
        }
        this.inboxOpen = !this.inboxOpen;
        if (this.inboxOpen) this.refresh();
      },

      closeInbox() {
        this.inboxOpen = false;
        this.fullInbox = false;
        this.settingsOpen = false;
        this.selectedItem = null;
        this.declineConfirm = false;
        this.categoryFilter = 'all';
      },

      escape() {
        if (this.declineConfirm) this.declineConfirm = false;
        else if (this.selectedItem) this.selectedItem = null;
        else if (this.settingsOpen) this.settingsOpen = false;
        else if (this.fullInbox) this.closeInbox();
        else this.inboxOpen = false;
      },

      toggleCategory(key, openFull) {
        this.categoryFilter = this.categoryFilter === key ? 'all' : key;
        if (openFull) {
          this.fullInbox = true;
          this.inboxOpen = false;
        }
      },

      categoryFor: canonicalCategory,
      toneClasses,
      machineNameError,

      categoryCount(key) {
        return Number((this.counts.categories || {})[key] || 0);
      },

      visibleNeedsCount() {
        return this.categoryFilter === 'all' ? this.badgeCount :
          this.categoryCount(this.categoryFilter);
      },

      needsItems() {
        return this.items.filter(item => item.attentionState === 'needs_attention' &&
          (this.categoryFilter === 'all' || canonicalCategory(item) === this.categoryFilter));
      },

      filteredItems() {
        return this.items.filter(item => {
          const categoryMatches = this.categoryFilter === 'all' ||
            canonicalCategory(item) === this.categoryFilter;
          const viewMatches = this.view === 'needs' ? item.attentionState === 'needs_attention' :
            this.view === 'waiting' ? item.attentionState === 'waiting' :
              this.view === 'recent' ? item.attentionState === 'resolved' :
                item.attentionState !== 'resolved';
          return categoryMatches && viewMatches;
        });
      },

      async openItem(item) {
        if (!item) return;
        item.decisionError = '';
        this.selectedItem = item;
        try {
          const payload = await jsonRequest('/api/attention/items/' + encodeURIComponent(item.id));
          if (!sameItem(item, normalizeItem(payload.item))) {
            await this.refresh();
            this.selectedItem = null;
            return;
          }
          const review = payload.review || {};
          item.safeReview = review.safe_review || {};
          item.rendererId = review.renderer_id || item.rendererId;
          item.authorityRequirement = review.authority_requirement || null;
          item.actions = Array.isArray(review.actions) ? review.actions.slice() : [];
          item.resolution = review.resolution || null;
          item.requester = review.requester || null;
          item.detail = item.safeReview.detail || item.safeReview.summary || item.summary;
          item.sourceLabel = [item.applicationScope, review.kind].filter(Boolean).join(' · ');
          item.comparisonCode = item.safeReview.verification_code ||
            item.safeReview.comparison_code || '';
          item.unavailable = false;
        } catch (error) {
          if (error && error.status === 409 && error.payload && error.payload.item) {
            item.unavailable = true;
          } else {
            item.unavailable = true;
          }
        }
        jsonRequest('/api/attention/items/' + encodeURIComponent(item.id) + '/opened', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        }).catch(() => {});
      },

      async buildGrantDecision(item) {
        if (item.rendererId === 'approval.dashboard_access.review') {
          const { signDashboardAccessGrant } = await import(
            '../ceremony/dashboard-access.js'
          );
          return signDashboardAccessGrant(item.safeReview.grant);
        }
        if (item.rendererId === 'approval.fleet_machine_admission.review') {
          return fleetDecision(item);
        }
        if (item.authorityRequirement === 'operator_session') return {};
        throw new Error('This approval renderer is not active yet.');
      },

      async grant(item) {
        if (!item || item.decisionBusy || !item.actions.includes('granted')) return;
        const validationError = machineNameError(item);
        if (validationError) {
          item.machineNameTouched = true;
          item.decisionError = validationError;
          return;
        }
        item.decisionBusy = true;
        item.decisionError = '';
        try {
          const decision = await this.buildGrantDecision(item);
          await jsonRequest('/api/attention/items/' + encodeURIComponent(item.id) + '/approval-decision', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ outcome: 'granted', decision }),
          });
          this.selectedItem = null;
          await this.refresh();
        } catch (error) {
          item.decisionError = error.message || 'This approval could not be granted.';
        } finally {
          item.decisionBusy = false;
        }
      },

      async decline(item) {
        if (!item || item.decisionBusy || !item.actions.includes('declined')) return;
        item.decisionBusy = true;
        item.decisionError = '';
        try {
          await jsonRequest('/api/attention/items/' + encodeURIComponent(item.id) + '/approval-decision', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ outcome: 'declined', decision: {} }),
          });
          this.declineConfirm = false;
          this.selectedItem = null;
          await this.refresh();
        } catch (error) {
          item.decisionError = error.message || 'This approval could not be declined.';
        } finally {
          item.decisionBusy = false;
        }
      },

      openDestination() {
        this.selectedItem = null;
      },

      async refreshPush() {
        let stateFailure = null;
        try {
          if (!window.AutonomyWebPush) {
            this.pushState = 'unsupported';
            this.pushLabel = 'Phone alerts are unavailable in this browser.';
          } else {
            const state = await window.AutonomyWebPush.state();
            this.pushState = state.state;
            this.pushLabel = state.label;
          }
        } catch (error) {
          stateFailure = error;
          this.pushState = 'error';
          this.pushLabel = error.message || 'Phone alerts are temporarily unavailable.';
        }
        try {
          const config = await jsonRequest('/api/web-push/config');
          this.syncApplications(config.applications || []);
          const preferences = config.preferences || {};
          this.apps.forEach(app => { app.pushMode = preferences[app.scope] || 'off'; });
        } catch (error) {
          if (!stateFailure) {
            this.pushState = 'error';
            this.pushLabel = error.message || 'Phone alerts are temporarily unavailable.';
          }
        }
      },

      async enablePush() {
        if (!window.AutonomyWebPush || this.pushBusy) return;
        this.pushBusy = true;
        try {
          const state = await window.AutonomyWebPush.enroll();
          this.pushState = state.state;
          this.pushLabel = state.label;
          await this.refreshPush();
        } catch (error) {
          this.pushLabel = error.message || 'Phone alerts could not be enabled.';
        } finally {
          this.pushBusy = false;
        }
      },

      async setPushPreference(app, event) {
        const requested = event.target.value;
        const previous = app.pushMode;
        app.pushMode = requested;
        app.pushBusy = true;
        try {
          const payload = await jsonRequest('/api/web-push/preferences/' +
            encodeURIComponent(app.scope), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: requested }),
          });
          app.pushMode = payload.mode;
        } catch (error) {
          app.pushMode = previous;
          this.pushLabel = error.message || 'That phone-alert preference was not saved.';
        } finally {
          app.pushBusy = false;
        }
      },
    };
  };
})();
