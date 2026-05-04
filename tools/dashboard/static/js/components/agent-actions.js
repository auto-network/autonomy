// Global agentic actions dropdown (auto-pqgrl).
//
// Lives in the global header right of the search input. Visible only on
// pages where the current asset has at least one applicable action in
// the asset's owning org. Subscribes to setting.changed (Round 1b) so
// adding/removing an action reflects without a manual refresh.
//
// The dropdown is strictly own-org-of-asset: actions defined in
// anchore.db render only on anchore notes; cross-org adoption happens
// via canonical promotion of Setting members.
//
// Top of dropdown: Send To (universal). Below: per-asset-type members
// rendered with icon + label + model/timing meta.

(function () {
  var NOTE_ROUTE_RE = /^\/(?:graph|source)\/([0-9a-f-]{6,})/i;
  var BEAD_ROUTE_RE = /^\/bead\/([^/?#]+)/i;
  var SET_ID = 'dashboard.agent-actions';
  var ASSET_TYPE_BY_PAGE = {
    'graph': 'note',
    'source': 'note',
    'bead': 'bead',
  };
  // Asset-type defaults that aren't note-shaped. Other route prefixes
  // (e.g. /sessions, /dispatch) intentionally yield no asset_type so the
  // button stays hidden until those types ship members.
  var TYPELESS_ROUTES = [
    /^\/sessions/, /^\/dispatch/, /^\/timeline/, /^\/activity/, /^\/worktrees/,
    /^\/beads/, /^\/orgs/, /^\/search/, /^\/$/,
  ];

  function deriveAsset() {
    var path = window.location.pathname || '/';
    var m = path.match(NOTE_ROUTE_RE);
    if (m) {
      var prefix = path.split('/')[1] || '';
      var aType = ASSET_TYPE_BY_PAGE[prefix] || '';
      return { id: m[1], type: aType };
    }
    m = path.match(BEAD_ROUTE_RE);
    if (m) {
      return { id: decodeURIComponent(m[1]), type: 'bead' };
    }
    return { id: '', type: '' };
  }

  function _orgSlug(org) {
    if (!org) return '';
    if (typeof org === 'string') return org;
    return org.slug || '';
  }

  async function fetchSourceOrg(assetId) {
    if (!assetId) return '';
    try {
      var resp = await fetch('/api/source/' + encodeURIComponent(assetId));
      if (!resp.ok) return '';
      var body = await resp.json();
      var direct = _orgSlug(body.org);
      if (direct) return direct;
      return _orgSlug(body.source && body.source.org);
    } catch (e) {
      return '';
    }
  }

  async function fetchAssetOrg(asset) {
    if (!asset || !asset.id) return '';
    if (asset.type === 'bead') return 'autonomy';
    return fetchSourceOrg(asset.id);
  }

  // Map an active-session row (from /api/dao/active_sessions) to the shape
  // that templates/partials/session-card.html expects. Mirrors the field
  // remapping that sessions.js does in _updateFromStore — kept here as a
  // pure function so the dropdown can present the same canonical card
  // without mounting the sessions Alpine component.
  function _sessionForCard(row) {
    var t = row.type || '';
    var sessionType = t === 'host' ? 'host'
      : t === 'chatwith' ? 'chatwith'
      : t === 'container' && row.bead_id ? 'dispatch'
      : 'interactive';
    var topics = row.topics;
    if (typeof topics === 'string') {
      try { topics = JSON.parse(topics); } catch (e) { topics = []; }
    }
    return Object.assign({}, row, {
      id: row.session_id,
      session_type: sessionType,
      topics: Array.isArray(topics) ? topics : [],
      latest: row.latest || row.last_message || '',
    });
  }

  async function fetchActionsForOrg(org) {
    if (!org) return [];
    var headers = { 'X-Graph-Org': org };
    try {
      var resp = await fetch('/api/graph/settings/' + SET_ID, { headers: headers });
      if (!resp.ok) return [];
      var body = await resp.json();
      return (body && body.members) || [];
    } catch (e) {
      return [];
    }
  }

  function applicableMembers(members, assetType) {
    var out = [];
    for (var i = 0; i < members.length; i++) {
      var m = members[i];
      var p = m.payload || {};
      if (p.universal) {
        out.push({ key: m.key, payload: p, universal: true });
        continue;
      }
      if (assetType && (p.asset_type === assetType || p.asset_type === '*')) {
        out.push({ key: m.key, payload: p, universal: false });
      }
    }
    return out;
  }

  function metaLine(payload) {
    var bits = [];
    if (payload.universal) {
      bits.push('primer to a live session');
    } else {
      var model = String(payload.model || '');
      var shortModel = model.indexOf('opus') !== -1 ? 'opus'
        : model.indexOf('sonnet') !== -1 ? 'sonnet'
        : model.indexOf('haiku') !== -1 ? 'haiku'
        : model;
      if (shortModel) bits.push(shortModel);
      if (payload.estimated_seconds) {
        bits.push('~' + payload.estimated_seconds + 's');
      }
    }
    return bits.join(' · ');
  }

  function iconClassFor(key) {
    if (key === 'session.send-to') return 'icon-send-to';
    if (key.indexOf('update') !== -1) return 'icon-update';
    if (key.indexOf('consolidate') !== -1) return 'icon-consolidate';
    if (key.indexOf('review') !== -1) return 'icon-review';
    return 'icon-default';
  }

  function agentActionsComponent() {
    var helpers = window.sessionCardHelpers || {};
    return {
      visible: false,
      panelOpen: false,
      modalOpen: false,
      // Operator-input modal state (auto-0tkwj). Shown for actions whose
      // payload declares ``input_prompt``. The action being prompted for
      // is held in ``inputModalMember`` so submit can re-dispatch with
      // the right member_key.
      inputModalOpen: false,
      inputModalMember: null,
      inputModalText: '',
      members: [],
      asset: { id: '', type: '' },
      org: '',
      liveSessions: [],
      pendingDispatch: false,
      lastError: '',
      // Action label shown in the success toast for ~4s after a
      // successful dispatch. Empty when no toast is active.
      lastDispatchLabel: '',

      // Shared session-card helpers (from /static/js/lib/session-card-helpers.js
      // via window.sessionCardHelpers). The Send-To modal embeds the
      // partials/session-card.html partial, which references these by name
      // on the Alpine scope.
      borderCls: helpers.borderCls || function () { return ''; },
      typeBadge: helpers.typeBadge || function () { return ''; },
      typeCls: helpers.typeCls || function () { return ''; },
      turnsStr: helpers.turnsStr || function () { return ''; },
      ctxStr: helpers.ctxStr || function () { return ''; },
      idleStr: helpers.idleStr || function () { return ''; },
      ctxWarn: helpers.ctxWarn || function () { return false; },
      recencyColor: helpers.recencyColor || function () { return ''; },
      endedOrIdleLabel: helpers.endedOrIdleLabel || function () { return ''; },
      endedOrIdleValue: helpers.endedOrIdleValue || function () { return ''; },
      showTmuxColumn: helpers.showTmuxColumn || function () { return false; },

      async init() {
        var self = this;
        await this.refresh();
        // Live update: refetch when any dashboard.agent-actions Setting
        // changes (Round 1b).
        if (window.dashboardEvents && window.dashboardEvents.onSettingChanged) {
          window.dashboardEvents.onSettingChanged(SET_ID, function () {
            self.refresh();
          });
        }
        // Refresh on every SPA navigation. The component's Alpine root
        // lives in #agent-actions-slot (outside the per-page fragment),
        // so x-init only fires once on initial load. We refetch on the
        // app:navigated event emitted by the router and on popstate
        // (back/forward), since pushState does not fire popstate.
        window.addEventListener('app:navigated', function () { self.refresh(); });
        window.addEventListener('popstate', function () { self.refresh(); });
      },

      async refresh() {
        var asset = deriveAsset();
        this.asset = asset;
        if (!asset.id) {
          this.visible = false;
          this.members = [];
          return;
        }
        var org = await fetchAssetOrg(asset);
        this.org = org;
        if (!org) {
          this.visible = false;
          this.members = [];
          return;
        }
        var raw = await fetchActionsForOrg(org);
        var members = applicableMembers(raw, asset.type);
        this.members = members;
        this.visible = members.length > 0;
      },

      openPanel() {
        this.panelOpen = true;
      },

      closePanel() {
        this.panelOpen = false;
      },

      togglePanel() {
        this.panelOpen = !this.panelOpen;
      },

      iconClass(key) {
        return iconClassFor(key);
      },

      metaFor(payload) {
        return metaLine(payload);
      },

      async dispatchMember(member) {
        this.closePanel();
        if (member.universal && member.key === 'session.send-to') {
          await this.openSendToModal();
          return;
        }
        // Schema #2 (auto-0tkwj): actions with ``input_prompt`` open a
        // small input modal first. Submit re-enters this method with
        // the operator's text via ``submitInputModal``.
        var inputPrompt = member.payload && member.payload.input_prompt;
        if (typeof inputPrompt === 'string' && inputPrompt.trim()) {
          this.openInputModal(member);
          return;
        }
        await this._postDispatch(member, {});
      },

      // Internal: actually POST to /api/agent-actions/dispatch. Shared
      // path between direct dispatch and input-modal submit so the
      // success/error/toast behaviour stays in one place.
      async _postDispatch(member, extras) {
        this.pendingDispatch = true;
        this.lastError = '';
        try {
          var body = Object.assign({
            asset_id: this.asset.id,
            member_key: member.key,
          }, extras || {});
          var resp = await fetch('/api/agent-actions/dispatch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!resp.ok) {
            this.lastError = 'Dispatch failed (' + resp.status + ')';
            return false;
          }
          await resp.json();
          this.lastDispatchLabel = (member.payload && member.payload.label) || member.key || '';
          var self = this;
          setTimeout(function () { self.lastDispatchLabel = ''; }, 4000);
          return true;
        } finally {
          this.pendingDispatch = false;
        }
      },

      // ── Operator-input modal (auto-0tkwj) ──
      openInputModal(member) {
        this.inputModalMember = member;
        this.inputModalText = '';
        this.inputModalOpen = true;
        this.lastError = '';
        // Autofocus after Alpine has flipped x-show. ``requestAnimationFrame``
        // runs after the layout pass so the textarea is in the flow and
        // focus() can land. Falling back to setTimeout with a small delay
        // covers headless harnesses that no-op on rAF.
        var self = this;
        var doFocus = function () {
          var ta = document.querySelector(
            '[data-testid=action-input-modal] textarea'
          );
          if (ta) ta.focus();
        };
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(doFocus);
        }
        setTimeout(doFocus, 30);
      },

      closeInputModal() {
        this.inputModalOpen = false;
        this.inputModalMember = null;
        this.inputModalText = '';
      },

      inputModalCanSubmit() {
        return !!(this.inputModalText && this.inputModalText.trim());
      },

      async submitInputModal() {
        if (!this.inputModalCanSubmit()) return;
        var member = this.inputModalMember;
        var text = this.inputModalText.trim();
        var ok = await this._postDispatch(member, { custom_input: text });
        if (ok) this.closeInputModal();
      },

      async openSendToModal() {
        this.modalOpen = true;
        this.liveSessions = [];
        try {
          var resp = await fetch('/api/dao/active_sessions');
          if (resp.ok) {
            var rows = await resp.json();
            this.liveSessions = (rows || [])
              .filter(function (r) { return r && r.is_live; })
              .map(_sessionForCard);
          }
        } catch (e) {
          this.liveSessions = [];
        }
      },

      closeSendToModal() {
        this.modalOpen = false;
      },

      async sendToSession(session) {
        var target = session && (
          session.tmux_session || session.tmux_name || session.session_id
        );
        if (!target) return;
        this.pendingDispatch = true;
        this.lastError = '';
        try {
          var body = {
            asset_id: this.asset.id,
            member_key: 'session.send-to',
            target_session_name: target,
          };
          var resp = await fetch('/api/agent-actions/dispatch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!resp.ok) {
            if (resp.status === 404) {
              this.lastError = 'session ended';
              await this.openSendToModal();
            } else {
              this.lastError = 'Send failed (' + resp.status + ')';
            }
            return;
          }
          this.modalOpen = false;
        } finally {
          this.pendingDispatch = false;
        }
      },
    };
  }

  window.agentActionsComponent = agentActionsComponent;
})();
