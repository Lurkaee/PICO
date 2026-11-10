import React from 'react';
import { CompanionEyes } from './CompanionEyes';
import { CompanionState, StateRecord } from '../../companion/stateMachine';
import { OperatorSessionState } from '../../types';
import {
  ShieldCheck,
  ShieldAlert,
  Maximize2,
  Minimize2,
  RefreshCw,
} from 'lucide-react';

export interface CompanionPresenceProps {
  stateRecord: StateRecord;
  sessionState: OperatorSessionState;
  isCompactMode: boolean;
  onToggleCompactMode: () => void;
  onOpenPairingModal: () => void;
  onRefreshHealth: () => void;
  isRefreshing?: boolean;
}

export const STATE_LABELS: Record<CompanionState, string> = {
  idle: 'Ready & Calm',
  listening: 'Listening...',
  thinking: 'Reasoning...',
  speaking: 'Speaking...',
  executing: 'Executing Task...',
  awaiting_approval: 'Awaiting Operator Approval',
  offline: 'Core Offline',
  error: 'System Degraded',
};

export const CompanionPresence: React.FC<CompanionPresenceProps> = ({
  stateRecord,
  sessionState,
  isCompactMode,
  onToggleCompactMode,
  onOpenPairingModal,
  onRefreshHealth,
  isRefreshing = false,
}) => {
  const { state, reason, source } = stateRecord;

  return (
    <section
      className={`pico-companion-presence ${isCompactMode ? 'compact-mode' : 'header-mode'}`}
      aria-label="PICO Companion Presence"
    >
      {/* Visual Identity Eyes */}
      <div className="companion-eyes-container">
        <CompanionEyes state={state} size={isCompactMode ? 'lg' : 'sm'} />
      </div>

      {/* Authoritative State & Meta Details */}
      <div className="companion-status-meta">
        <div className="companion-state-badge" data-state={state} role="status" aria-live="polite">
          <span className="status-dot" aria-hidden="true" />
          <span className="status-label">{STATE_LABELS[state]}</span>
        </div>

        {isCompactMode && reason && (
          <p className="companion-reason-text" title={reason}>
            {reason}
          </p>
        )}

        {isCompactMode && (
          <div className="companion-source-tag">
            <span>Source: {source}</span>
          </div>
        )}
      </div>

      {/* Quick Interactive Actions */}
      <div className="companion-quick-actions">
        {/* Pairing Pill */}
        <button
          type="button"
          className={`companion-pill-btn ${sessionState.paired ? 'paired' : 'unpaired'}`}
          onClick={onOpenPairingModal}
          aria-label={sessionState.paired ? `Operator paired: ${sessionState.approver}` : 'Pair operator'}
          title={sessionState.paired ? `Operator paired: ${sessionState.approver}` : 'Click to pair operator'}
        >
          {sessionState.paired ? (
            <>
              <ShieldCheck style={{ width: 14, height: 14, color: 'var(--success)' }} />
              <span className="pill-text">{sessionState.approver || 'Paired'}</span>
            </>
          ) : (
            <>
              <ShieldAlert style={{ width: 14, height: 14, color: 'var(--warning)' }} />
              <span className="pill-text">Pair Operator</span>
            </>
          )}
        </button>

        {/* Health Refresh */}
        <button
          type="button"
          className="companion-icon-btn"
          onClick={onRefreshHealth}
          disabled={isRefreshing}
          aria-label="Refresh core health"
          title="Refresh core health"
        >
          <RefreshCw
            style={{
              width: 14,
              height: 14,
              animation: isRefreshing ? 'spin 1s linear infinite' : undefined,
            }}
          />
        </button>

        {/* Compact / Expanded View Toggle */}
        <button
          type="button"
          className="companion-icon-btn"
          onClick={onToggleCompactMode}
          aria-label={isCompactMode ? 'Expand Command Center' : 'Switch to Compact Companion'}
          title={isCompactMode ? 'Expand Command Center' : 'Switch to Compact Companion'}
        >
          {isCompactMode ? (
            <Maximize2 style={{ width: 14, height: 14 }} />
          ) : (
            <Minimize2 style={{ width: 14, height: 14 }} />
          )}
        </button>
      </div>
    </section>
  );
};
