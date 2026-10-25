import React, { useState } from 'react';
import { WifiOff, RefreshCw, AlertTriangle, CloudOff, FileEdit } from 'lucide-react';
import { useConnectivity } from '../../context/ConnectivityContext';

export const ConnectivityBanner: React.FC = () => {
  const {
    status,
    isOffline,
    isReconnecting,
    isDegraded,
    lastSuccessfulCheck,
    checkNow,
    drafts,
    inferenceDegradedReason,
  } = useConnectivity();

  const [checking, setChecking] = useState(false);

  // If everything is completely normal and no drafts exist, don't show the banner
  if (status === 'online' && !isDegraded && drafts.length === 0) {
    return null;
  }

  // If initial unknown state and no drafts, don't flash an alarming banner
  if (status === 'unknown' && drafts.length === 0) {
    return null;
  }

  const handleManualCheck = async () => {
    if (checking) return;
    setChecking(true);
    try {
      await checkNow();
    } finally {
      setChecking(false);
    }
  };

  const formatLastCheck = (date: Date | null) => {
    if (!date) return 'Never';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  let bannerClass = 'connectivity-banner-warning';
  let icon = <AlertTriangle style={{ width: 16, height: 16, flexShrink: 0 }} />;
  let message = '';

  if (isOffline) {
    bannerClass = 'connectivity-banner-offline';
    icon = <WifiOff style={{ width: 16, height: 16, flexShrink: 0 }} />;
    message = 'PICO backend is unreachable. Local shell is active — state is retained in-memory only. Mutating operations are paused.';
  } else if (isReconnecting) {
    bannerClass = 'connectivity-banner-reconnecting';
    icon = <RefreshCw className="animate-spin" style={{ width: 16, height: 16, flexShrink: 0 }} />;
    message = 'Reconnecting to PICO backend and validating operator credentials...';
  } else if (isDegraded) {
    bannerClass = 'connectivity-banner-degraded';
    icon = <CloudOff style={{ width: 16, height: 16, flexShrink: 0 }} />;
    message = inferenceDegradedReason
      ? `Backend reachable, but inference provider is degraded: ${inferenceDegradedReason}`
      : 'Backend reachable, but one or more subsystems are running in degraded mode.';
  } else if (drafts.length > 0) {
    bannerClass = 'connectivity-banner-info';
    icon = <FileEdit style={{ width: 16, height: 16, flexShrink: 0 }} />;
    message = `You have ${drafts.length} unsent draft(s) stored in active memory.`;
  }

  return (
    <aside
      className={`connectivity-banner ${bannerClass}`}
      role="status"
      aria-live="polite"
    >
      <div className="connectivity-banner-content">
        <span className="connectivity-banner-icon">{icon}</span>
        <span className="connectivity-banner-text">{message}</span>
        {lastSuccessfulCheck && (
          <span className="connectivity-banner-timestamp">
            (Last contact: {formatLastCheck(lastSuccessfulCheck)})
          </span>
        )}
      </div>

      <div className="connectivity-banner-actions">
        {drafts.length > 0 && (
          <span className="draft-counter-badge" title="Unsent in-memory drafts">
            {drafts.length} draft{drafts.length === 1 ? '' : 's'}
          </span>
        )}
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={handleManualCheck}
          disabled={checking || isReconnecting}
          aria-label="Check backend connectivity now"
        >
          <RefreshCw className={checking ? 'animate-spin' : ''} style={{ width: 12, height: 12 }} />
          {checking ? 'Checking...' : 'Check Connection'}
        </button>
      </div>
    </aside>
  );
};
