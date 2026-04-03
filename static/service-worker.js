// Bump cache version so clients re-precacache the correct icon set.
const CACHE_NAME = "fds-pos-cache-v5";
const OFFLINE_URL = "/offline";

const PRECACHE_URLS = [
  "/",
  "/dashboard",
  "/protocol",
  "/static/style.css",
  "/static/icons/icon-192x192.png",
  "/static/icons/icon-512x512.png",
  "/static/manifest.json",
  OFFLINE_URL,
];

self.addEventListener("install", (event) => {
  console.log("[ServiceWorker] Install");

  // cache.addAll fails entirely if any URL fails; precache individually so one 404 doesn't break install
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.all(
        PRECACHE_URLS.map((url) =>
          cache.add(url).catch((err) => {
            console.warn("[ServiceWorker] Precache skip:", url, err);
          })
        )
      )
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  console.log("[ServiceWorker] Activated");
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request).catch(() =>
      caches.match(event.request).then((response) =>
        response || caches.match(OFFLINE_URL)
      )
    )
  );
});

