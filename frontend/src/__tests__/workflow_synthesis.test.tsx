import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ChatPanel } from '../components/chat/ChatPanel';
import { api } from '../api/client';
import { WorkflowSynthesizeResponse, WorkflowRun, Conversation } from '../types';

vi.mock('../api/client', () => ({
  api: {
    getMessages: vi.fn(),
    createConversation: vi.fn(),
    getPerceptionStatus: vi.fn().mockResolvedValue({
      enabled: false,
      audio_capture_enabled: false,
    }),
    synthesizeWorkflow: vi.fn(),
    submitWorkflow: vi.fn(),
  },
}));

describe('Conversational Workflow Synthesis UI (Milestone P11.1)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const activeConv = {
    id: 'conv_synthesis_1',
    title: 'Synthesis Test Session',
    created_at: '2026-11-02T01:00:00Z',
    updated_at: '2026-11-02T01:00:00Z',
  };

  const defaultProps = {
    activeConversation: activeConv,
    onConversationCreated: vi.fn(),
    classification: 'INTERNAL' as const,
    onClassificationChange: vi.fn(),
  };

  const persistedUserMessage = {
    id: 'msg_user_persisted_1',
    conversation_id: 'conv_synthesis_1',
    role: 'user' as const,
    content: 'Open IDE, inspect project, and calculate 42*10',
    created_at: '2026-11-02T01:00:00Z',
  };

  const assistantMessage = {
    id: 'msg_asst_1',
    conversation_id: 'conv_synthesis_1',
    role: 'assistant' as const,
    content: 'I can help break that down into automated steps.',
    created_at: '2026-11-02T01:00:05Z',
  };

  it('renders "Plan as Workflow" button for persisted user messages only', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage, assistantMessage]);

    render(<ChatPanel {...defaultProps} />);

    await screen.findByText(persistedUserMessage.content);
    await screen.findByText(assistantMessage.content);

    // Button should be present for user message
    const planButtons = screen.getAllByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i });
    expect(planButtons.length).toBe(1);
    expect(planButtons[0]).toHaveTextContent(/plan as workflow/i);

    // Button should not exist for assistant message
    expect(screen.queryByRole('button', { name: /plan request msg_asst_1 as workflow/i })).not.toBeInTheDocument();
  });

  it('handles clicking "Plan as Workflow", showing loading state and rendering ready proposal preview', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage]);

    const sampleReadyProposal: WorkflowSynthesizeResponse = {
      status: 'ready',
      conversation_id: 'conv_synthesis_1',
      user_message_id: 'msg_user_persisted_1',
      workflow_proposal: {
        name: 'Calculation & Verification Workflow',
        description: 'Computes product and validates format',
        data_classification: 'INTERNAL',
        steps: [
          {
            step_key: 'calc_step',
            capability_id: 'core.calculator',
            capability_version: '1.0.0',
            dependencies: [],
            input_template: { expression: '42 * 10' },
          },
          {
            step_key: 'validate_step',
            capability_id: 'core.validate_json',
            capability_version: '1.0.0',
            dependencies: ['calc_step'],
            input_template: { raw_json: '{"result": 420}' },
          },
        ],
      },
      step_details: [
        {
          step_key: 'calc_step',
          capability_id: 'core.calculator',
          capability_version: '1.0.0',
          capability_name: 'Safe Bounded Calculator',
          capability_description: 'Evaluates arithmetic',
          dependencies: [],
          input_template: { expression: '42 * 10' },
          risk_tier: 'READ_ONLY',
          consequential: false,
          requires_approval: false,
        },
        {
          step_key: 'validate_step',
          capability_id: 'core.validate_json',
          capability_version: '1.0.0',
          capability_name: 'JSON Validator',
          capability_description: 'Validates JSON syntax',
          dependencies: ['calc_step'],
          input_template: { raw_json: '{"result": 420}' },
          risk_tier: 'READ_ONLY',
          consequential: false,
          requires_approval: false,
        },
      ],
      explanation: 'Decomposed into calculation followed by validation.',
      effective_classification: 'INTERNAL',
      selected_profile: 'local-small',
      provider: 'ollama',
      model_name: 'qwen3.5:2b',
    };

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(sampleReadyProposal);

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    const planBtn = screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i });
    fireEvent.click(planBtn);

    expect(api.synthesizeWorkflow).toHaveBeenCalledWith({
      conversation_id: 'conv_synthesis_1',
      user_message_id: 'msg_user_persisted_1',
    });

    // Verify preview card renders
    await screen.findByTestId('workflow-proposal-card');
    expect(screen.getByText('Calculation & Verification Workflow')).toBeInTheDocument();
    expect(screen.getByText(/Decomposed into calculation followed by validation/i)).toBeInTheDocument();
    expect(screen.getByText(/1\. calc_step/i)).toBeInTheDocument();
    expect(screen.getByText(/2\. validate_step/i)).toBeInTheDocument();
    expect(screen.getByText(/Depends on: calc_step/i)).toBeInTheDocument();

    // Verify action buttons exist
    expect(screen.getByRole('button', { name: 'Discard' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /submit workflow/i })).toBeInTheDocument();
  });

  it('discards proposal preview without submitting anything', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage]);

    const sampleReadyProposal: WorkflowSynthesizeResponse = {
      status: 'ready',
      conversation_id: 'conv_synthesis_1',
      user_message_id: 'msg_user_persisted_1',
      workflow_proposal: {
        name: 'Discardable Workflow',
        steps: [
          {
            step_key: 'step_1',
            capability_id: 'core.calculator',
            dependencies: [],
            input_template: { expression: '1+1' },
          },
        ],
      },
      step_details: [
        {
          step_key: 'step_1',
          capability_id: 'core.calculator',
          capability_version: '1.0.0',
          capability_name: 'Calculator',
          capability_description: 'Math',
          dependencies: [],
          input_template: {},
          risk_tier: 'LOW',
          consequential: false,
          requires_approval: false,
        },
      ],
      effective_classification: 'PUBLIC',
      selected_profile: 'local-small',
      provider: 'ollama',
      model_name: 'qwen3.5:2b',
    };

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(sampleReadyProposal);

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByTestId('workflow-proposal-card');

    // Click Discard
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));

    // Preview card is removed
    await waitFor(() => {
      expect(screen.queryByTestId('workflow-proposal-card')).not.toBeInTheDocument();
    });
    expect(api.submitWorkflow).not.toHaveBeenCalled();
  });

  it('submits proposed workflow to existing /api/workflows and presents explicit execution guidance', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage]);

    const sampleReadyProposal: WorkflowSynthesizeResponse = {
      status: 'ready',
      conversation_id: 'conv_synthesis_1',
      user_message_id: 'msg_user_persisted_1',
      workflow_proposal: {
        name: 'Confirmed Submittable Workflow',
        data_classification: 'INTERNAL',
        steps: [
          {
            step_key: 'step_1',
            capability_id: 'core.calculator',
            dependencies: [],
            input_template: { expression: '1+1' },
          },
        ],
      },
      step_details: [
        {
          step_key: 'step_1',
          capability_id: 'core.calculator',
          capability_version: '1.0.0',
          capability_name: 'Calculator',
          capability_description: 'Math',
          dependencies: [],
          input_template: {},
          risk_tier: 'LOW',
          consequential: false,
          requires_approval: false,
        },
      ],
      effective_classification: 'INTERNAL',
      selected_profile: 'local-small',
      provider: 'ollama',
      model_name: 'qwen3.5:2b',
    };

    const mockCreatedRun: WorkflowRun = {
      id: 'wf_run_synthesized_777',
      name: 'Confirmed Submittable Workflow',
      status: 'queued',
      steps: [],
      created_at: '2026-11-02T01:05:00Z',
      updated_at: '2026-11-02T01:05:00Z',
    };

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(sampleReadyProposal);
    vi.mocked(api.submitWorkflow).mockResolvedValueOnce(mockCreatedRun);

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByTestId('workflow-proposal-card');

    // Click Submit Workflow
    fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));

    await waitFor(() => {
      expect(api.submitWorkflow).toHaveBeenCalledWith(sampleReadyProposal.workflow_proposal);
    });

    // Check success feedback message
    await screen.findByText(/Workflow Submitted Successfully/i);
    expect(screen.getByText(/wf_run_synthesized_777/i)).toBeInTheDocument();
    expect(screen.getByText(/Execution is separate and explicit/i)).toBeInTheDocument();
  });

  it('renders clarification question card when model returns needs_clarification', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage]);

    const mockClarification: WorkflowSynthesizeResponse = {
      status: 'needs_clarification',
      conversation_id: 'conv_synthesis_1',
      user_message_id: 'msg_user_persisted_1',
      workflow_proposal: null,
      step_details: [],
      clarification_question: 'Which calculator mode should be used (standard or scientific)?',
      explanation: 'Ambiguous arithmetic request.',
      effective_classification: 'PUBLIC',
      selected_profile: 'local-small',
      provider: 'ollama',
      model_name: 'qwen3.5:2b',
    };

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(mockClarification);

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));

    await screen.findByText(/Clarification Required/i);
    expect(screen.getByText(/Which calculator mode should be used/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
  });

  it('renders refusal explanation card when model returns unsupported', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage]);

    const mockUnsupported: WorkflowSynthesizeResponse = {
      status: 'unsupported',
      conversation_id: 'conv_synthesis_1',
      user_message_id: 'msg_user_persisted_1',
      workflow_proposal: null,
      step_details: [],
      explanation: 'Arbitrary shell execution is forbidden by PICO safety rules.',
      effective_classification: 'PUBLIC',
      selected_profile: 'local-small',
      provider: 'ollama',
      model_name: 'qwen3.5:2b',
    };

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(mockUnsupported);

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));

    await screen.findByText(/Workflow Unsupported/i);
    expect(screen.getByText(/Arbitrary shell execution is forbidden by PICO safety rules/i)).toBeInTheDocument();
  });

  it('renders error alert when synthesis API fails', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockRejectedValueOnce(new Error('Benchmark qualification gate closed: disabled'));

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));

    await screen.findByText(/Benchmark qualification gate closed: disabled/i);
  });
  const sampleReadyProposal: WorkflowSynthesizeResponse = {
    status: 'ready',
    conversation_id: 'conv_synthesis_1',
    user_message_id: 'msg_user_persisted_1',
    workflow_proposal: {
      name: 'Calculated Test Workflow',
      description: 'Test description',
      data_classification: 'INTERNAL',
      steps: [
        {
          step_key: 'calc_step',
          capability_id: 'core.calculator',
          capability_version: '1.0.0',
          dependencies: [],
          input_template: { expression: '42 * 10' },
        },
      ],
    },
    step_details: [
      {
        step_key: 'calc_step',
        capability_id: 'core.calculator',
        capability_version: '1.0.0',
        capability_name: 'Safe Bounded Calculator',
        capability_description: 'Evaluates arithmetic',
        dependencies: [],
        input_template: { expression: '42 * 10' },
        risk_tier: 'READ_ONLY',
        consequential: false,
        requires_approval: false,
      },
    ],
    explanation: 'Test explanation',
    effective_classification: 'INTERNAL',
    selected_profile: 'local-small',
    provider: 'ollama',
    model_name: 'qwen3.5:2b',
  };

  it('does not apply synthesis response if user navigates to another conversation before completion', async () => {
    let resolveSynthesis: (val: any) => void = () => {};
    const deferredPromise = new Promise((resolve) => {
      resolveSynthesis = resolve;
    });

    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockReturnValueOnce(deferredPromise as any);

    const { rerender } = render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    // Trigger synthesis in conv 1
    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    expect(screen.getByText(/Planning.../i)).toBeInTheDocument();

    // Navigate to conversation 2
    const conv2: Conversation = {
      id: 'conv_other_2',
      title: 'Second Conversation',
      created_at: '2026-11-02T10:00:00Z',
      updated_at: '2026-11-02T10:00:00Z',
    };
    rerender(<ChatPanel {...defaultProps} activeConversation={conv2} />);

    // Now resolve the synthesis request from conversation 1
    await act(async () => {
      resolveSynthesis(sampleReadyProposal);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Proposal card from conversation 1 must NOT appear in conversation 2
    expect(screen.queryByText(/Calculated Test Workflow/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Planning.../i)).not.toBeInTheDocument();
  });

  it('invalidates pending synthesis when proposal is dismissed or cancelled', async () => {
    let resolveSynthesis: (val: any) => void = () => {};
    const deferredPromise = new Promise((resolve) => {
      resolveSynthesis = resolve;
    });

    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockReturnValueOnce(deferredPromise as any);

    const { unmount } = render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    expect(screen.getByText(/Planning.../i)).toBeInTheDocument();

    // Unmount/dismiss
    unmount();

    // Resolve deferred synthesis
    await act(async () => {
      resolveSynthesis(sampleReadyProposal);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Clean teardown without uncaught state update warnings
  });

  it('ensures a stale response does not replace current conversation state', async () => {
    let resolveFirst: (val: any) => void = () => {};
    const deferredFirst = new Promise((resolve) => {
      resolveFirst = resolve;
    });

    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockReturnValueOnce(deferredFirst as any);

    const { rerender } = render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    // Request 1 started in conv1
    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));

    // Switch to conv2 and back to conv1
    const conv2: Conversation = {
      id: 'conv_other_2',
      title: 'Second Conversation',
      created_at: '2026-11-02T10:00:00Z',
      updated_at: '2026-11-02T10:00:00Z',
    };
    rerender(<ChatPanel {...defaultProps} activeConversation={conv2} />);
    rerender(<ChatPanel {...defaultProps} activeConversation={activeConv} />);

    // Now resolve the old request from before the switch
    await act(async () => {
      resolveFirst(sampleReadyProposal);
      await new Promise((r) => setTimeout(r, 50));
    });

    // Stale proposal should NOT appear
    expect(screen.queryByText(/Calculated Test Workflow/i)).not.toBeInTheDocument();
  });

  it('does not apply submission success if user navigates to another conversation before completion', async () => {
    let resolveSubmit: (val: any) => void = () => {};
    const deferredSubmit = new Promise((resolve) => {
      resolveSubmit = resolve;
    });

    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockResolvedValue(sampleReadyProposal);
    vi.mocked(api.submitWorkflow).mockReturnValueOnce(deferredSubmit as any);

    const { rerender } = render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    // Plan workflow to get proposal card
    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByText(/Calculated Test Workflow/i);

    // Click Submit Workflow
    fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));
    expect(screen.getByText(/Submitting.../i)).toBeInTheDocument();

    // Navigate to conversation 2
    const conv2: Conversation = {
      id: 'conv_other_2',
      title: 'Second Conversation',
      created_at: '2026-11-02T10:00:00Z',
      updated_at: '2026-11-02T10:00:00Z',
    };
    rerender(<ChatPanel {...defaultProps} activeConversation={conv2} />);

    // Resolve submission
    await act(async () => {
      resolveSubmit({
        id: 'run_submitted_999',
        name: 'Calculated Test Workflow',
        status: 'queued',
      });
      await new Promise((r) => setTimeout(r, 50));
    });

    // Success banner from conv 1 must not appear in conv 2
    expect(screen.queryByText(/run_submitted_999/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Submitting.../i)).not.toBeInTheDocument();
  });

  it('does not apply submission failure if user navigates to another conversation before rejection', async () => {
    let rejectSubmit: (val: any) => void = () => {};
    const deferredSubmit = new Promise((_, reject) => {
      rejectSubmit = reject;
    });

    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockResolvedValue(sampleReadyProposal);
    vi.mocked(api.submitWorkflow).mockReturnValueOnce(deferredSubmit as any);

    const { rerender } = render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    // Plan workflow to get proposal card
    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByText(/Calculated Test Workflow/i);

    // Click Submit Workflow
    fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));
    expect(screen.getByText(/Submitting.../i)).toBeInTheDocument();

    // Navigate to conversation 2
    const conv2: Conversation = {
      id: 'conv_other_2',
      title: 'Second Conversation',
      created_at: '2026-11-02T10:00:00Z',
      updated_at: '2026-11-02T10:00:00Z',
    };
    rerender(<ChatPanel {...defaultProps} activeConversation={conv2} />);

    // Reject submission
    await act(async () => {
      rejectSubmit(new Error('Network drop on submission'));
      await new Promise((r) => setTimeout(r, 50));
    });

    // Error banner from conv 1 must not appear in conv 2
    expect(screen.queryByText(/Network drop on submission/i)).not.toBeInTheDocument();
  });

  it('clears previous synthesis error when a new submission begins', async () => {
    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockResolvedValue(sampleReadyProposal);
    vi.mocked(api.submitWorkflow).mockRejectedValueOnce(new Error('Initial failure'));

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByText(/Calculated Test Workflow/i);

    // First submit fails
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));
    });
    await screen.findByText(/Failed to submit workflow: Initial failure/i);

    // Mock second submit to be pending
    let resolveSecond: (val: any) => void = () => {};
    const deferredSecond = new Promise((resolve) => {
      resolveSecond = resolve;
    });
    vi.mocked(api.submitWorkflow).mockReturnValueOnce(deferredSecond as any);

    // Trigger second submission
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));
    });

    // Error should be cleared immediately when new submission starts
    expect(screen.queryByText(/Failed to submit workflow: Initial failure/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Submitting.../i)).toBeInTheDocument();

    // Resolve second submission
    await act(async () => {
      resolveSecond({
        id: 'run_recovered_100',
        name: 'Calculated Test Workflow',
        status: 'queued',
      });
    });
    await screen.findByText(/run_recovered_100/i);
  });

  it('resets submitting state properly when navigating between conversations', async () => {
    let resolveSubmit: (val: any) => void = () => {};
    const deferredSubmit = new Promise((resolve) => {
      resolveSubmit = resolve;
    });

    vi.mocked(api.getMessages).mockResolvedValue([persistedUserMessage]);
    vi.mocked(api.synthesizeWorkflow).mockResolvedValue(sampleReadyProposal);
    vi.mocked(api.submitWorkflow).mockReturnValueOnce(deferredSubmit as any);

    const { rerender } = render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByText(/Calculated Test Workflow/i);

    fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));
    expect(screen.getByText(/Submitting.../i)).toBeInTheDocument();

    // Navigate to conv 2 wrapped in act
    const conv2: Conversation = {
      id: 'conv_other_2',
      title: 'Second Conversation',
      created_at: '2026-11-02T10:00:00Z',
      updated_at: '2026-11-02T10:00:00Z',
    };
    await act(async () => {
      rerender(<ChatPanel {...defaultProps} activeConversation={conv2} />);
    });
    expect(screen.queryByText(/Submitting.../i)).not.toBeInTheDocument();

    // Resolve in-flight submission after navigation
    await act(async () => {
      resolveSubmit({ id: 'run_other_1' });
      await new Promise((r) => setTimeout(r, 50));
    });

    // Return to conv 1: prior proposal should be clean
    await act(async () => {
      rerender(<ChatPanel {...defaultProps} activeConversation={activeConv} />);
    });
    expect(screen.queryByText(/Submitting.../i)).not.toBeInTheDocument();
  });
});
