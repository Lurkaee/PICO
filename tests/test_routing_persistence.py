from sqlalchemy.orm import Session

from pico.core.id_gen import generate_id
from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.models.routing_decision import RoutingDecision
from pico.repositories.routing_decision_repo import RoutingDecisionRepository


def test_routing_decision_persistence(db_session: Session) -> None:
    repo = RoutingDecisionRepository(db_session)

    # Setup parent conversation and message
    conv_id = generate_id("conv")
    conv = Conversation(id=conv_id, title="Audit Conv")
    db_session.add(conv)

    msg_id = generate_id("msg")
    msg = Message(id=msg_id, conversation_id=conv_id, role="user", content="Classify this")
    db_session.add(msg)
    db_session.commit()

    decision_id = generate_id("route")
    decision = repo.create(
        decision_id=decision_id,
        conversation_id=conv_id,
        message_id=msg_id,
        step_index=0,
        task_category="intent_classification",
        data_classification="INTERNAL",
        selected_profile="local-small",
        provider="ollama",
        model_name="qwen3.5:2b",
        decision_rule="RULE_PRIVATE_LOCAL_PIN",
        cloud_approved=False,
        outcome="success",
        prompt_tokens=18,
        completion_tokens=4,
        latency_ms=145.2,
        estimated_cost_cents=0.0,
    )

    assert decision.id == decision_id
    assert decision.conversation_id == conv_id
    assert decision.message_id == msg_id
    assert decision.selected_profile == "local-small"
    assert decision.outcome == "success"

    # Query back
    fetched = repo.get_by_id(decision_id)
    assert fetched is not None
    assert fetched.model_name == "qwen3.5:2b"
    assert fetched.latency_ms == 145.2
    assert fetched.created_at is not None

    # List by conversation
    by_conv = repo.list_by_conversation(conv_id)
    assert len(by_conv) == 1
    assert by_conv[0].id == decision_id
