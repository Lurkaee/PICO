import React from 'react';
import {
  MessageSquare,
  GitBranch,
  ShieldAlert,
  Database,
  Activity,
  Plus,
  Shield,
  ShieldCheck,
  ShieldX,
  Minus,
  X,
} from 'lucide-react';
import { Conversation, DataClassification, OperatorSessionState } from '../../types';
import { isTauri } from '@tauri-apps/api/core';
import { ConnectivityBanner } from './ConnectivityBanner';
import { CompanionPresence } from '../companion/CompanionPresence';
import { StateRecord } from '../../companion/stateMachine';

interface AppLayoutProps {
  currentTab: 'chat' | 'workflows' | 'approvals' | 'memory' | 'status';
  onTabChange: (tab: 'chat' | 'workflows' | 'approvals' | 'memory' | 'status') => void;
  conversations: Conversation[];
  activeConversation: Conversation | null;
  onSelectConversation: (c: Conversation) => void;
  onNewConversation: () => void;
  pendingApprovalsCount: number;
  sessionState: OperatorSessionState;
  onOpenPairingModal: () => void;
  classification: DataClassification;
  companionStateRecord?: StateRecord;
  isCompactMode?: boolean;
  onToggleCompactMode?: () => void;
  onRefreshHealth?: () => void;
  isRefreshingHealth?: boolean;
  children: React.ReactNode;
}

export const AppLayout: React.FC<AppLayoutProps> = ({
  currentTab,
  onTabChange,
  conversations,
  activeConversation,
  onSelectConversation,
  onNewConversation,
  pendingApprovalsCount,
  sessionState,
  onOpenPairingModal,
  classification,
  companionStateRecord,
  isCompactMode = false,
  onToggleCompactMode,
  onRefreshHealth,
  isRefreshingHealth = false,
  children,
}) => {
  const [isTauriEnv, setIsTauriEnv] = React.useState(false);

  React.useEffect(() => {
    if (isTauri()) {
      setIsTauriEnv(true);
    }
  }, []);

  const handleMinimize = async () => {
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      await getCurrentWindow().minimize();
    } catch (err) {
      console.error('Failed to minimize window:', err);
    }
  };

  const handleClose = async () => {
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      await getCurrentWindow().close();
    } catch (err) {
      console.error('Failed to close window:', err);
    }
  };

  return (
    <div className="app-container">
      {/* Top Header */}
      <header className="app-header" data-tauri-drag-region="deep">
        <div className="brand-section">
          <div className="brand-mark" aria-label="PICO brand">
            P
          </div>
          <span className="brand-title">PICO</span>
          <span className="brand-badge">Command Center</span>
        </div>

        {/* Center: Companion Presence & Privacy Scope */}
        <div className="header-center">
          {companionStateRecord && !isCompactMode && (
            <CompanionPresence
              stateRecord={companionStateRecord}
              sessionState={sessionState}
              isCompactMode={false}
              onToggleCompactMode={onToggleCompactMode || (() => {})}
              onOpenPairingModal={onOpenPairingModal}
              onRefreshHealth={onRefreshHealth || (() => {})}
              isRefreshing={isRefreshingHealth}
            />
          )}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '4px 10px',
              borderRadius: 'var(--radius-full)',
              backgroundColor: 'var(--bg-surface-elevated)',
              border: '1px solid var(--border-subtle)',
              fontSize: '0.75rem',
            }}
          >
            <Shield style={{ width: 13, height: 13, color: 'var(--text-muted)' }} />
            <span style={{ color: 'var(--text-secondary)' }}>Privacy Scope:</span>
            <span className={`classification-badge ${classification}`}>{classification}</span>
          </div>
        </div>

        {/* Right Section: Operator Session Status & Desktop Controls */}
        <div className="header-right">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onOpenPairingModal}
            style={{
              fontSize: '0.75rem',
              padding: '5px 10px',
              borderColor: sessionState.paired ? 'rgba(16, 185, 129, 0.4)' : 'rgba(245, 158, 11, 0.4)',
            }}
            aria-label="Operator session pairing status"
          >
            {sessionState.paired ? (
              <>
                <ShieldCheck style={{ width: 14, height: 14, color: 'var(--success)' }} />
                <span>Paired: {sessionState.approver}</span>
              </>
            ) : (
              <>
                <ShieldX style={{ width: 14, height: 14, color: 'var(--warning)' }} />
                <span>Pair Operator</span>
              </>
            )}
          </button>

          {isTauriEnv && (
            <div className="window-controls" role="group" aria-label="Window controls">
              <button
                type="button"
                className="window-control-btn"
                onClick={handleMinimize}
                aria-label="Minimize"
                title="Minimize"
              >
                <Minus style={{ width: 14, height: 14 }} />
              </button>
              <button
                type="button"
                className="window-control-btn close"
                onClick={handleClose}
                aria-label="Close to tray"
                title="Close to tray"
              >
                <X style={{ width: 14, height: 14 }} />
              </button>
            </div>
          )}
        </div>
      </header>
      <ConnectivityBanner />

      {/* Main Split Layout or Compact Companion View */}
      {isCompactMode && companionStateRecord ? (
        <div className="compact-companion-wrapper">
          <CompanionPresence
            stateRecord={companionStateRecord}
            sessionState={sessionState}
            isCompactMode={true}
            onToggleCompactMode={onToggleCompactMode || (() => {})}
            onOpenPairingModal={onOpenPairingModal}
            onRefreshHealth={onRefreshHealth || (() => {})}
            isRefreshing={isRefreshingHealth}
          />
        </div>
      ) : (
        <div className="main-content">
          {/* Left Sidebar */}
        <aside className="app-sidebar">
          {/* Navigation Tabs */}
          <nav className="nav-tabs" aria-label="Main Navigation">
            <button
              className={`nav-tab-btn ${currentTab === 'chat' ? 'nav-tab-btn-active' : ''}`}
              onClick={() => onTabChange('chat')}
              aria-label="Conversations"
            >
              <MessageSquare className="nav-icon" />
              <span>Conversations</span>
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'workflows' ? 'nav-tab-btn-active' : ''}`}
              onClick={() => onTabChange('workflows')}
              aria-label="Workflows & Verification"
            >
              <GitBranch className="nav-icon" />
              <span>Workflows & Verification</span>
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'approvals' ? 'nav-tab-btn-active' : ''}`}
              onClick={() => onTabChange('approvals')}
              aria-label={`Approval Inbox (${pendingApprovalsCount} pending)`}
            >
              <ShieldAlert className="nav-icon" />
              <span>Approval Inbox</span>
              {pendingApprovalsCount > 0 && (
                <span className="badge badge-warning">{pendingApprovalsCount}</span>
              )}
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'memory' ? 'nav-tab-btn-active' : ''}`}
              onClick={() => onTabChange('memory')}
              aria-label="Memory Inspector"
            >
              <Database className="nav-icon" />
              <span>Memory Inspector</span>
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'status' ? 'nav-tab-btn-active' : ''}`}
              onClick={() => onTabChange('status')}
              aria-label="System Diagnostics"
            >
              <Activity className="nav-icon" />
              <span>System Diagnostics</span>
            </button>
          </nav>

          {/* Conversations Section (when in chat view) */}
          <div className="sidebar-header">
            <span className="sidebar-label">Sessions ({conversations.length})</span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={onNewConversation}
              title="Start a new conversation"
              aria-label="New conversation"
            >
              <Plus className="nav-icon" />
              <span>New</span>
            </button>
          </div>

          <div className="sidebar-conversations">
            {conversations.map((c) => (
              <button
                key={c.id}
                className={`conversation-item ${activeConversation?.id === c.id ? 'conversation-item-active' : ''}`}
                onClick={() => {
                  onSelectConversation(c);
                  if (currentTab !== 'chat') onTabChange('chat');
                }}
              >
                <span className="conversation-title">
                  {c.title}
                </span>
              </button>
            ))}
          </div>
        </aside>

        {/* Center Area */}
        <main id="main-content" className="center-surface">{children}</main>
      </div>
      )}
    </div>
  );
};
