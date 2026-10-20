import pytest
from pydantic import BaseModel

from pico.capabilities.base import BaseCapability, CapabilityMetadata, ExecutionContext, ExecutionResult, RiskTier
from pico.capabilities.registry import CapabilityRegistry, registry
from pico.core.errors import EntityNotFoundError, ValidationError


def test_capability_registry_registration_and_lookup() -> None:
    reg = CapabilityRegistry()

    class DummyInput(BaseModel):
        msg: str

    class DummyOutput(BaseModel):
        echo: str

    class DummyCap(BaseCapability):
        metadata = CapabilityMetadata(
            capability_id="test.dummy",
            version="1.0.0",
            name="Dummy Capability",
            description="Testing capability",
            risk_tier=RiskTier.LOW,
            required_permission="test:dummy",
        )
        input_model = DummyInput
        output_model = DummyOutput

        async def execute(self, input_data: dict, context: ExecutionContext) -> ExecutionResult:
            return ExecutionResult(success=True, output_data={"echo": input_data["msg"]})

    cap = DummyCap()
    reg.register(cap)

    # 1. Lookup by ID
    assert reg.has("test.dummy")
    fetched = reg.get("test.dummy")
    assert fetched.metadata.capability_id == "test.dummy"
    assert fetched.metadata.version == "1.0.0"

    # 2. Version mismatch
    with pytest.raises(ValidationError):
        reg.get("test.dummy", version="2.0.0")

    # 3. Unknown capability lookup
    assert not reg.has("test.nonexistent")
    with pytest.raises(EntityNotFoundError):
        reg.get("test.nonexistent")


def test_default_registry_contains_initial_capabilities() -> None:
    # Verify the required standard initial safe capabilities are registered
    expected_ids = {
        "core.calculator",
        "core.validate_json",
        "db.read_project",
        "db.read_task",
        "system.diagnostics",
        "test.idempotent_counter",
        "test.controlled_failure",
        "test.slow_timeout",
        "test.verifier_failure",
        "test.consequential_action",
    }
    registered = {c.metadata.capability_id for c in registry.list()}
    assert expected_ids.issubset(registered)


def test_capability_description_schema_generation() -> None:
    desc = registry.describe("core.calculator")
    assert desc["capability_id"] == "core.calculator"
    assert desc["risk_tier"] == "READ_ONLY"
    assert "expression" in desc["input_schema"]["properties"]
    assert "result" in desc["output_schema"]["properties"]
