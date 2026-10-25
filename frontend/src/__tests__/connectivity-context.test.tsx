import { describe, it, expect, vi } from 'vitest';
import { render, act } from '@testing-library/react';
import { ConnectivityProvider, useConnectivity } from '../context/ConnectivityContext';
import { ConnectivityManager } from '../api/connectivity';

describe('ConnectivityContext & In-Memory Draft Outbox', () => {
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

    // Spy on probeHealth
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
});
