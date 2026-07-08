// agentdeck service worker.
//
// This is a *live* monitoring dashboard, so the goal here is installability
// (home-screen / standalone display), NOT offline data. We therefore cache
// only the static shell assets and let everything dynamic hit the network
// untouched. In particular the `/events*` Server-Sent-Events streams must
// never be intercepted — wrapping them in respondWith() buffers the stream
// and kills the live tail. Same for navigations and `/partials/*`, which must
// always reflect current server state.

const CACHE = 'agentdeck-static-v2';
const ASSETS = [
  '/static/app.css',
  '/static/htmx.min.js',
  '/static/sse.js',
  '/static/favicon.svg',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Only manage the static shell + the manifest. Everything else — page
  // navigations, /partials/* fragments, and the /events* SSE streams — is
  // left to the network so the dashboard always shows live state.
  const managed = url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest';
  if (!managed) return;

  // Stale-while-revalidate: serve the cached copy instantly, refresh in the
  // background so a new deploy's assets are picked up on the next load.
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(req).then((cached) => {
        const network = fetch(req)
          .then((res) => {
            if (res && res.ok) cache.put(req, res.clone());
            return res;
          })
          .catch(() => cached);
        return cached || network;
      })
    )
  );
});
