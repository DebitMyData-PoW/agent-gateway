"use strict";
// The installable Get a DID app's service worker. It keeps the last copy of the page, its font
// and its icons, so that the app still opens without a network (and says it is offline). The
// page itself always comes from the network first, so a new version shows at once. It never
// touches anything else: the API calls (/v1/...) go straight to the gateway, never cached.
const CACHE = "dmd-did-app-5"; // a new name drops the old copies (new icons)
const PAGE = "/";
const FILES = [
  "/fonts/mada-latin-400-normal.woff2",
  "/fonts/mada-latin-600-normal.woff2",
  "/fonts/mada-latin-700-normal.woff2",
  "/fonts/mada-latin-800-normal.woff2",
  "/favicon.png",
  "/logo.png",
  "/app/icon-192.png",
];

const OFFLINE = `<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Offline · DebitMyData</title></head>
<body>
<h1>You're offline</h1>
<p>Getting a DID needs an internet connection. Connect, then open the app again.</p></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll([PAGE, ...FILES]))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    if (url.pathname !== PAGE) return; // the policies and other pages: the network as usual
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(PAGE, copy));
          }
          return response;
        })
        .catch(() =>
          caches.match(PAGE).then(
            (cached) =>
              cached ||
              new Response(OFFLINE, {
                headers: { "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": "default-src 'none'" },
              })
          )
        )
    );
    return;
  }
  if (FILES.includes(url.pathname)) {
    // "?v=..." only tells browsers a file changed; a new CACHE name drops the old copies.
    event.respondWith(caches.match(request, { ignoreSearch: true }).then((cached) => cached || fetch(request)));
  }
});
