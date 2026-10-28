import React, { useState } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { ConnectivityProvider, useConnectivity } from '../context/ConnectivityContext';
import { ConnectivityManager } from '../api/connectivity';
import { api } from '../api/client';

describe('ConnectivityContext & In-Memory Draft Outbox', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('manages bounded in-memory drafts without browser persistence', () => {
    let contextValue: ReturnType<typeof useConnectivity> | null = null;

    const TestComponent = () => {
      contextValue = useConnectivity();
      return (
        <div>
          <span data-testid="draft-count">{contextValue.drafts.length}</span>
        </div>
      );
    };

    render(
      <ConnectivityProvider>
        <TestComponent />
      </ConnectivityProvider>
    );

    expect(contextValue).not.toBeNull();
    expect(contextValue!.drafts).toEqual([]);

    // Add draft 1
    act(() => {
      contextValue!.saveDraft('Draft message 1', 'conv_123', 'INTERNAL');
    });
    expect(contextValue!.drafts.length).toBe(1);
    expect(contextValue!.drafts[0].text).toBe('Draft message 1');
    expect(contextValue!.drafts[0].conversationId).toBe('conv_123');

    // Add draft 2
    act(() => {
      contextValue!.addDraft('Draft message 2', 'conv_123', 'SENSITIVE');
    });
    expect(contextValue!.drafts.length).toBe(2);

    // Update draft
    const draftId = contextValue!.drafts[0].id;
    act(() => {
      contextValue!.updateDraft(draftId, 'Draft message 2 updated');
    });
    expect(contextValue!.drafts[0].text).toBe('Draft message 2 updated');

    // Remove draft
    act(() => {
      contextValue!.removeDraft(draftId);
    });
    expect(contextValue!.drafts.length).toBe(1);

    // Clear drafts
    act(() => {
      contextValue!.clearDrafts();
    });
    expect(contextValue!.drafts.length).toBe(0);

    // Verify bounded limit of 25 drafts
    act(() => {
      for (let i = 0; i < 35; i++) {
        contextValue!.addDraft(`Message number ${i}`, `conv_${i}`, 'INTERNAL');
      }
    });
    expect(contextValue!.drafts.length).toBe(25);
  });

  it('triggers onReconnected callback when transitioning back to online', async () => {
    const onReconnected = vi.fn();
    const manager = new ConnectivityManager({ baseIntervalMs: 60000 });

    vi.spyOn(manager, 'probeHealth').mockResolvedValue(true);

    render(
      <ConnectivityProvider manager={manager} onReconnected={onReconnected}>
        <div>Active App</div>
      </ConnectivityProvider>
    );

    // Simulate transition to offline then online
    act(() => {
      manager.simulateStatus('offline', 'Dropped connection');
    });
    expect(manager.getState().status).toBe('offline');

    // Simulate successful recovery to online
    act(() => {
      manager.simulateStatus('online');
    });
    expect(manager.getState().status).toBe('online');
    expect(onReconnected).toHaveBeenCalledTimes(1);

    manager.dispose();
  });

  it('survives React.StrictMode effect replay with default internally owned manager', async () => {
    let capturedContext: ReturnType<typeof useConnectivity> | null = null;
    const onReconnected = vi.fn();

    const healthSpy = vi.spyOn(api, 'getHealth').mockResolvedValue({
      status: 'healthy',
      provider: 'local',
      database: 'connected',
    } as any);

    const TestConsumer = () => {
      capturedContext = useConnectivity();
      return <div data-testid="status">{capturedContext.status}</div>;
    };

    // Render under React.StrictMode to trigger setup -> cleanup -> setup
    const { unmount } = render(
      <React.StrictMode>
        <ConnectivityProvider onReconnected={onReconnected}>
          <TestConsumer />
        </ConnectivityProvider>
      </React.StrictMode>
    );

    expect(capturedContext).not.toBeNull();
    // Initially status is 'unknown' before first health probe completes
    expect(capturedContext!.status).toBe('unknown');

    // Run health probe
    await act(async () => {
      const probeResult = await capturedContext!.checkNow();
      expect(probeResult).toBe(true);
    });

    // After probe, backend is confirmed reachable and status is online
    expect(capturedContext!.status).toBe('online');
    expect(capturedContext!.isBackendReachable).toBe(true);

    unmount();
    healthSpy.mockRestore();
  });

  it('does not recreate manager or re-subscribe when parent rerender changes onReconnected callback identity', async () => {
    let capturedContext: ReturnType<typeof useConnectivity> | null = null;

    const TestParent = () => {
      const [count, setCount] = useState(0);
      // Callback changes identity on every render!
      const handleReconnected = () => {
        console.log('reconnected call', count);
      };

      return (
        <div>
          <button data-testid="inc-btn" onClick={() => setCount((c) => c + 1)}>
            Increment
          </button>
          <ConnectivityProvider onReconnected={handleReconnected}>
            <TestConsumer />
          </ConnectivityProvider>
        </div>
      );
    };

    const TestConsumer = () => {
      capturedContext = useConnectivity();
      return <div data-testid="status">{capturedContext.status}</div>;
    };

    const { getByTestId } = render(<TestParent />);

    const initialCheckNow = capturedContext!.checkNow;

    // Trigger parent rerender with new callback reference
    act(() => {
      getByTestId('inc-btn').click();
    });

    // The manager and checkNow function identity should remain stable across rerenders
    expect(capturedContext!.checkNow).toBe(initialCheckNow);
  });

  it('triggers onReconnected on recovery to degraded inference while backend is reachable', async () => {
    const onReconnected = vi.fn();
    const manager = new ConnectivityManager({ baseIntervalMs: 60000 });

    render(
      <ConnectivityProvider manager={manager} onReconnected={onReconnected}>
        <div>Active App</div>
      </ConnectivityProvider>
    );

    // Transition offline
    act(() => {
      manager.simulateStatus('offline', 'Network unreachable');
    });
    expect(onReconnected).not.toHaveBeenCalled();

    // Transition to degraded (backend reachable, inference unavailable)
    act(() => {
      manager.simulateStatus('degraded', 'Backend healthy but Ollama unreachable');
    });

    // Reconnection callback MUST fire so operator session can be revalidated
    expect(onReconnected).toHaveBeenCalledTimes(1);
    expect(manager.isBackendReachable()).toBe(true);

    manager.dispose();
  });

  it('bounds manager construction and event listeners across repeated parent rerenders without leaks', async () => {
    // Track window and document listener registrations
    const listeners: Record<string, number> = {
      online: 0,
      offline: 0,
      visibilitychange: 0,
    };

    const originalWindowAdd = window.addEventListener;
    const originalWindowRemove = window.removeEventListener;
    const originalDocAdd = document.addEventListener;
    const originalDocRemove = document.removeEventListener;

    window.addEventListener = vi.fn((type: string, listener: any, options?: any) => {
      if (type in listeners) listeners[type]++;
      return originalWindowAdd.call(window, type, listener, options);
    });
    window.removeEventListener = vi.fn((type: string, listener: any, options?: any) => {
      if (type in listeners) listeners[type]--;
      return originalWindowRemove.call(window, type, listener, options);
    });
    document.addEventListener = vi.fn((type: string, listener: any, options?: any) => {
      if (type in listeners) listeners[type]++;
      return originalDocAdd.call(document, type, listener, options);
    });
    document.removeEventListener = vi.fn((type: string, listener: any, options?: any) => {
      if (type in listeners) listeners[type]--;
      return originalDocRemove.call(document, type, listener, options);
    });

    const TestRerenderParent = () => {
      const [count, setCount] = useState(0);
      return (
        <div>
          <button data-testid="rerender-btn" onClick={() => setCount((c) => c + 1)}>
            Rerender {count}
          </button>
          <ConnectivityProvider>
            <div data-testid="child">Count {count}</div>
          </ConnectivityProvider>
        </div>
      );
    };

    const { getByTestId, unmount } = render(<TestRerenderParent />);

    // Initial mount attaches exactly 1 of each listener
    expect(listeners.online).toBe(1);
    expect(listeners.offline).toBe(1);
    expect(listeners.visibilitychange).toBe(1);

    // Trigger 10 parent rerenders
    for (let i = 0; i < 10; i++) {
      act(() => {
        getByTestId('rerender-btn').click();
      });
    }

    // CRITICAL INVARIANT: Rerenders MUST NOT evaluate new ConnectivityManager() or accumulate listeners!
    expect(listeners.online).toBe(1);
    expect(listeners.offline).toBe(1);
    expect(listeners.visibilitychange).toBe(1);

    // Unmount must clean up all listeners
    unmount();
    expect(listeners.online).toBe(0);
    expect(listeners.offline).toBe(0);
    expect(listeners.visibilitychange).toBe(0);

    // Restore original methods
    window.addEventListener = originalWindowAdd;
    window.removeEventListener = originalWindowRemove;
    document.addEventListener = originalDocAdd;
    document.removeEventListener = originalDocRemove;
  });

  it('preserves external ownership of custom manager without disposing it on unmount', () => {
    const customManager = new ConnectivityManager();
    expect(customManager.disposed).toBe(false);

    const { unmount } = render(
      <ConnectivityProvider manager={customManager}>
        <div>Active Consumer</div>
      </ConnectivityProvider>
    );

    expect(customManager.disposed).toBe(false);

    // Provider unmounts: external manager must NOT be disposed by the provider!
    unmount();
    expect(customManager.disposed).toBe(false);

    // External owner explicitly cleans it up
    customManager.dispose();
    expect(customManager.disposed).toBe(true);
  });

  it('cleanly transitions when switching from custom manager to default internal manager', () => {
    const customManager = new ConnectivityManager();

    const DynamicProvider = () => {
      const [useCustom, setUseCustom] = useState(true);
      return (
        <div>
          <button data-testid="switch-btn" onClick={() => setUseCustom(false)}>
            Switch to Internal
          </button>
          <ConnectivityProvider manager={useCustom ? customManager : undefined}>
            <div>Dynamic Child</div>
          </ConnectivityProvider>
        </div>
      );
    };

    const { getByTestId, unmount } = render(<DynamicProvider />);
    expect(customManager.disposed).toBe(false);

    // Switch to internal
    act(() => {
      getByTestId('switch-btn').click();
    });

    // Custom manager remains owned by caller
    expect(customManager.disposed).toBe(false);

    unmount();
    customManager.dispose();
  });

  describe('Render-Safe ConnectivityManager Lifecycle & Resource Accounting', () => {
    function trackBrowserResources() {
      const listenerCounts = {
        windowOnline: 0,
        windowOffline: 0,
        docVisibility: 0,
      };

      const originalWindowAdd = window.addEventListener;
      const originalWindowRemove = window.removeEventListener;
      const originalDocAdd = document.addEventListener;
      const originalDocRemove = document.removeEventListener;

      window.addEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type === 'online') listenerCounts.windowOnline++;
        if (type === 'offline') listenerCounts.windowOffline++;
        return originalWindowAdd.call(window, type, listener, options);
      });
      window.removeEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type === 'online') listenerCounts.windowOnline--;
        if (type === 'offline') listenerCounts.windowOffline--;
        return originalWindowRemove.call(window, type, listener, options);
      });
      document.addEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type === 'visibilitychange') listenerCounts.docVisibility++;
        return originalDocAdd.call(document, type, listener, options);
      });
      document.removeEventListener = vi.fn((type: string, listener: any, options?: any) => {
        if (type === 'visibilitychange') listenerCounts.docVisibility--;
        return originalDocRemove.call(document, type, listener, options);
      });

      return {
        getCounts: () => ({ ...listenerCounts }),
        restore: () => {
          window.addEventListener = originalWindowAdd;
          window.removeEventListener = originalWindowRemove;
          document.addEventListener = originalDocAdd;
          document.removeEventListener = originalDocRemove;
        },
      };
    }

    it('1. construction of a manager alone adds no browser listeners and schedules no timer', () => {
      const tracker = trackBrowserResources();
      try {
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });

        const manager = new ConnectivityManager();
        // Manager construction is completely side-effect-free
        expect(manager.started).toBe(false);
        expect(manager.disposed).toBe(false);
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });

        // Explicit start initiates listeners and probe
        manager.start();
        expect(manager.started).toBe(true);
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        // start() is idempotent
        manager.start();
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        // dispose() removes all listeners
        manager.dispose();
        expect(manager.disposed).toBe(true);
        expect(manager.started).toBe(false);
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });

        // Disposed manager cannot start
        manager.start();
        expect(manager.started).toBe(false);
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });
      } finally {
        tracker.restore();
      }
    });

    it('2 & 3. mounting inside React.StrictMode settles with exactly one live set of listeners and no orphaned resources', async () => {
      const tracker = trackBrowserResources();
      const disposeSpy = vi.spyOn(ConnectivityManager.prototype, 'dispose');
      const startSpy = vi.spyOn(ConnectivityManager.prototype, 'start');

      try {
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });

        const { unmount } = render(
          <React.StrictMode>
            <ConnectivityProvider>
              <div data-testid="strict-child">Strict Mode Content</div>
            </ConnectivityProvider>
          </React.StrictMode>
        );

        // StrictMode mounts, unmounts (cleanup 1), and remounts (effect 2)
        // Cleanup 1 disposed the first instance
        expect(disposeSpy).toHaveBeenCalled();
        // Remount called start on the second viable instance
        expect(startSpy).toHaveBeenCalled();

        // Exactly one live set of browser listeners exists after settling
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        // Unmount clears the remaining live manager
        unmount();
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });
      } finally {
        disposeSpy.mockRestore();
        startSpy.mockRestore();
        tracker.restore();
      }
    });

    it('4. repeated parent rerenders and callback-identity changes do not increase live resource counts', () => {
      const tracker = trackBrowserResources();
      try {
        const ParentComponent = () => {
          const [count, setCount] = useState(0);
          const dynamicCallback = () => {
            console.log('rerender count', count);
          };

          return (
            <div>
              <button data-testid="rerender-btn" onClick={() => setCount((c) => c + 1)}>
                Rerender
              </button>
              <ConnectivityProvider onReconnected={dynamicCallback}>
                <span>Child {count}</span>
              </ConnectivityProvider>
            </div>
          );
        };

        const { getByTestId, unmount } = render(<ParentComponent />);

        // After mount
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        // Trigger 15 rapid rerenders with changing callback references
        for (let i = 0; i < 15; i++) {
          act(() => {
            getByTestId('rerender-btn').click();
          });
        }

        // Must still have exactly 1 set of listeners
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        unmount();
        // 5. Unmount removes every listener
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });
      } finally {
        tracker.restore();
      }
    });

    it('6. switching from internal manager to injected manager disposes internal manager at effect boundary', () => {
      const tracker = trackBrowserResources();
      const disposeSpy = vi.spyOn(ConnectivityManager.prototype, 'dispose');
      const customManager = new ConnectivityManager();

      try {
        const SwitchableParent = () => {
          const [hasCustom, setHasCustom] = useState(false);
          return (
            <div>
              <button data-testid="inject-btn" onClick={() => setHasCustom(true)}>
                Inject Custom
              </button>
              <ConnectivityProvider manager={hasCustom ? customManager : undefined}>
                <span>Provider Content</span>
              </ConnectivityProvider>
            </div>
          );
        };

        const { getByTestId, unmount } = render(<SwitchableParent />);
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        const disposeCountBefore = disposeSpy.mock.calls.length;

        // Switch to custom manager
        act(() => {
          getByTestId('inject-btn').click();
        });

        // The internally created manager was cleanly disposed at effect boundary
        expect(disposeSpy.mock.calls.length).toBeGreaterThan(disposeCountBefore);
        // Custom manager was started and is not disposed
        expect(customManager.started).toBe(true);
        expect(customManager.disposed).toBe(false);

        // Exactly one live set of listeners remains
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        // 7. Unmount preserves external ownership of custom manager
        unmount();
        expect(customManager.disposed).toBe(false);

        // Explicit caller disposal cleans up
        customManager.dispose();
        expect(customManager.disposed).toBe(true);
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });
      } finally {
        disposeSpy.mockRestore();
        tracker.restore();
      }
    });

    it('8. switching back to internal ownership creates a fresh viable manager', () => {
      const tracker = trackBrowserResources();
      const customManager = new ConnectivityManager();

      try {
        const RevertParent = () => {
          const [useCustom, setUseCustom] = useState(true);
          return (
            <div>
              <button data-testid="revert-btn" onClick={() => setUseCustom(false)}>
                Revert to Internal
              </button>
              <ConnectivityProvider manager={useCustom ? customManager : undefined}>
                <span>Revert Content</span>
              </ConnectivityProvider>
            </div>
          );
        };

        const { getByTestId, unmount } = render(<RevertParent />);
        expect(customManager.started).toBe(true);
        expect(customManager.disposed).toBe(false);
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        // Revert to internal
        act(() => {
          getByTestId('revert-btn').click();
        });

        // Custom manager is not disposed by provider
        expect(customManager.disposed).toBe(false);

        // A fresh internal manager was created and started
        expect(tracker.getCounts()).toEqual({ windowOnline: 1, windowOffline: 1, docVisibility: 1 });

        unmount();
        // Unmounting provider cleans up internal manager
        // Only custom manager's listeners would remain if customManager wasn't detached from window
        // But customManager removeBrowserEvents ran during its effect cleanup
        expect(tracker.getCounts()).toEqual({ windowOnline: 0, windowOffline: 0, docVisibility: 0 });

        customManager.dispose();
      } finally {
        tracker.restore();
      }
    });

    it('9. connectivity recovery and operator-session revalidation work after StrictMode lifecycle replay', async () => {
      const onReconnected = vi.fn();
      let capturedContext: ReturnType<typeof useConnectivity> | null = null;

      // Mock getHealth to succeed from initial render
      const healthSpy = vi.spyOn(api, 'getHealth').mockResolvedValue({
        status: 'healthy',
        provider: 'local',
        database: 'connected',
      } as any);

      const TestConsumer = () => {
        capturedContext = useConnectivity();
        return <div>{capturedContext.status}</div>;
      };

      const { unmount } = render(
        <React.StrictMode>
          <ConnectivityProvider onReconnected={onReconnected}>
            <TestConsumer />
          </ConnectivityProvider>
        </React.StrictMode>
      );

      expect(capturedContext).not.toBeNull();

      // Ensure initial health probe settles to online
      await act(async () => {
        await capturedContext!.checkNow();
      });
      expect(capturedContext!.status).toBe('online');
      expect(onReconnected).toHaveBeenCalledTimes(1);

      // Trigger offline transition via window event
      act(() => {
        window.dispatchEvent(new Event('offline'));
      });
      expect(capturedContext!.status).toBe('offline');
      expect(onReconnected).toHaveBeenCalledTimes(1);

      // Trigger online recovery via event and health check
      await act(async () => {
        window.dispatchEvent(new Event('online'));
        await capturedContext!.checkNow();
      });

      expect(capturedContext!.status).toBe('online');
      expect(onReconnected).toHaveBeenCalledTimes(2);

      healthSpy.mockRestore();
      unmount();
    });

    it('10. provider detaches from an externally owned manager without disposing it, and manager remains idle after pending probes settle', async () => {
      let deferredResolve: (val: any) => void;
      const pendingPromise = new Promise((res) => {
        deferredResolve = res;
      });
      const healthSpy = vi.spyOn(api, 'getHealth').mockImplementation(() => pendingPromise as any);

      const customManager = new ConnectivityManager({ baseIntervalMs: 3000 });
      const { unmount } = render(
        <ConnectivityProvider manager={customManager}>
          <div>Injected Consumer</div>
        </ConnectivityProvider>
      );

      expect(customManager.started).toBe(true);
      expect(customManager.disposed).toBe(false);
      expect(healthSpy).toHaveBeenCalledTimes(1);

      // Unmount provider while probe is in flight
      unmount();

      // Manager is stopped, but NOT disposed
      expect(customManager.started).toBe(false);
      expect(customManager.disposed).toBe(false);
      expect(vi.getTimerCount()).toBe(0);

      // Settle the pending probe
      await act(async () => {
        deferredResolve({ status: 'ok', environment: 'dev', version: '0.1.0', timestamp: new Date().toISOString() });
      });

      // Manager must remain completely idle: zero timers, no new probe requests
      expect(vi.getTimerCount()).toBe(0);
      act(() => {
        vi.advanceTimersByTime(30000);
      });
      expect(healthSpy).toHaveBeenCalledTimes(1); // No new probes scheduled

      healthSpy.mockRestore();
      customManager.dispose();
    });

    it('11. a deliberately restarted externally owned manager resumes normal polling and cleans up correctly when stopped again', async () => {
      const healthSpy = vi.spyOn(api, 'getHealth').mockResolvedValue({ status: 'ok' } as any);
      const customManager = new ConnectivityManager({ baseIntervalMs: 2000 });

      // Mount and unmount provider to stop manager
      const { unmount } = render(
        <ConnectivityProvider manager={customManager}>
          <div>Injected Consumer</div>
        </ConnectivityProvider>
      );
      unmount();
      expect(customManager.started).toBe(false);
      expect(customManager.disposed).toBe(false);

      // Deliberately restart manager externally
      customManager.start();
      expect(customManager.started).toBe(true);
      expect(customManager.disposed).toBe(false);

      // Timer should be scheduled for normal polling
      await act(async () => {
        await Promise.resolve();
      });
      expect(vi.getTimerCount()).toBe(1);

      // Advance timer to trigger polling probe
      await act(async () => {
        vi.advanceTimersByTime(2500);
      });
      expect(healthSpy.mock.calls.length).toBeGreaterThanOrEqual(2);

      // Stop manager again: cleans up timers and listeners completely
      customManager.stop();
      expect(customManager.started).toBe(false);
      expect(vi.getTimerCount()).toBe(0);

      healthSpy.mockRestore();
      customManager.dispose();
    });
  });
});
