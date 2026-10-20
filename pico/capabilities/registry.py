from typing import Any
from pico.capabilities.base import BaseCapability
from pico.core.errors import EntityNotFoundError, ValidationError


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, BaseCapability] = {}

    def register(self, capability: BaseCapability) -> None:
        cap_id = capability.metadata.capability_id
        if not cap_id or not cap_id.strip():
            raise ValidationError("Capability ID cannot be empty.")
        self._capabilities[cap_id] = capability

    def get(self, capability_id: str, version: str | None = None) -> BaseCapability:
        capability = self._capabilities.get(capability_id)
        if not capability:
            raise EntityNotFoundError("Capability", capability_id)
        if version and capability.metadata.version != version:
            raise ValidationError(
                f"Capability '{capability_id}' version mismatch: requested '{version}', available '{capability.metadata.version}'."
            )
        return capability

    def has(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    def list(self) -> list[BaseCapability]:
        return sorted(self._capabilities.values(), key=lambda c: c.metadata.capability_id)

    def describe(self, capability_id: str) -> dict[str, Any]:
        cap = self.get(capability_id)
        return {
            "capability_id": cap.metadata.capability_id,
            "version": cap.metadata.version,
            "name": cap.metadata.name,
            "description": cap.metadata.description,
            "risk_tier": cap.metadata.risk_tier.value,
            "required_permission": cap.metadata.required_permission,
            "required_classification": cap.metadata.required_classification,
            "timeout_seconds": cap.metadata.timeout_seconds,
            "max_output_bytes": cap.metadata.max_output_bytes,
            "idempotent": cap.metadata.idempotent,
            "max_retries": cap.metadata.max_retries,
            "consequential": cap.metadata.consequential,
            "preconditions": cap.metadata.preconditions,
            "expected_effects": cap.metadata.expected_effects,
            "input_schema": cap.input_model.model_json_schema(),
            "output_schema": cap.output_model.model_json_schema(),
        }


# Global registry singleton for PICO
registry = CapabilityRegistry()
