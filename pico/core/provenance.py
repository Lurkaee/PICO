"""Authoritative Runtime and Build Provenance Resolution for PICO (Milestone P11.1).

Resolves the Git or build commit associated with the running application:
- Verifies build-time environment commitments against Git HEAD when Git metadata exists.
- Enforces clean working-tree invariants (no uncommitted tracked modifications, staged changes,
  or untracked non-ignored source files) when running inside Git checkouts with require_clean=True.
- Rejects malformed, unverified, conflicting, or mismatched commit identities.
- Supports packaged production environments lacking Git metadata strictly via authoritative PICO_BUILD_COMMIT.
- Rejects PICO_GIT_COMMIT or GIT_COMMIT alone as proof of build identity when Git metadata is absent.
- Fails closed (returns None) without fabricating or inferring a SHA when runtime identity cannot be established.
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
    1. Inspect environment / configuration for commit identities:
       - PICO_BUILD_COMMIT (authoritative build-time identity, via env or settings.PICO_BUILD_COMMIT)
       - PICO_GIT_COMMIT (developer / checkout override)
       - GIT_COMMIT (generic CI git commit variable)
       If any provided value is malformed (not 40-hex SHA), fail closed (returns None).
       If multiple variables are provided with conflicting values, fail closed (returns None).
    2. Check if running inside a Git checkout (by testing git rev-parse HEAD):
       - If Git metadata is available:
         a) Resolve actual Git HEAD via `git rev-parse HEAD`.
         b) If any environment / build commit was supplied, verify it strictly matches Git HEAD.
            Any mismatch fails closed.
         c) If require_clean is True, inspect `git status --porcelain`.
            If any tracked changes (staged or unstaged) or non-ignored untracked files
            are present, fail closed.
         d) Return the verified 40-hex commit SHA in lowercase.
       - If Git metadata is NOT available (e.g., packaged production containers/wheels):
         a) Only accept a valid authoritative PICO_BUILD_COMMIT (from env or validated settings).
         b) Do NOT accept PICO_GIT_COMMIT or GIT_COMMIT alone as proof of build identity.
         c) If authoritative PICO_BUILD_COMMIT is absent or invalid, fail closed (returns None).
            Never fabricate or infer a SHA.

    Returns:
        str | None: 40-character lowercase hex commit SHA, or None if identity cannot be established.
    """
    # 1. Collect and validate all supplied commit variables
    env_vars: dict[str, str] = {}
    pico_build_val = os.environ.get("PICO_BUILD_COMMIT") or getattr(settings, "PICO_BUILD_COMMIT", None)
    if pico_build_val and str(pico_build_val).strip():
        env_vars["PICO_BUILD_COMMIT"] = str(pico_build_val).strip()

    pico_git_val = os.environ.get("PICO_GIT_COMMIT")
    if pico_git_val and str(pico_git_val).strip():
        env_vars["PICO_GIT_COMMIT"] = str(pico_git_val).strip()

    git_commit_val = os.environ.get("GIT_COMMIT")
    if git_commit_val and str(git_commit_val).strip():
        env_vars["GIT_COMMIT"] = str(git_commit_val).strip()

    # Check format for each supplied variable
    for name, raw_val in env_vars.items():
        if not HEX_40_REGEX.fullmatch(raw_val):
            logger.warning("Configured commit %s '%s' is not a valid 40-hex SHA.", name, raw_val)
            return None

    # Check for conflicts among supplied variables
    normalized_vars = {k: v.lower() for k, v in env_vars.items()}
    unique_commits = set(normalized_vars.values())
    if len(unique_commits) > 1:
        logger.warning(
            "Conflicting commit identities provided across environment/settings: %s",
            normalized_vars,
        )
        return None

    configured_commit = next(iter(unique_commits)) if unique_commits else None
    authoritative_build_commit = normalized_vars.get("PICO_BUILD_COMMIT")

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
        # If any commit was configured in env/settings, verify it matches Git HEAD
        if configured_commit and configured_commit != git_head:
            logger.warning(
                "Configured commit '%s' does not match actual Git HEAD '%s'.",
                configured_commit,
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
                dirty_lines = [l for l in status_res.stdout.splitlines() if l.strip()]
                if dirty_lines:
                    logger.warning(
                        "Git checkout is not clean (uncommitted or untracked changes present): %s",
                        dirty_lines[:3],
                    )
                    return None
            except Exception as e:
                logger.warning("Failed to execute git status: %s", e)
                return None

        return git_head

    # Git metadata not available:
    # Strictly require authoritative PICO_BUILD_COMMIT; reject PICO_GIT_COMMIT or GIT_COMMIT alone.
    if authoritative_build_commit:
        return authoritative_build_commit

    if configured_commit:
        logger.warning(
            "Git metadata is absent; PICO_GIT_COMMIT and GIT_COMMIT cannot establish authoritative "
            "build provenance without PICO_BUILD_COMMIT. Failing closed."
        )
        return None

    return None
