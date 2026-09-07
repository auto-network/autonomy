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
      var live = liveSession(s.id);
      var label = (live && live.label) || s.label || s.id;
      return {
        id: s.id,
        label: label,
        initial: String(label).trim().charAt(0).toUpperCase() || '?',
        last_push: s.last_push || '',
        count: Number(s.count) || 0,
        live: !!live,
        href: '/session/' + encodeURIComponent(org || 'autonomy') + '/' + encodeURIComponent(s.id),
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

  function Control(el, opts) {
    this.el = el;
    this.opts = Object.assign({ extraIds: [], sessions: [], chat: null }, opts || {});
    this.share = { shared: false, grants: [] };
    this.shareState = 'idle'; // idle | requesting | awaiting | error
    this.shareError = '';
    this.approvalId = '';
    this.open = false;
    this._pollTimer = null;
    this._destroyed = false;
    this._onClick = this._onClick.bind(this);
    this._onToggle = this._onToggle.bind(this);
    el.classList.add('asset-presence-host');
    el.addEventListener('click', this._onClick);
    el.addEventListener('toggle', this._onToggle, true);
    this.render();
    this.refresh();
  }

  Control.prototype.update = function (opts) {
    Object.assign(this.opts, opts || {});
    this.render();
  };

  Control.prototype.destroy = function () {
    this._destroyed = true;
    this._stopPoll();
    this.el.removeEventListener('click', this._onClick);
    this.el.removeEventListener('toggle', this._onToggle, true);
    this.el.innerHTML = '';
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
    return text + (this.share.shared ? ' · shared by link' : ' · not shared');
  };

  Control.prototype.render = function () {
    var sessions = this.sessions();
    var chat = this.opts.chat;
    var wasOpen = this.open;
    var avatars = sessions.slice(0, 3).map(function (s) {
      return '<span class="design-presence-avatar' + (s.live ? ' is-live' : '') + '" title="' + esc(s.label + (s.live ? ' (live)' : '')) + '">' + esc(s.initial) + '</span>';
    }).join('');
    if (!sessions.length) avatars = '<span class="design-presence-avatar is-empty" title="No session has touched this yet">·</span>';
    var rows = sessions.map(function (s) {
      var meta = (s.live ? 'live · ' : '') + (s.last_push ? 'last push ' + formatAgo(s.last_push) : 'no push recorded')
        + (s.count ? ' · ' + s.count + (s.count === 1 ? ' rev' : ' revs') : '');
      return '<div class="design-presence-row' + (s.live ? ' is-live' : '') + '" data-testid="asset-presence-session" data-session="' + esc(s.id) + '">'
        + '<span class="design-topbar-avatar is-row">' + esc(s.initial) + '</span>'
        + '<a class="design-presence-copy" href="' + esc(s.href) + '" data-action="open-session"><strong>' + esc(s.label) + '</strong><span>' + esc(meta) + '</span></a>'
        + (chat && s.live ? '<button type="button" class="design-presence-action" data-action="chat-with" title="Chat with ' + esc(s.label) + '" aria-label="Chat with ' + esc(s.label) + '">' + CHAT_SVG + '</button>' : '')
        + '</div>';
    }).join('');
    if (!rows) rows = '<div class="design-presence-empty">No session has touched this yet</div>';
    var chatRow = '';
    if (chat) {
      var label = chat.open ? 'Close chat' : (chat.connected ? 'Open chat' : 'Chat with a session…');
      chatRow = '<button type="button" class="design-presence-share is-quiet" data-action="toggle-chat" data-testid="asset-presence-chat">' + esc(label) + '</button>';
    }
    var sharing;
    var grant = this.primaryGrant();
    if (this.share.shared && grant) {
      sharing = '<div class="design-presence-row is-share" data-testid="asset-share-row">'
        + '<span class="design-topbar-avatar is-row is-share">↗</span>'
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
    var title = esc(this.summaryTitle());
    this.el.innerHTML = '<details class="design-topbar-presence design-viewer-presence' + (sessions.some(function (s) { return s.live; }) ? ' is-live' : '') + (wasOpen ? ' " open="open' : '') + '" data-testid="asset-presence">'
      + '<summary class="design-presence-pill" title="' + title + '" aria-label="' + title + '">'
      + '<span class="design-presence-stack">' + avatars + '</span>'
      + (sessions.length > 3 ? '<span class="design-presence-count">+' + (sessions.length - 3) + '</span>' : '')
      + (this.share.shared ? '<span class="design-presence-sharemark" title="Shared by link">↗</span>' : '')
      + '</summary>'
      + '<div class="design-presence-menu" data-testid="asset-presence-menu">'
      + '<div class="design-presence-section">Sessions on this ' + esc(this.opts.noun || 'item') + '</div>'
      + rows + chatRow
      + '<div class="design-presence-section">Sharing</div>'
      + sharing + error
      + '</div></details>';
  };

  Control.prototype._onToggle = function (event) {
    var details = event.target;
    if (!details || details.tagName !== 'DETAILS') return;
    this.open = !!details.open;
    if (this.open) this.refresh();
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
    if (this._destroyed || !this.opts.targetUuid) return;
    try {
      var ids = (this.opts.extraIds || []).filter(Boolean).join(',');
      var url = '/api/share-state/' + encodeURIComponent(this.opts.targetType) + '/' + encodeURIComponent(this.opts.targetUuid)
        + '?org=' + encodeURIComponent(this.opts.org || 'autonomy') + (ids ? '&ids=' + encodeURIComponent(ids) : '');
      var res = await fetcher()(url);
      if (this._destroyed || !res.ok) return;
      var data = await res.json();
      if (this._destroyed) return;
      this.share = data && typeof data === 'object' ? { shared: !!data.shared, grants: data.grants || [] } : this.share;
      if (this.share.shared && this.shareState === 'awaiting') {
        this.shareState = 'idle';
        this._stopPoll();
      }
      this.render();
    } catch (e) { /* keep the last known state */ }
  };

  Control.prototype.requestShare = async function () {
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
})();
