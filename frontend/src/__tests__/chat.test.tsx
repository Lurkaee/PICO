import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ChatPanel } from '../components/chat/ChatPanel';
import { api } from '../api/client';
import * as sseModule from '../api/sse';

vi.mock('../api/client', () => ({
  api: {
    getMessages: vi.fn(),
    createConversation: vi.fn(),
    getPerceptionStatus: vi.fn().mockResolvedValue({
      enabled: false,
      audio_capture_enabled: false,
    }),
    startAudioSession: vi.fn(),
    stopAudioSession: vi.fn(),
    cancelAudioSession: vi.fn(),
    deliverAudioTranscript: vi.fn(),
    getAudioSessionStatus: vi.fn(),
  },
}));

describe('ChatPanel Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const conversation = (id = 'conv_1') => ({
    id, title: 'Test Session', created_at: '2026-10-22T10:00:00Z',
    updated_at: '2026-10-22T10:00:00Z',
  });
  const props = {
    onConversationCreated: vi.fn(), classification: 'INTERNAL' as const,
    onClassificationChange: vi.fn(),
  };

  it('discards messages from a previous conversation and preserves sorted history', async () => {
    const message = (id: string, conversation_id: string) => ({
      id, conversation_id, content: id, role: 'user' as const,
      created_at: '2026-10-22T10:00:00Z',
    });
    vi.mocked(api.getMessages).mockResolvedValueOnce([message('previous', 'conv_1')]);
    const { rerender, container } = render(<ChatPanel {...props} activeConversation={conversation()} />);
    await screen.findByText('previous');
    vi.mocked(api.getMessages).mockResolvedValueOnce([
      message('newer', 'conv_2'), message('older', 'conv_2'),
    ]);
    rerender(<ChatPanel {...props} activeConversation={conversation('conv_2')} />);
    await screen.findByText('newer');
    expect(screen.queryByText('previous')).not.toBeInTheDocument();
    expect(Array.from(container.querySelectorAll('.message-content'), (node) => node.textContent))
      .toEqual(['older', 'newer']);
  });

  it('keeps unmatched local messages for the same conversation without duplicating history', async () => {
    const persisted = {
      id: 'persisted', conversation_id: 'conv_1', content: 'Persisted message',
      role: 'user' as const, created_at: '2026-10-22T10:00:00Z',
    };
    vi.mocked(api.getMessages).mockResolvedValueOnce([persisted]);
    vi.spyOn(sseModule, 'streamChat').mockImplementation(async (_payload, callbacks) => {
      callbacks.onDone?.({});
    });
    const { rerender } = render(<ChatPanel {...props} activeConversation={conversation()} />);
    await screen.findByText('Persisted message');
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'Local message' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => expect(screen.queryByText('Stop Generation')).not.toBeInTheDocument());
    vi.mocked(api.getMessages).mockResolvedValueOnce([persisted]);
    rerender(<ChatPanel {...props} activeConversation={conversation()} />);
    await waitFor(() => expect(screen.queryByText(/Loading conversation history/)).not.toBeInTheDocument());
    expect(screen.getAllByText('Persisted message')).toHaveLength(1);
    expect(screen.getAllByText('Local message')).toHaveLength(1);
  });

  it.each([
    ['rejected', false], ['rejected', true], ['EOF', false], ['EOF', true],
  ] as const)('handles %s requests with partial tokens=%s and allows another send', async (mode, partial) => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([]);
    const stream = vi.spyOn(sseModule, 'streamChat').mockImplementation(async (_payload, callbacks) => {
      if (partial) callbacks.onToken?.('Incomplete response');
      if (mode === 'rejected') throw new Error('Network request failed');
    });
    render(<ChatPanel {...props} activeConversation={conversation()} />);
    await waitFor(() => expect(screen.queryByText(/Loading conversation history/)).not.toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'First request' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(mode === 'rejected' ? 'Network request failed' : 'interrupted');
      expect(screen.queryByText('Stop Generation')).not.toBeInTheDocument();
    });
    if (partial) expect(screen.getAllByText(/\[Response stream interrupted\]/)).toHaveLength(1);
    stream.mockImplementationOnce(async (_payload, callbacks) => {
      callbacks.onToken?.('Complete response');
      callbacks.onDone?.({});
    });
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'Second request' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await screen.findByText('Complete response');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('clears the abort controller after completion even while the request settles', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([]);
    let finish: (() => void) | undefined;
    let signal: AbortSignal | undefined;
    vi.spyOn(sseModule, 'streamChat').mockImplementation(async (_payload, callbacks, abortSignal) => {
      signal = abortSignal;
      callbacks.onDone?.({});
      await new Promise<void>((resolve) => { finish = resolve; });
    });
    const { unmount } = render(<ChatPanel {...props} activeConversation={conversation()} />);
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'Finish' } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => expect(screen.getByText('Stop Generation')).toBeInTheDocument());
    await act(async () => finish?.());
    expect(screen.queryByText('Stop Generation')).not.toBeInTheDocument();
    unmount();
    expect(signal?.aborted).toBe(false);
  });

  it('loads and displays persisted message history', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([
      {
        id: 'msg_2',
        conversation_id: 'conv_1',
        role: 'assistant',
        content: 'I am ready to assist.',
        created_at: '2026-10-22T10:00:05Z',
        routing_metadata: {
          provider: 'ollama',
          model: 'qwen3.5:2b',
          effective_classification: 'INTERNAL',
        },
      },
      {
        id: 'msg_1',
        conversation_id: 'conv_1',
        role: 'user',
        content: 'Hello PICO',
        created_at: '2026-10-22T10:00:00Z',
      },
    ]);

    render(
      <ChatPanel
        activeConversation={{
          id: 'conv_1',
          title: 'Test Session',
          created_at: '2026-10-22T10:00:00Z',
          updated_at: '2026-10-22T10:00:05Z',
        }}
        onConversationCreated={vi.fn()}
        classification="INTERNAL"
        onClassificationChange={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Hello PICO')).toBeInTheDocument();
      expect(screen.getByText('I am ready to assist.')).toBeInTheDocument();
    });

    // Check routing metadata tags
    expect(screen.getByText('qwen3.5:2b')).toBeInTheDocument();
    expect(screen.getAllByText('INTERNAL').length).toBeGreaterThan(0);
  });

  it('safely renders untrusted text without HTML injection', async () => {
    const maliciousScript = '<script>window.pwned=true</script><img src="x" onerror="alert(1)"/>';
    vi.mocked(api.getMessages).mockResolvedValueOnce([
      {
        id: 'msg_sec_1',
        conversation_id: 'conv_sec',
        role: 'user',
        content: maliciousScript,
        created_at: '2026-10-22T10:00:00Z',
      },
    ]);

    render(
      <ChatPanel
        activeConversation={{
          id: 'conv_sec',
          title: 'Security Test',
          created_at: '2026-10-22T10:00:00Z',
          updated_at: '2026-10-22T10:00:00Z',
        }}
        onConversationCreated={vi.fn()}
        classification="INTERNAL"
        onClassificationChange={vi.fn()}
      />
    );

    await waitFor(() => {
      // Must be present as plain text content, not parsed into DOM elements
      expect(screen.getByText(maliciousScript)).toBeInTheDocument();
    });

    // Verify no script tag or img tag was executed into DOM
    expect(document.querySelector('script[src="x"]')).toBeNull();
  });

  it('allows stopping an in-flight stream generation without false completion', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([]);

    // Mock streamChat to simulate an open streaming response
    let capturedSignal: AbortSignal | undefined;
    vi.spyOn(sseModule, 'streamChat').mockImplementation(async (_payload, callbacks, signal) => {
      capturedSignal = signal;
      callbacks.onMeta?.({
        conversation_id: 'conv_1',
        provider: 'ollama',
        model: 'qwen3.5:2b',
        classification: 'INTERNAL',
      });
      callbacks.onToken?.('Token1 ');
      callbacks.onToken?.('Token2 ');
      // Hold until aborted
      await new Promise<void>((resolve) => {
        signal?.addEventListener('abort', () => resolve());
      });
    });

    render(
      <ChatPanel
        activeConversation={{
          id: 'conv_1',
          title: 'Stop Test',
          created_at: '2026-10-22T10:00:00Z',
          updated_at: '2026-10-22T10:00:00Z',
        }}
        onConversationCreated={vi.fn()}
        classification="INTERNAL"
        onClassificationChange={vi.fn()}
      />
    );

    const input = screen.getByLabelText('Message input');
    fireEvent.change(input, { target: { value: 'Compute factorial 100' } });

    const sendBtn = screen.getByLabelText('Send message');
    fireEvent.click(sendBtn);

    // Verify token rendering in streaming bubble
    await waitFor(() => {
      expect(screen.getByText(/Token1 Token2/)).toBeInTheDocument();
    });

    // Click stop generation
    const stopBtn = screen.getByText('Stop Generation');
    fireEvent.click(stopBtn);

    expect(capturedSignal?.aborted).toBe(true);

    await waitFor(() => {
      expect(screen.getByText(/Generation stopped by user/)).toBeInTheDocument();
    });
  });

  it('does not duplicate messages when connection is interrupted', async () => {
    vi.mocked(api.getMessages).mockResolvedValueOnce([]);

    vi.spyOn(sseModule, 'streamChat').mockImplementation(async (_payload, callbacks) => {
      callbacks.onToken?.('Partial start ');
      callbacks.onError?.({ error: 'Connection reset by peer' });
    });

    render(
      <ChatPanel
        activeConversation={{
          id: 'conv_1',
          title: 'Error Test',
          created_at: '2026-10-22T10:00:00Z',
          updated_at: '2026-10-22T10:00:00Z',
        }}
        onConversationCreated={vi.fn()}
        classification="INTERNAL"
        onClassificationChange={vi.fn()}
      />
    );

    const input = screen.getByLabelText('Message input');
    fireEvent.change(input, { target: { value: 'Test retry' } });
    fireEvent.click(screen.getByLabelText('Send message'));

    await waitFor(() => {
      expect(screen.getByText(/Connection reset by peer/)).toBeInTheDocument();
      expect(screen.getByText(/\[Response stream interrupted\]/)).toBeInTheDocument();
    });

    // Verify user message only appears once
    const userMsgs = screen.getAllByText('Test retry');
    expect(userMsgs).toHaveLength(1);
  });

  it('guards stream completion across conversation switches and prevents stale stream cleanup pollution', async () => {
    const convA = {
      id: 'conv_a',
      title: 'Session A',
      created_at: '2026-10-22T10:00:00Z',
      updated_at: '2026-10-22T10:00:00Z',
    };
    const convB = {
      id: 'conv_b',
      title: 'Session B',
      created_at: '2026-10-22T10:00:00Z',
      updated_at: '2026-10-22T10:00:00Z',
    };

    vi.mocked(api.getMessages).mockImplementation(async (convId) => {
      if (convId === 'conv_a') {
        return [
          {
            id: 'msg_a_orig',
            conversation_id: 'conv_a',
            role: 'user',
            content: 'Original message in A',
            created_at: '2026-10-22T10:00:00Z',
          },
        ];
      }
      return [];
    });

    let resolveStreamA: (() => void) | undefined;
    let callbacksA: sseModule.SSECallbacks | undefined;
    let signalA: AbortSignal | undefined;

    let resolveStreamB: (() => void) | undefined;
    let callbacksB: sseModule.SSECallbacks | undefined;

    vi.spyOn(sseModule, 'streamChat').mockImplementation(async (payload, callbacks, signal) => {
      if (payload.conversation_id === 'conv_a') {
        callbacksA = callbacks;
        signalA = signal;
        callbacks.onMeta?.({
          conversation_id: 'conv_a',
          provider: 'ollama',
          model: 'qwen3.5:2b',
          classification: 'INTERNAL',
        });
        callbacks.onToken?.('Token from Stream A');
        return new Promise<void>((resolve) => {
          resolveStreamA = resolve;
        });
      }

      if (payload.conversation_id === 'conv_b') {
        callbacksB = callbacks;
        callbacks.onMeta?.({
          conversation_id: 'conv_b',
          provider: 'ollama',
          model: 'qwen3.5:4b',
          classification: 'INTERNAL',
        });
        callbacks.onToken?.('Token from Stream B');
        return new Promise<void>((resolve) => {
          resolveStreamB = resolve;
        });
      }
    });

    const { rerender } = render(<ChatPanel {...props} activeConversation={convA} />);
    await screen.findByText('Original message in A');

    // 1. Start streaming in conversation A
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'Prompt for A' } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // 2. Let stream accumulate content
    await screen.findByText(/Token from Stream A/);
    expect(screen.getByText('Stop Generation')).toBeInTheDocument();

    // 3. Switch to conversation B and abort A
    rerender(<ChatPanel {...props} activeConversation={convB} />);
    await waitFor(() => {
      expect(signalA?.aborted).toBe(true);
    });

    // 4. Start a new stream in B before A's cleanup completes
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'Prompt for B' } });
    fireEvent.click(screen.getByLabelText('Send message'));

    await screen.findByText(/Token from Stream B/);
    expect(screen.getByText('Stop Generation')).toBeInTheDocument();

    // 5. Resolve A's stale callbacks and cleanup while B is active
    await act(async () => {
      callbacksA?.onToken?.(' Extra stale text from A');
      callbacksA?.onMeta?.({
        conversation_id: 'conv_a',
        provider: 'ollama',
        model: 'stale-model-a',
        classification: 'INTERNAL',
      });
      callbacksA?.onError?.({ error: 'StaleErrorA', detail: 'Stale error detail A' });
      resolveStreamA?.();
    });

    // 6. Verify A's stale callbacks do not pollute B:
    // - B's streaming content remains Token from Stream B (not extra stale text from A)
    expect(screen.queryByText(/Extra stale text from A/)).not.toBeInTheDocument();
    // - B's routing model remains qwen3.5:4b (not stale-model-a)
    expect(screen.queryByText('stale-model-a')).not.toBeInTheDocument();
    // - No cancellation banner or stale errors in B
    expect(screen.queryByText(/Generation stopped/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Stale error detail A/)).not.toBeInTheDocument();
    // - B's streaming state and stop button are NOT cleared by A's finally cleanup
    expect(screen.getByText('Stop Generation')).toBeInTheDocument();

    // 7. Verify B can complete normally
    await act(async () => {
      callbacksB?.onToken?.(' - completed');
      callbacksB?.onDone?.({
        routing_metadata: {
          provider: 'ollama',
          model: 'qwen3.5:4b',
          effective_classification: 'INTERNAL',
        },
      });
      resolveStreamB?.();
    });

    await screen.findByText('Token from Stream B - completed');
    expect(screen.queryByText('Stop Generation')).not.toBeInTheDocument();
    expect(screen.queryByText(/Token from Stream A/)).not.toBeInTheDocument();

    // Verify switching back to A loads A's own correct history
    rerender(<ChatPanel {...props} activeConversation={convA} />);
    await screen.findByText('Original message in A');
    expect(screen.queryByText('Token from Stream B - completed')).not.toBeInTheDocument();
  });
it('immediately clears prior conversation messages on switch and handles failed history loading without leaking', async () => {
    const convA = conversation('conv_a');
    const convB = conversation('conv_b');

    // 1. Load messages for conversation A
    vi.mocked(api.getMessages).mockResolvedValueOnce([
      {
        id: 'msg_a1',
        conversation_id: 'conv_a',
        role: 'user',
        content: 'Confidential message in Conversation A',
        created_at: '2026-10-22T10:00:00Z',
      },
    ]);

    const { rerender } = render(<ChatPanel {...props} activeConversation={convA} />);
    await screen.findByText('Confidential message in Conversation A');

    // 2. Setup deferred/rejecting promise for B\'s history request
    let rejectConvB: ((err: any) => void) | undefined;
    const convBPromise = new Promise((_resolve, reject) => {
      rejectConvB = reject;
    });
    vi.mocked(api.getMessages).mockImplementationOnce(() => convBPromise as any);

    // Switch to conversation B
    rerender(<ChatPanel {...props} activeConversation={convB} />);

    // 3. Verify A\'s messages disappear immediately before B\'s request resolves
    expect(screen.queryByText('Confidential message in Conversation A')).not.toBeInTheDocument();
    expect(screen.getByText(/Loading conversation history/)).toBeInTheDocument();

    // 4. Reject B\'s history request
    await act(async () => {
      rejectConvB?.(new Error('Internal server error loading history'));
      await Promise.resolve();
    });

    // 5. Verify A\'s messages are still absent and history error is visible
    expect(screen.queryByText('Confidential message in Conversation A')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load history: Internal server error loading history');
  });

  it('preserves streaming bubble and controls during new-conversation creation transition', async () => {
    const newConv = conversation('conv_new');
    vi.mocked(api.createConversation).mockResolvedValueOnce(newConv);
    vi.mocked(api.getMessages).mockResolvedValueOnce([]);

    let streamCallbacks: any;
    let resolveStream: () => void = () => {};
    const streamPromise = new Promise<void>((resolve) => {
      resolveStream = resolve;
    });

    vi.spyOn(sseModule, 'streamChat').mockImplementation(async (_payload, callbacks) => {
      streamCallbacks = callbacks;
      return streamPromise;
    });

    const onConversationCreated = vi.fn();
    const { rerender } = render(
      <ChatPanel {...props} onConversationCreated={onConversationCreated} activeConversation={null} />
    );

    // Enter message and submit
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'First new session message' } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // Wait for createConversation to be called and onConversationCreated callback
    await waitFor(() => expect(onConversationCreated).toHaveBeenCalledWith(newConv));
    await waitFor(() => expect(streamCallbacks).toBeDefined());

    // Emit initial token while stream is pending
    await act(async () => {
      streamCallbacks.onToken('Initial chunk');
    });

    // Assert the live streaming bubble and Stop Generation button are visible
    expect(screen.getByText('Initial chunk')).toBeInTheDocument();
    expect(screen.getByText('Stop Generation')).toBeInTheDocument();

    // Allow the newly created conversation's history effect to run (as happens when parent sets activeConversation)
    rerender(
      <ChatPanel {...props} onConversationCreated={onConversationCreated} activeConversation={newConv} />
    );

    // Wait for history request to resolve
    await waitFor(() => expect(api.getMessages).toHaveBeenCalledWith('conv_new', 50, 0));

    // Assert live streaming bubble and Stop Generation button remain visible
    expect(screen.getByText('Initial chunk')).toBeInTheDocument();
    expect(screen.getByText('Stop Generation')).toBeInTheDocument();

    // Assert message input and send button remain disabled while streaming
    const inputEl = screen.getByLabelText('Message input') as HTMLTextAreaElement;
    const sendBtn = screen.getByLabelText('Send message') as HTMLButtonElement;
    expect(inputEl).toBeDisabled();
    expect(sendBtn).toBeDisabled();

    // Emit another token and verify live bubble updates
    await act(async () => {
      streamCallbacks.onToken(' and follow-up chunk');
    });
    expect(screen.getByText('Initial chunk and follow-up chunk')).toBeInTheDocument();

    // Complete the stream
    await act(async () => {
      streamCallbacks.onDone({
        routing_metadata: {
          provider: 'ollama',
          model: 'qwen3.5:2b',
          effective_classification: 'INTERNAL',
        },
      });
      resolveStream();
      await streamPromise;
    });

    // Verify assistant message is appended exactly once and streaming controls are cleared normally
    await waitFor(() => expect(screen.queryByText('Stop Generation')).not.toBeInTheDocument());
    expect(screen.getAllByText('Initial chunk and follow-up chunk')).toHaveLength(1);
    expect(inputEl).not.toBeDisabled();
    fireEvent.change(inputEl, { target: { value: 'Follow-up message' } });
    expect(sendBtn).not.toBeDisabled();
  });

  it('discards stale in-flight history response when switching conversation before promise resolves', async () => {
    let resolveConvA: (msgs: any[]) => void = () => {};
    const promiseConvA = new Promise<any[]>((resolve) => {
      resolveConvA = resolve;
    });

    let resolveConvB: (msgs: any[]) => void = () => {};
    const promiseConvB = new Promise<any[]>((resolve) => {
      resolveConvB = resolve;
    });

    vi.mocked(api.getMessages).mockImplementation((convId: string) => {
      if (convId === 'conv_A') return promiseConvA;
      if (convId === 'conv_B') return promiseConvB;
      return Promise.resolve([]);
    });

    const msgA = {
      id: 'msg_a_1',
      conversation_id: 'conv_A',
      content: 'Conversation A Message',
      role: 'user' as const,
      created_at: '2026-10-22T10:00:00Z',
    };
    const msgB = {
      id: 'msg_b_1',
      conversation_id: 'conv_B',
      content: 'Conversation B Message',
      role: 'user' as const,
      created_at: '2026-10-22T10:00:01Z',
    };

    // 1. Render on conversation A
    const { rerender } = render(<ChatPanel {...props} activeConversation={conversation('conv_A')} />);

    // Request for conv_A is in flight
    expect(api.getMessages).toHaveBeenCalledWith('conv_A', 50, 0);

    // 2. Switch to conversation B before A resolves
    rerender(<ChatPanel {...props} activeConversation={conversation('conv_B')} />);
    expect(api.getMessages).toHaveBeenCalledWith('conv_B', 50, 0);

    // 3. Resolve conv_B history
    await act(async () => {
      resolveConvB([msgB]);
    });
    await screen.findByText('Conversation B Message');
    expect(screen.getByText('Conversation B Message')).toBeInTheDocument();

    // 4. Resolve stale conv_A history afterwards
    await act(async () => {
      resolveConvA([msgA]);
    });

    // 5. Conv B message remains displayed, Conv A message must be completely discarded
    expect(screen.getByText('Conversation B Message')).toBeInTheDocument();
    expect(screen.queryByText('Conversation A Message')).not.toBeInTheDocument();
  });

  it('ignores delayed inactive conversation delivery refresh and preserves in-flight active conversation history', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValue({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 0,
    });

    vi.mocked(api.startAudioSession).mockResolvedValue({
      session_id: 'session_conv_A',
      status: 'active',
    });

    vi.mocked(api.stopAudioSession).mockResolvedValue({
      session_id: 'session_conv_A',
      transcription: 'Voice text for A',
      status: 'completed',
    });

    let resolveDeliveryA: (res: any) => void = () => {};
    const promiseDeliveryA = new Promise<any>((resolve) => {
      resolveDeliveryA = resolve;
    });
    vi.mocked(api.deliverAudioTranscript).mockReturnValue(promiseDeliveryA);

    let resolveConvB: (msgs: any[]) => void = () => {};
    const promiseConvB = new Promise<any[]>((resolve) => {
      resolveConvB = resolve;
    });

    const msgB = {
      id: 'msg_b_1',
      conversation_id: 'conv_B',
      content: 'Active Conversation B Message',
      role: 'user' as const,
      created_at: '2026-10-22T10:00:01Z',
    };

    vi.mocked(api.getMessages).mockImplementation((convId: string) => {
      if (convId === 'conv_A') return Promise.resolve([]);
      if (convId === 'conv_B') return promiseConvB;
      return Promise.resolve([]);
    });

    // 1. Start with conversation A
    const { rerender } = render(<ChatPanel {...props} activeConversation={conversation('conv_A')} />);
    await waitFor(() => expect(screen.queryByText(/Loading conversation history/)).not.toBeInTheDocument());

    // 2. Begin voice capture and stop/delivery while conv_A is active
    const micBtn = await screen.findByLabelText('Start voice capture session');
    fireEvent.click(micBtn);

    const stopBtn = await screen.findByLabelText('Stop recording and transcribe');
    fireEvent.click(stopBtn);

    await waitFor(() => expect(api.deliverAudioTranscript).toHaveBeenCalledWith('session_conv_A', 'conv_A'));

    // 3. Switch to conversation B while A's delivery is still pending
    rerender(<ChatPanel {...props} activeConversation={conversation('conv_B')} />);
    expect(api.getMessages).toHaveBeenCalledTimes(2);
    expect(api.getMessages).toHaveBeenLastCalledWith('conv_B', 50, 0);

    // 4. Now allow A's delayed delivery callback to complete and invoke onDelivered -> refreshHistory('conv_A')
    await act(async () => {
      resolveDeliveryA({
        session_id: 'session_conv_A',
        conversation_id: 'conv_A',
        message_id: 'msg_a_trans',
        transcription: 'Voice text for A',
        data_classification: 'INTERNAL',
        delivered: true,
        created_at: '2026-10-22T10:00:00Z',
      });
    });

    // Verify refreshHistory received A's delivered conversation ID and returned early:
    // No additional history request was started for either B or A
    expect(api.getMessages).toHaveBeenCalledTimes(2);

    // 5. Resolve conv_B history request
    await act(async () => {
      resolveConvB([msgB]);
    });

    // 6. Confirm B's original pending history loads normally, its loading state clears, and A's inactive refresh did not invalidate it
    await screen.findByText('Active Conversation B Message');
    expect(screen.getByText('Active Conversation B Message')).toBeInTheDocument();
    expect(screen.queryByText(/Loading conversation history/)).not.toBeInTheDocument();
  });

  it('triggers normal delivery history refresh when delivered conversation matches active conversation', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValue({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 0,
    });

    vi.mocked(api.startAudioSession).mockResolvedValue({
      session_id: 'session_conv_A',
      status: 'active',
    });

    vi.mocked(api.stopAudioSession).mockResolvedValue({
      session_id: 'session_conv_A',
      transcription: 'Voice text for A',
      status: 'completed',
    });

    const msgA1 = {
      id: 'msg_a_initial',
      conversation_id: 'conv_A',
      content: 'Initial message in A',
      role: 'user' as const,
      created_at: '2026-10-22T10:00:00Z',
    };

    const msgA2 = {
      id: 'msg_a_trans',
      conversation_id: 'conv_A',
      content: 'Voice text for A',
      role: 'user' as const,
      created_at: '2026-10-22T10:00:05Z',
    };

    let fetchCount = 0;
    vi.mocked(api.getMessages).mockImplementation(async (convId: string) => {
      if (convId === 'conv_A') {
        fetchCount++;
        return fetchCount === 1 ? [msgA1] : [msgA1, msgA2];
      }
      return [];
    });

    vi.mocked(api.deliverAudioTranscript).mockResolvedValue({
      session_id: 'session_conv_A',
      conversation_id: 'conv_A',
      message_id: 'msg_a_trans',
      transcription: 'Voice text for A',
      data_classification: 'INTERNAL',
      delivered: true,
      created_at: '2026-10-22T10:00:05Z',
    });

    // 1. Render ChatPanel with active conversation A
    render(<ChatPanel {...props} activeConversation={conversation('conv_A')} />);
    await screen.findByText('Initial message in A');
    expect(api.getMessages).toHaveBeenCalledTimes(1);
    expect(api.getMessages).toHaveBeenCalledWith('conv_A', 50, 0);

    // 2. Start recording and stop/deliver while conv_A remains active
    const micBtn = await screen.findByLabelText('Start voice capture session');
    fireEvent.click(micBtn);

    const stopBtn = await screen.findByLabelText('Stop recording and transcribe');
    fireEvent.click(stopBtn);

    // 3. Confirm delivery occurred and triggered refreshHistory('conv_A')
    await waitFor(() => expect(api.deliverAudioTranscript).toHaveBeenCalledWith('session_conv_A', 'conv_A'));
    await screen.findByText('Voice text for A');
    expect(api.getMessages).toHaveBeenCalledTimes(2);
    expect(api.getMessages).toHaveBeenLastCalledWith('conv_A', 50, 0);
    expect(screen.queryByText(/Loading conversation history/)).not.toBeInTheDocument();
  });

  it('does not substitute active conversation when delivery payload lacks conversation_id', async () => {
    vi.mocked(api.getPerceptionStatus).mockResolvedValue({
      enabled: true,
      audio_capture_enabled: true,
      vision_enabled: false,
      motion_enabled: false,
      environmental_enabled: false,
      adapter_mode: 'mock',
      active_audio_sessions: 0,
    });

    vi.mocked(api.startAudioSession).mockResolvedValue({
      session_id: 'session_no_conv',
      status: 'active',
    });

    vi.mocked(api.stopAudioSession).mockResolvedValue({
      session_id: 'session_no_conv',
      transcription: 'Orphan voice text',
      status: 'completed',
    });

    vi.mocked(api.deliverAudioTranscript).mockResolvedValue({
      session_id: 'session_no_conv',
      conversation_id: undefined as unknown as string,
      message_id: 'msg_orphan',
      transcription: 'Orphan voice text',
      data_classification: 'INTERNAL',
      delivered: true,
      created_at: '2026-10-22T10:00:00Z',
    });

    vi.mocked(api.getMessages).mockResolvedValue([]);

    render(<ChatPanel {...props} activeConversation={conversation('conv_A')} />);
    await waitFor(() => expect(screen.queryByText(/Loading conversation history/)).not.toBeInTheDocument());
    expect(api.getMessages).toHaveBeenCalledTimes(1);

    const micBtn = await screen.findByLabelText('Start voice capture session');
    fireEvent.click(micBtn);

    const stopBtn = await screen.findByLabelText('Stop recording and transcribe');
    fireEvent.click(stopBtn);

    await waitFor(() => expect(api.deliverAudioTranscript).toHaveBeenCalledWith('session_no_conv', 'conv_A'));

    // Wait short period to ensure onDelivered has processed
    await new Promise((r) => setTimeout(r, 50));

    // Because conversation_id is missing from delivery, refreshHistory must NOT substitute activeConversation
    // and must not trigger an additional getMessages fetch
    expect(api.getMessages).toHaveBeenCalledTimes(1);
  });
});
