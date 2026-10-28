import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ApprovalInbox } from '../components/approvals/ApprovalInbox';
import { PairingModal } from '../components/operator/PairingModal';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    listExecutions: vi.fn(),
    approveExecution: vi.fn(),
    cancelExecution: vi.fn(),
    pairOperator: vi.fn(),
    logoutOperator: vi.fn(),
  },
}));

describe('ApprovalInbox & Operator Session Security', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('keeps approval controls locked and explains limitation when operator is unpaired', async () => {
    vi.mocked(api.listExecutions).mockResolvedValueOnce([
      {
        id: 'exec_wipe_1',
        capability_id: 'filesystem.apply_staged_diff',
        capability_version: '1.0.0',
        status: 'awaiting_approval',
        input_data: { diff_path: '/tmp/staged.patch', root_id: 'workspace' },
        created_at: '2026-10-22T10:00:00Z',
      },
    ]);

    const openPairing = vi.fn();
    render(<ApprovalInbox sessionState={{ paired: false }} onOpenPairingModal={openPairing} />);

    await waitFor(() => {
      expect(screen.getByText('filesystem.apply_staged_diff')).toBeInTheDocument();
      expect(screen.getByText(/Operator Session Required for Approval/)).toBeInTheDocument();
    });

    const approveBtn = screen.getByRole('button', { name: /Review & Approve/ });
    expect(approveBtn).toBeDisabled();

    // Clicking pair operator button opens pairing modal
    const pairBtn = screen.getAllByRole('button', { name: /Pair Operator/ })[0];
    fireEvent.click(pairBtn);
    expect(openPairing).toHaveBeenCalled();
  });

  it('allows approval review and explicit submission when operator session is active', async () => {
    vi.mocked(api.listExecutions).mockResolvedValueOnce([
      {
        id: 'exec_app_1',
        capability_id: 'os.launch_application',
        capability_version: '1.0.0',
        status: 'awaiting_approval',
        input_data: { application_id: 'editor', working_dir: 'C:\\PICO' },
        created_at: '2026-10-22T10:00:00Z',
      },
    ]);

    vi.mocked(api.approveExecution).mockResolvedValueOnce({
      id: 'exec_app_1',
      capability_id: 'os.launch_application',
      capability_version: '1.0.0',
      status: 'queued',
      input_data: {},
      created_at: '2026-10-22T10:00:00Z',
    });

    render(
      <ApprovalInbox
        sessionState={{ paired: true, approver: 'human_operator', csrf_token: 'csrf_123' }}
        onOpenPairingModal={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('os.launch_application')).toBeInTheDocument();
    });

    const reviewBtn = screen.getByRole('button', { name: /Review & Approve/ });
    expect(reviewBtn).not.toBeDisabled();
    fireEvent.click(reviewBtn);

    // Confirmation step rendered with note input
    expect(screen.getByText('Confirm Consequential Execution')).toBeInTheDocument();
    const noteInput = screen.getByPlaceholderText(/Optional approval notes/);
    fireEvent.change(noteInput, { target: { value: 'Approved test launch' } });

    const confirmBtn = screen.getByRole('button', { name: /Confirm Approval/ });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.approveExecution).toHaveBeenCalledWith('exec_app_1', 'Approved test launch', 'human_operator');
    });
  });

  it('verifies server secrets are never exposed in PairingModal or client state', () => {
    render(
      <PairingModal
        isOpen={true}
        onClose={vi.fn()}
        sessionState={{ paired: false }}
        onSessionUpdate={vi.fn()}
      />
    );

    expect(screen.getByText('Local Operator Session')).toBeInTheDocument();
    expect(screen.getByText('python -m pico.cli.pair')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate Code/ })).not.toBeInTheDocument();

    // Verify raw secret keywords or tokens never appear in rendered HTML
    const domHtml = document.body.innerHTML;
    expect(domHtml).not.toContain('APPROVAL_SECRET_TOKEN');
    expect(domHtml).not.toContain('super_secret');
  });

  it('handles slow polling requests (>6s) without premature invalidation or stuck loading', async () => {
    vi.useFakeTimers();
    try {
      let resolveSlowFetch: ((value: any) => void) | undefined;
      const slowPromise = new Promise((resolve) => {
        resolveSlowFetch = resolve;
      });

      vi.mocked(api.listExecutions).mockImplementationOnce(() => slowPromise as any);

      render(<ApprovalInbox sessionState={{ paired: true }} onOpenPairingModal={vi.fn()} />);

      // Request is in flight
      expect(api.listExecutions).toHaveBeenCalledTimes(1);

      // Advance timers by 6 seconds (the polling frequency)
      await act(async () => {
        vi.advanceTimersByTime(6000);
      });

      // No second request should have been initiated while first request was still in-flight
      expect(api.listExecutions).toHaveBeenCalledTimes(1);

      // Resolve the slow request
      await act(async () => {
        resolveSlowFetch?.([
          {
            id: 'exec_slow_1',
            capability_id: 'filesystem.write_file',
            capability_version: '1.0.0',
            status: 'awaiting_approval',
            input_data: { path: '/tmp/test' },
            created_at: '2026-10-22T10:00:00Z',
          },
        ]);
      });

      // Item rendered, loading state resolved
      expect(screen.getByText('filesystem.write_file')).toBeInTheDocument();

      // Next poll fires 6 seconds AFTER settlement
      vi.mocked(api.listExecutions).mockResolvedValueOnce([]);
      await act(async () => {
        vi.advanceTimersByTime(6000);
      });
      expect(api.listExecutions).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears stale fetch error upon subsequent successful background poll', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.listExecutions).mockRejectedValueOnce(new Error('Network offline or gateway timeout'));

      render(<ApprovalInbox sessionState={{ paired: true }} onOpenPairingModal={vi.fn()} />);

      await act(async () => {
        await Promise.resolve();
      });

      expect(screen.getByText('Network offline or gateway timeout')).toBeInTheDocument();

      // Next background poll succeeds
      vi.mocked(api.listExecutions).mockResolvedValueOnce([
        {
          id: 'exec_recovered_1',
          capability_id: 'os.restart_service',
          capability_version: '1.0.0',
          status: 'awaiting_approval',
          input_data: {},
          created_at: '2026-10-22T10:00:00Z',
        },
      ]);

      await act(async () => {
        vi.advanceTimersByTime(6000);
        await Promise.resolve();
      });

      // Stale fetch error is cleared and new items are shown
      expect(screen.queryByText('Network offline or gateway timeout')).not.toBeInTheDocument();
      expect(screen.getByText('os.restart_service')).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('preserves action error across subsequent successful background polls until explicit refresh', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.listExecutions).mockResolvedValue([
        {
          id: 'exec_action_err_1',
          capability_id: 'cloud.deploy_bundle',
          capability_version: '1.0.0',
          status: 'awaiting_approval',
          input_data: {},
          created_at: '2026-10-22T10:00:00Z',
        },
      ]);

      vi.mocked(api.approveExecution).mockRejectedValueOnce(new Error('Operator token expired or invalid CSRF'));

      render(
        <ApprovalInbox
          sessionState={{ paired: true, approver: 'human_operator', csrf_token: 'csrf_tok' }}
          onOpenPairingModal={vi.fn()}
        />
      );

      await act(async () => {
        await Promise.resolve();
      });

      // Review and approve
      const reviewBtn = screen.getByRole('button', { name: /Review & Approve/ });
      fireEvent.click(reviewBtn);

      const confirmBtn = screen.getByRole('button', { name: /Confirm Approval/ });
      await act(async () => {
        fireEvent.click(confirmBtn);
        await Promise.resolve();
      });

      // Action error is shown
      expect(screen.getByText('Operator token expired or invalid CSRF')).toBeInTheDocument();

      // Background poll succeeds after 6 seconds
      await act(async () => {
        vi.advanceTimersByTime(6000);
        await Promise.resolve();
      });

      // Action error must NOT be silently erased by background poll
      expect(screen.getByText('Operator token expired or invalid CSRF')).toBeInTheDocument();

      // Explicit refresh clears action error
      const refreshBtn = screen.getByRole('button', { name: /Refresh/ });
      await act(async () => {
        fireEvent.click(refreshBtn);
        await Promise.resolve();
      });

      expect(screen.queryByText('Operator token expired or invalid CSRF')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
