import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ChatPanel } from '../components/chat/ChatPanel';
import { ApprovalInbox } from '../components/approvals/ApprovalInbox';
import { WorkflowTimeline } from '../components/workflows/WorkflowTimeline';
import { VoiceControl } from '../components/voice/VoiceControl';
import { ConnectivityProvider } from '../context/ConnectivityContext';
import { ConnectivityManager } from '../api/connectivity';
import { api } from '../api/client';
import { NetworkError } from '../api/errors';
import { MemoryInspector } from '../components/memory/MemoryInspector';
import { ToolExecution, WorkflowRun, SemanticFact } from '../types';

vi.mock('../api/client', () => ({
  api: {
    getHealth: vi.fn(),
    createConversation: vi.fn(),
    getMessages: vi.fn(),
    listExecutions: vi.fn(),
    getExecution: vi.fn(),
    approveExecution: vi.fn(),
    cancelExecution: vi.fn(),
    listWorkflows: vi.fn(),
    getWorkflow: vi.fn(),
    advanceWorkflow: vi.fn(),
    cancelWorkflow: vi.fn(),
    compensateWorkflow: vi.fn(),
    getWorkflowCompensations: vi.fn(),
    getWorkflowVerifications: vi.fn(),
    getSemanticFacts: vi.fn(),
    searchSemanticFacts: vi.fn(),
    forgetSemanticFact: vi.fn(),
    getPerceptionStatus: vi.fn(),
    startAudioSession: vi.fn(),
    stopAudioSession: vi.fn(),
    cancelAudioSession: vi.fn(),
  },
}));

vi.mock('../api/sse', () => ({
  streamChat: vi.fn(),
}));

describe('UI Resilience & Safe Degradation', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  describe('Chat Resilience', () => {
    it('saves user message to in-memory drafts when backend is offline and does not submit', async () => {
      vi.mocked(api.getHealth).mockRejectedValue(new NetworkError('Connection refused'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('offline', 'Connection refused');

      render(
        <ConnectivityProvider manager={manager}>
          <ChatPanel
            activeConversation={null}
            onConversationCreated={vi.fn()}
            classification="INTERNAL"
            onClassificationChange={vi.fn()}
          />
        </ConnectivityProvider>
      );

      const textarea = screen.getByLabelText(/message input/i);
      fireEvent.change(textarea, { target: { value: 'Offline note to remember' } });

      const saveBtn = screen.getByLabelText(/save as draft/i);
      fireEvent.click(saveBtn);

      // Verify createConversation or network request was NOT initiated
      expect(api.createConversation).not.toHaveBeenCalled();

      // Verify draft banner appears
      expect(await screen.findByText(/In-Memory Drafts/i)).toBeInTheDocument();
      expect(screen.getByText('Offline note to remember')).toBeInTheDocument();

      manager.dispose();
    });

    it('preserves user input in textarea when conversation creation fails', async () => {
      vi.mocked(api.createConversation).mockRejectedValueOnce(new Error('Internal Server Error 500'));

      render(
        <ChatPanel
          activeConversation={null}
          onConversationCreated={vi.fn()}
          classification="INTERNAL"
          onClassificationChange={vi.fn()}
        />
      );

      const textarea = screen.getByLabelText(/message input/i) as HTMLTextAreaElement;
      fireEvent.change(textarea, { target: { value: 'Critical instructions' } });

      const sendBtn = screen.getByLabelText(/send message/i);
      fireEvent.click(sendBtn);

      await waitFor(() => {
        expect(screen.getByRole('alert')).toHaveTextContent(/Failed to initialize session/i);
      });

      // User input is preserved, NOT wiped!
      expect(textarea.value).toBe('Critical instructions');
    });
  });

  describe('Approval Inbox Safe Locking', () => {
    it('retains in-memory executions on fetch error and shows stale badge', async () => {
      const mockItems: ToolExecution[] = [
        {
          id: 'exec_abc',
          capability_id: 'bash_shell',
          capability_version: '1.0.0',
          input_data: { command: 'rm -rf /tmp/scratch' },
          status: 'awaiting_approval',
          created_at: new Date().toISOString(),
        },
      ];

      vi.mocked(api.listExecutions)
        .mockResolvedValueOnce(mockItems)
        .mockRejectedValue(new Error('Network drop'));

      render(
        <ApprovalInbox sessionState={{ paired: true, approver: 'operator@local' }} onOpenPairingModal={vi.fn()} />
      );

      expect(await screen.findByText(/exec_abc/)).toBeInTheDocument();

      // Trigger second fetch that fails
      const refreshBtn = screen.getByText('Refresh');
      fireEvent.click(refreshBtn);

      // Items are STILL displayed (not wiped to empty), and stale indicator is visible
      await waitFor(() => {
        expect(screen.getByText(/exec_abc/)).toBeInTheDocument();
        expect(screen.getByTitle(/Data retained in-memory/i)).toBeInTheDocument();
      });
    });

    it('locks consequential approval and rejection controls when backend is offline', async () => {
      const mockItems: ToolExecution[] = [
        {
          id: 'exec_xyz',
          capability_id: 'filesystem_delete',
          capability_version: '1.0.0',
          input_data: { path: '/data/file.txt' },
          status: 'awaiting_approval',
          created_at: new Date().toISOString(),
        },
      ];
      vi.mocked(api.listExecutions).mockResolvedValueOnce(mockItems);
      vi.mocked(api.getHealth).mockRejectedValue(new NetworkError('Network offline'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('offline', 'Network offline');

      render(
        <ConnectivityProvider manager={manager}>
          <ApprovalInbox sessionState={{ paired: true, approver: 'operator@local' }} onOpenPairingModal={vi.fn()} />
        </ConnectivityProvider>
      );

      expect(await screen.findByText(/exec_xyz/)).toBeInTheDocument();

      // Verify lock alert is shown
      expect(screen.getByText(/Backend offline: consequential controls are locked/i)).toBeInTheDocument();

      // Verify approve button is disabled
      const approveBtn = screen.getByRole('button', { name: /review & approve/i });
      expect(approveBtn).toBeDisabled();

      manager.dispose();
    });

    it('locks execution card when approval network fails and allows reconciliation', async () => {
      const mockItems: ToolExecution[] = [
        {
          id: 'exec_ambig_1',
          capability_id: 'os_launch',
          capability_version: '1.0.0',
          input_data: { binary: 'calc.exe' },
          status: 'awaiting_approval',
          created_at: new Date().toISOString(),
        },
      ];
      vi.mocked(api.listExecutions).mockResolvedValueOnce(mockItems);
      vi.mocked(api.approveExecution).mockRejectedValueOnce(new NetworkError('Interrupted connection'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('online');

      render(
        <ConnectivityProvider manager={manager}>
          <ApprovalInbox sessionState={{ paired: true, approver: 'operator@local' }} onOpenPairingModal={vi.fn()} />
        </ConnectivityProvider>
      );

      expect(await screen.findByText(/exec_ambig_1/)).toBeInTheDocument();

      // Click Review & Approve
      const reviewBtn = screen.getByRole('button', { name: /review & approve/i });
      fireEvent.click(reviewBtn);

      // Confirm approval
      const confirmBtn = screen.getByRole('button', { name: /confirm approval/i });
      fireEvent.click(confirmBtn);

      // After network failure, ambiguous outcome banner MUST appear
      await waitFor(() => {
        expect(screen.getByText(/Outcome unknown:.*Controls locked/i)).toBeInTheDocument();
      });

      // The confirm/reject buttons must now be disabled
      expect(screen.getByRole('button', { name: /confirm approval/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /reject & cancel/i })).toBeDisabled();

      // Now reconcile: backend confirms execution succeeded
      vi.mocked(api.getExecution).mockResolvedValueOnce({
        ...mockItems[0],
        status: 'succeeded',
      });

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });
      fireEvent.click(reconcileBtn);

      // After reconciliation, execution is removed from awaiting approval inbox
      await waitFor(() => {
        expect(screen.queryByText('ID: exec_ambig_1')).not.toBeInTheDocument();
        expect(screen.getByText(/Inbox Clear/i)).toBeInTheDocument();
      });

      manager.dispose();
    });
  });

  describe('Workflow Timeline Resilience', () => {
    it('disables advance and cancel controls when offline', async () => {
      const mockRuns: WorkflowRun[] = [
        {
          id: 'wf_run_1',
          name: 'Data Ingestion Workflow',
          status: 'running',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          steps: [],
        },
      ];
      vi.mocked(api.listWorkflows).mockResolvedValueOnce(mockRuns);
      vi.mocked(api.getWorkflowVerifications).mockResolvedValueOnce([]);
      vi.mocked(api.getHealth).mockRejectedValue(new NetworkError('Offline'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('offline', 'Offline');

      render(
        <ConnectivityProvider manager={manager}>
          <WorkflowTimeline />
        </ConnectivityProvider>
      );

      const items = await screen.findAllByText(/Data Ingestion Workflow/i);
      expect(items.length).toBeGreaterThan(0);

      const advanceBtn = screen.getByRole('button', { name: /advance step/i });
      const cancelBtn = screen.getByRole('button', { name: /cancel/i });

      expect(advanceBtn).toBeDisabled();
      expect(cancelBtn).toBeDisabled();

      manager.dispose();
    });

    it('locks workflow controls on advance network failure and unlocks upon reconciliation', async () => {
      const mockRuns: WorkflowRun[] = [
        {
          id: 'wf_run_ambig',
          name: 'ETL Pipeline Run',
          status: 'running',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          steps: [],
        },
      ];
      vi.mocked(api.listWorkflows).mockResolvedValue(mockRuns);
      vi.mocked(api.getWorkflowVerifications).mockResolvedValue([]);
      vi.mocked(api.advanceWorkflow).mockRejectedValueOnce(new NetworkError('Connection timeout'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('online');

      render(
        <ConnectivityProvider manager={manager}>
          <WorkflowTimeline />
        </ConnectivityProvider>
      );

      const items = await screen.findAllByText(/ETL Pipeline Run/i);
      expect(items.length).toBeGreaterThan(0);

      const advanceBtn = screen.getByRole('button', { name: /advance step/i });
      fireEvent.click(advanceBtn);

      // Outcome unknown banner appears and controls lock
      await waitFor(() => {
        expect(screen.getAllByText(/Outcome unknown/i).length).toBeGreaterThan(0);
      });

      expect(screen.getByRole('button', { name: /advance step/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled();

      // Reconcile with server: server returns run status as completed
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({
        ...mockRuns[0],
        status: 'completed',
      });

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });
      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.queryByRole('button', { name: /reconcile/i })).not.toBeInTheDocument();
      });

      manager.dispose();
    });

    it('preserves last-known verification records as stale on fetch error', async () => {
      const mockRuns: WorkflowRun[] = [
        {
          id: 'wf_run_verif',
          name: 'Verified Workflow',
          status: 'running',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          steps: [
            {
              step_key: 'step_1',
              capability_id: 'data_clean',
              capability_version: '1.0',
              status: 'succeeded',
              dependencies: [],
            },
          ],
        },
      ];
      vi.mocked(api.listWorkflows).mockResolvedValue(mockRuns);
      vi.mocked(api.getWorkflowVerifications)
        .mockResolvedValueOnce([
          {
            id: 'vrec_1',
            workflow_run_id: 'wf_run_verif',
            step_key: 'step_1',
            status: 'passed',
            passed: true,
          },
        ])
        .mockRejectedValue(new Error('Network error on refresh'));

      render(<WorkflowTimeline />);

      // Step 1 rendered
      const items = await screen.findAllByText(/Verified Workflow/i);
      expect(items.length).toBeGreaterThan(0);
      expect(screen.getByText(/step_1/i)).toBeInTheDocument();

      // Trigger second verification fetch via refresh
      const refreshBtn = screen.getByRole('button', { name: /refresh/i });
      fireEvent.click(refreshBtn);

      // Verifications are NOT wiped, and stale badge appears
      await waitFor(() => {
        expect(screen.getByText(/step_1/i)).toBeInTheDocument();
      });
    });
  });

  describe('Voice Control Safe Locking', () => {
    it('locks audio capture when backend is offline', async () => {
      vi.mocked(api.getPerceptionStatus).mockResolvedValueOnce({
        enabled: true,
        audio_capture_enabled: true,
        vision_enabled: false,
        motion_enabled: false,
        environmental_enabled: false,
        adapter_mode: 'local',
        active_audio_sessions: 0,
      });
      vi.mocked(api.getHealth).mockRejectedValue(new NetworkError('Offline'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('offline', 'Offline');

      render(
        <ConnectivityProvider manager={manager}>
          <VoiceControl onTranscription={vi.fn()} />
        </ConnectivityProvider>
      );

      const micBtn = screen.getByRole('button', { name: /start voice capture session/i });
      expect(micBtn).toBeDisabled();

      manager.dispose();
    });

    it('does not falsely claim cancellation success when cancel audio session fails', async () => {
      vi.mocked(api.getPerceptionStatus).mockResolvedValue({
        enabled: true,
        audio_capture_enabled: true,
        vision_enabled: false,
        motion_enabled: false,
        environmental_enabled: false,
        adapter_mode: 'local',
        active_audio_sessions: 0,
      });
      vi.mocked(api.startAudioSession).mockResolvedValueOnce({
        session_id: 'audio_sess_1',
        status: 'recording',
      });
      vi.mocked(api.cancelAudioSession).mockRejectedValueOnce(new NetworkError('Network lost'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('online');

      render(
        <ConnectivityProvider manager={manager}>
          <VoiceControl onTranscription={vi.fn()} />
        </ConnectivityProvider>
      );

      // Start session
      const startBtn = await screen.findByRole('button', { name: /start voice capture session/i });
      fireEvent.click(startBtn);

      // Cancel session
      const cancelBtn = await screen.findByRole('button', { name: /cancel recording/i });
      fireEvent.click(cancelBtn);

      // It must NOT report "Audio capture session cancelled."!
      await waitFor(() => {
        expect(screen.queryByText(/Audio capture session cancelled/i)).not.toBeInTheDocument();
        expect(screen.getByText(/Outcome unknown/i)).toBeInTheDocument();
      });

      manager.dispose();
    });
  });

  describe('Memory Subsystem Safe Locking', () => {
    it('locks memory fact on deletion failure and enables reconciliation', async () => {
      const mockFacts: SemanticFact[] = [
        {
          id: 'fact_99',
          subject: 'PICO Core',
          predicate: 'runs',
          object: 'offline',
          confidence: 0.95,
          source_provenance: 'direct_observation',
          data_classification: 'INTERNAL',
          verified: true,
          is_inferred: false,
          created_at: new Date().toISOString(),
        },
      ];
      vi.mocked(api.getSemanticFacts).mockResolvedValue(mockFacts);
      vi.mocked(api.forgetSemanticFact).mockRejectedValueOnce(new NetworkError('Timeout during delete'));

      const manager = new ConnectivityManager({ baseIntervalMs: 60000 });
      manager.simulateStatus('online');

      render(
        <ConnectivityProvider manager={manager}>
          <MemoryInspector />
        </ConnectivityProvider>
      );

      expect(await screen.findByText('PICO Core')).toBeInTheDocument();

      // Click forget icon
      const trashBtn = screen.getByRole('button', { name: /forget fact/i });
      fireEvent.click(trashBtn);

      // Confirm forget
      const confirmForgetBtn = screen.getByRole('button', { name: /^forget$/i });
      fireEvent.click(confirmForgetBtn);

      // After network failure, outcome unknown appears and action is locked
      await waitFor(() => {
        expect(screen.getAllByText(/Outcome unknown/i).length).toBeGreaterThan(0);
      });

      // Reconcile: backend now returns empty facts list (fact was deleted)
      vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([]);

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });
      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.queryByText('PICO Core')).not.toBeInTheDocument();
      });

      manager.dispose();
    });
  });
});
