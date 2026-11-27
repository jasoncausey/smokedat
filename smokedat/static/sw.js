// Service worker: shows web-push alerts and opens the app when one is tapped.
// No offline caching: live temperatures are only useful from the bridge itself.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { d = {body: event.data && event.data.text()}; }
  const alarm = d.level === 'alarm';
  event.waitUntil(self.registration.showNotification(d.title || 'SmokeDat', {
    body: d.body || '',
    tag: d.tag || 'bbq',
    renotify: true,
    requireInteraction: alarm,
    icon: '/icon-192.png?v=2',
    badge: '/icon-192.png?v=2',
    data: {url: '/', alarm},
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    // Tapping an alarm counts as acknowledging it: stops Pushover repeats for every outstanding alarm.
    if (event.notification.data && event.notification.data.alarm) {
      try {
        await fetch(new URL('api/ack', self.registration.scope), {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({from: 'a notification'})});
      } catch {}
    }
    const wins = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const w of wins) if ('focus' in w) return w.focus();
    return self.clients.openWindow('/');
  })());
});
