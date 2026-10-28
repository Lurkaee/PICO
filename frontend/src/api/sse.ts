/**
 * Robust Server-Sent Events (SSE) streaming reader for POST /api/chat/stream.
 *
 * Implements full specification handling:
 * - Arbitrary network fragmentation across chunk boundaries.
 * - UTF-8 character boundary preservation with streaming TextDecoder.
 * - Multiple frames in a single chunk.
 * - Incomplete final frames buffered across reader intervals.
 * - Explicit 'meta', 'token', 'done', and 'error' event routing.
 * - Clean AbortSignal propagation and resource cleanup.
 */

import { SSEDoneEvent, SSEErrorEvent, SSEMetaEvent } from '../types';

export interface SSECallbacks {
  onMeta?: (meta: SSEMetaEvent) => void;
  onToken?: (token: string) => void;
  onDone?: (done: SSEDoneEvent) => void;
  onError?: (error: SSEErrorEvent) => void;
}

export interface StreamChatPayload {
  message: string;
  conversation_id?: string | null;
  classification?: string;
  stream?: boolean;
}

export class SSEParser {
  private buffer = '';
  private currentEvent = 'message';
  private currentData: string[] = [];

  constructor(private callbacks: SSECallbacks) {}

  public feed(chunk: string): void {
    this.buffer += chunk;
    const lines = this.buffer.split(/\r\n|\r|\n/);
    // Keep the last segment in buffer if incomplete
    this.buffer = lines.pop() || '';

    for (const line of lines) {
      if (line === '') {
        // Empty line indicates event dispatch boundary
        this.dispatchEvent();
      } else if (line.startsWith(':')) {
        // Comment/heartbeat line - ignore
        continue;
      } else if (line.startsWith('event:')) {
        this.currentEvent = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        this.currentData.push(line.slice(5).trimStart());
      }
    }
  }

  public flush(): void {
    if (this.buffer.trim().length > 0) {
      if (this.buffer.startsWith('data:')) {
        this.currentData.push(this.buffer.slice(5).trimStart());
      }
      this.buffer = '';
      this.dispatchEvent();
    }
  }

  private dispatchEvent(): void {
    if (this.currentData.length === 0 && this.currentEvent === 'message') {
      return;
    }

    const payload = this.currentData.join('\n');
    this.currentData = [];
    const eventType = this.currentEvent;
    this.currentEvent = 'message';

    if (eventType === 'meta') {
      try {
        const parsed = JSON.parse(payload) as SSEMetaEvent;
        this.callbacks.onMeta?.(parsed);
      } catch {
        this.callbacks.onError?.({ error: 'Failed to parse SSE meta payload', detail: payload });
      }
    } else if (eventType === 'token') {
      try {
        const parsed = JSON.parse(payload);
        const token = typeof parsed === 'string' ? parsed : parsed.token || '';
        this.callbacks.onToken?.(token);
      } catch {
        // If not JSON, emit raw string
        this.callbacks.onToken?.(payload);
      }
    } else if (eventType === 'done') {
      try {
        const parsed = JSON.parse(payload) as SSEDoneEvent;
        this.callbacks.onDone?.(parsed);
      } catch {
        this.callbacks.onDone?.({});
      }
    } else if (eventType === 'error') {
      try {
        const parsed = JSON.parse(payload) as SSEErrorEvent;
        this.callbacks.onError?.(parsed);
      } catch {
        this.callbacks.onError?.({ error: payload });
      }
    }
  }
}

export async function streamChat(
  payload: StreamChatPayload,
  callbacks: SSECallbacks,
  signal?: AbortSignal
): Promise<void> {
  const parser = new SSEParser(callbacks);
  const response = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({
      message: payload.message,
      conversation_id: payload.conversation_id || null,
      data_classification: payload.classification || 'INTERNAL',
      stream: true,
    }),
    signal,
  });

  if (!response.ok) {
    let errorDetail = `HTTP ${response.status} ${response.statusText}`;
    try {
      const errJson = await response.json();
      errorDetail = errJson.detail || errorDetail;
    } catch {
      // response not JSON
    }
    callbacks.onError?.({ error: 'Stream connection failed', detail: errorDetail });
    return;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    callbacks.onError?.({ error: 'ReadableStream body not available' });
    return;
  }

  const decoder = new TextDecoder('utf-8');
  try {
    while (true) {
      if (signal?.aborted) {
        await reader.cancel();
        return;
      }
      const { done, value } = await reader.read();
      if (done) {
        parser.flush();
        break;
      }
      if (value) {
        const chunk = decoder.decode(value, { stream: true });
        parser.feed(chunk);
      }
    }
  } catch (err: unknown) {
    if (signal?.aborted) {
      return;
    }
    const message = err instanceof Error ? err.message : String(err);
    callbacks.onError?.({ error: 'Stream reading interrupted', detail: message });
  } finally {
    reader.releaseLock();
  }
}
