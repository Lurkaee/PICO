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
});
