import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { WorkflowTimeline } from '../components/workflows/WorkflowTimeline';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    listWorkflows: vi.fn(),
    getWorkflowVerifications: vi.fn(),
    advanceWorkflow: vi.fn(),
    cancelWorkflow: vi.fn(),
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
});
