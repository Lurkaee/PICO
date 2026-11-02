"""Workflow synthesis qualification benchmark gate (Milestone P11.1).

Enforces that autonomous conversational workflow synthesis is enabled in system
configuration and backed by a valid, passing, fresh benchmark qualification report
matching the candidate model/provider.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from pico.core.config import settings
from pico.schemas.bench import BenchmarkReport
from pico.schemas.workflow_synthesis import (
    MANDATORY_QUALITY_GATES,
    WORKFLOW_SYNTHESIS_SUITE_ID,
    WORKFLOW_SYNTHESIS_TASK_CATEGORY,
)

logger = logging.getLogger(__name__)


def get_default_reports_dir() -> Path:
    """Resolve benchmark reports directory independent of current working directory."""
    configured_dir = getattr(settings, "WORKFLOW_SYNTHESIS_BENCHMARK_REPORTS_DIR", None)
    if configured_dir:
        return Path(configured_dir)
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidate = repo_root / "tools" / "bench" / "reports"
    if candidate.is_dir():
        return candidate
    return Path("tools/bench/reports")


class WorkflowSynthesisGate:
    """Evaluates readiness of model provider for workflow synthesis against qualification reports."""

    def check_readiness(
        self,
        selected_profile: str,
        provider: str,
        model_name: str,
        allow_mock: bool = False,
        benchmark_file: str | None = None,
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
                return self._validate_report(report, provider, model_name, allow_mock)
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
                return self._validate_report(report, provider, model_name, allow_mock)

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
    ) -> tuple[bool, str]:
        """Validate all qualification invariants on a parsed BenchmarkReport."""
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

        # Freshness validation
        try:
            ts_str = report.timestamp_utc.replace("Z", "+00:00")
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
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

        # Quality gates check
        gate_map = {g.threshold_name: g for g in report.quality_gates}
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

        return (
            True,
            "Workflow synthesis benchmark qualification verified.",
        )
