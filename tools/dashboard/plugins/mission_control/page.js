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
function missionPresenceRow(missionId) {
  // AVATAR_SIZE matches the shared .nx-avatar-stack > .nx-avatar width
  // (surface-presence.css) -- geometry must track the real substrate
  // class Mission Control renders through, not an assumed size.
  const AVATAR_STEP = 20, AVATAR_SIZE = 24, NAME_SHIFT = 96;
  return Presence.alpine(
    { surfaceId: 'mission:' + missionId },
    {
      openId: null,
      identityTimer: null,
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
    expanded: '',
    relativeTime: relativeTime,

    presenceStyles: MC_PRESENCE_STYLES,
    presenceStyle: loadPresenceStyle(),
    onPickerChange(e) {
      this.presenceStyle = e.detail.id;
      savePresenceStyle(e.detail.id);
    },

    async init() {
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
      } catch (_) {
        this.missions = [];
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
