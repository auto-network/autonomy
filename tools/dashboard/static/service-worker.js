'use strict';

// Push-only worker: no fetch handler, Cache Storage, offline shell, or
// navigation fallback.  Dashboard HTML and routing remain server-authoritative.
const DEVICE_DB = 'autonomy-web-push-v1';
const DEVICE_STORE = 'device';
const DEVICE_KEY = 'current';
const MAX_PAYLOAD_BYTES = 2048;
const GENERIC_TITLE = 'Autonomy needs your attention';
const GENERIC_BODY = 'Open the dashboard to review.';
const FALLBACK_TAG = 'autonomy-attention';
const REGISTERED_CLASSES = new Set([
  'approval.commit_sign.requested',
  'approval.jira_write.requested',
  'approval.link_publish.requested',
  'approval.link_revoke.requested',
  'approval.dashboard_access.requested',
  'approval.visitor_token.requested',
  'approval.secure_setting.requested',
  'approval.mcp_peer_link.requested',
  'approval.mcp_crosstalk.requested',
  'approval.fleet_machine_admission.requested',
  'approval.external_service_access.requested',
  'approval.vault_open.requested',
]);
let refreshChain = Promise.resolve();

function serializeRefresh(operation) {
  const running = refreshChain.then(operation, operation);
  refreshChain = running.catch(() => {});
  return running;
}

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await self.clients.claim();
    try { await serializeRefresh(reconcileStoredSubscription); } catch (_error) {}
  })());
});

function safeRoute(value) {
  if (value === '/activity') return value;
  if (typeof value !== 'string' || value.length > 320) return '/activity';
  try {
    const url = new URL(value, self.location.origin);
    if (url.origin !== self.location.origin || url.pathname !== '/activity') {
      return '/activity';
    }
    const keys = Array.from(url.searchParams.keys());
    if (keys.length !== 2 || keys.some(key => key !== 'focus' && key !== 'id')) {
      return '/activity';
    }
    if (url.searchParams.get('focus') !== 'approval') return '/activity';
    const id = url.searchParams.get('id');
    if (!id || !/^[A-Za-z0-9_-]{1,256}$/.test(id)) return '/activity';
    return url.pathname + url.search;
  } catch (_error) {
    return '/activity';
  }
}

function fallbackNotification() {
  return {
    title: GENERIC_TITLE,
    body: GENERIC_BODY,
    route: '/activity',
    tag: FALLBACK_TAG,
    eventId: null,
  };
}

function pushNotification(event) {
  const fallback = fallbackNotification();
  if (!event.data) return fallback;
  try {
    const text = event.data.text();
    if (typeof text !== 'string' || new TextEncoder().encode(text).length > MAX_PAYLOAD_BYTES) {
      return fallback;
    }
    const value = JSON.parse(text);
    if (
      !value || value.v !== 1 ||
      !/^[A-Za-z0-9_-]{43}$/.test(value.event_id) ||
      !REGISTERED_CLASSES.has(value.class)
    ) {
      return fallback;
    }
    if (
      typeof value.issued_at !== 'number' || !Number.isSafeInteger(value.issued_at) ||
      typeof value.expires_at !== 'number' || !Number.isSafeInteger(value.expires_at) ||
      value.issued_at < 0 || value.expires_at < value.issued_at
    ) {
      return fallback;
    }
    const expectedTag = 'attention:' + value.event_id;
    if (value.tag !== expectedTag) return fallback;
    return {
      // Phase one is deliberately generic even if a future or compromised
      // sender adds descriptive strings to an otherwise valid envelope.
      title: GENERIC_TITLE,
      body: GENERIC_BODY,
      route: safeRoute(value.route),
      tag: expectedTag,
      eventId: value.event_id,
    };
  } catch (_error) {
    return fallback;
  }
}

function notificationOptions(message) {
  const options = {
    body: message.body,
    tag: message.tag,
    renotify: false,
    requireInteraction: false,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    data: { route: message.route, event_id: message.eventId },
  };
  if (self.Notification && Number(self.Notification.maxActions) > 0) {
    options.actions = [{ action: 'review', title: 'Review' }];
  }
  return options;
}

self.addEventListener('push', (event) => {
  const message = pushNotification(event);
  event.waitUntil(
    self.registration.showNotification(
      message.title, notificationOptions(message),
    ),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const requested = event.notification && event.notification.data
    ? event.notification.data.route : null;
  const route = safeRoute(requested);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    const sameOrigin = windows.filter((client) => {
      try { return new URL(client.url).origin === self.location.origin; }
      catch (_error) { return false; }
    });
    const client = sameOrigin.find(item => item.visibilityState === 'visible') ||
      sameOrigin.find(item => item.focused) || sameOrigin[0];
    if (client) {
      try {
        client.postMessage({
          type: 'web-push:navigate',
          event_id: event.notification.data && event.notification.data.event_id,
          route,
        });
        await client.navigate(route);
        return await client.focus();
      } catch (_error) {
        // A client can close between matchAll and navigate. Opening a fresh
        // same-origin window is safer than losing the click.
      }
    }
    return self.clients.openWindow(route);
  })());
});

// notificationclose is intentionally absent. Closing OS chrome is not an
// acknowledgment, seen marker, cancellation, grant, or decline.

function openDeviceDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DEVICE_DB, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(DEVICE_STORE)) {
        request.result.createObjectStore(DEVICE_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('device store unavailable'));
    request.onblocked = () => reject(new Error('device store blocked'));
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
      transaction.onerror = () => reject(transaction.error || new Error('device store failed'));
      transaction.onabort = () => reject(transaction.error || new Error('device store aborted'));
    });
  } finally {
    db.close();
  }
}

function readDeviceRecord() {
  return deviceStore('readonly', store => store.get(DEVICE_KEY));
}

function writeDeviceRecord(value) {
  return deviceStore('readwrite', store => store.put(value, DEVICE_KEY));
}

function deleteDeviceRecord() {
  return deviceStore('readwrite', store => store.delete(DEVICE_KEY));
}

function validDeviceRecord(record) {
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

function serializedSubscription(subscription, vapidKeyId) {
  if (!subscription || typeof subscription.toJSON !== 'function') return null;
  const raw = subscription.toJSON();
  if (!raw || !raw.endpoint || !raw.keys || !raw.keys.p256dh || !raw.keys.auth) {
    throw new Error('browser returned an incomplete PushSubscription');
  }
  return {
    endpoint: raw.endpoint,
    expiration_time: raw.expirationTime == null ? null : raw.expirationTime,
    keys: { p256dh: raw.keys.p256dh, auth: raw.keys.auth },
    vapid_key_id: vapidKeyId,
  };
}

async function endpointHash(endpoint) {
  const digest = await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(endpoint),
  );
  return Array.from(new Uint8Array(digest), byte =>
    byte.toString(16).padStart(2, '0')).join('');
}

async function refreshStoredDevice(record, subscription, preserveOnRefusal) {
  if (!validDeviceRecord(record)) {
    return { ok: false, reason: 'missing_device' };
  }
  const serialized = serializedSubscription(subscription, record.vapid_key_id);
  const pending = Object.assign({}, record, { pending_refresh: true });
  await writeDeviceRecord(pending);
  let response;
  try {
    response = await fetch(
      '/api/web-push/devices/' + encodeURIComponent(record.device_id) + '/refresh',
      {
        method: 'POST',
        credentials: 'omit',
        cache: 'no-store',
        headers: {
          'Authorization': 'WebPushDevice ' + record.update_token,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          old_endpoint_hash: record.endpoint_hash,
          token_version: record.token_version,
          subscription: serialized,
        }),
      },
    );
  } catch (_error) {
    return { ok: false, reason: 'network' };
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 404 && !preserveOnRefusal) await deleteDeviceRecord();
    return { ok: false, reason: result.error || 'refresh_refused' };
  }
  if (!subscription) {
    if (result.retired !== true) {
      return { ok: false, reason: 'invalid_response' };
    }
    await deleteDeviceRecord();
    return { ok: true, retired: true };
  }
  const next = {
    v: 1,
    device_id: record.device_id,
    update_token: result.device_update_token,
    token_version: result.token_version,
    endpoint_hash: await endpointHash(serialized.endpoint),
    vapid_key_id: record.vapid_key_id,
    application_server_key: record.application_server_key,
    pending_refresh: false,
  };
  if (
    result.device_id !== record.device_id || result.status !== 'active' ||
    !validDeviceRecord(next)
  ) {
    return { ok: false, reason: 'invalid_response' };
  }
  await writeDeviceRecord(next);
  return { ok: true, retired: false };
}

async function reconcileStoredSubscription() {
  const record = await readDeviceRecord();
  if (!record) return { ok: true, skipped: true };
  const subscription = await self.registration.pushManager.getSubscription();
  if (!subscription) return refreshStoredDevice(record, null);
  const hash = await endpointHash(subscription.endpoint);
  if (!record.pending_refresh && hash === record.endpoint_hash) {
    return { ok: true, skipped: true };
  }
  return refreshStoredDevice(record, subscription);
}

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(serializeRefresh(async () => {
    const record = await readDeviceRecord();
    if (!record) return;
    // A browser-supplied null replacement is an explicit loss signal. It
    // retires this installation using only the narrow updater credential.
    const replacement = event.newSubscription === null
      ? null
      : (event.newSubscription || await self.registration.pushManager.getSubscription());
    await refreshStoredDevice(record, replacement);
  }));
});

self.addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'web-push-store-device') return;
  const reply = event.ports && event.ports[0];
  event.waitUntil(serializeRefresh(async () => {
    if (!validDeviceRecord(event.data.record)) {
      if (reply) reply.postMessage({ ok: false, reason: 'invalid_device' });
      return;
    }
    await writeDeviceRecord(event.data.record);
    if (reply) reply.postMessage({ ok: true });
  }).catch(() => {
    if (reply) reply.postMessage({ ok: false, reason: 'unavailable' });
  }));
});

self.addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'web-push-forget-local') return;
  const reply = event.ports && event.ports[0];
  event.waitUntil(serializeRefresh(async () => {
    const record = await readDeviceRecord();
    if (event.data.require_token_retirement && record) {
      // Shared-browser cleanup must not erase the only narrow credential while
      // the prior operator's active server row may still exist. Preserve it on
      // every refusal so an authenticated retry can finish retirement.
      const retired = await refreshStoredDevice(record, null, true);
      if (!retired.ok) {
        if (reply) reply.postMessage(retired);
        return;
      }
    }
    const subscription = await self.registration.pushManager.getSubscription();
    if (subscription) {
      try { await subscription.unsubscribe(); } catch (_error) {}
    }
    const notifications = await self.registration.getNotifications();
    notifications.forEach(item => item.close());
    await deleteDeviceRecord();
    if (reply) reply.postMessage({ ok: true });
  }).catch(() => {
    if (reply) reply.postMessage({ ok: false, reason: 'unavailable' });
  }));
});

self.addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'web-push-reconcile') return;
  const reply = event.ports && event.ports[0];
  event.waitUntil(serializeRefresh(reconcileStoredSubscription).then(
    result => { if (reply) reply.postMessage(result); },
    () => { if (reply) reply.postMessage({ ok: false, reason: 'unavailable' }); },
  ));
});
