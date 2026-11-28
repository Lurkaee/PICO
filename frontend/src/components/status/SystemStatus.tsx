import React, { useState, useEffect, useCallback } from 'react';
import { Activity, Cpu, Eye, Wrench, RefreshCw, Clock, Power } from 'lucide-react';
import { isTauri } from '@tauri-apps/api/core';
import { api } from '../../api/client';
import { PerceptionStatus, SchedulerMetrics, SystemHealth, ToolCapability } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

export const SystemStatus: React.FC = () => {
  const { connectivity: _connectivity, isBackendReachable: _isBackendReachable, inferenceDegraded: _inferenceDegraded } = useConnectivity();
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [schedulerStatus, setSchedulerStatus] = useState<{
    enabled: boolean;
    active_local: number;
    active_cloud: number;
    queue_depth: number;
  } | null>(null);
  const [schedulerMetrics, setSchedulerMetrics] = useState<SchedulerMetrics | null>(null);
  const [perceptionStatus, setPerceptionStatus] = useState<PerceptionStatus | null>(null);
  const [capabilities, setCapabilities] = useState<ToolCapability[]>([]);
  const [loading, setLoading] = useState(false);
  const [_errorNotice, setErrorNotice] = useState<string | null>(null);
  const [lastSuccessfulFetch, setLastSuccessfulFetch] = useState<Date | null>(null);
  const [isStale, setIsStale] = useState(false);
  const [autoStartStatus, setAutoStartStatus] = useState<{
    enabled: boolean;
    executable_path: string | null;
    matches_current_exe: boolean;
  } | null>(null);
  const [updatingAutoStart, setUpdatingAutoStart] = useState(false);

  const toggleAutoStart = useCallback(async () => {
    if (!autoStartStatus || updatingAutoStart) return;
    setUpdatingAutoStart(true);
    try {
      const nextEnabled = !autoStartStatus.enabled;
      const res = await api.setAutoStart(nextEnabled);
      setAutoStartStatus(res);
    } catch (err) {
      setErrorNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setUpdatingAutoStart(false);
    }
  }, [autoStartStatus, updatingAutoStart]);

  const fetchDiagnostics = useCallback(async () => {
    setLoading(true);
    setErrorNotice(null);
    try {
      const [h, sStatus, sMetrics, pStatus, caps, aStatus] = await Promise.allSettled([
        api.getHealth(),
        api.getSchedulerStatus(),
        api.getSchedulerMetrics(),
        api.getPerceptionStatus(),
        api.listCapabilities(),
        api.getAutoStartStatus(),
      ]);

      let anySuccess = false;
      if (h.status === 'fulfilled') {
        setHealth(h.value);
        anySuccess = true;
      }
      if (sStatus.status === 'fulfilled') {
        setSchedulerStatus(sStatus.value);
        anySuccess = true;
      }
      if (sMetrics.status === 'fulfilled') {
        setSchedulerMetrics(sMetrics.value);
        anySuccess = true;
      }
      if (pStatus.status === 'fulfilled') {
        setPerceptionStatus(pStatus.value);
        anySuccess = true;
      }
      if (caps.status === 'fulfilled') {
        setCapabilities(caps.value);
        anySuccess = true;
      }
      if (aStatus.status === 'fulfilled') {
        setAutoStartStatus(aStatus.value);
        anySuccess = true;
      }

      if (anySuccess) {
        setLastSuccessfulFetch(new Date());
        setIsStale(false);
      } else {
        setIsStale(true);
      }
    } catch (err) {
      setErrorNotice(err instanceof Error ? err.message : String(err));
      // Preserve existing in-memory values; do not substitute zeroes or clear state
    } finally {
      setLoading(false);
    }
  }, []);

  const handleRefresh = useCallback(() => {
    fetchDiagnostics();
  }, [fetchDiagnostics]);

  useEffect(() => {
    fetchDiagnostics();
    const interval = setInterval(fetchDiagnostics, 30000);
    return () => clearInterval(interval);
  }, [fetchDiagnostics]);

  const getHealthStatusClass = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'healthy') return 'status-indicator--healthy';
    if (s === 'degraded') return 'status-indicator--degraded';
    if (s === 'failed' || s === 'unhealthy') return 'status-indicator--failed';
    return 'status-indicator--unknown';
  };

  const getRiskTierClass = (tier: string) => {
    if (tier === 'CRITICAL' || tier === 'HIGH') return 'capability-card__risk--critical';
    if (tier === 'MEDIUM') return 'capability-card__risk--medium';
    return 'capability-card__risk--low';
  };

  const getHealthOverviewClass = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'healthy') return 'health-overview__value--healthy';
    if (s === 'degraded') return 'health-overview__value--degraded';
    if (s === 'failed') return 'health-overview__value--failed';
    return '';
  };

  const getDiagnosticMetricClass = (healthy: boolean | null) => {
    if (healthy === true) return 'diagnostic-metric__value--healthy';
    if (healthy === false) return 'diagnostic-metric__value--failed';
    return 'diagnostic-metric__value--unknown';
  };

  return (
    <div className="workspace-root" style={{ height: '100%', backgroundColor: 'var(--bg-canvas)' }}>
      <header className="workspace-header">
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 'var(--space-4)', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h1 className="workspace-title">System Status</h1>
            <p className="workspace-description">
              Real-time system health, diagnostics, and capability registry
            </p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
            {isStale && (
              <div className="stale-banner" style={{ marginBottom: 0, padding: 'var(--space-2) var(--space-3)', fontSize: 'var(--text-xs)' }}>
                <div className="stale-banner__text">
                  <Clock style={{ width: 14, height: 14 }} />
                  <span>Stale Data \u2014 last successful fetch: {lastSuccessfulFetch?.toLocaleTimeString() || 'never'}</span>
                </div>
              </div>
            )}
            <button
              className="btn--icon-sm"
              onClick={handleRefresh}
              disabled={loading}
              aria-label="Refresh diagnostics"
              title="Refresh"
            >
              <RefreshCw style={{ width: 18, height: 18 }} />
            </button>
          </div>
        </div>
      </header>

      <main className="workspace-content" role="main">
        <div className="workspace-grid two-column full-width">
          {/* Card 1: Overall System Health */}
          <section className="section-card" aria-labelledby="system-health-title">
            <header className="section-header">
              <h2 id="system-health-title" className="section-title">
                <Activity className="section-icon" style={{ width: 20, height: 20 }} />
                Overall System Health
              </h2>
              <span className={`status-indicator ${getHealthStatusClass(health?.status || 'unknown')}`}>
                <span className="status-indicator__dot" style={{ backgroundColor: 'currentColor' }} />
                {health?.status || 'Unknown'}
              </span>
            </header>
            <div className="health-overview">
              <div className="health-overview__item">
                <div className="health-overview__icon">
                  <Activity style={{ width: 24, height: 24 }} />
                </div>
                <div className={`health-overview__value ${getHealthOverviewClass(health?.status || 'unknown')}`}>
                  {health?.status || '\u2014'}
                </div>
                <div className="health-overview__label">Status</div>
              </div>
              <div className="health-overview__item">
                <div className="health-overview__icon">
                  <Cpu style={{ width: 24, height: 24 }} />
                </div>
                <div className="health-overview__value" style={{ fontFamily: 'var(--font-mono)' }}>
                  {health?.environment || '\u2014'}
                </div>
                <div className="health-overview__label">Environment</div>
              </div>
              <div className="health-overview__item">
                <div className="health-overview__icon">
                  <Clock style={{ width: 24, height: 24 }} />
                </div>
                <div className="health-overview__value" style={{ fontFamily: 'var(--font-mono)' }}>
                  {health?.version || '\u2014'}
                </div>
                <div className="health-overview__label">Version</div>
              </div>
            </div>
            {health?.timestamp && (
              <div style={{ marginTop: 'var(--space-3)', fontSize: 'var(--text-xs)', color: 'var(--text-muted)' }}>
                Last checked: {new Date(health.timestamp).toLocaleString()}
              </div>
            )}
          </section>

          {/* Card 3: Perception & Diagnostics */}
          <section className="section-card" aria-labelledby="perception-title">
            <header className="section-header">
              <h2 id="perception-title" className="section-title">
                <Eye className="section-icon" style={{ width: 20, height: 20 }} />
                Perception & Diagnostics
              </h2>
            </header>
            {perceptionStatus ? (
              <div className="diagnostics-grid">
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Perception Enabled</span>
                  <span className={`diagnostic-metric__value ${getDiagnosticMetricClass(perceptionStatus.enabled)}`}>
                    {perceptionStatus.enabled ? 'Yes' : 'No'}
                  </span>
                </div>
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Audio Capture</span>
                  <span className={`diagnostic-metric__value ${getDiagnosticMetricClass(perceptionStatus.audio_capture_enabled)}`}>
                    {perceptionStatus.audio_capture_enabled ? 'Active' : 'Inactive'}
                  </span>
                </div>
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Vision</span>
                  <span className={`diagnostic-metric__value ${getDiagnosticMetricClass(perceptionStatus.vision_enabled)}`}>
                    {perceptionStatus.vision_enabled ? 'Active' : 'Inactive'}
                  </span>
                </div>
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Motion</span>
                  <span className={`diagnostic-metric__value ${getDiagnosticMetricClass(perceptionStatus.motion_enabled)}`}>
                    {perceptionStatus.motion_enabled ? 'Active' : 'Inactive'}
                  </span>
                </div>
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Environmental</span>
                  <span className={`diagnostic-metric__value ${getDiagnosticMetricClass(perceptionStatus.environmental_enabled)}`}>
                    {perceptionStatus.environmental_enabled ? 'Active' : 'Inactive'}
                  </span>
                </div>
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Adapter Mode</span>
                  <span className="diagnostic-metric__value" style={{ fontFamily: 'var(--font-mono)' }}>
                    {perceptionStatus.adapter_mode}
                  </span>
                </div>
                <div className="diagnostic-metric">
                  <span className="diagnostic-metric__label">Active Audio Sessions</span>
                  <span className="diagnostic-metric__value" style={{ fontFamily: 'var(--font-mono)' }}>
                    {perceptionStatus.active_audio_sessions}
                  </span>
                </div>
              </div>
            ) : (
              <div style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)', padding: 'var(--space-4) 0' }}>
                No perception data available
              </div>
            )}
          </section>

          {/* Card 4: Scheduler Status */}
          <section className="section-card" aria-labelledby="scheduler-title">
            <header className="section-header">
              <h2 id="scheduler-title" className="section-title">
                <Wrench className="section-icon" style={{ width: 20, height: 20 }} />
                Scheduler
              </h2>
            </header>
            {schedulerStatus ? (
              <>
                <div className="scheduler-status">
                  <div className="scheduler-status__item">
                    <span className="scheduler-status__label">Enabled</span>
                    <span className={`scheduler-status__value ${getDiagnosticMetricClass(schedulerStatus.enabled)}`}>
                      {schedulerStatus.enabled ? 'Yes' : 'No'}
                    </span>
                  </div>
                  <div className="scheduler-status__item">
                    <span className="scheduler-status__label">Active Local</span>
                    <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                      {schedulerStatus.active_local}
                    </span>
                  </div>
                  <div className="scheduler-status__item">
                    <span className="scheduler-status__label">Active Cloud</span>
                    <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                      {schedulerStatus.active_cloud}
                    </span>
                  </div>
                  <div className="scheduler-status__item">
                    <span className="scheduler-status__label">Queue Depth</span>
                    <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                      {schedulerStatus.queue_depth}
                    </span>
                  </div>
                </div>
                {schedulerMetrics && (
                  <div style={{ marginTop: 'var(--space-4)' }}>
                    <h3 style={{ fontSize: 'var(--text-sm)', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 'var(--space-2)' }}>
                      Metrics
                    </h3>
                    <div className="scheduler-status">
                      <div className="scheduler-status__item">
                        <span className="scheduler-status__label">Current Model</span>
                        <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                          {schedulerMetrics.current_model || '\u2014'}
                        </span>
                      </div>
                      <div className="scheduler-status__item">
                        <span className="scheduler-status__label">Processed</span>
                        <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                          {schedulerMetrics.total_requests_processed}
                        </span>
                      </div>
                      <div className="scheduler-status__item">
                        <span className="scheduler-status__label">Cancelled</span>
                        <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                          {schedulerMetrics.total_cancelled}
                        </span>
                      </div>
                      <div className="scheduler-status__item">
                        <span className="scheduler-status__label">Timeouts</span>
                        <span className="scheduler-status__value" style={{ fontFamily: 'var(--font-mono)' }}>
                          {schedulerMetrics.total_timeouts}
                        </span>
                      </div>
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)', padding: 'var(--space-4) 0' }}>
                No scheduler data available
              </div>
            )}
          </section>

          {/* Card 5: Auto-Start (Tauri only) */}
          {isTauri() && (
            <section className="section-card" aria-labelledby="autostart-title">
              <header className="section-header">
                <h2 id="autostart-title" className="section-title">
                  <Power className="section-icon" style={{ width: 20, height: 20 }} />
                  Auto-Start
                </h2>
              </header>
              {autoStartStatus ? (
                <div className="auto-start-card">
                  <div className="auto-start-card__status">
                    <div className="auto-start-card__info">
                      <span className="auto-start-card__label">
                        Status: {autoStartStatus.enabled ? 'Enabled' : 'Disabled'}
                      </span>
                      {autoStartStatus.executable_path && (
                        <span className="auto-start-card__path">
                          Registered: {autoStartStatus.executable_path}
                        </span>
                      )}
                      {autoStartStatus.matches_current_exe === false && autoStartStatus.executable_path && (
                        <span style={{ fontSize: 'var(--text-xs)', color: 'var(--warning)', fontWeight: 600 }}>
                          \u26a0 Registered path differs from current executable
                        </span>
                      )}
                    </div>
                    <div className="auto-start-card__action">
                      <button
                        className={`btn--sm ${autoStartStatus.enabled ? 'btn--danger-sm' : 'btn--primary-sm'}`}
                        onClick={toggleAutoStart}
                        disabled={updatingAutoStart}
                      >
                        {updatingAutoStart
                          ? 'Updating\u2026'
                          : autoStartStatus.enabled
                          ? 'Disable Startup'
                          : 'Enable Startup'}
                      </button>
                    </div>
                  </div>
                </div>
              ) : (
                <div style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)', padding: 'var(--space-4) 0' }}>
                  Loading auto-start status\u2026
                </div>
              )}
            </section>
          )}

          {/* Card 6: Registered Tool Capabilities */}
          <section className="section-card" aria-labelledby="capabilities-title">
            <header className="section-header">
              <h2 id="capabilities-title" className="section-title">
                <Wrench className="section-icon" style={{ width: 20, height: 20, color: 'var(--warning)' }} />
                Registered Capabilities Allowlist
              </h2>
              <span className="section-count" aria-label="Capabilities count">
                {capabilities.length}
              </span>
            </header>
            <div className="capabilities-grid">
              {capabilities.map((c) => (
                <article key={c.capability_id} className="capability-card">
                  <header className="capability-card__header">
                    <span className="capability-card__name">{c.name}</span>
                    <span className={`capability-card__risk ${getRiskTierClass(c.risk_tier)}`}>
                      {c.risk_tier}
                    </span>
                  </header>
                  <span className="capability-card__id" style={{ fontFamily: 'var(--font-mono)' }}>
                    {c.capability_id}@{c.version}
                  </span>
                  <p className="capability-card__description">{c.description}</p>
                  {c.consequential && (
                    <span className="capability-card__consequential">\u2022 Consequential Action</span>
                  )}
                </article>
              ))}
              {capabilities.length === 0 && (
                <div style={{ gridColumn: '1 / -1', textAlign: 'center', color: 'var(--text-muted)', padding: 'var(--space-8) var(--space-4)' }}>
                  No capabilities registered
                </div>
              )}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
};
