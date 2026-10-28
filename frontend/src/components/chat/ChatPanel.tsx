import React, { useState, useEffect, useRef } from 'react';
import { Send, Square, AlertTriangle, Shield, Cpu, RefreshCw } from 'lucide-react';
import { api } from '../../api/client';
import { streamChat } from '../../api/sse';
import { Conversation, DataClassification, Message, RoutingMetadata } from '../../types';
import { VoiceControl } from '../voice/VoiceControl';

interface ChatPanelProps {
  activeConversation: Conversation | null;
  onConversationCreated: (conversation: Conversation) => void;
  classification: DataClassification;
  onClassificationChange: (classification: DataClassification) => void;
}

export const ChatPanel: React.FC<ChatPanelProps> = ({
  activeConversation,
  onConversationCreated,
  classification,
  onClassificationChange,
}) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState('');
  const [currentRouting, setCurrentRouting] = useState<RoutingMetadata | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [isCancelled, setIsCancelled] = useState(false);

  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: 'smooth' });
  };

  // Load message history when active conversation changes
  useEffect(() => {
    if (!activeConversation) {
      setMessages([]);
      return;
    }

    let isMounted = true;
    setLoadingHistory(true);
    setStreamError(null);

    api
      .getMessages(activeConversation.id, 50, 0)
      .then((history) => {
        if (isMounted) {
          const sorted = [...(history || [])].reverse();
          setMessages((prev) => {
            if (prev.length === 0) return sorted;
            const existingIds = new Set(sorted.map((m) => m.id));
            const newLocal = prev.filter(
              (m) => m.conversation_id === activeConversation.id && !existingIds.has(m.id)
            );
            return [...sorted, ...newLocal];
          });
        }
      })
      .catch((err) => {
        if (isMounted) {
          setStreamError(`Failed to load history: ${err.message}`);
        }
      })
      .finally(() => {
        if (isMounted) setLoadingHistory(false);
      });

    return () => {
      isMounted = false;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [activeConversation]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, streamingContent]);

  const handleSendMessage = async () => {
    const trimmed = input.trim();
    if (!trimmed || streaming) return;

    setInput('');
    setStreamError(null);
    setIsCancelled(false);

    let convId = activeConversation?.id;

    // Create conversation on the fly if none is active
    if (!convId) {
      try {
        const title = trimmed.slice(0, 30) + (trimmed.length > 30 ? '...' : '');
        const newConv = await api.createConversation(title);
        onConversationCreated(newConv);
        convId = newConv.id;
      } catch (err) {
        setStreamError(err instanceof Error ? err.message : String(err));
        return;
      }
    }

    // Add user message to UI state immediately
    const userMessage: Message = {
      id: `local_user_${Date.now()}`,
      conversation_id: convId,
      role: 'user',
      content: trimmed,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMessage]);

    // Prepare streaming
    setStreaming(true);
    setStreamingContent('');
    setCurrentRouting(null);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    let accumulated = '';
    let finalRouting: RoutingMetadata | null = null;
    let completionSuccessful = false;

    try {
      await streamChat(
        {
          message: trimmed,
          conversation_id: convId,
          classification,
          stream: true,
        },
        {
          onMeta: (meta) => {
            if (meta.provider && meta.model) {
              const routing: RoutingMetadata = {
                provider: meta.provider,
                model: meta.model,
                effective_classification: meta.classification || classification,
                decision_rule: meta.decision_rule,
              };
              setCurrentRouting(routing);
              finalRouting = routing;
            }
          },
          onToken: (token) => {
            accumulated += token;
            setStreamingContent(accumulated);
          },
          onDone: (done) => {
            completionSuccessful = true;
            if (done.routing_metadata) {
              finalRouting = done.routing_metadata;
              setCurrentRouting(done.routing_metadata);
            }
            const assistantMessage: Message = {
              id: `asst_${Date.now()}`,
              conversation_id: convId!,
              role: 'assistant',
              content: accumulated,
              created_at: new Date().toISOString(),
              routing_metadata: finalRouting,
            };
            setMessages((prev) => [...prev, assistantMessage]);
            setStreamingContent('');
          },
          onError: (err) => {
            setStreamError(err.detail ? `${err.error}: ${err.detail}` : err.error);
          },
        },
        controller.signal
      );
    } catch (err) {
      if (!controller.signal.aborted) {
        setStreamError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setStreaming(false);
      abortControllerRef.current = null;
      setStreamingContent('');
      if (!completionSuccessful) {
        const cancelled = controller.signal.aborted;
        if (cancelled) {
          setIsCancelled(true);
        } else {
          setStreamError((previous) => previous || 'Response stream interrupted before completion.');
        }
        if (accumulated) {
          const partialMessage: Message = {
            id: `asst_interrupted_${Date.now()}`,
            conversation_id: convId!,
            role: 'assistant',
            content: `${accumulated}\n\n[${cancelled ? 'Generation stopped by user' : 'Response stream interrupted'}]`,
            created_at: new Date().toISOString(),
            routing_metadata: finalRouting,
          };
          setMessages((prev) => [...prev, partialMessage]);
        }
      }
    }
  };

  const handleStopGeneration = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  return (
    <div className="chat-container">
      {/* Top Conversation Subheader */}
      <div
        style={{
          padding: '12px 20px',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          backgroundColor: 'var(--bg-surface)',
          flexShrink: 0,
        }}
      >
        <div>
          <h2 style={{ fontSize: '0.95rem', fontWeight: 600 }}>
            {activeConversation ? activeConversation.title : 'New Session'}
          </h2>
          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
            {activeConversation ? `ID: ${activeConversation.id}` : 'Ephemeral session until first message'}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          {/* Classification Selection */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Shield style={{ width: 14, height: 14, color: 'var(--text-muted)' }} />
            <label htmlFor="classification-select" style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              Data:
            </label>
            <select
              id="classification-select"
              value={classification}
              onChange={(e) => onClassificationChange(e.target.value as DataClassification)}
              style={{
                backgroundColor: 'var(--bg-surface-elevated)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-default)',
                borderRadius: 'var(--radius-sm)',
                padding: '3px 8px',
                fontSize: '0.75rem',
                cursor: 'pointer',
              }}
            >
              <option value="PUBLIC">PUBLIC (Cloud Eligible)</option>
              <option value="INTERNAL">INTERNAL (Local Only)</option>
              <option value="SENSITIVE">SENSITIVE (Strict Local)</option>
              <option value="SECRET">SECRET (Zero Cloud Egress)</option>
            </select>
          </div>

          {currentRouting && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                fontSize: '0.75rem',
                color: 'var(--text-secondary)',
                backgroundColor: 'var(--bg-surface-elevated)',
                padding: '3px 8px',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--border-subtle)',
                fontFamily: 'var(--font-mono)',
              }}
            >
              <Cpu style={{ width: 12, height: 12, color: 'var(--primary)' }} />
              <span>{currentRouting.model}</span>
            </div>
          )}
        </div>
      </div>

      {/* Messages Scroll Area */}
      <div className="messages-list">
        {loadingHistory && (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 20, color: 'var(--text-muted)' }}>
            <RefreshCw className="animate-spin" style={{ width: 20, height: 20, marginRight: 8 }} />
            Loading conversation history...
          </div>
        )}

        {messages.length === 0 && !loadingHistory && (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              margin: 'auto',
              textAlign: 'center',
              color: 'var(--text-muted)',
              padding: 40,
            }}
          >
            <Shield style={{ width: 42, height: 42, color: 'var(--primary)', marginBottom: 12, opacity: 0.8 }} />
            <h3 style={{ fontSize: '1.1rem', marginBottom: 6, color: 'var(--text-primary)' }}>
              PICO Intelligence Assistant
            </h3>
            <p style={{ fontSize: '0.85rem', maxWidth: 460, lineHeight: 1.5 }}>
              Ask questions, initiate automated tasks, explore memories, or review pending approvals.
              All non-public interactions remain local-only.
            </p>
          </div>
        )}

        {messages.map((msg) => (
          <div key={msg.id} className={`message-bubble ${msg.role}`}>
            <div className="message-header">
              <span style={{ fontWeight: 600, textTransform: 'capitalize' }}>
                {msg.role === 'user' ? 'Operator' : 'PICO'}
              </span>
              <span>{new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
            </div>

            <div className="message-content">{msg.content}</div>

            {msg.routing_metadata && (
              <div className="routing-meta-tag">
                <span>{msg.routing_metadata.provider}</span>
                <span>/</span>
                <span>{msg.routing_metadata.model}</span>
                <span>•</span>
                <span className={`classification-badge ${msg.routing_metadata.effective_classification}`}>
                  {msg.routing_metadata.effective_classification}
                </span>
              </div>
            )}
          </div>
        ))}

        {/* Live Streaming Content Bubble */}
        {streaming && (
          <div className="message-bubble assistant">
            <div className="message-header">
              <span style={{ fontWeight: 600 }}>PICO (Streaming)</span>
              <span className="animate-pulse" style={{ color: 'var(--primary)', fontSize: '0.75rem' }}>
                ● generating...
              </span>
            </div>
            <div className="message-content">
              {streamingContent || <span style={{ color: 'var(--text-muted)' }}>Thinking...</span>}
            </div>
            {currentRouting && (
              <div className="routing-meta-tag">
                <span>{currentRouting.provider}</span>
                <span>/</span>
                <span>{currentRouting.model}</span>
                <span>•</span>
                <span className={`classification-badge ${currentRouting.effective_classification}`}>
                  {currentRouting.effective_classification}
                </span>
              </div>
            )}
          </div>
        )}

        {isCancelled && !streaming && (
          <div style={{ textAlign: 'center', fontSize: '0.75rem', color: 'var(--warning)', margin: '4px 0' }}>
            Generation stopped by operator.
          </div>
        )}

        {streamError && (
          <div
            style={{
              padding: '10px 14px',
              borderRadius: 'var(--radius-md)',
              backgroundColor: 'var(--danger-subtle)',
              border: '1px solid rgba(239, 68, 68, 0.3)',
              color: 'var(--danger)',
              fontSize: '0.85rem',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
            role="alert"
          >
            <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
            <span>{streamError}</span>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input Area */}
      <div className="chat-input-area">
        <div className="input-controls-row">
          <span>Shift + Enter for new line</span>
          {streaming ? (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleStopGeneration}
              style={{ fontSize: '0.75rem', padding: '4px 8px' }}
            >
              <Square style={{ width: 12, height: 12, marginRight: 4, color: 'var(--danger)' }} />
              Stop Generation
            </button>
          ) : null}
        </div>

        <div className="input-box-wrapper">
          <VoiceControl onTranscription={(text) => setInput((prev) => (prev ? `${prev} ${text}` : text))} />

          <textarea
            className="chat-textarea"
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Type your message or instruction to PICO..."
            aria-label="Message input"
            disabled={streaming}
          />

          <button
            type="button"
            className="btn btn-primary"
            onClick={handleSendMessage}
            disabled={!input.trim() || streaming}
            style={{ padding: '8px 12px', minWidth: 40 }}
            aria-label="Send message"
          >
            <Send style={{ width: 16, height: 16 }} />
          </button>
        </div>
      </div>
    </div>
  );
};
