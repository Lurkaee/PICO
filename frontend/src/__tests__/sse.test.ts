import { describe, it, expect, vi, afterEach } from 'vitest';
import { SSEParser, streamChat } from '../api/sse';

describe('streamChat request', () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each(['PUBLIC', 'INTERNAL', 'SENSITIVE', 'SECRET', undefined])(
    'serializes selected classification %s under the backend field',
    async (classification) => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        body: new ReadableStream({ start: (controller) => controller.close() }),
      });
      vi.stubGlobal('fetch', fetchMock);
      await streamChat({ message: 'hello', classification }, {});
      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body.data_classification).toBe(classification || 'INTERNAL');
      expect(body).not.toHaveProperty('classification');
    }
  );
});

describe('SSEParser', () => {
  it('parses well-formed SSE stream with meta, token, and done events', () => {
    const onMeta = vi.fn();
    const onToken = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();

    const parser = new SSEParser({ onMeta, onToken, onDone, onError });

    const rawStream =
      'event: meta\ndata: {"conversation_id":"c1","provider":"ollama","model":"qwen3.5:2b"}\n\n' +
      'event: token\ndata: "Hello"\n\n' +
      'event: token\ndata: " world!"\n\n' +
      'event: done\ndata: {"stop_reason":"stop","total_tokens":2}\n\n';

    parser.feed(rawStream);
    parser.flush();

    expect(onMeta).toHaveBeenCalledTimes(1);
    expect(onMeta).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: 'c1', model: 'qwen3.5:2b' }));

    expect(onToken).toHaveBeenCalledTimes(2);
    expect(onToken).toHaveBeenNthCalledWith(1, 'Hello');
    expect(onToken).toHaveBeenNthCalledWith(2, ' world!');

    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ stop_reason: 'stop' }));
    expect(onError).not.toHaveBeenCalled();
  });

  it('correctly reassembles fragmented chunks split across network boundaries', () => {
    const onToken = vi.fn();
    const parser = new SSEParser({ onToken });

    // Chunk 1: partial event and data
    parser.feed('event: to');
    expect(onToken).not.toHaveBeenCalled();

    // Chunk 2: completing event name and partial data
    parser.feed('ken\ndata: "Fragm');
    expect(onToken).not.toHaveBeenCalled();

    // Chunk 3: completing data and dispatch boundary
    parser.feed('ented"\n\n');
    expect(onToken).toHaveBeenCalledTimes(1);
    expect(onToken).toHaveBeenCalledWith('Fragmented');
  });

  it('handles multiple events packed into a single chunk', () => {
    const onToken = vi.fn();
    const parser = new SSEParser({ onToken });

    parser.feed('event: token\ndata: "A"\n\nevent: token\ndata: "B"\n\nevent: token\ndata: "C"\n\n');
    expect(onToken).toHaveBeenCalledTimes(3);
    expect(onToken).toHaveBeenNthCalledWith(1, 'A');
    expect(onToken).toHaveBeenNthCalledWith(2, 'B');
    expect(onToken).toHaveBeenNthCalledWith(3, 'C');
  });

  it('ignores heartbeat and comment lines starting with colon', () => {
    const onToken = vi.fn();
    const parser = new SSEParser({ onToken });

    parser.feed(': heartbeat keep-alive\n\nevent: token\ndata: "Live"\n\n: another comment\n\n');
    expect(onToken).toHaveBeenCalledTimes(1);
    expect(onToken).toHaveBeenCalledWith('Live');
  });

  it('parses error events correctly', () => {
    const onError = vi.fn();
    const parser = new SSEParser({ onError });

    parser.feed('event: error\ndata: {"error":"ModelResidencyConflict","detail":"Only one model residency permitted"}\n\n');
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({
        error: 'ModelResidencyConflict',
        detail: 'Only one model residency permitted',
      })
    );
  });
});
