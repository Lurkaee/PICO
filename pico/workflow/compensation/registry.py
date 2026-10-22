"""Registry for explicit, verified capability compensation definitions (Milestone P9.3)."""

from typing import Any, Callable
from pydantic import BaseModel, ConfigDict


class CompensationDefinition(BaseModel):
    """
    Explicit specification of how to safely compensate a completed capability execution.
    Never synthesizes arbitrary commands from model explanations.
    """
    target_capability_id: str
    target_capability_version: str = "1.0.0"
    is_consequential: bool = True
    input_builder: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _build_filesystem_rollback_input(
    original_input: dict[str, Any], original_output: dict[str, Any]
) -> dict[str, Any]:
    """Constructs validated input for filesystem.rollback_staged_diff."""
    return {
        "proposal_id": original_input.get("proposal_id"),
        "expected_applied_digest": original_output.get("verified_digest"),
        "operation": original_input.get("operation"),
        "target_restore_state": original_input.get("expected_original_state"),
    }


class CompensationRegistry:
    """Registry of known reversible capabilities and their compensation definitions."""

    def __init__(self) -> None:
        self._definitions: dict[str, CompensationDefinition] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        # Register filesystem apply -> rollback compensation
        self.register(
            source_capability_id="filesystem.apply_staged_diff",
            definition=CompensationDefinition(
                target_capability_id="filesystem.rollback_staged_diff",
                target_capability_version="1.0.0",
                is_consequential=True,
                input_builder=_build_filesystem_rollback_input,
            ),
        )

    def register(self, source_capability_id: str, definition: CompensationDefinition) -> None:
        """Register a compensation definition for a capability."""
        self._definitions[source_capability_id] = definition

    def has_compensation(self, source_capability_id: str) -> bool:
        """Check if a capability has a verified compensation definition."""
        return source_capability_id in self._definitions

    def get_compensation(self, source_capability_id: str) -> CompensationDefinition | None:
        """Retrieve compensation definition for a capability."""
        return self._definitions.get(source_capability_id)


# Global registry singleton
default_compensation_registry = CompensationRegistry()


def get_compensation_registry() -> CompensationRegistry:
    """Return the global default compensation registry."""
    return default_compensation_registry
