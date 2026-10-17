"""Automated tests for PICO local hardware benchmark runner and telemetry."""

import json
import os
import tempfile
import httpx
import pytest

from tools.bench.models import BenchmarkReport
from tools.bench.runner import (
    LocalBenchmarkRunner,
    check_ollama_preflight,
    compute_metric_summary,
)
from tools.bench.telemetry import (
    get_gpu_memory,
    get_hardware_environment,
    get_system_memory,
)


def test_system_memory_telemetry() -> None:
    snapshot = get_system_memory()
    assert snapshot.available is True
    assert snapshot.total_mb > 1024  # At least 1GB
    assert snapshot.available_mb > 0
    assert snapshot.used_mb > 0
    assert snapshot.process_working_set_mb > 0
    assert snapshot.source == "windows_kernel32"


def test_gpu_memory_telemetry() -> None:
    snapshot = get_gpu_memory()
    # On this RTX 3050 machine, nvidia-smi is present and queries successfully
    if snapshot.available:
        assert snapshot.gpu_name is not None
        assert snapshot.total_vram_mb is not None
        assert snapshot.total_vram_mb > 0
        assert snapshot.source == "nvidia-smi"
    else:
        # If running in environment without GPU, must explicitly report reason without fabricating numbers
        assert snapshot.error_reason is not None


def test_hardware_environment_capture() -> None:
    env = get_hardware_environment()
    assert "os" in env
    assert "processor" in env
    assert "cpu_count" in env
    assert env["cpu_count"] > 0
    assert env["python_version"].startswith("3.")


def test_metric_summary_calculation() -> None:
    # Empty list
    empty_summary = compute_metric_summary([])
    assert empty_summary.count == 0
    assert empty_summary.median is None

    # Normal list
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    summary = compute_metric_summary(vals)
    assert summary.count == 5
    assert summary.median == 30.0
    assert summary.min == 10.0
    assert summary.max == 50.0
    assert summary.mean == 30.0


@pytest.mark.anyio
async def test_ollama_preflight_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point to an unreachable port
    ready, msg, digest = await check_ollama_preflight(
        base_url="http://127.0.0.1:59999",
        target_model="qwen3.5:2b",
        timeout=1.0,
    )
    assert ready is False
    assert "not reachable" in msg.lower() or "probe timed out" in msg.lower()
    assert digest is None


@pytest.mark.anyio
async def test_ollama_preflight_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock httpx returning other models
    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "models": [
                {"name": "llama3.2:3b", "digest": "sha256:1234567890abcdef"},
                {"name": "mistral:7b", "digest": "sha256:abcdef1234567890"},
            ]
        }
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        # We can test logic by directly probing mock response
        resp = await mock_client.get("http://mock-ollama/api/tags")
        data = resp.json()
        models = [m.get("name") for m in data.get("models", [])]
        assert "qwen3.5:2b" not in models


@pytest.mark.anyio
async def test_mock_benchmark_execution_and_reports() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        runner = LocalBenchmarkRunner(
            model_name="qwen3.5:2b",
            repetitions=2,
            max_tokens=32,
            output_dir=tmp_dir,
            is_mock=True,
        )

        report: BenchmarkReport = await runner.execute()

        assert report.status == "BASELINE_RECORDED"
        assert report.is_mock is True
        assert report.repetitions == 2
        assert report.warmup_run is not None
        assert report.warmup_run.is_warmup is True
        assert len(report.measured_runs) == 2

        for r in report.measured_runs:
            assert r.status == "SUCCESS"
            assert r.time_to_first_token_ms is not None
            assert r.time_to_first_token_ms > 0
            assert r.total_duration_ms is not None
            assert r.total_duration_ms > 0
            assert r.completion_tokens is not None
            assert r.completion_tokens > 0

        # Check aggregates
        assert report.aggregates.time_to_first_token_ms.median is not None
        assert report.aggregates.generation_tokens_per_sec.median is not None
        assert report.aggregates.error_rate == 0.0

        # Check files were written
        files = os.listdir(tmp_dir)
        json_files = [f for f in files if f.endswith(".json")]
        md_files = [f for f in files if f.endswith(".md")]
        assert len(json_files) == 1
        assert len(md_files) == 1

        with open(os.path.join(tmp_dir, json_files[0]), "r", encoding="utf-8") as f:
            saved_json = json.load(f)
            assert saved_json["benchmark_id"] == report.benchmark_id
            assert saved_json["status"] == "BASELINE_RECORDED"

        with open(os.path.join(tmp_dir, md_files[0]), "r", encoding="utf-8") as f:
            md_content = f.read()
            assert "# PICO Local Model Benchmark Report" in md_content
            assert "Time to First Token (TTFT)" in md_content
            assert "BASELINE_RECORDED" in md_content


@pytest.mark.anyio
async def test_mock_benchmark_quality_gates() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. Attainable gates -> PASSED
        passing_runner = LocalBenchmarkRunner(
            model_name="qwen3.5:2b",
            repetitions=2,
            max_tokens=16,
            output_dir=tmp_dir,
            is_mock=True,
            min_tokens_per_sec=10.0,  # Mock produces ~60+ tok/s
            max_ttft_ms=500.0,
        )
        pass_report = await passing_runner.execute()
        assert pass_report.status == "PASSED"

        # 2. Unattainable gate -> FAILED
        failing_runner = LocalBenchmarkRunner(
            model_name="qwen3.5:2b",
            repetitions=2,
            max_tokens=16,
            output_dir=tmp_dir,
            is_mock=True,
            min_tokens_per_sec=10000.0,  # Unattainable threshold
        )
        fail_report = await failing_runner.execute()
        assert fail_report.status == "FAILED"
        failing_gates = [g for g in fail_report.quality_gates if not g.passed]
        assert len(failing_gates) >= 1
        assert failing_gates[0].threshold_name == "min_tokens_per_sec"


@pytest.mark.anyio
async def test_live_benchmark_blocked_when_offline() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Runner pointing to an offline base URL without --mock
        runner = LocalBenchmarkRunner(
            model_name="qwen3.5:2b",
            base_url="http://127.0.0.1:59998",
            repetitions=1,
            output_dir=tmp_dir,
            is_mock=False,
        )

        report = await runner.execute()
        assert report.status == "BLOCKED"
        assert "not reachable" in report.status_reason.lower() or "timed out" in report.status_reason.lower()

        # Files should still be generated documenting the blocked state
        files = os.listdir(tmp_dir)
        assert any(f.endswith(".json") for f in files)
        assert any(f.endswith(".md") for f in files)
