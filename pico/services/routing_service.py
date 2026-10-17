from typing import Any
from pydantic import BaseModel, Field

from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.core.id_gen import generate_id


class StepRoutingDecision(BaseModel):
    decision_id: str
    selected_profile: str
    provider: str
    model_name: str
    decision_rule: str
    reason: str
    data_classification: str
    task_category: str
    cloud_approved: bool
    fallback_profiles: list[str] = Field(default_factory=list)
    estimated_cost_cents: float = 0.0


class RoutingService:
    """Deterministic, policy-driven model router for PICO."""

    VALID_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"}
    CLASSIFICATION_SEVERITY: dict[str, int] = {
        "PUBLIC": 0,
        "INTERNAL": 1,
        "SENSITIVE": 2,
        "SECRET": 3,
    }
    REASONING_CATEGORIES = {"planning", "coding", "verification"}
    ROUTINE_CATEGORIES = {"intent_classification", "entity_extraction", "summarization", "general_chat"}

    def __init__(self) -> None:
        pass

    def normalize_classification(self, classification: str | None) -> str:
        """Enforce invariant: missing or ambiguous classification defaults to private (INTERNAL)."""
        if not classification or not isinstance(classification, str):
            return "INTERNAL"
        upper = classification.strip().upper()
        if upper not in self.VALID_CLASSIFICATIONS:
            return "INTERNAL"
        return upper

    def compute_effective_classification(
        self, classifications: list[str | None] | tuple[str | None, ...] | set[str | None]
    ) -> str:
        """Compute deterministic effective sensitivity across all items in context.

        Invariants:
        - Any unknown, missing, or invalid classification defaults to private ('INTERNAL').
        - Highest sensitivity among items dominates ('SECRET' > 'SENSITIVE' > 'INTERNAL' > 'PUBLIC').
        - Mixed-classification context defaults to the highest severity level and remains local.
        - Returns 'INTERNAL' if empty.
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

    def resolve_route(
        self,
        task_category: str = "general_chat",
        data_classification: str | None = None,
        context_classifications: list[str | None] | tuple[str | None, ...] | None = None,
        requested_profile: str | None = None,
        cloud_approved: bool = False,
    ) -> StepRoutingDecision:
        """Resolve model route based strictly on deterministic policy invariants."""
        decision_id = generate_id("route")

        items_to_evaluate: list[str | None] = []
        if context_classifications:
            items_to_evaluate.extend(context_classifications)
        if data_classification is not None:
            items_to_evaluate.append(data_classification)

        if items_to_evaluate:
            classification = self.compute_effective_classification(items_to_evaluate)
        else:
            classification = self.normalize_classification(data_classification)

        category = task_category.strip().lower() if task_category else "general_chat"

        # Determine local profile and provider
        if settings.DEFAULT_PROVIDER == "mock":
            local_profile = "mock"
            local_provider = "mock"
            local_model = "pico-mock-deterministic"
        else:
            local_profile = "local-small"
            local_provider = "ollama"
            local_model = settings.OLLAMA_MODEL

        # Rule 1: SECRET Data Invariant -> Hard Local Pin
        if classification == "SECRET":
            if requested_profile in ("cloud-strong", "cloud"):
                raise ValidationError(
                    "Data classified as SECRET is strictly prohibited from cloud transmission."
                )
            return StepRoutingDecision(
                decision_id=decision_id,
                selected_profile=local_profile,
                provider=local_provider,
                model_name=local_model,
                decision_rule="RULE_SECRET_LOCAL_PIN",
                reason="SECRET data classification: strictly pinned to local execution.",
                data_classification=classification,
                task_category=category,
                cloud_approved=False,
                fallback_profiles=[],
                estimated_cost_cents=0.0,
            )

        # Rule 2: Ineligible Private Data (INTERNAL / SENSITIVE) -> Local Pin
        # (In Phase 1 Slice 2, cloud eligibility is strictly restricted to PUBLIC data)
        if classification in ("INTERNAL", "SENSITIVE"):
            if requested_profile in ("cloud-strong", "cloud"):
                raise ValidationError(
                    f"Data classification '{classification}' is not eligible for cloud processing in this policy slice."
                )
            return StepRoutingDecision(
                decision_id=decision_id,
                selected_profile=local_profile,
                provider=local_provider,
                model_name=local_model,
                decision_rule="RULE_PRIVATE_LOCAL_PIN",
                reason=f"Data classification '{classification}' is private and pinned to local execution.",
                data_classification=classification,
                task_category=category,
                cloud_approved=False,
                fallback_profiles=[],
                estimated_cost_cents=0.0,
            )

        # Rule 3: PUBLIC Data -> Evaluate Cloud Routing Eligibility
        # Request is eligible for cloud IF cloud is enabled AND (requested or complex reasoning)
        is_reasoning_task = category in self.REASONING_CATEGORIES
        wants_cloud = requested_profile in ("cloud-strong", "cloud") or is_reasoning_task

        if wants_cloud and settings.CLOUD_ENABLED:
            cloud_model = settings.CLOUD_MODEL or "gpt-4o-mini"
            return StepRoutingDecision(
                decision_id=decision_id,
                selected_profile="cloud-strong",
                provider="cloud",
                model_name=cloud_model,
                decision_rule="RULE_CLOUD_ELIGIBLE_APPROVED",
                reason="PUBLIC data classification and cloud access explicitly enabled.",
                data_classification=classification,
                task_category=category,
                cloud_approved=True,
                fallback_profiles=[local_profile],
                estimated_cost_cents=0.05,
            )

        # Rule 4: PUBLIC Data with Cloud Disabled or Routine Task -> Route to Local
        decision_rule = (
            "RULE_CLOUD_DISABLED_FALLBACK"
            if wants_cloud
            else "RULE_ROUTINE_LOCAL"
        )
        reason = (
            "Cloud access is disabled by policy; routed to local execution."
            if wants_cloud
            else "Routine task with public classification: routed to local execution."
        )

        return StepRoutingDecision(
            decision_id=decision_id,
            selected_profile=local_profile,
            provider=local_provider,
            model_name=local_model,
            decision_rule=decision_rule,
            reason=reason,
            data_classification=classification,
            task_category=category,
            cloud_approved=False,
            fallback_profiles=[],
            estimated_cost_cents=0.0,
        )
