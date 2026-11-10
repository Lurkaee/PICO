import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { api, setCsrfToken, getPicoProfile } from '../api/client';
import * as tauriCore from '@tauri-apps/api/core';
import fs from 'node:fs';
import path from 'node:path';

vi.mock('@tauri-apps/api/core', () => ({
  isTauri: vi.fn(),
  invoke: vi.fn(),
}));

describe('P11.2.2.2 Slice 1B Native Tauri Command Integration & Explicit Profiles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setCsrfToken(null);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  describe('Explicit Profile: browser-development', () => {
    beforeEach(() => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(false);
      vi.stubEnv('VITE_PICO_PROFILE', 'browser-development');
    });

    it('1. browser-development profile uses standard loopback HTTP fetch for pairing and retains dev workflow', async () => {
      expect(getPicoProfile()).toBe('browser-development');
      const mockFetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: 'OK',
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          status: 'paired',
          csrf_token: 'csrf_mock_token_123',
          expires_at: '2026-11-09T20:00:00Z',
          approver: 'local_operator',
        }),
      } as unknown as Response);

      const result = await api.pairOperator('654321');

      expect(tauriCore.invoke).not.toHaveBeenCalled();
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(mockFetch).toHaveBeenCalledWith(
        '/api/operator/pair',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ code: '654321' }),
        })
      );
      expect(result.paired).toBe(true);
      expect(result.csrf_token).toBe('csrf_mock_token_123');
    });

    it('uses standard HTTP fetch for getHealth', async () => {
      const mockFetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: 'OK',
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          status: 'healthy',
        }),
      } as unknown as Response);

      const result = await api.getHealth();

      expect(tauriCore.invoke).not.toHaveBeenCalled();
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(mockFetch).toHaveBeenCalledWith('/health', expect.anything());
      expect(result.status).toBe('healthy');
    });
  });

  describe('Explicit Profile: managed-desktop', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_PICO_PROFILE', 'managed-desktop');
    });

    it('2. managed-desktop profile uses native Tauri IPC for pairing and getHealth', async () => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(true);
      vi.mocked(tauriCore.invoke)
        .mockResolvedValueOnce(JSON.stringify({ status: 'paired', approver: 'local_desktop_companion' }))
        .mockResolvedValueOnce(JSON.stringify({ status: 'healthy', components: { db: 'ok' } }));

      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      const pairResult = await api.pairOperator('123456');
      expect(tauriCore.invoke).toHaveBeenCalledWith('pico_pair_operator', { code: '123456' });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(pairResult.paired).toBe(true);
      expect(pairResult.approver).toBe('local_desktop_companion');
      expect(pairResult.csrf_token).toBeUndefined();

      const healthResult = await api.getHealth();
      expect(tauriCore.invoke).toHaveBeenCalledWith('pico_get_system_status');
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(healthResult.status).toBe('healthy');
    });

    it('3. managed-desktop profile fails closed without browser fallback when isTauri() is false', async () => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(false);
      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      await expect(api.pairOperator('123456')).rejects.toThrow(
        'Tauri native bridge unavailable in managed-desktop profile'
      );
      await expect(api.getHealth()).rejects.toThrow(
        'Tauri native bridge unavailable in managed-desktop profile'
      );

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(tauriCore.invoke).not.toHaveBeenCalled();
    });

    it('4. managed-desktop profile fails closed when IPC rejects without browser fallback', async () => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(true);
      vi.mocked(tauriCore.invoke)
        .mockRejectedValueOnce(new Error('Pairing locked out'))
        .mockRejectedValueOnce(new Error('Backend offline'));
      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      await expect(api.pairOperator('123456')).rejects.toThrow('Pairing locked out');
      await expect(api.getHealth()).rejects.toThrow('Backend offline');

      expect(fetchSpy).not.toHaveBeenCalled();
    });
  });

  describe('Explicit Profile: packaged-production', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_PICO_PROFILE', 'packaged-production');
    });

    it('5. packaged-production profile uses native Tauri IPC', async () => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(true);
      vi.mocked(tauriCore.invoke)
        .mockResolvedValueOnce(JSON.stringify({ status: 'paired', approver: 'production_companion' }))
        .mockResolvedValueOnce(JSON.stringify({ status: 'healthy' }));

      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      const res = await api.pairOperator('999888');
      expect(tauriCore.invoke).toHaveBeenCalledWith('pico_pair_operator', { code: '999888' });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(res.paired).toBe(true);

      const health = await api.getHealth();
      expect(tauriCore.invoke).toHaveBeenCalledWith('pico_get_system_status');
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(health.status).toBe('healthy');
    });

    it('6. packaged-production profile fails closed without browser fallback when isTauri() is false', async () => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(false);
      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      await expect(api.pairOperator('123456')).rejects.toThrow(
        'Tauri native bridge unavailable in packaged-production profile'
      );
      await expect(api.getHealth()).rejects.toThrow(
        'Tauri native bridge unavailable in packaged-production profile'
      );

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(tauriCore.invoke).not.toHaveBeenCalled();
    });

    it('7. packaged-production profile fails closed when IPC rejects without browser fallback', async () => {
      vi.mocked(tauriCore.isTauri).mockReturnValue(true);
      vi.mocked(tauriCore.invoke).mockRejectedValueOnce(new Error('Bridge handshake timeout'));
      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      await expect(api.pairOperator('123456')).rejects.toThrow('Bridge handshake timeout');
      expect(fetchSpy).not.toHaveBeenCalled();
    });
  });

  describe('Profile Validation & Conflict Guards', () => {
    it('8. unknown profile fails closed immediately', async () => {
      vi.stubEnv('VITE_PICO_PROFILE', 'unsupported-staging');
      expect(() => getPicoProfile()).toThrow('Invalid or missing PICO profile: unsupported-staging');
      await expect(api.pairOperator('123456')).rejects.toThrow('Invalid or missing PICO profile');
      await expect(api.getHealth()).rejects.toThrow('Invalid or missing PICO profile');
    });

    it('9. missing profile fails closed immediately', async () => {
      vi.stubEnv('VITE_PICO_PROFILE', '');
      // When VITE_PICO_PROFILE is empty, MODE defaults to 'test' under vitest which is not an approved runtime profile
      expect(() => getPicoProfile()).toThrow('Invalid or missing PICO profile: test');
      await expect(api.pairOperator('123456')).rejects.toThrow('Invalid or missing PICO profile: test');
    });

    it('10. profile conflict: browser-development inside Tauri host fails closed', async () => {
      vi.stubEnv('VITE_PICO_PROFILE', 'browser-development');
      vi.mocked(tauriCore.isTauri).mockReturnValue(true);

      expect(() => getPicoProfile()).toThrow('Profile conflict: browser-development profile cannot be used within Tauri host');
      await expect(api.pairOperator('123456')).rejects.toThrow('Profile conflict');
      await expect(api.getHealth()).rejects.toThrow('Profile conflict');
    });

    it('11. operator credentials (cookies, tokens) are completely absent in IPC companion response', async () => {
      vi.stubEnv('VITE_PICO_PROFILE', 'managed-desktop');
      vi.mocked(tauriCore.isTauri).mockReturnValue(true);
      vi.mocked(tauriCore.invoke).mockResolvedValueOnce(
        JSON.stringify({
          status: 'paired',
          approver: 'local_desktop_companion',
          extra_internal_field: 'should_not_leak',
        })
      );

      const res = await api.pairOperator('123456');
      expect(res.paired).toBe(true);
      expect(res.approver).toBe('local_desktop_companion');
      expect((res as any).csrf_token).toBeUndefined();
      expect((res as any).cookie).toBeUndefined();
      expect((res as any).session_id).toBeUndefined();
    });

    it('12. package.json configures dev:tauri with explicit --mode managed-desktop', () => {
      const pkgPath = path.resolve(__dirname, '../../package.json');
      const pkgJson = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
      expect(pkgJson.scripts['dev:tauri']).toContain('--mode managed-desktop');
    });

    it('13. package.json configures build with explicit --mode packaged-production', () => {
      const pkgPath = path.resolve(__dirname, '../../package.json');
      const pkgJson = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
      expect(pkgJson.scripts['build']).toContain('--mode packaged-production');
    });
  });
});
