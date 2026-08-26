(function () {
  'use strict';

  const INSTALLATION_KEY = 'autonomy:web-push-installation:v1';
  const DEVICE_DB = 'autonomy-web-push-v1';
  const DEVICE_STORE = 'device';
  const DEVICE_KEY = 'current';
  let subscribing = false;

  const STATE_LABELS = {
    unsupported: 'Notifications are not supported in this browser.',
    requires_install: 'Add Autonomy to the Home Screen first.',
    permission_default: 'Phone alerts are off on this installed app.',
    permission_denied: 'Notifications are blocked in browser or system settings.',
    subscribing: 'Connecting this installed app…',
    active: 'Enabled on this installed app.',
    stale: 'This device needs to reconnect phone alerts.',
    revoked: 'Phone alerts were retired for this installed app.',
    error_retryable: 'Phone-alert state is temporarily unavailable.',
  };

  function result(state, extra) {
    return Object.assign({ state, label: STATE_LABELS[state] }, extra || {});
  }

  function supported() {
    return Boolean(window.isSecureContext && 'serviceWorker' in navigator &&
      'PushManager' in window && 'Notification' in window &&
      'indexedDB' in window && 'TextEncoder' in window &&
      'MessageChannel' in window && window.crypto && window.crypto.subtle);
  }

  function iosNeedsHomeScreen() {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    return ios && navigator.standalone !== true;
  }

  function installationId() {
    let value = localStorage.getItem(INSTALLATION_KEY);
    if (value && /^[A-Za-z0-9_-]{16,128}$/.test(value)) return value;
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      value = window.crypto.randomUUID();
    } else {
      const bytes = new Uint8Array(24);
      window.crypto.getRandomValues(bytes);
      value = Array.from(bytes, byte =>
        byte.toString(16).padStart(2, '0')).join('');
    }
    localStorage.setItem(INSTALLATION_KEY, value);
    return value;
  }

  function applicationServerKey(value) {
    if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{80,120}$/.test(value)) {
      throw new Error('The Web Push application key is invalid.');
    }
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    const key = Uint8Array.from(raw, character => character.charCodeAt(0));
    if (key.length !== 65 || key[0] !== 4) {
      throw new Error('The Web Push application key is invalid.');
    }
    return key;
  }

  function sameBytes(left, right) {
    if (!left || !right) return false;
    const a = new Uint8Array(left);
    const b = new Uint8Array(right);
    if (a.length !== b.length) return false;
    for (let index = 0; index < a.length; index += 1) {
      if (a[index] !== b[index]) return false;
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

  function openDeviceDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DEVICE_DB, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(DEVICE_STORE)) {
          request.result.createObjectStore(DEVICE_STORE);
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('Device-alert storage is unavailable.'));
      request.onblocked = () => reject(new Error('Device-alert storage is blocked.'));
    });
  }

  async function deviceStore(mode, operation) {
    const db = await openDeviceDb();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction(DEVICE_STORE, mode);
        const store = transaction.objectStore(DEVICE_STORE);
        let value;
        try { value = operation(store); } catch (error) { reject(error); return; }
        transaction.oncomplete = () => resolve(value && value.result);
        transaction.onerror = () => reject(
          transaction.error || new Error('Device-alert storage failed.'),
        );
        transaction.onabort = () => reject(
          transaction.error || new Error('Device-alert storage was aborted.'),
        );
      });
    } finally {
      db.close();
    }
  }

  function readDeviceRecord() {
    return deviceStore('readonly', store => store.get(DEVICE_KEY));
  }

  function deleteDeviceRecord() {
    return deviceStore('readwrite', store => store.delete(DEVICE_KEY));
  }

  function validStoredRecord(record) {
    return Boolean(
      record && record.v === 1 &&
      typeof record.device_id === 'string' &&
      /^[A-Za-z0-9_-]{16,128}$/.test(record.device_id) &&
      typeof record.update_token === 'string' &&
      /^[A-Za-z0-9_-]{43,128}$/.test(record.update_token) &&
      Number.isSafeInteger(record.token_version) && record.token_version >= 1 &&
      typeof record.endpoint_hash === 'string' &&
      /^[a-f0-9]{64}$/.test(record.endpoint_hash) &&
      typeof record.vapid_key_id === 'string' &&
      /^[A-Za-z0-9_-]{1,128}$/.test(record.vapid_key_id) &&
      typeof record.application_server_key === 'string' &&
      /^[A-Za-z0-9_-]{80,120}$/.test(record.application_server_key) &&
      typeof record.pending_refresh === 'boolean'
    );
  }

  async function endpointHash(endpoint) {
    const digest = await window.crypto.subtle.digest(
      'SHA-256', new TextEncoder().encode(endpoint),
    );
    return Array.from(new Uint8Array(digest), byte =>
      byte.toString(16).padStart(2, '0')).join('');
  }

  function serializedSubscription(subscription, vapidKeyId) {
    const raw = subscription && subscription.toJSON();
    if (!raw || !raw.endpoint || !raw.keys || !raw.keys.p256dh || !raw.keys.auth) {
      throw new Error('The browser returned an incomplete PushSubscription.');
    }
    return {
      endpoint: raw.endpoint,
      expiration_time: raw.expirationTime == null ? null : raw.expirationTime,
      keys: { p256dh: raw.keys.p256dh, auth: raw.keys.auth },
      vapid_key_id: vapidKeyId,
    };
  }

  async function jsonRequest(url, options) {
    const response = await fetch(url, Object.assign({
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    }, options || {}));
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || ('Request failed with HTTP ' + response.status));
      error.status = response.status;
      error.payload = body;
      throw error;
    }
    return body;
  }

  function browserHints() {
    const ua = navigator.userAgent;
    const ios = /iPad|iPhone|iPod/.test(ua) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const platform = ios ? 'ios' :
      /Android/.test(ua) ? 'android' : 'desktop';
    const browser = /Firefox\//.test(ua) ? 'firefox' :
      /CriOS|Chrome\//.test(ua) ? 'chromium' :
        /Safari\//.test(ua) ? 'safari' : 'unknown';
    return { platform_family: platform, browser_family: browser };
  }

  function subscriptionApplicationKey(subscription) {
    const raw = subscription && subscription.options &&
      subscription.options.applicationServerKey;
    if (!raw) throw new Error('The PushSubscription has no application key.');
    const binary = Array.from(new Uint8Array(raw), byte =>
      String.fromCharCode(byte)).join('');
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  async function workerCommand(reg, type, data, timeoutMs) {
    const worker = reg.active || navigator.serviceWorker.controller;
    if (!worker) throw new Error('The active service worker is unavailable.');
    return new Promise((resolve, reject) => {
      const channel = new MessageChannel();
      const timer = setTimeout(() => {
        channel.port1.close();
        reject(new Error('The service worker did not finish device reconciliation.'));
      }, timeoutMs || 12000);
      channel.port1.onmessage = (event) => {
        clearTimeout(timer);
        channel.port1.close();
        resolve(event.data || { ok: false, reason: 'unavailable' });
      };
      worker.postMessage(Object.assign({ type }, data || {}), [channel.port2]);
    });
  }

  async function config() {
    return jsonRequest('/api/web-push/config');
  }

  async function bindDevice(reg, subscription, vapidKeyId, publicKey, maxDetail) {
    const deviceId = installationId();
    const serialized = serializedSubscription(subscription, vapidKeyId);
    const enrolled = await jsonRequest(
      '/api/web-push/devices/' + encodeURIComponent(deviceId),
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({
          subscription: serialized,
          max_detail: maxDetail === 'descriptive' ? 'descriptive' : 'generic',
        }, browserHints())),
      },
    );
    const record = {
      v: 1,
      device_id: deviceId,
      update_token: enrolled.device_update_token,
      token_version: enrolled.token_version,
      endpoint_hash: await endpointHash(serialized.endpoint),
      vapid_key_id: vapidKeyId,
      application_server_key: publicKey,
      pending_refresh: false,
    };
    if (
      enrolled.device_id !== deviceId || enrolled.status !== 'active' ||
      !validStoredRecord(record)
    ) {
      throw new Error('The Web Push enrollment response is invalid.');
    }
    // Validate the public key encoding before making the new updater token
    // durable. This also catches a malformed authenticated recovery payload.
    applicationServerKey(record.application_server_key);
    const stored = await workerCommand(reg, 'web-push-store-device', { record });
    if (!stored.ok) {
      throw new Error('The service worker could not store the device credential.');
    }
    return record;
  }

  async function state() {
    if (!supported()) return result('unsupported');
    if (iosNeedsHomeScreen()) return result('requires_install');
    if (subscribing) return result('subscribing');
    if (Notification.permission !== 'granted') {
      // Permission can be revoked outside the app. If this installation was
      // previously enrolled, retire it with its narrow updater credential;
      // never prompt and never borrow the current page's operator identity.
      try {
        const reg = await registration();
        if (await readDeviceRecord()) {
          const retired = await workerCommand(reg, 'web-push-forget-local', {
            require_token_retirement: true,
          });
          if (!retired.ok) {
            const deviceId = installationId();
            const server = await config();
            const device = (server.devices || []).find(
              item => item.device_id === deviceId,
            );
            if (device && device.status === 'active') {
              await jsonRequest(
                '/api/web-push/devices/' + encodeURIComponent(deviceId),
                { method: 'DELETE', headers: { 'Content-Type': 'application/json' } },
              );
              await workerCommand(reg, 'web-push-forget-local', {
                require_token_retirement: false,
              });
            } else if (device) {
              await workerCommand(reg, 'web-push-forget-local', {
                require_token_retirement: false,
              });
            }
          }
        }
      } catch (_error) {
        // Keep the normative browser permission state. The stored pending
        // updater record makes retirement retryable on the next app open.
      }
      return result(
        Notification.permission === 'denied'
          ? 'permission_denied' : 'permission_default',
      );
    }
    try {
      const reg = await registration();
      const reconciliation = await workerCommand(reg, 'web-push-reconcile');
      if (!reconciliation.ok && reconciliation.reason === 'network') {
        return result('error_retryable');
      }
      const [local, initialStored, server] = await Promise.all([
        reg.pushManager.getSubscription(), readDeviceRecord(), config(),
      ]);
      let stored = initialStored;
      const deviceId = installationId();
      const device = (server.devices || []).find(item => item.device_id === deviceId);
      if (device && (device.status === 'retired' || device.health === 'revoked')) {
        let localCleanupPending = false;
        try {
          const cleanup = await workerCommand(reg, 'web-push-forget-local', {
            require_token_retirement: false,
          });
          localCleanupPending = !cleanup.ok;
        } catch (_error) {
          localCleanupPending = true;
        }
        return result('revoked', {
          device,
          local_cleanup_pending: localCleanupPending,
        });
      }
      if (!local && device && device.status === 'active') {
        // Browser permission/subscription loss is authoritative for this
        // installation. Config has already proven that the row belongs to the
        // currently authenticated stable operator, so retire it without using
        // any caller-supplied owner identity.
        await jsonRequest(
          '/api/web-push/devices/' + encodeURIComponent(deviceId),
          { method: 'DELETE', headers: { 'Content-Type': 'application/json' } },
        );
        try {
          await workerCommand(reg, 'web-push-forget-local', {
            require_token_retirement: false,
          });
        } catch (_error) {}
        return result('revoked', {
          device: Object.assign({}, device, { status: 'retired' }),
        });
      }
      if (
        local && device && device.status === 'active' &&
        !validStoredRecord(stored)
      ) {
        // The server may have committed a token rotation immediately before
        // this browser crashed. Because config lists only the current stable
        // operator's devices, authenticated re-enrollment repairs that narrow
        // credential without making an endpoint transferable across people.
        stored = await bindDevice(
          reg,
          local,
          device.vapid_key_id,
          subscriptionApplicationKey(local),
          device.max_detail,
        );
      }
      if (!local || !stored || !device || device.status !== 'active') {
        return result('stale', { device: device || null });
      }
      if (
        stored.v !== 1 || stored.device_id !== deviceId ||
        stored.vapid_key_id !== device.vapid_key_id || stored.pending_refresh ||
        stored.endpoint_hash !== await endpointHash(local.endpoint) ||
        !sameBytes(
          local.options && local.options.applicationServerKey,
          applicationServerKey(stored.application_server_key),
        )
      ) {
        return result('stale', { device });
      }
      return result('active', {
        device,
        activeInstallations: (server.devices || []).filter(
          item => item.status === 'active',
        ).length,
      });
    } catch (_error) {
      return result('error_retryable');
    }
  }

  async function enable() {
    if (!supported()) return result('unsupported');
    if (iosNeedsHomeScreen()) return result('requires_install');
    if (Notification.permission === 'denied') return result('permission_denied');
    if (subscribing) return result('subscribing');
    subscribing = true;
    try {
      // This is the only permission request in the transport. Central calls
      // enable() directly from its explained user gesture; startup never does.
      let permission = Notification.permission;
      if (permission === 'default') permission = await Notification.requestPermission();
      if (permission === 'denied') return result('permission_denied');
      if (permission !== 'granted') return result('permission_default');

      const reg = await registration();
      const server = await config();
      const serverKey = applicationServerKey(server.vapid.public_key);
      let subscription = await reg.pushManager.getSubscription();
      // The proof app and a rotated active key can share this origin. Explicit
      // enable replaces a mismatched subscription; otherwise the push service
      // rejects publication with VapidPkHashMismatch.
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
      const deviceId = installationId();
      await bindDevice(
        reg, subscription, server.vapid.key_id, server.vapid.public_key, 'generic',
      );
      return result('active', { device: { device_id: deviceId, status: 'active' } });
    } finally {
      subscribing = false;
    }
  }

  async function forget() {
    if (!supported()) return result('unsupported');
    const reg = await registration();
    const deviceId = installationId();
    let serverRetired = false;
    let localCleanupPending = false;
    try {
      await jsonRequest('/api/web-push/devices/' + encodeURIComponent(deviceId), {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
      });
      serverRetired = true;
    } catch (error) {
      // A 404 may mean this shared browser still belongs to the prior stable
      // operator. Let the narrow updater token settle that binding before any
      // local cleanup. Other failures preserve everything for a later retry.
      if (error.status !== 404) throw error;
    }
    try {
      const forgotten = await workerCommand(reg, 'web-push-forget-local', {
        require_token_retirement: !serverRetired,
      });
      if (!forgotten.ok) throw new Error('Local device cleanup is unavailable.');
    } catch (error) {
      if (!serverRetired) throw error;
      const subscription = await reg.pushManager.getSubscription();
      if (subscription) {
        try { await subscription.unsubscribe(); }
        catch (_ignored) { localCleanupPending = true; }
      }
      try {
        const notifications = await reg.getNotifications();
        notifications.forEach(item => item.close());
      } catch (_ignored) {
        localCleanupPending = true;
      }
      try { await deleteDeviceRecord(); }
      catch (_ignored) { localCleanupPending = true; }
    }
    if (navigator.clearAppBadge) {
      try { await navigator.clearAppBadge(); }
      catch (_error) { localCleanupPending = true; }
    }
    return result('revoked', { local_cleanup_pending: localCleanupPending });
  }

  async function sendTest() {
    return jsonRequest('/api/web-push/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
  }

  // The foreground approval bridge is migration-only until Central .23 moves
  // exact event/version applied-rendered acknowledgment into its private API.
  async function acknowledgeApproval(approvalId) {
    if (!approvalId || document.visibilityState !== 'visible') return false;
    const response = await fetch(
      '/api/web-push/attention/' + encodeURIComponent(approvalId) + '/ack',
      {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_version: 1, applied: true }),
      },
    );
    return response.ok;
  }

  window.AutonomyWebPush = {
    state,
    enable,
    forget,
    // Temporary method-name compatibility for the two landed Central callers;
    // all returned states use only the normative nine-state vocabulary.
    enroll: enable,
    disable: forget,
    sendTest,
    acknowledgeApproval,
    supported,
  };

  if (supported()) {
    registration().catch((error) => {
      console.warn('Web Push service worker registration failed:', error);
    });
  }
})();
