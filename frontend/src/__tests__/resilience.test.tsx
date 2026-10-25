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
import { ToolExecution, WorkflowRun } from '../types';

vi.mock('../api/client', () => ({
  api: {
    getHealth: vi.fn(),
    createConversation: vi.fn(),
    getMessages: vi.fn(),
    listExecutions: vi.fn(),
    approveExecution: vi.fn(),
    cancelExecution: vi.fn(),
    listWorkflows: vi.fn(),
    advanceWorkflow: vi.fn(),
    cancelWorkflow: vi.fn(),
    getWorkflowVerifications: vi.fn(),
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
  });
});
