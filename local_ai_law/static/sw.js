// RAG Studio service worker  -  minimal, offline shell only.
// Never caches /api/* (private + dynamic). Only caches the app shell.
const CACHE = 'rag-studio-v4';
const SHELL = [
  '/',
  '/static/manifest.webmanifest',
  '/static/logo-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Never touch API or WebSocket traffic.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return;
  if (req.method !== 'GET') return;

  // Same-origin static assets: network-first so updated chat bundles are not stale.
  if (url.origin === self.location.origin) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp && resp.status === 200 && resp.type !== 'opaque') {
          const clone = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, clone)).catch(() => {});
        }
        return resp;
      }).catch(() => caches.match(req).then((cached) => cached || Response.error()))
    );
  }
});
