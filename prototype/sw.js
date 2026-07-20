/* ResilienceAlert service worker — keeps the citizen reporting app usable when
   the network is congested, the power is out, or the cell site is down.

   Strategy:
   - App shell is precached, so /report opens with no network at all.
   - Navigations are network-first with a cache fallback (fresh when possible,
     available always).
   - Report submissions are never cached; when offline the page queues them in
     IndexedDB and the Background Sync event drains the queue on reconnect. */
const CACHE = "resilience-shell-v1";
const SHELL = ["/report", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;                       // never cache submissions
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;           // live data only

  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put("/report", copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match("/report").then(r => r || caches.match(req)))
    );
    return;
  }
  e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
});

/* Background Sync — fires when connectivity returns, even if the tab was closed.
   The page owns the queue, so we just wake every client and let it flush. */
self.addEventListener("sync", e => {
  if (e.tag === "flush-reports") {
    e.waitUntil(
      self.clients.matchAll({ includeUncontrolled: true, type: "window" })
        .then(cs => cs.forEach(c => c.postMessage({ type: "flush-reports" })))
    );
  }
});
