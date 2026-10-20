import pytest
from sqlalchemy.orm import Session

from pico.core.errors import ValidationError
from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.models.project import Project
from pico.models.task import Task
from pico.schemas.context import ContextItem
from pico.services.context_compiler import ContextCompiler


def test_context_compiler_basic_compilation() -> None:
    compiler = ContextCompiler(context_window=4096, completion_reserve=512, safety_headroom=256)
    items = [
        ContextItem(
            id="sys_01",
            item_type="system_prompt",
            role="system",
            content="You are PICO, a personal intelligence coordinator.",
            source_provenance="system:core_instruction",
            data_classification="PUBLIC",
            priority=100,
        ),
        ContextItem(
            id="msg_01",
            item_type="conversation_message",
            role="user",
            content="What tasks are scheduled for today?",
            source_provenance="msg:msg_01",
            data_classification="INTERNAL",
            priority=90,
        ),
    ]

    compiled = compiler.compile(items)

    # Documented conservative budget strategy
    assert compiled.context_window == 4096
    assert compiled.completion_reserve == 512
    assert compiled.safety_headroom == 256
    # Clamped to safe prompt ceiling (4096 - 512 - 256 = 3328)
    assert compiled.token_budget == 3328
    assert compiled.budget_strategy == "conservative-prompt-budget-v1"
    assert compiled.token_counting_method == "conservative-utf8-byte-bound-v1"
    assert compiled.bound_type == "utf8_byte_upper_bound"
    assert compiled.chat_template == "chatml"
    assert compiled.is_exact_count is False

    # Framing tokens accounted for: 2 messages * 5 + 3 generation prompt = 13
    assert compiled.framing_tokens == 13
    assert compiled.content_tokens > 0
    assert compiled.total_tokens == compiled.content_tokens + compiled.framing_tokens

    assert len(compiled.messages) == 2
    assert compiled.messages[0].role == "system"
    assert compiled.messages[1].role == "user"
    assert compiled.included_items_count == 2
    assert compiled.excluded_items_count == 0
    assert compiled.effective_classification == "INTERNAL"
    assert len(compiled.selection_decisions) == 2
    assert all(d.action == "included" for d in compiled.selection_decisions)


def test_context_compiler_token_budget_enforcement_and_pruning() -> None:
    compiler = ContextCompiler(context_window=4096)

    # Candidate items: system prompt (p=100), user current turn (p=90), historical turns (p=50)
    items = [
        ContextItem(
            id="sys_01",
            item_type="system_prompt",
            role="system",
            content="System prompt for PICO instructions.",
            source_provenance="system:core",
            data_classification="PUBLIC",
            priority=100,
        ),
        ContextItem(
            id="msg_hist_01",
            item_type="conversation_message",
            role="user",
            content="A very lengthy historical message about deep architectural designs and complex configurations that takes up considerable tokens.",
            source_provenance="msg:hist_01",
            data_classification="PUBLIC",
            priority=50,
        ),
        ContextItem(
            id="msg_hist_02",
            item_type="conversation_message",
            role="assistant",
            content="Another long historical assistant response detailing system operations and database migrations across multiple paragraphs.",
            source_provenance="msg:hist_02",
            data_classification="PUBLIC",
            priority=50,
        ),
        ContextItem(
            id="msg_current",
            item_type="conversation_message",
            role="user",
            content="Execute next step.",
            source_provenance="msg:current_turn",
            data_classification="PUBLIC",
            priority=90,
        ),
    ]

    compiled = compiler.compile(items, token_budget=100)

    # Critical items (system + current user) must be preserved
    included_ids = [it.id for it in compiled.items]
    assert "sys_01" in included_ids
    assert "msg_current" in included_ids

    # Total tokens (content + framing) must not exceed budget
    assert compiled.total_tokens <= 100
    assert compiled.excluded_items_count > 0

    # Inspect selection decisions
    excluded_decisions = [d for d in compiled.selection_decisions if d.action == "excluded"]
    assert len(excluded_decisions) >= 1
    assert "guarantee prompt + reserve <= 4096 limit" in excluded_decisions[0].reason


def test_context_compiler_fail_closed_classification() -> None:
    compiler = ContextCompiler()

    # Test unknown or missing classification defaults to INTERNAL
    assert compiler.normalize_classification(None) == "INTERNAL"
    assert compiler.normalize_classification("") == "INTERNAL"
    assert compiler.normalize_classification("UNKNOWN_TIER") == "INTERNAL"
    assert compiler.normalize_classification("PUBLIC") == "PUBLIC"
    assert compiler.normalize_classification("SECRET") == "SECRET"

    # Effective sensitivity computation: SECRET dominates everything
    assert compiler.compute_effective_classification(["PUBLIC", "INTERNAL"]) == "INTERNAL"
    assert compiler.compute_effective_classification(["PUBLIC", "INTERNAL", "SENSITIVE"]) == "SENSITIVE"
    assert compiler.compute_effective_classification(["PUBLIC", "SENSITIVE", "SECRET"]) == "SECRET"
    assert compiler.compute_effective_classification(["UNKNOWN_TIER", "PUBLIC"]) == "INTERNAL"
    assert compiler.compute_effective_classification([]) == "INTERNAL"


def test_context_compiler_effective_classification_in_compilation() -> None:
    compiler = ContextCompiler()

    # Compilation with SECRET item pins effective classification to SECRET
    items = [
        ContextItem(
            id="item_public",
            item_type="system_prompt",
            role="system",
            content="General prompt.",
            source_provenance="system:core",
            data_classification="PUBLIC",
            priority=100,
        ),
        ContextItem(
            id="item_secret",
            item_type="conversation_message",
            role="user",
            content="Confidential credential payload.",
            source_provenance="msg:secret_msg",
            data_classification="SECRET",
            priority=90,
        ),
    ]

    compiled = compiler.compile(items)
    assert compiled.effective_classification == "SECRET"

    # Corrupt tier defaults to INTERNAL
    items_corrupt = [
        ContextItem(
            id="item_corrupt",
            item_type="conversation_message",
            role="user",
            content="Message with unrecognized classification tag.",
            source_provenance="msg:corrupt",
            data_classification="UNRECOGNIZED_LEGACY_TAG",
            priority=90,
        )
    ]
    compiled_corrupt = compiler.compile(items_corrupt)
    assert compiled_corrupt.effective_classification == "INTERNAL"


def test_context_compiler_presentation_ordering() -> None:
    compiler = ContextCompiler()
    items = [
        ContextItem(
            id="msg_old",
            item_type="conversation_message",
            role="user",
            content="First message in thread.",
            source_provenance="msg:01",
            data_classification="PUBLIC",
            priority=50,
        ),
        ContextItem(
            id="sys_prompt",
            item_type="system_prompt",
            role="system",
            content="System directive.",
            source_provenance="system:core",
            data_classification="PUBLIC",
            priority=100,
        ),
        ContextItem(
            id="ctx_proj",
            item_type="project_context",
            role="system",
            content="Project: PICO Core",
            source_provenance="proj:01",
            data_classification="PUBLIC",
            priority=80,
        ),
        ContextItem(
            id="msg_current",
            item_type="conversation_message",
            role="user",
            content="Latest user turn.",
            source_provenance="msg:02",
            data_classification="PUBLIC",
            priority=90,
        ),
    ]

    compiled = compiler.compile(items)
    roles = [m.role for m in compiled.messages]
    contents = [m.content for m in compiled.messages]

    # Expected presentation order: System prompt first, Project context, then Messages
    assert roles[0] == "system"
    assert contents[0] == "System directive."
    assert contents[1] == "Project: PICO Core"
    assert contents[2] == "First message in thread."
    assert contents[3] == "Latest user turn."


def test_context_compiler_invalid_budget() -> None:
    compiler = ContextCompiler()
    with pytest.raises(ValidationError, match="Token budget must be greater than zero"):
        compiler.compile([], token_budget=0)

    with pytest.raises(ValidationError, match="Token budget must be greater than zero"):
        compiler.compile([], token_budget=-50)


def test_context_compiler_compile_conversation_context(db_session: Session) -> None:
    # 1. Create a Project
    proj = Project(
        id="proj_compiler_test",
        name="Telemetry & Context",
        description="Verify context compilation across relational entities.",
        status="active",
    )
    db_session.add(proj)

    # 2. Create a Conversation linked to Project via metadata_json
    conv = Conversation(
        id="conv_compiler_test",
        title="Context Compilation Thread",
        metadata_json={"project_id": proj.id},
    )
    db_session.add(conv)

    # 3. Create a Task
    task = Task(
        id="task_compiler_test",
        project_id=proj.id,
        conversation_id=conv.id,
        title="Audit Context Assembly",
        description="Verify that task context is incorporated into compiled prompt.",
        status="in_progress",
        priority="high",
    )
    db_session.add(task)

    # 4. Create Historical Messages
    msg1 = Message(
        id="msg_comp_01",
        conversation_id=conv.id,
        role="user",
        content="Prior turn message in discussion.",
        data_classification="PUBLIC",
    )
    msg2 = Message(
        id="msg_comp_02",
        conversation_id=conv.id,
        role="assistant",
        content="Prior assistant response.",
        data_classification="PUBLIC",
    )
    db_session.add_all([msg1, msg2])
    db_session.commit()

    # 5. Compile context with current user turn
    compiler = ContextCompiler()
    compiled = compiler.compile_conversation_context(
        db=db_session,
        conversation_id=conv.id,
        user_message_id="msg_comp_03",
        user_content="Current active turn inquiring about the task.",
        user_classification="INTERNAL",
        system_prompt="PICO Assistant Instructions.",
        task_id=task.id,
    )

    # Check that all elements are assembled and tagged with provenance
    provenances = [it.source_provenance for it in compiled.items]
    assert "system:core_instruction" in provenances
    assert f"proj:{proj.id}" in provenances
    assert f"task:{task.id}" in provenances
    assert f"msg:{msg1.id}" in provenances
    assert f"msg:{msg2.id}" in provenances
    assert "msg:msg_comp_03" in provenances

    # Effective classification must be INTERNAL (due to msg_comp_03 user_classification)
    assert compiled.effective_classification == "INTERNAL"

    # Check messages array and framing tokens
    assert len(compiled.messages) >= 5
    assert compiled.total_tokens > 0
    assert compiled.framing_tokens > 0
    # Clamped to safe prompt ceiling
    assert compiled.token_budget == compiler.max_prompt_budget


# =============================================================================
# Boundary Tests: Conservative Budget Strategy, Unicode, Mixed Content, Truncation
# =============================================================================


def test_context_compiler_conservative_budget_clamping_and_reserve() -> None:
    """Verify that ContextCompiler clamps prompt budget so prompt + completion + headroom <= 4096."""
    compiler = ContextCompiler(context_window=4096, completion_reserve=512, safety_headroom=256)
    assert compiler.max_prompt_budget == 3328

    items = [
        ContextItem(
            id="item_1",
            item_type="conversation_message",
            role="user",
            content="Hello world",
            source_provenance="msg:1",
            data_classification="PUBLIC",
        )
    ]

    # Explicit request for full 4096 must be clamped to safe ceiling 3328
    compiled = compiler.compile(items, token_budget=4096)
    assert compiled.token_budget == 3328
    assert compiled.context_window == 4096
    assert compiled.completion_reserve == 512
    assert compiled.safety_headroom == 256
    # Invariant: prompt budget + completion reserve + safety headroom == 4096
    assert compiled.token_budget + compiled.completion_reserve + compiled.safety_headroom == 4096


def test_context_compiler_unicode_boundary_cjk_indic_emojis() -> None:
    """Verify that conservative estimation safely upper-bounds CJK, Indic, Cyrillic, and Emojis."""
    compiler = ContextCompiler()

    # CJK text: 8 characters. Naive word count would be 1 word.
    # Conservative estimator must assign at least 2 tokens per CJK character (>= 16 tokens).
    cjk_text = "你好世界，人工智能。"
    cjk_tokens = compiler.count_tokens(cjk_text)
    assert cjk_tokens >= 16

    # Emojis: 4 emojis. Conservative estimator assigns 3 tokens per emoji (>= 12 tokens).
    emoji_text = "🚀🔥🤖💡"
    emoji_tokens = compiler.count_tokens(emoji_text)
    assert emoji_tokens >= 12

    # Indic (Devanagari): non-ASCII upper-bounded
    indic_text = "नमस्ते दुनिया"
    indic_tokens = compiler.count_tokens(indic_text)
    assert indic_tokens >= len(indic_text.replace(" ", ""))

    # Cyrillic
    cyrillic_text = "Привет мир"
    cyrillic_tokens = compiler.count_tokens(cyrillic_text)
    assert cyrillic_tokens >= len("Приветмир")

    # Mixed Unicode payload
    mixed_unicode = "PICO v2.0: 深度学习 🚀 नमस्ते (Cyrillic: Тест)"
    mixed_tokens = compiler.count_tokens(mixed_unicode)
    assert mixed_tokens > len(mixed_unicode.split())


def test_context_compiler_mixed_content_dense_code_and_json() -> None:
    """Verify that dense code syntax, JSON symbols, brackets, and operators are properly upper-bounded."""
    compiler = ContextCompiler()

    code_snippet = """
    async def process_event(event_id: str, data: dict[str, Any]) -> tuple[bool, int]:
        if not data or data.get("status") == "ABORT":
            return (False, -1)
        values = [x * 2 for x in data.get("items", []) if x > 0]
        return (True, len(values))
    """
    code_tokens = compiler.count_tokens(code_snippet)
    # Dense code contains many punctuation tokens: (, ), [, ], :, ->, ==, etc.
    # Tokens should exceed standard word count significantly
    word_count = len(code_snippet.split())
    assert code_tokens > word_count


def test_context_compiler_long_text_truncation_boundary() -> None:
    """Verify that long text exceeding budget is safely truncated without exceeding token ceiling."""
    compiler = ContextCompiler(context_window=4096)

    # Generate 15,000 characters of text (well over 3,000 tokens)
    long_text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 300
    assert len(long_text) > 15000

    items = [
        ContextItem(
            id="sys_instruction",
            item_type="system_prompt",
            role="system",
            content="You are a helpful assistant.",
            source_provenance="system:core",
            data_classification="PUBLIC",
            priority=100,
        ),
        ContextItem(
            id="massive_user_message",
            item_type="conversation_message",
            role="user",
            content=long_text,
            source_provenance="msg:massive_01",
            data_classification="PUBLIC",
            priority=90,
        ),
    ]

    # Test with a constrained budget of 200 tokens
    compiled = compiler.compile(items, token_budget=200)

    # Verification: Total tokens must stay within budget
    assert compiled.total_tokens <= 200
    assert len(compiled.messages) == 2

    user_msg = compiled.messages[1]
    assert "... [truncated to fit 4096 context budget]" in user_msg.content

    # Inspect decision log
    trunc_decisions = [d for d in compiled.selection_decisions if d.action == "truncated"]
    assert len(trunc_decisions) == 1
    assert trunc_decisions[0].item_id == "massive_user_message"
    assert "truncated from" in trunc_decisions[0].reason


def test_context_compiler_framing_overhead_accounting() -> None:
    """Verify that ChatML message framing overhead is calculated and enforced against the budget."""
    compiler = ContextCompiler(context_window=100)

    # 10 short messages, each 2 tokens of content.
    # Content tokens = 20.
    # Framing tokens = 10 * 5 + 3 = 53 tokens.
    # Total required tokens = 73 tokens.
    items = [
        ContextItem(
            id=f"msg_{i}",
            item_type="conversation_message",
            role="user" if i % 2 == 0 else "assistant",
            content="Hi",
            source_provenance=f"msg:{i}",
            data_classification="PUBLIC",
            priority=50,
        )
        for i in range(10)
    ]

    # If budget is only 40, not all 10 messages can fit because framing alone would take 53 tokens!
    compiled = compiler.compile(items, token_budget=40)

    assert compiled.total_tokens <= 40
    assert compiled.framing_tokens > 0
    assert compiled.content_tokens > 0
    assert compiled.total_tokens == compiled.content_tokens + compiled.framing_tokens
    # Some messages were excluded due to framing + content budget exhaustion
    assert compiled.excluded_items_count > 0


def test_context_compiler_model_aware_tokenizer_resolution() -> None:
    """Verify tokenizer selection and exactness flag are strictly model-aware."""
    # 1. Ollama Qwen model: exactness MUST be False even if tiktoken is available
    compiler_qwen = ContextCompiler(model_name="qwen3.5:2b", provider="ollama")
    assert compiler_qwen.chat_template == "chatml"
    assert compiler_qwen.per_message_framing == 5
    assert compiler_qwen.generation_prompt_framing == 3
    assert compiler_qwen.is_exact_count is False
    assert compiler_qwen.bound_type == "utf8_byte_upper_bound"
    assert compiler_qwen.token_counting_method == "conservative-utf8-byte-bound-v1"

    # 2. OpenAI GPT-4 model:
    # Text content uses exact cl100k BPE tokenizer when tiktoken is installed,
    # but complete compiled requests have is_exact_count=False because message formatting overhead varies.
    compiler_gpt4 = ContextCompiler(model_name="gpt-4", provider="openai")
    assert compiler_gpt4.chat_template == "openai_chat"
    assert compiler_gpt4.per_message_framing == 3
    assert compiler_gpt4.generation_prompt_framing == 3
    # Invariant: is_exact_count is False for all compiled chat requests due to estimated envelope overhead
    assert compiler_gpt4.is_exact_count is False
    try:
        import tiktoken
        assert compiler_gpt4.is_exact_content_tokenizer is True
        assert compiler_gpt4.bound_type == "exact_content_estimated_framing"
        assert compiler_gpt4.token_counting_method == "tiktoken-cl100k"
        assert compiler_gpt4.count_tokens("hello world") == 2

        # Verify full compiled context request also has is_exact_count=False
        compiled_gpt4 = compiler_gpt4.compile([
            ContextItem(
                id="msg_gpt_test",
                item_type="conversation_message",
                role="user",
                content="hello world",
                source_provenance="msg:1",
                data_classification="PUBLIC",
                priority=90,
            )
        ])
        assert compiled_gpt4.is_exact_count is False
        assert compiled_gpt4.bound_type == "exact_content_estimated_framing"
    except ImportError:
        assert compiler_gpt4.is_exact_content_tokenizer is False
        assert compiler_gpt4.bound_type == "utf8_byte_upper_bound"

    # 3. Custom counter injection
    compiler_custom = ContextCompiler(
        model_name="custom-model",
        custom_counter=lambda s: len(s) * 2,
    )
    assert compiler_custom.is_exact_count is False
    assert compiler_custom.count_tokens("test") == 8

    # 4. Calibrated estimate mode
    compiler_est = ContextCompiler(
        model_name="qwen3.5:2b",
        counting_method="calibrated-bpe-estimate-v1",
    )
    assert compiler_est.is_exact_count is False
    assert compiler_est.bound_type == "calibrated_estimate"
    assert compiler_est.token_counting_method == "calibrated-bpe-estimate-v1"


def test_qwen_chat_template_overhead_validation() -> None:
    """Validate Qwen ChatML template framing overhead against active model configuration."""
    compiler = ContextCompiler(model_name="qwen3.5:2b", provider="ollama")
    validation = compiler.validate_chat_template()

    assert validation["valid"] is True
    assert validation["chat_template"] == "chatml"
    assert validation["per_message_framing"] == 5
    assert validation["generation_prompt_framing"] == 3

    # Check exact token decomposition
    per_msg = validation["breakdown"]["per_message"]
    assert per_msg["<|im_start|>"] == 1
    assert per_msg["role"] == 1
    assert per_msg["header_newline"] == 1
    assert per_msg["<|im_end|>"] == 1
    assert per_msg["envelope_newline"] == 1
    assert per_msg["total"] == 5

    gen_prompt = validation["breakdown"]["generation_prompt"]
    assert gen_prompt["<|im_start|>"] == 1
    assert gen_prompt["assistant"] == 1
    assert gen_prompt["generation_newline"] == 1
    assert gen_prompt["total"] == 3

    # Check active config reflection
    assert validation["active_config"]["matches_active_qwen"] is True
    assert validation["safeguards"]["explicit_num_ctx"] == 4096
    assert validation["safeguards"]["explicit_num_predict_bound"] == 512
    assert validation["safeguards"]["framing_overhead_accounted"] is True


def test_context_compiler_mandatory_rules_protected_over_history() -> None:
    """Mandatory instructions and safety rules must survive when optional history is pruned."""
    compiler = ContextCompiler(context_window=4096)

    mandatory_system = ContextItem(
        id="sys_core",
        item_type="system_prompt",
        role="system",
        content="PICO core system directives and safety invariants.",
        source_provenance="system:invariants",
        data_classification="PUBLIC",
        priority=100,
        is_mandatory=True,
    )
    privacy_rule = ContextItem(
        id="privacy_rule_01",
        item_type="privacy_rule",
        role="system",
        content="Never transmit SECRET or SENSITIVE classified data to cloud endpoints.",
        source_provenance="policy:privacy_guardrail",
        data_classification="PUBLIC",
        priority=100,
        is_mandatory=True,
    )
    long_history_turn = ContextItem(
        id="hist_user_01",
        item_type="conversation_message",
        role="user",
        content="Could you explain the full architectural decomposition of the PICO storage and execution engine across all subsystems?" * 5,
        source_provenance="msg:old_turn",
        data_classification="PUBLIC",
        priority=50,
        is_mandatory=False,
    )
    current_user_turn = ContextItem(
        id="current_turn",
        item_type="conversation_message",
        role="user",
        content="Continue task.",
        source_provenance="msg:current_turn",
        data_classification="PUBLIC",
        priority=90,
        is_mandatory=False,
    )

    # Compile with a tight budget (180 tokens): fits mandatory rules + current turn, but NOT the long history
    compiled = compiler.compile(
        [mandatory_system, privacy_rule, long_history_turn, current_user_turn],
        token_budget=180,
    )

    included_ids = [it.id for it in compiled.items]
    assert "sys_core" in included_ids
    assert "privacy_rule_01" in included_ids
    assert "current_turn" in included_ids
    assert "hist_user_01" not in included_ids
    assert compiled.excluded_items_count == 1
    assert compiled.total_tokens <= 180


def test_context_compiler_mandatory_rules_exceeding_budget_fails_closed() -> None:
    """If mandatory instructions alone exceed the available budget, fail closed with ValidationError."""
    compiler = ContextCompiler(context_window=4096)

    huge_mandatory_rule = ContextItem(
        id="huge_rule",
        item_type="system_prompt",
        role="system",
        content="A" * 500,  # 500 bytes + 5 framing = 505 tokens
        source_provenance="system:huge",
        data_classification="PUBLIC",
        priority=100,
        is_mandatory=True,
    )

    # Budget of only 200 tokens cannot safely accommodate the mandatory rule
    with pytest.raises(ValidationError, match="Mandatory system and safety instructions .* exceed the available prompt budget"):
        compiler.compile([huge_mandatory_rule], token_budget=200)


def test_context_compiler_utf8_byte_bound_adversarial_and_multilingual() -> None:
    """Verify that UTF-8 byte length strictly upper-bounds BPE tokens for byte-fallback tokenizers.

    In byte-level/byte-fallback BPE tokenizers (like Qwen, GPT-4, LLaMA), every vocabulary token
    corresponds to at least 1 UTF-8 byte (merges of 1 or more bytes).
    Therefore: content_tokens <= len(text.encode('utf-8')) is an exact invariant.
    """
    compiler = ContextCompiler(model_name="qwen3.5:2b", provider="ollama")

    test_cases = [
        # 1. Plain ASCII
        "The quick brown fox jumps over the lazy dog.",
        # 2. Dense code & JSON
        '{"status": 200, "data": [{"id": "uuid-1234", "active": true, "score": 98.7}]}',
        """def compute_hash(payload: bytes) -> str:\n    return hashlib.sha256(payload).hexdigest()\n""",
        # 3. Multilingual Unicode: CJK, Indic, Arabic, Cyrillic
        "人工智能系统架构与本地推理引擎",
        "नमस्ते दुनिया, यह पिको व्यक्तिगत बुद्धिमत्ता प्रणाली है।",
        "مرحبا بالعالم، هذا نظام الذكاء الشخصي",
        "Привет, мир! Архитектура персонального интеллекта.",
        # 4. Emojis and complex ZWJ combining sequences
        "🚀🔥🤖💡✨",
        "👨‍👩‍👧‍👦",  # Family: Man + ZWJ + Woman + ZWJ + Girl + ZWJ + Boy (25 bytes)
        "👍🏽",       # Thumbs up with medium skin tone modifier
        "🏳️‍🌈",     # Rainbow flag sequence
        # 5. Adversarial strings: punctuation runs, whitespace, control chars
        " !@#$%^&*()_+=-`~[]\\{}|;':\",./<>?",
        "                    ",
        "\t\n\r\n\t",
        "a" * 1000,
    ]

    try:
        import tiktoken
        tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    except ImportError:
        tiktoken_enc = None

    for text in test_cases:
        utf8_bound = compiler.count_tokens(text)
        assert utf8_bound == max(1, len(text.encode("utf-8")))

        if tiktoken_enc is not None:
            actual_bpe_tokens = len(tiktoken_enc.encode(text, disallowed_special=()))
            # Strict invariant: UTF-8 byte bound must be >= actual BPE token count!
            assert utf8_bound >= actual_bpe_tokens, (
                f"Invariant violation: utf8_bound ({utf8_bound}) < actual_bpe ({actual_bpe_tokens}) for text: {text[:30]}"
            )


def test_context_compiler_missing_classification_fails_closed_in_compilation() -> None:
    """Missing or corrupted data classification in any item must fail closed to INTERNAL."""
    compiler = ContextCompiler()

    items = [
        ContextItem(
            id="item_none",
            item_type="conversation_message",
            role="user",
            content="Message without classification.",
            source_provenance="msg:01",
            data_classification=None,  # type: ignore[arg-type]
        ),
        ContextItem(
            id="item_public",
            item_type="conversation_message",
            role="assistant",
            content="Public message.",
            source_provenance="msg:02",
            data_classification="PUBLIC",
        ),
    ]

    compiled = compiler.compile(items)
    # Effective classification must fail closed to INTERNAL
    assert compiled.effective_classification == "INTERNAL"
    # The item with None classification was normalized to INTERNAL
    normalized_classes = [it.data_classification for it in compiled.items]
    assert "INTERNAL" in normalized_classes
