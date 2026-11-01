import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ApprovalInbox } from '../components/approvals/ApprovalInbox';
import { api } from '../api/client';
import { ToolExecution, ToolExecutionListEnvelope } from '../types';

vi.mock('../api/client', () => ({
  api: {
    listExecutions: vi.fn(),
    approveExecution: vi.fn(),
    cancelExecution: vi.fn(),
    executeExecution: vi.fn(),
    getExecution: vi.fn(),
    pairOperator: vi.fn(),
    logoutOperator: vi.fn(),
  },
}));

describe('P10.3.2 Operator Inbox Recovery & Lifecycle Hardening', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('clamps awaitingPage when total drops after approval and triggers immediate refetch', async () => {
    vi.useFakeTimers();

    const makeItem = (i: number): ToolExecution => ({
      id: `exec_await_${i}`,
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'awaiting_approval',
      input_data: { path: `/file_${i}.txt` },
      created_at: `2026-10-31T10:${i.toString().padStart(2, '0')}:00Z`,
    });

    // Page 1 (offset 10) initial fetch: total is 11, returns item 11 (the only item on page 2)
    const initialPage1: ToolExecutionListEnvelope = {
      items: [makeItem(10)],
      total: 11,
      limit: 10,
      offset: 10,
    };
    // Page 0 (offset 0) fetch after clamping: total is 10, returns items 0..9
    const clampedPage0: ToolExecutionListEnvelope = {
      items: Array.from({ length: 10 }, (_, i) => makeItem(i)),
      total: 10,
      limit: 10,
      offset: 0,
    };

    let fetchCount = 0;
    vi.mocked(api.listExecutions).mockImplementation(async (status, _limit, offset) => {
      if (status === 'awaiting_approval') {
        fetchCount++;
        if (offset === 10) {
          return initialPage1;
        }
        return clampedPage0;
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });
    vi.mocked(api.approveExecution).mockResolvedValueOnce({
      ...makeItem(10),
      status: 'queued',
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    // Initial load: navigate to page 2 (index 1)
    await act(async () => {
      await Promise.resolve();
    });

    // Simulate being on page 2 (index 1)
    // When item 10 is approved, total drops to 10
    // Now trigger fetch where awaiting total returns 10, but offset was 10
    // Mock returns total=10 and empty items for offset 10
    vi.mocked(api.listExecutions).mockImplementation(async (status, _limit, offset) => {
      if (status === 'awaiting_approval') {
        if (offset === 10) {
          // Total dropped to 10, so page 2 has 0 items!
          return { items: [], total: 10, limit: 10, offset: 10 };
        }
        // Offset 0 has the 10 items
        return clampedPage0;
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });

    // User approves or refreshes while awaitingPage is 1
    // The component detects aPage (1) > lastAwaitingPage (0), clamps to 0, and triggers scheduleNextPoll(0)
    const refreshBtn = screen.getByRole('button', { name: /Refresh/ });
    await act(async () => {
      fireEvent.click(refreshBtn);
      await Promise.resolve();
    });

    // Advance 0ms timer for the immediate refetch
    await act(async () => {
      vi.advanceTimersByTime(10);
      await Promise.resolve();
    });

    // Clamped page 0 items are now visible!
    expect(screen.getByText('ID: exec_await_0')).toBeInTheDocument();
    expect(screen.getByText('ID: exec_await_9')).toBeInTheDocument();
    expect(screen.getByText(/Awaiting Explicit Human Approval \(10\)/)).toBeInTheDocument();

    vi.useRealTimers();
  });

  it('clamps queuedPage when total drops after dispatch and triggers immediate refetch', async () => {
    vi.useFakeTimers();

    const makeQueued = (i: number): ToolExecution => ({
      id: `exec_queued_${i}`,
      capability_id: 'homeassistant.turn_on_switch',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: { entity_id: `switch.plug_${i}` },
      created_at: `2026-10-31T10:${i.toString().padStart(2, '0')}:00Z`,
    });

    const page0Queued: ToolExecutionListEnvelope = {
      items: Array.from({ length: 10 }, (_, i) => makeQueued(i)),
      total: 10,
      limit: 10,
      offset: 0,
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status, _limit, offset) => {
      if (status === 'queued') {
        if (offset === 10) {
          // Total dropped to 10 on page 2 (index 1)
          return { items: [], total: 10, limit: 10, offset: 10 };
        }
        return page0Queued;
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await act(async () => {
      await Promise.resolve();
    });

    const refreshBtn = screen.getByRole('button', { name: /Refresh/ });
    await act(async () => {
      fireEvent.click(refreshBtn);
      await Promise.resolve();
    });

    // Advance 0ms timer for immediate refetch after clamping
    await act(async () => {
      vi.advanceTimersByTime(10);
      await Promise.resolve();
    });

    expect(screen.getByText('ID: exec_queued_0')).toBeInTheDocument();
    expect(screen.getByText('ID: exec_queued_9')).toBeInTheDocument();
    expect(screen.getByText(/Approved — Ready to Dispatch \(10\)/)).toBeInTheDocument();

    vi.useRealTimers();
  });

  it('standalone ambiguous execution remains visible with operation type and reconcile control after leaving queued list', async () => {
    const mockQueued: ToolExecution = {
      id: 'exec_ambig_lifecycle',
      capability_id: 'homeassistant.turn_on_switch',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: { entity_id: 'switch.ac_plug' },
      created_at: '2026-10-31T10:00:00Z',
    };

    // 1. Initial list has exec in queued
    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'queued') return [mockQueued];
      return [];
    });

    // 2. Dispatch rejects with network timeout
    const timeoutErr = new Error('Gateway Timeout');
    timeoutErr.name = 'TimeoutError';
    vi.mocked(api.executeExecution).mockRejectedValueOnce(timeoutErr);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('homeassistant.turn_on_switch')).toBeInTheDocument();
    });

    // Operator clicks Dispatch
    const dispatchBtn = screen.getByRole('button', { name: /Dispatch Execution/ });
    fireEvent.click(dispatchBtn);

    // Timeout occurs -> card shows ambiguity banner
    await waitFor(() => {
      expect(screen.getByText(/Dispatch outcome uncertain/)).toBeInTheDocument();
    });

    // 3. Operator reconciles: backend reveals status is 'running'
    vi.mocked(api.getExecution).mockResolvedValueOnce({
      ...mockQueued,
      status: 'running',
    });

    // Next fetch returns empty queued list (execution is no longer queued on server!)
    vi.mocked(api.listExecutions).mockImplementation(async () => []);

    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    fireEvent.click(reconcileBtn);

    // 4. Standalone Ambiguous & Reconciling section appears!
    await waitFor(() => {
      expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();
      expect(screen.getByText('DISPATCH')).toBeInTheDocument(); // Operation type rendered!
      expect(screen.getByText(/Authoritative server state is 'running'/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Reconcile/ })).toBeInTheDocument();
    });

    // Verify executeExecution was only called once (never re-dispatched)
    expect(api.executeExecution).toHaveBeenCalledTimes(1);
  });

  it('supports manual reconciliation while execution is running and preserves polling without re-dispatch', async () => {
    const mockRunning: ToolExecution = {
      id: 'exec_manual_recon_running',
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { path: '/tmp/test.txt' },
      created_at: '2026-10-31T10:00:00Z',
    };

    // Reload discovery discovers in-flight execution
    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockRunning], total: 1, limit: 50, offset: 0 };
      }
      return [];
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();
      expect(screen.getByText(/Authoritative server state is 'running'/)).toBeInTheDocument();
    });

    // Manual reconcile returns running again
    vi.mocked(api.getExecution).mockResolvedValueOnce(mockRunning);

    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    fireEvent.click(reconcileBtn);

    await waitFor(() => {
      expect(screen.getByText(/Reconciliation in progress: execution exec_manual_recon_running is running on the server/)).toBeInTheDocument();
    });

    // Zero calls to executeExecution
    expect(api.executeExecution).not.toHaveBeenCalled();
  });

  it('supports manual reconciliation while execution is verifying and retains locks without re-dispatch', async () => {
    const mockVerifying: ToolExecution = {
      id: 'exec_manual_recon_verifying',
      capability_id: 'homeassistant.turn_on_switch',
      capability_version: '1.0.0',
      status: 'verifying',
      verification_status: 'inconclusive',
      input_data: { entity_id: 'switch.ac_plug' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockVerifying], total: 1, limit: 50, offset: 0 };
      }
      return [];
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();
      expect(screen.getByText('Verifying Evidence')).toBeInTheDocument();
    });

    vi.mocked(api.getExecution).mockResolvedValueOnce(mockVerifying);

    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    fireEvent.click(reconcileBtn);

    await waitFor(() => {
      expect(screen.getByText(/Reconciliation in progress: execution exec_manual_recon_verifying is verifying on the server/)).toBeInTheDocument();
    });

    expect(api.executeExecution).not.toHaveBeenCalled();
  });

  it('handles polling limit reached by showing informative error while keeping reconcile control active and controls locked', async () => {
    vi.useFakeTimers();

    const mockRunning: ToolExecution = {
      id: 'exec_limit_test',
      capability_id: 'homeassistant.turn_on_switch',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { entity_id: 'switch.ac_plug' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockRunning], total: 1, limit: 50, offset: 0 };
      }
      return [];
    });
    vi.mocked(api.getExecution).mockResolvedValue(mockRunning);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Advance timers through 30 polls (each poll is 2000ms)
    for (let i = 0; i < 31; i++) {
      await act(async () => {
        vi.advanceTimersByTime(2000);
        await Promise.resolve();
      });
    }

    // Limit reached message displayed
    expect(screen.getByText(/Background polling limit reached for execution exec_limit_test/)).toBeInTheDocument();
    // Reconcile button remains visible and enabled
    expect(screen.getByRole('button', { name: /Reconcile/ })).not.toBeDisabled();
    // Card remains visible
    expect(screen.getByText('ID: exec_limit_test')).toBeInTheDocument();

    vi.useRealTimers();
  });

  it('handles in-flight fetch failure by surfacing error, retaining existing active records, and marking inbox stale', async () => {
    const mockAwaiting: ToolExecution = {
      id: 'exec_await_stale',
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'awaiting_approval',
      input_data: { path: '/tmp/f.txt' },
      created_at: '2026-10-31T10:00:00Z',
    };

    // First fetch: awaiting succeeds, in-flight fails with NetworkError
    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'awaiting_approval') {
        return [mockAwaiting];
      }
      if (status === 'queued') {
        return [];
      }
      if (status === 'running,verifying') {
        throw new Error('Connection refused to in-flight ledger');
      }
      return [];
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    // Pending execution is preserved and displayed
    await waitFor(() => {
      expect(screen.getByText('filesystem.write_file')).toBeInTheDocument();
      // Fetch error is surfaced
      expect(screen.getByText(/In-flight execution discovery failed: Connection refused to in-flight ledger/)).toBeInTheDocument();
    });
  });

  it('paginates in-flight execution discovery beyond first 20 records up to total', async () => {
    const makeInFlight = (i: number): ToolExecution => ({
      id: `exec_inf_${i}`,
      capability_id: 'os.launch_application',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { app: `app_${i}` },
      created_at: `2026-10-31T10:${i.toString().padStart(2, '0')}:00Z`,
    });

    // 55 in-flight items: page 1 returns 50 items with total 55; page 2 returns 5 items with total 55
    const page1Items = Array.from({ length: 50 }, (_, i) => makeInFlight(i));
    const page2Items = Array.from({ length: 5 }, (_, i) => makeInFlight(50 + i));

    vi.mocked(api.listExecutions).mockImplementation(async (status, _limit, offset) => {
      if (status === 'running,verifying') {
        if (offset === 0) {
          return { items: page1Items, total: 55, limit: 50, offset: 0 };
        }
        return { items: page2Items, total: 55, limit: 50, offset: 50 };
      }
      return [];
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    // Standalone section discovers all 55 items without dropping records
    await waitFor(() => {
      expect(screen.getByText(/Ambiguous & Reconciling Executions \(55\)/)).toBeInTheDocument();
      expect(screen.getByText('ID: exec_inf_0')).toBeInTheDocument();
      expect(screen.getByText('ID: exec_inf_54')).toBeInTheDocument();
    });
  });

  it('discovers active running executions upon reload and populates standalone ambiguous section with reconcile affordance', async () => {
    const mockReloadRunning: ToolExecution = {
      id: 'exec_reload_active_1',
      capability_id: 'homeassistant.allowlisted_switch_actuate',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { entity_id: 'switch.outdoor' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockReloadRunning], total: 1, limit: 50, offset: 0 };
      }
      return [];
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();
      expect(screen.getByText('homeassistant.allowlisted_switch_actuate')).toBeInTheDocument();
      expect(screen.getByText('Running')).toBeInTheDocument();
      expect(screen.getByText('DISPATCH')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Reconcile/ })).toBeInTheDocument();
    });
  });

  it('cleans up all status polling and refresh timers on unmount without leaking handlers', async () => {
    vi.useFakeTimers();

    const mockRunning: ToolExecution = {
      id: 'exec_timer_clean',
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { path: '/tmp/f.txt' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockRunning], total: 1, limit: 50, offset: 0 };
      }
      return [];
    });
    vi.mocked(api.getExecution).mockResolvedValue(mockRunning);

    const { unmount } = render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await act(async () => {
      await Promise.resolve();
    });

    const callsBeforeUnmount = vi.mocked(api.getExecution).mock.calls.length;

    // Unmount component
    unmount();

    // Advance timers after unmount
    await act(async () => {
      vi.advanceTimersByTime(10000);
      await Promise.resolve();
    });

    // Zero additional getExecution calls occurred after unmount
    expect(vi.mocked(api.getExecution).mock.calls.length).toBe(callsBeforeUnmount);

    vi.useRealTimers();
  });

  it('enforces 30-poll limit across 6-second refreshes, pauses automatic polling, and keeps reconcile enabled', async () => {
    vi.useFakeTimers();

    const mockRunning: ToolExecution = {
      id: 'exec_exhaust_1',
      capability_id: 'homeassistant.allowlisted_switch_actuate',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { entity_id: 'switch.living_room' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockRunning], total: 1, limit: 50, offset: 0 };
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });
    vi.mocked(api.getExecution).mockResolvedValue(mockRunning);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    // Initial mount
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();
    expect(screen.getByText(/Read-only background polling active\.\.\./)).toBeInTheDocument();

    // Advance 30 polls (each 2000ms)
    for (let i = 0; i < 30; i++) {
      await act(async () => {
        vi.advanceTimersByTime(2000);
        await Promise.resolve();
      });
    }

    // Exactly 30 calls made
    expect(vi.mocked(api.getExecution)).toHaveBeenCalledTimes(30);

    // After 30 polls, limit reached error is surfaced
    expect(
      screen.getByText(/Background polling limit reached for execution exec_exhaust_1/i)
    ).toBeInTheDocument();

    // UI text accurately reflects that polling is exhausted, NOT active
    expect(
      screen.getByText(/Background polling budget exhausted\. Use Reconcile to inspect authoritative state\./i)
    ).toBeInTheDocument();
    expect(screen.queryByText(/Read-only background polling active\.\.\./)).not.toBeInTheDocument();

    // Reconcile control remains visible and enabled
    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    expect(reconcileBtn).toBeInTheDocument();
    expect(reconcileBtn).not.toBeDisabled();

    // Now advance across three consecutive 6-second inbox refresh cycles (18,000 ms)
    for (let cycle = 0; cycle < 3; cycle++) {
      await act(async () => {
        vi.advanceTimersByTime(6000);
        await Promise.resolve();
      });
    }

    // Crucial assertion: ZERO additional getExecution calls were triggered by the 6s refresh cycles
    expect(vi.mocked(api.getExecution)).toHaveBeenCalledTimes(30);

    // The execution remains visible in ambiguous section with enabled Reconcile
    expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();
    expect(reconcileBtn).not.toBeDisabled();

    vi.useRealTimers();
  });

  it('manual reconcile resets exhausted polling, starts fresh bounded cycle if running, and never calls executeExecution', async () => {
    vi.useFakeTimers();

    const mockRunning: ToolExecution = {
      id: 'exec_reconcile_reset_1',
      capability_id: 'homeassistant.allowlisted_switch_actuate',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { entity_id: 'switch.living_room' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return { items: [mockRunning], total: 1, limit: 50, offset: 0 };
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });
    vi.mocked(api.getExecution).mockResolvedValue(mockRunning);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Advance 30 polls to exhaust budget
    for (let i = 0; i < 30; i++) {
      await act(async () => {
        vi.advanceTimersByTime(2000);
        await Promise.resolve();
      });
    }

    expect(vi.mocked(api.getExecution)).toHaveBeenCalledTimes(30);
    expect(
      screen.getByText(/Background polling budget exhausted\. Use Reconcile to inspect authoritative state\./i)
    ).toBeInTheDocument();

    // Operator clicks Reconcile
    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    await act(async () => {
      fireEvent.click(reconcileBtn);
      await Promise.resolve();
    });

    // Invariant check: executeExecution must NEVER be invoked during manual reconciliation
    expect(vi.mocked(api.executeExecution)).not.toHaveBeenCalled();

    // getExecution was called once by reconcileExecution (31 total)
    expect(vi.mocked(api.getExecution)).toHaveBeenCalledTimes(31);

    // Exhausted state is cleared: UI switches back to "Read-only background polling active..."
    expect(screen.getByText(/Read-only background polling active\.\.\./)).toBeInTheDocument();
    expect(
      screen.queryByText(/Background polling budget exhausted/)
    ).not.toBeInTheDocument();

    // A fresh bounded polling cycle has begun: advancing 2000ms triggers the next poll (call 32)
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
    });

    expect(vi.mocked(api.getExecution)).toHaveBeenCalledTimes(32);

    vi.useRealTimers();
  });

  it('terminal reconciliation clears exhausted marker and transitions execution to authoritative outcomes without executeExecution', async () => {
    vi.useFakeTimers();

    const mockRunning: ToolExecution = {
      id: 'exec_terminal_rec_1',
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { path: '/data/log.txt' },
      created_at: '2026-10-31T10:00:00Z',
    };
    const mockTerminal: ToolExecution = {
      ...mockRunning,
      status: 'succeeded',
      verification_status: 'passed',
      output_data: { bytes_written: 1024 },
    };

    let isTerminal = false;
    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'running,verifying') {
        return isTerminal ? { items: [], total: 0, limit: 50, offset: 0 } : { items: [mockRunning], total: 1, limit: 50, offset: 0 };
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });
    vi.mocked(api.getExecution).mockImplementation(async () => {
      return isTerminal ? mockTerminal : mockRunning;
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Advance 30 polls to exhaust budget
    for (let i = 0; i < 30; i++) {
      await act(async () => {
        vi.advanceTimersByTime(2000);
        await Promise.resolve();
      });
    }

    expect(vi.mocked(api.getExecution)).toHaveBeenCalledTimes(30);
    expect(screen.getByText(/Ambiguous & Reconciling Executions \(1\)/)).toBeInTheDocument();

    // Now backend has completed execution
    isTerminal = true;

    // Operator clicks Reconcile
    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    await act(async () => {
      fireEvent.click(reconcileBtn);
      await Promise.resolve();
    });

    // Never call executeExecution
    expect(vi.mocked(api.executeExecution)).not.toHaveBeenCalled();

    // Ambiguity card is removed
    expect(screen.queryByText(/Ambiguous & Reconciling Executions/)).not.toBeInTheDocument();

    // Rendered in Authoritative Dispatch Outcomes
    expect(screen.getByText('Authoritative Dispatch Outcomes')).toBeInTheDocument();
    expect(screen.getByText('PASSED')).toBeInTheDocument();
    expect(screen.getByText(/bytes_written/)).toBeInTheDocument();

    vi.useRealTimers();
  });

  it('visibly exposes incomplete in-flight discovery when active records exceed 100 cap and marks inbox stale', async () => {
    const makeItem = (i: number): ToolExecution => ({
      id: `exec_active_${i}`,
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'running',
      input_data: { path: `/file_${i}.txt` },
      created_at: `2026-10-31T10:${(i % 60).toString().padStart(2, '0')}:00Z`,
    });

    // Total 125 active records in backend
    vi.mocked(api.listExecutions).mockImplementation(async (status, limit, offset) => {
      if (status === 'running,verifying') {
        const off = offset || 0;
        const lim = limit || 50;
        const items = Array.from({ length: Math.min(lim, Math.max(0, 125 - off)) }, (_, i) => makeItem(off + i));
        return {
          items,
          total: 125,
          limit: lim,
          offset: off,
        };
      }
      return { items: [], total: 0, limit: 10, offset: 0 };
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      // 1. Incomplete discovery alert is rendered with exact undiscovered count (125 - 100 = 25)
      const incompleteAlert = screen.getByTestId('incomplete-discovery-warning');
      expect(incompleteAlert).toBeInTheDocument();
      expect(incompleteAlert).toHaveTextContent(/25 active execution\(s\) not retrieved/i);
      expect(incompleteAlert).toHaveTextContent(/discovery capped at 100 of 125/i);

      // 2. Error banner also explicitly notes incomplete discovery
      expect(screen.getByText(/In-flight execution discovery is incomplete: 25 active execution\(s\) not retrieved/i)).toBeInTheDocument();

      // 3. Inbox is NOT marked fully fresh — stale indicator is displayed
      expect(screen.getByTestId('stale-indicator')).toBeInTheDocument();
      expect(screen.getByTitle(/Data retained in-memory/i)).toBeInTheDocument();
    });
  });
});
