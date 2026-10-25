import React, { useState, useEffect } from 'react';
import { ShieldCheck, ShieldAlert, Check, X, AlertTriangle, RefreshCw, Lock } from 'lucide-react';
import { api } from '../../api/client';
import { OperatorSessionState, ToolExecution } from '../../types';

interface ApprovalInboxProps {
  sessionState: OperatorSessionState;
  onOpenPairingModal: () => void;
}

export const ApprovalInbox: React.FC<ApprovalInboxProps> = ({ sessionState, onOpenPairingModal }) => {
  const [executions, setExecutions] = useState<ToolExecution[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [approvalNote, setApprovalNote] = useState('');
  const [processingId, setProcessingId] = useState<string | null>(null);

  const fetchAwaitingApprovals = async () => {
    setLoading(true);
    setActionError(null);
    try {
      const items = await api.listExecutions('awaiting_approval');
      setExecutions(items || []);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setExecutions([]);
    } finally {

      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAwaitingApprovals();
    const timer = setInterval(fetchAwaitingApprovals, 6000);
    return () => clearInterval(timer);
  }, []);

  const handleApprove = async (id: string) => {
    if (!sessionState.paired) {
      onOpenPairingModal();
      return;
    }
    setProcessingId(id);
    setActionError(null);
    try {
      await api.approveExecution(id, approvalNote || 'Explicit approval via desktop interface', sessionState.approver || undefined);
      setConfirmingId(null);
      setApprovalNote('');
      await fetchAwaitingApprovals();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setProcessingId(null);
    }
  };

  const handleCancel = async (id: string) => {
    setProcessingId(id);
    setActionError(null);
    try {
      await api.cancelExecution(id, 'Cancelled by operator via approval inbox');
      setConfirmingId(null);
      await fetchAwaitingApprovals();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setProcessingId(null);
    }
  };

  return (
    <div className="panel-view">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h2 style={{ fontSize: '1.25rem', marginBottom: 4 }}>Consequential Approval Inbox</h2>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Actions with side effects (OS automation, physical actuation, filesystem mutations) require explicit operator confirmation.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={fetchAwaitingApprovals} disabled={loading}>
          <RefreshCw className={loading ? 'animate-spin' : ''} style={{ width: 14, height: 14 }} />
          Refresh
        </button>
      </div>

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
                Operator Session Required for Approval
              </p>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                Approval actions are disabled until you authenticate a local session with a 6-digit one-time code.
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

      {(executions || []).length === 0 ? (
        <div
          className="card"
          style={{ textAlign: 'center', padding: '40px 20px', color: 'var(--text-muted)' }}
        >
          <ShieldCheck style={{ width: 36, height: 36, color: 'var(--success)', margin: '0 auto 10px', opacity: 0.8 }} />
          <h3 style={{ fontSize: '1rem', color: 'var(--text-primary)', marginBottom: 4 }}>
            Inbox Clear
          </h3>
          <p style={{ fontSize: '0.85rem' }}>No pending consequential executions awaiting approval.</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {(executions || []).map((item) => {

            const isConfirming = confirmingId === item.id;
            const isBusy = processingId === item.id;

            return (
              <div key={item.id} className="card" style={{ borderLeft: '4px solid var(--warning)' }}>
                <div className="card-header">
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <ShieldAlert style={{ color: 'var(--warning)', width: 18, height: 18 }} />
                    <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>{item.capability_id}</span>
                    <span
                      style={{
                        fontSize: '0.7rem',
                        padding: '1px 6px',
                        borderRadius: 'var(--radius-sm)',
                        backgroundColor: 'rgba(245, 158, 11, 0.15)',
                        color: 'var(--warning)',
                        border: '1px solid rgba(245, 158, 11, 0.3)',
                      }}
                    >
                      Awaiting Approval
                    </span>
                  </div>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                    ID: {item.id}
                  </span>
                </div>

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
                        disabled={isBusy}
                        style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                      >
                        <X style={{ width: 14, height: 14 }} />
                        Reject & Cancel
                      </button>
                      <button
                        className="btn btn-primary"
                        onClick={() => handleApprove(item.id)}
                        disabled={isBusy || !sessionState.paired}
                        style={{ fontSize: '0.8rem', padding: '6px 12px' }}
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
                      disabled={isBusy}
                      style={{ fontSize: '0.8rem' }}
                    >
                      <X style={{ width: 14, height: 14 }} />
                      Reject
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={() => setConfirmingId(item.id)}
                      disabled={isBusy || !sessionState.paired}
                      title={!sessionState.paired ? 'Pair operator session to approve' : 'Approve execution'}
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
      )}
    </div>
  );
};
