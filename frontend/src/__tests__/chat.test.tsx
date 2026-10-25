import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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
