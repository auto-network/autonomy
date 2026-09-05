// Approved Design Studio revision 8c40dd8d-346b-4da7-ae54-8d0c71e1ba64
// (design 0e0af1a3, "Organization Membership — Settings Screen").
// The DOM below is the approved artifact materialized as a production screen;
// only the simulated inputs were replaced: the projection comes from
// GET /api/orgs/{slug}/membership, and the ceremonies run the real
// openRoot + ledger-event + invitation modules. Epic auto-6v1l3;
// decision note graph://9282a825-4ce.
(function () {
  'use strict';
  var api = window.AutonomyOrgSettings;
  if (!api) return;
  var lastPending = 0;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var ICONS = {
    share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M8 6.5 12 3l4 3.5M5 13v7h14v-7"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  };
  function request(url, options) {
    options = options || {};
    options.headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
    options.credentials = 'same-origin';
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var error = new Error(body.error || ('HTTP ' + res.status));
          error.status = res.status;
          throw error;
        }
        return body;
      });
    });
  }
  function timeLabel(value, fallback) {
    if (!value) return fallback;
    var ms = Number(value);
    var delta = Date.now() - ms;
    if (delta >= 0 && delta < 60000) return Math.max(1, Math.floor(delta / 1000)) + ' sec ago';
    if (delta >= 0 && delta < 3600000) return Math.floor(delta / 60000) + ' min ago';
    if (delta >= 0 && delta < 86400000) return Math.floor(delta / 3600000) + ' hr ago';
    return new Date(ms).toLocaleString([], { month: 'short', day: 'numeric' });
  }
  function remainingLabel(value) {
    var remaining = Number(value) - Date.now();
    if (remaining <= 0) return 'Expired';
    var days = Math.ceil(remaining / 86400000);
    if (days >= 2) return 'Expires in ' + days + ' days';
    var hours = Math.ceil(remaining / 3600000);
    return 'Expires in ' + hours + (hours === 1 ? ' hour' : ' hours');
  }
  function initial(name) { return (name || '?').trim().charAt(0).toUpperCase(); }
  function avatarUrl(avatar) {
    if (!avatar) return '';
    return /^https?:/.test(avatar) ? avatar : '/api/attachment/' + encodeURIComponent(avatar);
  }
  var ROLE_WORDS = { owner: 'Owner' };
  var SCOPE_WORDS = { '*': 'Full authority' };
  function roleWord(roles) {
    return (roles || []).map(function (role) { return ROLE_WORDS[role] || role; }).join(' · ');
  }

  function Controller(slug, root, view) {
    this.slug = slug;
    this.root = root;
    this.view = view;
    this.tab = (view.pending_claims || []).length ? 'invites' : 'members';
    this.memberSearch = '';
    this.roleFilter = null;
    this.expandedMember = null;
    this.expandedRole = null;
    this.confirmDeactivate = null;
    this.mintStep = null;
    this.mintLabel = '';
    this.mintExpiryDays = '7';
    this.mintMaxUses = '1';
    this.mintResult = null;
    this.pendingMint = null;
    this.busy = null;
    this.error = '';
    this.readyClaims = {};
  }

  Controller.prototype.liveInvites = function () {
    return (this.view.invites || []).filter(function (invite) { return invite.status === 'live'; });
  };
  Controller.prototype.pendingClaims = function () { return this.view.pending_claims || []; };
  Controller.prototype.members = function () { return this.view.members || []; };
  Controller.prototype.filteredMembers = function () {
    var query = this.memberSearch.trim().toLowerCase();
    var filter = this.roleFilter;
    return this.members().filter(function (member) {
      if (filter && !(member.roles || []).includes(filter)) return false;
      if (!query) return true;
      return memberName(member).toLowerCase().includes(query);
    });
  };
  function memberName(member) { return member.display_name || 'Unnamed member'; }
  function claimantName(claim) {
    return (claim.introduction && claim.introduction.display_name) || 'Unnamed requester';
  }
  Controller.prototype.claimProvenance = function (claim) {
    if (claim.invite_binding === 'key') return 'Invitation issued directly to this identity';
    if (claim.invite_label) return 'Via ' + claim.invite_label;
    return 'Via a shareable invitation link';
  };
  Controller.prototype.claimLine = function (claim) {
    var parts = [this.claimProvenance(claim)];
    parts.push('Asked to join ' + timeLabel(claim.submitted_at, ''));
    if (claim.need > 1) parts.push(claim.have + ' of ' + claim.need + ' approvals');
    if (this.readyClaims[claim.claim_key] || claim.ready) {
      parts.push("Waiting for their machine to finish joining");
    }
    return parts.join(' · ');
  };
  Controller.prototype.inviteTitle = function (invite) {
    return invite.label || roleWord([invite.granted_role]) + ' invitation';
  };
  Controller.prototype.inviteLine = function (invite) {
    var parts = [roleWord([invite.granted_role]), remainingLabel(invite.expiry)];
    var uses = invite.uses;
    if (uses && uses.max_uses > 1) parts.push(uses.used + ' of ' + uses.max_uses + ' used');
    return parts.filter(Boolean).join(' · ');
  };
  Controller.prototype.roleMemberCount = function (name) {
    return this.members().filter(function (member) {
      return (member.roles || []).includes(name);
    }).length;
  };
  function scopeSummary(role) {
    return (role.scope_set || []).map(function (scope) {
      return SCOPE_WORDS[scope] || scope;
    }).join(', ');
  }
  function joinPolicyLine(role) {
    if (role.claim_requires === 'self') return 'A direct invitation admits immediately';
    if (role.claim_requires === 'sponsor') return 'The inviter approves each request';
    return role.approver_threshold
      + (role.approver_threshold === 1 ? ' approval' : ' approvals') + ' to join';
  }

  function avatarHtml(entity, name, online) {
    var classes = 'mem-avatar' + (online ? ' online' : '');
    var style = entity.color && !entity.avatar ? ' style="background:' + esc(entity.color) + '"' : '';
    if (entity.avatar) {
      return '<span class="' + classes + '"' + style + '><img src="' + esc(avatarUrl(entity.avatar)) + '" alt=""></span>';
    }
    return '<span class="' + classes + '"' + style + '>' + esc(initial(name)) + '</span>';
  }

  Controller.prototype.render = function () {
    var self = this;
    lastPending = this.pendingClaims().length;
    var view = this.view;
    if (!view.founded) {
      this.root.innerHTML = '<div class="mem-empty">This organization has no membership ledger yet. Founding it happens when the organization signs on to auto.network for the first time.</div>';
      return;
    }
    var html = '';
    if (this.error) html += '<p class="mem-error">' + esc(this.error) + '</p>';
    html += '<nav class="mem-tabs" role="tablist">'
      + '<button type="button" role="tab" data-tab="members" class="' + (this.tab === 'members' ? 'on' : '') + '">Members<sup>' + this.members().length + '</sup></button>'
      + '<button type="button" role="tab" data-tab="invites" class="' + (this.tab === 'invites' ? 'on' : '') + '">Invitations<sup class="' + (lastPending ? 'warn' : '') + '">' + (this.liveInvites().length + lastPending) + '</sup></button>'
      + '<button type="button" role="tab" data-tab="roles" class="' + (this.tab === 'roles' ? 'on' : '') + '">Roles<sup>' + (view.role_defs || []).length + '</sup></button>'
      + '</nav>';
    if (this.tab === 'members') html += this.membersHtml();
    if (this.tab === 'invites') html += this.invitesHtml();
    if (this.tab === 'roles') html += this.rolesHtml();
    this.root.innerHTML = html;
    this.bind();
  };

  Controller.prototype.membersHtml = function () {
    var self = this;
    var html = '<div class="mem-section">'
      + '<input type="search" class="mem-search" placeholder="Search members" aria-label="Search members" value="' + esc(this.memberSearch) + '">';
    var roles = this.view.role_defs || [];
    if (roles.length > 1) {
      html += '<div class="mem-filters">';
      roles.forEach(function (role) {
        html += '<button type="button" data-role-filter="' + esc(role.name) + '" class="' + (self.roleFilter === role.name ? 'on' : '') + '">' + esc(roleWord([role.name])) + '</button>';
      });
      html += '</div>';
    }
    html += '<div class="mem-table" data-testid="membership-members">' + this.memberRowsHtml() + '</div>'
      + '<p class="mem-count-note" data-count-note' + (this.memberSearch || this.roleFilter ? '' : ' hidden') + '></p></div>';
    return html;
  };
  Controller.prototype.memberRowsHtml = function () {
    var self = this;
    var rows = this.filteredMembers().map(function (member) {
      var name = memberName(member);
      var presence = member.online ? 'Online' : timeLabel(member.last_seen_at, '');
      var expanded = self.expandedMember === member.persona;
      var detail = '';
      if (expanded) {
        var sponsor = self.members().find(function (row) { return row.persona === member.sponsor; });
        var sponsorName = sponsor && sponsor.display_name ? sponsor.display_name : '';
        detail = '<div class="mem-detail"><dl>'
          + '<div><dt>Role</dt><dd>' + esc(roleWord(member.roles)) + '</dd></div>'
          + (member.joined_at ? '<div><dt>Joined</dt><dd>' + esc(timeLabel(member.joined_at, '')) + '</dd></div>' : '')
          + (sponsorName ? '<div><dt>Invited by</dt><dd>' + esc(sponsorName) + '</dd></div>' : '')
          + (member.byline ? '<div><dt>About</dt><dd>' + esc(member.byline) + '</dd></div>' : '')
          + '</dl><div class="mem-detail-actions">'
          + '<button type="button" class="mem-secondary" data-action="message">Message</button>'
          + '<button type="button" class="mem-secondary" data-action="profile">View profile</button>'
          + '</div></div>';
      }
      return '<div><div class="mem-trow" role="button" tabindex="0" data-member="' + esc(member.persona) + '">'
        + avatarHtml(member, name, member.online)
        + '<div class="mem-tname"><strong>' + esc(name) + '</strong>'
        + (member.persona === self.view.viewer_persona ? '<span class="mem-chip">You</span>' : '')
        + '</div>'
        + '<span class="mem-when' + (member.online ? ' on' : '') + '">' + esc(presence) + '</span>'
        + '</div>' + detail + '</div>';
    }).join('');
    if (!rows) rows = '<div class="mem-empty" style="border:0">No members match.</div>';
    return rows;
  };

  Controller.prototype.invitesHtml = function () {
    var self = this;
    var html = '';
    var pending = this.pendingClaims();
    if (pending.length) {
      html += '<div class="mem-section"><div class="mem-heading"><h2>Join requests</h2></div><div class="mem-list">';
      pending.forEach(function (claim) {
        var ready = self.readyClaims[claim.claim_key] || claim.ready;
        html += '<article class="mem-row warn" data-claim="' + esc(claim.claim_key) + '">'
          + avatarHtml(claim.introduction || {}, claimantName(claim), false)
          + '<div class="mem-main"><div class="mem-line1"><strong>' + esc(claimantName(claim)) + '</strong></div>'
          + '<div class="mem-line2">' + esc(self.claimLine(claim)) + '</div></div>'
          + (ready
            ? '<span class="mem-word good">Approved</span>'
            : '<button type="button" class="mem-primary" data-action="approve"'
              + (self.busy === 'approve:' + claim.claim_key ? ' disabled' : '') + '>'
              + (self.busy === 'approve:' + claim.claim_key ? 'Approving' : 'Approve') + '</button>')
          + '</article>';
      });
      html += '</div></div>';
    }
    if (this.mintStep === 'form') html += this.mintFormHtml();
    if (this.mintStep === 'publishing') html += this.publishingHtml();
    if (this.mintStep === 'show-once') html += this.showOnceHtml();
    html += '<div class="mem-section"><div class="mem-heading"><h2>Invitations</h2>'
      + (this.mintStep === null ? '<button type="button" class="mem-secondary" data-action="open-mint">Invite</button>' : '')
      + '</div><div class="mem-list">';
    var live = this.liveInvites();
    live.forEach(function (invite) {
      var confirming = self.confirmDeactivate === invite.invite_id;
      html += '<article class="mem-row" data-invite="' + esc(invite.invite_id) + '">'
        + '<div class="mem-main"><div class="mem-line1"><strong>' + esc(self.inviteTitle(invite)) + '</strong></div>'
        + '<div class="mem-line2">' + esc(self.inviteLine(invite)) + '</div></div>';
      if (!confirming) {
        html += '<span class="mem-actions-row">'
          + (invite.join_url
            ? '<button type="button" class="mem-icon-btn" data-action="share" aria-label="Share ' + esc(self.inviteTitle(invite)) + '">' + ICONS.share + '</button>'
              + '<button type="button" class="mem-icon-btn" data-action="copy" aria-label="Copy link for ' + esc(self.inviteTitle(invite)) + '">' + ICONS.copy + '</button>'
            : '')
          + '<button type="button" class="mem-icon-btn danger" data-action="deactivate" aria-label="Deactivate ' + esc(self.inviteTitle(invite)) + '">' + ICONS.x + '</button>'
          + '</span>';
      } else {
        html += '<span class="mem-actions-row" style="gap:.4rem">'
          + '<button type="button" class="mem-secondary" data-action="keep">Keep</button>'
          + '<button type="button" class="mem-danger-sm" data-action="confirm-deactivate"'
          + (self.busy === 'deactivate:' + invite.invite_id ? ' disabled' : '') + '>'
          + (self.busy === 'deactivate:' + invite.invite_id ? 'Deactivating' : 'Deactivate') + '</button>'
          + '</span>';
      }
      html += '</article>';
    });
    if (!live.length) {
      html += '<div class="mem-empty">No active invitations. Create and share invitation links for new members to join the organization.</div>';
    }
    html += '</div></div>';
    return html;
  };

  Controller.prototype.mintFormHtml = function () {
    var roles = this.view.role_defs || [];
    var only = roles.length === 1 ? roles[0] : null;
    var authority = only && only.scope_set && only.scope_set.indexOf('*') !== -1
      ? 'This invitation grants full ' + roleWord([only.name]).toLowerCase() + ' authority — the only role this organization defines today.'
      : 'This invitation grants the selected role.';
    var html = '<div class="mem-section"><div class="mem-panel">'
      + '<h3>Invite someone to this organization</h3>'
      + '<p>' + esc(authority) + ' Whoever opens the link can ask to join; you approve each request before they become a member.</p>'
      + '<div class="mem-form" style="margin-bottom:.6rem">'
      + '<label for="mem-label" style="margin-top:0">Name this invitation</label>'
      + '<input id="mem-label" type="text" maxlength="80" data-mint-label value="' + esc(this.mintLabel) + '" placeholder="Dean\'s invite">'
      + '</div>';
    if (!only) {
      html += '<label for="mem-role">Role</label><select id="mem-role" data-mint-role>';
      roles.forEach(function (role) {
        html += '<option value="' + esc(role.name) + '">' + esc(roleWord([role.name])) + '</option>';
      });
      html += '</select>';
    }
    html += '<label for="mem-expiry">Invitation expires</label>'
      + '<select id="mem-expiry" data-mint-expiry>'
      + [['7', 'In 7 days'], ['30', 'In 30 days'], ['90', 'In 90 days']].map(function (pair) {
        return '<option value="' + pair[0] + '"' + (this.mintExpiryDays === pair[0] ? ' selected' : '') + '>' + pair[1] + '</option>';
      }, this).join('')
      + '</select>'
      + '<label for="mem-uses">How many people can use this link</label>'
      + '<select id="mem-uses" data-mint-uses>'
      + [['1', 'One person'], ['5', 'Up to 5 people'], ['25', 'Up to 25 people']].map(function (pair) {
        return '<option value="' + pair[0] + '"' + (this.mintMaxUses === pair[0] ? ' selected' : '') + '>' + pair[1] + '</option>';
      }, this).join('')
      + '</select>'
      + (this.error ? '<p class="mem-once" style="color:#fca5a5">' + esc(this.error) + '</p>' : '')
      + '<div class="mem-panel-actions">'
      + '<button type="button" class="mem-secondary" data-action="cancel-mint">Cancel</button>'
      + '<button type="button" class="mem-primary" data-action="mint"' + (this.busy === 'mint' ? ' disabled' : '') + '>'
      + (this.busy === 'mint' ? 'Creating' : 'Create invitation') + '</button>'
      + '</div></div></div>';
    return html;
  };

  Controller.prototype.publishingHtml = function () {
    return '<div class="mem-section"><div class="mem-panel">'
      + '<h3>Invitation signed</h3>'
      + '<p>One step left: approve publishing its public join route. The request is in your approvals inbox' + (window.openApprovalOverlay ? ' — it should have just opened' : '') + '.</p>'
      + (this.error ? '<p class="mem-once" style="color:#fca5a5">' + esc(this.error) + '</p>' : '')
      + '<div class="mem-panel-actions">'
      + '<button type="button" class="mem-secondary" data-action="finish-mint">Close</button>'
      + '</div></div></div>';
  };

  Controller.prototype.showOnceHtml = function () {
    var full = this.mintResult ? this.mintResult.url + '#t=' + this.mintResult.bearer : '';
    return '<div class="mem-section"><div class="mem-panel">'
      + '<h3>Invitation ready</h3>'
      + '<p>Send this link to the person you are inviting. It contains their secret entry code.</p>'
      + '<button type="button" class="mem-link-code" data-action="copy-once">' + esc(full) + '</button>'
      + '<p class="mem-once">Shown once — the secret code lives only on this screen. Copy it before you leave.</p>'
      + '<div class="mem-panel-actions">'
      + '<button type="button" class="mem-secondary" data-action="copy-once" data-once-copy>Copy link</button>'
      + '<button type="button" class="mem-primary" data-action="finish-mint">Done</button>'
      + '</div></div></div>';
  };

  Controller.prototype.rolesHtml = function () {
    var self = this;
    var html = '<div class="mem-section"><div class="mem-table" data-testid="membership-roles">';
    (this.view.role_defs || []).forEach(function (role) {
      var count = self.roleMemberCount(role.name);
      var expanded = self.expandedRole === role.name;
      html += '<div><div class="mem-trow" role="button" tabindex="0" data-role="' + esc(role.name) + '">'
        + '<div class="mem-tname"><strong>' + esc(roleWord([role.name])) + '</strong></div>'
        + '<button type="button" class="mem-count-btn" data-action="filter-role" aria-label="Show members with the ' + esc(roleWord([role.name])) + ' role">'
        + count + (count === 1 ? ' member' : ' members') + '</button>'
        + '</div>'
        + (expanded
          ? '<div class="mem-detail" style="padding-left:.65rem"><dl>'
            + '<div><dt>Can</dt><dd>' + esc(scopeSummary(role)) + '</dd></div>'
            + '<div><dt>Joining</dt><dd>' + esc(joinPolicyLine(role)) + '</dd></div>'
            + '</dl></div>'
          : '')
        + '</div>';
    });
    html += '</div></div>';
    return html;
  };

  Controller.prototype.bind = function () {
    var self = this;
    this.root.querySelectorAll('[data-tab]').forEach(function (button) {
      button.onclick = function () { self.tab = button.dataset.tab; self.render(); };
    });
    var search = this.root.querySelector('.mem-search');
    if (search) {
      search.oninput = function () {
        self.memberSearch = search.value;
        var table = self.root.querySelector('[data-testid="membership-members"]');
        if (table) table.innerHTML = self.memberRowsHtml();
        self.bindMemberRows();
        var note = self.root.querySelector('[data-count-note]');
        if (note) {
          var active = self.memberSearch || self.roleFilter;
          note.hidden = !active;
          note.textContent = self.filteredMembers().length + ' of ' + self.members().length + ' members';
        }
      };
    }
    this.root.querySelectorAll('[data-role-filter]').forEach(function (button) {
      button.onclick = function () {
        self.roleFilter = self.roleFilter === button.dataset.roleFilter ? null : button.dataset.roleFilter;
        self.render();
      };
    });
    this.bindMemberRows();
    this.root.querySelectorAll('[data-claim]').forEach(function (card) {
      var claim = self.pendingClaims().find(function (row) { return row.claim_key === card.dataset.claim; });
      card.onclick = function (event) {
        var action = event.target.closest('[data-action]');
        if (action && action.dataset.action === 'approve') self.approve(claim);
      };
    });
    this.root.querySelectorAll('[data-invite]').forEach(function (card) {
      var invite = self.liveInvites().find(function (row) { return row.invite_id === card.dataset.invite; });
      card.onclick = function (event) {
        var action = event.target.closest('[data-action]');
        if (!action) return;
        self.inviteAction(action.dataset.action, invite, action);
      };
    });
    this.root.querySelectorAll('[data-action="open-mint"]').forEach(function (button) {
      button.onclick = function () { self.mintStep = 'form'; self.render(); };
    });
    this.root.querySelectorAll('[data-action="cancel-mint"]').forEach(function (button) {
      button.onclick = function () { self.mintStep = null; self.render(); };
    });
    this.root.querySelectorAll('[data-action="mint"]').forEach(function (button) {
      button.onclick = function () { self.mint(); };
    });
    this.root.querySelectorAll('[data-action="copy-once"]').forEach(function (button) {
      button.onclick = function () {
        if (!self.mintResult || !navigator.clipboard) return;
        navigator.clipboard.writeText(self.mintResult.url + '#t=' + self.mintResult.bearer);
        var label = self.root.querySelector('[data-once-copy]');
        if (label) label.textContent = 'Link copied';
      };
    });
    this.root.querySelectorAll('[data-action="finish-mint"]').forEach(function (button) {
      button.onclick = function () {
        self.mintStep = null;
        self.mintResult = null;
        self.mintLabel = '';
        self.render();
      };
    });
    var mintLabel = this.root.querySelector('[data-mint-label]');
    if (mintLabel) mintLabel.oninput = function () { self.mintLabel = mintLabel.value; };
    var mintExpiry = this.root.querySelector('[data-mint-expiry]');
    if (mintExpiry) mintExpiry.onchange = function () { self.mintExpiryDays = mintExpiry.value; };
    var mintUses = this.root.querySelector('[data-mint-uses]');
    if (mintUses) mintUses.onchange = function () { self.mintMaxUses = mintUses.value; };
    this.root.querySelectorAll('[data-role]').forEach(function (row) {
      row.onclick = function (event) {
        var action = event.target.closest('[data-action]');
        if (action && action.dataset.action === 'filter-role') {
          self.tab = 'members';
          self.roleFilter = row.dataset.role;
          self.render();
          return;
        }
        self.expandedRole = self.expandedRole === row.dataset.role ? null : row.dataset.role;
        self.render();
      };
    });
  };
  Controller.prototype.bindMemberRows = function () {
    var self = this;
    this.root.querySelectorAll('[data-member]').forEach(function (row) {
      row.onclick = function (event) {
        if (event.target.closest('[data-action]')) return;
        self.expandedMember = self.expandedMember === row.dataset.member ? null : row.dataset.member;
        self.render();
      };
    });
  };

  Controller.prototype.inviteAction = function (action, invite, button) {
    var self = this;
    if (action === 'copy') {
      if (!invite.join_url || !navigator.clipboard) return;
      navigator.clipboard.writeText(invite.join_url);
      button.classList.add('copied');
      setTimeout(function () { button.classList.remove('copied'); }, 1600);
      return;
    }
    if (action === 'share') {
      var title = this.inviteTitle(invite);
      if (navigator.share) {
        navigator.share({ title: title, url: invite.join_url }).catch(function (error) {
          if (!error || error.name !== 'AbortError') { self.error = error.message; self.render(); }
        });
      } else if (navigator.clipboard) {
        navigator.clipboard.writeText(invite.join_url);
      }
      return;
    }
    if (action === 'deactivate') { this.confirmDeactivate = invite.invite_id; return this.render(); }
    if (action === 'keep') { this.confirmDeactivate = null; return this.render(); }
    if (action === 'confirm-deactivate') return this.deactivate(invite);
  };

  Controller.prototype.refresh = function () {
    var self = this;
    return request('/api/orgs/' + encodeURIComponent(this.slug) + '/membership').then(function (view) {
      self.view = view;
      self.render();
    }).catch(function () { /* quiet poll; the next tick retries */ });
  };

  // ── ceremonies ──────────────────────────────────────────────────────

  Controller.prototype.openRoot = function (title, detail) {
    return import('/static/js/ceremony/open-root.js').then(function (module) {
      return module.openRoot({ title: title, detail: detail });
    });
  };
  function zero(opened) {
    if (opened && opened.seed && opened.seed.fill) { opened.seed.fill(0); opened.seed = null; }
  }

  Controller.prototype.mint = function () {
    var self = this;
    if (this.busy) return;
    var roles = this.view.role_defs || [];
    var roleSelect = this.root.querySelector('[data-mint-role]');
    var role = roleSelect ? roleSelect.value : (roles[0] && roles[0].name);
    if (!role) { this.error = 'This organization defines no roles to grant.'; return this.render(); }
    this.busy = 'mint';
    this.error = '';
    this.render();
    var opened = null;
    var expiry = Date.now() + Number(this.mintExpiryDays) * 86400000;
    var maxUses = Number(this.mintMaxUses);
    Promise.all([
      this.openRoot('Invite to this organization', 'Unlock your personal root to sign this invitation.'),
      import('/static/js/ceremony/org-invite.js'),
    ]).then(function (loaded) {
      opened = loaded[0];
      if (!opened) return null;
      return loaded[1].mintOrgInvite({
        fetchImpl: window.fetch.bind(window),
        org: self.slug,
        genesisId: self.view.genesis_id,
        personalRootSeed: opened.seed,
        role: role,
        expiry: expiry,
        maxUses: maxUses,
      });
    }).then(function (minted) {
      self.busy = null;
      if (!minted) return self.render();
      self.pendingMint = minted;
      self.mintStep = 'publishing';
      self.render();
      return self.publishMint();
    }).catch(function (error) {
      self.busy = null;
      self.error = (error && error.message) || String(error);
      self.render();
    }).finally(function () { zero(opened); });
  };

  Controller.prototype.publishMint = function () {
    var self = this;
    var minted = this.pendingMint;
    if (!minted) return;
    var label = this.mintLabel.trim();
    var request_body = {
      org: this.slug,
      target_uuid: this.view.org_uuid,
      target_type: 'org:join',
      invite_ref: minted.inviteId,
      expires_at: minted.expiry,
      meta: label ? { label: label } : {},
    };
    return request('/api/approvals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: 'link_publish',
        session: 'Organization membership',
        request: request_body,
      }),
    }).then(function (approval) {
      if (window.openApprovalOverlay) window.openApprovalOverlay(approval.id);
      var poll = function () {
        if (self.mintStep !== 'publishing') return null;
        return request('/api/approvals/' + encodeURIComponent(approval.id) + '?wait=25').then(function (row) {
          var result = row.result || {};
          if (result.approved === false) throw new Error('Publishing the invitation link was declined. The signed invitation stays active without a link; deactivate it if that was unintended.');
          var execution = result.execution;
          if (execution && execution.ok === false) {
            throw new Error(execution.error || 'Publishing the invitation link failed.');
          }
          if (execution && execution.url) return execution.url;
          return poll();
        });
      };
      return poll();
    }).then(function (url) {
      if (!url) return;
      self.mintResult = { url: url, bearer: minted.bearer };
      self.pendingMint = null;
      self.mintStep = 'show-once';
      self.render();
      return self.refresh();
    }).catch(function (error) {
      self.error = (error && error.message) || String(error);
      self.render();
    });
  };

  Controller.prototype.approve = function (claim) {
    var self = this;
    if (this.busy || !claim) return;
    this.busy = 'approve:' + claim.claim_key;
    this.error = '';
    this.render();
    var opened = null;
    Promise.all([
      this.openRoot('Approve ' + claimantName(claim) + ' joining this organization',
        'Unlock your personal root to countersign their request.'),
      import('/static/js/ceremony/claim.js'),
    ]).then(function (loaded) {
      opened = loaded[0];
      if (!opened) return null;
      var claimModule = loaded[1];
      return claimModule.signClaimApproval({
        context: {
          transport: { fetch: window.fetch.bind(window) },
          orgSlug: self.slug,
          genesisId: self.view.genesis_id,
          heads: self.view.heads,
          maxHlc: [Date.now(), 0],
        },
        personalRootSeed: opened.seed,
        inviteRef: claim.invite_ref,
        personaPub: claim.persona_pub,
      });
    }).then(function (entry) {
      if (!entry) return null;
      return request('/api/network/ledger/claim/' + encodeURIComponent(claim.claim_key) + '/approval', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          org: self.slug,
          invite_ref: claim.invite_ref,
          persona_pub: claim.persona_pub,
          approval: entry,
        }),
      });
    }).then(function (result) {
      self.busy = null;
      if (result && (result.status === 'ready' || result.status === 'admitted')) {
        self.readyClaims[claim.claim_key] = true;
      }
      return self.refresh();
    }).catch(function (error) {
      self.busy = null;
      self.error = (error && error.message) || String(error);
      self.render();
    }).finally(function () { zero(opened); });
  };

  Controller.prototype.deactivate = function (invite) {
    var self = this;
    if (this.busy || !invite) return;
    this.busy = 'deactivate:' + invite.invite_id;
    this.error = '';
    this.render();
    var opened = null;
    Promise.all([
      this.openRoot('Deactivate ' + this.inviteTitle(invite),
        'Unlock your personal root to sign the revocation.'),
      import('/static/js/ceremony/ledger-event.js'),
      import('/static/js/ceremony/primitives.js'),
    ]).then(function (loaded) {
      opened = loaded[0];
      if (!opened) return null;
      var ledger = loaded[1];
      var primitives = loaded[2];
      return ledger.derivePersona(new Uint8Array(opened.seed), self.view.genesis_id).then(function (persona) {
        var attempt = function (remaining) {
          return request('/api/network/ledger/heads?org=' + encodeURIComponent(self.slug)).then(function (heads) {
            var event = ledger.buildEvent({
              authorKey: persona.publicHex,
              parents: heads.heads,
              hlc: [Date.now(), 0],
              payload: { type: 'revoke', target_event: invite.invite_id },
            });
            return ledger.signEvent(event, persona.signingKey).then(function (signed) {
              return request('/api/network/ledger/revoke', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ org: self.slug, event: primitives.canonicalJson(signed) }),
              });
            });
          }).catch(function (error) {
            if (error.status === 409 && remaining > 0) return attempt(remaining - 1);
            throw error;
          });
        };
        return attempt(2);
      });
    }).then(function (posted) {
      self.busy = null;
      self.confirmDeactivate = null;
      if (!posted) return self.render();
      return self.refresh();
    }).catch(function (error) {
      self.busy = null;
      self.error = (error && error.message) || String(error);
      self.render();
    }).finally(function () { zero(opened); });
  };

  api.register({
    id: 'membership',
    label: 'Membership',
    windowTitle: 'Membership',
    order: 30,
    render: function (slug) {
      var root = document.createElement('section');
      root.className = 'org-membership';
      root.innerHTML = '<div class="orgset-loading">Loading…</div>';
      return request('/api/orgs/' + encodeURIComponent(slug) + '/membership').then(function (view) {
        var controller = new Controller(slug, root, view);
        controller.render();
        var timer = setInterval(function () {
          if (!root.isConnected) { clearInterval(timer); return; }
          if (controller.busy || controller.mintStep) return;
          controller.refresh();
        }, 5000);
        return root;
      });
    },
    count: function () {
      return lastPending ? { text: lastPending, tone: 'blocking' } : null;
    },
  });
})();
