"""Pydantic schemas and data structures for benchmark measurements and reports."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class MemoryMetric(BaseModel):
    system_ram_used_mb: float | None = None
    system_ram_total_mb: float | None = None
    system_ram_percent: float | None = None
    process_working_set_mb: float | None = None
    gpu_vram_used_mb: float | None = None
    gpu_vram_total_mb: float | None = None
    gpu_name: str | None = None
    gpu_telemetry_source: str = "unavailable"
    gpu_error_reason: str | None = None


class RunMeasurement(BaseModel):
    run_index: int
    is_warmup: bool = False
    prompt_id: str
    category: str
    status: Literal["SUCCESS", "FAILED", "INTERRUPTED", "BLOCKED"]
    error_message: str | None = None

    # Timing metrics (ms)
    time_to_first_token_ms: float | None = None
    total_duration_ms: float | None = None
    runner_measured_duration_ms: float | None = None

    # Runtime native durations (if provided by Ollama in DoneEvent)
    runtime_load_duration_ms: float | None = None
    runtime_prompt_eval_duration_ms: float | None = None
    runtime_eval_duration_ms: float | None = None
    runtime_total_duration_ms: float | None = None

    # Token counts
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    # Throughput (tokens / second)
    runner_eval_tok_per_sec: float | None = None
    runtime_eval_tok_per_sec: float | None = None
    runtime_prompt_eval_tok_per_sec: float | None = None

    # Memory telemetry sampled during run
    memory: MemoryMetric = Field(default_factory=MemoryMetric)
    generated_text: str = ""
    generated_text_length: int = 0
    finish_reason: str = "stop"

    # Response quality evaluation
    quality_score: float | None = None
    quality_passed: bool | None = None
    quality_details: str | None = None


class MetricSummary(BaseModel):
    median: float | None = None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    count: int = 0


class AggregateMetrics(BaseModel):
    time_to_first_token_ms: MetricSummary = Field(default_factory=MetricSummary)
    total_duration_ms: MetricSummary = Field(default_factory=MetricSummary)
    generation_tokens_per_sec: MetricSummary = Field(default_factory=MetricSummary)
    prompt_processing_tokens_per_sec: MetricSummary = Field(default_factory=MetricSummary)
    model_load_duration_ms: MetricSummary = Field(default_factory=MetricSummary)
    system_ram_used_mb: MetricSummary = Field(default_factory=MetricSummary)
    gpu_vram_used_mb: MetricSummary = Field(default_factory=MetricSummary)
    error_rate: float = 0.0
    incomplete_stream_rate: float = 0.0
    quality_pass_rate: float = 1.0
    average_quality_score: float = 1.0


class QualityGateResult(BaseModel):
    threshold_name: str
    target_value: Any
    measured_value: Any
    passed: bool
    description: str


class BenchmarkReport(BaseModel):
    benchmark_id: str
    timestamp_utc: str
    status: Literal["PASSED", "BASELINE_RECORDED", "BLOCKED", "FAILED"]
    status_reason: str

    model_name: str
    model_digest: str | None = None
    provider: str
    is_mock: bool = False
    context_window: int = 4096
    max_tokens: int = 128
    repetitions: int = 5

    free_disk_before_gb: float | None = None
    free_disk_after_gb: float | None = None

    environment: dict[str, Any] = Field(default_factory=dict)
    warmup_run: RunMeasurement | None = None
    measured_runs: list[RunMeasurement] = Field(default_factory=list)
    aggregates: AggregateMetrics = Field(default_factory=AggregateMetrics)
    per_prompt_summary: dict[str, Any] = Field(default_factory=dict)
    quality_gates: list[QualityGateResult] = Field(default_factory=list)
