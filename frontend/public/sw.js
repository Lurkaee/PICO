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
const MAX_CACHE_ENTRIES = 50;

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

const VITE_ASSET_REGEX = /^\/assets\/[a-zA-Z0-9_\.-]+\.[a-zA-Z0-9]+$/;

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
    // Must match direct Vite static asset filename beneath /assets/ without nested directories
    if (!VITE_ASSET_REGEX.test(norm)) return false;
    const dotIndex = norm.lastIndexOf('.');
    if (dotIndex !== -1) {
      const ext = norm.slice(dotIndex);
      return STATIC_ASSET_EXTENSIONS.has(ext);
    }
    return false;
  }
  return false;
}

function isCompatibleContentType(pathname, contentTypeHeader) {
  if (!contentTypeHeader || typeof contentTypeHeader !== 'string') {
    return false;
  }
  const ct = contentTypeHeader.toLowerCase();
  const norm = normalizePath(pathname);

  // HTML / App shell
  if (norm === '/' || norm === '/index.html') {
    return ct.includes('text/html');
  }

  // Manifests
  if (norm.endsWith('.webmanifest') || norm.endsWith('.json')) {
    return ct.includes('application/manifest+json') || ct.includes('application/json');
  }

  // File extension matching
  const dotIndex = norm.lastIndexOf('.');
  if (dotIndex === -1) return false;
  const ext = norm.slice(dotIndex);

  switch (ext) {
    case '.js':
      if (ct.includes('text/html') || ct.includes('application/json')) return false;
      return ct.includes('javascript');
    case '.css':
      if (ct.includes('text/html') || ct.includes('application/json')) return false;
      return ct.includes('text/css');
    case '.svg':
      return ct.includes('image/svg') || ct.includes('svg+xml');
    case '.png':
      return ct.includes('image/png');
    case '.jpg':
    case '.jpeg':
      return ct.includes('image/jpeg');
    case '.webp':
      return ct.includes('image/webp');
    case '.gif':
      return ct.includes('image/gif');
    case '.woff':
      return ct.includes('font/woff') || ct.includes('application/font-woff');
    case '.woff2':
      return ct.includes('font/woff2');
    case '.ttf':
      return ct.includes('font/ttf') || ct.includes('application/x-font-ttf') || ct.includes('font/sfnt');
    case '.eot':
      return ct.includes('application/vnd.ms-fontobject');
    case '.ico':
      return ct.includes('image/x-icon') || ct.includes('image/vnd.microsoft.icon') || ct.includes('image/ico');
    default:
      return false;
  }
}

function canCacheResponse(urlPathname, status, responseType, contentTypeHeader) {
  if (status !== 200) return false;
  if (responseType !== 'basic') return false;
  return isCompatibleContentType(urlPathname, contentTypeHeader);
}

async function enforceCacheBudget(cache, maxEntries = MAX_CACHE_ENTRIES) {
  const requests = await cache.keys();
  if (requests.length <= maxEntries) return;

  const excess = requests.length - maxEntries;
  let deletedCount = 0;

  for (const req of requests) {
    if (deletedCount >= excess) break;
    const url = new URL(req.url, 'http://localhost');
    const norm = normalizePath(url.pathname);
    // Never evict essential shell assets
    if (STATIC_EXPLICIT_ALLOWLIST.has(norm) || SHELL_PRECACHE_ASSETS.includes(norm)) {
      continue;
    }
    await cache.delete(req);
    deletedCount++;
  }
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
          return fetch(request).then(async (response) => {
            const url = new URL(request.url, self.location.origin);
            const contentType = response.headers ? response.headers.get('content-type') : null;
            if (canCacheResponse(url.pathname, response.status, response.type, contentType)) {
              const copy = response.clone();
              const cache = await caches.open(SHELL_CACHE_NAME);
              await cache.put(request, copy);
              await enforceCacheBudget(cache, MAX_CACHE_ENTRIES);
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
    MAX_CACHE_ENTRIES,
    SHELL_PRECACHE_ASSETS,
    STATIC_EXPLICIT_ALLOWLIST,
    STATIC_ASSET_EXTENSIONS,
    normalizePath,
    isApiOrOperationalPath,
    isAllowedStaticAsset,
    isCompatibleContentType,
    canCacheResponse,
    enforceCacheBudget,
    shouldDeleteCache,
    evaluateFetchPolicy,
  };
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = globalThis.__PICO_SW__;
}
