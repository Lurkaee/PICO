"""Typed contracts and domain models for independent workflow verification (Milestone P9.3)."""

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class VerificationStatus(str, Enum):
    """Canonical verification status outcomes."""
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    REQUIRES_REVIEW = "requires_review"
    REVIEW_REQUIRED = "requires_review"


class VerificationStage(str, Enum):
    """Pipeline stages executed by the verification engine."""
    STAGE_A_STRUCTURAL = "stage_a_structural"
    STAGE_B_EVIDENCE = "stage_b_evidence"
    STAGE_C_SEMANTIC = "stage_c_semantic"
    STAGE_D_DECISION = "stage_d_decision"


class VerificationMethod(str, Enum):
    """Specific method used to assess evidence."""
    DETERMINISTIC_RECOMPUTATION = "deterministic_recomputation"
    SCHEMA_AND_EVIDENCE = "schema_and_evidence"
    FILESYSTEM_HANDLE_DIGEST = "filesystem_handle_digest"
    DEVICE_STATE_POLL = "device_state_poll"
    MODEL_ASSISTED_REVIEW = "model_assisted_review"
    UNSUPPORTED_FALLBACK = "unsupported_fallback"


class VerificationRequest(BaseModel):
    """Input payload provided to the workflow verification engine."""
    workflow_run_id: str
    step_id: str
    step_key: str
    capability_id: str
    capability_version: str = "1.0.0"
    resolved_input: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    tool_execution_id: str | None = None
    tool_execution_record: Any | None = None
    data_classification: str = "INTERNAL"
    step_goal_or_description: str | None = None
    policy_override: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")


class VerificationResult(BaseModel):
    """Auditable verification outcome returned by the verification engine."""
    status: VerificationStatus
    stage: VerificationStage
    policy_id: str
    policy_version: str = "1.0.0"
    method: VerificationMethod
    input_digest: str
    output_digest: str
    passed: bool
    reason: str | None = None
    findings: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    data_classification: str = "INTERNAL"

    model_config = ConfigDict(extra="ignore")
