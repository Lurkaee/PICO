"""Stage PICO Core and pinned embeddable CPython 3.13 runtime for Windows NSIS bundling.

This script deterministically builds ``frontend/src-tauri/staged/{core,python}``:
1. Downloads official CPython 3.13.17 x64 embeddable runtime and verifies SHA-256.
2. Installs hash-locked direct and transitive runtime wheels from
   ``tools/packaging/requirements-windows-runtime.lock`` (excluding dev-only test tooling
   such as ``pytest``) into ``staged/python/Lib/site-packages`` using ``--require-hashes``
   and ``--only-binary=:all:`` targeting CPython 3.13 ``win_amd64``.
3. Configures isolated ``python313._pth`` to load stdlib, site-packages, and ``..\\core``.
4. Copies minimal PICO Core runtime files (``pico/``, ``alembic/``, ``alembic.ini``,
   ``pyproject.toml``) into ``staged/core`` without caches, tests, secrets, or databases.
5. Verifies payload hygiene and executes real import/App/health checks using ``staged/python/python.exe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PYTHON_VERSION = "3.14.10"
PYTHON_MAJOR_MINOR = "3.14"
PYTHON_ABI = "cp314"
TARGET_PLATFORM = "win_amd64"
EMBED_FILENAME = f"python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{EMBED_FILENAME}"
# Official SHA-256 extracted from CPython release Sigstore bundle:
# https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip.sigstore
EMBED_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"

DEV_ONLY_PACKAGES = frozenset({"pytest"})

REQUIRED_EMBED_FILES = (
    "python.exe",
    "python314.dll",
    "python314.zip",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)

FORBIDDEN_FILENAMES = frozenset(
    {
        ".env",
        "pico.db",
        "pico.db-wal",
        "pico.db-shm",
    }
)
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")
CORE_FORBIDDEN_DIR_NAMES = frozenset(
    {
        ".git",
        ".github",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "tests",
        "logs",
        "data",
        "bench_reports",
    }
)
PYTHON_FORBIDDEN_DIR_NAMES = CORE_FORBIDDEN_DIR_NAMES - frozenset({"tests"})


def sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _canonical_pkg_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def load_runtime_requirements(requirements_path: Path) -> list[str]:
    """Parse pinned requirements.txt entries and exclude dev/test-only packages."""
    if not requirements_path.is_file():
        raise FileNotFoundError(f"Requirements file not found: {requirements_path}")

    runtime_specs: list[str] = []
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pkg_name = line.split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0].strip().lower()
        if pkg_name in DEV_ONLY_PACKAGES:
            continue
        runtime_specs.append(line)

    if not runtime_specs:
        raise ValueError(f"No runtime dependencies resolved from {requirements_path}")
    return runtime_specs


def load_locked_runtime_requirements(
    lockfile_path: Path,
    requirements_path: Path | None = None,
) -> list[str]:
    """Parse hash-locked runtime wheel entries and verify alignment with direct runtime specs."""
    if not lockfile_path.is_file():
        raise FileNotFoundError(f"Windows runtime lockfile not found: {lockfile_path}")

    locked_entries: list[str] = []
    locked_map: dict[str, str] = {}
    for line_no, raw_line in enumerate(lockfile_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "--hash=sha256:" not in line or "==" not in line:
            raise ValueError(
                f"Invalid hash-locked entry in {lockfile_path} line {line_no}: {line}"
            )
        spec_part, hash_part = line.split("--hash=sha256:", 1)
        spec = spec_part.strip()
        digest = hash_part.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(
                f"Invalid SHA-256 digest in {lockfile_path} line {line_no}: {digest}"
            )
        pkg_raw, version = spec.split("==", 1)
        pkg_norm = _canonical_pkg_name(pkg_raw)
        if pkg_norm in DEV_ONLY_PACKAGES:
            raise ValueError(
                f"Forbidden dev package '{pkg_raw}' present in {lockfile_path} line {line_no}"
            )
        locked_entries.append(f"{pkg_norm}=={version.strip()}")
        locked_map[pkg_norm] = version.strip()

    if not locked_entries:
        raise ValueError(f"No locked dependencies found in {lockfile_path}")

    if requirements_path is not None:
        direct_specs = load_runtime_requirements(requirements_path)
        for direct in direct_specs:
            if "==" not in direct:
                raise ValueError(f"Direct runtime spec '{direct}' in {requirements_path} is not pinned")
            pkg_raw, ver = direct.split("==", 1)
            pkg_norm = _canonical_pkg_name(pkg_raw)
            locked_ver = locked_map.get(pkg_norm)
            if locked_ver != ver.strip():
                raise ValueError(
                    f"Lockfile {lockfile_path} out of sync with {requirements_path} for '{pkg_raw}': "
                    f"requirements has {ver.strip()}, lockfile has {locked_ver}"
                )

    return locked_entries


def ensure_verified_embed_zip(cache_dir: Path) -> Path:
    """Download embeddable CPython zip if missing and verify its pinned SHA-256."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / EMBED_FILENAME

    if archive_path.is_file():
        actual = sha256_file(archive_path)
        if actual == EMBED_SHA256:
            return archive_path
        archive_path.unlink(missing_ok=True)

    tmp_path = cache_dir / f"{EMBED_FILENAME}.tmp"
    tmp_path.unlink(missing_ok=True)
    print(f"Downloading {EMBED_URL} ...")
    with urllib.request.urlopen(EMBED_URL, timeout=120) as response, tmp_path.open("wb") as out:
        shutil.copyfileobj(response, out)

    actual = sha256_file(tmp_path)
    if actual != EMBED_SHA256:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-256 mismatch for {EMBED_FILENAME}: expected {EMBED_SHA256}, got {actual}"
        )

    tmp_path.replace(archive_path)
    return archive_path


def stage_python_runtime(archive_path: Path, python_dir: Path, lockfile_path: Path) -> None:
    """Extract embeddable CPython 3.13, install hash-locked runtime wheels, and write python313._pth."""
    python_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(python_dir)

    for rel in REQUIRED_EMBED_FILES:
        if not (python_dir / rel).is_file():
            raise RuntimeError(f"Missing expected embeddable runtime file: {python_dir / rel}")

    site_packages = python_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    pip_cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--no-compile",
        "--require-hashes",
        "--no-deps",
        "--python-version",
        PYTHON_MAJOR_MINOR,
        "--platform",
        TARGET_PLATFORM,
        "--implementation",
        "cp",
        "--abi",
        PYTHON_ABI,
        "--only-binary=:all:",
        "--target",
        str(site_packages),
        "-r",
        str(lockfile_path),
    ]
    print(f"Installing hash-locked CPython {PYTHON_VERSION} runtime dependencies ...")
    subprocess.run(pip_cmd, check=True)

    # Remove bytecode caches or bin wrappers accidentally created in site-packages
    for cache_dir in site_packages.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc in site_packages.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)

    pth_path = python_dir / "python314._pth"
    pth_content = "\n".join(
        [
            "python314.zip",
            ".",
            "Lib\\site-packages",
            "..\\core",
            "import site",
            "",
        ]
    )
    pth_path.write_text(pth_content, encoding="utf-8")


def _ignore_dev_entries(_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lower = name.lower()
        if (
            name in CORE_FORBIDDEN_DIR_NAMES
            or name in FORBIDDEN_FILENAMES
            or lower.startswith(".env.")
            or lower.endswith(FORBIDDEN_SUFFIXES)
        ):
            ignored.add(name)
    return ignored


def stage_core_payload(repo_root: Path, core_dir: Path) -> None:
    """Copy minimal PICO Core source and migration assets without dev/secret artifacts."""
    core_dir.mkdir(parents=True, exist_ok=True)

    for rel_dir in ("pico", "alembic"):
        src_dir = repo_root / rel_dir
        if not src_dir.is_dir():
            raise FileNotFoundError(f"Missing required Core directory: {src_dir}")
        shutil.copytree(src_dir, core_dir / rel_dir, ignore=_ignore_dev_entries)

    for rel_file in ("alembic.ini", "pyproject.toml"):
        src_file = repo_root / rel_file
        if not src_file.is_file():
            raise FileNotFoundError(f"Missing required Core file: {src_file}")
        shutil.copy2(src_file, core_dir / rel_file)


def verify_payload_hygiene(core_dir: Path, python_dir: Path) -> tuple[int, int, int]:
    """Ensure staged payload contains zero dev/test/secret/database files."""
    violations: list[str] = []
    core_files = 0
    python_files = 0
    total_bytes = 0

    for label, root, forbidden_dirs in (
        ("core", core_dir, CORE_FORBIDDEN_DIR_NAMES),
        ("python", python_dir, PYTHON_FORBIDDEN_DIR_NAMES),
    ):
        for item in root.rglob("*"):
            rel = item.relative_to(root)
            parts_lower = {part.lower() for part in rel.parts}
            if parts_lower & forbidden_dirs:
                violations.append(f"{label}/{rel.as_posix()} (forbidden directory)")
                continue
            if item.is_file():
                name_lower = item.name.lower()
                if (
                    name_lower in FORBIDDEN_FILENAMES
                    or name_lower.startswith(".env.")
                    or name_lower.endswith(FORBIDDEN_SUFFIXES)
                ):
                    violations.append(f"{label}/{rel.as_posix()} (forbidden file)")
                total_bytes += item.stat().st_size
                if label == "core":
                    core_files += 1
                else:
                    python_files += 1

    # Explicitly confirm pytest / standalone tests package was not installed into site-packages
    site_packages = python_dir / "Lib" / "site-packages"
    for forbidden_pkg in ("pytest", "tests"):
        if (site_packages / forbidden_pkg).exists():
            violations.append(f"python/Lib/site-packages/{forbidden_pkg} (dev package leaked)")

    if not (core_dir / "pico" / "main.py").is_file():
        violations.append("missing core/pico/main.py")
    if not (core_dir / "pyproject.toml").is_file():
        violations.append("missing core/pyproject.toml")
    if not (core_dir / "alembic.ini").is_file():
        violations.append("missing core/alembic.ini")
    if not (python_dir / "python.exe").is_file():
        violations.append("missing python/python.exe")

    if violations:
        raise RuntimeError("Staged payload hygiene check failed:\n  - " + "\n  - ".join(violations))

    return core_files, python_files, total_bytes


def verify_staged_python_imports(core_dir: Path, python_dir: Path) -> None:
    """Verify staged embeddable python.exe boots, isolates dev packages, and serves pico.main."""
    python_exe = python_dir / "python.exe"
    with tempfile.TemporaryDirectory(prefix="pico_stage_db_") as tmp_dir:
        db_path = Path(tmp_dir) / "stage_verify.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        probe_code = (
            "import asyncio, importlib, sys; "
            "assert sys.version_info[:3] == (3, 14, 10), sys.version_info; "
            "mods = ['fastapi', 'uvicorn', 'pydantic', 'pydantic_settings', "
            "'sqlalchemy', 'alembic', 'httpx', 'anyio', 'sounddevice', '_cffi_backend', 'pico.main']; "
            "[importlib.import_module(m) for m in mods]; "
            "blocked = False\n"
            "try:\n"
            "    import pytest\n"
            "except ModuleNotFoundError:\n"
            "    blocked = True\n"
            "assert blocked, 'pytest unexpectedly importable in staged runtime'; "
            "import httpx\n"
            "from pico.main import app\n"
            "assert app.title, 'FastAPI app title missing'\n"
            "async def _check_asgi():\n"
            "    async with app.router.lifespan_context(app):\n"
            "        transport = httpx.ASGITransport(app=app)\n"
            "        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:\n"
            "            r = await client.get('/health')\n"
            "            assert r.status_code == 200, r.text\n"
            "asyncio.run(_check_asgi())\n"
            "assert __import__('pathlib').Path(sys.argv[1]).is_file(), 'DB not created'\n"
            "print('STAGED_IMPORT_OK', sys.version.split()[0], app.title)"
        )

        clean_env = {
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "PATH": os.environ.get("PATH", ""),
            "TEMP": tmp_dir,
            "TMP": tmp_dir,
            "DATABASE_URL": db_url,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = subprocess.run(
            [str(python_exe), "-B", "-c", probe_code, str(db_path)],
            cwd=str(core_dir),
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Staged Python verification failed (exit {proc.returncode}):\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        print(proc.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Path to the PICO repository root.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Staging output root (defaults to <repo-root>/frontend/src-tauri/staged).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Download cache directory (defaults to <output-dir>/.cache).",
    )
    parser.add_argument(
        "--skip-import-check",
        action="store_true",
        help="Skip executing python.exe import verification (non-Windows staging only).",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    staged_root = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else repo_root / "frontend" / "src-tauri" / "staged"
    )
    cache_dir = (
        args.cache_dir.resolve() if args.cache_dir is not None else staged_root / ".cache"
    )

    core_dir = staged_root / "core"
    python_dir = staged_root / "python"

    # Clean previous staged payload while preserving .cache
    shutil.rmtree(core_dir, ignore_errors=True)
    shutil.rmtree(python_dir, ignore_errors=True)

    lockfile_path = repo_root / "tools" / "packaging" / "requirements-windows-runtime.lock"
    locked_specs = load_locked_runtime_requirements(lockfile_path, repo_root / "requirements.txt")
    archive_path = ensure_verified_embed_zip(cache_dir)
    stage_python_runtime(archive_path, python_dir, lockfile_path)
    stage_core_payload(repo_root, core_dir)
    core_files, python_files, total_bytes = verify_payload_hygiene(core_dir, python_dir)

    if not args.skip_import_check:
        verify_staged_python_imports(core_dir, python_dir)

    manifest = {
        "python_version": PYTHON_VERSION,
        "target_platform": TARGET_PLATFORM,
        "target_abi": PYTHON_ABI,
        "embed_archive": EMBED_FILENAME,
        "embed_sha256": EMBED_SHA256,
        "runtime_lockfile": lockfile_path.relative_to(repo_root).as_posix(),
        "runtime_lockfile_sha256": sha256_file(lockfile_path),
        "runtime_requirements": locked_specs,
        "locked_wheel_count": len(locked_specs),
        "excluded_dev_packages": sorted(DEV_ONLY_PACKAGES),
        "core_file_count": core_files,
        "python_file_count": python_files,
        "total_staged_bytes": total_bytes,
        "hygiene_verified": True,
        "import_smoke_verified": not args.skip_import_check,
    }
    manifest_path = staged_root / "staging-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Staging manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
