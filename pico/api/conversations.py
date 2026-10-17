from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pico.db.session import get_db
from pico.schemas.conversation import (
    ConversationCreate,
    ConversationListResponse,
    ConversationResponse,
)
from pico.schemas.message import (
    MessageCreate,
    MessageListResponse,
    MessageResponse,
)
from pico.services.conversation_service import ConversationService

router = APIRouter(prefix="/api/conversations", tags=["Conversations"])


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create conversation",
)
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
) -> ConversationResponse:
    service = ConversationService(db)
    return service.create_conversation(payload)


@router.get(
    "",
    response_model=ConversationListResponse,
    summary="List conversations",
)
def list_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ConversationListResponse:
    service = ConversationService(db)
    return service.list_conversations(limit=limit, offset=offset)


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="Get conversation by ID",
)
def get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
) -> ConversationResponse:
    service = ConversationService(db)
    return service.get_conversation(conversation_id)


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append message to conversation",
)
def create_message(
    conversation_id: str,
    payload: MessageCreate,
    db: Session = Depends(get_db),
) -> MessageResponse:
    service = ConversationService(db)
    return service.create_message(conversation_id=conversation_id, payload=payload)


@router.get(
    "/{conversation_id}/messages",
    response_model=MessageListResponse,
    summary="List messages for conversation",
)
def list_messages(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> MessageListResponse:
    service = ConversationService(db)
    return service.list_messages(
        conversation_id=conversation_id, limit=limit, offset=offset
    )
