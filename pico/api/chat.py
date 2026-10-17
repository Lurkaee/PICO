import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from pico.core.errors import EntityNotFoundError
from pico.core.id_gen import generate_id
from pico.db.session import get_db
from pico.providers.errors import ProviderCancelledError
from pico.providers.factory import ProviderFactory
from pico.providers.types import (
    ChatMessage,
    DoneEvent,
    GenerationOptions,
    MetaEvent,
    TokenEvent,
)
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
from pico.repositories.routing_decision_repo import RoutingDecisionRepository
from pico.schemas.chat import ChatStreamRequest
from pico.services.routing_service import RoutingService, StepRoutingDecision

router = APIRouter(prefix="/api/chat", tags=["Chat"])


def format_sse(event_type: str, data: dict[str, Any]) -> str:
    """Format SSE payload adhering to the text/event-stream standard."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@router.post("/stream", summary="Stream chat inference with per-step routing")
async def chat_stream(
    payload: ChatStreamRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream model response using Server-Sent Events (SSE).

    Workflow:
    1. Validate that the conversation exists.
    2. Persist the user message exactly once.
    3. Resolve the model profile using the deterministic router.
    4. Stream SSE events (meta, token, done, error).
    5. Persist the assistant message and routing decision upon successful completion.
    """
    conv_repo = ConversationRepository(db)
    msg_repo = MessageRepository(db)

    # 1. Validate that conversation exists
    conversation = conv_repo.get_by_id(payload.conversation_id)
    if not conversation:
        raise EntityNotFoundError("Conversation", payload.conversation_id)

    # 2. Normalize classification & persist user message
    routing_service = RoutingService()
    classification = routing_service.normalize_classification(payload.data_classification)

    user_msg_id = generate_id("msg")
    user_message = msg_repo.create(
        message_id=user_msg_id,
        conversation_id=payload.conversation_id,
        role="user",
        content=payload.message,
        data_classification=classification,
    )

    # 3. Resolve routing decision & record initial pending audit decision
    decision: StepRoutingDecision = routing_service.resolve_route(
        task_category=payload.task_category or "general_chat",
        data_classification=classification,
        requested_profile=payload.requested_profile,
    )
    route_repo = RoutingDecisionRepository(db)
    route_repo.create(
        decision_id=decision.decision_id,
        conversation_id=payload.conversation_id,
        message_id=user_msg_id,
        step_index=0,
        task_category=decision.task_category,
        data_classification=decision.data_classification,
        selected_profile=decision.selected_profile,
        provider=decision.provider,
        model_name=decision.model_name,
        decision_rule=decision.decision_rule,
        cloud_approved=decision.cloud_approved,
        outcome="pending",
    )

    # 4. Fetch recent conversation context for prompt assembly
    history_records = msg_repo.list_by_conversation(
        payload.conversation_id, limit=20, offset=0
    )
    chat_messages: list[ChatMessage] = [
        ChatMessage(
            role=rec.role,  # type: ignore[arg-type]
            content=rec.content,
            data_classification=rec.data_classification,
        )
        for rec in history_records
    ]

    # Explicit options for generation
    options = GenerationOptions(
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        stream=True,
    )

    # Factory bound to the current database engine
    stream_session_factory = sessionmaker(
        bind=db.get_bind(),
        autocommit=False,
        autoflush=False,
    )

    # Check for test/app-level provider override
    override_provider = getattr(request.app.state, "provider_override", None)

    async def sse_event_generator() -> AsyncIterator[str]:
        accumulated_chunks: list[str] = []
        assistant_msg_id: str | None = None

        try:
            # Emit meta event
            meta_data = {
                "decision_id": decision.decision_id,
                "provider": decision.provider,
                "model": decision.model_name,
                "data_classification": decision.data_classification,
                "selected_profile": decision.selected_profile,
                "decision_rule": decision.decision_rule,
            }
            yield format_sse("meta", meta_data)

            provider = ProviderFactory.get_provider(
                provider_name=decision.provider,
                model_name=decision.model_name,
                override_instance=override_provider,
            )

            async for event in provider.stream_chat(chat_messages, options):
                # Check for client disconnect
                if await request.is_disconnected():
                    raise asyncio.CancelledError()

                if isinstance(event, MetaEvent):
                    continue  # already emitted meta header
                elif isinstance(event, TokenEvent):
                    accumulated_chunks.append(event.delta)
                    yield format_sse("token", {"delta": event.delta})
                elif isinstance(event, DoneEvent):
                    full_response = "".join(accumulated_chunks)

                    # Persist assistant response and update routing decision in dedicated short session
                    write_db = stream_session_factory()
                    try:
                        write_msg_repo = MessageRepository(write_db)
                        write_route_repo = RoutingDecisionRepository(write_db)

                        assistant_msg_id = generate_id("msg")
                        write_msg_repo.create(
                            message_id=assistant_msg_id,
                            conversation_id=payload.conversation_id,
                            role="assistant",
                            content=full_response,
                            data_classification=classification,
                            token_count=event.completion_tokens,
                        )

                        updated_record = write_route_repo.update_outcome(
                            decision_id=decision.decision_id,
                            outcome="success",
                            message_id=assistant_msg_id,
                            prompt_tokens=event.prompt_tokens,
                            completion_tokens=event.completion_tokens,
                            latency_ms=event.latency_ms,
                            estimated_cost_cents=decision.estimated_cost_cents,
                        )
                        if not updated_record:
                            write_route_repo.create(
                                decision_id=decision.decision_id,
                                conversation_id=payload.conversation_id,
                                message_id=assistant_msg_id,
                                task_category=decision.task_category,
                                data_classification=decision.data_classification,
                                selected_profile=decision.selected_profile,
                                provider=decision.provider,
                                model_name=decision.model_name,
                                decision_rule=decision.decision_rule,
                                cloud_approved=decision.cloud_approved,
                                outcome="success",
                                prompt_tokens=event.prompt_tokens,
                                completion_tokens=event.completion_tokens,
                                latency_ms=event.latency_ms,
                                estimated_cost_cents=decision.estimated_cost_cents,
                            )
                    finally:
                        write_db.close()

                    done_payload = {
                        "message_id": assistant_msg_id,
                        "finish_reason": event.finish_reason,
                        "prompt_tokens": event.prompt_tokens,
                        "completion_tokens": event.completion_tokens,
                        "total_tokens": event.total_tokens,
                        "latency_ms": event.latency_ms,
                    }
                    yield format_sse("done", done_payload)
                    return

        except (asyncio.CancelledError, ProviderCancelledError):
            # Client disconnected: update decision to cancelled without saving assistant message
            write_db = stream_session_factory()
            try:
                write_route_repo = RoutingDecisionRepository(write_db)
                updated_record = write_route_repo.update_outcome(
                    decision_id=decision.decision_id,
                    outcome="cancelled",
                    error_message="Client disconnected during streaming.",
                )
                if not updated_record:
                    write_route_repo.create(
                        decision_id=decision.decision_id,
                        conversation_id=payload.conversation_id,
                        message_id=None,
                        task_category=decision.task_category,
                        data_classification=decision.data_classification,
                        selected_profile=decision.selected_profile,
                        provider=decision.provider,
                        model_name=decision.model_name,
                        decision_rule=decision.decision_rule,
                        cloud_approved=decision.cloud_approved,
                        outcome="cancelled",
                        error_message="Client disconnected during streaming.",
                    )
            finally:
                write_db.close()
            raise

        except Exception as exc:
            # Provider failure: update decision to failed without fabricating assistant message
            err_code = getattr(exc, "code", "INFERENCE_ERROR")
            err_msg = str(exc)

            write_db = stream_session_factory()
            try:
                write_route_repo = RoutingDecisionRepository(write_db)
                updated_record = write_route_repo.update_outcome(
                    decision_id=decision.decision_id,
                    outcome="failed",
                    error_message=err_msg,
                )
                if not updated_record:
                    write_route_repo.create(
                        decision_id=decision.decision_id,
                        conversation_id=payload.conversation_id,
                        message_id=None,
                        task_category=decision.task_category,
                        data_classification=decision.data_classification,
                        selected_profile=decision.selected_profile,
                        provider=decision.provider,
                        model_name=decision.model_name,
                        decision_rule=decision.decision_rule,
                        cloud_approved=decision.cloud_approved,
                        outcome="failed",
                        error_message=err_msg,
                    )
            finally:
                write_db.close()

            yield format_sse("error", {"code": err_code, "message": err_msg})

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
