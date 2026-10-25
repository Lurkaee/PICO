/**
 * Core Connectivity & Health Management Layer for PICO (Milestone P10.2).
 *
 * Implements:
 * - Explicit connectivity states: 'online' | 'degraded' | 'offline' | 'reconnecting' | 'unknown'
 * - Separate tracking of core backend reachability vs model inference degradation
 * - Single-flight health probe deduplication
 * - Bounded exponential backoff with jitter
 * - Immediate bounded recheck on window 'online' & visibility changes
 * - Clean listener & timer disposal
 */

import { api } from './client';
import { isNetworkError, isTimeoutError } from './errors';

export type ConnectivityStatus = 'online' | 'degraded' | 'offline' | 'reconnecting' | 'unknown';

export interface ConnectivityState {
  status: ConnectivityStatus;
  isOnline: boolean;
  isOffline: boolean;
  isReconnecting: boolean;
  isDegraded: boolean;
  lastSuccessfulCheck: Date | null;
  lastCheckAttempt: Date | null;
  errorMessage: string | null;
  inferenceDegraded: boolean;
  inferenceDegradedReason: string | null;
}

export interface ConnectivityManagerOptions {
  baseIntervalMs?: number;
  maxIntervalMs?: number;
  backoffFactor?: number;
  jitterFactor?: number;
  onStateChange?: (state: ConnectivityState) => void;
}

export class ConnectivityManager {
  private status: ConnectivityStatus = 'unknown';
  private lastSuccessfulCheck: Date | null = null;
  private lastCheckAttempt: Date | null = null;
  private errorMessage: string | null = null;
  private inferenceDegraded = false;
  private inferenceDegradedReason: string | null = null;

  private currentIntervalMs: number;
  private readonly baseIntervalMs: number;
  private readonly maxIntervalMs: number;
  private readonly backoffFactor: number;
  private readonly jitterFactor: number;

  private activeTimer: ReturnType<typeof setTimeout> | null = null;
  private inFlightProbe: Promise<boolean> | null = null;
  private isDisposed = false;
  private listeners: Set<(state: ConnectivityState) => void> = new Set();

  private windowOnlineHandler: (() => void) | null = null;
  private windowOfflineHandler: (() => void) | null = null;
  private visibilityHandler: (() => void) | null = null;

  constructor(options: ConnectivityManagerOptions = {}) {
    this.baseIntervalMs = options.baseIntervalMs ?? 3000;
    this.maxIntervalMs = options.maxIntervalMs ?? 30000;
    this.backoffFactor = options.backoffFactor ?? 1.5;
    this.jitterFactor = options.jitterFactor ?? 0.15;
    this.currentIntervalMs = this.baseIntervalMs;

    if (options.onStateChange) {
      this.listeners.add(options.onStateChange);
    }

    this.setupBrowserEvents();
  }

  public getState(): ConnectivityState {
    const isOnline = this.status === 'online' || this.status === 'degraded';
    const isOffline = this.status === 'offline';
    const isReconnecting = this.status === 'reconnecting';
    const isDegraded = this.status === 'degraded' || this.inferenceDegraded;

    return {
      status: this.status,
      isOnline,
      isOffline,
      isReconnecting,
      isDegraded,
      lastSuccessfulCheck: this.lastSuccessfulCheck,
      lastCheckAttempt: this.lastCheckAttempt,
      errorMessage: this.errorMessage,
      inferenceDegraded: this.inferenceDegraded,
      inferenceDegradedReason: this.inferenceDegradedReason,
    };
  }

  public subscribe(listener: (state: ConnectivityState) => void): () => void {
    this.listeners.add(listener);
    listener(this.getState());
    return () => {
      this.listeners.delete(listener);
    };
  }

  private notify(): void {
    const state = this.getState();
    for (const listener of this.listeners) {
      listener(state);
    }
  }

  public start(): void {
    if (this.isDisposed) return;
    this.probeHealth();
  }

  public dispose(): void {
    this.isDisposed = true;
    if (this.activeTimer) {
      clearTimeout(this.activeTimer);
      this.activeTimer = null;
    }
    this.removeBrowserEvents();
    this.listeners.clear();
  }

  /**
   * For testing or explicitly forcing connectivity state in simulations.
   */
  public simulateStatus(status: ConnectivityStatus, errorMsg: string | null = null): void {
    this.status = status;
    this.errorMessage = errorMsg;
    if (status === 'online') {
      this.lastSuccessfulCheck = new Date();
    }
    this.notify();
  }

  /**
   * Single-flight probe: if a probe is already running, reuse the existing promise.
   */
  public async probeHealth(): Promise<boolean> {
    if (this.isDisposed) return false;

    if (this.inFlightProbe) {
      return this.inFlightProbe;
    }

    this.inFlightProbe = this.executeProbe()
      .finally(() => {
        this.inFlightProbe = null;
      });

    return this.inFlightProbe;
  }

  private async executeProbe(): Promise<boolean> {
    this.lastCheckAttempt = new Date();

    // If currently offline, transition to 'reconnecting' during probe
    if (this.status === 'offline') {
      this.status = 'reconnecting';
      this.notify();
    }

    try {
      const health = await api.getHealth();
      if (this.isDisposed) return false;

      this.lastSuccessfulCheck = new Date();
      this.errorMessage = null;

      // Backend reachable! Check if database/core is degraded
      if (health.status === 'healthy' || health.status === 'ok') {
        this.status = this.inferenceDegraded ? 'degraded' : 'online';
      } else {
        this.status = 'degraded';
      }

      // Reset exponential backoff on successful contact
      this.currentIntervalMs = this.baseIntervalMs;
      this.notify();
      this.scheduleNextProbe(this.baseIntervalMs);
      return true;
    } catch (err: unknown) {
      if (this.isDisposed) return false;

      const isNetwork = isNetworkError(err) || isTimeoutError(err);
      this.errorMessage = err instanceof Error ? err.message : String(err);

      if (isNetwork) {
        this.status = 'offline';
      } else {
        // Non-network error (e.g. 500) means backend is reached but unhealthy
        this.status = 'degraded';
      }

      // Calculate next backoff with jitter
      const nextDelay = this.calculateNextBackoff();
      this.notify();
      this.scheduleNextProbe(nextDelay);
      return false;
    }
  }

  private calculateNextBackoff(): number {
    const raw = Math.min(this.currentIntervalMs * this.backoffFactor, this.maxIntervalMs);
    this.currentIntervalMs = raw;
    // Add +/- jitter
    const jitter = (Math.random() * 2 - 1) * this.jitterFactor * raw;
    return Math.max(1000, Math.round(raw + jitter));
  }

  private scheduleNextProbe(delayMs: number): void {
    if (this.isDisposed) return;
    if (this.activeTimer) {
      clearTimeout(this.activeTimer);
    }
    this.activeTimer = setTimeout(() => {
      this.probeHealth();
    }, delayMs);
  }

  /**
   * Reports inference/model degradation without claiming the entire backend is offline.
   */
  public reportInferenceDegraded(reason: string): void {
    this.inferenceDegraded = true;
    this.inferenceDegradedReason = reason;
    if (this.status === 'online') {
      this.status = 'degraded';
    }
    this.notify();
  }

  public clearInferenceDegraded(): void {
    this.inferenceDegraded = false;
    this.inferenceDegradedReason = null;
    if (this.status === 'degraded' && this.lastSuccessfulCheck !== null) {
      this.status = 'online';
    }
    this.notify();
  }

  private setupBrowserEvents(): void {
    if (typeof window === 'undefined') return;

    this.windowOnlineHandler = () => {
      // Browser reports network link up -> probe immediately with short delay
      if (this.status === 'offline') {
        this.status = 'reconnecting';
        this.notify();
      }
      this.scheduleNextProbe(200);
    };

    this.windowOfflineHandler = () => {
      // Browser reports offline hint -> mark offline immediately
      this.status = 'offline';
      this.errorMessage = 'Browser network connection is offline';
      this.notify();
    };

    this.visibilityHandler = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
        // Tab restored / became visible -> fresh probe
        this.probeHealth();
      }
    };

    window.addEventListener('online', this.windowOnlineHandler);
    window.addEventListener('offline', this.windowOfflineHandler);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.visibilityHandler);
    }
  }

  private removeBrowserEvents(): void {
    if (typeof window === 'undefined') return;
    if (this.windowOnlineHandler) {
      window.removeEventListener('online', this.windowOnlineHandler);
    }
    if (this.windowOfflineHandler) {
      window.removeEventListener('offline', this.windowOfflineHandler);
    }
    if (typeof document !== 'undefined' && this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
    }
  }
}
