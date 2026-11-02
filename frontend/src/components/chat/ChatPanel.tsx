import React, { useState, useEffect, useRef } from 'react';
import { Send, Square, AlertTriangle, Shield, Cpu, RefreshCw, FileText, Trash2, Edit3, ArrowUpRight, GitFork, CheckCircle, X, HelpCircle, Layers } from 'lucide-react';
import { api } from '../../api/client';
import { streamChat } from '../../api/sse';
import { Conversation, DataClassification, Message, RoutingMetadata, WorkflowSynthesizeResponse, WorkflowCreatePayload } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';
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
  const { isBackendReachable, drafts, addDraft, removeDraft } = useConnectivity();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState('');
  const [currentRouting, setCurrentRouting] = useState<RoutingMetadata | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [ambiguousOutcome, setAmbiguousOutcome] = useState(false);
  const [isCancelled, setIsCancelled] = useState(false);
  const [synthesizingMessageId, setSynthesizingMessageId] = useState<string | null>(null);
  const [synthesisResponse, setSynthesisResponse] = useState<WorkflowSynthesizeResponse | null>(null);
  const [synthesisError, setSynthesisError] = useState<string | null>(null);
  const [isSubmittingWorkflow, setIsSubmittingWorkflow] = useState(false);
  const [submittedWorkflowId, setSubmittedWorkflowId] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const activeConversationIdRef = useRef<string | null>(activeConversation?.id ?? null);
  const activeStreamIdRef = useRef<number>(0);
  const streamConversationIdRef = useRef<string | null>(null);
  const synthesisRequestIdRef = useRef<number>(0);

  activeConversationIdRef.current = activeConversation?.id ?? streamConversationIdRef.current ?? null;

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: 'smooth' });
  };

  const refreshHistory = async () => {
    if (!activeConversation) return;
    setLoadingHistory(true);
    setStreamError(null);
    try {
      const history = await api.getMessages(activeConversation.id, 50, 0);
      const sorted = [...(history || [])].reverse();
      setMessages(sorted);
      setAmbiguousOutcome(false);
    } catch (err) {
      setStreamError(`Failed to refresh history: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoadingHistory(false);
    }
  };

  // Load message history when active conversation changes
  useEffect(() => {
    activeConversationIdRef.current = activeConversation?.id ?? null;
    if (!activeConversation) {
      setMessages([]);
      setStreaming(false);
      setStreamingContent('');
      setCurrentRouting(null);
      setIsCancelled(false);
      streamConversationIdRef.current = null;
      return;
    }

    // Immediately filter local messages to retain only those matching the newly active conversation.
    // This prevents previous conversation messages from lingering while history loads or if the request fails,
    // while preserving unpersisted local messages for the newly active conversation.
    setMessages((prev) => prev.filter((m) => m.conversation_id === activeConversation.id));

    let isMounted = true;
    setLoadingHistory(true);
    if (streamConversationIdRef.current !== activeConversation.id) {
      setStreaming(false);
      setStreamingContent('');
      setCurrentRouting(null);
      setIsCancelled(false);
    }
    setStreamError(null);
    setAmbiguousOutcome(false);

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
      synthesisRequestIdRef.current += 1;
      setSynthesizingMessageId(null);
      setSynthesisResponse(null);
      setSynthesisError(null);
      setSubmittedWorkflowId(null);
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [activeConversation]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, streamingContent]);

  const handlePlanWorkflow = async (msg: Message) => {
    if (!isBackendReachable || streaming || synthesizingMessageId !== null) return;
    const reqId = ++synthesisRequestIdRef.current;
    const targetConvId = msg.conversation_id;
    setSynthesizingMessageId(msg.id);
    setSynthesisError(null);
    setSynthesisResponse(null);
    setSubmittedWorkflowId(null);
    try {
      const res = await api.synthesizeWorkflow({
        conversation_id: msg.conversation_id,
        user_message_id: msg.id,
      });
      if (synthesisRequestIdRef.current === reqId && activeConversationIdRef.current === targetConvId) {
        setSynthesisResponse(res);
      }
    } catch (err) {
      if (synthesisRequestIdRef.current === reqId && activeConversationIdRef.current === targetConvId) {
        setSynthesisError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (synthesisRequestIdRef.current === reqId) {
        setSynthesizingMessageId(null);
      }
    }
  };

  const handleSubmitWorkflow = async (proposal: WorkflowCreatePayload) => {
    if (!isBackendReachable || isSubmittingWorkflow) return;
    setIsSubmittingWorkflow(true);
    try {
      const run = await api.submitWorkflow(proposal);
      setSubmittedWorkflowId(run.id);
    } catch (err) {
      setSynthesisError(`Failed to submit workflow: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setIsSubmittingWorkflow(false);
    }
  };

  const handleDismissProposal = () => {
    synthesisRequestIdRef.current += 1;
    setSynthesizingMessageId(null);
    setSynthesisResponse(null);
    setSynthesisError(null);
    setSubmittedWorkflowId(null);
  };

  const handleSendMessage = async () => {
    const trimmed = input.trim();
    if (!trimmed || streaming) return;

    if (!isBackendReachable) {
      // Backend offline: store in bounded, in-memory drafts (never auto-sent)
      addDraft(trimmed, activeConversation?.id, classification);
      setInput('');
      setStreamError('Backend offline. Message preserved as an unsent in-memory draft.');
      return;
    }

    setStreamError(null);
    setIsCancelled(false);
    setAmbiguousOutcome(false);

    let convId = activeConversation?.id;

    // Create conversation on the fly if none is active
    if (!convId) {
      try {
        const title = trimmed.slice(0, 30) + (trimmed.length > 30 ? '...' : '');
        const newConv = await api.createConversation(title);
        convId = newConv.id;
        activeConversationIdRef.current = newConv.id;
        streamConversationIdRef.current = newConv.id;
        onConversationCreated(newConv);
      } catch (err) {
        // Do not clear the draft before the required conversation-creation request succeeds
        setStreamError(`Failed to initialize session: ${err instanceof Error ? err.message : String(err)}. Message retained in input.`);
        return;
      }
    }

    // Clear input now that conversation exists and request is submitted
    setInput('');

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
    const streamId = ++activeStreamIdRef.current;
    streamConversationIdRef.current = convId ?? null;
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
            if (activeConversationIdRef.current !== convId || activeStreamIdRef.current !== streamId) {
              return;
            }
            if (meta.user_message_id) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === userMessage.id ? { ...m, id: meta.user_message_id! } : m
                )
              );
            }
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
            if (activeConversationIdRef.current === convId && activeStreamIdRef.current === streamId) {
              setStreamingContent(accumulated);
            }
          },
          onDone: (done) => {
            completionSuccessful = true;
            if (done.routing_metadata) {
              finalRouting = done.routing_metadata;
            }
            if (activeConversationIdRef.current === convId && activeStreamIdRef.current === streamId) {
              if (done.routing_metadata) {
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
            }
          },
          onError: (err) => {
            if (activeConversationIdRef.current !== convId || activeStreamIdRef.current !== streamId) {
              return;
            }
            const isNetworkInterruption =
              err.error === 'NetworkError' ||
              err.error === 'TimeoutError' ||
              !isBackendReachable ||
              (err.detail && err.detail.toLowerCase().includes('network'));

            if (isNetworkInterruption) {
              setAmbiguousOutcome(true);
              setStreamError(
                'Stream connection lost. Outcome uncertain: check conversation history before resubmitting. Automatic retry is prevented to protect against duplicate actions.'
              );
            } else {
              setStreamError(err.detail ? `${err.error}: ${err.detail}` : err.error);
            }
          },
        },
        controller.signal
      );
    } catch (err) {
      if (activeConversationIdRef.current === convId && activeStreamIdRef.current === streamId) {
        if (!controller.signal.aborted) {
          setStreamError(err instanceof Error ? err.message : String(err));
        }
      }
    } finally {
      const isCurrentStreamInstance = activeStreamIdRef.current === streamId;
      const isCurrentConv = activeConversationIdRef.current === convId;

      if (isCurrentStreamInstance) {
        setStreaming(false);
        streamConversationIdRef.current = null;
        if (abortControllerRef.current === controller) {
          abortControllerRef.current = null;
        }
        setStreamingContent('');
      }

      if (!completionSuccessful && isCurrentConv && isCurrentStreamInstance) {
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

            {msg.role === 'user' && !msg.id.startsWith('local_user_') && (
              <div style={{ marginTop: 8, display: 'flex', justifyContent: 'flex-end' }}>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => handlePlanWorkflow(msg)}
                  disabled={streaming || !isBackendReachable || synthesizingMessageId !== null}
                  style={{
                    fontSize: '0.72rem',
                    padding: '3px 8px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                    opacity: streaming || !isBackendReachable ? 0.6 : 1,
                  }}
                  title={
                    !isBackendReachable
                      ? 'Backend offline — workflow planning unavailable'
                      : streaming
                      ? 'Streaming in progress'
                      : 'Decompose and plan this request as a structured workflow'
                  }
                  aria-label={`Plan request ${msg.id} as workflow`}
                >
                  <GitFork style={{ width: 12, height: 12 }} />
                  {synthesizingMessageId === msg.id ? 'Planning...' : 'Plan as Workflow'}
                </button>
              </div>
            )}

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

        {ambiguousOutcome && (
          <div
            style={{
              padding: '10px 14px',
              borderRadius: 'var(--radius-md)',
              backgroundColor: 'var(--warning-subtle)',
              border: '1px solid rgba(245, 158, 11, 0.4)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              fontSize: '0.85rem',
              color: '#fcd34d',
              margin: '6px 0',
            }}
            role="alert"
          >
            <span>Outcome uncertain: verify history before repeating your instruction.</span>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={refreshHistory}
              disabled={loadingHistory || !isBackendReachable}
              style={{ fontSize: '0.75rem', padding: '3px 8px' }}
            >
              <RefreshCw className={loadingHistory ? 'animate-spin' : ''} style={{ width: 12, height: 12, marginRight: 4 }} />
              Inspect History
            </button>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Bounded In-Memory Drafts Outbox */}
      {drafts.length > 0 && (
        <div
          style={{
            backgroundColor: 'var(--bg-surface-elevated)',
            border: '1px solid var(--border-default)',
            borderRadius: 'var(--radius-md)',
            padding: '10px 14px',
            margin: '0 20px 10px 20px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
              <FileText style={{ width: 14, height: 14, color: 'var(--info)' }} />
              <span>In-Memory Drafts ({drafts.length}) — Not Transmitted</span>
            </div>
            <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>Memory-only, never auto-sent</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 130, overflowY: 'auto' }}>
            {drafts.map((d) => (
              <div
                key={d.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  backgroundColor: 'var(--bg-surface)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '6px 10px',
                  fontSize: '0.8rem',
                }}
              >
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '65%' }}>
                  {d.text}
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    style={{ fontSize: '0.7rem', padding: '2px 6px' }}
                    onClick={() => {
                      setInput(d.text);
                      removeDraft(d.id);
                    }}
                    title="Load draft into editor"
                  >
                    <Edit3 style={{ width: 12, height: 12, marginRight: 3 }} />
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    style={{ fontSize: '0.7rem', padding: '2px 8px' }}
                    disabled={!isBackendReachable || streaming}
                    onClick={() => {
                      removeDraft(d.id);
                      setInput(d.text);
                    }}
                    title={isBackendReachable ? 'Stage draft to send' : 'Backend offline — cannot send'}
                  >
                    <ArrowUpRight style={{ width: 12, height: 12, marginRight: 3 }} />
                    Stage
                  </button>
                  <button
                    type="button"
                    className="btn-icon"
                    style={{ color: 'var(--danger)', padding: 3 }}
                    onClick={() => removeDraft(d.id)}
                    title="Discard draft"
                    aria-label="Discard draft"
                  >
                    <Trash2 style={{ width: 13, height: 13 }} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Workflow Synthesis Proposal & Feedback Cards */}
      {(synthesisResponse || synthesisError || submittedWorkflowId) && (
        <div
          style={{
            margin: '0 16px 12px 16px',
            padding: 14,
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'var(--bg-surface-elevated)',
            border: '1px solid var(--border-default)',
            boxShadow: 'var(--shadow-md)',
            maxHeight: '320px',
            overflowY: 'auto',
          }}
          data-testid="workflow-proposal-card"
        >
          {synthesisError && (
            <div
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                justifyContent: 'space-between',
                color: 'var(--danger)',
                fontSize: '0.85rem',
                gap: 8,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
                <span>{synthesisError}</span>
              </div>
              <button
                type="button"
                className="btn-icon"
                onClick={handleDismissProposal}
                title="Close"
                aria-label="Close"
              >
                <X style={{ width: 14, height: 14 }} />
              </button>
            </div>
          )}

          {submittedWorkflowId && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--success)' }}>
                  <CheckCircle style={{ width: 18, height: 18 }} />
                  <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>Workflow Submitted Successfully</span>
                </div>
                <button
                  type="button"
                  className="btn-icon"
                  onClick={handleDismissProposal}
                  title="Close"
                  aria-label="Close"
                >
                  <X style={{ width: 14, height: 14 }} />
                </button>
              </div>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                Registered pending workflow run <code>{submittedWorkflowId}</code>. Execution is separate and explicit: switch to the <strong>Workflows</strong> tab to inspect steps, approve consequential actions, and advance execution.
              </p>
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 4 }}>
                <button type="button" className="btn btn-secondary btn-sm" onClick={handleDismissProposal}>
                  Done
                </button>
              </div>
            </div>
          )}

          {synthesisResponse && !submittedWorkflowId && (
            <div>
              {synthesisResponse.status === 'needs_clarification' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--warning)' }}>
                      <HelpCircle style={{ width: 18, height: 18 }} />
                      <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>Clarification Required</span>
                    </div>
                    <button
                      type="button"
                      className="btn-icon"
                      onClick={handleDismissProposal}
                      title="Close"
                      aria-label="Close"
                    >
                      <X style={{ width: 14, height: 14 }} />
                    </button>
                  </div>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-primary)', fontWeight: 500 }}>
                    {synthesisResponse.clarification_question}
                  </p>
                  {synthesisResponse.explanation && (
                    <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                      {synthesisResponse.explanation}
                    </p>
                  )}
                  <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 4 }}>
                    <button type="button" className="btn btn-secondary btn-sm" onClick={handleDismissProposal}>
                      Dismiss
                    </button>
                  </div>
                </div>
              )}

              {synthesisResponse.status === 'unsupported' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--danger)' }}>
                      <AlertTriangle style={{ width: 18, height: 18 }} />
                      <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>Workflow Unsupported</span>
                    </div>
                    <button
                      type="button"
                      className="btn-icon"
                      onClick={handleDismissProposal}
                      title="Close"
                      aria-label="Close"
                    >
                      <X style={{ width: 14, height: 14 }} />
                    </button>
                  </div>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    {synthesisResponse.explanation || 'This request cannot be fulfilled by registered capabilities or exceeds system safety boundaries.'}
                  </p>
                  <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 4 }}>
                    <button type="button" className="btn btn-secondary btn-sm" onClick={handleDismissProposal}>
                      Dismiss
                    </button>
                  </div>
                </div>
              )}

              {synthesisResponse.status === 'ready' && synthesisResponse.workflow_proposal && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <Layers style={{ width: 16, height: 16, color: 'var(--primary)' }} />
                        <span style={{ fontWeight: 600, fontSize: '0.95rem', color: 'var(--text-primary)' }}>
                          {synthesisResponse.workflow_proposal.name}
                        </span>
                        <span className={`classification-badge ${synthesisResponse.effective_classification}`}>
                          {synthesisResponse.effective_classification}
                        </span>
                      </div>
                      <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 2 }}>
                        Synthesized via {synthesisResponse.selected_profile} ({synthesisResponse.provider}/{synthesisResponse.model_name}) • {synthesisResponse.step_details.length} steps
                      </div>
                    </div>
                    <button
                      type="button"
                      className="btn-icon"
                      onClick={handleDismissProposal}
                      title="Close"
                      aria-label="Close"
                    >
                      <X style={{ width: 14, height: 14 }} />
                    </button>
                  </div>

                  {synthesisResponse.explanation && (
                    <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                      {synthesisResponse.explanation}
                    </p>
                  )}

                  {/* Step Breakdown */}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, margin: '4px 0' }}>
                    {synthesisResponse.step_details.map((step, idx) => (
                      <div
                        key={step.step_key}
                        style={{
                          padding: '6px 10px',
                          borderRadius: 'var(--radius-sm)',
                          backgroundColor: 'var(--bg-surface)',
                          border: '1px solid var(--border-subtle)',
                          fontSize: '0.78rem',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 2 }}>
                          <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                            {idx + 1}. {step.step_key} <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>({step.capability_id})</span>
                          </span>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <span
                              style={{
                                padding: '1px 5px',
                                borderRadius: 3,
                                fontSize: '0.7rem',
                                fontWeight: 500,
                                backgroundColor: step.risk_tier === 'HIGH' ? 'var(--danger-subtle)' : 'var(--primary-subtle)',
                                color: step.risk_tier === 'HIGH' ? 'var(--danger)' : 'var(--primary)',
                              }}
                            >
                              {step.risk_tier}
                            </span>
                            {step.consequential && (
                              <span
                                style={{
                                  padding: '1px 5px',
                                  borderRadius: 3,
                                  fontSize: '0.7rem',
                                  fontWeight: 500,
                                  backgroundColor: 'var(--warning-subtle)',
                                  color: 'var(--warning)',
                                }}
                              >
                                Consequential
                              </span>
                            )}
                          </div>
                        </div>
                        <div style={{ color: 'var(--text-muted)', fontSize: '0.73rem' }}>
                          {step.capability_description}
                        </div>
                        {step.dependencies.length > 0 && (
                          <div style={{ color: 'var(--text-muted)', fontSize: '0.7rem', marginTop: 2 }}>
                            Depends on: {step.dependencies.join(', ')}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  {/* Footer Actions */}
                  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={handleDismissProposal}
                      disabled={isSubmittingWorkflow}
                    >
                      Discard
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => handleSubmitWorkflow(synthesisResponse.workflow_proposal!)}
                      disabled={isSubmittingWorkflow || !isBackendReachable}
                    >
                      {isSubmittingWorkflow ? 'Submitting...' : 'Submit Workflow'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

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
            placeholder={
              !isBackendReachable
                ? 'Backend is offline — type message to save as local draft...'
                : 'Type your message or instruction to PICO...'
            }
            aria-label="Message input"
            disabled={streaming}
          />

          <button
            type="button"
            className={`btn ${!isBackendReachable ? 'btn-secondary' : 'btn-primary'}`}
            onClick={handleSendMessage}
            disabled={!input.trim() || streaming}
            style={{ padding: '8px 12px', minWidth: 40 }}
            title={!isBackendReachable ? 'Backend offline: save as in-memory draft' : 'Send message'}
            aria-label={!isBackendReachable ? 'Save as draft' : 'Send message'}
          >
            {!isBackendReachable ? <FileText style={{ width: 16, height: 16 }} /> : <Send style={{ width: 16, height: 16 }} />}
          </button>
        </div>
      </div>
    </div>
  );
};
