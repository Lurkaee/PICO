import React, { useState, useEffect } from 'react';
import { GitBranch, Play, StopCircle, RefreshCw, CheckCircle2, XCircle, Clock, AlertTriangle, ShieldCheck, HelpCircle } from 'lucide-react';
import { api } from '../../api/client';
import { StepVerificationRecord, WorkflowRun, WorkflowStep } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

export const WorkflowTimeline: React.FC = () => {
  const { isBackendReachable } = useConnectivity();
  const [workflows, setWorkflows] = useState<WorkflowRun[]>([]);
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowRun | null>(null);
  const [verifications, setVerifications] = useState<StepVerificationRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [processing, setProcessing] = useState(false);
  const [lastSuccessfulFetch, setLastSuccessfulFetch] = useState<Date | null>(null);
  const [isStale, setIsStale] = useState(false);

  const fetchWorkflows = async () => {
    setLoading(true);
    setActionError(null);
    try {
      const runs = await api.listWorkflows(20, 0);
      setWorkflows(runs || []);
      setLastSuccessfulFetch(new Date());
      setIsStale(false);
      setSelectedWorkflow((curr) => {
        if (!curr && runs && runs.length > 0) return runs[0];
        if (curr && runs) {
          const match = runs.find((w) => w.id === curr.id);
          if (match) return match;
        }
        return curr;
      });
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      // Retain in-memory view during transient disconnection; do not wipe array!
      setIsStale(true);
    } finally {
      setLoading(false);
    }
  };


  useEffect(() => {
    fetchWorkflows();
    const interval = setInterval(fetchWorkflows, 8000);
    return () => clearInterval(interval);
  }, []);

  // Fetch verifications for selected workflow
  useEffect(() => {
    if (!selectedWorkflow) {
      setVerifications([]);
      return;
    }
    let isMounted = true;
    api
      .getWorkflowVerifications(selectedWorkflow.id)
      .then((records) => {
        if (isMounted) setVerifications(records);
      })
      .catch(() => {
        if (isMounted) setVerifications([]);
      });
    return () => {
      isMounted = false;
    };
  }, [selectedWorkflow]);

  const handleAdvance = async () => {
    if (!selectedWorkflow) return;
    if (!isBackendReachable) {
      setActionError('Cannot advance workflow while backend is offline. Automatic retry is prevented.');
      return;
    }
    setProcessing(true);
    setActionError(null);
    try {
      const advanced = await api.advanceWorkflow(selectedWorkflow.id);
      setSelectedWorkflow(advanced);
      await fetchWorkflows();
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setActionError('Outcome unknown: network interrupted during workflow advance. Please refresh state to inspect before repeating.');
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessing(false);
    }
  };

  const handleCancel = async () => {
    if (!selectedWorkflow) return;
    if (!isBackendReachable) {
      setActionError('Cannot cancel workflow while backend is offline. Automatic retry is prevented.');
      return;
    }
    setProcessing(true);
    setActionError(null);
    try {
      const cancelled = await api.cancelWorkflow(selectedWorkflow.id, 'Cancelled by operator via workflow UI');
      setSelectedWorkflow(cancelled);
      await fetchWorkflows();
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setActionError('Outcome unknown: network interrupted during workflow cancellation. Please refresh state to inspect before repeating.');
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessing(false);
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'completed':
      case 'succeeded':
        return (
          <span style={{ color: 'var(--success)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <CheckCircle2 style={{ width: 14, height: 14 }} /> Completed
          </span>
        );
      case 'running':
        return (
          <span style={{ color: 'var(--primary)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <RefreshCw className="animate-spin" style={{ width: 14, height: 14 }} /> Running
          </span>
        );
      case 'awaiting_approval':
        return (
          <span style={{ color: 'var(--warning)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <Clock style={{ width: 14, height: 14 }} /> Awaiting Approval
          </span>
        );
      case 'failed':
        return (
          <span style={{ color: 'var(--danger)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <XCircle style={{ width: 14, height: 14 }} /> Failed
          </span>
        );
      case 'cancelled':
        return (
          <span style={{ color: 'var(--text-muted)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <StopCircle style={{ width: 14, height: 14 }} /> Cancelled
          </span>
        );
      case 'blocked':
        return (
          <span style={{ color: 'var(--danger)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <AlertTriangle style={{ width: 14, height: 14 }} /> Blocked
          </span>
        );
      default:
        return (
          <span style={{ color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
            <Clock style={{ width: 14, height: 14 }} /> {status}
          </span>
        );
    }
  };

  return (
    <div className="panel-view" style={{ maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h2 style={{ fontSize: '1.25rem', marginBottom: 4 }}>Workflows & Independent Verification</h2>
            {isStale && lastSuccessfulFetch && (
              <span className="stale-badge" title="Data retained in-memory during offline disconnection">
                <Clock style={{ width: 12, height: 12 }} /> Stale as of {lastSuccessfulFetch.toLocaleTimeString()}
              </span>
            )}
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Deterministic DAG coordination with evidence auditing before dependent step execution.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={fetchWorkflows} disabled={loading}>
          <RefreshCw className={loading ? 'animate-spin' : ''} style={{ width: 14, height: 14 }} />
          Refresh
        </button>
      </div>

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

      <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 20 }}>
        {/* Left Column: Workflows List */}
        <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <h3 style={{ fontSize: '0.95rem', color: 'var(--text-secondary)', borderBottom: '1px solid var(--border-subtle)', paddingBottom: 6 }}>
            Workflow Runs
          </h3>
          {(workflows || []).length === 0 ? (
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', padding: 12 }}>No workflows recorded.</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, overflowY: 'auto', maxHeight: 600 }}>
              {(workflows || []).map((w) => (

                <button
                  key={w.id}
                  onClick={() => setSelectedWorkflow(w)}
                  style={{
                    padding: '10px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid',
                    borderColor: selectedWorkflow?.id === w.id ? 'var(--border-focus)' : 'var(--border-subtle)',
                    backgroundColor: selectedWorkflow?.id === w.id ? 'var(--bg-surface-elevated)' : 'transparent',
                    color: 'var(--text-primary)',
                    textAlign: 'left',
                    cursor: 'pointer',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 4,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontWeight: 600, fontSize: '0.875rem' }}>{w.name}</span>
                    {getStatusBadge(w.status)}
                  </div>
                  <span style={{ fontSize: '0.725rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                    ID: {w.id}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Right Column: Selected Workflow Details and Timeline */}
        {selectedWorkflow ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div className="card">
              <div className="card-header">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <GitBranch style={{ color: 'var(--primary)', width: 22, height: 22 }} />
                  <div>
                    <h3 style={{ fontSize: '1.1rem' }}>{selectedWorkflow.name}</h3>
                    <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                      Task ID: {selectedWorkflow.task_id || 'unbound'} • Created: {new Date(selectedWorkflow.created_at).toLocaleString()}
                    </span>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  {getStatusBadge(selectedWorkflow.status)}
                  {['pending', 'running'].includes(selectedWorkflow.status) && (
                    <>
                      <button
                        className="btn btn-primary"
                        onClick={handleAdvance}
                        disabled={processing || !isBackendReachable}
                        style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                        title={!isBackendReachable ? 'Controls locked: backend is offline' : 'Advance workflow step'}
                      >
                        <Play style={{ width: 14, height: 14 }} />
                        Advance Step
                      </button>
                      <button
                        className="btn btn-secondary"
                        onClick={handleCancel}
                        disabled={processing || !isBackendReachable}
                        style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                        title={!isBackendReachable ? 'Controls locked: backend is offline' : 'Cancel workflow run'}
                      >
                        <StopCircle style={{ width: 14, height: 14 }} />
                        Cancel
                      </button>
                    </>
                  )}
                </div>
              </div>

              {/* Step Timeline */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 12 }}>
                <h4 style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>Execution DAG & Verification Stages</h4>

                {(() => {
                  let stepList: WorkflowStep[] = [];
                  if (Array.isArray(selectedWorkflow.steps)) {
                    stepList = selectedWorkflow.steps.map((s: WorkflowStep) => ({
                      ...s,
                      step_key: s.step_key,
                      capability_id: s.capability_id,
                      capability_version: s.capability_version,
                      status: s.status,
                      dependencies: s.dependencies || [],
                      output: s.output,
                      error: s.error_message || s.error,
                      execution_id: s.tool_execution_id || s.execution_id,
                      verification_status: s.verification?.status as ('pending' | 'passed' | 'failed' | 'review_required' | null) || s.verification_status || null,
                    }));
                  } else if (selectedWorkflow.steps) {
                    const stepOrder = selectedWorkflow.step_order || Object.keys(selectedWorkflow.steps);
                    const stepRecord = selectedWorkflow.steps as Record<string, WorkflowStep>;
                    stepList = stepOrder.map((key) => stepRecord[key]).filter(Boolean);
                  }

                  return stepList.map((step, index) => {
                    const stepKey = step.step_key;
                    const stepVerifications = verifications.filter(
                      (v) => (v.step_key && v.step_key === stepKey) || (step.id && v.workflow_step_id === step.id)
                    );

                    return (
                      <div
                        key={stepKey || index}
                        style={{
                          padding: 14,
                          borderRadius: 'var(--radius-md)',
                          backgroundColor: 'var(--bg-canvas)',
                          border: '1px solid var(--border-subtle)',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 8,
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <span
                              style={{
                                width: 24,
                                height: 24,
                                borderRadius: 'var(--radius-full)',
                                backgroundColor: 'var(--bg-surface-elevated)',
                                border: '1px solid var(--border-default)',
                                display: 'inline-flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                fontSize: '0.75rem',
                                fontWeight: 700,
                              }}
                            >
                              {index + 1}
                            </span>
                            <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>{stepKey}</span>
                            <span
                              style={{
                                fontSize: '0.75rem',
                                color: 'var(--text-muted)',
                                fontFamily: 'var(--font-mono)',
                              }}
                            >
                              ({step.capability_id}@{step.capability_version})
                            </span>
                          </div>
                          {getStatusBadge(step.status)}
                        </div>

                        {step.dependencies && step.dependencies.length > 0 && (
                          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                            Depends on: <code>{step.dependencies.join(', ')}</code>
                          </div>
                        )}

                        {/* Verification Audit Display */}
                        <div
                          style={{
                            padding: '6px 10px',
                            borderRadius: 'var(--radius-sm)',
                            backgroundColor: 'var(--bg-surface-elevated)',
                            fontSize: '0.75rem',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <ShieldCheck style={{ width: 14, height: 14, color: 'var(--primary)' }} />
                            <span>Verification Gate:</span>
                            {step.verification_status === 'passed' ? (
                              <strong style={{ color: 'var(--success)' }}>PASSED (Evidence Accepted)</strong>
                            ) : step.verification_status === 'failed' ? (
                              <strong style={{ color: 'var(--danger)' }}>FAILED (Downstream Blocked)</strong>
                            ) : step.verification_status === 'review_required' ? (
                              <strong style={{ color: 'var(--warning)' }}>REVIEW REQUIRED (Inconclusive)</strong>
                            ) : (
                              <span style={{ color: 'var(--text-muted)' }}>Pending Step Completion</span>
                            )}
                          </div>

                          {(stepVerifications.length > 0 || step.verification?.stage) && (
                            <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                              {stepVerifications[0]?.stage || step.verification?.stage || 'Stage A'}
                            </span>
                          )}
                        </div>

                        {(step.error || step.error_message) && (
                          <div style={{ color: 'var(--danger)', fontSize: '0.75rem', padding: '4px 8px' }}>
                            Error: {step.error || step.error_message}
                          </div>
                        )}
                      </div>
                    );
                  });
                })()}
              </div>
            </div>
          </div>
        ) : (
          <div className="card" style={{ textAlign: 'center', padding: 40, color: 'var(--text-muted)' }}>
            <HelpCircle style={{ width: 36, height: 36, margin: '0 auto 10px', opacity: 0.8 }} />
            <p>Select a workflow on the left to inspect its timeline and verification gates.</p>
          </div>
        )}
      </div>
    </div>
  );
};
