"""Autonomous Conversational Task Decomposition & Workflow Synthesis Service (Milestone P11.1).

Coordinates context compilation, classification-aware routing, benchmark gate enforcement,
structured model scheduling, and graph validation for workflow proposals.
Strict execution boundary: Synthesis produces a reviewable proposal preview only; it never
creates WorkflowRuns, dispatches tools, or grants approvals.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from pico.capabilities import CapabilityRegistry, registry as default_registry
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.core.id_gen import generate_id
from pico.providers.base import ModelProvider
from pico.providers.types import GenerationOptions
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
from pico.repositories.routing_decision_repo import RoutingDecisionRepository
from pico.scheduler import (
    ResourceScheduler,
    PriorityClass,
    SchedulingRequest,
    WorkerRole,
    get_scheduler,
)
from pico.schemas.workflow import StepDefinition, WorkflowCreate
from pico.schemas.workflow_synthesis import (
    SynthesizedStepDetail,
    WorkflowSynthesisPlan,
    WorkflowSynthesizeRequest,
    WorkflowSynthesizeResponse,
)
from pico.services.context_compiler import ContextCompiler
from pico.services.routing_service import RoutingService
from pico.workflow.synthesis_gate import WorkflowSynthesisGate
from pico.workflow.validator import validate_workflow_graph

logger = logging.getLogger(__name__)


class WorkflowSynthesisService:
    """Service orchestrating conversational task decomposition into validated workflow proposals."""

    def __init__(
        self,
        db: Session,
        scheduler: ResourceScheduler | None = None,
        gate: WorkflowSynthesisGate | None = None,
        capability_registry: CapabilityRegistry | None = None,
        context_compiler: ContextCompiler | None = None,
        routing_service: RoutingService | None = None,
    ) -> None:
        self.db = db
        self.scheduler = scheduler or get_scheduler()
        self.gate = gate or WorkflowSynthesisGate()
        self.capability_registry = capability_registry or default_registry
        self.context_compiler = context_compiler or ContextCompiler()
        self.routing_service = routing_service or RoutingService()
        self.routing_decision_repo = RoutingDecisionRepository(db)

    def _build_system_prompt(self) -> str:
        """Construct prompt with bounded capability catalog and compact JSON contract."""
        catalog_lines = [f"- {c.metadata.capability_id}: {c.metadata.name}" for c in self.capability_registry.list()]
        catalog_str = "\n".join(catalog_lines)

        return (
            "You are PICO Personal Intelligence Coordinator's autonomous workflow synthesis engine.\n"
            "Decompose user requests into structured DAG workflow plans using ONLY these registered capabilities:\n"
            f"{catalog_str}\n\n"
            "OUTPUT CONTRACT (JSON only):\n"
            "{\n"
            '  "status": "ready" | "needs_clarification" | "unsupported",\n'
            '  "workflow_name": string | null,\n'
            '  "workflow_description": string | null,\n'
            '  "steps": [\n'
            '    {"step_key": string, "capability_id": string, "capability_version": "1.0.0", "dependencies": [string], "input_template": {}, "explanation": string | null}\n'
            "  ],\n"
            '  "explanation": string | null,\n'
            '  "clarification_question": string | null\n'
            "}\n\n"
            "RULES & CONSTRAINTS:\n"
            "1. If fulfilled by registered capabilities, set status='ready' and specify workflow_name and steps.\n"
            "2. If ambiguous or missing parameters, set status='needs_clarification' and ask clarification_question.\n"
            "3. If unsupported or unsafe, set status='unsupported' and explain in explanation.\n"
            "4. Return strictly valid JSON adhering to the contract above.\n"
        )

    def _parse_synthesis_plan(self, raw_text: str) -> WorkflowSynthesisPlan:
        """Extract and validate the JSON output from model text."""
        cleaned = raw_text.strip()
        # Strip markdown fences if present
        if cleaned.startswith("```"):
            fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
            if fence_match:
                cleaned = fence_match.group(1).strip()

        try:
            data = json.loads(cleaned)
        except Exception as exc:
            logger.warning("Failed to decode model JSON for workflow synthesis: %s (Raw: %s)", exc, raw_text)
            raise ValidationError(f"Model output is not valid JSON: {exc}")

        try:
            return WorkflowSynthesisPlan.model_validate(data)
        except Exception as exc:
            logger.warning("Model output failed WorkflowSynthesisPlan validation: %s", exc)
            raise ValidationError(f"Model output violates workflow synthesis schema: {exc}")

    async def synthesize(
        self,
        request: WorkflowSynthesizeRequest,
        override_provider: ModelProvider | None = None,
        allow_mock: bool = False,
        benchmark_file: str | None = None,
    ) -> WorkflowSynthesizeResponse:
        """Synthesize a reviewable workflow proposal from a conversation user message."""
        # 0. Early configuration check
        if not settings.WORKFLOW_SYNTHESIS_ENABLED:
            raise AuthorizationError(
                "Workflow synthesis is disabled in system configuration (WORKFLOW_SYNTHESIS_ENABLED=False)."
            )

        # 1. Validate conversation and message existence and ownership
        conv_repo = ConversationRepository(self.db)
        conv = conv_repo.get_by_id(request.conversation_id)
        if not conv:
            raise EntityNotFoundError("Conversation", request.conversation_id)

        msg_repo = MessageRepository(self.db)
        msg = msg_repo.get_by_id(request.user_message_id)
        if not msg:
            raise EntityNotFoundError("Message", request.user_message_id)

        if msg.conversation_id != request.conversation_id:
            raise ValidationError(
                f"Message '{request.user_message_id}' does not belong to conversation '{request.conversation_id}'."
            )

        if msg.role != "user":
            raise ValidationError(
                f"Message '{request.user_message_id}' has role '{msg.role}'; only user messages can be synthesized into workflows."
            )

        # 2. Context compilation & classification
        system_prompt = self._build_system_prompt()
        compiled = self.context_compiler.compile_conversation_context(
            db=self.db,
            conversation_id=request.conversation_id,
            user_message_id=request.user_message_id,
            user_content=msg.content,
            user_classification=msg.data_classification or "INTERNAL",
            system_prompt=system_prompt,
        )
        effective_classification = compiled.effective_classification

        # 3. Privacy & routing resolution
        context_classes = [item.data_classification for item in compiled.items]
        decision = self.routing_service.resolve_route(
            task_category="workflow_synthesis",
            data_classification=effective_classification,
            context_classifications=context_classes,
            requested_profile=request.requested_profile,
        )

        # Record routing decision for auditability
        self.routing_decision_repo.create(
            decision_id=decision.decision_id,
            conversation_id=request.conversation_id,
            message_id=request.user_message_id,
            task_category=decision.task_category,
            data_classification=decision.data_classification,
            selected_profile=decision.selected_profile,
            provider=decision.provider,
            model_name=decision.model_name,
            decision_rule=decision.decision_rule,
            cloud_approved=decision.cloud_approved,
            outcome="pending",
            metadata_json={"reason": decision.reason, "fallback_profiles": decision.fallback_profiles},
        )

        # 4. Benchmark Qualification Gate
        effective_allow_mock = allow_mock or (decision.provider == "mock") or (override_provider is not None)
        is_qualified, gate_reason = self.gate.check_readiness(
            selected_profile=decision.selected_profile,
            provider=decision.provider,
            model_name=decision.model_name,
            allow_mock=effective_allow_mock,
            benchmark_file=benchmark_file,
        )
        if not is_qualified:
            raise AuthorizationError(f"Workflow synthesis gate closed: {gate_reason}")

        # 5. Model scheduling
        sched_req = SchedulingRequest(
            request_id=generate_id("sched"),
            role=WorkerRole.PLANNER,
            priority=PriorityClass.WORKFLOW,
            provider=decision.provider,
            model_name=decision.model_name,
            task_category=decision.task_category,
            data_classification=effective_classification,
            prompt_tokens=compiled.total_tokens,
            max_tokens=getattr(settings, "SCHEDULER_COMPLETION_BUDGET_TOKENS", 1024),
            timeout_seconds=settings.OLLAMA_TIMEOUT_SECONDS,
            queue_timeout_seconds=getattr(settings, "SCHEDULER_QUEUE_TIMEOUT_SECONDS", 30.0),
            correlation_id=request.conversation_id,
        )

        options = GenerationOptions(
            temperature=0.0,
            max_tokens=1024,
            json_mode=True,
        )

        raw_text, _ = await self.scheduler.schedule_complete(
            request=sched_req,
            messages=compiled.messages,
            options=options,
            override_provider=override_provider,
        )

        # 6. Parse and evaluate model output
        plan = self._parse_synthesis_plan(raw_text)

        if plan.status == "needs_clarification":
            return WorkflowSynthesizeResponse(
                status="needs_clarification",
                conversation_id=request.conversation_id,
                user_message_id=request.user_message_id,
                workflow_proposal=None,
                step_details=[],
                explanation=plan.explanation,
                clarification_question=plan.clarification_question,
                effective_classification=effective_classification,
                selected_profile=decision.selected_profile,
                provider=decision.provider,
                model_name=decision.model_name,
            )

        if plan.status == "unsupported":
            return WorkflowSynthesizeResponse(
                status="unsupported",
                conversation_id=request.conversation_id,
                user_message_id=request.user_message_id,
                workflow_proposal=None,
                step_details=[],
                explanation=plan.explanation,
                clarification_question=None,
                effective_classification=effective_classification,
                selected_profile=decision.selected_profile,
                provider=decision.provider,
                model_name=decision.model_name,
            )

        # Status is ready
        if not plan.steps:
            raise ValidationError("Model returned status 'ready' but provided zero workflow steps.")

        step_definitions: list[StepDefinition] = []
        for s in plan.steps:
            step_definitions.append(
                StepDefinition(
                    step_key=s.step_key,
                    capability_id=s.capability_id,
                    capability_version=s.capability_version,
                    dependencies=s.dependencies,
                    input_template=s.input_template,
                )
            )

        workflow_proposal = WorkflowCreate(
            name=plan.workflow_name or f"Workflow for msg_{request.user_message_id[-8:]}",
            description=plan.workflow_description or plan.explanation,
            data_classification=effective_classification,
            steps=step_definitions,
        )

        # Comprehensive pre-dispatch graph validation
        validate_workflow_graph(workflow_proposal, capability_registry=self.capability_registry)

        # Derive authoritative server-side step details
        step_details: list[SynthesizedStepDetail] = []
        for s in plan.steps:
            cap = self.capability_registry.get(s.capability_id, version=s.capability_version)
            step_details.append(
                SynthesizedStepDetail(
                    step_key=s.step_key,
                    capability_id=s.capability_id,
                    capability_version=s.capability_version,
                    capability_name=cap.metadata.name,
                    capability_description=cap.metadata.description,
                    dependencies=s.dependencies,
                    input_template=s.input_template,
                    risk_tier=cap.metadata.risk_tier.value,
                    required_permission=cap.metadata.required_permission,
                    consequential=cap.metadata.consequential,
                    requires_approval=cap.metadata.consequential,
                    explanation=s.explanation,
                )
            )

        return WorkflowSynthesizeResponse(
            status="ready",
            conversation_id=request.conversation_id,
            user_message_id=request.user_message_id,
            workflow_proposal=workflow_proposal,
            step_details=step_details,
            explanation=plan.explanation,
            clarification_question=None,
            effective_classification=effective_classification,
            selected_profile=decision.selected_profile,
            provider=decision.provider,
            model_name=decision.model_name,
        )
