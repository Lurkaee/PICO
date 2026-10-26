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

  describe('In-Flight Probe Race & Stop-Safety Lifecycle', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    function trackBrowserListeners() {
      const counts = { online: 0, offline: 0, visibilitychange: 0 };
      const origWinAdd = window.addEventListener;
      const origWinRemove = window.removeEventListener;
      const origDocAdd = document.addEventListener;
      const origDocRemove = document.removeEventListener;

      window.addEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type in counts) (counts as any)[type]++;
        return origWinAdd.call(window, type, listener, options);
      });
      window.removeEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type in counts) (counts as any)[type]--;
        return origWinRemove.call(window, type, listener, options);
      });
      document.addEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type in counts) (counts as any)[type]++;
        return origDocAdd.call(document, type, listener, options);
      });
      document.removeEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type in counts) (counts as any)[type]--;
        return origDocRemove.call(document, type, listener, options);
      });

      return {
        counts,
        restore: () => {
          window.addEventListener = origWinAdd;
          window.removeEventListener = origWinRemove;
          document.addEventListener = origDocAdd;
          document.removeEventListener = origDocRemove;
        },
      };
    }

    function createDeferred<T>() {
      let resolve!: (val: T) => void;
      let reject!: (err: any) => void;
      const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
      });
      return { promise, resolve, reject };
    }

    it('constructing a manager does not attach listeners, start a timer, or initiate network activity', () => {
      const tracker = trackBrowserListeners();
      try {
        const manager = new ConnectivityManager();
        expect(manager.started).toBe(false);
        expect(manager.disposed).toBe(false);
        expect(manager.generation).toBe(0);
        expect(api.getHealth).not.toHaveBeenCalled();
        expect(vi.getTimerCount()).toBe(0);
        expect(tracker.counts).toEqual({ online: 0, offline: 0, visibilitychange: 0 });
      } finally {
        tracker.restore();
      }
    });

    it('start() initiates the intended probe and listeners exactly once', () => {
      const tracker = trackBrowserListeners();
      vi.mocked(api.getHealth).mockResolvedValue({ status: 'ok' } as any);

      try {
        const manager = new ConnectivityManager();
        manager.start();
        expect(manager.started).toBe(true);
        expect(api.getHealth).toHaveBeenCalledTimes(1);
        expect(tracker.counts).toEqual({ online: 1, offline: 1, visibilitychange: 1 });

        // Idempotent: second start call is a no-op
        manager.start();
        expect(api.getHealth).toHaveBeenCalledTimes(1);
        expect(tracker.counts).toEqual({ online: 1, offline: 1, visibilitychange: 1 });

        manager.dispose();
      } finally {
        tracker.restore();
      }
    });

    it('calling stop() twice leaves zero owned timers and zero attached browser listeners', () => {
      const tracker = trackBrowserListeners();
      vi.mocked(api.getHealth).mockResolvedValue({ status: 'ok' } as any);

      try {
        const manager = new ConnectivityManager();
        manager.start();
        expect(tracker.counts).toEqual({ online: 1, offline: 1, visibilitychange: 1 });

        // First stop
        manager.stop();
        expect(manager.started).toBe(false);
        expect(manager.disposed).toBe(false);
        expect(vi.getTimerCount()).toBe(0);
        expect(tracker.counts).toEqual({ online: 0, offline: 0, visibilitychange: 0 });

        // Second stop (idempotent)
        manager.stop();
        expect(manager.started).toBe(false);
        expect(manager.disposed).toBe(false);
        expect(vi.getTimerCount()).toBe(0);
        expect(tracker.counts).toEqual({ online: 0, offline: 0, visibilitychange: 0 });
      } finally {
        tracker.restore();
      }
    });

    it('if a successful health response resolves after stop(), it cannot schedule another polling timer or probe', async () => {
      const deferred = createDeferred<any>();
      vi.mocked(api.getHealth).mockImplementationOnce(() => deferred.promise);

      const manager = new ConnectivityManager({ baseIntervalMs: 3000 });
      manager.start();
      expect(api.getHealth).toHaveBeenCalledTimes(1);
      expect(manager.started).toBe(true);

      // Stop while probe is in flight
      manager.stop();
      expect(manager.started).toBe(false);
      expect(vi.getTimerCount()).toBe(0);

      // Probe resolves with healthy response AFTER stop
      deferred.resolve({ status: 'ok', environment: 'dev', version: '0.1.0', timestamp: new Date().toISOString() });
      await vi.waitFor(() => expect(vi.getTimerCount()).toBe(0));

      // Observable invariant: zero timers scheduled
      expect(vi.getTimerCount()).toBe(0);

      // Advancing time must NOT trigger another probe
      vi.advanceTimersByTime(30000);
      expect(api.getHealth).toHaveBeenCalledTimes(1);
    });

    it('if a failed health response resolves after stop(), it cannot schedule a retry timer', async () => {
      const deferred = createDeferred<any>();
      vi.mocked(api.getHealth).mockImplementationOnce(() => deferred.promise);

      const manager = new ConnectivityManager({ baseIntervalMs: 3000 });
      manager.start();
      expect(api.getHealth).toHaveBeenCalledTimes(1);

      // Stop while probe is in flight
      manager.stop();
      expect(manager.started).toBe(false);
      expect(vi.getTimerCount()).toBe(0);

      // Probe rejects with network failure AFTER stop
      deferred.reject(new NetworkError('Backend connection lost'));
      await vi.waitFor(() => expect(vi.getTimerCount()).toBe(0));

      // Observable invariant: zero retry timers scheduled
      expect(vi.getTimerCount()).toBe(0);

      // Advancing time must NOT trigger another probe
      vi.advanceTimersByTime(30000);
      expect(api.getHealth).toHaveBeenCalledTimes(1);
    });

    it('a stale probe from a previous lifecycle cannot overwrite state or interfere with subsequently restarted lifecycle', async () => {
      const probe1 = createDeferred<any>();
      const probe2 = createDeferred<any>();

      vi.mocked(api.getHealth)
        .mockImplementationOnce(() => probe1.promise)
        .mockImplementationOnce(() => probe2.promise);

      const manager = new ConnectivityManager();

      // Lifecycle 1: start probe 1
      manager.start();
      expect(api.getHealth).toHaveBeenCalledTimes(1);

      // Stop Lifecycle 1
      manager.stop();
      expect(manager.started).toBe(false);

      // Lifecycle 2: restart manager and trigger probe 2
      manager.start();
      expect(api.getHealth).toHaveBeenCalledTimes(2);

      // Now probe 1 (stale, from Lifecycle 1) resolves with 'online'
      probe1.resolve({ status: 'ok' });
      await Promise.resolve();

      // State must NOT be overwritten by stale probe 1
      expect(manager.getState().status).toBe('unknown');

      // Now probe 2 (current, from Lifecycle 2) rejects with network error
      probe2.reject(new NetworkError('Down'));
      await vi.waitFor(() => expect(manager.getState().status).toBe('offline'));

      // State truthfully reflects Lifecycle 2 outcome (offline), not stale probe 1
      expect(manager.getState().status).toBe('offline');

      manager.dispose();
    });

    it('start() after stop() works on the same non-disposed manager without duplicating listeners', async () => {
      const tracker = trackBrowserListeners();
      vi.mocked(api.getHealth).mockResolvedValue({ status: 'ok' } as any);

      try {
        const manager = new ConnectivityManager({ baseIntervalMs: 2000 });

        // Start 1
        manager.start();
        expect(tracker.counts).toEqual({ online: 1, offline: 1, visibilitychange: 1 });
        await vi.waitFor(() => expect(manager.getState().status).toBe('online'));

        // Stop 1
        manager.stop();
        expect(tracker.counts).toEqual({ online: 0, offline: 0, visibilitychange: 0 });
        expect(vi.getTimerCount()).toBe(0);

        // Start 2 (restarting the same manager)
        manager.start();
        expect(manager.started).toBe(true);
        expect(tracker.counts).toEqual({ online: 1, offline: 1, visibilitychange: 1 }); // Not duplicated (1, not 2)

        // Advance timer to verify automatic polling resumed
        vi.advanceTimersByTime(2500);
        expect(api.getHealth).toHaveBeenCalledTimes(2);

        manager.dispose();
        expect(tracker.counts).toEqual({ online: 0, offline: 0, visibilitychange: 0 });
      } finally {
        tracker.restore();
      }
    });

    it('dispose() during an in-flight probe prevents all future automatic work and later start() remains ineffective', async () => {
      const deferred = createDeferred<any>();
      vi.mocked(api.getHealth).mockImplementationOnce(() => deferred.promise);

      const manager = new ConnectivityManager({ baseIntervalMs: 2000 });
      manager.start();
      expect(api.getHealth).toHaveBeenCalledTimes(1);

      // Dispose while probe is in flight
      manager.dispose();
      expect(manager.disposed).toBe(true);
      expect(manager.started).toBe(false);

      // Resolve probe after disposal
      deferred.resolve({ status: 'ok' });
      await Promise.resolve();

      // Zero timers scheduled
      expect(vi.getTimerCount()).toBe(0);

      // Calling start() on disposed manager must be completely ineffective
      manager.start();
      expect(manager.started).toBe(false);
      expect(api.getHealth).toHaveBeenCalledTimes(1); // No new probe
      expect(vi.getTimerCount()).toBe(0);
    });

    it('a stopped manager retains no timer or listener loop and can be cleanly garbage-collected', () => {
      const tracker = trackBrowserListeners();
      try {
        const manager = new ConnectivityManager();
        manager.start();
        expect(tracker.counts).toEqual({ online: 1, offline: 1, visibilitychange: 1 });

        manager.stop();
        // Fully idle: zero timers, zero DOM event listeners
        expect(vi.getTimerCount()).toBe(0);
        expect(tracker.counts).toEqual({ online: 0, offline: 0, visibilitychange: 0 });
      } finally {
        tracker.restore();
      }
    });
  });
});
