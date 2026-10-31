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
import { OperatorSessionState, ToolExecution } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

interface ApprovalInboxProps {
  sessionState: OperatorSessionState;
  onOpenPairingModal: () => void;
}

interface AmbiguousState {
  reason: string;
  timestamp: string;
  action: 'approve' | 'reject' | 'dispatch';
}

export const ApprovalInbox: React.FC<ApprovalInboxProps> = ({ sessionState, onOpenPairingModal }) => {
  const { isBackendReachable } = useConnectivity();
  const [awaitingExecutions, setAwaitingExecutions] = useState<ToolExecution[]>([]);
  const [queuedExecutions, setQueuedExecutions] = useState<ToolExecution[]>([]);
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
  const requestSeq = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);

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

  const fetchExecutions = async (clearAllErrors = false) => {
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
    try {
      // Query recent executions from backend
      const items = await api.listExecutions();
      if (!isMountedRef.current || seq !== requestSeq.current) return;
      const all = items || [];
      setAwaitingExecutions(all.filter((item) => item.status === 'awaiting_approval'));
      setQueuedExecutions(all.filter((item) => item.status === 'queued'));
      setLastSuccessfulFetch(new Date());
      setIsStale(false);
      setFetchError(null);
    } catch (err) {
      if (!isMountedRef.current || seq !== requestSeq.current) return;
      setFetchError(err instanceof Error ? err.message : String(err));
      // Retain in-memory items during transient network failure
      setIsStale(true);
    } finally {
      if (isMountedRef.current && seq === requestSeq.current) {
        setLoading(false);
        scheduleNextPoll(6000);
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
          // Server is currently processing
          setDispatchResults((prev) => ({ ...prev, [id]: execution }));
          setActionError(
            `Reconciliation in progress: execution ${id} is ${execution.status} on the server. Controls remain locked.`
          );
        } else {
          // Terminal status reached (succeeded, verification_failed, failed, cancelled, timed_out)
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
    try {
      const result = await api.executeExecution(id);
      setDispatchResults((prev) => ({ ...prev, [id]: result }));
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

  const totalPending = awaitingExecutions.length + queuedExecutions.length;

  return (
    <div className="panel-view">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h2 style={{ fontSize: '1.25rem', marginBottom: 4 }}>Consequential Approval & Dispatch Inbox</h2>
            {isStale && lastSuccessfulFetch && (
              <span className="stale-badge" title="Data retained in-memory during offline disconnection">
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

      {totalPending === 0 && Object.keys(dispatchResults).length === 0 ? (
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
          {awaitingExecutions.length > 0 && (
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
                <ShieldAlert style={{ color: 'var(--warning)', width: 18, height: 18 }} />
                <span>Awaiting Explicit Human Approval ({awaitingExecutions.length})</span>
              </h3>
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
            </div>
          )}

          {/* SECTION 2: Approved — Ready to Dispatch */}
          {queuedExecutions.length > 0 && (
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
                <Play style={{ color: '#60a5fa', width: 18, height: 18 }} />
                <span>Approved — Ready to Dispatch ({queuedExecutions.length})</span>
              </h3>
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
            </div>
          )}

          {/* SECTION 3: Recent Execution Outcomes (if any active or recently finished) */}
          {Object.keys(dispatchResults).length > 0 && (
            <div>
              <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
                <CheckCircle2 style={{ color: 'var(--success)', width: 18, height: 18 }} />
                <span>Authoritative Dispatch Outcomes</span>
              </h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {Object.values(dispatchResults).map((res) => (
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
