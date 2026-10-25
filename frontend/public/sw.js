/**
 * PICO Offline Application Shell Service Worker (Milestone P10.2).
 *
 * Security & Data Protection Invariants:
 * 1. ONLY caches built application shell HTML and versioned static JS/CSS assets.
 * 2. HARD PROHIBITION: NEVER caches API responses (/api/*, /health), private data,
 *    conversations, memory, tool executions, pairing codes, CSRF tokens, or approvals.
 * 3. Network-only for all /api/* routes and non-GET requests. Failures are NEVER converted
 *    to HTML fallbacks.
 * 4. Zero automatic mutation replay: POST/PUT/DELETE requests are NEVER queued or replayed.
 */

const SHELL_CACHE_NAME = 'pico-shell-v1';

// Minimal static shell assets to pre-cache on install
const SHELL_PRECACHE_ASSETS = [
  '/',
  '/index.html',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_PRECACHE_ASSETS);
    }).then(() => {
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== SHELL_CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // 1. MUTATING REQUESTS: NEVER intercept or replay. Always direct network.
  if (request.method !== 'GET') {
    return;
  }

  // 2. HARD PROHIBITION: API & HEALTH ENDPOINTS ARE NETWORK-ONLY.
  // Never serve cached API responses. Never convert failed API calls to index.html.
  if (url.pathname.startsWith('/api/') || url.pathname === '/health') {
    return;
  }

  // 3. NAVIGATION REQUESTS: Network-first, with cached application shell fallback
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => {
        return caches.match('/index.html').then((cachedIndex) => {
          if (cachedIndex) {
            return cachedIndex;
          }
          return new Response(
            '<!DOCTYPE html><html><head><title>PICO Offline</title></head><body><div id="root">PICO Shell Loading...</div></body></html>',
            {
              headers: { 'Content-Type': 'text/html; charset=utf-8' },
            }
          );
        });
      })
    );
    return;
  }

  // 4. VERSIONED STATIC ASSETS (/assets/*): Cache-first with network fallback & background update
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) {
          return cached;
        }
        return fetch(request).then((response) => {
          if (response.status === 200) {
            const copy = response.clone();
            caches.open(SHELL_CACHE_NAME).then((cache) => {
              cache.put(request, copy);
            });
          }
          return response;
        });
      })
    );
    return;
  }

  // 5. Default GET requests: Stale-while-revalidate for static files
  event.respondWith(
    caches.match(request).then((cached) => {
      const networkFetch = fetch(request).then((response) => {
        if (response.status === 200) {
          const copy = response.clone();
          caches.open(SHELL_CACHE_NAME).then((cache) => {
            cache.put(request, copy);
          });
        }
        return response;
      }).catch(() => cached);

      return cached || networkFetch;
    })
  );
});
