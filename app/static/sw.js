const CACHE_NAME = "agy-hub-v170";
const SHELL_ASSETS = [
  "/",
  "/manifest.json",
  "/static/styles.css?v=170",
  "/static/ui-system.css?v=170",
  "/static/ui-shell.js?v=170",
  "/static/app.js?v=170",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/vendor/marked.min.js",
  "/static/vendor/highlight.min.js",
  "/static/vendor/github-dark.min.css",
  "/static/vendor/github.min.css"
];

function createOfflineResponse() {
  return new Response("", { status: 503, statusText: "Offline" });
}

function cachePutSafe(event, key, response) {
  const putPromise = caches
    .open(CACHE_NAME)
    .then((cache) => cache.put(key, response))
    .catch(() => {});
  if (event && typeof event.waitUntil === "function") {
    event.waitUntil(putPromise);
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        Promise.all(
          SHELL_ASSETS.map((url) =>
            fetch(url, { cache: "no-cache" }).then((resp) => {
              if (!resp || !resp.ok) {
                throw new Error(`Precache failed (${resp ? resp.status : "network"}): ${url}`);
              }
              return cache.put(url, resp);
            })
          )
        )
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k.startsWith("agy-hub-") && k !== CACHE_NAME)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
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
        return fetch(event.request)
          .then((resp) => {
            if (resp && resp.status === 200) {
              cachePutSafe(event, event.request, resp.clone());
            }
            return resp;
          })
          .catch(() => createOfflineResponse());
      })
    );
    return;
  }
  const isNavigate = event.request.mode === "navigate";
  const cacheKey = isNavigate ? "/" : event.request;
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp && resp.status === 200) {
          cachePutSafe(event, cacheKey, resp.clone());
        }
        return resp;
      })
      .catch(() =>
        caches.match(cacheKey).then((cached) => cached || createOfflineResponse())
      )
  );
});

self.addEventListener("push", event => {
  let payload = {};
  try {
    payload = event.data?.json() || {};
  } catch {
    payload = { body: event.data?.text() || "" };
  }
  event.waitUntil(self.registration.showNotification(payload.title || "ZeroHub", {
    body: payload.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    tag: payload.tag || "zerohub-alert",
    renotify: true,
    data: payload.data || {},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil((async () => {
    let target = new URL("/", self.location.origin);
    try {
      const requested = new URL(data.url || "/", self.location.origin);
      if (requested.origin === self.location.origin) target = requested;
    } catch {}
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      try {
        await client.focus();
        client.postMessage({ ...data, type: "zerohub_notification_click" });
        return;
      } catch {}
    }
    await self.clients.openWindow(target.href);
  })());
});
