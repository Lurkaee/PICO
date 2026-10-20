from sqlalchemy.orm import Session

from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.schemas.context import ContextItem
from pico.services.context_compiler import ContextCompiler
from pico.services.memory_service import MemoryService


def test_compiler_includes_memory_with_provenance_and_classification(db_session: Session) -> None:
    compiler = ContextCompiler(context_window=4096)
    mem_service = MemoryService(db_session)

    # Setup conversation
    conv = Conversation(id="conv_mem_01", title="Memory Compiler Test")
    db_session.add(conv)
    db_session.commit()

    # Add active working memory
    mem_service.put_working_state(
        session_id="conv_mem_01",
        key="active_subtask",
        content="Testing memory integration with Context Compiler",
        data_classification="INTERNAL",
        source_provenance="session:conv_mem_01",
    )

    # Add semantic memory matching query
    fact, _ = mem_service.save_fact(
        subject="User",
        predicate="preferred_format",
        content="User requires JSON output with ISO8601 timestamps.",
        is_verified=True,
        data_classification="PUBLIC",
        source_provenance="user:profile",
    )

    # Triggering turn mentioning JSON format
    user_msg_id = "msg_user_trigger_01"
    user_msg = Message(
        id=user_msg_id,
        conversation_id="conv_mem_01",
        role="user",
        content="Please provide the response in my preferred format.",
        data_classification="PUBLIC",
    )
    db_session.add(user_msg)
    db_session.commit()

    compiled = compiler.compile_conversation_context(
        db=db_session,
        conversation_id="conv_mem_01",
        user_message_id=user_msg_id,
        user_content=user_msg.content,
        user_classification="PUBLIC",
        system_prompt="You are PICO.",
        include_memory=True,
    )

    # Verify memory items entered compiled context
    item_types = [it.item_type for it in compiled.items]
    assert "working_memory" in item_types
    assert "memory_fact" in item_types

    # Provenance preserved
    provenance_list = [it.source_provenance for it in compiled.items]
    assert any("wmem:" in p or "session:" in p for p in provenance_list)
    assert any(f"mem:{fact.id}" in p for p in provenance_list)

    # Since working memory was INTERNAL, effective classification MUST be INTERNAL
    assert compiled.effective_classification == "INTERNAL"

    # Decisions log contains memory items
    mem_decisions = [d for d in compiled.selection_decisions if d.item_type in ("working_memory", "memory_fact")]
    assert len(mem_decisions) >= 2
    assert all(d.action == "included" for d in mem_decisions)


def test_compiler_prunes_memory_before_mandatory_rules(db_session: Session) -> None:
    compiler = ContextCompiler(context_window=4096)

    mandatory_rule = ContextItem(
        id="sys_mandatory",
        item_type="system_prompt",
        role="system",
        content="Core privacy policy: All data is confidential.",
        source_provenance="system:core",
        data_classification="PUBLIC",
        priority=100,
        is_mandatory=True,
    )
    user_turn = ContextItem(
        id="user_turn_01",
        item_type="conversation_message",
        role="user",
        content="Summarize recent updates.",
        source_provenance="msg:1",
        data_classification="PUBLIC",
        priority=90,
    )
    long_memory_item = ContextItem(
        id="mem_fact_long",
        item_type="memory_fact",
        role="system",
        content="Extensive historical fact description. " * 30,  # ~1200 tokens
        source_provenance="mem:sem_123",
        data_classification="INTERNAL",
        priority=40,
    )

    # Budget of 150 tokens: fits mandatory rule + user turn, but NOT the long memory item
    compiled = compiler.compile(
        [mandatory_rule, user_turn, long_memory_item],
        token_budget=150,
    )

    included_ids = [it.id for it in compiled.items]
    assert "sys_mandatory" in included_ids
    assert "user_turn_01" in included_ids
    assert "mem_fact_long" not in included_ids

    # Memory item was excluded to protect prompt budget
    mem_decision = next(d for d in compiled.selection_decisions if d.item_id == "mem_fact_long")
    assert mem_decision.action == "excluded"
    assert "excluded to protect budget" in mem_decision.reason.lower()


def test_compiler_retrieved_private_memory_blocks_cloud_eligibility(db_session: Session) -> None:
    compiler = ContextCompiler(context_window=4096)
    mem_service = MemoryService(db_session)

    conv = Conversation(id="conv_privacy_test", title="Privacy Gate Test")
    db_session.add(conv)
    db_session.commit()

    # User content is PUBLIC
    user_msg_id = "msg_public_prompt"
    user_msg = Message(
        id=user_msg_id,
        conversation_id="conv_privacy_test",
        role="user",
        content="Tell me about project configuration.",
        data_classification="PUBLIC",
    )
    db_session.add(user_msg)

    # But retrieved factual memory contains SENSITIVE client key
    mem_service.save_fact(
        subject="Project",
        predicate="configuration",
        content="Configuration includes sensitive internal server endpoints.",
        data_classification="SENSITIVE",
        source_provenance="doc:internal_config",
        is_verified=True,
    )

    compiled = compiler.compile_conversation_context(
        db=db_session,
        conversation_id="conv_privacy_test",
        user_message_id=user_msg_id,
        user_content=user_msg.content,
        user_classification="PUBLIC",
        system_prompt="PICO assistant.",
        include_memory=True,
    )

    # Effective classification must reflect the most restrictive memory item (SENSITIVE)
    assert compiled.effective_classification == "SENSITIVE"
    # SENSITIVE cannot be sent to cloud (only PUBLIC is cloud eligible)
    assert compiled.effective_classification != "PUBLIC"
