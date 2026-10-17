"""Automated tests for PICO local runtime automation PowerShell scripts."""

import os
import shutil
import subprocess
import tempfile
import pytest


def run_powershell_script(script_rel_path: str, args: list[str]) -> subprocess.CompletedProcess:
    """Execute a PowerShell script using powershell.exe or pwsh."""
    ps_bin = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not ps_bin:
        pytest.skip("PowerShell executable not found on host.")
    script_path = os.path.abspath(script_rel_path)
    cmd = [ps_bin, "-ExecutionPolicy", "Bypass", "-File", script_path] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_preflight_script_mock_mode() -> None:
    res = run_powershell_script("scripts/pico-preflight.ps1", ["-Mock"])
    assert res.returncode == 0
    assert "[PASS] Execution Mode: MOCK" in res.stdout
    assert "Preflight Result: PASS" in res.stdout


def test_preflight_script_live_missing_model() -> None:
    res = run_powershell_script(
        "scripts/pico-preflight.ps1",
        ["-Model", "nonexistent_pico_model:99b"],
    )
    # Must exit with code 2 when blocked by missing model or offline service
    assert res.returncode == 2
    assert "[BLOCKED]" in res.stdout
    assert "nonexistent_pico_model:99b" in res.stdout


def test_benchmark_script_mock_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = run_powershell_script(
            "scripts/pico-benchmark.ps1",
            ["-Mock", "-Repetitions", "1", "-OutputDir", tmp_dir],
        )
        assert res.returncode == 0
        assert "MOCK (Simulated Development)" in res.stdout
        assert "Benchmark execution completed successfully" in res.stdout
        files = os.listdir(tmp_dir)
        assert any(f.endswith(".json") for f in files)
        assert any(f.endswith(".md") for f in files)


def test_benchmark_script_blocks_on_missing_model() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = run_powershell_script(
            "scripts/pico-benchmark.ps1",
            ["-Model", "missing_model_test:1b", "-OutputDir", tmp_dir],
        )
        assert res.returncode == 2
        assert "Preflight Failed" in res.stdout
        # No benchmark reports should have been generated
        files = os.listdir(tmp_dir)
        assert len(files) == 0


def test_verify_script_fast_path() -> None:
    res = run_powershell_script("scripts/pico-verify.ps1", ["-SkipTests"])
    assert res.returncode == 0
    assert "Checking Python Bytecode Compilation..." in res.stdout
    assert "Checking Git Diff & Whitespace Markers..." in res.stdout
    assert "Pytest Suite: SKIPPED" in res.stdout
    assert "Result: PASSED" in res.stdout
