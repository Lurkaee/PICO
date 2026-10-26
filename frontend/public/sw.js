/**
 * PICO Offline Application Shell Service Worker (Milestone P10.2 / P10.2.1 Hardening).
 *
 * Security & Data Protection Invariants:
 * 1. ONLY caches built application shell HTML and explicitly allowlisted versioned static assets.
 * 2. HARD PROHIBITION: NEVER caches API responses (/api/*, /health), OpenAPI/docs specs (/openapi.json, /docs),
 *    private data, conversations, memory, tool executions, pairing codes, CSRF tokens, or approvals.
 * 3. Cross-origin requests strictly bypass the service worker cache.
 * 4. Network-only for all /api/* routes, operational endpoints, and non-GET requests. Failures are NEVER
 *    converted to HTML fallbacks.
 * 5. Zero automatic mutation replay: POST/PUT/DELETE requests are NEVER queued or replayed.
 */

const PICO_CACHE_PREFIX = 'pico-shell-';
const SHELL_CACHE_NAME = 'pico-shell-v2';

// Minimal static shell assets to pre-cache on install
const SHELL_PRECACHE_ASSETS = [
  '/',
  '/index.html',
  '/favicon.ico',
];

const STATIC_EXPLICIT_ALLOWLIST = new Set([
  '/',
  '/index.html',
  '/favicon.ico',
  '/manifest.webmanifest',
  '/manifest.json',
  '/vite.svg',
]);

const STATIC_ASSET_EXTENSIONS = new Set([
  '.js',
  '.css',
  '.svg',
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.woff',
  '.woff2',
  '.ttf',
  '.eot',
  '.ico',
]);

function normalizePath(pathname) {
  // Normalize consecutive slashes and convert to lowercase
  return pathname.replace(/\/+/g, '/').toLowerCase();
}

function isApiOrOperationalPath(pathname) {
  const norm = normalizePath(pathname);
  if (norm.startsWith('/api/') || norm === '/api') return true;
  if (norm.startsWith('/health') || norm === '/health') return true;
  if (norm === '/openapi.json' || norm.startsWith('/docs') || norm === '/redoc') return true;
  return false;
}

function isAllowedStaticAsset(pathname) {
  const norm = normalizePath(pathname);
  if (STATIC_EXPLICIT_ALLOWLIST.has(norm)) return true;
  if (norm.startsWith('/assets/')) {
    const dotIndex = norm.lastIndexOf('.');
    if (dotIndex !== -1) {
      const ext = norm.slice(dotIndex);
      return STATIC_ASSET_EXTENSIONS.has(ext);
    }
    return false;
  }
  return false;
}

function shouldDeleteCache(cacheName, currentCacheName = SHELL_CACHE_NAME) {
  return typeof cacheName === 'string' &&
    cacheName.startsWith(PICO_CACHE_PREFIX) &&
    cacheName !== currentCacheName;
}

function evaluateFetchPolicy(method, urlString, mode = 'cors', swOrigin = (typeof self !== 'undefined' && self.location ? self.location.origin : 'http://localhost:5173')) {
  let url;
  try {
    url = new URL(urlString, swOrigin);
  } catch {
    return { handled: false, reason: 'invalid-url' };
  }

  // 1. Cross-Origin Requests: Bypass SW cache completely
  if (url.origin !== swOrigin) {
    return { handled: false, reason: 'cross-origin-bypass' };
  }

  // 2. Non-GET Mutating Methods: Never intercept, cache, or replay
  if (method !== 'GET') {
    return { handled: false, reason: 'non-get-mutation-bypass' };
  }

  const normPath = normalizePath(url.pathname);

  // 3. API, Health, and Operational Endpoints: Strictly Network-Only
  if (isApiOrOperationalPath(normPath)) {
    return { handled: false, reason: 'api-operational-network-only' };
  }

  // 4. Navigation Requests: Network-first with cached shell fallback
  if (mode === 'navigate') {
    return { handled: true, strategy: 'network-first-shell-fallback' };
  }

  // 5. Allowed Static Assets (/assets/* and explicit allowlist): Cache-first
  if (isAllowedStaticAsset(normPath)) {
    return { handled: true, strategy: 'cache-first-static-asset' };
  }

  // 6. Unknown / Dynamic same-origin paths: Bypass cache completely (Network-only)
  return { handled: false, reason: 'unknown-path-cache-bypass' };
}

if (typeof self !== 'undefined' && 'addEventListener' in self) {
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
            if (shouldDeleteCache(cacheName, SHELL_CACHE_NAME)) {
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
    const decision = evaluateFetchPolicy(
      request.method,
      request.url,
      request.mode,
      self.location.origin
    );

    if (!decision.handled) {
      return; // Direct browser network handling without caching
    }

    if (decision.strategy === 'network-first-shell-fallback') {
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

    if (decision.strategy === 'cache-first-static-asset') {
      event.respondWith(
        caches.match(request).then((cached) => {
          if (cached) {
            return cached;
          }
          return fetch(request).then((response) => {
            if (response.status === 200 && response.type === 'basic') {
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
  });
}

// Export for Node/Vitest testing environments
if (typeof globalThis !== 'undefined') {
  globalThis.__PICO_SW__ = {
    PICO_CACHE_PREFIX,
    SHELL_CACHE_NAME,
    SHELL_PRECACHE_ASSETS,
    STATIC_EXPLICIT_ALLOWLIST,
    STATIC_ASSET_EXTENSIONS,
    normalizePath,
    isApiOrOperationalPath,
    isAllowedStaticAsset,
    shouldDeleteCache,
    evaluateFetchPolicy,
  };
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = globalThis.__PICO_SW__;
}
