"""Contracts, models, and validation routines for Milestone P8.2 Staged Filesystem Diffs."""

from enum import Enum
import hashlib
import os
import re
import stat
from typing import Any
from pydantic import BaseModel, Field

from pico.core.config import settings
from pico.core.errors import ValidationError

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


class OperationType(str, Enum):
    CREATE = "create"
    REPLACE = "replace"


class ProposalStatus(str, Enum):
    STAGED = "staged"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    DISCARDED = "discarded"


def is_symlink_or_reparse_point(path: str) -> bool:
    """
    Detect whether path is a symbolic link, directory junction, or Windows reparse point.
    Fails closed if inspection encounters errors.
    """
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
        reparse_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if hasattr(st, "st_file_attributes") and (st.st_file_attributes & reparse_attr):
            return True
        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag != 0:
            return True
    except (OSError, ValueError):
        # If path does not exist, lstat raises FileNotFoundError, which is fine
        pass
    return False


def validate_relative_path(relative_path: str) -> str:
    """
    Validate and normalize a workspace-relative path.
    Rejects path traversal, alternate streams, Windows device names,
    environment variables, UNC paths, and absolute paths.
    """
    if not relative_path or not relative_path.strip():
        raise ValidationError("Relative path cannot be empty.")

    cleaned = relative_path.strip()

    # Reject backslash/forward-slash UNC, device prefix, or absolute path
    if (
        cleaned.startswith("/")
        or cleaned.startswith("\\")
        or (len(cleaned) >= 2 and cleaned[1] == ":")
        or cleaned.startswith("//")
        or cleaned.startswith("\\\\")
    ):
        raise ValidationError(f"Absolute or UNC paths are not permitted: '{relative_path}'.")

    # Reject device path prefixes
    if cleaned.startswith("\\\\.\\") or cleaned.startswith("\\\\?\\"):
        raise ValidationError(f"Device paths are not permitted: '{relative_path}'.")

    # Reject environment variable expansion attempts
    if "%" in cleaned or "$" in cleaned:
        raise ValidationError("Environment variable expansion is not permitted in relative paths.")

    # Reject alternate data streams (colon)
    if ":" in cleaned:
        raise ValidationError("Alternate data streams or colons are not permitted in relative paths.")

    # Normalize slashes to forward slashes
    normalized = cleaned.replace("\\", "/")

    # Split into components and inspect each
    parts = normalized.split("/")
    safe_parts: list[str] = []

    for part in parts:
        part_clean = part.strip()
        if not part_clean or part_clean == ".":
            continue
        if part_clean == "..":
            raise ValidationError("Path traversal ('..') is not permitted.")

        # Check Windows reserved names (e.g. NUL, CON, COM1, or with extension like NUL.txt)
        base_name = part_clean.split(".")[0].upper()
        if base_name in WINDOWS_RESERVED_NAMES:
            raise ValidationError(f"Reserved device name '{part_clean}' is not permitted.")

        # Reject control characters or invalid characters
        if re.search(r'[\x00-\x1f<>:"|?*]', part_clean):
            raise ValidationError(f"Path component contains illegal characters: '{part_clean}'.")

        safe_parts.append(part_clean)

    if not safe_parts:
        raise ValidationError("Path must contain at least one valid filename component.")

    return "/".join(safe_parts)


def validate_text_content(
    content: str,
    max_bytes: int | None = None,
    max_line_length: int | None = None,
) -> str:
    """
    Validate and normalize text content under bounded text-file contract.
    Normalizes newlines to '\\n', checks for NUL bytes, maximum file size,
    and maximum line length.
    """
    if content is None:
        raise ValidationError("Content cannot be None.")

    effective_max_bytes = max_bytes if max_bytes is not None else settings.PICO_FILESYSTEM_MAX_FILE_BYTES
    effective_max_line = max_line_length if max_line_length is not None else settings.PICO_FILESYSTEM_MAX_LINE_LENGTH

    # Detect NUL bytes / binary content
    if "\x00" in content:
        raise ValidationError("Binary content detected: NUL bytes are not permitted.")

    # Canonical newline normalization: normalize \r\n and \r to \n
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    # Byte size check on UTF-8 encoding
    encoded_bytes = normalized.encode("utf-8")
    if len(encoded_bytes) > effective_max_bytes:
        raise ValidationError(
            f"Content size {len(encoded_bytes)} bytes exceeds maximum allowed limit of {effective_max_bytes} bytes."
        )

    # Line length check
    lines = normalized.split("\n")
    for idx, line in enumerate(lines, start=1):
        if len(line) > effective_max_line:
            raise ValidationError(
                f"Line {idx} length ({len(line)}) exceeds maximum allowed limit of {effective_max_line} characters."
            )

    return normalized


def compute_content_digest(content: str) -> str:
    """Calculate deterministic SHA-256 digest of canonical UTF-8 text content."""
    # Ensure content has canonical newlines
    canonical = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_diff_digest(diff_text: str) -> str:
    """Calculate deterministic SHA-256 digest of unified diff text."""
    canonical = diff_text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
