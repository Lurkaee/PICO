"""Unified diff calculation and bounded inspection utilities for Milestone P8.2."""

import difflib
from pico.core.config import settings
from pico.core.errors import ValidationError


def generate_unified_diff(
    original_text: str | None,
    proposed_text: str,
    relative_path: str,
    max_diff_bytes: int | None = None,
) -> str:
    """
    Generate deterministic unified diff string between original and proposed content.
    For file creation (original_text is None), treats original as empty /dev/null.
    Enforces maximum diff size limit.
    """
    effective_max_diff = (
        max_diff_bytes if max_diff_bytes is not None else settings.PICO_FILESYSTEM_MAX_DIFF_BYTES
    )

    clean_filename = relative_path.replace("\\", "/")
    from_file = "/dev/null" if original_text is None else f"a/{clean_filename}"
    to_file = f"b/{clean_filename}"

    # Split lines preserving newlines
    if original_text is None:
        original_lines: list[str] = []
    else:
        norm_orig = original_text.replace("\r\n", "\n").replace("\r", "\n")
        original_lines = norm_orig.splitlines(keepends=True)

    norm_prop = proposed_text.replace("\r\n", "\n").replace("\r", "\n")
    proposed_lines = norm_prop.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="\n",
        )
    )

    unified_diff_str = "".join(diff_lines)
    diff_bytes = unified_diff_str.encode("utf-8")
    if len(diff_bytes) > effective_max_diff:
        raise ValidationError(
            f"Generated diff size {len(diff_bytes)} bytes exceeds maximum allowed limit of {effective_max_diff} bytes."
        )

    return unified_diff_str
