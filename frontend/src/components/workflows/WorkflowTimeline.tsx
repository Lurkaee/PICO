import React, { useState, useEffect } from 'react';
import { GitBranch, Play, StopCircle, RefreshCw, CheckCircle2, XCircle, Clock, AlertTriangle, ShieldCheck, HelpCircle } from 'lucide-react';
import { api } from '../../api/client';
import { StepVerificationRecord, WorkflowCompensation, WorkflowRun, WorkflowStep } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

function extractStepStatuses(run: WorkflowRun | null): Record<string, string> {
  if (!run || !run.steps) return {};
  const res: Record<string, string> = {};
  if (Array.isArray(run.steps)) {
    for (const s of run.steps) {
      if (s.step_key) {
        res[s.step_key] = s.status;
      }
    }
  } else {
    for (const [k, v] of Object.entries(run.steps)) {
      if (v && v.status) {
        res[k] = v.status;
      }
    }
  }
  return res;
}

export const WorkflowTimeline: React.FC = () => {
  const { isBackendReachable } = useConnectivity();
  const [workflows, setWorkflows] = useState<WorkflowRun[]>([]);
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowRun | null>(null);
  const [verifications, setVerifications] = useState<StepVerificationRecord[]>([]);
  const [isVerificationsStale, setIsVerificationsStale] = useState(false);
  const [lastVerificationsFetch, setLastVerificationsFetch] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [processing, setProcessing] = useState(false);
  const [reconcilingWorkflowId, setReconcilingWorkflowId] = useState<string | null>(null);
  const [ambiguousWorkflows, setAmbiguousWorkflows] = useState<
    Record<
      string,
      {
        reason: string;
        timestamp: string;
        action: 'advance' | 'cancel' | 'compensate';
        previousStatus: string;
        previousCompletedSteps: number;
        previousStepStatuses: Record<string, string>;
      }
    >
  >({});
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
      setIsVerificationsStale(false);
      return;
    }
    let isMounted = true;
    api
      .getWorkflowVerifications(selectedWorkflow.id)
      .then((records) => {
        if (isMounted) {
          setVerifications(records);
          setIsVerificationsStale(false);
          setLastVerificationsFetch(new Date());
        }
      })
      .catch(() => {
        if (isMounted) {
          // Preserve last-known in-memory verification records; do NOT wipe array
          setIsVerificationsStale(true);
        }
      });
    return () => {
      isMounted = false;
    };
  }, [selectedWorkflow?.id]);

  const reconcileWorkflow = async (workflowId: string) => {
    setReconcilingWorkflowId(workflowId);
    setActionError(null);
    const ambiguousEntry = ambiguousWorkflows[workflowId];

    try {
      const run = await api.getWorkflow(workflowId);
      setSelectedWorkflow(run);
      setWorkflows((prev) => prev.map((w) => (w.id === run.id ? run : w)));

      if (!ambiguousEntry) {
        setAmbiguousWorkflows((prev) => {
          const next = { ...prev };
          delete next[workflowId];
          return next;
        });
        return;
      }

      const { action, previousStatus, previousCompletedSteps, previousStepStatuses } = ambiguousEntry;

      if (action === 'cancel') {
        if (run.status === 'cancelled') {
          setAmbiguousWorkflows((prev) => {
            const next = { ...prev };
            delete next[workflowId];
            return next;
          });
          setActionError(`Reconciliation confirmed: workflow ${workflowId} cancellation confirmed. Controls unlocked.`);
        } else if (['completed', 'succeeded', 'failed'].includes(run.status)) {
          setAmbiguousWorkflows((prev) => {
            const next = { ...prev };
            delete next[workflowId];
            return next;
          });
          setActionError(`Reconciliation confirmed: workflow ${workflowId} resolved with status ${run.status}. Controls unlocked.`);
        } else {
          setActionError(
            `Reconciliation inconclusive: workflow ${workflowId} status is still '${run.status}' (cancellation unconfirmed). Controls remain locked.`
          );
        }
      } else if (action === 'advance') {
        const currentSteps = extractStepStatuses(run);
        const stepsProgressed = Object.keys(currentSteps).some(
          (k) => previousStepStatuses[k] && previousStepStatuses[k] !== currentSteps[k]
        );
        const countProgressed = (run.completed_steps || 0) > (previousCompletedSteps || 0);
        const transitioned =
          previousStatus !== run.status &&
          ['completed', 'succeeded', 'failed', 'cancelled', 'awaiting_approval', 'review_required'].includes(run.status);

        if (stepsProgressed || countProgressed || transitioned) {
          setAmbiguousWorkflows((prev) => {
            const next = { ...prev };
            delete next[workflowId];
            return next;
          });
          setActionError(`Reconciliation confirmed: workflow ${workflowId} advancement confirmed (status: ${run.status}). Controls unlocked.`);
        } else {
          setActionError(
            `Reconciliation inconclusive: workflow ${workflowId} remains in status '${run.status}' without step transition. Advancement unconfirmed; controls remain locked.`
          );
        }
      } else if (action === 'compensate') {
        let compensations: WorkflowCompensation[] = [];
        try {
          compensations = await api.getWorkflowCompensations(workflowId);
        } catch {
          // Compensations list fetch failed
        }

        const hasExecutedComp = compensations.some((c) =>
          ['succeeded', 'failed', 'running', 'awaiting_approval', 'uncertain', 'blocked'].includes(c.status)
        );

        if (hasExecutedComp) {
          const compSummary = compensations.map((c) => `${c.compensation_capability_id}: ${c.status}`).join(', ');
          setAmbiguousWorkflows((prev) => {
            const next = { ...prev };
            delete next[workflowId];
            return next;
          });
          setActionError(`Reconciliation confirmed: compensation records verified on backend (${compSummary}). Controls unlocked.`);
        } else if (compensations.length === 0) {
          setAmbiguousWorkflows((prev) => {
            const next = { ...prev };
            delete next[workflowId];
            return next;
          });
          setActionError(`Reconciliation confirmed: no compensation records registered for workflow ${workflowId}. Controls unlocked.`);
        } else {
          setActionError(
            `Reconciliation inconclusive: workflow ${workflowId} compensation actions remain in 'registered' status without execution. Controls remain locked.`
          );
        }
      }
    } catch (err) {
      setActionError(
        `Reconciliation inconclusive: could not verify workflow state (${err instanceof Error ? err.message : String(err)}). Controls remain locked.`
      );
    } finally {
      setReconcilingWorkflowId(null);
    }
  };

  const handleAdvance = async () => {
    if (!selectedWorkflow) return;
    if (!isBackendReachable) {
      setActionError('Cannot advance workflow while backend is offline. Automatic retry is prevented.');
      return;
    }
    if (ambiguousWorkflows[selectedWorkflow.id]) {
      setActionError('Workflow outcome is unconfirmed. Reconcile with backend before retrying.');
      return;
    }
    setProcessing(true);
    setActionError(null);
    const prevStatus = selectedWorkflow.status;
    const prevCompleted = selectedWorkflow.completed_steps || 0;
    const prevSteps = extractStepStatuses(selectedWorkflow);
    try {
      const advanced = await api.advanceWorkflow(selectedWorkflow.id);
      setSelectedWorkflow(advanced);
      await fetchWorkflows();
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousWorkflows((prev) => ({
          ...prev,
          [selectedWorkflow.id]: {
            reason: 'Network interrupted during workflow advance; outcome is unconfirmed.',
            timestamp: new Date().toLocaleTimeString(),
            action: 'advance',
            previousStatus: prevStatus,
            previousCompletedSteps: prevCompleted,
            previousStepStatuses: prevSteps,
          },
        }));
        setActionError('Outcome unknown: network interrupted during workflow advance. Controls locked until authoritative reconciliation.');
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
    if (ambiguousWorkflows[selectedWorkflow.id]) {
      setActionError('Workflow outcome is unconfirmed. Reconcile with backend before retrying.');
      return;
    }
    setProcessing(true);
    setActionError(null);
    const prevStatus = selectedWorkflow.status;
    const prevCompleted = selectedWorkflow.completed_steps || 0;
    const prevSteps = extractStepStatuses(selectedWorkflow);
    try {
      const cancelled = await api.cancelWorkflow(selectedWorkflow.id, 'Cancelled by operator via workflow UI');
      setSelectedWorkflow(cancelled);
      await fetchWorkflows();
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousWorkflows((prev) => ({
          ...prev,
          [selectedWorkflow.id]: {
            reason: 'Network interrupted during workflow cancellation; outcome is unconfirmed.',
            timestamp: new Date().toLocaleTimeString(),
            action: 'cancel',
            previousStatus: prevStatus,
            previousCompletedSteps: prevCompleted,
            previousStepStatuses: prevSteps,
          },
        }));
        setActionError('Outcome unknown: network interrupted during workflow cancellation. Controls locked until authoritative reconciliation.');
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessing(false);
    }
  };

  const handleCompensate = async () => {
    if (!selectedWorkflow) return;
    if (!isBackendReachable) {
      setActionError('Cannot compensate workflow while backend is offline. Automatic retry is prevented.');
      return;
    }
    if (ambiguousWorkflows[selectedWorkflow.id]) {
      setActionError('Workflow outcome is unconfirmed. Reconcile with backend before retrying.');
      return;
    }
    setProcessing(true);
    setActionError(null);
    const prevStatus = selectedWorkflow.status;
    const prevCompleted = selectedWorkflow.completed_steps || 0;
    const prevSteps = extractStepStatuses(selectedWorkflow);
    try {
      const compensated = await api.compensateWorkflow(selectedWorkflow.id, 'Compensated by operator via workflow UI');
      setSelectedWorkflow(compensated);
      await fetchWorkflows();
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousWorkflows((prev) => ({
          ...prev,
          [selectedWorkflow.id]: {
            reason: 'Network interrupted during workflow compensation; outcome is unconfirmed.',
            timestamp: new Date().toLocaleTimeString(),
            action: 'compensate',
            previousStatus: prevStatus,
            previousCompletedSteps: prevCompleted,
            previousStepStatuses: prevSteps,
          },
        }));
        setActionError('Outcome unknown: network interrupted during workflow compensation. Controls locked until authoritative reconciliation.');
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
                        disabled={processing || Boolean(ambiguousWorkflows[selectedWorkflow.id]) || !isBackendReachable}
                        style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                        title={
                          ambiguousWorkflows[selectedWorkflow.id]
                            ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                            : !isBackendReachable
                            ? 'Controls locked: backend is offline'
                            : 'Advance workflow step'
                        }
                      >
                        <Play style={{ width: 14, height: 14 }} />
                        Advance Step
                      </button>
                      <button
                        className="btn btn-secondary"
                        onClick={handleCancel}
                        disabled={processing || Boolean(ambiguousWorkflows[selectedWorkflow.id]) || !isBackendReachable}
                        style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                        title={
                          ambiguousWorkflows[selectedWorkflow.id]
                            ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                            : !isBackendReachable
                            ? 'Controls locked: backend is offline'
                            : 'Cancel workflow run'
                        }
                      >
                        <StopCircle style={{ width: 14, height: 14 }} />
                        Cancel
                      </button>
                    </>
                  )}
                  {['completed', 'succeeded', 'failed', 'cancelled'].includes(selectedWorkflow.status) && (
                    <button
                      className="btn btn-secondary"
                      onClick={handleCompensate}
                      disabled={processing || Boolean(ambiguousWorkflows[selectedWorkflow.id]) || !isBackendReachable}
                      style={{ fontSize: '0.8rem', padding: '6px 12px' }}
                      title={
                        ambiguousWorkflows[selectedWorkflow.id]
                          ? 'Controls locked: outcome is unconfirmed. Reconcile with backend first.'
                          : !isBackendReachable
                          ? 'Controls locked: backend is offline'
                          : 'Compensate workflow'
                      }
                    >
                      <RefreshCw style={{ width: 14, height: 14 }} />
                      Compensate
                    </button>
                  )}
                </div>
              </div>

              {ambiguousWorkflows[selectedWorkflow.id] && (
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
                    margin: '12px 0 6px',
                  }}
                  role="alert"
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />
                    <span>
                      <strong>Outcome unknown:</strong> {ambiguousWorkflows[selectedWorkflow.id].reason} Actions locked to prevent duplicate execution.
                    </span>
                  </div>
                  <button
                    className="btn btn-secondary"
                    onClick={() => reconcileWorkflow(selectedWorkflow.id)}
                    disabled={reconcilingWorkflowId === selectedWorkflow.id || !isBackendReachable}
                    style={{ padding: '4px 10px', fontSize: '0.75rem', whiteSpace: 'nowrap' }}
                    title="Reconcile authoritative workflow state from backend"
                  >
                    <RefreshCw className={reconcilingWorkflowId === selectedWorkflow.id ? 'animate-spin' : ''} style={{ width: 12, height: 12 }} />
                    Reconcile
                  </button>
                </div>
              )}

              {/* Step Timeline */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <h4 style={{ fontSize: '0.875rem', color: 'var(--text-secondary)' }}>Execution DAG & Verification Stages</h4>
                  {isVerificationsStale && lastVerificationsFetch && (
                    <span className="stale-badge" title="Verification records retained in-memory during offline disconnection">
                      <Clock style={{ width: 12, height: 12 }} /> Stale verifications as of {lastVerificationsFetch.toLocaleTimeString()}
                    </span>
                  )}
                </div>

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
