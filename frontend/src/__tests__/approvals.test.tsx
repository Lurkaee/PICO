import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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
    expect(screen.getByText(/Pairing codes must be generated outside the browser/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate Code/ })).not.toBeInTheDocument();

    // Verify raw secret keywords or tokens never appear in rendered HTML
    const domHtml = document.body.innerHTML;
    expect(domHtml).not.toContain('APPROVAL_SECRET_TOKEN');
    expect(domHtml).not.toContain('super_secret');
  });
});
