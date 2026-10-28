import React, { useState, useEffect, useRef } from 'react';
import { Mic, Square, XCircle, AlertCircle } from 'lucide-react';
import { api } from '../../api/client';
import { PerceptionStatus } from '../../types';

interface VoiceControlProps {
  onTranscription: (text: string) => void;
  disabled?: boolean;
}

export const VoiceControl: React.FC<VoiceControlProps> = ({ onTranscription, disabled }) => {
  const [perceptionStatus, setPerceptionStatus] = useState<PerceptionStatus | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

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
      const sessionId = activeSessionIdRef.current;
      activeSessionIdRef.current = null;
      if (sessionId) {
        void api.cancelAudioSession(sessionId).catch(() => {
          // Cleanup must continue even if the backend is unavailable.
        });
      }
    };
  }, []);

  const startSession = async () => {
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
      setErrorNotice(err instanceof Error ? err.message : String(err));
      setIsRecording(false);
      setActiveSessionId(null);
    } finally {
      setProcessing(false);
    }
  };

  const cancelSession = async () => {
    if (!activeSessionId) return;
    setProcessing(true);
    try {
      await api.cancelAudioSession(activeSessionId);
    } catch {
      // Ignore cancellation error
    } finally {
      setIsRecording(false);
      setActiveSessionId(null);
      setProcessing(false);
      setErrorNotice('Audio capture session cancelled.');
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
          disabled={disabled || processing || !isPerceptionAvailable}
          title={
            !isPerceptionAvailable
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
            disabled={processing}
            title="Stop recording and process speech"
            aria-label="Stop recording and transcribe"
          >
            <Square style={{ width: 14, height: 14 }} />
          </button>
          <button
            type="button"
            className="btn-icon"
            onClick={cancelSession}
            disabled={processing}
            title="Cancel session without transcribing"
            aria-label="Cancel recording"
            style={{ color: 'var(--text-muted)' }}
          >
            <XCircle style={{ width: 18, height: 18 }} />
          </button>
        </div>
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
