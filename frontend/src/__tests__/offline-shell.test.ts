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
});
