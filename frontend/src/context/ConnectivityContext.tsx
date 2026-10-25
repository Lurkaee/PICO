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
  const onReconnectedRef = useRef(onReconnected);
  useEffect(() => {
    onReconnectedRef.current = onReconnected;
  });

  // Track the active manager instance.
  const managerRef = useRef<ConnectivityManager>(customManager ?? new ConnectivityManager());
  if (customManager && managerRef.current !== customManager) {
    managerRef.current = customManager;
  }

  const [state, setState] = useState<ConnectivityState>(() => managerRef.current.getState());
  const [drafts, setDrafts] = useState<DraftMessage[]>([]);
  const previousReachableRef = useRef<boolean>(
    managerRef.current.getState().isOnline || managerRef.current.getState().isDegraded
  );

  useEffect(() => {
    // If the manager in managerRef was disposed (e.g. from StrictMode unmount cleanup)
    // and no customManager was passed, construct a fresh viable manager.
    let currentManager = customManager ?? managerRef.current;
    if (!customManager && currentManager.disposed) {
      currentManager = new ConnectivityManager();
      managerRef.current = currentManager;
      setState(currentManager.getState());
      previousReachableRef.current = currentManager.getState().isOnline || currentManager.getState().isDegraded;
    }

    const unsubscribe = currentManager.subscribe((newState) => {
      const wasReachable = previousReachableRef.current;
      const isNowReachable = newState.isOnline || newState.isDegraded;
      previousReachableRef.current = isNowReachable;

      setState(newState);

      // Trigger recovery callback when transitioning from offline/reconnecting to reachable (online or degraded)
      if (!wasReachable && isNowReachable) {
        onReconnectedRef.current?.();
      }
    });

    currentManager.start();

    return () => {
      unsubscribe();
      // Only dispose if we created the manager internally
      if (!customManager) {
        currentManager.dispose();
      }
    };
  }, [customManager]);

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
      checkNow: () => managerRef.current.probeHealth(),
      reportInferenceDegraded: (reason: string) => managerRef.current.reportInferenceDegraded(reason),
      clearInferenceDegraded: () => managerRef.current.clearInferenceDegraded(),
      drafts,
      saveDraft,
      addDraft,
      updateDraft,
      removeDraft,
      clearDrafts,
    }),
    [state, isBackendReachable, drafts]
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
