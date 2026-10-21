"""Server-side filesystem roots allowlist configuration and safe path resolution."""

import json
import os
from typing import Any

from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.filesystem.contracts import is_symlink_or_reparse_point, validate_relative_path


class FilesystemRootsManager:
    """
    Manages server-configured allowed filesystem roots and safe path resolution.
    Empty by default. Requires explicit server-side configuration.
    """

    def __init__(self, raw_config: str | dict[str, str] | None = None) -> None:
        self._roots: dict[str, str] = {}
        self._load_config(raw_config)

    def _load_config(self, raw_config: str | dict[str, str] | None) -> None:
        config_data = raw_config
        if config_data is None:
            config_data = getattr(settings, "PICO_FILESYSTEM_ALLOWED_ROOTS", "{}")

        if isinstance(config_data, str):
            cleaned = config_data.strip()
            if not cleaned or cleaned == "{}":
                self._roots = {}
                return
            try:
                parsed = json.loads(cleaned)
            except Exception as exc:
                raise ValidationError(f"Invalid PICO_FILESYSTEM_ALLOWED_ROOTS JSON format: {exc}")
        elif isinstance(config_data, dict):
            parsed = config_data
        else:
            raise ValidationError("PICO_FILESYSTEM_ALLOWED_ROOTS must be a JSON string or dictionary.")

        if not isinstance(parsed, dict):
            raise ValidationError("PICO_FILESYSTEM_ALLOWED_ROOTS must be a mapping of root_id to directory path.")

        resolved: dict[str, str] = {}
        for root_id, path_str in parsed.items():
            if not isinstance(root_id, str) or not root_id.strip():
                raise ValidationError(f"Invalid root identifier: {root_id}")
            if not isinstance(path_str, str) or not path_str.strip():
                raise ValidationError(f"Invalid path for root '{root_id}': {path_str}")

            clean_path = path_str.strip()
            if not os.path.isabs(clean_path):
                raise ValidationError(f"Filesystem root '{root_id}' path must be absolute: '{clean_path}'")
            if not os.path.exists(clean_path):
                raise ValidationError(f"Filesystem root '{root_id}' path does not exist: '{clean_path}'")
            if not os.path.isdir(clean_path):
                raise ValidationError(f"Filesystem root '{root_id}' path is not a directory: '{clean_path}'")
            if is_symlink_or_reparse_point(clean_path):
                raise ValidationError(f"Filesystem root '{root_id}' cannot be a symlink or reparse point: '{clean_path}'")

            canonical = os.path.realpath(os.path.abspath(clean_path))
            resolved[root_id.strip()] = canonical

        self._roots = resolved

    def list_roots(self) -> dict[str, str]:
        """Return a copy of configured root_id to canonical path mappings."""
        return dict(self._roots)

    def get_root_path(self, root_id: str) -> str:
        """Resolve root_id to canonical absolute directory path or raise ValidationError."""
        if not self._roots:
            raise ValidationError("Filesystem operations are disabled: No filesystem roots are allowlisted.")
        if root_id not in self._roots:
            raise ValidationError(f"Filesystem root '{root_id}' is not in the server-configured allowlist.")
        return self._roots[root_id]

    def resolve_safe_path(
        self,
        root_id: str,
        relative_path: str,
        must_exist: bool = False,
        parent_must_exist: bool = True,
    ) -> tuple[str, str]:
        """
        Safely resolve a relative path within an allowlisted root.
        Enforces containment, detects path traversal and reparse points/symlinks,
        and verifies existence constraints.
        Returns (canonical_root, canonical_target).
        """
        clean_rel = validate_relative_path(relative_path)
        canonical_root = self.get_root_path(root_id)

        # Inspect all existing path components between root and candidate for symlinks/reparse points
        rel_parts = clean_rel.split("/")
        current = canonical_root
        for part in rel_parts:
            current = os.path.join(current, part)
            if os.path.lexists(current):
                if is_symlink_or_reparse_point(current):
                    raise ValidationError(
                        f"Symlink or reparse point detected in path component: '{part}'"
                    )

        # Construct candidate target path
        candidate = os.path.normpath(os.path.join(canonical_root, clean_rel))

        # Check path containment: both lexical containment and resolved realpath containment
        lex_root = os.path.normcase(os.path.abspath(canonical_root))
        lex_candidate = os.path.normcase(os.path.abspath(candidate))
        try:
            lex_common = os.path.commonpath([lex_root, lex_candidate])
        except ValueError:
            raise ValidationError(f"Path escapes root '{root_id}': '{relative_path}'")
        if lex_common != lex_root or lex_candidate == lex_root:
            raise ValidationError(f"Path escapes or equals root '{root_id}': '{relative_path}'")

        norm_root = os.path.normcase(os.path.realpath(canonical_root))
        norm_candidate = os.path.normcase(os.path.realpath(candidate))

        try:
            common = os.path.commonpath([norm_root, norm_candidate])
        except ValueError:
            raise ValidationError(f"Path escapes root '{root_id}': '{relative_path}'")

        if common != norm_root or norm_candidate == norm_root:
            raise ValidationError(f"Path escapes or equals root '{root_id}': '{relative_path}'")

        # Check parent directory constraints
        parent_dir = os.path.dirname(candidate)
        if parent_must_exist:
            if not os.path.exists(parent_dir):
                raise ValidationError(f"Parent directory does not exist for '{relative_path}'.")
            if not os.path.isdir(parent_dir):
                raise ValidationError(f"Parent path is not a directory for '{relative_path}'.")

        if os.path.lexists(candidate):
            if is_symlink_or_reparse_point(candidate):
                raise ValidationError(f"Target file is a symlink or reparse point: '{relative_path}'.")

        # Check target existence constraints
        if must_exist:
            if not os.path.exists(candidate):
                raise ValidationError(f"Target file does not exist: '{relative_path}'.")
            if not os.path.isfile(candidate):
                raise ValidationError(f"Target path is not a regular file: '{relative_path}'.")
            if is_symlink_or_reparse_point(candidate):
                raise ValidationError(f"Target file is a symlink or reparse point: '{relative_path}'.")

        return canonical_root, candidate


_roots_manager: FilesystemRootsManager | None = None


def get_roots_manager() -> FilesystemRootsManager:
    """Get the active global FilesystemRootsManager singleton."""
    global _roots_manager
    if _roots_manager is None:
        _roots_manager = FilesystemRootsManager()
    return _roots_manager


def set_roots_manager(manager: FilesystemRootsManager | None) -> None:
    """Override or reset the active FilesystemRootsManager singleton."""
    global _roots_manager
    _roots_manager = manager
