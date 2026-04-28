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
  var ASSET_ROUTE_RE = /^\/(?:graph|source)\/([0-9a-f-]{6,})/i;
  var SET_ID = 'dashboard.agent-actions';
  var ASSET_TYPE_BY_PAGE = {
    'graph': 'note',
    'source': 'note',
  };
  // Asset-type defaults that aren't note-shaped. Other route prefixes
  // (e.g. /sessions, /dispatch) intentionally yield no asset_type so the
  // button stays hidden until those types ship members.
  var TYPELESS_ROUTES = [
    /^\/sessions/, /^\/dispatch/, /^\/timeline/, /^\/worktrees/,
    /^\/beads/, /^\/bead\//, /^\/orgs/, /^\/search/, /^\/$/,
  ];

  function deriveAsset() {
    var path = window.location.pathname || '/';
    var m = path.match(ASSET_ROUTE_RE);
    if (m) {
      var prefix = path.split('/')[1] || '';
      var aType = ASSET_TYPE_BY_PAGE[prefix] || '';
      return { id: m[1], type: aType };
    }
    return { id: '', type: '' };
  }

  async function fetchSourceOrg(assetId) {
    if (!assetId) return '';
    try {
      var resp = await fetch('/api/source/' + encodeURIComponent(assetId));
      if (!resp.ok) return '';
      var body = await resp.json();
      return body.org || (body.source && body.source.org) || '';
    } catch (e) {
      return '';
    }
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
    if (key === 'universal.send-to') return 'icon-send-to';
    if (key.indexOf('update') !== -1) return 'icon-update';
    if (key.indexOf('consolidate') !== -1) return 'icon-consolidate';
    if (key.indexOf('review') !== -1) return 'icon-review';
    return 'icon-default';
  }

  function pageTitle() {
    var el = document.getElementById('page-title');
    return (el && el.textContent && el.textContent.trim()) || '';
  }

  function pageContext(asset) {
    return {
      asset_id: asset.id,
      asset_type: asset.type,
      asset_url: window.location.origin + window.location.pathname,
      asset_title: pageTitle(),
    };
  }

  function dispatchedBySession() {
    // Best-effort. The dashboard does not always know the operator's
    // session name; fall back to a sentinel so server-side provenance
    // still records *something*.
    var existing = (window.__dashboardSessionName || '').trim();
    return existing || 'dashboard';
  }

  function agentActionsComponent() {
    return {
      visible: false,
      panelOpen: false,
      modalOpen: false,
      members: [],
      asset: { id: '', type: '' },
      org: '',
      liveSessions: [],
      pendingDispatch: false,
      lastError: '',

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
        // Refresh on navigation — single-page app routes don't reload.
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
        var org = await fetchSourceOrg(asset.id);
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
        if (member.universal && member.key === 'universal.send-to') {
          await this.openSendToModal();
          return;
        }
        this.pendingDispatch = true;
        this.lastError = '';
        try {
          var body = {
            set_id: SET_ID,
            member_key: member.key,
            asset_id: this.asset.id,
            page_context: pageContext(this.asset),
            dispatched_by_session: dispatchedBySession(),
          };
          var resp = await fetch('/api/agent-actions/dispatch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!resp.ok) {
            this.lastError = 'Dispatch failed (' + resp.status + ')';
            return;
          }
          var result = await resp.json();
          if (result.agentic_source_id) {
            window.location.href = '/graph/' + result.agentic_source_id;
          }
        } finally {
          this.pendingDispatch = false;
        }
      },

      async openSendToModal() {
        this.modalOpen = true;
        this.liveSessions = [];
        try {
          var resp = await fetch('/api/dao/active_sessions');
          if (resp.ok) {
            var rows = await resp.json();
            this.liveSessions = (rows || []).filter(function (r) {
              return r && r.is_live;
            });
          }
        } catch (e) {
          this.liveSessions = [];
        }
      },

      closeSendToModal() {
        this.modalOpen = false;
      },

      stateClass(session) {
        if (!session) return 'state-idle';
        var act = session.last_activity || 0;
        var now = Date.now() / 1000;
        if (act && (now - act) < 120) return 'state-thinking';
        return 'state-idle';
      },

      stateLabel(session) {
        var c = this.stateClass(session);
        return c === 'state-thinking' ? 'Thinking' : 'Idle';
      },

      sessionGlyph(session) {
        var name = (session && session.tmux_name) || '';
        return (name[0] || '?').toUpperCase();
      },

      async sendToSession(session) {
        if (!session || !session.tmux_name) return;
        this.pendingDispatch = true;
        this.lastError = '';
        try {
          var body = {
            set_id: SET_ID,
            member_key: 'universal.send-to',
            asset_id: this.asset.id,
            page_context: pageContext(this.asset),
            target_session_name: session.tmux_name,
            dispatched_by_session: dispatchedBySession(),
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
