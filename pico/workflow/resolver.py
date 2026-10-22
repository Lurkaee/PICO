"""Input template resolver for workflow steps (Milestone P9.1)."""

from typing import Any
from pico.capabilities.base import BaseCapability
from pico.core.errors import ValidationError


def get_nested_path(data: Any, path: str) -> Any:
    """
    Extracts a value from a nested dictionary or list using dot-separated keys.
    e.g. 'result' or 'meta.id' or 'items.0.name'.
    """
    if not path or path == "":
        return data

    parts = path.strip().split(".")
    curr = data

    for part in parts:
        if isinstance(curr, dict):
            if part not in curr:
                raise KeyError(f"Key '{part}' not found in output mapping.")
            curr = curr[part]
        elif isinstance(curr, list):
            try:
                idx = int(part)
                curr = curr[idx]
            except (ValueError, IndexError):
                raise KeyError(f"Index '{part}' invalid or out of range in list.")
        else:
            raise KeyError(f"Cannot access attribute '{part}' on primitive value '{curr}'.")

    return curr


def resolve_template_value(val: Any, completed_outputs: dict[str, dict[str, Any]]) -> Any:
    """Recursively resolves $from_step references in values."""
    if isinstance(val, dict):
        if "$from_step" in val:
            source_step = str(val["$from_step"]).strip()
            path = str(val.get("$path") or val.get("$output_field") or "").strip()

            if source_step not in completed_outputs:
                raise ValidationError(
                    f"Dependency output for step '{source_step}' is not available."
                )

            upstream_output = completed_outputs[source_step]
            try:
                return get_nested_path(upstream_output, path)
            except KeyError as err:
                raise ValidationError(
                    f"Output of step '{source_step}' does not contain required path '{path}': {str(err)}"
                )

        return {k: resolve_template_value(v, completed_outputs) for k, v in val.items()}

    elif isinstance(val, list):
        return [resolve_template_value(item, completed_outputs) for item in val]

    return val


def resolve_step_input(
    input_template: dict[str, Any],
    completed_outputs: dict[str, dict[str, Any]],
    capability: BaseCapability,
) -> dict[str, Any]:
    """
    Resolves input template into concrete parameters by substituting upstream outputs,
    and validates the final resolved input against the capability's declared Pydantic schema.
    """
    resolved = resolve_template_value(input_template, completed_outputs)
    if not isinstance(resolved, dict):
        raise ValidationError("Resolved step input must be a dictionary.")

    try:
        capability.input_model.model_validate(resolved)
    except Exception as exc:
        raise ValidationError(
            f"Resolved input for capability '{capability.metadata.capability_id}' failed schema validation: {str(exc)}"
        )

    return resolved
