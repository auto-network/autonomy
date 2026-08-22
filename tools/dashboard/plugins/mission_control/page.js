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

// Recency reads as "3h ago" / "2d ago", not a raw timestamp — the home
// page's job is "is this alive?", not "what second was this written?"
// (graph://97ace518-788 §0: recency over current-state precision.)
function relativeTime(unixSeconds) {
  if (!unixSeconds) return '';
  const deltaS = Math.max(0, (Date.now() / 1000) - unixSeconds);
  if (deltaS < 60) return 'just now';
  const mins = Math.floor(deltaS / 60);
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.floor(hours / 24);
  if (days < 30) return days + 'd ago';
  return Math.floor(days / 30) + 'mo ago';
}

// Presence-style icon set for the page-level StylePicker. Only {id,color,
// icon} rides along -- the picker itself knows nothing about presence or
// missions (see static/js/style-picker.js).
const MC_PRESENCE_STYLES = [
  { id: 'avatar-row', color: '#a78bfa',
    icon: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="12" r="5.5"/><circle cx="15" cy="12" r="5.5"/></svg>' },
  { id: 'pan-strip', color: '#38bdf8',
    icon: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 7l-4 5 4 5M16 7l4 5-4 5"/></svg>' },
  { id: 'grouped', color: '#34d399',
    icon: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><circle cx="7" cy="9" r="2.4"/><circle cx="7" cy="15" r="2.4"/><circle cx="17" cy="9" r="2.4"/><circle cx="17" cy="15" r="2.4"/></svg>' },
  { id: 'compact', color: '#fbbf24',
    icon: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><circle cx="12" cy="12" r="4.6"/></svg>' },
  { id: 'delta-feed', color: '#fb7185',
    icon: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 7h14M5 12h14M5 17h9"/></svg>' },
];

const MC_PRESENCE_STYLE_KEY = 'mc:presenceStyle';

function loadPresenceStyle() {
  try {
    return window.localStorage.getItem(MC_PRESENCE_STYLE_KEY) || 'avatar-row';
  } catch (_) {
    return 'avatar-row';
  }
}

function savePresenceStyle(id) {
  try {
    window.localStorage.setItem(MC_PRESENCE_STYLE_KEY, id);
  } catch (_) {
    // best effort -- a private-browsing localStorage throw shouldn't
    // break the picker itself.
  }
}

// One Presence.alpine() surface per MISSION, not one for the whole plugin
// page -- mirrors presentations' per-resource `presentations:<designId>`
// convention, not coordinator_board's single page-wide surface id. Holds
// state for every presence-style variant the picker can select (not just
// avatar-row) so switching styles never re-subscribes the surface.
function missionPresenceRow(id, kind = 'mission') {
  // kind='mission' -> 'mission:<id>' (a mission's own surface); kind='pillar'
  // -> 'pillar:<id>' (one surface per pillar, same convention one level
  // down). Same component either way -- only the surface differs.
  // AVATAR_SIZE matches the shared .nx-avatar-stack > .nx-avatar width
  // (surface-presence.css) -- geometry must track the real substrate
  // class Mission Control renders through, not an assumed size.
  const AVATAR_STEP = 20, AVATAR_SIZE = 24, NAME_SHIFT = 96;
  return Presence.alpine(
    { surfaceId: kind + ':' + id },
    {
      openId: null,
      identityTimer: null,
      _avatars: null,
      _avatarTick: 0,
      groupedOpen: false,
      compactOpen: false,

      participantColor(id) {
        return (typeof Presence !== 'undefined' && Presence.participantColor)
          ? Presence.participantColor(id) : 'hsl(220 10% 50%)';
      },
      participantInitial(p) {
        const label = (p && p.participant_label) || '';
        return label ? label.charAt(0).toUpperCase() : '?';
      },

      // A guest's photo, fetched once per person and kept for the life of the
      // page. A presence row carries an id and a name and nothing else -- that
      // shape is shared with every other surface here -- so the picture is
      // looked up from the id the row already has.
      //
      // Only guests have one. An agent and the operator are drawn from their
      // initial, the same as everywhere else on the dashboard.
      participantAvatar(p) {
        const id = p && p.participant_id;
        if (!id || id.indexOf('guest:') !== 0) return null;
        if (!this._avatars) this._avatars = {};
        if (Object.prototype.hasOwnProperty.call(this._avatars, id)) {
          return this._avatars[id];
        }
        this._avatars[id] = null;          // claim it: one fetch per person
        fetch('/api/visitor-tokens/' + encodeURIComponent(id))
          .then(r => (r.ok ? r.json() : null))
          .then(d => {
            const att = d && d.visitor && d.visitor.avatar_attachment_id;
            // Assigning through the key Alpine already tracks is what makes
            // the picture appear without a reload.
            this._avatars[id] = att ? '/api/attachment/' + att : null;
            this._avatarTick = (this._avatarTick || 0) + 1;
          })
          .catch(() => {});               // a missing photo is not an error
        return null;
      },
      lastActive(p) {
        return (p && p.heartbeat_at) ? relativeTime(Date.parse(p.heartbeat_at) / 1000) : '';
      },

      // Avatar row (state 0): resting geometry is plain absolute-positioned
      // circles, no flex reflow -- so there's nothing for it to differ
      // from at rest. Tap shifts every avatar after the tapped one right
      // by NAME_SHIFT and reveals its name in the gap. Self-collapses.
      get openIndex() {
        return this.participants.findIndex((p) => p.participant_id === this.openId);
      },
      avatarLeft(i) {
        let left = i * AVATAR_STEP;
        if (this.openId && i > this.openIndex) left += NAME_SHIFT;
        return left;
      },
      get rowWidth() {
        return Math.max(0, this.participants.length - 1) * AVATAR_STEP + AVATAR_SIZE;
      },
      toggleIdentity(id) {
        clearTimeout(this.identityTimer);
        if (this.openId === id) {
          this.openId = null;
          return;
        }
        this.openId = id;
        this.identityTimer = setTimeout(() => { this.openId = null; }, 1600);
      },

      // Grouped (state 2): human vs agent split.
      get presenceHumans() {
        return this.participants.filter((p) => p.participant_kind === 'operator');
      },
      get presenceAgents() {
        return this.participants.filter((p) => p.participant_kind === 'agent');
      },
    });
}

function missionControlPage() {
  return {
    missions: [],
    revisions: {},
    conversation: {},
    pillars: {},
    expanded: '',
    relativeTime: relativeTime,

    statusFilter: 'all',
    get statusFilters() {
      const counts = { active: 0, paused: 0, complete: 0 };
      for (const m of this.missions) {
        if (Object.prototype.hasOwnProperty.call(counts, m.status)) counts[m.status] += 1;
      }
      return [
        { value: 'all', label: 'All', count: this.missions.length },
        { value: 'active', label: 'Active', count: counts.active },
        { value: 'paused', label: 'Paused', count: counts.paused },
        { value: 'complete', label: 'Complete', count: counts.complete },
      ];
    },
    // Standard org filter — same contract as the Sessions page: /api/orgs
    // list, dropdown-toggle chrome, localStorage persistence. Shown only
    // when the mission list spans more than one org.
    selectedOrg: localStorage.getItem('missionControlOrgFilter') || '',
    orgFilterList: [],
    orgFilterOpen: false,
    async _fetchOrgFilterList() {
      try {
        const data = await fetch('/api/orgs').then((r) => r.ok ? r.json() : { orgs: [] });
        this.orgFilterList = (data.orgs || []).map(function (e) {
          var org = (e && e.org) || {};
          var ident = (e && e.identity_resolved) || {};
          var slug = org.slug || ident.slug || '';
          return {
            slug: slug,
            name: ident.name || slug,
            color: ident.color || '#4b5563',
            favicon: ident.favicon || null,
            initial: ident.initial || (slug ? slug[0].toUpperCase() : '?'),
          };
        }).filter(function (o) { return o.slug; });
      } catch (e) {
        console.warn('[missionControlPage] orgs fetch error', e);
        this.orgFilterList = [];
      }
    },
    get missionOrgCount() {
      return new Set(this.missions.map((m) => m.org || '').filter(Boolean)).size;
    },
    get orgFilterPicked() {
      var self = this;
      if (!this.selectedOrg) return null;
      return (this.orgFilterList || []).find(function (o) { return o.slug === self.selectedOrg; }) || null;
    },
    pickOrgFilter(slug) {
      this.orgFilterOpen = false;
      slug = slug || '';
      if (slug === this.selectedOrg) return;
      this.selectedOrg = slug;
      localStorage.setItem('missionControlOrgFilter', slug);
    },
    _matchesOrg(m) {
      if (!this.selectedOrg) return true;
      return (m.org || '') === this.selectedOrg;
    },
    get filteredMissions() {
      let rows = this.missions.filter((m) => this._matchesOrg(m));
      if (this.statusFilter === 'all') return rows;
      return rows.filter((m) => m.status === this.statusFilter);
    },

    presenceStyles: MC_PRESENCE_STYLES,
    presenceStyle: loadPresenceStyle(),
    onPickerChange(e) {
      this.presenceStyle = e.detail.id;
      savePresenceStyle(e.detail.id);
    },

    async init() {
      this._fetchOrgFilterList();
      await this.refreshMissions();
    },

    async refreshMissions() {
      try {
        const res = await fetch('/api/missions');
        const data = await res.json();
        const list = (data && data.missions) || [];
        // The list endpoint doesn't include current_revision/open_question_count
        // — fetch each mission's detail for the summary line. Small N
        // (missions are a rare, coarse-grained entity), so no pagination/
        // batching needed.
        this.missions = await Promise.all(list.map(async (m) => {
          try {
            const detail = await fetch('/api/missions/' + encodeURIComponent(m.mission_id));
            const body = await detail.json();
            return (body && body.mission) || m;
          } catch (_) {
            return m;
          }
        }));
        // Pillars, same small-N no-pagination posture as mission detail
        // above -- a mission with zero pillars costs one cheap empty-list
        // fetch and renders exactly as it did before pillars existed.
        await Promise.all(this.missions.map((m) => this.refreshPillars(m.mission_id)));
      } catch (_) {
        this.missions = [];
      }
    },

    async refreshPillars(missionId) {
      try {
        const res = await fetch('/api/missions/' + encodeURIComponent(missionId) + '/pillars');
        const data = await res.json();
        this.pillars = { ...this.pillars, [missionId]: (data && data.pillars) || [] };
      } catch (_) {
        this.pillars = { ...this.pillars, [missionId]: [] };
      }
    },

    async toggleExpand(missionId) {
      if (this.expanded === missionId) {
        this.expanded = '';
        return;
      }
      this.expanded = missionId;
      await Promise.all([
        this.refreshRevisions(missionId),
        this.refreshConversation(missionId),
        this.markSeen(missionId),
      ]);
    },

    // Deliberate action, not a side effect of the incidental GET
    // /api/missions/<id> refreshMissions() already fires on every list
    // load -- if THAT advanced the watermark, the delta would be erased
    // before the operator ever saw it. Firing only when a mission's
    // detail panel is actually opened is the deliberate "I looked at
    // this" signal the watermark needs.
    async markSeen(missionId) {
      try {
        await fetch('/api/missions/' + encodeURIComponent(missionId) + '/seen', { method: 'POST' });
      } catch (_) {
        return;
      }
      try {
        const res = await fetch('/api/missions/' + encodeURIComponent(missionId));
        const body = await res.json();
        const updated = body && body.mission;
        if (updated) {
          this.missions = this.missions.map((m) => (m.mission_id === missionId ? updated : m));
        }
      } catch (_) {
        // best effort -- the badge just won't clear until the next full refresh
      }
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

    async refreshConversation(missionId) {
      try {
        const res = await fetch('/api/missions/' + encodeURIComponent(missionId) + '/questions');
        const data = await res.json();
        this.conversation = {...this.conversation, [missionId]: (data && data.questions) || []};
      } catch (_) {
        this.conversation = {...this.conversation, [missionId]: []};
      }
    },
  };
}

if (typeof window !== 'undefined') {
  window.missionControlPage = missionControlPage;
  window.missionPresenceRow = missionPresenceRow;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { missionControlPage, missionPresenceRow };
}

})();
