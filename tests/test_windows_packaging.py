"""Unit tests for Windows bundle staging rules and payload hygiene validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import pytest

_STAGE_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "packaging"
    / "stage_windows_bundle.py"
)
_SPEC = importlib.util.spec_from_file_location("stage_windows_bundle", _STAGE_SCRIPT)
assert _SPEC and _SPEC.loader
stage_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["stage_windows_bundle"] = stage_mod
_SPEC.loader.exec_module(stage_mod)


def test_load_runtime_requirements_excludes_dev_packages() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    specs = stage_mod.load_runtime_requirements(repo_root / "requirements.txt")
    lowered = [s.lower() for s in specs]

    assert not any(s.startswith("pytest") for s in lowered)
    for expected_prefix in (
        "fastapi==",
        "uvicorn==",
        "pydantic==",
        "pydantic-settings==",
        "sqlalchemy==",
        "alembic==",
        "httpx==",
        "anyio==",
    ):
        assert any(s.startswith(expected_prefix) for s in lowered), f"Missing {expected_prefix}"


def test_locked_windows_runtime_manifest_integrity() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    lock_path = repo_root / "tools" / "packaging" / "requirements-windows-runtime.lock"
    req_path = repo_root / "requirements.txt"

    assert stage_mod.PYTHON_VERSION == "3.14.10"
    assert stage_mod.PYTHON_ABI == "cp314"
    assert stage_mod.TARGET_PLATFORM == "win_amd64"
    assert "python314.dll" in stage_mod.REQUIRED_EMBED_FILES
    assert "python314.zip" in stage_mod.REQUIRED_EMBED_FILES

    locked = stage_mod.load_locked_runtime_requirements(lock_path, req_path)
    lowered = [entry.lower() for entry in locked]
    assert len(locked) == 23
    assert not any(entry.startswith("pytest==") for entry in lowered)
    for transitive in ("pydantic-core==", "starlette==", "markupsafe==", "mako==", "h11=="):
        assert any(entry.startswith(transitive) for entry in lowered), f"Missing transitive {transitive}"


def test_provision_nsis_script_pins_checksum_and_verifies_version() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "tools" / "packaging" / "provision_nsis.ps1"
    content = script_path.read_text(encoding="utf-8")

    assert "ba63dffc4410ee89193e1cb5a41989991bd77c61068da17e3156d136b7b0b3d8" in content
    assert "nsis-3.13/nsis-3.13.zip" in content
    assert "/VERSION" in content
    assert "Get-FileHash" in content


def test_nsis_autostart_hook_preserves_upgrade_mode() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    hook_path = repo_root / "frontend" / "src-tauri" / "nsis-hooks.nsh"
    content = hook_path.read_text(encoding="utf-8")

    assert "NSIS_HOOK_POSTUNINSTALL" in content
    assert "${If} $UpdateMode <> 1" in content
    assert 'DeleteRegValue HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "PICO"' in content
    assert "${EndIf}" in content


def test_verify_payload_hygiene_rejects_secrets_and_dbs(tmp_path: Path) -> None:
    core_dir = tmp_path / "core"
    python_dir = tmp_path / "python"
    (core_dir / "pico").mkdir(parents=True)
    python_dir.mkdir(parents=True)

    (core_dir / "pico" / "main.py").write_text("# core\n", encoding="utf-8")
    (core_dir / "pyproject.toml").write_text("[project]\nname='pico'\n", encoding="utf-8")
    (core_dir / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (python_dir / "python.exe").write_text("exe\n", encoding="utf-8")

    # Clean baseline passes
    core_count, python_count, _ = stage_mod.verify_payload_hygiene(core_dir, python_dir)
    assert core_count == 3
    assert python_count == 1

    # Secret .env file inside core must fail closed
    env_file = core_dir / ".env"
    env_file.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"\.env"):
        stage_mod.verify_payload_hygiene(core_dir, python_dir)
    env_file.unlink()

    # SQLite DB inside core must fail closed
    db_file = core_dir / "pico.db"
    db_file.write_text("sqlite\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"pico\.db"):
        stage_mod.verify_payload_hygiene(core_dir, python_dir)
    db_file.unlink()

    # tests directory inside core must fail closed
    (core_dir / "tests").mkdir()
    (core_dir / "tests" / "test_x.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"tests"):
        stage_mod.verify_payload_hygiene(core_dir, python_dir)
