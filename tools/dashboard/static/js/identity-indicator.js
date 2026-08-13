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
    inviteOpen = false;
    render();
  }

  function togglePanel() {
    panelOpen = !panelOpen;
    if (!panelOpen) inviteOpen = false;
    render();
  }

  // ── Accept invitation (auto-a1xq3, operator-ruled paste ingress) ──
  // Chrome only: parse/navigate logic lives in accept-invitation.js,
  // which is structurally network- and storage-free. The pasted value is
  // never sent, never persisted, and dies with the row (no draft state).
  var inviteOpen = false;

  function buildInviteRow() {
    var row = el('div', 'identity-panel-invite');
    var input = el('input', 'identity-panel-invite-input');
    input.type = 'url';
    input.placeholder = 'Paste your invitation link';
    input.autocomplete = 'off';
    input.setAttribute('data-testid', 'identity-invite-input');
    var hint = el('div', 'identity-panel-invite-hint');
    var go = el('button', 'identity-panel-action identity-panel-invite-go');
    go.type = 'button';
    go.textContent = 'Accept';
    go.setAttribute('data-testid', 'identity-invite-go');
    go.addEventListener('click', function () {
      var api = root.AutonomyAcceptInvitation;
      if (!api) { hint.textContent = 'Invitation handling failed to load.'; return; }
      var result = api.acceptPastedLink(input.value);
      if (result.kind === 'error') hint.textContent = result.reason;
    });
    row.appendChild(input);
    row.appendChild(go);
    row.appendChild(hint);
    return row;
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
    iconHost.appendChild(icon(kind === 'add-passkey' ? 'key' : 'lock'));
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

    var actions = el('div', 'identity-panel-actions');
    if (state === 'setup') {
      actions.appendChild(actionButton('add-passkey', 'Add a passkey',
        'Finish setup', function () { openOnboarding(2); }));
    }
    if (status && status.signed_in === true && status.gate_disabled !== true) {
      actions.appendChild(actionButton('lock', lockBusy ? 'Locking...' : 'Lock dashboard',
        'This session only', lockDashboard));
    }
    actions.appendChild(actionButton('accept-invite', 'Accept invitation',
      'Paste an invitation link', function () {
        inviteOpen = !inviteOpen;
        render();
      }));
    if (actions.childNodes.length) panel.appendChild(actions);
    if (inviteOpen) panel.appendChild(buildInviteRow());
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
    refresh();
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
    _state: function () {
      return { status: status, error: loadError, panelOpen: panelOpen, lockBusy: lockBusy };
    },
  };
});
