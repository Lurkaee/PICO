"""Explicit application allowlist configuration, validation, and registry."""

import hashlib
import json
import os
import re
from typing import Any

from pico.core.errors import ValidationError
from pico.os_automation.contracts import LaunchProfile

PROHIBITED_EXECUTABLE_NAMES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "bash.exe",
    "sh.exe",
    "zsh.exe",
    "wscript.exe",
    "cscript.exe",
    "python.exe",
    "pythonw.exe",
    "conhost.exe",
    "wt.exe",
    "cmd",
    "powershell",
    "pwsh",
    "bash",
    "sh",
    "python",
}

DISALLOWED_SCRIPT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".js",
    ".wsf",
    ".sh",
    ".bash",
    ".py",
    ".msc",
}

ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_launch_profile(data: dict[str, Any] | LaunchProfile) -> LaunchProfile:
    """
    Strictly validates a launch profile before registration into the allowlist.
    Fails closed on any ambiguity, traversal, shell interpreter, or invalid path.
    """
    if isinstance(data, LaunchProfile):
        profile = data
    else:
        try:
            profile = LaunchProfile.model_validate(data)
        except Exception as exc:
            raise ValidationError(f"Malformed launch profile configuration: {str(exc)}")

    # 1. Validate alias
    if not ALIAS_PATTERN.match(profile.alias):
        raise ValidationError(
            f"Invalid application alias '{profile.alias}': must match alphanumeric, underscore, or hyphen."
        )

    # 2. Check for path traversal or env expansion in alias
    if ".." in profile.alias or "/" in profile.alias or "\\" in profile.alias:
        raise ValidationError(f"Path traversal detected in alias '{profile.alias}'.")

    # 3. Validate executable path
    raw_path = profile.executable_path.strip()
    if not raw_path:
        raise ValidationError("Executable path cannot be empty.")

    if "%" in raw_path or "$" in raw_path:
        raise ValidationError(
            f"Environment variable expansion in executable path is forbidden: '{raw_path}'."
        )

    if not os.path.isabs(raw_path):
        raise ValidationError(
            f"Executable path must be absolute, received relative path: '{raw_path}'."
        )

    # Check for path traversal sequence before normalization collapses it
    parts = raw_path.replace("/", "\\").split("\\")
    if ".." in parts:
        raise ValidationError(
            f"Path traversal sequence '..' detected in executable path: '{raw_path}'."
        )

    normalized_path = os.path.normpath(raw_path)

    base_name = os.path.basename(normalized_path).lower()
    root_ext = os.path.splitext(base_name)[1].lower()

    # Reject interpreters and script runners
    if base_name in PROHIBITED_EXECUTABLE_NAMES:
        raise ValidationError(
            f"Prohibited executable '{base_name}': shells and interpreters cannot be allowlisted."
        )

    if root_ext in DISALLOWED_SCRIPT_EXTENSIONS:
        raise ValidationError(
            f"Prohibited script extension '{root_ext}': only direct binary executables (.exe) are permitted."
        )

    # On Windows, must be .exe
    if os.name == "nt" and root_ext != ".exe":
        raise ValidationError(
            f"Invalid executable format '{root_ext}': Windows allowlisted executables must have .exe extension."
        )

    # 4. Validate working directory if specified
    if profile.working_dir:
        raw_cwd = profile.working_dir.strip()
        if "%" in raw_cwd or "$" in raw_cwd:
            raise ValidationError("Environment variable expansion in working directory is forbidden.")
        if not os.path.isabs(raw_cwd):
            raise ValidationError(f"Working directory must be an absolute path: '{raw_cwd}'.")
        cwd_parts = raw_cwd.replace("/", "\\").split("\\")
        if ".." in cwd_parts:
            raise ValidationError("Path traversal sequence '..' detected in working directory.")
        norm_cwd = os.path.normpath(raw_cwd)

    # 5. Validate args are fixed strings without caller manipulation
    for arg in profile.args:
        if not isinstance(arg, str):
            raise ValidationError(f"Launch argument must be a string: {arg}")

    return profile


class ApplicationAllowlistManager:
    """
    Manages the server-side trusted application allowlist.
    Default allowlist is strictly empty.
    """

    def __init__(self, raw_config: str | dict[str, Any] | None = None) -> None:
        self._profiles: dict[str, LaunchProfile] = {}
        if raw_config:
            self.load_from_config(raw_config)

    def load_from_config(self, raw_config: str | dict[str, Any]) -> None:
        """Parses and validates a JSON string or dict mapping alias -> profile."""
        if isinstance(raw_config, str):
            clean_str = raw_config.strip()
            if not clean_str or clean_str == "{}":
                self._profiles.clear()
                return
            try:
                parsed = json.loads(clean_str)
            except Exception as exc:
                raise ValidationError(f"Failed to parse OS_APPLICATION_ALLOWLIST JSON: {str(exc)}")
        elif isinstance(raw_config, dict):
            parsed = raw_config
        else:
            raise ValidationError(
                f"Invalid allowlist configuration format: expected string or dict, got {type(raw_config).__name__}"
            )

        if not isinstance(parsed, dict):
            raise ValidationError("Application allowlist root must be a JSON object / dict.")

        loaded: dict[str, LaunchProfile] = {}
        for alias, item in parsed.items():
            if not isinstance(item, dict):
                raise ValidationError(f"Profile for alias '{alias}' must be a dictionary.")
            profile_data = dict(item)
            profile_data.setdefault("alias", alias)
            profile = validate_launch_profile(profile_data)
            loaded[profile.alias] = profile

        self._profiles = loaded

    def register_profile(self, profile: LaunchProfile | dict[str, Any]) -> None:
        validated = validate_launch_profile(profile)
        self._profiles[validated.alias] = validated

    def get_profile(self, alias: str) -> LaunchProfile | None:
        clean_alias = (alias or "").strip()
        return self._profiles.get(clean_alias)

    def has_alias(self, alias: str) -> bool:
        clean_alias = (alias or "").strip()
        return clean_alias in self._profiles

    def list_aliases(self) -> list[str]:
        return sorted(list(self._profiles.keys()))

    def clear(self) -> None:
        self._profiles.clear()


def compute_launch_profile_fingerprint(profile: LaunchProfile | dict[str, Any]) -> str:
    """
    Computes a canonical SHA-256 fingerprint of execution-relevant profile settings.
    Binds application alias, normalized executable path, ordered args, and normalized working directory.
    """
    if not isinstance(profile, LaunchProfile):
        profile = validate_launch_profile(profile)

    payload = {
        "alias": profile.alias,
        "args": list(profile.args),
        "executable_path": os.path.normpath(profile.executable_path),
        "working_dir": os.path.normpath(profile.working_dir) if profile.working_dir else None,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
