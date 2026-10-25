import React, { useState, useEffect } from 'react';
import { Activity, Cpu, Eye, Wrench, RefreshCw, AlertCircle, CheckCircle } from 'lucide-react';
import { api } from '../../api/client';
import { PerceptionStatus, SchedulerMetrics, SystemHealth, ToolCapability } from '../../types';

export const SystemStatus: React.FC = () => {
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
  const [errorNotice, setErrorNotice] = useState<string | null>(null);

  const fetchDiagnostics = async () => {
    setLoading(true);
    setErrorNotice(null);
    try {
      const [h, sStatus, sMetrics, pStatus, caps] = await Promise.allSettled([
        api.getHealth(),
        api.getSchedulerStatus(),
        api.getSchedulerMetrics(),
        api.getPerceptionStatus(),
        api.listCapabilities(),
      ]);

      if (h.status === 'fulfilled') setHealth(h.value);
      if (sStatus.status === 'fulfilled') setSchedulerStatus(sStatus.value);
      if (sMetrics.status === 'fulfilled') setSchedulerMetrics(sMetrics.value);
      if (pStatus.status === 'fulfilled') setPerceptionStatus(pStatus.value);
      if (caps.status === 'fulfilled') setCapabilities(caps.value);
    } catch (err) {
      setErrorNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDiagnostics();
    const interval = setInterval(fetchDiagnostics, 6000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="panel-view">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h2 style={{ fontSize: '1.25rem', marginBottom: 4 }}>System Diagnostics & Subsystems</h2>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Real-time telemetry grounded in backend state across resource scheduling, perception, and registered tools.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={fetchDiagnostics} disabled={loading}>
          <RefreshCw className={loading ? 'animate-spin' : ''} style={{ width: 14, height: 14 }} />
          Refresh
        </button>
      </div>

      {errorNotice && (
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
          <AlertCircle style={{ width: 16, height: 16, flexShrink: 0 }} />
          <span>{errorNotice}</span>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16 }}>
        {/* Card 1: Core System Health */}
        <div className="card">
          <div className="card-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Activity style={{ color: 'var(--success)', width: 18, height: 18 }} />
              <h3 style={{ fontSize: '0.95rem' }}>Core Engine Health</h3>
            </div>
            {health?.status === 'ok' ? (
              <span style={{ color: 'var(--success)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
                <CheckCircle style={{ width: 12, height: 12 }} /> Online
              </span>
            ) : (
              <span style={{ color: 'var(--danger)', display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: '0.75rem' }}>
                <AlertCircle style={{ width: 12, height: 12 }} /> Disconnected
              </span>
            )}
          </div>
          <div style={{ fontSize: '0.825rem', display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-secondary)' }}>
            <div>Environment: <strong style={{ color: 'var(--text-primary)' }}>{health?.environment || 'unknown'}</strong></div>
            <div>Version: <strong style={{ color: 'var(--text-primary)' }}>{health?.version || 'unknown'}</strong></div>
            <div>Timestamp: {health?.timestamp ? new Date(health.timestamp).toLocaleTimeString() : 'N/A'}</div>
          </div>
        </div>

        {/* Card 2: Single-Model Resource Scheduler */}
        <div className="card">
          <div className="card-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Cpu style={{ color: 'var(--primary)', width: 18, height: 18 }} />
              <h3 style={{ fontSize: '0.95rem' }}>Resource Scheduler</h3>
            </div>
            <span style={{ fontSize: '0.75rem', color: schedulerStatus?.enabled ? 'var(--success)' : 'var(--warning)' }}>
              {schedulerStatus?.enabled ? 'Active' : 'Disabled'}
            </span>
          </div>
          <div style={{ fontSize: '0.825rem', display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-secondary)' }}>
            <div>Active Local Leases: <strong style={{ color: 'var(--text-primary)' }}>{schedulerStatus?.active_local ?? 0} / 1</strong></div>
            <div>Active Cloud Leases: <strong style={{ color: 'var(--text-primary)' }}>{schedulerStatus?.active_cloud ?? 0} / 5</strong></div>
            <div>Queue Depth: <strong style={{ color: 'var(--text-primary)' }}>{schedulerStatus?.queue_depth ?? 0}</strong></div>
            <div>Resident Model: <strong style={{ color: 'var(--primary)' }}>{schedulerMetrics?.current_model || 'none'}</strong></div>
            <div>Total Requests Processed: <strong style={{ color: 'var(--text-primary)' }}>{schedulerMetrics?.total_requests_processed ?? 0}</strong></div>
          </div>
        </div>

        {/* Card 3: Perception Subsystem */}
        <div className="card">
          <div className="card-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Eye style={{ color: 'var(--info)', width: 18, height: 18 }} />
              <h3 style={{ fontSize: '0.95rem' }}>Perception Subsystem</h3>
            </div>
            <span style={{ fontSize: '0.75rem', color: perceptionStatus?.enabled ? 'var(--success)' : 'var(--text-muted)' }}>
              {perceptionStatus?.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          <div style={{ fontSize: '0.825rem', display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-secondary)' }}>
            <div>Audio Capture: <strong style={{ color: perceptionStatus?.audio_capture_enabled ? 'var(--success)' : 'var(--text-muted)' }}>{perceptionStatus?.audio_capture_enabled ? 'Available' : 'Disabled'}</strong></div>
            <div>Vision Engine: <strong style={{ color: perceptionStatus?.vision_enabled ? 'var(--success)' : 'var(--text-muted)' }}>{perceptionStatus?.vision_enabled ? 'Available' : 'Disabled'}</strong></div>
            <div>Adapter Mode: <strong style={{ color: 'var(--text-primary)' }}>{perceptionStatus?.adapter_mode || 'unknown'}</strong></div>
            <div>Active Audio Sessions: <strong style={{ color: 'var(--text-primary)' }}>{perceptionStatus?.active_audio_sessions ?? 0}</strong></div>
          </div>
        </div>

        {/* Card 4: Registered Tool Capabilities */}
        <div className="card" style={{ gridColumn: '1 / -1' }}>
          <div className="card-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Wrench style={{ color: 'var(--warning)', width: 18, height: 18 }} />
              <h3 style={{ fontSize: '0.95rem' }}>Registered Capabilities Allowlist ({capabilities.length})</h3>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 10, marginTop: 8 }}>
            {capabilities.map((c) => (
              <div
                key={c.capability_id}
                style={{
                  padding: 10,
                  borderRadius: 'var(--radius-sm)',
                  backgroundColor: 'var(--bg-canvas)',
                  border: '1px solid var(--border-subtle)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 4,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontWeight: 600, fontSize: '0.85rem' }}>{c.name}</span>
                  <span
                    style={{
                      fontSize: '0.65rem',
                      padding: '1px 5px',
                      borderRadius: 'var(--radius-sm)',
                      backgroundColor:
                        c.risk_tier === 'CRITICAL' || c.risk_tier === 'HIGH'
                          ? 'rgba(239, 68, 68, 0.15)'
                          : 'rgba(99, 102, 241, 0.15)',
                      color:
                        c.risk_tier === 'CRITICAL' || c.risk_tier === 'HIGH'
                          ? 'var(--danger)'
                          : 'var(--primary)',
                    }}
                  >
                    {c.risk_tier}
                  </span>
                </div>
                <span style={{ fontSize: '0.725rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                  {c.capability_id}@{c.version}
                </span>
                <p style={{ fontSize: '0.775rem', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                  {c.description}
                </p>
                {c.consequential && (
                  <span style={{ fontSize: '0.7rem', color: 'var(--warning)', fontWeight: 600 }}>
                    • Consequential Action
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
