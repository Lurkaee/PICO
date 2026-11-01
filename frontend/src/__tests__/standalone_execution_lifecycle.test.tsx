import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ApprovalInbox } from '../components/approvals/ApprovalInbox';
import { api } from '../api/client';
import { ToolExecution } from '../types';

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

describe('P10.3.2 Standalone Execution Lifecycle & Dispatch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('separates lifecycle: approval transitions to queued without auto-dispatching', async () => {
    const mockAwaiting: ToolExecution = {
      id: 'exec_await_1',
      capability_id: 'os.launch_application',
      capability_version: '1.0.0',
      status: 'awaiting_approval',
      input_data: { app: 'calc' },
      created_at: '2026-10-31T10:00:00Z',
    };

    const mockQueued: ToolExecution = {
      ...mockAwaiting,
      status: 'queued',
      approved_at: '2026-10-31T10:01:00Z',
    };

    let isApproved = false;
    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'awaiting_approval') return isApproved ? [] : [mockAwaiting];
      if (status === 'queued') return isApproved ? [mockQueued] : [];
      return [];
    });

    vi.mocked(api.approveExecution).mockImplementation(async () => {
      isApproved = true;
      return mockQueued;
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    // Initial state: awaiting approval
    await waitFor(() => {
      expect(screen.getByText('os.launch_application')).toBeInTheDocument();
      expect(screen.getByText(/Awaiting Explicit Human Approval/)).toBeInTheDocument();
    });

    // Review & approve
    const reviewBtn = screen.getByRole('button', { name: /Review & Approve/ });
    fireEvent.click(reviewBtn);

    const confirmBtn = screen.getByRole('button', { name: /Confirm Approval/ });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.approveExecution).toHaveBeenCalledWith('exec_await_1', expect.any(String), 'local_operator');
    });
    // Ensure approve does NOT automatically dispatch
    expect(api.executeExecution).not.toHaveBeenCalled();

    // After approval, execution is now visible in the Queued section
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Approved — Ready to Dispatch/ })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Dispatch Execution/ })).toBeInTheDocument();
    });
  });

  it('explicitly dispatches approved execution and renders terminal verification results', async () => {
    const mockQueued: ToolExecution = {
      id: 'exec_queued_1',
      capability_id: 'homeassistant.set_climate_temperature',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: { entity_id: 'climate.living_room', temperature: 21.5 },
      created_at: '2026-10-31T10:00:00Z',
      approved_at: '2026-10-31T10:02:00Z',
    };

    const mockSuccess: ToolExecution = {
      ...mockQueued,
      status: 'succeeded',
      verification_status: 'passed',
      verification_details: { verified_temperature: 21.5, verified_at: '2026-10-31T10:03:00Z' },
      output_data: { result: 'ok', current_temperature: 21.5 },
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'queued') return [mockQueued];
      return [];
    });
    vi.mocked(api.executeExecution).mockResolvedValueOnce(mockSuccess);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('homeassistant.set_climate_temperature')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: /Approved — Ready to Dispatch/ })).toBeInTheDocument();
    });

    const dispatchBtn = screen.getByRole('button', { name: /Dispatch Execution/ });
    fireEvent.click(dispatchBtn);

    await waitFor(() => {
      expect(api.executeExecution).toHaveBeenCalledWith('exec_queued_1');
    });

    // Authoritative result displayed
    await waitFor(() => {
      expect(screen.getByText('Authoritative Dispatch Outcomes')).toBeInTheDocument();
      expect(screen.getByText('Succeeded (Verified)')).toBeInTheDocument();
      expect(screen.getByText('PASSED')).toBeInTheDocument();
    });
  });

  it('truthfully renders verification_failed terminal status and error details', async () => {
    const mockQueued: ToolExecution = {
      id: 'exec_queued_2',
      capability_id: 'homeassistant.turn_on_switch',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: { entity_id: 'switch.ac_plug' },
      created_at: '2026-10-31T10:00:00Z',
    };

    const mockVerificationFailed: ToolExecution = {
      ...mockQueued,
      status: 'verification_failed',
      verification_status: 'failed',
      error_message: 'Post-actuation state did not match target state within verification window',
      output_data: { entity_id: 'switch.ac_plug', observed_state: 'off' },
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'queued') return [mockQueued];
      return [];
    });
    vi.mocked(api.executeExecution).mockResolvedValueOnce(mockVerificationFailed);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('homeassistant.turn_on_switch')).toBeInTheDocument();
    });

    const dispatchBtn = screen.getByRole('button', { name: /Dispatch Execution/ });
    fireEvent.click(dispatchBtn);

    await waitFor(() => {
      expect(api.executeExecution).toHaveBeenCalledWith('exec_queued_2');
    });

    await waitFor(() => {
      expect(screen.getByText('Verification Failed')).toBeInTheDocument();
      expect(screen.getByText(/Post-actuation state did not match target state/)).toBeInTheDocument();
      expect(screen.getByText('FAILED')).toBeInTheDocument();
    });
  });

  it('handles network ambiguity during dispatch by locking controls and reconciling with backend', async () => {
    const mockQueued: ToolExecution = {
      id: 'exec_ambig_1',
      capability_id: 'homeassistant.turn_on_test_light',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: { entity_id: 'light.test_light' },
      created_at: '2026-10-31T10:00:00Z',
    };

    vi.mocked(api.listExecutions).mockImplementation(async (status) => {
      if (status === 'queued') return [mockQueued];
      return [];
    });
    // Simulate network error during dispatch
    const netErr = new Error('Network timeout');
    netErr.name = 'TimeoutError';
    vi.mocked(api.executeExecution).mockRejectedValueOnce(netErr);

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('homeassistant.turn_on_test_light')).toBeInTheDocument();
    });

    const dispatchBtn = screen.getByRole('button', { name: /Dispatch Execution/ });
    fireEvent.click(dispatchBtn);

    // Controls locked, ambiguous banner shown
    await waitFor(() => {
      expect(screen.getByText(/Dispatch outcome uncertain/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Reconcile/ })).toBeInTheDocument();
    });

    // Simulate authoritative reconciliation: backend reveals execution is still queued (claim was unconsumed)
    vi.mocked(api.getExecution).mockResolvedValueOnce({
      ...mockQueued,
      status: 'queued',
    });

    const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
    fireEvent.click(reconcileBtn);

    await waitFor(() => {
      expect(api.getExecution).toHaveBeenCalledWith('exec_ambig_1');
    });

    // Controls unlocked after reconciliation
    await waitFor(() => {
      expect(screen.getByText(/Reconciliation confirmed: execution exec_ambig_1 is still queued/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Dispatch Execution/ })).not.toBeDisabled();
    });
  });

  it('discovers older pending executions despite >50 completed records and supports pagination', async () => {
    const olderAwaiting: ToolExecution = {
      id: 'exec_older_await_99',
      capability_id: 'filesystem.write_file',
      capability_version: '1.0.0',
      status: 'awaiting_approval',
      input_data: { path: '/var/log/app.log' },
      created_at: '2026-10-01T10:00:00Z',
    };

    const olderQueued: ToolExecution = {
      id: 'exec_older_queued_88',
      capability_id: 'os.restart_service',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: { service: 'nginx' },
      created_at: '2026-10-01T10:00:00Z',
      approved_at: '2026-10-01T10:05:00Z',
    };

    // Return status-specific paginated envelopes: older records are NOT crowded out
    vi.mocked(api.listExecutions).mockImplementation(async (status, limit, offset) => {
      if (status === 'awaiting_approval') {
        if (offset === 0) {
          return { items: [olderAwaiting], total: 55, limit: limit || 10, offset: 0 };
        }
        return { items: [], total: 55, limit: limit || 10, offset: offset || 10 };
      }
      if (status === 'queued') {
        return { items: [olderQueued], total: 25, limit: limit || 10, offset: 0 };
      }
      return { items: [], total: 0, limit: limit || 10, offset: 0 };
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    // Both older pending executions are discoverable despite >50 completed records
    await waitFor(() => {
      expect(screen.getByText('filesystem.write_file')).toBeInTheDocument();
      expect(screen.getByText('os.restart_service')).toBeInTheDocument();
      expect(screen.getByText(/Awaiting Explicit Human Approval \(55\)/)).toBeInTheDocument();
      expect(screen.getByText(/Approved — Ready to Dispatch \(25\)/)).toBeInTheDocument();
    });

    // Pagination controls are rendered
    expect(screen.getByText(/Showing 1–10 of 55/)).toBeInTheDocument();
    expect(screen.getByText(/Page 1 of 6/)).toBeInTheDocument();

    // Click Next page for awaiting approvals
    const nextBtns = screen.getAllByRole('button', { name: 'Next' });
    fireEvent.click(nextBtns[0]);

    await waitFor(() => {
      expect(api.listExecutions).toHaveBeenCalledWith('awaiting_approval', 10, 10);
    });
  });

  it('progresses ambiguous dispatch through read-only polling to terminal verification without re-dispatching', async () => {
    vi.useFakeTimers();
    try {
      const mockQueued: ToolExecution = {
        id: 'exec_in_flight_1',
        capability_id: 'homeassistant.set_climate_temperature',
        capability_version: '1.0.0',
        status: 'queued',
        input_data: { entity_id: 'climate.living_room', temperature: 22.0 },
        created_at: '2026-10-31T10:00:00Z',
      };

      vi.mocked(api.listExecutions).mockImplementation(async (status) => {
        if (status === 'queued') return [mockQueued];
        return [];
      });

      // Network error on dispatch
      const netErr = new Error('Network gateway timeout');
      netErr.name = 'TimeoutError';
      vi.mocked(api.executeExecution).mockRejectedValueOnce(netErr);

      render(
        <ApprovalInbox
          sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
          onOpenPairingModal={vi.fn()}
        />
      );

      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByText('homeassistant.set_climate_temperature')).toBeInTheDocument();

      // Dispatch execution
      const dispatchBtn = screen.getByRole('button', { name: /Dispatch Execution/ });
      await act(async () => {
        fireEvent.click(dispatchBtn);
        await Promise.resolve();
      });

      expect(screen.getByText(/Dispatch outcome uncertain/)).toBeInTheDocument();

      // Step 1: Reconcile finds execution is 'running' on backend
      vi.mocked(api.getExecution).mockResolvedValueOnce({
        ...mockQueued,
        status: 'running',
      });

      const reconcileBtn = screen.getByRole('button', { name: /Reconcile/ });
      await act(async () => {
        fireEvent.click(reconcileBtn);
        await Promise.resolve();
      });

      expect(screen.getByText(/is running on the server/)).toBeInTheDocument();

      // Step 2: Background read-only poll advances to 'verifying'
      vi.mocked(api.getExecution).mockResolvedValueOnce({
        ...mockQueued,
        status: 'verifying',
      });

      await act(async () => {
        vi.advanceTimersByTime(2000);
        await Promise.resolve();
      });

      // Step 3: Background read-only poll advances to 'succeeded' with verified evidence
      vi.mocked(api.getExecution).mockResolvedValueOnce({
        ...mockQueued,
        status: 'succeeded',
        verification_status: 'passed',
        verification_details: { verified_temperature: 22.0 },
        output_data: { status: 'temperature_set' },
      });

      await act(async () => {
        vi.advanceTimersByTime(2000);
        await Promise.resolve();
      });

      // Ambiguity cleared, authoritative result rendered with PASSED verification
      expect(screen.getByText('Authoritative Dispatch Outcomes')).toBeInTheDocument();
      expect(screen.getByText('Succeeded (Verified)')).toBeInTheDocument();

      // CRITICAL: verify api.executeExecution was NEVER called during polling/reconciliation (only the initial operator click)
      expect(api.executeExecution).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('discovers persisted in-flight executions upon reload and cleans up timers on unmount', async () => {
    vi.useFakeTimers();
    try {
      const activeRunning: ToolExecution = {
        id: 'exec_in_flight_reload',
        capability_id: 'homeassistant.turn_on_switch',
        capability_version: '1.0.0',
        status: 'running',
        input_data: { entity_id: 'switch.ac_plug' },
        created_at: '2026-10-31T10:00:00Z',
      };

      // Backend discovers active running execution
      vi.mocked(api.listExecutions).mockImplementation(async (status) => {
        if (status === 'running,verifying') {
          return { items: [activeRunning], total: 1, limit: 20, offset: 0 };
        }
        return { items: [], total: 0, limit: 10, offset: 0 };
      });

      const { unmount } = render(
        <ApprovalInbox
          sessionState={{ paired: true, approver: 'local_operator', csrf_token: 'csrf_valid' }}
          onOpenPairingModal={vi.fn()}
        />
      );

      // In-flight execution discovered on reload
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByText('homeassistant.turn_on_switch')).toBeInTheDocument();
      expect(screen.getByText('Running')).toBeInTheDocument();

      // Unmount component: must not throw or leak active timers
      unmount();
      await act(async () => {
        vi.advanceTimersByTime(10000);
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
