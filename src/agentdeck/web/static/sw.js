// agentdeck service worker.
//
// This is a *live* monitoring dashboard, so the goal here is installability
// (home-screen / standalone display), NOT offline data. We therefore cache
// only the static shell assets and let everything dynamic hit the network
// untouched. In particular the `/events*` Server-Sent-Events streams must
// never be intercepted — wrapping them in respondWith() buffers the stream
// and kills the live tail. Same for navigations and `/partials/*`, which must
// always reflect current server state.

// The cache name carries a content hash of the shell assets, substituted when
// /sw.js is served. Any change to app.css/sse.js/etc. changes this string, so
// the browser installs a fresh worker and re-fetches the assets — no manual
// version bump, no stale CSS after a deploy.
const CACHE = 'agentdeck-static-__CACHE_STAMP__';
const ASSETS = [
  '/static/app.css',
  '/static/htmx.min.js',
  '/static/sse.js',
  '/static/push.js',
  '/static/mobile_session_stack.js',
  '/static/favicon.svg',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  // Cache each shell asset independently. cache.addAll() rejects atomically if
  // any single URL 404s (e.g. a half-finished deploy), which would fail the
  // whole install and pin the browser to the previous worker + stale shell.
  // allSettled lets the new worker install with whatever assets are available;
  // the network-first fetch handler re-fetches any that were missed.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.allSettled(ASSETS.map((url) => cache.add(url))))
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

// Web Push (issue #7). A push arrives as a JSON {title, body, url}; show it as a
// notification. Clicking focuses an existing app tab (navigating it to the
// target) or opens a new one.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const title = data.title || 'agentdeck';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || '',
      // Collapse repeats about the same thing onto one notification.
      tag: data.url || 'agentdeck',
      data: { url: data.url || '/' },
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(openApp());
});

// Tapping a notification must reliably surface the AgentDeck *front page*,
// freshly loaded — never a no-op and never a stale, deep-linked chat view
// (issue #35). We ignore the push payload's url and always route home: the
// dashboard reflects current state on load, and the SW never caches navigations
// so a real navigate('/') always pulls fresh server state.
//
// Reliability matters more than elegance here, because the client we find may be
// a frozen, *uncontrolled* window (mobile PWA resumed from background) whose
// navigate() rejects — the old `navigate().catch().then(focus())` chain then
// left the app focused on its stale view, or did nothing at all. So: focus the
// existing window first (this brings the installed PWA forward even when
// navigate() is unavailable), then move it home via navigate(); if navigate()
// is missing or rejects, ask the page itself to go home via postMessage; and if
// there's no app window to reuse, open a fresh one.
async function openApp() {
  const home = new URL('/', self.location.origin).href;
  const clientList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clientList) {
    if (!client.url || new URL(client.url).origin !== self.location.origin) continue;
    try { await client.focus(); } catch (e) { /* still try to route it home below */ }
    if (client.navigate) {
      try { await client.navigate(home); return; } catch (e) { /* fall through */ }
    }
    // navigate() unavailable or rejected: let the page navigate itself home.
    client.postMessage({ type: 'agentdeck:open-home' });
    return;
  }
  await self.clients.openWindow(home);
}

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

  // Network-first: this is an always-online dashboard, so fetch the live asset
  // (a changed app.css/sse.js shows up immediately, no stale copy), update the
  // cache, and fall back to the cache only when the network is unreachable.
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      fetch(req)
        .then((res) => {
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        })
        .catch(() => cache.match(req))
    )
  );
});
