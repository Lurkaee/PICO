/**
 * Connectivity Context & In-Memory Draft Outbox (Milestone P10.2).
 *
 * Provides shared, typed connectivity state across the entire application,
 * manages in-memory drafts (strictly non-persistent, bounded), and coordinates
 * operator session revalidation on reconnection.
 */

import React, { createContext, useContext, useEffect, useState, useMemo, useRef } from 'react';
import {
  ConnectivityManager,
  ConnectivityState,
  ConnectivityStatus,
} from '../api/connectivity';
import { DataClassification } from '../types';

export interface DraftMessage {
  id: string;
  conversationId: string | null;
  text: string;
  classification: DataClassification;
  createdAt: string;
}

export interface ConnectivityContextValue extends ConnectivityState {
  connectivity: ConnectivityStatus;
  isBackendReachable: boolean;
  checkNow: () => Promise<boolean>;
  reportInferenceDegraded: (reason: string) => void;
  clearInferenceDegraded: () => void;
  drafts: DraftMessage[];
  saveDraft: (text: string, conversationId: string | null, classification: DataClassification) => DraftMessage;
  addDraft: (text: string, conversationId?: string | null, classification?: DataClassification) => DraftMessage;
  updateDraft: (id: string, text: string) => void;
  removeDraft: (id: string) => void;
  clearDrafts: () => void;
  onRecovered?: () => void;
}

const ConnectivityContext = createContext<ConnectivityContextValue | null>(null);

const MAX_IN_MEMORY_DRAFTS = 25;

export interface ConnectivityProviderProps {
  children: React.ReactNode;
  manager?: ConnectivityManager;
  onReconnected?: () => void;
}

export const ConnectivityProvider: React.FC<ConnectivityProviderProps> = ({
  children,
  manager: customManager,
  onReconnected,
}) => {
  const managerRef = useRef<ConnectivityManager | null>(null);
  if (!managerRef.current) {
    managerRef.current = customManager ?? new ConnectivityManager();
  }
  const manager = managerRef.current;

  const [state, setState] = useState<ConnectivityState>(manager.getState());
  const [drafts, setDrafts] = useState<DraftMessage[]>([]);
  const previousStatusRef = useRef<ConnectivityStatus>(state.status);

  useEffect(() => {
    const unsubscribe = manager.subscribe((newState) => {
      const prev = previousStatusRef.current;
      previousStatusRef.current = newState.status;

      setState(newState);

      // Trigger recovery callback when transitioning from offline/reconnecting to online
      if ((prev === 'offline' || prev === 'reconnecting') && newState.status === 'online') {
        onReconnected?.();
      }
    });

    manager.start();

    return () => {
      unsubscribe();
      // Only dispose if we created the manager internally
      if (!customManager) {
        manager.dispose();
      }
    };
  }, [manager, customManager, onReconnected]);

  // Draft operations (Strictly in-memory!)
  const saveDraft = (
    text: string,
    conversationId: string | null,
    classification: DataClassification
  ): DraftMessage => {
    const trimmed = text.trim();
    const newDraft: DraftMessage = {
      id: `draft_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
      conversationId,
      text: trimmed,
      classification,
      createdAt: new Date().toISOString(),
    };

    setDrafts((prev) => {
      // Avoid duplicate drafts with identical text & conversation
      const filtered = prev.filter((d) => !(d.conversationId === conversationId && d.text === trimmed));
      const next = [newDraft, ...filtered];
      return next.slice(0, MAX_IN_MEMORY_DRAFTS);
    });

    return newDraft;
  };

  const updateDraft = (id: string, text: string) => {
    setDrafts((prev) =>
      prev.map((d) => (d.id === id ? { ...d, text: text.trim() } : d))
    );
  };

  const removeDraft = (id: string) => {
    setDrafts((prev) => prev.filter((d) => d.id !== id));
  };

  const clearDrafts = () => {
    setDrafts([]);
  };

  const addDraft = (
    text: string,
    conversationId: string | null = null,
    classification: DataClassification = 'INTERNAL'
  ): DraftMessage => {
    return saveDraft(text, conversationId, classification);
  };

  const isBackendReachable = state.isOnline || state.isDegraded;

  const value: ConnectivityContextValue = useMemo(
    () => ({
      ...state,
      connectivity: state.status,
      isBackendReachable,
      checkNow: () => manager.probeHealth(),
      reportInferenceDegraded: (reason: string) => manager.reportInferenceDegraded(reason),
      clearInferenceDegraded: () => manager.clearInferenceDegraded(),
      drafts,
      saveDraft,
      addDraft,
      updateDraft,
      removeDraft,
      clearDrafts,
    }),
    [state, isBackendReachable, drafts, manager]
  );

  return (
    <ConnectivityContext.Provider value={value}>
      {children}
    </ConnectivityContext.Provider>
  );
};

const defaultConnectivityValue: ConnectivityContextValue = {
  status: 'online',
  isOnline: true,
  isOffline: false,
  isReconnecting: false,
  isDegraded: false,
  lastSuccessfulCheck: null,
  lastCheckAttempt: null,
  errorMessage: null,
  inferenceDegraded: false,
  inferenceDegradedReason: null,
  connectivity: 'online',
  isBackendReachable: true,
  checkNow: async () => true,
  reportInferenceDegraded: () => {},
  clearInferenceDegraded: () => {},
  drafts: [],
  saveDraft: (text, conversationId, classification) => ({
    id: `draft_${Date.now()}`,
    conversationId,
    text,
    classification,
    createdAt: new Date().toISOString(),
  }),
  addDraft: (text, conversationId = null, classification = 'INTERNAL') => ({
    id: `draft_${Date.now()}`,
    conversationId,
    text,
    classification,
    createdAt: new Date().toISOString(),
  }),
  updateDraft: () => {},
  removeDraft: () => {},
  clearDrafts: () => {},
};

export function useConnectivity(): ConnectivityContextValue {
  const context = useContext(ConnectivityContext);
  return context || defaultConnectivityValue;
}
