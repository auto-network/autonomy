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
  var lockBusy = false;
  var initialized = false;

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
    render();
  }

  function togglePanel() {
    panelOpen = !panelOpen;
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

  function panelForState(state) {
    var panel = el('section', 'identity-panel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Dashboard identity');
    panel.setAttribute('data-testid', 'identity-panel');

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
            if (typeof root.navigateTo === 'function') {
              root.navigateTo(plugin.path);
            } else {
              root.location.assign(plugin.path);
            }
          }
        ));
      });
    if (state === 'setup') {
      actions.appendChild(actionButton('add-passkey', 'Add a passkey',
        'Finish setup', function () { openOnboarding(2); }));
    }
    if (status && status.signed_in === true && status.gate_disabled !== true) {
      actions.appendChild(actionButton('add-passkey', 'Add a passkey',
        'Enroll this device', function () { openOnboarding(2); }));
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
    return status;
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
    root.document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closePanel();
    });
    root.document.addEventListener('click', function (event) {
      if (!panelOpen) return;
      var host = root.document.getElementById('identity-indicator');
      if (host && !host.contains(event.target)) closePanel();
    });
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
