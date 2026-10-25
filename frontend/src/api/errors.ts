/**
 * Structured API Error Model for PICO Desktop (Milestone P10.2).
 *
 * Distinguishes transport failures, request timeouts, HTTP error codes,
 * authorization failures, validation errors, and provider/inference errors.
 */

export class ApiError extends Error {
  public readonly status: number;
  public readonly statusText: string;
  public readonly detail: string;
  public readonly code?: string;
  public readonly isNetworkError: boolean;
  public readonly isTimeout: boolean;
  public readonly isAuthError: boolean;
  public readonly isValidationError: boolean;
  public readonly isProviderError: boolean;

  constructor(options: {
    message: string;
    status?: number;
    statusText?: string;
    detail?: string;
    code?: string;
    isNetworkError?: boolean;
    isTimeout?: boolean;
  }) {
    super(options.message);
    this.name = 'ApiError';
    this.status = options.status ?? 0;
    this.statusText = options.statusText ?? '';
    this.detail = options.detail ?? options.message;
    this.code = options.code;
    this.isNetworkError = options.isNetworkError ?? false;
    this.isTimeout = options.isTimeout ?? false;
    this.isAuthError = this.status === 401 || this.status === 403;
    this.isValidationError = this.status === 422;
    this.isProviderError = this.status === 503 || this.status === 504 || (this.code?.startsWith('PROVIDER_') ?? false);
  }
}

export class NetworkError extends ApiError {
  constructor(message = 'Network connection failed or backend unreachable') {
    super({
      message,
      status: 0,
      statusText: 'Network Error',
      detail: message,
      isNetworkError: true,
    });
    this.name = 'NetworkError';
  }
}

export class TimeoutError extends ApiError {
  constructor(message = 'Request timed out waiting for backend response') {
    super({
      message,
      status: 504,
      statusText: 'Gateway Timeout',
      detail: message,
      isTimeout: true,
    });
    this.name = 'TimeoutError';
  }
}

export class AuthenticationError extends ApiError {
  constructor(status: 401 | 403, detail = 'Operator authorization required or session expired') {
    super({
      message: `Authentication error (${status}): ${detail}`,
      status,
      statusText: status === 401 ? 'Unauthorized' : 'Forbidden',
      detail,
    });
    this.name = 'AuthenticationError';
  }
}

export class ProviderUnavailableError extends ApiError {
  constructor(detail = 'Inference provider or local model currently unavailable', code = 'PROVIDER_UNAVAILABLE') {
    super({
      message: detail,
      status: 503,
      statusText: 'Service Unavailable',
      detail,
      code,
    });
    this.name = 'ProviderUnavailableError';
  }
}

export function isNetworkError(error: unknown): boolean {
  return error instanceof NetworkError || (error instanceof ApiError && error.isNetworkError);
}

export function isAuthError(error: unknown): boolean {
  return error instanceof AuthenticationError || (error instanceof ApiError && error.isAuthError);
}

export function isTimeoutError(error: unknown): boolean {
  return error instanceof TimeoutError || (error instanceof ApiError && error.isTimeout);
}

export function isProviderError(error: unknown): boolean {
  return error instanceof ProviderUnavailableError || (error instanceof ApiError && error.isProviderError);
}
