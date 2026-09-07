/* Unified personal identity indicator for the dashboard shell.
 *
 * This module owns identity chrome only. Organization signing remains an
 * on-demand authority ceremony; network-signon.js retains those primitives
 * without rendering a competing button or panel.
 */
(function (root, factory) {
  'use strict';

  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) root.AutonomyIdentityIndicator = api;
})(typeof window !== 'undefined' ? window : null, function (root) {
  'use strict';

  var status = null;
  var loadError = null;
  //: Organizations this deployment knows about, for the panel's list. Held
  //: separately from `status` because it loads on its own schedule and a
  //: slow or failed org read must not blank the identity above it.
  var orgs = null;
  var orgsError = null;
  var panelOpen = false;
  // Credentials management renders INSIDE this drawer (a sub-view), not a
  // separate full-screen frame. credentialsMounted guards against the drawer's
  // own re-renders (status refresh, org load) tearing the mounted view down.
  var credentialsOpen = false;
  var credentialsMounted = false;
  var lockBusy = false;
  var initialized = false;
  //: The pre-auth status flag tray (Design Studio e31b7e22 rev 32): a strip of
  //: small tiles shown here and on the locked screen. `unlockState` is the
  //: GET /api/identity/unlock-state payload (null until loaded / if absent —
  //: absent renders every tile DIM, never falsely lit). openFlagId is the tile
  //: whose detail balloon is showing.
  var unlockState = null;
  var unlockStateError = false;
  var unlockStateLoading = false;
  var openFlagId = null;
  // The restart button's in-flight guard: true from the moment the root
  // ceremony succeeds until the restore finishes and the flags are re-read.
  var restartBusy = false;
  var FLAG_NS = 'http://www.w3.org/2000/svg';
  // id -> {key on the unlock-state payload, title, plain-language detail, icon shapes}.
  // Order = tray order. Icons are the design's outline glyphs (placeholders to refine).
  var FLAGS = [
    { id: 'key', key: 'agent', title: 'Delegate key',
      body: 'The key that keeps your fleet running while you’re away. When it lapses, unlock again to renew it.',
      shapes: [['rect', { x: 7.5, y: 7.5, width: 9, height: 9, rx: 1.6 }],
        ['path', { d: 'M10 4v3.5M14 4v3.5M10 16.5V20M14 16.5V20M4 10h3.5M4 14h3.5M16.5 10H20M16.5 14H20' }]] },
    { id: 'session', key: 'ttl', title: 'Session',
      body: 'How long this dashboard stays open unattended. Unlock again to extend it.',
      shapes: [['path', { d: 'M6.5 3h11M6.5 21h11M8 3v3.6c0 1.4 4 3.4 4 5.4 0-2 4-4 4-5.4V3M8 21v-3.6c0-1.4 4-3.4 4-5.4 0 2 4 4 4 5.4V21' }]] },
    { id: 'tunnel', key: 'tunnel', title: 'Tunnel',
      body: 'The connection your other devices use to reach this dashboard.',
      shapes: [['rect', { x: 3.5, y: 4.5, width: 17, height: 15, rx: 2.2 }], ['circle', { cx: 12, cy: 12, r: 3.4 }],
        ['path', { d: 'M12 8.6V6.8M12 17.2v-1.8M15.4 12h1.8M6.8 12h1.8' }]] },
    { id: 'cert', key: 'certificates', title: 'Certificate',
      body: 'Your dashboard’s certificate. Renew it before it expires so your devices keep connecting.',
      shapes: [['circle', { cx: 12, cy: 9.2, r: 5.2 }], ['path', { d: 'M9 13.6L8 21l4-2.2L16 21l-1-7.4' }]] },
    { id: 'identity', key: 'domain', title: 'Identity',
      body: 'Your personal identity, anchored to this dashboard.',
      shapes: [['path', { d: 'M5 19v-6a7 7 0 0 1 14 0v6' }], ['path', { d: 'M9.5 19v-6a2.5 2.5 0 0 1 5 0v6' }], ['path', { d: 'M3 19h18' }]] },
    { id: 'approvals', key: 'approvals', title: 'Approvals',
      body: 'Requests waiting for your approval.',
      shapes: [['path', { d: 'M4.5 5.5h15a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H12l-4.5 3.5v-3.5H4.5A1.5 1.5 0 0 1 3 15V7a1.5 1.5 0 0 1 1.5-1.5z' }],
        ['path', { d: 'M8.8 11l2.1 2.1 4.3-4.3' }]] },
    { id: 'sync', key: 'sync', title: 'Sync',
      body: 'Whether your other machines can sync with this one.',
      shapes: [['path', { d: 'M17 3.5l3 3-3 3' }], ['path', { d: 'M20 6.5H9a5 5 0 0 0-5 5' }],
        ['path', { d: 'M7 20.5l-3-3 3-3' }], ['path', { d: 'M4 17.5h11a5 5 0 0 0 5-5' }]] },
  ];

  function deriveIdentityState(value) {
    if (!value) return 'loading';
    if (value.error) return 'error';
    if (!value.personal_identity) return 'bootstrap';
    if (value.gate_disabled === true) return 'gate-off';
    if (value.signed_in !== true) {
      return value.enforced === true ? 'locked' : 'open';
    }
    return Array.isArray(value.passkeys) && value.passkeys.length > 0
      ? 'ready' : 'setup';
  }

  function identityName(value) {
    var identity = value && value.personal_identity;
    var name = identity && identity.display_name;
    return (typeof name === 'string' && name.trim()) ? name.trim() : 'Your identity';
  }

  function identityInitial(value) {
    var name = identityName(value);
    return name === 'Your identity' ? '?' : name.charAt(0).toUpperCase();
  }

  function methodLabel(value) {
    if (!value || value.signed_in !== true) return '';
    if (value.method === 'passkey') return 'Passkey';
    if (value.method === 'password') return 'Password';
    if (value.method === 'bootstrap') return 'Setup unfinished';
    return 'This session';
  }

  function statusLabel(value) {
    var state = deriveIdentityState(value);
    if (state === 'gate-off') return 'Access open \u00b7 Auth disabled';
    if (state === 'open') return 'Access open \u00b7 Not authenticated';
    if (state === 'setup') return 'Unlocked \u00b7 ' + methodLabel(value);
    if (state === 'ready') return 'Unlocked \u00b7 ' + methodLabel(value);
    if (state === 'locked') return 'Dashboard locked';
    return '';
  }

  function el(tag, className, text) {
    var node = root.document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(kind) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = root.document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');

    var paths = {
      lock: ['M6 10V7a6 6 0 0 1 12 0v3', 'M5 10h14v11H5z'],
      key: ['M15 7a5 5 0 1 1-4.4 7.4L3 22v-4h4v-4h3.6A5 5 0 0 1 15 7z',
            'M16.5 10.5h.01'],
      // The exact key glyph the credentials manager uses (#i-key in
      // factor-management.js), so "Manage credentials" carries the same mark —
      // rendered with the panel's currentColor, i.e. not the manager's green.
      // The credentials symbol's ring is a <circle>; drawn here as an arc path
      // since this factory only appends <path> elements.
      credkey: ['M10.7 12.3 21 2m-4 0 3 3m-6 0 3 3',
                'M12 15.5a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0z'],
      // A chain-link (lucide "link-2"): "Accept invitation" pastes an
      // invitation LINK, so a link mark reads truer than a padlock.
      link: ['M9 17H7a5 5 0 0 1 0-10h2', 'M15 7h2a5 5 0 0 1 0 10h-2', 'M8 12h8'],
      retry: ['M20 11a8 8 0 1 0-2.3 5.7', 'M20 4v7h-7'],
      machines: ['M4 5h16v11H4z', 'M8 20h8', 'M12 16v4'],
    };
    (paths[kind] || []).forEach(function (d) {
      var path = root.document.createElementNS(ns, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  }

  function avatar(value, panel) {
    var node = el('span', 'identity-avatar' + (panel ? ' identity-panel-avatar' : ''),
      identityInitial(value));
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function currentPath() {
    if (!root.location) return '/';
    return (root.location.pathname || '/') + (root.location.search || '');
  }

  function goToUnlock() {
    root.location.assign('/unlock?next=' + encodeURIComponent(currentPath()));
  }

  function openOnboarding(step) {
    closePanel();
    if (root.AutonomyOnboarding && typeof root.AutonomyOnboarding.open === 'function') {
      root.AutonomyOnboarding.open(step ? { step: step } : undefined);
    }
  }

  function closePanel() {
    if (!panelOpen) return;
    panelOpen = false;
    credentialsOpen = false;
    credentialsMounted = false;
    openFlagId = null;
    render();
  }

  // Open the credentials manager as a sub-view of THIS drawer.
  function openCredentials() {
    credentialsOpen = true;
    credentialsMounted = false;
    render();
  }
  // Return from the credentials sub-view to the drawer's main (org list).
  function closeCredentials() {
    credentialsOpen = false;
    credentialsMounted = false;
    render();
  }

  function togglePanel() {
    panelOpen = !panelOpen;
    // The flag tray reports volatile process state.  A value retained from a
    // previous opening can be actively misleading after a reload or lock, so
    // every open gets a fresh snapshot. loadUnlockState's in-flight guard also
    // covers the panel renderer's first-load call below.
    if (panelOpen) loadUnlockState();
    render();
  }

  function triggerForState(state) {
    var button = el('button', 'identity-trigger identity-trigger-' + state);
    button.type = 'button';
    button.setAttribute('data-testid', 'identity-trigger');
    button.setAttribute('data-state', state);

    if (state === 'loading') {
      button.disabled = true;
      button.setAttribute('aria-label', 'Loading identity');
      button.appendChild(el('span', 'identity-loading-dot'));
      return button;
    }
    if (state === 'bootstrap') {
      button.textContent = 'Get started';
      button.setAttribute('aria-label', 'Get started with your identity');
      button.addEventListener('click', function () { openOnboarding(); });
      return button;
    }
    if (state === 'locked') {
      button.setAttribute('aria-label', 'Dashboard locked. Unlock dashboard');
      button.title = 'Dashboard locked';
      var lockSvg = icon('lock');
      lockSvg.classList.add('identity-lock-icon');
      button.appendChild(lockSvg);
      button.addEventListener('click', goToUnlock);
      return button;
    }
    if (state === 'error') {
      button.textContent = '!';
      button.setAttribute('aria-label', 'Identity unavailable. Retry');
      button.title = 'Identity unavailable - retry';
      button.addEventListener('click', refresh);
      return button;
    }

    button.appendChild(avatar(status, false));
    if (state === 'gate-off') {
      button.appendChild(el('span', 'identity-auth-off-label', 'Off'));
    } else if (state === 'open') {
      button.appendChild(el('span', 'identity-auth-off-label', 'Open'));
    }
    button.setAttribute('aria-label', identityName(status) + '. ' + statusLabel(status));
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', panelOpen ? 'true' : 'false');
    button.title = state === 'setup' ? 'Add a passkey' : identityName(status);
    button.addEventListener('click', togglePanel);
    return button;
  }

  function actionButton(kind, label, detail, onClick) {
    var button = el('button', 'identity-panel-action');
    button.type = 'button';
    button.disabled = lockBusy;
    button.setAttribute('data-testid', 'identity-action-' + kind);
    var iconHost = el('span', 'identity-panel-action-icon');
    iconHost.appendChild(icon(
      kind === 'add-passkey' ? 'key'
        : kind === 'manage-factors' ? 'credkey'
          : kind === 'accept-invite' ? 'link'
            : kind === 'lock' ? 'lock'
              : kind.indexOf('plugin-') === 0 ? 'machines'
                : 'lock'
    ));
    button.appendChild(iconHost);
    var copy = el('span', 'identity-panel-action-copy');
    copy.appendChild(el('span', 'identity-panel-action-label', label));
    if (detail) copy.appendChild(el('span', 'identity-panel-action-detail', detail));
    button.appendChild(copy);
    button.addEventListener('click', onClick);
    return button;
  }

  async function lockDashboard() {
    if (lockBusy) return;
    lockBusy = true;
    render();
    try {
      var signerSession = root.AutonomyNetworkSession;
      if (signerSession && typeof signerSession.signOut === 'function') {
        await signerSession.signOut();
      }
      var response = await root.fetch('/api/identity/lock', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      });
      if (!response.ok) {
        var body = await response.json().catch(function () { return {}; });
        throw new Error(body.error || 'Could not lock this dashboard session');
      }
      goToUnlock();
    } catch (error) {
      lockBusy = false;
      loadError = (error && error.message) || String(error);
      render();
    }
  }

  function orgRow(entry) {
    // /api/orgs returns {org:{slug,...}, identity:{payload:{name,byline,color,
    // initial}}, identity_resolved}. The bootstrap row and the display fields
    // are separate objects, and the first cut read both off the top level —
    // which rendered six real organizations as "?" with no names, because
    // every field it asked for was one level down.
    var bootstrap = (entry && entry.org) || {};
    var org = (entry && entry.identity && entry.identity.payload)
      || (entry && entry.identity_resolved) || {};
    var slug = bootstrap.slug || org.slug || "";
    var row = el('button', 'identity-panel-action identity-panel-org');
    row.type = 'button';
    row.setAttribute('data-testid', 'identity-org-' + slug);
    var iconHost = el('span', 'identity-panel-action-icon identity-org-mark');
    // The org's own initial rather than a generic glyph — several of these
    // sit in a list and the first thing a reader does is tell them apart.
    var initial = el('span', '', String(
      (org && org.initial) || (slug || '?').charAt(0)).toUpperCase());
    iconHost.appendChild(initial);
    if (org && org.color) iconHost.style.background = org.color;
    // The org declares a favicon and it is the better mark: a logo is
    // recognised before a letter is read, and two orgs starting with the same
    // letter are otherwise told apart only by colour.
    //
    // Layered over the initial rather than replacing it. Every org that
    // declares a favicon today serves it, but the declaration is a path in a
    // Setting and nothing checks the file is still there — an org can be
    // created naming one that was never added. On error the image removes
    // itself and the letter is already underneath, so a missing file costs
    // nothing and never leaves a broken-image glyph in the list.
    if (org && org.favicon) {
      var mark = new root.Image();
      mark.className = 'identity-org-favicon';
      mark.alt = '';
      mark.setAttribute('aria-hidden', 'true');
      mark.addEventListener('error', function () {
        if (mark.parentNode) mark.parentNode.removeChild(mark);
      });
      // Design: when a real icon loads, let it stand on its own — drop the
      // colored tile and the initial underneath, so the logo sits transparent
      // on the dark drawer. A drawn background behind a real favicon looks wrong
      // (operator, 2026-08-24). On error the tile + letter remain (see above).
      mark.addEventListener('load', function () {
        iconHost.classList.add('has-favicon');
        iconHost.style.background = 'transparent';
        initial.style.display = 'none';
      });
      mark.src = org.favicon;
      iconHost.appendChild(mark);
    }
    row.appendChild(iconHost);
    var copy = el('span', 'identity-panel-action-copy');
    copy.appendChild(el('span', 'identity-panel-action-label',
      (org && (org.name || org.display_name)) || slug));
    if (org && org.byline) {
      copy.appendChild(el('span', 'identity-panel-action-detail', org.byline));
    }
    row.appendChild(copy);
    row.addEventListener('click', function () {
      closePanel();
      if (root.AutonomyOrgSettings) root.AutonomyOrgSettings.open(slug);
    });
    return row;
  }

  function orgSection() {
    var section = el('div', 'identity-panel-orgs');
    section.setAttribute('data-testid', 'identity-orgs');
    section.appendChild(el('div', 'identity-panel-section-label', 'Organizations'));
    if (orgsError) {
      section.appendChild(el('div', 'identity-panel-error', orgsError));
      return section;
    }
    if (orgs === null) {
      section.appendChild(el('div', 'identity-panel-action-detail', 'Loading…'));
      return section;
    }
    // personal.db and machine.db are STORES, not organizations — they appear
    // in /api/orgs because that route enumerates data/orgs/*.db, which is the
    // same reason they are default peers in a read. Neither is a thing the
    // operator "belongs to", and neither has an organization settings screen.
    var listed = orgs.filter(function (e) {
      var slug = ((e && e.org) || {}).slug;
      return slug !== "personal" && slug !== "machine";
    });
    if (!listed.length) {
      // Not an error, and worth saying: an operator with no organizations
      // has one obvious next move and the panel already offers it below.
      section.appendChild(el('div', 'identity-panel-action-detail',
        'None yet. Accept an invitation to join one.'));
      return section;
    }
    listed.forEach(function (e) { section.appendChild(orgRow(e)); });
    return section;
  }

  function loadOrgs() {
    fetch('/api/orgs', { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (body) {
        orgs = (body && body.orgs) || [];
        orgsError = null;
        if (panelOpen) render();
      })
      .catch(function (err) {
        orgsError = 'Organizations unavailable: ' + ((err && err.message) || err);
        if (panelOpen) render();
      });
  }

  function flagNeeds(f) {
    if (!unlockState) return false;   // unknown / not loaded -> dim, never lit
    var s = unlockState[f.key];
    return !!(s && s.needs);
  }
  function anyFlagNeeds() {
    return FLAGS.some(function (f) { return flagNeeds(f); });
  }
  // Dim: the balloon says what the flag IS (the static description). Lit: it
  // says what is BROKEN and what stopped working — the server's live detail.
  // The remedy alone ("unlock with your root") never told the operator what
  // they'd lost; a lit balloon now leads with the loss (bead auto-sdrsa).
  function flagDetail(f) {
    if (flagNeeds(f) && unlockState) {
      var s = unlockState[f.key];
      if (s && s.detail) return String(s.detail);
    }
    return f.body;
  }
  // An optional muted trailing line — e.g. a scope that was never set up to
  // serve, a quiet fact that must not light the tile.
  function flagNote(f) {
    if (unlockState) { var s = unlockState[f.key]; if (s && s.note) return String(s.note); }
    return '';
  }
  // The liveliness readout shown green in the balloon corner: a boolean flag
  // reports "Up"; a timed one reports its remaining range ("Valid for 71 days",
  // "6 hours left"). Only when the state is actually known — unknown stays blank.
  function flagValue(f) {
    if (!unlockState) return '';
    var s = unlockState[f.key];
    if (!s) return '';
    if (typeof s.value === 'string') return s.value;
    return '';
  }
  function svgFlag(shapes) {
    var s = root.document.createElementNS(FLAG_NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    shapes.forEach(function (sh) {
      var e = root.document.createElementNS(FLAG_NS, sh[0]);
      for (var k in sh[1]) { if (Object.prototype.hasOwnProperty.call(sh[1], k)) e.setAttribute(k, sh[1][k]); }
      s.appendChild(e);
    });
    return s;
  }
  function flagTray() {
    var band = el('div', 'identity-band');
    // Restart button: a refresh glyph shown whenever ANY flag is lit, gone when
    // none is. One tap runs the whole restore start-to-finish (auth, restart,
    // re-read) with no questions in between (bead auto-sdrsa).
    if (anyFlagNeeds()) {
      var restart = el('button', 'identity-flag-restart'
        + (restartBusy ? ' busy' : ''));
      restart.type = 'button';
      restart.setAttribute('data-testid', 'identity-flag-restart');
      restart.setAttribute('aria-label', 'Restore fleet services');
      restart.title = 'Restore fleet services';
      restart.disabled = restartBusy;
      var rsvg = root.document.createElementNS(FLAG_NS, 'svg');
      rsvg.setAttribute('viewBox', '0 0 24 24');
      // A circle drawn as two arrows — the refresh/restart glyph.
      [['path', { d: 'M3.5 12a8.5 8.5 0 0 1 14.4-6.2' }],
        ['path', { d: 'M18.5 3.2v3.3h-3.3' }],
        ['path', { d: 'M20.5 12a8.5 8.5 0 0 1-14.4 6.2' }],
        ['path', { d: 'M5.5 20.8v-3.3h3.3' }]].forEach(function (sh) {
        var e = root.document.createElementNS(FLAG_NS, sh[0]);
        for (var k in sh[1]) {
          if (Object.prototype.hasOwnProperty.call(sh[1], k)) e.setAttribute(k, sh[1][k]);
        }
        rsvg.appendChild(e);
      });
      restart.appendChild(rsvg);
      restart.addEventListener('click', function (ev) {
        ev.stopPropagation();
        runRestart();
      });
      band.appendChild(restart);
    }
    var tray = el('div', 'identity-flagtray');
    tray.setAttribute('data-testid', 'identity-flagtray');
    FLAGS.forEach(function (f) {
      var tile = el('button', 'identity-fl'
        + (flagNeeds(f) ? ' needs' : '') + (openFlagId === f.id ? ' on' : ''));
      tile.type = 'button';
      tile.setAttribute('data-testid', 'identity-fl-' + f.id);
      tile.setAttribute('aria-label', f.title);
      tile.appendChild(svgFlag(f.shapes));
      tile.addEventListener('click', function (ev) {
        ev.stopPropagation();
        openFlagId = (openFlagId === f.id) ? null : f.id;
        render();
      });
      tray.appendChild(tile);
    });
    band.appendChild(tray);
    if (openFlagId) {
      var f = FLAGS.filter(function (x) { return x.id === openFlagId; })[0];
      if (f) {
        var pop = el('div', 'identity-flagpop' + (flagNeeds(f) ? ' needs' : ''));
        pop.setAttribute('data-testid', 'identity-flagpop');
        var head = el('div', 'identity-flagpop-head');
        head.appendChild(el('div', 'identity-flagpop-title', f.title));
        var val = flagValue(f);
        if (val) {
          var vEl = el('div', 'identity-flagpop-value' + (flagNeeds(f) ? ' needs' : ''), val);
          vEl.setAttribute('data-testid', 'identity-flagpop-value');
          head.appendChild(vEl);
        }
        pop.appendChild(head);
        pop.appendChild(el('div', 'identity-flagpop-body', flagDetail(f)));
        var note = flagNote(f);
        if (note) pop.appendChild(el('div', 'identity-flagpop-note', note));
        // A caret that points the floated balloon back at its tile; placed
        // horizontally after layout in positionFlagpop().
        pop.appendChild(el('div', 'identity-flagpop-caret'));
        band.appendChild(pop);
      }
    }
    return band;
  }

  // Float the open balloon under its tile and keep it on screen. Measured after
  // the panel is in the DOM; in a layout-less environment (jsdom) the reads are
  // 0 and this degrades to a fixed offset — the balloon still renders.
  function positionFlagpop() {
    if (!root || !root.document) return;
    var host = root.document.getElementById('identity-indicator');
    if (!host) return;
    var pop = host.querySelector('.identity-flagpop');
    var tile = host.querySelector('.identity-fl.on');
    if (!pop || !tile) return;
    // position: fixed — everything is in viewport coordinates, so the balloon
    // floats over the page and is never clipped by the drawer's overflow.
    var tileRect = tile.getBoundingClientRect();
    var margin = 8;
    var vw = root.innerWidth || 0;
    var vh = root.innerHeight || 0;
    var popW = pop.offsetWidth || 0;
    var popH = pop.offsetHeight || 0;
    var tileCenter = tileRect.left + tileRect.width / 2;
    var left = tileCenter - popW / 2;
    var maxLeft = vw - popW - margin;
    if (maxLeft < margin) maxLeft = margin;
    if (left > maxLeft) left = maxLeft;
    if (left < margin) left = margin;
    // Below the tile, or flipped above it when there is no room below.
    var below = true;
    var top = tileRect.bottom + 6;
    if (vh && popH && top + popH > vh - margin && tileRect.top - popH - 6 >= margin) {
      top = tileRect.top - popH - 6;
      below = false;
    }
    if (top < margin) top = margin;
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
    var caret = pop.querySelector('.identity-flagpop-caret');
    if (caret) {
      var caretLeft = tileCenter - left;
      if (popW) caretLeft = Math.max(12, Math.min(popW - 12, caretLeft));
      caret.style.left = caretLeft + 'px';
      // The caret points at the tile: up-pointing when the balloon is below,
      // down-pointing when it flipped above.
      caret.classList.toggle('is-below', !below);
    }
  }
  function loadUnlockState() {
    if (unlockStateLoading) return;
    unlockStateLoading = true;
    fetch('/api/identity/unlock-state', {
      credentials: 'same-origin', cache: 'no-store',
      headers: { 'Accept': 'application/json' },
    })
      .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
      .then(function (body) { unlockState = body || {}; unlockStateError = false; if (panelOpen) render(); })
      .catch(function () { unlockState = null; unlockStateError = true; if (panelOpen) render(); })
      .finally(function () { unlockStateLoading = false; });
  }

  function _escHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function _joinAnd(items) {
    if (!items.length) return '';
    if (items.length === 1) return items[0];
    if (items.length === 2) return items[0] + ' and ' + items[1];
    return items.slice(0, -1).join(', ') + ', and ' + items[items.length - 1];
  }
  // The structured request's reason: ONE sentence saying what is about to
  // happen, then a short list naming what it applies to (scopes per flag). The
  // list names; the balloons explain — the operator reads them before pressing
  // the button (bead auto-sdrsa).
  function restartReasonHtml() {
    function lit(key) { var s = unlockState && unlockState[key]; return !!(s && s.needs); }
    var actions = [];
    if (lit('certificates')) actions.push('renew your serving certificate');
    if (lit('tunnel')) actions.push('bring the tunnel back');
    if (lit('sync')) actions.push('give the sync processes a new credential');
    var sentence = actions.length
      ? 'This will ' + _joinAnd(actions) + ', then restart them.'
      : 'This will restart your fleet services.';
    var labels = { certificates: 'Certificate', tunnel: 'Tunnel', sync: 'Sync',
      agent: 'Delegate key', session: 'Session' };
    var rows = [];
    ['certificates', 'tunnel', 'sync', 'agent', 'session'].forEach(function (key) {
      if (!lit(key)) return;
      var s = unlockState[key] || {};
      var scopes = Array.isArray(s.scopes) ? s.scopes : [];
      rows.push('<li>' + _escHtml(labels[key])
        + (scopes.length ? ' — ' + _escHtml(scopes.join(', ')) : '') + '</li>');
    });
    return '<div class="or-restore-sentence">' + _escHtml(sentence) + '</div>'
      + (rows.length ? '<ul class="or-restore-list">' + rows.join('') + '</ul>' : '');
  }

  // The tray's restart button: one action, start to finish, no questions in
  // between. Authenticate (the structured root ceremony, with a stated reason),
  // restart everything, re-read the flags — the tray re-renders into the new
  // state. Every step after the ceremony is best-effort: a hiccup in one must
  // not strand the others, and the re-read shows whatever actually cleared.
  async function runRestart() {
    if (restartBusy) return;
    var openRootMod;
    try {
      openRootMod = await import('./ceremony/open-root.js');
    } catch (e) {
      loadError = 'Could not start the restore: ' + ((e && e.message) || e);
      render();
      return;
    }
    var opened;
    try {
      opened = await openRootMod.openRoot({
        title: 'Restore fleet services',
        detail: restartReasonHtml(),
      });
    } catch (e) {
      loadError = (e && e.message) || String(e);
      render();
      return;
    }
    if (!opened) return;   // operator cancelled — leave the tray untouched
    restartBusy = true;
    openFlagId = null;
    render();
    var seed = opened.seed;
    opened.seed = null;
    try {
      // 1. Restart the serving processes: a fresh subprocess drops the stale
      //    code and the dead in-memory credential, and comes back ready to be
      //    re-armed.
      try {
        await root.fetch('/api/fleet/restore', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
      } catch (e) { /* best-effort — the arm below still runs */ }
      // 2. Re-warm the vault (mints a fresh delegate) with the open root.
      try {
        var signon = root.AutonomyNetworkSession;
        if (signon && signon._internals
            && typeof signon._internals.wakeVault === 'function') {
          await signon._internals.wakeVault({
            personalRootSeed: new Uint8Array(seed),
          });
        }
      } catch (e) { /* best-effort */ }
      // 3. Give the now-fresh sync processes a new credential (the shared,
      //    non-diverging restore used by the unlock path too).
      try {
        var fr = await import('./ceremony/fleet-restore.js');
        await fr.restoreFleetRuntime(new Uint8Array(seed), {
          fetchImpl: function (u, o) { return root.fetch(u, o); },
          signon: root.AutonomyNetworkSession,
        });
      } catch (e) { /* best-effort */ }
    } finally {
      if (seed && seed.fill) seed.fill(0);
      restartBusy = false;
      // 4. Re-read the flags: by the time the tray renders again, they show
      //    the new state — dim, or about to be.
      unlockState = null;
      unlockStateError = false;
      render();
      loadUnlockState();
    }
  }

  function panelForState(state) {
    var panel = el('section', 'identity-panel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Dashboard identity');
    panel.setAttribute('data-testid', 'identity-panel');

    // Manage credentials takes the WHOLE panel: full screen on mobile, no
    // drawer chrome — the frame carries its own header and close. The drawer's
    // person header (name + unlock state) would only duplicate it.
    if (credentialsOpen && status && status.signed_in === true) {
      panel.classList.add('identity-panel--credentials');
      panel.appendChild(el('div', 'identity-credentials-mount'));
      return panel;
    }

    var header = el('div', 'identity-panel-header');
    header.appendChild(avatar(status, true));
    var person = el('div', 'identity-panel-person');
    person.appendChild(el('div', 'identity-panel-name', identityName(status)));
    var stateLine = el('div', 'identity-panel-status');
    stateLine.appendChild(el('span', 'identity-status-dot ' + state));
    stateLine.appendChild(el('span', '', statusLabel(status)));
    person.appendChild(stateLine);
    header.appendChild(person);
    panel.appendChild(header);

    // Status flag tray — the identity band, directly under the header (same strip
    // that appears on the locked screen). Loads its own state; absent -> all dim.
    if (state !== 'bootstrap' && state !== 'error') {
      panel.appendChild(flagTray());
      if (unlockState === null && !unlockStateError) loadUnlockState();
      // Every unlock step that failed leaves an operator-legible message in
      // this map, keyed by step (and org for per-org steps) — written by
      // ceremony/step-report.js reportStepOutcome, cleared per entry by that
      // step's next success. Render them here: the shell is the one surface
      // that survives the unlock page's post-sign-in navigation, so a failed
      // vault wake or seed-mint is visible without DevTools (auto-uhdxm).
      // This file loads as a classic script and cannot import, so the key is
      // literal — keep it in step with STEP_FAILURES_STORAGE_KEY.
      try {
        var rawFailures = sessionStorage.getItem('autonomy.unlock.step-failures');
        var failures = rawFailures ? JSON.parse(rawFailures) : null;
        if (failures && typeof failures === 'object') {
          Object.keys(failures).sort().forEach(function (entry) {
            var message = failures[entry];
            if (typeof message === 'string' && message) {
              panel.appendChild(el('div', 'identity-panel-error', message));
            }
          });
        }
      } catch (e) { /* storage unavailable or unparseable — render nothing */ }
    }

    if (state === 'gate-off') {
      panel.appendChild(el('div', 'identity-panel-notice',
        'This dashboard is open because authentication is disabled.'));
    } else if (state === 'open') {
      panel.appendChild(el('div', 'identity-panel-notice',
        'Access is open. This browser is not authenticated.'));
    }

    // Only where there is an identity to have organizations. In bootstrap
    // or locked states the panel's job is to get past that first.
    if (state !== 'bootstrap' && state !== 'locked' && state !== 'error') {
      panel.appendChild(orgSection());
      if (orgs === null && !orgsError) loadOrgs();
    }

    var actions = el('div', 'identity-panel-actions');
    identityMenuPlugins(root && root.Autonomy && root.Autonomy.plugins)
      .forEach(function (plugin) {
        actions.appendChild(actionButton(
          'plugin-' + plugin.id, plugin.label, plugin.identity_detail || '', function () {
            closePanel();
            // Let the panel's close render commit before the SPA navigation
            // replaces the page.  Without this frame the detached identity
            // menu can remain visually latched over plug-in pages.
            // A full navigation is intentional for plug-in pages: the SPA
            // router can preserve the identity-menu host over the new view.
            root.setTimeout(function () {
              root.location.assign(plugin.path);
            }, 0);
          }
        ));
      });
    if (state === 'setup') {
      actions.appendChild(actionButton('add-passkey', 'Add a passkey',
        'Finish setup', function () { openOnboarding(2); }));
    }
    if (status && status.signed_in === true && status.gate_disabled !== true) {
      actions.appendChild(actionButton('manage-factors', 'Manage credentials',
        'Your password and passkeys', function () { openCredentials(); }));
      actions.appendChild(actionButton('lock', lockBusy ? 'Locking...' : 'Lock dashboard',
        'This session only', lockDashboard));
    }
    // Accept invitation (auto-a1xq3): opens the full-page flow — the
    // paste screen at /network/join owns the input and everything after.
    actions.appendChild(actionButton('accept-invite', 'Accept invitation',
      'Paste an invitation link', function () {
        root.location.assign('/network/join');
      }));
    if (actions.childNodes.length) panel.appendChild(actions);
    if (loadError) panel.appendChild(el('div', 'identity-panel-error', loadError));
    return panel;
  }

  function render() {
    if (!root || !root.document) return;
    var host = root.document.getElementById('identity-indicator');
    if (!host) return;
    // While the credentials sub-view is mounted, external re-renders (status
    // refresh, org load) must not rebuild the drawer and tear it down.
    if (credentialsOpen && credentialsMounted) return;
    var state = loadError && !status ? 'error' : deriveIdentityState(status);
    if (state === 'bootstrap' || state === 'locked' || state === 'error') {
      panelOpen = false;
    }
    host.textContent = '';
    host.setAttribute('data-state', state);
    host.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
    if (host.parentElement) {
      host.parentElement.setAttribute('data-identity-state', state);
    }
    host.appendChild(triggerForState(state));
    if (panelOpen) {
      var backdrop = el('button', 'identity-mobile-backdrop');
      backdrop.type = 'button';
      backdrop.setAttribute('aria-label', 'Close identity panel');
      backdrop.addEventListener('click', closePanel);
      host.appendChild(backdrop);
      host.appendChild(panelForState(state));
      if (credentialsOpen && !credentialsMounted) {
        var mount = host.querySelector('.identity-credentials-mount');
        if (mount) {
          credentialsMounted = true;
          import('./factor-management.js')
            .then(function (m) {
              m.open({ mount: mount, onBack: closeCredentials, onClose: closeCredentials });
            })
            .catch(function (err) {
              credentialsMounted = false; credentialsOpen = false;
              loadError = 'Credential management unavailable: ' + ((err && err.message) || err);
              render();
            });
        }
      }
      // Float the open flag balloon under its tile now the panel is in the DOM.
      if (openFlagId) positionFlagpop();
    }
  }

  async function refresh() {
    loadError = null;
    if (!status) render();
    try {
      // Personal identity is deliberately headerless. Autonomy.fetch stamps
      // X-Graph-Org for app/plugin data and must never be used here.
      var response = await root.fetch('/api/identity/status', {
        credentials: 'same-origin', cache: 'no-store',
        headers: { 'Accept': 'application/json' },
      });
      var body = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(body.error || 'Identity status is unavailable');
      status = body;
      applyOperatorIdentity(status);
    } catch (error) {
      loadError = (error && error.message) || String(error);
    }
    render();
    maybeGreetPendingEnrollment();
    return status;
  }

  // A passkey login detected this device needs enrollment (unlock.js stashed
  // it and landed on the shell home). The drawer must greet WITHOUT a click —
  // and stick: render() force-closes the panel for bootstrap/locked/error
  // states, so attempt only once the status state can host a panel, retrying
  // on later refreshes until it takes. One-shot per page load; the stash is
  // consumed by the enrollment itself.
  var greetAttempted = false;
  function maybeGreetPendingEnrollment() {
    if (greetAttempted || panelOpen) return;
    var pending = false;
    try {
      pending = !!(root.sessionStorage
        && (root.sessionStorage.getItem('autonomy.factor.pending-slot')
          || root.sessionStorage.getItem('autonomy.factor.slot-enrolled')
          || root.sessionStorage.getItem('autonomy.factor.open-credentials')));
    } catch (e) { pending = false; }
    // An empty check must NOT burn the one-shot: on a PWA-restored shell the
    // first refreshes run before any login writes the stash, and a stash that
    // appears later (login elsewhere, app resume) must still greet. Only an
    // actual greeting consumes greetAttempted.
    if (!pending) { return; }
    var state = loadError && !status ? 'error' : deriveIdentityState(status);
    if (state === 'loading' || state === 'bootstrap' || state === 'locked' || state === 'error') {
      return;   // not yet — retry on the next refresh
    }
    greetAttempted = true;
    panelOpen = true;
    // the recovery-landing flag is a one-shot "open credentials" with no
    // dialog; consume it so a later reload doesn't reopen the panel.
    try { root.sessionStorage.removeItem('autonomy.factor.open-credentials'); } catch (e) { /* ignore */ }
    openCredentials();
  }

  // Threads the already-known personal display_name into the globals
  // surface-presence.js's _resolveOperatorId/_resolveOperatorLabel read
  // (window.Autonomy.operatorId/operatorLabel) -- closing a wiring gap
  // that today leaves every plugin's presence stack without the human
  // viewer's own row, even though auth already knows a name. One stable
  // literal id: this is a single-personal-root gate, not multi-user yet
  // (that's the separate, deferred org-member-identity epic), so a
  // constant is the honest representation, not a synthetic derived id.
  // Gated on signed_in, not just personal_identity being present, so an
  // unlocked-but-not-authenticated ('open'/'gate-off') session doesn't
  // claim a live presence identity it hasn't actually proven.
  function applyOperatorIdentity(value) {
    if (!value || value.signed_in !== true) return;
    var name = value.personal_identity && value.personal_identity.display_name;
    if (typeof name !== 'string' || !name.trim()) return;
    root.Autonomy = root.Autonomy || {};
    root.Autonomy.operatorId = 'operator:personal';
    root.Autonomy.operatorLabel = name.trim();
  }

  function init() {
    if (initialized || !root || !root.document) return;
    initialized = true;
    var indicatorHost = root.document.getElementById('identity-indicator');
    if (indicatorHost) {
      // Rendering replaces the clicked trigger. Stop at the stable host so
      // the document's outside-click listener cannot mistake that detached
      // trigger for a click outside the newly opened panel.
      indicatorHost.addEventListener('click', function (event) {
        event.stopPropagation();
      });
    }
    // Keep one live identity component and move it between the two shell
    // homes at the same breakpoint that pins/unpins navigation. Moving the
    // node preserves focus and open-panel state; cloning it would create two
    // independently refreshed representations of the operator.
    var headerHome = indicatorHost && indicatorHost.parentNode;
    var sidebarHome = root.document.getElementById('identity-sidebar-slot');
    var desktopQuery = root.matchMedia && root.matchMedia('(min-width: 768px)');
    function syncPlacement() {
      if (!indicatorHost || !headerHome || !sidebarHome || !desktopQuery) return;
      var target = desktopQuery.matches ? sidebarHome : headerHome;
      if (indicatorHost.parentNode !== target) target.appendChild(indicatorHost);
    }
    syncPlacement();
    if (desktopQuery) {
      if (typeof desktopQuery.addEventListener === 'function') {
        desktopQuery.addEventListener('change', syncPlacement);
      } else if (typeof desktopQuery.addListener === 'function') {
        desktopQuery.addListener(syncPlacement);
      }
    }
    root.addEventListener('resize', syncPlacement);
    root.document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closePanel();
    });
    root.document.addEventListener('click', function (event) {
      if (!panelOpen) return;
      var host = root.document.getElementById('identity-indicator');
      if (host && !host.contains(event.target)) closePanel();
    });
    // A flag balloon closes when you tap anywhere but its own tile or itself.
    // Capture phase, because the indicator host stops click propagation on the
    // bubble to guard the panel's outside-click closer — so a bubble listener
    // here would never see in-panel taps (bead auto-sdrsa). A tap on a tray tile
    // is left to the tile's own handler (which toggles/switches balloons).
    root.document.addEventListener('click', function (event) {
      if (!openFlagId) return;
      var host = root.document.getElementById('identity-indicator');
      if (!host) return;
      var pop = host.querySelector('.identity-flagpop');
      var tray = host.querySelector('.identity-flagtray');
      var target = event.target;
      if (pop && pop.contains(target)) return;    // inside the balloon: keep open
      if (tray && tray.contains(target)) return;  // a tile: its handler decides
      openFlagId = null;
      render();
    }, true);
    root.document.addEventListener('visibilitychange', function () {
      if (root.document.visibilityState === 'visible') refresh();
    });
    root.addEventListener('autonomy:identity-changed', refresh);
    root.addEventListener('autonomy:plugins-changed', function () {
      if (panelOpen) render();
    });
    refresh();
  }

  function identityMenuPlugins(plugins) {
    if (!Array.isArray(plugins)) return [];
    return plugins.filter(function (plugin) {
      return plugin && plugin.identity_menu === true
        && typeof plugin.id === 'string' && plugin.id
        && typeof plugin.path === 'string' && plugin.path
        && typeof plugin.label === 'string' && plugin.label;
    });
  }

  if (root && root.document) {
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  }

  return {
    init: init,
    refresh: refresh,
    close: closePanel,
    deriveIdentityState: deriveIdentityState,
    identityName: identityName,
    identityInitial: identityInitial,
    methodLabel: methodLabel,
    statusLabel: statusLabel,
    identityMenuPlugins: identityMenuPlugins,
    _state: function () {
      return { status: status, error: loadError, panelOpen: panelOpen, lockBusy: lockBusy };
    },
  };
});
