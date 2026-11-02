"""Workflow synthesis qualification benchmark gate (Milestone P11.1).

Enforces that autonomous conversational workflow synthesis is enabled in system
configuration and backed by a valid, passing, fresh benchmark qualification report
matching the candidate model/provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path

import httpx

from pico.core.config import settings
from pico.schemas.bench import BenchmarkReport
from pico.schemas.workflow_synthesis import (
    MANDATORY_QUALITY_GATES,
    WORKFLOW_SYNTHESIS_SUITE_ID,
    WORKFLOW_SYNTHESIS_SUITE_VERSION,
    WORKFLOW_SYNTHESIS_TASK_CATEGORY,
)

logger = logging.getLogger(__name__)


def get_default_reports_dir() -> Path:
    """Resolve benchmark reports directory independent of current working directory."""
    configured_dir = getattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_REPORTS_DIR", None)
    if configured_dir:
        return Path(configured_dir)
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "tools" / "bench" / "reports"


def resolve_ollama_model_digest(
    model_name: str,
    base_url: str | None = None,
    timeout: float = 2.0,
) -> str | None:
    """Query local Ollama daemon /api/tags to resolve current model digest.

    Performs exact canonical tag matching without prefix matching.
    If untagged, resolves the expected ':latest' tag.
    """
    url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
    target = model_name.strip()
    if not target:
        return None
    target_tag = target if ":" in target else f"{target}:latest"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{url}/api/tags")
            if resp.status_code != 200:
                return None
            data = resp.json()
            models = data.get("models", [])
            for m in models:
                name = m.get("name", "").strip()
                model_field = m.get("model", "").strip()
                if name == target_tag or model_field == target_tag:
                    digest = m.get("digest")
                    if digest and str(digest).strip():
                        return str(digest).strip()
    except Exception:
        return None
    return None


class WorkflowSynthesisGate:
    """Evaluates readiness of model provider for workflow synthesis against qualification reports."""

    def check_readiness(
        self,
        selected_profile: str,
        provider: str,
        model_name: str,
        allow_mock: bool = False,
        benchmark_file: str | None = None,
        active_model_digest: str | None = None,
    ) -> tuple[bool, str]:
        """Check whether the model/provider is qualified for workflow synthesis.

        Returns:
            tuple[bool, str]: (is_qualified, reason)
        """
        if not settings.WORKFLOW_SYNTHESIS_ENABLED:
            return (
                False,
                "Workflow synthesis is disabled in system configuration (WORKFLOW_SYNTHESIS_ENABLED=False).",
            )

        target_file = benchmark_file or settings.WORKFLOW_SYNTHESIS_BENCHMARK_FILE
        if target_file:
            path = Path(target_file)
            if not path.is_file():
                return (
                    False,
                    f"Configured workflow synthesis benchmark report file not found: {path}",
                )
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                report = BenchmarkReport.model_validate(data)
                return self._validate_report(
                    report,
                    provider,
                    model_name,
                    allow_mock=allow_mock,
                    active_model_digest=active_model_digest,
                )
            except Exception as e:
                return (
                    False,
                    f"Invalid workflow synthesis benchmark report JSON at {path}: {e}",
                )

        # Scan default benchmark reports directory
        reports_dir = get_default_reports_dir()
        if not reports_dir.is_dir():
            return (
                False,
                f"Benchmark reports directory not found ({reports_dir}); no qualification report available.",
            )

        try:
            candidates = sorted(reports_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception as e:
            return (
                False,
                f"Failed to scan benchmark reports directory ({reports_dir}): {e}",
            )

        if not candidates:
            return (
                False,
                f"No benchmark report files found in {reports_dir}.",
            )

        # Search for a matching report
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                report = BenchmarkReport.model_validate(data)
            except Exception:
                continue

            if (
                report.task_category == WORKFLOW_SYNTHESIS_TASK_CATEGORY
                and report.suite_id == WORKFLOW_SYNTHESIS_SUITE_ID
                and report.provider.lower() == provider.lower()
                and report.model_name.lower() == model_name.lower()
            ):
                return self._validate_report(
                    report,
                    provider,
                    model_name,
                    allow_mock=allow_mock,
                    active_model_digest=active_model_digest,
                )

        return (
            False,
            f"No workflow synthesis benchmark qualification report found for provider '{provider}' and model '{model_name}'.",
        )

    def _validate_report(
        self,
        report: BenchmarkReport,
        provider: str,
        model_name: str,
        allow_mock: bool = False,
        active_model_digest: str | None = None,
    ) -> tuple[bool, str]:
        """Validate all qualification invariants on a parsed BenchmarkReport."""
        # Validate required non-empty identity fields
        for field_name in (
            "benchmark_id",
            "suite_id",
            "suite_version",
            "task_category",
            "provider",
            "model_name",
            "timestamp_utc",
        ):
            val = getattr(report, field_name, None)
            if not val or not str(val).strip():
                return (
                    False,
                    f"Benchmark qualification report missing required identity field '{field_name}'.",
                )

        if report.status != "PASSED":
            return (
                False,
                f"Benchmark qualification report status is '{report.status}' (reason: {report.status_reason}).",
            )

        if report.task_category != WORKFLOW_SYNTHESIS_TASK_CATEGORY:
            return (
                False,
                f"Report task category '{report.task_category}' does not match required '{WORKFLOW_SYNTHESIS_TASK_CATEGORY}'.",
            )

        if report.suite_id != WORKFLOW_SYNTHESIS_SUITE_ID:
            return (
                False,
                f"Report suite ID '{report.suite_id}' does not match required '{WORKFLOW_SYNTHESIS_SUITE_ID}'.",
            )

        if report.suite_version != WORKFLOW_SYNTHESIS_SUITE_VERSION:
            return (
                False,
                f"Report suite version '{report.suite_version}' does not match required '{WORKFLOW_SYNTHESIS_SUITE_VERSION}'.",
            )

        if report.is_mock and (not allow_mock or provider.lower() != "mock"):
            return (
                False,
                "Mock benchmark reports cannot qualify workflow synthesis in production mode.",
            )

        if (
            report.provider.lower() != provider.lower()
            or report.model_name.lower() != model_name.lower()
        ):
            return (
                False,
                f"Report model/provider mismatch: expected {provider}/{model_name}, got {report.provider}/{report.model_name}.",
            )

        # Freshness and future timestamp validation
        try:
            ts_str = report.timestamp_utc.replace("Z", "+00:00")
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if ts > now + timedelta(seconds=60):
                return (
                    False,
                    f"Benchmark qualification report timestamp is in the future: '{report.timestamp_utc}'.",
                )
            age_hours = (now - ts).total_seconds() / 3600.0
            if age_hours > settings.WORKFLOW_SYNTHESIS_MAX_BENCHMARK_AGE_HOURS:
                return (
                    False,
                    f"Benchmark qualification report expired (age: {age_hours:.1f}h exceeds max {settings.WORKFLOW_SYNTHESIS_MAX_BENCHMARK_AGE_HOURS}h).",
                )
        except Exception as e:
            return (
                False,
                f"Could not parse benchmark report timestamp '{report.timestamp_utc}': {e}",
            )

        # Quality gates check: detect duplicates and verify all mandatory gates pass
        seen_gate_names: set[str] = set()
        gate_map = {}
        for g in report.quality_gates:
            if g.threshold_name in seen_gate_names:
                return (
                    False,
                    f"Duplicate quality gate '{g.threshold_name}' found in qualification report.",
                )
            seen_gate_names.add(g.threshold_name)
            gate_map[g.threshold_name] = g

        for required_gate in MANDATORY_QUALITY_GATES:
            if required_gate not in gate_map:
                return (
                    False,
                    f"Mandatory quality gate '{required_gate}' missing from qualification report.",
                )
            gate = gate_map[required_gate]
            if not gate.passed:
                return (
                    False,
                    f"Mandatory quality gate '{required_gate}' failed (measured: {gate.measured_value}, target: {gate.target_value}).",
                )
            if required_gate in ("latency_throughput", "resource_stability"):
                if str(gate.measured_value).lower().startswith("missing"):
                    return (
                        False,
                        f"Mandatory quality gate '{required_gate}' has missing telemetry measurement.",
                    )

        # Validate prompt run coverage if measured runs are recorded
        if report.measured_runs:
            REQUIRED_PROMPT_IDS = {
                "WS-VALID-01",
                "WS-CHAIN-02",
                "WS-REFUSAL-03",
                "WS-CLARIFY-04",
                "WS-INJECT-05",
                "WS-ADVERSARIAL-06",
            }
            covered = {r.prompt_id for r in report.measured_runs}
            missing = REQUIRED_PROMPT_IDS - covered
            if missing:
                return (
                    False,
                    f"Qualification report missing required benchmark prompt run(s): {sorted(missing)}.",
                )

        # Model digest verification for production Ollama provider
        if provider.lower() == "ollama" and not (report.is_mock and allow_mock):
            if not report.model_digest:
                return (
                    False,
                    "Production qualification report missing required model digest.",
                )
            resolved_digest = active_model_digest or resolve_ollama_model_digest(model_name)
            if not resolved_digest:
                return (
                    False,
                    f"Active model digest could not be verified for provider '{provider}' and model '{model_name}'; qualification fails closed.",
                )
            if report.model_digest != resolved_digest:
                return (
                    False,
                    f"Report model digest '{report.model_digest}' does not match active runtime model digest '{resolved_digest}'.",
                )

        return (
            True,
            "Workflow synthesis benchmark qualification verified.",
        )
