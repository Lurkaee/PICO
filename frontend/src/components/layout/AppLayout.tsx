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
} from 'lucide-react';
import { Conversation, DataClassification, OperatorSessionState } from '../../types';
import { ConnectivityBanner } from './ConnectivityBanner';

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
  children,
}) => {
  return (
    <div className="app-container">
      {/* Top Header */}
      <header className="app-header">
        <div className="brand-section">
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 'var(--radius-md)',
              backgroundColor: 'var(--primary)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#ffffff',
              fontWeight: 800,
              fontSize: '1rem',
              letterSpacing: '0.05em',
            }}
          >
            P
          </div>
          <span className="brand-title">PICO</span>
          <span className="brand-badge">Command Center</span>
        </div>

        {/* Center / Privacy Indicator */}
        <div className="header-center">
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

        {/* Right Section: Operator Session Status */}
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
        </div>
      </header>
      <ConnectivityBanner />

      {/* Main Split Layout */}
      <div className="main-content">
        {/* Left Sidebar */}
        <aside className="app-sidebar">
          {/* Navigation Tabs */}
          <nav className="nav-tabs" aria-label="Main Navigation">
            <button
              className={`nav-tab-btn ${currentTab === 'chat' ? 'active' : ''}`}
              onClick={() => onTabChange('chat')}
            >
              <MessageSquare style={{ width: 16, height: 16 }} />
              <span>Conversations</span>
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'workflows' ? 'active' : ''}`}
              onClick={() => onTabChange('workflows')}
            >
              <GitBranch style={{ width: 16, height: 16 }} />
              <span>Workflows & Verification</span>
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'approvals' ? 'active' : ''}`}
              onClick={() => onTabChange('approvals')}
              style={{ justifyContent: 'space-between' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <ShieldAlert style={{ width: 16, height: 16 }} />
                <span>Approval Inbox</span>
              </div>
              {pendingApprovalsCount > 0 && (
                <span
                  style={{
                    backgroundColor: 'var(--warning)',
                    color: 'var(--text-inverse)',
                    fontSize: '0.7rem',
                    fontWeight: 700,
                    padding: '1px 6px',
                    borderRadius: 'var(--radius-full)',
                  }}
                >
                  {pendingApprovalsCount}
                </span>
              )}
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'memory' ? 'active' : ''}`}
              onClick={() => onTabChange('memory')}
            >
              <Database style={{ width: 16, height: 16 }} />
              <span>Memory Inspector</span>
            </button>

            <button
              className={`nav-tab-btn ${currentTab === 'status' ? 'active' : ''}`}
              onClick={() => onTabChange('status')}
            >
              <Activity style={{ width: 16, height: 16 }} />
              <span>System Diagnostics</span>
            </button>
          </nav>

          {/* Conversations Section (when in chat view) */}
          <div className="sidebar-header" style={{ marginTop: 8 }}>
            <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              Sessions ({conversations.length})
            </span>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={onNewConversation}
              style={{ fontSize: '0.75rem', padding: '3px 8px' }}
              title="Start a new conversation"
              aria-label="New conversation"
            >
              <Plus style={{ width: 13, height: 13 }} />
              New
            </button>
          </div>

          <div className="sidebar-conversations">
            {conversations.map((c) => (
              <button
                key={c.id}
                className={`conversation-item ${activeConversation?.id === c.id ? 'active' : ''}`}
                onClick={() => {
                  onSelectConversation(c);
                  if (currentTab !== 'chat') onTabChange('chat');
                }}
              >
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    maxWidth: 220,
                  }}
                >
                  {c.title}
                </span>
              </button>
            ))}
          </div>
        </aside>

        {/* Center Area */}
        <main className="center-surface">{children}</main>
      </div>
    </div>
  );
};
