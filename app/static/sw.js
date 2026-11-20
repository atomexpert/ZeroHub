const CACHE_NAME = "agy-hub-v161";
const SHELL_ASSETS = [
  "/",
  "/manifest.json",
  "/static/styles.css?v=161",
  "/static/ui-system.css?v=161",
  "/static/ui-shell.js?v=161",
  "/static/app.js?v=161",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/vendor/marked.min.js",
  "/static/vendor/highlight.min.js",
  "/static/vendor/github-dark.min.css",
  "/static/vendor/github.min.css"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.all(
        SHELL_ASSETS.map((url) =>
          fetch(url, { cache: "no-cache" }).then((resp) => {
            if (resp.ok) return cache.put(url, resp);
          })
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (
    url.pathname === "/sw.js" ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/mcp/") ||
    url.pathname.startsWith("/ws")
  ) {
    return;
  }
  // v111: Cache-First для версионированной статики (`?v=N`) и `/static/vendor/*` —
  // мгновенный старт PWA без сетевого round-trip через туннель.
  if (
    url.pathname.startsWith("/static/vendor/") ||
    (url.pathname.startsWith("/static/") && url.searchParams.has("v"))
  ) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((resp) => {
          if (resp && resp.status === 200) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return resp;
        });
      })
    );
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp && resp.status === 200 && event.request.method === "GET") {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return resp;
      })
      .catch(() =>
        caches.match(event.request).then(
          (cached) => cached || (event.request.mode === "navigate" ? caches.match("/") : undefined)
        )
      )
  );
});
