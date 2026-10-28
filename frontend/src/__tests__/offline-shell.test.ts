import { describe, it, expect } from 'vitest';
import '../../public/sw.js';

describe('Offline Application Shell Service Worker Real Policy Module', () => {
  const rawEvaluate = (globalThis as any).__PICO_SW__.evaluateFetchPolicy;
  const evaluateFetchPolicy = (method: string, url: string, mode = 'cors', origin = 'http://localhost:5173') =>
    rawEvaluate(method, url, mode, origin);

  it('bypasses service worker for cross-origin requests', () => {
    expect(evaluateFetchPolicy('GET', 'https://api.openai.com/v1/models', 'cors', 'http://localhost:5173')).toEqual({
      handled: false,
      reason: 'cross-origin-bypass',
    });
    expect(evaluateFetchPolicy('GET', 'https://fonts.googleapis.com/css', 'no-cors', 'http://localhost:5173')).toEqual({
      handled: false,
      reason: 'cross-origin-bypass',
    });
  });

  it('bypasses service worker for all mutating operations (POST, PUT, DELETE, PATCH)', () => {
    expect(evaluateFetchPolicy('POST', 'http://localhost:5173/api/chat/stream')).toEqual({
      handled: false,
      reason: 'non-get-mutation-bypass',
    });
    expect(evaluateFetchPolicy('POST', 'http://localhost:5173/api/approvals/exec_1/approve')).toEqual({
      handled: false,
      reason: 'non-get-mutation-bypass',
    });
    expect(evaluateFetchPolicy('DELETE', 'http://localhost:5173/api/memory/semantic/fact_1')).toEqual({
      handled: false,
      reason: 'non-get-mutation-bypass',
    });
    expect(evaluateFetchPolicy('PUT', 'http://localhost:5173/assets/test.js')).toEqual({
      handled: false,
      reason: 'non-get-mutation-bypass',
    });
  });

  it('strictly treats API, health, and operational endpoints as network-only without caching', () => {
    // API endpoints
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/api/conversations')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/API/conversations')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173//api/tools')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/api/operator/session')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/api/workflows')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });

    // Health endpoints
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/health')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/HEALTH/')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });

    // Operational docs and schema endpoints
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/openapi.json')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/docs')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/redoc')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
  });

  it('serves application shell for same-origin non-API navigation mode', () => {
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/', 'navigate')).toEqual({
      handled: true,
      strategy: 'network-first-shell-fallback',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/workflows', 'navigate')).toEqual({
      handled: true,
      strategy: 'network-first-shell-fallback',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/memory', 'navigate')).toEqual({
      handled: true,
      strategy: 'network-first-shell-fallback',
    });

    // Navigation targeting API endpoints must NOT fall back to SPA shell
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/api/conversations', 'navigate')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/health', 'navigate')).toEqual({
      handled: false,
      reason: 'api-operational-network-only',
    });
  });

  it('serves explicitly allowed static assets via cache-first strategy', () => {
    // Versioned bundle assets
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/index-D7h4sK.js')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/index-C8g2oL.css')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });

    // Explicit static shell assets
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/favicon.ico')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/manifest.webmanifest')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/vite.svg')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
  });

  it('bypasses cache for unknown, arbitrary GET paths', () => {
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/some/unknown/page.html', 'cors')).toEqual({
      handled: false,
      reason: 'unknown-path-cache-bypass',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/data.json', 'cors')).toEqual({
      handled: false,
      reason: 'unknown-path-cache-bypass',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/random-script.js', 'cors')).toEqual({
      handled: false,
      reason: 'unknown-path-cache-bypass',
    });
  });

  it('limits /assets/ caching to legitimate built static assets with allowed extensions', () => {
    // Valid static assets under /assets/
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/vendor.js')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/theme.css')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/logo.webp')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/font.woff2')).toEqual({
      handled: true,
      strategy: 'cache-first-static-asset',
    });

    // Arbitrary responses or non-static files beneath /assets/ are bypassed
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/payload.bin')).toEqual({
      handled: false,
      reason: 'unknown-path-cache-bypass',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/secret.txt')).toEqual({
      handled: false,
      reason: 'unknown-path-cache-bypass',
    });
    expect(evaluateFetchPolicy('GET', 'http://localhost:5173/assets/noextension')).toEqual({
      handled: false,
      reason: 'unknown-path-cache-bypass',
    });
  });

  describe('Service Worker Activation Cache Cleanup', () => {
    const sw = (globalThis as any).__PICO_SW__;
    const shouldDeleteCache = sw.shouldDeleteCache;
    const shellCacheName = sw.SHELL_CACHE_NAME;

    it('identifies only obsolete PICO-owned caches for deletion and preserves current and third-party caches', () => {
      // Obsolete PICO caches: must delete
      expect(shouldDeleteCache('pico-shell-v1', shellCacheName)).toBe(true);
      expect(shouldDeleteCache('pico-shell-v1.1', shellCacheName)).toBe(true);
      expect(shouldDeleteCache('pico-shell-legacy', shellCacheName)).toBe(true);

      // Current active PICO cache: must NOT delete
      expect(shouldDeleteCache(shellCacheName, shellCacheName)).toBe(false);

      // Third-party or unrelated application caches sharing the same origin: must NOT delete
      expect(shouldDeleteCache('workbox-precache-v1', shellCacheName)).toBe(false);
      expect(shouldDeleteCache('unrelated-app-cache', shellCacheName)).toBe(false);
      expect(shouldDeleteCache('analytics-offline-v1', shellCacheName)).toBe(false);
      expect(shouldDeleteCache('pico_other_data', shellCacheName)).toBe(false); // wrong delimiter/namespace
    });

    it('cleans up only obsolete PICO caches during activation against simulated Cache Storage', async () => {
      const existingCaches = [
        shellCacheName,
        'pico-shell-v1',
        'pico-shell-v1.2',
        'third-party-cache-v1',
        'shared-origin-app',
      ];
      const deletedCaches: string[] = [];

      const fakeCacheStorage = {
        keys: async () => existingCaches,
        delete: async (name: string) => {
          deletedCaches.push(name);
          return true;
        },
      };

      const keys = await fakeCacheStorage.keys();
      await Promise.all(
        keys.map((cacheName) => {
          if (shouldDeleteCache(cacheName, shellCacheName)) {
            return fakeCacheStorage.delete(cacheName);
          }
        })
      );

      // Only obsolete PICO caches were removed
      expect(deletedCaches).toEqual(['pico-shell-v1', 'pico-shell-v1.2']);

      // Current cache and unrelated caches were preserved
      expect(deletedCaches).not.toContain(shellCacheName);
      expect(deletedCaches).not.toContain('third-party-cache-v1');
      expect(deletedCaches).not.toContain('shared-origin-app');
    });
  });

  describe('Static Resource Content-Type Validation & Response Eligibility', () => {
    const sw = (globalThis as any).__PICO_SW__;
    const isCompatibleContentType = sw.isCompatibleContentType;
    const canCacheResponse = sw.canCacheResponse;

    it('validates compatible Content-Type headers for static assets and HTML shell', () => {
      // JavaScript
      expect(isCompatibleContentType('/assets/index-D7h4sK.js', 'application/javascript')).toBe(true);
      expect(isCompatibleContentType('/assets/index-D7h4sK.js', 'text/javascript; charset=utf-8')).toBe(true);

      // CSS
      expect(isCompatibleContentType('/assets/index-C8g2oL.css', 'text/css; charset=utf-8')).toBe(true);

      // Images and Fonts
      expect(isCompatibleContentType('/assets/logo.svg', 'image/svg+xml')).toBe(true);
      expect(isCompatibleContentType('/assets/banner.png', 'image/png')).toBe(true);
      expect(isCompatibleContentType('/assets/font.woff2', 'font/woff2')).toBe(true);
      expect(isCompatibleContentType('/assets/icon.ico', 'image/x-icon')).toBe(true);

      // HTML Shell
      expect(isCompatibleContentType('/', 'text/html; charset=utf-8')).toBe(true);
      expect(isCompatibleContentType('/index.html', 'text/html')).toBe(true);
      expect(isCompatibleContentType('/manifest.json', 'application/json')).toBe(true);
    });

    it('rejects HTML or JSON error/fallback responses returned for static assets', () => {
      // SPA index.html returned with 200 for missing script
      expect(isCompatibleContentType('/assets/missing.js', 'text/html; charset=utf-8')).toBe(false);

      // JSON error payload returned with 200 for stylesheet
      expect(isCompatibleContentType('/assets/theme.css', 'application/json')).toBe(false);

      // HTML returned for font or image
      expect(isCompatibleContentType('/assets/font.woff2', 'text/html')).toBe(false);
      expect(isCompatibleContentType('/assets/image.png', 'text/html')).toBe(false);

      // Missing or invalid content-type
      expect(isCompatibleContentType('/assets/bundle.js', '')).toBe(false);
      expect(isCompatibleContentType('/assets/bundle.js', null)).toBe(false);
      expect(isCompatibleContentType('/assets/bundle.js', 'application/octet-stream')).toBe(false);
    });

    it('enforces that only successful basic responses with compatible content-type are eligible for caching', () => {
      // Eligible response
      expect(canCacheResponse('/assets/index.js', 200, 'basic', 'application/javascript')).toBe(true);
      expect(canCacheResponse('/assets/theme.css', 200, 'basic', 'text/css')).toBe(true);

      // Non-200 responses are never cached
      expect(canCacheResponse('/assets/index.js', 404, 'basic', 'application/javascript')).toBe(false);
      expect(canCacheResponse('/assets/index.js', 500, 'basic', 'application/javascript')).toBe(false);
      expect(canCacheResponse('/assets/index.js', 304, 'basic', 'application/javascript')).toBe(false);

      // Non-basic responses (opaque, cors) are never cached
      expect(canCacheResponse('/assets/index.js', 200, 'opaque', 'application/javascript')).toBe(false);
      expect(canCacheResponse('/assets/index.js', 200, 'cors', 'application/javascript')).toBe(false);

      // 200 basic response with incompatible fallback Content-Type is never cached
      expect(canCacheResponse('/assets/index.js', 200, 'basic', 'text/html; charset=utf-8')).toBe(false);
      expect(canCacheResponse('/assets/theme.css', 200, 'basic', 'application/json')).toBe(false);
    });

    it('rejects nested or arbitrary subdirectory paths beneath /assets/', () => {
      // Direct file in /assets/ is allowed
      expect(sw.isAllowedStaticAsset('/assets/index-123.js')).toBe(true);
      expect(sw.isAllowedStaticAsset('/assets/style.css')).toBe(true);

      // Subdirectory nesting beneath /assets/ is forbidden
      expect(sw.isAllowedStaticAsset('/assets/sub/deep.js')).toBe(false);
      expect(sw.isAllowedStaticAsset('/assets/api/data.js')).toBe(false);
      expect(sw.isAllowedStaticAsset('/assets/../evil.js')).toBe(false);
    });
  });

  describe('Bounded Cache Budget Enforcement', () => {
    const sw = (globalThis as any).__PICO_SW__;
    const enforceCacheBudget = sw.enforceCacheBudget;

    it('enforces maximum cache entry cap by evicting oldest non-shell entries while preserving shell assets', async () => {
      const shellUrls = ['/', '/index.html', '/favicon.ico'];
      const dynamicAssetUrls: string[] = [];
      for (let i = 1; i <= 60; i++) {
        dynamicAssetUrls.push(`/assets/chunk-${i}.js`);
      }

      // Initial cache store has 3 shell assets + 60 dynamic assets = 63 entries
      const cacheStore = new Map<string, any>();
      for (const u of [...shellUrls, ...dynamicAssetUrls]) {
        cacheStore.set(`http://localhost:5173${u}`, { url: `http://localhost:5173${u}` });
      }

      const mockCache = {
        keys: async () => Array.from(cacheStore.values()).map((v) => ({ url: v.url })),
        delete: async (req: { url: string }) => {
          return cacheStore.delete(req.url);
        },
      };

      // Cap at 50 entries
      await enforceCacheBudget(mockCache, 50);

      // Total remaining entries must be <= 50
      expect(cacheStore.size).toBe(50);

      // Critical Invariant: Shell assets must NEVER be evicted!
      for (const s of shellUrls) {
        expect(cacheStore.has(`http://localhost:5173${s}`)).toBe(true);
      }

      // 13 oldest dynamic assets (chunk-1 through chunk-13) were evicted
      for (let i = 1; i <= 13; i++) {
        expect(cacheStore.has(`http://localhost:5173/assets/chunk-${i}.js`)).toBe(false);
      }

      // Newer assets remain in cache
      for (let i = 14; i <= 60; i++) {
        expect(cacheStore.has(`http://localhost:5173/assets/chunk-${i}.js`)).toBe(true);
      }
    });

    it('takes no eviction action when cache entries are below maximum limit', async () => {
      const cacheStore = new Map<string, any>();
      cacheStore.set('http://localhost:5173/', { url: 'http://localhost:5173/' });
      cacheStore.set('http://localhost:5173/index.html', { url: 'http://localhost:5173/index.html' });
      cacheStore.set('http://localhost:5173/assets/app.js', { url: 'http://localhost:5173/assets/app.js' });

      const mockCache = {
        keys: async () => Array.from(cacheStore.values()).map((v) => ({ url: v.url })),
        delete: async (req: { url: string }) => cacheStore.delete(req.url),
      };

      await enforceCacheBudget(mockCache, 50);
      expect(cacheStore.size).toBe(3);
    });
  });
});
