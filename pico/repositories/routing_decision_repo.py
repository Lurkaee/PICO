from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from pico.models.routing_decision import RoutingDecision
from pico.repositories.base import BaseRepository


class RoutingDecisionRepository(BaseRepository[RoutingDecision]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, decision_id: str) -> RoutingDecision | None:
        stmt = select(RoutingDecision).where(RoutingDecision.id == decision_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_by_conversation(
        self,
        conversation_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RoutingDecision]:
        stmt = (
            select(RoutingDecision)
            .where(RoutingDecision.conversation_id == conversation_id)
            .order_by(RoutingDecision.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).scalars().all())

    def create(
        self,
        decision_id: str,
        task_category: str,
        data_classification: str,
        selected_profile: str,
        provider: str,
        model_name: str,
        decision_rule: str,
        conversation_id: str | None = None,
        message_id: str | None = None,
        step_index: int = 0,
        cloud_approved: bool = False,
        fallback_occurred: bool = False,
        outcome: str = "success",
        error_message: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: float | None = None,
        estimated_cost_cents: float | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> RoutingDecision:
        record = RoutingDecision(
            id=decision_id,
            conversation_id=conversation_id,
            message_id=message_id,
            step_index=step_index,
            task_category=task_category,
            data_classification=data_classification,
            selected_profile=selected_profile,
            provider=provider,
            model_name=model_name,
            decision_rule=decision_rule,
            cloud_approved=cloud_approved,
            fallback_occurred=fallback_occurred,
            outcome=outcome,
            error_message=error_message,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            estimated_cost_cents=estimated_cost_cents,
            metadata_json=metadata_json,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def update_outcome(
        self,
        decision_id: str,
        outcome: str,
        message_id: str | None = None,
        error_message: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: float | None = None,
        estimated_cost_cents: float | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> RoutingDecision | None:
        record = self.get_by_id(decision_id)
        if not record:
            return None
        record.outcome = outcome
        if message_id is not None:
            record.message_id = message_id
        if error_message is not None:
            record.error_message = error_message
        if prompt_tokens is not None:
            record.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            record.completion_tokens = completion_tokens
        if latency_ms is not None:
            record.latency_ms = latency_ms
        if estimated_cost_cents is not None:
            record.estimated_cost_cents = estimated_cost_cents
        if metadata_json is not None:
            record.metadata_json = metadata_json
        self.db.commit()
        self.db.refresh(record)
        return record
