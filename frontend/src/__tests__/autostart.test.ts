import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api } from '../api/client';
import * as tauriCore from '@tauri-apps/api/core';

vi.mock('@tauri-apps/api/core', () => ({
  isTauri: vi.fn(),
  invoke: vi.fn(),
}));

describe('Windows Desktop Auto-Start API & IPC', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns false/empty when not running inside Tauri', async () => {
    vi.mocked(tauriCore.isTauri).mockReturnValue(false);

    const status = await api.getAutoStartStatus();
    expect(status).toEqual({
      enabled: false,
      executable_path: null,
      matches_current_exe: false,
    });
    expect(tauriCore.invoke).not.toHaveBeenCalled();

    const setRes = await api.setAutoStart(true);
    expect(setRes).toEqual({
      enabled: false,
      executable_path: null,
      matches_current_exe: false,
    });
    expect(tauriCore.invoke).not.toHaveBeenCalled();
  });

  it('invokes native pico_get_autostart_status when running inside Tauri', async () => {
    vi.mocked(tauriCore.isTauri).mockReturnValue(true);
    vi.mocked(tauriCore.invoke).mockResolvedValueOnce({
      enabled: true,
      executable_path: 'C:\\Program Files\\PICO\\PICO.exe',
      matches_current_exe: true,
    });

    const status = await api.getAutoStartStatus();
    expect(tauriCore.invoke).toHaveBeenCalledWith('pico_get_autostart_status');
    expect(status.enabled).toBe(true);
    expect(status.matches_current_exe).toBe(true);
  });

  it('invokes native pico_set_autostart with parameters when running inside Tauri', async () => {
    vi.mocked(tauriCore.isTauri).mockReturnValue(true);
    vi.mocked(tauriCore.invoke).mockResolvedValueOnce({
      enabled: true,
      executable_path: 'C:\\Program Files\\PICO\\PICO.exe',
      matches_current_exe: true,
    });

    const res = await api.setAutoStart(true);
    expect(tauriCore.invoke).toHaveBeenCalledWith('pico_set_autostart', { enabled: true });
    expect(res.enabled).toBe(true);
  });
});
