import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  CompanionStateMachine,
  VALID_TRANSITIONS,
  ACTIVE_SOURCES,
} from '../companion/stateMachine';

describe('P11.2.3 Companion Visual State Machine & Truthfulness Invariants', () => {
  let sm: CompanionStateMachine;

  beforeEach(() => {
    sm = new CompanionStateMachine('offline', 'Initial connection pending');
  });

  describe('1. Initial State & Invariants', () => {
    it('initializes in truthful offline state before connection verification', () => {
      const state = sm.getState();
      expect(state.state).toBe('offline');
      expect(state.source).toBe('supervisor_lifecycle');
      expect(state.reason).toBe('Initial connection pending');
      expect(state.sequenceNumber).toBe(1);
    });

    it('subscribes and immediately emits current state record', () => {
      const listener = vi.fn();
      const unsubscribe = sm.subscribe(listener);

      expect(listener).toHaveBeenCalledTimes(1);
      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          state: 'offline',
          sequenceNumber: 1,
        })
      );

      unsubscribe();
    });
  });

  describe('2. Truthful Health Transitions', () => {
    it('transitions to idle when backend reports healthy', () => {
      const seq = sm.nextSequence();
      const ok = sm.handleHealthResponse(true, 'healthy', 'Supervised core online', seq);

      expect(ok).toBe(true);
      const current = sm.getState();
      expect(current.state).toBe('idle');
      expect(current.source).toBe('supervisor_lifecycle');
      expect(current.reason).toBe('Supervised core online');
    });

    it('transitions to error when backend reports degraded', () => {
      const seq1 = sm.nextSequence();
      sm.handleHealthResponse(true, 'healthy', 'Healthy', seq1);

      const seq2 = sm.nextSequence();
      const ok = sm.handleHealthResponse(true, 'degraded', 'Database WAL lock degraded', seq2);

      expect(ok).toBe(true);
      expect(sm.getState().state).toBe('error');
      expect(sm.getState().source).toBe('system_diagnostics');
      expect(sm.getState().reason).toBe('Database WAL lock degraded');
    });

    it('transitions to offline when backend becomes unreachable', () => {
      // First go idle
      sm.handleHealthResponse(true, 'healthy', 'Healthy', sm.nextSequence());
      expect(sm.getState().state).toBe('idle');

      // Then disconnect
      const ok = sm.handleHealthResponse(false, undefined, 'Core process terminated', sm.nextSequence());
      expect(ok).toBe(true);
      expect(sm.getState().state).toBe('offline');
      expect(sm.getState().reason).toBe('Core process terminated');
    });

    it('recovers from offline to idle only after verified healthy response', () => {
      expect(sm.getState().state).toBe('offline');

      // 1. Unsuccessful probe keeps offline
      sm.handleHealthResponse(false, undefined, 'Connection refused', sm.nextSequence());
      expect(sm.getState().state).toBe('offline');

      // 2. Successful probe transitions to idle
      sm.handleHealthResponse(true, 'healthy', 'Core restored', sm.nextSequence());
      expect(sm.getState().state).toBe('idle');
      expect(sm.getState().reason).toBe('Core restored');
    });
  });

  describe('3. Transition Legality & Illegal Transition Rejection', () => {
    it('rejects jumping from offline directly to listening, thinking, or speaking', () => {
      expect(sm.getState().state).toBe('offline');

      // Offline can only go to idle or error
      const okListening = sm.transition('listening', 'perception_audio');
      expect(okListening).toBe(false);
      expect(sm.getState().state).toBe('offline');

      const okThinking = sm.transition('thinking', 'model_gateway');
      expect(okThinking).toBe(false);
      expect(sm.getState().state).toBe('offline');

      const okSpeaking = sm.transition('speaking', 'voice_tts');
      expect(okSpeaking).toBe(false);
      expect(sm.getState().state).toBe('offline');
    });

    it('rejects illegal transition from error to listening or executing', () => {
      sm.transition('error', 'system_diagnostics', 'Crash');
      expect(sm.getState().state).toBe('error');

      const okExec = sm.transition('executing', 'workflow_execution');
      expect(okExec).toBe(false);
      expect(sm.getState().state).toBe('error');
    });

    it('validates transition graph exhaustive rules', () => {
      for (const allowedNext of Object.values(VALID_TRANSITIONS)) {
        expect(Array.isArray(allowedNext)).toBe(true);
      }
    });
  });

  describe('4. Critical Truthfulness Rule & Unsupported Signal Rejection', () => {
    it('rejects active operational states if driven by deferred/unconnected sources', () => {
      // Advance to idle first
      sm.handleHealthResponse(true, 'healthy', 'Online', sm.nextSequence());
      expect(sm.getState().state).toBe('idle');

      // Attempt transition to speaking with voice_tts (which is not yet an ACTIVE_SOURCE)
      expect(ACTIVE_SOURCES.has('voice_tts')).toBe(false);
      const ok = sm.transition('speaking', 'voice_tts');
      expect(ok).toBe(false);
      // Companion must stay truthful in idle!
      expect(sm.getState().state).toBe('idle');
    });

    it('rejects executing and awaiting_approval when signal is from deferred source', () => {
      sm.handleHealthResponse(true, 'healthy', 'Online', sm.nextSequence());

      expect(ACTIVE_SOURCES.has('workflow_execution')).toBe(false);
      const okExec = sm.transition('executing', 'workflow_execution');
      expect(okExec).toBe(false);
      expect(sm.getState().state).toBe('idle');

      expect(ACTIVE_SOURCES.has('operator_approval')).toBe(false);
      const okApproval = sm.transition('awaiting_approval', 'operator_approval');
      expect(okApproval).toBe(false);
      expect(sm.getState().state).toBe('idle');
    });
  });

  describe('5. Stale Asynchronous Response Protection', () => {
    it('discards late-arriving asynchronous responses with older sequence numbers', () => {
      const seq1 = sm.nextSequence(); // e.g. seq 2
      const seq2 = sm.nextSequence(); // e.g. seq 3

      // seq2 resolves first (e.g. quick disconnect)
      sm.handleHealthResponse(false, undefined, 'Core dropped', seq2);
      expect(sm.getState().state).toBe('offline');
      expect(sm.getState().reason).toBe('Core dropped');

      // seq1 resolves later (stale healthy probe)
      const applied = sm.handleHealthResponse(true, 'healthy', 'Old healthy probe', seq1);
      expect(applied).toBe(false);

      // State remains offline from newer seq2!
      expect(sm.getState().state).toBe('offline');
      expect(sm.getState().reason).toBe('Core dropped');
    });

    it('updates reason on same-state heartbeat without advancing sequence unnecessarily', () => {
      sm.handleHealthResponse(true, 'healthy', 'Health check 1', sm.nextSequence());
      const s1 = sm.getState().sequenceNumber;

      sm.transition('idle', 'supervisor_lifecycle', 'Health check 2');
      expect(sm.getState().state).toBe('idle');
      expect(sm.getState().reason).toBe('Health check 2');
      expect(sm.getState().sequenceNumber).toBe(s1); // No new sequence number for heartbeat
    });
  });

  describe('6. Voice Perception Capture Transitions (Milestone P11.3)', () => {
    beforeEach(() => {
      sm.handleHealthResponse(true, 'healthy', 'Online', sm.nextSequence());
      expect(sm.getState().state).toBe('idle');
    });

    it('transitions to listening when capturing with microphoneActive=true', () => {
      const ok = sm.handleAudioCaptureState('capturing', true, 'Recording speech', sm.nextSequence());
      expect(ok).toBe(true);
      const st = sm.getState();
      expect(st.state).toBe('listening');
      expect(st.source).toBe('perception_audio');
      expect(st.reason).toBe('Recording speech');
    });

    it('rejects transition to listening when microphoneActive=false (truthfulness invariant)', () => {
      const ok = sm.handleAudioCaptureState('capturing', false, 'Simulated recording', sm.nextSequence());
      expect(ok).toBe(false);
      // State remains idle!
      expect(sm.getState().state).toBe('idle');
    });

    it('transitions from listening to thinking when processing', () => {
      sm.handleAudioCaptureState('capturing', true, 'Recording', sm.nextSequence());
      expect(sm.getState().state).toBe('listening');

      const ok = sm.handleAudioCaptureState('processing', false, 'Transcribing', sm.nextSequence());
      expect(ok).toBe(true);
      expect(sm.getState().state).toBe('thinking');
      expect(sm.getState().source).toBe('perception_audio');
    });

    it('transitions from thinking back to idle when audio session completes or cancels', () => {
      sm.handleAudioCaptureState('capturing', true, 'Recording', sm.nextSequence());
      sm.handleAudioCaptureState('processing', false, 'Transcribing', sm.nextSequence());
      expect(sm.getState().state).toBe('thinking');

      const ok = sm.handleAudioCaptureState('completed', false, 'Finished', sm.nextSequence());
      expect(ok).toBe(true);
      expect(sm.getState().state).toBe('idle');
    });

    it('transitions to error when audio capture fails', () => {
      sm.handleAudioCaptureState('capturing', true, 'Recording', sm.nextSequence());
      const ok = sm.handleAudioCaptureState('failed', false, 'Mic error', sm.nextSequence());
      expect(ok).toBe(true);
      expect(sm.getState().state).toBe('error');
    });
  });
});
