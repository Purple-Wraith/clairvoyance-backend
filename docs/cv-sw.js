// CLAIRVOYANCE service worker (2026-10-22, owner request: the home-screen app stalled on cold GitHub Pages fetches).
//
// What it does, and deliberately nothing more:
//   * app shell (app.html / index.html): served INSTANTLY from cache, refreshed in the background; when the background copy differs the page is told ("cv-sw" / "update") and shows a tap-to-update bar.
//     It NEVER reloads a page by itself (the May-2026 blank-page "loading loops" came from forced reloads).
//   * data files (same-origin *.json): network first, but if the network is slower than DATA_TIMEOUT the last good copy is used and the network answer still refreshes the cache for next time.
//     A file with no cached copy simply waits for the network. A 4xx is passed through untouched (a missing file stays missing).
//   * images / fonts / static js+css: cache first, refreshed in the background.
//   * NEVER touched: other origins (ESPN, Supabase, fonts, Worker), non-GET, range requests, and the files that must always be live (version / automation status / relay ping / manual locks).
// Kill switches: open the app with ?nosw=1 (unregisters + clears this worker's caches), or ship a cv-sw.js that unregisters itself (like the legacy sw.js, which stays in place to clean up old installs).
// Automation (the lock / settle pipeline and every test) never registers it unless asked with ?sw=1.
const SCOPE = new URL(self.registration.scope).pathname;                 // the desktop and mobile sites share an origin: namespace by site path so they never touch each other's caches
const V = 'cv-v1:' + SCOPE;
const SHELL = V + ':shell', DATA = V + ':data', ASSET = V + ':asset';
const NET_ONLY = /(^|\/)(version|automation_status|relay_ping|manual_locks)\.json$/;
const DROP_PARAMS = ['_cb', '_v', 'cb', 'v', 't', '_t', 'nocache', 'ts'];  // cache-busting params differ on every fetch; they must not create a new cache entry each time (the app uses ?<bucket number>= too: dropped below)
const DATA_TIMEOUT = 3500, MAX_DATA = 90, MAX_ASSET = 120;

const norm = (url) => {
  const u = new URL(url);
  DROP_PARAMS.forEach((p) => u.searchParams.delete(p));
  for (const k of [...u.searchParams.keys()]) if (/^\d{5,}$/.test(k)) u.searchParams.delete(k);       // the app's own buster: data.json?9959414=
  u.hash = '';
  return u.toString();
};
const shellKey = () => new URL('app.html', self.registration.scope).toString();   // app.html, index.html and the bare folder URL are the same page
const sig = (res) => res && (res.headers.get('etag') || res.headers.get('last-modified') || res.headers.get('content-length'));
const tell = (msg) => self.clients.matchAll({ type: 'window' }).then((cs) => cs.forEach((c) => c.postMessage({ type: 'cv-sw', msg })));
const trim = async (cache, max) => { const ks = await cache.keys(); for (let i = 0; i < ks.length - max; i++) await cache.delete(ks[i]); };

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    try {
      const cache = await caches.open(SHELL);
      const res = await fetch(new Request(shellKey(), { cache: 'reload' }));
      if (res && res.ok && !res.redirected) await cache.put(shellKey(), res);
    } catch (x) { /* first visit offline etc.: the shell is cached on its first successful navigation instead */ }
  })());
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keep = new Set([SHELL, DATA, ASSET]);
    for (const k of await caches.keys()) if (k.startsWith('cv-v') && k.includes(':' + SCOPE + ':') && !keep.has(k)) await caches.delete(k);   // only THIS site's older versions
    await self.clients.claim();
  })());
});

async function shell(e) {
  const cache = await caches.open(SHELL), key = shellKey();
  const hit = await cache.match(key);
  if (!hit) {                                                    // first visit (or cache cleared): the network, and remember it
    const res = await fetch(e.request);
    if (res && res.ok && !res.redirected && e.request.mode === 'navigate') e.waitUntil(cache.put(key, res.clone()));
    return res;
  }
  e.waitUntil((async () => {                                     // serve the cached shell now; look for a newer one in the background
    try {
      const res = await fetch(new Request(key, { cache: 'no-cache' }));
      if (res && res.ok && !res.redirected) {
        const changed = sig(hit) && sig(res) && sig(hit) !== sig(res);
        await cache.put(key, res.clone());
        if (changed) await tell('update');
      }
    } catch (x) { /* offline / slow: keep what we have */ }
  })());
  return hit;
}

async function data(e) {
  const cache = await caches.open(DATA), key = norm(e.request.url);
  const hit = await cache.match(key);
  const net = fetch(e.request).then(async (res) => {
    if (res && res.ok) { await cache.put(key, res.clone()); await trim(cache, MAX_DATA); }
    return res;
  });
  e.waitUntil(net.catch(() => {}));                              // a slow network answer still refreshes the cache after we have already replied
  if (!hit) return net;                                          // nothing to fall back on: wait for the network
  const slow = new Promise((r) => setTimeout(() => r(hit), DATA_TIMEOUT));
  const live = net.then((res) => (res.status >= 500 ? hit : res), () => hit);
  return Promise.race([live, slow]);
}

async function asset(e) {
  const cache = await caches.open(ASSET), key = norm(e.request.url);
  const hit = await cache.match(key);
  const net = fetch(e.request).then(async (res) => {
    if (res && res.ok) { await cache.put(key, res.clone()); await trim(cache, MAX_ASSET); }
    return res;
  });
  if (hit) { e.waitUntil(net.catch(() => {})); return hit; }
  return net;
}

self.addEventListener('fetch', (e) => {
  const r = e.request;
  if (r.method !== 'GET' || r.headers.has('range')) return;
  const u = new URL(r.url);
  if (u.origin !== self.location.origin) return;                 // ESPN, Supabase, Cloudflare Worker, Google Fonts...: never intercepted
  if (/\/(cv-)?sw\.js$/.test(u.pathname) || NET_ONLY.test(u.pathname)) return;
  if (r.mode === 'navigate' || /\/(app|index)\.html$/.test(u.pathname)) { e.respondWith(shell(e)); return; }
  if (/\.json$/i.test(u.pathname)) { e.respondWith(data(e)); return; }
  if (/\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?)$/i.test(u.pathname)) { e.respondWith(asset(e)); return; }
});
