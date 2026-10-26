import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { WorkflowTimeline } from '../components/workflows/WorkflowTimeline';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    listWorkflows: vi.fn(),
    getWorkflow: vi.fn(),
    getWorkflowVerifications: vi.fn(),
    advanceWorkflow: vi.fn(),
    cancelWorkflow: vi.fn(),
    compensateWorkflow: vi.fn(),
    getWorkflowCompensations: vi.fn(),
  },
}));

describe('WorkflowTimeline Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders execution DAG and verification gate status truthfully', async () => {
    vi.mocked(api.listWorkflows).mockResolvedValueOnce([
      {
        id: 'wf_run_1',
        name: 'Automated Build Pipeline',
        status: 'running',
        step_order: ['step_build', 'step_test', 'step_deploy'],
        steps: {
          step_build: {
            step_key: 'step_build',
            capability_id: 'build.compile',
            capability_version: '1.0.0',
            status: 'completed',
            dependencies: [],
            verification_status: 'passed',
          },
          step_test: {
            step_key: 'step_test',
            capability_id: 'test.execute',
            capability_version: '1.0.0',
            status: 'failed',
            dependencies: ['step_build'],
            verification_status: 'failed',
            error: 'Assertion failure in unit test suite',
          },
          step_deploy: {
            step_key: 'step_deploy',
            capability_id: 'deploy.staging',
            capability_version: '1.0.0',
            status: 'blocked',
            dependencies: ['step_test'],
            verification_status: null,
          },
        },
        created_at: '2026-10-22T09:00:00Z',
        updated_at: '2026-10-22T09:05:00Z',
      },
    ]);

    vi.mocked(api.getWorkflowVerifications).mockResolvedValueOnce([
      {
        step_key: 'step_build',
        verification_status: 'passed',
        passed: true,
        stage: 'Stage A: Deterministic Evaluation',
        timestamp: '2026-10-22T09:02:00Z',
      },
    ]);

    render(<WorkflowTimeline />);

    await waitFor(() => {
      expect(screen.getAllByText('Automated Build Pipeline')[0]).toBeInTheDocument();
      expect(screen.getAllByText('step_build').length).toBeGreaterThan(0);
      expect(screen.getAllByText('step_test').length).toBeGreaterThan(0);
      expect(screen.getAllByText('step_deploy').length).toBeGreaterThan(0);
    });

    // Verification gates
    expect(screen.getByText('PASSED (Evidence Accepted)')).toBeInTheDocument();
    expect(screen.getByText('FAILED (Downstream Blocked)')).toBeInTheDocument();
    expect(screen.getAllByText('Blocked')[0]).toBeInTheDocument();
    expect(screen.getByText(/Assertion failure in unit test suite/)).toBeInTheDocument();
  });

  it('triggers advance step on user action', async () => {
    const mockWorkflow = {
      id: 'wf_run_2',
      name: 'Single Step Run',
      status: 'pending' as const,
      step_order: ['step_init'],
      steps: {
        step_init: {
          step_key: 'step_init',
          capability_id: 'system.init',
          capability_version: '1.0.0',
          status: 'ready' as const,
          dependencies: [],
          verification_status: null,
        },
      },
      created_at: '2026-10-22T09:00:00Z',
      updated_at: '2026-10-22T09:00:00Z',
    };

    vi.mocked(api.listWorkflows).mockResolvedValue([mockWorkflow]);
    vi.mocked(api.getWorkflowVerifications).mockResolvedValue([]);

    vi.mocked(api.advanceWorkflow).mockResolvedValueOnce({
      ...mockWorkflow,
      status: 'running',
    });

    render(<WorkflowTimeline />);

    await waitFor(() => {
      expect(screen.getAllByText('Single Step Run')[0]).toBeInTheDocument();
    });

    const advanceBtn = screen.getByText('Advance Step');
    fireEvent.click(advanceBtn);

    await waitFor(() => {
      expect(api.advanceWorkflow).toHaveBeenCalledWith('wf_run_2');
    });
  });

  it('renders correctly when steps are provided as an array from the real backend API', async () => {
    vi.mocked(api.listWorkflows).mockResolvedValueOnce([
      {
        id: 'wf_backend_1',
        name: 'Real Backend Array Workflow',
        status: 'succeeded',
        steps: [
          {
            id: 'step_uuid_1',
            step_key: 'stage_compile',
            capability_id: 'tool.compile',
            capability_version: '1.0.0',
            status: 'succeeded',
            dependencies: [],
            verification: {
              id: 'verif_1',
              workflow_run_id: 'wf_backend_1',
              workflow_step_id: 'step_uuid_1',
              status: 'passed',
              stage: 'Stage A: Compilation Gate',
            },
          },
        ],
        created_at: '2026-10-22T09:00:00Z',
        updated_at: '2026-10-22T09:05:00Z',
      },
    ]);

    vi.mocked(api.getWorkflowVerifications).mockResolvedValueOnce([]);

    render(<WorkflowTimeline />);

    await waitFor(() => {
      expect(screen.getAllByText('Real Backend Array Workflow')[0]).toBeInTheDocument();
      expect(screen.getByText('stage_compile')).toBeInTheDocument();
      expect(screen.getByText('PASSED (Evidence Accepted)')).toBeInTheDocument();
      expect(screen.getByText('Stage A: Compilation Gate')).toBeInTheDocument();
    });
  });

  describe('Action-Specific Workflow Reconciliation', () => {
    const baseRunningWf = {
      id: 'wf_ambig_test',
      name: 'Resilient Pipeline',
      status: 'running',
      completed_steps: 1,
      total_steps: 3,
      steps: [
        {
          step_key: 'step_1',
          capability_id: 'tool.fetch',
          capability_version: '1.0.0',
          status: 'succeeded',
          dependencies: [],
        },
        {
          step_key: 'step_2',
          capability_id: 'tool.process',
          capability_version: '1.0.0',
          status: 'ready',
          dependencies: ['step_1'],
        },
        {
          step_key: 'step_3',
          capability_id: 'tool.publish',
          capability_version: '1.0.0',
          status: 'pending',
          dependencies: ['step_2'],
        },
      ],
      created_at: '2026-10-22T09:00:00Z',
      updated_at: '2026-10-22T09:00:00Z',
    };

    it('keeps advance lock active when GET succeeds but workflow shows no progress, and unlocks when transition is confirmed', async () => {
      vi.mocked(api.listWorkflows).mockResolvedValue([baseRunningWf]);
      vi.mocked(api.getWorkflowVerifications).mockResolvedValue([]);

      const netErr = new Error('Network error during advance');
      netErr.name = 'NetworkError';
      vi.mocked(api.advanceWorkflow).mockRejectedValueOnce(netErr);

      render(<WorkflowTimeline />);

      const advanceBtn = await screen.findByRole('button', { name: /advance step/i });
      fireEvent.click(advanceBtn);

      // Outcome unknown appears and controls lock
      await waitFor(() => {
        expect(screen.getAllByText(/Outcome unknown/i).length).toBeGreaterThan(0);
      });

      expect(screen.getByRole('button', { name: /advance step/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled();

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });

      // 1. Backend GET returns identical running state with NO transition or completed_steps change
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({ ...baseRunningWf });

      fireEvent.click(reconcileBtn);

      // Inconclusive! Must remain locked
      await waitFor(() => {
        expect(screen.getByText(/Reconciliation inconclusive/i)).toBeInTheDocument();
        expect(screen.getByText(/without step transition/i)).toBeInTheDocument();
      });
      expect(screen.getByRole('button', { name: /advance step/i })).toBeDisabled();

      // 2. Backend GET returns confirmed transition: step_2 succeeded, completed_steps = 2
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({
        ...baseRunningWf,
        completed_steps: 2,
        steps: [
          baseRunningWf.steps[0],
          { ...baseRunningWf.steps[1], status: 'succeeded' },
          { ...baseRunningWf.steps[2], status: 'ready' },
        ],
      });

      fireEvent.click(reconcileBtn);

      // Successfully resolved and unlocked
      await waitFor(() => {
        expect(screen.getByText(/advancement confirmed/i)).toBeInTheDocument();
        expect(screen.queryAllByText(/Outcome unknown/i)).toHaveLength(0);
      });
      expect(screen.getByRole('button', { name: /advance step/i })).not.toBeDisabled();
    });

    it('keeps cancel lock active when workflow is still running, and unlocks when cancelled', async () => {
      vi.mocked(api.listWorkflows).mockResolvedValue([baseRunningWf]);
      vi.mocked(api.getWorkflowVerifications).mockResolvedValue([]);

      const netErr = new Error('Timeout on cancel');
      netErr.name = 'TimeoutError';
      vi.mocked(api.cancelWorkflow).mockRejectedValueOnce(netErr);

      render(<WorkflowTimeline />);

      const cancelBtn = await screen.findByRole('button', { name: /cancel/i });
      fireEvent.click(cancelBtn);

      await waitFor(() => {
        expect(screen.getAllByText(/Outcome unknown/i).length).toBeGreaterThan(0);
      });

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });

      // 1. GET returns still running
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({ ...baseRunningWf });

      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.getByText(/Reconciliation inconclusive/i)).toBeInTheDocument();
        expect(screen.getByText(/cancellation unconfirmed/i)).toBeInTheDocument();
      });
      expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled();

      // 2. GET returns cancelled
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({
        ...baseRunningWf,
        status: 'cancelled',
      });

      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.getByText(/cancellation confirmed/i)).toBeInTheDocument();
        expect(screen.queryAllByText(/Outcome unknown/i)).toHaveLength(0);
      });
    });

    it('reconciles compensation by inspecting authoritative compensation records', async () => {
      const completedWf = {
        ...baseRunningWf,
        status: 'completed',
      };
      vi.mocked(api.listWorkflows).mockResolvedValue([completedWf]);
      vi.mocked(api.getWorkflowVerifications).mockResolvedValue([]);

      const netErr = new Error('Interrupted during compensate');
      netErr.name = 'NetworkError';
      vi.mocked(api.compensateWorkflow).mockRejectedValueOnce(netErr);

      render(<WorkflowTimeline />);

      const compBtn = await screen.findByRole('button', { name: /compensate/i });
      fireEvent.click(compBtn);

      await waitFor(() => {
        expect(screen.getAllByText(/Outcome unknown/i).length).toBeGreaterThan(0);
      });

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });

      // 1. Compensation records still in 'registered' status (not executed)
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({ ...completedWf });
      vi.mocked(api.getWorkflowCompensations).mockResolvedValueOnce([
        {
          id: 'comp_1',
          workflow_run_id: completedWf.id,
          workflow_step_id: 'step_1',
          compensation_capability_id: 'tool.fetch_rollback',
          compensation_capability_version: '1.0.0',
          status: 'registered',
          input_digest: 'digest_1',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ]);

      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.getByText(/Reconciliation inconclusive/i)).toBeInTheDocument();
        expect(screen.getByText(/remain in 'registered' status/i)).toBeInTheDocument();
      });
      expect(screen.getByRole('button', { name: /compensate/i })).toBeDisabled();

      // 2. Compensation records verified as 'succeeded'
      vi.mocked(api.getWorkflow).mockResolvedValueOnce({ ...completedWf });
      vi.mocked(api.getWorkflowCompensations).mockResolvedValueOnce([
        {
          id: 'comp_1',
          workflow_run_id: completedWf.id,
          workflow_step_id: 'step_1',
          compensation_capability_id: 'tool.fetch_rollback',
          compensation_capability_version: '1.0.0',
          status: 'succeeded',
          input_digest: 'digest_1',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ]);

      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.getByText(/compensation records verified on backend/i)).toBeInTheDocument();
        expect(screen.queryAllByText(/Outcome unknown/i)).toHaveLength(0);
      });
      expect(screen.getByRole('button', { name: /compensate/i })).not.toBeDisabled();
    });

    it('keeps controls locked when reconciliation GET fails with network error', async () => {
      vi.mocked(api.listWorkflows).mockResolvedValue([baseRunningWf]);
      vi.mocked(api.getWorkflowVerifications).mockResolvedValue([]);

      const netErr = new Error('Advance network drop');
      netErr.name = 'NetworkError';
      vi.mocked(api.advanceWorkflow).mockRejectedValueOnce(netErr);

      render(<WorkflowTimeline />);

      const advanceBtn = await screen.findByRole('button', { name: /advance step/i });
      fireEvent.click(advanceBtn);

      await waitFor(() => {
        expect(screen.getAllByText(/Outcome unknown/i).length).toBeGreaterThan(0);
      });

      const reconcileBtn = screen.getByRole('button', { name: /reconcile/i });

      // GET fails
      vi.mocked(api.getWorkflow).mockRejectedValueOnce(new Error('Connection refused'));

      fireEvent.click(reconcileBtn);

      await waitFor(() => {
        expect(screen.getByText(/Reconciliation inconclusive: could not verify workflow state/i)).toBeInTheDocument();
      });
      expect(screen.getByRole('button', { name: /advance step/i })).toBeDisabled();
    });
  });
});
