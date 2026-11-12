/**
 * P11.2.3 Expressive Companion Presence & Visual State Machine
 *
 * Authoritative, typed state machine governing PICO's expressive presence.
 *
 * CRITICAL TRUTHFULNESS INVARIANTS:
 * 1. The visual state machine must NEVER fabricate activity.
 * 2. It only transitions to states for which an authoritative data source exists.
 * 3. Currently active bridge sources:
 *    - `pico_get_system_status`: Authoritative Core process and database health.
 *    - `pico_pair_operator`: Authoritative operator pairing session.
 * 4. Future bridge capabilities required for deferred states:
 *    - `listening`: Requires P11.3 native audio capture session event.
 *    - `thinking`: Requires P11.3/P11.4 model scheduling lease event.
 *    - `speaking`: Requires P11.3 TTS playback buffer stream event.
 *    - `executing`: Requires P11.2.2.3 execution stream event.
 *    - `awaiting_approval`: Requires P11.2.2.3 approval ticket notification.
 * 5. Stale asynchronous responses are rejected via monotonic sequence checking.
 */

export type CompanionState =
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'executing'
  | 'awaiting_approval'
  | 'offline'
  | 'error';

export type AuthoritativeSource =
  | 'supervisor_lifecycle'   // Active: Verified native bridge / health check
  | 'operator_session'       // Active: Verified native pairing
  | 'perception_audio'       // Deferred (P11.3): Audio capture active
  | 'model_gateway'          // Deferred (P11.3/P11.4): Model inference active
  | 'voice_tts'              // Deferred (P11.3): TTS playback active
  | 'workflow_execution'     // Deferred (P11.2.2.3): Capability execution in flight
  | 'operator_approval'      // Deferred (P11.2.2.3): Pending approval ticket
  | 'system_diagnostics';    // Active: Authoritative error or degraded state

export interface StateRecord {
  state: CompanionState;
  source: AuthoritativeSource;
  reason?: string;
  sequenceNumber: number;
  timestamp: number;
}

/**
 * Valid state transitions mapping.
 * Enforces that an offline or unverified companion cannot jump directly into active operational states.
 */
export const VALID_TRANSITIONS: Record<CompanionState, readonly CompanionState[]> = {
  offline: ['idle', 'error'],
  idle: ['offline', 'error', 'listening', 'thinking', 'executing', 'awaiting_approval'],
  listening: ['idle', 'thinking', 'offline', 'error'],
  thinking: ['idle', 'speaking', 'executing', 'awaiting_approval', 'offline', 'error'],
  speaking: ['idle', 'listening', 'offline', 'error'],
  executing: ['idle', 'awaiting_approval', 'offline', 'error'],
  awaiting_approval: ['idle', 'executing', 'offline', 'error'],
  error: ['idle', 'offline'],
} as const;

/**
 * Active authoritative source map.
 * Identifies which sources are currently wired to genuine backend signals versus deferred to future milestones.
 */
export const ACTIVE_SOURCES: ReadonlySet<AuthoritativeSource> = new Set([
  'supervisor_lifecycle',
  'operator_session',
  'system_diagnostics',
  'perception_audio',
]);

export class CompanionStateMachine {
  private currentRecord: StateRecord;
  private sequenceCounter: number = 0;
  private listeners: Set<(record: StateRecord) => void> = new Set();

  constructor(initialState: CompanionState = 'offline', initialReason: string = 'Initial connection pending') {
    this.sequenceCounter = 1;
    this.currentRecord = {
      state: initialState,
      source: 'supervisor_lifecycle',
      reason: initialReason,
      sequenceNumber: this.sequenceCounter,
      timestamp: Date.now(),
    };
  }

  /**
   * Returns the current authoritative state record.
   */
  public getState(): StateRecord {
    return { ...this.currentRecord };
  }

  /**
   * Subscribes a listener to state transitions.
   */
  public subscribe(listener: (record: StateRecord) => void): () => void {
    this.listeners.add(listener);
    listener(this.getState());
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Attempts a state transition.
   *
   * Rejects:
   * 1. Invalid transitions according to VALID_TRANSITIONS.
   * 2. Stale asynchronous updates (sequenceNumber <= current sequenceNumber).
   * 3. Transitions driven by unverified/simulated sources for deferred states.
   */
  public transition(
    targetState: CompanionState,
    source: AuthoritativeSource,
    reason?: string,
    responseSequence?: number
  ): boolean {
    // 1. Stale response protection
    if (responseSequence !== undefined && responseSequence < this.currentRecord.sequenceNumber) {
      return false;
    }

    const appliedSequence = responseSequence !== undefined
      ? Math.max(this.sequenceCounter, responseSequence)
      : this.sequenceCounter + 1;
    this.sequenceCounter = appliedSequence;

    // 2. Self-transition is a no-op update (e.g. heartbeat while remaining idle)
    if (targetState === this.currentRecord.state) {
      if (responseSequence !== undefined) {
        this.currentRecord.sequenceNumber = responseSequence;
      }
      if (reason && reason !== this.currentRecord.reason) {
        this.currentRecord.reason = reason;
        this.notify();
      }
      return true;
    }

    // 3. Validate transition legality
    const allowed = VALID_TRANSITIONS[this.currentRecord.state];
    if (!allowed || !allowed.includes(targetState)) {
      return false;
    }

    // 4. Critical truthfulness rule:
    // If the transition targets an active task state (listening, thinking, speaking, executing, awaiting_approval),
    // the source must be an approved, currently connected authoritative source.
    const isOperationalState = [
      'listening',
      'thinking',
      'speaking',
      'executing',
      'awaiting_approval',
    ].includes(targetState);

    if (isOperationalState && !ACTIVE_SOURCES.has(source)) {
      // Truthfulness violation: Attempted transition to active state without connected authoritative source.
      return false;
    }

    // 5. Apply valid transition
    this.currentRecord = {
      state: targetState,
      source,
      reason,
      sequenceNumber: appliedSequence,
      timestamp: Date.now(),
    };

    this.notify();
    return true;
  }

  /**
   * Helper to handle backend health responses from verified bridge.
   * Produces 'idle' when healthy, 'error' when degraded, or 'offline' on failure.
   */
  public handleHealthResponse(
    isReachable: boolean,
    healthStatus?: 'healthy' | 'degraded' | string,
    detail?: string,
    sequence?: number
  ): boolean {
    if (!isReachable) {
      return this.transition('offline', 'supervisor_lifecycle', detail || 'Supervised backend offline', sequence);
    }

    if (healthStatus === 'healthy') {
      return this.transition('idle', 'supervisor_lifecycle', detail || 'Core online and idle', sequence);
    }

    if (healthStatus === 'degraded') {
      return this.transition('error', 'system_diagnostics', detail || 'Core reporting degraded subsystem health', sequence);
    }

    return this.transition('error', 'system_diagnostics', detail || `Unknown health status: ${healthStatus}`, sequence);
  }

  /**
   * Helper to handle perception audio capture state updates.
   * Enforces truthfulness: 'listening' is only permitted when microphoneActive is strictly true.
   * Transitions to 'thinking' when processing, and returns to 'idle' when completed or cancelled.
   */
  public handleAudioCaptureState(
    captureState: 'capturing' | 'processing' | 'completed' | 'cancelled' | 'failed' | string,
    microphoneActive: boolean,
    detail?: string,
    sequence?: number
  ): boolean {
    if (captureState === 'capturing') {
      if (!microphoneActive) {
        // Truthfulness invariant: never display listening if physical/mock capture is inactive.
        // If companion was previously listening, revert to idle immediately to avoid stuck state.
        if (this.currentRecord.state === 'listening') {
          return this.transition('idle', 'perception_audio', detail || 'Microphone capture stopped or inactive', sequence);
        }
        return false;
      }
      return this.transition('listening', 'perception_audio', detail || 'Microphone capture active', sequence);
    }

    if (captureState === 'processing') {
      return this.transition('thinking', 'perception_audio', detail || 'Transcribing speech audio', sequence);
    }

    if (captureState === 'completed' || captureState === 'cancelled') {
      return this.transition('idle', 'perception_audio', detail || 'Audio session finished', sequence);
    }

    if (captureState === 'failed') {
      return this.transition('error', 'perception_audio', detail || 'Audio capture or transcription failed', sequence);
    }

    return false;
  }

  /**
   * Helper to allocate a request sequence number before launching an async poll.
   * Guarantees that older async requests resolving late will be discarded.
   */
  public nextSequence(): number {
    this.sequenceCounter += 1;
    return this.sequenceCounter;
  }

  private notify(): void {
    const snapshot = this.getState();
    for (const listener of this.listeners) {
      try {
        listener(snapshot);
      } catch (err) {
        console.error('CompanionStateMachine listener error:', err);
      }
    }
  }
}
