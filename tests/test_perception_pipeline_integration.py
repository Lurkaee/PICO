import pytest
from sqlalchemy.orm import Session

from pico.capabilities.registry import registry
from pico.perception.context import format_perception_context_items
from pico.perception.contracts import (
    EnvironmentalIngestRequest,
    Modality,
    MotionIngestRequest,
    NormalizedPerceptionEvent,
)
from pico.perception.pipeline import PerceptionPipeline
from pico.repositories.perception_event_repo import PerceptionEventRepository
from pico.schemas.context import ContextItem
from pico.services.context_compiler import ContextCompiler
from pico.services.execution_engine import ToolExecutionEngine


def test_perception_event_persistence_and_query(db_session: Session) -> None:
    pipeline = PerceptionPipeline(db=db_session)

    # 1. Ingest PIR motion event
    motion_req = MotionIngestRequest(
        source_device="pir:garden_gate",
        motion_detected=True,
    )
    m_ev = pipeline.ingest_pir_motion(motion_req)

    # 2. Ingest environmental event
    env_req = EnvironmentalIngestRequest(
        source_device="sensor:garden_temp",
        temperature_c=17.2,
    )
    e_ev, _ = pipeline.ingest_environmental(env_req)

    # Query via repository
    repo = PerceptionEventRepository(db_session)
    stored_m = repo.get_by_id(m_ev.event_id)
    assert stored_m is not None
    assert stored_m.modality == "motion"
    assert stored_m.source_device_id == "pir:garden_gate"
    assert stored_m.data_classification == "INTERNAL"

    stored_e = repo.get_by_id(e_ev.event_id)
    assert stored_e is not None
    assert stored_e.modality == "environmental"

    # List with modality filter
    motion_events = repo.list_events(modality="motion")
    assert any(ev.id == m_ev.event_id for ev in motion_events)
    assert not any(ev.id == e_ev.event_id for ev in motion_events)


def test_context_compiler_perception_pruning_precedence() -> None:
    # Build perception events
    ev1 = NormalizedPerceptionEvent(
        event_id="pevt_1",
        modality=Modality.MOTION,
        event_type="movement_interpreted",
        source_device_id="cam:hall",
        structured_data={"direction": "right", "magnitude": "moderate"},
        provenance="motion:cam:hall",
        data_classification="INTERNAL",
    )
    ev2 = NormalizedPerceptionEvent(
        event_id="pevt_2",
        modality=Modality.ENVIRONMENTAL,
        event_type="environmental_reading",
        source_device_id="sensor:temp1",
        structured_data={"readings": {"temperature_c": 21.5}},
        provenance="sensor:temp1",
        data_classification="INTERNAL",
    )

    sensor_items = format_perception_context_items([ev1, ev2])
    assert len(sensor_items) == 2
    assert sensor_items[0].priority == 45
    assert sensor_items[0].is_mandatory is False
    assert sensor_items[0].data_classification == "INTERNAL"

    # Assemble context with mandatory instructions, user turn, and sensor events
    mandatory_rule = ContextItem(
        id="sys_rule_mandatory",
        item_type="privacy_rule",
        role="system",
        content="MANDATORY RULE: Never leak secrets.",
        source_provenance="system:security",
        data_classification="INTERNAL",
        priority=100,
        is_mandatory=True,
    )
    user_turn = ContextItem(
        id="user_msg_current",
        item_type="conversation_message",
        role="user",
        content="What is happening in the house right now?",
        source_provenance="msg:1",
        data_classification="INTERNAL",
        priority=90,
    )

    all_items = [mandatory_rule, user_turn] + sensor_items

    # Compile with small budget to force pruning
    compiler = ContextCompiler(context_window=2048, completion_reserve=256, safety_headroom=128)
    compiled = compiler.compile(
        items=all_items,
        token_budget=200,  # Tight budget
    )

    included_ids = [it.id for it in compiled.items]

    # Mandatory rule MUST survive
    assert "sys_rule_mandatory" in included_ids
    # User message MUST survive
    assert "user_msg_current" in included_ids

    # Classification of compiled context reflects highest classification (INTERNAL)
    assert compiled.effective_classification == "INTERNAL"


@pytest.mark.anyio
async def test_perception_capabilities_registered_and_executable(db_session: Session) -> None:
    # Verify capabilities are present in global registry
    assert registry.has("perception.status")
    assert registry.has("perception.get_recent_events")
    assert registry.has("perception.validate_sensor_payload")
    assert registry.has("perception.analyze_motion")

    # Invariant: Perception capabilities are strictly READ_ONLY
    status_cap = registry.get("perception.status")
    assert status_cap.metadata.risk_tier.value == "READ_ONLY"
    assert status_cap.metadata.consequential is False

    # Execute perception.status through ToolExecutionEngine
    engine = ToolExecutionEngine(db_session, registry)
    perms = {"perception:read", "perception:validate", "perception:analyze"}
    execution = engine.submit(
        capability_id="perception.status",
        input_data={"check_adapters": True},
        caller_permissions=perms,
    )
    assert execution.status == "queued"

    exec_res = await engine.execute(execution.id, caller_permissions=perms)
    assert exec_res.status == "succeeded"
    assert exec_res.verification_status == "passed"
    assert exec_res.output_json is not None
    assert exec_res.output_json["overall_ready"] is True
