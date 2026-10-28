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
});
