// Asset presence + sharing — one control for every shareable surface.
//
// The pill in a page's top-right corner: the sessions that touched the
// asset (initials, green when live), and the asset's share state. Its
// dropdown lists the sessions (open one, chat with a live one), and holds
// the sharing controls: request a public link through the existing
// link_publish approval, or — once shared — share-sheet / open / manage the
// newest grant. Design Studio, Slides, and Notes all mount this; none of
// them re-implement any of it.
//
//   var handle = AssetPresence.mount(el, {
//     org: 'autonomy',
//     targetType: 'design' | 'present' | 'note' | 'mission',
//     targetUuid: '<stable id the link ceremony records>',
//     extraIds: ['<revision ids that a grant may also target>'],
//     title: 'Shown in the share sheet',
//     sessions: [{id, label, last_push, count}],   // live state resolves from Alpine.store('sessions')
//     chat: {open: false, connected: false, onToggle: fn} | null,  // omit when the surface has no chat
//     onOpenSession: function (session) {},        // default: navigate to the session viewer
//     onChatWith: function (session) {},           // default: chat.onToggle
//   });
//   handle.update({sessions: [...], chat: {...}});  // re-render with new inputs
//   handle.refresh();                               // re-read share state
//   handle.destroy();
//
// Share state comes from GET /api/share-state/<type>/<uuid>?ids=…, a read
// model over the org's link grants; nothing here mints or revokes a grant.

(function () {
  'use strict';

  var POLL_MS = 4500;
  var POLL_TICKS = 40; // ~3 minutes

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function fetcher() {
    return (window.Autonomy && window.Autonomy.fetch) || window.fetch;
  }

  function liveSession(id) {
    try {
      var store = window.Alpine && window.Alpine.store && window.Alpine.store('sessions');
      var s = store && store[id];
      return s && s.isLive ? s : null;
    } catch (e) { return null; }
  }

  function formatAgo(value) {
    if (!value) return 'unknown';
    var raw = String(value).trim();
    var normalized = raw.indexOf('T') >= 0 ? raw : raw.replace(' ', 'T') + 'Z';
    var parsed = new Date(normalized);
    if (isNaN(parsed.getTime())) return raw;
    var diff = Date.now() - parsed.getTime();
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
    return parsed.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  function expiryText(grant) {
    if (!grant) return '';
    if (!grant.expires_at) return 'no expiry';
    var ms = grant.expires_at * 1000 - Date.now();
    if (ms <= 0) return 'expired';
    var days = Math.floor(ms / 86400000);
    if (days >= 1) return 'expires in ' + days + (days === 1 ? ' day' : ' days');
    var hours = Math.max(1, Math.floor(ms / 3600000));
    return 'expires in ' + hours + (hours === 1 ? ' hour' : ' hours');
  }

  // Sessions with live state resolved, live first, newest push first.
  function resolveSessions(input, org) {
    var out = (input || []).filter(function (s) { return s && s.id; }).map(function (s) {
      // Human presence is not a session route. Keep names as plain text until
      // a real person/messaging destination exists. Older session-only callers
      // omit kind, so preserve their links.
      var kind = s.participant_kind || s.kind || 'agent';
      var isSession = kind === 'agent' || kind === 'session';
      var live = isSession ? liveSession(s.id) : null;
      var label = (live && live.label) || s.label || s.id;
      return {
        id: s.id,
        label: label,
        initial: String(label).trim().charAt(0).toUpperCase() || '?',
        last_push: s.last_push || '',
        count: Number(s.count) || 0,
        live: typeof s.live === 'boolean' ? s.live : !!live,
        href: isSession ? '/session/' + encodeURIComponent(org || 'autonomy') + '/' + encodeURIComponent(s.id) : null,
      };
    });
    out.sort(function (a, b) {
      if (a.live !== b.live) return a.live ? -1 : 1;
      return String(b.last_push).localeCompare(String(a.last_push));
    });
    return out;
  }

  var SHARE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V3M7 8l5-5 5 5"/><path d="M5 12v8h14v-8"/></svg>';
  var OPEN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 5h5v5M19 5l-9 9"/><path d="M19 13v6H5V5h6"/></svg>';
  var MANAGE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>';
  var CHAT_SVG = '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 3h14v9H6l-4 4V3z"/></svg>';
  var DOCUMENT_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M3 1.5h6l4 4v9H3zM9 1.5v4h4M5 8h6M5 11h4"/></svg>';
  var SESSION_SVG = '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="8" r="2.4" fill="currentColor"/></svg>';

  function activityEntries(input) {
    var seen = new Set();
    return (Array.isArray(input) ? input : []).filter(function (entry) {
      if (!entry || !entry.session_id || !entry.artifact_id || !entry.org) return false;
      var href = String(entry.artifact_href || '');
      if (!/^\/(?!\/)/.test(href) || /[\\\x00-\x20]/.test(href)) return false;
      try {
        var origin = (window.location && window.location.origin) || 'https://local.invalid';
        var parsed = new URL(href, origin);
        if (parsed.origin !== origin || !/^https?:$/.test(parsed.protocol)) return false;
      } catch (_) { return false; }
      var key = JSON.stringify([entry.org, entry.session_id, entry.artifact_id]);
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
  }

  function Control(el, opts) {
    this.el = el;
    this.opts = Object.assign({ extraIds: [], sessions: [], chat: null }, opts || {});
    this.share = { shared: false, grants: [] };
    this.shareState = 'idle'; // idle | requesting | awaiting | error
    this.shareError = '';
    this.approvalId = '';
    this._pollTimer = null;
    this._destroyed = false;
    this._onClick = this._onClick.bind(this);
    this._onToggle = this._onToggle.bind(this);
    this._onDocumentClick = this._onDocumentClick.bind(this);
    this._onKeydown = this._onKeydown.bind(this);
    this._positionMenu = this._positionMenu.bind(this);
    el.classList.add('asset-presence-host');
    el.addEventListener('click', this._onClick);
    // The shell is built ONCE and never replaced. Re-creating the <details>
    // on each render fired a fresh toggle event, whose handler refreshed,
    // whose success re-rendered: an unbounded render/refresh loop that made
    // every control in it unclickable (the button was destroyed mid-click).
    this._buildShell();
    document.addEventListener('click', this._onDocumentClick, true);
    document.addEventListener('keydown', this._onKeydown);
    if (window.addEventListener) {
      window.addEventListener('resize', this._positionMenu);
      window.addEventListener('scroll', this._positionMenu, true);
    }
    this.render();
    this.refresh();
  }

  Control.prototype._buildShell = function () {
    this.el.innerHTML = '<details class="design-topbar-presence design-viewer-presence" data-testid="asset-presence">'
      + '<summary class="design-presence-pill"></summary>'
      + '<div class="design-presence-menu" data-testid="asset-presence-menu"></div>'
      + '</details>';
    this.details = this.el.querySelector('details');
    this.summary = this.el.querySelector('summary');
    this.menu = this.el.querySelector('.design-presence-menu');
    this.details.addEventListener('toggle', this._onToggle);
  };

  Object.defineProperty(Control.prototype, 'open', {
    get: function () { return !!(this.details && this.details.open); },
    set: function (value) { if (this.details) this.details.open = !!value; },
  });

  Control.prototype.update = function (opts) {
    Object.assign(this.opts, opts || {});
    this.render();
  };

  Control.prototype.destroy = function () {
    this._destroyed = true;
    this._stopPoll();
    this.el.removeEventListener('click', this._onClick);
    document.removeEventListener('click', this._onDocumentClick, true);
    document.removeEventListener('keydown', this._onKeydown);
    if (window.removeEventListener) {
      window.removeEventListener('resize', this._positionMenu);
      window.removeEventListener('scroll', this._positionMenu, true);
    }
    this.el.innerHTML = '';
    this.details = this.summary = this.menu = null;
  };

  Control.prototype.sessions = function () {
    return resolveSessions(this.opts.sessions, this.opts.org);
  };

  Control.prototype.primaryGrant = function () {
    var grants = this.share.grants || [];
    return grants.length ? grants[0] : null;
  };

  Control.prototype.summaryTitle = function () {
    var sessions = this.sessions();
    var live = sessions.filter(function (s) { return s.live; }).length;
    var text = sessions.length === 0 ? 'No sessions on this ' + (this.opts.noun || 'item')
      : sessions.length + (sessions.length === 1 ? ' session' : ' sessions') + (live ? ', ' + live + ' live' : '');
    if (this.opts.people) text = sessions.length + (sessions.length === 1 ? ' person' : ' people') + (live ? ', ' + live + ' live' : '');
    return text + (this.opts.sharing === false ? '' : this.share.shared ? ' · shared by link' : ' · not shared');
  };

  // Do not destroy a focused link merely because the sessions store emitted
  // an unchanged snapshot. Changed lists preserve the same destination key.
  Control.prototype._fill = function (summary, menu, title, live) {
    this.details.classList.toggle('is-live', !!live);
    this.summary.setAttribute('title', title);
    this.summary.setAttribute('aria-label', title);
    if (this._summaryHtml !== summary) { this._summaryHtml = summary; this.summary.innerHTML = summary; }
    if (this._menuHtml !== menu) {
      var focused = document.activeElement;
      var key = focused && focused.getAttribute && focused.getAttribute('data-activity-key');
      var inside = focused && this.menu.contains(focused);
      this._menuHtml = menu; this.menu.innerHTML = menu;
      if (inside) {
        var match = key && Array.from(this.menu.querySelectorAll('[data-activity-key]')).find(function (a) { return a.getAttribute('data-activity-key') === key; });
        (match || this.summary).focus({preventScroll: true});
      }
    }
    this._positionMenu();
  };

  Control.prototype._renderActivity = function () {
    var entries = activityEntries(this.opts.entries), seen = new Set(), sessions = [];
    entries.forEach(function (e) {
      var key = JSON.stringify([e.org, e.session_id]);
      if (!seen.has(key)) { seen.add(key); sessions.push(e); }
    });
    var avatars = sessions.slice(0, 3).map(function (e) {
      return '<span class="design-presence-avatar is-live" title="' + esc(e.session_label || e.session_id) + '">' + esc(String(e.session_label || e.session_id).trim().charAt(0).toUpperCase()) + '</span>';
    }).join('');
    var rows = entries.map(function (e) {
      var key = JSON.stringify([e.org, e.session_id, e.artifact_id]);
      var sessionHref = '/session/' + encodeURIComponent(e.org) + '/' + encodeURIComponent(e.session_id);
      return '<div class="asset-activity-row">'
        + '<a class="asset-activity-link is-artifact" data-testid="activity-artifact" data-activity-key="' + esc(key + ':artifact') + '" href="' + esc(e.artifact_href) + '" title="Open ' + esc(e.artifact_kind || 'Artifact') + ': ' + esc(e.artifact_title) + '">'
        + DOCUMENT_SVG + '<span><small>' + esc(e.artifact_kind || 'Artifact') + '</small><strong>' + esc(e.artifact_title || 'Untitled') + '</strong></span></a>'
        + '<a class="asset-activity-link is-session" data-testid="activity-session" data-activity-key="' + esc(key + ':session') + '" href="' + esc(sessionHref) + '" title="Open session: ' + esc(e.session_label || e.session_id) + '">'
        + SESSION_SVG + '<span><small>Session</small><span>' + esc(e.session_label || e.session_id) + '</span></span></a></div>';
    }).join('');
    var heading = this.opts.heading || 'Designing now';
    var title = sessions.length + (sessions.length === 1 ? ' live session' : ' live sessions') + ' · ' + heading;
    this._fill('<span class="design-topbar-live-dot"></span><span class="design-presence-stack">' + avatars + '</span>'
      + '<span class="asset-activity-mobile-count">' + sessions.length + '</span>'
      + (sessions.length > 3 ? '<span class="design-presence-count">+' + (sessions.length - 3) + '</span>' : sessions.length ? '' : '<span class="design-presence-count">—</span>'),
      '<div class="design-presence-section">' + esc(heading) + '</div>' + (rows || '<div class="design-presence-empty">' + esc(this.opts.emptyText || 'No live session is designing right now') + '</div>'), title, sessions.length > 0);
  };

  Control.prototype.render = function () {
    if (this._destroyed || !this.summary) return;
    this.details.classList.toggle('is-activity', this.opts.mode === 'activity');
    if (this.opts.mode === 'activity') { this._renderActivity(); return; }
    var sessions = this.sessions();
    var chat = this.opts.chat;
    var avatars = sessions.slice(0, 3).map(function (s) {
      return '<span class="design-presence-avatar' + (s.live ? ' is-live' : '') + '" title="' + esc(s.label + (s.live ? ' (live)' : '')) + '">' + esc(s.initial) + '</span>';
    }).join('');
    if (!sessions.length) avatars = '<span class="design-presence-avatar is-empty" title="No session has touched this yet">·</span>';
    var people = this.opts.people;
    var rows = sessions.map(function (s) {
      var meta = (s.live ? 'live · ' : '') + (s.last_push ? formatAgo(s.last_push) : 'no push recorded')
        + (s.count ? ' · ' + s.count + (s.count === 1 ? ' rev' : ' revs') : '');
      if (people) meta = s.live ? 'Here now' : 'Away';
      return '<' + (s.href ? 'a' : 'div') + ' class="design-presence-row" data-testid="asset-presence-session" data-session="' + esc(s.id) + '"'
        + (s.href ? ' href="' + esc(s.href) + '" data-action="open-session" title="Open session ' + esc(s.label) + '"' : '') + '>'
        + '<span class="design-presence-avatar' + (s.live ? ' is-live' : '') + '">' + esc(s.initial) + '</span>'
        + '<span class="design-presence-copy"><strong>' + esc(s.label) + '</strong><span>' + esc(meta) + '</span></span>'
        + (s.href ? '</a>' : '</div>');
    }).join('');
    if (!rows) rows = '<div class="design-presence-empty">No session has touched this yet</div>';
    // ONE chat action, named for what it will do. Connecting a particular
    // session is the picker's job, so a per-row chat button (which read the
    // same as this one) is deliberately not offered.
    var chatRow = '';
    if (chat) {
      var label = chat.open ? 'Close chat' : (chat.connected ? 'Open chat' : 'Chat with a session…');
      chatRow = '<button type="button" class="design-presence-share is-quiet" data-action="toggle-chat" data-testid="asset-presence-chat">' + esc(label) + '</button>';
    }
    var sharing;
    var grant = this.primaryGrant();
    if (this.share.shared && grant) {
      sharing = '<div class="design-presence-row is-share" data-testid="asset-share-row">'
        + '<span class="design-presence-avatar is-share">↗</span>'
        + '<span class="design-presence-copy"><strong>Shared by link</strong><span>' + esc(expiryText(grant)) + '</span></span>'
        + '<span class="design-presence-share-actions">'
        + '<button type="button" class="design-presence-action" data-action="share-link" title="Share link" aria-label="Share link">' + SHARE_SVG + '</button>'
        + '<button type="button" class="design-presence-action" data-action="open-link" title="Open link in a new tab" aria-label="Open link in a new tab">' + OPEN_SVG + '</button>'
        + '<button type="button" class="design-presence-action" data-action="manage" data-testid="asset-share-manage" title="Manage share in Published Links" aria-label="Manage share in Published Links">' + MANAGE_SVG + '</button>'
        + '</span></div>';
    } else {
      var text = this.shareState === 'requesting' ? 'Requesting…'
        : this.shareState === 'awaiting' ? 'Awaiting approval'
        : this.shareState === 'error' ? 'Share failed — retry'
        : 'Share by link';
      var disabled = (this.shareState === 'requesting' || this.shareState === 'awaiting') ? ' disabled' : '';
      sharing = '<button type="button" class="design-presence-share" data-action="share" data-testid="asset-share-request"' + disabled + '>' + esc(text) + '</button>'
        + (this.shareState === 'awaiting'
          ? '<div class="design-presence-note">Approve the publish request in Central; the link appears here once it is minted. '
            + '<button type="button" class="design-presence-link" data-action="cancel-share" data-testid="asset-share-cancel">Cancel</button></div>'
          : '');
    }
    var error = this.shareError ? '<div class="design-presence-note is-error">' + esc(this.shareError) + '</div>' : '';
    var title = this.summaryTitle();
    var summaryHtml = '<span class="design-presence-stack">' + avatars + '</span>'
      + (sessions.length > 3 ? '<span class="design-presence-count">+' + (sessions.length - 3) + '</span>' : '')
      + (this.opts.sharing !== false && this.share.shared ? '<span class="design-presence-sharemark" title="Shared by link">↗</span>' : '');
    var menuHtml = '<div class="design-presence-section">' + (this.opts.people ? 'People' : 'Sessions') + ' on this ' + esc(this.opts.noun || 'item') + '</div>'
      + rows + chatRow
      + (this.opts.sharing === false ? '' : '<div class="design-presence-section">Sharing</div>' + sharing + error);
    this._fill(summaryHtml, menuHtml, title, sessions.some(function (s) { return s.live; }));
  };

  Control.prototype._positionMenu = function () {
    if (this._destroyed || !this.open || !this.summary.getBoundingClientRect) return;
    var r = this.summary.getBoundingClientRect();
    if (!this.summary.isConnected || r.bottom <= 0 || r.top >= window.innerHeight || r.right <= 0 || r.left >= window.innerWidth) {
      if (document.activeElement && this.menu.contains(document.activeElement)) this.summary.focus({preventScroll:true});
      this.open = false; return;
    }
    var width = Math.min(320, window.innerWidth - 16);
    this.menu.style.width = width + 'px';
    this.menu.style.left = Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8)) + 'px';
    this.menu.style.right = 'auto';
    var below = Math.max(0, window.innerHeight - r.bottom - 14), above = Math.max(0, r.top - 14);
    var down = below >= Math.min(360, this.menu.scrollHeight) || below >= above;
    this.menu.style.maxHeight = Math.min(360, down ? below : above) + 'px';
    this.menu.style.top = (down ? r.bottom + 6 : Math.max(8, r.top - 6 - this.menu.getBoundingClientRect().height)) + 'px';
  };

  Control.prototype._onToggle = function () {
    // Safe now that render() fills the shell instead of replacing it: this
    // fires only on a real open/close, never as a side effect of rendering.
    if (this.open) { this._positionMenu(); this.refresh(); }
  };

  // A <details> does not close on an outside click or Escape; every other
  // menu on the dashboard does, so this one must too.
  Control.prototype._onDocumentClick = function (event) {
    if (this._destroyed || !this.open) return;
    if (this.el.contains(event.target)) return;
    this.open = false;
  };

  Control.prototype._onKeydown = function (event) {
    if (this._destroyed || !this.open || event.key !== 'Escape') return;
    this.open = false;
    if (this.summary && this.summary.focus) this.summary.focus();
  };

  Control.prototype._onClick = function (event) {
    var target = event.target.closest('[data-action]');
    if (!target || !this.el.contains(target)) return;
    var action = target.getAttribute('data-action');
    var row = target.closest('[data-session]');
    var session = row ? this.sessions().filter(function (s) { return s.id === row.getAttribute('data-session'); })[0] : null;
    if (action === 'open-session') {
      event.preventDefault();
      if (this.opts.onOpenSession) this.opts.onOpenSession(session);
      else if (session && window.navigateTo) window.navigateTo(session.href);
      return;
    }
    event.preventDefault();
    if (action === 'chat-with') {
      if (this.opts.onChatWith) this.opts.onChatWith(session);
      else if (this.opts.chat && this.opts.chat.onToggle) this.opts.chat.onToggle(session);
    } else if (action === 'toggle-chat') {
      if (this.opts.chat && this.opts.chat.onToggle) this.opts.chat.onToggle(null);
    } else if (action === 'share') {
      this.requestShare();
    } else if (action === 'cancel-share') {
      this.cancelShareWait();
    } else if (action === 'share-link') {
      this.shareLink();
    } else if (action === 'open-link') {
      this.openLink();
    } else if (action === 'manage') {
      this.manage();
    }
  };

  // ── Share state ─────────────────────────────────────────────────

  Control.prototype.refresh = async function () {
    if (this._destroyed || this.opts.mode === 'activity' || this.opts.sharing === false || !this.opts.targetUuid) return;
    try {
      var ids = (this.opts.extraIds || []).filter(Boolean).join(',');
      var url = '/api/share-state/' + encodeURIComponent(this.opts.targetType) + '/' + encodeURIComponent(this.opts.targetUuid)
        + '?org=' + encodeURIComponent(this.opts.org || 'autonomy') + (ids ? '&ids=' + encodeURIComponent(ids) : '');
      var res = await fetcher()(url);
      if (this._destroyed || !res.ok) return;
      var data = await res.json();
      if (this._destroyed) return;
      var next = data && typeof data === 'object' ? { shared: !!data.shared, grants: data.grants || [] } : this.share;
      var changed = JSON.stringify(next) !== JSON.stringify(this.share);
      this.share = next;
      if (this.share.shared && this.shareState === 'awaiting') {
        this.shareState = 'idle';
        this._stopPoll();
        changed = true;
      }
      if (changed) this.render();
    } catch (e) { /* keep the last known state */ }
  };

  Control.prototype.requestShare = async function () {
    if (this.opts.mode === 'activity' || this.opts.sharing === false) return;
    if (this.shareState === 'requesting' || this.shareState === 'awaiting') return;
    this.shareError = '';
    this.shareState = 'requesting';
    this.render();
    try {
      var res = await fetcher()('/api/approvals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'link_publish',
          session: 'dashboard-ui',
          request: {
            org: this.opts.org || 'autonomy',
            target_type: this.opts.targetType,
            target_uuid: this.opts.targetUuid,
            meta: {},
          },
        }),
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok || data.error) {
        this.shareState = 'error';
        this.shareError = data.error || ('Share request failed (HTTP ' + res.status + ')');
        this.render();
        return;
      }
      this.shareState = 'awaiting';
      this.approvalId = data.id || '';
      this.render();
      if (data.id && typeof window.openApprovalOverlay === 'function') {
        try { window.openApprovalOverlay(data.id); } catch (e) { /* Central still has it */ }
      }
      this._startPoll();
    } catch (e) {
      this.shareState = 'error';
      this.shareError = 'Share request failed: ' + (e.message || e);
      this.render();
    }
  };

  Control.prototype.checkApproval = async function () {
    if (!this.approvalId) return 'pending';
    try {
      var res = await fetcher()('/api/approvals/' + encodeURIComponent(this.approvalId));
      if (!res.ok) return res.status === 404 ? 'declined' : 'pending';
      var data = await res.json();
      var result = data && data.result;
      if (!result) return 'pending';
      return result.approved ? 'approved' : 'declined';
    } catch (e) { return 'pending'; }
  };

  Control.prototype._startPoll = function () {
    this._stopPoll();
    var self = this;
    var remaining = POLL_TICKS;
    var tick = function () {
      self._pollTimer = null;
      if (self._destroyed || self.shareState !== 'awaiting') return;
      self.checkApproval().then(function (decided) {
        if (self._destroyed || self.shareState !== 'awaiting') return;
        if (decided === 'declined') { self.shareState = 'idle'; self.render(); return; }
        return self.refresh();
      }).then(function () {
        if (self._destroyed || self.shareState !== 'awaiting') return;
        if (--remaining <= 0) { self.shareState = 'idle'; self.render(); return; }
        self._pollTimer = setTimeout(tick, POLL_MS);
      });
    };
    this._pollTimer = setTimeout(tick, POLL_MS);
  };

  Control.prototype._stopPoll = function () {
    if (this._pollTimer) { clearTimeout(this._pollTimer); this._pollTimer = null; }
  };

  Control.prototype.cancelShareWait = function () {
    this._stopPoll();
    this.approvalId = '';
    this.shareState = 'idle';
    this.render();
  };

  Control.prototype.shareLink = async function () {
    var grant = this.primaryGrant();
    if (!grant || !grant.url) return;
    try {
      if (navigator.share) { await navigator.share({ title: this.opts.title || 'Shared link', url: grant.url }); return; }
    } catch (e) { if (e && e.name === 'AbortError') return; }
    try {
      await navigator.clipboard.writeText(grant.url);
      this.shareError = '';
    } catch (e) {
      this.shareError = 'Could not copy the link: ' + grant.url;
      this.render();
    }
  };

  Control.prototype.openLink = function () {
    var grant = this.primaryGrant();
    if (grant && grant.url) window.open(grant.url, '_blank', 'noopener');
  };

  Control.prototype.manage = function () {
    var grant = this.primaryGrant();
    if (window.AutonomyOrgSettings && typeof window.AutonomyOrgSettings.open === 'function') {
      window.AutonomyOrgSettings.open(this.opts.org || 'autonomy', { screen: 'published-links', focus: grant ? grant.token : '' });
    }
  };

  var AssetPresence = {
    mount: function (el, opts) { return new Control(el, opts); },
    resolveSessions: resolveSessions,
    expiryText: expiryText,
    formatAgo: formatAgo,
    Control: Control,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = AssetPresence;
  if (typeof window !== 'undefined') window.AssetPresence = AssetPresence;
  if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('alpine:init', function () {
    window.Alpine.directive('asset-presence', function (el, binding, u) {
      var handle, disposed = false, read = u.evaluateLater(binding.expression);
      u.effect(function () { read(function (opts) {
        if (disposed) return;
        if (handle) handle.update(opts); else handle = AssetPresence.mount(el, opts);
      }); });
      u.cleanup(function () { disposed = true; if (handle) handle.destroy(); });
    });
  });
})();
