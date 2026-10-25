import React, { useState } from 'react';
import { Shield, ShieldAlert, ShieldCheck, X, Key, LogOut } from 'lucide-react';
import { api } from '../../api/client';
import { OperatorSessionState } from '../../types';

interface PairingModalProps {
  isOpen: boolean;
  onClose: () => void;
  sessionState: OperatorSessionState;
  onSessionUpdate: (session: OperatorSessionState) => void;
}

export const PairingModal: React.FC<PairingModalProps> = ({
  isOpen,
  onClose,
  sessionState,
  onSessionUpdate,
}) => {
  const [code, setCode] = useState('');
  const [approver, setApprover] = useState('human_operator');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generatedCode, setGeneratedCode] = useState<string | null>(null);

  if (!isOpen) return null;

  const handlePair = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code || code.trim().length < 4) {
      setError('Please enter a valid 6-digit pairing code.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const updated = await api.pairOperator(code.trim(), approver);
      onSessionUpdate(updated);
      setCode('');
      setGeneratedCode(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = async () => {
    setLoading(true);
    setError(null);
    try {
      await api.logoutOperator();
      onSessionUpdate({ paired: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleRequestLocalCode = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.requestPairingCode();
      setGeneratedCode(res.code);
      setCode(res.code);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="pairing-modal-title">
      <div className="modal-content">
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Shield style={{ color: 'var(--primary)', width: 22, height: 22 }} />
            <h2 id="pairing-modal-title" style={{ fontSize: '1.15rem' }}>
              Local Operator Session
            </h2>
          </div>
          <button className="btn-icon" onClick={onClose} aria-label="Close dialog">
            <X style={{ width: 18, height: 18 }} />
          </button>
        </div>

        {sessionState.paired ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: 12,
                borderRadius: 'var(--radius-md)',
                backgroundColor: 'var(--success-subtle)',
                border: '1px solid rgba(16, 185, 129, 0.3)',
              }}
            >
              <ShieldCheck style={{ color: 'var(--success)', width: 24, height: 24, flexShrink: 0 }} />
              <div>
                <p style={{ fontWeight: 600, color: 'var(--success)' }}>Operator Paired</p>
                <p style={{ fontSize: '0.825rem', color: 'var(--text-secondary)' }}>
                  Active as <strong>{sessionState.approver}</strong>. Consequential approvals enabled.
                </p>
                {sessionState.expires_at && (
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
                    Expires: {new Date(sessionState.expires_at).toLocaleTimeString()}
                  </p>
                )}
              </div>
            </div>

            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              Your session is authenticated via an <code>HttpOnly</code> cookie with CSRF token verification.
              Server secrets are never exposed in browser storage.
            </p>

            <button className="btn btn-danger" onClick={handleLogout} disabled={loading} style={{ alignSelf: 'flex-start' }}>
              <LogOut style={{ width: 16, height: 16 }} />
              {loading ? 'Revoking...' : 'Revoke Session (Unpair)'}
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: 12,
                borderRadius: 'var(--radius-md)',
                backgroundColor: 'var(--warning-subtle)',
                border: '1px solid rgba(245, 158, 11, 0.3)',
              }}
            >
              <ShieldAlert style={{ color: 'var(--warning)', width: 24, height: 24, flexShrink: 0 }} />
              <div>
                <p style={{ fontWeight: 600, color: 'var(--warning)' }}>Unpaired (Read-Only Mode)</p>
                <p style={{ fontSize: '0.825rem', color: 'var(--text-secondary)' }}>
                  Approval controls are locked until you pair via a one-time pairing code.
                </p>
              </div>
            </div>

            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              Run the terminal command below to generate a secure pairing code on this machine:
            </p>

            <div
              style={{
                padding: '8px 12px',
                borderRadius: 'var(--radius-sm)',
                backgroundColor: 'var(--bg-canvas)',
                border: '1px solid var(--border-subtle)',
                fontFamily: 'var(--font-mono)',
                fontSize: '0.825rem',
                color: 'var(--text-primary)',
              }}
            >
              python -m pico.cli.pair
            </div>

            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={handleRequestLocalCode}
                disabled={loading}
                style={{ fontSize: '0.8rem' }}
              >
                <Key style={{ width: 14, height: 14 }} />
                Generate Code (Loopback)
              </button>
              {generatedCode && (
                <span style={{ fontSize: '0.8rem', color: 'var(--success)' }}>
                  Code generated: <strong>{generatedCode}</strong>
                </span>
              )}
            </div>

            <form onSubmit={handlePair} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div>
                <label
                  htmlFor="pairing-code-input"
                  style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 4 }}
                >
                  6-Digit Pairing Code
                </label>
                <input
                  id="pairing-code-input"
                  type="text"
                  maxLength={12}
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="e.g. 123456"
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-default)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '1rem',
                    letterSpacing: '0.1em',
                  }}
                />
              </div>

              <div>
                <label
                  htmlFor="approver-identity-input"
                  style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 4 }}
                >
                  Operator Identity
                </label>
                <input
                  id="approver-identity-input"
                  type="text"
                  value={approver}
                  onChange={(e) => setApprover(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--border-default)',
                    backgroundColor: 'var(--bg-canvas)',
                    color: 'var(--text-primary)',
                    fontSize: '0.875rem',
                  }}
                />
              </div>

              {error && (
                <div style={{ color: 'var(--danger)', fontSize: '0.8rem' }} role="alert">
                  {error}
                </div>
              )}

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
                <button type="button" className="btn btn-secondary" onClick={onClose}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={loading || !code}>
                  {loading ? 'Pairing...' : 'Authenticate & Pair'}
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    </div>
  );
};
