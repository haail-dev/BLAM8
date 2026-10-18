/* BLAM8 service worker — push notifications + a minimal offline shell.
 *
 * Served from the site ROOT as /sw.js (src/app/main.py) so its scope covers /app.
 * Bump VERSION on any change to the precache list or caching strategy — the
 * activate step drops caches from older versions.
 *
 * Caching is deliberately conservative: /app pages are authenticated and served
 * Cache-Control: no-store (src/app/security.py), so page HTML is NEVER written to
 * the SW cache — a navigation that fails offline gets the /offline page instead.
 * Only same-origin /static/ assets (already cache-busted per deploy via
 * ?v=<asset_rev>) and the /offline page itself are cached.
 */
const VERSION = 'v4';   // 2026-10-13: named exact-URL cache, retained writes and lookup timings
const CACHE = 'blam8-' + VERSION;
// Where a tapped notification wants to go (notificationclick below). Its own
// cache, so a VERSION bump's clean-up never wipes a tap in flight.
const OPEN_CACHE = 'blam8-open';
const OPEN_KEY = '/__blam8_open';
const OFFLINE_URL = '/offline';
const PRECACHE = [
  OFFLINE_URL,
  '/static/icons/icon-192.png',
  '/static/icons/badge-72.png',
  '/static/pull_refresh.js',       // the offline page's pull to refresh
];

// Worker-clock timings stay local and bounded. Inventory is collected only on
// an explicit diagnostic request, never on the asset response path.
const timings = [];
function record(name, start, detail = {}) {
  timings.push({ name, at: start, duration: performance.now() - start, ...detail });
  if (timings.length > 200) timings.splice(0, timings.length - 200);
}
async function matchAsset(req) {
  const start = performance.now();
  try {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req, { ignoreSearch: false });
    record('cache.lookup', start, { url: req.url, cache: CACHE, hit: Boolean(hit) });
    return hit;
  } catch (_) {
    record('cache.lookup-error', start, { url: req.url, cache: CACHE });
    return undefined;
  }
}
self.addEventListener('message', event => {
  if (event.data?.type !== 'blam8:timings' || !event.ports[0]) return;
  event.waitUntil((async () => {
    const start = performance.now();
    const names = await caches.keys();
    const inventory = await Promise.all(names.map(async name => ({
      name, entries: (await (await caches.open(name)).keys()).length,
    })));
    event.ports[0].postMessage({ version: VERSION, cache: CACHE,
      timeOrigin: performance.timeOrigin, timings: timings.slice(), inventory,
      inventoryMs: performance.now() - start });
  })().catch(() => event.ports[0].postMessage({ version: VERSION, error: 'Cache inventory unavailable' })));
});

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== OPEN_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      // Navigation preload: the page request starts the moment a navigation begins, in parallel with
      // this worker booting (a stopped worker can take 50-500 ms to start on a cold launch of the
      // installed app), instead of waiting for the worker to be ready and then starting fetch().
      .then(() => self.registration.navigationPreload && self.registration.navigationPreload.enable())
      .catch(() => {})
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Navigations: network-first, offline page as the fallback. Never cache the
  // (authenticated) HTML itself.
  if (req.mode === 'navigate') {
    event.respondWith(
      // Always consume the preload response when there is one (an unused preload is a wasted
      // request); otherwise fetch as before. HTML is still network-only and never cached.
      Promise.resolve(event.preloadResponse).then(pre => pre || fetch(req))
        .catch(() => caches.open(CACHE).then(c => c.match(OFFLINE_URL)))
    );
    return;
  }

  // Versioned assets are immutable by URL. Unversioned module imports and
  // offline-shell assets must revalidate, with their cache only for offline.
  if (url.pathname.startsWith('/static/')) {
    let write = Promise.resolve();
    const network = async () => {
      const start = performance.now();
      const res = await fetch(req, url.searchParams.get('v') ? undefined : { cache: 'no-cache' });
      record('asset.network-headers', start, { url: req.url, status: res.status });
      if (res.ok) {
        const copy = res.clone(), writeStart = performance.now();
        write = caches.open(CACHE).then(c => c.put(req, copy))
          .then(() => record('cache.write', writeStart, { url: req.url }))
          .catch(() => record('cache.write-error', writeStart, { url: req.url }));
      }
      return res;
    };
    const response = url.searchParams.get('v')
      ? matchAsset(req).then(hit => hit || network())
      : network().catch(() => matchAsset(req));
    event.respondWith(response);
    // Registered during dispatch; writes never delay the selected response.
    event.waitUntil(response.then(() => write).catch(() => {}));
  }
  // Everything else (API calls, SSE stream) goes straight to the network.
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* non-JSON push */ }
  const options = {
    body: data.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/badge-72.png',
    data: { url: data.url || '/app/' },
    actions: [
      { action: 'open', title: 'Open' },
      { action: 'dismiss', title: 'Dismiss' },
    ],
    tag: data.tag || 'blam8-notification',
    renotify: true,
  };
  event.waitUntil(self.registration.showNotification(data.title || 'BLAM8', options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'dismiss') return;
  const url = event.notification.data && event.notification.data.url || '/app/';
  // The window comes FIRST and at once. iOS only honours openWindow() while
  // it still counts as the user's tap: tap-5..8 saved the tap and posted a
  // report BEFORE opening, and a killed app tapped awake came up as a blank
  // in-app browser sheet ("Search or enter website name", Kris 2026-09-24).
  // Remembering the tap and the report now run alongside, never ahead.
  const open = clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    wins.forEach((w) => { try { w.postMessage({ type: 'blam8:open', url }); } catch (e) {} });
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin && 'focus' in w) {
        if ('navigate' in w) w.navigate(url).catch(() => null);
        return w.focus();
      }
    }
    return clients.openWindow(url);
  });
  // Written for the page too (push.js reads it when it comes forward).
  const remember = caches.open(OPEN_CACHE).then((c) => c.put(OPEN_KEY,
    new Response(JSON.stringify({ url, at: Date.now() }), { headers: { 'Content-Type': 'application/json' } })))
    .catch(() => null);
  event.waitUntil(Promise.all([open, remember]).catch(() => null));
});
