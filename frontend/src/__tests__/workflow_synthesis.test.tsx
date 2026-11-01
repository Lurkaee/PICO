import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ChatPanel } from '../components/chat/ChatPanel';
import { api } from '../api/client';
import { WorkflowSynthesizeResponse, WorkflowRun } from '../types';

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

    const mockProposal: WorkflowSynthesizeResponse = {
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

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(mockProposal);

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

    const mockProposal: WorkflowSynthesizeResponse = {
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

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(mockProposal);

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

    const mockProposal: WorkflowSynthesizeResponse = {
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

    vi.mocked(api.synthesizeWorkflow).mockResolvedValueOnce(mockProposal);
    vi.mocked(api.submitWorkflow).mockResolvedValueOnce(mockCreatedRun);

    render(<ChatPanel {...defaultProps} />);
    await screen.findByText(persistedUserMessage.content);

    fireEvent.click(screen.getByRole('button', { name: /plan request msg_user_persisted_1 as workflow/i }));
    await screen.findByTestId('workflow-proposal-card');

    // Click Submit Workflow
    fireEvent.click(screen.getByRole('button', { name: /submit workflow/i }));

    await waitFor(() => {
      expect(api.submitWorkflow).toHaveBeenCalledWith(mockProposal.workflow_proposal);
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
});
