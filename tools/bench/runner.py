"""PICO Local Model Benchmark Runner.

Measures Time to First Token (TTFT), generation throughput (tokens/sec),
prompt processing throughput, RAM and VRAM usage on the actual local machine.
Supports both real Ollama execution and deterministic offline mock runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Any
import httpx

from pico.core.config import settings
from pico.core.id_gen import generate_id
from pico.providers.mock import MockModelProvider
from pico.providers.ollama import OllamaProvider
from pico.providers.types import ChatMessage, DoneEvent, GenerationOptions, TokenEvent
from tools.bench.models import (
    AggregateMetrics,
    BenchmarkReport,
    MemoryMetric,
    MetricSummary,
    QualityGateResult,
    RunMeasurement,
)
from tools.bench.prompts import BENCHMARK_PROMPTS, WARMUP_PROMPT, BenchmarkPrompt
from tools.bench.telemetry import (
    get_gpu_memory,
    get_hardware_environment,
    get_system_memory,
)


def compute_metric_summary(values: list[float]) -> MetricSummary:
    """Compute median, min, max, mean for a list of floats."""
    if not values:
        return MetricSummary(count=0)
    sorted_vals = sorted(values)
    med = statistics.median(sorted_vals)
    mean_val = statistics.mean(sorted_vals)
    return MetricSummary(
        median=round(med, 2),
        min=round(min(sorted_vals), 2),
        max=round(max(sorted_vals), 2),
        mean=round(mean_val, 2),
        count=len(values),
    )


async def check_ollama_preflight(
    base_url: str,
    target_model: str,
    timeout: float = 3.0,
) -> tuple[bool, str, str | None]:
    """Check whether Ollama is reachable and configured model is installed.

    Returns:
        (is_ready, blocker_reason, model_digest)
    """
    clean_url = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{clean_url}/api/tags")
            if resp.status_code != 200:
                return (
                    False,
                    f"Ollama returned HTTP {resp.status_code} on tags probe: {resp.text[:200]}",
                    None,
                )
            data = resp.json()
            models = data.get("models", [])
            installed_names = [m.get("name", "") for m in models]

            # Check exact match or base tag match (e.g. qwen3.5:2b vs qwen3.5:2b:latest)
            matched_digest = None
            found = False
            for m in models:
                name = m.get("name", "")
                if name == target_model or name.startswith(f"{target_model}:") or target_model.startswith(f"{name}:"):
                    found = True
                    matched_digest = m.get("digest")
                    break

            if not found:
                available_str = ", ".join(installed_names) if installed_names else "none"
                return (
                    False,
                    (
                        f"Target model '{target_model}' is not installed in local Ollama runtime. "
                        f"Installed models: [{available_str}]. "
                        f"Run 'ollama run {target_model}' or 'ollama pull {target_model}' manually. "
                        "PICO safety policy strictly forbids automatic model downloads."
                    ),
                    None,
                )

            return True, "Ollama reachable and model verified.", matched_digest

    except httpx.ConnectError:
        return (
            False,
            (
                f"Ollama daemon is not reachable at {clean_url}. "
                "Ensure Ollama is installed and running ('ollama serve')."
            ),
            None,
        )
    except httpx.TimeoutException:
        return (
            False,
            f"Ollama probe timed out after {timeout}s at {clean_url}.",
            None,
        )
    except Exception as exc:
        return (
            False,
            f"Unexpected error probing Ollama: {exc}",
            None,
        )


async def run_single_benchmark_iteration(
    provider: Any,
    prompt: BenchmarkPrompt,
    run_index: int,
    is_warmup: bool,
    max_tokens: int,
) -> RunMeasurement:
    """Execute one inference stream and record granular timing and telemetry."""
    sys_before = get_system_memory()
    gpu_before = get_gpu_memory()

    start_perf = time.perf_counter()
    first_token_time: float | None = None
    accumulated_chars = 0
    token_count = 0
    finish_reason = "stop"
    error_msg: str | None = None
    status: Any = "SUCCESS"

    runtime_meta: dict[str, Any] = {}
    reported_prompt_tokens: int | None = None
    reported_eval_tokens: int | None = None

    chat_msg = ChatMessage(role="user", content=prompt.prompt)
    options = GenerationOptions(max_tokens=max_tokens, stream=True)

    try:
        async for event in provider.stream_chat([chat_msg], options):
            if isinstance(event, TokenEvent):
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                accumulated_chars += len(event.delta)
                token_count += 1
            elif isinstance(event, DoneEvent):
                finish_reason = event.finish_reason
                reported_prompt_tokens = event.prompt_tokens
                reported_eval_tokens = event.completion_tokens
                runtime_meta = event.metadata or {}

    except Exception as exc:
        status = "FAILED"
        error_msg = str(exc)

    end_perf = time.perf_counter()
    total_dur_ms = round((end_perf - start_perf) * 1000.0, 2)
    ttft_ms = (
        round((first_token_time - start_perf) * 1000.0, 2)
        if first_token_time is not None
        else None
    )

    sys_after = get_system_memory()
    gpu_after = get_gpu_memory()

    # Determine completion tokens for throughput calculation
    eval_tok = reported_eval_tokens if reported_eval_tokens is not None else token_count
    prompt_tok = reported_prompt_tokens if reported_prompt_tokens is not None else max(1, len(prompt.prompt) // 4)

    # Runner-measured throughput (eval tokens per second excluding TTFT)
    runner_eval_tok_s: float | None = None
    if ttft_ms is not None and eval_tok > 0 and (total_dur_ms - ttft_ms) > 0:
        runner_eval_tok_s = round(eval_tok / ((total_dur_ms - ttft_ms) / 1000.0), 2)
    elif eval_tok > 0 and total_dur_ms > 0:
        runner_eval_tok_s = round(eval_tok / (total_dur_ms / 1000.0), 2)

    # Runtime-reported throughput
    runtime_eval_tok_s: float | None = None
    if "eval_duration_ms" in runtime_meta and runtime_meta["eval_duration_ms"] > 0 and eval_tok > 0:
        runtime_eval_tok_s = round(eval_tok / (runtime_meta["eval_duration_ms"] / 1000.0), 2)

    runtime_prompt_tok_s: float | None = None
    if "prompt_eval_duration_ms" in runtime_meta and runtime_meta["prompt_eval_duration_ms"] > 0 and prompt_tok > 0:
        runtime_prompt_tok_s = round(prompt_tok / (runtime_meta["prompt_eval_duration_ms"] / 1000.0), 2)

    # Peak memory sampling
    max_sys_used = max(sys_before.used_mb, sys_after.used_mb)
    max_gpu_used = (
        max(gpu_before.used_vram_mb or 0.0, gpu_after.used_vram_mb or 0.0)
        if gpu_before.available
        else None
    )

    memory_metric = MemoryMetric(
        system_ram_used_mb=max_sys_used,
        system_ram_total_mb=sys_before.total_mb if sys_before.available else None,
        system_ram_percent=sys_after.percent_used if sys_after.available else None,
        process_working_set_mb=sys_after.process_working_set_mb if sys_after.available else None,
        gpu_vram_used_mb=max_gpu_used,
        gpu_vram_total_mb=gpu_before.total_vram_mb if gpu_before.available else None,
        gpu_name=gpu_before.gpu_name if gpu_before.available else None,
        gpu_telemetry_source=gpu_before.source if gpu_before.available else "unavailable",
        gpu_error_reason=gpu_before.error_reason,
    )

    return RunMeasurement(
        run_index=run_index,
        is_warmup=is_warmup,
        prompt_id=prompt.prompt_id,
        category=prompt.category,
        status=status,
        error_message=error_msg,
        time_to_first_token_ms=ttft_ms,
        total_duration_ms=total_dur_ms,
        runner_measured_duration_ms=total_dur_ms,
        runtime_load_duration_ms=runtime_meta.get("load_duration_ms"),
        runtime_prompt_eval_duration_ms=runtime_meta.get("prompt_eval_duration_ms"),
        runtime_eval_duration_ms=runtime_meta.get("eval_duration_ms"),
        runtime_total_duration_ms=runtime_meta.get("total_duration_ms"),
        prompt_tokens=prompt_tok,
        completion_tokens=eval_tok,
        total_tokens=prompt_tok + eval_tok,
        runner_eval_tok_per_sec=runner_eval_tok_s,
        runtime_eval_tok_per_sec=runtime_eval_tok_s,
        runtime_prompt_eval_tok_per_sec=runtime_prompt_tok_s,
        memory=memory_metric,
        generated_text_length=accumulated_chars,
        finish_reason=finish_reason,
    )


class LocalBenchmarkRunner:
    def __init__(
        self,
        model_name: str = "qwen3.5:2b",
        base_url: str = "http://127.0.0.1:11434",
        repetitions: int = 3,
        context_size: int = 4096,
        max_tokens: int = 128,
        output_dir: str = "bench_reports",
        is_mock: bool = False,
        min_tokens_per_sec: float | None = None,
        max_ttft_ms: float | None = None,
        max_error_rate: float = 0.0,
    ) -> None:
        self.model_name = model_name
        self.base_url = base_url
        self.repetitions = max(1, repetitions)
        self.context_size = context_size
        self.max_tokens = max_tokens
        self.output_dir = output_dir
        self.is_mock = is_mock
        self.min_tokens_per_sec = min_tokens_per_sec
        self.max_ttft_ms = max_ttft_ms
        self.max_error_rate = max_error_rate

    async def execute(self) -> BenchmarkReport:
        bench_id = generate_id("bench")
        now_iso = datetime.now(timezone.utc).isoformat()
        env_snapshot = get_hardware_environment()

        # Step 1: Pre-flight check
        model_digest: str | None = None
        if not self.is_mock:
            is_ready, blocker_msg, digest = await check_ollama_preflight(
                self.base_url, self.model_name
            )
            if not is_ready:
                report = BenchmarkReport(
                    benchmark_id=bench_id,
                    timestamp_utc=now_iso,
                    status="BLOCKED",
                    status_reason=blocker_msg,
                    model_name=self.model_name,
                    model_digest=None,
                    provider="ollama",
                    is_mock=False,
                    context_window=self.context_size,
                    max_tokens=self.max_tokens,
                    repetitions=self.repetitions,
                    environment=env_snapshot,
                )
                self._save_reports(report)
                return report
            model_digest = digest

        # Step 2: Initialize Provider
        if self.is_mock:
            provider = MockModelProvider(
                model_name=f"mock-{self.model_name}",
                simulate_delay=0.015,  # ~66 tokens/sec simulated
            )
            provider_name = "mock"
        else:
            provider = OllamaProvider(
                base_url=self.base_url,
                model_name=self.model_name,
                context_window=self.context_size,
            )
            provider_name = "ollama"

        # Step 3: Warm-up run
        warmup_run = await run_single_benchmark_iteration(
            provider=provider,
            prompt=WARMUP_PROMPT,
            run_index=0,
            is_warmup=True,
            max_tokens=16,
        )

        # Step 4: Measured runs
        measured_runs: list[RunMeasurement] = []
        for i in range(self.repetitions):
            prompt = BENCHMARK_PROMPTS[i % len(BENCHMARK_PROMPTS)]
            run_result = await run_single_benchmark_iteration(
                provider=provider,
                prompt=prompt,
                run_index=i + 1,
                is_warmup=False,
                max_tokens=self.max_tokens,
            )
            measured_runs.append(run_result)

        # Step 5: Aggregation
        aggregates = self._calculate_aggregates(measured_runs)

        # Step 6: Quality Gates evaluation
        quality_gates, final_status, status_reason = self._evaluate_quality_gates(
            aggregates
        )

        report = BenchmarkReport(
            benchmark_id=bench_id,
            timestamp_utc=now_iso,
            status=final_status,
            status_reason=status_reason,
            model_name=self.model_name,
            model_digest=model_digest,
            provider=provider_name,
            is_mock=self.is_mock,
            context_window=self.context_size,
            max_tokens=self.max_tokens,
            repetitions=self.repetitions,
            environment=env_snapshot,
            warmup_run=warmup_run,
            measured_runs=measured_runs,
            aggregates=aggregates,
            quality_gates=quality_gates,
        )

        self._save_reports(report)
        return report

    def _calculate_aggregates(self, runs: list[RunMeasurement]) -> AggregateMetrics:
        successful_runs = [r for r in runs if r.status == "SUCCESS"]
        failed_count = sum(1 for r in runs if r.status == "FAILED")
        incomplete_count = sum(1 for r in runs if r.finish_reason not in ("stop", "length"))

        error_rate = round(failed_count / len(runs), 3) if runs else 0.0
        incomplete_rate = round(incomplete_count / len(runs), 3) if runs else 0.0

        ttft_vals = [r.time_to_first_token_ms for r in successful_runs if r.time_to_first_token_ms is not None]
        dur_vals = [r.total_duration_ms for r in successful_runs if r.total_duration_ms is not None]

        # Prefer runtime eval tok/s if available; otherwise runner-measured
        tok_s_vals = [
            (r.runtime_eval_tok_per_sec or r.runner_eval_tok_per_sec)
            for r in successful_runs
            if (r.runtime_eval_tok_per_sec or r.runner_eval_tok_per_sec) is not None
        ]

        prompt_tok_s_vals = [
            r.runtime_prompt_eval_tok_per_sec
            for r in successful_runs
            if r.runtime_prompt_eval_tok_per_sec is not None
        ]

        load_dur_vals = [
            r.runtime_load_duration_ms
            for r in successful_runs
            if r.runtime_load_duration_ms is not None
        ]

        sys_ram_vals = [
            r.memory.system_ram_used_mb
            for r in successful_runs
            if r.memory.system_ram_used_mb is not None
        ]

        gpu_vram_vals = [
            r.memory.gpu_vram_used_mb
            for r in successful_runs
            if r.memory.gpu_vram_used_mb is not None
        ]

        return AggregateMetrics(
            time_to_first_token_ms=compute_metric_summary(ttft_vals),
            total_duration_ms=compute_metric_summary(dur_vals),
            generation_tokens_per_sec=compute_metric_summary(tok_s_vals),
            prompt_processing_tokens_per_sec=compute_metric_summary(prompt_tok_s_vals),
            model_load_duration_ms=compute_metric_summary(load_dur_vals),
            system_ram_used_mb=compute_metric_summary(sys_ram_vals),
            gpu_vram_used_mb=compute_metric_summary(gpu_vram_vals),
            error_rate=error_rate,
            incomplete_stream_rate=incomplete_rate,
        )

    def _evaluate_quality_gates(
        self, aggregates: AggregateMetrics
    ) -> tuple[list[QualityGateResult], Any, str]:
        gates: list[QualityGateResult] = []
        all_passed = True
        custom_thresholds_configured = False

        # Error rate threshold
        err_passed = aggregates.error_rate <= self.max_error_rate
        if not err_passed:
            all_passed = False
        gates.append(
            QualityGateResult(
                threshold_name="max_error_rate",
                target_value=self.max_error_rate,
                measured_value=aggregates.error_rate,
                passed=err_passed,
                description=f"Error rate must not exceed {self.max_error_rate * 100}%.",
            )
        )

        # Minimum tokens per second threshold
        if self.min_tokens_per_sec is not None:
            custom_thresholds_configured = True
            med_tok_s = aggregates.generation_tokens_per_sec.median or 0.0
            tok_passed = med_tok_s >= self.min_tokens_per_sec
            if not tok_passed:
                all_passed = False
            gates.append(
                QualityGateResult(
                    threshold_name="min_tokens_per_sec",
                    target_value=self.min_tokens_per_sec,
                    measured_value=med_tok_s,
                    passed=tok_passed,
                    description=f"Median generation throughput must be >= {self.min_tokens_per_sec} tok/s.",
                )
            )

        # Maximum TTFT threshold
        if self.max_ttft_ms is not None:
            custom_thresholds_configured = True
            med_ttft = aggregates.time_to_first_token_ms.median or 999999.0
            ttft_passed = med_ttft <= self.max_ttft_ms
            if not ttft_passed:
                all_passed = False
            gates.append(
                QualityGateResult(
                    threshold_name="max_ttft_ms",
                    target_value=self.max_ttft_ms,
                    measured_value=med_ttft,
                    passed=ttft_passed,
                    description=f"Median TTFT must be <= {self.max_ttft_ms} ms.",
                )
            )

        if not custom_thresholds_configured:
            # Per Gate 4: "Until thresholds are deliberately configured and justified by measurements,
            # report performance as a baseline instead of claiming the model passed a production-readiness gate."
            final_status = "BASELINE_RECORDED"
            reason = "Benchmark completed successfully. Performance metrics recorded as hardware baseline."
        elif all_passed:
            final_status = "PASSED"
            reason = "All configured quality gates satisfied."
        else:
            final_status = "FAILED"
            reason = "One or more configured quality gates failed."

        return gates, final_status, reason

    def _save_reports(self, report: BenchmarkReport) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        safe_model = self.model_name.replace(":", "_").replace("/", "_")
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        prefix = f"bench_{safe_model}_{timestamp_str}"

        # 1. JSON Report
        json_path = os.path.join(self.output_dir, f"{prefix}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(report.model_dump_json(indent=2))

        # 2. Markdown Report
        md_path = os.path.join(self.output_dir, f"{prefix}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self._generate_markdown(report))

    def _generate_markdown(self, report: BenchmarkReport) -> str:
        lines: list[str] = [
            f"# PICO Local Model Benchmark Report — `{report.model_name}`",
            "",
            f"- **Benchmark ID**: `{report.benchmark_id}`",
            f"- **Timestamp (UTC)**: `{report.timestamp_utc}`",
            f"- **Execution Mode**: `{'MOCK (Synthetic)' if report.is_mock else 'NATIVE OLLAMA'}`",
            f"- **Status**: **`{report.status}`**",
            f"- **Status Summary**: {report.status_reason}",
            f"- **Model Digest**: `{report.model_digest or 'N/A'}`",
            f"- **Context Window**: `{report.context_window}` tokens",
            f"- **Repetitions**: `{report.repetitions}` measured runs",
            "",
            "## 1. Hardware Environment",
            "",
            f"- **Operating System**: {report.environment.get('os', 'Unknown')}",
            f"- **Processor**: {report.environment.get('processor', 'Unknown')}",
            f"- **CPU Logical Cores**: {report.environment.get('cpu_count', 'Unknown')}",
            f"- **Python Version**: {report.environment.get('python_version', 'Unknown')}",
            f"- **Host System RAM**: {report.environment.get('system_ram_total_mb', 'N/A')} MB total",
            f"- **GPU**: {report.environment.get('gpu_name') or 'None detected'}",
            f"- **Dedicated VRAM**: {report.environment.get('gpu_vram_total_mb', 'N/A')} MB (Source: {report.environment.get('gpu_telemetry_source', 'unavailable')})",
            "",
        ]

        if report.status == "BLOCKED":
            lines.extend([
                "## 2. Blocked Status Details",
                "",
                "> [!WARNING]",
                f"> **Inference Blocked**: {report.status_reason}",
                "",
                "PICO local-first invariants require explicit manual setup. To unblock:",
                "1. Verify Ollama is running: `ollama list`",
                f"2. Pull the required model: `ollama run {report.model_name}`",
                "3. Re-run the benchmark.",
                "",
            ])
            return "\n".join(lines)

        lines.extend([
            "## 2. Performance Summary Metrics",
            "",
            "| Metric | Median | Range (Min - Max) | Mean | Telemetry Source |",
            "| :--- | :--- | :--- | :--- | :--- |",
            f"| **Time to First Token (TTFT)** | `{report.aggregates.time_to_first_token_ms.median or 'N/A'} ms` | `{report.aggregates.time_to_first_token_ms.min or 'N/A'} - {report.aggregates.time_to_first_token_ms.max or 'N/A'} ms` | `{report.aggregates.time_to_first_token_ms.mean or 'N/A'} ms` | Runner-measured |",
            f"| **Generation Throughput** | `{report.aggregates.generation_tokens_per_sec.median or 'N/A'} tok/s` | `{report.aggregates.generation_tokens_per_sec.min or 'N/A'} - {report.aggregates.generation_tokens_per_sec.max or 'N/A'} tok/s` | `{report.aggregates.generation_tokens_per_sec.mean or 'N/A'} tok/s` | Runtime/Runner |",
            f"| **Prompt Processing Speed** | `{report.aggregates.prompt_processing_tokens_per_sec.median or 'N/A'} tok/s` | `{report.aggregates.prompt_processing_tokens_per_sec.min or 'N/A'} - {report.aggregates.prompt_processing_tokens_per_sec.max or 'N/A'} tok/s` | `{report.aggregates.prompt_processing_tokens_per_sec.mean or 'N/A'} tok/s` | Runtime-reported |",
            f"| **Total Request Duration** | `{report.aggregates.total_duration_ms.median or 'N/A'} ms` | `{report.aggregates.total_duration_ms.min or 'N/A'} - {report.aggregates.total_duration_ms.max or 'N/A'} ms` | `{report.aggregates.total_duration_ms.mean or 'N/A'} ms` | Runner-measured |",
            f"| **Model Load Duration** | `{report.aggregates.model_load_duration_ms.median or 'N/A'} ms` | `{report.aggregates.model_load_duration_ms.min or 'N/A'} - {report.aggregates.model_load_duration_ms.max or 'N/A'} ms` | `{report.aggregates.model_load_duration_ms.mean or 'N/A'} ms` | Runtime-reported |",
            f"| **System RAM Peak** | `{report.aggregates.system_ram_used_mb.median or 'N/A'} MB` | `{report.aggregates.system_ram_used_mb.min or 'N/A'} - {report.aggregates.system_ram_used_mb.max or 'N/A'} MB` | `{report.aggregates.system_ram_used_mb.mean or 'N/A'} MB` | OS Kernel API |",
            f"| **GPU VRAM Peak** | `{report.aggregates.gpu_vram_used_mb.median or 'N/A'} MB` | `{report.aggregates.gpu_vram_used_mb.min or 'N/A'} - {report.aggregates.gpu_vram_used_mb.max or 'N/A'} MB` | `{report.aggregates.gpu_vram_used_mb.mean or 'N/A'} MB` | nvidia-smi |",
            f"| **Error Rate** | `{report.aggregates.error_rate * 100}%` | - | - | Runner-measured |",
            f"| **Incomplete Stream Rate** | `{report.aggregates.incomplete_stream_rate * 100}%` | - | - | Runner-measured |",
            "",
            "## 3. Quality Gate Evaluation",
            "",
            "| Gate | Target | Measured | Result | Details |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])

        for qg in report.quality_gates:
            badge = "**PASS**" if qg.passed else "**FAIL**"
            lines.append(
                f"| `{qg.threshold_name}` | `{qg.target_value}` | `{qg.measured_value}` | {badge} | {qg.description} |"
            )

        lines.extend([
            "",
            "## 4. Auditable Run Log",
            "",
            "| Run # | Type | Prompt ID | Status | TTFT (ms) | Total (ms) | Tok/s | Completion Tok | Memory (RAM/VRAM MB) |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])

        if report.warmup_run:
            w = report.warmup_run
            lines.append(
                f"| 0 (Warmup) | Warmup | `{w.prompt_id}` | `{w.status}` | `{w.time_to_first_token_ms or 'N/A'}` | `{w.total_duration_ms}` | `{w.runner_eval_tok_per_sec or 'N/A'}` | `{w.completion_tokens}` | `{w.memory.system_ram_used_mb or 'N/A'} / {w.memory.gpu_vram_used_mb or 'N/A'}` |"
            )

        for r in report.measured_runs:
            lines.append(
                f"| {r.run_index} | Measured | `{r.prompt_id}` | `{r.status}` | `{r.time_to_first_token_ms or 'N/A'}` | `{r.total_duration_ms}` | `{r.runner_eval_tok_per_sec or r.runtime_eval_tok_per_sec or 'N/A'}` | `{r.completion_tokens}` | `{r.memory.system_ram_used_mb or 'N/A'} / {r.memory.gpu_vram_used_mb or 'N/A'}` |"
            )

        lines.append("")
        return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pico-bench",
        description="PICO Local Hardware Inference Benchmark & Evaluation Tool",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=settings.OLLAMA_MODEL,
        help=f"Target Ollama model name (default: {settings.OLLAMA_MODEL})",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=settings.OLLAMA_BASE_URL,
        help=f"Ollama native HTTP base URL (default: {settings.OLLAMA_BASE_URL})",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=3,
        help="Number of measured benchmark runs (default: 3)",
    )
    parser.add_argument(
        "--context-size",
        type=int,
        default=settings.OLLAMA_CONTEXT_WINDOW,
        help=f"Context window token size (default: {settings.OLLAMA_CONTEXT_WINDOW})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=128,
        help="Bounded maximum generated tokens per iteration (default: 128)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="bench_reports",
        help="Output directory for JSON and Markdown report artifacts (default: bench_reports)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Execute synthetic benchmark using MockModelProvider (offline test path)",
    )
    parser.add_argument(
        "--min-tokens-per-sec",
        type=float,
        default=None,
        help="Optional quality gate: minimum acceptable median tokens per second",
    )
    parser.add_argument(
        "--max-ttft-ms",
        type=float,
        default=None,
        help="Optional quality gate: maximum acceptable median TTFT in milliseconds",
    )
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    runner = LocalBenchmarkRunner(
        model_name=args.model,
        base_url=args.base_url,
        repetitions=args.repetitions,
        context_size=args.context_size,
        max_tokens=args.max_tokens,
        output_dir=args.output_dir,
        is_mock=args.mock,
        min_tokens_per_sec=args.min_tokens_per_sec,
        max_ttft_ms=args.max_ttft_ms,
    )

    print("=================================================================")
    print("  PICO Local Model Hardware Benchmark Runner")
    print(f"  Target Model : {args.model}")
    print(f"  Runtime Mode : {'MOCK (Offline)' if args.mock else 'LIVE OLLAMA'}")
    print(f"  Repetitions  : {args.repetitions} (+1 warmup)")
    print("=================================================================\n")

    report = await runner.execute()

    if report.status == "BLOCKED":
        print("[!] BENCHMARK BLOCKED:")
        print(f"    {report.status_reason}\n")
        print(f"[*] Artifacts recorded in: {args.output_dir}/")
        return 2

    print(f"[+] Status: {report.status}")
    print(f"    {report.status_reason}")
    print(f"    Median TTFT: {report.aggregates.time_to_first_token_ms.median or 'N/A'} ms")
    print(f"    Median Throughput: {report.aggregates.generation_tokens_per_sec.median or 'N/A'} tok/s")
    print(f"    Median Latency: {report.aggregates.total_duration_ms.median or 'N/A'} ms")
    print(f"    Peak RAM: {report.aggregates.system_ram_used_mb.median or 'N/A'} MB")
    print(f"    Peak VRAM: {report.aggregates.gpu_vram_used_mb.median or 'N/A'} MB")
    print(f"\n[*] Artifacts saved to: {args.output_dir}/\n")

    return 0 if report.status in ("PASSED", "BASELINE_RECORDED") else 1


def main() -> None:
    exit_code = asyncio.run(main_async())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
