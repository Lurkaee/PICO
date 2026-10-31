/**
 * TypeScript Type Definitions for PICO Desktop Web Experience.
 * Strictly mirrors backend schemas without duplicating business logic.
 */

export type DataClassification = 'PUBLIC' | 'INTERNAL' | 'SENSITIVE' | 'SECRET';

export type Role = 'user' | 'assistant' | 'system';

export interface Message {
  id: string;
  conversation_id: string;
  role: Role;
  content: string;
  created_at: string;
  token_count?: number | null;
  routing_metadata?: RoutingMetadata | null;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface RoutingMetadata {
  provider: string;
  model: string;
  effective_classification: DataClassification;
  decision_rule?: string | null;
  latency_ms?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
}

export interface SSEMetaEvent {
  conversation_id: string;
  user_message_id?: string;
  assistant_message_id?: string;
  classification?: DataClassification;
  provider?: string;
  model?: string;
  decision_rule?: string;
}

export interface SSEDoneEvent {
  stop_reason?: string;
  total_tokens?: number;
  routing_metadata?: RoutingMetadata;
}

export interface SSEErrorEvent {
  error: string;
  detail?: string;
}

export interface OperatorSessionState {
  paired: boolean;
  approver?: string | null;
  expires_at?: string | null;
  csrf_token?: string | null;
}

export interface ToolCapability {
  capability_id: string;
  version: string;
  name: string;
  description: string;
  risk_tier: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  required_permission: string;
  required_classification: DataClassification;
  timeout_seconds: number;
  max_output_bytes: number;
  idempotent: boolean;
  max_retries: number;
  consequential: boolean;
  preconditions: string[];
  expected_effects: string[];
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
}

export interface ToolExecution {
  id: string;
  capability_id: string;
  capability_version: string;
  task_id?: string | null;
  session_id?: string | null;
  idempotency_key?: string | null;
  status:
    | 'queued'
    | 'running'
    | 'awaiting_approval'
    | 'verifying'
    | 'succeeded'
    | 'verification_failed'
    | 'failed'
    | 'timed_out'
    | 'cancelled'
    | string;
  input_data: Record<string, unknown>;
  output_data?: Record<string, unknown> | null;
  error_message?: string | null;
  error_details?: Record<string, unknown> | null;
  verification_status?: string | null;
  verification_details?: Record<string, unknown> | null;
  approved_by?: string | null;
  approved_at?: string | null;
  created_at: string;
  queued_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface WorkflowStep {
  id?: string;
  step_key: string;
  capability_id: string;
  capability_version: string;
  status: 'pending' | 'ready' | 'running' | 'completed' | 'succeeded' | 'failed' | 'cancelled' | 'blocked' | 'awaiting_approval' | 'review_required' | 'verifying' | string;
  dependencies: string[];
  output?: Record<string, unknown> | null;
  error?: string | null;
  error_message?: string | null;
  execution_id?: string | null;
  tool_execution_id?: string | null;
  verification_status?: 'pending' | 'passed' | 'failed' | 'review_required' | null;
  verification?: StepVerificationRecord | null;
}

export interface WorkflowRun {
  id: string;
  name: string;
  task_id?: string | null;
  status: 'pending' | 'running' | 'completed' | 'succeeded' | 'failed' | 'cancelled' | 'blocked' | string;
  steps: Record<string, WorkflowStep> | WorkflowStep[];
  step_order?: string[];
  total_steps?: number;
  completed_steps?: number;
  failed_steps?: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StepVerificationRecord {
  id?: string;
  workflow_run_id?: string;
  workflow_step_id?: string;
  step_key?: string;
  verification_status?: string;
  status?: string;
  verifier_id?: string | null;
  passed?: boolean;
  stage?: string | null;
  evidence_digest?: string | null;
  details?: Record<string, unknown> | null;
  timestamp?: string;
}

export interface WorkflowCompensation {
  id: string;
  workflow_run_id: string;
  workflow_step_id: string;
  original_tool_execution_id?: string | null;
  compensation_capability_id: string;
  compensation_capability_version: string;
  compensation_tool_execution_id?: string | null;
  status: string;
  input_digest: string;
  order_index?: number;
  reason?: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkingMemoryItem {
  id: string;
  session_id: string;
  key: string;
  value: unknown;
  data_classification: DataClassification;
  created_at: string;
  expires_at?: string | null;
}

export interface SemanticFact {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  confidence: number;
  source_provenance: string;
  data_classification: DataClassification;
  verified: boolean;
  is_inferred: boolean;
  created_at: string;
}

export interface ProceduralMemoryItem {
  id: string;
  name: string;
  description: string;
  status: 'draft' | 'pending_approval' | 'approved' | 'deprecated';
  version: string;
  created_at: string;
}

export interface PerceptionStatus {
  enabled: boolean;
  audio_capture_enabled: boolean;
  vision_enabled: boolean;
  motion_enabled: boolean;
  environmental_enabled: boolean;
  adapter_mode: string;
  active_audio_sessions: number;
}

export interface SchedulerMetrics {
  active_local_leases: number;
  active_cloud_leases: number;
  current_model: string | null;
  queued_requests: number;
  total_requests_processed: number;
  total_cancelled: number;
  total_timeouts: number;
}

export interface SystemHealth {
  status: string;
  environment: string;
  version: string;
  timestamp: string;
}
