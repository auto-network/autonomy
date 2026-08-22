(function () {
  'use strict';

  const INSTALLATION_KEY = 'autonomy:web-push-installation:v1';

  function supported() {
    return window.isSecureContext && 'serviceWorker' in navigator &&
      'PushManager' in window && 'Notification' in window;
  }

  function iosNeedsHomeScreen() {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    return ios && navigator.standalone !== true;
  }

  function installationId() {
    let value = localStorage.getItem(INSTALLATION_KEY);
    if (value) return value;
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      value = window.crypto.randomUUID();
    } else {
      const bytes = new Uint8Array(24);
      window.crypto.getRandomValues(bytes);
      value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }
    localStorage.setItem(INSTALLATION_KEY, value);
    return value;
  }

  function applicationServerKey(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, ch => ch.charCodeAt(0));
  }

  function sameBytes(left, right) {
    if (!left || !right) return false;
    const a = new Uint8Array(left);
    const b = new Uint8Array(right);
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  async function registration() {
    await navigator.serviceWorker.register('/service-worker.js', {
      scope: '/',
      updateViaCache: 'none',
    });
    return navigator.serviceWorker.ready;
  }

  async function state() {
    if (!supported()) return { state: 'unsupported', label: 'Not supported by this browser' };
    if (iosNeedsHomeScreen()) {
      return { state: 'needs_install', label: 'Add Autonomy to the Home Screen first' };
    }
    const permission = Notification.permission;
    if (permission === 'denied') {
      return { state: 'denied', label: 'Blocked in browser or system settings' };
    }
    const reg = await registration();
    const local = await reg.pushManager.getSubscription();
    const response = await fetch('/api/web-push/state?installation_id=' +
      encodeURIComponent(installationId()), { credentials: 'same-origin' });
    const server = await response.json();
    if (!response.ok) throw new Error(server.error || 'Could not read device-alert state');
    const active = server.this_installation && server.this_installation.status === 'active';
    if (permission === 'granted' && local && active) {
      return { state: 'subscribed', label: 'Enabled on this installed app', activeInstallations: server.active_installations };
    }
    if (permission === 'granted' && local && !active) {
      return { state: 'needs_enroll', label: 'Ready to connect to this operator', activeInstallations: server.active_installations };
    }
    return { state: 'prompt', label: 'Off on this installed app', activeInstallations: server.active_installations };
  }

  async function enroll() {
    if (!supported()) throw new Error('This browser does not support Web Push.');
    if (iosNeedsHomeScreen()) throw new Error('Open Safari, add Autonomy to the Home Screen, then use the installed app.');
    let permission = Notification.permission;
    if (permission === 'default') permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Notification permission was not granted.');
    const reg = await registration();
    const configResponse = await fetch('/api/web-push/config', { credentials: 'same-origin' });
    const config = await configResponse.json();
    if (!configResponse.ok) throw new Error(config.error || 'Web Push is unavailable.');
    const serverKey = applicationServerKey(config.application_server_key);
    let subscription = await reg.pushManager.getSubscription();
    // The temporary proof used the same origin/root registration with a
    // deliberately separate VAPID key. Replace that subscription explicitly;
    // sending it with the production key would produce VapidPkHashMismatch.
    if (subscription && !sameBytes(
      subscription.options && subscription.options.applicationServerKey,
      serverKey,
    )) {
      await subscription.unsubscribe();
      subscription = null;
    }
    if (!subscription) {
      subscription = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: serverKey,
      });
    }
    const response = await fetch('/api/web-push/subscriptions', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        installation_id: installationId(),
        subscription: subscription.toJSON(),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not enroll this installed app.');
    return state();
  }

  async function sendTest() {
    const response = await fetch('/api/web-push/test', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not queue the test alert.');
    return result;
  }

  async function disable() {
    if (!supported()) return state();
    const id = installationId();
    const response = await fetch('/api/web-push/subscriptions/' + encodeURIComponent(id), {
      method: 'DELETE', credentials: 'same-origin',
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not retire device alerts.');
    const reg = await registration();
    const subscription = await reg.pushManager.getSubscription();
    if (subscription) await subscription.unsubscribe();
    const notifications = await reg.getNotifications();
    notifications.forEach(item => item.close());
    if (navigator.clearAppBadge) {
      try { await navigator.clearAppBadge(); } catch (_) {}
    }
    return state();
  }

  async function acknowledgeApproval(approvalId) {
    if (!approvalId || document.visibilityState !== 'visible') return false;
    const response = await fetch('/api/web-push/attention/' +
      encodeURIComponent(approvalId) + '/ack', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_version: 1, applied: true }),
      });
    return response.ok;
  }

  window.AutonomyWebPush = {
    state,
    enroll,
    sendTest,
    disable,
    acknowledgeApproval,
    supported,
  };

  if (supported()) {
    registration().catch(error => {
      console.warn('Web Push service worker registration failed:', error);
    });
  }
})();
