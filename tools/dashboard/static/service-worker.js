'use strict';

// Push-only proof/foundation. There is intentionally no fetch handler and no
// Cache Storage: the dashboard remains server-first while Web Push is proven.
self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

function proofNotification(event) {
  const fallback = {
    title: 'Autonomy needs your attention',
    body: 'Open the dashboard to review.',
    route: '/web-push-proof',
    tag: 'autonomy-web-push-proof'
  };
  if (!event.data) return fallback;
  try {
    const value = event.data.json();
    if (!value || value.v !== 1) return fallback;
    const route = typeof value.route === 'string' &&
      (value.route === '/web-push-proof' || value.route === '/activity')
      ? value.route : fallback.route;
    return {
      title: typeof value.title === 'string' ? value.title.slice(0, 80) : fallback.title,
      body: typeof value.body === 'string' ? value.body.slice(0, 160) : fallback.body,
      route,
      tag: typeof value.tag === 'string' ? value.tag.slice(0, 80) : fallback.tag
    };
  } catch (_error) {
    return fallback;
  }
}

self.addEventListener('push', (event) => {
  const message = proofNotification(event);
  event.waitUntil(self.registration.showNotification(message.title, {
    body: message.body,
    tag: message.tag,
    renotify: false,
    requireInteraction: false,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    data: { route: message.route }
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const requested = event.notification && event.notification.data
    ? event.notification.data.route : null;
  const route = requested === '/web-push-proof' || requested === '/activity'
    ? requested : '/';
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true
    });
    for (const client of windows) {
      if (new URL(client.url).origin === self.location.origin) {
        await client.navigate(route);
        return client.focus();
      }
    }
    return self.clients.openWindow(route);
  })());
});

// notificationclose is intentionally absent: dismissing OS chrome has no
// semantic meaning and must never mark an item seen, declined, or canceled.
