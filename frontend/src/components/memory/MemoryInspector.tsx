import React, { useState, useEffect } from 'react';
import { Search, Trash2, RefreshCw, AlertCircle, CheckCircle, Info, Clock } from 'lucide-react';
import { api } from '../../api/client';
import { ProceduralMemoryItem, SemanticFact, WorkingMemoryItem } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

export const MemoryInspector: React.FC = () => {
  const { isBackendReachable } = useConnectivity();
  const [activeTab, setActiveTab] = useState<'semantic' | 'working' | 'procedural' | 'status'>('semantic');
  const [semanticFacts, setSemanticFacts] = useState<SemanticFact[]>([]);
  const [workingItems, setWorkingItems] = useState<WorkingMemoryItem[]>([]);
  const [procedures, setProcedures] = useState<ProceduralMemoryItem[]>([]);
  const [memoryStatus, setMemoryStatus] = useState<Record<string, unknown> | null>(null);

  const [searchQuery, setSearchQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  const [confirmForgetFactId, setConfirmForgetFactId] = useState<string | null>(null);
  const [lastSuccessfulFetch, setLastSuccessfulFetch] = useState<Date | null>(null);
  const [isStale, setIsStale] = useState(false);

  const loadData = async () => {
    setLoading(true);
    setErrorNotice(null);
    try {
      if (activeTab === 'semantic') {
        if (searchQuery.trim()) {
          const results = await api.searchSemanticFacts(searchQuery.trim(), 20);
          setSemanticFacts(results);
        } else {
          const items = await api.getSemanticFacts(50, 0);
          setSemanticFacts(items);
        }
      } else if (activeTab === 'working') {
        const items = await api.getWorkingMemory('default');
        setWorkingItems(items);
      } else if (activeTab === 'procedural') {
        const items = await api.getProceduralMemory(50, 0);
        setProcedures(items);
      } else if (activeTab === 'status') {
        const s = await api.getMemoryStatus();
        setMemoryStatus(s);
      }
      setLastSuccessfulFetch(new Date());
      setIsStale(false);
    } catch (err) {
      setErrorNotice(err instanceof Error ? err.message : String(err));
      // Retain in-memory records during transient failure
      setIsStale(true);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [activeTab]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    loadData();
  };

  const handleForgetFact = async (factId: string) => {
    if (!isBackendReachable) {
      setErrorNotice('Cannot forget memory while offline. Deletion requests are not queued offline.');
      return;
    }
    setLoading(true);
    setErrorNotice(null);
    try {
      const res = await api.forgetSemanticFact(factId);
      if (res.forgotten) {
        setConfirmForgetFactId(null);
        await loadData();
      } else {
        setErrorNotice('Fact could not be forgotten or does not exist.');
      }
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setErrorNotice('Outcome unknown: network interrupted during memory deletion. Please refresh state to inspect.');
      } else {
        setErrorNotice(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="panel-view">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h2 style={{ fontSize: '1.25rem', marginBottom: 4 }}>Memory Subsystem Inspector</h2>
            {isStale && lastSuccessfulFetch && (
              <span className="stale-badge" title="Data retained in-memory during offline disconnection">
                <Clock style={{ width: 12, height: 12 }} /> Stale as of {lastSuccessfulFetch.toLocaleTimeString()}
              </span>
            )}
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Inspect synthesized working, episodic, semantic, and procedural knowledge with provenance.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={loadData} disabled={loading}>
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

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 8, borderBottom: '1px solid var(--border-subtle)', paddingBottom: 8 }}>
        <button
          className={`btn ${activeTab === 'semantic' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('semantic')}
          style={{ fontSize: '0.825rem' }}
        >
          Semantic Facts
        </button>
        <button
          className={`btn ${activeTab === 'working' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('working')}
          style={{ fontSize: '0.825rem' }}
        >
          Working Memory
        </button>
        <button
          className={`btn ${activeTab === 'procedural' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('procedural')}
          style={{ fontSize: '0.825rem' }}
        >
          Procedural Skills
        </button>
        <button
          className={`btn ${activeTab === 'status' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('status')}
          style={{ fontSize: '0.825rem' }}
        >
          Subsystem Status
        </button>
      </div>

      {/* Tab 1: Semantic Facts */}
      {activeTab === 'semantic' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <form onSubmit={handleSearchSubmit} style={{ display: 'flex', gap: 8 }}>
            <div style={{ position: 'relative', flex: 1 }}>
              <input
                type="text"
                placeholder="Search semantic memory facts via FTS5 index..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                style={{
                  width: '100%',
                  padding: '8px 12px 8px 36px',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-default)',
                  backgroundColor: 'var(--bg-surface-elevated)',
                  color: 'var(--text-primary)',
                  fontSize: '0.875rem',
                }}
              />
              <Search
                style={{
                  position: 'absolute',
                  left: 10,
                  top: '50%',
                  transform: 'translateY(-50%)',
                  width: 16,
                  height: 16,
                  color: 'var(--text-muted)',
                }}
              />
            </div>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              Search
            </button>
            {searchQuery && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => {
                  setSearchQuery('');
                  loadData();
                }}
              >
                Clear
              </button>
            )}
          </form>

          {semanticFacts.length === 0 ? (
            <div className="card" style={{ textAlign: 'center', padding: 30, color: 'var(--text-muted)' }}>
              No semantic facts found matching the query.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {semanticFacts.map((fact) => (
                <div key={fact.id} className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>{fact.subject}</span>
                        <span style={{ color: 'var(--primary)', fontStyle: 'italic', fontSize: '0.85rem' }}>
                          {fact.predicate}
                        </span>
                        <span style={{ color: 'var(--text-primary)', fontSize: '0.95rem' }}>{fact.object}</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                        <span className={`classification-badge ${fact.data_classification}`}>
                          {fact.data_classification}
                        </span>
                        <span>•</span>
                        <span>Provenance: {fact.source_provenance}</span>
                        <span>•</span>
                        <span>Confidence: {(fact.confidence * 100).toFixed(0)}%</span>
                        <span>•</span>
                        {fact.is_inferred ? (
                          <span style={{ color: 'var(--warning)', display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                            <Info style={{ width: 12, height: 12 }} /> Inferred Hypothesis
                          </span>
                        ) : (
                          <span style={{ color: 'var(--success)', display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                            <CheckCircle style={{ width: 12, height: 12 }} /> Observed Ground Truth
                          </span>
                        )}
                      </div>
                    </div>

                    {confirmForgetFactId === fact.id ? (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.75rem', color: 'var(--warning)' }}>Confirm forget?</span>
                        <button
                          className="btn btn-danger"
                          onClick={() => handleForgetFact(fact.id)}
                          disabled={loading || !isBackendReachable}
                          title={!isBackendReachable ? 'Controls locked: backend offline' : 'Forget fact'}
                          style={{ padding: '4px 8px', fontSize: '0.75rem' }}
                        >
                          Forget
                        </button>
                        <button
                          className="btn btn-secondary"
                          onClick={() => setConfirmForgetFactId(null)}
                          style={{ padding: '4px 8px', fontSize: '0.75rem' }}
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        className="btn-icon"
                        onClick={() => setConfirmForgetFactId(fact.id)}
                        disabled={loading || !isBackendReachable}
                        title={!isBackendReachable ? 'Cannot delete facts while offline' : 'Forget this memory fact'}
                        aria-label="Forget fact"
                        style={{ color: !isBackendReachable ? 'var(--text-muted)' : 'var(--text-muted)', opacity: !isBackendReachable ? 0.5 : 1 }}
                      >
                        <Trash2 style={{ width: 16, height: 16 }} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Tab 2: Working Memory */}
      {activeTab === 'working' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {workingItems.length === 0 ? (
            <div className="card" style={{ textAlign: 'center', padding: 30, color: 'var(--text-muted)' }}>
              No active items in working memory for the current session.
            </div>
          ) : (
            workingItems.map((item) => (
              <div key={item.id} className="card">
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                  <span style={{ fontWeight: 600, fontSize: '0.9rem', fontFamily: 'var(--font-mono)' }}>{item.key}</span>
                  <span className={`classification-badge ${item.data_classification}`}>
                    {item.data_classification}
                  </span>
                </div>
                <pre
                  style={{
                    backgroundColor: 'var(--bg-canvas)',
                    padding: 8,
                    borderRadius: 'var(--radius-sm)',
                    fontSize: '0.8rem',
                    fontFamily: 'var(--font-mono)',
                    overflowX: 'auto',
                  }}
                >
                  {JSON.stringify(item.value, null, 2)}
                </pre>
              </div>
            ))
          )}
        </div>
      )}

      {/* Tab 3: Procedural Skills */}
      {activeTab === 'procedural' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {procedures.length === 0 ? (
            <div className="card" style={{ textAlign: 'center', padding: 30, color: 'var(--text-muted)' }}>
              No registered procedures in procedural memory.
            </div>
          ) : (
            procedures.map((p) => (
              <div key={p.id} className="card">
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontWeight: 600, fontSize: '0.95rem' }}>{p.name}</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Status: {p.status}</span>
                </div>
                <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: 4 }}>
                  {p.description}
                </p>
              </div>
            ))
          )}
        </div>
      )}

      {/* Tab 4: Subsystem Status */}
      {activeTab === 'status' && (
        <div className="card">
          <h3 style={{ fontSize: '0.95rem', marginBottom: 12 }}>Memory Subsystem Telemetry</h3>
          <pre
            style={{
              backgroundColor: 'var(--bg-canvas)',
              padding: 12,
              borderRadius: 'var(--radius-sm)',
              fontSize: '0.85rem',
              fontFamily: 'var(--font-mono)',
              overflowX: 'auto',
            }}
          >
            {JSON.stringify(memoryStatus || { status: 'loading...' }, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
};
