import React, { useState, useEffect } from 'react';
import { Mic, Square, XCircle, AlertCircle, RefreshCw } from 'lucide-react';
import { api } from '../../api/client';
import { PerceptionStatus } from '../../types';
import { useConnectivity } from '../../context/ConnectivityContext';

interface VoiceControlProps {
  onTranscription: (text: string) => void;
  disabled?: boolean;
}

interface AmbiguousVoiceOperation {
  sessionId: string;
  action: 'stop' | 'cancel';
  reason: string;
  timestamp: string;
}

export const VoiceControl: React.FC<VoiceControlProps> = ({ onTranscription, disabled }) => {
  const { isBackendReachable } = useConnectivity();
  const [perceptionStatus, setPerceptionStatus] = useState<PerceptionStatus | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [ambiguousSession, setAmbiguousSession] = useState<AmbiguousVoiceOperation | null>(null);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const fetchStatus = async () => {
      try {
        const s = await api.getPerceptionStatus();
        if (mounted) setPerceptionStatus(s);
      } catch {
        if (mounted) setPerceptionStatus(null);
      }
    };
    fetchStatus();
    return () => {
      mounted = false;
    };
  }, []);

  const startSession = async () => {
    if (!isBackendReachable) {
      setErrorNotice('Voice capture unavailable while backend is offline.');
      return;
    }
    if (ambiguousSession) {
      setErrorNotice('Voice session state is unconfirmed. Reconcile with backend before starting a new session.');
      return;
    }
    setErrorNotice(null);
    setProcessing(true);
    try {
      // Explicit user initiation passed through API contract with user_consent=true
      const session = await api.startAudioSession(true);
      setActiveSessionId(session.session_id);
      setIsRecording(true);
    } catch (err) {
      setErrorNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setProcessing(false);
    }
  };

  const stopSession = async () => {
    if (!activeSessionId) return;
    if (ambiguousSession) {
      setErrorNotice('Voice session outcome is unconfirmed. Reconcile with backend first.');
      return;
    }
    setProcessing(true);
    setErrorNotice(null);
    try {
      const res = await api.stopAudioSession(activeSessionId);
      setIsRecording(false);
      setActiveSessionId(null);
      if (res.transcription && res.transcription.trim()) {
        onTranscription(res.transcription.trim());
      } else {
        setErrorNotice('No speech detected or adapter returned empty transcription.');
      }
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousSession({
          sessionId: activeSessionId,
          action: 'stop',
          reason: 'Network interrupted during audio stop; server outcome is unconfirmed.',
          timestamp: new Date().toLocaleTimeString(),
        });
        setErrorNotice('Outcome unknown: network interrupted while stopping audio session. Backend state is unconfirmed.');
      } else {
        setErrorNotice(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessing(false);
    }
  };

  const cancelSession = async () => {
    if (!activeSessionId) return;
    if (ambiguousSession) {
      setErrorNotice('Voice session outcome is unconfirmed. Reconcile with backend first.');
      return;
    }
    setProcessing(true);
    setErrorNotice(null);
    try {
      await api.cancelAudioSession(activeSessionId);
      setIsRecording(false);
      setActiveSessionId(null);
      setErrorNotice('Audio capture session cancelled.');
    } catch (err) {
      const isNet = !isBackendReachable || (err instanceof Error && (err.name === 'NetworkError' || err.name === 'TimeoutError'));
      if (isNet) {
        setAmbiguousSession({
          sessionId: activeSessionId,
          action: 'cancel',
          reason: 'Network interrupted during audio cancel; server outcome is unconfirmed.',
          timestamp: new Date().toLocaleTimeString(),
        });
        setErrorNotice('Outcome unknown: network interrupted while cancelling audio session. Backend session state is unconfirmed.');
      } else {
        setErrorNotice(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setProcessing(false);
    }
  };

  const reconcileSession = async () => {
    if (!ambiguousSession) return;
    setReconciling(true);
    setErrorNotice(null);
    try {
      const status = await api.getAudioSessionStatus(ambiguousSession.sessionId);
      if (ambiguousSession.action === 'cancel') {
        if (status.state === 'cancelled') {
          setAmbiguousSession(null);
          setIsRecording(false);
          setActiveSessionId(null);
          setErrorNotice('Audio session cancellation confirmed by server.');
        } else if (status.state === 'completed' || status.state === 'failed') {
          setAmbiguousSession(null);
          setIsRecording(false);
          setActiveSessionId(null);
          setErrorNotice(`Audio session resolved as ${status.state} on server.`);
        } else {
          setErrorNotice(
            `Reconciliation inconclusive: server reports session is still '${status.state}'. Cancellation unconfirmed; controls remain locked.`
          );
        }
      } else if (ambiguousSession.action === 'stop') {
        if (status.state === 'completed') {
          setAmbiguousSession(null);
          setIsRecording(false);
          setActiveSessionId(null);
          setErrorNotice('Audio session stop confirmed by server.');
        } else if (status.state === 'cancelled' || status.state === 'failed') {
          setAmbiguousSession(null);
          setIsRecording(false);
          setActiveSessionId(null);
          setErrorNotice(`Audio session resolved as ${status.state} on server.`);
        } else {
          setErrorNotice(
            `Reconciliation inconclusive: server reports session is still '${status.state}'. Stop unconfirmed; controls remain locked.`
          );
        }
      }
    } catch (err: unknown) {
      const isNotFound = err instanceof Error && (err.message.includes('not found') || err.message.includes('404'));
      if (isNotFound) {
        setErrorNotice(
          `Reconciliation inconclusive: session '${ambiguousSession.sessionId}' not found on server (server restarted or session expired). Authoritative state unavailable; controls remain locked.`
        );
      } else {
        setErrorNotice(
          `Reconciliation failed: ${err instanceof Error ? err.message : String(err)}. Controls remain locked.`
        );
      }
    } finally {
      setReconciling(false);
    }
  };

  const isPerceptionAvailable = perceptionStatus?.enabled && perceptionStatus?.audio_capture_enabled;

  return (
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      {!isRecording ? (
        <button
          type="button"
          className="ptt-button"
          onClick={startSession}
          disabled={disabled || processing || Boolean(ambiguousSession) || !isPerceptionAvailable || !isBackendReachable}
          title={
            ambiguousSession
              ? 'Voice capture locked: previous session state is unconfirmed. Reconcile with backend first.'
              : !isBackendReachable
              ? 'Voice capture unavailable while backend is offline'
              : !isPerceptionAvailable
              ? 'Voice perception is disabled or unavailable on backend'
              : 'Push-to-talk: Explicit audio capture'
          }
          aria-label="Start voice capture session"
        >
          <Mic style={{ width: 18, height: 18 }} />
        </button>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <button
            type="button"
            className="ptt-button recording"
            onClick={stopSession}
            disabled={processing || Boolean(ambiguousSession)}
            title={
              ambiguousSession
                ? 'Session outcome is unconfirmed. Reconcile with backend first.'
                : 'Stop recording and process speech'
            }
            aria-label="Stop recording and transcribe"
          >
            <Square style={{ width: 14, height: 14 }} />
          </button>
          <button
            type="button"
            className="btn-icon"
            onClick={cancelSession}
            disabled={processing || Boolean(ambiguousSession)}
            title={
              ambiguousSession
                ? 'Session outcome is unconfirmed. Reconcile with backend first.'
                : 'Cancel session without transcribing'
            }
            aria-label="Cancel recording"
            style={{ color: 'var(--text-muted)' }}
          >
            <XCircle style={{ width: 18, height: 18 }} />
          </button>
        </div>
      )}

      {ambiguousSession && (
        <button
          type="button"
          className="btn-icon"
          onClick={reconcileSession}
          disabled={reconciling || !isBackendReachable}
          title="Reconcile audio session status with server"
          aria-label="Reconcile audio session"
          style={{ color: 'var(--warning)' }}
        >
          <RefreshCw className={reconciling ? 'animate-spin' : ''} style={{ width: 14, height: 14 }} />
        </button>
      )}

      {errorNotice && (
        <span
          style={{
            fontSize: '0.725rem',
            color: 'var(--warning)',
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
          }}
          title={errorNotice}
        >
          <AlertCircle style={{ width: 12, height: 12 }} />
          {errorNotice.length > 35 ? errorNotice.slice(0, 35) + '...' : errorNotice}
        </span>
      )}
    </div>
  );
};
