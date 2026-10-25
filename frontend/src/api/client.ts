/**
 * Centralized typed API client for PICO Backend.
 *
 * Implements server-side operator session binding with CSRF headers.
 * Never stores approval secrets in the client.
 */

import {
  Conversation,
  Message,
  OperatorSessionState,
  PerceptionStatus,
  ProceduralMemoryItem,
  SchedulerMetrics,
  SemanticFact,
  StepVerificationRecord,
  SystemHealth,
  ToolCapability,
  ToolExecution,
  WorkflowRun,
  WorkingMemoryItem,
} from '../types';

import {
  ApiError,
  AuthenticationError,
  NetworkError,
  ProviderUnavailableError,
  TimeoutError,
} from './errors';

export * from './errors';

let currentCsrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  currentCsrfToken = token;
}

export function getCsrfToken(): string | null {
  return currentCsrfToken;
}

export interface RequestOptions extends RequestInit {
  timeoutMs?: number;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  headers.set('Accept', 'application/json');

  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  // Include CSRF token on mutating requests if present
  const method = (options.method || 'GET').toUpperCase();
  if (currentCsrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    headers.set('X-CSRF-Token', currentCsrfToken);
  }

  const timeoutMs = options.timeoutMs ?? 15000;
  const controller = new AbortController();
  let timedOut = false;

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  let combinedSignal: AbortSignal = controller.signal;
  if (options.signal) {
    if (options.signal.aborted) {
      clearTimeout(timer);
      throw new Error('Request aborted');
    }
    options.signal.addEventListener('abort', () => controller.abort(), { once: true });
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      headers,
      credentials: 'same-origin',
      signal: combinedSignal,
    });
  } catch (err: unknown) {
    clearTimeout(timer);
    if (timedOut) {
      throw new TimeoutError(`Request to ${path} timed out after ${timeoutMs}ms`);
    }
    if (options.signal?.aborted) {
      throw err;
    }
    throw new NetworkError(
      err instanceof Error ? err.message : 'Network connection failed or backend unreachable'
    );
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    let errorDetail = `HTTP ${response.status} ${response.statusText}`;
    let errorCode: string | undefined;
    try {
      const err = await response.json();
      errorDetail = err.detail || errorDetail;
      errorCode = err.code;
    } catch {
      // not JSON
    }

    if (response.status === 401 || response.status === 403) {
      throw new AuthenticationError(response.status as 401 | 403, errorDetail);
    }
    if (response.status === 503) {
      throw new ProviderUnavailableError(errorDetail, errorCode);
    }
    if (response.status === 504) {
      throw new TimeoutError(errorDetail);
    }

    throw new ApiError({
      message: errorDetail,
      status: response.status,
      statusText: response.statusText,
      detail: errorDetail,
      code: errorCode,
    });
  }

  // Handle empty bodies (e.g. 204 or void endpoints)
  const contentType = response.headers.get('content-type');
  if (contentType && contentType.includes('application/json')) {
    return (await response.json()) as T;
  }
  return {} as T;
}

export const api = {
  // --- Operator Session ---
  async getOperatorSession(): Promise<OperatorSessionState> {
    const res = await request<OperatorSessionState>('/api/operator/session');
    if (res.csrf_token) {
      setCsrfToken(res.csrf_token);
    }
    return res;
  },

  async pairOperator(code: string): Promise<OperatorSessionState> {
    const res = await request<{ status: string; csrf_token: string; expires_at: string; approver: string }>(
      '/api/operator/pair',
      {
        method: 'POST',
        body: JSON.stringify({ code }),
      }
    );
    setCsrfToken(res.csrf_token);
    return {
      paired: true,
      approver: res.approver,
      expires_at: res.expires_at,
      csrf_token: res.csrf_token,
    };
  },

  async logoutOperator(): Promise<void> {
    await request('/api/operator/logout', { method: 'POST' });
    setCsrfToken(null);
  },

  // --- Conversations & Messages ---
  async listConversations(): Promise<Conversation[]> {
    return request<Conversation[]>('/api/conversations');
  },

  async createConversation(title?: string): Promise<Conversation> {
    return request<Conversation>('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({ title: title || 'New Conversation' }),
    });
  },

  async getConversation(id: string): Promise<Conversation> {
    return request<Conversation>(`/api/conversations/${id}`);
  },

  async getMessages(conversationId: string, limit = 50, offset = 0): Promise<Message[]> {
    return request<Message[]>(`/api/conversations/${conversationId}/messages?limit=${limit}&offset=${offset}`);
  },

  // --- Perception Subsystem ---
  async getPerceptionStatus(): Promise<PerceptionStatus> {
    return request<PerceptionStatus>('/api/perception/status');
  },

  async startAudioSession(userConsent = true): Promise<{ session_id: string; status: string }> {
    return request('/api/perception/audio/session/start', {
      method: 'POST',
      body: JSON.stringify({ user_consent: userConsent }),
    });
  },

  async stopAudioSession(sessionId: string): Promise<{ session_id: string; transcription?: string; status: string }> {
    return request(`/api/perception/audio/session/${sessionId}/stop`, { method: 'POST' });
  },

  async cancelAudioSession(sessionId: string): Promise<{ session_id: string; status: string }> {
    return request(`/api/perception/audio/session/${sessionId}/cancel`, { method: 'POST' });
  },

  // --- Memory Subsystem ---
  async getMemoryStatus(): Promise<Record<string, unknown>> {
    return request('/api/memory/status');
  },

  async getWorkingMemory(sessionId = 'default'): Promise<WorkingMemoryItem[]> {
    return request<WorkingMemoryItem[]>(`/api/memory/working?session_id=${encodeURIComponent(sessionId)}`);
  },

  async getSemanticFacts(limit = 50, offset = 0): Promise<SemanticFact[]> {
    return request<SemanticFact[]>(`/api/memory/facts?limit=${limit}&offset=${offset}`);
  },

  async searchSemanticFacts(query: string, limit = 10): Promise<SemanticFact[]> {
    return request<SemanticFact[]>(`/api/memory/facts/search?query=${encodeURIComponent(query)}&limit=${limit}`);
  },

  async forgetSemanticFact(factId: string): Promise<{ forgotten: boolean }> {
    return request(`/api/memory/facts/${factId}`, { method: 'DELETE' });
  },

  async getProceduralMemory(limit = 50, offset = 0): Promise<ProceduralMemoryItem[]> {
    return request<ProceduralMemoryItem[]>(`/api/memory/procedures?limit=${limit}&offset=${offset}`);
  },

  // --- Workflows ---
  async listWorkflows(limit = 20, offset = 0): Promise<WorkflowRun[]> {
    const res = await request<{ items: WorkflowRun[]; total: number }>(`/api/workflows?limit=${limit}&offset=${offset}`);
    return res.items || [];
  },

  async getWorkflow(workflowId: string): Promise<WorkflowRun> {
    return request<WorkflowRun>(`/api/workflows/${workflowId}`);
  },

  async advanceWorkflow(workflowId: string): Promise<WorkflowRun> {
    return request<WorkflowRun>(`/api/workflows/${workflowId}/advance`, { method: 'POST' });
  },

  async cancelWorkflow(workflowId: string, reason?: string): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/api/workflows/${workflowId}/cancel`,
      {
        method: 'POST',
        body: JSON.stringify({ reason: reason || 'Cancelled by operator via workflow UI' }),
      }
    );
  },

  async getWorkflowVerifications(workflowId: string): Promise<StepVerificationRecord[]> {
    return request<StepVerificationRecord[]>(`/api/workflows/${workflowId}/verifications`);
  },

  // --- Tool Capabilities & Executions ---
  async listCapabilities(): Promise<ToolCapability[]> {
    return request<ToolCapability[]>('/api/tools/capabilities');
  },

  async listExecutions(statusFilter?: string, limit = 50): Promise<ToolExecution[]> {
    const url = `/api/tools/executions?limit=${limit}${statusFilter ? `&status=${encodeURIComponent(statusFilter)}` : ''}`;
    const res = await request<{ items: ToolExecution[]; total: number }>(url);
    return res.items || [];
  },

  async getExecution(executionId: string): Promise<ToolExecution> {
    return request<ToolExecution>(`/api/tools/executions/${executionId}`);
  },

  async approveExecution(executionId: string, notes?: string, approver?: string): Promise<ToolExecution> {
    return request<ToolExecution>(`/api/tools/executions/${executionId}/approve`, {
      method: 'POST',
      body: JSON.stringify({
        notes: notes || 'Approved via PICO desktop interface',
        approver: approver || undefined,
      }),
    });
  },

  async cancelExecution(executionId: string, reason?: string): Promise<ToolExecution> {
    return request<ToolExecution>(
      `/api/tools/executions/${executionId}/cancel${reason ? `?reason=${encodeURIComponent(reason)}` : ''}`,
      { method: 'POST' }
    );
  },

  // --- Diagnostics & System Status ---
  async getHealth(): Promise<SystemHealth> {
    return request<SystemHealth>('/health');
  },

  async getSchedulerStatus(): Promise<{ enabled: boolean; active_local: number; active_cloud: number; queue_depth: number }> {
    return request('/api/scheduler/status');
  },

  async getSchedulerMetrics(): Promise<SchedulerMetrics> {
    return request<SchedulerMetrics>('/api/scheduler/metrics');
  },
};
