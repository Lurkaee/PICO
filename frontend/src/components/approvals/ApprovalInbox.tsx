import React, { useState, useEffect, useRef } from 'react';
import {
  ShieldCheck,
  ShieldAlert,
  Check,
  X,
  AlertTriangle,
  RefreshCw,
  Lock,
  Clock,
  Play,
  CheckCircle2,
} from 'lucide-react';
import { api } from '../../api/client';
import { OperatorSessionState, ToolExecution, ToolExecutionListEnvelope } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

interface ApprovalInboxProps {
  sessionState: OperatorSessionState;
  onOpenPairingModal: () => void;
}

interface AmbiguousState {
  reason: string;
  timestamp: string;
  action: 'approve' | 'reject' | 'dispatch';
  execution?: ToolExecution;
}

const PAGE_SIZE = 10;

const normalizeExecutionResponse = (
  res: any,
  targetStatus: string,
  defaultLimit = PAGE_SIZE,
  defaultOffset = 0
): ToolExecutionListEnvelope => {
  if (!res) {
    return { items: [], total: 0, limit: defaultLimit, offset: defaultOffset };
  }
  const targetStatuses = targetStatus.split(',').map((s) => s.trim());
  if (Array.isArray(res)) {
    const matched = res.filter((it) => targetStatuses.includes(it.status));
    return { items: matched, total: matched.length, limit: defaultLimit, offset: defaultOffset };
  }
  const rawItems: ToolExecution[] = Array.isArray(res.items) ? res.items : [];
  const matched = rawItems.filter((it: ToolExecution) => targetStatuses.includes(it.status));
  return {
    items: matched,
    total: typeof res.total === 'number' ? res.total : matched.length,
    limit: typeof res.limit === 'number' ? res.limit : defaultLimit,
    offset: typeof res.offset === 'number' ? res.offset : defaultOffset,
  };
};

export const ApprovalInbox: React.FC<ApprovalInboxProps> = ({ sessionState, onOpenPairingModal }) => {
  const { isBackendReachable } = useConnectivity();
  const [awaitingExecutions, setAwaitingExecutions] = useState<ToolExecution[]>([]);
  const [queuedExecutions, setQueuedExecutions] = useState<ToolExecution[]>([]);
  const [awaitingTotal, setAwaitingTotal] = useState(0);
  const [queuedTotal, setQueuedTotal] = useState(0);
  const [awaitingPage, setAwaitingPage] = useState(0);
  const [queuedPage, setQueuedPage] = useState(0);

  const [dispatchResults, setDispatchResults] = useState<Record<string, ToolExecution>>({});
  const [loading, setLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [approvalNote, setApprovalNote] = useState('');
  const [processingId, setProcessingId] = useState<string | null>(null);
  const [dispatchingId, setDispatchingId] = useState<string | null>(null);
  const [reconcilingId, setReconcilingId] = useState<string | null>(null);
  const [ambiguousExecutions, setAmbiguousExecutions] = useState<Record<string, AmbiguousState>>({});
  const [lastSuccessfulFetch, setLastSuccessfulFetch] = useState<Date | null>(null);
  const [isStale, setIsStale] = useState(false);
  const [incompleteDiscovery, setIncompleteDiscovery] = useState<{ undiscovered: number; total: number } | null>(null);

  const requestSeq = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const statusPollTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const isMountedRef = useRef(true);
  const awaitingPageRef = useRef(0);
  const queuedPageRef = useRef(0);
  const exhaustedPollsRef = useRef<Set<string>>(new Set());
  const [exhaustedPolls, setExhaustedPolls] = useState<Set<string>>(new Set());

  const markPollExhausted = (id: string) => {
    exhaustedPollsRef.current.add(id);
    setExhaustedPolls(new Set(exhaustedPollsRef.current));
  };

  const clearPollExhausted = (id: string) => {
    if (exhaustedPollsRef.current.has(id)) {
      exhaustedPollsRef.current.delete(id);
      setExhaustedPolls(new Set(exhaustedPollsRef.current));
    }
  };

  useEffect(() => {
    awaitingPageRef.current = awaitingPage;
  }, [awaitingPage]);

  useEffect(() => {
    queuedPageRef.current = queuedPage;
  }, [queuedPage]);

  const scheduleNextPoll = (delay = 6000) => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (!isMountedRef.current) return;
    pollTimerRef.current = setTimeout(() => {
      fetchExecutions();
    }, delay);
  };

  const clearStatusPollTimer = (id: string) => {
    if (statusPollTimersRef.current[id]) {
      clearTimeout(statusPollTimersRef.current[id]);
      delete statusPollTimersRef.current[id];
    }
  };

  const pollStatusUntilTerminal = (id: string, count = 0, maxPolls = 30) => {
    clearStatusPollTimer(id);
    if (!isMountedRef.current) return;
    if (count >= maxPolls) {
      markPollExhausted(id);
      setActionError(
        `Background polling limit reached for execution ${id}. Use the Reconcile button to inspect authoritative state.`
      );
      return;
    }

    statusPollTimersRef.current[id] = setTimeout(async () => {
      if (!isMountedRef.current) return;
      try {
        const updated = await api.getExecution(id);
        if (!isMountedRef.current) return;

        if (updated.status === 'running' || updated.status === 'verifying') {
          setDispatchResults((prev) => ({ ...prev, [id]: updated }));
          setAmbiguousExecutions((prev) =>
            prev[id]
              ? { ...prev, [id]: { ...prev[id], execution: updated } }
              : {
                  ...prev,
                  [id]: {
                    reason: 'In-flight execution reconciling with backend; awaiting terminal outcome.',
                    timestamp: new Date().toLocaleTimeString(),
                    action: 'dispatch',
                    execution: updated,
                  },
                }
          );
          pollStatusUntilTerminal(id, count + 1, maxPolls);
        } else {
          // Terminal status reached (succeeded, verification_failed, failed, cancelled, timed_out)
          clearStatusPollTimer(id);
          clearPollExhausted(id);
          setAmbiguousExecutions((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
          setDispatchResults((prev) => ({ ...prev, [id]: updated }));
          setActionError(
            `Execution ${id} reached terminal state '${updated.status}' with verification status '${updated.verification_status || 'unverified'}'.`
          );
          await fetchExecutions();
        }
      } catch {
        // Transient error during read-only status poll; retry boundedly
        if (!isMountedRef.current) return;
        pollStatusUntilTerminal(id, count + 1, maxPolls);
      }
    }, 2000);
  };

  const fetchInFlightExecutions = async (): Promise<ToolExecutionListEnvelope> => {
    const BATCH_SIZE = 50;
    const MAX_DISCOVERY = 100;
    const firstRes = await api.listExecutions('running,verifying', BATCH_SIZE, 0);
    const firstEnv = normalizeExecutionResponse(firstRes, 'running,verifying', BATCH_SIZE, 0);

    const allItems: ToolExecution[] = [...firstEnv.items];
    const total = firstEnv.total;
    let currentOffset = BATCH_SIZE;

    while (allItems.length < total && allItems.length < MAX_DISCOVERY) {
      const nextRes = await api.listExecutions('running,verifying', BATCH_SIZE, currentOffset);
      const nextEnv = normalizeExecutionResponse(nextRes, 'running,verifying', BATCH_SIZE, currentOffset);
      if (!nextEnv.items || nextEnv.items.length === 0) break;
      allItems.push(...nextEnv.items);
      currentOffset += BATCH_SIZE;
    }

    return {
      items: allItems,
      total,
      limit: BATCH_SIZE,
      offset: 0,
    };
  };

  const fetchExecutions = async (
    clearAllErrors = false,
    overrideAwaitingPage?: number,
    overrideQueuedPage?: number
  ) => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    const seq = ++requestSeq.current;
    setLoading(true);
    if (clearAllErrors) {
      setActionError(null);
      setFetchError(null);
    }

    const aPage = overrideAwaitingPage !== undefined ? overrideAwaitingPage : awaitingPageRef.current;
    const qPage = overrideQueuedPage !== undefined ? overrideQueuedPage : queuedPageRef.current;
    let needsImmediateRefetch = false;

    try {
      // Status-aware independent retrieval: awaiting_approval, queued, and bounded in-flight (running,verifying)
      const awaitingPromise = Promise.resolve(
        api.listExecutions('awaiting_approval', PAGE_SIZE, aPage * PAGE_SIZE)
      );
      const queuedPromise = Promise.resolve(
        api.listExecutions('queued', PAGE_SIZE, qPage * PAGE_SIZE)
      );
      const inFlightPromise = fetchInFlightExecutions()
        .then((env) => ({ ok: true as const, env }))
        .catch((err) => ({ ok: false as const, error: err instanceof Error ? err.message : String(err) }));

      const [awaitingRes, queuedRes, inFlightResult] = await Promise.all([
        awaitingPromise,
        queuedPromise,
        inFlightPromise,
      ]);

      if (!isMountedRef.current || seq !== requestSeq.current) return;

      const awaitingEnv = normalizeExecutionResponse(awaitingRes, 'awaiting_approval', PAGE_SIZE, aPage * PAGE_SIZE);
      const queuedEnv = normalizeExecutionResponse(queuedRes, 'queued', PAGE_SIZE, qPage * PAGE_SIZE);

      setAwaitingExecutions(awaitingEnv.items);
      setAwaitingTotal(awaitingEnv.total);

      setQueuedExecutions(queuedEnv.items);
      setQueuedTotal(queuedEnv.total);
      setLastSuccessfulFetch((prev) => prev || new Date());

      // Clamp pages to the last valid page for each status
      const lastAwaitingPage = Math.max(0, Math.ceil(awaitingEnv.total / PAGE_SIZE) - 1);
      const lastQueuedPage = Math.max(0, Math.ceil(queuedEnv.total / PAGE_SIZE) - 1);

      if (aPage > lastAwaitingPage) {
        setAwaitingPage(lastAwaitingPage);
        awaitingPageRef.current = lastAwaitingPage;
        needsImmediateRefetch = true;
      }

      if (qPage > lastQueuedPage) {
        setQueuedPage(lastQueuedPage);
        queuedPageRef.current = lastQueuedPage;
        needsImmediateRefetch = true;
      }

      // Handle in-flight results or failure
      if (!inFlightResult.ok) {
        // Do not silently swallow in-flight failures or report inbox as fully fresh
        setFetchError(`In-flight execution discovery failed: ${inFlightResult.error}. Active execution state may be incomplete.`);
        setIsStale(true);
      } else {
        const inFlightEnv = inFlightResult.env;
        const isDiscoveryIncomplete = inFlightEnv.total > inFlightEnv.items.length;
        const undiscoveredCount = inFlightEnv.total - inFlightEnv.items.length;

        if (isDiscoveryIncomplete) {
          setIncompleteDiscovery({ undiscovered: undiscoveredCount, total: inFlightEnv.total });
          setFetchError(
            `In-flight execution discovery is incomplete: ${undiscoveredCount} active execution(s) not retrieved (discovery capped at ${inFlightEnv.items.length} of ${inFlightEnv.total}). Active execution state may be incomplete.`
          );
          setIsStale(true);
        } else {
          setIncompleteDiscovery(null);
          setFetchError(null);
          setIsStale(false);
          setLastSuccessfulFetch(new Date());
        }

        // Reload and recovery: discover active in-flight executions from authoritative backend state
        if (inFlightEnv.items && inFlightEnv.items.length > 0) {
          setDispatchResults((prev) => {
            const next = { ...prev };
            for (const item of inFlightEnv.items) {
              next[item.id] = item;
            }
            return next;
          });

          setAmbiguousExecutions((prev) => {
            const next = { ...prev };
            for (const item of inFlightEnv.items) {
              if (item.status === 'running' || item.status === 'verifying') {
                if (!next[item.id]) {
                  next[item.id] = {
                    reason: 'In-flight execution discovered from backend; awaiting terminal outcome.',
                    timestamp: new Date().toLocaleTimeString(),
                    action: 'dispatch',
                    execution: item,
                  };
                } else {
                  next[item.id] = {
                    ...next[item.id],
                    execution: item,
                  };
                }
              }
            }
            return next;
          });

          for (const item of inFlightEnv.items) {
            if (item.status === 'running' || item.status === 'verifying') {
              if (!exhaustedPollsRef.current.has(item.id) && !statusPollTimersRef.current[item.id]) {
                pollStatusUntilTerminal(item.id);
              }
            }
          }
        }
      }
    } catch (err) {
      if (!isMountedRef.current || seq !== requestSeq.current) return;
      setFetchError(err instanceof Error ? err.message : String(err));
      // Retain in-memory items during transient network failure
      setIsStale(true);
    } finally {
      if (isMountedRef.current && seq === requestSeq.current) {
        setLoading(false);
        if (needsImmediateRefetch) {
          scheduleNextPoll(0);
        } else {
          scheduleNextPoll(6000);
        }
      }
    }
  };

  useEffect(() => {
    isMountedRef.current = true;
    fetchExecutions();
    return () => {
      isMountedRef.current = false;
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      Object.values(statusPollTimersRef.current).forEach((timer) => clearTimeout(timer));
      statusPollTimersRef.current = {};
      exhaustedPollsRef.current.clear();
    };
  }, []);

  const reconcileExecution = async (id: string) => {
    setReconcilingId(id);
    setActionError(null);
    try {
      const execution = await api.getExecution(id);
      const ambiguousInfo = ambiguousExecutions[id];
      const actionType = ambiguousInfo?.action;

      if (actionType === 'dispatch') {
        if (execution.status === 'queued') {
          // Confirmed server state is still queued: dispatch claim was not consumed
          clearStatusPollTimer(id);
          clearPollExhausted(id);
          setAmbiguousExecutions((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
          setActionError(
            `Reconciliation confirmed: execution ${id} is still queued and unconsumed. Dispatch controls unlocked.`
          );
          await fetchExecutions();
        } else if (execution.status === 'running' || execution.status === 'verifying') {
          // Server is currently processing: retain ambiguity lock, keep visible, continue bounded read-only polling
          clearStatusPollTimer(id);
          clearPollExhausted(id);
          setDispatchResults((prev) => ({ ...prev, [id]: execution }));
          setAmbiguousExecutions((prev) => ({
            ...prev,
            [id]: {
              ...(prev[id] || {
                reason: 'In-flight execution reconciling with backend',
                timestamp: new Date().toLocaleTimeString(),
                action: 'dispatch',
              }),
              execution,
            },
          }));
          setActionError(
            `Reconciliation in progress: execution ${id} is ${execution.status} on the server. Read-only polling active.`
          );
          pollStatusUntilTerminal(id, 0);
          await fetchExecutions();
        } else {
          // Terminal status reached (succeeded, verification_failed, failed, cancelled, timed_out)
          clearStatusPollTimer(id);
          clearPollExhausted(id);
          setAmbiguousExecutions((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
          setDispatchResults((prev) => ({ ...prev, [id]: execution }));
          setActionError(
            `Reconciliation confirmed: execution ${id} reached terminal state '${execution.status}'.`
          );
          await fetchExecutions();
        }
      } else if (actionType === 'approve') {
        clearStatusPollTimer(id);
        clearPollExhausted(id);
        if (execution.status === 'awaiting_approval') {
          // Confirmed server state is still awaiting approval (approval was not applied)
          setAmbiguousExecutions((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
          setActionError(
            `Reconciliation confirmed: execution ${id} is still awaiting approval. Controls unlocked.`
          );
          await fetchExecutions();
        } else {
          // Execution advanced to queued or beyond
          setAmbiguousExecutions((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
          setActionError(
            `Reconciliation confirmed: execution ${id} is now ${execution.status}.`
          );
          await fetchExecutions();
        }
      } else {
        // Fallback for cancel or general reconciliation
        clearStatusPollTimer(id);
        clearPollExhausted(id);
        setAmbiguousExecutions((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        setActionError(
          `Reconciliation confirmed: execution ${id} state is ${execution.status}.`
        );
        await fetchExecutions();
      }
    } catch (err) {
      setActionError(
        `Reconciliation inconclusive: could not verify execution state (${err instanceof Error ? err.message : String(err)}). Controls remain locked.`
      );
    } finally {
      setReconcilingId(null);
    }
  };

  const handleApprove = async (id: string) => {
    if (!isBackendReachable) {
      setActionError('Consequential approval controls are locked while backend is offline.');
      return;
    }
    if (ambiguousExecutions[id]) {
      setActionError('Execution outcome is unconfirmed. Reconcile with backend before retrying.');
      return;
    }
    if (!sessionState.paired) {
      onOpenPairingModal();
      return;
    }
    setProcessingId(id);
    setActionError(null);
    const executionItem = awaitingExecutions.find((x) => x.id === id);
    try {
      await api.approveExecution(
        id,
        approvalNote || 'Explicit approval via desktop interface',
        sessionState.approver || undefined
      );
      setConfirmingId(null);
      setApprovalNote('');
      await fetchExecutions();
    } catch (err) {
      const isNet =
        !isBackendReachable ||
        (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousExecutions((prev) => ({
          ...prev,
          [id]: {
            reason: 'Network interrupted during approval submission; outcome is unconfirmed.',
            timestamp: new Date().toLocaleTimeString(),
            action: 'approve',
            execution: executionItem,
          },
        }));
        setActionError(
          'Outcome unknown: network interrupted during approval submission. Controls locked until authoritative reconciliation.'
        );
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessingId(null);
    }
  };

  const handleDispatch = async (id: string) => {
    if (!isBackendReachable) {
      setActionError('Consequential dispatch controls are locked while backend is offline.');
      return;
    }
    if (ambiguousExecutions[id]) {
      setActionError('Dispatch outcome is unconfirmed. Reconcile with backend before retrying.');
      return;
    }
    if (!sessionState.paired) {
      onOpenPairingModal();
      return;
    }
    setDispatchingId(id);
    setActionError(null);
    const executionItem = queuedExecutions.find((x) => x.id === id);
    try {
      const result = await api.executeExecution(id);
      setDispatchResults((prev) => ({ ...prev, [id]: result }));
      if (result.status === 'running' || result.status === 'verifying') {
        clearPollExhausted(id);
        pollStatusUntilTerminal(id, 0);
      }
      await fetchExecutions();
    } catch (err) {
      const isNet =
        !isBackendReachable ||
        (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousExecutions((prev) => ({
          ...prev,
          [id]: {
            reason: 'Network interrupted during dispatch submission; execution outcome is unconfirmed.',
            timestamp: new Date().toLocaleTimeString(),
            action: 'dispatch',
            execution: executionItem,
          },
        }));
        setActionError(
          'Dispatch outcome uncertain: network interrupted during execution dispatch. Controls locked until authoritative reconciliation.'
        );
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setDispatchingId(null);
    }
  };

  const handleCancel = async (id: string) => {
    if (!isBackendReachable) {
      setActionError('Consequential rejection controls are locked while backend is offline.');
      return;
    }
    if (ambiguousExecutions[id]) {
      setActionError('Execution outcome is unconfirmed. Reconcile with backend before retrying.');
      return;
    }
    setProcessingId(id);
    setActionError(null);
    const executionItem =
      awaitingExecutions.find((x) => x.id === id) || queuedExecutions.find((x) => x.id === id);
    try {
      await api.cancelExecution(id, 'Cancelled by operator via approval inbox');
      setConfirmingId(null);
      await fetchExecutions();
    } catch (err) {
      const isNet =
        !isBackendReachable ||
        (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousExecutions((prev) => ({
          ...prev,
          [id]: {
            reason: 'Network interrupted during rejection submission; outcome is unconfirmed.',
            timestamp: new Date().toLocaleTimeString(),
            action: 'reject',
            execution: executionItem,
          },
        }));
        setActionError(
          'Outcome unknown: network interrupted during rejection submission. Controls locked until authoritative reconciliation.'
        );
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessingId(null);
    }
  };

  const getTargetEntity = (input: Record<string, unknown>) => {
    return (
      (input.target_entity_id as string) ||
      (input.entity_id as string) ||
      (input.resource_id as string) ||
      (input.alias as string) ||
      (input.application_id as string) ||
      (input.path as string) ||
      null
    );
  };

  const renderStatusBadge = (status: string, verificationStatus?: string | null) => {
    switch (status) {
      case 'awaiting_approval':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(245, 158, 11, 0.15)',
              color: 'var(--warning)',
              border: '1px solid rgba(245, 158, 11, 0.3)',
              fontWeight: 600,
            }}
          >
            Awaiting Approval
          </span>
        );
      case 'queued':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(59, 130, 246, 0.15)',
              color: '#60a5fa',
              border: '1px solid rgba(59, 130, 246, 0.3)',
              fontWeight: 600,
            }}
          >
            Approved — Ready to Dispatch
          </span>
        );
      case 'running':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(168, 85, 247, 0.15)',
              color: '#c084fc',
              border: '1px solid rgba(168, 85, 247, 0.3)',
              fontWeight: 600,
            }}
          >
            Running
          </span>
        );
      case 'verifying':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(99, 102, 241, 0.15)',
              color: '#818cf8',
              border: '1px solid rgba(99, 102, 241, 0.3)',
              fontWeight: 600,
            }}
          >
            Verifying Evidence
          </span>
        );
      case 'succeeded':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(34, 197, 94, 0.15)',
              color: '#4ade80',
              border: '1px solid rgba(34, 197, 94, 0.3)',
              fontWeight: 600,
            }}
          >
            Succeeded {verificationStatus === 'passed' ? '(Verified)' : ''}
          </span>
        );
      case 'verification_failed':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(239, 68, 68, 0.15)',
              color: '#f87171',
              border: '1px solid rgba(239, 68, 68, 0.3)',
              fontWeight: 600,
            }}
          >
            Verification Failed
          </span>
        );
      case 'failed':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(239, 68, 68, 0.15)',
              color: '#f87171',
              border: '1px solid rgba(239, 68, 68, 0.3)',
              fontWeight: 600,
            }}
          >
            Failed
          </span>
        );
      case 'cancelled':
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'rgba(100, 116, 139, 0.15)',
              color: '#94a3b8',
              border: '1px solid rgba(100, 116, 139, 0.3)',
              fontWeight: 600,
            }}
          >
            Cancelled
          </span>
        );
      default:
        return (
          <span
            style={{
              fontSize: '0.7rem',
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              backgroundColor: 'var(--bg-canvas)',
              color: 'var(--text-muted)',
              border: '1px solid var(--border-subtle)',
              fontWeight: 600,
            }}
          >
            {status}
          </span>
        );
    }
  };

  const totalPending = awaitingTotal + queuedTotal;
  const ambiguousEntries = Object.entries(ambiguousExecutions);
  // Only render independent section for ambiguous executions that are NOT in the active awaiting/queued card lists
  const standaloneAmbiguous = ambiguousEntries.filter(
    ([id]) =>
      !awaitingExecutions.some((it) => it.id === id) &&
      !queuedExecutions.some((it) => it.id === id)
  );
  // Authoritative terminal outcomes to display (excluding in-flight, queued, awaiting, or ambiguous items)
  const terminalOutcomes = Object.values(dispatchResults).filter(
    (res) =>
      res.status !== 'running' &&
      res.status !== 'verifying' &&
      res.status !== 'queued' &&
      res.status !== 'awaiting_approval' &&
      !ambiguousExecutions[res.id]
  );

  return (
    <div className="panel-view">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h2 style={{ fontSize: '1.25rem', marginBottom: 4 }}>Consequential Approval & Dispatch Inbox</h2>
            {isStale && lastSuccessfulFetch && (
              <span
                data-testid="stale-indicator"
                className="stale-badge"
                title="Data retained in-memory during offline disconnection"
              >
                <Clock style={{ width: 12, height: 12 }} /> Stale as of {lastSuccessfulFetch.toLocaleTimeString()}
              </span>
            )}
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Actions with side effects (OS automation, physical actuation, filesystem mutations) require explicit operator review and manual dispatch.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={() => fetchExecutions(true)} disabled={loading}>
          <RefreshCw className={loading ? 'animate-spin' : ''} style={{ width: 14, height: 14 }} />
          Refresh
        </button>
      </div>

      {!isBackendReachable && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'rgba(239, 68, 68, 0.15)',
            border: '1px solid rgba(239, 68, 68, 0.3)',
            color: '#fca5a5',
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            fontSize: '0.85rem',
          }}
          role="alert"
        >
          <Lock style={{ width: 16, height: 16, flexShrink: 0 }} />
          <span>Backend offline: consequential controls are locked until connectivity and session state are revalidated.</span>
        </div>
      )}

      {!sessionState.paired && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'var(--warning-subtle)',
            border: '1px solid rgba(245, 158, 11, 0.3)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Lock style={{ color: 'var(--warning)', width: 20, height: 20 }} />
            <div>
              <p style={{ fontWeight: 600, color: 'var(--warning)', fontSize: '0.875rem' }}>
                Operator Session Required for Approval & Dispatch
              </p>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                Consequential approvals and manual dispatches are disabled until you authenticate a local session with a 6-digit one-time code.
              </p>
            </div>
          </div>
          <button className="btn btn-primary" onClick={onOpenPairingModal} style={{ fontSize: '0.8rem' }}>
            Pair Operator
          </button>
        </div>
      )}

      {actionError && (
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
          <span>{actionError}</span>
        </div>
      )}

      {fetchError && (
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
          <span>{fetchError}</span>
        </div>
      )}

      {incompleteDiscovery && (
        <div
          data-testid="incomplete-discovery-warning"
          style={{
            padding: '10px 14px',
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'rgba(245, 158, 11, 0.15)',
            border: '1px solid rgba(245, 158, 11, 0.35)',
            color: 'var(--warning)',
            fontSize: '0.85rem',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
          role="alert"
        >
          <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
          <span>
            <strong>Incomplete In-Flight Discovery:</strong> {incompleteDiscovery.undiscovered} active execution(s) not retrieved (discovery capped at 100 of {incompleteDiscovery.total} active executions). The inbox cannot guarantee complete visibility until existing in-flight executions are resolved.
          </span>
        </div>
      )}

      {/* SECTION: Ambiguous & Reconciling Executions (Kept visible independently when not present in pending lists) */}
      {standaloneAmbiguous.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          <h3
            style={{
              fontSize: '1rem',
              fontWeight: 600,
              marginBottom: 10,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              color: 'var(--warning)',
            }}
          >
            <AlertTriangle style={{ color: 'var(--warning)', width: 18, height: 18 }} />
            <span>Ambiguous & Reconciling Executions ({standaloneAmbiguous.length})</span>
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {standaloneAmbiguous.map(([id, amb]) => {
              const isReconciling = reconcilingId === id;
              const isExhausted = exhaustedPolls.has(id) || exhaustedPollsRef.current.has(id);
              const exec = amb.execution || dispatchResults[id];
              const statusDisplay = exec?.status || 'unconfirmed';
              const operationType = amb.action ? amb.action.toUpperCase() : 'UNKNOWN';

              return (
                <div key={id} className="card" style={{ borderLeft: '4px solid var(--warning)' }}>
                  <div className="card-header">
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <AlertTriangle style={{ color: 'var(--warning)', width: 18, height: 18 }} />
                      <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>
                        {exec?.capability_id || 'Consequential Operation'}
                      </span>
                      {renderStatusBadge(statusDisplay, exec?.verification_status)}
                    </div>
                    <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                      ID: {id}
                    </span>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: 16, fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: 4, marginBottom: 8 }}>
                    <div>
                      <strong>Operation:</strong> <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600 }}>{operationType}</span>
                    </div>
                    <div>
                      <strong>Authoritative Status:</strong> <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600 }}>{statusDisplay}</span>
                    </div>
                  </div>

                  <div
                    style={{
                      padding: '10px 14px',
                      borderRadius: 'var(--radius-sm)',
                      backgroundColor: 'rgba(245, 158, 11, 0.15)',
                      border: '1px solid rgba(245, 158, 11, 0.35)',
                      color: 'var(--warning)',
                      fontSize: '0.825rem',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: 10,
                      marginTop: 8,
                      marginBottom: 8,
                    }}
                    role="alert"
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
                      <span>
                        <strong>Ambiguity:</strong> {amb.reason} Controls are locked until authoritative reconciliation.
                      </span>
                    </div>
                    <button
                      className="btn btn-secondary"
                      onClick={() => reconcileExecution(id)}
                      disabled={isReconciling || !isBackendReachable}
                      style={{ padding: '4px 10px', fontSize: '0.75rem', whiteSpace: 'nowrap' }}
                      title="Reconcile authoritative execution state from backend"
                    >
                      <RefreshCw className={isReconciling ? 'animate-spin' : ''} style={{ width: 12, height: 12 }} />
                      Reconcile
                    </button>
                  </div>

                  {exec?.status && (exec.status === 'running' || exec.status === 'verifying') && (
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: 4 }}>
                      {isExhausted ? (
                        <em>Authoritative server state is '{exec.status}'. Background polling budget exhausted. Use Reconcile to inspect authoritative state.</em>
                      ) : (
                        <em>Authoritative server state is '{exec.status}'. Read-only background polling active...</em>
                      )}
                    </div>
                  )}

                  {exec?.verification_status && (
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: 6 }}>
                      <strong>Independent Verification:</strong>{' '}
                      <span
                        style={{
                          fontWeight: 600,
                          color: exec.verification_status === 'passed' ? 'var(--success)' : 'var(--danger)',
                        }}
                      >
                        {exec.verification_status.toUpperCase()}
                      </span>
                    </div>
                  )}

                  {exec?.error_message && (
                    <div
                      style={{
                        padding: '8px 12px',
                        borderRadius: 'var(--radius-sm)',
                        backgroundColor: 'var(--danger-subtle)',
                        color: 'var(--danger)',
                        fontSize: '0.8rem',
                        marginTop: 6,
                      }}
                    >
                      <strong>Error:</strong> {exec.error_message}
                    </div>
                  )}

                  {exec?.output_data && (
                    <div style={{ fontSize: '0.85rem', marginTop: 6 }}>
                      <p style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>
                        <strong>Output:</strong>
                      </p>
                      <pre
                        style={{
                          backgroundColor: 'var(--bg-canvas)',
                          padding: 8,
                          borderRadius: 'var(--radius-sm)',
                          fontSize: '0.75rem',
                          fontFamily: 'var(--font-mono)',
                          overflowX: 'auto',
                          border: '1px solid var(--border-subtle)',
                          maxHeight: 120,
                        }}
                      >
                        {JSON.stringify(exec.output_data, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {totalPending === 0 && ambiguousEntries.length === 0 && terminalOutcomes.length === 0 ? (
        <div
          className="card"
          style={{ textAlign: 'center', padding: '40px 20px', color: 'var(--text-muted)' }}
        >
          <ShieldCheck style={{ width: 36, height: 36, color: 'var(--success)', margin: '0 auto 10px', opacity: 0.8 }} />
          <h3 style={{ fontSize: '1rem', color: 'var(--text-primary)', marginBottom: 4 }}>
            Inbox Clear
          </h3>
          <p style={{ fontSize: '0.85rem' }}>No pending consequential executions awaiting approval or dispatch.</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          {/* SECTION 1: Awaiting Human Approval */}
          {awaitingTotal > 0 && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                <h3 style={{ fontSize: '1rem', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <ShieldAlert style={{ color: 'var(--warning)', width: 18, height: 18 }} />
                  <span>Awaiting Explicit Human Approval ({awaitingTotal})</span>
                </h3>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {awaitingExecutions.map((item) => {
                  const isConfirming = confirmingId === item.id;
                  const isBusy = processingId === item.id;
                  const isAmbiguous = Boolean(ambiguousExecutions[item.id]);
                  const isReconciling = reconcilingId === item.id;
                  const targetEntity = getTargetEntity(item.input_data || {});

                  return (
                    <div key={item.id} className="card" style={{ borderLeft: '4px solid var(--warning)' }}>
                      <div className="card-header">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <ShieldAlert style={{ color: 'var(--warning)', width: 18, height: 18 }} />
                          <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>{item.capability_id}</span>
                          {renderStatusBadge(item.status)}
                          {targetEntity && (
                            <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
                              [{targetEntity}]
                            </span>
                          )}
                        </div>
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                          ID: {item.id}
                        </span>
                      </div>

                      {isAmbiguous && (
                        <div
                          style={{
                            padding: '10px 14px',
                            borderRadius: 'var(--radius-sm)',
                            backgroundColor: 'rgba(245, 158, 11, 0.15)',
                            border: '1px solid rgba(245, 158, 11, 0.35)',
                            color: 'var(--warning)',
                            fontSize: '0.825rem',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            gap: 10,
                            marginBottom: 12,
                          }}
                          role="alert"
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
                            <span>
                              <strong>Outcome unknown:</strong> {ambiguousExecutions[item.id].reason} Actions are locked to prevent duplicate execution.
                            </span>
                          </div>
                          <button
                            className="btn btn-secondary"
                            onClick={() => reconcileExecution(item.id)}
                            disabled={isReconciling || !isBackendReachable}
                            style={{ padding: '4px 10px', fontSize: '0.75rem', whiteSpace: 'nowrap' }}
                            title="Reconcile authoritative execution state from backend"
                          >
                            <RefreshCw className={isReconciling ? 'animate-spin' : ''} style={{ width: 12, height: 12 }} />
                            Reconcile
                          </button>
                        </div>
                      )}

                      <div style={{ fontSize: '0.85rem', marginBottom: 12 }}>
                        <p style={{ color: 'var(--text-secondary)', marginBottom: 6 }}>
                          <strong>Requested Arguments:</strong>
                        </p>
                        <pre
                          style={{
                            backgroundColor: 'var(--bg-canvas)',
                            padding: 10,
                            borderRadius: 'var(--radius-sm)',
                            fontSize: '0.8rem',
                            fontFamily: 'var(--font-mono)',
                            overflowX: 'auto',
                            border: '1px solid var(--border-subtle)',
                            maxHeight: 180,
                          }}
                        >
                          {JSON.stringify(item.input_data, null, 2)}
                        </pre>
                      </div>

                      {isConfirming ? (
                        <div
                          style={{
                            padding: 12,
                            borderRadius: 'var(--radius-md)',
                            backgroundColor: 'var(--bg-canvas)',
                            border: '1px solid var(--border-default)',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 8,
                          }}
                        >
                          <p style={{ fontSize: '0.825rem', fontWeight: 600, color: 'var(--warning)' }}>
                            Confirm Consequential Execution
                          </p>
                          <input
                            type="text"
                            placeholder="Optional approval notes for event ledger audit"
                            value={approvalNote}
                            onChange={(e) => setApprovalNote(e.target.value)}
                            style={{
                              padding: '6px 10px',
                              borderRadius: 'var(--radius-sm)',
                              border: '1px solid var(--border-default)',
                              backgroundColor: 'var(--bg-surface)',
                              color: 'var(--text-primary)',
                              fontSize: '0.825rem',
                            }}
                          />
                          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                            <button
                              className="btn btn-secondary"
                              onClick={() => setConfirmingId(null)}
                              disabled={isBusy}
                              style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                            >
                              Back
                            </button>
                            <button
                              className="btn btn-danger"
                              onClick={() => handleCancel(item.id)}
                              disabled={isBusy || isAmbiguous || !isBackendReachable}
                              style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                              title={
                                isAmbiguous
                                  ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                                  : !isBackendReachable
                                  ? 'Controls locked: backend offline'
                                  : 'Reject execution'
                              }
                            >
                              <X style={{ width: 14, height: 14 }} />
                              Reject & Cancel
                            </button>
                            <button
                              className="btn btn-primary"
                              onClick={() => handleApprove(item.id)}
                              disabled={isBusy || isAmbiguous || !sessionState.paired || !isBackendReachable}
                              style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                              title={
                                isAmbiguous
                                  ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                                  : !isBackendReachable
                                  ? 'Controls locked: backend offline'
                                  : !sessionState.paired
                                  ? 'Pair operator session to approve'
                                  : 'Confirm approval'
                              }
                            >
                              <Check style={{ width: 14, height: 14 }} />
                              {isBusy ? 'Submitting...' : 'Confirm Approval'}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                          <button
                            className="btn btn-secondary"
                            onClick={() => handleCancel(item.id)}
                            disabled={isBusy || isAmbiguous || !isBackendReachable}
                            title={
                              isAmbiguous
                                ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                                : !isBackendReachable
                                ? 'Controls locked: backend offline'
                                : 'Reject execution'
                            }
                            style={{ fontSize: '0.8rem' }}
                          >
                            <X style={{ width: 14, height: 14 }} />
                            Reject
                          </button>
                          <button
                            className="btn btn-primary"
                            onClick={() => setConfirmingId(item.id)}
                            disabled={isBusy || isAmbiguous || !sessionState.paired || !isBackendReachable}
                            title={
                              isAmbiguous
                                ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                                : !isBackendReachable
                                ? 'Controls locked: backend offline'
                                : !sessionState.paired
                                ? 'Pair operator session to approve'
                                : 'Review & Approve'
                            }
                            style={{ fontSize: '0.8rem' }}
                          >
                            <Check style={{ width: 14, height: 14 }} />
                            Review & Approve
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Pagination Controls for Awaiting Executions */}
              {awaitingTotal > PAGE_SIZE && (
                <div
                  className="pagination-controls"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginTop: 12,
                    fontSize: '0.8rem',
                    color: 'var(--text-secondary)',
                  }}
                >
                  <span>
                    Showing {awaitingPage * PAGE_SIZE + 1}–{Math.min((awaitingPage + 1) * PAGE_SIZE, awaitingTotal)} of {awaitingTotal}
                  </span>
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <button
                      className="btn btn-secondary"
                      onClick={() => {
                        const prevPage = Math.max(0, awaitingPage - 1);
                        setAwaitingPage(prevPage);
                        awaitingPageRef.current = prevPage;
                        fetchExecutions(false, prevPage, queuedPageRef.current);
                      }}
                      disabled={awaitingPage === 0 || loading}
                      style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                    >
                      Previous
                    </button>
                    <span>
                      Page {awaitingPage + 1} of {Math.max(1, Math.ceil(awaitingTotal / PAGE_SIZE))}
                    </span>
                    <button
                      className="btn btn-secondary"
                      onClick={() => {
                        const nextPage = awaitingPage + 1;
                        setAwaitingPage(nextPage);
                        awaitingPageRef.current = nextPage;
                        fetchExecutions(false, nextPage, queuedPageRef.current);
                      }}
                      disabled={(awaitingPage + 1) * PAGE_SIZE >= awaitingTotal || loading}
                      style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* SECTION 2: Approved — Ready to Dispatch */}
          {queuedTotal > 0 && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                <h3 style={{ fontSize: '1rem', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Play style={{ color: '#60a5fa', width: 18, height: 18 }} />
                  <span>Approved — Ready to Dispatch ({queuedTotal})</span>
                </h3>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {queuedExecutions.map((item) => {
                  const isDispatching = dispatchingId === item.id;
                  const isAmbiguous = Boolean(ambiguousExecutions[item.id]);
                  const isReconciling = reconcilingId === item.id;
                  const targetEntity = getTargetEntity(item.input_data || {});

                  return (
                    <div key={item.id} className="card" style={{ borderLeft: '4px solid #3b82f6' }}>
                      <div className="card-header">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <Play style={{ color: '#60a5fa', width: 18, height: 18 }} />
                          <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>{item.capability_id}</span>
                          {renderStatusBadge(item.status)}
                          {targetEntity && (
                            <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
                              [{targetEntity}]
                            </span>
                          )}
                        </div>
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                          ID: {item.id}
                        </span>
                      </div>

                      {item.approved_by && (
                        <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 8 }}>
                          <span>
                            <strong>Approved by:</strong>{' '}
                            <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                              {item.approved_by}
                            </span>
                          </span>
                        </div>
                      )}

                      {isAmbiguous && (
                        <div
                          style={{
                            padding: '10px 14px',
                            borderRadius: 'var(--radius-sm)',
                            backgroundColor: 'rgba(245, 158, 11, 0.15)',
                            border: '1px solid rgba(245, 158, 11, 0.35)',
                            color: 'var(--warning)',
                            fontSize: '0.825rem',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            gap: 10,
                            marginBottom: 12,
                          }}
                          role="alert"
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
                            <span>
                              <strong>Dispatch outcome unknown:</strong> {ambiguousExecutions[item.id].reason} Actions are locked until authoritative reconciliation.
                            </span>
                          </div>
                          <button
                            className="btn btn-secondary"
                            onClick={() => reconcileExecution(item.id)}
                            disabled={isReconciling || !isBackendReachable}
                            style={{ padding: '4px 10px', fontSize: '0.75rem', whiteSpace: 'nowrap' }}
                            title="Reconcile authoritative execution state from backend"
                          >
                            <RefreshCw className={isReconciling ? 'animate-spin' : ''} style={{ width: 12, height: 12 }} />
                            Reconcile
                          </button>
                        </div>
                      )}

                      <div style={{ fontSize: '0.85rem', marginBottom: 12 }}>
                        <p style={{ color: 'var(--text-secondary)', marginBottom: 6 }}>
                          <strong>Approved Arguments:</strong>
                        </p>
                        <pre
                          style={{
                            backgroundColor: 'var(--bg-canvas)',
                            padding: 10,
                            borderRadius: 'var(--radius-sm)',
                            fontSize: '0.8rem',
                            fontFamily: 'var(--font-mono)',
                            overflowX: 'auto',
                            border: '1px solid var(--border-subtle)',
                            maxHeight: 180,
                          }}
                        >
                          {JSON.stringify(item.input_data, null, 2)}
                        </pre>
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                        <button
                          className="btn btn-secondary"
                          onClick={() => handleCancel(item.id)}
                          disabled={isDispatching || isAmbiguous || !isBackendReachable}
                          title={
                            isAmbiguous
                              ? 'Controls locked: outcome is unconfirmed. Reconcile first.'
                              : !isBackendReachable
                              ? 'Controls locked: backend offline'
                              : 'Cancel queued execution'
                          }
                          style={{ fontSize: '0.8rem' }}
                        >
                          <X style={{ width: 14, height: 14 }} />
                          Cancel
                        </button>
                        <button
                          className="btn btn-primary"
                          onClick={() => handleDispatch(item.id)}
                          disabled={isDispatching || isAmbiguous || !sessionState.paired || !isBackendReachable}
                          title={
                            isAmbiguous
                              ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                              : !isBackendReachable
                              ? 'Controls locked: backend offline'
                              : !sessionState.paired
                              ? 'Pair operator session to dispatch'
                              : 'Dispatch execution immediately'
                          }
                          style={{ fontSize: '0.8rem' }}
                        >
                          <Play style={{ width: 14, height: 14 }} />
                          {isDispatching ? 'Dispatching...' : 'Dispatch Execution'}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Pagination Controls for Queued Executions */}
              {queuedTotal > PAGE_SIZE && (
                <div
                  className="pagination-controls"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginTop: 12,
                    fontSize: '0.8rem',
                    color: 'var(--text-secondary)',
                  }}
                >
                  <span>
                    Showing {queuedPage * PAGE_SIZE + 1}–{Math.min((queuedPage + 1) * PAGE_SIZE, queuedTotal)} of {queuedTotal}
                  </span>
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <button
                      className="btn btn-secondary"
                      onClick={() => {
                        const prevPage = Math.max(0, queuedPage - 1);
                        setQueuedPage(prevPage);
                        queuedPageRef.current = prevPage;
                        fetchExecutions(false, awaitingPageRef.current, prevPage);
                      }}
                      disabled={queuedPage === 0 || loading}
                      style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                    >
                      Previous
                    </button>
                    <span>
                      Page {queuedPage + 1} of {Math.max(1, Math.ceil(queuedTotal / PAGE_SIZE))}
                    </span>
                    <button
                      className="btn btn-secondary"
                      onClick={() => {
                        const nextPage = queuedPage + 1;
                        setQueuedPage(nextPage);
                        queuedPageRef.current = nextPage;
                        fetchExecutions(false, awaitingPageRef.current, nextPage);
                      }}
                      disabled={(queuedPage + 1) * PAGE_SIZE >= queuedTotal || loading}
                      style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* SECTION 3: Authoritative Dispatch Outcomes */}
          {terminalOutcomes.length > 0 && (
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
                <CheckCircle2 style={{ color: 'var(--success)', width: 18, height: 18 }} />
                <span>Authoritative Dispatch Outcomes</span>
              </h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {terminalOutcomes.map((res) => (
                  <div
                    key={res.id}
                    className="card"
                    style={{
                      borderLeft: `4px solid ${
                        res.status === 'succeeded'
                          ? 'var(--success)'
                          : res.status === 'verification_failed' || res.status === 'failed'
                          ? 'var(--danger)'
                          : '#60a5fa'
                      }`,
                    }}
                  >
                    <div className="card-header">
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>{res.capability_id}</span>
                        {renderStatusBadge(res.status, res.verification_status)}
                      </div>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                        ID: {res.id}
                      </span>
                    </div>

                    {res.error_message && (
                      <div
                        style={{
                          padding: '8px 12px',
                          borderRadius: 'var(--radius-sm)',
                          backgroundColor: 'var(--danger-subtle)',
                          color: 'var(--danger)',
                          fontSize: '0.8rem',
                          marginBottom: 8,
                        }}
                      >
                        <strong>Error:</strong> {res.error_message}
                      </div>
                    )}

                    {res.verification_status && (
                      <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 6 }}>
                        <strong>Independent Verification:</strong>{' '}
                        <span style={{ fontWeight: 600, color: res.verification_status === 'passed' ? 'var(--success)' : 'var(--danger)' }}>
                          {res.verification_status.toUpperCase()}
                        </span>
                      </div>
                    )}

                    {res.output_data && (
                      <div style={{ fontSize: '0.85rem' }}>
                        <p style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>
                          <strong>Output:</strong>
                        </p>
                        <pre
                          style={{
                            backgroundColor: 'var(--bg-canvas)',
                            padding: 8,
                            borderRadius: 'var(--radius-sm)',
                            fontSize: '0.75rem',
                            fontFamily: 'var(--font-mono)',
                            overflowX: 'auto',
                            border: '1px solid var(--border-subtle)',
                            maxHeight: 120,
                          }}
                        >
                          {JSON.stringify(res.output_data, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
