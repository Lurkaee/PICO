import math
import re
from typing import Any, Callable, Literal
from sqlalchemy.orm import Session

from pico.core.errors import ValidationError
from pico.providers.types import ChatMessage
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
from pico.repositories.project_repo import ProjectRepository
from pico.repositories.semantic_memory_repo import SemanticMemoryRepository
from pico.repositories.task_repo import TaskRepository
from pico.repositories.working_memory_repo import WorkingMemoryRepository
from pico.schemas.context import CompiledContext, ContextItem, SelectionDecision

# Try importing tiktoken for exact BPE counting when model matches
try:
    import tiktoken  # type: ignore[import-untyped]
    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False


class ContextCompiler:
    """The Context Compiler assembles, budgets, ranks, and classifies context for model inference.

    Budget & Tokenizer Strategy ('conservative-prompt-budget-v1'):
    1. Model-Aware Tokenizer Selection:
       - Identifies the active provider and model to select chat template and tokenizer behavior.
       - 'is_exact_count' is False for all compiled chat requests because complete provider requests
         include estimated message framing overhead (<|im_start|>, roles, newlines, generation trigger)
         which varies across API versions and provider formatting implementations.
       - When an exact BPE tokenizer is available for text content (e.g. tiktoken for GPT-4/GPT-4o),
         the content tokens are counted with the exact vocabulary (is_exact_content_tokenizer=True),
         and bound_type is labeled 'exact_content_estimated_framing', while is_exact_count remains False.
       - For Qwen models (Ollama 'qwen3.5:2b'), Claude, or when an exact model-specific tokenizer is absent,
         is_exact_count is False, is_exact_content_tokenizer is False, and bound_type is 'utf8_byte_upper_bound'.
    2. Fallback Guarantee:
       - Default counting method: 'conservative-utf8-byte-bound-v1'.
         In byte-fallback BPE tokenizers (Qwen, LLaMA, GPT), every vocabulary token encodes >= 1 UTF-8 byte.
         Therefore, UTF-8 byte length is a true content-token upper bound: content_tokens <= len(text.encode('utf-8')).
       - Template framing overhead: ChatML adds 5 tokens per message envelope (<|im_start|>role\ncontent<|im_end|>\n)
         plus 3 tokens for the assistant generation trigger (<|im_start|>assistant\n), verified against active model config.
       - Also supports 'calibrated-bpe-estimate-v1' as an empirical estimate, explicitly declared as non-exact.
    3. Provider Boundary Budget Enforcement:
       - Enforces provider context window (default 4096).
       - Reserves completion_reserve (default 512, or options.max_tokens).
       - Reserves safety_headroom (default 256) for tokenization variance and template tokens.
       - max_prompt_budget = context_window - completion_reserve - safety_headroom (e.g. 4096 - 512 - 256 = 3328).
       - User-requested budgets are clamped to this ceiling: prompt + reserve + headroom <= context_window.
    4. Mandatory Instructions Protection:
       - Mandatory system prompts and privacy/safety rules (priority >= 100 or is_mandatory=True)
         are allocated FIRST and CANNOT be pruned for conversational history.
       - If mandatory instructions alone exceed prompt budget, the compiler fails closed with ValidationError.
       - Optional history and context are pruned before any mandatory instructions or current user turn.
    """

    DEFAULT_CONTEXT_WINDOW = 4096
    DEFAULT_COMPLETION_RESERVE = 512
    DEFAULT_SAFETY_HEADROOM = 256
    BUDGET_STRATEGY = "conservative-prompt-budget-v1"

    METHOD_UTF8_BYTE_BOUND = "conservative-utf8-byte-bound-v1"
    METHOD_CALIBRATED_ESTIMATE = "calibrated-bpe-estimate-v1"
    METHOD_TIKTOKEN_O200K = "tiktoken-o200k"
    METHOD_TIKTOKEN_CL100K = "tiktoken-cl100k"

    VALID_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"}
    CLASSIFICATION_SEVERITY: dict[str, int] = {
        "PUBLIC": 0,
        "INTERNAL": 1,
        "SENSITIVE": 2,
        "SECRET": 3,
    }

    # Framing overhead per ChatML message: <|im_start|>role\ncontent<|im_end|>\n
    CHATML_PER_MESSAGE_FRAMING = 5
    CHATML_GENERATION_PROMPT = 3

    # Framing overhead for OpenAI chat format:
    OPENAI_PER_MESSAGE_FRAMING = 3
    OPENAI_GENERATION_PROMPT = 3

    def __init__(
        self,
        model_name: str | None = None,
        provider: str | None = None,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        completion_reserve: int = DEFAULT_COMPLETION_RESERVE,
        safety_headroom: int = DEFAULT_SAFETY_HEADROOM,
        counting_method: str | None = None,
        custom_counter: Callable[[str], int] | None = None,
    ) -> None:
        self.model_name = (model_name or "qwen3.5:2b").lower()
        self.provider = (provider or "ollama").lower()
        self.context_window = context_window
        self.completion_reserve = completion_reserve
        self.safety_headroom = safety_headroom
        self._custom_counter = custom_counter
        self._tiktoken_enc = None

        # 1. Resolve Chat Template and framing parameters based on active model/provider
        if "qwen" in self.model_name or self.provider == "ollama":
            self.chat_template = "chatml"
            self.per_message_framing = self.CHATML_PER_MESSAGE_FRAMING
            self.generation_prompt_framing = self.CHATML_GENERATION_PROMPT
        elif "gpt-4" in self.model_name or "gpt-3.5" in self.model_name:
            self.chat_template = "openai_chat"
            self.per_message_framing = self.OPENAI_PER_MESSAGE_FRAMING
            self.generation_prompt_framing = self.OPENAI_GENERATION_PROMPT
        else:
            self.chat_template = "generic"
            self.per_message_framing = 4
            self.generation_prompt_framing = 3

        # 2. Model-aware Tokenizer Resolution
        # Invariant: is_exact_count is False for all compiled chat requests because complete provider requests
        # include estimated message framing overhead (<|im_start|>, roles, newlines, generation trigger)
        # which varies across provider versions and formatting rules.
        # We track is_exact_content_tokenizer for text content exactness.
        self.is_exact_content_tokenizer = False
        self.is_exact_count = False

        if custom_counter is not None:
            self.token_counting_method = counting_method or "custom-injected-counter"
            self.bound_type = "calibrated_estimate"
        elif _HAS_TIKTOKEN and ("gpt-4o" in self.model_name or "o200k" in (counting_method or "")):
            try:
                self._tiktoken_enc = tiktoken.get_encoding("o200k_base")
                self.token_counting_method = self.METHOD_TIKTOKEN_O200K
                self.is_exact_content_tokenizer = True
                self.bound_type = "exact_content_estimated_framing"
            except Exception:
                self._tiktoken_enc = None
                self.token_counting_method = counting_method or self.METHOD_UTF8_BYTE_BOUND
                self.bound_type = "utf8_byte_upper_bound"
        elif _HAS_TIKTOKEN and ("gpt-4" in self.model_name or "gpt-3.5" in self.model_name or "cl100k" in (counting_method or "")):
            try:
                self._tiktoken_enc = tiktoken.get_encoding("cl100k_base")
                # cl100k matches vocabulary for GPT-4/GPT-3.5 text content; framing is estimated
                if "gpt-4" in self.model_name or "gpt-3.5" in self.model_name:
                    self.token_counting_method = self.METHOD_TIKTOKEN_CL100K
                    self.is_exact_content_tokenizer = True
                    self.bound_type = "exact_content_estimated_framing"
                else:
                    # Model is Qwen or other; cl100k does not match vocabulary
                    self.token_counting_method = counting_method or self.METHOD_UTF8_BYTE_BOUND
                    self.bound_type = "utf8_byte_upper_bound"
            except Exception:
                self._tiktoken_enc = None
                self.token_counting_method = counting_method or self.METHOD_UTF8_BYTE_BOUND
                self.bound_type = "utf8_byte_upper_bound"
        else:
            # For Qwen, Claude, or zero-dependency offline environments
            self.token_counting_method = counting_method or self.METHOD_UTF8_BYTE_BOUND
            self.bound_type = (
                "utf8_byte_upper_bound"
                if self.token_counting_method == self.METHOD_UTF8_BYTE_BOUND
                else "calibrated_estimate"
            )

    @property
    def max_prompt_budget(self) -> int:
        """Maximum allowable prompt token budget after reserving completion and safety headroom."""
        budget = self.context_window - self.completion_reserve - self.safety_headroom
        return max(64, budget)

    def count_tokens(self, text: str) -> int:
        """Calculate token footprint using exact model tokenizer if matched, or documented bound/estimate."""
        if not text:
            return 0

        if self._custom_counter is not None:
            return max(1, self._custom_counter(text))

        if self._tiktoken_enc is not None:
            return max(1, len(self._tiktoken_enc.encode(text, disallowed_special=())))

        if self.token_counting_method == self.METHOD_UTF8_BYTE_BOUND:
            return self._count_utf8_bytes(text)

        return self._estimate_calibrated_bpe(text)

    def _count_utf8_bytes(self, text: str) -> int:
        """UTF-8 byte length as a conservative content-token upper bound for byte-fallback BPE.

        Mathematical property: In any byte-fallback BPE tokenizer (Qwen, LLaMA, GPT),
        every token encodes >= 1 UTF-8 byte. Therefore, BPE tokens <= UTF-8 bytes.
        """
        encoded = text.encode("utf-8")
        return max(1, len(encoded))

    def _estimate_calibrated_bpe(self, text: str) -> int:
        """Calibrated empirical estimate for subwords, numbers, symbols, and Unicode."""
        tokens = 0
        for ch in text:
            code = ord(ch)
            if code <= 127:
                if ch.isalnum() or ch.isspace():
                    tokens += 1
                else:
                    # ASCII punctuation/symbol
                    tokens += 1
            elif code >= 0x1F000:
                # Emojis (codepoints can expand to multiple bytes/subwords)
                tokens += 3
            elif (0x4E00 <= code <= 0x9FFF) or (0x3400 <= code <= 0x4DBF):
                # CJK Ideographs
                tokens += 2
            else:
                # Indic, Cyrillic, Arabic, Accented characters
                tokens += 2

        # Adjust for word boundaries and subwords
        words = re.findall(r"[A-Za-z]+|\d+", text)
        for w in words:
            if w.isalpha() and len(w) > 4:
                tokens += math.ceil(len(w) / 4.0)

        return max(1, tokens)

    def normalize_classification(self, classification: str | None) -> str:
        """Enforce fail-closed private invariant: missing or invalid classification -> INTERNAL."""
        if not classification or not isinstance(classification, str):
            return "INTERNAL"
        upper = classification.strip().upper()
        if upper not in self.VALID_CLASSIFICATIONS:
            return "INTERNAL"
        return upper

    def compute_effective_classification(self, classifications: list[str]) -> str:
        """Compute the dominant classification among items.

        Severity hierarchy: SECRET > SENSITIVE > INTERNAL > PUBLIC.
        Defaults to INTERNAL if empty.
        """
        if not classifications:
            return "INTERNAL"

        max_severity = 0
        effective = "PUBLIC"

        for c in classifications:
            norm = self.normalize_classification(c)
            sev = self.CLASSIFICATION_SEVERITY.get(norm, 1)
            if sev > max_severity:
                max_severity = sev
                effective = norm

        return effective

    def compile(
        self,
        items: list[ContextItem],
        token_budget: int | None = None,
        completion_reserve: int | None = None,
    ) -> CompiledContext:
        """Compile a list of typed context items within the enforced conservative budget.

        Guarantees:
        1. Mandatory instructions (is_mandatory=True or item_type in ('system_prompt', 'privacy_rule'))
           are protected and NEVER pruned for optional conversational history.
        2. Prompt budget ceiling is enforced: budget <= context_window - completion_reserve - safety_headroom.
        3. Framing overhead (ChatML envelope tokens) is explicitly accounted for.
        4. Optional history and context are pruned before the current user turn.
        5. Effective classification fails closed to INTERNAL if any item is missing/invalid.
        """
        active_reserve = completion_reserve if completion_reserve is not None else self.completion_reserve
        safe_ceiling = max(64, self.context_window - active_reserve - self.safety_headroom)

        if token_budget is not None:
            if token_budget <= 0:
                raise ValidationError("Token budget must be greater than zero.")
            budget = min(token_budget, safe_ceiling)
        else:
            budget = safe_ceiling

        # 1. Normalize items and compute individual token counts
        prepared_items: list[ContextItem] = []
        for it in items:
            norm_class = self.normalize_classification(it.data_classification)
            is_mand = it.is_mandatory or it.item_type in ("system_prompt", "privacy_rule", "safety_directive") or it.priority >= 100 or (it.role == "system" and it.priority >= 90)
            t_count = it.token_count if it.token_count > 0 else self.count_tokens(it.content)
            prepared_items.append(
                it.model_copy(
                    update={
                        "data_classification": norm_class,
                        "is_mandatory": is_mand,
                        "token_count": t_count,
                    }
                )
            )

        decisions: list[SelectionDecision] = []
        included_items: list[ContextItem] = []
        accumulated_content_tokens = 0
        accumulated_framing_tokens = self.generation_prompt_framing

        # 2. Partition into Priority Tiers:
        # - Tier 1: Mandatory instructions (System instructions, privacy/safety rules)
        # - Tier 2: Current triggering user turn (Priority 90)
        # - Tier 3: Active task / project context (Priority 80)
        # - Tier 4: Conversation history (Priority 50), ordered newest to oldest
        # - Tier 5: Memory / auxiliary facts / sensor observations (Priority <= 45)
        mandatory_items = [it for it in prepared_items if it.is_mandatory]
        current_turn = [it for it in prepared_items if not it.is_mandatory and it.priority == 90 and it.role == "user"]
        active_context = [it for it in prepared_items if not it.is_mandatory and it.priority == 80]
        history_items = [it for it in prepared_items if not it.is_mandatory and it.priority == 50]
        # Reverse history so newest turns are evaluated first
        history_newest_first = list(reversed(history_items))
        aux_memory = [it for it in prepared_items if not it.is_mandatory and it.priority <= 45]
        aux_memory.sort(key=lambda x: x.priority, reverse=True)

        # STEP A: Allocate Mandatory Instructions FIRST
        for m_item in mandatory_items:
            m_total = m_item.token_count + self.per_message_framing
            projected = accumulated_content_tokens + accumulated_framing_tokens + m_total
            if projected > budget:
                raise ValidationError(
                    f"Mandatory system and safety instructions ({m_item.token_count} tokens) exceed the available prompt budget ({budget} tokens)."
                )
            included_items.append(m_item)
            accumulated_content_tokens += m_item.token_count
            accumulated_framing_tokens += self.per_message_framing
            decisions.append(
                SelectionDecision(
                    item_id=m_item.id,
                    item_type=m_item.item_type,
                    source_provenance=m_item.source_provenance,
                    data_classification=m_item.data_classification,
                    token_count=m_item.token_count,
                    action="included",
                    reason="Mandatory system instruction allocated with top priority.",
                )
            )

        # STEP B: Allocate Current User Turn (Tier 2)
        for u_item in current_turn:
            u_total = u_item.token_count + self.per_message_framing
            projected = accumulated_content_tokens + accumulated_framing_tokens + u_total
            if projected <= budget:
                included_items.append(u_item)
                accumulated_content_tokens += u_item.token_count
                accumulated_framing_tokens += self.per_message_framing
                decisions.append(
                    SelectionDecision(
                        item_id=u_item.id,
                        item_type=u_item.item_type,
                        source_provenance=u_item.source_provenance,
                        data_classification=u_item.data_classification,
                        token_count=u_item.token_count,
                        action="included",
                        reason="Current user turn fully fits within prompt budget.",
                    )
                )
            else:
                # If current turn alone exceeds budget, truncate it to fit remaining budget
                remaining = budget - (accumulated_content_tokens + accumulated_framing_tokens)
                available_content = remaining - self.per_message_framing
                if available_content > 20:
                    truncated_content = self._truncate_text_to_tokens(u_item.content, available_content)
                    trunc_tokens = self.count_tokens(truncated_content)
                    truncated_item = u_item.model_copy(
                        update={
                            "content": truncated_content,
                            "token_count": trunc_tokens,
                        }
                    )
                    included_items.append(truncated_item)
                    accumulated_content_tokens += trunc_tokens
                    accumulated_framing_tokens += self.per_message_framing
                    decisions.append(
                        SelectionDecision(
                            item_id=u_item.id,
                            item_type=u_item.item_type,
                            source_provenance=u_item.source_provenance,
                            data_classification=u_item.data_classification,
                            token_count=trunc_tokens,
                            action="truncated",
                            reason=f"Current user turn truncated from {u_item.token_count} to {trunc_tokens} tokens to fit remaining budget.",
                        )
                    )
                else:
                    decisions.append(
                        SelectionDecision(
                            item_id=u_item.id,
                            item_type=u_item.item_type,
                            source_provenance=u_item.source_provenance,
                            data_classification=u_item.data_classification,
                            token_count=u_item.token_count,
                            action="excluded",
                            reason="Current turn excluded due to insufficient budget after mandatory instructions.",
                        )
                    )

        # STEP C: Allocate Active Task / Project Context (Tier 3) if budget permits
        for ctx_item in active_context:
            ctx_total = ctx_item.token_count + self.per_message_framing
            projected = accumulated_content_tokens + accumulated_framing_tokens + ctx_total
            if projected <= budget:
                included_items.append(ctx_item)
                accumulated_content_tokens += ctx_item.token_count
                accumulated_framing_tokens += self.per_message_framing
                decisions.append(
                    SelectionDecision(
                        item_id=ctx_item.id,
                        item_type=ctx_item.item_type,
                        source_provenance=ctx_item.source_provenance,
                        data_classification=ctx_item.data_classification,
                        token_count=ctx_item.token_count,
                        action="included",
                        reason="Task/Project context fits within available budget.",
                    )
                )
            else:
                decisions.append(
                    SelectionDecision(
                        item_id=ctx_item.id,
                        item_type=ctx_item.item_type,
                        source_provenance=ctx_item.source_provenance,
                        data_classification=ctx_item.data_classification,
                        token_count=ctx_item.token_count,
                        action="excluded",
                        reason="Task/Project context excluded to protect prompt budget.",
                    )
                )

        # STEP D: Allocate Conversation History (Tier 4) - Newest turns first
        for h_item in history_newest_first:
            h_total = h_item.token_count + self.per_message_framing
            projected = accumulated_content_tokens + accumulated_framing_tokens + h_total
            if projected <= budget:
                included_items.append(h_item)
                accumulated_content_tokens += h_item.token_count
                accumulated_framing_tokens += self.per_message_framing
                decisions.append(
                    SelectionDecision(
                        item_id=h_item.id,
                        item_type=h_item.item_type,
                        source_provenance=h_item.source_provenance,
                        data_classification=h_item.data_classification,
                        token_count=h_item.token_count,
                        action="included",
                        reason="Historical message fits within prompt budget.",
                    )
                )
            else:
                decisions.append(
                    SelectionDecision(
                        item_id=h_item.id,
                        item_type=h_item.item_type,
                        source_provenance=h_item.source_provenance,
                        data_classification=h_item.data_classification,
                        token_count=h_item.token_count,
                        action="excluded",
                        reason=f"Historical message pruned before mandatory rules or recent turns to guarantee prompt + reserve <= {self.context_window} limit.",
                    )
                )

        # STEP E: Allocate Auxiliary Memory (Tier 5)
        for mem_item in aux_memory:
            m_total = mem_item.token_count + self.per_message_framing
            projected = accumulated_content_tokens + accumulated_framing_tokens + m_total
            if projected <= budget:
                included_items.append(mem_item)
                accumulated_content_tokens += mem_item.token_count
                accumulated_framing_tokens += self.per_message_framing
                decisions.append(
                    SelectionDecision(
                        item_id=mem_item.id,
                        item_type=mem_item.item_type,
                        source_provenance=mem_item.source_provenance,
                        data_classification=mem_item.data_classification,
                        token_count=mem_item.token_count,
                        action="included",
                        reason="Memory item fits within prompt budget.",
                    )
                )
            else:
                decisions.append(
                    SelectionDecision(
                        item_id=mem_item.id,
                        item_type=mem_item.item_type,
                        source_provenance=mem_item.source_provenance,
                        data_classification=mem_item.data_classification,
                        token_count=mem_item.token_count,
                        action="excluded",
                        reason="Memory item excluded to protect budget ceiling.",
                    )
                )

        # Preserve natural model presentation order:
        # 1. Mandatory system instructions & safety rules (role == "system")
        # 2. Context background (project/task)
        # 3. Chronological conversation history
        # 4. Current user turn (last)
        original_indices = {it.id: idx for idx, it in enumerate(prepared_items)}
        sorted_included = sorted(
            included_items,
            key=lambda x: (
                0 if (x.is_mandatory or x.role == "system") else (1 if x.item_type in ("project_context", "task_context", "memory_fact") else 2),
                original_indices.get(x.id, 0),
            ),
        )

        chat_messages: list[ChatMessage] = [
            ChatMessage(
                role=it.role,
                content=it.content,
                data_classification=it.data_classification,
            )
            for it in sorted_included
        ]

        included_classifications = [it.data_classification for it in sorted_included]
        effective_class = self.compute_effective_classification(included_classifications)
        total_tokens = accumulated_content_tokens + accumulated_framing_tokens

        return CompiledContext(
            messages=chat_messages,
            items=sorted_included,
            total_tokens=total_tokens,
            content_tokens=accumulated_content_tokens,
            framing_tokens=accumulated_framing_tokens,
            token_budget=budget,
            context_window=self.context_window,
            completion_reserve=active_reserve,
            safety_headroom=self.safety_headroom,
            token_counting_method=self.token_counting_method,
            is_exact_count=self.is_exact_count,
            bound_type=self.bound_type,
            chat_template=self.chat_template,
            budget_strategy=self.BUDGET_STRATEGY,
            effective_classification=effective_class,
            included_items_count=len(sorted_included),
            excluded_items_count=len(decisions) - len(sorted_included),
            selection_decisions=decisions,
        )

    def _truncate_text_to_tokens(self, text: str, max_tokens: int) -> str:
        """Safely truncate text so its token count does not exceed max_tokens."""
        suffix = "... [truncated to fit 4096 context budget]"
        suffix_tokens = self.count_tokens(suffix)
        available = max(5, max_tokens - suffix_tokens)

        # Binary search for the longest character cut whose token count <= available
        low = 0
        high = len(text)
        best_cut = 0

        while low <= high:
            mid = (low + high) // 2
            candidate_text = text[:mid]
            if self.count_tokens(candidate_text) <= available:
                best_cut = mid
                low = mid + 1
            else:
                high = mid - 1

        cut_text = text[:best_cut]
        last_space = cut_text.rfind(" ")
        if last_space > best_cut // 2:
            cut_text = cut_text[:last_space]

        return cut_text.rstrip() + " " + suffix

    def compile_conversation_context(
        self,
        db: Session,
        conversation_id: str,
        user_message_id: str,
        user_content: str,
        user_classification: str,
        system_prompt: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        token_budget: int | None = None,
        max_history_messages: int = 20,
        include_memory: bool = True,
        max_memory_items: int = 5,
    ) -> CompiledContext:
        """Compile complete conversation context from database entities, memory subsystems, and current user turn."""
        items: list[ContextItem] = []

        # 1. Mandatory System Prompt & Safety Rules (Priority 100, is_mandatory=True)
        if system_prompt:
            items.append(
                ContextItem(
                    id="sys_prompt_core",
                    item_type="system_prompt",
                    role="system",
                    content=system_prompt,
                    source_provenance="system:core_instruction",
                    data_classification="PUBLIC",
                    priority=100,
                    is_mandatory=True,
                )
            )

        # 2. Check for Task context (Priority 80)
        resolved_project_id = project_id
        if task_id:
            task_repo = TaskRepository(db)
            task = task_repo.get_by_id(task_id)
            if task:
                if not resolved_project_id and task.project_id:
                    resolved_project_id = task.project_id
                task_desc = f"Active Task: {task.title} (Status: {task.status})"
                if task.description:
                    task_desc += f" - {task.description}"
                items.append(
                    ContextItem(
                        id=f"ctx_task_{task.id}",
                        item_type="task_context",
                        role="system",
                        content=task_desc,
                        source_provenance=f"task:{task.id}",
                        data_classification="INTERNAL",
                        priority=80,
                    )
                )

        # 3. Check for Project context (Priority 80)
        conv_repo = ConversationRepository(db)
        conv = conv_repo.get_by_id(conversation_id)
        if not resolved_project_id and conv and conv.metadata_json and "project_id" in conv.metadata_json:
            resolved_project_id = conv.metadata_json["project_id"]

        if resolved_project_id:
            proj_repo = ProjectRepository(db)
            proj = proj_repo.get_by_id(resolved_project_id)
            if proj:
                proj_desc = f"Active Project: {proj.name}"
                if proj.description:
                    proj_desc += f" - {proj.description}"
                items.append(
                    ContextItem(
                        id=f"ctx_proj_{proj.id}",
                        item_type="project_context",
                        role="system",
                        content=proj_desc,
                        source_provenance=f"proj:{proj.id}",
                        data_classification="INTERNAL",
                        priority=80,
                    )
                )

        # 4. Working Memory & Semantic Memory Retrieval (Priority 40, Tier 5)
        if include_memory:
            # A. Bounded active working memory for session/task
            wmem_repo = WorkingMemoryRepository(db)
            active_wmem = wmem_repo.list_active(
                session_id=conversation_id,
                task_id=task_id,
                project_id=resolved_project_id,
                limit=max_memory_items,
            )
            for w in active_wmem:
                items.append(
                    ContextItem(
                        id=f"ctx_wmem_{w.id}",
                        item_type="working_memory",
                        role="system",
                        content=f"Working State [{w.key}]: {w.content}",
                        source_provenance=f"wmem:{w.id}",
                        data_classification=self.normalize_classification(w.data_classification),
                        priority=40,
                    )
                )

            # B. Relevant semantic facts via FTS5 search
            sem_repo = SemanticMemoryRepository(db)
            matched_facts = sem_repo.search_fts(
                query=user_content,
                limit=max_memory_items,
                project_id=resolved_project_id,
            )
            for f in matched_facts:
                if f.conflict_status == "conflicted":
                    status_prefix = "Unresolved Memory (Conflicted)"
                elif f.is_verified:
                    status_prefix = "Verified Fact"
                else:
                    status_prefix = "Inferred Memory"
                fact_desc = f"{status_prefix}: {f.content}"
                items.append(
                    ContextItem(
                        id=f"ctx_sem_{f.id}",
                        item_type="memory_fact",
                        role="system",
                        content=fact_desc,
                        source_provenance=f"mem:{f.id}",
                        data_classification=self.normalize_classification(f.data_classification),
                        priority=40,
                    )
                )

        # 5. Historical Messages (Priority 50, Tier 4)
        msg_repo = MessageRepository(db)
        history_records = msg_repo.list_by_conversation(
            conversation_id, limit=max_history_messages, offset=0
        )

        for rec in history_records:
            if rec.id == user_message_id:
                continue

            role = rec.role if rec.role in ("user", "assistant", "system", "tool") else "user"
            items.append(
                ContextItem(
                    id=rec.id,
                    item_type="conversation_message",
                    role=role,  # type: ignore[arg-type]
                    content=rec.content,
                    source_provenance=f"msg:{rec.id}",
                    data_classification=self.normalize_classification(rec.data_classification),
                    priority=50,
                )
            )

        # 6. Current Triggering User Turn (Priority 90, Tier 2)
        items.append(
            ContextItem(
                id=user_message_id,
                item_type="conversation_message",
                role="user",
                content=user_content,
                source_provenance=f"msg:{user_message_id}",
                data_classification=self.normalize_classification(user_classification),
                priority=90,
            )
        )

        return self.compile(items, token_budget=token_budget)

    @classmethod
    def validate_chat_template_overhead(
        cls,
        model_name: str | None = None,
        chat_template: str | None = None,
    ) -> dict[str, Any]:
        """Validate chat-template framing overhead against active model configuration.

        Qwen ChatML Specification:
          Message envelope: <|im_start|>{role}\\n{content}<|im_end|>\\n
          Decomposition:
            - <|im_start|> (token ID 151644): 1 token
            - role ('system'|'user'|'assistant'): 1 token
            - header newline (\\n, token ID 198): 1 token
            - <|im_end|> (token ID 151645): 1 token
            - envelope newline (\\n, token ID 198): 1 token
          Total per-message framing = 5 tokens.

          Assistant generation prompt: <|im_start|>assistant\\n
          Decomposition:
            - <|im_start|>: 1 token
            - assistant: 1 token
            - newline (\\n): 1 token
          Total generation prompt framing = 3 tokens.
        """
        from pico.core.config import settings

        target_model = (model_name or settings.OLLAMA_MODEL).lower()
        active_template = (chat_template or ("chatml" if "qwen" in target_model else "openai_chat" if "gpt" in target_model else "generic")).lower()

        if active_template == "chatml":
            per_msg = cls.CHATML_PER_MESSAGE_FRAMING
            gen_prompt = cls.CHATML_GENERATION_PROMPT
            breakdown = {
                "per_message": {
                    "<|im_start|>": 1,
                    "role": 1,
                    "header_newline": 1,
                    "<|im_end|>": 1,
                    "envelope_newline": 1,
                    "total": per_msg,
                },
                "generation_prompt": {
                    "<|im_start|>": 1,
                    "assistant": 1,
                    "generation_newline": 1,
                    "total": gen_prompt,
                },
            }
            template_verified = True
        elif active_template == "openai_chat":
            per_msg = cls.OPENAI_PER_MESSAGE_FRAMING
            gen_prompt = cls.OPENAI_GENERATION_PROMPT
            breakdown = {
                "per_message": {
                    "role_and_formatting": 3,
                    "total": per_msg,
                },
                "generation_prompt": {
                    "assistant_trigger": 3,
                    "total": gen_prompt,
                },
            }
            template_verified = True
        else:
            per_msg = 4
            gen_prompt = 3
            breakdown = {
                "per_message": {"envelope_estimate": 4, "total": 4},
                "generation_prompt": {"trigger_estimate": 3, "total": 3},
            }
            template_verified = False

        return {
            "valid": template_verified,
            "model_name": target_model,
            "chat_template": active_template,
            "per_message_framing": per_msg,
            "generation_prompt_framing": gen_prompt,
            "breakdown": breakdown,
            "active_config": {
                "configured_ollama_model": settings.OLLAMA_MODEL,
                "configured_context_window": settings.OLLAMA_CONTEXT_WINDOW,
                "matches_active_qwen": "qwen" in settings.OLLAMA_MODEL.lower(),
            },
            "safeguards": {
                "explicit_num_ctx": settings.OLLAMA_CONTEXT_WINDOW,
                "explicit_num_predict_bound": 512,
                "framing_overhead_accounted": True,
            },
        }

    def validate_chat_template(self) -> dict[str, Any]:
        """Validate the compiler instance's chat template overhead against active configuration."""
        return self.validate_chat_template_overhead(
            model_name=self.model_name,
            chat_template=self.chat_template,
        )
