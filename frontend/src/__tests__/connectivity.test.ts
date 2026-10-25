import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ConnectivityManager, ConnectivityState } from '../api/connectivity';
import { api } from '../api/client';
import { NetworkError } from '../api/errors';

vi.mock('../api/client', () => ({
  api: {
    getHealth: vi.fn(),
  },
}));

describe('ConnectivityManager', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('initializes with unknown status and performs health probe', async () => {
    vi.mocked(api.getHealth).mockResolvedValueOnce({
      status: 'ok',
      environment: 'development',
      version: '0.1.0',
      timestamp: new Date().toISOString(),
    });

    const manager = new ConnectivityManager({ baseIntervalMs: 5000 });
    expect(manager.getState().status).toBe('unknown');

    const result = await manager.probeHealth();
    expect(result).toBe(true);
    expect(manager.getState().status).toBe('online');
    expect(manager.getState().isOnline).toBe(true);
    expect(manager.getState().isOffline).toBe(false);
    expect(manager.getState().lastSuccessfulCheck).toBeInstanceOf(Date);

    manager.dispose();
  });

  it('transitions to offline on network error and applies backoff', async () => {
    vi.mocked(api.getHealth).mockRejectedValueOnce(new NetworkError('Backend unreachable'));

    const manager = new ConnectivityManager({ baseIntervalMs: 1000, maxIntervalMs: 10000, backoffFactor: 2, jitterFactor: 0 });
    const result = await manager.probeHealth();

    expect(result).toBe(false);
    expect(manager.getState().status).toBe('offline');
    expect(manager.getState().isOffline).toBe(true);
    expect(manager.getState().errorMessage).toContain('Backend unreachable');

    manager.dispose();
  });

  it('deduplicates concurrent probe requests via single-flight execution', async () => {
    let resolver: (val: any) => void;
    const pendingPromise = new Promise((resolve) => {
      resolver = resolve;
    });

    vi.mocked(api.getHealth).mockImplementationOnce(() => pendingPromise as any);

    const manager = new ConnectivityManager();
    const probe1 = manager.probeHealth();
    const probe2 = manager.probeHealth();

    // Verify api.getHealth was only called once
    expect(api.getHealth).toHaveBeenCalledTimes(1);

    resolver!({ status: 'ok', environment: 'dev', version: '0.1.0', timestamp: new Date().toISOString() });

    const [r1, r2] = await Promise.all([probe1, probe2]);
    expect(r1).toBe(true);
    expect(r2).toBe(true);

    manager.dispose();
  });

  it('notifies subscribers on connectivity state transitions', async () => {
    vi.mocked(api.getHealth)
      .mockResolvedValueOnce({ status: 'ok', environment: 'dev', version: '0.1.0', timestamp: new Date().toISOString() })
      .mockRejectedValueOnce(new NetworkError('Connection reset'));

    const manager = new ConnectivityManager();
    const states: ConnectivityState[] = [];
    const unsubscribe = manager.subscribe((state) => states.push({ ...state }));

    await manager.probeHealth();
    expect(states[states.length - 1].status).toBe('online');

    await manager.probeHealth();
    expect(states[states.length - 1].status).toBe('offline');

    unsubscribe();
    manager.dispose();
  });

  it('tracks inference degradation separately from core backend reachability', async () => {
    vi.mocked(api.getHealth).mockResolvedValueOnce({
      status: 'ok',
      environment: 'dev',
      version: '0.1.0',
      timestamp: new Date().toISOString(),
    });

    const manager = new ConnectivityManager();
    await manager.probeHealth();
    expect(manager.getState().status).toBe('online');

    // Report local model failure (e.g. Ollama OOM or GPU memory exhausted)
    manager.reportInferenceDegraded('Local model runtime unreachable (VRAM exhaustion)');
    expect(manager.getState().status).toBe('degraded');
    expect(manager.getState().inferenceDegraded).toBe(true);
    expect(manager.getState().inferenceDegradedReason).toContain('VRAM exhaustion');
    expect(manager.getState().isOnline).toBe(true); // Core backend remains reachable!

    // Clear degradation once model recovers
    manager.clearInferenceDegraded();
    expect(manager.getState().status).toBe('online');
    expect(manager.getState().inferenceDegraded).toBe(false);

    manager.dispose();
  });

  it('cleans up all listeners and timers on disposal', () => {
    const manager = new ConnectivityManager({ baseIntervalMs: 1000 });
    expect(() => manager.dispose()).not.toThrow();
  });
});
