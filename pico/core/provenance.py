"""Authoritative Runtime and Build Provenance Resolution for PICO (Milestone P11.1).

Resolves the Git or build commit associated with the running application:
- Verifies build-time environment commitments against Git HEAD when Git metadata exists.
- Enforces clean working-tree invariants when running inside Git checkouts with require_clean=True.
- Rejects malformed, unverified, or mismatched commit identities.
- Supports packaged production environments lacking Git metadata via authoritative PICO_BUILD_COMMIT.
- Fails closed (returns None) when runtime identity cannot be established.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import subprocess

from pico.core.config import settings

logger = logging.getLogger(__name__)

HEX_40_REGEX = re.compile(r"^[0-9a-fA-F]{40}$")


def resolve_active_runtime_commit(
    require_clean: bool = False,
    repo_root: Path | None = None,
) -> str | None:
    """Resolve the authoritative 40-character commit SHA for PICO runtime or benchmark execution.

    Resolution precedence & verification rules:
    1. Inspect build-time environment / configuration for an authoritative identity:
       (PICO_BUILD_COMMIT, PICO_GIT_COMMIT, GIT_COMMIT, settings.PICO_BUILD_COMMIT).
       If present, it must strictly match the 40-hexadecimal SHA format; malformed values fail closed.
    2. Check if running inside a Git checkout (by testing git rev-parse HEAD).
       - If Git metadata is available:
         a) Resolve actual Git HEAD.
         b) If an environment / build commit was supplied, verify it strictly matches Git HEAD.
            Any mismatch fails closed.
         c) If require_clean is True, inspect git status --porcelain. If tracked files
            contain uncommitted changes (staged or unstaged modifications), fail closed.
         d) Return the verified 40-hex commit SHA in lowercase.
       - If Git metadata is NOT available (e.g., packaged production containers/wheels):
         a) If an authoritative 40-hex build commit was provided in configuration, accept it.
         b) If no build commit was provided, fail closed (returns None).

    Returns:
        str | None: 40-character lowercase hex commit SHA, or None if identity cannot be established.
    """
    # 1. Inspect build-time environment / configuration
    raw_env_commit = (
        os.environ.get("PICO_BUILD_COMMIT")
        or getattr(settings, "PICO_BUILD_COMMIT", None)
        or os.environ.get("PICO_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
    )
    build_commit: str | None = None
    if raw_env_commit and str(raw_env_commit).strip():
        val = str(raw_env_commit).strip()
        if not HEX_40_REGEX.fullmatch(val):
            logger.warning("Configured build commit '%s' is not a valid 40-hex SHA.", val)
            return None
        build_commit = val.lower()

    # 2. Probe Git metadata
    cwd = str(repo_root) if repo_root is not None else None
    git_head: str | None = None
    has_git = False

    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            cwd=cwd,
        )
        if res.returncode == 0 and res.stdout.strip():
            head_val = res.stdout.strip()
            if HEX_40_REGEX.fullmatch(head_val):
                git_head = head_val.lower()
                has_git = True
    except Exception:
        has_git = False

    if has_git and git_head:
        # If build commit was supplied, verify it matches Git HEAD
        if build_commit and build_commit != git_head:
            logger.warning(
                "Configured build commit '%s' does not match actual Git HEAD '%s'.",
                build_commit,
                git_head,
            )
            return None

        # If clean tree is required, enforce it
        if require_clean:
            try:
                status_res = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                    cwd=cwd,
                )
                if status_res.returncode != 0:
                    logger.warning("git status check failed with returncode %d.", status_res.returncode)
                    return None
                dirty_lines = [
                    l for l in status_res.stdout.splitlines()
                    if l.strip() and not l.startswith("??")
                ]
                if dirty_lines:
                    logger.warning("Git checkout has uncommitted tracked changes: %s", dirty_lines[:3])
                    return None
            except Exception as e:
                logger.warning("Failed to execute git status: %s", e)
                return None

        return git_head

    # Git metadata not available: fallback to authoritative build identity if present
    if build_commit:
        return build_commit

    # Cannot establish runtime identity
    return None
