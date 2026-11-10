import React, { useState, useEffect, useCallback } from 'react';
import { api } from './api/client';
import { Conversation, DataClassification, OperatorSessionState } from './types';
import { ConnectivityProvider } from './context/ConnectivityContext';
import { AppLayout } from './components/layout/AppLayout';
import { ChatPanel } from './components/chat/ChatPanel';
import { WorkflowTimeline } from './components/workflows/WorkflowTimeline';
import { ApprovalInbox } from './components/approvals/ApprovalInbox';
import { MemoryInspector } from './components/memory/MemoryInspector';
import { SystemStatus } from './components/status/SystemStatus';
import { PairingModal } from './components/operator/PairingModal';
import { CompanionStateMachine, StateRecord } from './companion/stateMachine';

export const App: React.FC = () => {
  const [currentTab, setCurrentTab] = useState<'chat' | 'workflows' | 'approvals' | 'memory' | 'status'>('chat');
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<Conversation | null>(null);
  const [classification, setClassification] = useState<DataClassification>('INTERNAL');
  const [sessionState, setSessionState] = useState<OperatorSessionState>({ paired: false });
  const [pendingApprovalsCount, setPendingApprovalsCount] = useState(0);
  const [isPairingModalOpen, setIsPairingModalOpen] = useState(false);

  // P11.2.3 Expressive Companion Presence & State Machine
  const [companionStateMachine] = useState(() => new CompanionStateMachine('offline', 'Initial Core connection pending'));
  const [companionStateRecord, setCompanionStateRecord] = useState<StateRecord>(() => companionStateMachine.getState());
  const [isCompactMode, setIsCompactMode] = useState(false);
  const [isRefreshingHealth, setIsRefreshingHealth] = useState(false);

  // Subscribe to companion state changes
  useEffect(() => {
    return companionStateMachine.subscribe(setCompanionStateRecord);
  }, [companionStateMachine]);

  const pollHealth = useCallback(async () => {
    const seq = companionStateMachine.nextSequence();
    try {
      setIsRefreshingHealth(true);
      const health = await api.getHealth();
      companionStateMachine.handleHealthResponse(true, health.status, undefined, seq);
    } catch (err: any) {
      companionStateMachine.handleHealthResponse(false, undefined, err?.message || 'Backend offline', seq);
    } finally {
      setIsRefreshingHealth(false);
    }
  }, [companionStateMachine]);

  // Initialize data on mount
  useEffect(() => {
    // 1. Initial health check
    pollHealth();
    const healthInterval = setInterval(pollHealth, 8000);

    // 2. Check operator session status
    api
      .getOperatorSession()
      .then(setSessionState)
      .catch(() => setSessionState({ paired: false }));

    // 3. Fetch conversations
    api
      .listConversations()
      .then((items) => {
        setConversations(items);
        if (items.length > 0 && !activeConversation) {
          setActiveConversation(items[0]);
        }
      })
      .catch(() => {});

    // 4. Check pending approvals count
    const checkApprovals = () => {
      api
        .listExecutions('awaiting_approval', 50)
        .then((items) => {
          const count = Array.isArray(items) ? items.length : items.total;
          setPendingApprovalsCount(count);
        })
        .catch(() => {});
    };
    checkApprovals();
    const interval = setInterval(checkApprovals, 10000);
    return () => {
      clearInterval(interval);
      clearInterval(healthInterval);
    };
  }, [pollHealth]);

  const handleNewConversation = () => {
    setActiveConversation(null);
    setCurrentTab('chat');
  };

  const handleConversationCreated = (conv: Conversation) => {
    setConversations((prev) => [conv, ...prev]);
    setActiveConversation(conv);
  };

  const handleReconnected = React.useCallback(() => {
    // Re-verify health and operator session immediately upon confirmed backend recovery
    pollHealth();
    api
      .getOperatorSession()
      .then(setSessionState)
      .catch(() => setSessionState({ paired: false }));

    // Refresh pending approvals
    api
      .listExecutions('awaiting_approval', 50)
      .then((res: any) => {
        const count = Array.isArray(res) ? res.length : (res?.total ?? res?.items?.length ?? 0);
        setPendingApprovalsCount(count);
      })
      .catch(() => {});
  }, [pollHealth]);

  return (
    <ConnectivityProvider onReconnected={handleReconnected}>
      <AppLayout
        currentTab={currentTab}
        onTabChange={setCurrentTab}
        conversations={conversations}
        activeConversation={activeConversation}
        onSelectConversation={setActiveConversation}
        onNewConversation={handleNewConversation}
        pendingApprovalsCount={pendingApprovalsCount}
        sessionState={sessionState}
        onOpenPairingModal={() => setIsPairingModalOpen(true)}
        classification={classification}
        companionStateRecord={companionStateRecord}
        isCompactMode={isCompactMode}
        onToggleCompactMode={() => setIsCompactMode((prev) => !prev)}
        onRefreshHealth={pollHealth}
        isRefreshingHealth={isRefreshingHealth}
      >
        {currentTab === 'chat' && (
          <ChatPanel
            activeConversation={activeConversation}
            onConversationCreated={handleConversationCreated}
            classification={classification}
            onClassificationChange={setClassification}
          />
        )}

        {currentTab === 'workflows' && <WorkflowTimeline />}

        {currentTab === 'approvals' && (
          <ApprovalInbox
            sessionState={sessionState}
            onOpenPairingModal={() => setIsPairingModalOpen(true)}
          />
        )}

        {currentTab === 'memory' && <MemoryInspector />}

        {currentTab === 'status' && <SystemStatus />}
      </AppLayout>

      <PairingModal
        isOpen={isPairingModalOpen}
        onClose={() => setIsPairingModalOpen(false)}
        sessionState={sessionState}
        onSessionUpdate={(updated) => {
          setSessionState(updated);
          if (updated.paired) {
            setIsPairingModalOpen(false);
          }
        }}
      />
    </ConnectivityProvider>
  );
};
