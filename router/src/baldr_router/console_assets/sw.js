// Installability needs a service worker with a fetch handler, but caching a
// local control plane would be actively wrong: the console must never show a
// stale queue, and the page is re-read from disk on every request so that
// editing it takes effect on refresh. This worker therefore registers and then
// stays out of the way.
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
