"""Environment sanitization and credential isolation for child processes."""

import os
from typing import Mapping

SENSITIVE_ENV_PREFIXES = (
    "PICO_",
    "APPROVAL_",
    "HOME_ASSISTANT_",
    "HA_",
    "CLOUD_",
    "OLLAMA_",
    "SECRET",
    "TOKEN",
    "KEY",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
)

ALLOWED_SYSTEM_ENV_VARS = {
    "SystemRoot",
    "SystemDrive",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
}


def build_sanitized_environment(
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Construct a minimal, explicitly controlled child environment from necessary
    operating-system variables, strictly excluding PICO-specific secrets and credentials.
    """
    base = source_env if source_env is not None else os.environ
    clean_env: dict[str, str] = {}

    for k, v in base.items():
        k_upper = k.upper()

        # 1. Strictly exclude anything matching sensitive prefixes
        if any(k_upper.startswith(prefix) for prefix in SENSITIVE_ENV_PREFIXES):
            continue

        # 2. Retain only allowed system variables
        if any(k_upper == allowed.upper() for allowed in ALLOWED_SYSTEM_ENV_VARS):
            clean_env[k] = v

    return clean_env
