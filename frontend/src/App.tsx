import React, { useState, useEffect } from 'react';
import { api } from './api/client';
import { Conversation, DataClassification, OperatorSessionState } from './types';
import { AppLayout } from './components/layout/AppLayout';
import { ChatPanel } from './components/chat/ChatPanel';
import { WorkflowTimeline } from './components/workflows/WorkflowTimeline';
import { ApprovalInbox } from './components/approvals/ApprovalInbox';
import { MemoryInspector } from './components/memory/MemoryInspector';
import { SystemStatus } from './components/status/SystemStatus';
import { PairingModal } from './components/operator/PairingModal';

export const App: React.FC = () => {
  const [currentTab, setCurrentTab] = useState<'chat' | 'workflows' | 'approvals' | 'memory' | 'status'>('chat');
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<Conversation | null>(null);
  const [classification, setClassification] = useState<DataClassification>('INTERNAL');
  const [sessionState, setSessionState] = useState<OperatorSessionState>({ paired: false });
  const [pendingApprovalsCount, setPendingApprovalsCount] = useState(0);
  const [isPairingModalOpen, setIsPairingModalOpen] = useState(false);

  // Initialize data on mount
  useEffect(() => {
    // 1. Check operator session status
    api
      .getOperatorSession()
      .then(setSessionState)
      .catch(() => setSessionState({ paired: false }));

    // 2. Fetch conversations
    api
      .listConversations()
      .then((items) => {
        setConversations(items);
        if (items.length > 0 && !activeConversation) {
          setActiveConversation(items[0]);
        }
      })
      .catch(() => {});

    // 3. Check pending approvals count
    const checkApprovals = () => {
      api
        .listExecutions('awaiting_approval', 50)
        .then((items) => setPendingApprovalsCount(items.length))
        .catch(() => {});
    };
    checkApprovals();
    const interval = setInterval(checkApprovals, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleNewConversation = () => {
    setActiveConversation(null);
    setCurrentTab('chat');
  };

  const handleConversationCreated = (conv: Conversation) => {
    setConversations((prev) => [conv, ...prev]);
    setActiveConversation(conv);
  };

  return (
    <>
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
    </>
  );
};
